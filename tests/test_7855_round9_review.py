"""#7855 review round 9 — the 2026-10-13 re-gate's three MUST-FIX items.

Round 8 shipped the server-side projection and the failed-send / switch-save
token threading. The re-gate at head 553a74168999 reproduced three more, all of
them token-loss or token-leak shapes:

1. **[MUST-FIX 1] A two-client autosave drops an unchanged continuation's
   token.** The debounced autosave (``static/sessions.js`` ``_saveComposerDraft``)
   never sent ``goal_continuation_id`` at all. After another client changed the
   stored draft, this tab's own keystroke re-save therefore overwrote the text
   without the token, and the return restored an ordinary draft.

2. **[MUST-FIX 2] A pre-upgrade tab loses master's marker admission.** ``send()``
   posted ``goal_continuation_id: _goalContinuationId || undefined`` and
   ``JSON.stringify`` drops undefined keys, so a genuine turn and a pre-upgrade
   tab's turn were byte-identical on the wire. A key-presence legacy path would
   admit every genuine turn — the #6885 bug this PR exists to fix. Current
   clients must send ``||''`` (key always present) and the server must resolve
   the pending ID server-side only when the key is ABSENT.

3. **[MUST-FIX 3] A same-session refresh drops an unchanged continuation's
   token.** ``loadSession`` cleared the restored binding unconditionally; a
   force refresh is not a session switch, so the binding must survive it.

Plus **[SHOULD-FIX 4]**: four comment/doc lines that still said pause retires
the record (it does not — see test_7855_regate_behaviour CORE 2).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
GOALS_PY = (REPO_ROOT / "api" / "goals.py").read_text(encoding="utf-8")
CONFIG_PY = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
RFC_MD = (REPO_ROOT / "docs" / "rfcs" / "session-sse-contract-v1.md").read_text(encoding="utf-8")


# ── MUST-FIX 1: the debounced autosave must snapshot and send the token ──────


def test_the_debounced_autosave_snapshots_the_restored_binding() -> None:
    """The autosave must read the restored binding when it is SCHEDULED.

    A peek inside the timer callback would read the binding AFTER the user's
    edit (or another client's refresh) had already cleared or replaced it —
    which is how the two-client sequence lost the token in the first place.
    """
    start = SESSIONS_JS.find("function _saveComposerDraft(sid, text, files) {")
    assert start >= 0, "the debounced save helper is missing"
    end = SESSIONS_JS.find("function _safeDraftSessionId(", start)
    assert end > start, "could not delimit the debounced save helper"
    body = SESSIONS_JS[start:end]

    timer_idx = body.find("_draftSaveTimer = setTimeout(")
    assert timer_idx >= 0, "the debounced save no longer schedules a timer"
    snapshot_idx = body.find("_peekRestoredGoalContinuationDraft()")
    assert 0 <= snapshot_idx < timer_idx, (
        "the restored continuation binding is not snapshotted BEFORE the "
        "autosave is scheduled, so another client's draft write or a local "
        "edit can clear it first and the token is lost (#7855 round 9 "
        "MUST-FIX 1)"
    )


def test_the_debounced_autosave_sends_the_token_with_its_own_text() -> None:
    """The id is sent only when its bound text equals the text being saved."""
    start = SESSIONS_JS.find("function _saveComposerDraft(sid, text, files) {")
    end = SESSIONS_JS.find("function _safeDraftSessionId(", start)
    body = SESSIONS_JS[start:end]
    # The comparison must be inside the timer callback, against the text that
    # was scheduled — not a live read of the composer.
    callback = body[body.find("_draftSaveTimer = setTimeout("):]
    assert "_restoredCont.text" in callback and "normalizedText" in callback, (
        "the autosave does not compare the restored binding's text with the "
        "text being saved, so an edited draft can carry a stale token "
        "(#7855 round 9 MUST-FIX 1 / round-7 CORE)"
    )
    # And the payload helper must receive the resolved id (possibly empty).
    assert "_postComposerDraftPayload(sid, normalizedDraft, intent, _autosaveContId)" in callback, (
        "the autosave does not hand the resolved continuation id to the "
        "payload helper, so the token is dropped (#7855 round 9 MUST-FIX 1)"
    )


def test_the_draft_payload_helper_always_sends_the_token_field() -> None:
    """The wire body must ALWAYS carry ``goal_continuation_id``.

    Sending the field only when a token exists is what let the keystroke
    autosave silently drop the token of an unchanged continuation: the server's
    absent-field rule preserved the stored token, but this tab's save had
    already lost its own binding. The explicit empty string is the fail-closed
    signal that clears a stale stored token (round-7 rule).
    """
    start = SESSIONS_JS.find("function _postComposerDraftPayload(")
    assert start >= 0, "the shared draft payload helper is missing"
    nxt = SESSIONS_JS.find("\nfunction ", start + 10)
    body = SESSIONS_JS[start : nxt if nxt > 0 else len(SESSIONS_JS)]
    assert "goalContinuationId=''" in body, (
        "the payload helper does not accept a continuation token argument, so "
        "the callers cannot thread the id onto the wire"
    )
    assert "goal_continuation_id: normalizedGoalContId" in body, (
        "the payload helper still omits goal_continuation_id from the POST "
        "body, so the token is never persisted with the draft text "
        "(#7855 round 9 MUST-FIX 1)"
    )
    # The clear path must therefore still be an explicit empty string.
    clear_start = SESSIONS_JS.find("function _clearComposerDraft(sid, text, files) {")
    clear_end = SESSIONS_JS.find("const SESSION_VIEWED_COUNTS_KEY", clear_start)
    clear_body = SESSIONS_JS[clear_start:clear_end]
    assert "_postComposerDraftPayload(sid, { text: '', files: [] }, intent)" in clear_body, (
        "the clear no longer posts through the shared payload helper"
    )
    # No 4th argument → the helper's default '' clears the stored token.
    assert "goal_continuation_id" not in clear_body.replace(
        "goal_continuation_id: normalizedGoalContId", ""
    ), "the clear must rely on the helper's default empty token, not re-specify it"


def test_the_switch_save_still_threads_the_peeked_token() -> None:
    """Round 7's switch-save fix must survive the rebase."""
    start = SESSIONS_JS.find("async function loadSession(")
    assert start >= 0
    nxt = SESSIONS_JS.find("\nfunction ", start + 10)
    body = SESSIONS_JS[start : nxt if nxt > 0 else len(SESSIONS_JS)]
    assert "_peekRestoredGoalContinuationDraft()" in body, (
        "the session-switch save no longer peeks the restored binding "
        "(#7855 round 7 MUST-FIX 3)"
    )
    assert "_saveComposerDraftNow(currentSid, draftText, draftFiles, _switchContId)" in body, (
        "the session-switch save omits the continuation token (#7855 round 7 "
        "MUST-FIX 3)"
    )


# ── MUST-FIX 3: a same-session force refresh must keep the binding ───────────


def test_a_same_session_force_refresh_keeps_the_restored_binding() -> None:
    """``loadSession`` must clear the binding only on a real session switch.

    A force refresh (external poll / state.db change / boot restore of the
    SAME session) is not a context boundary: clearing the binding there
    dropped an unchanged continuation's token while the visible local text
    survived, so the next send posted no id and became an ordinary turn.
    """
    start = SESSIONS_JS.find("async function loadSession(")
    assert start >= 0
    nxt = SESSIONS_JS.find("\nfunction ", start + 10)
    body = SESSIONS_JS[start : nxt if nxt > 0 else len(SESSIONS_JS)]
    assert "currentSid !== sid" in body, (
        "loadSession no longer compares currentSid with sid"
    )
    clear_idx = body.find("_clearRestoredGoalContinuationDraft()")
    assert clear_idx >= 0, "loadSession no longer clears the restored binding"
    guard = body[max(0, clear_idx - 400):clear_idx]
    assert "currentSid !== sid" in guard, (
        "the restored-continuation clear is not guarded by currentSid !== sid, "
        "so a same-session force refresh drops an unchanged continuation's "
        "token (#7855 round 9 MUST-FIX 3)"
    )


# ── MUST-FIX 2: the wire shape and the server's legacy-body path ─────────────


def test_current_clients_always_send_the_key() -> None:
    """``send()`` must post ``||''``, never ``||undefined``.

    ``JSON.stringify`` drops undefined keys, which made a current client's
    genuine turn and a pre-upgrade tab's turn byte-identical on the wire. With
    a key-presence legacy path on the server, that would admit every genuine
    turn — #6885 all over again.
    """
    assert "goal_continuation_id:_goalContinuationId||''" in MESSAGES_JS, (
        "send() does not post the ID as `||''`, so the key can be dropped from "
        "the wire body (#7855 round 9 MUST-FIX 2)"
    )
    assert "goal_continuation_id:_goalContinuationId||undefined" not in MESSAGES_JS, (
        "send() still posts `||undefined`; a genuine turn and a pre-upgrade "
        "tab's turn are byte-identical again (#6885 via the legacy path)"
    )


def test_the_chat_start_resolver_peeks_only_when_the_key_is_absent() -> None:
    """The server resolves the pending ID ONLY for a legacy-shaped body.

    A key-presence legacy path (peeking on every turn) would admit every
    genuine turn. The discriminator must be the KEY's absence, which only a
    pre-upgrade tab can produce now that current clients send ``||''``.
    """
    assert "def _resolve_chat_start_goal_continuation_id(" in ROUTES_PY, (
        "api/routes.py has no continuation-ID resolver for the chat-start body"
    )
    start = ROUTES_PY.find("def _resolve_chat_start_goal_continuation_id(")
    nxt = ROUTES_PY.find("\ndef ", start + 10)
    body = ROUTES_PY[start : nxt if nxt > 0 else len(ROUTES_PY)]
    assert '"goal_continuation_id" not in body' in body, (
        "the resolver does not gate the peek on the key being ABSENT, so a "
        "current client's genuine turn (explicit empty string) would peek the "
        "session's pending ID and consume the goal — #6885 again "
        "(#7855 round 9 MUST-FIX 2)"
    )
    assert "peek_pending_goal_continuation_id(session_id)" in body, (
        "the resolver does not resolve the pending ID through "
        "peek_pending_goal_continuation_id, so a pre-upgrade tab loses "
        "master's marker admission"
    )
    # The chat/start handler must use the resolver, not the raw normalizer.
    assert '"goal_continuation_id": _resolve_chat_start_goal_continuation_id(' in ROUTES_PY, (
        "/api/chat/start still normalizes the body field directly, so the "
        "legacy-shaped body never reaches the peek path"
    )


class TestLegacyShapedBodyAdmission:
    """Behavioural gate: a body without the key still admits the pending record.

    Drives the REAL resolver with a REAL pending record, and proves the
    explicit-empty-string (genuine turn) and explicit-ID shapes keep their
    #6885 semantics.
    """

    @pytest.fixture(autouse=True)
    def _clean_markers(self):
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_PROMPTS

        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_PROMPTS.clear()
        yield
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_PROMPTS.clear()

    def _resolver(self):
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from api.routes import _resolve_chat_start_goal_continuation_id

        return _resolve_chat_start_goal_continuation_id

    def test_a_body_without_the_key_peeks_the_pending_id(self):
        from api.goals import (
            consume_pending_goal_continuation,
            register_pending_goal_continuation,
        )

        cont_id = register_pending_goal_continuation("s1", "continue step 2")
        assert cont_id
        resolved = self._resolver()({"message": "continue step 2"}, "s1")
        assert resolved == cont_id, (
            "a pre-upgrade tab's legacy-shaped body (no goal_continuation_id "
            "key) does not resolve the session's pending ID, so it loses "
            "master's marker admission (#7855 round 9 MUST-FIX 2)"
        )
        # And the resolved id goes through the ordinary admission, which
        # consumes the record and returns a rollback receipt.
        receipt = consume_pending_goal_continuation("s1", resolved)
        assert receipt and receipt.get("continuation_id") == cont_id

    def test_a_genuine_turn_with_an_explicit_empty_string_does_not_peek(self):
        from api.goals import (
            consume_pending_goal_continuation,
            register_pending_goal_continuation,
        )

        cont_id = register_pending_goal_continuation("s1", "continue step 2")
        resolved = self._resolver()(
            {"message": "my own question", "goal_continuation_id": ""}, "s1"
        )
        assert resolved == "", (
            "a current client's genuine turn (explicit empty string) resolved "
            f"to a real id ({resolved!r}) — the key-presence path would admit "
            "every genuine turn and #6885 is back (#7855 round 9 MUST-FIX 2)"
        )
        # The record must survive for the browser's real dispatch.
        assert consume_pending_goal_continuation("s1", cont_id), (
            "the pending record was consumed by the genuine-turn probe"
        )

    def test_an_explicit_id_still_admits_by_id(self):
        from api.goals import register_pending_goal_continuation

        cont_id = register_pending_goal_continuation("s1", "continue step 2")
        resolved = self._resolver()(
            {"message": "continue step 2, edited", "goal_continuation_id": cont_id.upper()},
            "s1",
        )
        assert resolved == cont_id, (
            "an explicit continuation id is not normalized/admitted as before"
        )

    def test_a_malformed_explicit_id_degrades_to_no_id(self):
        from api.goals import register_pending_goal_continuation

        cont_id = register_pending_goal_continuation("s1", "continue step 2")
        resolved = self._resolver()(
            {"message": "x", "goal_continuation_id": "not-a-hex-token"}, "s1"
        )
        assert resolved == "", (
            "a malformed explicit id reached the matcher instead of "
            "degrading to the genuine-turn signal"
        )
        assert cont_id, "the pending record setup failed"

    def test_a_legacy_body_with_no_pending_record_is_an_ordinary_turn(self):
        resolved = self._resolver()({"message": "hello"}, "s-none")
        assert resolved == "", (
            "a legacy-shaped body with no pending record resolved to a real id"
        )


# ── SHOULD-FIX 4: the docs must not say pause retires the record ─────────────


def test_the_docs_do_not_say_pause_retires_the_record() -> None:
    for name, src in (
        ("api/config.py", CONFIG_PY),
        ("api/goals.py", GOALS_PY),
        ("docs/rfcs/session-sse-contract-v1.md", RFC_MD),
    ):
        assert "pause / session" not in src.replace(
            "Pause does NOT retire it", ""
        ).replace("Pause does NOT retire a record", ""), (
            f"{name} still documents pause as retiring the continuation record "
            "(#7855 round 9 SHOULD-FIX 4)"
        )
        assert "goal clear/pause`" not in src, (
            f"{name} still lists `goal clear/pause` as the retirement path "
            "(#7855 round 9 SHOULD-FIX 4)"
        )
