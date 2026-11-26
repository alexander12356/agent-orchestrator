import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location('unity_worker', Path(__file__).with_name('unity_worker.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_parse_structured_usage():
    result = module.extract_usage('{"usage":{"input_tokens":20,"output_tokens":5}}')
    assert result['input_tokens'] == 20
    assert result['estimated'] is False


def test_kimi_implementer_uses_model_and_low_effort(monkeypatch, tmp_path):
    node = tmp_path / "node.exe"
    entry = tmp_path / "main.mjs"
    node.write_text("node", encoding="utf-8")
    entry.write_text("entry", encoding="utf-8")
    monkeypatch.setattr(module, "KIMI_NODE", node)
    monkeypatch.setattr(module, "KIMI_ENTRY", entry)
    monkeypatch.setattr(module.os, "name", "posix")
    command = module.kimi_command({"model": "kimi-code/k3.1-pro", "reasoning_effort": "low"}, "implement")
    assert command[:2] == [str(node), str(entry)]
    assert module.windows_path(Path("/mnt/e/WSL/ZelyaKimiWindows/node_modules/@moonshot-ai/kimi-code/dist/main.mjs")) == "E:\\WSL\\ZelyaKimiWindows\\node_modules\\@moonshot-ai\\kimi-code\\dist\\main.mjs"
    assert command[command.index("-m") + 1] == "kimi-code/k3.1-pro"
    assert command[-2:] == ["-p", "implement"]
    assert module.kimi_environment({"reasoning_effort": "low"})["KIMI_MODEL_THINKING_EFFORT"] == "low"


def test_missing_usage_is_unknown():
    assert module.extract_usage('plain output') == {}


def test_zcode_implementer_runs_a_headless_yolo_prompt(monkeypatch):
    monkeypatch.setattr(module, 'zcode_launcher', lambda: ['zcode'])
    assert module.zcode_command('implement') == ['zcode', '-p', 'implement', '--mode', 'yolo', '--no-color']


def test_zcode_node_launcher_sets_provider_and_credential_paths(monkeypatch, tmp_path):
    node = tmp_path / 'node.exe'
    node.write_text('node', encoding='utf-8')
    entry = tmp_path / 'resources' / 'glm' / 'zcode.cjs'
    entry.parent.mkdir(parents=True)
    entry.write_text('entry', encoding='utf-8')
    monkeypatch.setattr(module, 'zcode_launcher', lambda: [str(node), str(entry)])
    environment = module.zcode_environment()
    assert environment['ZCODE_BUILTIN_PROVIDER_CONFIG_FILE'].endswith('zcode-builtin.json')
    assert environment['ZCODE_CREDENTIALS_FILE'].endswith('credentials.json')
    assert environment['NO_COLOR'] == '1'


def make_worker(tmp_path):
    worker = module.UnityWorker(SimpleNamespace(
        server='', token='', repo=str(tmp_path), worktrees=str(tmp_path / 'worktrees'),
        library=str(tmp_path / 'reference-library'), unity=None,
    ))
    return worker


def test_unity_command_uses_windows_project_and_log_paths(tmp_path):
    worker = make_worker(tmp_path)
    worker.unity = 'Unity.exe'
    command = worker.unity_command(Path('/mnt/d/projects/Project_3/zelya-worktrees/task-63'))
    assert command[command.index('-projectPath') + 1] == 'D:\\projects\\Project_3\\zelya-worktrees\\task-63'
    assert command[command.index('-logFile') + 1] == 'D:\\projects\\Project_3\\zelya-worktrees\\task-63\\unity-build.log'


def test_prepare_library_is_local_to_the_worktree(tmp_path):
    worker = make_worker(tmp_path)
    first = tmp_path / 'worktrees' / 'task-1'
    second = tmp_path / 'worktrees' / 'task-2'
    asyncio.run(worker.prepare_library(first))
    asyncio.run(worker.prepare_library(second))
    assert (first / 'Library').is_dir()
    assert (second / 'Library').is_dir()
    assert not (first / 'Library').is_symlink()
    assert first / 'Library' != second / 'Library'


def test_existing_worktree_is_not_reset(tmp_path):
    worker = make_worker(tmp_path)
    path = tmp_path / 'worktrees' / 'task-1'
    path.mkdir(parents=True)
    marker = path / 'changes.txt'
    marker.write_text('keep', encoding='utf-8')

    async def checked(command, cwd=None):
        if command != ['git', 'fetch', 'origin', 'main']:
            raise AssertionError(f'Неожиданная команда: {command}')
        return ''

    worker.checked = checked
    result = asyncio.run(worker.prepare_worktree(1, 'feat/task-1'))
    assert result == path
    assert marker.read_text(encoding='utf-8') == 'keep'


def test_worktree_is_created_on_issue_key_branch(tmp_path):
    worker = make_worker(tmp_path)
    commands = []

    async def checked(command, cwd=None):
        commands.append(command)
        return ''

    async def execute(command, cwd=None):
        commands.append(command)
        return 1, ''

    worker.checked = checked
    worker.execute = execute
    result = asyncio.run(worker.prepare_worktree(23, 'Agent-Orchestrator/ZEL-23'))
    assert result == worker.worktrees / 'task-23'
    assert commands == [
        ['git', 'fetch', 'origin', 'main'],
        ['git', 'show-ref', '--verify', 'refs/heads/Agent-Orchestrator/ZEL-23'],
        ['git', 'worktree', 'add', '-b', 'Agent-Orchestrator/ZEL-23', str(result), 'origin/main'],
    ]


def test_worktree_rejects_branch_names_outside_the_naming(tmp_path):
    worker = make_worker(tmp_path)

    async def unexpected(command, cwd=None):
        raise AssertionError(f'Неожиданная команда: {command}')

    worker.checked = worker.execute = unexpected
    for branch in ('main', 'codex/zelya-1', 'Agent-Orchestrator/-main', 'Agent-Orchestrator/ZEL-23/extra'):
        with pytest.raises(ValueError):
            asyncio.run(worker.prepare_worktree(23, branch))


def test_terminate_process_escalates_to_kill(tmp_path):
    worker = make_worker(tmp_path)

    class Process:
        returncode = None
        terminated = False
        killed = False

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            while self.returncode is None:
                await asyncio.sleep(0)
            return self.returncode

    process = Process()
    asyncio.run(worker.terminate_process(process, timeout=0.01))
    assert process.terminated
    assert process.killed
    assert process.returncode == -9


def test_cancel_waits_for_worker_command_to_stop(tmp_path, monkeypatch):
    worker = make_worker(tmp_path)

    async def scenario():
        launched = asyncio.Event()
        start_process = worker.start_process
        process = None

        async def record_process(*args, **kwargs):
            nonlocal process
            process = await start_process(*args, **kwargs)
            launched.set()
            return process

        monkeypatch.setattr(worker, "start_process", record_process)
        job = asyncio.create_task(worker.execute([sys.executable, "-c", "import time; time.sleep(60)"]))
        worker.jobs[1] = job
        await asyncio.wait_for(launched.wait(), timeout=5)
        await worker.cancel(1)

        assert job.cancelled()
        assert process is not None and process.returncode is not None

    asyncio.run(scenario())


def test_finish_consumer_cancels_a_stuck_reader(tmp_path):
    worker = make_worker(tmp_path)

    async def scenario():
        consumer = asyncio.create_task(asyncio.Event().wait())
        await worker.finish_consumer(consumer, timeout=0.01)
        assert consumer.cancelled()

    asyncio.run(scenario())


def agent_result(exit_code=0, *, timed_out=False, rate_limited=False, output=''):
    return {
        'exit_code': exit_code, 'output': output, 'rate_limited': rate_limited,
        'timed_out': timed_out, 'input_tokens': 0, 'output_tokens': 0, 'cache_tokens': 0, 'estimated': True,
    }


def fallback_worker(tmp_path, answers):
    worker = make_worker(tmp_path)
    worker.logs = []
    calls = []

    async def run_agent(task_id, path, config, prompt, timeout):
        calls.append(config)
        answer = answers[len(calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def log(task_id, text):
        worker.logs.append(text)

    worker.run_agent = run_agent
    worker.log = log
    return worker, calls


def run_fallback(worker, config, triggers=None):
    triggers = triggers or {'rate_limit': True, 'exit_code': True, 'timeout': True, 'timeout_minutes': 20}
    return asyncio.run(worker.run_with_fallback(1, Path('.'), 'prompt', config, triggers))


KIMI = {'cli': 'kimi', 'model': 'kimi-code/k3.1-pro'}
ZCODE = {'cli': 'zcode', 'model': 'account:zai/GLM-5.4'}


def test_failed_primary_switches_to_fallback(tmp_path):
    worker, calls = fallback_worker(tmp_path, [{**agent_result(1), "input_tokens": 5, "estimated": False}, {**agent_result(0), "input_tokens": 12, "estimated": False}])
    models = []

    async def send(message):
        if message['type'] == 'TASK_MODEL':
            models.append((len(calls), message['model_used'], message['is_fallback']))

    worker.send = send
    selected, result = asyncio.run(worker.run_with_fallback(1, tmp_path, 'prompt', {'primary': KIMI, 'fallback': [ZCODE]}, {}, run_id=4))
    assert [call['cli'] for call in calls] == ['kimi', 'zcode']
    assert models == [(0, KIMI['model'], False), (1, ZCODE['model'], True)]
    assert selected == ZCODE
    assert result['exit_code'] == 0
    assert result['input_tokens'] == 17
    assert any('kimi (kimi-code/k3.1-pro): код возврата 1. Переключаюсь на fallback: zcode (account:zai/GLM-5.4)' in line for line in worker.logs)


def test_unlaunchable_fallback_does_not_abort_the_stage(tmp_path):
    worker, calls = fallback_worker(tmp_path, [ValueError('Для Custom CLI укажите command'), agent_result(0)])
    selected, result = run_fallback(worker, {'primary': KIMI, 'fallback': [ZCODE]})
    assert [call['cli'] for call in calls] == ['kimi', 'zcode']
    assert selected == ZCODE
    assert any('не удалось запустить CLI' in line and 'zcode' in line for line in worker.logs)


def test_all_configs_failed_reports_each_reason(tmp_path):
    worker, _ = fallback_worker(tmp_path, [agent_result(1), ValueError('boom')])
    with pytest.raises(RuntimeError) as error:
        run_fallback(worker, {'primary': KIMI, 'fallback': [ZCODE]})
    message = str(error.value)
    assert 'kimi (kimi-code/k3.1-pro): код возврата 1' in message
    assert 'zcode (account:zai/GLM-5.4): boom' in message


def test_timeout_is_reported_with_the_limit(tmp_path):
    worker, _ = fallback_worker(tmp_path, [agent_result(-15, timed_out=True), agent_result(0)])
    run_fallback(worker, {'primary': KIMI, 'fallback': [ZCODE]}, {'timeout': True, 'timeout_minutes': 7})
    assert any('таймаут 7 мин' in line for line in worker.logs)


def test_limit_is_reported_only_by_error_lines():
    assert module.rate_limited_line("ERROR: You've hit your usage limit.")
    assert module.rate_limited_line('{"type":"error","message":"rate limit exceeded"}')
    assert not module.rate_limited_line('HTTP 429 means the rate limit is exceeded.')
    assert not module.rate_limited_line('{"type":"assistant","message":{"content":[{"type":"text","text":"rate limit"}]}}')


def run_cli(tmp_path, body, messages=None):
    worker = make_worker(tmp_path)
    script = tmp_path / 'cli.py'
    script.write_text('import sys, time\n' + body, encoding='utf-8')
    async def send(message):
        if messages is not None:
            messages.append(message)

    worker.send = send
    config = {'cli': 'custom', 'command': f'"{sys.executable}" "{script}"'}
    return asyncio.run(worker.run_agent(1, tmp_path, config, 'implement', 1))


def test_run_agent_reads_long_lines_and_ignores_limit_words_in_prose(tmp_path):
    messages = []
    result = run_cli(
        tmp_path,
        "from pathlib import Path\n"
        "next(Path.cwd().glob('.orchestrator-progress-*/updates.txt')).write_text('Проверки прошли.\\n', encoding='utf-8')\n"
        "sys.stdout.write('x' * 200_000 + '\\n')\n"
        "sys.stdout.write('HTTP 429 means the rate limit is exceeded.\\n')\n",
        messages,
    )
    assert result['exit_code'] == 0
    assert result['rate_limited'] is False
    assert result['output'] == 'x' * 200_000 + '\nHTTP 429 means the rate limit is exceeded.\n'
    assert messages == [
        {'type': 'LOG_CHUNK', 'task_id': 1, 'chunk': 'Проверки прошли.\n', 'format': 'activity'},
    ]


def test_run_agent_reports_a_limit_of_a_failed_cli(tmp_path):
    result = run_cli(tmp_path, "print(\"You've hit your usage limit. Try again later.\")\nsys.exit(1)\n")
    assert (result['exit_code'], result['rate_limited']) == (1, True)


def test_run_agent_stops_a_cli_that_reports_a_limit(tmp_path):
    result = run_cli(tmp_path, "print('ERROR: rate limit exceeded', flush=True)\ntime.sleep(30)\n")
    assert result['rate_limited'] is True
    assert result['timed_out'] is False
    assert result['exit_code'] != 0


def implementation_details():
    return {
        'purpose': 'Сохранять прогресс после перезапуска игры.',
        'changes': ['Добавлено сохранение прогресса в файл.'],
        'implementation': ['SaveSystem сериализует состояние перед выходом.'],
        'before': 'После перезапуска прогресс терялся.',
        'after': 'Сохранённое состояние восстанавливается при запуске.',
        'validation': ['pytest tests/test_save.py — успешно'],
        'review_focus': ['Проверить обработку повреждённого файла.'],
    }


def test_pr_body_contains_implementation_and_verified_results(tmp_path):
    path = tmp_path / module.PR_DETAILS_FILE
    path.write_text(json.dumps(implementation_details()), encoding='utf-8')
    details = module.read_pr_details(path)
    body = module.format_pr_body(
        details, 42, 'Unity batchmode завершился успешно.', 'SaveSystem.cs | 20 +++',
        'ZEL-42', 'https://tracker.example/ZEL-42',
    )
    for heading in ('## Цель', '## Что сделано', '## Как реализовано', '## Было', '## Стало', '## Проверка', '## Область изменений', '## На что обратить внимание при ревью'):
        assert heading in body
    assert 'Добавлено сохранение прогресса в файл.' in body
    assert 'Unity batchmode завершился успешно.' in body
    assert 'pytest tests/test_save.py — успешно' in body
    assert 'SaveSystem.cs | 20 ++' in body
    assert '[ZEL-42](https://tracker.example/ZEL-42)' in body
    assert 'Задача оркестратора №42' in body


def test_pr_details_require_specific_content_and_omit_empty_review_focus(tmp_path):
    path = tmp_path / module.PR_DETAILS_FILE
    details = implementation_details()
    details['changes'] = []
    path.write_text(json.dumps(details), encoding='utf-8')
    with pytest.raises(RuntimeError, match='changes'):
        module.read_pr_details(path)

    details = implementation_details()
    details['review_focus'] = []
    details['validation'] = []
    body = module.format_pr_body(details, 1, 'Сборка не запускалась.', '')
    assert '## На что обратить внимание при ревью' not in body
    assert 'Нет изменений относительно origin/main.' in body


def test_assign_creates_pr_from_summary_and_keeps_service_files_out_of_commit(tmp_path):
    worker = make_worker(tmp_path)
    path = tmp_path / 'task-worktree'
    path.mkdir()
    commands = []
    prompts = []
    sent = []

    async def prepare_worktree(task_id, branch):
        return path

    async def run_with_fallback(task_id, worktree, prompt, config, triggers, run_id):
        prompts.append(prompt)
        (path / module.PR_DETAILS_FILE).write_text(json.dumps(implementation_details()), encoding='utf-8')
        return KIMI, agent_result(output='raw CLI output')

    async def run_unity(worktree, task_id):
        return 'Unity batchmode завершился успешно.'

    async def execute(command, cwd=None):
        commands.append(command)
        assert command == ['git', 'diff', '--quiet']
        return 1, ''

    async def checked(command, cwd=None):
        commands.append(command)
        if command == ['git', 'add', '-A']:
            assert not (path / 'orchestrator-tech-doc.md').exists()
            assert not (path / module.PR_DETAILS_FILE).exists()
        if command[:3] == ['git', 'diff', '--stat']:
            return 'SaveSystem.cs | 20 +++\n'
        if command[:3] == ['gh', 'pr', 'list']:
            return ''
        if command[:3] == ['gh', 'pr', 'create']:
            return 'https://github.example/pull/42\n'
        return ''

    async def send(message):
        sent.append(message)

    worker.prepare_worktree = prepare_worktree
    worker.run_with_fallback = run_with_fallback
    worker.run_unity = run_unity
    worker.execute = execute
    worker.checked = checked
    worker.send = send
    asyncio.run(worker.assign({
        'task_id': 42,
        'run_id': 7,
        'branch_name': 'Agent-Orchestrator/ZEL-42',
        'task_title': 'Сохранение прогресса',
        'task_description': 'Прогресс должен переживать перезапуск.',
        'external_issue_key': 'ZEL-42',
        'external_issue_url': 'https://tracker.example/ZEL-42',
        'tech_doc_content': '# Технический документ',
        'review_comments_content': '',
        'agent_config': {'primary': KIMI, 'fallback': [], 'triggers': {}, 'role_prompt': 'Unity developer'},
    }))

    create = next(command for command in commands if command[:3] == ['gh', 'pr', 'create'])
    assert create[create.index('--title') + 1] == 'Сохранение прогресса'
    body = create[create.index('--body') + 1]
    assert '## Что сделано' in body
    assert 'SaveSystem сериализует состояние перед выходом.' in body
    assert 'raw CLI output' not in body
    assert 'Прогресс должен переживать перезапуск.' in prompts[0]
    assert module.PR_DETAILS_CONTRACT in prompts[0]
    completed = next(message for message in sent if message['type'] == 'TASK_COMPLETED')
    assert completed['pr_url'] == 'https://github.example/pull/42'
    assert body in completed['report']
    assert completed['pr_url'] in completed['report']
    assert 'raw CLI output' not in completed['report']


def test_existing_pr_is_updated_instead_of_reused_unchanged(tmp_path):
    worker = make_worker(tmp_path)
    commands = []

    async def checked(command, cwd=None):
        commands.append(command)
        if command[:3] == ['gh', 'pr', 'list']:
            return 'https://github.example/pull/42\n'
        return ''

    worker.checked = checked
    url = asyncio.run(worker.upsert_pr(tmp_path, 'Agent-Orchestrator/ZEL-42', 'Title', 'Updated body'))
    assert url == 'https://github.example/pull/42'
    assert [command[:3] for command in commands] == [['gh', 'pr', 'list'], ['gh', 'pr', 'edit']]
    edit = commands[1]
    assert edit[3] == url
    assert edit[edit.index('--body') + 1] == 'Updated body'
