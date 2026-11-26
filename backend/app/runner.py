import asyncio
import contextlib
import json
import os
import re
import shlex
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

from .progress import progress_prompt
from .usage import extract_usage, zcode_usage

RATE_LIMIT_RE = re.compile(r"rate.?limit|usage.?limit|quota exceeded|\b429\b|try again later", re.I)
# A plain text line has to read like a CLI error report before it is checked for a limit.
ERROR_LINE_RE = re.compile(
    r"^(?:\W{0,3}|\[[^\]\n]{1,40}\]\s*|\d{2}:\d{2}(?::\d{2})?\W{1,3})"
    r"(?:error|api\s*error|ActionRequiredError|fatal|exception|traceback|uncaught|rate.?limit|usage.?limit|quota|too\s+many\s+requests|reconnecting|retrying)\b",
    re.I,
)
RATE_LIMIT_TAIL_LINES = 20
KIMI_NODE = Path("/home/paperclip/.local/opt/node-v24.15.0-linux-x64/bin/node")
KIMI_ENTRY = Path("/mnt/e/WSL/ZelyaKimiWindows/node_modules/@moonshot-ai/kimi-code/dist/main.mjs")
KIMI_HOME = Path("/mnt/c/Users/Meku/.kimi-code")


def signal_cli(process: asyncio.subprocess.Process, *, force: bool = False) -> None:
    with contextlib.suppress(ProcessLookupError):
        if os.name == "posix":
            # A CLI launcher may exit before its children close the output pipe.
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif force:
            process.kill()
        else:
            process.terminate()


@dataclass
class RunResult:
    exit_code: int
    output: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    estimated: bool = False
    rate_limited: bool = False
    timed_out: bool = False


@dataclass
class ProcessRunner:
    log_callback: Callable[[str], Awaitable[None]] | None = None
    rate_limit_callback: Callable[[], Awaitable[None]] | None = None
    on_process: Callable[[asyncio.subprocess.Process], None] | None = None
    on_prompt_sent: Callable[[str], None] | None = None

    async def run(
        self,
        config: dict[str, Any],
        prompt: str,
        cwd: Path,
        env: dict[str, str],
        timeout_minutes: int,
    ) -> RunResult:
        async with progress_prompt(prompt, cwd, self.log_callback) as prompt:
            return await self._run_cli(config, prompt, cwd, env, timeout_minutes)

    async def _run_cli(
        self, config: dict[str, Any], prompt: str, cwd: Path, env: dict[str, str], timeout_minutes: int,
    ) -> RunResult:
        command = build_command(config, prompt, cwd, int(env.get("ORCHESTRATOR_STAGE", "0")), Path(env["ORCHESTRATOR_REPO"]) if env.get("ORCHESTRATOR_REPO") else None)
        env = {**os.environ, **env, **cli_environment(config)}
        started = datetime.now(UTC)

        def measured_usage(output):
            usage = extract_usage(output, cwd, env.get("KIMI_CODE_HOME", Path.home() / ".kimi-code"))
            if not usage and Path(command[0]).stem.lower() == "zcode":
                usage = zcode_usage(cwd, started, datetime.now(UTC))
            return usage or {"estimated": True}
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=os.name == "posix",
            )
        except FileNotFoundError:
            return RunResult(127, f"CLI не найден: {command[0]}\n")
        if self.on_process:
            self.on_process(process)
        lines: list[str] = []
        limited = False
        async def handle_line(line: bytes) -> bool:
            nonlocal limited
            text = line.decode("utf-8", errors="replace")
            lines.append(text)
            if not limited and rate_limited_line(text):
                limited = True
                if self.rate_limit_callback:
                    await self.rate_limit_callback()
                return False
            return True

        async def consume() -> None:
            # Stream the output by raw chunks: a stream-json event that carries a
            # whole document is one line well above StreamReader's 64 KiB limit,
            # and readline() raises instead of returning it.
            assert process.stdout is not None
            buffer = b""
            while chunk := await process.stdout.read(65536):
                buffer += chunk
                while (end := buffer.find(b"\n")) >= 0:
                    line, buffer = buffer[: end + 1], buffer[end + 1 :]
                    if not await handle_line(line):
                        return
            if buffer:
                await handle_line(buffer)

        async def stop_cli() -> None:
            signal_cli(process, force=True)
            with contextlib.suppress(TimeoutError):
                # Drain buffered data so a paused pipe cannot block process cleanup.
                await asyncio.wait_for(process.communicate(), timeout=5)

        try:
            async with asyncio.timeout(timeout_minutes * 60):
                if self.on_prompt_sent:
                    self.on_prompt_sent(prompt)
                await consume()
                if not limited:
                    await process.wait()
        except TimeoutError:
            await stop_cli()
            output = "".join(lines)
            usage = {**measured_usage(output), "estimated": True}
            return RunResult(-1, output, **usage, rate_limited=limited, timed_out=not limited)
        except (asyncio.CancelledError, Exception):
            await stop_cli()
            raise
        if limited:
            await stop_cli()
        output = "".join(lines)
        # A failed run is judged by the end of its output too: a text CLI may report the
        # limit on a line that does not look like an error report.
        if not limited and (process.returncode or 0) != 0 and limited_in_tail(lines):
            limited = True
        usage = measured_usage(output)
        return RunResult(process.returncode or 0, output, **usage, rate_limited=limited)


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


def build_command(config: dict[str, Any], prompt: str, cwd: Path, stage: int = 0, repo: Path | None = None) -> list[str]:
    cli = config.get("cli", "custom")
    model = config.get("model", "")
    custom = config.get("command")
    if custom:
        values = {
            "prompt": prompt,
            "model": model,
            "artifact_dir": str(cwd),
            "task_id": cwd.name,
            "stage": str(stage),
            "repo": str(repo or ""),
        }
        parts = shlex.split(custom, posix=os.name != "nt")
        parts = [part[1:-1] if os.name == "nt" and len(part) > 1 and part[0] == part[-1] == '"' else part for part in parts]
        args = [part.format_map(values) for part in parts]
        if not any("{prompt}" in part for part in parts):
            args.append(prompt)
        return _with_live_output(_resolve_known_cli(args))
    if cli == "claude":
        args = ["claude", "-p", prompt, "--output-format", "json"]
    elif cli == "codex":
        args = ["codex", "exec", "--skip-git-repo-check", "-s", "workspace-write", "--json"]
        if model:
            args.extend(["--model", model])
        effort = str(config.get("reasoning_effort") or "").strip()
        if effort:
            args.extend(["-c", f'model_reasoning_effort="{effort}"'])
        args.append(prompt)
        return _with_live_output(_resolve_known_cli(args))
    elif cli == "kimi":
        args = ["kimi", "--output-format", "stream-json"]
        if model:
            args.extend(["-m", model])
        args.extend(["-p", prompt])
        return _with_live_output(_resolve_known_cli(args))
    elif cli == "zcode":
        # zcode-app-cli accepts an explicit provider/model id per run; agents
        # that leave the model empty fall back to the saved ZCode default.
        args = [zcode_program(), "-p", prompt, "--mode", "yolo", "--no-color"]
        if model:
            args[1:1] = ["--model", model]
        return _with_live_output(args)
    elif cli == "gemini":
        args = ["gemini", "-p", prompt]
    elif cli == "aider":
        args = ["aider", "--message", prompt, "--yes-always"]
    else:
        raise ValueError("Custom CLI requires a command")
    if model:
        args.extend(["--model", model])
    return _with_live_output(_resolve_known_cli(args))


def cli_environment(config: dict[str, Any]) -> dict[str, str]:
    command = str(config.get("command") or "")
    program = Path(shlex.split(command)[0]).name.lower().removesuffix(".exe") if command else ""
    if config.get("cli") == "zcode" or program == "zcode":
        return {"CI": "1", "NO_COLOR": "1"}
    if config.get("cli") != "kimi" and program != "kimi":
        return {}
    environment = {"CI": "1", "NO_COLOR": "1", "KIMI_CODE_NO_AUTO_UPDATE": "1"}
    if kimi_launcher() and KIMI_HOME.is_dir():
        environment["KIMI_CODE_HOME"] = str(KIMI_HOME)
    effort = resolve_kimi_effort(str(config.get("reasoning_effort") or ""))
    if effort:
        environment["KIMI_MODEL_THINKING_EFFORT"] = effort
    return environment


def resolve_kimi_effort(effort: str) -> str:
    normalized = effort.strip().lower()
    if normalized == "medium":
        return "high"
    if normalized in {"low", "high", "max"}:
        return normalized
    return ""


def _resolve_known_cli(args: list[str]) -> list[str]:
    if not args:
        return args
    program = Path(args[0]).name.lower().removesuffix(".exe")
    if program not in {"codex", "kimi"}:
        return args
    if program == "kimi":
        launcher = kimi_launcher()
        if launcher:
            return [*launcher, *args[1:]]
    return [resolve_executable(program), *args[1:]]


def kimi_launcher() -> list[str]:
    if os.name != "nt" and KIMI_NODE.is_file() and KIMI_ENTRY.is_file():
        return [str(KIMI_NODE), str(KIMI_ENTRY)]
    return []


def zcode_program() -> str:
    return resolve_executable("zcode")


def resolve_executable(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    candidates: list[Path] = []
    if name == "codex" and os.name != "nt":
        # Server prompts and the workspace sandbox use paths in the Linux filesystem.
        candidates.append(Path.home() / ".local" / "bin" / "codex")
    if name == "kimi":
        candidates.extend([
            Path("/home/paperclip/.local/bin/kimi"),
            Path.home() / ".local" / "bin" / "kimi",
            Path("/usr/local/bin/kimi"),
        ])
    if name == "zcode":
        candidates.extend([
            Path("/usr/local/bin/zcode"),
            Path.home() / ".local" / "bin" / "zcode",
        ])
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return name
    return str(max(existing, key=lambda path: path.stat().st_mtime))


def _with_live_output(args: list[str]) -> list[str]:
    if not args or Path(args[0]).name.lower() not in {"agent", "agent.exe"}:
        return args
    rewritten: list[str] = []
    skip = False
    replaced = False
    for index, arg in enumerate(args):
        if skip:
            skip = False
            continue
        if arg == "--output-format" and index + 1 < len(args):
            rewritten.extend(["--output-format", "stream-json"])
            skip = True
            replaced = True
            continue
        rewritten.append(arg)
    if not replaced:
        rewritten[1:1] = ["--output-format", "stream-json"]
    if "--stream-partial-output" not in rewritten:
        rewritten.insert(1, "--stream-partial-output")
    return rewritten
