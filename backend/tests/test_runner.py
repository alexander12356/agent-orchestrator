import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from app.runner import ProcessRunner, build_command, cli_environment, extract_usage, rate_limited_line


def test_codex_runs_outside_the_task_directory():
    command = build_command({"cli": "codex", "model": "gpt-6.2-sol", "reasoning_effort": "medium"}, "review the doc", Path("/tmp/7"), 2)
    assert command[1:5] == ["exec", "--skip-git-repo-check", "-s", "workspace-write"]
    assert command[command.index("--model") + 1] == "gpt-6.2-sol"
    assert command[command.index("-c") + 1] == 'model_reasoning_effort="medium"'
    assert command[-1] == "review the doc"
    assert Path(command[0]).name.lower().removesuffix(".exe") == "codex"


def test_kimi_uses_the_paperclip_model_and_effort(monkeypatch):
    monkeypatch.setattr('app.runner.kimi_launcher', lambda: [])
    command = build_command({"cli": "kimi", "model": "kimi-code/k3.1-pro", "reasoning_effort": "low"}, "review the doc", Path("/tmp/7"), 2)
    assert command[1:3] == ["--output-format", "stream-json"]
    assert command[command.index("-m") + 1] == "kimi-code/k3.1-pro"
    assert command[-2:] == ["-p", "review the doc"]
    assert Path(command[0]).name.lower().removesuffix(".exe") == "kimi"
    assert cli_environment({"cli": "kimi", "model": "kimi-code/k3.1-pro", "reasoning_effort": "low"})["KIMI_MODEL_THINKING_EFFORT"] == "low"
    assert cli_environment({"cli": "kimi", "reasoning_effort": "medium"})["KIMI_MODEL_THINKING_EFFORT"] == "high"


def test_custom_kimi_command_is_resolved(monkeypatch):
    monkeypatch.setattr('app.runner.kimi_launcher', lambda: [])
    command = build_command({"cli": "custom", "command": "kimi -p {prompt}"}, "review the doc", Path("/tmp/7"), 2)
    assert command[1:3] == ["-p", "review the doc"]
    assert Path(command[0]).name.lower().removesuffix(".exe") == "kimi"


def test_kimi_uses_native_linux_node_when_available(monkeypatch, tmp_path):
    node = tmp_path / 'node'
    entry = tmp_path / 'main.mjs'
    node.write_text('node', encoding='utf-8')
    entry.write_text('entry', encoding='utf-8')
    monkeypatch.setattr('app.runner.KIMI_NODE', node)
    monkeypatch.setattr('app.runner.KIMI_ENTRY', entry)
    monkeypatch.setattr('app.runner.KIMI_HOME', tmp_path)
    monkeypatch.setattr('app.runner.os.name', 'posix')
    command = build_command({'cli': 'kimi', 'model': 'kimi-code/k3.1-pro'}, 'review', Path('/tmp/7'), 4)
    assert command[:2] == [str(node), str(entry)]
    assert command[-2:] == ['-p', 'review']
    assert cli_environment({'cli': 'kimi'})['KIMI_CODE_HOME'] == str(tmp_path)


def test_zcode_runs_a_headless_yolo_prompt(monkeypatch):
    monkeypatch.setattr('app.runner.zcode_program', lambda: 'zcode')
    command = build_command({'cli': 'zcode', 'model': 'ignored/model'}, 'review the doc', Path('/tmp/7'), 2)
    assert command == ['zcode', '--model', 'ignored/model', '-p', 'review the doc', '--mode', 'yolo', '--no-color']
    assert cli_environment({'cli': 'zcode'})['NO_COLOR'] == '1'


def test_zcode_without_model_uses_the_saved_default(monkeypatch):
    monkeypatch.setattr('app.runner.zcode_program', lambda: 'zcode')
    command = build_command({'cli': 'zcode', 'model': ''}, 'review the doc', Path('/tmp/7'), 2)
    assert '--model' not in command


def test_zcode_cli_is_accepted_by_the_agent_schema():
    from app.schemas import ModelConfig

    assert ModelConfig(cli='zcode').cli == 'zcode'


def test_custom_command_placeholders():
    command = build_command({'cli': 'custom', 'command': 'python agent.py --stage {stage} --prompt {prompt}'}, 'do work', Path('/tmp/7'), 2)
    assert command[-2:] == ['--prompt', 'do work']
    assert command[command.index('--stage') + 1] == '2'


def test_agent_command_keeps_the_repository_out_of_the_process():
    command = build_command(
        {'cli': 'custom', 'command': 'agent --print --trust --output-format json --model {model} {prompt}'},
        'do work',
        Path('/tmp/7'),
        1,
        Path('/mirrors/zelya'),
    )
    assert '--add-dir' not in command
    assert '/mirrors/zelya' not in command
    assert command[command.index('--output-format') + 1] == 'stream-json'
    assert command[-1] == 'do work'


def test_explicit_repo_placeholder_is_not_duplicated():
    command = build_command(
        {'cli': 'custom', 'command': 'agent --print --add-dir {repo} {prompt}'},
        'do work',
        Path('/tmp/7'),
        1,
        Path('/mirrors/zelya'),
    )
    assert command.count('--add-dir') == 1
    assert '/mirrors/zelya' in command


def test_live_log_publishes_agent_updates_before_the_cli_exits(monkeypatch, tmp_path):
    script = tmp_path / "cli.py"
    script.write_text(
        "import re, sys, time\n"
        "from pathlib import Path\n"
        "progress = Path(re.search(r'`([^\x60]+/updates.txt)`', sys.argv[1]).group(1))\n"
        "print('{\"type\":\"thinking\",\"text\":\"internal text\"}', flush=True)\n"
        "with progress.open('a', encoding='utf-8') as file:\n"
        "    file.write('Проверяю контракт.\\n')\n"
        "for _ in range(50):\n"
        "    if Path('published').exists():\n"
        "        break\n"
        "    time.sleep(0.1)\n"
        "else:\n"
        "    sys.exit(1)\n"
        "with progress.open('a', encoding='utf-8') as file:\n"
        "    file.write('Контракт соблюдён.')\n",
        encoding="utf-8",
    )
    launched_prompts = []

    def command(config, prompt, *args):
        launched_prompts.append(prompt)
        return [sys.executable, str(script), prompt]

    monkeypatch.setattr("app.runner.build_command", command)
    logged = []
    sent_prompts = []

    async def log(text: str) -> None:
        logged.append(text)
        (tmp_path / "published").touch()

    runner = ProcessRunner(log_callback=log, on_prompt_sent=sent_prompts.append)
    result = asyncio.run(runner.run({"cli": "custom"}, "prompt", tmp_path, {}, 1))
    assert result.exit_code == 0
    assert sent_prompts == launched_prompts
    assert logged == ["Проверяю контракт.\n", "Контракт соблюдён.\n"]
    assert "internal text" in result.output
    assert not list(tmp_path.glob(".orchestrator-progress-*"))


def test_cursor_tool_events_are_published_once_before_cli_exit(monkeypatch, tmp_path):
    public = "Прогресс: Нужен доступ к исходникам."
    assistant = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": public}]},
    }
    calls = [
        {"readToolCall": {"args": {"path": "src/input.txt"}}},
        {"writeToolCall": {"args": {"path": "out.md", "fileText": "Прогресс: Содержимое файла"}}},
        {"editToolCall": {"args": {"path": "out.md", "streamContent": "Новый текст"}}},
        {"todoWriteToolCall": {"args": {"todos": ["plan"]}}},
    ]
    events = [
        *(
            {"type": "tool_call", "subtype": subtype, "tool_call": call}
            for call in calls for subtype in ("started", "completed")
        ),
        {"type": "thinking", "text": "Прогресс: Скрытый текст"},
        {"type": "tool_call", "tool_call": {"result": "Прогресс: Содержимое файла"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Итог."}]}},
        {**assistant, "timestamp_ms": 1},
        {**assistant, "timestamp_ms": 1, "model_call_id": "turn"},
        {"type": "result", "result": public, "usage": {"inputTokens": 10, "outputTokens": 2}},
    ]
    script = tmp_path / "cursor.py"
    script.write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "events = json.loads(sys.argv[1])\n"
        "for event in events[:-1]:\n"
        "    print(json.dumps(event), flush=True)\n"
        "for _ in range(50):\n"
        "    if Path('published').exists():\n"
        "        break\n"
        "    time.sleep(0.1)\n"
        "else:\n"
        "    sys.exit(1)\n"
        "print(json.dumps(events[-1]))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.runner.build_command", lambda *args: [sys.executable, str(script), json.dumps(events)])
    prompts, logged = [], []

    async def log(text: str) -> None:
        logged.append(text)
        (tmp_path / "published").touch()

    runner = ProcessRunner(log_callback=log, on_prompt_sent=prompts.append)
    result = asyncio.run(runner.run({"cli": "custom", "command": "agent --print {prompt}"}, "prompt", tmp_path, {}, 1))

    assert result.exit_code == 0
    assert logged == [
        "Читаю: src/input.txt\n", "Записываю: out.md\n", "Изменяю: out.md\n",
        "Нужен доступ к исходникам.\n",
    ]
    assert (result.input_tokens, result.output_tokens) == (10, 2)
    assert "Прогресс:" in prompts[0]
    assert "Используй TodoList по необходимости" in prompts[0]
    assert "Перед первым инструментом" not in prompts[0]
    assert "updates.txt" not in prompts[0]


@pytest.mark.parametrize("cli", ["kimi", "zcode"])
def test_neo_fallback_uses_no_progress_file(monkeypatch, tmp_path, cli):
    calls = [
        {"type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for name, args in [
            ("Read", {"path": "src/file.cs"}),
            ("Edit", {"path": "doc.md", "content": "Содержимое правки"}),
            ("TodoList", {"todos": []}),
        ]
    ]
    events = [
        {"role": "assistant", "tool_calls": calls},
        {"role": "tool", "content": "Прогресс: Содержимое файла"},
        {"role": "assistant", "content": "Прогресс: Решение согласовано."},
    ] if cli == "kimi" else []
    script = "\n".join(f"print({json.dumps(event)!r})" for event in events)
    monkeypatch.setattr("app.runner.build_command", lambda *args: [sys.executable, "-c", script])
    logged, prompts = [], []

    async def log(text):
        logged.append(text)

    runner = ProcessRunner(log_callback=log, on_prompt_sent=prompts.append, tool_event_logs=True)
    result = asyncio.run(runner.run({"cli": cli}, "prompt", tmp_path, {}, 1))
    assert result.exit_code == 0
    assert "updates.txt" not in prompts[0]
    assert not list(tmp_path.glob(".orchestrator-progress-*"))
    assert logged == ([
        "Читаю: src/file.cs\n", "Изменяю: doc.md\n", "Решение согласовано.\n",
    ] if cli == "kimi" else [])


def test_extract_cursor_usage_names():
    usage = extract_usage('{"usage":{"inputTokens":243609,"outputTokens":26567,"cacheReadTokens":929536}}')
    assert usage["input_tokens"] == 243609
    assert usage["output_tokens"] == 26567
    assert usage["cache_tokens"] == 929536
    assert usage["estimated"] is False


def test_resumed_kimi_usage_only_counts_the_new_turn(tmp_path):
    session_id = "session_b41feaf1-1d74-42b1-a595-cd324f908759"
    session = tmp_path / "sessions" / "workspace" / session_id
    wire = session / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True)
    (session / "state.json").write_text(json.dumps({"cwd": str(tmp_path)}), encoding="utf-8")
    wire.write_text("\n".join(json.dumps({
        "type": "usage.record", "time": timestamp, "usage": {"inputTokens": tokens, "outputTokens": 1},
    }) for timestamp, tokens in [(100, 80), (200, 5)]), encoding="utf-8")
    output = json.dumps({"type": "session.resume_hint", "session_id": session_id})
    assert extract_usage(output, tmp_path, tmp_path, kimi_since=200) == {
        "input_tokens": 5, "output_tokens": 1, "cache_tokens": 0, "estimated": False,
    }


def test_extract_structured_usage():
    usage = extract_usage('log\n{"usage":{"input_tokens":12,"output_tokens":8,"cache_read_input_tokens":3}}')
    assert usage == {'input_tokens': 12, 'output_tokens': 8, 'cache_tokens': 3, 'estimated': False}


def test_extract_usage_skips_non_object_output():
    output = '{"result":{"usage":{"input_tokens":12,"output_tokens":8}}}\n42\nnull\n[]'
    assert extract_usage(output) == {'input_tokens': 12, 'output_tokens': 8, 'cache_tokens': 0, 'estimated': False}
    assert extract_usage('42') == {}


def test_long_stream_lines_are_read_instead_of_failing(monkeypatch, tmp_path):
    script = tmp_path / 'big_line.py'
    script.write_text(
        "import sys\n"
        "sys.stdout.write('x' * 200_000 + '\\n')\n"
        "sys.stdout.write('tail')\n",
        encoding='utf-8',
    )
    monkeypatch.setattr('app.runner.build_command', lambda *args, **kwargs: [sys.executable, str(script)])
    logged: list[str] = []

    async def log(text: str) -> None:
        logged.append(text)

    result = asyncio.run(ProcessRunner(log_callback=log).run({'cli': 'custom'}, 'prompt', tmp_path, {}, 1))

    assert result.exit_code == 0
    assert result.output.startswith('x' * 200_000 + '\n')
    assert result.output.endswith('tail')
    assert logged == []


def test_codex_cached_input_counted_once():
    event = '{"type":"turn.completed","usage":{"input_tokens":200,"cached_input_tokens":150,"output_tokens":5}}'
    assert extract_usage(event) == dict(input_tokens=50, cache_tokens=150, output_tokens=5, estimated=False)
    assert extract_usage("some output") == {}


def test_limit_is_reported_only_by_error_lines():
    assert rate_limited_line("ERROR: You've hit your usage limit.")
    assert rate_limited_line("ActionRequiredError: Weekly usage limit reached. It resets in 1 day.")
    assert not rate_limited_line("ActionRequiredError: Please sign in.")
    assert rate_limited_line('[12:00:01] API Error: 429 Too Many Requests')
    assert rate_limited_line('{"type":"result","subtype":"success","is_error":true,"result":"usage limit reached"}')
    assert rate_limited_line('{"type":"error","message":"rate limit exceeded"}')
    assert not rate_limited_line('## Rate limits')
    assert not rate_limited_line('The API returns 429 when the rate limit is exceeded.')
    assert not rate_limited_line('{"type":"assistant","message":{"content":[{"type":"text","text":"HTTP 429 means rate limit"}]}}')
    assert not rate_limited_line('{"type":"result","subtype":"success","is_error":false,"result":"Document mentions a rate limit"}')


def run_script(monkeypatch, tmp_path, body):
    script = tmp_path / 'cli.py'
    script.write_text('import sys, time\n' + body, encoding='utf-8')
    monkeypatch.setattr('app.runner.build_command', lambda *args, **kwargs: [sys.executable, str(script)])
    return asyncio.run(ProcessRunner().run({'cli': 'custom'}, 'prompt', tmp_path, {}, 1))


def test_limit_words_in_agent_output_do_not_count(monkeypatch, tmp_path):
    result = run_script(monkeypatch, tmp_path, "print('HTTP 429 means the rate limit is exceeded.')\nprint('done')\n")
    assert (result.exit_code, result.rate_limited) == (0, False)


def test_limit_reported_by_a_failed_run_counts_even_on_a_plain_line(monkeypatch, tmp_path):
    result = run_script(monkeypatch, tmp_path, "print(\"You've hit your usage limit. Try again later.\")\nsys.exit(1)\n")
    assert (result.exit_code, result.rate_limited) == (1, True)


def test_limit_on_an_error_line_stops_the_cli_at_once(monkeypatch, tmp_path):
    result = run_script(monkeypatch, tmp_path, "print('ERROR: rate limit exceeded', flush=True)\ntime.sleep(30)\n")
    assert result.rate_limited is True
    assert result.timed_out is False


@pytest.mark.skipif(os.name != "posix", reason="The Linux runner manages a CLI process group")
def test_cursor_limit_stops_launcher_children_holding_output(monkeypatch, tmp_path):
    script = tmp_path / "cli.py"
    script.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print('ActionRequiredError: Weekly usage limit reached.', flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.runner.build_command", lambda *args, **kwargs: [sys.executable, str(script)])

    async def run():
        return await asyncio.wait_for(
            ProcessRunner().run({"cli": "custom"}, "prompt", tmp_path, {}, 1),
            timeout=3,
        )

    result = asyncio.run(run())
    assert result.rate_limited is True
    assert result.timed_out is False
