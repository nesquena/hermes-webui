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


def _draft_restore_suppression_block() -> str:
    start = SESSIONS_JS.index("const _composerDraftRestoreSuppressedUntilBySid")
    end = SESSIONS_JS.index("function _profileMatchesActiveProfile", start)
    return SESSIONS_JS[start:end]


def _run_case(*, initial_text: str, draft: dict | None, opts: dict | None, current_sid, force_reload: bool, suppress_restore: bool | dict = False, remembered_server_draft: dict | None = None) -> dict:
    if NODE is None:
        pytest.skip("node not on PATH")
    restore_fn = _function_block(SESSIONS_JS, "function _restoreComposerDraft(draft, targetSid, opts={}) {")
    draft_block = _draft_restore_block()
    suppression_block = _draft_restore_suppression_block()
    if isinstance(suppress_restore, dict):
        suppress_call = (
            "_suppressComposerDraftRestoreAfterSubmit("
            f"sid, {json.dumps(suppress_restore.get('text', ''))}, {json.dumps(suppress_restore.get('files', []))}"
            ");"
        )
    elif suppress_restore:
        suppress_call = "_suppressComposerDraftRestoreAfterSubmit(sid);"
    else:
        suppress_call = ""
    # At SEND time the session's composer_draft holds what was last persisted to
    # the server (the prefix/old draft), which the dual-signature suppression
    # reads. That is DISTINCT from the draft a later stale/cross-tab poll echoes
    # back for restore. Seed the send-time remembered draft separately, then swap
    # in the restore draft before _restoreComposerDraft runs.
    send_time_draft = remembered_server_draft if remembered_server_draft is not None else draft
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
let _loadingSessionId = null;
const sid = 'boot-session';
const S = {{
  session: {{
    session_id: sid,
    composer_draft: {json.dumps(send_time_draft)},
  }},
}};
const currentSid = {json.dumps(current_sid)};
const forceReload = {json.dumps(force_reload)};
const opts = {json.dumps(opts or {})};
function _composerDraftHasPayload(text, files) {{
  return !!(String(text || '') || (Array.isArray(files) && files.filter(Boolean).length));
}}
function _rememberComposerDraftPayloadState(sid, text, files) {{
  state.rememberedDraft = {{sid, text, files}};
  if (S.session && S.session.session_id === sid) {{
    S.session.composer_draft = {{text, files}};
  }}
}}
{suppression_block}
{suppress_call}
// After send-time suppression is captured, the stale/cross-tab poll delivers a
// (possibly different) draft that restore will evaluate.
S.session.composer_draft = {json.dumps(draft)};
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


def test_same_session_submitted_clear_blocks_stale_server_draft_restore():
    """After send clears the composer, a stale same-session refresh must not refill it.

    Mobile browsers can deliver refresh/input timing such that the textarea is
    empty locally while /api/session still returns the previous non-empty
    composer_draft. The submitted-clear suppression should keep that old draft
    out of the input.
    """
    data = _run_case(
        initial_text="",
        draft={"text": "old submitted suffix", "files": []},
        opts={"preserveActiveInput": True},
        current_sid="boot-session",
        force_reload=True,
        remembered_server_draft={"text": "old submitted suffix", "files": []},
        suppress_restore={"text": "old submitted suffix", "files": []},
    )
    assert data["value"] == ""
    assert data["autoResizeCount"] == 0
    assert data["updateSendBtnCount"] == 0


def test_same_session_submitted_clear_blocks_stale_prefix_server_draft_restore():
    """#5471 (Opus Finding 1): the server's last persisted draft is often a PREFIX
    of the submitted text (Enter-to-send within 400ms of a keystroke cancels the
    pending debounced save), so an exact-match on the submitted text would miss it.
    The dual signature also captures the remembered SERVER draft, so the stale
    prefix is suppressed.
    """
    data = _run_case(
        initial_text="",
        draft={"text": "hello worl", "files": []},          # stale prefix echoed back
        opts={"preserveActiveInput": True},
        current_sid="boot-session",
        force_reload=True,
        remembered_server_draft={"text": "hello worl", "files": []},  # server had the prefix
        suppress_restore={"text": "hello world!", "files": []},        # user sent the full text
    )
    assert data["value"] == "", "stale prefix of the just-sent text must be suppressed"
    assert data["autoResizeCount"] == 0
    assert data["updateSendBtnCount"] == 0


def test_same_session_submitted_clear_allows_different_cross_tab_draft_restore():
    """A different same-session draft saved by another tab must not be hidden by send-time suppression."""
    data = _run_case(
        initial_text="",
        draft={"text": "new cross-tab draft", "files": []},
        opts={"preserveActiveInput": True},
        current_sid="boot-session",
        force_reload=True,
        remembered_server_draft={"text": "old submitted suffix", "files": []},
        suppress_restore={"text": "old submitted suffix", "files": []},
    )
    assert data["value"] == "new cross-tab draft"
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
