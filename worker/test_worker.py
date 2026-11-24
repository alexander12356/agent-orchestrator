import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace


spec = importlib.util.spec_from_file_location('unity_worker', Path(__file__).with_name('unity_worker.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_parse_structured_usage():
    result = module.parse_usage('prompt', '{"usage":{"input_tokens":20,"output_tokens":5}}')
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


def test_parse_usage_estimate():
    assert module.parse_usage('prompt', 'plain output')['estimated'] is True


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
    path.mkdir(parents=True)
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


def test_finish_consumer_cancels_a_stuck_reader(tmp_path):
    worker = make_worker(tmp_path)

    async def scenario():
        consumer = asyncio.create_task(asyncio.Event().wait())
        await worker.finish_consumer(consumer, timeout=0.01)
        assert consumer.cancelled()

    asyncio.run(scenario())
