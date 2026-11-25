import asyncio
import json

import pytest

from app.database import connect
from app.dispatcher import dispatcher, latest_version, limits, task_dir
from app.runner import RunResult
from conftest import make_task


APPROVED = json.dumps({'status': 'APPROVED', 'summary': 'Готово', 'comments': [], 'notes': ['Переименовать поле']})


def current(client, task_id):
    return client.get(f'/api/tasks/{task_id}').json()


def log_lines(task_id):
    with connect() as conn:
        return [row['message'] for row in conn.execute('SELECT message FROM events WHERE task_id=? ORDER BY id', (task_id,))]


@pytest.fixture()
def idle(monkeypatch):
    # Tests drive run_stage themselves; a status change launches nothing.
    async def start(task_id):
        pass

    monkeypatch.setattr('app.main.dispatcher.start', start)


def fake_runner(monkeypatch, outcomes):
    """Replace the CLI runner: every run answers with the next outcome.

    A callable outcome receives the task directory, so a test can create the result file.
    """
    prompts = []

    class FakeRunner:
        def __init__(self, log_callback=None, on_process=None, rate_limit_callback=None):
            pass

        async def run(self, config, prompt, cwd, env, timeout_minutes):
            prompts.append(prompt)
            outcome = outcomes.pop(0)
            if callable(outcome):
                outcome = outcome(cwd)
            if asyncio.iscoroutine(outcome):
                outcome = await outcome
            return outcome

    monkeypatch.setattr('app.dispatcher.ProcessRunner', FakeRunner)
    return prompts


def writes(name, text, **usage):
    def outcome(directory):
        (directory / name).write_text(text, encoding='utf-8')
        return RunResult(0, 'ok', **usage)

    return outcome


def save_limits(client, **values):
    assert client.put('/api/settings', json=values).status_code == 200


def test_missing_result_is_retried_with_what_went_wrong(client, agent, idle, monkeypatch):
    task = make_task(client, agent)
    prompts = fake_runner(monkeypatch, [RunResult(0, 'nothing written'), writes('01_tech_doc_v2.md', '# Doc')])

    asyncio.run(dispatcher.run_stage(task['id'], 1))
    first = current(client, task['id'])
    assert first['status'] == 'queued'
    assert first['auto_retries'] == 1
    assert any('Автоповтор 1 из 2' in line for line in log_lines(task['id']))

    asyncio.run(dispatcher.run_stage(task['id'], 1))
    final = current(client, task['id'])
    assert (final['current_stage'], final['status'], final['auto_retries']) == (2, 'queued', 0)
    assert [run['status'] for run in final['runs']] == ['error', 'completed']
    assert 'Предыдущая попытка' not in prompts[0]
    assert 'Предыдущая попытка этого этапа не завершена: Артефакт 01_tech_doc_v1.md не создан' in prompts[1]


@pytest.mark.parametrize('verdict', ['not json', '{"status": "MAYBE"}', '{"status": "REJECTED", "comments": []}'])
def test_verdict_outside_the_contract_is_retried(client, agent, idle, monkeypatch, verdict):
    task = make_task(client, agent)
    (task_dir(task['id']) / '01_tech_doc_v1.md').write_text('# Doc')
    asyncio.run(dispatcher._process_artifact(task['id'], 1, 1))
    fake_runner(monkeypatch, [writes('02_doc_review_v1.json', verdict), writes('02_doc_review_v2.json', APPROVED)])

    asyncio.run(dispatcher.run_stage(task['id'], 2))
    assert current(client, task['id'])['status'] == 'queued'
    asyncio.run(dispatcher.run_stage(task['id'], 2))

    result = current(client, task['id'])
    assert (result['current_stage'], result['status']) == (3, 'queued')
    failed = result['runs'][-2]
    assert failed['status'] == 'error' and '02_doc_review_v1.json' in failed['error']


def test_retries_stop_at_the_configured_limit(client, agent, idle, monkeypatch):
    save_limits(client, max_auto_retries=1)
    task = make_task(client, agent)
    fake_runner(monkeypatch, [RunResult(1, 'crash'), RunResult(1, 'crash again')])

    asyncio.run(dispatcher.run_stage(task['id'], 1))
    assert current(client, task['id'])['status'] == 'queued'
    asyncio.run(dispatcher.run_stage(task['id'], 1))

    result = current(client, task['id'])
    assert result['status'] == 'error'
    assert result['auto_retries'] == 1
    assert 'CLI завершился с кодом 1' in result['runs'][-1]['error']
    assert 'crash again' in result['runs'][-1]['error']


def test_retries_can_be_switched_off(client, agent, idle, monkeypatch):
    save_limits(client, max_auto_retries=0)
    task = make_task(client, agent)
    fake_runner(monkeypatch, [RunResult(1, 'crash')])

    asyncio.run(dispatcher.run_stage(task['id'], 1))

    assert current(client, task['id'])['status'] == 'error'


@pytest.mark.parametrize('failure', [
    RunResult(-1, '', timed_out=True),
    RunResult(1, 'ERROR: usage limit', rate_limited=True),
    RunResult(127, 'CLI не найден'),
])
def test_timeouts_rate_limits_and_missing_clis_are_not_retried(client, agent, idle, monkeypatch, failure):
    task = make_task(client, agent)
    fake_runner(monkeypatch, [failure])

    asyncio.run(dispatcher.run_stage(task['id'], 1))

    result = current(client, task['id'])
    assert result['status'] == 'error'
    assert len(result['runs']) == 1


def test_fallback_runs_only_after_a_failed_run(client, agent, idle, monkeypatch):
    backup = {'cli': 'custom', 'model': 'backup', 'environment': 'server', 'command': 'python backup.py'}
    assert client.put(f"/api/agents/{agent['id']}", json={**agent, 'fallback_configs': [backup]}).status_code == 200

    # A result that is there wins over anything the output says about limits.
    task = make_task(client, agent)
    prompts = fake_runner(monkeypatch, [writes('01_tech_doc_v1.md', '# Doc', rate_limited=True)])
    asyncio.run(dispatcher.run_stage(task['id'], 1))
    result = current(client, task['id'])
    assert (result['current_stage'], len(prompts)) == (2, 1)
    assert result['runs'][0]['is_fallback'] == 0

    other = make_task(client, agent, 'With backup')
    prompts = fake_runner(monkeypatch, [RunResult(1, 'crash'), writes('01_tech_doc_v1.md', '# Doc')])
    asyncio.run(dispatcher.run_stage(other['id'], 1))
    result = current(client, other['id'])
    assert (result['current_stage'], result['auto_retries'], len(prompts)) == (2, 0, 2)
    assert (result['runs'][0]['is_fallback'], result['runs'][0]['model_used']) == (1, 'backup')


def test_rejected_in_a_row_parks_the_task_on_the_review_stage(client, agent, idle):
    save_limits(client, max_returns=2)
    task = make_task(client, agent)
    directory = task_dir(task['id'])
    for attempt in (1, 2, 3):
        (directory / f'01_tech_doc_v{attempt}.md').write_text('# Doc')
        asyncio.run(dispatcher._process_artifact(task['id'], 1, attempt))
        (directory / f'02_doc_review_v{attempt}.json').write_text(json.dumps({'status': 'REJECTED', 'comments': ['Fix']}))
        asyncio.run(dispatcher._process_artifact(task['id'], 2, attempt))

    result = current(client, task['id'])
    assert (result['current_stage'], result['status']) == (2, 'needs_human')
    assert '3 раз подряд' in result['hold_reason']

    # Continuing without a change only puts the task back in the queue; approval moves it on.
    assert client.post(f"/api/tasks/{task['id']}/start").status_code == 202
    assert current(client, task['id'])['status'] == 'queued'
    assert client.post(f"/api/tasks/{task['id']}/approve?stage=2").status_code == 202
    result = current(client, task['id'])
    assert (result['current_stage'], result['status'], result['hold_reason'], result['doc_returns']) == (3, 'queued', None, 0)


def test_code_returns_park_the_task_until_the_implementation_is_restarted(client, agent, idle):
    save_limits(client, max_returns=1)
    task = make_task(client, agent)
    directory = task_dir(task['id'])
    for attempt in (1, 2):
        (directory / f'03_impl_report_v{attempt}.md').write_text('# Report')
        asyncio.run(dispatcher._process_artifact(task['id'], 3, attempt))
        (directory / f'04_code_review_v{attempt}.json').write_text(json.dumps({'status': 'REJECTED', 'comments': ['Null check']}))
        asyncio.run(dispatcher._process_artifact(task['id'], 4, attempt))

    result = current(client, task['id'])
    assert (result['current_stage'], result['status'], result['code_returns']) == (4, 'needs_human', 2)

    assert client.post(f"/api/tasks/{task['id']}/restart?stage=3").status_code == 202
    result = current(client, task['id'])
    assert (result['current_stage'], result['status'], result['hold_reason'], result['code_returns']) == (3, 'queued', None, 0)


def test_approval_resets_the_return_counter(client, agent, idle):
    task = make_task(client, agent)
    directory = task_dir(task['id'])
    for attempt, verdict in ((1, {'status': 'REJECTED', 'comments': ['Fix']}), (2, json.loads(APPROVED))):
        (directory / f'01_tech_doc_v{attempt}.md').write_text('# Doc')
        asyncio.run(dispatcher._process_artifact(task['id'], 1, attempt))
        (directory / f'02_doc_review_v{attempt}.json').write_text(json.dumps(verdict))
        asyncio.run(dispatcher._process_artifact(task['id'], 2, attempt))

    result = current(client, task['id'])
    assert (result['current_stage'], result['doc_returns']) == (3, 0)


def test_token_budget_parks_the_task_before_the_next_stage(client, agent, idle, monkeypatch):
    save_limits(client, task_token_budget=100)
    task = make_task(client, agent)
    prompts = fake_runner(monkeypatch, [writes('01_tech_doc_v1.md', '# Doc', input_tokens=150), writes('02_doc_review_v1.json', APPROVED)])

    asyncio.run(dispatcher.run_stage(task['id'], 1))
    assert (current(client, task['id'])['current_stage'], current(client, task['id'])['token_budget_effective']) == (2, 100)

    asyncio.run(dispatcher.run_stage(task['id'], 2))
    result = current(client, task['id'])
    assert (result['current_stage'], result['status']) == (2, 'needs_human')
    assert '150 из 100' in result['hold_reason']
    assert (len(prompts), len(result['runs'])) == (1, 1)

    save_limits(client, task_token_budget=1000)
    assert client.post(f"/api/tasks/{task['id']}/start").status_code == 202
    assert current(client, task['id'])['status'] == 'queued'
    asyncio.run(dispatcher.run_stage(task['id'], 2))
    assert current(client, task['id'])['current_stage'] == 3


def test_task_budget_overrides_the_shared_one(client, agent, idle):
    save_limits(client, task_token_budget=100)
    task = make_task(client, agent)
    assert task['token_budget_effective'] == 100
    assert client.get('/api/tasks').json()[0]['token_budget_effective'] == 100

    payload = {'title': 'Task', 'description': 'Description', 'stage_agents': {'1': agent['id']}}
    for value, expected in ((0, 0), (500, 500), (None, 100)):
        response = client.put(f"/api/tasks/{task['id']}", json={**payload, 'token_budget': value})
        assert response.status_code == 200, response.text
        assert response.json()['token_budget_effective'] == expected


def test_a_late_failure_does_not_override_a_manual_decision(client, agent, idle, monkeypatch):
    task = make_task(client, agent)

    async def approved_meanwhile(directory):
        with connect() as conn:
            conn.execute("UPDATE tasks SET current_stage=2, status='queued' WHERE id=?", (task['id'],))
        return RunResult(1, 'crash')

    fake_runner(monkeypatch, [approved_meanwhile])
    asyncio.run(dispatcher.run_stage(task['id'], 1))

    result = current(client, task['id'])
    assert (result['current_stage'], result['status']) == (2, 'queued')
    assert (result['runs'][0]['status'], result['auto_retries']) == ('error', 0)


def test_reviewer_is_asked_for_blockers_and_notes_separately(client, agent, idle):
    task = make_task(client, agent)
    (task_dir(task['id']) / '01_tech_doc_v1.md').write_text('# Doc')

    prompt = dispatcher._prompt(task, agent, 2, 1)

    assert '"notes": string[]' in prompt
    assert 'Ты настроен на одобрение' in prompt
    assert 'При REJECTED comments не может быть пустым' in prompt


def test_settings_expose_limits_as_numbers(client):
    body = client.get('/api/settings').json()
    assert (body['max_auto_retries'], body['max_returns'], body['task_token_budget']) == (2, 3, 0)

    save_limits(client, max_auto_retries=7)
    assert client.get('/api/settings').json()['max_auto_retries'] == 7
    assert limits() == {'max_auto_retries': 7, 'max_returns': 3, 'task_token_budget': 0}


def test_latest_version_orders_revisions_by_number(tmp_path):
    for number in (2, 9, 10):
        (tmp_path / f'01_tech_doc_v{number}.md').write_text('# Doc')
    assert latest_version(tmp_path, '01_tech_doc_v*.md').name == '01_tech_doc_v10.md'
    assert latest_version(tmp_path, '02_doc_review_v*.json') is None
