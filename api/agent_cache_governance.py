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

* ``sweep_idle_cached_agents`` — fully evicts cached agents idle past the TTL.
* ``sweep_agent_cache_under_pressure`` — soft-evicts the least-recently-used
  transcripts (drops ``_session_messages`` only, keeping the agent object so
  cross-turn state survives); the transcript is rebuilt from the persisted
  session on the next turn.

Both are driven by one daemon thread started from ``server.py``.  Everything
here is pure/read-only so it can be unit-tested without a running server.
"""

from __future__ import annotations

import logging
import os
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
    """Read the process's own cgroup path (v2: single line; v1: pick memory)."""
    try:
        with open("/proc/self/cgroup", "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(":", 2)
                if len(parts) == 3 and parts[1] == "memory":
                    return parts[2]
                if len(parts) == 3 and parts[2].startswith("/"):
                    # v2 single hierarchy: controllers field empty.
                    return parts[2]
    except OSError:
        return None
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
    transcript already on disk).  The ``protect_recent`` most-recently-used
    sessions are never touched — their warm prompt cache is worth the most.
    """
    plan: List[Tuple[str, Any]] = []
    if not ordered:
        return plan
    if protect_recent > 0:
        ordered = ordered[: len(ordered) - protect_recent]
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
    """Drop the live transcript from a cached agent without tearing it down.

    The next turn rebuilds messages from the persisted session (the WebUI feeds
    ``get_state_db_session_messages`` fresh each turn), so dropping the in-RAM
    list is safe and is the single biggest lever on resident memory.  Keeps the
    agent object itself so cross-turn state (_user_turn_count, memory-provider
    state) survives the pressure pass.

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

    Mirrors ``gateway/agent_cache_pressure.py::transcript_persistence_caught_up``:
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

    def _entry_turn_active(self, key: str, agent: Any) -> bool:
        """True when the cached entry is mid-turn (per-entry lease).

        The streaming layer maintains ``agent._turn_active`` at turn
        boundaries (api.config.register_active_run / unregister_active_run),
        so reading it here inside the cache lock yields the NEWEST liveness
        state — the plan-time snapshot can be stale by the time a release
        runs.  Entries without a lease (pre-lease agents, injected test
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
        """Soft-evict LRU transcripts once anonymous RSS crosses the budget.

        Returns the number of transcripts dropped (0 when memory is fine or the
        pass is disabled).

        Concurrency: the cache snapshot is taken under ``self._lock`` so the
        plan is built from a consistent view (no dict-mutation RuntimeError
        while server threads churn the cache).  Each planned soft-release is
        then re-validated immediately before clearing the transcript: the entry
        must still exist, still hold the SAME agent object we planned (a turn
        could have replaced it), the session must not be mid-turn, and the
        transcript must be fully persisted.  A new turn can start after the
        plan but before release; skipping the eviction then costs memory, a
        wrong release costs the user their conversation.
        """
        if not self.memory_high_mb:
            return 0
        if rss_mb is None:
            rss_mb = read_anon_rss_mb()
        if rss_mb is None or rss_mb < self.memory_high_mb:
            return 0

        # Snapshot under the lock: the live OrderedDict is mutated by server
        # threads on every turn, and iterating it unlocked races an insert or
        # eviction (RuntimeError: dictionary changed size during iteration).
        with self._lock:
            snapshot = [
                (key, entry[0] if isinstance(entry, tuple) and entry else entry)
                for key, entry in self._cache.items()
            ]

        try:
            active = self._active_sids_fn()
        except Exception:
            active = None
        if active is None:
            return 0  # liveness registry unavailable — skip the pass (fail closed)
        self._last_active_snapshot = active

        def _is_evictable(key: str, agent: Any) -> bool:
            if agent is None:
                return False
            if key in active:
                return False
            if not transcript_persistence_caught_up(agent):
                return False
            return True

        plan = plan_pressure_evictions(
            snapshot,
            _is_evictable,
            protect_recent=self.protect_recent,
        )
        dropped = 0
        for key, agent in plan:
            try:
                # Revalidate under the lock immediately before release: the
                # plan can go stale between planning and execution.  The
                # mid-turn check here uses the per-entry lease (newest state),
                # NOT the plan-time snapshot — a turn that started after the
                # snapshot must stop the release.
                with self._lock:
                    entry = self._cache.get(key)
                    current = entry[0] if isinstance(entry, tuple) and entry else entry
                    if current is not agent:
                        continue  # entry replaced since planning — leave it
                    if self._entry_turn_active(key, agent) or not transcript_persistence_caught_up(agent):
                        continue  # went active / not persisted — skip
                    soft_release_transcript(agent)
                if self._eviction_hook is not None:
                    self._eviction_hook(key, agent, soft=True)
                logger.info(
                    "Agent-cache governor: pressure soft-evicted session=%s "
                    "(rss=%sMB budget=%sMB, cache_size=%d)",
                    key, rss_mb, self.memory_high_mb, len(self._cache),
                )
                dropped += 1
            except Exception:
                logger.debug("pressure soft-evict failed for %s", key, exc_info=True)
        return dropped

    def run_pass(self) -> Dict[str, int]:
        """Run one full governance pass; returns {idle_evicted, pressure_dropped}."""
        idle = self.sweep_idle()
        pressure = self.sweep_pressure()
        return {"idle_evicted": idle, "pressure_dropped": pressure}
