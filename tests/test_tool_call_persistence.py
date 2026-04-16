"""Tests for backend tool-call summary extraction used by WebUI session persistence."""
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

from api.streaming import _extract_tool_calls_from_messages


def test_extract_tool_calls_from_openai_message_linkage():
    messages = [
        {"role": "user", "content": "ls"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-1",
                "function": {"name": "terminal", "arguments": '{"command":"ls"}'},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"output":"file.txt","exit_code":0}',
        },
    ]
    result = _extract_tool_calls_from_messages(messages)
    assert len(result) == 1
    assert result[0]["name"] == "terminal"
    assert result[0]["assistant_msg_idx"] == 1
    assert result[0]["snippet"] == "file.txt"


def test_extract_tool_calls_falls_back_to_live_progress_when_ids_missing():
    messages = [
        {"role": "user", "content": "write spec"},
        {"role": "assistant", "content": "Starting."},
        {"role": "tool", "content": '{"bytes_written":4955}'},
        {"role": "assistant", "content": ""},
    ]
    live_tool_calls = [{"name": "write_file", "args": {"path": "/tmp/SPEC.md"}}]
    result = _extract_tool_calls_from_messages(messages, live_tool_calls=live_tool_calls)
    assert len(result) == 1
    assert result[0]["name"] == "write_file"
    assert result[0]["assistant_msg_idx"] == 1
    assert "bytes_written" in result[0]["snippet"]
    assert result[0]["args"]["path"] == "/tmp/SPEC.md"


def test_extract_tool_calls_preserves_mixed_linked_and_fallback_results():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "terminal", "arguments": '{"command":"pwd"}'}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"output":"/tmp"}'},
        {"role": "assistant", "content": "Next"},
        {"role": "tool", "content": '{"result":"saved"}'},
    ]
    live_tool_calls = [
        {"name": "terminal", "args": {"command": "pwd"}},
        {"name": "write_file", "args": {"path": "/tmp/out.txt"}},
    ]
    result = _extract_tool_calls_from_messages(messages, live_tool_calls=live_tool_calls)
    assert len(result) == 2
    assert result[0]["name"] == "terminal"
    assert result[1]["name"] == "write_file"
    assert result[1]["assistant_msg_idx"] == 2
    assert result[1]["snippet"] == "saved"
