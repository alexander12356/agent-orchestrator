import hashlib
import hmac
import json

from app.config import settings
from app.database import connect, now
from conftest import make_task


def test_agent_and_task_crud(client, agent):
    task = make_task(client, agent, title='Player controller')
    assert task['current_stage'] == 1
    assert task['branch_name'] == f"Agent-Orchestrator/task-{task['id']}"
    assert task['repository_url'] == ''
    response = client.put(f"/api/tasks/{task['id']}", json={'title': 'Player controller', 'description': 'Description', 'repository_url': 'https://github.com/T1mposter/zelya.git', 'stage_agents': {'1': agent['id']}})
    assert response.status_code == 200
    assert response.json()['repository_url'] == 'https://github.com/T1mposter/zelya.git'
    assert task['stage_agents']['1'] == agent['id']
    response = client.get('/api/tasks')
    assert response.json()[0]['title'] == 'Player controller'
    assert (settings.storage_dir / 'tasks' / str(task['id']) / '00_task.md').exists()


def test_worker_reports_model_while_stage_is_running(client):
    response = client.post('/api/tasks', json={'title': 'Worker task', 'stage_agents': {}})
    assert response.status_code == 201
    task = response.json()
    with connect() as conn:
        conn.execute("UPDATE tasks SET current_stage=3, status='running' WHERE id=?", (task['id'],))
        run_id = conn.execute(
            "INSERT INTO stage_runs(task_id, stage, attempt, status, started_at) VALUES (?, 3, 1, 'running', ?)",
            (task['id'], now()),
        ).lastrowid

    with client.websocket_connect('/ws/worker', headers={'Authorization': f'Bearer {settings.worker_token}'}) as worker:
        worker.send_json({
            'type': 'TASK_MODEL', 'task_id': task['id'], 'run_id': run_id,
            'cli_used': 'zcode', 'model_used': 'account:zai/GLM-5.4', 'is_fallback': True,
        })
        worker.send_json({'type': 'HEARTBEAT'})
        assert worker.receive_json()['type'] == 'HEARTBEAT'

    run = client.get(f"/api/tasks/{task['id']}").json()['runs'][0]
    assert (run['status'], run['cli_used'], run['model_used'], run['is_fallback']) == ('running', 'zcode', 'account:zai/GLM-5.4', 1)


def test_task_without_agent_assignment_does_not_get_default_agent(client, agent):
    response = client.post('/api/tasks', json={'title': 'Unassigned', 'description': 'Description', 'stage_agents': {}})

    assert response.status_code == 201
    task = response.json()
    assert task['stage_agents'] == {}
    assert task['current_agent'] is None
    assert task['status'] == 'queued'


def test_dependency_cycle_is_rejected(client, agent):
    first = make_task(client, agent, 'First')
    second = make_task(client, agent, 'Second', [first['id']])
    assert second['status'] == 'blocked_main'
    response = client.put(f"/api/tasks/{first['id']}", json={'title': 'First', 'description': '', 'dependencies': [second['id']], 'stage_agents': {'1': agent['id']}})
    assert response.status_code == 400
    assert 'циклическая' in response.json()['detail']


def test_start_open_task(client, agent, monkeypatch):
    async def start(task_id):
        pass

    monkeypatch.setattr('app.main.dispatcher.start', start)
    task = make_task(client, agent)
    from app.database import connect
    with connect() as conn:
        conn.execute("UPDATE tasks SET status='open' WHERE id=?", (task['id'],))

    response = client.post(f"/api/tasks/{task['id']}/start")

    assert response.status_code == 202
    assert client.get(f"/api/tasks/{task['id']}").json()['status'] == 'queued'
    assert client.post(f"/api/tasks/{task['id']}/start").status_code == 409


def test_manual_merge_unlocks_children(client, agent):
    parent = make_task(client, agent, 'Parent')
    child = make_task(client, agent, 'Child', [parent['id']])
    response = client.post(f"/api/tasks/{parent['id']}/merge", json={'pr_url': 'https://github.test/pr/1'})
    assert response.status_code == 200
    assert client.get(f"/api/tasks/{parent['id']}").json()['status'] == 'done'
    assert client.get(f"/api/tasks/{child['id']}").json()['current_stage'] == 1


def test_signed_git_webhook(client, agent):
    task = make_task(client, agent)
    payload = {'pull_request': {'merged': True, 'base': {'ref': 'main'}, 'head': {'ref': task['branch_name']}, 'html_url': 'https://github.test/pr/2'}}
    body = json.dumps(payload).encode()
    signature = 'sha256=' + hmac.new(settings.git_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    response = client.post('/api/webhooks/git', content=body, headers={'Content-Type': 'application/json', 'X-Hub-Signature-256': signature, 'X-GitHub-Event': 'pull_request'})
    assert response.status_code == 200
    assert response.json()['accepted'] is True
    assert client.get(f"/api/tasks/{task['id']}").json()['merged_to_main'] is True


def test_tracker_answer(client, agent, monkeypatch):
    task = make_task(client, agent)
    from app.database import connect
    with connect() as conn:
        conn.execute("UPDATE tasks SET status='waiting_answer' WHERE id=?", (task['id'],))

    async def start(task_id):
        pass

    monkeypatch.setattr('app.main.dispatcher.start', start)
    response = client.post(f"/api/tasks/{task['id']}/answer", json={'answer': 'Use fixed timestep'})
    assert response.status_code == 202
    assert client.get(f"/api/tasks/{task['id']}").json()['status'] == 'queued'
    assert 'fixed timestep' in (settings.storage_dir / 'tasks' / str(task['id']) / '01_answers_v1.md').read_text()


def test_delete_task_removes_database_record_and_artifacts(client, agent):
    task = make_task(client, agent)
    directory = settings.storage_dir / 'tasks' / str(task['id'])
    (directory / 'artifact.md').write_text('artifact')

    response = client.delete(f"/api/tasks/{task['id']}")

    assert response.status_code == 204
    assert client.get(f"/api/tasks/{task['id']}").status_code == 404
    assert not directory.exists()
