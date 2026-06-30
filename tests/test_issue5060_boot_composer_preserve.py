"""Regression tests for #5060: preserve typed composer input during boot restore."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_block(source: str, marker: str) -> str:
    start = source.index(marker)
    signature_end = source.index(") {", start)
    brace = source.index("{", signature_end)
    depth = 1
    idx = brace + 1
    while depth:
        char = source[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        idx += 1
    return source[start:idx]


def _draft_restore_block() -> str:
    start_marker = "// Restore server-persisted composer draft (synced across clients + survives refresh)."
    end_marker = "// Clear the in-flight session marker now that this load has completed (#1060)."
    start = SESSIONS_JS.index(start_marker)
    end = SESSIONS_JS.index(end_marker, start)
    return SESSIONS_JS[start:end]


def _run_case(*, initial_text: str, draft: dict | None, opts: dict | None, current_sid, force_reload: bool) -> dict:
    if NODE is None:
        pytest.skip("node not on PATH")
    restore_fn = _function_block(SESSIONS_JS, "function _restoreComposerDraft(draft, targetSid, opts={}) {")
    draft_block = _draft_restore_block()
    script = f"""
const state = {{
  value: {json.dumps(initial_text)},
  autoResizeCount: 0,
  updateSendBtnCount: 0,
}};
function $() {{
  return state;
}}
function autoResize() {{
  state.autoResizeCount += 1;
}}
function updateSendBtn() {{
  state.updateSendBtnCount += 1;
}}
const S = {{
  session: {{
    composer_draft: {json.dumps(draft)},
  }},
}};
let _loadingSessionId = null;
const sid = 'boot-session';
const currentSid = {json.dumps(current_sid)};
const forceReload = {json.dumps(force_reload)};
const opts = {json.dumps(opts or {})};
{restore_fn}
{draft_block}
process.stdout.write(JSON.stringify({{
  value: state.value,
  autoResizeCount: state.autoResizeCount,
  updateSendBtnCount: state.updateSendBtnCount,
}}));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_boot_restore_preserves_typed_input_against_remote_draft():
    """Boot restore should keep local typed text when the saved draft arrives later."""
    data = _run_case(
        initial_text="typed locally",
        draft={"text": "server draft", "files": []},
        opts={"preserveActiveInput": True},
        current_sid=None,
        force_reload=False,
    )
    assert data["value"] == "typed locally"
    assert data["autoResizeCount"] == 0
    assert data["updateSendBtnCount"] == 0


def test_boot_restore_preserves_typed_input_when_saved_draft_is_empty():
    """Boot restore should keep local typed text even when the restored draft is empty."""
    data = _run_case(
        initial_text="typed locally",
        draft={"text": "", "files": []},
        opts={"preserveActiveInput": True},
        current_sid=None,
        force_reload=False,
    )
    assert data["value"] == "typed locally"
    assert data["autoResizeCount"] == 0
    assert data["updateSendBtnCount"] == 0


def test_boot_restore_still_populates_empty_composer_from_saved_draft():
    """A blank composer should still take the server draft during boot restore."""
    data = _run_case(
        initial_text="",
        draft={"text": "server draft", "files": []},
        opts={"preserveActiveInput": True},
        current_sid=None,
        force_reload=False,
    )
    assert data["value"] == "server draft"
    assert data["autoResizeCount"] == 1
    assert data["updateSendBtnCount"] == 1


def test_cross_session_restore_keeps_existing_draft_semantics():
    """Ordinary cross-session loads should still restore the target session draft."""
    data = _run_case(
        initial_text="typed locally",
        draft={"text": "server draft", "files": []},
        opts={},
        current_sid="other-session",
        force_reload=False,
    )
    assert data["value"] == "server draft"
    assert data["autoResizeCount"] == 1
    assert data["updateSendBtnCount"] == 1
