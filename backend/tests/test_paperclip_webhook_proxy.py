import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import httpx
from fastapi.testclient import TestClient

from app import paperclip_webhook_proxy as proxy
from app.database import connect
from app.main import app
from conftest import make_task


def test_merge_is_forwarded_with_original_signature(client, agent, monkeypatch):
    secret = "paperclip-test-secret"
    assert client.put("/api/settings", json={"git_webhook_secret": secret}).status_code == 200
    hub_send = AsyncMock()
    monkeypatch.setattr("app.dispatcher.hub.send", hub_send)
    task = make_task(client, agent)
    with connect() as conn:
        conn.execute("UPDATE tasks SET worktree_path=? WHERE id=?", ("/worktrees/task-1", task["id"]))
    trigger_id = "1234abcd" * 3
    monkeypatch.setenv("PAPERCLIP_TRIGGER_ID", trigger_id)
    path = f"/api/routine-triggers/public/{trigger_id}/fire"
    head_sha = "a" * 40
    body = json.dumps({
        "action": "closed",
        "pull_request": {"merged": True, "base": {"ref": "main"}, "head": {"ref": task["branch_name"], "sha": head_sha}},
    }, indent=2).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    requests = []
    async_client = httpx.AsyncClient

    async def paperclip(request):
        requests.append(request)
        status = 201 if request.headers["x-hub-signature-256"] == signature else 401
        return httpx.Response(status, json={"paperclip": status})

    def upstream_client(**kwargs):
        return async_client(mounts={
            proxy.PAPERCLIP_URL: httpx.MockTransport(paperclip),
            "http://127.0.0.1:8000": httpx.ASGITransport(app=app),
        }, **kwargs)

    monkeypatch.setattr(proxy.httpx, "AsyncClient", upstream_client)
    headers = {"X-GitHub-Event": "pull_request", "Content-Type": "application/json"}
    with TestClient(proxy.app) as gateway:
        rejected = gateway.post(path, content=body, headers={**headers, "X-Hub-Signature-256": "sha256=invalid"})
        assert rejected.status_code == 401
        assert not client.get(f"/api/tasks/{task['id']}").json()["merged_to_main"]
        accepted = gateway.post(path, content=body, headers={**headers, "X-Hub-Signature-256": signature})

    assert requests[-1].content == body
    assert requests[-1].url.path == path
    assert accepted.status_code == 201
    assert accepted.json() == {"paperclip": 201}
    assert client.get(f"/api/tasks/{task['id']}").json()["merged_to_main"]
    hub_send.assert_awaited_once_with({
        "type": "CLEANUP_WORKTREE",
        "task_id": task["id"],
        "branch_name": task["branch_name"],
        "worktree_path": "/worktrees/task-1",
        "head_sha": head_sha,
    })
