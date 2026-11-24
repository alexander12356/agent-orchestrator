import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.repo import RepoError, export_task_files, prepare_checkout


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

    checkout, ref = prepare_checkout(origin.as_uri(), 7, "feat/task-7")

    assert ref == "feat/task-7"
    assert (checkout / "FEATURE.md").read_text(encoding="utf-8") == "task"


def test_checkout_uses_main_when_task_branch_is_absent(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    commit(origin, "README.md", "main")
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")

    checkout, ref = prepare_checkout(origin.as_uri(), 8, "feat/task-8")

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

    checkout, ref = prepare_checkout(origin.as_uri(), 10, "feat/task-10", "task-uuid")

    assert ref == "codex/zelya-task-uuid"
    assert (checkout / "FEATURE.md").read_text(encoding="utf-8") == "card"


def test_export_keeps_only_files_named_by_the_task(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    commit(origin, "AGENTS.md", "rules")
    commit(origin, "Docs/agent/drafts/ZEL-17.md", "draft")
    commit(origin, "Docs/ZEL-15.md", "related")
    commit(origin, "Assets/Scripts/Gameplay/Node.cs", "class Node {}")
    commit(origin, "Assets/Art/hero.png", "not-text")
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    checkout, _ref = prepare_checkout(origin.as_uri(), 17, "feat/task-17")

    copied, missing = export_task_files(
        checkout,
        tmp_path / "repo",
        "Нужны ZEL-17 и ZEL-15. Каталог Assets/Scripts/Gameplay/. Черновика ZEL-99 нет.",
    )

    assert copied[:3] == ["AGENTS.md", "Docs/ZEL-15.md", "Docs/agent/drafts/ZEL-17.md"]
    assert "Assets/Scripts/Gameplay/Node.cs" in copied
    assert "Assets/Art/hero.png" not in copied
    assert missing == ["ZEL-99"]
    assert (tmp_path / "repo" / "Docs" / "agent" / "drafts" / "ZEL-17.md").read_text(encoding="utf-8") == "draft"


def test_missing_remote_branches_raise(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "develop")
    commit(origin, "README.md", "develop")
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")

    with pytest.raises(RepoError, match="нет ветки"):
        prepare_checkout(origin.as_uri(), 9, "feat/task-9")
