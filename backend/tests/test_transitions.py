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


def test_single_stage_pauses_and_pipeline_resumes_from_the_next_stage(client, agent, monkeypatch):
    async def idle_start(task_id):
        pass

    monkeypatch.setattr(dispatcher, "start", idle_start)
    task_id = make_task(client, agent)['id']
    start = f'/api/tasks/{task_id}/start'
    with connect() as conn:
        conn.execute("UPDATE tasks SET current_stage=0, status='open' WHERE id=?", (task_id,))

    assert client.post(f'{start}?stage=2').status_code == 409
    assert client.post(f'{start}?stage=1').status_code == 202
    assert client.post(f'{start}?stage=1').status_code == 409
    directory = task_dir(task_id)
    (directory / '01_questions_v1.json').write_text(json.dumps({'has_questions': False, 'questions': []}))
    asyncio.run(dispatcher._process_artifact(task_id, 1, 1))

    result = current(client, task_id)
    assert (result['current_stage'], result['status']) == (2, 'stage_ready')
    assert client.post(f'{start}?stage=3').status_code == 409
    assert client.post(f'{start}?pipeline=true').status_code == 202
    result = current(client, task_id)
    assert (result['current_stage'], result['status']) == (2, 'queued')

    (directory / '02_doc_review_v1.json').write_text(json.dumps({'status': 'APPROVED', 'comments': []}))
    asyncio.run(dispatcher._process_artifact(task_id, 2, 1))
    result = current(client, task_id)
    assert (result['current_stage'], result['status']) == (3, 'queued')


def test_single_stage_rejection_pauses_at_the_stage_that_needs_changes(client, agent):
    task_id = make_task(client, agent)['id']
    with connect() as conn:
        conn.execute("UPDATE tasks SET current_stage=2, status='running', single_stage=1 WHERE id=?", (task_id,))
    (task_dir(task_id) / '02_doc_review_v1.json').write_text(json.dumps({'status': 'REJECTED', 'comments': ['More detail']}))
    asyncio.run(dispatcher._process_artifact(task_id, 2, 1))
    result = current(client, task_id)
    assert (result['current_stage'], result['status']) == (1, 'stage_ready')


def test_single_worker_stage_pauses_before_code_review(client, agent):
    task_id = make_task(client, agent)['id']
    with connect() as conn:
        conn.execute("UPDATE tasks SET current_stage=3, status='running', single_stage=1 WHERE id=?", (task_id,))
        run = conn.execute(
            "INSERT INTO stage_runs(task_id, stage, attempt, started_at) VALUES (?, 3, 1, CURRENT_TIMESTAMP)",
            (task_id,),
        )
        run_id = run.lastrowid
    asyncio.run(dispatcher.worker_completed({'task_id': task_id, 'run_id': run_id, 'report': 'Implemented'}))
    result = current(client, task_id)
    assert (result['current_stage'], result['status']) == (4, 'stage_ready')
