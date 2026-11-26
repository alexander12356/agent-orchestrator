import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.database import connect, init_db, now
from app.dispatcher import dispatcher
from app.config import settings
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


def test_merge_cleanup_is_replayed_on_worker_connection_until_acknowledged(client, agent, monkeypatch):
    task = make_task(client, agent)
    task_id = task["id"]
    path = f"/worktrees/task-{task_id}"
    with connect() as conn:
        conn.execute("UPDATE tasks SET worktree_path=? WHERE id=?", (path, task_id))

    # A send while offline cannot clear the persisted cleanup request.
    response = client.post(f"/api/tasks/{task_id}/merge", json={})
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {settings.worker_token}"}
    with client.websocket_connect("/ws/worker", headers=headers) as socket:
        message = socket.receive_json()
        assert message == {
            "type": "CLEANUP_WORKTREE", "task_id": task_id,
            "branch_name": task["branch_name"], "worktree_path": path,
        }
    with client.websocket_connect("/ws/worker", headers=headers) as socket:
        assert socket.receive_json() == message
        socket.send_json({**message, "type": "WORKTREE_CLEANED"})
        socket.send_json({"type": "HEARTBEAT"})
        assert socket.receive_json()["type"] == "HEARTBEAT"

    assert client.get(f"/api/tasks/{task_id}").json()["worktree_path"] is None
    send = AsyncMock()
    monkeypatch.setattr("app.dispatcher.hub.send", send)
    assert client.post(f"/api/tasks/{task_id}/merge", json={}).status_code == 200
    send.assert_not_awaited()


def test_worker_assignment_contains_task_context_for_the_pr(client, agent, monkeypatch):
    task = make_task(client, agent, 'Сохранение прогресса')
    task.update({
        'external_issue_key': 'ZEL-42',
        'external_issue_url': 'https://tracker.example/ZEL-42',
    })
    sent = []

    async def send(message):
        sent.append(message)
        return True

    monkeypatch.setattr('app.dispatcher.hub', SimpleNamespace(send=send))
    asyncio.run(dispatcher._run_worker(task, agent, 1, 1))
    assert sent[0]['task_title'] == 'Сохранение прогресса'
    assert sent[0]['task_description'] == 'Description'
    assert sent[0]['external_issue_key'] == 'ZEL-42'
    assert sent[0]['external_issue_url'] == 'https://tracker.example/ZEL-42'
