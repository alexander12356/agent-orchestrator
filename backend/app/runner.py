import asyncio
import json
import os
import re
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

RATE_LIMIT_RE = re.compile(r"rate.?limit|usage.?limit|quota exceeded|\b429\b|try again later", re.I)
# A plain text line has to read like a CLI error report before it is checked for a limit.
ERROR_LINE_RE = re.compile(
    r"^(?:\W{0,3}|\[[^\]\n]{1,40}\]\s*|\d{2}:\d{2}(?::\d{2})?\W{1,3})"
    r"(?:error|api\s*error|fatal|exception|traceback|uncaught|rate.?limit|usage.?limit|quota|too\s+many\s+requests|reconnecting|retrying)\b",
    re.I,
)
RATE_LIMIT_TAIL_LINES = 20
KIMI_NODE = Path("/home/paperclip/.local/opt/node-v24.15.0-linux-x64/bin/node")
KIMI_ENTRY = Path("/mnt/e/WSL/ZelyaKimiWindows/node_modules/@moonshot-ai/kimi-code/dist/main.mjs")
KIMI_HOME = Path("/mnt/c/Users/Meku/.kimi-code")


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

    async def run(
        self,
        config: dict[str, Any],
        prompt: str,
        cwd: Path,
        env: dict[str, str],
        timeout_minutes: int,
    ) -> RunResult:
        command = build_command(config, prompt, cwd, int(env.get("ORCHESTRATOR_STAGE", "0")), Path(env["ORCHESTRATOR_REPO"]) if env.get("ORCHESTRATOR_REPO") else None)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env={**os.environ, **env, **cli_environment(config)},
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return RunResult(127, f"CLI не найден: {command[0]}\n")
        if self.on_process:
            self.on_process(process)
        lines: list[str] = []
        limited = False
        thinking = False

        async def handle_line(line: bytes) -> bool:
            nonlocal limited, thinking
            text = line.decode("utf-8", errors="replace")
            lines.append(text)
            visible = present_log_line(text, thinking)
            thinking = visible.thinking
            if self.log_callback and visible.text:
                await self.log_callback(visible.text)
            if not limited and rate_limited_line(text):
                limited = True
                if self.rate_limit_callback:
                    await self.rate_limit_callback()
                process.terminate()
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

        consumer = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_minutes * 60)
        except TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()
            await consumer
            return RunResult(-1, "".join(lines), timed_out=True)
        try:
            await consumer
        except Exception:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        output = "".join(lines)
        # A failed run is judged by the end of its output too: a text CLI may report the
        # limit on a line that does not look like an error report.
        if not limited and (process.returncode or 0) != 0 and limited_in_tail(lines):
            limited = True
        usage = extract_usage(output)
        if not usage:
            usage = estimate_usage(prompt, output)
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
        args = ["codex", "exec", "--skip-git-repo-check", "-s", "workspace-write"]
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


@dataclass
class LogLine:
    text: str = ""
    thinking: bool = False


def present_log_line(text: str, thinking: bool = False) -> LogLine:
    raw = text.strip()
    if not raw.startswith("{"):
        return LogLine(text if text.endswith("\n") or not text else f"{text}\n", thinking)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return LogLine(text if text.endswith("\n") else f"{text}\n", thinking)
    if not isinstance(data, dict):
        return LogLine("", thinking)
    kind = str(data.get("type") or "")
    if kind in {"system", "user", "result"}:
        return LogLine("", thinking)
    if kind in {"thinking", "reasoning"}:
        return LogLine("" if thinking else "Агент думает…\n", True)
    if kind == "tool_call":
        if data.get("subtype") == "completed":
            return LogLine("", thinking)
        return LogLine(f"[{_tool_name(data)}]\n", thinking)
    if kind == "assistant":
        message = data.get("message", {})
        content = message.get("content") if isinstance(message, dict) else data.get("content")
        chunks: list[str] = []
        reasoning = False
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"thinking", "reasoning"}:
                    reasoning = True
                    continue
                if part.get("type") == "text" and part.get("text"):
                    chunks.append(str(part["text"]))
        if chunks:
            return LogLine("".join(chunks), thinking)
        if reasoning:
            return LogLine("" if thinking else "Агент думает…\n", True)
    return LogLine("", thinking)


def _tool_name(data: dict[str, Any]) -> str:
    call = data.get("tool_call") or data.get("toolCall") or {}
    if isinstance(call, dict):
        if call.get("name"):
            return str(call["name"])
        for key in call:
            if key.lower().endswith("toolcall"):
                return re.sub(r"ToolCall$", "", key)
    return str(data.get("name") or "tool")


def extract_usage(output: str) -> dict[str, int | bool]:
    for line in reversed(output.splitlines()):
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        result = data.get("result")
        candidates = [data, data.get("usage"), result.get("usage") if isinstance(result, dict) else None]
        for usage in candidates:
            if not isinstance(usage, dict):
                continue
            input_tokens = usage.get("input_tokens", usage.get("inputTokens", usage.get("prompt_tokens", 0))) or 0
            output_tokens = usage.get("output_tokens", usage.get("outputTokens", usage.get("completion_tokens", 0))) or 0
            cache_tokens = usage.get("cache_read_input_tokens", usage.get("cacheReadTokens", usage.get("cached_tokens", 0))) or 0
            if input_tokens or output_tokens:
                return {
                    "input_tokens": int(input_tokens),
                    "output_tokens": int(output_tokens),
                    "cache_tokens": int(cache_tokens),
                    "estimated": False,
                }
    match = re.findall(r"(?:input|prompt)[ _-]?tokens?[:= ]+(\d+)", output, re.I)
    output_match = re.findall(r"(?:output|completion)[ _-]?tokens?[:= ]+(\d+)", output, re.I)
    if match or output_match:
        return {
            "input_tokens": int(match[-1]) if match else 0,
            "output_tokens": int(output_match[-1]) if output_match else 0,
            "cache_tokens": 0,
            "estimated": False,
        }
    return {}


def estimate_usage(prompt: str, output: str) -> dict[str, int | bool]:
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        count = lambda text: len(encoding.encode(text))
    except (ImportError, ValueError):
        count = lambda text: max(1, len(text) // 4)
    return {"input_tokens": count(prompt), "output_tokens": count(output), "cache_tokens": 0, "estimated": True}
