"""#7855 review round 6 — the four findings the re-gate still reproduced.

Round 6 fixed the wrong-turn admission property. The 2026-10-08 re-gate found
four more, two CORE:

1. **[CORE] A refresh after a failed continuation start silently ends the goal
   loop.** The persist saved only text and files; the restore put the text back
   without re-marking the id. The retry then posted ``goal_continuation_id:
   None``, which start admission scores ``goal_related=false``. Master admits
   the same retry as goal-related.

2. **[CORE] A genuine message can steal the id after a failed start.** The
   deferred persist re-bound the token to whatever text was in the composer, so
   a message typed after the failure inherited it and consumed the server's
   pending record.

3. **[SHOULD-FIX] Default ``steer`` mode drops the id.** A tokenized
   continuation arriving while busy was steered into the live run; nothing was
   queued or posted and the server record was orphaned with no TTL.

4. **[SHOULD-FIX] A restored draft re-entered during an in-flight send** was
   queued without its id, because the re-entrant guard runs before the
   restored-draft resolution.
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
    # Skip default-parameter braces (`opts={}`) by finding the `{` that opens
    # the BODY: the first one at depth 0 that is followed by a newline or is not
    # part of an `= {...}` default.
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
    i = source.find("{", i)
    assert i >= 0, f"no body brace after {marker!r}"
    depth = 0
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces from {marker!r}")


# ── finding 1: the token survives a reload between failure and retry ────────


def test_the_save_helper_accepts_and_persists_the_token() -> None:
    """``_saveComposerDraftNow`` must carry the token in its POST body."""
    body = _js_function(SESSIONS_JS, "function _saveComposerDraftNow(")
    assert "goalContinuationId" in body, (
        "the draft save helper does not accept a continuation token, so a "
        "reload between a failed start and its retry loses it (#7855 finding 1)"
    )
    assert "body.goal_continuation_id" in body, (
        "the token is accepted but never put on the wire"
    )


def test_the_backend_draft_route_stores_the_token() -> None:
    """``/api/session/draft`` must persist ``goal_continuation_id``."""
    start = ROUTES_PY.find('if parsed.path == "/api/session/draft":')
    assert start >= 0
    body = ROUTES_PY[start : start + 9000]
    assert 'body.get("goal_continuation_id")' in body, (
        "the draft route ignores goal_continuation_id; the client persists a "
        "field the server drops (#7855 finding 1)"
    )
    # The write itself is applied by the shared projection helper, so the route
    # must delegate to it rather than reimplementing the field rules.
    assert "_project_session_draft_write(" in body, (
        "the draft route does not apply the shared draft-write projection"
    )
    assert "def _project_session_draft_write(" in ROUTES_PY, (
        "the draft-write projection helper is missing (#7855 finding 1)"
    )


def test_the_restore_path_re_marks_the_token() -> None:
    """``_restoreComposerDraft`` must bind the stored token to the restored text."""
    body = _js_function(SESSIONS_JS, "function _restoreComposerDraft(")
    assert "_setRestoredGoalContinuationDraft(" in body, (
        "the restore path does not re-mark the persisted continuation token, "
        "so a reload restores the text and drops the id (#7855 finding 1)"
    )
    assert "draft.goal_continuation_id" in body, (
        "the restore path does not read the token out of the stored draft"
    )


def test_the_failed_send_persist_passes_the_token() -> None:
    """The failed-start persist must hand the token to the save helper."""
    start = MESSAGES_JS.find("function _restoreComposerDraftAfterFailedSend(")
    assert start >= 0
    body = MESSAGES_JS[start : start + 6000]
    assert "_saveComposerDraftNow(sid, liveText" in body, (
        "the failed-send persist no longer calls the save helper"
    )
    # The 4th argument is the token, and it is conditional on the text being
    # unchanged — an edited draft must not inherit it.
    assert "_restoreContId:'')" in body, (
        "the failed-send persist does not pass the token (and does not fail "
        "closed on an edited draft) (#7855 finding 1)"
    )


def test_an_edited_draft_does_not_persist_the_token() -> None:
    """The token is persisted only while the text is the one it belongs to."""
    start = MESSAGES_JS.find("function _restoreComposerDraftAfterFailedSend(")
    body = MESSAGES_JS[start : start + 6000]
    assert (
        "String(liveText||'').trim()===String(restore||'').trim()?_restoreContId:''"
        in body
    ), (
        "the persist must drop the token as soon as the composer text differs "
        "from the restored draft (#7855 finding 1)"
    )


def test_submitting_the_draft_clears_the_stored_token() -> None:
    """``_clearComposerDraft`` must send an explicit empty token."""
    body = _js_function(SESSIONS_JS, "function _clearComposerDraft(")
    assert "goal_continuation_id: ''" in body, (
        "clearing the draft leaves the stored token behind on the server, and "
        "the next session to restore that draft inherits it (#7855 finding 1)"
    )


# ── finding 2: a genuine message cannot steal the token ─────────────────────


def test_the_deferred_persist_does_not_rebind_the_token() -> None:
    """The persist must not re-bind the token to the live composer text."""
    start = MESSAGES_JS.find("function _restoreComposerDraftAfterFailedSend(")
    body = MESSAGES_JS[start : start + 6000]
    # The round-5 line bound the token to whatever was in the composer at
    # persist time; a genuine message typed after the failure inherited it.
    assert "_setRestoredGoalContinuationDraft(_restoreContId,liveText)" not in body, (
        "the deferred persist re-binds the token to the live text, so a genuine "
        "message typed after the failed start steals it (#7855 finding 2)"
    )
    # The restore-time binding is the only one, and it is guarded on the text
    # being unchanged.
    assert "_setRestoredGoalContinuationDraft(_restoreContId,restore)" in body, (
        "the restore-time binding must still exist — it is what lets a one-key "
        "resend carry the token"
    )


# ── finding 3: steer must not swallow a tokenized continuation ──────────────


def test_a_tokenized_continuation_is_not_steered() -> None:
    """Steering orphans the server's pending record (no TTL)."""
    start = MESSAGES_JS.find("const defaultMessageMode=window._defaultMessageMode||'steer';")
    assert start >= 0
    body = MESSAGES_JS[start : start + 900]
    steer = re.search(r"if\(defaultMessageMode==='steer'[^)]*\)\{", body)
    assert steer, "the steer branch was not found"
    assert "!_goalContinuationId" in steer.group(0), (
        "a tokenized continuation is still steered into the live run, which "
        "queues nothing, posts nothing, and orphans the server's pending "
        "record (#7855 finding 3)"
    )


# ── finding 4: the re-entrant guard resolves a restored token ───────────────


def test_the_reentrant_guard_resolves_a_restored_token() -> None:
    """The guard runs before the restored-draft resolution, so it must resolve."""
    start = MESSAGES_JS.find("if (_sendInProgress) {")
    assert start >= 0
    guard = MESSAGES_JS[start : MESSAGES_JS.index("_sendInProgress = true;", start)]
    assert "_takeRestoredDraftGoalContinuationId(_text)" in guard, (
        "a restored continuation re-entered mid-send is queued without its "
        "token, and the dataset marker is left on the composer "
        "(#7855 finding 4)"
    )
    # It must still never read another invocation's token.
    assert "_sendInProgressGoalContinuationId" not in MESSAGES_JS, (
        "the shared in-flight continuation slot is back"
    )
    assert "_requeueContId=_goalContinuationId" in guard, (
        "the guard must seed from its OWN argument before falling back to the "
        "restored draft"
    )


# ── behavioural: the round trip through the real reader/writer ──────────────


def test_the_token_survives_a_draft_round_trip(tmp_path) -> None:
    """Save → read back: the token is still bound to its text.

    Uses the real route handler's draft-projection logic rather than a stub, so
    the field names and the clear-on-empty rule are the ones production uses.
    """
    db = tmp_path / "state.db"
    conn = __import__("sqlite3").connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, session_source TEXT, title TEXT,
            model TEXT, started_at REAL NOT NULL, message_count INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            timestamp REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, session_source, title, model,"
        " started_at, message_count) VALUES ('s1','tui','tui','T','m',1.0,0)"
    )
    conn.commit()
    conn.close()

    # Drive the projection the route uses, so the test fails if the field name
    # or the empty-string rule changes.
    from api.routes import _project_session_draft_write

    stored = _project_session_draft_write(
        {"text": "continuation text to retry", "files": []},
        {"text": "continuation text to retry", "files": [], "goal_continuation_id": "0123abcd"},
    )
    assert stored.get("goal_continuation_id") == "0123abcd", (
        "the token was not stored alongside its draft (#7855 finding 1)"
    )

    cleared = _project_session_draft_write(
        {"text": "", "files": [], "goal_continuation_id": ""},
        dict(stored),
    )
    assert "goal_continuation_id" not in cleared, (
        "an explicitly empty token must clear the stored one (#7855 finding 1)"
    )
