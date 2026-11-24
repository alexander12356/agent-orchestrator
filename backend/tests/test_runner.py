from pathlib import Path

from app.runner import build_command, estimate_usage, extract_usage, present_log_line


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


def test_usage_fallback_is_marked_estimated():
    assert estimate_usage('some prompt', 'some output')['estimated'] is True
