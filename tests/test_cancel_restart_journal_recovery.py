"""Focused restart durability for journal-only output after WebUI Stop."""

from __future__ import annotations

import copy
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
