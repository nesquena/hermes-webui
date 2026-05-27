"""Regression tests for WebUI session prefill parity."""
from __future__ import annotations

import json
import sys
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


def test_webui_prefill_script_wraps_plain_text_for_any_notes_source(tmp_path):
    from api.streaming import _load_webui_prefill_context

    script = tmp_path / "obsidian_recall.py"
    script.write_text("print('Obsidian project note context')\n", encoding="utf-8")

    result = _load_webui_prefill_context({"webui_prefill_messages_script": [sys.executable, str(script)]})

    assert result["status"] == "loaded"
    assert result["source"] == "script"
    assert result["messages"] == [{"role": "system", "content": "Obsidian project note context"}]


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
    assert result["messages"] == [{"role": "system", "content": "dynamic"}]


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
