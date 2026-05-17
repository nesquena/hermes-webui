"""
Hermes WebUI memory-provider session lifecycle.

Batch-extraction memory providers (OpenViking, Holographic) only extract memories
when AIAgent.commit_memory_session() invokes provider on_session_end(). WebUI
sessions can be reopened and continued many times, so the lifecycle must guarantee:

1. Only completed, non-ephemeral turns are committable.
2. A commit finishing late must not erase work completed while it was in flight.
3. A failed commit preserves the uncommitted generation and owning agent handle.
4. Replacement/reopened agents cannot steal older dirty generations.
5. Overlapping commits are serialised via a per-session in-flight guard.

The design uses a monotonic generation counter per session plus per-generation
agent ownership segments. mark_turn_completed() records which agent owns the new
generation. commit_session_memory() commits the earliest uncommitted segment and
compare-and-clears only that captured segment after success.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_condition = threading.Condition(_lock)

_sessions: dict[str, dict] = {}


def _new_entry() -> dict:
    return {
        "generation": 0,
        "committed_generation": 0,
        "agent": None,
        "in_flight": False,
        "segments": [],
    }


def _reset_for_tests() -> None:
    with _condition:
        _sessions.clear()
        _condition.notify_all()


def register_agent(session_id: str, agent) -> None:
    """Register the current agent handle for future completed generations.

    Existing dirty generations keep their original segment owner. This prevents
    a rebuilt/reopened agent from overwriting the handle needed to retry older
    failed memory-provider work.
    """
    if not session_id:
        return
    with _condition:
        entry = _sessions.setdefault(session_id, _new_entry())
        entry["agent"] = agent
        _condition.notify_all()


def unregister_agent(session_id: str) -> None:
    """Clear the current future-generation agent handle.

    Dirty segment owners are intentionally preserved so failed work remains
    retryable even if the cache drops the current agent reference.
    """
    if not session_id:
        return
    with _condition:
        entry = _sessions.get(session_id)
        if entry is not None:
            entry["agent"] = None
        _condition.notify_all()


def mark_turn_completed(session_id: str, *, agent=None) -> int:
    if not session_id:
        return 0
    with _condition:
        entry = _sessions.setdefault(session_id, _new_entry())
        if agent is not None:
            entry["agent"] = agent
        owner = agent if agent is not None else entry.get("agent")
        entry["generation"] += 1
        generation = entry["generation"]
        segments = entry["segments"]
        if segments and not entry["in_flight"] and segments[-1].get("agent") is owner:
            segments[-1]["end"] = generation
        else:
            segments.append({"start": generation, "end": generation, "agent": owner})
        _condition.notify_all()
        return generation


def has_uncommitted_work(session_id: str) -> bool:
    if not session_id:
        return False
    with _lock:
        entry = _sessions.get(session_id)
        if entry is None:
            return False
        return entry["generation"] > entry["committed_generation"]


def _first_uncommitted_segment(entry: dict) -> dict | None:
    committed = entry["committed_generation"]
    for segment in entry["segments"]:
        if segment["end"] > committed:
            return segment
    return None


def commit_session_memory(session_id: str, agent=None, *, wait: bool = False, timeout: float | None = None) -> bool:
    if not session_id:
        return False
    deadline = time.monotonic() + timeout if timeout is not None else None
    with _condition:
        entry = _sessions.get(session_id)
        if entry is None:
            return False
        while entry["in_flight"]:
            if not wait:
                return False
            if deadline is None:
                _condition.wait()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                _condition.wait(remaining)
            entry = _sessions.get(session_id)
            if entry is None:
                return False
        if entry["generation"] <= entry["committed_generation"]:
            return False
        segment = _first_uncommitted_segment(entry)
        if segment is None:
            return False
        effective_agent = segment.get("agent")
        if effective_agent is None:
            effective_agent = agent if agent is not None else entry.get("agent")
            if effective_agent is not None:
                segment["agent"] = effective_agent
        if effective_agent is None:
            return False
        captured_generation = segment["end"]
        entry["in_flight"] = True

    try:
        effective_agent.commit_memory_session()
    except Exception:
        logger.exception("commit_memory_session() failed for session %s", session_id)
        with _condition:
            re_entry = _sessions.get(session_id)
            if re_entry is not None:
                re_entry["in_flight"] = False
            _condition.notify_all()
        return False

    with _condition:
        re_entry = _sessions.get(session_id)
        if re_entry is not None:
            re_entry["in_flight"] = False
            if captured_generation > re_entry["committed_generation"]:
                re_entry["committed_generation"] = captured_generation
            committed = re_entry["committed_generation"]
            segments = re_entry["segments"]
            while segments and segments[0]["end"] <= committed:
                segments.pop(0)
            if segments and segments[0]["start"] <= committed:
                segments[0]["start"] = committed + 1
        _condition.notify_all()
    return True


def drain_all_on_shutdown() -> None:
    while True:
        with _lock:
            snapshot = [sid for sid, entry in _sessions.items() if entry["generation"] > entry["committed_generation"]]
        if not snapshot:
            return

        made_progress = False
        for sid in snapshot:
            if commit_session_memory(sid, wait=True):
                made_progress = True
        if not made_progress:
            logger.debug("drain_all_on_shutdown: stopped with uncommitted sessions: %s", sorted(snapshot))
            return
