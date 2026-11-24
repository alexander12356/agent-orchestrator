import asyncio
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

RATE_LIMIT_RE = re.compile(r"rate.?limit|usage.?limit|quota exceeded|\b429\b|try again later", re.I)


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

    async def run(
        self,
        config: dict[str, Any],
        prompt: str,
        cwd: Path,
        env: dict[str, str],
        timeout_minutes: int,
    ) -> RunResult:
        command = build_command(config, prompt, cwd, int(env.get("ORCHESTRATOR_STAGE", "0")))
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env={**os.environ, **env},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        lines: list[str] = []
        limited = False

        async def consume() -> None:
            nonlocal limited
            assert process.stdout is not None
            while line := await process.stdout.readline():
                text = line.decode("utf-8", errors="replace")
                lines.append(text)
                if self.log_callback:
                    await self.log_callback(text)
                if RATE_LIMIT_RE.search(text) and not limited:
                    limited = True
                    if self.rate_limit_callback:
                        await self.rate_limit_callback()
                    process.terminate()

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
        await consumer
        output = "".join(lines)
        usage = extract_usage(output)
        if not usage:
            usage = estimate_usage(prompt, output)
        return RunResult(process.returncode or 0, output, **usage, rate_limited=limited)


def build_command(config: dict[str, Any], prompt: str, cwd: Path, stage: int = 0) -> list[str]:
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
        }
        parts = shlex.split(custom, posix=os.name != "nt")
        parts = [part[1:-1] if os.name == "nt" and len(part) > 1 and part[0] == part[-1] == '"' else part for part in parts]
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
        raise ValueError("Custom CLI requires a command")
    if model:
        args.extend(["--model", model])
    return args


def extract_usage(output: str) -> dict[str, int | bool]:
    for line in reversed(output.splitlines()):
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = [data, data.get("usage", {}), data.get("result", {}).get("usage", {}) if isinstance(data.get("result"), dict) else {}]
        for usage in candidates:
            if not isinstance(usage, dict):
                continue
            input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
            output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
            cache_tokens = usage.get("cache_read_input_tokens", usage.get("cached_tokens", 0)) or 0
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
