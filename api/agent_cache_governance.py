"""Agent-cache governance for the WebUI's per-session AIAgent cache.

The WebUI keeps one ``AIAgent`` per session in ``SESSION_AGENT_CACHE`` (an
OrderedDict LRU) so cross-turn state (``_user_turn_count``, memory-provider
state) survives between messages.  Each cached agent pins the session's full
live transcript (``_session_messages``) in RAM — tens of MB on a tool-heavy
session — so the LRU *count* cap alone does not bound the process's resident
memory: warm sessions churn, transcripts keep growing, and RSS climbs until an
operator restarts the service.

The gateway solved exactly this problem with three valves
(``agent.agent_cache`` in config_defaults.py, issue #80764): an LRU cap, an
idle TTL, and a memory-pressure pass that soft-evicts LRU transcripts once
anonymous RSS crosses a budget.  This module ports the latter two to the WebUI:

* ``AgentCacheGovernor.sweep_idle`` — fully evicts cached agents idle past the
  TTL (commit, then release clients / close the session DB).
* ``AgentCacheGovernor.sweep_pressure`` — sheds the least-recently-used
  transcripts: the entry leaves the cache, the agent handle and its tool state
  survive, ``_session_messages`` is dropped, and the next turn rebuilds the
  transcript from the persisted session.

Both are driven by one daemon thread started from ``server.py``.  Everything
here is pure/read-only so it can be unit-tested without a running server.

DESIGN NOTE — the pressure valve IS the gateway's, not a new one
---------------------------------------------------------------
These four points are the whole contract; anything not written here was
deliberately left out.

1. The ONLY losslessness condition is the persistence gate,
   ``transcript_persistence_caught_up(agent)``
   (gateway/agent_cache_pressure.py:198): a transcript may be shed only when
   ``_last_flushed_db_idx >= len(_session_messages)`` — the session DB already
   holds every message the in-RAM list does, and the flush index only advances
   on a fully successful write.  Unknown shapes count as NOT caught up: a
   skipped eviction costs memory, a wrong one costs the conversation.
2. The commit before a shed is BEST EFFORT
   (gateway/run_agent_cache.py:890 ``_commit_memory_before_soft_evict``): the
   captured transcript is delivered to ``AIAgent.commit_memory_session()``
   inside the session owner's profile scope, the hook's return value is
   ignored, and a raising hook is logged and dropped.  There is NO
   acknowledgement, NO per-session release debt and NO retry loop.  The
   installed Agent returns None and runs provider I/O through a swallow-all
   helper, so no caller can observe whether extraction happened; gating a
   release on that confirmation would mean never releasing.  What keeps the
   conversation safe is (1), not the commit.
3. The release is SOFT (gateway/run_agent_cache.py:911
   ``_release_evicted_agent_soft``): the plan is popped out of the LRU cache
   under the cache lock, the agent handle is kept (no session-DB teardown),
   ``_session_messages`` and ``_db_flush_scan_prefix`` are dropped, and the
   next turn rebuilds the transcript from the persisted session.
4. One pass sheds at most ``_MAX_EVICTIONS_PER_PASS`` transcripts, LRU first,
   and hands the batch to the one pressure-release daemon thread
   (``PressureReleaseWorker``), so a provider commit can never hold the cache
   lock or stall the pass.  ``protect_recent`` is clamped to half the cache.
   When nothing is shedable above budget the pass logs the gateway's warning
   once, with the cached / mid-turn / un-flushed counts — the only way an
   operator learns the valve is stuck rather than idle.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_BYTES_PER_MB = 1024 * 1024
# Fraction of the resolved memory limit at which we start shedding transcripts.
# Deliberately well under the limit so eviction happens while the process still
# has room to breathe (parity with gateway.agent_cache_pressure).
_AUTO_BUDGET_FRACTION = 0.65
# Below this a "budget" is noise — tiny boxes would evict on every pass.
# NOTE (intentional WebUI divergence from gateway.agent_cache_pressure.py):
# the gateway DISABLES the pressure pass (returns None) when the computed
# auto budget falls below this floor, whereas WebUI RAISES it to the floor.
# Preserve the existing WebUI contract here (including its regression test)
# and document the difference explicitly. Operators who want the gateway's
# disabled behavior can set HERMES_WEBUI_AGENT_CACHE_MEMORY_HIGH_MB=0/off.
_AUTO_BUDGET_FLOOR_MB = 512
# Upper bound on soft-evictions per pass, so one pressure burst cannot stall
# the server tearing down a dozen agents (parity with the gateway).
_MAX_EVICTIONS_PER_PASS = 16


def _positive_int(value: Any, default: int) -> int:
    """Parse an int >= 0; fall back on anything malformed (0 = disabled)."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _cgroup_memory_limit_bytes() -> Optional[int]:
    """Return the memory limit this process runs under, if cgroup-capped.

    Mirrors gateway/agent_cache_pressure.py: prefers cgroup v2 ``memory.max``
    (and ``memory.high``) on the process's own cgroup, then the root files.
    ``max`` / near-2^63 sentinels mean "unlimited" → None.
    """
    if not sys_platform_linux():
        return None
    candidates: List[str] = []
    try:
        own = _own_cgroup_path()
        if own and own != "/":
            candidates.extend(
                (
                    f"/sys/fs/cgroup{own}/memory.high",
                    f"/sys/fs/cgroup{own}/memory.max",
                    # cgroup v1: the process's own group lives under the v1
                    # memory-controller mount, not at the unified root.  Without
                    # this a server in a capped v1 child with an unlimited root
                    # reads "unlimited" and the pressure pass never fires.
                    f"/sys/fs/cgroup/memory{own}/memory.limit_in_bytes",
                )
            )
    except Exception:
        pass
    candidates.extend(
        (
            "/sys/fs/cgroup/memory.high",
            "/sys/fs/cgroup/memory.max",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        )
    )
    for candidate in candidates:
        try:
            with open(candidate, "r", encoding="utf-8") as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        if not raw or raw == "max":
            continue
        try:
            limit = int(raw)
        except ValueError:
            continue
        if limit <= 0 or limit >= (1 << 62):
            continue
        return limit
    return None


def sys_platform_linux() -> bool:
    import sys

    return sys.platform == "linux"


def _own_cgroup_path() -> Optional[str]:
    """Read the process's own cgroup path (v2: single line; v1: pick memory).

    The memory-controller line wins when present: on a v1/hybrid host the first
    line belongs to an unrelated controller (commonly ``cpuset`` at ``/``), and
    returning that would point the limit lookup at the unlimited root instead of
    the capped group this process actually runs in.  Only a unified (v2) line —
    empty controller field — is used as the fallback.
    """
    try:
        with open("/proc/self/cgroup", "r", encoding="utf-8") as fh:
            unified = None
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(":", 2)
                if len(parts) != 3:
                    continue
                if parts[1] == "memory":
                    return parts[2]
                if parts[1] == "" and parts[2].startswith("/"):
                    unified = parts[2]  # v2 single hierarchy: no controller field
            return unified
    except OSError:
        return None


def read_anon_rss_mb() -> Optional[int]:
    """Return the process's anonymous resident memory in MB, or None.

    Anonymous pages are the ones cached transcripts live in.  Uses
    /proc/self/status RssAnon; falls back to psutil total RSS if unavailable.
    """
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("RssAnon:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) // 1024
    except (OSError, ValueError):
        pass
    try:
        import psutil  # type: ignore

        return int(psutil.Process(os.getpid()).memory_info().rss / _BYTES_PER_MB)
    except Exception:
        return None


def resolve_memory_high_mb(raw: Any, total_ram_mb: Optional[int] = None) -> Optional[int]:
    """Resolve the memory-pressure budget in MB.

    ``raw`` accepts:
      * "auto" — cgroup memory limit × _AUTO_BUDGET_FRACTION (floored at
        _AUTO_BUDGET_FLOOR_MB), falling back to total RAM × fraction.
      * a number — the budget in MB.
      * 0 / "0" / "" / "off" — pressure pass disabled (None).

    ``total_ram_mb`` is injectable for tests; None → read from the system.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if raw == "" or raw.lower() in ("0", "off", "none", "false"):
            return None
        if raw.lower() == "auto":
            limit = _cgroup_memory_limit_bytes()
            if limit is not None:
                budget = int(limit * _AUTO_BUDGET_FRACTION / _BYTES_PER_MB)
                return budget if budget >= _AUTO_BUDGET_FLOOR_MB else _AUTO_BUDGET_FLOOR_MB
            if total_ram_mb is None:
                total_ram_mb = _total_ram_mb()
            if total_ram_mb is None:
                return None
            budget = int(total_ram_mb * _AUTO_BUDGET_FRACTION)
            return budget if budget >= _AUTO_BUDGET_FLOOR_MB else _AUTO_BUDGET_FLOOR_MB
        try:
            value = int(raw)
        except ValueError:
            return None
        if value <= 0:
            return None
        return value
    if isinstance(raw, (int, float)):
        value = int(raw)
        return value if value > 0 else None
    return None


def _total_ram_mb() -> Optional[int]:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) // 1024
    except (OSError, ValueError):
        pass
    try:
        import os as _os

        pages = _os.sysconf("SC_PHYS_PAGES")
        page_size = _os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size / _BYTES_PER_MB)
    except Exception:
        return None


def _agent_last_activity(agent: Any) -> Optional[float]:
    """Return the agent's last-activity timestamp (mirrors gateway semantics).

    Falls back to the attribute the AIAgent maintains on every turn.
    """
    if agent is None:
        return None
    ts = getattr(agent, "_last_activity_ts", None)
    if isinstance(ts, (int, float)):
        return float(ts)
    return None


def plan_pressure_evictions(
    ordered: List[Tuple[str, Any]],
    is_evictable,
    protect_recent: int = 8,
    max_evictions: int = _MAX_EVICTIONS_PER_PASS,
) -> List[Tuple[str, Any]]:
    """Pick LRU sessions to soft-evict under memory pressure.

    ``ordered`` is the cache's LRU order (oldest first).  ``is_evictable(key,
    agent)`` decides whether a session may be shed (e.g. not mid-turn, live
    transcript already on disk).  ``protect_recent`` is an upper bound,
    clamped to half the cache: a handful of sessions can be big enough to
    exhaust the budget on their own (a single tool-heavy transcript runs to
    hundreds of MB), and a fixed guard would then protect the entire cache
    and leave the process climbing toward the OOM killer with nothing it is
    willing to shed (parity with gateway/agent_cache_pressure.py).  The clamp
    also guarantees the slice endpoint is never negative, so a cache smaller
    than ``protect_recent`` can no longer invert the LRU protection window.
    """
    plan: List[Tuple[str, Any]] = []
    if not ordered:
        return plan
    if max_evictions <= 0:
        return plan
    protect = min(max(protect_recent, 0), len(ordered) // 2)
    if protect:
        ordered = ordered[:-protect]
    for key, agent in ordered:
        if len(plan) >= max_evictions:
            break
        try:
            if is_evictable(key, agent):
                plan.append((key, agent))
        except Exception:
            logger.debug("pressure evictability check failed for %s", key, exc_info=True)
    return plan


def soft_release_transcript(agent: Any) -> None:
    """Drop the live transcript from an evicted agent without tearing it down.

    Mirrors gateway/run_agent_cache.py:911 ``_release_evicted_agent_soft``: the
    agent handle and its tool state (terminal sandbox, browser, background
    processes) are kept — the session may resume — while the conversation is
    given back.  The next turn rebuilds messages from the persisted session (the
    WebUI feeds ``get_state_db_session_messages`` fresh each turn), so dropping
    the in-RAM list is safe and is the single biggest lever on resident memory.

    Only safe once the transcript is fully on disk: clearing the sole complete
    in-memory copy while persistence lags loses history on the next rebuild.
    Callers must check ``transcript_persistence_caught_up(agent)`` first.
    """
    if agent is None:
        return
    try:
        if hasattr(agent, "_session_messages"):
            agent._session_messages = []
    except Exception:
        logger.debug("soft-release transcript failed", exc_info=True)
    # _db_flush_scan_prefix is a shallow copy of the flushed transcript — it
    # shares every message dict, so leaving it pins the multi-MB content strings
    # the eviction exists to free (parity with gateway #80764).
    try:
        if hasattr(agent, "_db_flush_scan_prefix"):
            agent._db_flush_scan_prefix = None
    except Exception:
        logger.debug("soft-release flush prefix failed", exc_info=True)


def transcript_persistence_caught_up(agent: Any) -> bool:
    """True when the agent's live transcript is fully on disk (gateway parity).

    Mirrors ``gateway/agent_cache_pressure.py::transcript_persistence_caught_up``
    (line 198 — the only losslessness condition this module consults):
    ``_last_flushed_db_idx`` is advanced to ``len(messages)`` by
    ``AIAgent._flush_messages_to_session_db`` only on a fully successful write,
    so ``flushed >= len(messages)`` means the in-memory list is exactly what the
    session DB already has.  Unknown shapes are treated as *not* caught up: a
    skipped eviction costs memory, a wrong one costs the user their conversation.
    """
    messages = getattr(agent, "_session_messages", None)
    if not isinstance(messages, list):
        return False
    flushed = getattr(agent, "_last_flushed_db_idx", None)
    if not isinstance(flushed, int) or isinstance(flushed, bool):
        return False
    return flushed >= len(messages)


def _commit_before_soft_release(session_id: str, agent: Any) -> None:
    """Best-effort memory commit before the transcript is dropped.

    Mirrors gateway/run_agent_cache.py:890 ``_commit_memory_before_soft_evict``:
    the return value is ignored and a raising hook is only logged.  The call
    goes through the session lifecycle so (a) it runs inside the session
    owner's profile scope (#8517) and (b) the provider receives the OWNER's
    captured transcript instead of ``[]`` — a batch-extraction provider with an
    empty-input guard would otherwise skip the session's memories entirely.
    """
    try:
        from api.streaming import _lifecycle_commit_session_memory

        _lifecycle_commit_session_memory(session_id, agent=agent, wait=False)
    except Exception:
        logger.debug("pre-release memory commit failed for %s", session_id, exc_info=True)


def _release_pressure_batch(plan: List[Tuple[str, Any]]) -> None:
    """Release one pressure batch sequentially: commit, then soft release.

    The plan is drained (``pop(0)`` + ``del``) rather than iterated so no local
    reference pins the evicted agents until the batch ends
    (gateway/run_agent_cache.py:1036 drains its batch the same way).
    """
    while plan:
        key, agent = plan.pop(0)  # FIFO — LRU-first order preserved
        try:
            _commit_before_soft_release(key, agent)
            soft_release_transcript(agent)
        except Exception:
            logger.debug("pressure soft-release failed for %s", key, exc_info=True)
        del agent


class PressureReleaseWorker:
    """ONE daemon thread that drains pressure batches, strictly in order.

    The gateway starts a fresh daemon thread per batch
    (gateway/run_agent_cache.py:1026).  ``Thread.start()`` blocks until the
    child is scheduled, and under the cache churn this valve exists for that
    costs tens of milliseconds per pass on some platforms (measured ~47ms on
    Windows/CPython with a thread spinning on the cache lock) — enough to turn
    the valve itself into the stall it exists to prevent.  One long-lived
    worker gives the same contract — the commit (provider I/O) never runs on
    the pass or under the cache lock — without paying that per pass.

    ``submit(plan, done)`` is non-blocking; ``done`` (an Event) is set when the
    batch finishes.  Ordering is FIFO, so LRU-first order across batches is
    preserved.
    """

    def __init__(self) -> None:
        self._batches: "queue.SimpleQueue" = queue.SimpleQueue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def submit(self, plan: List[Tuple[str, Any]], done: threading.Event) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    name="agent-cache-pressure",
                    daemon=True,
                )
                self._thread.start()
        self._batches.put((plan, done))

    def _run(self) -> None:
        while True:
            plan, done = self._batches.get()
            try:
                _release_pressure_batch(plan)
            except Exception:
                logger.debug("pressure batch release failed", exc_info=True)
            finally:
                done.set()


class AgentCacheGovernor:
    """One governance pass: idle TTL eviction + memory-pressure soft eviction.

    Holds only a reference to the cache + lock + config snapshot so it can be
    constructed in-process with zero import cycles (streaming.py owns the
    cache; the governor is pure orchestration).
    """

    def __init__(
        self,
        cache: Dict[str, Any],
        lock: threading.Lock,
        *,
        idle_ttl_secs: int = 3600,
        memory_high_mb: Optional[int] = None,
        protect_recent: int = 8,
        active_sids_fn=None,
        close_agent_fn=None,
        eviction_hook=None,
    ) -> None:
        self._cache = cache
        self._lock = lock
        self.idle_ttl_secs = idle_ttl_secs
        self.memory_high_mb = memory_high_mb
        self.protect_recent = protect_recent
        # active_sids_fn() → set[str]: session_ids with a live agent worker.
        # Defaults to snapshotting api.config.ACTIVE_RUNS; injectable for tests.
        self._active_sids_fn = active_sids_fn or self._snapshot_active_sids
        # close_agent_fn(key, agent) → bool: full teardown of an evicted agent
        # (commit memory, close session DB).  Used by the idle-TTL pass.
        self._close_agent_fn = close_agent_fn
        # eviction_hook(key, agent, soft: bool): observability (counters/logs).
        self._eviction_hook = eviction_hook
        # Plan-time liveness snapshot, kept for lease-less entries (legacy
        # agents / injected test agents).  Refreshed at the top of each sweep;
        # None until the first sweep (unknown → fail closed).
        self._last_active_snapshot: Optional[set] = None
        # The single daemon thread that releases pressure batches (provider
        # I/O must never run on the pass or under the cache lock), and the
        # Event for the batch most recently submitted.
        self._release_worker = PressureReleaseWorker()
        self._last_release_done: Optional[threading.Event] = None

    def _entry_turn_active(self, key: str, agent: Any) -> bool:
        """True when the cached entry is mid-turn (per-entry lease).

        The streaming layer maintains ``agent._turn_active`` at turn
        boundaries (api.config.register_active_run / unregister_active_run),
        so reading it here inside the cache lock yields the NEWEST liveness
        state — the plan-time snapshot can be stale by the time the plan is
        built.  Entries without a lease (pre-lease agents, injected test
        agents) fall back to the plan-time snapshot for compatibility.  An
        unknown liveness (no lease AND no snapshot yet) is treated as ACTIVE:
        never evict an agent whose liveness we cannot determine (fail closed).
        An EMPTY snapshot is authoritative — "no one is running" really means
        the pass may evict.
        """
        lease = getattr(agent, "_turn_active", None)
        if lease is not None:
            return bool(lease)
        if self._last_active_snapshot is None:
            return True  # no snapshot yet — fail closed
        return key in self._last_active_snapshot

    # ── Idle TTL ────────────────────────────────────────────────────────────
    def _snapshot_active_sids(self) -> Optional[set]:
        """Snapshot the set of session_ids with a live agent worker.

        Taken BEFORE the cache lock (parity with the LRU eviction path in
        api/streaming.py:10422): the codebase never nests ACTIVE_RUNS_LOCK
        inside SESSION_AGENT_CACHE_LOCK, to avoid a lock-ordering deadlock.
        A cancel/reconnect can drop STREAMS while the worker is still
        unwinding, so ACTIVE_RUNS (worker lifecycle) is the authoritative
        liveness signal — snapshotting it up front is exactly what the
        existing eviction path does.

        Returns None when the liveness registry is unavailable; callers must
        skip the pass entirely (fail CLOSED).  Returning an empty set here
        would mark EVERY agent as inactive and soft-release/evict live
        workers — the exact "can't determine liveness" case that must never
        evict.
        """
        active = set()
        try:
            from api.config import ACTIVE_RUNS, ACTIVE_RUNS_LOCK

            with ACTIVE_RUNS_LOCK:
                for _entry in (ACTIVE_RUNS or {}).values():
                    sid = (_entry or {}).get("session_id")
                    if sid:
                        active.add(sid)
        except Exception:
            # Liveness registry unavailable — fail CLOSED: skip the pass.
            # An empty set would read as "no one is running" and evict
            # mid-turn agents (the empty-set fallback was fail-OPEN).
            return None
        return active

    def sweep_idle(self, now: Optional[float] = None) -> int:
        """Fully evict cached agents idle past the TTL (0 = disabled).

        Returns the number of agents evicted.  Runs the close callback outside
        the cache lock so provider I/O never blocks cache users.
        """
        if not self.idle_ttl_secs:
            return 0
        if now is None:
            now = time.time()
        try:
            active = self._active_sids_fn()
        except Exception:
            active = None
        if active is None:
            return 0  # liveness registry unavailable — skip the pass (fail closed)
        self._last_active_snapshot = active
        to_evict: List[Tuple[str, Any]] = []
        with self._lock:
            for key, entry in list(self._cache.items()):
                agent = entry[0] if isinstance(entry, tuple) and entry else None
                if agent is None:
                    continue
                if self._entry_turn_active(key, agent):
                    continue  # mid-turn — don't tear it down
                last_activity = _agent_last_activity(agent)
                if last_activity is None:
                    continue  # unknown — leave it (parity with gateway)
                if (now - last_activity) > self.idle_ttl_secs:
                    self._cache.pop(key, None)
                    to_evict.append((key, agent))
        for key, agent in to_evict:
            try:
                if self._close_agent_fn is not None:
                    self._close_agent_fn(key, agent)
                if self._eviction_hook is not None:
                    self._eviction_hook(key, agent, soft=False)
                logger.info(
                    "Agent-cache governor: idle evicted session=%s "
                    "(idle_ttl=%ss, cache_size=%d)",
                    key, self.idle_ttl_secs, len(self._cache),
                )
            except Exception:
                logger.debug("idle eviction teardown failed for %s", key, exc_info=True)
        return len(to_evict)

    # ── Memory pressure ─────────────────────────────────────────────────────
    def sweep_pressure(self, rss_mb: Optional[int] = None) -> int:
        """Shed LRU transcripts once anonymous RSS crosses the budget.

        Mirrors gateway/run_agent_cache.py:961
        ``_sweep_agent_cache_under_pressure``: over budget, plan the LRU
        transcripts that are live, not mid-turn, and whose persistence is
        caught up; pop the plan OUT of the cache under ``self._lock``; then
        hand the batch to the single pressure-release daemon thread
        (``PressureReleaseWorker``), because the commit is provider I/O and
        must hold neither the cache lock nor the pass.  Popping under
        the lock is what makes the release race-free without a re-check: a turn
        that starts right after misses the cache and rebuilds its agent, so the
        batch can only touch agents nobody is serving.

        Returns the number of transcripts claimed for release (0 when memory is
        fine, the pass is disabled, or nothing is shedable).
        """
        if not self.memory_high_mb:
            return 0
        if rss_mb is None:
            rss_mb = read_anon_rss_mb()
        if rss_mb is None or rss_mb < self.memory_high_mb:
            return 0

        try:
            active = self._active_sids_fn()
        except Exception:
            active = None
        if active is None:
            return 0  # liveness registry unavailable — skip the pass (fail closed)
        self._last_active_snapshot = active

        def _is_evictable(key: str, agent: Any) -> bool:
            # Gateway parity: _is_live(agent) and transcript_persistence_caught_up.
            if agent is None:
                return False
            if self._entry_turn_active(key, agent):
                return False
            return transcript_persistence_caught_up(agent)

        # Snapshot AND claim under the lock: the live OrderedDict is mutated by
        # server threads on every turn, and iterating it unlocked races an
        # insert or eviction (RuntimeError: dictionary changed size).
        with self._lock:
            ordered = [
                (key, entry[0] if isinstance(entry, tuple) and entry else entry)
                for key, entry in self._cache.items()
            ]
            if not ordered:
                # Nothing cached — whatever is using the heap, it isn't us, and
                # warning about it every tick would point at the wrong subsystem.
                return 0
            plan = plan_pressure_evictions(
                ordered,
                _is_evictable,
                protect_recent=self.protect_recent,
                max_evictions=_MAX_EVICTIONS_PER_PASS,
            )
            for key, agent in plan:
                # Only drop the entry we validated: a turn can re-insert the key
                # between planning and this pop, and that replacement is a live
                # handle nobody planned to shed.
                entry = self._cache.get(key)
                current = entry[0] if isinstance(entry, tuple) and entry else entry
                if current is agent:
                    self._cache.pop(key, None)

        if not plan:
            self._warn_nothing_shedable(ordered, rss_mb)
            return 0

        claimed = len(plan)  # the worker drains `plan` in place
        # One summary line per shedding pass (INFO: the WARNING below is
        # reserved for a valve that cannot shed at all).
        logger.info(
            "Agent cache pressure: anon RSS %sMB over budget %sMB — shedding "
            "%d LRU transcript(s): %s",
            rss_mb, self.memory_high_mb, claimed, ", ".join(key for key, _ in plan),
        )
        if self._eviction_hook is not None:
            for key, agent in plan:
                try:
                    self._eviction_hook(key, agent, soft=True)
                except Exception:
                    logger.debug("eviction hook failed for %s", key, exc_info=True)
        done = threading.Event()
        self._last_release_done = done
        try:
            self._release_worker.submit(plan, done)
        except Exception:
            # Worker could not be started (interpreter shutdown): inline.
            _release_pressure_batch(plan)
            done.set()
        return claimed

    def _warn_nothing_shedable(self, ordered: List[Tuple[str, Any]], rss_mb: int) -> None:
        """Log the gateway's one warning when over budget with nothing to shed.

        Same shape as gateway/run_agent_cache.py:1004-1019: the counts say
        whether memory is pinned by turns that are still running (it will fall
        on its own) or by transcripts that never reach the session DB (the valve
        is stuck, and the operator has to look at persistence).
        """
        mid_turn = sum(
            1 for key, agent in ordered if agent is not None and self._entry_turn_active(key, agent)
        )
        unflushed = sum(
            1 for _key, agent in ordered
            if agent is not None and not transcript_persistence_caught_up(agent)
        )
        logger.warning(
            "Agent cache pressure: anon RSS %sMB over budget %sMB but no "
            "evictable session (%d cached, %d mid-turn, %d blocked on "
            "un-flushed persistence)%s",
            rss_mb, self.memory_high_mb, len(ordered), mid_turn, unflushed,
            (
                " — transcripts are not reaching the session DB (session "
                "persistence disabled or failing?); the memory valve cannot "
                "shed sessions until they persist."
                if unflushed and not mid_turn
                else " — memory will keep climbing until those turns finish."
            ),
        )

    def run_pass(self) -> Dict[str, int]:
        """Run one full governance pass; returns {idle_evicted, pressure_dropped}."""
        idle = self.sweep_idle()
        pressure = self.sweep_pressure()
        return {"idle_evicted": idle, "pressure_dropped": pressure}


def start_server_governor():
    """Start the WebUI agent-cache governor (idle-TTL + pressure passes).

    Wires the governor to the WebUI cache and starts its daemon thread.
    Lives here rather than in server.py so server.py stays under its
    line-count guard; server.py calls this once at startup.  Returns the
    daemon thread, or None when governance is disabled (interval <= 0) or
    the startup failed -- startup must never raise.
    """
    try:
        from api.config import (
            SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK,
            SESSION_AGENT_CACHE_IDLE_TTL, SESSION_AGENT_CACHE_MEMORY_HIGH_MB,
            SESSION_AGENT_CACHE_PROTECT_RECENT, SESSION_AGENT_CACHE_GOVERN_INTERVAL,
        )

        def _close_agent(key, agent):
            try:
                from api.streaming import _close_evicted_agent_at_session_boundary
                _close_evicted_agent_at_session_boundary(key, agent)
            except Exception as e:
                print(f'[!!] WARNING: Agent-cache idle eviction teardown failed for {key}: {e}', flush=True)

        governor = AgentCacheGovernor(
            SESSION_AGENT_CACHE,
            SESSION_AGENT_CACHE_LOCK,
            idle_ttl_secs=SESSION_AGENT_CACHE_IDLE_TTL,
            memory_high_mb=resolve_memory_high_mb(SESSION_AGENT_CACHE_MEMORY_HIGH_MB),
            protect_recent=SESSION_AGENT_CACHE_PROTECT_RECENT,
            close_agent_fn=_close_agent,
            eviction_hook=None,
        )

        # A zero interval means "governance disabled", not "sweep forever":
        # starting the loop would busy-spin (time.sleep(0)) and pin a CPU
        # core.  The guard lives here, not only at the call site, so a daemon
        # can never be created in the disabled state.
        interval = SESSION_AGENT_CACHE_GOVERN_INTERVAL
        if interval <= 0:
            print('[ok] Agent-cache governor: disabled (GOVERN_INTERVAL=0)', flush=True)
            return None

        def _governor_loop():
            while True:
                try:
                    governor.run_pass()
                except Exception as e:
                    print(f'[!!] Agent-cache governance pass failed: {e}', flush=True)
                time.sleep(interval)

        thread = threading.Thread(
            target=_governor_loop,
            name='agent-cache-governor',
            daemon=True,
        )
        thread.start()
        budget = governor.memory_high_mb
        budget_disp = f'{budget}MB' if budget else 'off'
        print(
            f'[ok] Agent-cache governor: idle_ttl={SESSION_AGENT_CACHE_IDLE_TTL}s '
            f'pressure={budget_disp} protect_recent={SESSION_AGENT_CACHE_PROTECT_RECENT}',
            flush=True,
        )
        return thread
    except Exception as e:
        print(f'[!!] WARNING: Agent-cache governor failed to start: {e}', flush=True)
        return None
