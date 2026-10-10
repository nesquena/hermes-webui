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


def _token_journal(sid: str, stream_id: str) -> None:
    """One journaled token and no terminal event: the interrupted-turn shape."""
    append_run_event(sid, stream_id, "token", {"text": "hello"})


def _shapes(session: Session) -> list[str]:
    """Compact transcript shape for a probe table."""
    out = []
    for m in session.messages:
        role = m.get("role")
        content = str(m.get("content") or "")
        out.append(f"{role}:{content[:24]!r}")
    return out


def _probe_repair(sid: str, stream_id: str, hermes_home, cycles: int = 5):
    """Run repair cycles, capturing the transcript shape after each one."""
    current = None
    session = None
    seen = []
    for _cycle in range(cycles):
        session = _make_session(sid, stream_id, previous_messages=current)
        assert _apply_core_sync_or_error_marker(
            session,
            hermes_home / "sessions" / f"session_{sid}.json",
            stream_id_for_recheck=stream_id,
        ) is True
        current = session.messages
        seen.append(_shapes(session))
    return session, seen


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
    # No duplicated prompt pile-up: the prompt is materialized exactly once.
    # It used to land twice — the second copy appended AFTER the interruption
    # marker, which is exactly the dangling-prompt defect #7167's re-gate
    # describes (the chat then reads as though the prompt was never answered).
    assert _count(session, "crash-turn prompt", "user") == 1
    # ...and the interruption marker stays last, never the recovered prompt.
    assert str(session.messages[-1].get("content") or "").startswith(
        "**Response interrupted"
    )


# ─── #7167 re-gate: repeated repair must end with the marker LAST ───────────


def test_repeated_repair_leaves_marker_last_never_a_dangling_prompt(hermes_home):
    """Repeated same-stream repair must not leave a dangling prompt.

    The maintainer's probe (three repair/save/reload cycles, one journaled
    token) produced:

    | cycle | this PR                                | master |
    | 0     | old, reply, prompt, hello, [interrupted] | same   |
    | 1     | …, [interrupted], **new prompt**        | grows by 3 rows |
    | 2     | stable                                 | grows by 3 rows |

    The PR fixed master's runaway growth but left the dangling prompt: the
    recovered user row was re-appended AFTER the interruption marker, so the
    chat reads as though the user's prompt was never answered.

    Root cause: the append gate was a textual check on the transcript's LAST
    message. After the first cycle the pending prompt's row sits *before* the
    journaled answer and the marker — so the tail is the marker, which no
    user-row predicate can match, and the gate says "append" forever.

    The fix adds the token-bound materiality proof and a marker-anchored
    recovery check to the gate, and lifts the #6366 cleanup out of it so an
    already-finished turn still clears its pending state instead of falling
    through to the append branch.
    """
    sid = "regate_p3_dangling_prompt"
    stream_id = "dead-stream-dangling"
    _token_journal(sid, stream_id)

    session, seen = _probe_repair(sid, stream_id, hermes_home, cycles=3)

    # Marker is the LAST row, in every cycle from the first onward.
    for cycle, shape in enumerate(seen):
        assert shape[-1].startswith("assistant:'**Response interrupted"), (
            f"cycle {cycle} must end with the interruption marker, got {shape[-1]!r}"
        )
        marker_idx = len(shape) - 1
        prompt_rows = [
            i for i, row in enumerate(shape)
            if row.startswith("user:'crash-turn prompt'")
        ]
        # The prompt must exist exactly once, always BEFORE the marker. A copy
        # after it is the dangling prompt this test pins.
        assert prompt_rows, f"cycle {cycle} lost the user prompt: {shape}"
        assert all(i < marker_idx for i in prompt_rows), (
            f"cycle {cycle} appended a prompt after the marker: {shape}"
        )
        assert len(prompt_rows) == 1, (
            f"cycle {cycle} duplicated the prompt: {prompt_rows}"
        )
    # Stable from cycle 1 — the rows added by cycle 0 are never re-added.
    assert seen[1] == seen[2]

    # Exactly one recovered prompt row in the final session.
    assert _count(session, "crash-turn prompt", "user") == 1


def test_already_repaired_turn_still_clears_stale_pending_state(hermes_home):
    """A finished turn must clear pending state, not duplicate itself.

    The #6366 cleanup (transcript already advanced past this turn) used to sit
    *inside* the append gate. gating that cleanup on materiality made the
    already-checkpointed case fall through to the append branch, duplicating a
    turn that is visibly finished — the exact regression its own docstring
    warns about. It is now a pure cleanup path that runs before the gate.
    """
    sid = "regate_p3_completed_turn"
    stream_id = "dead-stream-completed"
    started_at = 1_700_009_999.5

    session = Session(
        session_id=sid,
        title="completed per turn journal",
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
        pending_user_message="summarise the diff",
        pending_started_at=started_at,
        pending_user_source="webui",
        pending_attachments=[],
        active_stream_id=stream_id,
    )
    before = [dict(m) for m in session.messages]

    from api.run_journal import append_run_event
    from api.turn_journal import append_turn_journal_event_for_stream

    append_run_event(sid, stream_id, "token", {"text": "The diff adds a guard."})
    # The turn journal recorded the exact-stream terminal completion.
    append_turn_journal_event_for_stream(
        sid,
        stream_id,
        {"event": "completed", "created_at": 1_700_000_000.0},
    )

    assert (
        _apply_core_sync_or_error_marker(
            session,
            hermes_home / "sessions" / f"session_{sid}.json",
            stream_id_for_recheck=stream_id,
        )
        is True
    )
    # The transcript is untouched and the pending state is gone.
    assert session.messages == before
    assert session.pending_user_message is None
    assert session.active_stream_id is None


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


# ── #7167 Must-fix 1: identity-based tool matching ──────────────────────────
#
# The installed Agent journals the LIVE path (``tool_start_callback``):
# ``tool`` carries ``preview: None`` with a real ``tid``/seq, then
# ``tool_complete`` overwrites the CARD's preview with the result snippet.
# The submitted tests used equal start/completion previews, so they never hit
# the two failures that shape causes:
#
#   * completion BEFORE repair — the start event's empty preview can no longer
#     match a card whose preview was replaced, so every repair cycle appends
#     another card (1 event -> 3 cards over 3 cycles);
#   * completion AFTER the first repair — the card matched by identity is
#     skipped by the completion handler (it only walked freshly built cards),
#     so it stays ``done=False`` with no result forever.
#
# Both are fixed by matching on immutable identity (stream + event seq) and by
# completing REUSED cards too.

EMPTY_PREVIEW_TOOL_JOURNAL = {
    "name": "terminal",
    "preview": None,  # what api/streaming.py writes for tool_start_callback
    "tid": "tid-live-1",
}


def _tool_journal_real_shape(sid: str, stream_id: str, *, delay_completion: bool) -> None:
    """The shape the installed Agent actually journals (re-gate Must-fix 1)."""
    append_run_event(sid, stream_id, "tool", dict(EMPTY_PREVIEW_TOOL_JOURNAL))
    if not delay_completion:
        append_run_event(
            sid,
            stream_id,
            "tool_complete",
            {"name": "terminal", "preview": "total 0\n", "duration": 12},
        )


def test_completion_before_repair_keeps_one_card(hermes_home):
    """Completion landed before the repair: the start preview is stale.

    The card's preview was already replaced by the result snippet, so a
    preview-based match fails and each repair cycle appends another card.
    Identity (stream + event seq) must match instead.
    """
    sid = "regate_real_shape_completed"
    dead_stream = "dead-stream-completed"
    _tool_journal_real_shape(sid, dead_stream, delay_completion=False)

    session = _repair_five_times(sid, dead_stream, hermes_home)

    cards = _stream_tool_cards(session, dead_stream)
    assert len(cards) == 1, (
        "one journaled tool event must recover exactly one card across "
        f"repeated repair cycles, got {len(cards)}"
    )
    assert cards[0].get("done") is True, "the reused card must be completed"
    assert cards[0].get("preview") == "total 0\n", (
        "the completion's result snippet must survive, got "
        f"{cards[0].get('preview')!r}"
    )
    assert cards[0].get("duration") == 12


def test_delayed_completion_completes_the_reused_card(hermes_home):
    """Completion arrives after the repair already reused the card.

    The card matched by identity while still ``done=False`` must be updated by
    the completion handler, not left incomplete and result-less.
    """
    sid = "regate_real_shape_delayed"
    dead_stream = "dead-stream-delayed"
    # Repair cycles run against a journal that has NOT completed yet.
    _tool_journal_real_shape(sid, dead_stream, delay_completion=True)

    session = _repair_five_times(sid, dead_stream, hermes_home)
    cards = _stream_tool_cards(session, dead_stream)
    assert len(cards) == 1, f"expected exactly one card, got {len(cards)}"
    assert cards[0].get("done") is False, "no completion yet: card stays open"

    # The stream's completion lands late (after a reopen/repair already ran).
    append_run_event(
        sid,
        dead_stream,
        "tool_complete",
        {"name": "terminal", "preview": "total 0\n", "duration": 7},
    )
    session = _repair_five_times(sid, dead_stream, hermes_home)
    cards = _stream_tool_cards(session, dead_stream)
    assert len(cards) == 1, (
        "a late completion must not append a second card, got "
        f"{len(cards)}"
    )
    assert cards[0].get("done") is True, "the reused card must be completed"
    assert cards[0].get("preview") == "total 0\n"
    assert cards[0].get("duration") == 7


def test_identity_match_still_consumes_one_to_one(hermes_home):
    """Two genuinely identical live calls stay as two cards.

    Identity matching must not collapse across DISTINCT events: each journal
    event carries its own seq, so two events claim two distinct cards.
    """
    sid = "regate_real_shape_two_events"
    dead_stream = "dead-stream-two-events"
    append_run_event(sid, dead_stream, "tool", dict(EMPTY_PREVIEW_TOOL_JOURNAL))
    append_run_event(
        sid, dead_stream, "tool_complete",
        {"name": "terminal", "preview": "first\n", "duration": 1},
    )
    append_run_event(
        sid, dead_stream, "tool",
        {"name": "terminal", "preview": None, "tid": "tid-live-2"},
    )
    append_run_event(
        sid, dead_stream, "tool_complete",
        {"name": "terminal", "preview": "second\n", "duration": 2},
    )

    session = _repair_five_times(sid, dead_stream, hermes_home)

    cards = _stream_tool_cards(session, dead_stream)
    assert len(cards) == 2, (
        "two distinct journal events must recover two cards, got "
        f"{len(cards)}"
    )
    previews = sorted(str(tc.get('preview') or '') for tc in cards)
    assert previews == ["first\n", "second\n"], (
        f"each card takes its own completion, got {previews}"
    )


def test_marker_promotion_keeps_tool_anchor_on_assistant(hermes_home):
    """Must-fix 3: promoting a reused marker must not orphan a tool card.

    ``_reorder_journal_tail_above_marker`` rebased anchors with a contiguous
    minus-one shift that assumed every row after the marker was journaled. A
    stale-pending cycle can interleave the journaled rows with a recovered user
    prompt, so a card anchored past that interleaving kept an index that ended
    up pointing at a USER row. Rebase by identity instead.
    """
    from api.models import _reorder_journal_tail_above_marker

    markers_msg = {"role": "assistant", "content": "reload to retry",
                   "_pending_journal_recovery": True}
    first_journaled = {"role": "assistant", "content": "first recovered",
                       "_recovered_from_run_journal": True,
                       "_recovered_stream_id": "stream-a"}
    # The interleaving the contiguous shift cannot express: a recovered user
    # prompt sits BETWEEN two journaled assistant rows.
    interleaved_user = {"role": "user", "content": "recovered prompt"}
    second_journaled = {"role": "assistant", "content": "second recovered",
                        "_recovered_from_run_journal": True,
                        "_recovered_stream_id": "stream-a"}

    messages = [
        {"role": "user", "content": "earlier turn"},
        {"role": "assistant", "content": "earlier reply"},
        markers_msg,          # index 2 - the marker being promoted
        first_journaled,      # index 3
        interleaved_user,     # index 4
        second_journaled,     # index 5  <- the tool card anchors here
    ]
    session = Session(session_id="anchor_probe", title="probe",
                      messages=[dict(m) for m in messages])
    # The card points at index 5, the second journaled assistant row.
    session.tool_calls = [
        {"name": "terminal", "preview": "ls", "assistant_msg_idx": 5,
         "_recovered_from_run_journal": True, "_recovered_stream_id": "stream-a"}
    ]
    # Point the in-memory list at the ORIGINAL dicts so identity survives.
    session.messages = [
        {"role": "user", "content": "earlier turn"},
        {"role": "assistant", "content": "earlier reply"},
        markers_msg,
        first_journaled,
        interleaved_user,
        second_journaled,
    ]

    _reorder_journal_tail_above_marker(session, 2)

    anchor = (session.tool_calls or [{}])[0].get("assistant_msg_idx")
    assert isinstance(anchor, int) and not isinstance(anchor, bool), (
        f"anchor must stay an int, got {anchor!r}"
    )
    assert 0 <= anchor < len(session.messages), f"anchor {anchor} out of range"
    anchored = session.messages[anchor]
    assert isinstance(anchored, dict) and anchored.get("role") == "assistant", (
        "tool card must stay anchored to an ASSISTANT row after the reorder, "
        f"got role={anchored.get('role')!r} at index {anchor}"
    )
    assert anchored is second_journaled, (
        "the card must still anchor the SAME assistant row, not a neighbour"
    )
