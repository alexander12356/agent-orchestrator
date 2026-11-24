#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
import shlex
import signal
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import websockets

RATE_LIMIT_RE = re.compile(r"rate.?limit|usage.?limit|quota exceeded|\b429\b|try again later", re.I)


class UnityWorker:
    def __init__(self, args: argparse.Namespace) -> None:
        self.server = args.server
        self.token = args.token
        self.repo = Path(args.repo).resolve()
        self.worktrees = Path(args.worktrees).resolve()
        self.library = Path(args.library).resolve() if args.library else None
        self.unity = args.unity
        self.websocket: Any = None
        self.stopping = False

    async def send(self, message: dict[str, Any]) -> None:
        if self.websocket:
            await self.websocket.send(json.dumps(message, ensure_ascii=False))

    async def log(self, task_id: int, text: str) -> None:
        print(text, end="", flush=True)
        await self.send({"type": "LOG_CHUNK", "task_id": task_id, "chunk": text})

    async def execute(self, command: list[str], cwd: Path | None = None) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(*command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        assert process.stdout
        output, _ = await process.communicate()
        return process.returncode or 0, output.decode("utf-8", errors="replace")

    async def checked(self, command: list[str], cwd: Path | None = None) -> str:
        code, output = await self.execute(command, cwd)
        if code:
            raise RuntimeError(f"{' '.join(command)} завершился с кодом {code}:\n{output}")
        return output

    async def prepare_worktree(self, task_id: int, branch: str) -> Path:
        if not re.fullmatch(r"feat/task-\d+", branch):
            raise ValueError("Некорректное имя ветки")
        path = self.worktrees / f"task-{task_id}"
        await self.checked(["git", "fetch", "origin", "main"], self.repo)
        if path.exists():
            await self.checked(["git", "reset", "--hard", "origin/main"], path)
        else:
            self.worktrees.mkdir(parents=True, exist_ok=True)
            code, _ = await self.execute(["git", "show-ref", "--verify", f"refs/heads/{branch}"], self.repo)
            if code == 0:
                await self.checked(["git", "branch", "-D", branch], self.repo)
            await self.checked(["git", "worktree", "add", "-b", branch, str(path), "origin/main"], self.repo)
        await self.link_library(path)
        return path

    async def link_library(self, path: Path) -> None:
        if not self.library or not self.library.exists():
            return
        target = path / "Library"
        if target.exists() or target.is_symlink():
            return
        if os.name == "nt":
            await self.checked(["cmd", "/c", "mklink", "/J", str(target), str(self.library)])
        else:
            target.symlink_to(self.library, target_is_directory=True)

    @contextmanager
    def unity_mutex(self):
        lock_path = self.worktrees / ".unity.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                if not lock.read(1):
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    async def run_unity(self, path: Path, task_id: int) -> str:
        if not self.unity:
            return "Unity Editor не настроен; сборка не запускалась."
        with self.unity_mutex():
            code, output = await self.execute([
                self.unity, "-batchmode", "-quit", "-projectPath", str(path), "-logFile", str(path / "unity-build.log")
            ])
        if code:
            raise RuntimeError(f"Unity batchmode завершился с кодом {code}: {output}")
        return "Unity batchmode завершился успешно."

    def command(self, config: dict[str, Any], prompt: str, path: Path) -> list[str]:
        cli, model, custom = config.get("cli", "custom"), config.get("model", ""), config.get("command")
        if custom:
            parts = shlex.split(custom, posix=os.name != "nt")
            parts = [part[1:-1] if os.name == "nt" and len(part) > 1 and part[0] == part[-1] == '"' else part for part in parts]
            values = {"prompt": prompt, "model": model, "artifact_dir": str(path)}
            args = [part.format_map(values) for part in parts]
            if not any("{prompt}" in part for part in parts):
                args.append(prompt)
            return args
        if cli == "claude":
            args = ["claude", "-p", prompt, "--output-format", "json"]
        elif cli == "codex":
            args = ["codex", "exec", prompt]
        elif cli == "gemini":
            args = ["gemini", "-p", prompt]
        elif cli == "aider":
            args = ["aider", "--message", prompt, "--yes-always"]
        else:
            raise ValueError("Для Custom CLI укажите command")
        if model:
            args += ["--model", model]
        return args

    async def run_agent(self, task_id: int, path: Path, config: dict[str, Any], prompt: str, timeout: int) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            *self.command(config, prompt, path), cwd=path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        lines: list[str] = []
        limited = False

        async def consume() -> None:
            nonlocal limited
            assert process.stdout
            while raw := await process.stdout.readline():
                text = raw.decode("utf-8", errors="replace")
                lines.append(text)
                await self.log(task_id, text)
                if RATE_LIMIT_RE.search(text):
                    limited = True
                    process.terminate()

        consumer = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(process.wait(), timeout * 60)
            timed_out = False
        except TimeoutError:
            timed_out = True
            process.terminate()
            await process.wait()
        await consumer
        output = "".join(lines)
        usage = parse_usage(prompt, output)
        return {"exit_code": process.returncode or 0, "output": output, "rate_limited": limited, "timed_out": timed_out, **usage}

    async def assign(self, message: dict[str, Any]) -> None:
        task_id, run_id = int(message["task_id"]), int(message["run_id"])
        config = message["agent_config"]
        triggers = config.get("triggers", {})
        path: Path | None = None
        selected: dict[str, Any] = {}
        try:
            path = await self.prepare_worktree(task_id, message["branch_name"])
            docs = path / "orchestrator-tech-doc.md"
            docs.write_text(message.get("tech_doc_content", ""), encoding="utf-8")
            review = message.get("review_comments_content", "")
            prompt = f"{config.get('role_prompt', '')}\nРеализуй задачу по {docs}.\n"
            if review:
                prompt += f"Замечания прошлого ревью:\n{review}\n"
            prompt += "Вноси только необходимые изменения. Не выполняй git commit/push: это сделает воркер."
            result = None
            configs = [config["primary"], *config.get("fallback", [])]
            for index, selected in enumerate(configs):
                result = await self.run_agent(task_id, path, selected, prompt, int(triggers.get("timeout_minutes", 20)))
                retry = ((result["rate_limited"] and triggers.get("rate_limit", True)) or
                         (result["timed_out"] and triggers.get("timeout", True)) or
                         (result["exit_code"] and triggers.get("exit_code", True)))
                if not retry or index == len(configs) - 1:
                    break
                await self.log(task_id, f"Переключение на fallback: {selected.get('cli')}\n")
            assert result
            if result["exit_code"] or result["timed_out"]:
                raise RuntimeError("CLI завершился с ошибкой")
            unity_status = await self.run_unity(path, task_id)
            docs.unlink(missing_ok=True)
            code, _ = await self.execute(["git", "diff", "--quiet"], path)
            if code:
                await self.checked(["git", "add", "-A"], path)
                await self.checked(["git", "commit", "-m", f"feat: implement task {task_id}"], path)
            await self.checked(["git", "push", "-u", "origin", message["branch_name"]], path)
            code, pr_url = await self.execute(["gh", "pr", "list", "--head", message["branch_name"], "--json", "url", "--jq", ".[0].url"], path)
            pr_url = pr_url.strip()
            if not pr_url:
                pr_url = (await self.checked(["gh", "pr", "create", "--draft", "--title", f"Task {task_id}", "--body", f"Автоматическая реализация задачи #{task_id}"], path)).strip()
            stat = await self.checked(["git", "diff", "--stat", "origin/main...HEAD"], path)
            report = f"# Отчет реализации\n\n{unity_status}\n\n## Git diff --stat\n\n```text\n{stat}\n```\n\n## Вывод CLI\n\n```text\n{result['output'][-20000:]}\n```"
            await self.send({"type": "TASK_COMPLETED", "task_id": task_id, "run_id": run_id, "report": report, "pr_url": pr_url, "worktree_path": str(path), "cli_used": selected.get("cli"), "model_used": selected.get("model", ""), "is_fallback": selected != config["primary"], **{k: v for k, v in result.items() if k in ("input_tokens", "output_tokens", "cache_tokens", "estimated")}})
            await self.send({"type": "TASK_META", "task_id": task_id, "tokens_estimated": result.get("estimated", False)})
        except Exception as exc:
            await self.log(task_id, f"ERROR: {exc}\n")
            await self.send({"type": "TASK_FAILED", "task_id": task_id, "run_id": run_id, "error": str(exc)})

    async def cleanup(self, message: dict[str, Any]) -> None:
        task_id = int(message["task_id"])
        path = Path(message.get("worktree_path") or self.worktrees / f"task-{task_id}").resolve()
        if path.parent != self.worktrees:
            raise ValueError("Worktree находится вне разрешенной директории")
        if path.exists():
            await self.checked(["git", "worktree", "remove", "--force", str(path)], self.repo)
        await self.execute(["git", "branch", "-D", message["branch_name"]], self.repo)
        await self.execute(["git", "push", "origin", "--delete", message["branch_name"]], self.repo)

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(10)
            await self.send({"type": "HEARTBEAT"})

    async def connect(self) -> None:
        async with websockets.connect(self.server, additional_headers={"Authorization": f"Bearer {self.token}"}, ping_interval=20) as websocket:
            self.websocket = websocket
            heartbeat = asyncio.create_task(self.heartbeat())
            running: set[asyncio.Task[Any]] = set()
            try:
                async for raw in websocket:
                    message = json.loads(raw)
                    if message.get("type") == "ASSIGN_TASK":
                        task = asyncio.create_task(self.assign(message))
                        running.add(task)
                        task.add_done_callback(running.discard)
                    elif message.get("type") == "CLEANUP_WORKTREE":
                        await self.cleanup(message)
            finally:
                heartbeat.cancel()
                for task in running:
                    task.cancel()
                self.websocket = None

    async def run(self) -> None:
        while not self.stopping:
            try:
                await self.connect()
            except (OSError, websockets.WebSocketException) as exc:
                print(f"Подключение потеряно: {exc}; повтор через 5 секунд")
            if not self.stopping:
                await asyncio.sleep(5)


def parse_usage(prompt: str, output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            data = json.loads(line)
            usage = data.get("usage", data)
            if usage.get("input_tokens") or usage.get("output_tokens"):
                return {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0), "cache_tokens": usage.get("cache_read_input_tokens", 0), "estimated": False}
        except (json.JSONDecodeError, AttributeError):
            pass
    try:
        import tiktoken
        encoding = tiktoken.get_encoding("cl100k_base")
        count = lambda value: len(encoding.encode(value))
    except (ImportError, ValueError):
        count = lambda value: max(1, len(value) // 4)
    return {"input_tokens": count(prompt), "output_tokens": count(output), "cache_tokens": 0, "estimated": True}


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальный Unity Worker")
    parser.add_argument("--server", default=os.getenv("ORCHESTRATOR_WS", "ws://localhost:8000/ws/worker"))
    parser.add_argument("--token", default=os.getenv("ORCHESTRATOR_WORKER_TOKEN", "change-me-worker-token"))
    parser.add_argument("--repo", default=os.getenv("UNITY_REPO", "."))
    parser.add_argument("--worktrees", default=os.getenv("UNITY_WORKTREES", "./worktrees"))
    parser.add_argument("--library", default=os.getenv("UNITY_REFERENCE_LIBRARY"))
    parser.add_argument("--unity", default=os.getenv("UNITY_EDITOR"))
    args = parser.parse_args()
    worker = UnityWorker(args)
    try:
        asyncio.run(worker.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
