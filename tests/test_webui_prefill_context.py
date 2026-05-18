"""Regression tests for WebUI session prefill parity."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path


def test_prefill_script_output_becomes_safe_user_message(tmp_path):
    from api.streaming import _load_webui_prefill_context

    script = tmp_path / "recall.py"
    script.write_text("print('JOPLIN SESSION RECALL\\nCurrent Context: loaded')\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    result = _load_webui_prefill_context(
        {"prefill_messages_script": str(script)},
        python_exe=sys.executable,
        env={"PATH": os.environ.get("PATH", "")},
    )

    assert result["status"] == "loaded"
    assert result["source"] == "script"
    assert result["label"] == "recall.py"
    assert result["message_count"] == 1
    assert result["messages"] == [
        {
            "role": "user",
            "content": "JOPLIN SESSION RECALL\nCurrent Context: loaded",
        }
    ]


def test_prefill_json_file_is_loaded_without_script_execution(tmp_path):
    from api.streaming import _load_webui_prefill_context

    prefill = tmp_path / "prefill.json"
    prefill.write_text(json.dumps([{"role": "user", "content": "Pinned context"}]), encoding="utf-8")

    result = _load_webui_prefill_context({"prefill_messages_file": str(prefill)}, python_exe=sys.executable)

    assert result["status"] == "loaded"
    assert result["source"] == "file"
    assert result["label"] == "prefill.json"
    assert result["messages"] == [{"role": "user", "content": "Pinned context"}]


def test_prefill_context_redacts_secret_shaped_errors(tmp_path):
    from api.streaming import _load_webui_prefill_context

    missing = tmp_path / "missing.py"
    result = _load_webui_prefill_context(
        {"prefill_messages_script": str(missing)},
        python_exe=sys.executable,
    )

    assert result["status"] == "error"
    assert result["messages"] == []
    assert "missing.py" in result["label"]
    assert "token" not in result.get("error", "").lower()
