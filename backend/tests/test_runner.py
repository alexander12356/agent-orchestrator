import asyncio
import sys
from pathlib import Path

from app.runner import ProcessRunner, build_command, cli_environment, estimate_usage, extract_usage, present_log_line


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


def test_live_log_keeps_agent_text_and_tool_names():
    started = present_log_line('{"type":"tool_call","subtype":"started","tool_call":{"readToolCall":{"args":{"path":"AGENTS.md"}}}}')
    assert started.text == "[read]\n"
    delta = present_log_line('{"type":"assistant","message":{"content":[{"type":"text","text":"Пишу документ"}]}}')
    assert delta.text == "Пишу документ"
    hidden = present_log_line('{"type":"result","subtype":"success"}')
    assert hidden.text == ""
    thinking = present_log_line('{"type":"thinking","text":"plan"}')
    assert thinking.text == "Агент думает…\n"
    assert present_log_line('{"type":"thinking","text":"more"}', thinking.thinking).text == ""


def test_extract_cursor_usage_names():
    usage = extract_usage('{"usage":{"inputTokens":243609,"outputTokens":26567,"cacheReadTokens":929536}}')
    assert usage["input_tokens"] == 243609
    assert usage["output_tokens"] == 26567
    assert usage["cache_tokens"] == 929536
    assert usage["estimated"] is False


def test_extract_structured_usage():
    usage = extract_usage('log\n{"usage":{"input_tokens":12,"output_tokens":8,"cache_read_input_tokens":3}}')
    assert usage == {'input_tokens': 12, 'output_tokens': 8, 'cache_tokens': 3, 'estimated': False}


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
    assert logged[0] == 'x' * 200_000 + '\n'
    assert logged[-1] == 'tail\n'


def test_usage_fallback_is_marked_estimated():
    assert estimate_usage('some prompt', 'some output')['estimated'] is True
