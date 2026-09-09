"""Regression coverage for #6920 cancel-time run-journal fallback."""

from __future__ import annotations

import copy
import json
import queue
import threading
import time
from unittest.mock import Mock, patch

import pytest

import api.config as config
import api.models as models
import api.streaming as streaming
from api.models import Session
from api.run_journal import RunJournalWriter
from api.streaming import _context_messages_for_new_turn, cancel_stream


def _empty_state_db_messages(*_args, with_revision=False, **_kwargs):
    """Model an unavailable state.db while honoring the revision-read contract."""
    if with_revision:
        return models.StateDBSessionMessagesSnapshot(messages=[], revision=None)
    return []


def _session_state_db_messages(
    current,
    *_args,
    prefer_context=False,
    state_messages=None,
    with_revision=False,
    **_kwargs,
):
    """Return the test session projection without discarding snapshot identity."""
    field = "context_messages" if prefer_context else "messages"
    messages = copy.deepcopy(getattr(current, field, None) or [])
    revision = (
        state_messages.revision
        if isinstance(state_messages, models.StateDBSessionMessagesSnapshot)
        else None
    )
    if with_revision:
        return models.StateDBSessionMessagesSnapshot(
            messages=messages,
            revision=revision,
        )
    return messages


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    for mapping_name in (
        "STREAMS",
        "CANCEL_FLAGS",
        "AGENT_INSTANCES",
        "STREAM_PARTIAL_TEXT",
        "STREAM_REASONING_TEXT",
        "STREAM_LIVE_TOOL_CALLS",
    ):
        getattr(config, mapping_name).clear()
    config.ACTIVE_RUNS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    config.STREAM_SESSION_OWNERS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()
    config.STREAM_CANCEL_GENERATIONS.clear()
    getattr(config, "STREAM_CANCEL_RECONCILIATION_RETRY_OWNERS", set()).clear()
    getattr(config, "STREAM_CANCEL_RECONCILIATION_DEAD_LETTERS", []).clear()
    yield
    models.SESSIONS.clear()
    for mapping_name in (
        "STREAMS",
        "CANCEL_FLAGS",
        "AGENT_INSTANCES",
        "STREAM_PARTIAL_TEXT",
        "STREAM_REASONING_TEXT",
        "STREAM_LIVE_TOOL_CALLS",
    ):
        getattr(config, mapping_name).clear()
    config.ACTIVE_RUNS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    config.STREAM_SESSION_OWNERS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()
    config.STREAM_CANCEL_GENERATIONS.clear()
    getattr(config, "STREAM_CANCEL_RECONCILIATION_RETRY_OWNERS", set()).clear()
    getattr(config, "STREAM_CANCEL_RECONCILIATION_DEAD_LETTERS", []).clear()


def _session(session_id: str, stream_id: str) -> Session:
    session = Session(
        session_id=session_id,
        title="Issue 6920",
        messages=[],
        context_messages=[],
        pending_user_message="Continue this cancelled turn",
        pending_started_at=1.0,
        active_stream_id=stream_id,
    )
    session.save()
    models.SESSIONS[session_id] = session
    return session


def _start_cancel_state(session_id: str, stream_id: str):
    config.STREAMS[stream_id] = queue.Queue()
    config.CANCEL_FLAGS[stream_id] = threading.Event()
    agent = Mock()
    agent.session_id = session_id
    agent.interrupt = Mock()
    config.AGENT_INSTANCES[stream_id] = agent
    return agent


def _seed_successor_progress(session, prompt: str, tool_name: str) -> None:
    """Persist a progressed successor so the old splice must preserve its owner indexes."""
    user = {
        "role": "user",
        "content": prompt,
        "timestamp": 20,
    }
    assistant = {
        "role": "assistant",
        "content": f"{tool_name} successor answer",
        "timestamp": 21,
    }
    session.messages.extend([user, assistant])
    session.context_messages.extend([copy.deepcopy(user), copy.deepcopy(assistant)])
    session.tool_calls = [
        {
            "name": tool_name,
            "preview": f"{tool_name} successor result",
            "assistant_msg_idx": len(session.messages) - 1,
            "done": True,
        },
    ]
    session.save()


def _seed_historical_duplicate_prompt(session) -> None:
    """Keep an older equal-text user turn to exercise boundary identity."""
    historical_user = {
        "role": "user",
        "content": session.pending_user_message,
        "timestamp": 0.5,
    }
    historical_assistant = {
        "role": "assistant",
        "content": "Historical answer before the cancelled turn.",
        "timestamp": 0.75,
    }
    session.messages.extend([historical_user, historical_assistant])
    session.context_messages.extend(
        [copy.deepcopy(historical_user), copy.deepcopy(historical_assistant)]
    )
    session.save()


def _assistant_rows(session):
    return [
        message for message in session.messages
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]


def _block_recovered_session_save(monkeypatch, recovered_text):
    """Hold the final recovered save so ownership can be observed in-flight."""
    original_save = Session.save
    save_entered = threading.Event()
    release_save = threading.Event()
    save_returned = threading.Event()

    def gated_save(session, *args, **kwargs):
        recovered = any(
            isinstance(row, dict)
            and row.get("_recovered_from_run_journal") is True
            and recovered_text in str(row.get("content") or "")
            for row in getattr(session, "messages", [])
        ) or any(
            isinstance(tool, dict)
            and tool.get("_recovered_from_run_journal") is True
            and recovered_text in {
                tool.get("preview"),
                tool.get("summary"),
            }
            for tool in getattr(session, "tool_calls", [])
        )
        if recovered:
            save_entered.set()
            assert release_save.wait(timeout=5), "recovered Session.save was not released"
            try:
                return original_save(session, *args, **kwargs)
            finally:
                save_returned.set()
        return original_save(session, *args, **kwargs)

    monkeypatch.setattr(Session, "save", gated_save)
    return save_entered, release_save, save_returned


def test_empty_live_buffers_recover_journal_prose_before_cancel_marker():
    sid = "issue6920_prose"
    stream_id = "stream-prose"
    _session(sid, stream_id)
    _start_cancel_state(sid, stream_id)
    RunJournalWriter(sid, stream_id).append_sse_event(
        "token", {"text": "Visible work survived in the journal."},
    )

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    assistant = _assistant_rows(reloaded)
    partial = [row for row in assistant if row.get("_partial")]
    assert [row.get("content") for row in partial] == [
        "Visible work survived in the journal.",
    ]
    marker_index = next(index for index, row in enumerate(assistant) if row.get("_error"))
    partial_index = next(
        index for index, row in enumerate(assistant) if row.get("_partial")
    )
    assert partial_index < marker_index
    assert marker_index == len(assistant) - 1

    context = _context_messages_for_new_turn(reloaded, "What happened?")
    assert any(
        row.get("content") == "Visible work survived in the journal."
        for row in context
    )


def test_empty_live_buffers_recover_reasoning_only_without_context_leak():
    sid = "issue6920_reasoning"
    stream_id = "stream-reasoning"
    _session(sid, stream_id)
    _start_cancel_state(sid, stream_id)
    RunJournalWriter(sid, stream_id).append_sse_event(
        "reasoning", {"text": "Display-only reasoning from the cancelled turn."},
    )

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    partial = [row for row in _assistant_rows(reloaded) if row.get("_partial")]
    assert len(partial) == 1
    assert partial[0].get("content") == ""
    assert partial[0].get("reasoning") == (
        "Display-only reasoning from the cancelled turn."
    )
    assert "Display-only reasoning" not in json.dumps(
        reloaded.context_messages, ensure_ascii=False,
    )


def test_empty_live_buffers_recover_tool_round_boundary_and_seal_completion():
    sid = "issue6920_tool"
    stream_id = "stream-tool"
    _session(sid, stream_id)
    _start_cancel_state(sid, stream_id)
    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event(
        "tool",
        {
            "name": "terminal",
            "preview": "printf recovered",
            "args": {"command": "printf recovered"},
        },
    )
    writer.append_sse_event(
        "tool_complete",
        {"name": "terminal", "duration": 0.25, "is_error": False},
    )

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    partial = [row for row in _assistant_rows(reloaded) if row.get("_partial")]
    assert len(partial) == 1
    assert partial[0].get("content") == ""
    assert len(reloaded.tool_calls) == 1
    assert reloaded.tool_calls[0]["name"] == "terminal"
    assert reloaded.tool_calls[0]["done"] is True
    assert reloaded.tool_calls[0]["preview"] == "printf recovered"
    assert reloaded.tool_calls[0]["_recovered_stream_id"] == stream_id


def test_multi_round_journal_fallback_keeps_prose_tool_data_and_order():
    sid = "issue6920_multi_round"
    stream_id = "stream-multi-round"
    _session(sid, stream_id)
    _start_cancel_state(sid, stream_id)
    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event("token", {"text": "First visible work."})
    writer.append_sse_event(
        "tool",
        {
            "name": "terminal",
            "preview": "printf first",
            "args": {"command": "printf first"},
        },
    )
    writer.append_sse_event(
        "tool_complete",
        {"name": "terminal", "preview": "first result", "duration": 0.5},
    )
    writer.append_sse_event(
        "interim_assistant", {"text": "A second visible work row."},
    )
    writer.append_sse_event("token", {"text": "Final visible work."})

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    assistant = _assistant_rows(reloaded)
    partial = [row for row in assistant if row.get("_partial")]
    assert [row.get("content") for row in partial] == [
        "First visible work.",
        "A second visible work row.",
        "Final visible work.",
    ]
    assert len(reloaded.tool_calls) == 1
    assert reloaded.tool_calls[0]["name"] == "terminal"
    assert reloaded.tool_calls[0]["preview"] == "first result"
    assert reloaded.tool_calls[0]["args"] == {"command": "printf first"}
    assert reloaded.tool_calls[0]["done"] is True
    assert reloaded.tool_calls[0]["_partial"] is True
    marker_index = next(index for index, row in enumerate(assistant) if row.get("_error"))
    assert marker_index == len(assistant) - 1


def test_live_partial_buffer_wins_without_journal_duplicate():
    sid = "issue6920_live_priority"
    stream_id = "stream-live-priority"
    session = _session(sid, stream_id)
    _start_cancel_state(sid, stream_id)
    config.STREAM_PARTIAL_TEXT[stream_id] = "Live buffer is authoritative."
    RunJournalWriter(sid, stream_id).append_sse_event(
        "token", {"text": "Journal copy must not be appended."},
    )

    with patch("api.models._append_journaled_partial_output") as fallback:
        assert cancel_stream(stream_id) is True

    fallback.assert_not_called()
    partial = [row for row in _assistant_rows(session) if row.get("_partial")]
    assert [row.get("content") for row in partial] == [
        "Live buffer is authoritative.",
    ]


def test_foreign_and_malformed_journal_fail_soft_to_cancel_marker():
    sid = "issue6920_fail_soft"
    stream_id = "stream-current"
    session = _session(sid, stream_id)
    _start_cancel_state(sid, stream_id)
    with patch(
        "api.run_journal.read_run_events",
        return_value={
            "events": [
                {
                    "event": "token",
                    "type": "token",
                    "seq": 1,
                    "session_id": "foreign-session",
                    "run_id": "foreign-stream",
                    "event_id": "foreign-stream:1",
                    "payload": {"text": "Foreign run must be ignored."},
                },
                {"event": "token", "payload": "not-a-dict"},
            ],
        },
    ):
        assert cancel_stream(stream_id) is True

    assert not [row for row in _assistant_rows(session) if row.get("_partial")]
    assert len([row for row in _assistant_rows(session) if row.get("_error")]) == 1


def test_repeated_journal_fallback_dedupes_rows_and_does_not_mark_history_partial():
    sid = "issue6920_dedupe"
    stream_id = "stream-dedupe"
    session = _session(sid, stream_id)
    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event("token", {"text": "One durable partial."})
    _start_cancel_state(sid, stream_id)

    assert cancel_stream(stream_id) is True
    first_partial_count = len(
        [row for row in _assistant_rows(session) if row.get("_partial")]
    )
    assert first_partial_count == 1

    # The helper is intentionally idempotent when the same journal is seen
    # again; its default call must not retroactively mark existing history.
    from api.models import _append_journaled_partial_output

    assert _append_journaled_partial_output(
        session, stream_id, dedupe_existing=True, mark_partial=True,
    ) is False
    partial_rows = [row for row in _assistant_rows(session) if row.get("_partial")]
    assert len(partial_rows) == first_partial_count
    assert partial_rows[0]["content"] == "One durable partial."


def test_cancel_journal_dedupe_does_not_claim_same_text_from_an_older_turn():
    """Current cancelled work must stay after its owning user turn."""
    sid = "issue6920_turn_scoped_dedupe"
    stream_id = "stream-turn-scoped-dedupe"
    session = _session(sid, stream_id)
    session.messages = [
        {
            "role": "user",
            "content": "Earlier question",
            "timestamp": 1,
        },
        {
            "role": "assistant",
            "content": "Repeated answer",
            "timestamp": 2,
        },
    ]
    session.context_messages = [dict(message) for message in session.messages]
    session.pending_started_at = 10
    session.save()
    _start_cancel_state(sid, stream_id)
    RunJournalWriter(sid, stream_id).append_sse_event(
        "token", {"text": "Repeated answer"},
    )

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    assert reloaded.messages[1] == {
        "role": "assistant",
        "content": "Repeated answer",
        "timestamp": 2,
    }
    current_user_index = next(
        index
        for index, message in enumerate(reloaded.messages)
        if message.get("role") == "user"
        and message.get("content") == "Continue this cancelled turn"
    )
    current_partial_index = next(
        index
        for index, message in enumerate(reloaded.messages)
        if message.get("role") == "assistant"
        and message.get("content") == "Repeated answer"
        and message.get("_partial") is True
    )
    marker_index = next(
        index
        for index, message in enumerate(reloaded.messages)
        if message.get("role") == "assistant" and message.get("_error") is True
    )
    assert current_user_index < current_partial_index < marker_index
    assert marker_index == len(reloaded.messages) - 1


def test_cancel_journal_tool_dedupe_is_turn_scoped():
    """A repeated tool in an older turn must not suppress current recovery."""
    sid = "issue6920_tool_turn_scope"
    stream_id = "stream-tool-turn-scope"
    session = _session(sid, stream_id)
    session.messages = [
        {
            "role": "user",
            "content": "Earlier question",
            "timestamp": 1,
        },
        {
            "role": "assistant",
            "content": "Earlier answer",
            "timestamp": 2,
        },
    ]
    session.context_messages = [dict(message) for message in session.messages]
    session.tool_calls = [
        {
            "name": "terminal",
            "preview": "ls",
            "snippet": "ls",
            "tid": "old-tool",
            "assistant_msg_idx": 1,
            "done": True,
        }
    ]
    session.pending_started_at = 10
    session.save()
    _start_cancel_state(sid, stream_id)
    RunJournalWriter(sid, stream_id).append_sse_event(
        "tool",
        {
            "name": "terminal",
            "preview": "ls",
            "args": {"cmd": "ls"},
        },
    )

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    current_user_index = next(
        index
        for index, message in enumerate(reloaded.messages)
        if message.get("role") == "user"
        and message.get("content") == "Continue this cancelled turn"
    )
    current_assistant_index = next(
        index
        for index, message in enumerate(reloaded.messages)
        if message.get("role") == "assistant"
        and message.get("_recovered_stream_id") == stream_id
    )
    current_tools = [
        tool_call
        for tool_call in reloaded.tool_calls
        if tool_call.get("_recovered_stream_id") == stream_id
    ]
    assert len(current_tools) == 1
    assert current_tools[0]["assistant_msg_idx"] == current_assistant_index
    assert current_user_index < current_assistant_index
    assert reloaded.messages[current_assistant_index].get("_error") is not True
    assert len(reloaded.tool_calls) == 2
    assert reloaded.tool_calls[0].get("_recovered_stream_id") is None
    assert reloaded.tool_calls[0]["assistant_msg_idx"] == 1

    # Re-reading the same recovered stream remains idempotent.
    from api.models import _append_journaled_partial_output

    assert _append_journaled_partial_output(
        reloaded,
        stream_id,
        dedupe_existing=True,
        mark_partial=True,
        current_turn_start=current_user_index,
    ) is False
    assert len(
        [
            tool_call
            for tool_call in reloaded.tool_calls
            if tool_call.get("_recovered_stream_id") == stream_id
        ]
    ) == 1

    # Callers without the cancellation-only lower bound retain the legacy
    # session-wide match for an untagged core-transcript tool card.
    legacy_session = Session(
        session_id="issue6920_tool_legacy",
        messages=[{"role": "assistant", "content": "Earlier answer"}],
        tool_calls=[{"name": "terminal", "preview": "ls", "snippet": "ls"}],
    )
    assert models._journal_tool_already_present(
        legacy_session,
        "terminal",
        "ls",
        stream_id="legacy-stream",
    ) is True


def test_cancel_journal_tool_dedupe_rejects_invalid_owner_rows():
    """Malformed or marker owners must not suppress a current-turn tool."""
    session = Session(
        session_id="issue6920_tool_invalid_owner",
        messages=[
            {"role": "user", "content": "Current request"},
            {"role": "assistant", "content": "Current progress"},
            {
                "role": "assistant",
                "content": "Task cancelled.",
                "_error": True,
                "type": "interrupted",
            },
        ],
    )
    for owner_index in (999, 2):
        session.tool_calls = [
            {
                "name": "terminal",
                "preview": "ls",
                "assistant_msg_idx": owner_index,
            }
        ]
        assert models._journal_tool_already_present(
            session,
            "terminal",
            "ls",
            stream_id="current-stream",
            min_index=1,
        ) is False

    session.tool_calls = [
        {
            "name": "terminal",
            "preview": "ls",
            "assistant_msg_idx": 1,
        }
    ]
    assert models._journal_tool_already_present(
        session,
        "terminal",
        "ls",
        stream_id="current-stream",
        min_index=1,
    ) is True


def test_recovered_journal_segments_bypass_legacy_partial_collapse():
    """Distinct recovered rows survive the save/load partial cleanup pass."""
    sid = "issue6920_recovered_partial_collapse"
    stream_id = "stream-recovered-partials"
    session = Session(
        session_id=sid,
        messages=[
            {"role": "user", "content": "Continue this cancelled turn", "timestamp": 1},
            {
                "role": "assistant",
                "content": "Repeated recovered progress.",
                "timestamp": 2,
                "_partial": True,
                "_recovered_from_run_journal": True,
                "_recovered_stream_id": stream_id,
            },
            {
                "role": "assistant",
                "content": "Repeated recovered progress.",
                "timestamp": 3,
                "_partial": True,
                "_recovered_from_run_journal": True,
                "_recovered_stream_id": stream_id,
            },
            {
                "role": "assistant",
                "content": "Task cancelled.",
                "timestamp": 4,
                "_error": True,
            },
        ],
        tool_calls=[
            {
                "name": "terminal",
                "preview": "ls",
                "snippet": "ls",
                "tid": "journal-tool-1",
                "assistant_msg_idx": 2,
                "_partial": True,
                "_recovered_from_run_journal": True,
                "_recovered_stream_id": stream_id,
            },
        ],
    )
    session.save()

    reloaded = Session.load(sid)
    assert reloaded is not None
    recovered = [
        row for row in _assistant_rows(reloaded)
        if row.get("_recovered_from_run_journal")
    ]
    assert [row.get("content") for row in recovered] == [
        "Repeated recovered progress.",
        "Repeated recovered progress.",
    ]
    assert len(recovered) == 2
    assert len(reloaded.messages) == 4
    assert reloaded.tool_calls[0]["assistant_msg_idx"] == 2
    assert reloaded.messages[reloaded.tool_calls[0]["assistant_msg_idx"]].get(
        "_recovered_stream_id"
    ) == stream_id
    assert reloaded.messages[reloaded.tool_calls[0]["assistant_msg_idx"]].get(
        "_error"
    ) is not True

    legacy = Session(
        session_id="issue6920_legacy_partial_collapse",
        messages=[
            {"role": "assistant", "content": "Legacy duplicate.", "_partial": True},
            {"role": "assistant", "content": "Legacy duplicate.", "_partial": True},
        ],
    )
    legacy.save()
    legacy_reloaded = Session.load(legacy.session_id)
    assert legacy_reloaded is not None
    assert [
        row for row in _assistant_rows(legacy_reloaded) if row.get("_partial")
    ] == [
        {"role": "assistant", "content": "Legacy duplicate.", "_partial": True},
    ]


def test_cancel_journal_context_includes_owning_user():
    """Recovered assistant progress keeps its cancelled user turn in context."""
    sid = "issue6920_context_owner"
    stream_id = "stream-context-owner"
    session = _session(sid, stream_id)
    session.messages = [
        {
            "role": "user",
            "content": "Earlier question",
            "timestamp": 1,
        },
        {
            "role": "assistant",
            "content": "Earlier answer",
            "timestamp": 2,
        },
    ]
    session.context_messages = [dict(message) for message in session.messages]
    session.pending_started_at = 10
    session.save()
    _start_cancel_state(sid, stream_id)
    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event(
        "token", {"text": "Recovered assistant progress."},
    )
    writer.append_sse_event(
        "reasoning", {"text": "Display-only reasoning must stay hidden."},
    )

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    context = reloaded.context_messages
    current_user_indexes = [
        index
        for index, message in enumerate(context)
        if message.get("role") == "user"
        and message.get("content") == "Continue this cancelled turn"
    ]
    recovered_indexes = [
        index
        for index, message in enumerate(context)
        if message.get("role") == "assistant"
        and message.get("content") == "Recovered assistant progress."
    ]
    assert len(current_user_indexes) == 1
    assert len(recovered_indexes) == 1
    assert recovered_indexes[0] == current_user_indexes[0] + 1
    assert "Display-only reasoning" not in json.dumps(
        context, ensure_ascii=False,
    )
    assert reloaded.pending_user_message is None
    assert reloaded.pending_attachments == []
    assert reloaded.pending_started_at is None


def test_btw_cancel_recovery_never_mutates_parent_history():
    """A shallow-copied /btw transcript must isolate cancel recovery rows."""
    parent = Session(
        session_id="issue6920_btw_parent",
        title="Parent",
        messages=[
            {"role": "user", "content": "Earlier question", "timestamp": 1},
            {
                "role": "assistant",
                "content": "Repeated parent answer",
                "timestamp": 2,
            },
        ],
        context_messages=[],
    )
    parent.save()
    parent_before = copy.deepcopy(parent.messages)

    stream_id = "stream-btw-parent-isolation"
    ephemeral = Session(
        session_id="issue6920_btw_ephemeral",
        title="btw: side question",
        messages=list(parent.messages),
        context_messages=[],
        parent_session_id=parent.session_id,
        session_source="btw",
    )
    writer = RunJournalWriter(ephemeral.session_id, stream_id)
    writer.append_sse_event("token", {"text": "Repeated parent answer"})
    writer.append_sse_event(
        "reasoning", {"text": "New side-question reasoning"},
    )

    from api.models import _append_journaled_partial_output

    assert _append_journaled_partial_output(
        ephemeral,
        stream_id,
        dedupe_existing=True,
        mark_partial=True,
    ) is True
    assert parent.messages == parent_before
    assert len(ephemeral.messages) == len(parent_before) + 1
    recovered = ephemeral.messages[-1]
    assert recovered["content"] == "Repeated parent answer"
    assert recovered["reasoning"] == "New side-question reasoning"
    assert recovered["_partial"] is True
    assert recovered["_recovered_stream_id"] == stream_id

    assert _append_journaled_partial_output(
        ephemeral,
        stream_id,
        dedupe_existing=True,
        mark_partial=True,
    ) is False
    assert parent.messages == parent_before
    assert len(ephemeral.messages) == len(parent_before) + 1


def test_live_buffer_cancel_partial_is_next_turn_context():
    """A live-buffer partial must be durable in provider context after cancel."""
    sid = "issue6920_live_buffer_context"
    stream_id = "stream-live-buffer-context"
    session = _session(sid, stream_id)
    session.messages = [
        {"role": "user", "content": "Earlier question", "timestamp": 1},
        {"role": "assistant", "content": "Earlier answer", "timestamp": 2},
    ]
    session.context_messages = [dict(message) for message in session.messages]
    session.pending_started_at = 10
    session.save()
    _start_cancel_state(sid, stream_id)
    config.STREAM_PARTIAL_TEXT[stream_id] = "Live partial survives cancellation."
    config.STREAM_REASONING_TEXT[stream_id] = (
        "Display-only live reasoning must stay out of provider context."
    )

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    current_user = "Continue this cancelled turn"
    partial_text = "Live partial survives cancellation."
    message_user_indexes = [
        idx for idx, message in enumerate(reloaded.messages)
        if message.get("role") == "user" and message.get("content") == current_user
    ]
    message_partial_indexes = [
        idx for idx, message in enumerate(reloaded.messages)
        if message.get("role") == "assistant"
        and message.get("_partial") is True
        and message.get("content") == partial_text
    ]
    context_user_indexes = [
        idx for idx, message in enumerate(reloaded.context_messages)
        if message.get("role") == "user" and message.get("content") == current_user
    ]
    context_partial_indexes = [
        idx for idx, message in enumerate(reloaded.context_messages)
        if message.get("role") == "assistant"
        and message.get("_partial") is True
        and message.get("content") == partial_text
    ]
    assert len(message_user_indexes) == 1
    assert len(message_partial_indexes) == 1
    assert message_user_indexes[0] + 1 == message_partial_indexes[0]
    assert len(context_user_indexes) == 1
    assert len(context_partial_indexes) == 1
    assert context_user_indexes[0] + 1 == context_partial_indexes[0]
    assert reloaded.pending_user_message is None
    assert reloaded.pending_attachments == []
    assert reloaded.pending_started_at is None
    next_context = _context_messages_for_new_turn(reloaded, "Next turn")
    next_user_indexes = [
        idx for idx, message in enumerate(next_context)
        if message.get("role") == "user" and message.get("content") == current_user
    ]
    next_partial_indexes = [
        idx for idx, message in enumerate(next_context)
        if message.get("role") == "assistant"
        and message.get("_partial") is True
        and message.get("content") == partial_text
    ]
    assert len(next_user_indexes) == 1
    assert len(next_partial_indexes) == 1
    assert next_user_indexes[0] + 1 == next_partial_indexes[0]
    assert next_context[next_user_indexes[0]]["content"] == current_user
    assert next_context[next_partial_indexes[0]]["content"] == partial_text
    assert "Display-only live reasoning" not in json.dumps(
        reloaded.context_messages, ensure_ascii=False,
    )
    assert "Display-only live reasoning" not in json.dumps(
        next_context, ensure_ascii=False,
    )


def test_repeated_journal_prose_is_turn_scoped_in_context():
    """Equal recovered prose in an older turn cannot own the current segment."""
    sid = "issue6920_repeated_journal_context"
    stream_id = "stream-repeated-journal-context"
    repeated_text = "The same journal prose appears in two turns."
    session = _session(sid, stream_id)
    session.messages = [
        {"role": "user", "content": "Earlier question", "timestamp": 1},
        {"role": "assistant", "content": repeated_text, "timestamp": 2},
    ]
    session.context_messages = [dict(message) for message in session.messages]
    session.pending_started_at = 10
    session.save()
    _start_cancel_state(sid, stream_id)
    RunJournalWriter(sid, stream_id).append_sse_event(
        "token", {"text": repeated_text},
    )

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    current_message_user_index = next(
        idx for idx, message in enumerate(reloaded.messages)
        if message.get("role") == "user"
        and message.get("content") == "Continue this cancelled turn"
    )
    current_message_rows = [
        (idx, message) for idx, message in enumerate(reloaded.messages)
        if message.get("role") == "assistant"
        and message.get("content") == repeated_text
        and message.get("_recovered_from_run_journal") is True
    ]
    assert len(current_message_rows) == 1
    assert current_message_rows[0][0] == current_message_user_index + 1
    current_user_index = next(
        idx for idx, message in enumerate(reloaded.context_messages)
        if message.get("role") == "user"
        and message.get("content") == "Continue this cancelled turn"
    )
    recovered_rows = [
        (idx, message) for idx, message in enumerate(reloaded.context_messages)
        if message.get("role") == "assistant"
        and message.get("content") == repeated_text
    ]
    assert len(recovered_rows) == 2
    assert recovered_rows[0][0] < current_user_index < recovered_rows[1][0]
    assert recovered_rows[1][0] == current_user_index + 1
    next_context = _context_messages_for_new_turn(reloaded, "Next turn")
    next_user_indexes = [
        (idx, message) for idx, message in enumerate(next_context)
        if message.get("role") == "user"
        and message.get("content") == "Continue this cancelled turn"
    ]
    next_rows = [
        (idx, message) for idx, message in enumerate(next_context)
        if message.get("role") == "assistant"
        and message.get("content") == repeated_text
    ]
    assert len(next_user_indexes) == 1
    assert len(next_rows) == 2
    assert next_rows[1][0] == next_user_indexes[0][0] + 1
    assert next_rows[1][1].get("_recovered_from_run_journal") is True
    from api.models import _append_journaled_partial_output

    assert _append_journaled_partial_output(
        reloaded,
        stream_id,
        dedupe_existing=True,
        mark_partial=True,
        current_turn_start=current_message_user_index,
    ) is False
    assert len([
        message for message in reloaded.messages
        if message.get("role") == "assistant"
        and message.get("content") == repeated_text
        and message.get("_recovered_from_run_journal") is True
    ]) == 1


def test_settlement_preserves_identity_distinct_recovered_segments():
    """Successful settlement keeps equal-text recovered segments by identity."""
    sid = "issue6920_settlement_segments"
    repeated_text = "Recovered progress is intentionally repeated."
    recovered_one = {
        "role": "assistant",
        "content": repeated_text,
        "timestamp": 3,
        "_partial": True,
        "_recovered_from_run_journal": True,
        "_recovered_stream_id": "stream-settlement-segments",
        "_recovered_event_id": "stream-settlement-segments:7",
    }
    recovered_two = {
        **recovered_one,
        "timestamp": 4,
        "_recovered_event_id": "stream-settlement-segments:9",
    }
    previous_messages = [
        {"role": "user", "content": "Earlier question", "timestamp": 1},
        {"role": "assistant", "content": "Earlier answer", "timestamp": 2},
        {"role": "user", "content": "Continue this cancelled turn", "timestamp": 3},
        recovered_one,
        recovered_two,
    ]
    previous_context = copy.deepcopy(previous_messages)
    session = Session(
        session_id=sid,
        title="Settlement segments",
        messages=copy.deepcopy(previous_messages),
        context_messages=copy.deepcopy(previous_context),
    )
    next_user = {"role": "user", "content": "Continue after cancellation", "timestamp": 5}
    sanitized_history = streaming._sanitize_messages_for_agent(previous_context)
    assert all(
        "_recovered_event_id" not in message
        and "_recovered_from_run_journal" not in message
        for message in sanitized_history
    )
    result_messages = sanitized_history + [
        next_user,
        {"role": "assistant", "content": "The next turn is complete."},
    ]

    streaming._settle_result_messages(
        session,
        previous_messages,
        previous_context,
        result_messages,
        next_user["content"],
        "webui",
        None,
    )
    session.save()
    reloaded = Session.load(sid)
    assert reloaded is not None
    recovered_context = [
        message for message in reloaded.context_messages
        if message.get("_recovered_from_run_journal")
    ]
    assert [message.get("_recovered_event_id") for message in recovered_context] == [
        "stream-settlement-segments:7",
        "stream-settlement-segments:9",
    ]
    assert [message.get("content") for message in recovered_context] == [
        repeated_text,
        repeated_text,
    ]
    recovered = [
        message for message in reloaded.messages
        if message.get("_recovered_from_run_journal")
    ]
    assert [message.get("_recovered_event_id") for message in recovered] == [
        "stream-settlement-segments:7",
        "stream-settlement-segments:9",
    ]
    assert [message.get("content") for message in recovered] == [
        repeated_text,
        repeated_text,
    ]
    current_user_index = next(
        idx for idx, message in enumerate(reloaded.messages)
        if message.get("role") == "user"
        and message.get("content") == "Continue this cancelled turn"
    )
    recovered_indexes = [
        idx for idx, message in enumerate(reloaded.messages)
        if message.get("_recovered_from_run_journal")
    ]
    assert recovered_indexes == [current_user_index + 1, current_user_index + 2]
    assert any(
        message.get("role") == "assistant"
        and message.get("content") == "The next turn is complete."
        for message in reloaded.messages
    )


def test_settlement_preserves_recovered_tool_owner_and_summary():
    """Tool-summary rebuild retains a recovered tool and its assistant owner."""
    sid = "issue6920_settlement_tool"
    stream_id = "stream-settlement-tool"
    recovered_assistant = {
        "role": "assistant",
        "content": "Recovered tool activity.",
        "timestamp": 3,
        "_partial": True,
        "_recovered_from_run_journal": True,
        "_recovered_stream_id": stream_id,
        "_recovered_event_id": f"{stream_id}:4",
    }
    recovered_tool = {
        "name": "terminal",
        "preview": "completed output",
        "snippet": "completed output",
        "summary": "completed output",
        "tid": f"{stream_id}:5",
        "assistant_msg_idx": 3,
        "done": True,
        "_recovered_from_run_journal": True,
        "_recovered_stream_id": stream_id,
        "_recovered_event_id": f"{stream_id}:5",
    }
    previous_messages = [
        {"role": "user", "content": "Earlier question", "timestamp": 1},
        {"role": "assistant", "content": "Earlier answer", "timestamp": 2},
        {"role": "user", "content": "Continue this cancelled turn", "timestamp": 3},
        recovered_assistant,
    ]
    previous_context = copy.deepcopy(previous_messages)
    session = Session(
        session_id=sid,
        title="Settlement tool",
        messages=copy.deepcopy(previous_messages),
        context_messages=copy.deepcopy(previous_context),
        tool_calls=[recovered_tool],
    )
    next_user = {"role": "user", "content": "Continue after cancellation", "timestamp": 5}
    streaming._settle_result_messages(
        session,
        previous_messages,
        previous_context,
        copy.deepcopy(previous_context) + [
            next_user,
            {"role": "assistant", "content": "The next turn is complete."},
        ],
        next_user["content"],
        "webui",
        None,
    )
    # Mirror the normal successful-turn tool-summary rebuild in the worker.
    session.tool_calls = streaming._extract_tool_calls_from_messages(
        session.messages,
        live_tool_calls=session.tool_calls,
    )
    session.save()
    reloaded = Session.load(sid)
    assert reloaded is not None
    recovered_tools = [
        tool for tool in reloaded.tool_calls
        if tool.get("_recovered_event_id") == f"{stream_id}:5"
    ]
    assert len(recovered_tools) == 1
    assert recovered_tools[0].get("summary") == "completed output"
    owner_index = recovered_tools[0].get("assistant_msg_idx")
    assert isinstance(owner_index, int)
    assert reloaded.messages[owner_index].get("_recovered_event_id") == f"{stream_id}:4"
    assert reloaded.messages[owner_index].get("_recovered_from_run_journal") is True


def test_settlement_preserves_late_recovery_before_media_snapshot_annotation(monkeypatch):
    """Annotate the finalized display after late recovery and before compaction."""
    sid = "issue6920_settlement_media_annotation"
    old_stream_id = "stream-settlement-media-old"
    late_assistant_event_id = f"{old_stream_id}:assistant-7"
    late_tool_event_id = f"{old_stream_id}:tool-8"
    late_text = "Late recovered cancellation prose."
    cancelled_prompt = "Continue this cancelled turn"
    successor_prompt = "Continue after cancellation"
    old_user = {
        "role": "user",
        "content": cancelled_prompt,
        "timestamp": 1,
    }
    late_assistant = {
        "role": "assistant",
        "content": late_text,
        "timestamp": 3,
        "_partial": True,
        "_recovered_from_run_journal": True,
        "_recovered_stream_id": old_stream_id,
        "_recovered_event_id": late_assistant_event_id,
    }
    cancel_marker = {
        "role": "assistant",
        "content": "**Task cancelled:** provider stopped.\n\n"
        "*The run was cancelled by the user before Hermes finished. No provider failure occurred.*",
        "_error": True,
        "provider_details": "provider stopped",
        "timestamp": 4,
    }
    successor_user = {
        "role": "user",
        "content": successor_prompt,
        "timestamp": 5,
    }
    successor_assistant = {
        "role": "assistant",
        "content": "Successor answer",
        "tool_calls": [{
            "id": "successor-tool-id",
            "type": "function",
            "function": {
                "name": "successor_tool",
                "arguments": '{"command": "printf successor"}',
            },
        }],
        "timestamp": 6,
    }
    successor_tool_message = {
        "role": "tool",
        "tool_call_id": "successor-tool-id",
        "content": "successor result",
        "timestamp": 7,
    }
    canonical_messages = [
        old_user,
        late_assistant,
        cancel_marker,
        successor_user,
        successor_assistant,
        successor_tool_message,
    ]
    late_tool = {
        "name": "terminal",
        "preview": "late tool output",
        "snippet": "late tool output",
        "summary": "late tool output",
        "tid": late_tool_event_id,
        "assistant_msg_idx": 1,
        "done": True,
        "_recovered_from_run_journal": True,
        "_recovered_stream_id": old_stream_id,
        "_recovered_event_id": late_tool_event_id,
        "_recovered_assistant_event_id": late_assistant_event_id,
    }
    successor_tool = {
        "name": "successor_tool",
        "preview": "successor result",
        "snippet": "successor result",
        "summary": "successor result",
        "tid": "successor-tool-id",
        "assistant_msg_idx": 4,
        "done": True,
    }
    session = Session(
        session_id=sid,
        title="Settlement media annotation",
        messages=copy.deepcopy(canonical_messages),
        context_messages=copy.deepcopy(canonical_messages),
        tool_calls=[copy.deepcopy(late_tool), copy.deepcopy(successor_tool)],
    )
    # The detached late reconciler's canonical state is durable while the
    # successor settlement owns the in-memory session object.
    session.save()
    previous_messages = copy.deepcopy([old_user, cancel_marker])
    previous_context = copy.deepcopy(previous_messages)
    result_messages = copy.deepcopy(previous_context) + [
        successor_user,
        successor_assistant,
        successor_tool_message,
    ]

    call_order = []
    preserve_original = streaming._preserve_late_recovered_rows_across_settlement

    def record_preservation(*args, **kwargs):
        result = preserve_original(*args, **kwargs)
        call_order.append("preserve")
        return result

    monkeypatch.setattr(
        streaming,
        "_preserve_late_recovered_rows_across_settlement",
        record_preservation,
    )

    def record_annotation(messages):
        call_order.append("annotate")
        assert messages is session.messages
        assert call_order == ["preserve", "annotate"]
        late_rows = [
            row
            for row in messages
            if row.get("_recovered_event_id") == late_assistant_event_id
        ]
        assert len(late_rows) == 1
        late_index = messages.index(late_rows[0])
        marker_index = next(
            index
            for index, row in enumerate(messages)
            if row.get("_error") is True and row.get("provider_details") == "provider stopped"
        )
        successor_user_index = next(
            index
            for index, row in enumerate(messages)
            if row.get("role") == "user" and row.get("content") == successor_prompt
        )
        assert late_index < marker_index < successor_user_index
        assert sum(row.get("content") == late_text for row in messages) == 1
        assert sum(row.get("content") == successor_prompt for row in messages) == 1
        assert any(
            row.get("role") == "assistant"
            and row.get("content") == "Successor answer"
            for row in messages
        )
        late_context_rows = [
            row
            for row in session.context_messages
            if row.get("_recovered_event_id") == late_assistant_event_id
        ]
        assert len(late_context_rows) == 1
        assert sum(
            row.get("role") == "user" and row.get("content") == successor_prompt
            for row in session.context_messages
        ) == 1
        assert sum(
            row.get("role") == "assistant" and row.get("content") == "Successor answer"
            for row in session.context_messages
        ) == 1

        recovered_tools = [
            tool for tool in session.tool_calls if tool.get("_recovered_event_id") == late_tool_event_id
        ]
        assert len(recovered_tools) == 1
        assert messages[recovered_tools[0]["assistant_msg_idx"]].get(
            "_recovered_event_id"
        ) == late_assistant_event_id
        successor_tools = [tool for tool in session.tool_calls if tool.get("name") == "successor_tool"]
        assert len(successor_tools) == 1
        assert messages[successor_tools[0]["assistant_msg_idx"]].get("content") == "Successor answer"

    # The pre-resolution branch has no production helper yet.  ``raising=False``
    # keeps this oracle behavioral: the recorder stays empty until settlement
    # actually invokes the helper, rather than failing during test setup.
    monkeypatch.setattr(
        streaming,
        "_annotate_media_snapshots_for_settled_messages",
        record_annotation,
        raising=False,
    )

    streaming._settle_result_messages(
        session,
        previous_messages,
        previous_context,
        result_messages,
        successor_prompt,
        "webui",
        None,
    )

    assert call_order == ["preserve", "annotate"]


def test_local_cancel_before_flag_registration_stops_worker(tmp_path, monkeypatch):
    """A cancel between queue capture and flag registration reaches the worker."""
    sid = "issue6920_preflag_local"
    stream_id = "stream-preflag-local"
    late_text = "Late local callback must not run after preflight cancel."
    session = _session(sid, stream_id)
    config.register_stream_owner(stream_id, sid)
    config.register_session_writeback_owner(sid, stream_id)
    config.STREAMS[stream_id] = queue.Queue()

    registration_entered = threading.Event()
    release_registration = threading.Event()
    provider_started = threading.Event()
    callback_started = threading.Event()
    register_original = streaming.register_stream_cancel_state

    def gated_registration(session_id, current_stream_id, cancel_event):
        registration_entered.set()
        assert release_registration.wait(timeout=5), "worker flag registration was not released"
        return register_original(session_id, current_stream_id, cancel_event)

    monkeypatch.setattr(streaming, "register_stream_cancel_state", gated_registration)

    class FakeAgent:
        def __init__(self, *, interim_assistant_callback=None, session_id=None, **_kwargs):
            provider_started.set()
            self.session_id = session_id or sid
            self.interim_assistant_callback = interim_assistant_callback
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self._last_error = None

        def run_conversation(self, **kwargs):
            callback_started.set()
            if self.interim_assistant_callback:
                self.interim_assistant_callback(late_text)
            return {
                "completed": True,
                "final_response": "",
                "messages": list(kwargs.get("conversation_history") or []),
            }

        def interrupt(self, _message):
            return None

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *args, **kwargs: (
        "test-model",
        "test-provider",
        None,
    ))
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config_for_profile_home", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(streaming, "_build_session_db_for_stream", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(streaming, "get_state_db_session_messages", _empty_state_db_messages)
    monkeypatch.setattr(streaming, "reconciled_state_db_messages_for_session", _empty_state_db_messages)
    monkeypatch.setattr(streaming, "warm_models_catalog_provenance_if_cold", lambda: None)

    errors = []

    def run_worker():
        try:
            streaming._run_agent_streaming(
                sid,
                session.pending_user_message,
                "test-model",
                str(tmp_path),
                stream_id,
                [],
                model_provider="test-provider",
            )
        except BaseException as exc:  # pragma: no cover - failure surface
            errors.append(exc)

    worker = threading.Thread(target=run_worker, daemon=True)
    worker.start()
    assert registration_entered.wait(timeout=5), "local worker did not reach pre-flag seam"

    assert streaming.cancel_stream(stream_id) is True
    assert worker.is_alive(), "cancel_stream must return before worker registration resumes"
    assert config.session_writeback_owner(sid) == stream_id

    release_registration.set()
    worker.join(timeout=10)
    assert not worker.is_alive(), "local worker did not finish after preflight cancellation"
    assert not errors
    assert not provider_started.is_set()
    assert not callback_started.is_set()
    assert (sid, stream_id) not in config.STREAM_CANCEL_GENERATIONS
    assert config.session_writeback_owner(sid) is None

    reloaded = Session.load(sid)
    assert reloaded is not None
    assert reloaded.active_stream_id is None
    assert reloaded.pending_user_message is None
    assert sum(row.get("_error") is True for row in reloaded.messages) == 1
    assert not any(late_text in str(row.get("content") or "") for row in reloaded.messages)


def test_gateway_cancel_before_flag_registration_stops_worker(tmp_path, monkeypatch):
    """Gateway observes the shared preflight cancellation barrier too."""
    import api.gateway_chat as gateway_chat

    sid = "issue6920_preflag_gateway"
    stream_id = "stream-preflag-gateway"
    late_text = "Late gateway callback must not run after preflight cancel."
    session = _session(sid, stream_id)
    config.register_stream_owner(stream_id, sid)
    config.register_session_writeback_owner(sid, stream_id)
    config.STREAMS[stream_id] = queue.Queue()

    registration_entered = threading.Event()
    release_registration = threading.Event()
    provider_started = threading.Event()
    callback_started = threading.Event()
    register_original = gateway_chat.register_stream_cancel_state

    def gated_registration(session_id, current_stream_id, cancel_event):
        registration_entered.set()
        assert release_registration.wait(timeout=5), "gateway worker flag registration was not released"
        return register_original(session_id, current_stream_id, cancel_event)

    monkeypatch.setattr(gateway_chat, "register_stream_cancel_state", gated_registration)
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(gateway_chat, "_gateway_base_url", lambda *_args, **_kwargs: "http://gateway.test")
    monkeypatch.setattr(gateway_chat, "_gateway_api_key", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(gateway_chat, "_gateway_use_runs_api_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(gateway_chat, "_gateway_reasoning_effort_for_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(config, "_main_model_request_overrides", lambda *_args, **_kwargs: {})

    def fake_runs_api(*_args, **kwargs):
        provider_started.set()
        callback_started.set()
        kwargs["put_gateway_event"]("token", {"text": late_text})
        return late_text, {"input_tokens": 1, "output_tokens": 1, "estimated_cost": 0}

    monkeypatch.setattr(gateway_chat, "_run_gateway_runs_api_streaming", fake_runs_api)
    errors = []

    def run_worker():
        try:
            gateway_chat._run_gateway_chat_streaming(
                sid,
                session.pending_user_message,
                "test-model",
                str(tmp_path),
                stream_id,
                [],
                model_provider="test-provider",
            )
        except BaseException as exc:  # pragma: no cover - failure surface
            errors.append(exc)

    worker = threading.Thread(target=run_worker, daemon=True)
    worker.start()
    assert registration_entered.wait(timeout=5), "gateway worker did not reach pre-flag seam"

    assert streaming.cancel_stream(stream_id) is True
    assert worker.is_alive(), "cancel_stream must return before gateway registration resumes"
    assert config.session_writeback_owner(sid) == stream_id

    release_registration.set()
    worker.join(timeout=10)
    assert not worker.is_alive(), "gateway worker did not finish after preflight cancellation"
    assert not errors
    assert not provider_started.is_set()
    assert not callback_started.is_set()
    assert (sid, stream_id) not in config.STREAM_CANCEL_GENERATIONS
    assert config.session_writeback_owner(sid) is None

    reloaded = Session.load(sid)
    assert reloaded is not None
    assert reloaded.active_stream_id is None
    assert reloaded.pending_user_message is None
    assert sum(row.get("_error") is True for row in reloaded.messages) == 1
    assert not any(late_text in str(row.get("content") or "") for row in reloaded.messages)


def test_local_cancel_finalizer_defers_until_cancel_records_user_boundary(tmp_path, monkeypatch):
    """A worker finalizer must not recover journal rows before cancel owns its user turn."""
    sid = "issue6920_finalizer_boundary_local"
    stream_id = "stream-finalizer-boundary-local"
    late_text = "Late output recovered after the finalizer race."
    session = _session(sid, stream_id)
    cancelled_prompt = session.pending_user_message
    config.register_stream_owner(stream_id, sid)
    config.register_session_writeback_owner(sid, stream_id)
    config.STREAMS[stream_id] = queue.Queue()

    callback_done = threading.Event()
    interrupt_entered = threading.Event()
    release_interrupt = threading.Event()
    cancel_returned = threading.Event()
    cancel_result = []
    errors = []

    class FakeAgent:
        def __init__(self, *, interim_assistant_callback=None, session_id=None, **_kwargs):
            self.session_id = session_id or sid
            self.interim_assistant_callback = interim_assistant_callback
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self._last_error = None

        def run_conversation(self, **kwargs):
            if self.interim_assistant_callback:
                self.interim_assistant_callback(late_text)
            callback_done.set()
            cancel_flag = config.CANCEL_FLAGS[stream_id]
            while not cancel_flag.wait(0.01):
                pass
            return {
                "completed": False,
                "final_response": "",
                "messages": list(kwargs.get("conversation_history") or []),
            }

        def interrupt(self, _message):
            interrupt_entered.set()
            release_interrupt.wait(timeout=5)

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *args, **kwargs: (
        "test-model",
        "test-provider",
        None,
    ))
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config_for_profile_home", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(streaming, "_build_session_db_for_stream", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(streaming, "get_state_db_session_messages", _empty_state_db_messages)
    monkeypatch.setattr(streaming, "reconciled_state_db_messages_for_session", _empty_state_db_messages)
    monkeypatch.setattr(streaming, "warm_models_catalog_provenance_if_cold", lambda: None)

    def run_worker():
        try:
            streaming._run_agent_streaming(
                sid,
                session.pending_user_message,
                "test-model",
                str(tmp_path),
                stream_id,
                [],
                model_provider="test-provider",
            )
        except BaseException as exc:  # pragma: no cover - failure surface
            errors.append(exc)

    worker = threading.Thread(target=run_worker, daemon=True)
    worker.start()
    assert callback_done.wait(timeout=5), "local worker did not publish its journal segment"

    def run_cancel():
        try:
            cancel_result.append(streaming.cancel_stream(stream_id))
        finally:
            cancel_returned.set()

    cancel_thread = threading.Thread(target=run_cancel, daemon=True)
    cancel_thread.start()
    assert interrupt_entered.wait(timeout=5), "cancel_stream did not reach the real agent interrupt"

    # The worker finalizer wins the session lock while cancel_stream is still
    # blocked in interrupt(). It must leave the exact generation pending rather
    # than recovering journal output without a cancelled-user boundary.
    worker.join(timeout=10)
    assert not worker.is_alive(), "local worker finalizer did not complete"
    assert not cancel_returned.is_set()
    assert not errors
    assert config.STREAM_CANCEL_GENERATIONS[(sid, stream_id)]["worker_done"] is True
    assert config.STREAM_CANCEL_GENERATIONS[(sid, stream_id)]["turn_start"] is None
    blocked = Session.load(sid)
    assert blocked is not None
    assert not any(row.get("content") == late_text for row in blocked.messages)
    assert not any(row.get("content") == cancelled_prompt for row in blocked.messages)

    release_interrupt.set()
    assert cancel_returned.wait(timeout=10), "cancel_stream did not finish after interrupt release"
    cancel_thread.join(timeout=1)
    assert cancel_result == [True]
    assert (sid, stream_id) not in config.STREAM_CANCEL_GENERATIONS
    assert config.session_writeback_owner(sid) is None

    reloaded = Session.load(sid)
    assert reloaded is not None
    user_index = next(
        index
        for index, row in enumerate(reloaded.messages)
        if row.get("role") == "user" and row.get("content") == cancelled_prompt
    )
    late_index = next(
        index
        for index, row in enumerate(reloaded.messages)
        if row.get("content") == late_text
        and row.get("_recovered_from_run_journal") is True
    )
    marker_index = next(
        index for index, row in enumerate(reloaded.messages) if row.get("_error") is True
    )
    assert user_index < late_index < marker_index
    assert sum(row.get("content") == late_text for row in reloaded.messages) == 1
    context = _context_messages_for_new_turn(reloaded, "What happened?")
    assert sum(row.get("content") == late_text for row in context) == 1


def test_local_cancel_finalizes_late_admitted_journal_after_empty_snapshot(tmp_path, monkeypatch):
    sid = "issue6920_late_empty_local"
    stream_id = "stream-late-empty-local"
    late_text = "Late local prose admitted before cancellation."
    session = _session(sid, stream_id)
    _seed_historical_duplicate_prompt(session)
    cancelled_prompt = session.pending_user_message
    successor_prompt = cancelled_prompt
    config.register_stream_owner(stream_id, sid)
    config.register_session_writeback_owner(sid, stream_id)
    config.STREAMS[stream_id] = queue.Queue()

    admitted = threading.Event()
    release_append = threading.Event()
    append_original = RunJournalWriter.append_sse_event

    def gated_append(writer, event, data):
        if event == "interim_assistant" and isinstance(data, dict) and data.get("text") == late_text:
            admitted.set()
            # Successor admission performs real session reload, route setup, and
            # provider-snapshot capture before this gate is released.  Keep the
            # handshake bounded below the outer test timeout, but do not let the
            # append expire while that expected setup is still in progress.
            assert release_append.wait(timeout=30), "late journal append was not released"
        return append_original(writer, event, data)

    monkeypatch.setattr(RunJournalWriter, "append_sse_event", gated_append)
    save_entered, release_save, save_returned = _block_recovered_session_save(
        monkeypatch,
        late_text,
    )
    successor_snapshot_ready = threading.Event()
    release_successor_settle = threading.Event()
    successor_finished = threading.Event()
    agent_run_count = {'value': 0}
    agent_run_lock = threading.Lock()
    successor_provider_history = []

    class FakeAgent:
        def __init__(
            self,
            *,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            clarify_callback=None,
            interim_assistant_callback=None,
            tool_start_callback=None,
            tool_complete_callback=None,
            status_callback=None,
            session_id=None,
            **_kwargs,
        ):
            self.session_id = session_id or sid
            self.stream_delta_callback = stream_delta_callback
            self.reasoning_callback = reasoning_callback
            self.tool_progress_callback = tool_progress_callback
            self.interim_assistant_callback = interim_assistant_callback
            self.tool_start_callback = tool_start_callback
            self.tool_complete_callback = tool_complete_callback
            self.status_callback = status_callback
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self._last_error = None

        def run_conversation(self, **kwargs):
            with agent_run_lock:
                agent_run_count['value'] += 1
                run_number = agent_run_count['value']
            if run_number == 1:
                self.interim_assistant_callback(late_text)
                return {
                    "completed": True,
                    "final_response": "",
                    "messages": list(kwargs.get("conversation_history") or []),
                }
            history = copy.deepcopy(kwargs.get("conversation_history") or [])
            successor_provider_history.append(history)
            successor_snapshot_ready.set()
            assert release_successor_settle.wait(timeout=10), (
                "successor provider did not reach its settlement gate"
            )
            tool_id = "successor-tool-id"
            tool_name = "local_empty_tool"
            tool_args = {"command": "printf successor"}
            if callable(self.tool_start_callback):
                self.tool_start_callback(tool_id, tool_name, tool_args)
            if callable(self.tool_complete_callback):
                self.tool_complete_callback(
                    tool_id,
                    tool_name,
                    tool_args,
                    "local successor result",
                )
            successor_finished.set()
            successor_user = {
                "role": "user",
                "content": kwargs.get("persist_user_message") or successor_prompt,
            }
            return {
                "completed": True,
                "final_response": "local_empty_tool successor answer",
                "messages": history + [
                    successor_user,
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": tool_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(tool_args),
                            },
                        }],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": "local successor result",
                    },
                    {
                        "role": "assistant",
                        "content": "local_empty_tool successor answer",
                    },
                ],
            }

        def interrupt(self, _message):
            return None

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *args, **kwargs: (
        "test-model",
        "test-provider",
        None,
    ))
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config_for_profile_home", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(streaming, "_build_session_db_for_stream", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(streaming, "get_state_db_session_messages", _empty_state_db_messages)

    monkeypatch.setattr(
        streaming,
        "reconciled_state_db_messages_for_session",
        _session_state_db_messages,
    )
    monkeypatch.setattr(streaming, "warm_models_catalog_provenance_if_cold", lambda: None)

    errors = []
    worker_finished = threading.Event()

    # Hosted required jobs may lack the context-local Hermes-home override even
    # though local runs have it. Pin this schedule to the existing dynamic
    # production path so neither worker can deadlock on the legacy full-turn
    # module-patch lock while the old journal append is gated below.
    import api.profiles as profiles_api

    dynamic_override_homes = []
    dynamic_capability_homes = []

    def _install_dynamic_home_override(profile_home):
        dynamic_override_homes.append(str(profile_home))
        return object(), object(), True

    def _reset_dynamic_home_override(_override_mod, _override_token, _installed):
        return None

    def _dynamic_skill_home_capability(profile_home):
        dynamic_capability_homes.append(str(profile_home))
        return True

    class _LegacySkillHomeLock:
        def __init__(self):
            self.acquire_calls = 0

        def acquire(self, *args, **kwargs):
            self.acquire_calls += 1
            raise AssertionError(
                "target workers entered the legacy full-turn skill-home lock"
            )

        def release(self):
            return None

    legacy_skill_home_lock = _LegacySkillHomeLock()
    monkeypatch.setattr(
        streaming,
        "_set_streaming_hermes_home_override",
        _install_dynamic_home_override,
    )
    monkeypatch.setattr(
        streaming,
        "_reset_streaming_hermes_home_override",
        _reset_dynamic_home_override,
    )
    monkeypatch.setattr(
        profiles_api,
        "_skill_modules_support_profile_home",
        _dynamic_skill_home_capability,
    )
    monkeypatch.setattr(
        profiles_api,
        "_SKILL_HOME_MODULE_PATCH_LOCK",
        legacy_skill_home_lock,
    )

    def run_worker():
        try:
            streaming._run_agent_streaming(
                sid,
                session.pending_user_message,
                "test-model",
                str(tmp_path),
                stream_id,
                [],
                model_provider="test-provider",
            )
        except BaseException as exc:  # pragma: no cover - failure surface
            errors.append(exc)
        finally:
            worker_finished.set()

    worker = threading.Thread(target=run_worker, daemon=True)
    worker.start()
    admitted_deadline = time.monotonic() + 5
    while not admitted.is_set():
        if errors:
            raise errors[0]
        if legacy_skill_home_lock.acquire_calls:
            pytest.fail(
                "old worker selected the legacy skill-home fallback before event admission"
            )
        if worker_finished.is_set():
            pytest.fail("old worker exited before admitting the late event")
        remaining = admitted_deadline - time.monotonic()
        if remaining <= 0:
            break
        admitted.wait(timeout=min(0.05, remaining))
    assert admitted.is_set(), "real local event bridge did not admit the late event"

    assert streaming.cancel_stream(stream_id) is True
    assert worker.is_alive(), "cancel_stream must return before the provider worker completes"
    assert config.session_writeback_owner(sid) == stream_id

    # The production admission helper must be able to claim a successor while
    # the old generation is still blocked inside its durable journal append.
    import api.routes as routes

    successor_worker_finished = threading.Event()
    successor_worker_errors = []
    real_successor_worker = routes._run_agent_streaming

    def _run_successor_worker(*args, **kwargs):
        try:
            # Keep the real streaming worker and all route/cancel/reconciliation
            # behavior; this wrapper only exposes an early escaping failure
            # before the provider-snapshot barrier.
            return real_successor_worker(*args, **kwargs)
        except BaseException as exc:  # pragma: no cover - failure surface
            successor_worker_errors.append(exc)
            raise
        finally:
            successor_worker_finished.set()

    monkeypatch.setattr(routes, "_run_agent_streaming", _run_successor_worker)

    models.SESSIONS.pop(sid, None)
    successor = Session.load(sid)
    assert successor is not None
    models.SESSIONS[sid] = successor
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "ensure_agent_runtime_current", lambda: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    response = routes._start_chat_stream_for_session(
        successor,
        msg=successor_prompt,
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        external_runtime_owned=False,
    )
    assert response["session_id"] == sid
    successor_stream_id = response["stream_id"]
    assert successor.active_stream_id == successor_stream_id
    assert successor.pending_user_message == successor_prompt
    assert config.session_writeback_owner(sid) == successor_stream_id
    snapshot_deadline = time.monotonic() + 10
    while not successor_snapshot_ready.is_set():
        if successor_worker_errors:
            # Surface the causal successor failure before a derivative snapshot
            # timeout can hide it.
            raise successor_worker_errors[0]
        if legacy_skill_home_lock.acquire_calls:
            pytest.fail(
                "successor worker selected the legacy skill-home fallback before its provider snapshot"
            )
        if successor_worker_finished.is_set():
            pytest.fail("successor worker exited before capturing its provider snapshot")
        remaining = snapshot_deadline - time.monotonic()
        if remaining <= 0:
            break
        successor_snapshot_ready.wait(timeout=min(0.05, remaining))
    assert successor_snapshot_ready.is_set(), (
        "real successor worker did not capture its provider snapshot"
    )
    assert len(dynamic_override_homes) == 2
    assert len(dynamic_capability_homes) == 2
    assert legacy_skill_home_lock.acquire_calls == 0
    assert successor_provider_history
    assert not any(
        row.get("content") == late_text
        for row in successor_provider_history[0]
        if isinstance(row, dict)
    )
    assert not release_append.is_set(), "successor settled before the old append was released"

    release_append.set()
    save_deadline = time.monotonic() + 5
    while not (save_entered.is_set() or worker_finished.is_set()):
        remaining = save_deadline - time.monotonic()
        if remaining <= 0:
            break
        worker_finished.wait(timeout=min(0.05, remaining))
    if errors:
        # Surface the causal worker failure before reporting a derivative save
        # barrier timeout, so the test cannot hide the original exception.
        raise errors[0]
    if worker_finished.is_set() and not save_entered.is_set():
        pytest.fail("local worker exited before final recovered Session.save")
    assert save_entered.is_set(), "final recovered Session.save did not block"
    assert (sid, stream_id) in config.STREAM_CANCEL_GENERATIONS
    assert config.STREAM_CANCEL_GENERATIONS[(sid, stream_id)]["reconcile_started"] is True
    blocked = Session.load(sid)
    assert blocked is not None
    assert not any(
        row.get("content") == late_text
        and row.get("_recovered_from_run_journal") is True
        for row in blocked.messages
    )
    assert config.session_writeback_owner(sid) == successor_stream_id
    assert successor.active_stream_id == successor_stream_id
    assert successor.pending_user_message == successor_prompt
    assert not save_returned.is_set()
    release_save.set()
    worker.join(timeout=10)
    assert not worker.is_alive(), "local worker did not reach its finalizer"
    assert not errors
    assert save_returned.is_set()
    assert (sid, stream_id) not in config.STREAM_CANCEL_GENERATIONS
    # The old finalizer must not clear the successor's writeback claim.
    assert config.session_writeback_owner(sid) == successor_stream_id
    assert successor.active_stream_id == successor_stream_id
    assert successor.pending_user_message == successor_prompt

    # The successor captured its provider request before old-generation
    # reconciliation.  Let it settle only after that reconciliation has
    # durably committed and retired the old ownership record.
    assert (sid, stream_id) not in config.STREAM_CANCEL_GENERATIONS
    release_successor_settle.set()
    assert successor_finished.wait(timeout=10), "successor worker did not finish"
    successor_thread = config.ACTIVE_RUNS.get(successor_stream_id)
    if successor_thread is not None and hasattr(successor_thread, "join"):
        successor_thread.join(timeout=10)
    for _ in range(200):
        if (
            successor.active_stream_id is None
            and config.session_writeback_owner(sid) is None
        ):
            break
        threading.Event().wait(0.05)
    assert config.session_writeback_owner(sid) is None

    reloaded = Session.load(sid)
    assert reloaded is not None
    late_rows = [
        (index, row)
        for index, row in enumerate(reloaded.messages)
        if row.get("role") == "assistant"
        and row.get("content") == late_text
        and row.get("_recovered_from_run_journal") is True
    ]
    assert len(late_rows) == 1
    user_index = next(
        index
        for index, row in enumerate(reloaded.messages)
        if (
            row.get("role") == "user"
            and row.get("content") == cancelled_prompt
            and row.get("timestamp") == 1
        )
    )
    marker_index = next(
        index for index, row in enumerate(reloaded.messages) if row.get("_error") is True
    )
    assert user_index < late_rows[0][0] < marker_index
    successor_user_index = next(
        index
        for index, row in enumerate(reloaded.messages)
        if (
            index > marker_index
            and row.get("role") == "user"
            and row.get("content") == successor_prompt
        )
    )
    assert marker_index < successor_user_index
    next_context = _context_messages_for_new_turn(reloaded, successor_prompt)
    late_context_indices = [
        index
        for index, row in enumerate(next_context)
        if row.get("content") == late_text
    ]
    assert len(late_context_indices) == 1
    assert late_context_indices[0] < next(
        index
        for index, row in enumerate(next_context)
        if row.get("content") == "local_empty_tool successor answer"
    )
    reloaded_successor_tool = next(
        tool for tool in reloaded.tool_calls if tool.get("name") == "local_empty_tool"
    )
    successor_owner = reloaded.messages[reloaded_successor_tool["assistant_msg_idx"]]
    assert successor_owner.get("role") == "assistant"
    assert successor_owner.get("tool_calls", [])[0]["id"] == "successor-tool-id"
    assert any(
        row.get("content") == "local_empty_tool successor answer"
        for row in reloaded.messages
    )

    assert sum(row.get("content") == late_text for row in successor.messages) == 1
    assert successor.active_stream_id is None
    assert successor.pending_user_message is None


def test_local_cancel_finalizes_late_admitted_journal_after_live_partial(tmp_path, monkeypatch):
    sid = "issue6920_late_live_local"
    stream_id = "stream-late-live-local"
    token_a = "Repeated prefix"
    token_b = " token B"
    live_text = f"{token_a}{token_b}"
    late_segment = "Late local prose after the tool boundary."
    late_text = f"{token_a} {late_segment}"
    session = _session(sid, stream_id)
    _seed_historical_duplicate_prompt(session)
    historical_live_partial = {
        "role": "assistant",
        "content": "Unrelated historical live partial before the cancelled turn.",
        "_partial": True,
        "timestamp": 0.9,
    }
    session.messages.append(historical_live_partial)
    session.context_messages.append(copy.deepcopy(historical_live_partial))
    session.save()
    cancelled_prompt = session.pending_user_message
    successor_prompt = cancelled_prompt
    config.register_stream_owner(stream_id, sid)
    config.register_session_writeback_owner(sid, stream_id)
    config.STREAMS[stream_id] = queue.Queue()

    admitted = threading.Event()
    release_append = threading.Event()
    append_original = RunJournalWriter.append_sse_event

    def gated_append(writer, event, data):
        if event == "interim_assistant" and isinstance(data, dict) and data.get("text") == late_text:
            admitted.set()
            assert release_append.wait(timeout=5), "late journal append was not released"
        return append_original(writer, event, data)

    monkeypatch.setattr(RunJournalWriter, "append_sse_event", gated_append)
    save_entered, release_save, save_returned = _block_recovered_session_save(
        monkeypatch,
        late_segment,
    )

    class FakeAgent:
        def __init__(
            self,
            *,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            clarify_callback=None,
            interim_assistant_callback=None,
            tool_start_callback=None,
            tool_complete_callback=None,
            status_callback=None,
            session_id=None,
            **_kwargs,
        ):
            self.session_id = session_id or sid
            self.stream_delta_callback = stream_delta_callback
            self.reasoning_callback = reasoning_callback
            self.tool_progress_callback = tool_progress_callback
            self.interim_assistant_callback = interim_assistant_callback
            self.tool_start_callback = tool_start_callback
            self.tool_complete_callback = tool_complete_callback
            self.status_callback = status_callback
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self._last_error = None

        def run_conversation(self, **kwargs):
            # Drive the production token bridge so two ordered token segments
            # overlap the aggregate live partial. Flush a tool round between
            # them, then publish a later identity-distinct interim whose text
            # shares token A's prefix but does not represent token B.
            self.stream_delta_callback(token_a)
            self.tool_start_callback(
                "live-tool-id",
                "terminal",
                {"command": "printf live"},
            )
            self.tool_complete_callback(
                "live-tool-id",
                "terminal",
                {"command": "printf live"},
                "live tool result",
            )
            self.stream_delta_callback(token_b)
            self.interim_assistant_callback(late_text)
            return {
                "completed": True,
                "final_response": "",
                "messages": list(kwargs.get("conversation_history") or []),
            }

        def interrupt(self, _message):
            return None

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *args, **kwargs: (
        "test-model",
        "test-provider",
        None,
    ))
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config_for_profile_home", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(streaming, "_build_session_db_for_stream", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(streaming, "get_state_db_session_messages", _empty_state_db_messages)
    monkeypatch.setattr(
        streaming,
        "reconciled_state_db_messages_for_session",
        _empty_state_db_messages,
    )
    monkeypatch.setattr(streaming, "warm_models_catalog_provenance_if_cold", lambda: None)

    errors = []

    def run_worker():
        try:
            streaming._run_agent_streaming(
                sid,
                session.pending_user_message,
                "test-model",
                str(tmp_path),
                stream_id,
                [],
                model_provider="test-provider",
            )
        except BaseException as exc:  # pragma: no cover - failure surface
            errors.append(exc)

    worker = threading.Thread(target=run_worker, daemon=True)
    worker.start()
    assert admitted.wait(timeout=5), "real local event bridge did not admit the late event"

    assert streaming.cancel_stream(stream_id) is True
    assert worker.is_alive(), "cancel_stream must return before the provider worker completes"
    assert config.session_writeback_owner(sid) == stream_id

    # Admit the successor through the real chat-start helper before the late
    # append is released.  The old generation must remain claimable without
    # overwriting this successor's pending state.
    import api.routes as routes

    models.SESSIONS.pop(sid, None)
    successor = Session.load(sid)
    assert successor is not None
    models.SESSIONS[sid] = successor
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "ensure_agent_runtime_current", lambda: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    response = routes._start_chat_stream_for_session(
        successor,
        msg=successor_prompt,
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        external_runtime_owned=False,
    )
    assert response["session_id"] == sid
    successor_stream_id = response["stream_id"]
    assert successor.active_stream_id == successor_stream_id
    assert successor.pending_user_message == successor_prompt
    assert config.session_writeback_owner(sid) == successor_stream_id
    _seed_successor_progress(successor, successor_prompt, "local_live_tool")
    successor_tool_before = next(
        tool for tool in successor.tool_calls if tool.get("name") == "local_live_tool"
    )
    successor_owner_before = successor.messages[successor_tool_before["assistant_msg_idx"]]

    release_append.set()
    assert save_entered.wait(timeout=5), "final recovered Session.save did not block"
    assert (sid, stream_id) in config.STREAM_CANCEL_GENERATIONS
    assert config.STREAM_CANCEL_GENERATIONS[(sid, stream_id)]["reconcile_started"] is True
    blocked = Session.load(sid)
    assert blocked is not None
    assert not any(
        row.get("content") == late_text
        and row.get("_recovered_from_run_journal") is True
        for row in blocked.messages
    )
    assert config.session_writeback_owner(sid) == successor_stream_id
    assert successor.active_stream_id == successor_stream_id
    assert successor.pending_user_message == successor_prompt
    blocked_tool = next(
        tool for tool in successor.tool_calls if tool.get("name") == "local_live_tool"
    )
    assert successor.messages[blocked_tool["assistant_msg_idx"]]["content"] == (
        "local_live_tool successor answer"
    )
    assert not save_returned.is_set()
    release_save.set()
    worker.join(timeout=10)
    assert not worker.is_alive(), "local worker did not reach its finalizer"
    assert not errors
    assert save_returned.is_set()
    assert (sid, stream_id) not in config.STREAM_CANCEL_GENERATIONS
    assert config.session_writeback_owner(sid) == successor_stream_id
    successor_tool = next(
        tool for tool in successor.tool_calls if tool.get("name") == "local_live_tool"
    )
    successor_owner_index = successor_tool["assistant_msg_idx"]
    assert successor.messages[successor_owner_index]["content"] == (
        "local_live_tool successor answer"
    )
    assert successor.messages[successor_owner_index] is successor_owner_before

    reloaded = Session.load(sid)
    assert reloaded is not None
    recovered = [
        row for row in reloaded.messages
        if row.get("role") == "assistant" and row.get("_partial") is True
    ]
    assert sum(row.get("content") == live_text for row in recovered) == 1
    assert sum(row.get("content") == late_text for row in recovered) == 1, reloaded.messages
    assert sum(token_b in str(row.get("content") or "") for row in recovered) == 1
    user_index = next(
        index
        for index, row in enumerate(reloaded.messages)
        if (
            row.get("role") == "user"
            and row.get("content") == cancelled_prompt
            and row.get("timestamp") == 1
        )
    )
    late_index = next(
        index
        for index, row in enumerate(reloaded.messages)
        if row.get("content") == late_text
    )
    marker_index = next(
        index for index, row in enumerate(reloaded.messages) if row.get("_error") is True
    )
    assert user_index < late_index < marker_index
    successor_user_index = next(
        index
        for index, row in enumerate(reloaded.messages)
        if row.get("role") == "user"
        and row.get("content") == successor_prompt
        and row.get("timestamp") == 20
    )
    assert marker_index < successor_user_index
    next_context = _context_messages_for_new_turn(reloaded, successor_prompt)
    assert sum(row.get("content") == live_text for row in next_context) == 1
    assert sum(row.get("content") == late_text for row in next_context) == 1
    assert sum(token_b in str(row.get("content") or "") for row in next_context) == 1
    assert sum(late_segment in str(row.get("content") or "") for row in next_context) == 1
    assert sum(row.get("content") == token_a for row in next_context) == 0
    successor_context_user_index = next(
        index
        for index, row in enumerate(next_context)
        if row.get("role") == "user"
        and row.get("content") == successor_prompt
        and row.get("timestamp") == 20
    )
    old_stream_context_rows = [
        (index, row)
        for index, row in enumerate(next_context)
        if row.get("_recovered_from_run_journal") is True
        and row.get("_recovered_stream_id") == stream_id
    ]
    assert old_stream_context_rows
    assert all(index < successor_context_user_index for index, _row in old_stream_context_rows)
    cancelled_context_index = next(
        index
        for index, row in enumerate(next_context)
        if row.get("role") == "user"
        and row.get("content") == cancelled_prompt
        and row.get("timestamp") == 1
    )
    late_context_index = next(
        index
        for index, row in enumerate(next_context)
        if row.get("content") == late_text
    )
    assert cancelled_context_index < late_context_index

    assert successor.pending_user_message == successor_prompt
    assert successor.active_stream_id == successor_stream_id
    successor_tool = next(
        tool for tool in successor.tool_calls if tool.get("name") == "local_live_tool"
    )
    assert successor.messages[successor_tool["assistant_msg_idx"]]["content"] == (
        "local_live_tool successor answer"
    )
    config.STREAMS.pop(successor_stream_id, None)
    config.unregister_stream_owner(successor_stream_id)
    config.clear_session_writeback_owner_if_owned(sid, successor_stream_id)


def test_gateway_cancel_finalizes_late_admitted_journal_before_owner_retirement(tmp_path, monkeypatch):
    sid = "issue6920_late_gateway"
    stream_id = "stream-late-gateway"
    late_preview = "Late gateway tool output."
    session = _session(sid, stream_id)
    _seed_historical_duplicate_prompt(session)
    cancelled_prompt = session.pending_user_message
    successor_prompt = cancelled_prompt
    config.register_stream_owner(stream_id, sid)
    config.register_session_writeback_owner(sid, stream_id)
    config.STREAMS[stream_id] = queue.Queue()

    import api.gateway_chat as gateway_chat
    real_runs_api = gateway_chat._run_gateway_runs_api_streaming

    admitted = threading.Event()
    release_append = threading.Event()
    append_original = RunJournalWriter.append_sse_event

    def gated_append(writer, event, data):
        if event == "tool" and isinstance(data, dict) and data.get("preview") == late_preview:
            admitted.set()
            assert release_append.wait(timeout=5), "late gateway journal append was not released"
        return append_original(writer, event, data)

    monkeypatch.setattr(RunJournalWriter, "append_sse_event", gated_append)
    save_entered, release_save, save_returned = _block_recovered_session_save(
        monkeypatch,
        late_preview,
    )
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(gateway_chat, "_gateway_base_url", lambda *_args, **_kwargs: "http://gateway.test")
    monkeypatch.setattr(gateway_chat, "_gateway_api_key", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(gateway_chat, "_gateway_use_runs_api_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(gateway_chat, "_gateway_reasoning_effort_for_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(config, "_main_model_request_overrides", lambda *_args, **_kwargs: {})

    def fake_runs_api(*_args, **kwargs):
        kwargs["put_gateway_event"](
            "tool",
            {
                "name": "terminal",
                "preview": late_preview,
                "args": {"command": "printf late"},
            },
        )
        return "gateway answer after cancellation", {
            "input_tokens": 1,
            "output_tokens": 1,
            "estimated_cost": 0,
        }

    monkeypatch.setattr(gateway_chat, "_run_gateway_runs_api_streaming", fake_runs_api)
    errors = []

    def run_worker():
        try:
            gateway_chat._run_gateway_chat_streaming(
                sid,
                session.pending_user_message,
                "test-model",
                str(tmp_path),
                stream_id,
                [],
                model_provider="test-provider",
            )
        except BaseException as exc:  # pragma: no cover - failure surface
            errors.append(exc)

    worker = threading.Thread(target=run_worker, daemon=True)
    worker.start()
    assert admitted.wait(timeout=5), "real gateway event bridge did not admit the late event"

    assert streaming.cancel_stream(stream_id) is True
    assert worker.is_alive(), "cancel_stream must return before the gateway worker completes"
    assert config.session_writeback_owner(sid) == stream_id

    # Admit the successor through the production chat-start helper while the
    # old gateway publication is still blocked in its journal append.
    import api.routes as routes

    models.SESSIONS.pop(sid, None)
    successor = Session.load(sid)
    assert successor is not None
    models.SESSIONS[sid] = successor
    monkeypatch.setattr(routes, "_run_gateway_chat_streaming", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "ensure_agent_runtime_current", lambda: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    response = routes._start_chat_stream_for_session(
        successor,
        msg=successor_prompt,
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        external_runtime_owned=True,
    )
    assert response["session_id"] == sid
    successor_stream_id = response["stream_id"]
    assert successor.active_stream_id == successor_stream_id
    assert successor.pending_user_message == successor_prompt
    assert config.session_writeback_owner(sid) == successor_stream_id
    _seed_successor_progress(successor, successor_prompt, "gateway_successor_tool")

    release_append.set()
    assert save_entered.wait(timeout=5), "final recovered Session.save did not block"
    assert (sid, stream_id) in config.STREAM_CANCEL_GENERATIONS
    assert config.STREAM_CANCEL_GENERATIONS[(sid, stream_id)]["reconcile_started"] is True
    blocked = Session.load(sid)
    assert blocked is not None
    assert not any(
        row.get("content") == late_preview
        and row.get("_recovered_from_run_journal") is True
        for row in blocked.messages
    )
    assert config.session_writeback_owner(sid) == successor_stream_id
    assert successor.active_stream_id == successor_stream_id
    assert successor.pending_user_message == successor_prompt
    blocked_tool = next(
        tool for tool in successor.tool_calls if tool.get("name") == "gateway_successor_tool"
    )
    assert successor.messages[blocked_tool["assistant_msg_idx"]]["content"] == (
        "gateway_successor_tool successor answer"
    )
    assert not save_returned.is_set()
    release_save.set()
    worker.join(timeout=10)
    assert not worker.is_alive(), "gateway worker did not reach its finalizer"
    assert not errors
    assert save_returned.is_set()
    assert (sid, stream_id) not in config.STREAM_CANCEL_GENERATIONS
    assert config.session_writeback_owner(sid) == successor_stream_id
    successor_tool = next(
        tool
        for tool in successor.tool_calls
        if tool.get("name") == "gateway_successor_tool"
    )
    successor_owner_index = successor_tool["assistant_msg_idx"]
    assert successor.messages[successor_owner_index]["content"] == (
        "gateway_successor_tool successor answer"
    )

    reloaded = Session.load(sid)
    assert reloaded is not None
    recovered_tools = [
        tool for tool in reloaded.tool_calls
        if tool.get("preview") == late_preview
        and tool.get("_recovered_stream_id") == stream_id
    ]
    assert len(recovered_tools) == 1
    owner_index = recovered_tools[0].get("assistant_msg_idx")
    assert isinstance(owner_index, int)
    user_index = next(
        index
        for index, row in enumerate(reloaded.messages)
        if (
            row.get("role") == "user"
            and row.get("content") == cancelled_prompt
            and row.get("timestamp") == 1
        )
    )
    marker_index = next(
        index for index, row in enumerate(reloaded.messages) if row.get("_error") is True
    )
    assert user_index < owner_index < marker_index
    successor_user_index = next(
        index
        for index, row in enumerate(reloaded.messages)
        if row.get("role") == "user"
        and row.get("content") == successor_prompt
        and row.get("timestamp") == 20
    )
    assert marker_index < successor_user_index

    assert sum(tool.get("preview") == late_preview for tool in successor.tool_calls) == 1
    assert successor.pending_user_message == successor_prompt
    assert successor.active_stream_id == successor_stream_id
    successor_tool = next(
        tool for tool in successor.tool_calls if tool.get("name") == "gateway_successor_tool"
    )
    assert successor.messages[successor_tool["assistant_msg_idx"]]["content"] == (
        "gateway_successor_tool successor answer"
    )
    next_context = _context_messages_for_new_turn(reloaded, successor_prompt)
    cancelled_context_index = next(
        index
        for index, row in enumerate(next_context)
        if row.get("role") == "user"
        and row.get("content") == cancelled_prompt
        and row.get("timestamp") == 1
    )
    successor_context_index = next(
        index
        for index, row in enumerate(next_context)
        if row.get("role") == "user"
        and row.get("content") == successor_prompt
        and row.get("timestamp") == 20
    )
    assert cancelled_context_index < successor_context_index
    assert all("tool_calls" not in row for row in next_context)
    assert not any(
        row.get("role") == "tool" and row.get("content") == late_preview
        for row in next_context
    )
    assert any(
        tool.get("preview") == late_preview
        and tool.get("_recovered_stream_id") == stream_id
        for tool in reloaded.tool_calls
    )
    provider_requests = []

    class _GatewayResponse:
        def __init__(self, body=b"", lines=()):
            self._body = body
            self._lines = tuple(lines)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return self._body

        def __iter__(self):
            return iter(self._lines)

    def capture_provider_request(request, timeout=None):
        del timeout
        if request.full_url.endswith("/v1/runs"):
            provider_requests.append(json.loads(request.data.decode("utf-8")))
            return _GatewayResponse(body=b'{"run_id":"next-run"}')
        return _GatewayResponse(lines=(b"data: [DONE]\n",))

    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", capture_provider_request)
    real_runs_api(
        sid,
        "next gateway prompt",
        "test-model",
        str(tmp_path),
        "stream-next-gateway",
        "http://gateway.test",
        "",
        [],
        {},
        put_gateway_event=lambda *_args, **_kwargs: None,
        cancel_event=threading.Event(),
        attachments=[],
        cfg={},
        session=reloaded,
        active_provider="test-provider",
    )
    assert len(provider_requests) == 1
    provider_history = provider_requests[0].get("conversation_history") or []
    assert all("tool_calls" not in row for row in provider_history)
    assert not any(row.get("role") == "tool" for row in provider_history)
    reloaded_successor_tool = next(
        tool for tool in reloaded.tool_calls if tool.get("name") == "gateway_successor_tool"
    )
    assert reloaded.messages[reloaded_successor_tool["assistant_msg_idx"]]["content"] == (
        "gateway_successor_tool successor answer"
    )
    config.STREAMS.pop(successor_stream_id, None)
    config.unregister_stream_owner(successor_stream_id)
    config.clear_session_writeback_owner_if_owned(sid, successor_stream_id)


def _run_success_commit_late_stop_worker(
    *,
    worker_target,
    sid: str,
    old_stream: str,
    tmp_path,
    monkeypatch,
    configure_worker,
    external_runtime_owned: bool,
    success_text: str,
):
    """Exercise the real producer pre-commit success/cancel race."""
    session = _session(sid, old_stream)
    old_prompt = session.pending_user_message
    config.register_stream_owner(old_stream, sid)
    config.register_session_writeback_owner(sid, old_stream)
    config.STREAMS[old_stream] = queue.Queue()

    success_save_entered = threading.Event()
    release_success_save = threading.Event()
    save_original = Session.save

    def gated_success_save(session, *args, **kwargs):
        assembled = any(
            isinstance(row, dict)
            and row.get("role") == "assistant"
            and row.get("content") == success_text
            for row in getattr(session, "messages", [])
        )
        if (
            not success_save_entered.is_set()
            and assembled
            and getattr(session, "active_stream_id", None) is None
            and getattr(session, "pending_user_message", None) is None
        ):
            success_save_entered.set()
            assert release_success_save.wait(timeout=5), "success Session.save was not released"
        return save_original(session, *args, **kwargs)

    monkeypatch.setattr(Session, "save", gated_success_save)
    configure_worker()
    errors = []

    def run_worker():
        try:
            worker_target(
                sid,
                old_prompt,
                "test-model",
                str(tmp_path),
                old_stream,
                [],
                model_provider="test-provider",
            )
        except BaseException as exc:  # pragma: no cover - failure surface
            errors.append(exc)

    worker = threading.Thread(target=run_worker, daemon=True)
    worker.start()
    assert success_save_entered.wait(timeout=5), "real worker did not block pre-commit success save"
    assert config.STREAM_CANCEL_GENERATIONS[(sid, old_stream)]["success_committed"] is False

    # Stop arrives after success assembly cleared active/pending fields but
    # before the durable success generation is marked.  The real cancel path
    # must settle one coherent terminal outcome and retire this exact owner.
    cancel_result = []
    cancel_returned = threading.Event()

    def run_cancel():
        try:
            cancel_result.append(cancel_stream(old_stream))
        finally:
            cancel_returned.set()

    cancel_thread = threading.Thread(target=run_cancel, daemon=True)
    cancel_thread.start()
    for _ in range(100):
        record = config.STREAM_CANCEL_GENERATIONS.get((sid, old_stream))
        if record and record.get("cancel_requested"):
            break
        threading.Event().wait(0.01)
    record = config.STREAM_CANCEL_GENERATIONS.get((sid, old_stream))
    assert record is not None and record.get("cancel_requested") is True
    assert not cancel_returned.is_set()
    assert worker.is_alive(), "success worker should remain blocked in Session.save"

    release_success_save.set()
    worker.join(timeout=10)
    assert not worker.is_alive(), "success worker did not reach its finalizer"
    assert cancel_returned.wait(timeout=10), "cancel_stream did not settle after save release"
    cancel_thread.join(timeout=1)
    assert cancel_result == [True]
    assert (sid, old_stream) not in config.STREAM_CANCEL_GENERATIONS, config.STREAM_CANCEL_GENERATIONS.get((sid, old_stream))
    assert config.session_writeback_owner(sid) is None

    saved_outcome = Session.load(sid)
    assert saved_outcome is not None
    assert any(
        row.get("role") == "assistant" and row.get("_error") is True
        for row in saved_outcome.messages
    )
    assert not any(
        row.get("role") == "assistant"
        and row.get("content") == success_text
        and not (
            row.get("_partial") is True
            or row.get("_recovered_from_run_journal") is True
        )
        for row in saved_outcome.messages
    )

    import api.routes as routes

    models.SESSIONS.pop(sid, None)
    successor = Session.load(sid)
    assert successor is not None
    models.SESSIONS[sid] = successor
    if external_runtime_owned:
        monkeypatch.setattr(routes, "_run_gateway_chat_streaming", lambda *_args, **_kwargs: None)
    else:
        monkeypatch.setattr(routes, "_run_agent_streaming", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "ensure_agent_runtime_current", lambda: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    response = routes._start_chat_stream_for_session(
        successor,
        msg="successor prompt",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        external_runtime_owned=external_runtime_owned,
    )
    assert response["session_id"] == sid
    successor_stream_id = response["stream_id"]
    assert successor.active_stream_id == successor_stream_id
    assert successor.pending_user_message == "successor prompt"
    assert config.session_writeback_owner(sid) == successor_stream_id
    _seed_successor_progress(
        successor,
        "successor prompt",
        "gateway_successor_tool" if external_runtime_owned else "local_successor_tool",
    )

    assert not errors
    assert config.session_writeback_owner(sid) == successor_stream_id

    reloaded = Session.load(sid)
    assert reloaded is not None
    assert reloaded.active_stream_id == successor_stream_id
    assert reloaded.pending_user_message == "successor prompt"
    assert any(row.get("content") == old_prompt for row in reloaded.messages)
    assert any(row.get("content") == "successor prompt" for row in reloaded.messages)
    successor_tool_name = "gateway_successor_tool" if external_runtime_owned else "local_successor_tool"
    successor_tool = next(
        tool for tool in reloaded.tool_calls if tool.get("name") == successor_tool_name
    )
    assert reloaded.messages[successor_tool["assistant_msg_idx"]]["content"] == (
        f"{successor_tool_name} successor answer"
    )
    assert config.session_writeback_owner(sid) == successor_stream_id
    config.STREAMS.pop(successor_stream_id, None)
    config.unregister_stream_owner(successor_stream_id)
    config.clear_session_writeback_owner_if_owned(sid, successor_stream_id)


def test_local_success_commit_late_stop_retires_old_generation(tmp_path, monkeypatch):
    sid = "issue6920_success_local"
    old_stream = "stream-success-local"

    class FakeAgent:
        def __init__(self, *, stream_delta_callback=None, session_id=None, **_kwargs):
            self.session_id = session_id or sid
            self.stream_delta_callback = stream_delta_callback
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self._last_error = None

        def run_conversation(self, **kwargs):
            answer = "successful answer"
            self.stream_delta_callback(answer)
            return {
                "completed": True,
                "final_response": answer,
                "messages": list(kwargs.get("conversation_history") or []) + [
                    {"role": "assistant", "content": answer},
                ],
            }

        def interrupt(self, _message):
            return None

    def configure_worker():
        monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
        monkeypatch.setattr(streaming, "resolve_model_provider", lambda *args, **kwargs: (
            "test-model",
            "test-provider",
            None,
        ))
        monkeypatch.setattr(streaming, "get_config", lambda: {})
        monkeypatch.setattr(config, "get_config", lambda: {})
        monkeypatch.setattr(config, "get_config_for_profile_home", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(streaming, "_build_session_db_for_stream", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(streaming, "get_state_db_session_messages", _empty_state_db_messages)
        monkeypatch.setattr(
            streaming,
            "reconciled_state_db_messages_for_session",
            _empty_state_db_messages,
        )
        monkeypatch.setattr(streaming, "warm_models_catalog_provenance_if_cold", lambda: None)

    _run_success_commit_late_stop_worker(
        worker_target=streaming._run_agent_streaming,
        sid=sid,
        old_stream=old_stream,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        configure_worker=configure_worker,
        external_runtime_owned=False,
        success_text="successful answer",
    )


def test_gateway_success_commit_late_stop_retires_old_generation(tmp_path, monkeypatch):
    import api.gateway_chat as gateway_chat

    sid = "issue6920_success_gateway"
    old_stream = "stream-success-gateway"

    def configure_worker():
        monkeypatch.setattr(config, "get_config", lambda: {})
        monkeypatch.setattr(gateway_chat, "_gateway_base_url", lambda *_args, **_kwargs: "http://gateway.test")
        monkeypatch.setattr(gateway_chat, "_gateway_api_key", lambda *_args, **_kwargs: "")
        monkeypatch.setattr(gateway_chat, "_gateway_use_runs_api_enabled", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(gateway_chat, "_gateway_reasoning_effort_for_request", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(config, "_main_model_request_overrides", lambda *_args, **_kwargs: {})

        def fake_runs_api(*_args, **kwargs):
            kwargs["put_gateway_event"]("token", {"text": "successful answer"})
            return "successful answer", {
                "input_tokens": 1,
                "output_tokens": 1,
                "estimated_cost": 0,
            }

        monkeypatch.setattr(gateway_chat, "_run_gateway_runs_api_streaming", fake_runs_api)

    _run_success_commit_late_stop_worker(
        worker_target=gateway_chat._run_gateway_chat_streaming,
        sid=sid,
        old_stream=old_stream,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        configure_worker=configure_worker,
        external_runtime_owned=True,
        success_text="successful answer",
    )


def test_gateway_cancel_arming_before_flag_set_does_not_deadlock(tmp_path, monkeypatch):
    """Gateway cancellation must not reconcile while its session lock is held."""
    import api.gateway_chat as gateway_chat

    sid = "issue6920_cancel_arm_gateway"
    old_stream = "stream-cancel-arm-gateway"
    session = _session(sid, old_stream)
    old_prompt = session.pending_user_message
    config.register_stream_owner(old_stream, sid)
    config.register_session_writeback_owner(sid, old_stream)
    config.STREAMS[old_stream] = queue.Queue()

    success_save_entered = threading.Event()
    release_success_save = threading.Event()
    success_mark_attempted = threading.Event()
    original_save = Session.save

    def gated_success_save(session, *args, **kwargs):
        assembled = any(
            isinstance(row, dict)
            and row.get("role") == "assistant"
            and row.get("content") == "successful answer"
            for row in getattr(session, "messages", [])
        )
        if (
            not success_save_entered.is_set()
            and assembled
            and getattr(session, "active_stream_id", None) is None
            and getattr(session, "pending_user_message", None) is None
        ):
            success_save_entered.set()
            assert release_success_save.wait(timeout=5), (
                "success Session.save was not released"
            )
        return original_save(session, *args, **kwargs)

    monkeypatch.setattr(Session, "save", gated_success_save)
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(
        gateway_chat,
        "_gateway_base_url",
        lambda *_args, **_kwargs: "http://gateway.test",
    )
    monkeypatch.setattr(gateway_chat, "_gateway_api_key", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        gateway_chat,
        "_gateway_use_runs_api_enabled",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        gateway_chat,
        "gateway_supports_approval",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        gateway_chat,
        "_gateway_reasoning_effort_for_request",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(config, "_main_model_request_overrides", lambda *_args, **_kwargs: {})

    def fake_runs_api(*_args, **kwargs):
        kwargs["put_gateway_event"]("token", {"text": "successful answer"})
        return "successful answer", {
            "input_tokens": 1,
            "output_tokens": 1,
            "estimated_cost": 0,
        }

    monkeypatch.setattr(gateway_chat, "_run_gateway_runs_api_streaming", fake_runs_api)

    original_mark_success = gateway_chat.mark_stream_success_committed

    def observe_success_mark(*args, **kwargs):
        result = original_mark_success(*args, **kwargs)
        success_mark_attempted.set()
        return result

    monkeypatch.setattr(gateway_chat, "mark_stream_success_committed", observe_success_mark)

    arm_entered = threading.Event()
    release_after_arm = threading.Event()
    original_begin = streaming.begin_stream_cancel_generation

    def pause_after_arm(*args, **kwargs):
        armed = original_begin(*args, **kwargs)
        if armed:
            arm_entered.set()
            assert release_after_arm.wait(timeout=5), (
                "cancel-generation arm was not released"
            )
        return armed

    monkeypatch.setattr(streaming, "begin_stream_cancel_generation", pause_after_arm)
    worker_errors = []
    cancel_errors = []
    cancel_result = []

    def run_worker():
        try:
            gateway_chat._run_gateway_chat_streaming(
                sid,
                old_prompt,
                "test-model",
                str(tmp_path),
                old_stream,
                [],
                model_provider="test-provider",
            )
        except BaseException as exc:  # pragma: no cover - failure surface
            worker_errors.append(exc)

    def run_cancel():
        try:
            cancel_result.append(streaming.cancel_stream(old_stream))
        except BaseException as exc:  # pragma: no cover - failure surface
            cancel_errors.append(exc)

    worker = threading.Thread(target=run_worker, daemon=True)
    worker.start()
    assert success_save_entered.wait(timeout=5), (
        "real Gateway worker did not block pre-commit success save"
    )

    cancel_thread = threading.Thread(target=run_cancel, daemon=True)
    cancel_thread.start()
    assert arm_entered.wait(timeout=5), (
        "cancel_stream did not pause after generation arming"
    )
    assert config.STREAM_CANCEL_GENERATIONS[(sid, old_stream)]["cancel_requested"] is True

    release_success_save.set()
    assert success_mark_attempted.wait(timeout=5), (
        "Gateway worker did not attempt success commit"
    )
    release_after_arm.set()

    worker.join(timeout=10)
    cancel_thread.join(timeout=10)
    assert not worker.is_alive(), "Gateway worker deadlocked during cancelled success settlement"
    assert not cancel_thread.is_alive(), "cancel_stream deadlocked during cancelled success settlement"
    assert cancel_result == [True]
    assert not worker_errors
    assert not cancel_errors


def test_local_successor_cancel_after_success_save_preserves_detached_predecessor_recovery(
    tmp_path,
    monkeypatch,
):
    """A successor Stop must not roll back a predecessor recovered after its snapshot."""
    sid = "issue6920_successor_rollback_recovery"
    old_stream = "stream-predecessor-recovery"
    late_text = "Detached predecessor recovery survives successor rollback."
    late_tool_id = "predecessor-tool-id"
    late_tool_name = "predecessor_terminal"
    successor_prompt = "Continue after predecessor cancellation"
    successor_text = "successor answer must be rolled back"

    session = _session(sid, old_stream)
    _seed_historical_duplicate_prompt(session)
    cancelled_prompt = session.pending_user_message
    config.register_stream_owner(old_stream, sid)
    config.register_session_writeback_owner(sid, old_stream)
    config.STREAMS[old_stream] = queue.Queue()
    # The worker registration replaces the pre-seeded flag; capture the exact
    # event object from the worker constructor below rather than this setup
    # placeholder.

    admitted = threading.Event()
    release_append = threading.Event()
    successor_snapshot_ready = threading.Event()
    release_successor_provider = threading.Event()
    predecessor_recovery_saved = threading.Event()
    successor_success_saved = threading.Event()
    mark_attempted = threading.Event()
    release_mark = threading.Event()
    successor_finished = threading.Event()
    causal_events = []
    provider_history = []
    run_lock = threading.Lock()
    run_count = {"value": 0}
    append_original = RunJournalWriter.append_sse_event

    def gated_append(writer, event, data):
        if (
            event == "interim_assistant"
            and isinstance(data, dict)
            and data.get("text") == late_text
        ):
            admitted.set()
            assert release_append.wait(timeout=30), "predecessor journal append was not released"
        return append_original(writer, event, data)

    monkeypatch.setattr(RunJournalWriter, "append_sse_event", gated_append)

    save_original = Session.save

    def observe_save(current, *args, **kwargs):
        result = save_original(current, *args, **kwargs)
        rows = getattr(current, "messages", None) or []
        has_predecessor = any(
            isinstance(row, dict)
            and row.get("_recovered_from_run_journal") is True
            and row.get("_recovered_stream_id") == old_stream
            and row.get("content") == late_text
            for row in rows
        )
        has_success = any(
            isinstance(row, dict)
            and row.get("role") == "assistant"
            and row.get("content") == successor_text
            for row in rows
        )
        if has_predecessor and not predecessor_recovery_saved.is_set():
            causal_events.append("predecessor_recovery_save_return")
            predecessor_recovery_saved.set()
        if has_success and not successor_success_saved.is_set():
            causal_events.append("successor_success_save_return")
            successor_success_saved.set()
        return result

    monkeypatch.setattr(Session, "save", observe_save)

    original_mark_success = streaming.mark_stream_success_committed

    def gate_success_mark(*args, **kwargs):
        causal_events.append("mark_stream_success_committed_attempt")
        mark_attempted.set()
        assert release_mark.wait(timeout=15), "success commit barrier was not released"
        return original_mark_success(*args, **kwargs)

    monkeypatch.setattr(streaming, "mark_stream_success_committed", gate_success_mark)

    class FakeAgent:
        def __init__(
            self,
            *,
            stream_delta_callback=None,
            tool_start_callback=None,
            tool_complete_callback=None,
            interim_assistant_callback=None,
            session_id=None,
            **_kwargs,
        ):
            self.session_id = session_id or sid
            self.stream_delta_callback = stream_delta_callback
            self.tool_start_callback = tool_start_callback
            self.tool_complete_callback = tool_complete_callback
            self.interim_assistant_callback = interim_assistant_callback
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0.0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self._last_error = None
            self.cancel_event = config.CANCEL_FLAGS.get(old_stream)

        def run_conversation(self, **kwargs):
            with run_lock:
                run_count["value"] += 1
                run_number = run_count["value"]
            if run_number == 1:
                self.tool_start_callback(
                    late_tool_id,
                    late_tool_name,
                    {"command": "printf predecessor"},
                )
                self.tool_complete_callback(
                    late_tool_id,
                    late_tool_name,
                    {"command": "printf predecessor"},
                    "predecessor tool output",
                )
                # Tool journal identity is admitted before the successor
                # snapshot, while the visible predecessor prose is held at
                # the publication barrier.  The durable Session reconciliation
                # therefore still occurs only after that successor snapshot.
                self.interim_assistant_callback(late_text)
                while not self.cancel_event.wait(0.01):
                    pass
                return {
                    "completed": False,
                    "final_response": "",
                    "messages": list(kwargs.get("conversation_history") or []),
                }

            history = copy.deepcopy(kwargs.get("conversation_history") or [])
            provider_history.append(history)
            causal_events.append("successor_provider_snapshot")
            successor_snapshot_ready.set()
            assert release_successor_provider.wait(timeout=15), (
                "successor provider did not reach its settlement barrier"
            )
            tool_id = "successor-tool-id"
            tool_name = "successor_terminal"
            tool_args = {"command": "printf successor"}
            self.tool_start_callback(tool_id, tool_name, tool_args)
            self.tool_complete_callback(
                tool_id,
                tool_name,
                tool_args,
                "successor tool output",
            )
            successor_user = {
                "role": "user",
                "content": kwargs.get("persist_user_message") or successor_prompt,
            }
            return {
                "completed": True,
                "final_response": successor_text,
                "messages": history + [
                    successor_user,
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": tool_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(tool_args),
                            },
                        }],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": "successor tool output",
                    },
                    {"role": "assistant", "content": successor_text},
                ],
            }

        def interrupt(self, _message):
            return None

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *args, **kwargs: ("test-model", "test-provider", None),
    )
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config_for_profile_home", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(streaming, "_build_session_db_for_stream", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(streaming, "get_state_db_session_messages", _empty_state_db_messages)
    monkeypatch.setattr(
        streaming,
        "reconciled_state_db_messages_for_session",
        _session_state_db_messages,
    )
    monkeypatch.setattr(streaming, "warm_models_catalog_provenance_if_cold", lambda: None)

    # Hosted workers must use the existing dynamic profile-home path so this
    # test exercises the production concurrent schedule without the legacy
    # process-global skill-home lock.
    import api.profiles as profiles_api

    dynamic_override_homes = []
    dynamic_capability_homes = []

    def install_dynamic_home(profile_home):
        dynamic_override_homes.append(str(profile_home))
        return object(), object(), True

    monkeypatch.setattr(streaming, "_set_streaming_hermes_home_override", install_dynamic_home)
    monkeypatch.setattr(
        streaming,
        "_reset_streaming_hermes_home_override",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        profiles_api,
        "_skill_modules_support_profile_home",
        lambda profile_home: dynamic_capability_homes.append(str(profile_home)) or True,
    )

    old_errors = []

    def run_old_worker():
        try:
            streaming._run_agent_streaming(
                sid,
                cancelled_prompt,
                "test-model",
                str(tmp_path),
                old_stream,
                [],
                model_provider="test-provider",
            )
        except BaseException as exc:  # pragma: no cover - failure surface
            old_errors.append(exc)

    old_worker = threading.Thread(target=run_old_worker, daemon=True)
    old_worker.start()
    assert admitted.wait(timeout=10), "old worker did not admit predecessor recovery"

    assert streaming.cancel_stream(old_stream) is True
    assert old_worker.is_alive(), "old worker must remain blocked in the admitted journal append"
    assert config.session_writeback_owner(sid) == old_stream

    import api.routes as routes

    successor_errors = []
    successor_finished.clear()
    real_successor_worker = routes._run_agent_streaming

    def run_successor_worker(*args, **kwargs):
        try:
            return real_successor_worker(*args, **kwargs)
        except BaseException as exc:  # pragma: no cover - failure surface
            successor_errors.append(exc)
            raise
        finally:
            successor_finished.set()

    monkeypatch.setattr(routes, "_run_agent_streaming", run_successor_worker)
    models.SESSIONS.pop(sid, None)
    successor = Session.load(sid)
    assert successor is not None
    models.SESSIONS[sid] = successor
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "ensure_agent_runtime_current", lambda: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    response = routes._start_chat_stream_for_session(
        successor,
        msg=successor_prompt,
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        external_runtime_owned=False,
    )
    successor_stream = response["stream_id"]
    assert config.session_writeback_owner(sid) == successor_stream
    assert successor_snapshot_ready.wait(timeout=15), "successor provider snapshot was not reached"
    assert provider_history
    assert not any(
        isinstance(row, dict)
        and (
            row.get("content") == late_text
            or row.get("_recovered_stream_id") == old_stream
        )
        for row in provider_history[0]
    )
    assert not release_append.is_set()
    causal_events.append("successor_snapshot_asserted")

    # Keep the successor's provider snapshot object detached from the canonical
    # cache while the cancelled predecessor reconciler saves its late journal
    # rows.  Production can evict/replace this object at the same boundary;
    # the successor must therefore not observe the durable recovery until its
    # settlement merge explicitly reloads it.
    models.SESSIONS.pop(sid, None)

    release_append.set()
    assert predecessor_recovery_saved.wait(timeout=15), (
        "predecessor recovery did not reach a durable save after successor snapshot"
    )
    assert causal_events.index("successor_provider_snapshot") < causal_events.index(
        "predecessor_recovery_save_return"
    )

    # The detached predecessor transaction has now retired its exact retry
    # hook.  Reflect that canonical metadata readback on the successor's stale
    # object so a later Session.load cannot mask a rollback overwrite by lazily
    # re-importing the already-settled predecessor journal.
    from api.models import _strip_journal_retry_meta

    for row in successor.messages:
        if (
            isinstance(row, dict)
            and row.get("_error") is True
            and str(row.get("_journal_retry_stream_id") or "") == old_stream
        ):
            _strip_journal_retry_meta(row)

    release_successor_provider.set()
    assert successor_success_saved.wait(timeout=15), (
        "successor success save did not return with predecessor recovery"
    )
    assert mark_attempted.wait(timeout=15), "worker did not reach post-save success commit barrier"
    assert causal_events.index("predecessor_recovery_save_return") < causal_events.index(
        "successor_success_save_return"
    ) < causal_events.index("mark_stream_success_committed_attempt")

    mid_save = Session.load(sid)
    assert mid_save is not None
    mid_recovered = [
        row
        for row in mid_save.messages
        if row.get("_recovered_from_run_journal") is True
        and row.get("_recovered_stream_id") == old_stream
        and row.get("content") == late_text
    ]
    assert len(mid_recovered) == 1
    mid_context_recovered = [
        row
        for row in mid_save.context_messages
        if row.get("_recovered_from_run_journal") is True
        and row.get("_recovered_stream_id") == old_stream
        and row.get("content") == late_text
    ]
    assert len(mid_context_recovered) == 1
    mid_recovered_tools = [
        tool
        for tool in mid_save.tool_calls
        if tool.get("_recovered_from_run_journal") is True
        and tool.get("_recovered_stream_id") == old_stream
        and tool.get("tid") == late_tool_id
    ]
    assert len(mid_recovered_tools) == 1
    mid_tool_owner = mid_recovered_tools[0].get("assistant_msg_idx")
    assert isinstance(mid_tool_owner, int)
    assert 0 <= mid_tool_owner < len(mid_save.messages)
    assert mid_recovered_tools[0].get("_recovered_assistant_event_id") == (
        mid_save.messages[mid_tool_owner].get("_recovered_event_id")
    )
    assert any(row.get("content") == successor_text for row in mid_save.messages)

    cancel_result = []
    cancel_errors = []

    def cancel_successor():
        try:
            cancel_result.append(streaming.cancel_stream(successor_stream))
        except BaseException as exc:  # pragma: no cover - failure surface
            cancel_errors.append(exc)

    cancel_thread = threading.Thread(target=cancel_successor, daemon=True)
    cancel_thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        record = config.STREAM_CANCEL_GENERATIONS.get((sid, successor_stream))
        if record and record.get("cancel_requested"):
            break
        threading.Event().wait(0.01)
    record = config.STREAM_CANCEL_GENERATIONS.get((sid, successor_stream))
    assert record is not None and record.get("cancel_requested") is True
    causal_events.append("successor_stop_armed")
    release_mark.set()

    old_worker.join(timeout=15)
    assert not old_worker.is_alive(), "old predecessor worker did not finish"
    assert not old_errors
    successor_finished.wait(timeout=15)
    assert successor_finished.is_set(), "successor worker did not finish"
    successor_thread = config.ACTIVE_RUNS.get(successor_stream)
    if successor_thread is not None and hasattr(successor_thread, "join"):
        successor_thread.join(timeout=15)
    assert not successor_errors
    cancel_thread.join(timeout=15)
    assert not cancel_thread.is_alive(), "successor cancel did not settle"
    assert not cancel_errors
    assert cancel_result == [True]
    assert config.session_writeback_owner(sid) is None

    models.SESSIONS.pop(sid, None)
    reloaded = Session.load(sid)
    assert reloaded is not None
    recovered_rows = [
        (index, row)
        for index, row in enumerate(reloaded.messages)
        if row.get("role") == "assistant"
        and row.get("content") == late_text
        and row.get("_recovered_from_run_journal") is True
        and row.get("_recovered_stream_id") == old_stream
    ]
    assert len(recovered_rows) == 1, reloaded.messages
    assert not any(row.get("content") == successor_text for row in reloaded.messages)
    assert any(
        row.get("_error") is True and "cancel" in str(row.get("content") or "").lower()
        for row in reloaded.messages
    )

    from api.run_journal import read_run_events

    journal_events = read_run_events(sid, old_stream).get("events") or []
    predecessor_event_id = next(
        event["event_id"]
        for event in journal_events
        if event.get("event") == "interim_assistant"
        and (event.get("payload") or {}).get("text") == late_text
    )
    predecessor_tool_event_id = next(
        event["event_id"]
        for event in journal_events
        if event.get("event") == "tool"
        and (event.get("payload") or {}).get("tid") == late_tool_id
    )
    recovered_row = recovered_rows[0][1]
    assert recovered_row.get("_recovered_event_id") == predecessor_event_id
    recovered_tools = [
        tool
        for tool in reloaded.tool_calls
        if tool.get("_recovered_from_run_journal") is True
        and tool.get("_recovered_stream_id") == old_stream
        and tool.get("tid") == late_tool_id
    ]
    assert len(recovered_tools) == 1
    recovered_tool = recovered_tools[0]
    assert recovered_tool.get("_recovered_event_id") == predecessor_tool_event_id
    owner_index = recovered_tool.get("assistant_msg_idx")
    assert isinstance(owner_index, int)
    owner_event_id = reloaded.messages[owner_index].get("_recovered_event_id")
    assert recovered_tool.get("_recovered_assistant_event_id") == owner_event_id
    assert owner_event_id in {predecessor_event_id, recovered_tool.get("_recovered_event_id")}

    context_recovered = [
        row
        for row in reloaded.context_messages
        if row.get("_recovered_from_run_journal") is True
        and row.get("_recovered_stream_id") == old_stream
        and row.get("content") == late_text
    ]
    assert len(context_recovered) == 1
    next_context = _context_messages_for_new_turn(reloaded, "next turn after rollback")
    assert sum(row.get("content") == late_text for row in next_context) == 1
    assert causal_events.index("successor_provider_snapshot") < causal_events.index(
        "predecessor_recovery_save_return"
    ) < causal_events.index("successor_success_save_return") < causal_events.index(
        "mark_stream_success_committed_attempt"
    ) < causal_events.index("successor_stop_armed")


def _wait_for_cancel_reconciliation(predicate, *, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(0.01)
    return bool(predicate())


def _arm_quiescent_cancel_reconciliation(sid, stream_id):
    session = _session(sid, stream_id)
    _start_cancel_state(sid, stream_id)
    config.register_stream_owner(stream_id, sid)
    config.register_session_writeback_owner(sid, stream_id)
    assert cancel_stream(stream_id) is True
    record = config.STREAM_CANCEL_GENERATIONS[(sid, stream_id)]
    assert isinstance(record.get("turn_start"), int)
    return session


def test_final_cancel_reconciliation_save_failure_retries_without_new_event(monkeypatch):
    sid = "issue6920_retry_after_final_save"
    old_stream = "stream-retry-old"
    successor_stream = "stream-retry-successor"
    late_text = "Late prose survives the first failed final save."
    baseline_generations = len(config.STREAM_CANCEL_GENERATIONS)
    baseline_retry_owners = len(
        getattr(config, "STREAM_CANCEL_RECONCILIATION_RETRY_OWNERS", ())
    )

    _arm_quiescent_cancel_reconciliation(sid, old_stream)
    writer = RunJournalWriter(sid, old_stream)
    writer.append_sse_event("token", {"text": late_text})
    writer.append_sse_event(
        "tool",
        {
            "name": "terminal",
            "preview": "printf retry-once",
            "args": {"command": "printf retry-once"},
        },
    )
    writer.append_sse_event(
        "tool_complete",
        {"name": "terminal", "preview": "retry-once", "is_error": False},
    )
    assert config.mark_stream_worker_done(sid, old_stream) is True
    config.register_session_writeback_owner(sid, successor_stream)

    monkeypatch.setattr(
        streaming,
        "_CANCEL_RECONCILIATION_RETRY_DELAYS",
        (0.01, 0.02),
        raising=False,
    )
    save_original = Session.save
    recovered_save_attempts = []

    def fail_first_recovered_save(session, *args, **kwargs):
        has_exact_recovery = any(
            isinstance(row, dict)
            and row.get("_recovered_from_run_journal") is True
            and row.get("_recovered_stream_id") == old_stream
            for row in getattr(session, "messages", [])
        )
        if has_exact_recovery:
            recovered_save_attempts.append(copy.deepcopy(session.messages))
            if len(recovered_save_attempts) == 1:
                retry_owner = config.stream_cancel_reconciliation_retry_owner(
                    sid,
                    old_stream,
                )
                assert retry_owner is not None
                assert retry_owner["attempts"] == 1
                raise OSError("coordinator oracle: first final save fails")
        return save_original(session, *args, **kwargs)

    monkeypatch.setattr(Session, "save", fail_first_recovered_save)
    assert streaming._reconcile_cancelled_stream_generation(sid, old_stream) is False
    assert _wait_for_cancel_reconciliation(
        lambda: (sid, old_stream) not in config.STREAM_CANCEL_GENERATIONS
    ), "retry owner did not converge without another event"

    assert len(recovered_save_attempts) == 2
    assert len(config.STREAM_CANCEL_GENERATIONS) == baseline_generations
    assert len(
        getattr(config, "STREAM_CANCEL_RECONCILIATION_RETRY_OWNERS", ())
    ) == baseline_retry_owners
    assert config.session_writeback_owner(sid) == successor_stream
    assert not getattr(config, "STREAM_CANCEL_RECONCILIATION_DEAD_LETTERS", ())

    reloaded = Session.load(sid)
    assert reloaded is not None
    recovered_rows = [
        (index, row)
        for index, row in enumerate(reloaded.messages)
        if isinstance(row, dict)
        and row.get("_recovered_from_run_journal") is True
        and row.get("_recovered_stream_id") == old_stream
    ]
    assert [row.get("content") for _, row in recovered_rows] == [late_text]
    marker_index = next(
        index for index, row in enumerate(reloaded.messages) if row.get("_error") is True
    )
    assert recovered_rows[0][0] < marker_index
    recovered_tools = [
        tool
        for tool in reloaded.tool_calls
        if tool.get("_recovered_from_run_journal") is True
        and tool.get("_recovered_stream_id") == old_stream
    ]
    assert len(recovered_tools) == 1
    assert recovered_tools[0]["name"] == "terminal"
    assert recovered_tools[0]["preview"] == "retry-once"
    assert recovered_tools[0]["done"] is True
    assert recovered_tools[0]["assistant_msg_idx"] == recovered_rows[0][0]
    assert sum(
        isinstance(row, dict)
        and row.get("content") == late_text
        for row in reloaded.context_messages
    ) == 1


def test_final_cancel_reconciliation_persistent_failure_dead_letters_bounded_owner(monkeypatch):
    sid = "issue6920_retry_dead_letter"
    old_stream = "stream-retry-dead-letter"
    successor_stream = "stream-retry-dead-letter-successor"
    late_text = "Journal remains durable after bounded retry exhaustion."
    successor_prompt = "Successor turn must survive restart recovery."
    successor_tool = "restart_successor_tool"
    baseline_generations = len(config.STREAM_CANCEL_GENERATIONS)
    baseline_owners = len(config.SESSION_WRITEBACK_OWNERS)

    _arm_quiescent_cancel_reconciliation(sid, old_stream)
    writer = RunJournalWriter(sid, old_stream)
    writer.append_sse_event("token", {"text": late_text})
    writer.append_sse_event(
        "tool",
        {
            "name": "terminal",
            "preview": "printf durable-restart",
            "args": {"command": "printf durable-restart"},
        },
    )
    writer.append_sse_event(
        "tool_complete",
        {"name": "terminal", "preview": "durable-restart", "is_error": False},
    )
    assert writer._path.is_file()
    durable_before_retry = Session.load(sid)
    assert durable_before_retry is not None
    cancellation_marker = next(
        row
        for row in durable_before_retry.messages
        if isinstance(row, dict) and row.get("_error") is True
    )
    expected_cancellation_content = cancellation_marker["content"]
    assert cancellation_marker.get("_pending_journal_recovery") is True
    assert cancellation_marker.get("_journal_retry_stream_id") == old_stream
    assert config.mark_stream_worker_done(sid, old_stream) is True
    successor = models.get_session(sid)
    assert successor is not None
    _seed_successor_progress(successor, successor_prompt, successor_tool)
    config.register_session_writeback_owner(sid, successor_stream)
    expected_successor_messages = copy.deepcopy(successor.messages[-2:])
    expected_successor_tool = copy.deepcopy(successor.tool_calls[-1])

    monkeypatch.setattr(
        streaming,
        "_CANCEL_RECONCILIATION_RETRY_DELAYS",
        (0.01, 0.02),
        raising=False,
    )
    save_original = Session.save
    recovered_save_attempts = {"count": 0}

    def always_fail_recovered_save(session, *args, **kwargs):
        has_exact_recovery = any(
            isinstance(row, dict)
            and row.get("_recovered_from_run_journal") is True
            and row.get("_recovered_stream_id") == old_stream
            for row in getattr(session, "messages", [])
        )
        if has_exact_recovery:
            recovered_save_attempts["count"] += 1
            raise OSError("coordinator oracle: persistent final save failure")
        return save_original(session, *args, **kwargs)

    monkeypatch.setattr(Session, "save", always_fail_recovered_save)
    assert streaming._reconcile_cancelled_stream_generation(sid, old_stream) is False
    assert _wait_for_cancel_reconciliation(
        lambda: (
            (sid, old_stream) not in config.STREAM_CANCEL_GENERATIONS
            and not getattr(
                config,
                "STREAM_CANCEL_RECONCILIATION_RETRY_OWNERS",
                {(sid, old_stream)},
            )
        )
    ), "bounded retry owner did not retire after persistent failure"

    assert recovered_save_attempts["count"] == 3
    assert len(config.STREAM_CANCEL_GENERATIONS) == baseline_generations
    assert len(config.SESSION_WRITEBACK_OWNERS) == baseline_owners + 1
    assert config.session_writeback_owner(sid) == successor_stream
    assert writer._path.is_file()

    dead_letters = list(
        getattr(config, "STREAM_CANCEL_RECONCILIATION_DEAD_LETTERS", ())
    )
    assert len(dead_letters) == 1
    assert dead_letters[0]["session_id"] == sid
    assert dead_letters[0]["stream_id"] == old_stream
    assert dead_letters[0]["attempts"] == 3
    assert dead_letters[0]["error_type"] == "OSError"

    durable = Session.load(sid)
    assert durable is not None
    assert not any(
        isinstance(row, dict)
        and row.get("_recovered_stream_id") == old_stream
        for row in durable.messages
    )

    # Simulate a managed restart: every volatile owner/diagnostic/cache is gone,
    # while the session file and exact run journal remain. The normal production
    # get_session() path must consume the durable hook without another event.
    monkeypatch.setattr(Session, "save", save_original)
    models.SESSIONS.clear()
    models._JOURNAL_RETRY_LOCKS.clear()
    config.STREAM_CANCEL_GENERATIONS.clear()
    config.STREAM_CANCEL_RECONCILIATION_RETRY_OWNERS.clear()
    config.STREAM_CANCEL_RECONCILIATION_DEAD_LETTERS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()

    recovered = models.get_session(sid)
    assert recovered is not None
    assert sum(
        isinstance(row, dict)
        and row.get("content") == late_text
        and row.get("_recovered_stream_id") == old_stream
        for row in recovered.messages
    ) == 1
    recovered_tools = [
        tool
        for tool in recovered.tool_calls
        if isinstance(tool, dict)
        and tool.get("_recovered_stream_id") == old_stream
    ]
    assert len(recovered_tools) == 1
    assert recovered_tools[0]["name"] == "terminal"
    assert recovered.messages[-2:] == expected_successor_messages
    assert recovered.tool_calls[-1]["name"] == expected_successor_tool["name"]
    successor_owner = recovered.tool_calls[-1]["assistant_msg_idx"]
    assert recovered.messages[successor_owner] == expected_successor_messages[-1]
    marker = next(
        row
        for row in recovered.messages
        if isinstance(row, dict) and row.get("_error") is True
    )
    assert marker["content"] == expected_cancellation_content
    assert marker.get("_pending_journal_recovery") is None
    assert marker.get("_journal_retry_stream_id") is None

    models.SESSIONS.clear()
    recovered_again = models.get_session(sid)
    assert recovered_again is not None
    assert sum(
        isinstance(row, dict)
        and row.get("content") == late_text
        for row in recovered_again.messages
    ) == 1
    assert len([
        tool
        for tool in recovered_again.tool_calls
        if isinstance(tool, dict)
        and tool.get("_recovered_stream_id") == old_stream
    ]) == 1


def test_cancel_reconciliation_backoff_abandonment_recovers_after_restart(monkeypatch):
    sid = "issue6920_retry_backoff_restart"
    old_stream = "stream-retry-backoff-restart"
    late_text = "Backoff abandonment remains restart recoverable."

    _arm_quiescent_cancel_reconciliation(sid, old_stream)
    writer = RunJournalWriter(sid, old_stream)
    writer.append_sse_event("token", {"text": late_text})
    assert config.mark_stream_worker_done(sid, old_stream) is True

    save_original = Session.save
    failed_once = threading.Event()
    wait_entered = threading.Event()
    release_wait = threading.Event()

    def fail_first_recovered_save(session, *args, **kwargs):
        if not failed_once.is_set() and any(
            isinstance(row, dict)
            and row.get("_recovered_stream_id") == old_stream
            for row in getattr(session, "messages", [])
        ):
            failed_once.set()
            raise OSError("coordinator oracle: abandon retry during backoff")
        return save_original(session, *args, **kwargs)

    def block_retry_wait(_delay):
        wait_entered.set()
        assert release_wait.wait(timeout=10), "abandoned retry wait was not released"

    monkeypatch.setattr(Session, "save", fail_first_recovered_save)
    monkeypatch.setattr(streaming, "_CANCEL_RECONCILIATION_RETRY_DELAYS", (1.0,))
    monkeypatch.setattr(streaming, "_cancel_reconciliation_retry_wait", block_retry_wait)

    assert streaming._reconcile_cancelled_stream_generation(sid, old_stream) is False
    assert failed_once.wait(timeout=5)
    assert wait_entered.wait(timeout=5)

    # Drop all process-local ownership while the old daemon is parked in its
    # backoff, then recover solely through the durable marker and real loader.
    monkeypatch.setattr(Session, "save", save_original)
    durable_hook = Session.load(sid)
    assert durable_hook is not None
    durable_marker = next(
        row
        for row in durable_hook.messages
        if isinstance(row, dict) and row.get("_error") is True
    )
    durable_marker["_journal_retry_attempts"] = models._JOURNAL_RETRY_MAX_ATTEMPTS
    durable_marker["_journal_retry_first_seen_ts"] = (
        time.time() - models._JOURNAL_RETRY_GIVEUP_SECONDS - 1
    )
    save_original(durable_hook)
    models.SESSIONS.clear()
    models._JOURNAL_RETRY_LOCKS.clear()
    config.STREAM_CANCEL_GENERATIONS.clear()
    config.STREAM_CANCEL_RECONCILIATION_RETRY_OWNERS.clear()
    config.STREAM_CANCEL_RECONCILIATION_DEAD_LETTERS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()
    recovered = models.get_session(sid)
    release_wait.set()

    assert recovered is not None
    assert sum(
        isinstance(row, dict)
        and row.get("content") == late_text
        and row.get("_recovered_stream_id") == old_stream
        for row in recovered.messages
    ) == 1
    marker = next(
        row
        for row in recovered.messages
        if isinstance(row, dict) and row.get("_error") is True
    )
    assert marker.get("_pending_journal_recovery") is None


def test_cancel_restart_retires_hook_when_exact_journal_was_already_materialized():
    sid = "issue6920_cancel_restart_already_materialized"
    old_stream = "stream-cancel-restart-already-materialized"
    late_text = "Cancellation save already materialized this exact journal row."

    _session(sid, old_stream)
    _start_cancel_state(sid, old_stream)
    writer = RunJournalWriter(sid, old_stream)
    writer.append_sse_event("token", {"text": late_text})

    assert cancel_stream(old_stream) is True
    durable = Session.load(sid)
    assert durable is not None
    assert sum(
        isinstance(row, dict)
        and row.get("content") == late_text
        and row.get("_recovered_stream_id") == old_stream
        for row in durable.messages
    ) == 1
    marker = next(
        row
        for row in durable.messages
        if isinstance(row, dict) and row.get("_error") is True
    )
    assert marker.get("_pending_journal_recovery") is True

    # Crash before worker teardown/final reconciliation: only the sidecar and
    # exact journal survive. The normal loader must recognize that every exact
    # visible event is already represented and retire the hook transactionally.
    models.SESSIONS.clear()
    models._JOURNAL_RETRY_LOCKS.clear()
    config.STREAM_CANCEL_GENERATIONS.clear()
    config.STREAM_CANCEL_RECONCILIATION_RETRY_OWNERS.clear()
    config.STREAM_CANCEL_RECONCILIATION_DEAD_LETTERS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()

    recovered = models.get_session(sid)
    assert recovered is not None
    assert sum(
        isinstance(row, dict)
        and row.get("content") == late_text
        and row.get("_recovered_stream_id") == old_stream
        for row in recovered.messages
    ) == 1
    marker = next(
        row
        for row in recovered.messages
        if isinstance(row, dict) and row.get("_error") is True
    )
    assert marker.get("_pending_journal_recovery") is None
    assert marker.get("_journal_retry_stream_id") is None


def test_cancel_restart_splices_late_suffix_without_repeating_materialized_prefix():
    sid = "issue6920_cancel_restart_late_suffix"
    old_stream = "stream-cancel-restart-late-suffix"
    early_text = "Prefix materialized by cancellation."
    late_text = " Late suffix admitted before the crash."

    _session(sid, old_stream)
    _start_cancel_state(sid, old_stream)
    writer = RunJournalWriter(sid, old_stream)
    writer.append_sse_event("token", {"text": early_text})
    assert cancel_stream(old_stream) is True

    # This second token belongs to the same exact journal prose segment, but
    # arrives after the cancellation save and before worker teardown/reconcile.
    writer.append_sse_event("token", {"text": late_text})
    models.SESSIONS.clear()
    models._JOURNAL_RETRY_LOCKS.clear()
    config.STREAM_CANCEL_GENERATIONS.clear()
    config.STREAM_CANCEL_RECONCILIATION_RETRY_OWNERS.clear()
    config.STREAM_CANCEL_RECONCILIATION_DEAD_LETTERS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()

    recovered = models.get_session(sid)
    assert recovered is not None
    exact_rows = [
        row
        for row in recovered.messages
        if isinstance(row, dict)
        and row.get("_recovered_stream_id") == old_stream
    ]
    assert "".join(str(row.get("content") or "") for row in exact_rows) == (
        early_text + late_text
    )
    assert sum(early_text in str(row.get("content") or "") for row in exact_rows) == 1
    marker_index = next(
        index
        for index, row in enumerate(recovered.messages)
        if isinstance(row, dict) and row.get("_error") is True
    )
    assert all(recovered.messages.index(row) < marker_index for row in exact_rows)
    marker = recovered.messages[marker_index]
    assert marker.get("_pending_journal_recovery") is None


def test_cancel_restart_extends_live_buffer_prefix_without_duplicate():
    sid = "issue6920_cancel_restart_live_prefix"
    old_stream = "stream-cancel-restart-live-prefix"
    early_text = "Live prefix persisted by cancellation."
    late_text = " Late journal suffix before the crash."

    _session(sid, old_stream)
    _start_cancel_state(sid, old_stream)
    writer = RunJournalWriter(sid, old_stream)
    writer.append_sse_event("token", {"text": early_text})
    config.STREAM_PARTIAL_TEXT[old_stream] = early_text
    assert cancel_stream(old_stream) is True

    durable = Session.load(sid)
    assert durable is not None
    assert [
        row.get("content")
        for row in durable.messages
        if isinstance(row, dict) and row.get("_partial") is True
    ] == [early_text]

    # The live buffer row has no journal event metadata. A late token extends
    # that same visible segment before the crash; restart must adopt and extend
    # the existing row rather than append the full segment a second time.
    writer.append_sse_event("token", {"text": late_text})
    models.SESSIONS.clear()
    models._JOURNAL_RETRY_LOCKS.clear()
    config.STREAM_CANCEL_GENERATIONS.clear()
    config.STREAM_CANCEL_RECONCILIATION_RETRY_OWNERS.clear()
    config.STREAM_CANCEL_RECONCILIATION_DEAD_LETTERS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()

    recovered = models.get_session(sid)
    assert recovered is not None
    expected = early_text + late_text
    recovered_partials = [
        row
        for row in recovered.messages
        if isinstance(row, dict) and row.get("_partial") is True
    ]
    assert [row.get("content") for row in recovered_partials] == [expected]
    assert recovered_partials[0].get("_recovered_stream_id") == old_stream
    assert sum(
        isinstance(row, dict) and row.get("content") == expected
        for row in recovered.context_messages
    ) == 1
    marker = next(
        row
        for row in recovered.messages
        if isinstance(row, dict) and row.get("_error") is True
    )
    assert marker.get("_pending_journal_recovery") is None


def test_cancel_restart_applies_late_tool_completion_before_retiring_hook():
    sid = "issue6920_cancel_restart_late_tool_complete"
    old_stream = "stream-cancel-restart-late-tool-complete"
    first_tool_id = "late-tool-first"
    second_tool_id = "late-tool-second"

    _session(sid, old_stream)
    _start_cancel_state(sid, old_stream)
    writer = RunJournalWriter(sid, old_stream)
    writer.append_sse_event(
        "tool",
        {
            "name": "terminal",
            "preview": "printf first",
            "args": {"command": "printf first"},
            "tid": first_tool_id,
        },
    )
    writer.append_sse_event(
        "tool",
        {
            "name": "terminal",
            "preview": "printf second",
            "args": {"command": "printf second"},
            "tid": second_tool_id,
        },
    )
    assert cancel_stream(old_stream) is True

    durable = Session.load(sid)
    assert durable is not None
    exact_tools = [
        tool
        for tool in durable.tool_calls
        if isinstance(tool, dict)
        and tool.get("_recovered_stream_id") == old_stream
    ]
    assert {tool["tid"] for tool in exact_tools} == {first_tool_id, second_tool_id}
    assert all(tool["done"] is False for tool in exact_tools)

    # The completion arrives after cancellation persisted the tool card but
    # before the process dies. Restart recovery must update that exact card,
    # not treat the earlier tool-start event as complete coverage.
    writer.append_sse_event(
        "tool_complete",
        {
            "name": "terminal",
            "preview": "late-complete",
            "duration": 0.25,
            "is_error": False,
            "tid": first_tool_id,
        },
    )
    models.SESSIONS.clear()
    models._JOURNAL_RETRY_LOCKS.clear()
    config.STREAM_CANCEL_GENERATIONS.clear()
    config.STREAM_CANCEL_RECONCILIATION_RETRY_OWNERS.clear()
    config.STREAM_CANCEL_RECONCILIATION_DEAD_LETTERS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()

    recovered = models.get_session(sid)
    assert recovered is not None
    recovered_tools = [
        tool
        for tool in recovered.tool_calls
        if isinstance(tool, dict)
        and tool.get("_recovered_stream_id") == old_stream
    ]
    assert {tool["tid"] for tool in recovered_tools} == {first_tool_id, second_tool_id}
    by_tid = {tool["tid"]: tool for tool in recovered_tools}
    assert by_tid[first_tool_id]["done"] is True
    assert by_tid[first_tool_id]["preview"] == "late-complete"
    assert by_tid[first_tool_id]["duration"] == 0.25
    assert by_tid[second_tool_id]["done"] is False
    assert by_tid[second_tool_id]["preview"] == "printf second"
    marker = next(
        row
        for row in recovered.messages
        if isinstance(row, dict) and row.get("_error") is True
    )
    assert marker.get("_pending_journal_recovery") is None
