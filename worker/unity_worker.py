#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import signal
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import websockets

RATE_LIMIT_RE = re.compile(r"rate.?limit|usage.?limit|quota exceeded|\b429\b|try again later", re.I)
# A plain text line has to read like a CLI error report before it is checked for a limit.
ERROR_LINE_RE = re.compile(
    r"^(?:\W{0,3}|\[[^\]\n]{1,40}\]\s*|\d{2}:\d{2}(?::\d{2})?\W{1,3})"
    r"(?:error|api\s*error|fatal|exception|traceback|uncaught|rate.?limit|usage.?limit|quota|too\s+many\s+requests|reconnecting|retrying)\b",
    re.I,
)
RATE_LIMIT_TAIL_LINES = 20
KIMI_NODE = Path("/mnt/c/Program Files/nodejs/node.exe")
KIMI_ENTRY = Path("/mnt/e/WSL/ZelyaKimiWindows/node_modules/@moonshot-ai/kimi-code/dist/main.mjs")
ZCODE_NODE = Path("/mnt/c/Program Files/nodejs/node.exe")
PR_DETAILS_FILE = "orchestrator-pr.json"
PR_DETAILS_CONTRACT = (
    '{"purpose": string, "changes": string[], "implementation": string[], '
    '"before": string, "after": string, "validation": string[], "review_focus": string[]}'
)


def read_pr_details(path: Path) -> dict[str, Any]:
    """Read the implementation summary that becomes the public pull request body."""
    invalid = f"Файл {path.name} не соответствует контракту {PR_DETAILS_CONTRACT}: "
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Файл {path.name} не читается как JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(invalid + "нужен JSON-объект")
    result: dict[str, Any] = {}
    for key in ("purpose", "before", "after"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(invalid + f"{key} должен быть непустой строкой")
        result[key] = value.strip()
    for key in ("changes", "implementation"):
        items = data.get(key)
        if not isinstance(items, list) or not items or not all(isinstance(item, str) and item.strip() for item in items):
            raise RuntimeError(invalid + f"{key} должен быть непустым списком строк")
        result[key] = [item.strip() for item in items]
    for key in ("validation", "review_focus"):
        items = data.get(key, [])
        if not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items):
            raise RuntimeError(invalid + f"{key} должен быть списком непустых строк")
        result[key] = [item.strip() for item in items]
    return result


def markdown_list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def format_pr_body(
    details: dict[str, Any], task_id: int, unity_status: str, stat: str,
    external_issue_key: str = "", external_issue_url: str = "",
) -> str:
    sections = [
        f"## Цель\n\n{details['purpose']}",
        f"## Что сделано\n\n{markdown_list(details['changes'])}",
        f"## Как реализовано\n\n{markdown_list(details['implementation'])}",
        f"## Было\n\n{details['before']}",
        f"## Стало\n\n{details['after']}",
        f"## Проверка\n\n{markdown_list([unity_status, *details['validation']])}",
        f"## Область изменений\n\n```text\n{stat.strip() or 'Нет изменений относительно origin/main.'}\n```",
    ]
    if details["review_focus"]:
        sections.append(f"## На что обратить внимание при ревью\n\n{markdown_list(details['review_focus'])}")
    source = external_issue_key or "Исходная задача"
    if external_issue_url:
        source = f"[{source}]({external_issue_url})"
    elif not external_issue_key:
        source = ""
    footer = " · ".join(filter(None, [source, f"Задача оркестратора №{task_id}"]))
    sections.append(f"---\n\n{footer}")
    return "\n\n".join(sections)


def kimi_launcher() -> list[str]:
    if os.name != "nt" and KIMI_NODE.is_file() and KIMI_ENTRY.is_file():
        return [str(KIMI_NODE), windows_path(KIMI_ENTRY)]
    return [shutil.which("kimi") or "kimi"]


def windows_path(path: Path) -> str:
    text = path.as_posix()
    if text.startswith("/mnt/") and len(text) > 6 and text[6] == "/":
        return f"{text[5].upper()}:\\{text[7:].replace('/', '\\')}"
    return str(path)


def rate_limited_line(text: str) -> bool:
    """Whether an output line reports a spent subscription limit.

    Only error reports count: a JSON event that carries an error, or a text line that
    reads like an error report. Agent prose and tool results may mention limits without
    hitting one.
    """
    if not RATE_LIMIT_RE.search(text):
        return False
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return ERROR_LINE_RE.match(stripped) is not None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    kind = f"{data.get('type', '')} {data.get('subtype', '')}".lower()
    return data.get("is_error") is True or "error" in kind or "failed" in kind or "error" in data


def limited_in_tail(lines: list[str]) -> bool:
    # JSON keeps its event rule even here, so a failed run that ends with a non-error
    # event quoting a document is not mistaken for a limit.
    for line in lines[-RATE_LIMIT_TAIL_LINES:]:
        if rate_limited_line(line) if line.lstrip().startswith("{") else RATE_LIMIT_RE.search(line):
            return True
    return False


def kimi_environment(config: dict[str, Any]) -> dict[str, str]:
    environment = {"CI": "1", "NO_COLOR": "1", "KIMI_CODE_NO_AUTO_UPDATE": "1"}
    effort = str(config.get("reasoning_effort") or "").strip().lower()
    if effort == "medium":
        effort = "high"
    if effort in {"low", "high", "max"}:
        environment["KIMI_MODEL_THINKING_EFFORT"] = effort
    return environment


def kimi_command(config: dict[str, Any], prompt: str) -> list[str]:
    args = [*kimi_launcher(), "--output-format", "stream-json"]
    model = str(config.get("model") or "").strip()
    if model:
        args += ["-m", model]
    args += ["-p", prompt]
    return args


def zcode_launcher() -> list[str]:
    found = shutil.which("zcode")
    if found:
        return [found]
    node = Path(r"C:\Program Files\nodejs\node.exe") if os.name == "nt" else ZCODE_NODE
    pattern = Path.home().glob("AppData/Local/Programs/ZCode/resources/glm/zcode.cjs") if os.name == "nt" else Path("/mnt/c/Users").glob("*/AppData/Local/Programs/ZCode/resources/glm/zcode.cjs")
    entries = [path for path in pattern if path.is_file()]
    if node.is_file() and entries:
        return [str(node), windows_path(max(entries, key=lambda path: path.stat().st_mtime))]
    return ["zcode"]


def zcode_command(prompt: str) -> list[str]:
    return [*zcode_launcher(), "-p", prompt, "--mode", "yolo", "--no-color"]


def zcode_environment() -> dict[str, str]:
    environment = {"CI": "1", "NO_COLOR": "1"}
    launcher = zcode_launcher()
    if len(launcher) != 2:
        return environment
    entry = Path(launcher[1])
    resources = entry.parent.parent
    environment["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = str(resources / "config" / "provider" / "zcode-builtin.json")
    environment["ZCODE_CREDENTIALS_FILE"] = str(Path.home() / ".zcode" / "cli" / "credentials.json")
    return environment


class UnityWorker:
    def __init__(self, args: argparse.Namespace) -> None:
        self.server = args.server
        self.token = args.token
        self.repo = Path(args.repo).resolve()
        self.worktrees = Path(args.worktrees).resolve()
        self.unity = args.unity
        self.websocket: Any = None
        self.stopping = False
        self.jobs: dict[int, asyncio.Task[Any]] = {}
        self.processes: dict[int, asyncio.subprocess.Process] = {}

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
        # feat/task-N is kept for tasks whose branch was created under the old naming.
        if not re.fullmatch(r"(?:Agent-Orchestrator/(?:[A-Za-z0-9]+-\d+|task-\d+)|feat/task-\d+)", branch):
            raise ValueError("Некорректное имя ветки")
        path = self.worktrees / f"task-{task_id}"
        await self.checked(["git", "fetch", "origin", "main"], self.repo)
        if not path.exists():
            self.worktrees.mkdir(parents=True, exist_ok=True)
            code, _ = await self.execute(["git", "show-ref", "--verify", f"refs/heads/{branch}"], self.repo)
            if code == 0:
                await self.checked(["git", "branch", "-D", branch], self.repo)
            await self.checked(["git", "worktree", "add", "-b", branch, str(path), "origin/main"], self.repo)
        await self.prepare_library(path)
        return path

    async def prepare_library(self, path: Path) -> None:
        (path / "Library").mkdir(parents=True, exist_ok=True)

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

    def unity_command(self, path: Path) -> list[str]:
        return [
            self.unity, "-batchmode", "-quit", "-projectPath", windows_path(path),
            "-logFile", windows_path(path / "unity-build.log"),
        ]

    async def run_unity(self, path: Path, task_id: int) -> str:
        if not self.unity:
            return "Unity Editor не настроен; сборка не запускалась."
        with self.unity_mutex():
            code, output = await self.execute(self.unity_command(path))
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
        elif cli == "kimi":
            return kimi_command(config, prompt)
        elif cli == "zcode":
            return zcode_command(prompt)
        elif cli == "gemini":
            args = ["gemini", "-p", prompt]
        elif cli == "aider":
            args = ["aider", "--message", prompt, "--yes-always"]
        else:
            raise ValueError("Для Custom CLI укажите command")
        if model:
            args += ["--model", model]
        effort = str(config.get("reasoning_effort") or "").strip()
        if cli == "codex" and effort:
            args += ["-c", f'model_reasoning_effort="{effort}"']
        return args

    async def terminate_process(self, process: asyncio.subprocess.Process, timeout: float = 5) -> None:
        if process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await asyncio.wait_for(process.wait(), timeout)

    async def finish_consumer(self, consumer: asyncio.Task[Any], timeout: float = 5) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(consumer), timeout)
        except TimeoutError:
            consumer.cancel()
            with suppress(asyncio.CancelledError):
                await consumer

    async def run_agent(self, task_id: int, path: Path, config: dict[str, Any], prompt: str, timeout: int) -> dict[str, Any]:
        command = self.command(config, prompt, path)
        env = os.environ.copy()
        if config.get("cli") == "kimi":
            env.update(kimi_environment(config))
        if config.get("cli") == "zcode":
            env.update(zcode_environment())
        process = await asyncio.create_subprocess_exec(
            *command, cwd=path, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        self.processes[task_id] = process
        lines: list[str] = []
        limited = False

        async def handle_line(raw: bytes) -> bool:
            nonlocal limited
            text = raw.decode("utf-8", errors="replace")
            lines.append(text)
            await self.log(task_id, text)
            if not limited and rate_limited_line(text):
                limited = True
                await self.terminate_process(process)
                return False
            return True

        async def consume() -> None:
            assert process.stdout
            # Read by raw chunks: one stream-json event may be a line far above the 64 KiB readline limit.
            buffer = b""
            while chunk := await process.stdout.read(65536):
                buffer += chunk
                while (end := buffer.find(b"\n")) >= 0:
                    line, buffer = buffer[: end + 1], buffer[end + 1 :]
                    if not await handle_line(line):
                        return
            if buffer:
                await handle_line(buffer)

        consumer = asyncio.create_task(consume())
        timed_out = False
        try:
            try:
                await asyncio.wait_for(process.wait(), timeout * 60)
            except TimeoutError:
                timed_out = True
                await self.terminate_process(process)
        finally:
            if process.returncode is None:
                await self.terminate_process(process)
            await self.finish_consumer(consumer)
            self.processes.pop(task_id, None)
        output = "".join(lines)
        # A failed run is judged by the end of its output too: a text CLI may report the
        # limit on a line that does not look like an error report.
        if process.returncode == 0:
            limited = False
        elif not limited and not timed_out and limited_in_tail(lines):
            limited = True
        usage = parse_usage(prompt, output)
        return {"exit_code": process.returncode or 0, "output": output, "rate_limited": limited, "timed_out": timed_out, **usage}

    @staticmethod
    def config_label(config: dict[str, Any]) -> str:
        cli = str(config.get("cli") or "custom")
        model = str(config.get("model") or "").strip()
        return f"{cli} ({model})" if model else cli

    @staticmethod
    def failure_details(result: dict[str, Any], timeout: int) -> str | None:
        if result["rate_limited"]:
            return "превышен лимит запросов"
        if result["timed_out"]:
            return f"таймаут {timeout} мин"
        if result["exit_code"]:
            return f"код возврата {result['exit_code']}"
        return None

    async def run_with_fallback(self, task_id: int, path: Path, prompt: str, config: dict[str, Any], triggers: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        timeout = int(triggers.get("timeout_minutes", 20))
        configs = [config["primary"], *config.get("fallback", [])]
        failures: list[str] = []
        for index, candidate in enumerate(configs):
            label = self.config_label(candidate)
            if index:
                await self.log(task_id, f"Fallback: {label} — запуск\n")
            try:
                result = await self.run_agent(task_id, path, candidate, prompt, timeout)
            except Exception as exc:
                failures.append(f"{label}: {exc}")
                if index == len(configs) - 1:
                    break
                next_label = self.config_label(configs[index + 1])
                await self.log(task_id, f"{label}: не удалось запустить CLI ({exc}). Переключаюсь на fallback: {next_label}\n")
                continue
            details = self.failure_details(result, timeout)
            if details is None:
                return candidate, result
            failures.append(f"{label}: {details}")
            if index == len(configs) - 1:
                break
            retry = (
                (result["rate_limited"] and triggers.get("rate_limit", True))
                or (result["timed_out"] and triggers.get("timeout", True))
                or (bool(result["exit_code"]) and triggers.get("exit_code", True))
            )
            if not retry:
                break
            next_label = self.config_label(configs[index + 1])
            await self.log(task_id, f"{label}: {details}. Переключаюсь на fallback: {next_label}\n")
        raise RuntimeError("CLI завершился с ошибкой — " + "; ".join(failures))

    async def upsert_pr(self, path: Path, branch: str, title: str, body: str) -> str:
        pr_url = (await self.checked(["gh", "pr", "list", "--head", branch, "--json", "url", "--jq", ".[0].url"], path)).strip()
        if pr_url:
            await self.checked(["gh", "pr", "edit", pr_url, "--title", title, "--body", body], path)
            return pr_url
        return (await self.checked(["gh", "pr", "create", "--draft", "--title", title, "--body", body], path)).strip()

    async def assign(self, message: dict[str, Any]) -> None:
        task_id, run_id = int(message["task_id"]), int(message["run_id"])
        config = message["agent_config"]
        triggers = config.get("triggers", {})
        path: Path | None = None
        try:
            path = await self.prepare_worktree(task_id, message["branch_name"])
            docs = path / "orchestrator-tech-doc.md"
            details_path = path / PR_DETAILS_FILE
            docs.write_text(message.get("tech_doc_content", ""), encoding="utf-8")
            details_path.unlink(missing_ok=True)
            title = str(message.get("task_title") or "").strip() or f"Task {task_id}"
            description = str(message.get("task_description") or "").strip()
            review = message.get("review_comments_content", "")
            prompt = f"{config.get('role_prompt', '')}\nЗадача #{task_id}: {title}\n"
            if description:
                prompt += f"Исходное описание задачи:\n{description}\n"
            prompt += f"Реализуй задачу по {docs}.\n"
            if review:
                prompt += f"Замечания прошлого ревью (comments — блокеры, исправить обязательно; notes — учесть):\n{review}\n"
            prompt += (
                "Вноси только необходимые изменения. Не выполняй git commit/push и не создавай PR: это сделает воркер.\n"
                f"После реализации обязательно создай {details_path} по контракту {PR_DETAILS_CONTRACT}.\n"
                "Заполни его по фактически внесённым изменениям конкретно и без общих фраз: purpose — зачем нужна задача, "
                "changes — что сделано, implementation — какими способами и в каких местах это реализовано, "
                "before и after — поведение до и после изменений. В validation перечисли только проверки, которые ты действительно "
                "запустил, с командой и результатом; если таких нет, оставь список пустым. В review_focus укажи важные для ревью "
                "решения, ограничения или риски либо оставь список пустым. Не включай в JSON незапущенные проверки и выдуманные результаты."
            )
            selected, result = await self.run_with_fallback(task_id, path, prompt, config, triggers)
            unity_status = await self.run_unity(path, task_id)
            details = read_pr_details(details_path)
            docs.unlink(missing_ok=True)
            details_path.unlink(missing_ok=True)
            code, _ = await self.execute(["git", "diff", "--quiet"], path)
            if code:
                await self.checked(["git", "add", "-A"], path)
                await self.checked(["git", "commit", "-m", f"feat: implement task {task_id}"], path)
            await self.checked(["git", "push", "-u", "origin", message["branch_name"]], path)
            stat = await self.checked(["git", "diff", "--stat", "origin/main...HEAD"], path)
            pr_body = format_pr_body(
                details, task_id, unity_status, stat,
                str(message.get("external_issue_key") or ""), str(message.get("external_issue_url") or ""),
            )
            pr_url = await self.upsert_pr(path, message["branch_name"], title, pr_body)
            report = f"# Отчет реализации\n\n{pr_body}\n\n## Вывод CLI\n\n```text\n{result['output'][-20000:]}\n```"
            await self.send({"type": "TASK_COMPLETED", "task_id": task_id, "run_id": run_id, "report": report, "pr_url": pr_url, "worktree_path": str(path), "cli_used": selected.get("cli"), "model_used": selected.get("model", ""), "is_fallback": selected != config["primary"], **{k: v for k, v in result.items() if k in ("input_tokens", "output_tokens", "cache_tokens", "estimated")}})
            await self.send({"type": "TASK_META", "task_id": task_id, "tokens_estimated": result.get("estimated", False)})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.log(task_id, f"ERROR: {exc}\n")
            await self.send({"type": "TASK_FAILED", "task_id": task_id, "run_id": run_id, "error": str(exc)})

    async def cancel(self, task_id: int) -> None:
        process = self.processes.pop(task_id, None)
        if process and process.returncode is None:
            await self.terminate_process(process)
        job = self.jobs.pop(task_id, None)
        if job and not job.done():
            job.cancel()

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
                        task_id = int(message["task_id"])
                        task = asyncio.create_task(self.assign(message))
                        self.jobs[task_id] = task
                        running.add(task)
                        task.add_done_callback(running.discard)
                        task.add_done_callback(lambda _task, task_id=task_id: self.jobs.pop(task_id, None))
                    elif message.get("type") == "CANCEL_TASK":
                        await self.cancel(int(message["task_id"]))
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
