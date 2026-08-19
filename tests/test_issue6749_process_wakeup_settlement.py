"""Faithful process-wakeup final-settlement trace for issue #6749.

This test intentionally models the crash/recovery ordering called out by the
issue: the same wakeup run has already persisted its final assistant row, but
the late display snapshot still contains an empty partial assistant row.  The
Agent result carries an independent normal-completion contract and the
canonical tool history remains paired.
"""

from __future__ import annotations

import copy
import json
import queue
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

import api.config as config
import api.models as models
import api.streaming as streaming
from api.models import Session
from api.run_journal import read_run_events
from api.turn_journal import read_turn_journal


@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    yield
    models.SESSIONS.clear()


@pytest.fixture(autouse=True)
def _isolate_stream_state():
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    if hasattr(config, "STREAM_REASONING_TEXT"):
        config.STREAM_REASONING_TEXT.clear()
    if hasattr(config, "STREAM_LIVE_TOOL_CALLS"):
        config.STREAM_LIVE_TOOL_CALLS.clear()
    yield
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    if hasattr(config, "STREAM_REASONING_TEXT"):
        config.STREAM_REASONING_TEXT.clear()
    if hasattr(config, "STREAM_LIVE_TOOL_CALLS"):
        config.STREAM_LIVE_TOOL_CALLS.clear()


@pytest.fixture(autouse=True)
def _isolate_agent_locks():
    config.SESSION_AGENT_LOCKS.clear()
    yield
    config.SESSION_AGENT_LOCKS.clear()


@pytest.fixture(autouse=True)
def _mock_hermes_modules(monkeypatch):
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = lambda requested=None, **_kw: {
        "provider": requested or "test-provider",
        "api_key": "synthetic-key",
        "base_url": None,
    }
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = mock.Mock(return_value=None)
    injected = {
        "hermes_cli": fake_hermes_cli,
        "hermes_cli.runtime_provider": fake_runtime_module,
        "hermes_state": fake_hermes_state,
    }
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in injected}
    sys.modules.update(injected)
    yield
    for name, previous in saved.items():
        if previous is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


class _FaithfulCompletedAgent:
    """Synthetic Agent shaped like turn_finalizer's normal completion result."""

    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id")
        self.model = kwargs.get("model")
        self.provider = kwargs.get("provider")
        self.base_url = kwargs.get("base_url")
        self.stream_delta_callback = kwargs.get("stream_delta_callback")
        self.reasoning_callback = kwargs.get("reasoning_callback")
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.status_callback = kwargs.get("status_callback")
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.session_prompt_tokens = 1
        self.session_completion_tokens = 1
        self.session_estimated_cost_usd = 0.0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_api_calls = 1
        self.context_compressor = None
        self._last_error = None
        self._current_turn_id = "turn-6749-wakeup"
        self._current_run_id = getattr(
            self,
            "trace_run_id",
            "run:stream-6749-wakeup",
        )
        # Deliberately leave this unset/None: recovery has not materialized the
        # pending process-wakeup user row when the final assistant is persisted.
        self._persist_user_message_idx = None
        self.ephemeral_system_prompt = None

    def run_conversation(self, **kwargs):
        history = copy.deepcopy(list(kwargs.get("conversation_history") or []))
        active_user = {
            "role": "user",
            "content": self.trace_user_message,
            "timestamp": self.trace_pending_started_at,
            "_source": "process_wakeup",
            "_active_turn_token": streaming.build_active_turn_token(
                self.trace_stream_id,
                self.trace_pending_started_at,
            ),
        }
        first_active_tool = next(
            (
                index
                for index, row in enumerate(history)
                if isinstance(row, dict)
                and row.get("role") == "assistant"
                and row.get("tool_calls")
            ),
            len(history),
        )
        history.insert(first_active_tool, active_user)
        final_rows = [
            row
            for row in history
            if isinstance(row, dict)
            and row.get("role") == "assistant"
            and row.get("content") == "Final wakeup answer"
        ]
        assert len(final_rows) == 1, "faithful trace must carry one persisted final row"
        final_rows[0]["_recovered_from_run_journal"] = True
        final_rows[0]["_recovered_stream_id"] = self.result_owner_stream_id
        final_rows[0]["turn_id"] = self._current_turn_id
        final_rows[0]["_run_id"] = self._current_run_id
        # This is the reported late display snapshot: it repeats the completed
        # answer and carries WebUI-only tool activity.  The canonical Agent
        # assistant/tool rows above, not these display flags, prove closure.
        history.append(
            {
                "role": "assistant",
                "content": "Final wakeup answer",
                "_partial": True,
                "_partial_tool_calls": [
                    {
                        "tid": "call-6749-1",
                        "name": "inspect_1",
                        "done": True,
                        "snippet": "tool result 1",
                    },
                    {
                        "tid": "call-6749-2",
                        "name": "inspect_2",
                        "done": True,
                        "snippet": "tool result 2",
                    },
                ],
                "_recovered_from_run_journal": True,
                "_recovered_stream_id": self.trace_stream_id,
            }
        )
        return {
            "messages": history,
            "completed": True,
            "failed": False,
            "interrupted": False,
            "partial": False,
            "status": "ok",
            "turn_exit_reason": "text_response(stop)",
            "finish_reason": "stop",
            "final_response": "Final wakeup answer",
            "turn_id": self._current_turn_id,
            "run_id": self._current_run_id,
            "session_id": self.session_id,
            "error": None,
        }

    def interrupt(self, _message):
        return None


class _HealthyProcessWakeupAgent(_FaithfulCompletedAgent):
    """Normal wakeup result with no recovery rows or tool arc."""

    def run_conversation(self, **kwargs):
        history = copy.deepcopy(list(kwargs.get("conversation_history") or []))
        history.append(
            {
                "role": "user",
                "content": self.trace_user_message,
                "timestamp": self.trace_pending_started_at,
                "_source": "process_wakeup",
                "_active_turn_token": streaming.build_active_turn_token(
                    self.trace_stream_id,
                    self.trace_pending_started_at,
                ),
            }
        )
        history.append({"role": "assistant", "content": "Healthy wakeup answer"})
        return {
            "messages": history,
            "completed": True,
            "failed": False,
            "interrupted": False,
            "partial": False,
            "status": "ok",
            "turn_exit_reason": "text_response(stop)",
            "finish_reason": "stop",
            "final_response": "Healthy wakeup answer",
            "turn_id": self._current_turn_id,
            "session_id": self.session_id,
            "error": None,
        }


def _tool_pair(index: int) -> tuple[dict, dict]:
    tool_id = f"call-6749-{index}"
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tool_id,
                "type": "function",
                "function": {
                    "name": f"inspect_{index}",
                    "arguments": json.dumps({"index": index}),
                },
            }
        ],
    }
    result = {
        "role": "tool",
        "tool_call_id": tool_id,
        "content": f"tool result {index}",
    }
    return assistant, result


def _prepare_persisted_wakeup_final(
    session_id: str,
    stream_id: str,
    wakeup_prompt: str,
    *,
    final_owner_stream_id: str | None = None,
) -> Session:
    session = Session(
        session_id=session_id,
        title="Issue 6749 faithful trace",
        workspace=str(Path.cwd()),
        model="test-model",
        model_provider="test-provider",
    )
    messages = [
        {"role": "user", "content": "Earlier turn", "timestamp": 1},
        {"role": "assistant", "content": "Earlier answer", "timestamp": 2},
    ]
    for index in (1, 2):
        assistant, result = _tool_pair(index)
        messages.extend((assistant, result))
    messages.append(
        {
            "role": "assistant",
            "content": "Final wakeup answer",
            "timestamp": 10,
            "_recovered_from_run_journal": True,
            "_recovered_stream_id": stream_id,
            "turn_id": "turn-6749-wakeup",
            "_run_id": f"run:{stream_id}",
        }
    )
    # The final row is already durable, while the pending wakeup user row has
    # not yet been materialized.  The agent result materializes the active
    # synthetic user immediately before this turn's tool closure.
    final_owner_stream_id = final_owner_stream_id or stream_id
    for row in messages:
        if row.get("content") == "Final wakeup answer":
            row["_recovered_stream_id"] = final_owner_stream_id
    session.messages = copy.deepcopy(messages)
    session.context_messages = copy.deepcopy(messages)
    session.pending_user_message = wakeup_prompt
    session.pending_attachments = []
    session.pending_started_at = 1234567890.0
    session.pending_user_source = "process_wakeup"
    session.active_stream_id = stream_id
    session.save()
    models.SESSIONS[session_id] = session
    return session


def _prepare_healthy_wakeup(session_id: str, stream_id: str, wakeup_prompt: str) -> Session:
    session = Session(
        session_id=session_id,
        title="Issue 6749 healthy wakeup",
        workspace=str(Path.cwd()),
        model="test-model",
        model_provider="test-provider",
    )
    session.messages = [
        {"role": "user", "content": "Earlier turn", "timestamp": 1},
        {"role": "assistant", "content": "Earlier answer", "timestamp": 2},
    ]
    session.context_messages = copy.deepcopy(session.messages)
    session.pending_user_message = wakeup_prompt
    session.pending_attachments = []
    session.pending_started_at = 1234567890.0
    session.pending_user_source = "process_wakeup"
    session.active_stream_id = stream_id
    session.save()
    models.SESSIONS[session_id] = session
    return session


def _closed_wakeup_session(session_id: str, stream_id: str, *, partial=None) -> Session:
    session = _prepare_persisted_wakeup_final(
        session_id,
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    active_user = {
        "role": "user",
        "content": session.pending_user_message,
        "timestamp": session.pending_started_at,
        "_source": "process_wakeup",
        "_active_turn_token": streaming.build_active_turn_token(
            stream_id,
            session.pending_started_at,
        ),
    }
    rows = copy.deepcopy(session.messages)
    rows.insert(2, active_user)
    if partial is not None:
        rows.append(partial)
    session.messages = rows
    session.context_messages = copy.deepcopy(rows)
    return session


def _queue_events(fake_queue):
    return [(item[0], item[1]) for item in list(fake_queue.queue)]


def _run_stream(session, stream_id: str, agent_cls, workspace: str):
    fake_queue = queue.Queue()
    streaming.STREAMS[stream_id] = fake_queue
    config.STREAM_PARTIAL_TEXT[stream_id] = ""
    class BoundAgent(agent_cls):
        trace_stream_id = stream_id
        trace_run_id = f"run:{stream_id}"
        trace_user_message = session.pending_user_message
        trace_pending_started_at = session.pending_started_at
        result_owner_stream_id = getattr(agent_cls, "result_owner_stream_id", stream_id)

    with mock.patch.object(streaming, "get_session", return_value=session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=BoundAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            workspace=workspace,
            stream_id=stream_id,
        )
    return fake_queue


def _trace_payload(session, fake_queue, stream_id: str):
    saved = Session.load(session.session_id)
    stream_events = _queue_events(fake_queue)
    run_events = read_run_events(session.session_id, stream_id)["events"]
    turn_events = read_turn_journal(session.session_id)["events"]
    return {
        "stream_events": [event for event, _data in stream_events],
        "stream_terminal_payloads": [
            data for event, data in stream_events if event in {"done", "apperror", "stream_end"}
        ],
        "saved_messages": [
            {
                "role": row.get("role"),
                "content": row.get("content"),
                "partial": bool(row.get("_partial")),
                "error": bool(row.get("_error")),
                "recovered_stream_id": row.get("_recovered_stream_id"),
                "id": row.get("id"),
                "turn_id": row.get("turn_id"),
                "tool_call_ids": [
                    call.get("id")
                    for call in row.get("tool_calls") or []
                    if isinstance(call, dict)
                ],
                "tool_call_id": row.get("tool_call_id"),
            }
            for row in (saved.messages if saved else [])
            if isinstance(row, dict)
        ],
        "run_journal": [
            {
                "event": row.get("event"),
                "terminal": row.get("terminal"),
                "terminal_state": row.get("terminal_state"),
                "seq": row.get("seq"),
            }
            for row in run_events
        ],
        "turn_journal": [
            {
                "event": row.get("event"),
                "terminal": row.get("terminal"),
                "stream_id": row.get("stream_id"),
                "turn_id": row.get("turn_id"),
                "assistant_message_index": row.get("assistant_message_index"),
            }
            for row in turn_events
        ],
    }


def _phase0_merge_inputs(scope):
    stream_id = f"stream-6749-phase0-{scope}"
    wakeup_prompt = "[IMPORTANT: Background process completed]"
    previous_session = _prepare_persisted_wakeup_final(
        f"issue6749-phase0-previous-{scope}",
        stream_id,
        wakeup_prompt,
    )
    previous_display = copy.deepcopy(previous_session.messages)
    candidates = copy.deepcopy(
        _closed_wakeup_session(
            f"issue6749-phase0-candidates-{scope}",
            stream_id,
        ).messages
    )
    active_identity = {
        "source": "process_wakeup",
        "stream_id": stream_id,
        "token": streaming.build_active_turn_token(stream_id, 1234567890.0),
        "turn_id": "turn-6749-wakeup",
        "run_id": f"run:{stream_id}",
    }
    provenance = {
        "verification_nudge_seen": False,
        "active_turn_identity": active_identity,
    }
    return (
        previous_display,
        copy.deepcopy(previous_display),
        candidates,
        wakeup_prompt,
        provenance,
    )


def test_process_wakeup_merge_preserves_replay_without_settlement_authorization(monkeypatch):
    previous_display, previous_context, candidates, wakeup_prompt, provenance = (
        _phase0_merge_inputs("default")
    )
    strip_calls = []

    def spy_strip(previous, candidate_rows, active_identity):
        strip_calls.append((previous, candidate_rows, active_identity))
        return previous

    monkeypatch.setattr(streaming, "_strip_replayed_process_wakeup_arc", spy_strip)
    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        candidates,
        wakeup_prompt,
        source="process_wakeup",
        verification_nudge_provenance=provenance,
    )

    assert strip_calls == []
    assert sum(
        row.get("role") == "assistant" and row.get("content") == "Final wakeup answer"
        for row in merged
    ) == 2


def test_process_wakeup_normal_completion_preserves_replay_before_settlement(tmp_path, monkeypatch):
    session_id = "issue6749-wakeup"
    stream_id = "stream-6749-wakeup"
    wakeup_prompt = "[IMPORTANT: Background process completed]"
    session = _prepare_persisted_wakeup_final(session_id, stream_id, wakeup_prompt)
    fake_queue = _run_stream(
        session,
        stream_id,
        _FaithfulCompletedAgent,
        str(tmp_path),
    )

    trace = _trace_payload(session, fake_queue, stream_id)
    trace_path = tmp_path / "issue-6749-faithful-trace.json"
    trace_path.write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")

    saved = Session.load(session_id)
    assert saved is not None
    final_rows = [
        row
        for row in saved.messages
        if row.get("role") == "assistant" and row.get("content") == "Final wakeup answer"
    ]
    stream_event_names = trace["stream_events"]
    assert stream_event_names.count("done") == 1, f"faithful trace emitted wrong terminal events; evidence={trace_path}\n{json.dumps(trace, indent=2)}"
    assert "apperror" not in stream_event_names, f"faithful trace emitted generated failure; evidence={trace_path}\n{json.dumps(trace, indent=2)}"
    assert len(final_rows) == 2, f"phase-0 settlement deleted a replay observation without authorization; evidence={trace_path}\n{json.dumps(trace, indent=2)}"
    assert not any(row.get("_error") for row in saved.messages), f"error row persisted; evidence={trace_path}\n{json.dumps(trace, indent=2)}"
    assert not any(row.get("_partial") for row in saved.messages), f"late partial marker survived; evidence={trace_path}\n{json.dumps(trace, indent=2)}"
    active_user_index = next(
        index
        for index, row in enumerate(saved.messages)
        if row.get("role") == "user" and row.get("_source") == "process_wakeup"
    )
    final_index = next(
        index
        for index, row in enumerate(saved.messages)
        if index > active_user_index
        and row.get("role") == "assistant"
        and row.get("content") == "Final wakeup answer"
    )
    assert final_index > active_user_index, f"final row not settled behind its owner; evidence={trace_path}\n{json.dumps(trace, indent=2)}"
    current_arc = saved.messages[active_user_index + 1 : final_index]
    call_ids = [
        call.get("id")
        for row in current_arc
        if row.get("role") == "assistant"
        for call in row.get("tool_calls") or []
        if isinstance(call, dict) and call.get("id")
    ]
    result_ids = [
        row.get("tool_call_id")
        for row in current_arc
        if row.get("role") == "tool" and row.get("tool_call_id")
    ]
    assert call_ids == ["call-6749-1", "call-6749-2"]
    assert result_ids == call_ids
    assert [row.get("role") for row in current_arc] == ["assistant", "tool", "assistant", "tool"]
    all_call_ids = [
        call.get("id")
        for row in saved.messages
        if row.get("role") == "assistant"
        for call in row.get("tool_calls") or []
        if isinstance(call, dict) and call.get("id")
    ]
    all_result_ids = [
        row.get("tool_call_id")
        for row in saved.messages
        if row.get("role") == "tool" and row.get("tool_call_id")
    ]
    assert all_call_ids.count("call-6749-1") == 2
    assert all_call_ids.count("call-6749-2") == 2
    assert all_result_ids.count("call-6749-1") == 2
    assert all_result_ids.count("call-6749-2") == 2
    prior_call_ids = [
        call.get("id")
        for row in saved.messages[:active_user_index]
        if row.get("role") == "assistant"
        for call in row.get("tool_calls") or []
        if isinstance(call, dict) and call.get("id")
    ]
    prior_result_ids = [
        row.get("tool_call_id")
        for row in saved.messages[:active_user_index]
        if row.get("role") == "tool" and row.get("tool_call_id")
    ]
    assert prior_call_ids == ["call-6749-1", "call-6749-2"]
    assert prior_result_ids == prior_call_ids
    assert next(
        row["terminal_state"]
        for row in trace["run_journal"]
        if row["event"] == "done"
    ) == "completed", f"run journal did not settle completed; evidence={trace_path}\n{json.dumps(trace, indent=2)}"
    assert any(row.get("event") == "completed" for row in trace["turn_journal"]), f"turn journal missing completed; evidence={trace_path}\n{json.dumps(trace, indent=2)}"


class _ProviderErrorAfterFinalAgent(_FaithfulCompletedAgent):
    def run_conversation(self, **kwargs):
        result = super().run_conversation(**kwargs)
        result.update(
            {
                "completed": False,
                "failed": True,
                "error": {"message": "provider failed after final"},
            }
        )
        return result


class _MissingToolResultAgent(_FaithfulCompletedAgent):
    def run_conversation(self, **kwargs):
        result = super().run_conversation(**kwargs)
        result["messages"] = [
            row for row in result["messages"]
            if not (
                isinstance(row, dict)
                and row.get("role") == "tool"
                and row.get("tool_call_id") == "call-6749-2"
            )
        ]
        result.update(
            {
                "completed": False,
                "partial": True,
                "turn_exit_reason": "tool_result_missing",
                "final_response": "",
            }
        )
        return result


class _GenuinelyPartialAgent(_FaithfulCompletedAgent):
    def run_conversation(self, **kwargs):
        result = super().run_conversation(**kwargs)
        result["messages"] = [
            row for row in result["messages"]
            if not (
                isinstance(row, dict)
                and row.get("role") == "assistant"
                and row.get("content") == "Final wakeup answer"
            )
        ]
        result.update(
            {
                "completed": False,
                "partial": True,
                "turn_exit_reason": "max_iterations_reached(30/30)",
                "final_response": "",
            }
        )
        return result


class _MismatchedRunAgent(_FaithfulCompletedAgent):
    result_owner_stream_id = "other-stream"

    def run_conversation(self, **kwargs):
        result = super().run_conversation(**kwargs)
        result["messages"] = [
            row for row in result["messages"]
            if not (
                isinstance(row, dict)
                and row.get("role") == "assistant"
                and row.get("content") == "Final wakeup answer"
            )
        ]
        return result


def _assert_failure_settlement(session, fake_queue, stream_id: str, tmp_path):
    trace = _trace_payload(session, fake_queue, stream_id)
    trace_path = tmp_path / f"{stream_id}-trace.json"
    trace_path.write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")
    events = trace["stream_events"]
    assert "done" not in events, f"negative trace settled done; evidence={trace_path}\n{json.dumps(trace, indent=2)}"
    assert "apperror" in events, f"negative trace lost terminal failure; evidence={trace_path}\n{json.dumps(trace, indent=2)}"
    saved = Session.load(session.session_id)
    assert saved is not None
    assert any(row.get("_error") for row in saved.messages), f"negative trace has no persisted error; evidence={trace_path}\n{json.dumps(trace, indent=2)}"


def test_process_wakeup_missing_tool_result_keeps_failure(tmp_path, monkeypatch):
    session_id = "issue6749-missing-tool-result"
    stream_id = "stream-6749-missing-tool-result"
    session = _prepare_persisted_wakeup_final(
        session_id,
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    session.messages = [
        row
        for row in session.messages
        if row.get("tool_call_id") != "call-6749-2"
    ]
    session.context_messages = copy.deepcopy(session.messages)
    session.save()
    fake_queue = _run_stream(session, stream_id, _MissingToolResultAgent, str(tmp_path))
    _assert_failure_settlement(session, fake_queue, stream_id, tmp_path)


def test_process_wakeup_provider_error_after_final_keeps_failure(tmp_path, monkeypatch):
    session_id = "issue6749-provider-error"
    stream_id = "stream-6749-provider-error"
    session = _prepare_persisted_wakeup_final(
        session_id,
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    fake_queue = _run_stream(session, stream_id, _ProviderErrorAfterFinalAgent, str(tmp_path))
    _assert_failure_settlement(session, fake_queue, stream_id, tmp_path)


def test_process_wakeup_mismatched_run_identity_keeps_failure(tmp_path, monkeypatch):
    session_id = "issue6749-mismatched-run"
    stream_id = "stream-6749-mismatched-run"
    session = _prepare_persisted_wakeup_final(
        session_id,
        stream_id,
        "[IMPORTANT: Background process completed]",
        final_owner_stream_id="other-stream",
    )
    fake_queue = _run_stream(session, stream_id, _MismatchedRunAgent, str(tmp_path))
    _assert_failure_settlement(session, fake_queue, stream_id, tmp_path)


def test_process_wakeup_genuinely_partial_result_keeps_failure(tmp_path, monkeypatch):
    session_id = "issue6749-genuinely-partial"
    stream_id = "stream-6749-genuinely-partial"
    session = _prepare_persisted_wakeup_final(
        session_id,
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    fake_queue = _run_stream(session, stream_id, _GenuinelyPartialAgent, str(tmp_path))
    _assert_failure_settlement(session, fake_queue, stream_id, tmp_path)


def test_process_wakeup_healthy_result_without_recovery_shape_succeeds(tmp_path):
    session_id = "issue6749-healthy"
    stream_id = "stream-6749-healthy"
    session = _prepare_healthy_wakeup(
        session_id,
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    fake_queue = _run_stream(session, stream_id, _HealthyProcessWakeupAgent, str(tmp_path))
    trace = _trace_payload(session, fake_queue, stream_id)
    assert trace["stream_events"].count("done") == 1
    assert "apperror" not in trace["stream_events"]
    saved = Session.load(session_id)
    assert saved is not None
    assert any(row.get("content") == "Healthy wakeup answer" for row in saved.messages)
    assert not any(row.get("_recovered_stream_id") for row in saved.messages)
    assert not any(row.get("role") == "tool" for row in saved.messages)


def test_process_wakeup_replay_strip_is_scoped_to_process_wakeup_source():
    stream_id = "stream-6749-source-scope"
    previous_session = _prepare_persisted_wakeup_final(
        "issue6749-source-previous",
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    previous = copy.deepcopy(previous_session.messages)
    candidates = _closed_wakeup_session(
        "issue6749-source-candidates",
        stream_id,
    ).messages
    identity = {
        "source": "webui",
        "stream_id": stream_id,
        "token": streaming.build_active_turn_token(stream_id, 1234567890.0),
        "turn_id": "turn-6749-wakeup",
        "run_id": f"run:{stream_id}",
    }
    assert streaming._strip_replayed_process_wakeup_arc(previous, candidates, identity) == previous


def test_process_wakeup_replay_strip_rejects_mismatched_turn_identity():
    stream_id = "stream-6749-turn-scope"
    previous_session = _prepare_persisted_wakeup_final(
        "issue6749-turn-previous",
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    previous = copy.deepcopy(previous_session.messages)
    candidates = _closed_wakeup_session(
        "issue6749-turn-candidates",
        stream_id,
    ).messages
    identity = {
        "source": "process_wakeup",
        "stream_id": stream_id,
        "token": streaming.build_active_turn_token(stream_id, 1234567890.0),
        "turn_id": "different-turn",
        "run_id": f"run:{stream_id}",
    }
    assert streaming._strip_replayed_process_wakeup_arc(previous, candidates, identity) == previous


def test_process_wakeup_replay_strip_preserves_older_identical_answer():
    stream_id = "stream-6749-older-answer"
    previous_session = _prepare_persisted_wakeup_final(
        "issue6749-older-answer-previous",
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    older = {
        "role": "assistant",
        "content": "Final wakeup answer",
        "timestamp": 0,
    }
    previous = [older, *copy.deepcopy(previous_session.messages)]
    candidates = _closed_wakeup_session(
        "issue6749-older-answer-candidates",
        stream_id,
    ).messages
    identity = {
        "source": "process_wakeup",
        "stream_id": stream_id,
        "token": streaming.build_active_turn_token(stream_id, 1234567890.0),
        "turn_id": "turn-6749-wakeup",
        "run_id": f"run:{stream_id}",
    }
    settled = streaming._strip_replayed_process_wakeup_arc(
        previous,
        candidates,
        identity,
    )
    assert settled[0] is older
    assert sum(
        row.get("role") == "assistant"
        and row.get("content") == "Final wakeup answer"
        for row in settled
    ) == 1


def test_process_wakeup_replay_strip_rejects_tool_before_call():
    stream_id = "stream-6749-tool-before-call"
    previous_session = _prepare_persisted_wakeup_final(
        "issue6749-tool-before-call-previous",
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    previous = copy.deepcopy(previous_session.messages)
    candidates = _closed_wakeup_session(
        "issue6749-tool-before-call-candidates",
        stream_id,
    ).messages
    active_index = next(
        index for index, row in enumerate(candidates)
        if row.get("_source") == "process_wakeup"
    )
    candidates[active_index + 1], candidates[active_index + 2] = (
        candidates[active_index + 2],
        candidates[active_index + 1],
    )
    previous[2], previous[3] = previous[3], previous[2]
    identity = {
        "source": "process_wakeup",
        "stream_id": stream_id,
        "token": streaming.build_active_turn_token(stream_id, 1234567890.0),
        "turn_id": "turn-6749-wakeup",
        "run_id": f"run:{stream_id}",
    }
    assert streaming._strip_replayed_process_wakeup_arc(
        previous, candidates, identity
    ) == previous


def test_process_wakeup_replay_strip_rejects_intermediary_plain_assistant():
    stream_id = "stream-6749-intermediary-assistant"
    previous_session = _prepare_persisted_wakeup_final(
        "issue6749-intermediary-assistant-previous",
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    previous = copy.deepcopy(previous_session.messages)
    candidates = _closed_wakeup_session(
        "issue6749-intermediary-assistant-candidates",
        stream_id,
    ).messages
    active_index = next(
        index for index, row in enumerate(candidates)
        if row.get("_source") == "process_wakeup"
    )
    candidates.insert(
        active_index + 1,
        {"role": "assistant", "content": "intermediary text"},
    )
    previous.insert(2, {"role": "assistant", "content": "intermediary text"})
    identity = {
        "source": "process_wakeup",
        "stream_id": stream_id,
        "token": streaming.build_active_turn_token(stream_id, 1234567890.0),
        "turn_id": "turn-6749-wakeup",
        "run_id": f"run:{stream_id}",
    }
    assert streaming._strip_replayed_process_wakeup_arc(
        previous, candidates, identity
    ) == previous


def test_process_wakeup_replay_strip_rejects_lossy_content_match():
    stream_id = "stream-6749-lossy-content"
    previous_session = _prepare_persisted_wakeup_final(
        "issue6749-lossy-content-previous",
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    previous = copy.deepcopy(previous_session.messages)
    candidates = _closed_wakeup_session(
        "issue6749-lossy-content-candidates",
        stream_id,
    ).messages
    previous[3]["content"] = "x" * 500 + "previous-tail"
    candidates[4]["content"] = "x" * 500 + "candidate-tail"
    identity = {
        "source": "process_wakeup",
        "stream_id": stream_id,
        "token": streaming.build_active_turn_token(stream_id, 1234567890.0),
        "turn_id": "turn-6749-wakeup",
        "run_id": f"run:{stream_id}",
    }
    assert streaming._strip_replayed_process_wakeup_arc(
        previous, candidates, identity
    ) == previous


def test_process_wakeup_replay_strip_is_idempotent_with_ambiguous_matches():
    stream_id = "stream-6749-idempotent"
    previous_session = _prepare_persisted_wakeup_final(
        "issue6749-idempotent-previous",
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    previous = copy.deepcopy(previous_session.messages)
    previous.extend(copy.deepcopy(previous_session.messages[2:]))
    candidates = _closed_wakeup_session(
        "issue6749-idempotent-candidates",
        stream_id,
    ).messages
    identity = {
        "source": "process_wakeup",
        "stream_id": stream_id,
        "token": streaming.build_active_turn_token(stream_id, 1234567890.0),
        "turn_id": "turn-6749-wakeup",
        "run_id": f"run:{stream_id}",
    }
    settled = streaming._strip_replayed_process_wakeup_arc(
        previous, candidates, identity
    )
    assert streaming._strip_replayed_process_wakeup_arc(
        settled, candidates, identity
    ) == settled


def test_process_wakeup_replay_strip_rejects_any_turn_identity_disagreement():
    stream_id = "stream-6749-turn-identities"
    previous_session = _prepare_persisted_wakeup_final(
        "issue6749-turn-identities-previous",
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    previous = copy.deepcopy(previous_session.messages)
    candidates = _closed_wakeup_session(
        "issue6749-turn-identities-candidates",
        stream_id,
    ).messages
    candidate_final = next(
        row for row in candidates
        if row.get("role") == "assistant"
        and row.get("content") == "Final wakeup answer"
    )
    candidate_final.pop("turn_id")
    identity = {
        "source": "process_wakeup",
        "stream_id": stream_id,
        "token": streaming.build_active_turn_token(stream_id, 1234567890.0),
        "turn_id": "current-turn",
    }
    assert streaming._strip_replayed_process_wakeup_arc(
        previous, candidates, identity
    ) == previous


def test_process_wakeup_replay_strip_requires_turn_and_run_identity_agreement():
    """A missing old turn plus conflicting run ids must keep the old arc."""
    stream_id = "stream-6749-turn-run-conflict"
    previous_session = _prepare_persisted_wakeup_final(
        "issue6749-turn-run-conflict-previous",
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    previous = copy.deepcopy(previous_session.messages)
    candidates = _closed_wakeup_session(
        "issue6749-turn-run-conflict-candidates",
        stream_id,
    ).messages
    old_final = next(
        row for row in previous
        if row.get("role") == "assistant"
        and row.get("content") == "Final wakeup answer"
    )
    candidate_final = next(
        row for row in candidates
        if row.get("role") == "assistant"
        and row.get("content") == "Final wakeup answer"
    )
    old_final.pop("turn_id")
    old_final["_run_id"] = "run-old"
    candidate_final["turn_id"] = "turn-6749-wakeup"
    candidate_final["_run_id"] = "run-new"
    identity = {
        "source": "process_wakeup",
        "stream_id": stream_id,
        "token": streaming.build_active_turn_token(stream_id, 1234567890.0),
        "turn_id": "turn-6749-wakeup",
        "run_id": f"run:{stream_id}",
    }
    assert streaming._strip_replayed_process_wakeup_arc(
        previous, candidates, identity
    ) == previous


def test_process_wakeup_replay_strip_transfers_reasoning_metadata_before_delete():
    """Matched old reasoning must have one exact canonical destination."""
    stream_id = "stream-6749-reasoning-transfer"
    previous_session = _prepare_persisted_wakeup_final(
        "issue6749-reasoning-transfer-previous",
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    previous = copy.deepcopy(previous_session.messages)
    candidates = _closed_wakeup_session(
        "issue6749-reasoning-transfer-candidates",
        stream_id,
    ).messages
    old_final = next(
        row for row in previous
        if row.get("role") == "assistant"
        and row.get("content") == "Final wakeup answer"
    )
    candidate_final = next(
        row for row in candidates
        if row.get("role") == "assistant"
        and row.get("content") == "Final wakeup answer"
    )
    old_final["reasoning_content"] = "reasoning-content-6749"
    old_final["reasoning"] = "reasoning-display-6749"
    candidate_final.pop("reasoning_content", None)
    candidate_final.pop("reasoning", None)
    identity = {
        "source": "process_wakeup",
        "stream_id": stream_id,
        "token": streaming.build_active_turn_token(stream_id, 1234567890.0),
        "turn_id": "turn-6749-wakeup",
        "run_id": f"run:{stream_id}",
    }
    settled = streaming._strip_replayed_process_wakeup_arc(
        previous, candidates, identity
    )
    assert settled != previous
    assert candidate_final["reasoning_content"] == "reasoning-content-6749"
    assert candidate_final["reasoning"] == "reasoning-display-6749"


def test_process_wakeup_replay_strip_distinguishes_missing_content_from_none():
    """Presence-aware content comparison must not equate missing with None."""
    stream_id = "stream-6749-content-presence"
    previous_session = _prepare_persisted_wakeup_final(
        "issue6749-content-presence-previous",
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    previous = copy.deepcopy(previous_session.messages)
    candidates = _closed_wakeup_session(
        "issue6749-content-presence-candidates",
        stream_id,
    ).messages
    old_tool = next(
        row for row in previous
        if row.get("role") == "tool" and row.get("tool_call_id") == "call-6749-1"
    )
    candidate_tool = next(
        row for row in candidates
        if row.get("role") == "tool" and row.get("tool_call_id") == "call-6749-1"
    )
    old_tool.pop("content")
    candidate_tool["content"] = None
    identity = {
        "source": "process_wakeup",
        "stream_id": stream_id,
        "token": streaming.build_active_turn_token(stream_id, 1234567890.0),
        "turn_id": "turn-6749-wakeup",
        "run_id": f"run:{stream_id}",
    }
    assert streaming._strip_replayed_process_wakeup_arc(
        previous, candidates, identity
    ) == previous


def test_process_wakeup_replay_key_rejects_cyclic_content():
    """Cyclic content must fail closed without reaching session serialization."""
    cyclic_content = []
    cyclic_content.append(cyclic_content)
    assert streaming._process_wakeup_replay_key(
        {"role": "tool", "content": cyclic_content}
    ) is None


@pytest.mark.parametrize(
    ("old_content", "candidate_content", "should_strip"),
    [
        (
            [
                {"type": "text", "text": "tool result"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ],
            [
                {"text": "tool result", "type": "text"},
                {"image_url": {"url": "data:image/png;base64,AA=="}, "type": "image_url"},
            ],
            True,
        ),
        (
            [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}],
            [{"type": "text", "text": "second"}, {"type": "text", "text": "first"}],
            False,
        ),
        ({"text": "tool result", "part": {"a": 1, "b": 2}}, {"part": {"b": 2, "a": 1}, "text": "tool result"}, True),
        (None, "", False),
        (float("nan"), float("nan"), False),
        (object(), object(), False),
    ],
)
def test_process_wakeup_replay_strip_content_comparison_is_canonical_and_fail_closed(
    old_content,
    candidate_content,
    should_strip,
):
    stream_id = "stream-6749-content-canonical"
    previous_session = _prepare_persisted_wakeup_final(
        f"issue6749-content-canonical-{should_strip}",
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    previous = copy.deepcopy(previous_session.messages)
    candidates = _closed_wakeup_session(
        f"issue6749-content-canonical-candidate-{should_strip}",
        stream_id,
    ).messages
    old_tool = next(
        row for row in previous
        if row.get("role") == "tool" and row.get("tool_call_id") == "call-6749-1"
    )
    candidate_tool = next(
        row for row in candidates
        if row.get("role") == "tool" and row.get("tool_call_id") == "call-6749-1"
    )
    old_tool["content"] = old_content
    candidate_tool["content"] = candidate_content
    identity = {
        "source": "process_wakeup",
        "stream_id": stream_id,
        "token": streaming.build_active_turn_token(stream_id, 1234567890.0),
        "turn_id": "turn-6749-wakeup",
        "run_id": f"run:{stream_id}",
    }
    settled = streaming._strip_replayed_process_wakeup_arc(
        previous, candidates, identity
    )
    if should_strip:
        assert settled != previous
        assert not any(
            row.get("tool_call_id") == "call-6749-1"
            for row in settled
            if isinstance(row, dict)
        )
    else:
        assert settled == previous


@pytest.mark.parametrize("missing_identity", ["active", "candidate", "old", "all"])
def test_process_wakeup_replay_strip_requires_run_id_on_all_three_owners(missing_identity):
    stream_id = "stream-6749-run-required"
    previous_session = _prepare_persisted_wakeup_final(
        f"issue6749-run-required-{missing_identity}",
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    previous = copy.deepcopy(previous_session.messages)
    candidates = _closed_wakeup_session(
        f"issue6749-run-required-candidate-{missing_identity}",
        stream_id,
    ).messages
    old_final = next(
        row for row in previous
        if row.get("role") == "assistant" and row.get("content") == "Final wakeup answer"
    )
    candidate_final = next(
        row for row in candidates
        if row.get("role") == "assistant" and row.get("content") == "Final wakeup answer"
    )
    identity = {
        "source": "process_wakeup",
        "stream_id": stream_id,
        "token": streaming.build_active_turn_token(stream_id, 1234567890.0),
        "turn_id": "turn-6749-wakeup",
        "run_id": f"run:{stream_id}",
    }
    if missing_identity in {"old", "all"}:
        old_final.pop("_run_id", None)
    if missing_identity in {"candidate", "all"}:
        candidate_final.pop("_run_id", None)
    if missing_identity in {"active", "all"}:
        identity.pop("run_id", None)
    assert streaming._strip_replayed_process_wakeup_arc(
        previous, candidates, identity
    ) == previous


def test_process_wakeup_replay_strip_transfers_reasoning_transactionally():
    """A later checkpoint failure must not leave candidate metadata mutated."""
    stream_id = "stream-6749-reasoning-transaction"
    previous_session = _prepare_persisted_wakeup_final(
        "issue6749-reasoning-transaction-previous",
        stream_id,
        "[IMPORTANT: Background process completed]",
    )
    previous = copy.deepcopy(previous_session.messages)
    candidates = _closed_wakeup_session(
        "issue6749-reasoning-transaction-candidates",
        stream_id,
    ).messages
    active_user = {
        "role": "user",
        "content": "[IMPORTANT: Background process completed]",
        "_source": "process_wakeup",
        "_active_turn_token": streaming.build_active_turn_token(
            stream_id,
            1234567890.0,
        ),
    }
    previous.insert(0, active_user)
    old_final = next(
        row for row in previous
        if row.get("role") == "assistant" and row.get("content") == "Final wakeup answer"
    )
    candidate_final = next(
        row for row in candidates
        if row.get("role") == "assistant" and row.get("content") == "Final wakeup answer"
    )
    old_final["reasoning_content"] = "reasoning-content-transaction"
    old_final["reasoning"] = "reasoning-display-transaction"
    candidate_final.pop("reasoning_content", None)
    candidate_final.pop("reasoning", None)
    before_candidates = copy.deepcopy(candidates)
    identity = {
        "source": "process_wakeup",
        "stream_id": stream_id,
        "token": streaming.build_active_turn_token(stream_id, 1234567890.0),
        "turn_id": "turn-6749-wakeup",
        "run_id": f"run:{stream_id}",
    }
    assert streaming._strip_replayed_process_wakeup_arc(
        previous, candidates, identity
    ) == previous
    assert candidates == before_candidates
