"""#7855 review round 7 — the three findings the 2026-10-08 re-gate reproduced.

Round 6 fixed the wrong-turn admission property. The round-7 re-gate found
one server-side defect and two front-end ones, plus a CI pin and a
security hardening item:

1. **[CORE, MUST-FIX 1] An edited draft re-binds the stale token across
   reload or session switch.** ``_project_session_draft_write``
   (``api/routes.py``) kept the stored token whenever the field was
   absent, and the keystroke autosave (``static/boot.js`` into
   ``static/sessions.js``) and the session-switch save
   (``static/sessions.js:2388``) never send it. The server therefore
   stored the user's edited text next to the old token, and the restore
   path bound them together — the retry then posted the edited text WITH
   the continuation id, so a genuine turn consumed the real continuation
   record. Pending records have no TTL (``api/goals.py``), which is the
   #6885 bug this PR exists to fix.

2. **[CORE] A continuation start that fails after the user switches
   sessions loses its token.** The background restore saves only
   text/files (``static/messages.js:1465``); returning and retrying
   posted no id and admission scored ``goal_related=false`` versus
   ``true`` on master.

3. **[CORE] Switching away from a restored queued continuation drops its
   token.** The switch save omits the fourth argument
   (``static/sessions.js:2388``), so the return restored an ordinary
   draft.

4. **[P1, security] The draft token has no limit.** Unlike draft text,
   ``goal_continuation_id`` had no size or shape validation before it was
   persisted into the session JSON.

5. **[CI] ``tests/test_composer_draft_after_send.py`` pins the old
   3-argument signature** of ``_saveComposerDraftNow`` and fails at this
   head (the file is untouched by this PR and passes on master).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0,
    reason="node not on PATH",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def _js_function(source: str, marker: str) -> str:
    start = source.find(marker)
    assert start >= 0, f"marker not found: {marker!r}"
    i = source.find("(", start)
    depth = 0
    while i < len(source):
        ch = source[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body_start = source.find("{", i)
    depth = 0
    j = body_start
    while j < len(source):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return source[body_start : j + 1]
        j += 1
    raise AssertionError(f"unbalanced braces after {marker!r}")


def _projection_helper() -> str:
    start = ROUTES_PY.find("def _project_session_draft_write(")
    assert start >= 0, "the draft-write projection helper is missing"
    nxt = ROUTES_PY.find("\ndef ", start + 10)
    return ROUTES_PY[start : nxt if nxt > 0 else len(ROUTES_PY)]


# ── Finding 1: an edited draft must not keep the stale token ────────────────


def test_the_projection_drops_the_token_when_the_text_changes() -> None:
    """The absent-field branch must not blindly keep the stored token: when
    the draft text it is stored against has changed, the token no longer
    describes it and must be dropped."""
    body = _projection_helper()
    assert "goal_continuation_id" in body, (
        "the projection helper does not handle the continuation token"
    )
    assert "text != _stored_text" in body or "text!=_stored_text" in body, (
        "the absent-field branch never compares the incoming text with the "
        "stored one, so an edited draft keeps the stale token and a reload "
        "re-binds them (#7855 round-7 MUST-FIX 1)"
    )
    assert 'pop("goal_continuation_id", None)' in body, (
        "the projection helper never clears a stale token"
    )


# ── Finding 2: the background failed-send persist must carry the token ──────


def test_the_background_failed_send_persist_carries_the_token() -> None:
    """The background-restore branch of the failed-send persist saves only
    text/files today; the token must travel with that snapshot."""
    body = _js_function(
        MESSAGES_JS, "function _restoreComposerDraftAfterFailedSend("
    )
    assert "_saveComposerDraftNow(sid, restore, files, _restoreContId)" in body, (
        "the background failed-send persist drops the continuation token, so "
        "a return to that session and a retry posts no id and admission "
        "scores goal_related=false (#7855 round-7 CORE)"
    )


# ── Finding 3: the session-switch save must persist the token ───────────────


def test_the_session_switch_save_persists_the_token() -> None:
    """Switching away from a session whose composer holds a restored
    continuation must save the token with the exact text it belongs to."""
    switch_body = _js_function(SESSIONS_JS, "async function loadSession(")
    assert "_saveComposerDraftNow(currentSid" in switch_body, (
        "the session-switch save call site was not found"
    )
    assert "_saveComposerDraftNow(currentSid, ($('msg') || {}).value || '', S.pendingFiles ? [...S.pendingFiles] : [], _switchContId)" in switch_body, (
        "the session-switch save omits the continuation token, so a restored "
        "queued continuation degrades to an ordinary draft on return "
        "(#7855 round-7 CORE)"
    )


def test_a_non_consuming_peek_of_the_binding_exists() -> None:
    """The switch save must read the binding WITHOUT consuming it — the
    one-shot marker has to stay armed for the user's resend."""
    assert "function _peekRestoredGoalContinuationDraft()" in MESSAGES_JS, (
        "no non-consuming read of the restored-continuation binding exists; "
        "the switch save cannot persist the token without disarming it "
        "(#7855 round-7 CORE)"
    )
    peek_body = _js_function(
        MESSAGES_JS, "function _peekRestoredGoalContinuationDraft("
    )
    assert "delete _msg.dataset.goalContinuationId" not in peek_body, (
        "the peek helper consumes the binding — it must be read-only"
    )
    assert "_switchContId" in SESSIONS_JS, (
        "the session-switch save never resolves the token it should persist"
    )


# ── Finding 4: the draft token gets a size/shape limit ──────────────────────


def test_the_draft_route_validates_the_token_shape() -> None:
    """``goal_continuation_id`` had no size or shape limit before it was
    persisted into the session JSON; the producer mints uuid4().hex and
    admission requires exactly 32 lowercase hex characters."""
    start = ROUTES_PY.find('if parsed.path == "/api/session/draft":')
    assert start >= 0
    body = ROUTES_PY[start : start + 12000]
    assert "_looks_like_cont_id" in body, (
        "the draft route does not validate goal_continuation_id's shape, so "
        "an authenticated client can persist an arbitrarily large value into "
        "the session JSON on every keystroke (#7855 round-7 P1)"
    )
    assert "0123456789abcdef" in body, (
        "the token validation does not enforce the producer's hex alphabet"
    )


# ── Finding 5: the master-green CI pin matches the new signature ────────────


def test_the_signature_pin_matches_the_save_helper() -> None:
    """``tests/test_composer_draft_after_send.py`` pins the save helper's
    signature; the added fourth parameter must be reflected there or shard 4
    stays red on a file this PR does not otherwise touch."""
    pin_file = REPO_ROOT / "tests" / "test_composer_draft_after_send.py"
    text = pin_file.read_text(encoding="utf-8")
    assert "function _saveComposerDraftNow(sid, text, files, goalContinuationId)" in text, (
        "the signature pin still names the 3-argument form, so the "
        "_block() lookup raises ValueError and shard 4 fails (#7855 round-7 CI)"
    )
    assert "function _saveComposerDraftNow(sid, text, files, goalContinuationId)" in SESSIONS_JS, (
        "the save helper's real signature does not match the pin"
    )
