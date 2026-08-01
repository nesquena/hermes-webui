import inspect

import api.streaming as streaming


def test_streaming_completion_records_only_successful_server_result():
    source = inspect.getsource(streaming)
    start = source.index("def on_tool_complete(tool_call_id, name, args, function_result):")
    block = source[start:source.index("\n            _AIAgent =", start)]
    assert "record_server_skill_result(function_result)" in block
    assert "record_server_skill_names" not in block
    assert "args" in block
