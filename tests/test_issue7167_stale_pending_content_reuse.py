"""Regression tests: the stale-pending repair call site must not duplicate output.

greptile P1 on #7167 (api/models.py:4559): the stale-pending call site passes
``dedupe_existing=False``, and the "new unconditional reuse covers only
reasoning-only rows; content and tool matching still require
``dedupe_existing=True``". Repeated cache-miss repairs of the same dead stream
therefore accumulate duplicate answers, tool cards, and empty anchors.

Root cause: that call site appends the recovered user row BEFORE recovering
output, so the ownership-gated content search (``min_index=current_turn_min_idx``)
looks past the artifacts an earlier pass already produced and refuses them. Every
repair cycle then appends another copy of the journal's visible output.

Fix: reuse is now keyed on provenance, not ownership — a row carrying both
``_recovered_from_run_journal`` and this exact ``_recovered_stream_id`` is the
recovery's own earlier output, so reusing it is idempotent. An untagged live row
can never satisfy that provenance check, so a genuine current-turn answer is
never suppressed.

These tests drive the PRODUCTION entry point
(``_apply_core_sync_or_error_marker``) with a real run journal, the same way the
WebUI re-repairs a sidecar on each cache-miss read.
"""
from __future__ import annotations

import pytest

import api.profiles as profiles
from api.models import Session, _apply_core_sync_or_error_marker
from api.run_journal import append_run_event

ANSWER = "The answer is 42."


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    return home


def _content_journal(sid: str, stream_id: str) -> None:
    append_run_event(sid, stream_id, "token", {"text": ANSWER})
    # No terminal event: the stream died mid-turn (the repair target).


def _make_session(sid, stream_id, previous_messages=None) -> Session:
    messages = (
        [dict(m) for m in previous_messages]
        if previous_messages
        else [
            {"role": "user", "content": "earlier turn"},
            {"role": "assistant", "content": "earlier reply"},
        ]
    )
    s = Session(session_id=sid, title="repro", messages=messages)
    s.pending_user_message = "crash-turn prompt"
    s.active_stream_id = stream_id
    s.pending_attachments = []
    s.pending_started_at = None
    s.pending_user_source = None
    return s


def _count(session: Session, needle: str, role: str) -> int:
    return sum(
        1
        for m in session.messages
        if isinstance(m, dict)
        and m.get("role") == role
        and needle in str(m.get("content") or "")
    )


def _repair_five_times(sid, stream_id, hermes_home, previous_messages=None, previous_tool_calls=None):
    """Replay the WebUI repair loop: each pass reloads the persisted sidecar."""
    current = previous_messages
    current_tool_calls = previous_tool_calls
    session = None
    for _cycle in range(5):
        session = _make_session(sid, stream_id, previous_messages=current)
        if current_tool_calls is not None:
            session.tool_calls = [dict(tc) for tc in current_tool_calls]
        result = _apply_core_sync_or_error_marker(
            session,
            hermes_home / "sessions" / f"session_{sid}.json",
            stream_id_for_recheck=stream_id,
        )
        assert result is True
        current = session.messages
        current_tool_calls = session.tool_calls
    return session


def test_repeated_repair_does_not_duplicate_content(hermes_home):
    """Five repair cycles must leave ONE recovered answer row, not five."""
    sid = "regate_p1_content"
    stream_id = "dead-stream-content"
    _content_journal(sid, stream_id)

    session = _repair_five_times(sid, stream_id, hermes_home)

    recovered_rows = [
        m
        for m in session.messages
        if isinstance(m, dict)
        and m.get("_recovered_from_run_journal")
        and m.get("_recovered_stream_id") == stream_id
        and m.get("role") == "assistant"
    ]
    assert len(recovered_rows) == 1, (
        f"content duplicated: {len(recovered_rows)} recovered answer rows"
    )
    assert _count(session, ANSWER, "assistant") == 1


def test_repeated_repair_content_then_reasoning(hermes_home):
    """A reasoning + content journal must also stay at a single row."""
    sid = "regate_p1_mixed"
    stream_id = "dead-stream-mixed"
    append_run_event(sid, stream_id, "reasoning", {"text": "**Thinking**"})
    append_run_event(sid, stream_id, "token", {"text": ANSWER})

    session = _repair_five_times(sid, stream_id, hermes_home)

    recovered_rows = [
        m
        for m in session.messages
        if isinstance(m, dict)
        and m.get("_recovered_from_run_journal")
        and m.get("_recovered_stream_id") == stream_id
        and m.get("role") == "assistant"
    ]
    assert len(recovered_rows) == 1, (
        f"mixed journal duplicated: {len(recovered_rows)} recovered rows"
    )
    assert _count(session, ANSWER, "assistant") == 1


def test_repeated_repair_does_not_stack_interruption_markers(hermes_home):
    """The reuse must not re-raise a fresh interruption marker each pass."""
    sid = "regate_p1_marker"
    stream_id = "dead-stream-marker"
    _content_journal(sid, stream_id)

    session = _repair_five_times(sid, stream_id, hermes_home)

    markers = [
        m
        for m in session.messages
        if isinstance(m, dict) and m.get("_error")
    ]
    assert len(markers) <= 1, f"interruption markers stacked: {len(markers)}"


def test_fresh_current_turn_answer_is_never_suppressed(hermes_home):
    """Control: a DIFFERENT stream's live answer must not be reused as ours.

    Provenance is the reuse key, so an untagged live row that happens to carry
    the same text must still get its own recovered row appended rather than
    being silently absorbed.
    """
    sid = "regate_p1_crosstream"
    dead_stream = "dead-stream-ours"
    _content_journal(sid, dead_stream)

    # A live (untagged) assistant row from another turn already holds the text.
    previous = [
        {"role": "user", "content": "unrelated turn"},
        {"role": "assistant", "content": ANSWER},
        {"role": "user", "content": "crash-turn prompt"},
    ]
    session = _repair_five_times(sid, dead_stream, hermes_home, previous)

    recovered_rows = [
        m
        for m in session.messages
        if isinstance(m, dict)
        and m.get("_recovered_from_run_journal")
        and m.get("_recovered_stream_id") == dead_stream
    ]
    assert len(recovered_rows) == 1, (
        "the dead stream's own output must be recovered exactly once"
    )
    # The untagged live row that already held the same text is never consumed
    # by the recovery: it survives untouched as a plain, unmarked row, and the
    # journal's output is recovered BESIDE it rather than through it.
    live_rows = [
        m
        for m in session.messages
        if isinstance(m, dict)
        and m.get("role") == "assistant"
        and m.get("content") == ANSWER
        and not m.get("_recovered_from_run_journal")
    ]
    assert len(live_rows) == 1, "the pre-existing live answer row must survive"
    assert recovered_rows[0] is not live_rows[0]
    # No duplicated prompt pile-up either: the prompt stays at a single
    # checkpointed row plus the original one.
    assert _count(session, "crash-turn prompt", "user") == 2


# ─── #7167 re-gate: tool cards must be idempotent at the stale-pending caller ───


def _tool_journal(sid: str, stream_id: str) -> None:
    """One journaled tool call + completion for ``stream_id``; no terminal."""
    append_run_event(
        sid,
        stream_id,
        "tool",
        {"name": "terminal", "preview": "ls -la"},
    )
    append_run_event(
        sid,
        stream_id,
        "tool_complete",
        {"name": "terminal", "preview": "ls -la"},
    )


def _stream_tool_cards(session: Session, stream_id: str) -> list[dict]:
    return [
        tc
        for tc in (session.tool_calls or [])
        if isinstance(tc, dict)
        and tc.get("_recovered_from_run_journal")
        and tc.get("_recovered_stream_id") == stream_id
    ]


def test_repeated_repair_tool_only_keeps_one_card(hermes_home):
    """The reviewer's remaining failure: driving the real
    ``_apply_core_sync_or_error_marker`` through repeated stale-pending
    repair cycles left THREE cards for ONE journaled ``terminal: ls -la``
    event (all tid ``journal-1``, stream A). The stale-pending caller passed
    ``dedupe_existing=False``, so the tool branch never ran the match.
    """
    sid = "regate_tool_only"
    dead_stream = "dead-stream-a"
    _tool_journal(sid, dead_stream)

    session = _repair_five_times(sid, dead_stream, hermes_home)

    cards = _stream_tool_cards(session, dead_stream)
    assert len(cards) == 1, (
        "one journaled tool event must recover exactly one card across "
        f"repeated repair cycles, got {len(cards)}"
    )
    assert cards[0].get("name") == "terminal"
    assert cards[0].get("done") is True, "the tool_complete event must still land"
    # One-to-one consumption: the single card is claimed once, not re-claimed.
    anchors = [tc.get("assistant_msg_idx") for tc in cards]
    assert all(isinstance(a, int) for a in anchors), (
        f"cards must have real assistant anchors, got {anchors}"
    )


def test_repeated_repair_mixed_content_and_tool_reuses_both(hermes_home):
    """Content reuse (already covered) + card reuse in ONE mixed replay."""
    sid = "regate_mixed"
    dead_stream = "dead-stream-mixed"
    _content_journal(sid, dead_stream)
    _tool_journal(sid, dead_stream)

    session = _repair_five_times(sid, dead_stream, hermes_home)

    assert _stream_tool_cards(session, dead_stream) != []
    assert len(_stream_tool_cards(session, dead_stream)) == 1, (
        "mixed replay must not grow tool cards either"
    )
    recovered_rows = [
        m
        for m in session.messages
        if isinstance(m, dict)
        and m.get("_recovered_from_run_journal")
        and m.get("_recovered_stream_id") == dead_stream
    ]
    assert recovered_rows, "content reuse must keep working"


def test_distinct_streams_keep_two_cards(hermes_home):
    """Provenance is preserved: two DIFFERENT streams journaling an
    identical-looking tool call keep TWO cards (one per stream). The dedupe
    is stream-scoped, so turning it on must not collapse them.
    """
    sid = "regate_tool_crosstream"
    stream_a = "dead-stream-a"
    stream_b = "dead-stream-b"
    _tool_journal(sid, stream_a)
    _tool_journal(sid, stream_b)

    session = _repair_five_times(sid, stream_a, hermes_home)
    # Now recover the second stream against the same persisted session.
    session_b = _make_session(
        sid, stream_b, previous_messages=list(session.messages)
    )
    session_b.tool_calls = list(session.tool_calls or [])
    _apply_core_sync_or_error_marker(
        session_b,
        hermes_home / "sessions" / f"session_{sid}.json",
        stream_id_for_recheck=stream_b,
    )

    cards_a = _stream_tool_cards(session_b, stream_a)
    cards_b = _stream_tool_cards(session_b, stream_b)
    assert len(cards_a) == 1 and len(cards_b) == 1, (
        "distinct-stream multiplicity must survive: "
        f"a={len(cards_a)} b={len(cards_b)}"
    )


def test_old_untagged_live_card_is_not_swallowed(hermes_home):
    """A pre-existing UNTAGGED live card (no provenance) with the same
    name/preview must never be consumed by the recovery — ownership cannot
    be proven, so the journal output appends BESIDE it.
    """
    sid = "regate_tool_untagged"
    dead_stream = "dead-stream-a"
    _tool_journal(sid, dead_stream)

    previous = [
        {"role": "user", "content": "earlier turn"},
        {"role": "assistant", "content": "earlier reply"},
    ]
    live_card = {
        "name": "terminal",
        "preview": "ls -la",
        "assistant_msg_idx": 1,
        "_live": True,
    }

    session = _repair_five_times(
        sid,
        dead_stream,
        hermes_home,
        previous_messages=previous,
        previous_tool_calls=[live_card],
    )

    untagged = [
        tc
        for tc in (session.tool_calls or [])
        if isinstance(tc, dict) and not tc.get("_recovered_from_run_journal")
    ]
    assert len(untagged) == 1, "the pre-existing live card must survive untouched"
    assert len(_stream_tool_cards(session, dead_stream)) == 1, (
        "the journaled event still recovers beside the live card"
    )
