from pathlib import Path

from app.runner import build_command, estimate_usage, extract_usage


def test_custom_command_placeholders():
    command = build_command({'cli': 'custom', 'command': 'python agent.py --stage {stage} --prompt {prompt}'}, 'do work', Path('/tmp/7'), 2)
    assert command[-2:] == ['--prompt', 'do work']
    assert command[command.index('--stage') + 1] == '2'


def test_extract_structured_usage():
    usage = extract_usage('log\n{"usage":{"input_tokens":12,"output_tokens":8,"cache_read_input_tokens":3}}')
    assert usage == {'input_tokens': 12, 'output_tokens': 8, 'cache_tokens': 3, 'estimated': False}


def test_usage_fallback_is_marked_estimated():
    assert estimate_usage('some prompt', 'some output')['estimated'] is True
