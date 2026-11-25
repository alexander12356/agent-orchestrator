import hashlib
import os
import subprocess
from pathlib import Path

from .config import settings


class RepoError(RuntimeError):
    pass


def repository_url(task: dict, configured: str = "") -> str:
    return str(task.get("repository_url") or configured or "").strip()


def prepare_checkout(url: str, task_id: int, branch: str, external_id: str = "") -> tuple[Path, str, str]:
    ref = _select_branch(url, branch, external_id)
    mirror = _ensure_mirror(url)
    _git(["fetch", "--depth", "50", "origin", f"+refs/heads/{ref}:refs/heads/{ref}"], mirror)
    commit = _git(["rev-parse", f"refs/heads/{ref}^{{commit}}"], mirror).strip()
    if ref not in {"main", "master"}:
        for base in ("main", "master"):
            try:
                _git(["fetch", "--depth", "50", "origin", f"+refs/heads/{base}:refs/heads/{base}"], mirror)
                break
            except RepoError:
                continue
    checkout = settings.storage_dir / "checkouts" / str(task_id)
    checkout.parent.mkdir(parents=True, exist_ok=True)
    if (checkout / ".git").exists():
        _git(["reset", "--hard", commit], checkout)
        _git(["clean", "-fd"], checkout)
    else:
        _git(["worktree", "add", "--detach", str(checkout), commit], mirror)
    return checkout, ref, commit


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
