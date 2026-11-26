import asyncio
import json

import httpx
import pytest

from app.config import settings
from app.database import connect, now
from app.dispatcher import dispatcher, task_dir
from app.tracker import BOT_PREFIX, ensure_webhook, fetch_import_tasks, publish_questions, sync_yougile_moves, test_connection as check_connection
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

    async def put(self, url, **kwargs):
        self.requests.append(('PUT', url, kwargs))
        return self.responses.pop(0)


def response(status=200, payload=None):
    return httpx.Response(status, json=payload or {}, request=httpx.Request('GET', 'https://yougile.test'))


def save_settings(client, **values):
    data = {
        'tracker_api_token': 'secret-key',
        'yougile_project': 'Unity',
        'yougile_board': 'Development',
        'yougile_sticker': '',
        'yougile_team_url': 'https://ru.yougile.com/team/068505cc4436/',
        'tracker_webhook_secret': 'webhook-secret',
        'yougile_webhook_id': '',
        'git_webhook_secret': '',
        'worker_token': '',
        **values,
    }
    assert client.put('/api/settings', json=data).status_code == 200


def test_yougile_column_follows_task_lifecycle(client, agent, monkeypatch):
    save_settings(client)
    task_id = make_task(client, agent)['id']
    monkeypatch.setattr('app.tracker.httpx.AsyncClient', FakeClient)
    monkeypatch.setattr(dispatcher, 'start', lambda task_id: asyncio.sleep(0))
    FakeClient.requests = []

    with connect() as conn:
        conn.execute("UPDATE tasks SET external_issue_id='task-uuid', status='open' WHERE id=?", (task_id,))
    asyncio.run(sync_yougile_moves())
    assert not FakeClient.requests

    def assert_move(source_id, source_title, target_id, target_title):
        FakeClient.requests = []
        FakeClient.responses = [
            response(payload={'columnId': source_id}),
            response(payload={'title': source_title, 'boardId': 'actual-board'}),
            response(payload={'content': [{'title': target_title, 'id': target_id}]}),
            response(),
        ]
        asyncio.run(sync_yougile_moves())
        base = 'https://yougile.com/api-v2'
        assert [(method, url) for method, url, _ in FakeClient.requests] == [
            ('GET', f'{base}/tasks/task-uuid'), ('GET', f'{base}/columns/{source_id}'),
            ('GET', f'{base}/columns'), ('PUT', f'{base}/tasks/task-uuid'),
        ]
        assert FakeClient.requests[2][2]['params']['boardId'] == 'actual-board'
        assert FakeClient.requests[-1][2]['json'] == {'columnId': target_id}
        assert not FakeClient.responses

    assert client.post(f'/api/tasks/{task_id}/start?pipeline=true').status_code == 202
    assert_move('open', 'Open', 'progress', 'InProgress')

    with connect() as conn:
        conn.execute("UPDATE tasks SET status='waiting_answer' WHERE id=?", (task_id,))
    FakeClient.responses = [response(status=503)]
    asyncio.run(sync_yougile_moves())
    with connect() as conn:
        pending, retry_after = conn.execute("SELECT pending, retry_after FROM yougile_moves WHERE task_id=?", (task_id,)).fetchone()
        assert pending == 1 and retry_after > 0
        conn.execute("UPDATE yougile_moves SET retry_after=0 WHERE task_id=?", (task_id,))
    assert_move('progress', 'InProgress', 'blocked', 'Blocked')

    assert client.post(f'/api/tasks/{task_id}/answer', json={'answer': 'Ответ'}).status_code == 202
    assert_move('blocked', 'Blocked', 'progress', 'In progress')
    FakeClient.requests = []
    for stage in (2, 3, 4):
        with connect() as conn:
            conn.execute("UPDATE tasks SET current_stage=?, status='running' WHERE id=?", (stage, task_id))
        asyncio.run(sync_yougile_moves())
    assert not FakeClient.requests

    assert client.post(f'/api/tasks/{task_id}/approve?stage=4').status_code == 202
    assert_move('progress', 'In progress', 'feedback', 'Feedback')
    assert client.post(f'/api/tasks/{task_id}/merge', json={}).status_code == 200
    assert_move('feedback', 'Feedback', 'complete', 'Complete')


def test_publish_questions_and_check_connection(client, monkeypatch):
    save_settings(client)
    FakeClient.requests = []
    FakeClient.responses = [response(payload={'id': 1}), response(payload={'content': []})]
    monkeypatch.setattr('app.tracker.httpx.AsyncClient', FakeClient)

    assert asyncio.run(publish_questions('task-uuid', ['First question', 'Second question'])) == 1
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


@pytest.mark.parametrize('tracker_only', [False, True])
def test_register_webhook_endpoint(client, monkeypatch, tracker_only):
    save_settings(client)
    public_url = 'http://localhost:8000' if tracker_only else 'https://orchestrator.test'
    monkeypatch.setattr(settings, 'public_url', public_url)
    monkeypatch.setattr(settings, 'tracker_webhook_public_url', 'https://orchestrator.test/' if tracker_only else '')
    monkeypatch.setattr('app.main.ensure_webhook', fake_webhook)

    displayed = client.get('/api/settings').json()
    assert displayed['tracker_webhook_url'] == 'https://orchestrator.test/api/webhooks/tracker?token=webhook-secret'
    assert displayed['git_webhook_url'] == f'{public_url}/api/webhooks/git'
    assert displayed['worker_websocket_url'] == public_url.replace('http', 'ws', 1) + '/ws/worker'

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


@pytest.mark.parametrize('delivery', ['reply', 'message_id', 'signal'])
def test_yougile_webhook_accepts_only_reply_to_current_question(client, agent, monkeypatch, delivery):
    save_settings(client)
    task = make_task(client, agent)
    with connect() as conn:
        conn.execute("UPDATE tasks SET external_issue_id='task-uuid' WHERE id=?", (task['id'],))
        conn.execute("UPDATE stage_runs SET yougile_question_message_id=100 WHERE task_id=? AND stage=1 AND attempt=1", (task['id'],))
        conn.execute(
            "INSERT INTO stage_runs(task_id, stage, attempt, started_at) VALUES (?, 1, 2, ?)",
            (task['id'], now()),
        )

    (task_dir(task['id']) / '01_questions_v2.json').write_text(json.dumps({'has_questions': True, 'questions': ['Which timestep?']}))
    FakeClient.requests = []
    FakeClient.responses = [response(payload={'id': 200})]
    monkeypatch.setattr('app.tracker.httpx.AsyncClient', FakeClient)
    asyncio.run(dispatcher._process_artifact(task['id'], 1, 2))

    async def start(task_id):
        pass

    monkeypatch.setattr('app.main.dispatcher.start', start)
    url = '/api/webhooks/tracker?token=webhook-secret'
    data = {'chatId': 'task-uuid', 'text': 'Use fixed timestep'}
    for properties in ({}, {'replyTo': 100}, {'replyTo': 300}):
        result = client.post(url, json={'event': 'chat_message-created', 'data': {**data, 'properties': properties}})
        assert result.json() == {'accepted': False}
        assert client.get(f"/api/tasks/{task['id']}").json()['status'] == 'waiting_answer'

    if delivery == 'reply':
        data['properties'] = {'replyTo': 200}
    elif delivery == 'message_id':
        data['id'] = 201
        FakeClient.requests = []
        FakeClient.responses = [response(payload={'id': 201, 'properties': {'replyTo': 200}})]
    else:
        data = {}
        FakeClient.requests = []
        FakeClient.responses = [response(payload={'content': [
            {'id': 204, 'text': 'Later reply', 'properties': {'replyTo': 200}},
            {'id': 202, 'text': 'Use fixed timestep', 'properties': {'replyTo': 200}},
            {'id': 201, 'text': 'Reply to an older question', 'properties': {'replyTo': 100}},
        ]})]

    result = client.post(url, json={'event': 'chat_message-created', 'data': data})

    assert result.status_code == 200
    assert result.json() == {'accepted': True, 'task_id': task['id']}
    assert client.get(f"/api/tasks/{task['id']}").json()['status'] == 'queued'
    assert 'fixed timestep' in (settings.storage_dir / 'tasks' / str(task['id']) / '01_answers_v2.md').read_text()
    if delivery == 'message_id':
        assert FakeClient.requests[0][1] == 'https://yougile.com/api-v2/chats/task-uuid/messages/201'
    elif delivery == 'signal':
        assert FakeClient.requests[0][1] == 'https://yougile.com/api-v2/chats/task-uuid/messages'
        assert FakeClient.requests[0][2]['params']['since'] == 200
    assert client.post(url, json={'event': 'chat_message-created', 'data': data}).json() == {'accepted': False}


def test_fetch_import_tasks_includes_descriptions_and_chats(client, monkeypatch):
    save_settings(client)
    FakeClient.requests = []
    FakeClient.responses = [
        response(payload={'content': [{'id': 'project-id', 'title': 'Unity'}]}),
        response(payload={'content': [{'id': 'board-id', 'title': 'Development'}]}),
        response(payload={'content': [
            {'id': 'open-id', 'title': 'Open'},
            {'id': 'ready-id', 'title': 'Ready'},
            {'id': 'done-id', 'title': 'Done'},
        ]}),
        response(payload={'content': [{'id': 'user-id', 'realName': 'Alice'}]}),
        response(payload={'content': [{'id': 'task-open', 'idTaskProject': 'ZEL-17', 'title': 'Open task', 'description': 'Open description'}]}),
        response(payload={'content': [{'id': 1700000000000, 'fromUserId': 'user-id', 'text': 'Open chat'}]}),
        response(payload={'content': [{'id': 'task-ready', 'title': 'Ready task', 'description': 'Ready description'}]}),
        response(payload={'content': [{'id': 1700000001000, 'fromUserId': 'user-id', 'text': 'Ready chat'}]}),
    ]
    monkeypatch.setattr('app.tracker.httpx.AsyncClient', FakeClient)

    result = asyncio.run(fetch_import_tasks('Unity', 'Development'))

    assert [task['id'] for task in result] == ['task-open', 'task-ready']
    assert result[0]['description'] == 'Open description'
    assert result[0]['key'] == 'ZEL-17'
    assert 'Alice' in result[0]['chat']
    assert 'Open chat' in result[0]['chat']
    assert 'Ready chat' in result[1]['chat']
    task_requests = [request for request in FakeClient.requests if request[1].endswith('/tasks')]
    assert [request[2]['params']['columnId'] for request in task_requests] == ['open-id', 'ready-id']


def test_import_endpoint_is_idempotent_and_preserves_active_tasks(client, monkeypatch):
    save_settings(client)
    items = [
        {'id': 'task-open', 'key': 'ZEL-17', 'title': 'Open task', 'description': 'Description', 'chat': '# Чат Yougile\n\nHello\n'},
        {'id': 'task-ready', 'key': 'ZEL-18', 'title': 'Ready task', 'description': 'Ready', 'chat': '# Чат Yougile\n'},
    ]

    async def fetch(project, board, sticker):
        assert (project, board, sticker) == ('Unity', 'Development', '')
        return items

    monkeypatch.setattr('app.main.fetch_import_tasks', fetch)
    first = client.post('/api/integrations/yougile/import')
    assert first.status_code == 200
    assert first.json() == {'imported': 2, 'updated': 0, 'skipped': 0, 'total': 2}
    tasks = client.get('/api/tasks').json()
    assert {task['status'] for task in tasks} == {'open'}
    assert {task['project'] for task in tasks} == {'Unity'}
    assert all(task['current_agent'] is None for task in tasks)
    assert next(task for task in tasks if task['external_issue_id'] == 'task-open')['external_issue_key'] == 'ZEL-17'
    assert next(task for task in tasks if task['external_issue_id'] == 'task-open')['external_issue_url'] == 'https://ru.yougile.com/team/068505cc4436/#ZEL-17'
    with connect() as conn:
        conn.execute("UPDATE tasks SET milestone='Прототип', sprint='Спринт 1'")
        conn.execute("UPDATE tasks SET project='Local project' WHERE external_issue_id='task-open'")
        conn.execute("UPDATE tasks SET project='' WHERE external_issue_id='task-ready'")
    imported_open = next(task for task in tasks if task['external_issue_id'] == 'task-open')
    directory = settings.storage_dir / 'tasks' / str(imported_open['id'])
    assert (directory / '00_task.md').exists()
    assert 'Hello' in (directory / '00_yougile_chat.md').read_text()

    second = client.post('/api/integrations/yougile/import')
    assert second.json() == {'imported': 0, 'updated': 2, 'skipped': 0, 'total': 2}
    tasks = client.get('/api/tasks').json()
    assert {task['external_issue_id']: task['project'] for task in tasks} == {
        'task-open': 'Local project', 'task-ready': 'Unity',
    }
    assert all(task['milestone'] == 'Прототип' and task['sprint'] == 'Спринт 1' for task in tasks)

    with connect() as conn:
        conn.execute("UPDATE tasks SET status='running', title='Local title' WHERE external_issue_id='task-open'")
    third = client.post('/api/integrations/yougile/import')
    assert third.json() == {'imported': 0, 'updated': 1, 'skipped': 1, 'total': 2}
    active = next(task for task in client.get('/api/tasks').json() if task['external_issue_id'] == 'task-open')
    assert active['title'] == 'Local title'


def test_import_filters_tasks_by_sticker_state(client, monkeypatch):
    save_settings(client)
    FakeClient.requests = []
    FakeClient.responses = [
        response(payload={'content': [{'id': 'project-id', 'title': 'Unity'}]}),
        response(payload={'content': [{'id': 'board-id', 'title': 'Development'}]}),
        response(payload={'content': [{'id': 'open-id', 'title': 'Open'}, {'id': 'ready-id', 'title': 'Ready'}]}),
        response(payload={'content': [{'id': 'sticker-id', 'name': 'Development', 'states': [{'id': 'state-id', 'name': 'Код'}]}]}),
        response(payload={'content': [{'id': 'user-id', 'realName': 'Alice'}]}),
        response(payload={'content': [
            {'id': 'matching', 'title': 'Matching', 'stickers': {'sticker-id': 'state-id'}},
            {'id': 'other', 'title': 'Other', 'stickers': {'sticker-id': 'other-state'}},
            {'id': 'missing', 'title': 'Missing', 'stickers': {}},
        ]}),
        response(payload={'content': [{'id': 1700000000000, 'fromUserId': 'user-id', 'text': 'Chat'}]}),
        response(payload={'content': []}),
    ]
    monkeypatch.setattr('app.tracker.httpx.AsyncClient', FakeClient)

    result = asyncio.run(fetch_import_tasks('Unity', 'Development', 'Development.Код'))

    assert [task['id'] for task in result] == ['matching']
    sticker_request = next(request for request in FakeClient.requests if request[1].endswith('/string-stickers'))
    assert sticker_request[2]['params']['name'] == 'Development'
    assert not any(request[1].endswith('/chats/other/messages') for request in FakeClient.requests)
