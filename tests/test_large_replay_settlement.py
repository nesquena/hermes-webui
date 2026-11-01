"""Bounded replay comparison and non-blocking read-side stream maintenance."""

import copy
import random
import threading
from types import SimpleNamespace

import pytest

from api import config, routes, streaming
from api.models import Session


def test_compacted_settlement_preserves_one_non_partial_incomplete_thinking_row(monkeypatch):
    monkeypatch.setattr(streaming, "_annotate_media_snapshots_for_settled_messages", lambda _m: None)
    thinking = {
        "role": "assistant", "content": "", "reasoning": "historical thinking",
        "reasoning_content": "historical thinking", "finish_reason": "incomplete",
        "timestamp": 3., "id": 3, "_row_id": 30, "_db_persisted": True,
        "codex_message_items": [],
    }
    display = [
        {"role": "user", "content": "old", "id": 1},
        {"role": "assistant", "content": "old answer", "id": 2},
        thinking,
        {"role": "user", "content": "recent", "id": 4},
        {"role": "assistant", "content": "recent answer", "id": 5},
    ]
    session = Session(session_id="incomplete-thinking", messages=display,
                      context_messages=copy.deepcopy(display[-2:]))
    for turn in range(4):
        prompt = f"follow up {turn}"
        result = copy.deepcopy(session.context_messages) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": f"reply {turn}"},
        ]
        streaming._settle_result_messages(
            session, list(session.messages), list(session.context_messages),
            result, prompt, "webui", None,
        )
        assert sum(m.get("_row_id") == 30 for m in session.messages) == 1
        assert session.messages[2] == thinking
        assert session.messages[-1]["content"] == f"reply {turn}"


def _reference_overlap(existing, incoming):
    for size in range(min(len(existing), len(incoming)), 0, -1):
        if [streaming._message_replay_key(m) for m in existing[-size:]] == [
            streaming._message_replay_key(m) for m in incoming[:size]
        ]:
            return incoming[size:]
    return incoming


def test_overlap_preserves_existing_semantics_for_repeated_and_structured_rows():
    rows = [
        None,
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "answer"},
        {"role": "assistant", "content": "answer", "api_content": "wire A"},
        {"role": "assistant", "content": "answer", "api_content": "wire B"},
        {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "content": "ok", "tool_call_id": "call-1"},
    ]
    rng = random.Random(7)
    for _ in range(300):
        existing = [copy.deepcopy(rng.choice(rows)) for _ in range(rng.randrange(12))]
        incoming = [copy.deepcopy(rng.choice(rows)) for _ in range(rng.randrange(12))]
        if existing and rng.random() < .5:
            incoming = copy.deepcopy(existing[-rng.randint(1, len(existing)):]) + incoming
        before = copy.deepcopy((existing, incoming))
        result = streaming._strip_replayed_prefix(existing, incoming)
        assert result == _reference_overlap(existing, incoming)
        assert (existing, incoming) == before


def test_large_nonmatching_replay_does_not_repeat_payload_serialization(monkeypatch):
    count = 0
    original = streaming._message_replay_key
    size = 1024

    def counted(message):
        nonlocal count
        count += 1
        assert count <= 2 * size, "quadratic replay-key construction"
        return original(message)

    monkeypatch.setattr(streaming, "_message_replay_key", counted)
    existing = [{"role": "assistant", "content": "old"}] * size
    incoming = [{"role": "assistant", "content": "new"}] * size
    assert streaming._strip_replayed_prefix(existing, incoming) == incoming


@pytest.mark.parametrize("metadata_only", [False, True])
def test_stale_cleanup_returns_without_waiting_for_settlement(monkeypatch, metadata_only):
    session_lock = threading.Lock()
    monkeypatch.setattr(routes, "STREAMS", {})
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: session_lock)
    saved = []
    session = SimpleNamespace(
        session_id="busy-settlement", active_stream_id="stale-stream",
        pending_user_message=None, pending_started_at=None,
        _loaded_metadata_only=metadata_only, messages=[],
        save=lambda **kw: saved.append(kw),
    )
    # An occupied session must not upgrade a metadata stub just for maintenance.
    from api import models
    upgrades = []

    def get_full(*_args, **_kwargs):
        upgrades.append(True)
        return session

    monkeypatch.setattr(models, "get_session", get_full)
    result = []
    session_lock.acquire()
    worker = threading.Thread(target=lambda: result.append(routes._clear_stale_stream_state(session)))
    try:
        worker.start()
        worker.join(.5)
        returned_while_occupied = not worker.is_alive()
    finally:
        session_lock.release()
        worker.join(2)
    assert returned_while_occupied, "session reads waited on the settlement lock"
    assert result == [False]
    assert not saved and not upgrades
    assert session.active_stream_id == "stale-stream"


def test_stale_cleanup_releases_lock_after_save_failure(monkeypatch):
    lock = threading.Lock()
    monkeypatch.setattr(routes, "STREAMS", {})
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: lock)

    def fail_save(**_kwargs):
        raise OSError("synthetic write failure")

    session = SimpleNamespace(
        session_id="save-failure", active_stream_id="stale", messages=[],
        pending_user_message=None, pending_started_at=None, save=fail_save,
    )
    routes._clear_stale_stream_state(session)
    assert lock.acquire(blocking=False)
    lock.release()


def test_chat_start_cleanup_waits_for_a_transient_writer(monkeypatch):
    """chat_start is itself a writer: a dead stream id must be cleared after a brief
    holder releases the lock, not answered with 409 (maintainer fix on #8072)."""
    session_lock = threading.Lock()
    monkeypatch.setattr(routes, "STREAMS", {})
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: session_lock)
    saved = []
    session = SimpleNamespace(
        session_id="chat-start-dead-stream", active_stream_id="dead-stream",
        pending_user_message=None, pending_started_at=None,
        _loaded_metadata_only=False, messages=[],
        save=lambda **kw: saved.append(kw),
    )
    result = []
    session_lock.acquire()
    worker = threading.Thread(
        target=lambda: result.append(routes._clear_stale_stream_state(session, wait_for_writer=True)))
    try:
        worker.start()
        worker.join(.3)
        waited = worker.is_alive()
    finally:
        session_lock.release()
        worker.join(5)
    assert waited, "chat_start cleanup returned while a writer held the lock"
    assert result == [True]
    assert session.active_stream_id is None
    assert saved
    assert not session_lock.locked()
