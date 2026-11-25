from types import SimpleNamespace

from app.database import connect, init_db, now
from conftest import make_task


def test_imported_task_branch_is_named_after_issue_key(client, monkeypatch):
    assert client.put('/api/settings', json={'yougile_project': 'Zelya', 'yougile_board': 'Sprint 1'}).status_code == 200

    async def fetch(project, board, sticker):
        return [
            {'id': 'issue-23', 'key': 'ZEL-23', 'title': 'Keyed', 'description': '', 'chat': ''},
            {'id': 'issue-24', 'key': 'ZEL 24/x', 'title': 'Unkeyed', 'description': '', 'chat': ''},
        ]

    monkeypatch.setattr('app.main.fetch_import_tasks', fetch)
    assert client.post('/api/integrations/yougile/import').status_code == 200

    tasks = client.get('/api/tasks').json()
    assert {task['external_issue_id']: task['branch_name'] for task in tasks} == {
        'issue-23': 'Agent-Orchestrator/ZEL-23',
        'issue-24': 'Agent-Orchestrator/task-2',
    }


def test_legacy_branch_is_renamed_on_restart_unless_the_worker_already_ran(client, agent):
    fresh = make_task(client, agent, 'Fresh')
    started = make_task(client, agent, 'Started')
    with connect() as conn:
        conn.execute("UPDATE tasks SET branch_name='feat/task-' || id, external_issue_key='ZEL-' || id")
        conn.execute("INSERT INTO stage_runs(task_id, stage, attempt, status, started_at) VALUES (?, 3, 1, 'completed', ?)", (started['id'], now()))

    init_db()

    names = {task['id']: task['branch_name'] for task in client.get('/api/tasks').json()}
    assert names == {fresh['id']: f'Agent-Orchestrator/ZEL-{fresh["id"]}', started['id']: f"feat/task-{started['id']}"}


def test_merge_cleanup_uses_the_task_branch(client, agent, monkeypatch):
    task = make_task(client, agent)
    with connect() as conn:
        conn.execute("UPDATE tasks SET branch_name='Agent-Orchestrator/ZEL-23', worktree_path='/worktrees/task-1' WHERE id=?", (task['id'],))
    sent = []

    async def send(message):
        sent.append(message)
        return True

    monkeypatch.setattr('app.dispatcher.hub', SimpleNamespace(send=send))
    response = client.post(f"/api/tasks/{task['id']}/merge", json={})
    assert response.status_code == 200
    assert [(message['type'], message['branch_name']) for message in sent] == [('CLEANUP_WORKTREE', 'Agent-Orchestrator/ZEL-23')]
