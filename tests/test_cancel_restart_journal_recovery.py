"""Focused restart durability for journal-only output after WebUI Stop."""

from __future__ import annotations

import base64
import copy
import struct
import zlib
import queue
import threading
import time
from unittest.mock import Mock

import pytest

import api.config as config
import api.models as models
from api.models import Session
from api.helpers import public_session_projection
from api.run_journal import RunJournalWriter
from api.streaming import cancel_stream


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")

    models.SESSIONS.clear()
    models._JOURNAL_RETRY_LOCKS.clear()
    for name in (
        "STREAMS",
        "CANCEL_FLAGS",
        "AGENT_INSTANCES",
        "STREAM_PARTIAL_TEXT",
        "STREAM_REASONING_TEXT",
        "STREAM_LIVE_TOOL_CALLS",
        "ACTIVE_RUNS",
        "STREAM_SESSION_OWNERS",
        "SESSION_WRITEBACK_OWNERS",
    ):
        getattr(config, name).clear()
    config.SESSION_AGENT_LOCKS.clear()
    yield
    models.SESSIONS.clear()
    models._JOURNAL_RETRY_LOCKS.clear()
    for name in (
        "STREAMS",
        "CANCEL_FLAGS",
        "AGENT_INSTANCES",
        "STREAM_PARTIAL_TEXT",
        "STREAM_REASONING_TEXT",
        "STREAM_LIVE_TOOL_CALLS",
        "ACTIVE_RUNS",
        "STREAM_SESSION_OWNERS",
        "SESSION_WRITEBACK_OWNERS",
    ):
        getattr(config, name).clear()
    config.SESSION_AGENT_LOCKS.clear()


def _start_cancelled_turn(sid: str, stream_id: str) -> Session:
    session = Session(
        session_id=sid,
        title="cancel restart recovery",
        messages=[],
        context_messages=[],
        pending_user_message="Do the cancellable task.",
        pending_started_at=10.0,
        pending_user_source="webui",
        active_stream_id=stream_id,
    )
    session.save()
    models.SESSIONS[sid] = session

    config.STREAMS[stream_id] = queue.Queue()
    config.CANCEL_FLAGS[stream_id] = threading.Event()
    agent = Mock()
    agent.session_id = sid
    agent.interrupt = Mock()
    config.AGENT_INSTANCES[stream_id] = agent
    config.ACTIVE_RUNS[stream_id] = {
        "session_id": sid,
        "backend": "legacy",
        "phase": "running",
        "started_at": time.time(),
    }
    return session


def _cancel_marker(session: Session) -> tuple[int, dict]:
    for index, row in enumerate(session.messages):
        if not isinstance(row, dict) or row.get("role") != "assistant":
            continue
        if row.get("_error") is True and "cancel" in str(row.get("content") or "").lower():
            return index, row
    raise AssertionError("cancel marker missing")


def _simulate_restart() -> None:
    # The production token changes at interpreter restart. Rotate it in
    # process here so the durable sidecar exercises that exact ownership edge.
    models._JOURNAL_RECOVERY_PROCESS_TOKEN = f"restart-{time.time_ns()}"
    models.SESSIONS.clear()
    models._JOURNAL_RETRY_LOCKS.clear()
    config.ACTIVE_RUNS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    config.STREAM_REASONING_TEXT.clear()
    config.STREAM_LIVE_TOOL_CALLS.clear()
    config.STREAM_SESSION_OWNERS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()
    config.SESSION_AGENT_LOCKS.clear()


@pytest.mark.parametrize("previous_exchange", [False, True])
@pytest.mark.parametrize("saved_context", [False, True])
@pytest.mark.parametrize("state_db_owner", [False, True])
@pytest.mark.parametrize("partial", [
    "A useful partial answer",
    "```python\nprint(42)\n```",
    "- first\n- second\n\n1. third",
])
def test_stop_saved_partial_survives_next_send(
    previous_exchange, saved_context, state_db_owner, partial,
):
    from api.streaming import (
        _build_partial_message,
        _sanitize_messages_for_agent,
        build_active_turn_token,
    )

    sid = "stop-saved-partial-history"
    stream_id = "stream-stop-saved-partial-history"
    session = _start_cancelled_turn(sid, stream_id)
    previous = [
        {"role": "user", "content": "Earlier question", "timestamp": 1},
        {"role": "assistant", "content": "Earlier answer", "timestamp": 2},
    ] if previous_exchange else []
    owner = {"role": "user", "content": session.pending_user_message, "timestamp": 10}
    models.stamp_message_source(
        owner, "webui", active_turn_token=build_active_turn_token(stream_id, 10),
    )
    session.messages = copy.deepcopy(previous)
    session.context_messages = (
        copy.deepcopy(previous + [owner, _build_partial_message(partial, "", [])])
        if saved_context else []
    )
    original_context = copy.deepcopy(session.context_messages)
    session.save()
    config.STREAM_PARTIAL_TEXT[stream_id] = partial

    assert cancel_stream(stream_id) is True
    # Stop must not change the authoritative provider snapshot for the live
    # partial path, or create the journal-only provisional user boundary.
    stopped = Session.load(sid)
    assert stopped.context_messages == original_context
    visible_owner = next(row for row in stopped.messages if row.get("content") == owner["content"])
    assert not visible_owner.get("_recovered")
    _, marker = _cancel_marker(stopped)
    assert not marker.get("_pending_journal_recovery")

    _simulate_restart()
    stopped = models.get_session(sid)
    state_messages = copy.deepcopy(previous + [owner]) if state_db_owner else []
    history = _sanitize_messages_for_agent(
        models.reconciled_state_db_messages_for_session(
            stopped, prefer_context=True, state_messages=state_messages,
        )
    )
    next_send = history + [{"role": "user", "content": "Next request"}]
    assert [(row["role"], row["content"]) for row in next_send] == [
        *((row["role"], row["content"]) for row in previous),
        ("user", owner["content"]),
        ("assistant", partial),
        ("user", "Next request"),
    ]


@pytest.mark.parametrize("previous_exchange", [False, True])
@pytest.mark.parametrize("raw_partial", ["   \n", "<think>unfinished trace</think>"])
def test_stop_without_model_visible_partial_keeps_journal_owner_provisional(
    previous_exchange, raw_partial,
):
    from api.streaming import _sanitize_messages_for_agent

    sid = "stop-empty-partial-history"
    stream_id = "stream-stop-empty-partial-history"
    session = _start_cancelled_turn(sid, stream_id)
    previous = [
        {"role": "user", "content": "Earlier question", "timestamp": 1},
        {"role": "assistant", "content": "Earlier answer", "timestamp": 2},
    ] if previous_exchange else []
    session.messages = copy.deepcopy(previous)
    session.context_messages = copy.deepcopy(previous)
    session.save()
    config.STREAM_PARTIAL_TEXT[stream_id] = raw_partial
    assert cancel_stream(stream_id) is True
    stopped = Session.load(sid)
    _, marker = _cancel_marker(stopped)
    assert marker.get("_pending_journal_recovery") is True
    _simulate_restart()
    stopped = models.get_session(sid)
    history = _sanitize_messages_for_agent(
        models.reconciled_state_db_messages_for_session(
            stopped, prefer_context=True, state_messages=[],
        )
    )
    assert [(row["role"], row["content"]) for row in history] == [
        (row["role"], row["content"]) for row in previous
    ]


@pytest.mark.parametrize("previous_exchange", [False, True])
@pytest.mark.parametrize("context_owner", ["absent", "tokenless", "exact", "missing-context"])
def test_stop_empty_journal_next_send_omits_unanswered_prompt(previous_exchange, context_owner):
    from api.streaming import _sanitize_messages_for_agent, build_active_turn_token

    sid = "stop-empty-journal-history"
    stream_id = "stream-stop-empty-journal-history"
    session = _start_cancelled_turn(sid, stream_id)
    previous = [
        {"role": "user", "content": "Earlier question", "timestamp": 1},
        {"role": "assistant", "content": "Earlier answer", "timestamp": 2},
    ] if previous_exchange else []
    session.messages = copy.deepcopy(previous)
    session.context_messages = copy.deepcopy(previous)
    if context_owner in {"tokenless", "exact"}:
        owner = {"role": "user", "content": session.pending_user_message, "timestamp": 10}
        models.stamp_message_source(owner, "webui")
        if context_owner == "exact":
            owner["_active_turn_token"] = build_active_turn_token(stream_id, 10)
        session.messages.append(copy.deepcopy(owner))
        session.context_messages.append(copy.deepcopy(owner))
    elif context_owner == "missing-context":
        session.context_messages = None
    session.save()

    assert cancel_stream(stream_id) is True
    # Real Stop, a cold sidecar read, then the same history boundaries used by
    # the next send. No journal assistant output exists to answer this prompt.
    _simulate_restart()
    stopped = models.get_session(sid)
    history = _sanitize_messages_for_agent(
        models.reconciled_state_db_messages_for_session(
            stopped, prefer_context=True, state_messages=[],
        )
    )
    next_send = history + [{"role": "user", "content": "Next request"}]
    assert [(row["role"], row["content"]) for row in next_send] == [
        *((row["role"], row["content"]) for row in previous),
        ("user", "Next request"),
    ]
    assert any(row.get("content") == "Do the cancellable task." for row in stopped.messages)


@pytest.mark.parametrize("previous_exchange", [False, True])
@pytest.mark.parametrize("journal_output", ["token", "reasoning"])
def test_stop_journal_owner_promotes_only_after_model_visible_answer(previous_exchange, journal_output):
    from api.streaming import _sanitize_messages_for_agent

    sid = "stop-journal-answer-history"
    stream_id = "stream-stop-journal-answer-history"
    session = _start_cancelled_turn(sid, stream_id)
    previous = [
        {"role": "user", "content": "Earlier question", "timestamp": 1},
        {"role": "assistant", "content": "Earlier answer", "timestamp": 2},
    ] if previous_exchange else []
    session.messages = copy.deepcopy(previous)
    session.context_messages = copy.deepcopy(previous)
    session.save()
    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event(journal_output, {"text": "Recovered answer"})
    assert cancel_stream(stream_id) is True
    provisional = Session.load(sid)
    owner = next(row for row in provisional.context_messages if row.get("role") == "user" and row.get("content") == "Do the cancellable task.")
    assert owner.get("_recovered") is True
    _simulate_restart()
    recovered = models.get_session(sid)
    owner = next(row for row in recovered.context_messages if row.get("role") == "user" and row.get("content") == "Do the cancellable task.")
    history = _sanitize_messages_for_agent(
        models.reconciled_state_db_messages_for_session(recovered, prefer_context=True, state_messages=[])
    )
    expected = [(row["role"], row["content"]) for row in previous]
    if journal_output == "token":
        assert not owner.get("_recovered")
        expected += [("user", "Do the cancellable task."), ("assistant", "Recovered answer")]
    else:
        assert owner.get("_recovered") is True
    assert [(row["role"], row["content"]) for row in history] == expected


def test_cancel_retry_metadata_stays_server_private():
    sid = "cancel-restart-public-scrub"
    stream_id = "stream-cancel-restart-public-scrub"

    _start_cancelled_turn(sid, stream_id)
    RunJournalWriter(sid, stream_id).append_sse_event(
        "token", {"text": "private recovery metadata proof"}
    )
    assert cancel_stream(stream_id) is True

    durable = Session.load(sid)
    assert durable is not None
    _, marker = _cancel_marker(durable)
    assert marker["_pending_journal_recovery"] is True
    assert marker["_journal_retry_process_token"]
    assert marker["_journal_retry_owner_token"]

    public = public_session_projection({"messages": durable.messages})
    public_marker = next(
        row
        for row in public["messages"]
        if isinstance(row, dict) and row.get("_error") is True
    )
    for field in (
        "_pending_journal_recovery",
        "_journal_retry_stream_id",
        "_journal_retry_attempts",
        "_journal_retry_first_seen_ts",
        "_journal_retry_kind",
        "_journal_retry_owner_token",
        "_journal_retry_process_token",
    ):
        assert field not in public_marker


def test_cancel_restart_recovers_exact_journal_before_successor():
    sid = "cancel-restart-successor"
    stream_id = "stream-cancel-restart-successor"
    early = "Journal-only prefix before Stop."
    late = " Late suffix before process loss."

    _start_cancelled_turn(sid, stream_id)
    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event("token", {"text": early})

    assert cancel_stream(stream_id) is True
    cancelled = Session.load(sid)
    assert cancelled is not None
    marker_index, marker = _cancel_marker(cancelled)
    assert marker.get("_pending_journal_recovery") is True
    assert marker.get("_journal_retry_kind") == "cancelled"
    assert marker.get("_journal_retry_stream_id") == stream_id
    assert not any(
        isinstance(row, dict) and row.get("_recovered_stream_id") == stream_id
        for row in cancelled.messages
    )

    # A same-session successor can be saved before the old process disappears.
    successor_user = {"role": "user", "content": "Successor prompt.", "timestamp": 20}
    successor_assistant = {"role": "assistant", "content": "Successor answer.", "timestamp": 21}
    cancelled.messages.extend([copy.deepcopy(successor_user), copy.deepcopy(successor_assistant)])
    cancelled.context_messages.extend([copy.deepcopy(successor_user), copy.deepcopy(successor_assistant)])
    cancelled.save()

    # The old stream publishes one last durable token, then the process dies.
    writer.append_sse_event("token", {"text": late})
    _simulate_restart()

    recovered = models.get_session(sid)
    exact_rows = [
        row
        for row in recovered.messages
        if isinstance(row, dict) and row.get("_recovered_stream_id") == stream_id
    ]
    assert [row.get("content") for row in exact_rows] == [early + late]

    marker_index, marker = _cancel_marker(recovered)
    recovered_index = recovered.messages.index(exact_rows[0])
    successor_user_index = next(
        index
        for index, row in enumerate(recovered.messages)
        if isinstance(row, dict) and row.get("content") == successor_user["content"]
    )
    successor_assistant_row = next(
        row
        for row in recovered.messages
        if isinstance(row, dict) and row.get("content") == successor_assistant["content"]
    )
    assert recovered_index < marker_index < successor_user_index
    assert successor_assistant_row == successor_assistant
    assert marker.get("_pending_journal_recovery") is None
    assert marker.get("_journal_retry_stream_id") is None

    context_contents = [
        row.get("content")
        for row in recovered.context_messages
        if isinstance(row, dict)
    ]
    assert context_contents == [
        "Do the cancellable task.",
        early + late,
        successor_user["content"],
        successor_assistant["content"],
    ]


def test_cancel_lazy_recovery_waits_for_old_worker_to_retire():
    sid = "cancel-restart-active-owner"
    stream_id = "stream-cancel-restart-active-owner"
    text = "Durable output while the cancelled worker is still unwinding."

    _start_cancelled_turn(sid, stream_id)
    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event("token", {"text": text})

    # #7188: cancel_stream() journals a durable terminal cancel row, and a
    # terminal row legitimately closes the same-process ambiguity. This test
    # exercises the NONTERMINAL half of the contract — the hook must stay
    # armed while the journal has no terminal row — so simulate a cancel whose
    # journal write failed (the fallback publish path) by suppressing the
    # durable terminal append before cancelling.
    import unittest.mock as _mock
    from api import run_journal as _run_journal
    with _mock.patch.object(
        _run_journal.RunJournalWriter,
        "close_acceptance_fence_and_publish_terminal",
        side_effect=OSError("journal write failed"),
    ):
        assert cancel_stream(stream_id) is True
    cached = models.get_session(sid)
    _, marker = _cancel_marker(cached)
    assert marker.get("_pending_journal_recovery") is True
    assert config.ACTIVE_RUNS.get(stream_id, {}).get("phase") == "cancelling"
    assert not any(
        isinstance(row, dict) and row.get("_recovered_stream_id") == stream_id
        for row in cached.messages
    )
    assert marker.get("_pending_journal_recovery") is True

    # Registry reclamation inside the same interpreter is not proof the
    # worker is dead. A nonterminal journal must keep the durable hook armed.
    config.ACTIVE_RUNS.clear()
    models.SESSIONS.clear()
    same_process = models.get_session(sid)
    assert not any(
        isinstance(row, dict) and row.get("_recovered_stream_id") == stream_id
        for row in same_process.messages
    )
    _, same_process_marker = _cancel_marker(same_process)
    assert same_process_marker.get("_pending_journal_recovery") is True

    # A real process restart changes the persisted process token. Only then can
    # an ordinary cold read consume a nonterminal exact-stream journal tail.
    _simulate_restart()
    recovered = models.get_session(sid)
    exact_rows = [
        row
        for row in recovered.messages
        if isinstance(row, dict) and row.get("_recovered_stream_id") == stream_id
    ]
    assert [row.get("content") for row in exact_rows] == [text]
    _, marker = _cancel_marker(recovered)
    assert marker.get("_pending_journal_recovery") is None


def test_cancel_restart_keeps_same_text_from_other_turns_distinct():
    sid = "cancel-restart-same-text"
    stream_id = "stream-cancel-restart-same-text"
    repeated = "The same assistant prose appears in three different turns."

    session = _start_cancelled_turn(sid, stream_id)
    historical_user = {"role": "user", "content": "Historical prompt.", "timestamp": 1}
    historical_assistant = {"role": "assistant", "content": repeated, "timestamp": 2}
    session.messages[:] = [copy.deepcopy(historical_user), copy.deepcopy(historical_assistant)]
    session.context_messages[:] = [copy.deepcopy(historical_user), copy.deepcopy(historical_assistant)]
    session.save()

    RunJournalWriter(sid, stream_id).append_sse_event("token", {"text": repeated})
    assert cancel_stream(stream_id) is True

    cancelled = Session.load(sid)
    assert cancelled is not None
    successor_user = {"role": "user", "content": "Successor prompt.", "timestamp": 30}
    successor_assistant = {"role": "assistant", "content": repeated, "timestamp": 31}
    cancelled.messages.extend([copy.deepcopy(successor_user), copy.deepcopy(successor_assistant)])
    cancelled.context_messages.extend([copy.deepcopy(successor_user), copy.deepcopy(successor_assistant)])
    cancelled.save()

    _simulate_restart()
    recovered = models.get_session(sid)
    same_text_rows = [
        row
        for row in recovered.messages
        if isinstance(row, dict)
        and row.get("role") == "assistant"
        and row.get("content") == repeated
    ]
    assert len(same_text_rows) == 3
    exact = [
        row for row in same_text_rows
        if row.get("_recovered_stream_id") == stream_id
    ]
    assert len(exact) == 1
    marker_index, _ = _cancel_marker(recovered)
    exact_index = recovered.messages.index(exact[0])
    successor_index = recovered.messages.index(
        next(row for row in recovered.messages if row.get("content") == successor_user["content"])
    )
    assert exact_index < marker_index < successor_index

    context_same_text = [
        row
        for row in recovered.context_messages
        if isinstance(row, dict)
        and row.get("role") == "assistant"
        and row.get("content") == repeated
    ]
    assert len(context_same_text) == 3
    recovered_context = [
        row
        for row in context_same_text
        if row.get("_recovered_stream_id") == stream_id
    ]
    assert len(recovered_context) == 1
    recovered_context_index = recovered.context_messages.index(recovered_context[0])
    successor_context_index = recovered.context_messages.index(
        next(
            row
            for row in recovered.context_messages
            if isinstance(row, dict) and row.get("content") == successor_user["content"]
        )
    )
    assert recovered_context_index < successor_context_index


def test_cancel_restart_failed_recovery_save_keeps_hook_for_next_read(monkeypatch):
    sid = "cancel-restart-save-failure"
    stream_id = "stream-cancel-restart-save-failure"
    text = "Journal recovery must remain retryable after a failed sidecar save."

    _start_cancelled_turn(sid, stream_id)
    RunJournalWriter(sid, stream_id).append_sse_event("token", {"text": text})
    assert cancel_stream(stream_id) is True
    _simulate_restart()

    original_save = Session.save
    failed = {"value": False}

    def fail_first_recovered_save(session, *args, **kwargs):
        if (
            not failed["value"]
            and any(
                isinstance(row, dict)
                and row.get("_recovered_stream_id") == stream_id
                for row in getattr(session, "messages", [])
            )
        ):
            failed["value"] = True
            raise OSError("synthetic recovered sidecar save failure")
        return original_save(session, *args, **kwargs)

    monkeypatch.setattr(Session, "save", fail_first_recovered_save)
    first = models.get_session(sid)
    assert failed["value"] is True
    assert not any(
        isinstance(row, dict) and row.get("_recovered_stream_id") == stream_id
        for row in first.messages
    )
    _, first_marker = _cancel_marker(first)
    assert first_marker.get("_pending_journal_recovery") is True

    durable = Session.load(sid)
    assert durable is not None
    assert not any(
        isinstance(row, dict) and row.get("_recovered_stream_id") == stream_id
        for row in durable.messages
    )
    _, durable_marker = _cancel_marker(durable)
    assert durable_marker.get("_pending_journal_recovery") is True

    monkeypatch.setattr(Session, "save", original_save)
    models.SESSIONS.clear()
    recovered = models.get_session(sid)
    exact = [
        row for row in recovered.messages
        if isinstance(row, dict) and row.get("_recovered_stream_id") == stream_id
    ]
    assert [row.get("content") for row in exact] == [text]
    _, marker = _cancel_marker(recovered)
    assert marker.get("_pending_journal_recovery") is None



def test_cancel_restart_tool_completion_id_falls_back_only_to_idless_start():
    sid = "cancel-restart-tool-idless-start"
    stream_id = "stream-cancel-restart-tool-idless-start"

    _start_cancelled_turn(sid, stream_id)
    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event(
        "tool",
        {"name": "terminal", "preview": "running", "args": {"command": "printf ok"}},
    )
    writer.append_sse_event(
        "tool_complete",
        {
            "name": "terminal",
            "tid": "gateway-completion-id",
            "preview": "done",
            "duration": 0.5,
            "is_error": False,
        },
    )
    assert cancel_stream(stream_id) is True

    _simulate_restart()
    recovered = models.get_session(sid)
    tools = [
        tool for tool in recovered.tool_calls
        if isinstance(tool, dict) and tool.get("_recovered_stream_id") == stream_id
    ]
    assert len(tools) == 1
    assert tools[0]["done"] is True
    assert tools[0]["preview"] == "done"
    assert tools[0]["duration"] == 0.5
    assert tools[0]["tid"].startswith("journal-")
    assert "_journal_synthetic_tid" not in tools[0]



def test_cancel_restart_tool_recovery_does_not_claim_successor_tool():
    sid = "cancel-restart-tool-owner"
    stream_id = "stream-cancel-restart-tool-owner"
    preview = "printf same-output"

    _start_cancelled_turn(sid, stream_id)
    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event(
        "tool",
        {
            "name": "terminal",
            "preview": preview,
            "args": {"command": "printf old-first"},
            "tid": "old-tool-first",
        },
    )
    writer.append_sse_event(
        "tool",
        {
            "name": "terminal",
            "preview": preview,
            "args": {"command": "printf old-second"},
            "tid": "old-tool-second",
        },
    )
    writer.append_sse_event(
        "tool_complete",
        {
            "name": "terminal",
            "preview": "old-first-complete",
            "duration": 0.25,
            "is_error": False,
            "tid": "old-tool-first",
        },
    )
    assert cancel_stream(stream_id) is True

    cancelled = Session.load(sid)
    assert cancelled is not None
    successor_user = {"role": "user", "content": "Run the successor tool.", "timestamp": 40}
    successor_assistant = {"role": "assistant", "content": "Successor tool done.", "timestamp": 41}
    cancelled.messages.extend([copy.deepcopy(successor_user), copy.deepcopy(successor_assistant)])
    cancelled.context_messages.extend([copy.deepcopy(successor_user), copy.deepcopy(successor_assistant)])
    successor_tool = {
        "name": "terminal",
        "preview": preview,
        "snippet": preview,
        "assistant_msg_idx": len(cancelled.messages) - 1,
        "done": True,
    }
    cancelled.tool_calls = [copy.deepcopy(successor_tool)]
    cancelled.save()

    _simulate_restart()
    recovered = models.get_session(sid)
    recovered_tools = [
        tool
        for tool in recovered.tool_calls
        if isinstance(tool, dict) and tool.get("_recovered_stream_id") == stream_id
    ]
    assert len(recovered_tools) == 2
    by_tid = {tool["tid"]: tool for tool in recovered_tools}
    assert set(by_tid) == {"old-tool-first", "old-tool-second"}
    assert by_tid["old-tool-first"]["done"] is True
    assert by_tid["old-tool-first"]["preview"] == "old-first-complete"
    assert by_tid["old-tool-first"]["duration"] == 0.25
    assert by_tid["old-tool-second"]["done"] is False
    assert by_tid["old-tool-second"]["preview"] == preview

    successor_tools = [
        tool
        for tool in recovered.tool_calls
        if isinstance(tool, dict) and not tool.get("_recovered_stream_id")
    ]
    assert len(successor_tools) == 1
    assert {
        key: successor_tools[0][key]
        for key in ("name", "preview", "snippet", "done")
    } == {
        key: successor_tool[key]
        for key in ("name", "preview", "snippet", "done")
    }
    successor_owner_index = successor_tools[0]["assistant_msg_idx"]
    assert recovered.messages[successor_owner_index].get("content") == successor_assistant["content"]

    marker_index, _ = _cancel_marker(recovered)
    for tool in recovered_tools:
        owner_index = tool["assistant_msg_idx"]
        assert owner_index < marker_index
        assert recovered.messages[owner_index].get("_recovered_stream_id") == stream_id


@pytest.mark.parametrize("new_runtime_active", [True, False])
def test_newer_blocked_cancel_does_not_hide_older_recoverable_hook(new_runtime_active):
    sid = "cancel-restart-two-hooks"
    old_stream = "stream-cancel-old-ready"
    new_stream = "stream-cancel-new-blocked"
    old_text = "Older cancelled output is already durable."
    process_token = models._JOURNAL_RECOVERY_PROCESS_TOKEN

    old_owner_token = "old-cancel-owner-token"
    new_owner_token = "new-cancel-owner-token"
    old_user = {
        "role": "user",
        "content": "Older prompt.",
        "timestamp": 10,
        "_active_turn_token": old_owner_token,
    }
    old_marker = {
        "role": "assistant",
        "content": "Task cancelled.",
        "_error": True,
        "timestamp": 11,
        "_pending_journal_recovery": True,
        "_journal_retry_kind": "cancelled",
        "_journal_retry_stream_id": old_stream,
        "_journal_retry_attempts": 0,
        "_journal_retry_first_seen_ts": int(time.time()),
        "_journal_retry_process_token": process_token,
        "_journal_retry_owner_token": old_owner_token,
    }
    new_user = {
        "role": "user",
        "content": "Newer prompt.",
        "timestamp": 20,
        "_active_turn_token": new_owner_token,
    }
    new_marker = {
        "role": "assistant",
        "content": "Task cancelled.",
        "_error": True,
        "timestamp": 21,
        "_pending_journal_recovery": True,
        "_journal_retry_kind": "cancelled",
        "_journal_retry_stream_id": new_stream,
        "_journal_retry_attempts": 0,
        "_journal_retry_first_seen_ts": int(time.time()),
        "_journal_retry_process_token": process_token,
        "_journal_retry_owner_token": new_owner_token,
    }
    session = Session(
        session_id=sid,
        title="two cancel hooks",
        messages=[
            copy.deepcopy(old_user),
            copy.deepcopy(old_marker),
            copy.deepcopy(new_user),
            copy.deepcopy(new_marker),
        ],
        context_messages=[copy.deepcopy(old_user), copy.deepcopy(new_user)],
    )
    session.save()

    old_writer = RunJournalWriter(sid, old_stream)
    old_writer.append_sse_event("token", {"text": old_text})
    old_writer.append_sse_event("cancel", {"message": "Cancelled by user"})

    new_writer = RunJournalWriter(sid, new_stream)
    new_writer.append_sse_event(
        "token", {"text": "Newer output remains owned by a live worker."}
    )
    if new_runtime_active:
        config.ACTIVE_RUNS[new_stream] = {
            "session_id": sid,
            "backend": "legacy",
            "phase": "cancelling",
            "started_at": time.time(),
        }

    models.SESSIONS.clear()
    recovered = models.get_session(sid)

    old_rows = [
        row for row in recovered.messages
        if isinstance(row, dict) and row.get("_recovered_stream_id") == old_stream
    ]
    assert [row.get("content") for row in old_rows] == [old_text]

    pending_by_stream = {
        str(row.get("_journal_retry_stream_id")): row
        for row in recovered.messages
        if isinstance(row, dict) and row.get("_journal_retry_kind") == "cancelled"
    }
    assert old_stream not in pending_by_stream
    assert pending_by_stream[new_stream].get("_pending_journal_recovery") is True


@pytest.mark.parametrize(
    ("newer_state", "expected_new_attempts"),
    [
        ("live", 0),
        ("nonterminal", 0),
        ("terminal-empty", 1),
    ],
)
def test_newer_cancel_hook_does_not_block_older_interrupted_recovery(
    newer_state, expected_new_attempts
):
    sid = f"cancel-vs-interrupted-{newer_state}"
    old_stream = f"stream-interrupted-old-{newer_state}"
    new_stream = f"stream-cancel-new-{newer_state}"
    old_text = "Older interrupted output is already recoverable."
    new_owner_token = f"new-owner-{newer_state}"

    old_user = {
        "role": "user",
        "content": "Older interrupted prompt.",
        "timestamp": 10,
    }
    old_marker = models._build_recovery_marker_with_retry_hook(
        recovered_output=False,
        stream_id=old_stream,
        pending_started_at=10,
    )
    new_user = {
        "role": "user",
        "content": "Newer cancelled prompt.",
        "timestamp": 20,
        "_active_turn_token": new_owner_token,
    }
    new_marker = {
        "role": "assistant",
        "content": "Task cancelled.",
        "_error": True,
        "timestamp": 21,
        "_pending_journal_recovery": True,
        "_journal_retry_kind": "cancelled",
        "_journal_retry_stream_id": new_stream,
        "_journal_retry_attempts": 0,
        "_journal_retry_first_seen_ts": int(time.time()),
        "_journal_retry_process_token": models._JOURNAL_RECOVERY_PROCESS_TOKEN,
        "_journal_retry_owner_token": new_owner_token,
    }
    session = Session(
        session_id=sid,
        title="cancel must not mask interrupted recovery",
        messages=[
            copy.deepcopy(old_user),
            copy.deepcopy(old_marker),
            copy.deepcopy(new_user),
            copy.deepcopy(new_marker),
        ],
        context_messages=[copy.deepcopy(old_user), copy.deepcopy(new_user)],
    )
    session.save()

    RunJournalWriter(sid, old_stream).append_sse_event(
        "token", {"text": old_text}
    )

    new_writer = RunJournalWriter(sid, new_stream)
    if newer_state == "live":
        config.ACTIVE_RUNS[new_stream] = {
            "session_id": sid,
            "backend": "legacy",
            "phase": "cancelling",
            "started_at": time.time(),
        }
    elif newer_state == "nonterminal":
        new_writer.append_sse_event(
            "token", {"text": "Newer cancelled output is still arriving."}
        )
    else:
        new_writer.append_sse_event(
            "cancel", {"message": "Cancelled by user"}
        )

    models.SESSIONS.clear()
    recovered = models.get_session(sid)

    old_rows = [
        row
        for row in recovered.messages
        if isinstance(row, dict)
        and row.get("_recovered_stream_id") == old_stream
    ]
    assert [row.get("content") for row in old_rows] == [old_text]

    recovered_old_marker = next(
        row
        for row in recovered.messages
        if isinstance(row, dict) and row.get("type") == "interrupted"
    )
    assert recovered_old_marker.get("_pending_journal_recovery") is None
    assert recovered_old_marker.get("_journal_retry_stream_id") is None

    pending_new = next(
        row
        for row in recovered.messages
        if isinstance(row, dict)
        and row.get("_journal_retry_stream_id") == new_stream
    )
    assert pending_new.get("_pending_journal_recovery") is True
    assert pending_new.get("_journal_retry_attempts") == expected_new_attempts


def test_cancel_restart_context_fails_closed_for_ambiguous_duplicate_prompt_and_timestamp():
    sid = "cancel-restart-duplicate-user-owner"
    stream_id = "stream-cancel-restart-duplicate-user-owner"
    prompt = "Repeat exactly the same prompt."
    timestamp = 10
    recovered_text = "Recovered output belongs to the second identical prompt."

    session = _start_cancelled_turn(sid, stream_id)
    historical_user = {
        "role": "user",
        "content": prompt,
        "timestamp": timestamp,
        "_owner_probe": "historical-owner",
        "_active_turn_token": "historical-owner-token",
    }
    historical_assistant = {
        "role": "assistant",
        "content": "Historical answer.",
        "timestamp": timestamp,
    }
    cancelled_user = {
        "role": "user",
        "content": prompt,
        "timestamp": timestamp,
        "_owner_probe": "cancelled-owner",
    }
    session.pending_user_message = prompt
    session.pending_started_at = float(timestamp)
    session.messages[:] = [
        copy.deepcopy(historical_user),
        copy.deepcopy(historical_assistant),
        copy.deepcopy(cancelled_user),
    ]
    session.context_messages[:] = copy.deepcopy(session.messages)
    session.save()

    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event("token", {"text": recovered_text})
    assert cancel_stream(stream_id) is True

    cancelled = Session.load(sid)
    assert cancelled is not None
    display_owner = next(
        row for row in cancelled.messages
        if isinstance(row, dict) and row.get("_owner_probe") == "cancelled-owner"
    )
    marker_index, marker = _cancel_marker(cancelled)
    owner_token = str(display_owner.get("_active_turn_token") or "")
    assert owner_token
    assert marker.get("_journal_retry_owner_token") == owner_token
    historical_context = next(
        row for row in cancelled.context_messages
        if isinstance(row, dict) and row.get("_owner_probe") == "historical-owner"
    )
    cancelled_context = next(
        row for row in cancelled.context_messages
        if isinstance(row, dict) and row.get("_owner_probe") == "cancelled-owner"
    )
    assert historical_context.get("_active_turn_token") == "historical-owner-token"
    assert not cancelled_context.get("_active_turn_token")

    successor_user = {"role": "user", "content": "Successor prompt.", "timestamp": 20}
    successor_assistant = {"role": "assistant", "content": "Successor answer.", "timestamp": 21}
    cancelled.messages.extend([copy.deepcopy(successor_user), copy.deepcopy(successor_assistant)])
    cancelled.context_messages.extend([copy.deepcopy(successor_user), copy.deepcopy(successor_assistant)])
    cancelled.save()

    _simulate_restart()
    recovered = models.get_session(sid)
    context = recovered.context_messages

    # The duplicate tokenless provider owner is ambiguous. Recovery remains
    # visible in the transcript, but provider context must not guess either
    # equal prompt as its owner or overwrite the historical token.
    assert not any(
        isinstance(row, dict)
        and row.get("_recovered_stream_id") == stream_id
        for row in context
    )
    historical_context = next(
        row for row in context
        if isinstance(row, dict) and row.get("_owner_probe") == "historical-owner"
    )
    assert historical_context.get("_active_turn_token") == "historical-owner-token"
    recovered_display = next(
        row for row in recovered.messages
        if isinstance(row, dict)
        and row.get("_recovered_stream_id") == stream_id
        and row.get("content") == recovered_text
    )
    display_index = recovered.messages.index(recovered_display)
    marker_index, _marker = _cancel_marker(recovered)
    successor_index = next(
        index for index, row in enumerate(recovered.messages)
        if isinstance(row, dict) and row.get("content") == successor_user["content"]
    )
    assert display_index < marker_index < successor_index



def test_cancel_restart_context_does_not_reassign_earlier_repeated_prompt_owner():
    sid = "cancel-restart-earlier-owner"
    stream_id = "stream-cancel-restart-earlier-owner"
    prompt = "Repeat prompt whose earlier owner must stay intact."
    timestamp = 10
    recovered_text = "Recovered output must not inherit the earlier prompt."

    session = _start_cancelled_turn(sid, stream_id)
    historical_user = {
        "role": "user",
        "content": prompt,
        "timestamp": timestamp,
        "_owner_probe": "historical-owner",
        "_active_turn_token": "historical-owner-token",
    }
    cancelled_user = {
        "role": "user",
        "content": prompt,
        "timestamp": timestamp,
        "_owner_probe": "cancelled-owner",
    }
    session.pending_user_message = prompt
    session.pending_started_at = float(timestamp)
    session.messages[:] = [
        copy.deepcopy(historical_user),
        {"role": "assistant", "content": "Historical answer.", "timestamp": timestamp},
        copy.deepcopy(cancelled_user),
    ]
    # The current pending owner has not reached provider context yet. The only
    # context user is an earlier equal prompt that already has a different token.
    session.context_messages[:] = [copy.deepcopy(historical_user)]
    session.save()

    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event("token", {"text": recovered_text})
    assert cancel_stream(stream_id) is True

    cancelled = Session.load(sid)
    assert cancelled is not None
    display_owner = next(
        row for row in cancelled.messages
        if isinstance(row, dict) and row.get("_owner_probe") == "cancelled-owner"
    )
    owner_token = str(display_owner.get("_active_turn_token") or "")
    assert owner_token
    marker_index, marker = _cancel_marker(cancelled)
    assert marker.get("_journal_retry_owner_token") == owner_token

    historical_context = next(
        row for row in cancelled.context_messages
        if isinstance(row, dict) and row.get("_owner_probe") == "historical-owner"
    )
    assert historical_context.get("_active_turn_token") == "historical-owner-token"
    assert all(
        not (
            isinstance(row, dict)
            and row.get("role") == "user"
            and row.get("_active_turn_token") == owner_token
        )
        for row in cancelled.context_messages
    )

    successor_user = {"role": "user", "content": "Successor prompt.", "timestamp": 20}
    successor_assistant = {"role": "assistant", "content": "Successor answer.", "timestamp": 21}
    cancelled.messages.extend([copy.deepcopy(successor_user), copy.deepcopy(successor_assistant)])
    cancelled.context_messages.extend([copy.deepcopy(successor_user), copy.deepcopy(successor_assistant)])
    cancelled.save()

    _simulate_restart()
    recovered = models.get_session(sid)

    assert not any(
        isinstance(row, dict)
        and row.get("_recovered_stream_id") == stream_id
        for row in recovered.context_messages
    )
    historical_context = next(
        row for row in recovered.context_messages
        if isinstance(row, dict) and row.get("_owner_probe") == "historical-owner"
    )
    assert historical_context.get("_active_turn_token") == "historical-owner-token"

    recovered_display = next(
        row for row in recovered.messages
        if isinstance(row, dict)
        and row.get("_recovered_stream_id") == stream_id
        and row.get("content") == recovered_text
    )
    display_index = recovered.messages.index(recovered_display)
    marker_index, _marker = _cancel_marker(recovered)
    successor_index = next(
        index for index, row in enumerate(recovered.messages)
        if isinstance(row, dict) and row.get("content") == successor_user["content"]
    )
    assert display_index < marker_index < successor_index
def test_cancel_restart_context_fails_closed_when_compression_removed_exact_owner():
    sid = "cancel-restart-compressed-owner-missing"
    stream_id = "stream-cancel-restart-compressed-owner-missing"
    prompt = "Cancelled prompt after compressed history."
    recovered_text = "Recovered output must stay out of context without its exact owner."

    session = _start_cancelled_turn(sid, stream_id)
    history = []
    for index in range(3):
        history.extend([
            {"role": "user", "content": f"Historical prompt {index}.", "timestamp": index * 2 + 1},
            {"role": "assistant", "content": f"Historical answer {index}.", "timestamp": index * 2 + 2},
        ])
    cancelled_user = {
        "role": "user",
        "content": prompt,
        "timestamp": 10,
        "_owner_probe": "cancelled-owner",
    }
    compression_summary = {
        "role": "user",
        "content": "[Earlier conversation compressed into summary.]",
        "timestamp": 9,
        "_owner_probe": "compression-summary",
    }
    session.pending_user_message = prompt
    session.pending_started_at = 10.0
    session.messages[:] = history + [copy.deepcopy(cancelled_user)]
    session.context_messages[:] = [
        copy.deepcopy(compression_summary),
        copy.deepcopy(cancelled_user),
    ]
    session.save()

    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event("token", {"text": recovered_text})
    assert cancel_stream(stream_id) is True

    cancelled = Session.load(sid)
    assert cancelled is not None
    successors = []
    for index in range(3):
        successors.extend([
            {"role": "user", "content": f"Successor prompt {index}.", "timestamp": 20 + index * 2},
            {"role": "assistant", "content": f"Successor answer {index}.", "timestamp": 21 + index * 2},
        ])
    cancelled.messages.extend(copy.deepcopy(successors))
    # Simulate a later compression that retained the summary and successors but
    # removed the cancelled turn's provider-context owner.
    cancelled.context_messages[:] = [copy.deepcopy(compression_summary)] + copy.deepcopy(successors)
    cancelled.save()

    _simulate_restart()
    recovered = models.get_session(sid)

    recovered_display = [
        row for row in recovered.messages
        if isinstance(row, dict)
        and row.get("_recovered_stream_id") == stream_id
        and row.get("content") == recovered_text
    ]
    assert len(recovered_display) == 1
    marker_index, _marker = _cancel_marker(recovered)
    display_index = recovered.messages.index(recovered_display[0])
    first_successor_index = next(
        index for index, row in enumerate(recovered.messages)
        if isinstance(row, dict) and row.get("content") == "Successor prompt 0."
    )
    assert display_index < marker_index < first_successor_index

    # Provider context has no exact token-bearing cancelled owner after
    # compression, so visible recovery must not be attached to any successor.
    assert not any(
        isinstance(row, dict)
        and row.get("_recovered_stream_id") == stream_id
        for row in recovered.context_messages
    )


def test_cancel_restart_context_uses_exact_owner_token_after_compression():
    sid = "cancel-restart-compressed-owner-token"
    stream_id = "stream-cancel-restart-compressed-owner-token"
    prompt = "Cancelled prompt whose exact owner survives compression."
    recovered_text = "Recovered output belongs immediately after the cancelled owner."

    session = _start_cancelled_turn(sid, stream_id)
    history = []
    for index in range(3):
        history.extend([
            {"role": "user", "content": f"Historical prompt {index}.", "timestamp": index * 2 + 1},
            {"role": "assistant", "content": f"Historical answer {index}.", "timestamp": index * 2 + 2},
        ])
    cancelled_user = {
        "role": "user",
        "content": prompt,
        "timestamp": 10,
        "_owner_probe": "cancelled-owner",
    }
    compression_summary = {
        "role": "user",
        "content": "[Earlier conversation compressed into summary.]",
        "timestamp": 9,
        "_owner_probe": "compression-summary",
    }
    session.pending_user_message = prompt
    session.pending_started_at = 10.0
    session.messages[:] = history + [copy.deepcopy(cancelled_user)]
    session.context_messages[:] = [
        copy.deepcopy(compression_summary),
        copy.deepcopy(cancelled_user),
    ]
    session.save()

    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event("token", {"text": recovered_text})
    assert cancel_stream(stream_id) is True

    cancelled = Session.load(sid)
    assert cancelled is not None
    display_owner = next(
        row for row in cancelled.messages
        if isinstance(row, dict) and row.get("_owner_probe") == "cancelled-owner"
    )
    context_owner = next(
        row for row in cancelled.context_messages
        if isinstance(row, dict) and row.get("_owner_probe") == "cancelled-owner"
    )
    owner_token = str(display_owner.get("_active_turn_token") or "")
    assert owner_token
    assert context_owner.get("_active_turn_token") == owner_token

    successors = [
        {"role": "user", "content": "Successor prompt 0.", "timestamp": 20},
        {"role": "assistant", "content": "Successor answer 0.", "timestamp": 21},
        {"role": "user", "content": "Successor prompt 1.", "timestamp": 22},
        {"role": "assistant", "content": "Successor answer 1.", "timestamp": 23},
    ]
    cancelled.messages.extend(copy.deepcopy(successors))
    cancelled.context_messages.extend(copy.deepcopy(successors))
    cancelled.save()

    _simulate_restart()
    recovered = models.get_session(sid)
    context = recovered.context_messages

    owner_index = next(
        index for index, row in enumerate(context)
        if isinstance(row, dict) and row.get("_active_turn_token") == owner_token
    )
    recovered_index = next(
        index for index, row in enumerate(context)
        if isinstance(row, dict)
        and row.get("_recovered_stream_id") == stream_id
        and row.get("content") == recovered_text
    )
    successor_index = next(
        index for index, row in enumerate(context)
        if isinstance(row, dict) and row.get("content") == "Successor prompt 0."
    )
    assert owner_index < recovered_index < successor_index


@pytest.mark.parametrize("completion_key", ["tid", "tool_call_id"])
def test_overlapping_tool_completion_prefers_exact_id_before_idless_fallback(completion_key):
    sid = "cancel-overlap-exact-id"
    stream_id = "stream-cancel-overlap-exact-id"
    _start_cancelled_turn(sid, stream_id)
    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event("tool", {"name": "terminal", "tid": "A", "preview": "start A"})
    writer.append_sse_event("tool", {"name": "terminal", "preview": "start B"})
    writer.append_sse_event("tool_complete", {"name": "terminal", completion_key: "A", "preview": "done A"})
    writer.append_sse_event("tool_complete", {"name": "terminal", completion_key: "B", "preview": "done B"})
    assert cancel_stream(stream_id)
    _simulate_restart()
    recovered = models.get_session(sid)
    tools = recovered.tool_calls
    assert [(tool["tid"], tool["preview"], tool["done"]) for tool in tools] == [
        ("A", "done A", True), ("journal-2", "done B", True),
    ]
    assert all("_journal_synthetic_tid" not in tool for tool in tools)


def test_recovered_equal_segments_and_tool_owners_survive_cold_load():
    sid = "cancel-equal-segments-cold-load"
    stream_id = "stream-cancel-equal-segments-cold-load"
    _start_cancelled_turn(sid, stream_id)
    writer = RunJournalWriter(sid, stream_id)
    for tid in ("A", "B"):
        writer.append_sse_event("token", {"text": "Checking…"})
        writer.append_sse_event("tool", {"name": "terminal", "tid": tid})
        writer.append_sse_event("tool_complete", {"name": "terminal", "tid": tid, "preview": f"done {tid}"})
    assert cancel_stream(stream_id)
    _simulate_restart()
    recovered = models.get_session(sid)
    before_messages = copy.deepcopy(recovered.messages)
    before_tools = copy.deepcopy(recovered.tool_calls)
    models.SESSIONS.clear()
    cold = models.get_session(sid)
    rows = [row for row in cold.messages if row.get("_recovered_stream_id") == stream_id]
    assert len(rows) == 2
    assert all(row["content"] == "Checking…" and not row.get("_partial") for row in rows)
    assert cold.messages == before_messages
    assert cold.tool_calls == before_tools
    assert models._sidecar_has_terminal_partial_error(cold.messages)
    assert not models._sidecar_has_terminal_partial_error(rows)
    owner_indexes = [tool["assistant_msg_idx"] for tool in cold.tool_calls]
    assert len(set(owner_indexes)) == 2
    for index in owner_indexes:
        assert cold.messages[index]["_recovered_stream_id"] == stream_id
        assert cold.messages[index]["content"] == "Checking…"
    assert not _cancel_marker(cold)[1].get("_pending_journal_recovery")



def _persist_multi_retry_turns(sid, kinds, outputs, *, defer_first=False):
    """Persist actual Stop hooks and production interrupted markers/journals."""
    session = Session(session_id=sid, title="multiple retry turns", messages=[], context_messages=[])
    session.save()
    models.SESSIONS[sid] = session
    streams = []
    for number, kind in enumerate(kinds):
        stream_id = f"{sid}-stream-{number}"
        streams.append(stream_id)
        started = 10 * (number + 1)
        owner = {"role": "user", "content": f"Prompt {number}", "timestamp": started}
        session.messages.append(copy.deepcopy(owner))
        session.context_messages.append(copy.deepcopy(owner))
        if kind == "ordinary":
            answer = {"role": "assistant", "content": f"Ordinary answer {number}", "timestamp": started + 1}
            session.messages.append(copy.deepcopy(answer))
            session.context_messages.append(copy.deepcopy(answer))
        elif kind == "interrupted":
            marker = models._build_recovery_marker_with_retry_hook(
                recovered_output=False, stream_id=stream_id, pending_started_at=started,
            )
            marker["timestamp"] = started + 1
            session.messages.append(marker)
        else:
            assert kind == "cancelled"
            session.pending_user_message = owner["content"]
            session.pending_started_at = started
            session.pending_user_source = "webui"
            session.active_stream_id = stream_id
            config.STREAMS[stream_id] = queue.Queue()
            config.CANCEL_FLAGS[stream_id] = threading.Event()
            agent = Mock()
            agent.session_id = sid
            config.AGENT_INSTANCES[stream_id] = agent
            config.ACTIVE_RUNS[stream_id] = {
                "session_id": sid, "phase": "running", "started_at": time.time(),
            }
            session.save()
            assert cancel_stream(stream_id) is True
        session.save()
    # Journals arrive after all markers, exactly the lazy-recovery condition.
    for number, events in enumerate(outputs):
        if defer_first and number == 0:
            continue
        writer = RunJournalWriter(sid, streams[number])
        for event, payload in events:
            writer.append_sse_event(event, payload)
        if kinds[number] != "ordinary" and (not events or events[-1][0] not in {"apperror", "error", "done", "cancel"}):
            writer.append_sse_event("cancel", {"message": "Terminal journal"})
    _simulate_restart()
    return streams


def _stream_output(session, stream_id):
    return [row for row in session.messages if row.get("_recovered_stream_id") == stream_id]


def _pending_stream_hook(session, stream_id):
    return next((row for row in session.messages if row.get("_journal_retry_stream_id") == stream_id), None)


def _assert_retry_turn_ownership(session, kinds, streams):
    user_positions = [i for i, row in enumerate(session.messages) if row.get("role") == "user"]
    assert len(user_positions) == len(kinds)
    for number, stream_id in enumerate(streams):
        end = user_positions[number + 1] if number + 1 < len(kinds) else len(session.messages)
        for index, row in enumerate(session.messages):
            if row.get("_recovered_stream_id") == stream_id:
                assert user_positions[number] < index < end
    for tool in session.tool_calls or []:
        owner = session.messages[tool["assistant_msg_idx"]]
        assert owner.get("_recovered_stream_id") == tool.get("_recovered_stream_id")


def test_newer_recovered_cancel_does_not_hide_older_interrupted_output():
    sid = "round4-older-interrupted-newer-cancel"
    kinds = ["interrupted", "cancelled"]
    streams = _persist_multi_retry_turns(sid, kinds, [
        [("token", {"text": "Older interrupted answer"})],
        [("token", {"text": "Newer cancelled answer"})],
    ])
    first = models.get_session(sid)
    assert [row["content"] for row in _stream_output(first, streams[1])] == ["Newer cancelled answer"]
    models.SESSIONS.clear()
    second = models.get_session(sid)
    assert [row["content"] for row in _stream_output(second, streams[0])] == ["Older interrupted answer"]
    assert _pending_stream_hook(second, streams[0]) is None
    _assert_retry_turn_ownership(second, kinds, streams)


def test_newer_empty_cancel_does_not_spend_older_ready_cancel_retry():
    sid = "round4-older-ready-newer-empty-cancel"
    streams = _persist_multi_retry_turns(sid, ["cancelled", "cancelled"], [
        [("token", {"text": "Older ready cancelled answer"})], [],
    ])
    first = models.get_session(sid)
    assert [row["content"] for row in _stream_output(first, streams[0])] == ["Older ready cancelled answer"]
    assert _pending_stream_hook(first, streams[0]) is None
    assert _pending_stream_hook(first, streams[1])["_journal_retry_attempts"] == 1


@pytest.mark.parametrize("cache_hits", [False, True], ids=["cold", "cached"])
@pytest.mark.parametrize("older_kind", ["interrupted", "cancelled"])
@pytest.mark.parametrize("ordinary_boundary", [False, True])
@pytest.mark.parametrize("newer_state", ["empty", "ready", "reasoning", "live", "nonterminal", "arriving", "expired"])
def test_mixed_multiple_retry_turns_keep_boundaries_and_independent_budgets(
    cache_hits, older_kind, ordinary_boundary, newer_state,
):
    sid = f"round4-mixed-{cache_hits}-{older_kind}-{ordinary_boundary}-{newer_state}"
    kinds = [older_kind, "cancelled"]
    outputs = [
        [("token", {"text": "Oldest output"}),
         ("tool", {"name": "read_file", "tid": "old-tool", "args": {"path": "old.txt"}}),
         ("tool_complete", {"name": "read_file", "tid": "old-tool", "preview": "Old full result"})],
        [("token", {"text": "Middle cancelled output"}),
         ("tool", {"name": "read_file", "tid": "middle-tool", "args": {"path": "middle.txt"}})],
    ]
    if ordinary_boundary:
        kinds.append("ordinary")
        outputs.append([])
    kinds.append("cancelled")
    newer_events = []
    if newer_state == "ready":
        newer_events = [("token", {"text": "Newest output"})]
    elif newer_state == "reasoning":
        newer_events = [("reasoning", {"text": "Newest private thought"})]
    outputs.append(newer_events)
    streams = _persist_multi_retry_turns(sid, kinds, outputs)
    newer_stream = streams[-1]
    if newer_state == "live":
        config.ACTIVE_RUNS[newer_stream] = {"session_id": sid, "phase": "cancelling", "started_at": time.time()}
    elif newer_state == "nonterminal":
        # A distinct same-process nonterminal journal is not permission to read
        # cancellation output, even when registry bookkeeping is absent.
        session = Session.load(sid)
        _pending_stream_hook(session, newer_stream)["_journal_retry_process_token"] = models._JOURNAL_RECOVERY_PROCESS_TOKEN
        session.save()
        from api.run_journal import _run_path
        _run_path(sid, newer_stream).unlink()
        RunJournalWriter(sid, newer_stream).append_sse_event("token", {"text": "Still owned output"})
    elif newer_state == "arriving":
        from api.run_journal import _run_path
        _run_path(sid, newer_stream).unlink()
    elif newer_state == "expired":
        session = Session.load(sid)
        _pending_stream_hook(session, newer_stream)["_journal_retry_attempts"] = models._JOURNAL_RETRY_MAX_ATTEMPTS
        session.save()

    for _ in range(4):
        if not cache_hits:
            models.SESSIONS.clear()
        recovered = models.get_session(sid)
    old_should_recover = older_kind == "cancelled" or not ordinary_boundary
    assert bool(_stream_output(recovered, streams[0])) is old_should_recover
    assert [row["content"] for row in _stream_output(recovered, streams[1]) if row.get("content")] == ["Middle cancelled output"]
    _assert_retry_turn_ownership(recovered, kinds, streams)
    if not old_should_recover:
        assert _pending_stream_hook(recovered, streams[0])["_journal_retry_attempts"] == 0
    if newer_state in {"empty", "live", "nonterminal", "arriving"}:
        expected = 4 if newer_state == "empty" else 0
        assert _pending_stream_hook(recovered, newer_stream)["_journal_retry_attempts"] == expected
    else:
        assert _pending_stream_hook(recovered, newer_stream) is None
    if newer_state == "reasoning":
        assert not any("Newest private thought" in str(row.get("content")) for row in recovered.context_messages)
    # Recovered cancellation output belongs to its exact user even when a
    # genuine ordinary assistant boundary prevents older interruption retry.
    context_middle = next(i for i, row in enumerate(recovered.context_messages) if row.get("content") == "Prompt 1")
    assert recovered.context_messages[context_middle + 1]["content"] == "Middle cancelled output"
    if old_should_recover:
        from api.streaming import _sanitize_messages_for_agent
        history = _sanitize_messages_for_agent(
            models.reconciled_state_db_messages_for_session(
                recovered, prefer_context=True, state_messages=[],
            )
        )
        assert any(row.get("content") == "Oldest output" for row in history)


@pytest.mark.parametrize("expiry", ["attempts", "age"])
def test_multiple_empty_cancel_hooks_have_separate_retry_budgets(expiry):
    sid = f"round4-independent-budgets-{expiry}"
    streams = _persist_multi_retry_turns(sid, ["cancelled"] * 3, [
        [("token", {"text": "Old ready output"})], [], [],
    ])
    session = Session.load(sid)
    _pending_stream_hook(session, streams[1])["_journal_retry_attempts"] = 3
    newest = _pending_stream_hook(session, streams[2])
    if expiry == "attempts":
        newest["_journal_retry_attempts"] = models._JOURNAL_RETRY_MAX_ATTEMPTS
    else:
        newest["_journal_retry_first_seen_ts"] = time.time() - models._JOURNAL_RETRY_GIVEUP_SECONDS - 1
    session.save()
    recovered = models.get_session(sid)
    assert [row["content"] for row in _stream_output(recovered, streams[0])] == ["Old ready output"]
    assert _pending_stream_hook(recovered, streams[2]) is None
    assert _pending_stream_hook(recovered, streams[1])["_journal_retry_attempts"] == 4
    assert _pending_stream_hook(recovered, streams[0]) is None
    models.SESSIONS.clear()
    again = models.get_session(sid)
    assert _pending_stream_hook(again, streams[1])["_journal_retry_attempts"] == 5
    assert [row["content"] for row in _stream_output(again, streams[0])] == ["Old ready output"]


def test_multiple_cancel_retry_counter_save_failure_aborts_pass(monkeypatch):
    sid = "round4-multiple-hooks-counter-rollback"
    streams = _persist_multi_retry_turns(sid, ["cancelled", "cancelled"], [
        [("token", {"text": "Older output waits for a durable retry transaction"})], [],
    ])
    original_save = Session.save
    failed = False

    def fail_first_budget_save(session, *args, **kwargs):
        nonlocal failed
        marker = _pending_stream_hook(session, streams[1])
        if not failed and marker and marker.get("_journal_retry_attempts") == 1:
            failed = True
            raise OSError("synthetic retry budget save failure")
        return original_save(session, *args, **kwargs)

    monkeypatch.setattr(Session, "save", fail_first_budget_save)
    first = models.get_session(sid)
    assert failed
    assert not _stream_output(first, streams[0])
    assert _pending_stream_hook(first, streams[1])["_journal_retry_attempts"] == 0
    durable = Session.load(sid)
    assert _pending_stream_hook(durable, streams[1])["_journal_retry_attempts"] == 0
    assert not _stream_output(durable, streams[0])
    monkeypatch.setattr(Session, "save", original_save)
    second = models.get_session(sid)
    assert len(_stream_output(second, streams[0])) == 1
    assert _pending_stream_hook(second, streams[1])["_journal_retry_attempts"] == 1


def test_older_interrupted_terminal_error_keeps_newer_cancel_tool_owner():
    sid = "round4-old-terminal-new-cancel-tool"
    kinds = ["interrupted", "cancelled"]
    streams = _persist_multi_retry_turns(sid, kinds, [
        [("token", {"text": "Old progress before terminal error"}),
         ("tool", {"name": "read_file", "tid": "old-error-tool", "args": {"path": "old.txt"}}),
         ("apperror", {"session_id": sid, "terminal_session_persisted": False,
                       "session": {"session_id": sid, "messages": [
                           {"role": "user", "content": "Prompt 0"},
                           {"role": "assistant", "content": "Old terminal failure", "_error": True},
                       ]}})],
        [("token", {"text": "New cancel output"}),
         ("tool", {"name": "read_file", "tid": "new-tool", "args": {"path": "new.txt"}})],
    ])
    models.get_session(sid)
    models.SESSIONS.clear()
    recovered = models.get_session(sid)
    assert any(row.get("content") == "Old terminal failure" for row in _stream_output(recovered, streams[0]))
    assert not any(row.get("type") == "interrupted" for row in recovered.messages)
    _assert_retry_turn_ownership(recovered, kinds, streams)
    assert {tool["tid"] for tool in recovered.tool_calls} == {"old-error-tool", "new-tool"}
    models.SESSIONS.clear()
    _assert_retry_turn_ownership(models.get_session(sid), kinds, streams)


@pytest.mark.parametrize("cache_hits", [False, True], ids=["cold", "cached"])
@pytest.mark.parametrize("later_cancel", [False, True])
@pytest.mark.parametrize("newer_output", ["prose", "reasoning", "tool"])
def test_two_interrupted_hooks_keep_newest_assistant_cutoff(cache_hits, later_cancel, newer_output):
    """A later interrupted recovery is an answer boundary, not a cancel recovery."""
    sid = f"round5-two-interrupts-{cache_hits}-{later_cancel}-{newer_output}"
    kinds = ["interrupted", "interrupted"]
    new_events = {
        "prose": [("token", {"text": "New interrupted answer"})],
        "reasoning": [("reasoning", {"text": "New interrupted thought"})],
        "tool": [("tool", {"name": "read_file", "tid": "new-interrupted-tool", "args": {"path": "new.txt"}})],
    }[newer_output]
    outputs = [[("token", {"text": "Old interrupted answer"})], new_events]
    if later_cancel:
        # A pending cancellation forces the retry selector to run even after
        # the scan-only fast path has a newer non-cancel assistant boundary.
        kinds.append("cancelled")
        outputs.append([])
    streams = _persist_multi_retry_turns(sid, kinds, outputs)
    first = models.get_session(sid)
    assert _stream_output(first, streams[1])
    initial_context = copy.deepcopy(first.context_messages)
    for _ in range(3):
        if not cache_hits:
            models.SESSIONS.clear()
        recovered = models.get_session(sid)
        assert not _stream_output(recovered, streams[0])
        assert _pending_stream_hook(recovered, streams[0])["_journal_retry_attempts"] == 0
        assert recovered.context_messages == initial_context
        _assert_retry_turn_ownership(recovered, kinds, streams)
    from api.streaming import _sanitize_messages_for_agent
    history = _sanitize_messages_for_agent(models.reconciled_state_db_messages_for_session(
        recovered, prefer_context=True, state_messages=[],
    ))
    assert not any(row.get("content") == "Old interrupted answer" for row in history)


@pytest.mark.parametrize("cache_hits", [False, True], ids=["cold", "cached"])
@pytest.mark.parametrize("context_shape", ["empty", "compressed"])
@pytest.mark.parametrize("equal_prose", [False, True])
def test_unproven_older_interrupted_recovery_stays_display_only(
    cache_hits, context_shape, equal_prose,
):
    sid = f"round5-display-only-{cache_hits}-{context_shape}-{equal_prose}"
    old_text = "Repeated answer" if equal_prose else "Old display-only answer"
    new_text = "Repeated answer" if equal_prose else "New cancelled answer"
    kinds = ["interrupted", "cancelled"]
    streams = _persist_multi_retry_turns(sid, kinds, [
        [("token", {"text": old_text}),
         ("tool", {"name": "read_file", "tid": "old-display-tool", "args": {"path": "old.txt"}})],
        [("token", {"text": new_text}),
         ("tool", {"name": "read_file", "tid": "new-display-tool", "args": {"path": "new.txt"}})],
    ])
    first = models.get_session(sid)
    assert _stream_output(first, streams[1])
    if context_shape == "empty":
        first.context_messages = []
    elif context_shape == "compressed":
        first.context_messages = [{"role": "system", "content": "Context compression: newer work only."}]
    first.save()
    if not cache_hits:
        models.SESSIONS.clear()
    recovered = models.get_session(sid)
    assert [row["content"] for row in _stream_output(recovered, streams[0]) if row.get("content")] == [old_text]
    assert _pending_stream_hook(recovered, streams[0]) is None
    _assert_retry_turn_ownership(recovered, kinds, streams)
    from api.streaming import _sanitize_messages_for_agent, _sanitize_messages_for_api, _api_safe_message_positions
    for _ in range(3):
        if not cache_hits:
            models.SESSIONS.clear()
        recovered = models.get_session(sid)
        _assert_retry_turn_ownership(recovered, kinds, streams)
        # Exercise the real next-send reconciliation, as well as raw display
        # fallbacks used by replay/compression and empty-context seeding.
        inputs = [models.reconciled_state_db_messages_for_session(
            recovered, prefer_context=True, state_messages=[],
        ), recovered.messages]
        for rows in inputs:
            histories = [_sanitize_messages_for_agent(rows), _sanitize_messages_for_api(rows),
                         [row for _, row in _api_safe_message_positions(rows)]]
            for history in histories:
                expected = int(equal_prose and (context_shape != "compressed" or rows is recovered.messages))
                assert sum(row.get("content") == old_text for row in history) == expected
        seeded = []
        models._seed_recovered_context_from_messages(recovered, seeded)
        assert sum(row.get("content") == old_text for row in seeded) == (1 if equal_prose else 0)


def test_latest_interrupted_recovery_still_feeds_provider_context():
    sid = "round5-latest-interrupted-context"
    streams = _persist_multi_retry_turns(sid, ["interrupted"], [[("token", {"text": "Current interrupted answer"})]])
    recovered = models.get_session(sid)
    assert [row["content"] for row in _stream_output(recovered, streams[0])] == ["Current interrupted answer"]
    from api.streaming import _sanitize_messages_for_agent
    history = _sanitize_messages_for_agent(models.reconciled_state_db_messages_for_session(
        recovered, prefer_context=True, state_messages=[],
    ))
    assert [(row["role"], row["content"]) for row in history] == [
        ("user", "Prompt 0"), ("assistant", "Current interrupted answer"),
    ]


@pytest.mark.parametrize("tag_value", [None, False, "true"])
def test_unproven_cancel_recovery_rows_keep_interrupted_scan_boundary(tag_value):
    sid = f"round5-unproven-cancel-{tag_value}"
    kinds = ["interrupted", "cancelled"]
    streams = _persist_multi_retry_turns(sid, kinds, [
        [("token", {"text": "Old interrupted answer"})],
        [("token", {"text": "New cancellation answer"})],
    ])
    recovered = models.get_session(sid)
    for row in _stream_output(recovered, streams[1]):
        if tag_value is None:
            row.pop("_recovered_from_cancel_journal", None)
        else:
            row["_recovered_from_cancel_journal"] = tag_value
    recovered.save()
    original_context = copy.deepcopy(recovered.context_messages)
    for _ in range(2):
        models.SESSIONS.clear()
        recovered = models.get_session(sid)
        assert not _stream_output(recovered, streams[0])
        assert recovered.context_messages == original_context
        assert _pending_stream_hook(recovered, streams[0])["_journal_retry_attempts"] == 0


def _next_send_history(session):
    from api.streaming import (
        _new_turn_context_from_messages, _dedupe_replayed_context_messages,
        _dedupe_replayed_active_context, _sanitize_messages_for_agent,
    )
    prompt = "Continue the next task"
    reconciled = models.reconciled_state_db_messages_for_session(
        session, prefer_context=True, state_messages=[],
    )
    history = _new_turn_context_from_messages(reconciled, prompt)
    history = _dedupe_replayed_context_messages(history, history, prompt)
    history = _dedupe_replayed_active_context(history, history, prompt)
    return _sanitize_messages_for_agent(history)


@pytest.mark.parametrize("cache_hits", [False, True], ids=["cold", "cached"])
@pytest.mark.parametrize("new_output", [False, True], ids=["empty-stop", "output-stop"])
@pytest.mark.parametrize("equal_prose", [False, True])
@pytest.mark.parametrize("native_pair", [False, True])
def test_proven_older_interrupted_answer_survives_real_stop_next_send(
    cache_hits, new_output, equal_prose, native_pair,
):
    sid = f"proven-old-context-{cache_hits}-{new_output}-{equal_prose}-{native_pair}"
    old_text = "Repeated answer" if equal_prose else "Old answer"
    new_text = "Repeated answer" if equal_prose else "New answer"
    kinds = ["interrupted", "cancelled"]
    streams = _persist_multi_retry_turns(sid, kinds, [
        [("token", {"text": old_text}),
         ("tool", {"name": "read_file", "tid": "old-owned-tool", "args": {"path": "old.txt"}})],
        [("token", {"text": new_text})] if new_output else [],
    ], defer_first=True)
    first = models.get_session(sid)
    assert not _stream_output(first, streams[0])
    pair = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "native-call", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"prior.txt"}'}}]},
        {"role": "tool", "content": "Native tool result", "tool_call_id": "native-call"},
    ] if native_pair else []
    first.context_messages[1:1] = copy.deepcopy(pair)
    first.save()
    writer = RunJournalWriter(sid, streams[0])
    writer.append_sse_event("token", {"text": old_text})
    writer.append_sse_event("tool", {"name": "read_file", "tid": "old-owned-tool", "args": {"path": "old.txt"}})
    writer.append_sse_event("cancel", {"message": "Old terminal journal arrives late"})
    expected = [("user", "Prompt 0")] + [(row["role"], row["content"]) for row in pair] + [("assistant", old_text)]
    if new_output:
        expected += [("user", "Prompt 1"), ("assistant", new_text)]
    for _ in range(3):
        if not cache_hits:
            models.SESSIONS.clear()
        recovered = models.get_session(sid)
        history = _next_send_history(recovered)
        assert [(row["role"], row.get("content", "")) for row in history] == expected
        if native_pair:
            assert history[1]["tool_calls"] == pair[0]["tool_calls"]
            assert history[2]["tool_call_id"] == "native-call"
        assert _pending_stream_hook(recovered, streams[0]) is None
        assert not any(row.get("_recovered_display_only") for row in _stream_output(recovered, streams[0]))
        _assert_retry_turn_ownership(recovered, kinds, streams)


@pytest.mark.parametrize("failure", ["duplicate-owner", "source", "timestamp", "owner-token", "tokenless-successor", "foreign-successor-token", "duplicate-successor-token"])
def test_ambiguous_interrupted_context_proof_stays_display_only(failure):
    sid = f"ambiguous-old-context-{failure}"
    streams = _persist_multi_retry_turns(sid, ["interrupted", "cancelled"], [
        [("token", {"text": "Old ambiguous answer"})],
        [("token", {"text": "New answer"})],
    ])
    first = models.get_session(sid)
    assert not _stream_output(first, streams[0])
    owner = first.context_messages[0]
    successor = next(row for row in first.context_messages if row.get("role") == "user" and row.get("content") == "Prompt 1")
    if failure == "duplicate-owner":
        first.context_messages.insert(1, copy.deepcopy(owner))
    elif failure == "source":
        owner["_source"] = "telegram"
    elif failure == "timestamp":
        # Distinct fractional timestamps conflict; int10/float10.25 is now
        # the explicitly supported legacy truncation shape, not this negative.
        display_owner=next(row for row in first.messages if row.get("role")=="user" and row.get("content")=="Prompt 0")
        display_owner["timestamp"]=float(display_owner["timestamp"])
        owner["timestamp"] += 0.25
    elif failure == "owner-token":
        owner["_active_turn_token"] = "foreign-owner"
    elif failure == "tokenless-successor":
        successor.pop("_active_turn_token", None)
    elif failure == "foreign-successor-token":
        successor["_active_turn_token"] = "foreign-successor"
    else:
        first.context_messages.append(copy.deepcopy(successor))
    first.save()
    before = copy.deepcopy(first.context_messages)
    for _ in range(3):
        models.SESSIONS.clear()
        recovered = models.get_session(sid)
        assert recovered.context_messages == before
        assert any(row.get("_recovered_display_only") is True for row in _stream_output(recovered, streams[0]))
        assert not any(row.get("content") == "Old ambiguous answer" for row in _next_send_history(recovered))
        from api.streaming import _sanitize_messages_for_agent
        assert not any(row.get("content") == "Old ambiguous answer" for row in _sanitize_messages_for_agent(recovered.messages))


def test_proven_interrupted_context_save_failure_retains_hook(monkeypatch):
    sid = "proven-old-context-save-failure"
    streams = _persist_multi_retry_turns(sid, ["interrupted", "cancelled"], [
        [("token", {"text": "Old answer"})], [("token", {"text": "New answer"})],
    ])
    session = models.get_session(sid)
    assert _pending_stream_hook(session, streams[0]) is not None
    for rows in (session.messages, session.context_messages):
        next(row for row in rows if row.get("role")=="user" and row.get("content")=="Prompt 0")["_recovered"]=True
    before = copy.deepcopy((session.messages, session.context_messages, session.tool_calls, session.updated_at))
    original_save = Session.save
    monkeypatch.setattr(Session, "save", Mock(side_effect=OSError("fixture save failure")))
    assert models._retry_journal_recovery_in_place(session) is False
    assert session.save.called, session.messages
    assert (session.messages, session.context_messages, session.tool_calls, session.updated_at) == before
    monkeypatch.setattr(Session, "save", original_save)
    assert models._retry_journal_recovery_in_place(session) is True, session.messages
    models.SESSIONS.clear()
    recovered = models.get_session(sid)
    assert [(row["role"], row["content"]) for row in _next_send_history(recovered)] == [
        ("user", "Prompt 0"), ("assistant", "Old answer"), ("user", "Prompt 1"), ("assistant", "New answer"),
    ]


@pytest.mark.parametrize("suffix", [None, "compression-system", "compression-assistant", "unowned-assistant"])
def test_interrupted_owner_without_context_successor_requires_context_tail(suffix):
    sid = f"old-context-tail-{suffix}"
    streams = _persist_multi_retry_turns(sid, ["interrupted", "cancelled"], [
        [("token", {"text": "Old tail answer"})], [("token", {"text": "New answer"})],
    ])
    first = models.get_session(sid)
    assert not _stream_output(first, streams[0])
    first.context_messages = [copy.deepcopy(first.context_messages[0])]
    if suffix is not None:
        first.context_messages.append({
            "role": "system" if suffix == "compression-system" else "assistant",
            "content": "Context compression: newer work only." if suffix.startswith("compression") else "Unowned later answer",
        })
    first.save()
    before = copy.deepcopy(first.context_messages)
    for _ in range(3):
        models.SESSIONS.clear()
        recovered = models.get_session(sid)
        if suffix is None:
            assert [(row["role"], row["content"]) for row in _next_send_history(recovered)] == [
                ("user", "Prompt 0"), ("assistant", "Old tail answer"),
            ]
        else:
            assert recovered.context_messages == before
            assert not any(row.get("content") == "Old tail answer" for row in _next_send_history(recovered))
            assert all(row.get("_recovered_display_only") is True for row in _stream_output(recovered, streams[0]))


@pytest.mark.parametrize("cache_hits", [False, True], ids=["cold", "cached"])
@pytest.mark.parametrize("new_output", [False, True], ids=["empty-stop", "output-stop"])
@pytest.mark.parametrize("identity", [None, "token", "message-id", "state-row", "message-uid"])
@pytest.mark.parametrize("shape", ["native-image", "native-image-successor", "context-api", "shared-api", "display-attachments"])
def test_rich_interrupted_owner_preserves_exact_real_next_send(
    tmp_path, cache_hits, new_output, identity, shape,
):
    from api.streaming import _build_native_multimodal_message, _workspace_context_prefix
    sid = f"rich-old-owner-{cache_hits}-{new_output}-{identity}-{shape}"
    streams = _persist_multi_retry_turns(sid, ["interrupted", "cancelled"], [
        [("token", {"text": "Old rich answer"})],
        [("token", {"text": "New answer"})] if new_output else [],
    ], defer_first=True)
    first = models.get_session(sid)
    display_owner = next(row for row in first.messages if row.get("role") == "user" and row.get("timestamp") == 10)
    context_owner = first.context_messages[0]
    if identity is not None:
        if identity == "message-id":
            display_owner["id"] = "old-rich-id"
            context_owner["message_id"] = "old-rich-id"
        else:
            key, value = {"token": ("_active_turn_token", f"{streams[0]}:10"),
                          "state-row": ("_state_db_row_id", 42), "message-uid": ("message_uid", "old-rich-uid")}[identity]
            display_owner[key] = value
            context_owner[key] = value
    prefix = _workspace_context_prefix(str(tmp_path))
    attachments = [{"path": str(tmp_path / "upload.png"), "name": "upload.png", "mime": "image/png"}]
    if shape.startswith("native-image"):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(b"\0\xfb\xef\xff")) + chunk(b"IEND", b""))
        (tmp_path / "upload.png").write_bytes(png)
        rich = _build_native_multimodal_message(prefix, "Prompt 0", attachments, str(tmp_path), cfg={"agent": {"image_input_mode": "native"}})
        assert isinstance(rich, list) and len(rich) == 2
        assert base64.b64decode(rich[1]["image_url"]["url"].split(",", 1)[1]) == png
        context_owner["content"] = rich
        context_owner[models._WEBUI_TRUSTED_AGENT_INPUT_FIELD] = prefix + "Prompt 0"
        display_owner["attachments"] = attachments
    elif shape in ("context-api", "shared-api"):
        context_owner["api_content"] = prefix + "Prompt 0\n\n[Attached files: upload.txt]"
        if shape == "shared-api":
            display_owner["api_content"] = context_owner["api_content"]
    else:
        display_owner["attachments"] = attachments
        context_owner["content"] = prefix + "Prompt 0\n\n[Attached files: upload.png]"
    successor_content = "Prompt 1"
    if shape == "native-image-successor" and new_output:
        successor = next(row for row in first.context_messages if row.get("role") == "user" and row.get("timestamp") == 20)
        successor_content = _build_native_multimodal_message(prefix, "Prompt 1", attachments, str(tmp_path), cfg={"agent": {"image_input_mode": "native"}})
        successor["content"] = successor_content
        successor[models._WEBUI_TRUSTED_AGENT_INPUT_FIELD] = prefix + "Prompt 1"
        successor["api_content"] = prefix + "Prompt 1\n\n[Attached files: upload.png]"
        display_successor = next(row for row in first.messages if row.get("role") == "user" and row.get("timestamp") == 20)
        display_successor["attachments"] = attachments
    original_owner = copy.deepcopy(context_owner)
    original_display = copy.deepcopy(display_owner)
    first.save()
    writer = RunJournalWriter(sid, streams[0])
    writer.append_sse_event("token", {"text": "Old rich answer"})
    writer.append_sse_event("cancel", {"message": "Old journal visible after new Stop"})
    for _ in range(3):
        if not cache_hits:
            models.SESSIONS.clear()
        recovered = models.get_session(sid)
        history = _next_send_history(recovered)
        expected = [("user", original_owner["content"]), ("assistant", "Old rich answer")]
        if new_output:
            expected += [("user", successor_content), ("assistant", "New answer")]
        assert [(row["role"], row["content"]) for row in history] == expected
        if "api_content" in original_owner:
            assert history[0]["api_content"] == original_owner["api_content"]
        assert next(row for row in recovered.context_messages if row.get("role") == "user" and row.get("timestamp") == 10) == original_owner
        assert next(row for row in recovered.messages if row.get("role") == "user" and row.get("timestamp") == 10) == original_display
        assert _pending_stream_hook(recovered, streams[0]) is None


@pytest.mark.parametrize("failure", ["conflicting-api", "conflicting-attachments", "conflicting-id", "contradictory-id-alias", "contradictory-db-alias", "invalid-id", "conflicting-uid", "duplicate-id-context", "duplicate-id-display", "duplicate-normalized-owner", "interior-workspace", "nonterminal-attachment", "invalid-timestamp", "nonfinite-timestamp", "invalid-source", "invalid-uid", "structured-api-conflict", "duplicate-uid", "duplicate-token"] + [
    f"reused-{namespace}-{role}-{projection}"
    for namespace in ("message", "state", "uid")
    for role in ("assistant", "tool")
    for projection in ("display", "context")
])
def test_rich_interrupted_owner_proof_rejects_conflicts_and_ambiguity(failure):
    sid = f"rich-old-owner-reject-{failure}"
    streams = _persist_multi_retry_turns(sid, ["interrupted", "cancelled"], [
        [("token", {"text": "Old rejected answer"})], [("token", {"text": "New answer"})],
    ], defer_first=True)
    first = models.get_session(sid)
    display = next(row for row in first.messages if row.get("role") == "user" and row.get("timestamp") == 10)
    context = first.context_messages[0]
    if failure == "conflicting-api":
        display["api_content"], context["api_content"] = "display API", "other API"
    elif failure == "conflicting-attachments":
        display["attachments"], context["attachments"] = [{"path": "one.png"}], [{"path": "other.png"}]
    elif failure == "conflicting-id":
        display["id"], context["id"] = "one", "other"
    elif failure == "contradictory-id-alias":
        context.update(id="one", message_id="other")
    elif failure == "contradictory-db-alias":
        context.update(_state_db_row_id=1, _row_id=2)
    elif failure == "invalid-id":
        context["id"] = True
    elif failure == "conflicting-uid":
        display["message_uid"], context["message_uid"] = "one", "other"
    elif failure.startswith("duplicate-id"):
        display["id"] = context["id"] = "reused"
        if failure == "duplicate-id-context":
            first.context_messages.insert(1, {"role": "user", "content": "Unrelated prompt", "timestamp": 15, "id": "reused"})
        else:
            first.messages.insert(0, {"role": "user", "content": "Unrelated prompt", "timestamp": 5, "id": "reused"})
    elif failure == "duplicate-normalized-owner":
        duplicate = copy.deepcopy(context)
        duplicate["content"] = "[Workspace::v1: /tmp/example]\nPrompt 0\n\n[Attached files: duplicate.txt]"
        first.context_messages.insert(1, duplicate)
    elif failure.startswith("reused-"):
        _, namespace, role, projection = failure.split("-")
        key, value = {"message": ("id", "reused"), "state": ("_state_db_row_id", 42), "uid": ("message_uid", "reused")}[namespace]
        context[key] = display[key] = value
        duplicate = {"role": role, "content": "Unrelated output", "timestamp": 15, key: value}
        if projection == "display":
            first.messages.insert(1, duplicate)
        else:
            first.context_messages.insert(1, duplicate)
    elif failure == "invalid-timestamp":
        context["timestamp"] = True
    elif failure == "nonfinite-timestamp":
        context["timestamp"] = float("inf")
    elif failure == "invalid-source":
        context["_source"] = ["webui"]
    elif failure == "invalid-uid":
        context["message_uid"] = ["bad"]
    elif failure == "structured-api-conflict":
        context["id"] = display["id"] = "same"
        context["api_content"], display["api_content"] = {"text": "one"}, {"text": "two"}
    elif failure in ("duplicate-uid", "duplicate-token"):
        key = "message_uid" if failure == "duplicate-uid" else "_active_turn_token"
        context[key] = display[key] = "reused"
        first.context_messages.insert(1, {"role": "user", "content": "Unrelated prompt", "timestamp": 15, key: "reused"})
    elif failure == "interior-workspace":
        context["content"] = "Prompt 0\n[Workspace::v1: /tmp/example]"
    else:
        context["content"] = "Prompt 0\n\n[Attached files: upload.txt]\nUser body continues."
    first.save()
    before = copy.deepcopy(first.context_messages)
    writer = RunJournalWriter(sid, streams[0])
    writer.append_sse_event("token", {"text": "Old rejected answer"})
    writer.append_sse_event("cancel", {"message": "Old terminal tail"})
    for _ in range(3):
        models.SESSIONS.clear()
        recovered = models.get_session(sid)
        assert recovered.context_messages == before
        assert all(row.get("_recovered_display_only") is True for row in _stream_output(recovered, streams[0]))
        assert not any(row.get("content") == "Old rejected answer" for row in _next_send_history(recovered))


@pytest.mark.parametrize('mode,started,legacy', [('deferred',10,False), ('deferred',10.5,False), ('eager',10.5,False), ('deferred',10.5,True), ('eager',10.5,True)])
@pytest.mark.parametrize('previous_exchange', [False,True])
@pytest.mark.parametrize('cache_hits', [False,True], ids=['cold','cached'])
@pytest.mark.parametrize('new_output', [False,True], ids=['empty-stop','output-stop'])
def test_fractional_stale_repair_keeps_exact_real_next_send(mode,started,legacy,previous_exchange,cache_hits,new_output,monkeypatch,tmp_path):
    from api import routes
    started_id=str(started).replace('.', 'p')
    sid=f'r8-{mode}-{started_id}-{legacy}-{previous_exchange}-{cache_hits}-{new_output}'
    old_stream=f'{sid}-old'
    prior=[{'role':'user','content':'Before Q','timestamp':1},
           {'role':'assistant','content':'Before A','timestamp':2}]
    if not previous_exchange: prior=[]
    s=Session(session_id=sid,title='disposable',messages=copy.deepcopy(prior),context_messages=copy.deepcopy(prior))
    if mode=='deferred':
        # Provider context was saved after constructing the pending question;
        # display still relies on the pending intent, matching the review shape.
        s.context_messages.append({'role':'user','content':'Old Q','timestamp':started})
    monkeypatch.setattr(routes,'get_webui_session_save_mode',lambda:mode)
    routes._prepare_chat_start_session_for_stream(s,msg='Old Q',attachments=[],workspace=str(tmp_path),model='test',model_provider=None,stream_id=old_stream,started_at=started)
    _simulate_restart()
    repaired=models.get_session(sid)
    assert repaired.active_stream_id is None
    assert repaired.pending_user_message is None
    assert _pending_stream_hook(repaired,old_stream) is not None
    if legacy:
        # Existing sidecars produced by the old int-truncating repair must
        # recover as well as newly materialized exact-time rows.
        display_owner=next(row for row in repaired.messages if row.get('content')=='Old Q')
        context_owner=next(row for row in repaired.context_messages if row.get('content')=='Old Q')
        if mode=='deferred':
            display_owner['timestamp']=int(started)
            display_owner.pop('_active_turn_token',None)
        else:
            context_owner['timestamp']=int(started)
        context_owner.pop('_active_turn_token',None)
        repaired.save()
    new_stream=f'{sid}-new'
    new_owner={'role':'user','content':'New Q','timestamp':20}
    repaired.messages.append(copy.deepcopy(new_owner));repaired.context_messages.append(copy.deepcopy(new_owner))
    repaired.pending_user_message='New Q';repaired.pending_started_at=20
    repaired.pending_user_source='webui';repaired.active_stream_id=new_stream
    models.SESSIONS[sid]=repaired
    config.STREAMS[new_stream]=queue.Queue();config.CANCEL_FLAGS[new_stream]=threading.Event()
    agent=Mock();agent.session_id=sid;config.AGENT_INSTANCES[new_stream]=agent
    config.ACTIVE_RUNS[new_stream]={'session_id':sid,'phase':'running','started_at':time.time()}
    repaired.save();assert cancel_stream(new_stream) is True
    new_writer=RunJournalWriter(sid,new_stream)
    if new_output:new_writer.append_sse_event('token',{'text':'New A'})
    new_writer.append_sse_event('cancel',{'message':'New Stop'})
    _simulate_restart();models.get_session(sid)
    old_writer=RunJournalWriter(sid,old_stream)
    old_writer.append_sse_event('token',{'text':'Old A'})
    old_writer.append_sse_event('cancel',{'message':'Old journal now visible'})
    observed=[]
    _simulate_restart()
    for _ in range(3):
        if not cache_hits: _simulate_restart()
        assert Session.load(sid) is not None
        recovered=models.get_session(sid)
        history=_next_send_history(recovered)
        observed.append([(r['role'],r.get('content')) for r in history])
        outputs=[r for r in recovered.messages if r.get('_recovered_stream_id')==old_stream and r.get('content')=='Old A']
        assert len(outputs)==1
    expected=[(r['role'],r['content']) for r in prior]+[('user','Old Q'),('assistant','Old A')]
    if new_output:expected += [('user','New Q'),('assistant','New A')]
    assert observed==[expected]*3
    assert _pending_stream_hook(recovered,old_stream) is None
    assert not outputs[0].get('_recovered_display_only')


@pytest.mark.parametrize('branch', ['append-helper','nonempty','core','empty'])
def test_stale_pending_producers_preserve_fractional_time_and_stamp_before_context(branch,tmp_path,monkeypatch):
    sid=f'fractional-producer-{branch}'
    stream=f'{sid}-stream'
    prior=[{'role':'user','content':'Before Q','timestamp':1}, {'role':'assistant','content':'Before A','timestamp':2}]
    session=Session(session_id=sid,title='fractional producer',messages=copy.deepcopy(prior) if branch=='nonempty' else [],context_messages=copy.deepcopy(prior) if branch=='nonempty' else [],pending_user_message='Old Q',pending_started_at=10.5,pending_user_source='webui',active_stream_id=stream)
    original=models._append_recovered_turn_to_context
    projected=[]
    def observe(target,row):
        if row.get('role')=='user' and row.get('content')=='Old Q':projected.append(copy.deepcopy(row))
        return original(target,row)
    monkeypatch.setattr(models,'_append_recovered_turn_to_context',observe)
    if branch=='append-helper':
        models._append_recovered_pending_turn(session,timestamp=10.5)
    else:
        core=tmp_path/'core.json'
        if branch=='core':
            import json
            core.write_text(json.dumps({'messages':prior}))
            writer=RunJournalWriter(sid,stream)
            writer.append_sse_event('token',{'text':'Old partial'})
        assert models._apply_core_sync_or_error_marker(session,core) is True
    assert projected
    for row in projected:
        assert row['timestamp']==10.5 and type(row['timestamp']) is float
        assert row['_active_turn_token']==f'{stream}:10.5'
    for rows in (session.messages,session.context_messages):
        owner=next(row for row in rows if row.get('role')=='user' and row.get('content')=='Old Q')
        assert owner['timestamp']==10.5
        assert owner['_active_turn_token']==f'{stream}:10.5'


@pytest.mark.parametrize('projection',['display','context'])
@pytest.mark.parametrize('anchor',['int','float'])
def test_legacy_int_equivalence_rejects_every_same_second_candidate(projection,anchor):
    sid=f'legacy-ambiguous-{projection}-{anchor}'
    streams=_persist_multi_retry_turns(sid,['interrupted','cancelled'],[
        [('token',{'text':'Old ambiguous answer'})],[('token',{'text':'New answer'})],
    ],defer_first=True)
    first=models.get_session(sid)
    display=next(row for row in first.messages if row.get('role')=='user' and row.get('timestamp')==10)
    context=first.context_messages[0]
    display['timestamp'],context['timestamp']=(10,10.5) if anchor=='int' else (10.5,10)
    # Only one endpoint may be fractional; either projection can carry an
    # additional same-second row compatible with the integer endpoint.
    target=first.messages if projection=='display' else first.context_messages
    duplicate=copy.deepcopy(display if projection=='display' else context)
    duplicate['timestamp']=10.9
    target.insert(0,duplicate)
    first.save();before=copy.deepcopy(first.context_messages)
    writer=RunJournalWriter(sid,streams[0]);writer.append_sse_event('token',{'text':'Old ambiguous answer'});writer.append_sse_event('cancel',{'message':'Late terminal'})
    for _ in range(3):
        models.SESSIONS.clear();recovered=models.get_session(sid)
        assert recovered.context_messages==before
        assert all(row.get('_recovered_display_only') is True for row in _stream_output(recovered,streams[0]))
        assert not any(row.get('content')=='Old ambiguous answer' for row in _next_send_history(recovered))


@pytest.mark.parametrize('kind', ['reasoning','tool','error'])
def test_proven_legacy_owner_is_not_promoted_without_model_visible_answer(kind):
    sid=f'legacy-provisional-{kind}'
    streams=_persist_multi_retry_turns(sid,['interrupted','cancelled'],[
        [],[('token',{'text':'New answer'})],
    ],defer_first=True)
    first=models.get_session(sid)
    display=next(row for row in first.messages if row.get('role')=='user' and row.get('content')=='Prompt 0')
    context=first.context_messages[0]
    display.update(timestamp=10.5,_recovered=True,_active_turn_token=f'{streams[0]}:10.5')
    context.update(timestamp=10,_recovered=True)
    context.pop('_active_turn_token',None)
    first.save()
    writer=RunJournalWriter(sid,streams[0])
    if kind=='reasoning':
        writer.append_sse_event('reasoning',{'text':'Private saved reasoning'})
    elif kind=='tool':
        writer.append_sse_event('tool',{'name':'read_file','tid':'old-card','args':{'path':'fake.txt'}})
    else:
        writer.append_sse_event('apperror',{
            'session_id':sid,'terminal_session_persisted':False,
            'session':{'session_id':sid,'messages':[
                copy.deepcopy(display),
                {'role':'assistant','content':'Old terminal error','_error':True},
            ]},
        })
    if kind!='error':
        writer.append_sse_event('cancel',{'message':'Old terminal'})
    models.SESSIONS.clear()
    recovered=models.get_session(sid)
    assert _pending_stream_hook(recovered,streams[0]) is None
    output=_stream_output(recovered,streams[0])
    if kind=='reasoning':
        assert any(row.get('role')=='assistant' and row.get('reasoning')=='Private saved reasoning' for row in output)
    elif kind=='tool':
        assert any(tool.get('tid')=='old-card' for tool in recovered.tool_calls)
        _assert_retry_turn_ownership(recovered,['interrupted','cancelled'],streams)
    else:
        assert any(row.get('_error') is True and row.get('content')=='Old terminal error' for row in output)
    for rows in (recovered.messages,recovered.context_messages):
        owner=next(row for row in rows if row.get('role')=='user' and row.get('content')=='Prompt 0')
        assert owner.get('_recovered') is True
    history=_next_send_history(recovered)
    assert not any(row.get('content')=='Private saved reasoning' for row in history)
    assert not any(row.get('content')=='Old terminal error' for row in history)


# October 3 gate: cancelled recovery cannot grant model authority by display alone.
@pytest.mark.parametrize("consumer", ["next-send", "manual-compression"])
@pytest.mark.parametrize("context_owner", ["empty", "absent", "duplicate", "exact"])
@pytest.mark.parametrize("cached", [False, True])
def test_cancelled_display_answer_requires_owner_at_real_model_inputs(
    consumer, context_owner, cached, monkeypatch,
):
    sid = f"gate-cancel-owner-{consumer}-{context_owner}-{cached}"
    stream = sid + "-run"
    answer = "Already emitted cancelled answer"
    session = _start_cancelled_turn(sid, stream)
    previous = [
        {"role": "user", "content": "Prior question one", "timestamp": 1},
        {"role": "assistant", "content": "Prior answer one", "timestamp": 2},
        {"role": "user", "content": "Prior question two", "timestamp": 3},
        {"role": "assistant", "content": "Prior answer two", "timestamp": 4},
    ]
    session.messages = copy.deepcopy(previous)
    session.context_messages = copy.deepcopy(previous)
    session.save()
    assert cancel_stream(stream)
    stopped = Session.load(sid)
    owner = next(row for row in stopped.messages if row.get("role") == "user"
                 and row.get("content") == "Do the cancellable task.")
    stopped.context_messages = [] if context_owner == "empty" else copy.deepcopy(previous)
    if context_owner in {"duplicate", "exact"}:
        stopped.context_messages.append(copy.deepcopy(owner))
        if context_owner == "duplicate":
            stopped.context_messages.append(copy.deepcopy(owner))
    stopped.save()
    writer = RunJournalWriter(sid, stream)
    writer.append_sse_event("token", {"text": answer})
    writer.append_sse_event("cancel", {"message": "Stopped"})
    _simulate_restart()
    recovered = models.get_session(sid)
    if cached:
        assert models.get_session(sid) is recovered
    else:
        models.SESSIONS.clear()
        recovered = models.get_session(sid)
    if consumer == "next-send":
        inputs = _next_send_history(recovered)
    else:
        from tests.test_issue4836_manual_compression_recovery import (
            _FakeAgent, _FakeCompressor, _FakeHandler, _install_fake_compression_runtime,
        )
        from api.routes import _handle_session_compress
        captured = []

        class RecordingCompressor(_FakeCompressor):
            def compress(self, messages, current_tokens=None, focus_topic=None):
                captured.extend(copy.deepcopy(messages))
                return super().compress(messages, current_tokens, focus_topic)

        class RecordingAgent(_FakeAgent):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.context_compressor = RecordingCompressor()

        _install_fake_compression_runtime(monkeypatch, RecordingAgent)
        handler = _FakeHandler()
        _handle_session_compress(handler, {"session_id": sid})
        assert handler.status == 200, handler.payload()
        inputs = captured
    proven = context_owner == "exact"
    assert any(row.get("content") == answer for row in inputs) is proven
    outputs = _stream_output(recovered, stream)
    assert any(row.get("content") == answer for row in outputs)
    assert all((row.get("_recovered_display_only") is True) is (not proven)
               for row in outputs if row.get("content") == answer)
    assert not _pending_stream_hook(recovered, stream)


@pytest.mark.parametrize("same_process", [False, True])
@pytest.mark.parametrize("corruption", [
    "foreign-session", "foreign-run", "foreign-event", "seq77", "duplicate-seq",
    "foreign-session-run-seq77", "foreign-terminal-session", "foreign-terminal-run",
    "bool-seq", "string-seq", "float-seq", "malformed-json", "array-row",
    "forged-terminal", "hidden-terminal", "wrong-terminal-state",
])
def test_cancel_restart_rejects_invalid_entire_journal_before_recovery(
    corruption, same_process,
):
    import json
    from api.run_journal import _run_path
    sid = f"gate-journal-{corruption}-{same_process}"
    stream = sid + "-run"
    _start_cancelled_turn(sid, stream)
    assert cancel_stream(stream)
    writer = RunJournalWriter(sid, stream)
    writer.append_sse_event("token", {"text": "Validated prefix must not bypass bad suffix"})
    writer.append_sse_event("token", {"text": " Foreign seq77 answer"})
    writer.append_sse_event("cancel", {"message": "Terminal"})
    path = _run_path(sid, stream)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    # #7188: cancel_stream() journals a durable terminal cancel row first, so
    # the fixture journal is [cancel(T), token, token, cancel(T)] — 4 rows, not
    # 3. Anchor the corruption targets semantically: first/last token rows and
    # the final (test-authored) terminal row instead of fixed indices.
    token_rows = [row for row in rows if row["event"] == "token"]
    test_terminal = rows[-1]
    bad = token_rows[1]
    if corruption == "foreign-session":
        bad["session_id"] = "different-session"
    elif corruption == "foreign-session-run-seq77":
        bad.update(session_id="different-session", run_id="different-run",
                   seq=77, event_id="different-run:77")
    elif corruption == "foreign-terminal-session":
        test_terminal["session_id"] = "different-session"
    elif corruption == "foreign-terminal-run":
        test_terminal.update(run_id="different-run", event_id="different-run:3")
    elif corruption == "foreign-run":
        bad["run_id"] = "different-run"
    elif corruption == "foreign-event":
        bad["event_id"] = "different-run:2"
    elif corruption == "seq77":
        bad.update(seq=77, event_id=f"{stream}:77")
    elif corruption == "duplicate-seq":
        bad.update(seq=token_rows[0]["seq"], event_id=f"{stream}:{token_rows[0]['seq']}")
    elif corruption == "bool-seq":
        rows[0].update(seq=True, event_id=f"{stream}:1")
    elif corruption == "string-seq":
        bad.update(seq="2")
    elif corruption == "float-seq":
        bad.update(seq=2.0)
    elif corruption == "array-row":
        rows[rows.index(token_rows[1])] = [bad]
    elif corruption == "forged-terminal":
        bad.update(terminal=True, terminal_state="completed")
    elif corruption == "hidden-terminal":
        test_terminal.update(terminal=False)
    elif corruption == "wrong-terminal-state":
        test_terminal.update(terminal_state="completed")
    lines = [json.dumps(row) for row in rows]
    if corruption == "malformed-json":
        lines[rows.index(token_rows[1])] = "{unfinished"
    path.write_text("\n".join(lines) + "\n")
    if same_process:
        config.ACTIVE_RUNS.clear()
        models.SESSIONS.clear()
    else:
        _simulate_restart()
    for _ in range(2):
        recovered = models.get_session(sid)
        assert not _stream_output(recovered, stream)
        assert _pending_stream_hook(recovered, stream) is not None
        models.SESSIONS.clear()


@pytest.mark.parametrize("limit", ["rows", "bytes"])
@pytest.mark.parametrize("oversized", [False, True])
def test_cancel_recovery_journal_boundaries_are_explicit(limit, oversized, monkeypatch):
    from api import run_journal
    sid = f"gate-journal-limit-{limit}-{oversized}"
    stream = sid + "-run"
    _start_cancelled_turn(sid, stream)
    assert cancel_stream(stream)
    writer = RunJournalWriter(sid, stream)
    writer.append_sse_event("token", {"text": "Bounded valid answer"})
    writer.append_sse_event("cancel", {"message": "Terminal"})
    path = run_journal._run_path(sid, stream)
    if limit == "rows":
        # #7188: cancel_stream() already journaled a durable terminal cancel
        # row, so the journal holds 3 rows (cancel, token, cancel), not 2.
        monkeypatch.setattr(run_journal, "_SESSION_REPLAY_MAX_ROWS", 1 if oversized else 3)
    else:
        size = path.stat().st_size
        monkeypatch.setattr(run_journal, "_SESSION_REPLAY_MAX_BYTES", size - 1 if oversized else size)
    _simulate_restart()
    recovered = models.get_session(sid)
    assert _stream_output(recovered, stream)
    assert _pending_stream_hook(recovered, stream) is None
    replay = run_journal.read_session_run_events(
        sid, after_event_id=f"{stream}:1",
        max_rows=run_journal._SESSION_REPLAY_MAX_ROWS,
        max_bytes=run_journal._SESSION_REPLAY_MAX_BYTES,
    )
    assert replay["status"] == (f"replay_limit_{limit}" if oversized else "ok")


def _persist_recovery_boundary_turn(sid, stream, lifecycle):
    session = _start_cancelled_turn(sid, stream)
    session.messages = [{"role": "user", "content": session.pending_user_message,
                         "timestamp": session.pending_started_at}]
    session.context_messages = copy.deepcopy(session.messages)
    session.save()
    if lifecycle == "stop":
        assert cancel_stream(stream)
    return session


def _assert_boundary_output_recovered(sid, stream, answer, *, completed, lifecycle):
    from api import run_journal
    _simulate_restart()
    for _ in range(3):
        recovered = models.get_session(sid)
        outputs = _stream_output(recovered, stream)
        assert [row.get("content") for row in outputs] == [answer]
        assert any(row.get("content") == answer for row in _next_send_history(recovered))
        assert _pending_stream_hook(recovered, stream) is None
        markers = [row for row in recovered.messages if row.get("type") == "interrupted"]
        if lifecycle == "stop" or completed:
            assert not markers
        else:
            # A real nonterminal crash still gets the existing partial-output
            # notice. A reader repair must not conceal that genuine interruption.
            assert len(markers) == 1
            assert "partial output above was recovered" in markers[0]["content"]
            assert "no agent output was recovered" not in markers[0]["content"]
        assert models._run_journal_terminal_state(recovered, stream) == (
            "completed" if completed else (
                # #7188: cancel_stream() journals a durable terminal
                # cancel row, so a stop lifecycle settles the run as
                # interrupted-by-user instead of leaving the journal
                # nonterminal. A crash lifecycle still has no terminal row.
                "interrupted-by-user" if lifecycle == "stop" else None
            )
        )
        assert run_journal.read_run_events(sid, stream, validated_recovery=True)["events"]
        models.SESSIONS.clear()


@pytest.mark.parametrize("lifecycle", ["crash", "stop"])
@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("token_rows", [4095, 4096, 4097])
def test_authoritative_recovery_keeps_long_journal(lifecycle, completed, token_rows):
    from api import run_journal
    sid = f"long-recovery-{lifecycle}-{completed}-{token_rows}"
    stream = sid + "-run"
    session = _persist_recovery_boundary_turn(sid, stream, lifecycle)
    writer = RunJournalWriter(sid, stream)
    for _ in range(token_rows):
        writer.append_sse_event("token", {"text": "x"})
    if completed:
        writer.append_sse_event("done", {"session": public_session_projection(session.__dict__)})
    # #7188: cancel_stream() journals a durable terminal cancel row before
    # these appends, so the journal carries token_rows + int(completed) + 1.
    rows = token_rows + int(completed) + (1 if lifecycle == "stop" else 0)
    replay = run_journal.read_session_run_events(sid, after_event_id=f"{stream}:1")
    assert replay["status"] == ("replay_limit_rows" if rows > 4096 else "ok")
    _assert_boundary_output_recovered(
        sid, stream, "x" * token_rows, completed=completed, lifecycle=lifecycle,
    )


@pytest.mark.parametrize("lifecycle", ["crash", "stop"])
@pytest.mark.parametrize("large_event", ["token", "done"])
def test_authoritative_recovery_keeps_journal_over_four_mib(lifecycle, large_event):
    from api import run_journal
    sid = f"large-recovery-{lifecycle}-{large_event}"
    stream = sid + "-run"
    session = _persist_recovery_boundary_turn(sid, stream, lifecycle)
    writer = RunJournalWriter(sid, stream)
    answer = "Long answer end" if large_event == "done" else "x" * (4 * 1024 * 1024) + "END"
    writer.append_sse_event("token", {"text": answer})
    if large_event == "done":
        payload = public_session_projection(session.__dict__)
        payload["messages"].append({"role": "assistant", "content": "x" * (4 * 1024 * 1024)})
        writer.append_sse_event("done", {"session": payload})
    assert run_journal._run_path(sid, stream).stat().st_size > 4 * 1024 * 1024
    assert run_journal.read_session_run_events(
        sid, after_event_id=f"{stream}:1",
    )["status"] == "replay_limit_bytes"
    _assert_boundary_output_recovered(
        sid, stream, answer, completed=large_event == "done", lifecycle=lifecycle,
    )


@pytest.mark.parametrize("lifecycle", ["crash", "stop"])
@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("tail", [b'{"seq":', b'{"payload":{"text":"\xe2\x82'])
def test_authoritative_recovery_keeps_valid_prefix_before_torn_tail(lifecycle, completed, tail):
    from api import run_journal
    sid = f"torn-recovery-{lifecycle}-{completed}-{len(tail)}"
    stream = sid + "-run"
    session = _persist_recovery_boundary_turn(sid, stream, lifecycle)
    writer = RunJournalWriter(sid, stream)
    answer = "Durable prefix answer"
    writer.append_sse_event("token", {"text": answer})
    if completed:
        writer.append_sse_event("done", {"session": public_session_projection(session.__dict__)})
    with run_journal._run_path(sid, stream).open("ab") as fh:
        fh.write(tail)
    _assert_boundary_output_recovered(sid, stream, answer, completed=completed, lifecycle=lifecycle)


@pytest.mark.parametrize("same_process", [False, True])
@pytest.mark.parametrize("tail_kind", [
    "torn-cancel", "torn-after-cancel", "valid-foreign", "valid-gap",
    "forged-terminal", "malformed-newline", "malformed-middle", "invalid-utf8",
    "complete-no-newline",
])
def test_torn_tail_cannot_bypass_identity_or_terminal_admission(same_process, tail_kind):
    import json
    from api import run_journal
    sid = f"tail-admission-{same_process}-{tail_kind}"
    stream = sid + "-run"
    _persist_recovery_boundary_turn(sid, stream, "stop")
    writer = RunJournalWriter(sid, stream)
    writer.append_sse_event("token", {"text": "Verified prefix"})
    path = run_journal._run_path(sid, stream)
    # #7188: cancel_stream() journals a durable terminal cancel row, so the
    # fixture journal already opens with [cancel(T), token]. For the torn-cancel
    # same-process case the original contract ("a nonterminal journal keeps the
    # lazy hook armed in the same process") must hold, so suppress the durable
    # terminal row by rebuilding the journal from a cancel whose terminal write
    # failed: keep the token row, drop the terminal row, then append the torn
    # cancel tail bytes.
    if tail_kind == "torn-cancel" and same_process:
        keep = [
            line
            for line in path.read_bytes().splitlines(keepends=True)
            if b'"event":"cancel"' not in line
        ]
        path.write_bytes(b"".join(keep))
    if tail_kind == "torn-cancel":
        with path.open("ab") as fh:
            fh.write(b'{"event":"cancel","terminal":true')
    else:
        terminal = writer.append_sse_event("cancel", {"message": "Stopped"})
        if tail_kind in {"valid-foreign", "valid-gap", "forged-terminal"}:
            if tail_kind == "valid-foreign":
                terminal["session_id"] = "foreign-session"
            elif tail_kind == "valid-gap":
                terminal.update(seq=77, event_id=f"{stream}:77")
            else:
                terminal["terminal_state"] = "completed"
            path.write_bytes(path.read_bytes().splitlines(keepends=True)[0]
                             + json.dumps(terminal).encode())
        elif tail_kind == "complete-no-newline":
            path.write_bytes(path.read_bytes().rstrip(b"\n"))
        else:
            tail = b'{"seq":'
            if tail_kind in {"malformed-newline", "malformed-middle"}:
                tail += b"\n"
            elif tail_kind == "invalid-utf8":
                tail = b'{"text":"\xff"}'
            with path.open("ab") as fh:
                fh.write(tail)
                if tail_kind == "malformed-middle":
                    fh.write(json.dumps(terminal).encode() + b"\n")
    if same_process:
        config.ACTIVE_RUNS.clear()
        models.SESSIONS.clear()
    else:
        _simulate_restart()
    allowed = tail_kind in {"torn-after-cancel", "complete-no-newline"} or (
        tail_kind == "torn-cancel" and not same_process
    )
    for _ in range(2):
        recovered = models.get_session(sid)
        assert bool(_stream_output(recovered, stream)) is allowed
        assert (_pending_stream_hook(recovered, stream) is None) is allowed
        models.SESSIONS.clear()


@pytest.mark.parametrize("corruption", ["foreign-session", "gap", "terminal", "malformed"])
def test_long_recovery_still_validates_rows_outside_client_and_sequence_windows(corruption):
    import json
    from api import run_journal
    sid = f"long-invalid-{corruption}"
    stream = sid + "-run"
    _persist_recovery_boundary_turn(sid, stream, "stop")
    writer = RunJournalWriter(sid, stream)
    for _ in range(4096):
        writer.append_sse_event("token", {"text": "x"})
    bad = writer.append_sse_event("cancel", {"message": "Stopped"})
    if corruption == "foreign-session":
        bad["session_id"] = "foreign-session"
    elif corruption == "gap":
        bad.update(seq=77, event_id=f"{stream}:77")
    elif corruption == "terminal":
        bad["terminal"] = False
    path = run_journal._run_path(sid, stream)
    rows = path.read_bytes().splitlines(keepends=True)
    rows[-1] = b"{malformed\n" if corruption == "malformed" else json.dumps(bad).encode()
    path.write_bytes(b"".join(rows))
    filtered = run_journal.read_run_events(sid, stream, max_seq=1, validated_recovery=True)
    assert filtered["events"] == []
    assert filtered["malformed"]
    _simulate_restart()
    recovered = models.get_session(sid)
    assert not _stream_output(recovered, stream)
    assert _pending_stream_hook(recovered, stream) is not None
