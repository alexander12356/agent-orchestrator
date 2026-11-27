import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.dispatcher import dispatcher
from app.repo import RepoError, prepare_checkout
from app.runner import RunResult
from conftest import make_task


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=path, check=True, capture_output=True, text=True)


def commit(path: Path, name: str, text: str) -> None:
    file = path / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding="utf-8")
    git(path, "add", name)
    git(path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", name)


def test_checkout_uses_task_branch_when_remote_has_it(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    commit(origin, "README.md", "main")
    git(origin, "checkout", "-b", "feat/task-7")
    commit(origin, "FEATURE.md", "task")
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")

    checkout, ref, revision = prepare_checkout(origin.as_uri(), 7, "feat/task-7")

    assert revision == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    assert ref == "feat/task-7"
    assert (checkout / "FEATURE.md").read_text(encoding="utf-8") == "task"


def test_checkout_uses_main_when_task_branch_is_absent(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    commit(origin, "README.md", "main")
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")

    checkout, ref, revision = prepare_checkout(origin.as_uri(), 8, "feat/task-8")

    assert revision == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    assert ref == "main"
    assert (checkout / "README.md").read_text(encoding="utf-8") == "main"
    assert not (checkout / "FEATURE.md").exists()


def test_checkout_uses_remote_branch_matching_external_task(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    commit(origin, "README.md", "main")
    git(origin, "checkout", "-b", "codex/zelya-task-uuid")
    commit(origin, "FEATURE.md", "card")
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")

    checkout, ref, revision = prepare_checkout(origin.as_uri(), 10, "feat/task-10", "task-uuid")

    assert revision == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    assert ref == "codex/zelya-task-uuid"
    assert (checkout / "FEATURE.md").read_text(encoding="utf-8") == "card"


@pytest.mark.parametrize("with_unity", [False, True])
def test_server_agent_receives_full_checkout_and_separate_artifact_dir(client, agent, tmp_path, monkeypatch, with_unity):
    async def start_manually(task_id):
        pass

    monkeypatch.setattr(dispatcher, "start", start_manually)
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    project = "Game/" if with_unity else ""
    commit(origin, project + "Assets/Windows/IWindowSystem.cs", "public interface IWindowSystem {}")
    if with_unity:
        commit(origin, "Game/Packages/manifest.json", "{}")
        commit(origin, "Game/ProjectSettings/ProjectVersion.txt", "m_EditorVersion: 6000.0.0f1")
    commit(origin, "Assets/Data/Catalog.asset", "Capacity: 2")
    which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/unity-cli" if name == "unity-cli" else which(name))
    if with_unity:
        config = {**agent["primary_config"], "command": "python backup.py"}
        assert client.put(f"/api/agents/{agent['id']}", json={**agent, "fallback_configs": [config]}).status_code == 200
    assert client.put("/api/settings", json={"repository_url": origin.as_uri()}).status_code == 200
    task = make_task(client, agent)
    seen = []

    class FakeRunner:
        def __init__(self, **kwargs):
            pass

        async def run(self, config, prompt, cwd, env, timeout_minutes):
            checkout = Path(env["ORCHESTRATOR_REPO"])
            assert checkout != cwd
            assert str(checkout) in prompt
            assert (checkout / project / "Assets/Windows/IWindowSystem.cs").is_file()
            assert (checkout / "Assets/Data/Catalog.asset").read_text(encoding="utf-8") == "Capacity: 2"
            assert "grep -l -F" in prompt
            assert "Начни с ТЗ" in prompt
            assert "постепенно расширяй область вплоть до всего Assets" in prompt
            if with_unity:
                index_root = Path(env["UNITY_PROJECT_ROOT"])
                assert not index_root.resolve().is_relative_to(checkout.resolve())
                assert (index_root / "Assets").resolve() == (checkout / "Game/Assets").resolve()
                assert env["UNITY_CLI_LSP_MODE"] == "off"
                assert "tool call find_symbol" in prompt
                seen.append(index_root)
                if config["command"] != "python backup.py":
                    return RunResult(1, "primary failed")
            else:
                assert "UNITY_PROJECT_ROOT" not in env
                assert "tool call find_symbol" not in prompt
            assert (cwd / "00_task.md").is_file()
            (cwd / "01_tech_doc_v1.md").write_text("# Doc", encoding="utf-8")
            seen.append(checkout)
            return RunResult(0, "ok")

    monkeypatch.setattr("app.dispatcher.ProcessRunner", FakeRunner)
    asyncio.run(dispatcher.run_stage(task["id"], 1))

    result = client.get(f"/api/tasks/{task['id']}").json()
    assert result["runs"][0]["status"] == "completed", result["runs"][0]["error"]
    assert len(seen) == (3 if with_unity else 1)
    if with_unity:
        assert seen[0] == seen[1]
        assert not seen[0].exists()


def test_missing_remote_branches_raise(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "develop")
    commit(origin, "README.md", "develop")
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")

    with pytest.raises(RepoError, match="нет ветки"):
        prepare_checkout(origin.as_uri(), 9, "feat/task-9")
