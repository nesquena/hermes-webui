"""Regression tests: journal recovery call sites must pass dedupe_existing=True.

Root cause (2026-08-20): `_apply_core_sync_or_error_marker` had two call sites
into `_recover_journaled_output_and_terminal_error` that omitted
``dedupe_existing=True`` (lines ~3437 and ~3551). The sibling call sites in the
lazy-retry path and the completed-stream branch passed it. With dedupe off,
every get_session() -> cache-miss -> repair cycle re-replayed the same dead
stream's run journal and appended a fresh batch of empty recovered assistant
rows each time — a session observed with 1024 duplicate empty-content
``_recovered_from_run_journal`` messages (2^n block growth: 1,2,4,...,512).

These tests drive the PRODUCTION entry point (`_apply_core_sync_or_error_marker`)
with a real run journal, proving repeated repair for the same stream does not
accumulate duplicate empty recovered rows.
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


def _make_reasoning_only_journal(session_id: str, stream_id: str) -> None:
    """Journal whose only visible output is reasoning (no tokens/tools).

    This is the shape that yields empty-content recovered assistant rows:
    recovery restores display-only reasoning but appends rows with empty
    ``content``. Without dedupe, each repair pass adds a fresh copy.
    """
    append_run_event(session_id, stream_id, "reasoning", {"text": "**Planning check**"})
    append_run_event(session_id, stream_id, "reasoning", {"text": "\n\n**Inspecting state**"})
    # No terminal event — the stream died mid-turn (the repair target).


def _make_repair_session(
    session_id: str,
    stream_id: str,
    previous_messages: list | None = None,
) -> Session:
    """Session shaped like a WebUI sidecar found after a crash: messages
    already non-empty (previous history), stale pending turn, dead stream.

    ``previous_messages`` simulates the persisted sidecar content from an
    earlier repair pass (get_session() reloads the sidecar file each time),
    which is exactly how duplicate recovered rows accumulated in the wild.
    """
    messages = (
        [dict(m) for m in previous_messages]
        if previous_messages
        else [
            {"role": "user", "content": "earlier turn"},
            {"role": "assistant", "content": "earlier reply"},
        ]
    )
    s = Session(session_id=session_id, title="repro", messages=messages)
    s.pending_user_message = "crash-turn prompt"
    s.active_stream_id = stream_id
    s.pending_attachments = []
    s.pending_started_at = None
    s.pending_user_source = None
    return s


def _empty_recovered_count(session: Session) -> int:
    return sum(
        1
        for m in session.messages
        if isinstance(m, dict)
        and m.get("_recovered_from_run_journal")
        and m.get("role") == "assistant"
        and not str(m.get("content") or "").strip()
    )


def test_repair_reuses_existing_empty_recovered_rows(hermes_home):
    """Repeated repair for the same dead stream must not pile up empty
    recovered assistant rows — the dedupe_existing=True contract.

    Before the fix (dedupe omitted at the _apply_core_sync_or_error_marker
    call site), every repair pass appended the journal's reasoning again,
    producing the 1024-duplicate transcript observed in the wild.
    """
    sid = "dedupe_call_site"
    stream_id = "dead-stream-A"
    _make_reasoning_only_journal(sid, stream_id)

    # Simulate multiple cache-miss repair cycles: each pass reloads the
    # sidecar messages persisted by the previous pass (as get_session does).
    current = None
    for _ in range(5):
        session = _make_repair_session(sid, stream_id, previous_messages=current)
        result = _apply_core_sync_or_error_marker(
            session,
            hermes_home / "sessions" / f"session_{sid}.json",
            stream_id_for_recheck=stream_id,
        )
        assert result is True
        assert session.pending_user_message is None  # repair cleared pending
        current = session.messages

    assert _empty_recovered_count(session) <= 2, (
        "repeated repair accumulated duplicate empty recovered rows "
        "(dedupe_existing was not honored at this call site)"
    )


def test_repair_dedupe_is_stream_scoped(hermes_home):
    """Distinct dead streams keep their own recovered rows — dedupe must not
    collapse unrelated streams together."""
    sid = "dedupe_scoped"
    session = _make_repair_session(sid, "stream-X")
    _make_reasoning_only_journal(sid, "stream-X")
    _apply_core_sync_or_error_marker(
        session,
        hermes_home / "sessions" / f"session_{sid}.json",
        stream_id_for_recheck="stream-X",
    )
    assert _empty_recovered_count(session) == 1, (
        "first repair for a fresh stream should create one recovered row"
    )

    # Second stream, same session: separate anchor.
    session2 = _make_repair_session(sid, "stream-Y")
    _make_reasoning_only_journal(sid, "stream-Y")
    _apply_core_sync_or_error_marker(
        session2,
        hermes_home / "sessions" / f"session_{sid}.json",
        stream_id_for_recheck="stream-Y",
    )
    assert _empty_recovered_count(session2) == 1


def test_repair_preserves_current_turn_recovered_rows_when_older_identical_history_exists(hermes_home):
    """Recovery must preserve current turn's recovered content and tool cards even
    if an older turn in session history contained identical text/tool calls.
    """
    sid = "dedupe_identical_history"
    stream_id = "stream-Z"

    # Write journal for stream-Z with assistant text and a tool card
    append_run_event(sid, stream_id, "token", {"text": "identical response"})
    append_run_event(sid, stream_id, "tool", {"name": "terminal", "preview": "ls -la"})

    # Session with older history containing identical text & tool call
    older_history = [
        {"role": "user", "content": "same prompt"},
        {"role": "assistant", "content": "identical response"},
    ]
    session = _make_repair_session(sid, stream_id, previous_messages=older_history)
    session.tool_calls = [{"name": "terminal", "preview": "ls -la", "_recovered_stream_id": "older-stream"}]

    result = _apply_core_sync_or_error_marker(
        session,
        hermes_home / "sessions" / f"session_{sid}.json",
        stream_id_for_recheck=stream_id,
    )
    assert result is True

    # Assert that current turn's recovered assistant text is retained
    assistant_contents = [
        m.get("content")
        for m in session.messages
        if isinstance(m, dict) and m.get("role") == "assistant"
    ]
    assert assistant_contents.count("identical response") == 2, (
        "Current turn's identical recovered assistant text was dropped due to session-wide dedupe"
    )

    # Assert that current turn's recovered tool card is retained in tool_calls
    recovered_tools = [
        tc for tc in (session.tool_calls or [])
        if tc.get("_recovered_stream_id") == stream_id
    ]
    assert len(recovered_tools) == 1, (
        "Current turn's identical recovered tool card was dropped due to session-wide dedupe"
    )



# ---------------------------------------------------------------------------
# Re-gate 2026-08-21 (nesquena-hermes), CORE#2:
#   "Ordinary persisted tool summaries have NO stream tag, so the
#    _stream_id/_recovered_stream_id guard doesn't protect them — an older
#    `terminal: ls` still suppresses the current recovered tool."
#
# The pre-existing test above does NOT bite this, because it gives the older
# tool card an ARTIFICIAL stream tag (`_recovered_stream_id: "older-stream"`),
# which routes matching into the tagged branch and skips the real defect.
#
# This test uses the true persisted shape: an UNTAGGED card with the same
# name+preview, anchored at an EARLIER turn's assistant row, and drives the
# real `get_session()` cache-miss repair path (the reviewer's reproduction
# entry point) rather than calling the repair helper directly.
# ---------------------------------------------------------------------------


def test_older_untagged_tool_card_does_not_suppress_current_recovery(hermes_home):
    """CORE#2: an older UNTAGGED tool card must not swallow the current turn's
    recovered tool card."""
    import api.models as models

    sid = "regate_untagged_tool"
    stream_id = "regate-untagged-stream"

    append_run_event(sid, stream_id, "token", {"text": "all clear"})
    append_run_event(sid, stream_id, "tool", {"name": "terminal", "preview": "ls -la"})

    session = Session(
        session_id=sid,
        title="regate",
        messages=[
            {"role": "user", "content": "run the check", "timestamp": 111},
            {"role": "assistant", "content": "all clear", "timestamp": 112},
            {"role": "user", "content": "run the check", "timestamp": 222},
        ],
    )
    session.pending_user_message = "run the check"
    session.active_stream_id = stream_id
    session.pending_attachments = []
    session.pending_started_at = 222
    session.pending_user_source = None
    # The real persisted shape: NO _stream_id / _recovered_stream_id, anchored
    # at the OLDER turn's assistant row (index 1).
    session.tool_calls = [
        {
            "name": "terminal",
            "preview": "ls -la",
            "snippet": "ls -la",
            "assistant_msg_idx": 1,
            "done": True,
        },
    ]
    session.save()
    models.SESSIONS.pop(sid, None)          # force the cache-miss repair path

    reloaded = models.get_session(sid)

    recovered_tools = [
        tc for tc in (reloaded.tool_calls or [])
        if tc.get("_recovered_from_run_journal")
    ]
    assert len(recovered_tools) == 1, (
        "CORE#2: the current turn's recovered tool card was dropped because an "
        "older UNTAGGED card shared name+preview "
        f"(tool_calls={reloaded.tool_calls!r})"
    )
    assert recovered_tools[0].get("_recovered_stream_id") == stream_id

    # The pre-existing untagged card must still be there (no data destroyed).
    assert any(
        tc.get("assistant_msg_idx") == 1 and not tc.get("_recovered_from_run_journal")
        for tc in (reloaded.tool_calls or [])
    ), "the older untagged tool card must be preserved, not replaced"
