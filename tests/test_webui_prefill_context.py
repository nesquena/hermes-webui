"""Regression tests for WebUI session prefill parity."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path


def test_prefill_json_file_keeps_valid_roles_and_drops_invalid_items(tmp_path):
    from api.streaming import _load_webui_prefill_context

    prefill = tmp_path / "prefill.json"
    prefill.write_text(
        json.dumps(
            [
                {"role": "user", "content": "Pinned context"},
                {"role": "tool", "content": "drop invalid role"},
                {"role": "assistant", "content": "Useful assistant context"},
                {"role": "system", "content": "   "},
                "not a message",
            ]
        ),
        encoding="utf-8",
    )

    result = _load_webui_prefill_context({"prefill_messages_file": str(prefill)})

    assert result["status"] == "loaded"
    assert result["source"] == "file"
    assert result["label"] == "prefill.json"
    assert result["messages"] == [
        {"role": "user", "content": "Pinned context"},
        {"role": "assistant", "content": "Useful assistant context"},
    ]


def test_prefill_script_config_is_not_used_without_webui_opt_in(tmp_path):
    from api.streaming import _load_webui_prefill_context

    script = tmp_path / "recall.py"
    script.write_text("raise SystemExit('should not run')\n", encoding="utf-8")

    result = _load_webui_prefill_context({"prefill_messages_script": str(script)})

    assert result == {
        "status": "not_configured",
        "source": "none",
        "label": "",
        "messages": [],
        "message_count": 0,
    }


def test_webui_prefill_script_loads_json_messages(tmp_path):
    from api.streaming import _load_webui_prefill_context

    script = tmp_path / "recall.py"
    script.write_text(
        "import json\n"
        "content = 'Joplin has durable context; use notes/search tools for details.'\n"
        "print(json.dumps([{'role': 'system', 'content': content}, {'role': 'tool', 'content': 'drop me'}]))\n",
        encoding="utf-8",
    )

    result = _load_webui_prefill_context({"webui_prefill_messages_script": [sys.executable, str(script)]})

    assert result["status"] == "loaded"
    assert result["source"] == "script"
    assert result["label"] == Path(sys.executable).name
    assert result["messages"] == [{"role": "system", "content": "Joplin has durable context; use notes/search tools for details."}]


def test_webui_prefill_script_wraps_plain_text_as_user_context(tmp_path):
    from api.streaming import _load_webui_prefill_context

    script = tmp_path / "obsidian_recall.py"
    script.write_text("print('Obsidian project note context')\n", encoding="utf-8")

    result = _load_webui_prefill_context({"webui_prefill_messages_script": [sys.executable, str(script)]})

    assert result["status"] == "loaded"
    assert result["source"] == "script"
    assert result["messages"] == [{"role": "user", "content": "Obsidian project note context"}]


def test_webui_prefill_script_errors_are_redacted(tmp_path):
    from api.streaming import _load_webui_prefill_context

    script = tmp_path / "bad_recall.py"
    script.write_text("import sys; print('token=redaction-test-placeholder', file=sys.stderr); raise SystemExit(2)\n", encoding="utf-8")

    result = _load_webui_prefill_context({"webui_prefill_messages_script": [sys.executable, str(script)]})

    assert result["status"] == "error"
    assert result["source"] == "script"
    assert "redaction-test-placeholder" not in result["error"]
    assert "[REDACTED]" in result["error"]


def test_webui_prefill_script_takes_precedence_over_static_file(tmp_path):
    from api.streaming import _load_webui_prefill_context

    prefill = tmp_path / "prefill.json"
    prefill.write_text(json.dumps([{"role": "system", "content": "static"}]), encoding="utf-8")
    script = tmp_path / "recall.py"
    script.write_text("print('dynamic')\n", encoding="utf-8")

    result = _load_webui_prefill_context({
        "prefill_messages_file": str(prefill),
        "webui_prefill_messages_script": [sys.executable, str(script)],
    })

    assert result["source"] == "script"
    assert result["messages"] == [{"role": "user", "content": "dynamic"}]


def test_webui_prefill_script_error_falls_back_to_static_router_file(tmp_path):
    from api.streaming import _load_webui_prefill_context

    prefill = tmp_path / "prefill.json"
    prefill.write_text(json.dumps([{"role": "system", "content": "Joplin router fallback"}]), encoding="utf-8")
    script = tmp_path / "broken_recall.py"
    script.write_text("import sys; print('api_key=redaction-test-placeholder', file=sys.stderr); raise SystemExit(2)\n", encoding="utf-8")

    result = _load_webui_prefill_context({
        "prefill_messages_file": str(prefill),
        "webui_prefill_messages_script": [sys.executable, str(script)],
    })

    assert result["status"] == "loaded"
    assert result["source"] == "file_fallback"
    assert result["messages"] == [{"role": "system", "content": "Joplin router fallback"}]
    assert "redaction-test-placeholder" not in result.get("script_error", "")


def test_webui_prefill_script_timeout_returns_redacted_error(tmp_path):
    from api.streaming import _load_webui_prefill_context

    script = tmp_path / "slow_recall.py"
    script.write_text("import time\ntime.sleep(1)\nprint('too late')\n", encoding="utf-8")

    result = _load_webui_prefill_context({
        "webui_prefill_messages_script": [sys.executable, str(script)],
        "webui_prefill_messages_script_timeout": 0.1,
    })

    assert result["status"] == "error"
    assert result["source"] == "script"
    assert result["messages"] == []
    assert result["message_count"] == 0
    assert result["error"] == "prefill script timed out"


def test_webui_prefill_script_rejects_oversized_stdout(tmp_path):
    from api.streaming import _load_webui_prefill_context

    script = tmp_path / "large_recall.py"
    script.write_text("print('x' * 262145)\n", encoding="utf-8")

    result = _load_webui_prefill_context({"webui_prefill_messages_script": [sys.executable, str(script)]})

    assert result["status"] == "error"
    assert result["source"] == "script"
    assert result["messages"] == []
    assert result["message_count"] == 0
    assert "output exceeded" in result["error"]


def test_webui_prefill_script_over_budget_uses_static_file_fallback(tmp_path):
    from api.streaming import _load_webui_prefill_context, _public_prefill_context_status

    prefill = tmp_path / "router.json"
    prefill.write_text(json.dumps([{"role": "user", "content": "Compact router context"}]), encoding="utf-8")
    script = tmp_path / "large_recall.py"
    script.write_text("print('x' * 80)\n", encoding="utf-8")

    result = _load_webui_prefill_context(
        {
            "webui_prefill_messages_script": [sys.executable, str(script)],
            "prefill_messages_file": str(prefill),
            "webui_prefill_context_max_chars": 40,
        }
    )

    assert result["status"] == "loaded"
    assert result["source"] == "file_budget_fallback"
    assert result["messages"] == [{"role": "user", "content": "Compact router context"}]
    assert result["compacted"] is True
    assert result["original_source"] == "script"
    assert result["original_char_count"] == 80
    public = _public_prefill_context_status(result)
    assert public["compacted"] is True
    assert public["original_char_count"] == 80
    assert "messages" not in public


def test_webui_prefill_file_over_budget_compacts_without_leaking_body(tmp_path):
    from api.streaming import _load_webui_prefill_context, _public_prefill_context_status

    prefill = tmp_path / "huge.json"
    prefill.write_text(json.dumps([{"role": "user", "content": "secret project note " * 20}]), encoding="utf-8")

    result = _load_webui_prefill_context(
        {
            "prefill_messages_file": str(prefill),
            "webui_prefill_context_max_chars": 50,
        }
    )

    assert result["status"] == "loaded"
    assert result["source"] == "budget_compacted"
    assert result["message_count"] == 1
    assert result["compacted"] is True
    assert result["original_source"] == "file"
    assert result["original_char_count"] > 50
    compact_message = result["messages"][0]["content"]
    assert "exceeded the WebUI prefill context budget" in compact_message
    assert "secret project note" not in compact_message
    public = _public_prefill_context_status(result)
    assert public["source"] == "budget_compacted"
    assert public["compacted"] is True
    assert "messages" not in public


def test_webui_prefill_context_budget_can_be_disabled(tmp_path):
    from api.streaming import _load_webui_prefill_context

    prefill = tmp_path / "huge.json"
    prefill.write_text(json.dumps([{"role": "user", "content": "x" * 80}]), encoding="utf-8")

    result = _load_webui_prefill_context(
        {
            "prefill_messages_file": str(prefill),
            "webui_prefill_context_max_chars": 0,
        }
    )

    assert result["source"] == "file"
    assert result["messages"] == [{"role": "user", "content": "x" * 80}]
    assert "compacted" not in result


def test_public_prefill_status_strips_message_bodies():
    from api.streaming import _public_prefill_context_status

    public = _public_prefill_context_status(
        {
            "status": "loaded",
            "source": "file",
            "label": "prefill.json",
            "message_count": 1,
            "messages": [{"role": "user", "content": "private recall payload"}],
        }
    )

    assert public == {
        "status": "loaded",
        "source": "file",
        "label": "prefill.json",
        "message_count": 1,
    }
    assert "messages" not in public


def test_webui_session_context_adds_gateway_like_metadata(monkeypatch, tmp_path):
    # #3324: the gateway-like session/delivery context moved OUT of a prefill
    # `user` message (which broke strict chat templates with two consecutive
    # user turns) and INTO the ephemeral system prompt. _prefill_messages_with_webui_context
    # now returns only recall prefill; the metadata is asserted on
    # _webui_ephemeral_system_prompt instead. The invariants preserved here:
    # paused platforms excluded, connected platforms shown, home-channel name
    # shown, and the chat_id never leaks.
    from api.streaming import (
        _prefill_messages_with_webui_context,
        _webui_ephemeral_system_prompt,
    )

    gateway_state = tmp_path / "gateway_state.json"
    gateway_state.write_text(
        json.dumps(
            {
                "platforms": {
                    "telegram": {"state": "connected"},
                    "discord": {"state": "paused"},
                    "api_server": {"state": "connected"},
                }
            }
        ),
        encoding="utf-8",
    )

    class FakeHome:
        def __truediv__(self, name):
            return gateway_state if name == "gateway_state.json" else tmp_path / name

    fake_constants = types.SimpleNamespace(
        get_hermes_home=lambda: FakeHome(),
        display_hermes_home=lambda: "/tmp/hermes-test-home",
    )
    monkeypatch.setitem(sys.modules, "hermes_constants", fake_constants)

    config_data = {
        "platforms": {
            "telegram": {
                "enabled": True,
                "home_channel": {"name": "Home DM", "chat_id": "should-not-leak"},
            }
        }
    }

    # The prefill helper no longer appends a session-context user message.
    messages = _prefill_messages_with_webui_context(
        {"messages": [{"role": "user", "content": "recall"}]},
        config_data,
    )
    assert messages == [{"role": "user", "content": "recall"}]
    assert not any(
        isinstance(m, dict) and "## Current Session Context" in str(m.get("content", ""))
        for m in messages
    ), "session context must NOT be appended as a prefill user message anymore (#3324)"

    # The same gateway-like metadata is now carried in the ephemeral system prompt.
    prompt = _webui_ephemeral_system_prompt(
        None,
        surface_context={"source": "webui"},
        config_data=config_data,
    )
    assert "**Connected Platforms:**" in prompt
    assert "telegram: Connected" in prompt
    assert "discord: Connected" not in prompt  # paused platform excluded
    assert "Home DM" in prompt
    assert "should-not-leak" not in prompt  # chat_id must never leak


def test_prefill_status_redactor_handles_secret_shaped_text():
    from api.streaming import _redact_prefill_status_text

    redacted = _redact_prefill_status_text("api_key=redaction-test-placeholder leaked")

    assert "redaction-test-placeholder" not in redacted
    assert "[REDACTED]" in redacted
