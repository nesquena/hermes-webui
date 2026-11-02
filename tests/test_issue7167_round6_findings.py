"""Round-6 review findings on #7167 (nesquena-hermes 2026-10-13).

Two regressions in ``api/models.py``, both in the recovery/repair path:

F1 [CORE] — ``_promote_or_refresh_reused_marker`` hoists the journaled rows
above a REUSED interruption marker via ``_reorder_journal_tail_above_marker``.
That reorder moves EVERY ``_recovered_from_run_journal`` row after the marker,
including a *different* stream's answer, and only rebases the tool indexes of
the rows it moved. A later-arriving recovered answer therefore lands BEFORE
its own user turn, and an ordinary answer's tool card can end up pointing at a
USER row. Both reorder sites must use the stream-scoped
``_rehome_cancel_journal_rows`` instead, and an authorized legacy recovery row
must be stamped with its proven stream id before context projection.

F2 [SILENT] — the metadata backfill onto a reused user row was gated on
``_already_checkpointed OR _tail_user_already_checkpointed``. The tail
predicate is a pure TEXT match against the transcript's last message, so an
earlier, unrelated turn that happened to use the same prompt received the
pending turn's attachments/source/pending-turn token, while the genuinely
recovered row got them too — two user rows sharing one ``_active_turn_token``.
The backfill must run only when the token-bound ``_already_checkpointed``
proof holds.
"""

from __future__ import annotations

import pytest

import api.profiles as profiles
from api.models import Session, _apply_core_sync_or_error_marker
from api.run_journal import append_run_event


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolate HERMES_HOME so Session.save() + run-journal writes are sandboxed."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    return home


def _user(text, timestamp=None, **extra):
    row = {"role": "user", "content": text}
    if timestamp is not None:
        row["timestamp"] = timestamp
    row.update(extra)
    return row


# ── F2: the backfill must not stamp an earlier same-text prompt ──────────────


def test_backfill_never_stamps_an_earlier_same_text_prompt(hermes_home):
    """SILENT #7167 round-6: an earlier, unrelated prompt with the SAME text
    must not receive the pending turn's attachments/source/turn token.

    Repro input: transcript ``[user "run the check" ts=100, no metadata]``,
    pending ``"run the check"`` at 900.5 with ``['/tmp/upload.png']`` and
    ``cli``, plus one completed journal. The tail predicate is a pure text
    match, so the old gate treated the ts=100 row as the reused pending turn:
    it was stamped with the attachment/source/pending turn token, and the
    recovered ts=900.5 row received them too — two user rows sharing one
    ``_active_turn_token``.
    """
    sid = "regate_backfill_earlier_same_text"
    stream_id = "dead-stream-backfill-same-text"
    started_at = 900.5
    token = f"{stream_id}:{started_at:.17g}"
    append_run_event(sid, stream_id, "token", {"text": "recovered answer"})
    append_run_event(sid, stream_id, "done", {"terminal_state": "completed"})

    earlier = _user("run the check", timestamp=100)
    session = Session(
        session_id=sid,
        title="earlier same-text prompt",
        messages=[earlier],
        pending_user_message="run the check",
        pending_started_at=started_at,
        pending_attachments=["/tmp/upload.png"],
        pending_user_source="cli",
        active_stream_id=stream_id,
    )
    session.save()

    assert _apply_core_sync_or_error_marker(
        session,
        hermes_home / "sessions" / f"session_{sid}.json",
        stream_id_for_recheck=stream_id,
    ) is True

    user_rows = [m for m in session.messages if m.get("role") == "user"]
    assert len(user_rows) == 2, (
        "the earlier unrelated turn and the recovered pending turn are two "
        f"distinct rows, got {len(user_rows)}"
    )
    old_row, recovered_row = user_rows
    assert old_row is earlier or old_row.get("timestamp") == 100, (
        "the earlier turn must keep its place"
    )
    assert "attachments" not in old_row and "_source" not in old_row, (
        "an earlier UNRELATED prompt must never receive the pending turn's "
        f"metadata, got {old_row!r}"
    )
    assert not old_row.get("_active_turn_token"), (
        "the earlier prompt must not be stamped with the pending turn token"
    )
    assert recovered_row.get("attachments") == ["/tmp/upload.png"], (
        "the genuinely recovered row keeps the pending turn's attachments"
    )
    assert recovered_row.get("_source") == "cli", (
        "the genuinely recovered row keeps the pending turn's source"
    )
    # One token, one row: the duplicate-token shape is the SILENT bug.
    token_rows = [
        m for m in user_rows if m.get("_active_turn_token") == token
    ]
    assert len(token_rows) == 1, (
        "exactly one user row may carry the pending turn token, got "
        f"{len(token_rows)}"
    )
    assert token_rows[0].get("timestamp") == started_at, (
        "the token belongs to the recovered pending turn, not the old prompt"
    )


# ── F1: the marker reorder must not steal another stream's rows ──────────────


def _recovered_answer(text, stream_id, **extra):
    row = {
        "role": "assistant",
        "content": text,
        "_recovered_from_run_journal": True,
        "_recovered_stream_id": stream_id,
    }
    row.update(extra)
    return row


def test_marker_reorder_keeps_other_stream_rows_after_the_marker():
    """CORE #7167 round-6: reusing an older interruption marker must move only
    THIS stream's recovered rows above it.

    The stream-scoped rehome left the other stream's answer where it was,
    so a later-arriving recovered answer never lands before its own user turn.
    """
    from api.models import _promote_or_refresh_reused_marker

    stream_a = "stream-a"
    stream_b = "stream-b"
    marker = {
        "role": "assistant",
        "content": "reload to retry",
        "_pending_journal_recovery": True,
        "_journal_marker_stream_id": stream_a,
    }
    own_answer = _recovered_answer("own recovered answer", stream_a)
    own_user = _user("own prompt", timestamp=500)
    other_answer = _recovered_answer("other stream answer", stream_b)
    # Another stream's ordinary answer with a tool card anchored to it.
    plain_answer = {
        "role": "assistant",
        "content": "plain answer",
        "_active_turn_token": f"{stream_b}:400",
    }

    session = Session(
        session_id="marker_reorder_scope",
        title="repro",
        messages=[
            _user("first turn", timestamp=100),
            plain_answer,          # index 1 — its card anchors here
            own_user,              # index 2 — the cancelled turn's prompt
            marker,                # index 3 — the marker being promoted
            own_answer,            # index 4 — THIS stream's recovered row
            other_answer,          # index 5 — a DIFFERENT stream's row
        ],
    )
    session.tool_calls = [
        {"name": "terminal", "preview": "ls", "assistant_msg_idx": 1},
    ]

    _promote_or_refresh_reused_marker(
        session, 3, resolved=True, terminal_error=False, stream_id=stream_a,
    )

    messages = session.messages
    marker_idx = next(i for i, m in enumerate(messages) if m is marker)
    own_idx = next(i for i, m in enumerate(messages) if m is own_answer)
    other_idx = next(i for i, m in enumerate(messages) if m is other_answer)
    own_user_idx = next(i for i, m in enumerate(messages) if m is own_user)

    assert own_idx < marker_idx, (
        "this stream's recovered row must sit above the promoted marker"
    )
    assert own_user_idx < own_idx, (
        "the recovered answer must stay AFTER its own user turn, got user="
        f"{own_user_idx} answer={own_idx}"
    )
    assert other_idx > marker_idx, (
        "another stream's recovered row must not be moved by this marker, "
        f"got index {other_idx} for marker at {marker_idx}"
    )
    anchor = (session.tool_calls or [{}])[0].get("assistant_msg_idx")
    assert isinstance(anchor, int) and not isinstance(anchor, bool), (
        f"anchor must stay an int, got {anchor!r}"
    )
    assert 0 <= anchor < len(messages), f"anchor {anchor} out of range"
    anchored = messages[anchor]
    assert isinstance(anchored, dict) and anchored.get("role") == "assistant", (
        "an ordinary answer's tool card must not point at a USER row, got "
        f"role={anchored.get('role')!r} at index {anchor}"
    )
    assert anchored is plain_answer, (
        "the card must still anchor the SAME assistant row, not a neighbour"
    )


def test_legacy_recovered_row_gets_its_stream_identity_stamped(hermes_home):
    """CORE #7167 round-6: an authorized legacy recovery row (no
    ``_recovered_stream_id``) must be stamped with its proven stream id
    before context projection.

    Without the stamp the row is invisible to the stream-scoped rehome and
    to the cancel-journal ownership gates, so it either stays below the
    marker or is treated as another stream's row.
    """
    from api.models import _recover_journaled_output_and_terminal_error

    sid = "regate_legacy_stream_stamp"
    stream_id = "dead-stream-legacy-stamp"
    started_at = 1_700_005_000.5
    token = f"{stream_id}:{started_at:.17g}"
    append_run_event(sid, stream_id, "token", {"text": "legacy recovered"})

    session = Session(
        session_id=sid,
        title="legacy recovered row",
        messages=[
            # A legacy recovery artifact: journal provenance WITHOUT the
            # stream tag (written before stream tagging existed).
            {
                "role": "assistant",
                "content": "legacy recovered",
                "_recovered_from_run_journal": True,
                "_active_turn_token": token,
            },
        ],
        pending_user_message="crash-turn prompt",
        pending_started_at=started_at,
        pending_attachments=[],
        pending_user_source="webui",
        active_stream_id=stream_id,
        context_messages=[],
    )

    # The no-cancel-hook lazy retry authorizes an already-saved turn row
    # (authorize_saved_turn_rows=True) even though it carries no provenance.
    recovered_output, _terminal, output_accounted_for = (
        _recover_journaled_output_and_terminal_error(
            session,
            stream_id,
            dedupe_existing=True,
            authorize_saved_turn_rows=True,
        )
    )

    assert recovered_output is False, (
        "the legacy row must be REUSED, not appended a second time"
    )
    assert output_accounted_for is True
    rows = [
        m for m in session.messages
        if m.get("_recovered_from_run_journal") is True
    ]
    assert len(rows) == 1, (
        f"exactly one recovered row must remain, got {len(rows)}"
    )
    assert str(rows[0].get("_recovered_stream_id") or "") == str(stream_id), (
        "the authorized legacy recovery row must be stamped with its proven "
        f"stream id, got {rows[0].get('_recovered_stream_id')!r}"
    )
