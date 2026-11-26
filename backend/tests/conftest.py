import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def disable_live_telegram(monkeypatch):
    def unexpected_request(*args, **kwargs):
        pytest.fail("Telegram requests in tests must use a local stub")

    monkeypatch.setattr(settings, "telegram_credentials_file", None)
    monkeypatch.setattr("app.telegram.urlopen", unexpected_request)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    async def disable_yougile_background_sync():
        pass

    monkeypatch.setattr("app.main.sync_yougile_moves_forever", disable_yougile_background_sync)
    settings.storage_dir = tmp_path / "storage"
    settings.database_path = settings.storage_dir / "test.db"
    settings.auto_start = False
    settings.storage_dir.mkdir(parents=True)
    (settings.storage_dir / "avatars").mkdir()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def agent(client):
    response = client.post('/api/agents', json={
        'name': 'Test bot', 'primary_config': {'cli': 'custom', 'model': 'test', 'environment': 'server', 'command': 'python fake.py'},
        'fallback_configs': [], 'fallback_triggers': {'rate_limit': True, 'exit_code': True, 'timeout': True, 'timeout_minutes': 1},
    })
    assert response.status_code == 201
    return response.json()


def make_task(client, agent, title='Task', dependencies=None):
    response = client.post('/api/tasks', json={'title': title, 'description': 'Description', 'dependencies': dependencies or [], 'stage_agents': {str(i): agent['id'] for i in range(1, 5)}})
    assert response.status_code == 201, response.text
    return response.json()
