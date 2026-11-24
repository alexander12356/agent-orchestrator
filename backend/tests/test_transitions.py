import asyncio
import json

from app.database import connect
from app.dispatcher import dispatcher, task_dir
from conftest import make_task


def current(client, task_id):
    return client.get(f'/api/tasks/{task_id}').json()


def test_doc_review_transitions(client, agent):
    task = make_task(client, agent)
    directory = task_dir(task['id'])
    (directory / '01_tech_doc_v1.md').write_text('# Doc')
    (directory / '01_questions_v1.json').write_text(json.dumps({'has_questions': False, 'questions': []}))
    asyncio.run(dispatcher._process_artifact(task['id'], 1, 1))
    assert current(client, task['id'])['current_stage'] == 2
    (directory / '02_doc_review_v1.json').write_text(json.dumps({'status': 'REJECTED', 'comments': ['More detail']}))
    asyncio.run(dispatcher._process_artifact(task['id'], 2, 1))
    assert current(client, task['id'])['current_stage'] == 1


def test_code_approval_waits_for_pr(client, agent):
    task = make_task(client, agent)
    (task_dir(task['id']) / '04_code_review_v1.json').write_text(json.dumps({'status': 'APPROVED', 'comments': []}))
    asyncio.run(dispatcher._process_artifact(task['id'], 4, 1))
    result = current(client, task['id'])
    assert result['current_stage'] == 5
    assert result['status'] == 'waiting_pr'
