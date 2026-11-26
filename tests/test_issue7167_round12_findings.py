"""Round-12 review finding on #7167 (nesquena-hermes 2026-11-23).

SILENT — a repeated-prompt legacy core row takes the current turn's identity.

A modeled legacy core snapshot holds user ``repeat`` at 100.0 and assistant
``same answer`` at 101.0. The saved WebUI session carries a NEW pending
``repeat`` at 900.5 with its own stream, source and attachment, and the real
journal writer records ``same answer`` plus completion for that new stream.
After cold recovery on the head:

- only the 100.0 user row remained, restamped with the new turn's token,
  attachment and source;
- the 900.5 user boundary was absent from both the saved display transcript
  and the provider context;
- the same assertion failed when the old row was tokenless and when it
  carried an explicit foreign token.

``_core_transcript_holds_pending_turn`` accepted equal prompt text plus a
matching journal-text substring in the old assistant/tool tail, so an older
equal-content row was treated as the current turn. Fix: reject explicit
conflicting owner/token/timestamp evidence before suppressing the pending
user or backfilling its predecessor; when positive ownership cannot be
established, preserve the pending boundary instead of assigning its metadata
to history.
"""

from __future__ import annotations

import json

import pytest

import api.models as models
import api.profiles as profiles
from api.models import Session, _apply_core_sync_or_error_marker
from api.run_journal import append_run_event
from api.streaming import _context_messages_for_new_turn, _session_context_messages

PROMPT = "repeat"
OLD_ANSWER = "same answer"
OLD_USER_TS = 100.0
OLD_ASSISTANT_TS = 101.0
STARTED_AT = 900.5
STREAM_ID = "stream-r12-new"
ATTACHMENT = "/tmp/upload.png"
FOREIGN_TOKEN = "stream-old:100"
CURRENT_TOKEN = f"{STREAM_ID}:{STARTED_AT:.17g}"


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolate HERMES_HOME + the WebUI state dir so journal writes sandbox."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    # Session.load() resolves SESSION_DIR at import time; redirect the module
    # global so the fresh-file reload reads OUR sandbox (round-8 pattern).
    monkeypatch.setattr(models, "SESSION_DIR", home / "sessions")
    monkeypatch.setattr(
        models, "SESSION_INDEX_FILE", home / "sessions" / "_index.json"
    )
    return home


def _write_core_transcript(hermes_home, sid, old_user_extra=None):
    """Write the modeled legacy core snapshot and return its path."""
    old_user = {
        "role": "user",
        "content": PROMPT,
        "timestamp": OLD_USER_TS,
    }
    if old_user_extra:
        old_user.update(old_user_extra)
    core = {
        "messages": [
            old_user,
            {"role": "assistant", "content": OLD_ANSWER, "timestamp": OLD_ASSISTANT_TS},
        ],
    }
    path = hermes_home / "sessions" / f"session_{sid}.json"
    path.write_text(json.dumps(core), encoding="utf-8")
    return path


def _make_pending_session(sid, started_at: float | None = STARTED_AT):
    """Empty-messages session with the NEW pending turn (reviewer's start state)."""
    session = Session(
        session_id=sid,
        title="r12 repeated prompt keeps both boundaries",
        messages=[],
        pending_user_message=PROMPT,
        pending_started_at=started_at,
        pending_attachments=[ATTACHMENT],
        pending_user_source="cli",
        active_stream_id=STREAM_ID,
    )
    session.save()
    return session


def _core_synced_session(sid, old_user_extra=None, drop_timestamp=False):
    """Session whose messages already hold the synced legacy core rows.

    This is the state ``_apply_core_sync_or_error_marker`` builds internally
    before it consults ``_core_transcript_holds_pending_turn``, so the
    exact-source assertions exercise the helper on the shape the reviewer
    reproduced instead of on an empty transcript.
    """
    old_user = {"role": "user", "content": PROMPT, "timestamp": OLD_USER_TS}
    if drop_timestamp:
        old_user.pop("timestamp")
    if old_user_extra:
        old_user.update(old_user_extra)
    session = Session(
        session_id=sid,
        title="r12 core-synced shape",
        messages=[
            old_user,
            {
                "role": "assistant",
                "content": OLD_ANSWER,
                "timestamp": OLD_ASSISTANT_TS,
            },
        ],
        pending_user_message=PROMPT,
        pending_started_at=STARTED_AT,
        pending_attachments=[ATTACHMENT],
        pending_user_source="cli",
        active_stream_id=STREAM_ID,
    )
    for row in session.messages:
        if isinstance(row, dict):
            row["_from_core_transcript"] = True
    return session


def _write_new_stream_journal(sid):
    """The real run-journal writer records this stream's answer + completion."""
    append_run_event(sid, STREAM_ID, "token", {"text": OLD_ANSWER})
    append_run_event(sid, STREAM_ID, "done", {})


def _assert_current_boundary_preserved(loaded, token):
    """Display + provider context must keep BOTH user boundaries, each with
    its own identity: the historical 100.0 row and the current 900.5 row."""
    display_users = [
        m for m in loaded.messages
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    current_rows = [m for m in display_users if m.get("timestamp") == STARTED_AT]
    assert len(current_rows) == 1, (
        "the current turn's user boundary must be materialized, got "
        f"{len(current_rows)} rows; transcript="
        f"{[(m.get('timestamp'), str(m.get('content'))[:16]) for m in display_users]!r}"
    )
    current = current_rows[0]
    assert current.get("content") == PROMPT
    assert current.get("attachments") == [ATTACHMENT], (
        "the current row keeps the pending attachment, got "
        f"{current.get('attachments')!r}"
    )
    assert current.get("_active_turn_token") == token, (
        "the current row keeps its own turn token, got "
        f"{current.get('_active_turn_token')!r}"
    )
    assert current.get("_source") == "cli", (
        "the current row keeps its own source, got {current.get('_source')!r}"
    )
    old_rows = [m for m in display_users if m.get("timestamp") == OLD_USER_TS]
    assert len(old_rows) == 1, (
        "the historical same-text turn must stay intact, got "
        f"{len(old_rows)} rows"
    )
    old = old_rows[0]
    assert not old.get("attachments"), (
        "the historical same-text row must never receive the current turn's "
        f"attachment, got {old.get('attachments')!r}"
    )
    assert old.get("_active_turn_token") != token, (
        "the historical same-text row must never receive the current turn token"
    )
    assert old.get("_source") != "cli" or not old.get("_source"), (
        "the historical same-text row must never receive the current turn's "
        f"source, got {old.get('_source')!r}"
    )

    # ── provider context: effective history keeps both boundaries ──
    context = _session_context_messages(loaded)
    context_users = [
        m for m in context
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    context_current = [m for m in context_users if m.get("timestamp") == STARTED_AT]
    assert len(context_current) == 1, (
        "the provider context must carry the current user row, got "
        f"{len(context_current)} rows; context="
        f"{[(m.get('timestamp'), str(m.get('content'))[:16]) for m in context_users]!r}"
    )
    assert context_current[0].get("attachments") == [ATTACHMENT], (
        "the context copy keeps the attachment on the current row"
    )
    context_old = [m for m in context_users if m.get("timestamp") == OLD_USER_TS]
    assert len(context_old) == 1, (
        "the provider context must keep the historical user boundary, got "
        f"{len(context_old)} rows"
    )
    assert not context_old[0].get("attachments"), (
        "the historical context row must not carry the current attachment"
    )

    # ── next-send input: the next user turn sees both boundaries ──
    next_input = _context_messages_for_new_turn(loaded, "ask again please")
    next_users = [
        m for m in next_input
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    next_current = [m for m in next_users if m.get("timestamp") == STARTED_AT]
    assert len(next_current) == 1, (
        "the next-send input must carry the current user row, got "
        f"{len(next_current)} rows"
    )
    assert next_current[0].get("attachments") == [ATTACHMENT]
    assert any(m.get("timestamp") == OLD_USER_TS for m in next_users), (
        "the next-send input must keep the historical boundary"
    )


def test_legacy_core_repeated_prompt_tokenless_row_keeps_both_boundaries(
    hermes_home,
):
    """Reviewer's tokenless variant: the old core row has no token, only an
    earlier timestamp. That timestamp is explicit conflicting evidence and
    must stop the core-sync helper from reusing the row for the current turn.
    """
    sid = "r12_core_repeat_tokenless"
    token = CURRENT_TOKEN
    _write_new_stream_journal(sid)

    # Exact-source level: the helper must refuse the identity-less old row
    # once the legacy core rows are synced into the transcript.
    synced = _core_synced_session(sid)
    assert models._core_transcript_holds_pending_turn(
        synced, STREAM_ID, models._normalize_journal_recovery_text(PROMPT)
    ) is False, (
        "an older same-text core row with an earlier timestamp must not be "
        "accepted as the current turn"
    )

    session = _make_pending_session(sid)
    core_path = _write_core_transcript(hermes_home, sid)
    assert _apply_core_sync_or_error_marker(
        session, core_path, stream_id_for_recheck=STREAM_ID
    ) is True
    session.save()

    loaded = Session.load(sid)
    assert loaded is not None
    _assert_current_boundary_preserved(loaded, token)

    # Reload idempotence: a second repair pass on the reloaded sidecar
    # (pending state already cleared) must not append anything.
    before = [dict(m) for m in loaded.messages]
    assert _apply_core_sync_or_error_marker(
        loaded, core_path, stream_id_for_recheck=STREAM_ID
    ) is False
    assert [dict(m) for m in loaded.messages] == before, (
        "a second repair pass must be a no-op once pending state is cleared"
    )


def test_legacy_core_repeated_prompt_foreign_token_row_keeps_both_boundaries(
    hermes_home,
):
    """Reviewer's explicit-token variant: the old core row carries a foreign
    active-turn token. A token conflict is authoritative negative evidence.
    """
    sid = "r12_core_repeat_foreign_token"
    token = CURRENT_TOKEN
    _write_new_stream_journal(sid)

    synced = _core_synced_session(
        sid, old_user_extra={"_active_turn_token": FOREIGN_TOKEN}
    )
    assert models._core_transcript_holds_pending_turn(
        synced, STREAM_ID, models._normalize_journal_recovery_text(PROMPT)
    ) is False, (
        "a core row carrying a foreign active-turn token must not be accepted "
        "as the current turn"
    )

    session = _make_pending_session(sid)
    core_path = _write_core_transcript(
        hermes_home, sid, old_user_extra={"_active_turn_token": FOREIGN_TOKEN}
    )
    assert _apply_core_sync_or_error_marker(
        session, core_path, stream_id_for_recheck=STREAM_ID
    ) is True
    session.save()

    loaded = Session.load(sid)
    assert loaded is not None
    _assert_current_boundary_preserved(loaded, token)
    old_rows = [
        m for m in loaded.messages
        if isinstance(m, dict) and m.get("timestamp") == OLD_USER_TS
    ]
    assert old_rows[0].get("_active_turn_token") == FOREIGN_TOKEN, (
        "the historical row keeps its own foreign token untouched"
    )

    # Reload idempotence: a second repair pass on the reloaded sidecar
    # (pending state already cleared) must not append anything.
    before = [dict(m) for m in loaded.messages]
    assert _apply_core_sync_or_error_marker(
        loaded, core_path, stream_id_for_recheck=STREAM_ID
    ) is False
    assert [dict(m) for m in loaded.messages] == before, (
        "a second repair pass must be a no-op once pending state is cleared"
    )


def test_identity_less_core_row_does_not_suppress_a_stamped_current_turn(hermes_home):
    """Gate 2026-11-25 blocker 1 contract: when the pending turn carries a
    positive start time and the core row carries NO identity, text equality is
    not ownership. The current user turn must be materialized beside the
    historical same-text row, and the historical row must not receive the
    current turn's metadata.

    This supersedes the previous reuse expectation: text plus a journal-text
    overlap could not tell an older same-text turn from an unstamped current
    one, and the reuse path lost the current prompt outright.
    """
    sid = "r12_core_current_tokenless"
    _write_new_stream_journal(sid)

    synced = _core_synced_session(sid, drop_timestamp=True)
    assert models._core_transcript_holds_pending_turn(
        synced, STREAM_ID, models._normalize_journal_recovery_text(PROMPT)
    ) is False, (
        "a pending turn with a positive start time and an identity-less core "
        "row must not be suppressed on text equality alone"
    )

    session = _make_pending_session(sid)
    core_path = _write_core_transcript(hermes_home, sid)
    # Strip the conflicting identity: no timestamp, no token on the core row.
    core = json.loads(core_path.read_text(encoding="utf-8"))
    core["messages"][0].pop("timestamp")
    core_path.write_text(json.dumps(core), encoding="utf-8")

    assert _apply_core_sync_or_error_marker(
        session, core_path, stream_id_for_recheck=STREAM_ID
    ) is True
    user_rows = [m for m in session.messages if m.get("role") == "user"]
    assert len(user_rows) == 2, (
        "the distinct current turn must be retained beside the historical row, "
        f"got {len(user_rows)} user rows"
    )
    old_row, current_row = user_rows
    assert not old_row.get("attachments"), (
        "the historical same-text row must not receive the current turn's metadata"
    )
    assert current_row.get("attachments") == [ATTACHMENT]
    assert current_row.get("_active_turn_token") == CURRENT_TOKEN


def test_current_turn_core_row_still_reused_stamped(hermes_home):
    """Positive control (stamped current turn): the core user row carries the
    current turn's exact timestamp. A matching timestamp is not a conflict.
    """
    sid = "r12_core_current_stamped"
    _write_new_stream_journal(sid)

    synced = _core_synced_session(sid)
    synced.messages[0]["timestamp"] = STARTED_AT
    assert models._core_transcript_holds_pending_turn(
        synced, STREAM_ID, models._normalize_journal_recovery_text(PROMPT)
    ) is True, (
        "a core row stamped with the current turn's own timestamp is still "
        "the current turn"
    )

    session = _make_pending_session(sid)
    core_path = _write_core_transcript(hermes_home, sid)
    core = json.loads(core_path.read_text(encoding="utf-8"))
    core["messages"][0]["timestamp"] = STARTED_AT
    core_path.write_text(json.dumps(core), encoding="utf-8")

    assert _apply_core_sync_or_error_marker(
        session, core_path, stream_id_for_recheck=STREAM_ID
    ) is True
    user_rows = [m for m in session.messages if m.get("role") == "user"]
    assert len(user_rows) == 1, (
        "the stamped current turn must be reused, not duplicated, got "
        f"{len(user_rows)} user rows"
    )


def test_nonfinite_pending_timestamp_keeps_legacy_reuse(hermes_home):
    """Non-finite pending timestamps stay tolerated: with no authoritative
    token and no comparable pending time, the identity-less core row keeps
    the legacy reuse behaviour instead of failing closed into a duplicate.
    """
    sid = "r12_core_repeat_nonfinite"
    _write_new_stream_journal(sid)

    synced = _core_synced_session(sid, drop_timestamp=True)
    synced.pending_started_at = None
    assert models._core_transcript_holds_pending_turn(
        synced, STREAM_ID, models._normalize_journal_recovery_text(PROMPT)
    ) is True, (
        "a non-finite pending timestamp must not turn the legacy core row "
        "into a conflict — the old row carries the only turn evidence"
    )

    session = _make_pending_session(sid, started_at=None)
    core_path = _write_core_transcript(hermes_home, sid)
    core = json.loads(core_path.read_text(encoding="utf-8"))
    core["messages"][0].pop("timestamp")
    core_path.write_text(json.dumps(core), encoding="utf-8")

    assert _apply_core_sync_or_error_marker(
        session, core_path, stream_id_for_recheck=STREAM_ID
    ) is True
    user_rows = [m for m in session.messages if m.get("role") == "user"]
    assert len(user_rows) == 1, (
        "the legacy identity-less shape keeps single-row reuse, got "
        f"{len(user_rows)} user rows"
    )
