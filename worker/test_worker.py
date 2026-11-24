import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location('unity_worker', Path(__file__).with_name('unity_worker.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_parse_structured_usage():
    result = module.parse_usage('prompt', '{"usage":{"input_tokens":20,"output_tokens":5}}')
    assert result['input_tokens'] == 20
    assert result['estimated'] is False


def test_parse_usage_estimate():
    assert module.parse_usage('prompt', 'plain output')['estimated'] is True
