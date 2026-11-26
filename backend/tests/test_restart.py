import os
import signal
import time

from app.database import connect
from app.dispatcher import dispatcher, task_dir
from tests.conftest import make_task


def test_restart_stops_the_running_agent_and_starts_another(client, agent):
    command = (
        'python3 -c "from pathlib import Path; import time; '
        "next(Path.cwd().glob('.orchestrator-progress-*/updates.txt')).write_text('started' + chr(10)); time.sleep(30)\""
    )
    updated = client.put(f"/api/agents/{agent['id']}", json={
        'name': 'Test bot',
        'primary_config': {'cli': 'custom', 'model': 'test', 'environment': 'server', 'command': command},
        'fallback_configs': [],
        'fallback_triggers': {'rate_limit': False, 'exit_code': False, 'timeout': False, 'timeout_minutes': 1},
    })
    assert updated.status_code == 200, updated.text
    task = make_task(client, agent)
    log = task_dir(task['id']) / 'logs' / 'stage_1_attempt_1.log'
    for _ in range(50):
        if log.exists() and 'started' in log.read_text(encoding='utf-8'):
            break
        time.sleep(0.1)
    else:
        raise AssertionError(log.read_text(encoding='utf-8') if log.exists() else 'живой лог агента не появился')

    response = client.post(f"/api/tasks/{task['id']}/restart?stage=1")
    assert response.status_code == 202, response.text
    with connect() as conn:
        rows = conn.execute('SELECT attempt, status, error FROM stage_runs WHERE task_id=? ORDER BY attempt', (task['id'],)).fetchall()
    assert rows[0]['status'] == 'error'
    assert rows[0]['error'] == 'Остановлено перезапуском'
    second = task_dir(task['id']) / 'logs' / 'stage_1_attempt_2.log'
    for _ in range(50):
        if second.exists() and 'Агент запущен' in second.read_text(encoding='utf-8'):
            break
        time.sleep(0.1)
    else:
        raise AssertionError('новая сессия не запустилась')
    process = dispatcher.processes.get(task['id'])
    if process and process.pid and process.returncode is None:
        os.kill(process.pid, signal.SIGKILL)
