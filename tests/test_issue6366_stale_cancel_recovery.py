"""Tests for #6366: stale-cancel recovery must not append duplicate
rows when the durable transcript has already advanced past the
pending turn — and must never silently discard a distinct repeated
prompt or an interrupted tool-running turn.

Bug shape: the recovery path at ``api.models.py::_apply_core_sync_or_error_marker``
checks only ``session.messages[-1]`` against the pending user
checkpoint. When a stale pending turn's user row is older than the
transcript tail, the tail is a *newer* settled assistant row that
can never match the pending **user** checkpoint, and the recovery
path falls through into the append branch. It then appends a
recovered user row, a ``_partial`` clone of the old journal, and a
generic ``No response from provider`` error — all after the valid
final answer. The duplicates survive reload.

Fix: add a transcript-advance detector that scans the durable
messages for the pending turn's own user row at any index and, when
found, checks for a genuine final assistant answer inside that turn's
boundary. If one exists, the recovery path must clear the stale
pending fields without appending any rows. The helper is a pure read
so recovery is idempotent: running it twice produces the same
outcome.

Two guard rails keep the suppression from destroying data:

* **Identity** is bound to the pending stream's exact active-turn
  token, never to integer-second timestamp equality. Two different
  streams that send the same prompt within one second truncate to the
  same second, so the checkpoint matcher alone would match the other
  stream's row and the branch would then clear the only durable copy
  of the prompt. A token mismatch — or a token-less legacy row —
  always loses, so an unprovable identity recovers instead of
  suppressing.

* **Completion** reuses the established final-answer semantics
  (``_assistant_message_has_final_visible_text``) rather than "an
  assistant row exists". Empty, tool-call-only, interim ``_partial``
  and compaction tails all keep recovery active, so a turn that was
  interrupted mid-tool-execution is replayed and marked instead of
  being treated as already advanced.

Because a false negative leaves a visible cosmetic duplicate while a
false positive silently destroys a prompt, the suppression defaults to
recovering whenever identity or completion cannot be proven. The
full-recovery cases below exercise ``_apply_core_sync_or_error_marker``
itself, because neither guard rail is reachable through the predicate
in isolation — the suppression decision only takes effect on the real
recovery path.
"""

import json

import pytest

import api.models as models
from api.models import (
    SESSIONS,
    Session,
    _apply_core_sync_or_error_marker,
    _pending_turn_has_final_assistant_answer,
    _run_journal_terminal_state,
    _transcript_already_advanced_past_pending,
    _transcript_user_row_is_pending_turn,
    _turn_journal_records_completion,
)
from api.run_journal import append_run_event
from api.turn_journal import append_turn_journal_event_for_stream


@pytest.fixture(autouse=True)
def _isolate_session_state(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    SESSIONS.clear()
    yield
    SESSIONS.clear()


class _FakeSession:
    """Minimal stand-in for api.models.Session — only the fields the
    new helpers read."""

    def __init__(
        self,
        pending=None,
        messages=None,
        *,
        stream_id=None,
        started_at=None,
        source=None,
        attachments=None,
        session_id="fake-session-6366",
    ):
        self.session_id = session_id
        self.pending_user_message = pending
        self.messages = list(messages or [])
        self.active_stream_id = stream_id
        self.pending_started_at = started_at
        self.pending_user_source = source
        self.pending_attachments = attachments


def _token(stream_id, started_at):
    return f"{stream_id}:{float(started_at):.17g}"


# ── _transcript_user_row_is_pending_turn identity cases ─────────────────


def test_user_row_matches_only_with_identical_active_turn_token():
    """Identity is unforgeable: the row's ``_active_turn_token`` must equal
    the token derived from the pending stream id + started_at."""
    s = _FakeSession(
        pending="hello world",
        stream_id="stream-a",
        started_at=222.5,
    )
    own_row = {
        "role": "user",
        "content": "hello world",
        "timestamp": 222,
        "_active_turn_token": "stream-a:222.5",
    }
    other_row = {
        "role": "user",
        "content": "hello world",
        "timestamp": 222,
        "_active_turn_token": "stream-b:222.5",
    }
    assert _transcript_user_row_is_pending_turn(own_row, s, _token("stream-a", 222.5)) is True
    assert _transcript_user_row_is_pending_turn(other_row, s, _token("stream-a", 222.5)) is False


def test_user_row_without_token_never_matches_a_token_bearing_pending_turn():
    """A legacy row that predates token stamping cannot be proven to be the
    pending turn, so the caller must recover rather than suppress."""
    s = _FakeSession(
        pending="hello world",
        stream_id="stream-a",
        started_at=222.5,
    )
    legacy_row = {
        "role": "user",
        "content": "hello world",
        "timestamp": 222,
    }
    assert _transcript_user_row_is_pending_turn(legacy_row, s, _token("stream-a", 222.5)) is False


def test_token_bearing_row_never_matches_a_token_less_pending_turn():
    """The mirror case: a token-bearing row belongs to a real turn, so it
    must not be claimed by a pending turn that has no resolvable token."""
    s = _FakeSession(
        pending="hello world",
        stream_id=None,
        started_at=None,
    )
    token_row = {
        "role": "user",
        "content": "hello world",
        "timestamp": 222,
        "_active_turn_token": "stream-a:222.5",
    }
    assert _transcript_user_row_is_pending_turn(token_row, s, None) is False


def test_legacy_pending_turn_falls_back_to_strict_checkpoint_match():
    """With no token on either side the strict content + timestamp + source
    + attachments checkpoint is the only identity signal, and it still
    distinguishes a same-second repeated prompt from the pending turn."""
    s = _FakeSession(
        pending="hello world",
        stream_id=None,
        started_at=222,
        source="webui",
        attachments=[{"name": "a.png"}],
    )
    same_turn_row = {
        "role": "user",
        "content": "hello world",
        "timestamp": 222,
        "_source": "webui",
        "attachments": [{"name": "a.png"}],
    }
    other_turn_row = {
        "role": "user",
        "content": "hello world",
        "timestamp": 222,
        "_source": "webui",
        "attachments": [{"name": "old.png"}],
    }
    assert _transcript_user_row_is_pending_turn(same_turn_row, s, None) is True
    assert _transcript_user_row_is_pending_turn(other_turn_row, s, None) is False


# ── _pending_turn_has_final_assistant_answer completion cases ───────────


def test_final_answer_requires_visible_answer_text():
    assert _pending_turn_has_final_assistant_answer(
        [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "Here is the final answer."},
        ],
        0,
    ) is True


def test_final_answer_rejects_empty_assistant_row():
    assert _pending_turn_has_final_assistant_answer(
        [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": ""},
        ],
        0,
    ) is False


def test_final_answer_rejects_tool_call_only_row():
    assert _pending_turn_has_final_assistant_answer(
        [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "I found a source and will inspect it.",
                "tool_calls": [{"id": "call_1"}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ],
        0,
    ) is False


def test_final_answer_rejects_interim_partial_row():
    assert _pending_turn_has_final_assistant_answer(
        [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "Still working on it…",
                "_partial": True,
            },
        ],
        0,
    ) is False


def test_final_answer_rejects_compaction_marker_row():
    assert _pending_turn_has_final_assistant_answer(
        [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
            },
        ],
        0,
    ) is False


def test_final_answer_accepts_answer_after_compaction_marker():
    assert _pending_turn_has_final_assistant_answer(
        [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
            },
            {"role": "assistant", "content": "Here is the final answer."},
        ],
        0,
    ) is True


def test_final_answer_does_not_leak_across_a_later_user_turn():
    """A final answer belonging to a *later* turn must not settle this one."""
    assert _pending_turn_has_final_assistant_answer(
        [
            {"role": "user", "content": "q"},
            {"role": "user", "content": "later question"},
            {"role": "assistant", "content": "Here is the final answer."},
        ],
        0,
    ) is False


def test_final_answer_tolerates_tool_rows_before_the_answer():
    assert _pending_turn_has_final_assistant_answer(
        [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "assistant", "content": "Here is the final answer."},
        ],
        0,
    ) is True


# ── _transcript_already_advanced_past_pending unit cases ───────────────


def test_advances_past_pending_when_user_matched_and_settled_assistant_follows():
    """Canonical bug shape: a stale pending turn's user row sits at
    an older index, and a newer settled assistant row follows.
    Recovery must not append duplicate rows after the assistant.
    Identity comes from the pending stream's own active-turn token;
    the journal is marked ``done`` (terminal_state ``completed``) so
    the suppression gate is satisfied."""
    s = _FakeSession(
        pending="hello world",
        messages=[
            {
                "role": "user",
                "content": "hello world",
                "timestamp": 222,
                "_source": "webui",
                "attachments": [{"name": "a.png"}],
                "_active_turn_token": "stream-a:222.5",
            },
            {"role": "assistant", "content": "answer", "timestamp": 223},
        ],
        stream_id="stream-a",
        started_at=222.5,
        source="webui",
        attachments=[{"name": "a.png"}],
    )
    _write_journal(s.session_id, "stream-a", [("done", {})])
    _write_turn_journal(
        s.session_id,
        "stream-a",
        [("submitted", {}), ("completed", {})],
    )
    assert _transcript_already_advanced_past_pending(s) is True


def test_advances_past_pending_ignores_partial_clone_after_user():
    """A ``_partial`` clone after the pending turn's user row does NOT count
    as a settled advance — the canonical bug shape is the partial
    clone followed by a generic no-response error, both of which
    the previous recovery path appended. Only a genuine final visible
    answer represents a real advance."""
    s = _FakeSession(
        pending="hello world",
        messages=[
            {
                "role": "user",
                "content": "hello world",
                "timestamp": 1,
                "_active_turn_token": "stream-a:1.5",
            },
            {"role": "assistant", "content": "answer", "timestamp": 2, "_partial": True},
            {"role": "assistant", "content": "no response", "timestamp": 3, "_error": True},
        ],
        stream_id="stream-a",
        started_at=1.5,
    )
    assert _transcript_already_advanced_past_pending(s) is False


def test_advances_past_pending_ignores_recovered_assistant_row():
    """A previously-recovered assistant row is not a settled answer
    either; the recovery path may have produced it on an earlier
    pass. Skip it and keep looking forward."""
    s = _FakeSession(
        pending="hello world",
        messages=[
            {
                "role": "user",
                "content": "hello world",
                "timestamp": 1,
                "_active_turn_token": "stream-a:1.5",
            },
            {"role": "assistant", "content": "answer", "timestamp": 2, "_recovered": True},
        ],
        stream_id="stream-a",
        started_at=1.5,
    )
    assert _transcript_already_advanced_past_pending(s) is False


def test_does_not_advance_when_only_user_row_matches():
    """The pending turn's user row exists but no final assistant answer has
    been settled. Recovery is the legitimate path; the helper
    returns False."""
    s = _FakeSession(
        pending="hello world",
        messages=[
            {
                "role": "user",
                "content": "hello world",
                "timestamp": 1,
                "_active_turn_token": "stream-a:1.5",
            },
        ],
        stream_id="stream-a",
        started_at=1.5,
    )
    assert _transcript_already_advanced_past_pending(s) is False


def test_does_not_advance_when_no_user_row_matches():
    """No transcript row carries the pending turn's token. The bug shape
    here is the older non-matching rows + a stale pending. The
    recovery path is legitimate; the helper returns False."""
    s = _FakeSession(
        pending="hello world",
        messages=[
            {
                "role": "user",
                "content": "hello world",
                "timestamp": 1,
                "_active_turn_token": "stream-z:1.5",
            },
            {"role": "assistant", "content": "answer", "timestamp": 2},
        ],
        stream_id="stream-a",
        started_at=1.5,
    )
    assert _transcript_already_advanced_past_pending(s) is False


def test_does_not_advance_when_no_pending_text():
    """An empty pending text means there is nothing to compare
    against. The helper returns False without scanning the
    transcript."""
    s = _FakeSession(
        pending=None,
        messages=[
            {"role": "assistant", "content": "answer", "timestamp": 2},
        ],
    )
    assert _transcript_already_advanced_past_pending(s) is False


def test_does_not_advance_when_no_messages():
    """An empty transcript is the cold-start case; recovery is the
    legitimate path. The helper returns False."""
    s = _FakeSession(pending="hello world", messages=[])
    assert _transcript_already_advanced_past_pending(s) is False


def test_advances_past_pending_with_unrelated_intervening_rows():
    """The pending turn's user row followed by a tool row and THEN a final
    answer counts as a real advance. The helper scans past non-assistant
    roles correctly, and the journal is marked ``done`` so the durable
    terminal evidence gate is satisfied."""
    s = _FakeSession(
        pending="hello world",
        messages=[
            {
                "role": "user",
                "content": "hello world",
                "timestamp": 222,
                "_source": "webui",
                "attachments": [],
                "_active_turn_token": "stream-a:222.5",
            },
            {"role": "tool", "content": "tool output"},
            {"role": "assistant", "content": "answer", "timestamp": 223},
        ],
        stream_id="stream-a",
        started_at=222.5,
        source="webui",
        attachments=[],
    )
    _write_journal(s.session_id, "stream-a", [("done", {})])
    _write_turn_journal(
        s.session_id,
        "stream-a",
        [("submitted", {}), ("completed", {})],
    )
    assert _transcript_already_advanced_past_pending(s) is True


def test_helper_is_pure_read_idempotent():
    """Idempotence: running the helper twice on the same session
    state returns the same result. The helper does not mutate the
    session, so recovery is automatically safe to run twice. Journal
    is marked ``done`` so the durable terminal evidence gate passes
    on both invocations."""
    s = _FakeSession(
        pending="hello world",
        messages=[
            {
                "role": "user",
                "content": "hello world",
                "timestamp": 222,
                "_source": "webui",
                "attachments": [],
                "_active_turn_token": "stream-a:222.5",
            },
            {"role": "assistant", "content": "answer", "timestamp": 223},
        ],
        stream_id="stream-a",
        started_at=222.5,
        source="webui",
        attachments=[],
    )
    _write_journal(s.session_id, "stream-a", [("done", {})])
    _write_turn_journal(
        s.session_id,
        "stream-a",
        [("submitted", {}), ("completed", {})],
    )
    first = _transcript_already_advanced_past_pending(s)
    second = _transcript_already_advanced_past_pending(s)
    assert first is True
    assert second is True
    # And the session state was not mutated.
    assert s.pending_user_message == "hello world"
    assert len(s.messages) == 2


def test_does_not_advance_when_repeated_prompt_has_different_token():
    """#6366 regression guard for repeated-prompt identity: a *new*
    user turn that happens to repeat the same prompt text as an
    older transcript row is NOT a stale advance. The active-turn
    token tells a stale duplicate from a legitimate re-send, even
    when both truncate to the same integer second."""
    s = _FakeSession(
        pending="repeat this",
        messages=[
            # Older user row from a DIFFERENT stream, same text and the
            # same integer second.
            {
                "role": "user",
                "content": "repeat this",
                "timestamp": 111,
                "_source": "webui",
                "attachments": [{"name": "old.png"}],
                "_active_turn_token": "stream-old:111.5",
            },
            {"role": "assistant", "content": "Earlier answer"},
            # The current turn's own row — pending, no answer yet.
            {
                "role": "user",
                "content": "repeat this",
                "timestamp": 111,
                "_source": "webui",
                "attachments": [{"name": "current.png"}],
                "_active_turn_token": "stream-a:111.5",
            },
        ],
        stream_id="stream-a",
        started_at=111.5,
        source="webui",
        attachments=[{"name": "current.png"}],
    )
    assert _transcript_already_advanced_past_pending(s) is False


def test_advances_past_pending_with_exact_token_match():
    """The strict-match path: a transcript user row that carries the
    pending stream's own active-turn token, followed by a genuine final
    answer, is the canonical #6366 bug shape and must suppress the
    recovery append."""
    s = _FakeSession(
        pending="hello world",
        messages=[
            {
                "role": "user",
                "content": "hello world",
                "timestamp": 222,
                "_source": "webui",
                "attachments": [{"name": "a.png"}],
                "_active_turn_token": "stream-a:222.5",
            },
            {"role": "assistant", "content": "answer", "timestamp": 223},
        ],
        stream_id="stream-a",
        started_at=222.5,
        source="webui",
        attachments=[{"name": "a.png"}],
    )
    _write_journal(s.session_id, "stream-a", [("done", {})])
    _write_turn_journal(
        s.session_id,
        "stream-a",
        [("submitted", {}), ("completed", {})],
    )
    assert _transcript_already_advanced_past_pending(s) is True


def test_does_not_advance_when_final_answer_belongs_to_a_later_turn():
    """A final answer that sits after a newer user row belongs to that
    newer turn, so it cannot prove the pending turn advanced."""
    s = _FakeSession(
        pending="hello world",
        messages=[
            {
                "role": "user",
                "content": "hello world",
                "timestamp": 222,
                "_active_turn_token": "stream-a:222.5",
            },
            {"role": "user", "content": "a different question", "timestamp": 224},
            {"role": "assistant", "content": "answer", "timestamp": 225},
        ],
        stream_id="stream-a",
        started_at=222.5,
    )
    assert _transcript_already_advanced_past_pending(s) is False


def test_does_not_advance_when_tool_execution_was_interrupted():
    """A turn interrupted mid-tool-execution ends with a tool-call-only
    assistant row and a tool result — not a final answer. Recovery must
    still replay the journal and mark the interruption."""
    s = _FakeSession(
        pending="run the migration",
        messages=[
            {
                "role": "user",
                "content": "run the migration",
                "timestamp": 222,
                "_active_turn_token": "stream-a:222.5",
            },
            {
                "role": "assistant",
                "content": "I will run the migration now.",
                "tool_calls": [{"id": "call_1"}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "partial output"},
        ],
        stream_id="stream-a",
        started_at=222.5,
    )
    assert _transcript_already_advanced_past_pending(s) is False


def test_does_not_advance_when_unflagged_interim_prose_precedes_tools():
    """9/22 re-gate regression: an unflagged interim prose row
    ("Let me check the logs first.") followed by tool rows passes the
    transcript predicate (``_pending_turn_has_final_assistant_answer``
    returns True at the first visible assistant row), but the run
    journal was never marked ``done`` — the stream may have died mid-
    tool. Without durable same-stream terminal evidence the suppression
    gate must stay closed, so ``_transcript_already_advanced_past_pending``
    returns False and the recovery path is free to run."""
    s = _FakeSession(
        pending="check the failing test",
        messages=[
            {
                "role": "user",
                "content": "check the failing test",
                "timestamp": 222,
                "_active_turn_token": "stream-a:222.5",
            },
            # The bug shape: an unflagged assistant row with visible
            # text and no tool_calls. The transcript heuristic accepts
            # this as a final answer (it carries visible text and has
            # no _partial / _error / _recovered marker), but the row
            # is actually pre-final interim prose that the repository
            # journals as ``interim_assistant`` while the model
            # decides what tool to call. The tool_calls that
            # produced the following tool rows live on a *later* row
            # in the original stream — the journal has not yet
            # recorded them because the WebUI process died.
            {
                "role": "assistant",
                "content": "Let me check the logs first.",
            },
            # The stream then ran tools before dying. The transcript
            # therefore has tool rows after the unflagged prose.
            {"role": "tool", "tool_call_id": "call_1", "content": "log tail"},
            {"role": "tool", "tool_call_id": "call_2", "content": "log grep"},
        ],
        stream_id="stream-a",
        started_at=222.5,
    )
    # The journal has tokens and an interim_assistant checkpoint, but
    # no ``done`` / ``stream_end`` — the run is still mid-tool from
    # the journal's point of view.
    _write_journal(
        s.session_id, "stream-a",
        [
            ("token", {"text": "Let me check the logs first."}),
            ("interim_assistant", {"text": "Let me check the logs first."}),
            ("tool", {"name": "terminal", "tid": "call_1", "preview": "tail -n 50"}),
            ("tool_complete", {"name": "terminal", "tid": "call_1", "preview": "log tail"}),
            ("tool", {"name": "terminal", "tid": "call_2", "preview": "grep ERROR"}),
        ],
    )
    assert _transcript_already_advanced_past_pending(s) is False


def test_does_advance_when_journal_marked_done_after_interim_prose():
    """Companion positive control: when the run journal IS marked
    ``done`` (terminal_state ``completed``) for the same stream, the
    suppression gate opens even though the transcript carries the
    unflagged interim prose + tool shape. Without the journal evidence
    the predicate would still hold the gate closed, so this case pins
    the boundary between the two new branches."""
    s = _FakeSession(
        pending="check the failing test",
        messages=[
            {
                "role": "user",
                "content": "check the failing test",
                "timestamp": 222,
                "_active_turn_token": "stream-a:222.5",
            },
            {
                "role": "assistant",
                "content": "Let me check the logs first.",
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "log tail"},
            {"role": "assistant", "content": "The logs show a stale lock.", "timestamp": 223},
        ],
        stream_id="stream-a",
        started_at=222.5,
    )
    _write_journal(s.session_id, "stream-a", [("done", {})])
    _write_turn_journal(
        s.session_id,
        "stream-a",
        [("submitted", {}), ("completed", {})],
    )
    assert _transcript_already_advanced_past_pending(s) is True


# ── turn-journal completion evidence (9/23 re-gate) ───────────────────
#
# The suppression predicate used to require a *run-journal* terminal
# ``completed`` state. Its caller in ``_apply_core_sync_or_error_marker``
# already returns early on that same condition, so by the time the
# predicate ran the run-journal branch could never be true and the
# predicate could never suppress anything. The completion evidence
# therefore has to come from the turn journal — the crash-safe journal
# that records the exact-stream ``completed`` event for the pending turn.


_TURN_JOURNAL_CLOCK = {"tick": 0}


def _write_turn_journal(session_id, stream_id, events):
    """Append events to the turn journal for ``stream_id``, reusing the
    stream's turn id exactly like ``append_turn_journal_event_for_stream``
    does in production.

    ``created_at`` is taken from a module-level monotonic clock so events
    appended across several calls — and across turn ids — keep a strict
    ordering; a per-call sequence would hand the same timestamp to events
    of different turns, leaving the derived "latest turn per stream"
    ambiguous.
    """
    written = []
    for event_name, payload in events:
        _TURN_JOURNAL_CLOCK["tick"] += 1
        written.append(
            append_turn_journal_event_for_stream(
                session_id,
                stream_id,
                {
                    "event": event_name,
                    "created_at": 1_700_000_000.0 + _TURN_JOURNAL_CLOCK["tick"],
                    **payload,
                },
            )
        )
    return written


def test_turn_journal_helper_reports_completed_for_matching_stream():
    """The helper reads the turn journal and reports completion only for
    the stream whose ``completed`` event was recorded."""
    session_id = "issue6366_turn_journal_helper"
    _write_turn_journal(
        session_id,
        "stream-a",
        [("submitted", {}), ("completed", {})],
    )
    probe = _FakeSession(
        pending="hello world",
        messages=[],
        stream_id="stream-a",
        started_at=222.5,
        session_id=session_id,
    )
    assert _turn_journal_records_completion(probe, "stream-a") is True
    assert _turn_journal_records_completion(probe, "stream-b") is False


def test_turn_journal_helper_rejects_interrupted_turn():
    """An ``interrupted`` turn is not a completed turn, even when the
    journal also carries an earlier ``completed`` event for another
    turn of the same stream."""
    session_id = "issue6366_turn_journal_interrupted"
    _write_turn_journal(
        session_id,
        "stream-a",
        [("submitted", {}), ("completed", {})],
    )
    _write_turn_journal(
        session_id,
        "stream-a",
        [("submitted", {}), ("interrupted", {"reason": "cancelled"})],
    )
    probe = _FakeSession(
        pending="hello world",
        messages=[],
        stream_id="stream-a",
        started_at=222.5,
        session_id=session_id,
    )
    assert _turn_journal_records_completion(probe, "stream-a") is False


def test_advances_past_pending_from_turn_journal_without_run_journal_terminal():
    """9/23 re-gate root case: the run journal never wrote a terminal
    event (the stream died before ``done``/``stream_end`` reached it),
    but the turn journal recorded the exact-stream ``completed`` event.

    The transcript alone proves the turn produced a genuine final
    answer, and the turn journal proves the turn reached its terminal
    state, so the recovery path must suppress the duplicate recovered
    user row + journal clone + interruption marker.
    """
    session_id = "issue6366_turn_journal_completed"
    s = _FakeSession(
        pending="hello world",
        messages=[
            {
                "role": "user",
                "content": "hello world",
                "timestamp": 222,
                "_source": "webui",
                "attachments": [{"name": "a.png"}],
                "_active_turn_token": "stream-a:222.5",
            },
            {"role": "assistant", "content": "answer", "timestamp": 223},
        ],
        stream_id="stream-a",
        started_at=222.5,
        source="webui",
        attachments=[{"name": "a.png"}],
        session_id=session_id,
    )
    # The run journal holds visible progress but NO terminal event.
    _write_journal(
        session_id,
        "stream-a",
        [
            ("token", {"text": "answer"}),
        ],
    )
    # The turn journal has the exact-stream completion event.
    _write_turn_journal(
        session_id,
        "stream-a",
        [("submitted", {}), ("completed", {})],
    )
    assert _run_journal_terminal_state(s, "stream-a") != "completed"
    assert _turn_journal_records_completion(s, "stream-a") is True
    assert _transcript_already_advanced_past_pending(s) is True


def test_full_recovery_suppresses_duplicates_on_turn_journal_completion(tmp_path):
    """Full-path 9/23 re-gate regression: the run journal never wrote a
    terminal state, but the turn journal recorded the exact-stream
    ``completed`` event. Recovery must clear the stale pending state
    without appending the recovered user row, the journal clone, or the
    interruption marker after the valid final answer."""
    session_id = "issue6366_full_recovery_turn_journal_completed"
    stream_id = "stream-turn-journal-completed"
    started_at = 1_700_002_100.5

    session = Session(
        session_id=session_id,
        title="Completed per turn journal, silent run journal",
        messages=[
            {
                "role": "user",
                "content": "summarise the diff",
                "timestamp": int(started_at),
                "_source": "webui",
                "attachments": [],
                "_active_turn_token": f"{stream_id}:{started_at:.17g}",
            },
            {"role": "assistant", "content": "The diff adds a guard."},
        ],
        context_messages=[
            {"role": "user", "content": "summarise the diff"},
            {"role": "assistant", "content": "The diff adds a guard."},
        ],
        pending_user_message="summarise the diff",
        pending_started_at=started_at,
        pending_user_source="webui",
        pending_attachments=[],
        active_stream_id=stream_id,
    )
    # The run journal has visible progress but no terminal event, so the
    # line-3671 early return cannot fire and the suppression predicate
    # is now the only gate that can keep the transcript clean.
    _write_journal(
        session_id,
        stream_id,
        [("token", {"text": "The diff adds a guard."})],
    )
    # The turn journal recorded the exact-stream terminal completion.
    _write_turn_journal(
        session_id,
        stream_id,
        [("submitted", {}), ("completed", {})],
    )
    before = json.dumps(session.messages, ensure_ascii=False)

    assert _apply_core_sync_or_error_marker(
        session,
        tmp_path / "missing-core-transcript.json",
        stream_id_for_recheck=stream_id,
    ) is True

    # Nothing was appended after the valid final answer.
    assert json.dumps(session.messages, ensure_ascii=False) == before
    assert not any(
        message.get("_recovered") or message.get("_recovered_from_run_journal")
        for message in session.messages
    )
    assert not any(message.get("_error") for message in session.messages)
    # The stale pending state was cleared.
    assert session.pending_user_message is None
    assert session.active_stream_id is None
    assert session.pending_started_at is None
    assert session.pending_user_source is None
    assert session.pending_attachments == []


def test_full_recovery_appends_rows_on_turn_journal_completion_loss(tmp_path):
    """Mirror control: when neither the run journal nor the turn journal
    has terminal completion evidence, the same transcript shape must
    still recover — the suppression must not widen to cover a turn whose
    completion can no longer be proven."""
    session_id = "issue6366_full_recovery_no_turn_journal_completion"
    stream_id = "stream-no-turn-journal-completion"
    started_at = 1_700_002_200.5

    session = Session(
        session_id=session_id,
        title="Silent run journal, no turn journal completion",
        messages=[
            {
                "role": "user",
                "content": "summarise the diff",
                "timestamp": int(started_at),
                "_source": "webui",
                "attachments": [],
                "_active_turn_token": f"{stream_id}:{started_at:.17g}",
            },
            {"role": "assistant", "content": "The diff adds a guard."},
        ],
        context_messages=[
            {"role": "user", "content": "summarise the diff"},
        ],
        pending_user_message="summarise the diff",
        pending_started_at=started_at,
        pending_user_source="webui",
        pending_attachments=[],
        active_stream_id=stream_id,
    )
    # Neither journal carries terminal completion evidence.
    _write_journal(
        session_id,
        stream_id,
        [("token", {"text": "The diff adds a guard."})],
    )
    before = json.dumps(session.messages, ensure_ascii=False)

    assert _apply_core_sync_or_error_marker(
        session,
        tmp_path / "missing-core-transcript.json",
        stream_id_for_recheck=stream_id,
    ) is True

    # Recovery ran: the journal replay is visible again and the turn is
    # marked interrupted instead of being silently declared finished.
    assert json.dumps(session.messages, ensure_ascii=False) != before
    assert any(
        message.get("_recovered_from_run_journal")
        and "The diff adds a guard." in str(message.get("content", ""))
        for message in session.messages
    )
    assert any(
        message.get("_error") and message.get("type") == "interrupted"
        for message in session.messages
    )


# ── full _apply_core_sync_or_error_marker recovery cases ────────────────
#
# Neither guard rail above is reachable through the predicate in
# isolation: the suppression decision only takes effect where
# ``_apply_core_sync_or_error_marker`` calls it. These probes drive the
# real repair path end to end.


def _write_journal(session_id, stream_id, events):
    for event_name, payload in events:
        append_run_event(session_id, stream_id, event_name, payload)


def test_full_recovery_keeps_distinct_same_second_repeated_prompt(tmp_path):
    """Two distinct streams send the same prompt inside one integer second.

    The earlier stream's turn is fully settled in the durable transcript.
    The later stream is the pending one and was cancelled before it ever
    checkpointed a user row. The repair must recover the pending prompt
    instead of matching it against the earlier stream's row — which the
    integer-second-truncating checkpoint matcher happily accepts — and
    silently clearing the only durable copy of it.
    """
    session_id = "issue6366_same_second_repeat"
    stale_stream = "stream-stale"
    pending_stream = "stream-pending"
    # Both turns started inside the same integer second.
    shared_second = 1_700_000_000

    session = Session(
        session_id=session_id,
        title="Same-second repeated prompt",
        messages=[
            {
                "role": "user",
                "content": "run the migration",
                "timestamp": shared_second + 0.1,
                "_active_turn_token": f"{stale_stream}:{shared_second + 0.1:.17g}",
            },
            {"role": "assistant", "content": "Migration finished."},
        ],
        context_messages=[
            {"role": "user", "content": "run the migration"},
            {"role": "assistant", "content": "Migration finished."},
        ],
        pending_user_message="run the migration",
        pending_started_at=shared_second + 0.6,
        active_stream_id=pending_stream,
    )
    _write_journal(
        session_id,
        pending_stream,
        [("token", {"text": "Checking the migration status."})],
    )

    assert _apply_core_sync_or_error_marker(
        session,
        tmp_path / "missing-core-transcript.json",
        stream_id_for_recheck=pending_stream,
    ) is True

    # The pending prompt survives — recovery materialized it as a recovered
    # user row instead of dropping it against the other stream's checkpoint.
    recovered_users = [
        message
        for message in session.messages
        if message.get("_recovered") and message.get("content") == "run the migration"
    ]
    assert len(recovered_users) == 1
    # The journal's visible partial work was replayed, not suppressed.
    assert any(
        message.get("_recovered_from_run_journal")
        and message.get("content") == "Checking the migration status."
        for message in session.messages
    )
    # And the interruption is marked so the user can see what happened.
    assert any(
        message.get("_error") and message.get("type") == "interrupted"
        for message in session.messages
    )
    # The earlier settled turn was left untouched.
    assert session.messages[1]["content"] == "Migration finished."


def test_full_recovery_keeps_legacy_same_second_repeated_prompt(tmp_path):
    """Legacy transcript with no tokens on either side: two identical
    prompts inside one second must still be told apart, otherwise the
    second prompt is matched against the first's checkpoint and dropped."""
    session_id = "issue6366_legacy_same_second_repeat"
    stream_id = "stream-legacy-pending"
    shared_second = 1_700_000_100

    session = Session(
        session_id=session_id,
        title="Legacy same-second repeated prompt",
        messages=[
            # The other stream's row: same text, same integer second, and
            # distinguishable only by its different attachments.
            {
                "role": "user",
                "content": "retry the upload",
                "timestamp": shared_second,
                "_source": "webui",
                "attachments": [{"name": "first.png"}],
            },
            {"role": "assistant", "content": "Upload done."},
        ],
        context_messages=[
            {"role": "user", "content": "retry the upload"},
            {"role": "assistant", "content": "Upload done."},
        ],
        pending_user_message="retry the upload",
        pending_started_at=shared_second,
        pending_user_source="webui",
        pending_attachments=[{"name": "second.png"}],
        active_stream_id=stream_id,
    )
    _write_journal(session_id, stream_id, [("token", {"text": "Re-running upload."})])

    assert _apply_core_sync_or_error_marker(
        session,
        tmp_path / "missing-core-transcript.json",
        stream_id_for_recheck=stream_id,
    ) is True

    recovered_users = [
        message
        for message in session.messages
        if message.get("_recovered") and message.get("content") == "retry the upload"
    ]
    assert len(recovered_users) == 1
    assert recovered_users[0].get("attachments") == [{"name": "second.png"}]
    assert any(
        message.get("_recovered_from_run_journal")
        and message.get("content") == "Re-running upload."
        for message in session.messages
    )


@pytest.mark.parametrize(
    "tail_rows",
    [
        pytest.param(
            [{"role": "assistant", "content": ""}],
            id="empty-assistant-tail",
        ),
        pytest.param(
            [
                {
                    "role": "assistant",
                    "content": "I will inspect the file now.",
                    "tool_calls": [{"id": "call_1"}],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "partial output"},
            ],
            id="tool-call-only-tail",
        ),
        pytest.param(
            [
                {
                    "role": "assistant",
                    "content": "Still working on the first step…",
                    "_partial": True,
                },
            ],
            id="interim-progress-tail",
        ),
        pytest.param(
            [
                {
                    "role": "assistant",
                    "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
                },
            ],
            id="compaction-tail",
        ),
    ],
)
def test_full_recovery_runs_journal_replay_for_non_final_tails(tmp_path, tail_rows):
    """An interrupted turn whose tail is not a genuine final answer must NOT
    be treated as completed: journal replay has to run and the turn has to
    be marked as interrupted.
    """
    session_id = f"issue6366_tail_{tail_rows[-1].get('_partial', 'x')}"
    session_id = "issue6366_non_final_tail"
    stream_id = "stream-interrupted-tools"
    started_at = 1_700_000_500.5

    session = Session(
        session_id=session_id,
        title="Interrupted tool-running turn",
        messages=[
            {
                "role": "user",
                "content": "inspect the failing test",
                "timestamp": int(started_at),
                "_active_turn_token": f"{stream_id}:{started_at:.17g}",
            },
            *tail_rows,
        ],
        context_messages=[
            {"role": "user", "content": "inspect the failing test"},
        ],
        pending_user_message="inspect the failing test",
        pending_started_at=started_at,
        active_stream_id=stream_id,
    )
    _write_journal(
        session_id,
        stream_id,
        [
            ("reasoning", {"text": "Reading the assertion first."}),
            ("token", {"text": "The assertion never ran."}),
            (
                "tool",
                {
                    "name": "terminal",
                    "preview": "pytest tests/test_x.py",
                    "args": {"command": "pytest tests/test_x.py"},
                },
            ),
        ],
    )
    before = json.dumps(session.messages, ensure_ascii=False)

    assert _apply_core_sync_or_error_marker(
        session,
        tmp_path / "missing-core-transcript.json",
        stream_id_for_recheck=stream_id,
    ) is True

    # Journal replay ran: the visible partial output is back in the transcript.
    assert any(
        message.get("_recovered_from_run_journal")
        and message.get("content") == "The assertion never ran."
        for message in session.messages
    )
    # The interrupted turn is marked, so the user sees what happened.
    assert any(
        message.get("_error")
        and message.get("type") == "interrupted"
        and "partial output above was recovered"
        in str(message.get("content", ""))
        for message in session.messages
    )
    # Recovery actually appended rows rather than clearing pending state only.
    assert json.dumps(session.messages, ensure_ascii=False) != before
    assert session.messages[0]["content"] == "inspect the failing test"


def test_full_recovery_suppresses_duplicates_only_for_genuine_final_answer(tmp_path):
    """Positive control for the suppression branch: when the pending turn's
    own user row is followed by a genuine final answer AND the run journal
    has the same-stream ``done`` event (durable terminal evidence), the
    repair clears the stale pending state and appends nothing.
    """
    session_id = "issue6366_suppression_control"
    stream_id = "stream-stale-cancelled"
    started_at = 1_700_000_900.25

    session = Session(
        session_id=session_id,
        title="Already-answered pending turn",
        messages=[
            {
                "role": "user",
                "content": "summarise the diff",
                "timestamp": int(started_at),
                "_active_turn_token": f"{stream_id}:{started_at:.17g}",
            },
            {"role": "assistant", "content": "The diff adds a guard."},
        ],
        context_messages=[
            {"role": "user", "content": "summarise the diff"},
            {"role": "assistant", "content": "The diff adds a guard."},
        ],
        pending_user_message="summarise the diff",
        pending_started_at=started_at,
        active_stream_id=stream_id,
    )
    # Same-stream durable terminal evidence — the run journal was marked
    # ``done`` (terminal_state ``completed``) so the suppression gate
    # opens and the transcript-only advance is accepted as terminal.
    # No visible token events are written so the line-3648 path's
    # ``_append_journaled_partial_output`` has nothing to append.
    _write_journal(session_id, stream_id, [("done", {})])
    before = json.dumps(session.messages, ensure_ascii=False)

    assert _apply_core_sync_or_error_marker(
        session,
        tmp_path / "missing-core-transcript.json",
        stream_id_for_recheck=stream_id,
    ) is True

    # Nothing was appended after the valid final answer.
    assert json.dumps(session.messages, ensure_ascii=False) == before
    assert not any(
        message.get("_recovered") or message.get("_recovered_from_run_journal")
        for message in session.messages
    )
    assert not any(message.get("_error") for message in session.messages)
    # The stale pending state was cleared.
    assert session.pending_user_message is None
    assert session.active_stream_id is None
    assert session.pending_started_at is None
    assert session.pending_user_source is None
    assert session.pending_attachments == []


def test_full_recovery_runs_journal_replay_for_unflagged_interim_prose(tmp_path):
    """9/22 re-gate regression, end-to-end: the durable transcript
    already advanced past the pending turn into an unflagged interim
    prose row followed by tool rows, but the run journal was never
    marked ``done``. Before the fix,
    ``_transcript_already_advanced_past_pending`` returned True at the
    first visible assistant row, ``_apply_core_sync_or_error_marker``
    cleared the pending state, and the user saw a half-finished turn
    with the journal's partial output and the interruption marker
    both suppressed. The fix requires durable same-stream terminal
    evidence (``done`` / ``stream_end`` → ``completed``) before
    suppressing recovery, so the run journal is replayed and the
    ``interrupted`` marker is appended for this unflagged-prose
    shape.
    """
    session_id = "issue6366_unflagged_interim_prose"
    stream_id = "stream-unflagged-interim"
    started_at = 1_700_001_200.75

    session = Session(
        session_id=session_id,
        title="Unflagged interim prose followed by tool rows",
        messages=[
            {
                "role": "user",
                "content": "check the failing test",
                "timestamp": int(started_at),
                "_active_turn_token": f"{stream_id}:{started_at:.17g}",
            },
            # The bug shape: an unflagged assistant row that the
            # transcript heuristic accepts as a "final answer" because
            # it carries visible text and no _partial / _error /
            # _recovered / _recovered_from_run_journal hint. The
            # stream had just started talking about what it was about
            # to do, then ran tools. The tool_calls that produced the
            # following tool rows live on a later row in the original
            # stream — the WebUI process died before the tool-call
            # assistant row was committed, so the transcript currently
            # shows just the pre-final prose followed by tool result
            # rows.
            {
                "role": "assistant",
                "content": "Let me check the logs first.",
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "log tail"},
            {"role": "tool", "tool_call_id": "call_2", "content": "log grep"},
        ],
        context_messages=[
            {"role": "user", "content": "check the failing test"},
        ],
        pending_user_message="check the failing test",
        pending_started_at=started_at,
        active_stream_id=stream_id,
    )
    # The run journal has visible progress — the unflagged prose was
    # emitted as a token, the run was checkpointed as interim_assistant,
    # and one of the tool rows finished. Crucially there is no
    # ``done`` / ``stream_end`` event: the stream died mid-tool, so
    # the journal has no durable terminal evidence.
    _write_journal(
        session_id, stream_id,
        [
            ("reasoning", {"text": "Reading the assertion first."}),
            ("token", {"text": "Let me check the logs first."}),
            ("interim_assistant", {"text": "checkpoint"}),
            (
                "tool",
                {
                    "name": "terminal",
                    "tid": "call_1",
                    "preview": "tail -n 50",
                    "args": {"command": "tail -n 50"},
                },
            ),
            (
                "tool_complete",
                {
                    "name": "terminal",
                    "tid": "call_1",
                    "preview": "log tail",
                    "duration": 0.05,
                },
            ),
        ],
    )
    before = json.dumps(session.messages, ensure_ascii=False)

    assert _apply_core_sync_or_error_marker(
        session,
        tmp_path / "missing-core-transcript.json",
        stream_id_for_recheck=stream_id,
    ) is True

    # Recovery actually appended rows rather than clearing pending
    # state only.
    assert json.dumps(session.messages, ensure_ascii=False) != before
    # The journal's visible partial output is back in the transcript:
    # the "Let me check the logs first." prose that was emitted over
    # SSE before the WebUI process died is now visible again.
    assert any(
        message.get("_recovered_from_run_journal")
        and "Let me check the logs first." in str(message.get("content", ""))
        for message in session.messages
    )
    # And the interrupted turn is marked, so the user can see the
    # stream was cut off rather than presented with a half-finished
    # turn that looks complete.
    interrupted_marker = next(
        (
            message
            for message in session.messages
            if message.get("_error") and message.get("type") == "interrupted"
        ),
        None,
    )
    assert interrupted_marker is not None
    assert "partial output above was recovered" in str(
        interrupted_marker.get("content", ""),
    )
    # Pending state was cleared (recovery always clears it) and no
    # duplicate user row was appended ahead of the journal replay —
    # the original user row is still the first message.
    assert session.messages[0]["content"] == "check the failing test"
    assert session.pending_user_message is None
    assert session.active_stream_id is None
