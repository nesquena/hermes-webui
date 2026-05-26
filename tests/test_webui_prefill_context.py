"""Regression tests for WebUI session prefill parity."""
from __future__ import annotations

import json
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


def test_prefill_script_config_is_ignored_in_webui(tmp_path):
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


def test_prefill_status_redactor_handles_secret_shaped_text():
    from api.streaming import _redact_prefill_status_text

    redacted = _redact_prefill_status_text("api_key=redaction-test-placeholder leaked")

    assert "redaction-test-placeholder" not in redacted
    assert "[REDACTED]" in redacted


def test_cached_agent_prefill_refresh_requires_explicit_kwargs():
    """Cached agents should not get an empty prefill list when kwargs omitted it."""
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    callback_refresh = src.index("# Refresh per-turn callbacks")
    session_db_refresh = src.index("if _session_db is not None:", callback_refresh)
    body = src[callback_refresh:session_db_refresh]

    assert "'prefill_messages' in _agent_kwargs" in body
    assert "hasattr(agent, 'prefill_messages')" in body
