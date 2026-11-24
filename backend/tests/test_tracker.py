import asyncio

import httpx

from app.config import settings
from app.database import connect
from app.tracker import BOT_PREFIX, ensure_webhook, publish_questions, test_connection as check_connection
from conftest import make_task


class FakeClient:
    responses = []
    requests = []

    def __init__(self, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, url, **kwargs):
        self.requests.append(('GET', url, kwargs))
        return self.responses.pop(0)

    async def post(self, url, **kwargs):
        self.requests.append(('POST', url, kwargs))
        return self.responses.pop(0)


def response(status=200, payload=None):
    return httpx.Response(status, json=payload or {}, request=httpx.Request('GET', 'https://yougile.test'))


def save_settings(client, **values):
    data = {
        'tracker_api_token': 'secret-key',
        'tracker_webhook_secret': 'webhook-secret',
        'yougile_webhook_id': '',
        'git_webhook_secret': '',
        'worker_token': '',
        **values,
    }
    assert client.put('/api/settings', json=data).status_code == 200


def test_publish_questions_and_check_connection(client, monkeypatch):
    save_settings(client)
    FakeClient.requests = []
    FakeClient.responses = [response(payload={'id': 1}), response(payload={'content': []})]
    monkeypatch.setattr('app.tracker.httpx.AsyncClient', FakeClient)

    asyncio.run(publish_questions('task-uuid', ['First question', 'Second question']))
    asyncio.run(check_connection())

    method, url, request = FakeClient.requests[0]
    assert method == 'POST'
    assert url == 'https://yougile.com/api-v2/chats/task-uuid/messages'
    assert request['headers']['Authorization'] == 'Bearer secret-key'
    assert request['json']['text'].startswith(BOT_PREFIX)
    assert '1. First question' in request['json']['text']
    assert '<br>' in request['json']['textHtml']
    assert FakeClient.requests[1][1] == 'https://yougile.com/api-v2/users'
    assert FakeClient.requests[1][2]['params'] == {'limit': 1}


def test_ensure_webhook_is_idempotent(client, monkeypatch):
    save_settings(client)
    FakeClient.requests = []
    FakeClient.responses = [
        response(payload=[]),
        response(status=201, payload={'id': 'webhook-id'}),
        response(payload=[{'id': 'webhook-id', 'url': 'https://orchestrator.test/api/webhooks/tracker?token=secret'}]),
    ]
    monkeypatch.setattr('app.tracker.httpx.AsyncClient', FakeClient)
    url = 'https://orchestrator.test/api/webhooks/tracker?token=secret'

    assert asyncio.run(ensure_webhook(url)) == 'webhook-id'
    assert asyncio.run(ensure_webhook(url)) == 'webhook-id'

    posts = [request for request in FakeClient.requests if request[0] == 'POST']
    assert len(posts) == 1
    assert posts[0][2]['json'] == {'url': url, 'event': 'chat_message-created'}


def test_register_webhook_endpoint(client, monkeypatch):
    save_settings(client)
    monkeypatch.setattr(settings, 'public_url', 'https://orchestrator.test')
    monkeypatch.setattr('app.main.ensure_webhook', fake_webhook)

    result = client.post('/api/integrations/yougile/webhook')

    assert result.status_code == 200
    assert result.json() == {'id': 'webhook-id'}
    with connect() as conn:
        assert conn.execute("SELECT value FROM settings WHERE key='yougile_webhook_id'").fetchone()['value'] == 'webhook-id'


async def fake_webhook(url):
    assert url == 'https://orchestrator.test/api/webhooks/tracker?token=webhook-secret'
    return 'webhook-id'


def test_yougile_webhook_security_and_bot_filter(client):
    save_settings(client)
    payload = {'event': 'chat_message-created', 'data': {'chatId': 'task-uuid', 'text': 'Answer'}}

    assert client.post('/api/webhooks/tracker?token=wrong', json=payload).status_code == 401
    bot_payload = {'event': 'chat_message-created', 'data': {'chatId': 'task-uuid', 'text': BOT_PREFIX + '\nQuestions'}}
    result = client.post('/api/webhooks/tracker?token=webhook-secret', json=bot_payload)
    assert result.status_code == 200
    assert result.json()['accepted'] is False


def test_yougile_webhook_accepts_answer(client, agent, monkeypatch):
    save_settings(client)
    task = make_task(client, agent)
    with connect() as conn:
        conn.execute("UPDATE tasks SET external_issue_id='task-uuid', status='waiting_answer' WHERE id=?", (task['id'],))

    async def start(task_id):
        pass

    monkeypatch.setattr('app.main.dispatcher.start', start)
    result = client.post('/api/webhooks/tracker?token=webhook-secret', json={
        'event': 'chat_message-created',
        'data': {'chatId': 'task-uuid', 'text': 'Use fixed timestep'},
    })

    assert result.status_code == 200
    assert result.json() == {'accepted': True, 'task_id': task['id']}
    assert client.get(f"/api/tasks/{task['id']}").json()['status'] == 'queued'
    assert 'fixed timestep' in (settings.storage_dir / 'tasks' / str(task['id']) / '01_answers_v1.md').read_text()
