import hashlib
import hmac
import json

from app.config import settings
from conftest import make_task


def test_agent_and_task_crud(client, agent):
    task = make_task(client, agent, title='Player controller')
    assert task['current_stage'] == 1
    assert task['branch_name'] == f"feat/task-{task['id']}"
    assert task['stage_agents']['1'] == agent['id']
    response = client.get('/api/tasks')
    assert response.json()[0]['title'] == 'Player controller'
    assert (settings.storage_dir / 'tasks' / str(task['id']) / '00_task.md').exists()


def test_dependency_cycle_is_rejected(client, agent):
    first = make_task(client, agent, 'First')
    second = make_task(client, agent, 'Second', [first['id']])
    assert second['status'] == 'blocked_main'
    response = client.put(f"/api/tasks/{first['id']}", json={'title': 'First', 'description': '', 'dependencies': [second['id']], 'stage_agents': {'1': agent['id']}})
    assert response.status_code == 400
    assert 'циклическая' in response.json()['detail']


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
