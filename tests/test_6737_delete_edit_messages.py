"""Static regression tests for #6737 — delete or edit messages.

The feature reuses the existing POST /api/session/truncate endpoint
(keep_count semantics) from the frontend:

- Edit affordance: every user message now renders the pencil button
  (previously only the latest user message). submitEdit() truncates the
  session at that message and re-sends the edited text.
- Delete affordance: every message renders a trash button; deleteMessage()
  truncates the session at that message (message + everything after is
  removed) after a danger confirm dialog.

These are source-structure tests (Pattern 1/2 from the WebUI contribution
skill): they pin the rendered button wiring, the deleteMessage() contract,
the i18n keys, and the backend truncate guards the feature depends on.
"""

import os
import re
from pathlib import Path
import sys

ROOT = Path(os.environ.get("ISSUE6737_REPO_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "tests"))

UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def _function(source, name, prefix="function"):
    """Extract a function body by brace matching (see tests/js_source_extract.py)."""
    marker = f"{prefix} {name}("
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    for pos in range(brace, len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                return source[start : pos + 1]
    raise AssertionError(f"could not extract {name}")


def _en_locale_block():
    start = I18N_JS.index("  en: {")
    # Next top-level locale block (or the closing "};" of LOCALES) ends en.
    for m in re.finditer(r"^  [a-z][a-z0-9_-]*: \{$", I18N_JS, re.MULTILINE):
        if m.start() > start:
            return I18N_JS[start : m.start()]
    end = I18N_JS.index("\n};", start)
    return I18N_JS[start:end]


# ── Edit affordance on every user message ────────────────────────────────


def test_edit_affordance_on_every_user_message():
    # The pencil must render on ALL user messages, not only the latest one.
    assert "const isEditableUser=isUser;" in UI_JS
    assert "rawIdx===lastUserRawIdx" not in UI_JS
    assert "lastUserRawIdx" not in UI_JS  # last-only lookup loop removed


def test_edit_submit_still_truncates_at_message_index():
    # Editing any previous message reuses the truncate-and-resend contract.
    fn = _function(UI_JS, "submitEdit")
    assert "absoluteKeepCount = _oldestIdx + msgIdx" in fn
    assert "keep_count: absoluteKeepCount" in fn
    assert "/api/session/truncate" in fn


# ── Delete affordance ────────────────────────────────────────────────────


def test_delete_button_wired_into_message_foot():
    assert "onclick=\"deleteMessage(this)\"" in UI_JS
    assert "li('trash-2',13)" in UI_JS
    assert "msg-delete-btn" in UI_JS
    assert "t('delete_message')" in UI_JS
    foot_idx = UI_JS.index("const footHtml")
    foot_snippet = UI_JS[foot_idx : foot_idx + 400]
    assert "${deleteBtn}" in foot_snippet


def test_delete_button_hidden_on_read_only_sessions():
    # Matches the fork affordance gate: read-only (non-branchable) sessions
    # must not offer destructive actions.
    del_idx = UI_JS.index("const deleteBtn")
    snippet = UI_JS[del_idx : del_idx + 300]
    assert "(readOnlySession&&!branchableReadOnlySession) ? ''" in snippet


def test_delete_message_truncates_session():
    fn = _function(UI_JS, "deleteMessage")
    # Contract: resolve the row, keep everything BEFORE this message.
    assert "btn.closest('[data-msg-idx]')" in fn
    assert "absoluteKeepCount = _oldestIdx + msgIdx" in fn
    # Danger confirm before the destructive call.
    assert "showConfirmDialog" in fn
    assert "danger:true" in fn
    assert "focusCancel:true" in fn
    # Reuses the proven truncate endpoint with the keep_count contract.
    assert "/api/session/truncate" in fn
    assert "keep_count: absoluteKeepCount" in fn
    # Local optimistic update + re-render after the server round-trip.
    assert "S.messages.slice(0, absoluteKeepCount)" in fn
    assert "renderMessages()" in fn
    # Session-switch race guard (mirrors submitEdit).
    assert "S.session.session_id !== initialSid" in fn


# ── Backend dependency: /api/session/truncate guards ─────────────────────


def test_truncate_endpoint_guards_keep_count():
    # The delete/edit UI depends on truncate semantics; keep its validations.
    assert 'if parsed.path == "/api/session/truncate":' in ROUTES_PY
    assert "keep_count must be non-negative" in ROUTES_PY
    assert "truncate_session_at_keep(s, keep)" in ROUTES_PY
    assert "s.save()" in ROUTES_PY


# ── i18n ─────────────────────────────────────────────────────────────────


def test_delete_i18n_keys_in_english_locale():
    en_block = _en_locale_block()
    for key in (
        "delete_message",
        "delete_message_confirm_title",
        "delete_message_confirm_message",
        "delete_failed",
    ):
        assert key in en_block, f"missing i18n key {key!r} in en locale"


# ── Mobile safety ────────────────────────────────────────────────────────


def test_msg_foot_wraps_on_narrow_screens():
    # #6737 adds one more action button; .msg-foot must keep wrapping so the
    # actions stay on-screen at phone width (no overflow regression).
    foot_idx = STYLE_CSS.index(".msg-foot {")
    foot_block = STYLE_CSS[foot_idx : foot_idx + 700]
    assert "flex-wrap: wrap" in foot_block
