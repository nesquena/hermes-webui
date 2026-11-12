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
2. The commit before a shed has NO ACKNOWLEDGEMENT
   (gateway/run_agent_cache.py:890 ``_commit_memory_before_soft_evict``): the
   captured transcript is delivered to ``AIAgent.commit_memory_session()``
   inside the session owner's profile scope, the hook's return value is
   ignored, and a raising hook is logged and dropped.  There is NO per-session
   release debt and NO retry loop.  The installed Agent returns None and runs
   provider I/O through a swallow-all helper, so no caller can observe whether
   extraction happened; gating a release on that confirmation would mean never
   releasing.  What is still observable — and therefore still enforced — is the
   lifecycle ledger: a delivery the owner scope REFUSED leaves the session's
   work pending, and that pending extraction keeps its payload (point 6).
3. The release is SOFT (gateway/run_agent_cache.py:911
   ``_release_evicted_agent_soft``) and happens in the gateway's order:
   ``release_clients()`` FIRST (the evicted handle's LLM clients go; tool state,
   the sandbox, background processes and the memory provider survive so the
   session can resume), THEN ``_session_messages`` and
   ``_db_flush_scan_prefix`` are dropped.  The agent handle itself is kept (no
   session-DB teardown) and the next turn rebuilds the transcript from the
   persisted session, with fresh clients.  The plan is popped out of the LRU
   cache under the cache lock, so nothing has to re-validate the entry later.
4. One pass sheds at most ``_MAX_EVICTIONS_PER_PASS`` transcripts, LRU first,
   and hands the batch to the one pressure-release daemon thread
   (``PressureReleaseWorker``), so a provider commit can never hold the cache
   lock or stall the pass.  ``protect_recent`` is clamped to half the cache.
   A pass must NOT claim a new batch while the previous one is still draining
   (see point 5).  When nothing is shedable above budget the pass logs the
   gateway's warning once, with the cached / mid-turn / un-flushed counts — the
   only way an operator learns the valve is stuck rather than idle.
5. ONE batch in flight, and the backlog is visible.  The gateway starts a
   thread per batch, so its drain rate scales with the backlog; this port has a
   single worker and therefore must not queue batches behind it: claiming an
   entry means popping it out of the cache, so every queued batch is a
   transcript still pinned in RAM while the log says "shedding".  With an
   unbounded queue and a pass every interval the depth grew without limit
   (measured 1 -> 10, then 4 -> 41 pinned transcripts, while every pass logged
   a shed).  A pass over budget that finds the worker busy skips the claim and
   logs the observed backlog (batches / transcripts / queue depth), so an
   operator can see a drain that is falling behind instead of a valve that
   silently piles up.  Nothing is lost: the batch already claimed drains in
   full, and the pass after it sheds again.
6. A REFUSED pre-release commit keeps the transcript.  The commit is best
   effort (point 2) in the sense that nothing waits for an acknowledgement and
   the hook's return value is ignored — but the LIFECYCLE LEDGER still knows
   whether the session's extraction is pending, and that pending extraction has
   exactly one payload: the live transcript.  So a delivery the owner scope
   refused does not clear it: the entry still leaves the cache (no pass
   re-delivers to a hook that is refusing), the transcript stays resident for
   the next session boundary, and no empty-payload commit is ever issued in its
   place.  A session with nothing pending is released normally.

   THIS IS THE DELIBERATE DIVERGENCE FROM THE GATEWAY: the gateway logs a
   raising hook and DROPS it (point 2).  Here a raising hook is logged, the
   transcript is RETAINED for the next boundary, and the ledger stays dirty —
   because the ledger says that extraction is still owed and the transcript is
   the only payload it can be given.  Retaining is the whole difference: no
   retry loop, no debt, no timer lives in this valve; the next session boundary
   re-attempts the delivery as it would for any pending generation.

   The price of that choice, recorded rather than fixed: a session whose commit
   is permanently REFUSED keeps its transcript and its provider client until the
   next boundary (reviewer-measured: 40 such sessions -> 32 MB over 24 h).  The
   valve cannot free the transcript without losing the extraction, and the only
   release path here (``soft_release_transcript``) frees the client and drops
   the transcript in one step — so a refused session keeps both.  That is the
   price of not silently losing extraction.  An entry whose commit raises
   ``BaseException`` is a different case: it is ABANDONED explicitly (claim
   released, clients released, transcript kept and logged as kept), because
   nothing else will ever touch that handle.

MEASURED COST (ordering only; the numbers are one machine's, not a benchmark)
-----------------------------------------------------------------------------
What is defensible is the ORDERING: the provider commit is moved off the pass
(it runs on the release worker, never on the pass and never under the cache
lock), and the cache walk is unchanged.  The reviewer's harness, which is
shape-independent in method, measured 87.7 ms -> 0.226 ms per over-budget pass
once the commit is real (5 ms), and a pure cache walk of 0.184 ms -> 0.119 ms.
Those are measurements from ONE machine and do not reproduce elsewhere — the
same code re-measured on another host is 4-7x apart, so treat them as the shape
of the win, not as numbers to quote.  What must not be claimed: a pass-latency
win measured on an EMPTY cache (the ~2 us early return), or the churn smoke's
wall clock — that is import amortisation and it disappears once
``api.streaming`` is already imported, as it always is in a running server.
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


def _cgroup_group_chain(own: Optional[str]) -> List[str]:
    """Cgroup paths from ``own`` up to the hierarchy root (``/``), own first."""
    own = (own or "").strip()
    if not own or own == "/":
        return ["/"]
    chain: List[str] = []
    path = own if own.startswith("/") else f"/{own}"
    while True:
        chain.append(path)
        if path == "/":
            return chain
        path = path.rsplit("/", 1)[0] or "/"


def _cgroup_level_files(group: str) -> Tuple[str, ...]:
    """The limit files to read for ONE cgroup level (v2 first, then v1)."""
    prefix = "" if group == "/" else group
    return (
        f"/sys/fs/cgroup{prefix}/memory.high",
        f"/sys/fs/cgroup{prefix}/memory.max",
        # cgroup v1: a level lives under the v1 memory-controller mount, not at
        # the unified root.  Without this a server in a capped v1 child with an
        # unlimited root reads "unlimited" and the pressure pass never fires.
        f"/sys/fs/cgroup/memory{prefix}/memory.limit_in_bytes",
    )


def _finite_cgroup_limit(path: str) -> Optional[int]:
    """One limit file as a finite cap; None when missing, empty, max/sentinel."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        limit = int(raw)
    except ValueError:
        return None
    # A v1 root reads the near-2^63 sentinel; both spellings mean unlimited.
    return limit if 0 < limit < (1 << 62) else None


def _cgroup_memory_limit_bytes() -> Optional[int]:
    """Return the smallest memory limit this process runs under, if capped.

    Walks the process's OWN cgroup and every PARENT up to the hierarchy root,
    reading ``memory.high``/``memory.max`` (v2) and ``memory.limit_in_bytes``
    (v1) at each level, then keeps the SMALLEST finite value.  A ``max`` (or a
    v1 near-2^63 sentinel) at one level therefore cannot hide a finite cap at
    another, and a cap larger than one below it never wins.

    Why the walk and not the gateway's two probes: a container/pod limit
    normally lands on a PARENT of the process's group (``/kubepods/.../podX``
    caps the pod while the container group reads ``max``; a systemd slice
    ``MemoryMax=`` sits above the unit's scope).  Reading only own + root makes
    that case look uncapped, so ``auto`` would derive the budget from HOST RAM,
    the pass would never fire, and the OOM killer would take the process before
    any shedding happened.

    DIVERGENCE (intentional, documented): ``gateway/agent_cache_pressure.py``
    ``_cgroup_limit_bytes`` reads own + root only and returns the FIRST finite
    hit.  This walk is strictly stricter — it reports the tightest cap on the
    chain, never a larger one than the gateway would — so it is gateway parity
    plus inherited caps, not a different policy.  Non-Linux returns None (no
    cgroup files), and every unreadable level is skipped.
    """
    if not sys_platform_linux():
        return None
    try:
        own = _own_cgroup_path()
    except Exception:
        own = None
    limits = [
        limit
        for group in _cgroup_group_chain(own)
        for limit in (_finite_cgroup_limit(p) for p in _cgroup_level_files(group))
        if limit is not None
    ]
    return min(limits) if limits else None


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


def _agent_has_resident_transcript(agent: Any) -> bool:
    """True when the cached agent still pins transcript memory.

    A soft-released entry keeps ``_session_messages == []`` and
    ``_db_flush_scan_prefix is None``, so it pins nothing worth re-dropping —
    and it is *trivially* "caught up" (``0 >= 0``), which is why the persistence
    gate alone cannot exclude it.  Streaming re-inserts the handle it fetched,
    so a released handle reappears in the cache: without this guard every later
    pass re-claims it (measured 5 of 5 passes), wasting an eviction slot on
    entries that hold memory and issuing a no-payload
    ``commit_memory_session()`` call for a transcript that no longer exists.
    """
    if agent is None:
        return False
    try:
        messages = getattr(agent, "_session_messages", None)
        prefix = getattr(agent, "_db_flush_scan_prefix", None)
        return bool(messages) or bool(prefix)
    except Exception:
        return False


def _legacy_full_close(agent: Any) -> None:
    """Close a legacy agent shape that predates ``release_clients()``.

    Gateway parity for the fallback branch of ``_release_evicted_agent_soft``
    (gateway/run_agent_cache.py:921 calls the full ``_cleanup_agent_resources``
    when ``release_clients`` is absent): drain the memory provider with the live
    transcript, then ``close()``.  Both are optional and swallowed — this is a
    best-effort teardown of an agent shape that no longer ships.
    """
    messages = getattr(agent, "_session_messages", None)
    for name in ("shutdown_memory_provider", "close"):
        fn = getattr(agent, name, None)
        if not callable(fn):
            continue
        try:
            if name == "shutdown_memory_provider" and isinstance(messages, list):
                fn(list(messages))
            else:
                fn()
        except TypeError:
            # A hook that takes no transcript: retry bare, never fabricate one.
            try:
                fn()
            except Exception:
                logger.debug("legacy %s failed on soft release", name, exc_info=True)
        except Exception:
            logger.debug("legacy %s failed on soft release", name, exc_info=True)


def _release_agent_clients(agent: Any) -> None:
    """Drop an evicted agent's LLM clients (gateway/run_agent_cache.py:911).

    ``AIAgent.release_clients()`` is the gateway's soft-release primitive: it
    retires the shared client and closes the per-request clients / codex child,
    is idempotent, and deliberately keeps session tool state (sandbox, browser,
    background processes, memory provider) so the session can resume — the next
    turn builds fresh clients.  Without this call every shed session keeps its
    provider client open for the life of the process, and because the entry has
    left the cache the idle-TTL teardown can never reach the handle to close it
    (measured: 0 provider shutdowns / 0 client releases for shed agents).
    """
    if agent is None:
        return
    release = getattr(agent, "release_clients", None)
    if callable(release):
        try:
            release()
        except Exception:
            logger.debug("soft-release release_clients failed", exc_info=True)
        return
    _legacy_full_close(agent)


def soft_release_transcript(agent: Any) -> None:
    """Drop the live transcript from an evicted agent without tearing it down.

    Mirrors gateway/run_agent_cache.py:911 ``_release_evicted_agent_soft``, in
    the gateway's order: ``release_clients()`` first (the commit that runs
    before this needs the live memory manager, so it has already happened), then
    the transcript.  The agent handle and its tool state (terminal sandbox,
    browser, background processes) are kept — the session may resume — while the
    conversation is given back.  The next turn rebuilds messages from the
    persisted session (the WebUI feeds ``get_state_db_session_messages`` fresh
    each turn), so dropping the in-RAM list is safe and is the single biggest
    lever on resident memory.

    Only safe once the transcript is fully on disk: clearing the sole complete
    in-memory copy while persistence lags loses history on the next rebuild.
    Callers must check ``transcript_persistence_caught_up(agent)`` first.
    """
    if agent is None:
        return
    _release_agent_clients(agent)
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


def _commit_before_soft_release(session_id: str, agent: Any) -> bool:
    """Best-effort memory commit before the transcript is dropped.

    Mirrors gateway/run_agent_cache.py:890 ``_commit_memory_before_soft_evict``:
    the return value of the hook is ignored and a raising hook is only logged —
    nothing here waits for an acknowledgement.  The call goes through the
    session lifecycle so (a) it runs inside the session owner's profile scope
    (#8517) and (b) the provider receives the OWNER's captured transcript
    instead of ``[]`` — a batch-extraction provider with an empty-input guard
    would otherwise skip the session's memories entirely.

    Returns True when the transcript may be released — never because "the hook
    ran", only because the LEDGER has nothing pending for this session:

    * the lifecycle has no pending work for the session — nothing needs the
      payload, so there is nothing to commit and no commit is issued; or
    * after the delivery, the ledger shows no pending work left.

    The second condition has to be re-read after the attempt: the delivery
    commits the session's EARLIEST pending segment
    (``session_lifecycle._first_uncommitted_segment``), so an agent that owns a
    later generation can watch an OLDER owner's segment succeed while its own
    generation is still pending.  Treating that as success cleared the agent's
    transcript, and the next boundary then committed ``payload=None`` and marked
    its extraction clean.

    False means a pending extraction was REFUSED (owner scope refused, hook
    raised, or the lifecycle could not even be probed): the transcript is the
    only payload that extraction can still be given, so the caller must keep it.
    Clearing it there is how the ledger ended up committing ``payload=None`` at
    the next boundary and marking the extraction done.
    """
    try:
        from api.streaming import (
            _lifecycle_commit_session_memory,
            _lifecycle_has_uncommitted_work,
        )
    except Exception:
        # Cannot tell whether a pending extraction needs this payload: keep it.
        logger.debug("lifecycle probe unavailable for %s; keeping transcript", session_id, exc_info=True)
        return False
    try:
        if not _lifecycle_has_uncommitted_work(session_id):
            return True
        _lifecycle_commit_session_memory(session_id, agent=agent, wait=False)
        # The delivery commits the session's EARLIEST pending segment, which is
        # not necessarily this agent's: a rebuilt agent can own a later
        # generation while an older owner's segment is still pending.  "The hook
        # ran" is therefore not "this transcript is free" — the ledger is the
        # only fence, and it is re-read AFTER the attempt (which also covers a
        # concurrent boundary that committed the work while we were refused).
        return not _lifecycle_has_uncommitted_work(session_id)
    except Exception:
        logger.debug("pre-release memory commit failed for %s", session_id, exc_info=True)
        return False


def _abandon_aborted_release(session_id: str, agent: Any, exc: BaseException) -> None:
    """Give up on ONE entry whose release raised a ``BaseException``.

    A commit hook can raise past ``Exception`` — ``SystemExit`` from a lazy
    relaunch import, an interrupt.  ``commit_session_memory`` unwinds from the
    provider call without reaching either of its ``in_flight`` clear points, so
    without this the session's claim stays set for the life of the process:
    ``commit_session_memory(wait=True)`` then blocks on every later boundary and
    ``discard_session`` refuses, while the provider client stays open
    (measured by the reviewer: claim stuck, >3 s per call, client never
    released).

    So the abandonment is explicit and logged: hand the in-flight claim back,
    release the LLM clients with the same helper the normal path uses, and keep
    the transcript.  It is kept because the ledger still reports the extraction
    as pending and the transcript is the only payload a later boundary can give
    it — dropping it is precisely the silent data loss this valve avoids.
    Releasing the clients does not disable that retry: ``release_clients()``
    keeps the memory provider (gateway parity), so ``commit_memory_session``
    still works on the next boundary.  A legacy agent shape with no
    ``release_clients()`` only has the full close, which takes the transcript
    with it; the log says that instead of pretending the payload survived.
    """
    released = False
    try:
        from api.streaming import _lifecycle_release_in_flight

        released = _lifecycle_release_in_flight(session_id)
    except Exception:
        logger.debug(
            "could not release the in-flight commit claim for %s", session_id, exc_info=True
        )
    # The legacy shape (no ``release_clients``) only has the FULL close, which
    # also drops the transcript — report what actually happened to the payload
    # instead of claiming it survived.
    soft = callable(getattr(agent, "release_clients", None))
    _release_agent_clients(agent)
    kept = bool(getattr(agent, "_session_messages", None))
    if not kept:
        fate = (
            "and its transcript went with the legacy full close (that agent shape "
            "has no soft release), so the next boundary has no payload for the "
            "extraction that is still pending."
        )
    elif soft:
        fate = (
            "transcript KEPT because its extraction is still pending and the "
            "transcript is the only payload the next session boundary can deliver."
        )
    else:
        fate = (
            "transcript KEPT — the legacy full close left it in place, so the next "
            "boundary can still deliver the pending extraction."
        )
    logger.warning(
        "Agent cache pressure: release for session=%s aborted by %s; abandoning "
        "that entry — in-flight commit claim %s, LLM clients released, %s",
        session_id,
        type(exc).__name__,
        "released" if released else "was not held",
        fate,
    )


def _forget_released_agent(session_id: str, agent: Any) -> bool:
    """Drop the lifecycle ledger's strong reference to a released handle.

    ``release_clients()`` keeps the memory provider on purpose (the session may
    resume), and the claim already popped the entry out of the cache — so after
    a shed the ledger's ``agent`` slot is the ONLY reference that can still reach
    the handle.  Nothing else ever will: the idle sweep walks the cache, and the
    next turn builds a different agent.  Leaving it there pins the provider for
    the life of the process.

    The ENTRY stays: its ``generation`` / ``committed_generation`` is what the
    next turn's registration continues from, and an uncommitted segment keeps
    its owner (the fence guarantees none exists here; the callee asserts it).
    """
    try:
        from api.streaming import _lifecycle_forget_released_agent

        return _lifecycle_forget_released_agent(session_id, agent)
    except Exception:
        logger.debug("could not drop the ledger reference for %s", session_id, exc_info=True)
        return False


def _release_pressure_batch(plan: List[Tuple[str, Any]]) -> None:
    """Release one pressure batch sequentially: commit, then soft release.

    The plan is drained (``pop(0)`` + ``del``) rather than iterated so no local
    reference pins the evicted agents until the batch ends
    (gateway/run_agent_cache.py:1041 drains its batch the same way).

    Failure policy per entry — the plan was popped out of the cache at claim
    time, so whatever happens next the entry is unreachable for later passes:

    * no resident transcript -> skip without a commit (nothing to free, no
      payload to deliver);
    * transcript released -> the lifecycle ledger's reference to the handle is
      dropped as well (``_forget_released_agent``): the entry and its
      generation ledger stay for the next turn, but the released agent — and the
      memory provider ``release_clients()`` keeps — becomes collectable instead
      of living for the rest of the process;
    * commit refused (Exception) -> KEEP the transcript for the next session
      boundary instead of shedding it; no pass re-delivers to a hook that is
      refusing, and an empty-payload commit never happens;
    * BaseException (SystemExit from a lazy import, an interrupt) -> abandon
      THAT entry explicitly (``_abandon_aborted_release``: in-flight claim
      handed back, clients released, transcript kept and logged as kept) and
      continue with the REST of the batch.  The gateway's thread-per-batch shape
      loses only its own batch to this; this port has one worker for the
      process's lifetime, so letting the exception escape would both strand the
      remaining claimed transcripts and permanently disable the valve.
    """
    while plan:
        key, agent = plan.pop(0)  # FIFO — LRU-first order preserved
        try:
            if not _agent_has_resident_transcript(agent):
                logger.debug("pressure release skipped %s: no resident transcript", key)
            elif not _commit_before_soft_release(key, agent):
                # Not necessarily a refusal: the delivery may have committed an
                # OLDER owner's segment and left this agent's extraction
                # pending (see _commit_before_soft_release).  Either way the
                # transcript is the only payload that extraction can still be
                # given, so the log states the fact, not a guessed cause.
                logger.warning(
                    "Agent cache pressure: session=%s still has a pending "
                    "extraction after the pre-release memory commit; keeping its "
                    "transcript for the next session boundary instead of shedding "
                    "it (shedding it would leave that extraction with no payload).",
                    key,
                )
            else:
                soft_release_transcript(agent)
                _forget_released_agent(key, agent)
        except Exception:
            logger.debug("pressure soft-release failed for %s", key, exc_info=True)
        except BaseException as exc:  # noqa: BLE001 — see the docstring
            try:
                _abandon_aborted_release(key, agent, exc)
            except BaseException:  # noqa: BLE001 — never strand the rest of the batch
                logger.debug("pressure abort cleanup failed for %s", key, exc_info=True)
        del agent


class PressureReleaseWorker:
    """ONE daemon thread that drains pressure batches, strictly in order.

    The gateway starts a fresh daemon thread per batch
    (gateway/run_agent_cache.py:1027).  ``Thread.start()`` blocks until the
    child is scheduled, and under the cache churn this valve exists for that
    costs tens of milliseconds on some platforms (measured locally on
    Windows/CPython with a thread spinning on the cache lock; not reproduced
    by the reviewers, who measured 0.24-0.36 ms for the uncontended start) —
    enough to turn the valve itself into the stall it exists to prevent.  One
    long-lived worker gives the same contract — the commit (provider I/O) never
    runs on the pass or under the cache lock — without paying that per pass.

    What the single worker does NOT inherit is the gateway's scaling: a thread
    per batch drains in parallel with the backlog, while one worker drains it
    serially.  So the pass must never queue a second batch behind a draining
    one (``busy()``), and the backlog (``backlog()``) is reported when a pass
    skips for that reason.  Depth is therefore bounded at one batch.

    ``submit(plan, done)`` is non-blocking; ``done`` (an Event) is set when the
    batch finishes.  Ordering is FIFO, so LRU-first order across batches is
    preserved.
    """

    def __init__(self) -> None:
        self._batches: "queue.SimpleQueue" = queue.SimpleQueue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # Batches submitted and not yet finished, and the transcripts inside
        # them — i.e. what is still pinned in RAM by this valve.
        self._outstanding = 0
        self._outstanding_transcripts = 0
        self._newest_done: Optional[threading.Event] = None

    def busy(self) -> bool:
        with self._lock:
            return self._outstanding > 0

    def backlog(self) -> Dict[str, int]:
        """Observed backlog: batches/transcripts still in flight, queue depth."""
        with self._lock:
            return {
                "batches": self._outstanding,
                "transcripts": self._outstanding_transcripts,
                "depth": self._batches.qsize(),
            }

    def newest_done(self) -> Optional[threading.Event]:
        """Event of the most recently submitted batch (set when it finishes)."""
        with self._lock:
            return self._newest_done

    def submit(self, plan: List[Tuple[str, Any]], done: threading.Event) -> None:
        count = len(plan)
        with self._lock:
            self._outstanding += 1
            self._outstanding_transcripts += count
            self._newest_done = done
            try:
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(
                        target=self._run,
                        name="agent-cache-pressure",
                        daemon=True,
                    )
                    self._thread.start()
            except BaseException:
                # Never leave a phantom batch counted as in flight: that would
                # make every later pass skip (the valve would wedge shut).
                self._outstanding -= 1
                self._outstanding_transcripts -= count
                raise
        try:
            self._batches.put((plan, done, count))
        except BaseException:
            with self._lock:
                self._outstanding -= 1
                self._outstanding_transcripts -= count
            raise

    def _run(self) -> None:
        while True:
            plan, done, count = self._batches.get()
            try:
                _release_pressure_batch(plan)
            except Exception:
                logger.debug("pressure batch release failed", exc_info=True)
            except BaseException as exc:  # noqa: BLE001
                # Never let one entry's abort take the worker (and with it the
                # whole valve) out of service: the thread is process-lifetime.
                logger.warning(
                    "Agent cache pressure: release batch aborted by %s; worker "
                    "stays alive", type(exc).__name__,
                )
            finally:
                with self._lock:
                    self._outstanding -= 1
                    self._outstanding_transcripts -= count
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
        # Claim+submit is one step: a pass that finds another pass claiming (or
        # a batch still draining) must not stack a second batch on the worker.
        self._pressure_gate = threading.Lock()

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
        transcripts that are live, not mid-turn, still resident, and whose
        persistence is caught up; pop the plan OUT of the cache under
        ``self._lock``; then hand the batch to the single pressure-release
        daemon thread (``PressureReleaseWorker``), because the commit is
        provider I/O and must hold neither the cache lock nor the pass.  Popping
        under the lock is what makes the release race-free without a re-check: a
        turn that starts right after misses the cache and rebuilds its agent, so
        the batch can only touch agents nobody is serving.

        ONE batch in flight: claiming pops the entry out of the cache, so a
        second batch queued behind a slow drain is a set of transcripts still
        pinned in RAM while the log reports them shed (the unbounded queue grew
        1 -> 10 -> 41 pinned transcripts under a slow drain).  A pass that finds
        the worker busy therefore claims nothing and logs the observed backlog;
        the claimed batch drains in full and the pass after it sheds again.

        Returns the number of transcripts claimed for release (0 when memory is
        fine, the pass is disabled, nothing is shedable, or the previous batch
        is still draining).
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
            # Gateway parity: _is_live(agent) and transcript_persistence_caught_up,
            # plus "still holds a transcript" so an already-released handle that
            # was re-inserted into the cache is not re-claimed every pass.  The
            # resident check runs LAST because it is the only one that has to
            # look at a transcript that is (usually) already ruled out: a
            # released handle is caught at 0 >= 0 by the persistence gate, so
            # the order costs nothing and keeps the pass's per-entry cost at the
            # gateway's two checks for every un-flushed entry.
            if agent is None:
                return False
            if self._entry_turn_active(key, agent):
                return False
            if not transcript_persistence_caught_up(agent):
                return False
            return _agent_has_resident_transcript(agent)

        if not self._pressure_gate.acquire(blocking=False):
            # A concurrent pass is claiming/submitting right now.
            self._log_backlog("another pass is claiming")
            return 0
        try:
            if self._release_worker.busy():
                self._log_backlog("the release worker is still draining")
                # Callers waiting on the batch must wait for the live one.
                in_flight = self._release_worker.newest_done()
                if in_flight is not None:
                    self._last_release_done = in_flight
                return 0

            # Snapshot AND claim under the lock: the live OrderedDict is mutated
            # by server threads on every turn, and iterating it unlocked races
            # an insert or eviction (RuntimeError: dictionary changed size).
            with self._lock:
                ordered = [
                    (key, entry[0] if isinstance(entry, tuple) and entry else entry)
                    for key, entry in self._cache.items()
                ]
                if not ordered:
                    # Nothing cached — whatever is using the heap, it isn't us,
                    # and warning about it every tick would point at the wrong
                    # subsystem.
                    return 0
                plan = plan_pressure_evictions(
                    ordered,
                    _is_evictable,
                    protect_recent=self.protect_recent,
                    max_evictions=_MAX_EVICTIONS_PER_PASS,
                )
                for key, agent in plan:
                    # Only drop the entry we validated: a turn can re-insert the
                    # key between planning and this pop, and that replacement is
                    # a live handle nobody planned to shed.
                    entry = self._cache.get(key)
                    current = entry[0] if isinstance(entry, tuple) and entry else entry
                    if current is agent:
                        self._cache.pop(key, None)

            if not plan:
                self._warn_nothing_shedable(ordered, rss_mb)
                return 0

            claimed = len(plan)  # the worker drains `plan` in place
            # One summary line per claiming pass (INFO: the WARNING above is
            # reserved for a valve that cannot shed at all).  "Claimed", not
            # "shed": the release happens on the worker, and a refused commit
            # keeps the transcript (see _release_pressure_batch).
            logger.info(
                "Agent cache pressure: anon RSS %sMB over budget %sMB — claiming "
                "%d LRU transcript(s) for soft release: %s",
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
        finally:
            self._pressure_gate.release()

    def _log_backlog(self, why: str) -> None:
        """Report the observed backlog when a pass skips claiming.

        The counts are what an operator needs to see a drain falling behind the
        pass cadence: batches / transcripts still pinned by the valve, and the
        queue depth behind the worker.
        """
        backlog = self._release_worker.backlog()
        logger.info(
            "Agent cache pressure: skipping this pass — %s (%d batch(es), %d "
            "transcript(s) still pinned by the release worker, queue depth %d); "
            "the next pass sheds once the backlog drains.",
            why, backlog["batches"], backlog["transcripts"], backlog["depth"],
        )

    def _warn_nothing_shedable(self, ordered: List[Tuple[str, Any]], rss_mb: int) -> None:
        """Log the gateway's one warning when over budget with nothing to shed.

        Same shape and the same COUNTING as gateway/run_agent_cache.py:1004-1019:
        the buckets are mutually exclusive — a mid-turn session is counted as
        mid-turn and NOT also as "blocked on un-flushed persistence" (the
        gateway's ``_is_live`` excludes running agents from the un-flushed sum).
        Counting a mid-turn session in both made this line disagree with the
        gateway for the same state (1 vs 0 blocked on un-flushed persistence) and
        sent an operator looking at session persistence when the memory was
        pinned by turns that were still running.
        """
        mid_turn = 0
        unflushed = 0
        for key, agent in ordered:
            if agent is None:
                continue
            if self._entry_turn_active(key, agent):
                mid_turn += 1
            elif not transcript_persistence_caught_up(agent):
                unflushed += 1
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
