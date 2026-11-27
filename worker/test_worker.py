import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location('unity_worker', Path(__file__).with_name('unity_worker.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_parse_structured_usage():
    result = module.extract_usage('{"usage":{"input_tokens":20,"output_tokens":5}}')
    assert result['input_tokens'] == 20
    assert result['estimated'] is False


def test_kimi_implementer_uses_model_and_low_effort(monkeypatch, tmp_path):
    node = tmp_path / "node.exe"
    entry = tmp_path / "main.mjs"
    node.write_text("node", encoding="utf-8")
    entry.write_text("entry", encoding="utf-8")
    monkeypatch.setattr(module, "KIMI_NODE", node)
    monkeypatch.setattr(module, "KIMI_ENTRY", entry)
    monkeypatch.setattr(module.os, "name", "posix")
    command = module.kimi_command({"model": "kimi-code/k3.1-pro", "reasoning_effort": "low"}, "implement")
    assert command[:2] == [str(node), str(entry)]
    assert module.windows_path(Path("/mnt/e/WSL/ZelyaKimiWindows/node_modules/@moonshot-ai/kimi-code/dist/main.mjs")) == "E:\\WSL\\ZelyaKimiWindows\\node_modules\\@moonshot-ai\\kimi-code\\dist\\main.mjs"
    assert command[command.index("-m") + 1] == "kimi-code/k3.1-pro"
    assert command[-2:] == ["-p", "implement"]
    assert module.kimi_environment({"reasoning_effort": "low"})["KIMI_MODEL_THINKING_EFFORT"] == "low"


def test_missing_usage_is_unknown():
    assert module.extract_usage('plain output') == {}


def test_zcode_implementer_runs_a_headless_yolo_prompt(monkeypatch):
    monkeypatch.setattr(module, 'zcode_launcher', lambda: ['zcode'])
    assert module.zcode_command('implement') == ['zcode', '-p', 'implement', '--mode', 'yolo', '--no-color']


def test_zcode_node_launcher_sets_provider_and_credential_paths(monkeypatch, tmp_path):
    node = tmp_path / 'node.exe'
    node.write_text('node', encoding='utf-8')
    entry = tmp_path / 'resources' / 'glm' / 'zcode.cjs'
    entry.parent.mkdir(parents=True)
    entry.write_text('entry', encoding='utf-8')
    monkeypatch.setattr(module, 'zcode_launcher', lambda: [str(node), str(entry)])
    environment = module.zcode_environment()
    assert environment['ZCODE_BUILTIN_PROVIDER_CONFIG_FILE'].endswith('zcode-builtin.json')
    assert environment['ZCODE_CREDENTIALS_FILE'].endswith('credentials.json')
    assert environment['NO_COLOR'] == '1'


def make_worker(tmp_path):
    worker = module.UnityWorker(SimpleNamespace(
        server='', token='', repo=str(tmp_path), worktrees=str(tmp_path / 'worktrees'),
        library=str(tmp_path / 'reference-library'), unity=None,
    ))
    return worker


def test_merge_cleanup_removes_git_worktree_and_both_branches(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    origin = tmp_path / "origin.git"

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True, stderr=subprocess.STDOUT).strip()

    git("init", "--bare", str(origin))
    git("init", "-b", "main")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "initial")
    git("remote", "add", "origin", str(origin))
    worker = make_worker(tmp_path)
    worker.repo = repo
    path = worker.worktrees / "task-23"
    branch = "Agent-Orchestrator/ZEL-23"
    git("worktree", "add", "-b", branch, str(path))
    git("push", "origin", branch)
    message = {"task_id": 23, "worktree_path": str(path), "branch_name": branch, "head_sha": git("rev-parse", branch)}

    draft = path / "draft.txt"
    draft.write_text("local edits", encoding="utf-8")
    with pytest.raises(RuntimeError, match="незакоммиченные изменения"):
        asyncio.run(worker.cleanup(message))
    assert draft.exists()
    assert git("ls-remote", "--heads", "origin", branch)

    draft.unlink()
    with pytest.raises(RuntimeError, match="изменилась после merge"):
        asyncio.run(worker.cleanup({**message, "head_sha": "a" * 40}))
    assert path.exists()
    assert git("ls-remote", "--heads", "origin", branch)

    asyncio.run(worker.cleanup(message))
    asyncio.run(worker.cleanup(message))
    assert not path.exists()
    assert "ZEL-23" not in git("worktree", "list")
    assert not git("branch", "--list", branch)
    assert not git("ls-remote", "--heads", "origin", branch)


def test_unity_command_uses_windows_project_and_log_paths(tmp_path):
    worker = make_worker(tmp_path)
    worker.unity = 'Unity.exe'
    command = worker.unity_command(Path('/mnt/d/projects/Project_3/zelya-worktrees/task-63'))
    assert command[command.index('-projectPath') + 1] == 'D:\\projects\\Project_3\\zelya-worktrees\\task-63'
    assert command[command.index('-logFile') + 1] == 'D:\\projects\\Project_3\\zelya-worktrees\\task-63\\unity-build.log'


def test_prepare_library_is_local_to_the_worktree(tmp_path):
    worker = make_worker(tmp_path)
    first = tmp_path / 'worktrees' / 'task-1'
    second = tmp_path / 'worktrees' / 'task-2'
    asyncio.run(worker.prepare_library(first))
    asyncio.run(worker.prepare_library(second))
    assert (first / 'Library').is_dir()
    assert (second / 'Library').is_dir()
    assert not (first / 'Library').is_symlink()
    assert first / 'Library' != second / 'Library'


def test_existing_worktree_is_not_reset(tmp_path):
    worker = make_worker(tmp_path)
    path = tmp_path / 'worktrees' / 'task-1'

    def git(*args):
        subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True)

    git('init', '-b', 'main')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-m', 'initial')
    git('worktree', 'add', '-b', 'feat/task-1', str(path))
    marker = path / 'changes.txt'
    marker.write_text('keep', encoding='utf-8')

    async def checked(command, cwd=None):
        if command != ['git', 'fetch', 'origin', 'main']:
            raise AssertionError(f'Неожиданная команда: {command}')
        return ''

    worker.checked = checked
    result = asyncio.run(worker.prepare_worktree(1, 'feat/task-1'))
    assert result == path
    assert marker.read_text(encoding='utf-8') == 'keep'
    with pytest.raises(RuntimeError, match='не является рабочей копией ветки'):
        asyncio.run(worker.prepare_worktree(1, 'feat/task-2'))


def test_incomplete_worktree_preserves_files_and_fails_before_preparing_library(tmp_path):
    worker = make_worker(tmp_path)
    path = worker.worktrees / 'task-1'
    path.mkdir(parents=True)
    marker = path / 'changes.txt'
    marker.write_text('keep', encoding='utf-8')

    async def checked(command, cwd=None):
        assert command == ['git', 'fetch', 'origin', 'main']
        return ''

    worker.checked = checked
    with pytest.raises(RuntimeError, match='существует без метаданных Git worktree'):
        asyncio.run(worker.prepare_worktree(1, 'feat/task-1'))
    assert marker.read_text(encoding='utf-8') == 'keep'
    assert not (path / 'Library').exists()


def test_worktree_is_created_on_issue_key_branch(tmp_path):
    worker = make_worker(tmp_path)
    commands = []

    async def checked(command, cwd=None):
        commands.append(command)
        return ''

    async def execute(command, cwd=None):
        commands.append(command)
        return 1, ''

    worker.checked = checked
    worker.execute = execute
    result = asyncio.run(worker.prepare_worktree(23, 'Agent-Orchestrator/ZEL-23'))
    assert result == worker.worktrees / 'task-23'
    assert commands == [
        ['git', 'fetch', 'origin', 'main'],
        ['git', 'show-ref', '--verify', 'refs/heads/Agent-Orchestrator/ZEL-23'],
        ['git', 'worktree', 'add', '-b', 'Agent-Orchestrator/ZEL-23', str(result), 'origin/main'],
    ]


def test_worktree_rejects_branch_names_outside_the_naming(tmp_path):
    worker = make_worker(tmp_path)

    async def unexpected(command, cwd=None):
        raise AssertionError(f'Неожиданная команда: {command}')

    worker.checked = worker.execute = unexpected
    for branch in ('main', 'codex/zelya-1', 'Agent-Orchestrator/-main', 'Agent-Orchestrator/ZEL-23/extra'):
        with pytest.raises(ValueError):
            asyncio.run(worker.prepare_worktree(23, branch))


def test_terminate_process_escalates_to_kill(tmp_path):
    worker = make_worker(tmp_path)

    class Process:
        returncode = None
        terminated = False
        killed = False

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            while self.returncode is None:
                await asyncio.sleep(0)
            return self.returncode

    process = Process()
    asyncio.run(worker.terminate_process(process, timeout=0.01))
    assert process.terminated
    assert process.killed
    assert process.returncode == -9


def test_cancel_waits_for_worker_command_to_stop(tmp_path, monkeypatch):
    worker = make_worker(tmp_path)

    async def scenario():
        launched = asyncio.Event()
        start_process = worker.start_process
        process = None

        async def record_process(*args, **kwargs):
            nonlocal process
            process = await start_process(*args, **kwargs)
            launched.set()
            return process

        monkeypatch.setattr(worker, "start_process", record_process)
        job = asyncio.create_task(worker.execute([sys.executable, "-c", "import time; time.sleep(60)"]))
        worker.jobs[1] = job
        await asyncio.wait_for(launched.wait(), timeout=5)
        await worker.cancel(1)

        assert job.cancelled()
        assert process is not None and process.returncode is not None

    asyncio.run(scenario())


def test_finish_consumer_cancels_a_stuck_reader(tmp_path):
    worker = make_worker(tmp_path)

    async def scenario():
        consumer = asyncio.create_task(asyncio.Event().wait())
        await worker.finish_consumer(consumer, timeout=0.01)
        assert consumer.cancelled()

    asyncio.run(scenario())


def agent_result(exit_code=0, *, timed_out=False, rate_limited=False, output=''):
    return {
        'exit_code': exit_code, 'output': output, 'rate_limited': rate_limited,
        'timed_out': timed_out, 'input_tokens': 0, 'output_tokens': 0, 'cache_tokens': 0, 'estimated': True,
    }


def fallback_worker(tmp_path, answers):
    worker = make_worker(tmp_path)
    worker.logs = []
    calls = []

    async def run_agent(task_id, path, config, prompt, timeout, run_id=None, publish_usage=None):
        calls.append(config)
        answer = answers[len(calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def log(task_id, text):
        worker.logs.append(text)

    worker.run_agent = run_agent
    worker.log = log
    return worker, calls


def run_fallback(worker, config, triggers=None):
    triggers = triggers or {'rate_limit': True, 'exit_code': True, 'timeout': True, 'timeout_minutes': 20}
    return asyncio.run(worker.run_with_fallback(1, Path('.'), 'prompt', config, triggers))


KIMI = {'cli': 'kimi', 'model': 'kimi-code/k3.1-pro'}
ZCODE = {'cli': 'zcode', 'model': 'account:zai/GLM-5.4'}


def test_failed_primary_switches_to_fallback(tmp_path):
    worker, calls = fallback_worker(tmp_path, [{**agent_result(1), "input_tokens": 5, "estimated": False}, {**agent_result(0), "input_tokens": 12, "estimated": False}])
    models = []

    async def send(message):
        if message['type'] == 'TASK_MODEL':
            models.append((len(calls), message['model_used'], message['is_fallback']))

    worker.send = send
    selected, result = asyncio.run(worker.run_with_fallback(1, tmp_path, 'prompt', {'primary': KIMI, 'fallback': [ZCODE]}, {}, run_id=4))
    assert [call['cli'] for call in calls] == ['kimi', 'zcode']
    assert models == [(0, KIMI['model'], False), (1, ZCODE['model'], True)]
    assert selected == ZCODE
    assert result['exit_code'] == 0
    assert result['input_tokens'] == 17
    assert any('kimi (kimi-code/k3.1-pro): код возврата 1. Переключаюсь на fallback: zcode (account:zai/GLM-5.4)' in line for line in worker.logs)


def test_unlaunchable_fallback_does_not_abort_the_stage(tmp_path):
    worker, calls = fallback_worker(tmp_path, [ValueError('Для Custom CLI укажите command'), agent_result(0)])
    selected, result = run_fallback(worker, {'primary': KIMI, 'fallback': [ZCODE]})
    assert [call['cli'] for call in calls] == ['kimi', 'zcode']
    assert selected == ZCODE
    assert any('не удалось запустить CLI' in line and 'zcode' in line for line in worker.logs)


def test_all_configs_failed_reports_each_reason(tmp_path):
    worker, _ = fallback_worker(tmp_path, [agent_result(1), ValueError('boom')])
    with pytest.raises(RuntimeError) as error:
        run_fallback(worker, {'primary': KIMI, 'fallback': [ZCODE]})
    message = str(error.value)
    assert 'kimi (kimi-code/k3.1-pro): код возврата 1' in message
    assert 'zcode (account:zai/GLM-5.4): boom' in message


def test_timeout_is_reported_with_the_limit(tmp_path):
    worker, _ = fallback_worker(tmp_path, [agent_result(-15, timed_out=True), agent_result(0)])
    run_fallback(worker, {'primary': KIMI, 'fallback': [ZCODE]}, {'timeout': True, 'timeout_minutes': 7})
    assert any('таймаут 7 мин' in line for line in worker.logs)


def test_limit_is_reported_only_by_error_lines():
    assert module.rate_limited_line("ERROR: You've hit your usage limit.")
    assert module.rate_limited_line('{"type":"error","message":"rate limit exceeded"}')
    assert not module.rate_limited_line('HTTP 429 means the rate limit is exceeded.')
    assert not module.rate_limited_line('{"type":"assistant","message":{"content":[{"type":"text","text":"rate limit"}]}}')


def run_cli(tmp_path, body, messages=None, run_id=None):
    worker = make_worker(tmp_path)
    script = tmp_path / 'cli.py'
    script.write_text('import sys, time\n' + body, encoding='utf-8')
    async def send(message):
        if messages is not None:
            messages.append(message)

    worker.send = send
    config = {'cli': 'custom', 'command': f'"{sys.executable}" "{script}"'}
    return asyncio.run(worker.run_agent(1, tmp_path, config, 'implement', 1, run_id))


def test_worker_reports_the_prompt_passed_to_cli(tmp_path):
    messages = []
    result = run_cli(tmp_path, "sys.stdout.write(sys.argv[1])\n", messages, run_id=7)

    assert result['exit_code'] == 0
    assert messages == [{'type': 'TASK_PROMPT', 'task_id': 1, 'run_id': 7, 'prompt': result['output']}]
    assert result['output'].startswith('implement\n\nДля Live Logs')


def test_kimi_worker_reports_live_and_final_windows_usage(monkeypatch, tmp_path):
    worker = make_worker(tmp_path)
    cwd = PureWindowsPath(r'D:\worktrees\task-1')
    monkeypatch.setattr(worker, 'kimi_usage_location', lambda *args: asyncio.sleep(0, result=(tmp_path, cwd)))
    script = tmp_path / 'kimi.py'
    script.write_text(
        'import json, time\n'
        'from pathlib import Path\n'
        'session = Path("sessions/worktree/session_12345678-1234-1234-1234-123456789abc")\n'
        'wire = session / "agents/main/wire.jsonl"\n'
        'wire.parent.mkdir(parents=True)\n'
        f'(session / "state.json").write_text(json.dumps(dict(cwd={str(cwd).lower()!r}, createdAt=time.time_ns() // 1000000)))\n'
        'record = json.dumps(dict(type="usage.record", usage=dict(inputOther=10, inputCacheRead=20, output=3))) + "\\n"\n'
        'wire.write_text(record)\n'
        'for _ in range(200):\n'
        '    if Path("reported").exists():\n'
        '        break\n'
        '    time.sleep(.05)\n'
        'else:\n'
        '    raise SystemExit("usage did not arrive while the CLI was running")\n'
        'with wire.open("a") as stream:\n'
        '    stream.write(record)\n'
        'print(json.dumps(dict(type="session.resume_hint", session_id=session.name)))\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(worker, 'command', lambda *args: [sys.executable, str(script)])
    updates = []

    async def send(message):
        if message['type'] == 'TASK_USAGE':
            updates.append(tuple(message[key] for key in module.FIELDS))
            (tmp_path / 'reported').touch()

    worker.send = send
    _, result = asyncio.run(worker.run_with_fallback(
        1, tmp_path, 'implement', {'primary': KIMI}, {}, run_id=7,
        previous_usage={'input_tokens': 5, 'cache_tokens': 8, 'output_tokens': 2},
    ))

    assert updates == [(15, 28, 5), (25, 48, 8)]
    assert tuple(result[key] for key in module.FIELDS) == updates[-1]
    assert result['usage_partial'] is False


def test_kimi_worker_reuses_the_session_saved_on_cancel(monkeypatch, tmp_path):
    worker = make_worker(tmp_path)
    worker.worktrees.mkdir()
    session_id = "session_12345678-1234-1234-1234-123456789abc"
    script = tmp_path / "kimi.py"
    script.write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        f"session_id = {session_id!r}\n"
        "if '--session' in sys.argv:\n"
        "    assert sys.argv[-2:] == ['--session', session_id]\n"
        "    Path('resumed').touch()\n"
        "    print(json.dumps(dict(type='session.resume_hint', session_id=session_id)))\n"
        "else:\n"
        "    session = Path('sessions/worktree') / session_id\n"
        "    session.mkdir(parents=True)\n"
        "    temp = session / 'state.tmp'\n"
        "    temp.write_text(json.dumps(dict(cwd=str(Path.cwd()), createdAt=time.time_ns() // 1000000)))\n"
        "    temp.rename(session / 'state.json')\n"
        "    Path('started').touch()\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(worker, "command", lambda *args: [sys.executable, str(script)])
    monkeypatch.setattr(worker, "kimi_usage_location", lambda *args: asyncio.sleep(0, result=(tmp_path, tmp_path)))

    async def scenario():
        async def send(message):
            pass

        worker.send = send
        config = {"primary": KIMI}
        job = asyncio.create_task(worker.run_with_fallback(1, tmp_path, "implement", config, {}, run_id=7))
        worker.jobs[1] = job
        async with asyncio.timeout(5):
            while not (tmp_path / "started").exists():
                await asyncio.sleep(0.02)
        await worker.cancel(1)
        assert job.cancelled()
        _, result = await worker.run_with_fallback(
            1, tmp_path, "implement", config, {}, run_id=8, paused_run_id=7,
        )
        assert result["exit_code"] == 0
        assert (tmp_path / "resumed").exists()

    asyncio.run(scenario())


def test_run_agent_reads_long_lines_and_ignores_limit_words_in_prose(tmp_path):
    messages = []
    result = run_cli(
        tmp_path,
        "from pathlib import Path\n"
        "next(Path.cwd().glob('.orchestrator-progress-*/updates.txt')).write_text('Проверки прошли.\\n', encoding='utf-8')\n"
        "sys.stdout.write('x' * 200_000 + '\\n')\n"
        "sys.stdout.write('HTTP 429 means the rate limit is exceeded.\\n')\n",
        messages,
    )
    assert result['exit_code'] == 0
    assert result['rate_limited'] is False
    assert result['output'] == 'x' * 200_000 + '\nHTTP 429 means the rate limit is exceeded.\n'
    assert messages == [
        {'type': 'LOG_CHUNK', 'task_id': 1, 'chunk': 'Проверки прошли.\n', 'format': 'activity'},
    ]


def test_run_agent_reports_a_limit_of_a_failed_cli(tmp_path):
    result = run_cli(tmp_path, "print(\"You've hit your usage limit. Try again later.\")\nsys.exit(1)\n")
    assert (result['exit_code'], result['rate_limited']) == (1, True)


def test_run_agent_stops_a_cli_that_reports_a_limit(tmp_path):
    result = run_cli(tmp_path, "print('ERROR: rate limit exceeded', flush=True)\ntime.sleep(30)\n")
    assert result['rate_limited'] is True
    assert result['timed_out'] is False
    assert result['exit_code'] != 0


def implementation_details():
    return {
        'purpose': 'Сохранять прогресс после перезапуска игры.',
        'changes': ['Добавлено сохранение прогресса в файл.'],
        'implementation': ['SaveSystem сериализует состояние перед выходом.'],
        'before': 'После перезапуска прогресс терялся.',
        'after': 'Сохранённое состояние восстанавливается при запуске.',
        'validation': ['pytest tests/test_save.py — успешно'],
        'review_focus': ['Проверить обработку повреждённого файла.'],
    }


def test_pr_body_contains_implementation_and_verified_results(tmp_path):
    path = tmp_path / module.PR_DETAILS_FILE
    path.write_text(json.dumps(implementation_details()), encoding='utf-8')
    details = module.read_pr_details(path)
    body = module.format_pr_body(
        details, 42, 'Unity batchmode завершился успешно.', 'SaveSystem.cs | 20 +++',
        'ZEL-42', 'https://tracker.example/ZEL-42',
    )
    for heading in ('## Цель', '## Что сделано', '## Как реализовано', '## Было', '## Стало', '## Проверка', '## Область изменений', '## На что обратить внимание при ревью'):
        assert heading in body
    assert 'Добавлено сохранение прогресса в файл.' in body
    assert 'Unity batchmode завершился успешно.' in body
    assert 'pytest tests/test_save.py — успешно' in body
    assert 'SaveSystem.cs | 20 ++' in body
    assert '[ZEL-42](https://tracker.example/ZEL-42)' in body
    assert 'Задача оркестратора №42' in body


def test_pr_details_require_specific_content_and_omit_empty_review_focus(tmp_path):
    path = tmp_path / module.PR_DETAILS_FILE
    details = implementation_details()
    details['changes'] = []
    path.write_text(json.dumps(details), encoding='utf-8')
    with pytest.raises(RuntimeError, match='changes'):
        module.read_pr_details(path)

    details = implementation_details()
    details['review_focus'] = []
    details['validation'] = []
    body = module.format_pr_body(details, 1, 'Сборка не запускалась.', '')
    assert '## На что обратить внимание при ревью' not in body
    assert 'Нет изменений относительно origin/main.' in body


@pytest.mark.parametrize("repair,resume", [(False, False), (True, False), (False, True)])
def test_assign_creates_pr_from_summary_and_keeps_service_files_out_of_commit(tmp_path, repair, resume):
    worker = make_worker(tmp_path)
    path = tmp_path / 'task-worktree'
    path.mkdir()
    worker.worktrees.mkdir()
    commands = []
    prompts = []
    sent = []

    async def prepare_worktree(task_id, branch):
        return path

    async def run_with_fallback(task_id, worktree, prompt, config, triggers, run_id, previous_usage=None):
        if prompts:
            assert previous_usage is not None
            assert "Test.cs(3,2): error CS0161" in prompt
        prompts.append(prompt)
        (path / module.PR_DETAILS_FILE).write_text(json.dumps(implementation_details()), encoding='utf-8')
        return KIMI, agent_result(output='raw CLI output')

    async def run_unity(worktree, task_id):
        if repair and len(prompts) == 1:
            raise module.UnityCompilationError("Test.cs(3,2): error CS0161")
        return 'Unity batchmode завершился успешно.'

    create_attempts = 0
    body = ""

    async def checked(command, cwd=None):
        nonlocal create_attempts, body
        commands.append(command)
        if command[:2] == ['git', 'rev-parse']:
            return 'validated-head'
        if command == ['git', 'status', '--porcelain']:
            return '' if create_attempts else ' M SaveSystem.cs'
        if command == ['git', 'add', '-A']:
            assert not (path / 'orchestrator-tech-doc.md').exists()
            assert not (path / module.PR_DETAILS_FILE).exists()
        if command[:3] == ['git', 'diff', '--stat']:
            return 'SaveSystem.cs | 20 +++\n'
        if command[:3] == ['gh', 'pr', 'list']:
            return ''
        if command[:3] == ['gh', 'pr', 'create']:
            create_attempts += 1
            if resume and create_attempts == 1:
                raise RuntimeError('remote unavailable')
            body = Path(command[-1]).read_text(encoding='utf-8')
            return 'https://github.example/org/repo/pull/42\n'
        return ''

    async def send(message):
        sent.append(message)

    worker.prepare_worktree = prepare_worktree
    worker.run_with_fallback = run_with_fallback
    worker.run_unity = run_unity
    worker.checked = checked
    worker.send = send
    message = {
        'task_id': 42,
        'run_id': 7,
        'branch_name': 'Agent-Orchestrator/ZEL-42',
        'task_title': 'Сохранение прогресса',
        'task_description': 'Прогресс должен переживать перезапуск.',
        'external_issue_key': 'ZEL-42',
        'external_issue_url': 'https://tracker.example/ZEL-42',
        'tech_doc_content': '# Технический документ',
        'review_comments_content': '',
        'agent_config': {'primary': KIMI, 'fallback': [], 'triggers': {}, 'role_prompt': 'Unity developer'},
    }
    asyncio.run(worker.assign(message))
    if resume:
        assert sent[-1]['type'] == 'TASK_FAILED'
        asyncio.run(worker.assign({**message, 'run_id': 8}))
        assert len(prompts) == 1
        assert not list(worker.worktrees.glob('*.json'))

    commits = [command for command in commands if command[:2] == ['git', 'commit']]
    assert commits == [[
        'git', 'commit', '-m',
        'ZEL-42 https://tracker.example/ZEL-42 Сохранять прогресс после перезапуска игры.',
    ]]
    create = next(command for command in commands if command[:3] == ['gh', 'pr', 'create'])
    assert create[create.index('--title') + 1] == 'Сохранение прогресса'
    assert '## Что сделано' in body
    assert 'SaveSystem сериализует состояние перед выходом.' in body
    assert 'raw CLI output' not in body
    assert 'Прогресс должен переживать перезапуск.' in prompts[0]
    assert module.PR_DETAILS_CONTRACT in prompts[0]
    completed = next(message for message in sent if message['type'] == 'TASK_COMPLETED')
    assert completed['pr_url'] == 'https://github.example/org/repo/pull/42'
    assert body in completed['report']
    assert completed['pr_url'] in completed['report']
    assert 'raw CLI output' not in completed['report']


def test_existing_pr_is_updated_instead_of_reused_unchanged(tmp_path):
    worker = make_worker(tmp_path)
    commands = []

    async def checked(command, cwd=None):
        commands.append(command)
        if command[:3] == ['gh', 'pr', 'list']:
            return 'https://github.example/org/repo/pull/42\n'
        assert command[:3] == ['gh', 'api', '--hostname']
        assert 'repos/org/repo/pulls/42' in command
        assert json.loads(Path(command[command.index('--input') + 1]).read_text()) == {
            'title': 'Title', 'body': 'Updated body',
        }
        return ''

    worker.checked = checked
    url = asyncio.run(worker.upsert_pr(tmp_path, 'Agent-Orchestrator/ZEL-42', 'Title', 'Updated body'))
    assert url == 'https://github.example/org/repo/pull/42'
    assert len(commands) == 2


@pytest.mark.parametrize("first_output,close_code,expected", [
    ("another Unity instance is running with this project open", 0, ["unity", "close", "unity"]),
    ("another Unity instance is running with this project open", 2, ["unity", "close"]),
    ("compilation failed", 0, ["unity"]),
])
def test_unity_retries_only_after_editor_closes(tmp_path, first_output, close_code, expected):
    worker = make_worker(tmp_path)
    worker.unity = "Unity.exe"
    calls = []

    async def execute(command):
        if "-EncodedCommand" in command:
            calls.append("close")
            return close_code, ""
        calls.append("unity")
        return (1, first_output) if len(calls) == 1 else (0, "")

    worker.execute = execute
    if expected[-1] == "unity" and len(expected) > 1:
        asyncio.run(worker.run_unity(tmp_path, 59))
    else:
        with pytest.raises(RuntimeError):
            asyncio.run(worker.run_unity(tmp_path, 59))
    assert calls == expected


def test_compiler_error_includes_unique_diagnostics(tmp_path):
    worker = make_worker(tmp_path)
    worker.unity = "Unity.exe"
    diagnostic = "Assets/Test.cs(3,2): error CS0161: missing return"
    (tmp_path / "unity-build.log").write_text((diagnostic + "\n") * 2)

    async def execute(command):
        return 1, "Scripts have compiler errors."

    worker.execute = execute
    with pytest.raises(module.UnityCompilationError) as error:
        asyncio.run(worker.run_unity(tmp_path, 59))
    assert str(error.value).count(diagnostic) == 1
