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

CLI-parity semantics — post-turn marking, boundary extraction/commit:

- Completed turn: Hermes core still mirrors the exchange through
  run_agent.py::_sync_external_memory_for_turn(), MemoryManager sync_all(), and
  provider sync_turn() WITHOUT triggering extraction.  WebUI then calls
  mark_turn_completed() after the saved/completed-turn boundary so later drains
  know the synced session has uncommitted work and which agent owns it.

- Session boundary: commit_session_memory() triggers
  AIAgent.commit_memory_session(), which calls provider on_session_end(),
  posting /api/v1/sessions/<sid>/commit and triggering extraction. This is
  called only at boundaries — /api/session/new with prev_session_id, explicit
  agent eviction, LRU cache eviction, and shutdown drain — matching the CLI's
  AIAgent.commit_memory_session()/shutdown_memory_provider() boundary.

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

# Background commit threads spawned by fire-and-forget paths (e.g. POST /api/session/new).
# Tracked so drain_all_on_shutdown() can join them before interpreter teardown,
# preventing silent data loss when daemon threads are abandoned mid-commit.
_background_commit_threads: set[threading.Thread] = set()
_background_commit_threads_lock = threading.Lock()
# Set once drain_all_on_shutdown() begins so no NEW background worker is started
# after the drain has snapshotted the registry. A late arrival is not lost: its
# uncommitted generation is still committed inline by the generation-drain loop
# below (which re-snapshots _sessions each pass and commits with wait=True).
_draining = False


def _register_background_commit_thread(t: threading.Thread) -> bool:
    """Register a fire-and-forget commit thread. Returns True if the caller
    should start it. Returns False when shutdown draining has already begun —
    the caller must NOT start a new worker in that window; the inline
    generation-drain will commit the pending work instead."""
    with _background_commit_threads_lock:
        if _draining:
            return False
        _background_commit_threads.add(t)
        return True


def _unregister_background_commit_thread(t: threading.Thread) -> None:
    # A completed worker removes itself so the registry does not grow without
    # bound over the process lifetime; drain only needs threads still running.
    with _background_commit_threads_lock:
        _background_commit_threads.discard(t)


def _drain_background_commit_threads(timeout: float = 5.0, deadline_fn=None) -> None:
    """Join tracked fire-and-forget commit threads. When ``deadline_fn`` is given
    (returns the remaining overall shutdown budget in seconds), each join is
    clamped to that budget and the phase stops early once it is exhausted, so the
    join phase cannot overrun ``drain_all_on_shutdown``'s deadline."""
    with _background_commit_threads_lock:
        threads = list(_background_commit_threads)
        _background_commit_threads.clear()
    for t in threads:
        if not t.is_alive():
            continue
        if deadline_fn is None:
            t.join(timeout)
            continue
        remaining = deadline_fn()
        if remaining <= 0:
            return
        t.join(min(timeout, remaining))


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


def discard_session(session_id: str) -> bool:
    """Permanently drop a session's lifecycle entry to bound memory growth.

    The ``_sessions`` dict is process-global and historically only ever grew:
    ``register_agent`` / ``mark_turn_completed`` insert keys but no runtime path
    ever removed them, so every unique ``session_id`` the WebUI touched leaked a
    permanent entry (issue #3506). Over days of use on a large install this is a
    monotonic, unbounded climb.

    This removes the entry, but only when it is provably safe to do so: no commit
    is in flight and there is no uncommitted memory work that still needs the
    retained agent handle. If the entry is busy or dirty it is left untouched so
    failed batch-extraction memory work stays retryable -- exactly the invariant
    ``unregister_agent`` and ``_evict_session_agent`` already preserve.

    Returns True when the entry was removed (or was already absent), False when
    it was retained because work is still pending.
    """
    if not session_id:
        return False
    with _condition:
        entry = _sessions.get(session_id)
        if entry is None:
            return True
        if entry["in_flight"]:
            return False
        if entry["generation"] > entry["committed_generation"]:
            return False
        del _sessions[session_id]
        _condition.notify_all()
        return True


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


def drain_all_on_shutdown(deadline: float = 30.0) -> None:
    # Signal that draining has begun so no NEW background commit worker is
    # started after this point (see _register_background_commit_thread). Any
    # already-registered worker is joined below; a caller that loses the race
    # (registered after our snapshot, or refused a start) still has its pending
    # generation committed by the inline loop.
    global _draining
    with _background_commit_threads_lock:
        _draining = True

    # Overall wall-clock budget so a stuck worker holding in_flight cannot hang
    # the shutdown path forever. Start the clock BEFORE the join phase so the
    # join and the inline drain share one budget. The inline commit runs in a
    # daemon worker joined only for the remaining budget — otherwise a provider
    # wedged inside commit_memory_session() (whether we are waiting on an
    # existing in_flight commit, OR the inline drain owns the segment and calls
    # the provider directly) would block indefinitely and the deadline check
    # would never run again.
    started = time.monotonic()

    def _remaining() -> float:
        return deadline - (time.monotonic() - started)

    def _commit_within_budget(session_id: str, budget: float) -> bool:
        """Run one commit in a daemon worker, join at most `budget` seconds.
        Returns True only if the commit completed within budget AND made
        progress. On timeout the daemon worker is left running (it will finish
        or die at interpreter exit; either way it mutates generation state only
        under _condition, so nothing is corrupted) and we report no progress so
        the drain can honor its deadline instead of hanging on a wedged
        provider."""
        holder = {"ok": False}

        def _run():
            holder["ok"] = commit_session_memory(session_id, wait=True)

        worker = threading.Thread(target=_run, daemon=True, name=f"drain-commit-{session_id}")
        worker.start()
        worker.join(budget)
        return holder["ok"] if not worker.is_alive() else False

    # Drain in-flight background commit threads first (fire-and-forget from
    # POST /api/session/new) so their commit work completes before we drain any
    # remaining uncommitted generations inline. Each join consumes the shared
    # budget so it cannot overrun the overall deadline.
    _drain_background_commit_threads(deadline_fn=_remaining)

    while True:
        with _lock:
            snapshot = [sid for sid, entry in _sessions.items() if entry["generation"] > entry["committed_generation"]]
        if not snapshot:
            return
        remaining = _remaining()
        if remaining <= 0:
            logger.warning("drain_all_on_shutdown: deadline hit with uncommitted sessions: %s", sorted(snapshot))
            return

        made_progress = False
        for sid in snapshot:
            budget = _remaining()
            if budget <= 0:
                logger.warning("drain_all_on_shutdown: deadline hit with uncommitted sessions: %s", sorted(snapshot))
                return
            # Budget-bounded (covers both the wait-for-in_flight path AND the
            # unbounded provider call when the inline drain owns the segment).
            if _commit_within_budget(sid, budget):
                made_progress = True
        if not made_progress:
            logger.debug("drain_all_on_shutdown: stopped with uncommitted sessions: %s", sorted(snapshot))
            return
