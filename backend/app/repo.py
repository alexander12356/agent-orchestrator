import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import settings


class RepoError(RuntimeError):
    pass


def repository_url(task: dict, configured: str = "") -> str:
    return str(task.get("repository_url") or configured or "").strip()


TEXT_SUFFIXES = {".md", ".txt", ".cs", ".json", ".yml", ".yaml", ".asmdef", ".xml"}
MAX_EXPORT_FILES = 40
MAX_EXPORT_BYTES = 200_000
ISSUE_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
PATH_RE = re.compile(r"(?<![\w@])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+/?)")
BLOB_RE = re.compile(r"blob/[0-9a-fA-F]{7,40}/([^\s)#]+)")


def export_task_files(checkout: Path, destination: Path, text: str) -> tuple[list[str], list[str]]:
    names = set(_tracked_files(checkout))
    chosen: list[str] = []
    missing: list[str] = []

    def add_file(relative: str) -> bool:
        relative = relative.strip().lstrip("./")
        if not relative or relative in chosen or len(chosen) >= MAX_EXPORT_FILES:
            return False
        if relative not in names:
            return False
        path = checkout / relative
        if not path.is_file() or path.stat().st_size > MAX_EXPORT_BYTES:
            return False
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != "AGENTS.md":
            return False
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        chosen.append(relative)
        return True

    def add_tree(prefix: str) -> bool:
        prefix = prefix.strip().lstrip("./")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        matched = sorted(name for name in names if name.startswith(prefix))
        added = False
        for name in matched:
            if add_file(name):
                added = True
            if len(chosen) >= MAX_EXPORT_FILES:
                break
        return added

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if not add_file("AGENTS.md"):
        missing.append("AGENTS.md")
    for key in sorted(set(ISSUE_RE.findall(text))):
        matches = sorted(name for name in names if key in name and (name.startswith("Docs/") or name.endswith(".md")))
        if not matches:
            missing.append(key)
            continue
        for name in matches[:6]:
            add_file(name)
    for mention in _mentioned_paths(text):
        if mention in chosen:
            continue
        if mention in names:
            if not add_file(mention):
                missing.append(mention)
            continue
        prefix = mention if mention.endswith("/") else f"{mention}/"
        if any(name.startswith(prefix) for name in names):
            add_tree(prefix)
            continue
        if mention.startswith(("Docs/", "Assets/", "Packages/", "ProjectSettings/")) or mention == "AGENTS.md":
            missing.append(mention.rstrip("/"))
    if not chosen and destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    return chosen, missing


def _mentioned_paths(text: str) -> list[str]:
    found: list[str] = []
    for pattern in (BLOB_RE, PATH_RE):
        for match in pattern.finditer(text):
            path = match.group(1).strip("`").rstrip(").,;\"'")
            if ".." in path.split("/") or path in found:
                continue
            found.append(path)
    return found


def _tracked_files(checkout: Path) -> list[str]:
    output = _git(["ls-tree", "-r", "--name-only", "HEAD"], checkout)
    return [line for line in output.splitlines() if line]


def prepare_checkout(url: str, task_id: int, branch: str, external_id: str = "") -> tuple[Path, str]:
    ref = _select_branch(url, branch, external_id)
    mirror = _ensure_mirror(url)
    _git(["fetch", "--depth", "1", "origin", f"+refs/heads/{ref}:refs/heads/{ref}"], mirror)
    checkout = settings.storage_dir / "checkouts" / str(task_id)
    checkout.parent.mkdir(parents=True, exist_ok=True)
    if (checkout / ".git").exists():
        _git(["reset", "--hard", ref], checkout)
        _git(["clean", "-fd"], checkout)
    else:
        _git(["worktree", "add", "--detach", str(checkout), ref], mirror)
    return checkout, ref


def _select_branch(url: str, branch: str, external_id: str) -> str:
    heads = _remote_heads(url)
    if branch in heads:
        return branch
    marker = external_id.strip()
    if marker:
        matched = [name for name in heads if marker in name]
        if matched:
            return sorted(matched, key=len)[0]
    for fallback in ("main", "master"):
        if fallback in heads:
            return fallback
    raise RepoError("На удалённом репозитории нет ветки задачи, main или master")


def _remote_heads(url: str) -> list[str]:
    heads = []
    for line in _git(["ls-remote", "--heads", url]).splitlines():
        _sha, ref = line.split()
        heads.append(ref.removeprefix("refs/heads/"))
    return heads


def _ensure_mirror(url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    mirror = settings.storage_dir / "mirrors" / digest
    if (mirror / "HEAD").exists():
        return mirror
    mirror.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", "--bare", "--depth", "1", url, str(mirror)])
    return mirror


def _git(args: list[str], cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=cwd,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **_credential_env()},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git завершился с ошибкой").strip()
        raise RepoError(detail.splitlines()[-1])
    return completed.stdout.strip()


def _credential_env() -> dict[str, str]:
    manager = Path("/mnt/c/Program Files/Git/mingw64/bin/git-credential-manager.exe")
    if not manager.is_file():
        return {}
    helper = settings.storage_dir / "git-credential-helper"
    helper.parent.mkdir(parents=True, exist_ok=True)
    script = '#!/bin/sh\nexec "/mnt/c/Program Files/Git/mingw64/bin/git-credential-manager.exe" "$@"\n'
    if not helper.exists() or helper.read_text(encoding="utf-8") != script:
        helper.write_text(script, encoding="utf-8")
        helper.chmod(0o755)
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": str(helper)}
