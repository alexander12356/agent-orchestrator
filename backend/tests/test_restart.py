import json
import os
import signal
import time

import pytest

from app.database import connect
from app.dispatcher import dispatcher, task_dir
from tests.conftest import make_task


@pytest.mark.parametrize("action", ["restart", "pause"])
def test_interrupt_stops_the_running_agent_and_allows_another_attempt(client, agent, action):
    command = (
        'python3 -c "from pathlib import Path; import time; '
        "Path('01_tech_doc_v1.md').write_text('unfinished'); "
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

    task_path = f"/api/tasks/{task['id']}"
    process = dispatcher.processes[task['id']]
    response = client.post(f"{task_path}/restart?stage=1" if action == "restart" else f"{task_path}/pause")
    assert response.status_code == 202, response.text
    assert process.returncode is not None
    with connect() as conn:
        rows = conn.execute('SELECT attempt, status, error FROM stage_runs WHERE task_id=? ORDER BY attempt', (task['id'],)).fetchall()
    if action == "restart":
        assert rows[0]['status'] == 'error'
        assert rows[0]['error'] == 'Остановлено перезапуском'
    else:
        assert len(rows) == 1
        assert rows[0]['status'] == 'paused'
        assert client.get(task_path).json()['status'] == 'paused'
        assert '01_tech_doc_v1.md' not in {artifact['name'] for artifact in client.get(f'{task_path}/artifacts').json()}
        assert client.post(f'{task_path}/start').status_code == 202
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


@pytest.mark.parametrize("resume_fails", [False, True])
def test_pause_resumes_the_exact_cursor_session(client, agent, monkeypatch, tmp_path, resume_fails):
    session_id = "12345678-1234-1234-1234-123456789abc"
    executable = tmp_path / "agent"
    init = json.dumps({"type": "system", "subtype": "init", "session_id": session_id})
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "from pathlib import Path\n"
        "with Path('launches').open('a') as stream:\n"
        "    stream.write(next((arg for arg in sys.argv if arg.startswith('--resume=')), 'fresh') + '\\n')\n"
        f"print({init!r}, flush=True)\n"
        f"if '--resume={session_id}' not in sys.argv:\n"
        "    time.sleep(30)\n"
        f"if {resume_fails!r}:\n"
        "    raise SystemExit(1)\n"
        "Path('01_tech_doc_v2.md').write_text('complete')\n"
        "Path('01_questions_v2.json').write_text('{\"has_questions\":false,\"questions\":[]}')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    config = {"cli": "custom", "command": "agent --print {prompt}", "environment": "server"}
    response = client.put(f"/api/agents/{agent['id']}", json={"name": agent["name"], "primary_config": config})
    assert response.status_code == 200, response.text
    task = make_task(client, agent)
    directory = task_dir(task["id"])
    path = f"/api/tasks/{task['id']}"

    def wait_for_run(attempt, predicate):
        for _ in range(100):
            with connect() as conn:
                run = conn.execute(
                    "SELECT * FROM stage_runs WHERE task_id=? AND attempt=? AND stage=1",
                    (task["id"], attempt),
                ).fetchone()
            if run and predicate(run):
                return run
            time.sleep(0.05)
        pytest.fail(f"Attempt {attempt} did not reach the expected state: {dict(run) if run else None}")

    wait_for_run(1, lambda run: run["cli_session"])
    assert client.post(f"{path}/pause").status_code == 202
    assert client.post(f"{path}/start").status_code == 202
    resumed = wait_for_run(2, lambda run: run["status"] in ("completed", "error"))
    assert resumed["status"] == ("error" if resume_fails else "completed")
    assert (directory / "launches").read_text().splitlines() == ["fresh", f"--resume={session_id}"]
