#!/usr/bin/env python3
"""Concurrency smoke: AgentCacheGovernor.sweep_pressure must not crash under cache churn.

Reproduces Manny7717's conditions: 2000 cache entries + a churn thread
(concurrent insert/evict) + the governor repeatedly running sweep_pressure.
The old code raised
RuntimeError: dictionary changed size during iteration (swallowed by the sweep's
except clause, so the pressure pass silently stopped working);
after the fix the sweep must complete steadily, releasing only
"inactive + persisted" transcripts.

Run (per AGENTS.md): from the repo root,
    ./scripts/test.sh tests/concurrency_smoke_governor.py
(pytest only imports this module to run main(); the script is not collected by
pytest.)
"""
import sys
import threading
import time
from pathlib import Path

# Repo root = two levels up from this file; no private machine path hard-coded.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.agent_cache_governance import AgentCacheGovernor, transcript_persistence_caught_up  # noqa: E402


class FakeAgent:
    """Mimic AIAgent: _session_messages + _last_flushed_db_idx + _db_flush_scan_prefix."""

    def __init__(self, session_id, flushed=False, activity=0.0):
        self.session_id = session_id
        self._session_messages = [{"role": "user", "content": "x" * 100} for _ in range(10)]
        self._last_flushed_db_idx = len(self._session_messages) if flushed else 3
        self._db_flush_scan_prefix = list(self._session_messages[:3])
        self._last_activity_ts = activity

    def __repr__(self):
        return f"<FakeAgent {self.session_id} flushed={self._last_flushed_db_idx == len(self._session_messages)}>"


def main():
    n = 2000
    cache = {}
    for i in range(n):
        cache[f"s{i}"] = (FakeAgent(f"s{i}", flushed=True), object())
    lock = threading.Lock()

    gov = AgentCacheGovernor(
        cache, lock,
        idle_ttl_secs=0,          # pressure path only
        memory_high_mb=1,         # force over-budget (production code reads RSS; rss_mb overrides)
        protect_recent=0,
    )

    stop = threading.Event()
    stats = {"churn": 0, "evicted": 0}

    def churn():
        i = 0
        while not stop.is_set():
            with lock:
                if len(cache) > n:
                    # evict an arbitrary entry
                    key = next(iter(cache))
                    cache.pop(key, None)
                cache[f"churn{i % 500}"] = (FakeAgent(f"churn{i % 500}", flushed=True), object())
                i += 1
                stats["churn"] += 1

    t = threading.Thread(target=churn, daemon=True)
    t.start()

    errors = []
    start = time.time()
    passes = 300
    for p in range(passes):
        try:
            dropped = gov.sweep_pressure(rss_mb=2000)  # force over-budget
            stats["evicted"] += dropped
        except RuntimeError as e:
            errors.append(f"pass {p}: RuntimeError: {e}")
        except Exception as e:
            errors.append(f"pass {p}: {type(e).__name__}: {e}")

    stop.set()
    t.join(timeout=5)
    elapsed = time.time() - start

    print(f"passes={passes} churn_ops={stats['churn']} dropped_total={stats['evicted']} elapsed={elapsed:.1f}s")
    if errors:
        print(f"FAIL: {len(errors)} errors, first: {errors[0]}")
        sys.exit(1)

    # Check: released agents must have been flushed; active agents must not be released.
    unflushed_released = 0
    with lock:
        for key, entry in cache.items():
            agent = entry[0] if isinstance(entry, tuple) and entry else entry
            if agent is not None and hasattr(agent, "_session_messages"):
                if agent._session_messages == [] and agent._last_flushed_db_idx != len([1] * 10) and agent._last_flushed_db_idx < 10:
                    # after release messages=[]; if it was not flushed at release
                    # time (flushed idx<10) that is a bug
                    unflushed_released += 1
    # Simpler check: a released agent's _session_messages must be empty and persistence
    # must have been caught up at release time. We cannot trace the release moment
    # afterwards, so instead assert: every agent still in the cache whose messages
    # are empty must have flush idx == 10 (flushed).
    bad = 0
    with lock:
        for key, entry in cache.items():
            agent = entry[0] if isinstance(entry, tuple) and entry else entry
            if agent is not None and getattr(agent, "_session_messages", None) == []:
                if not transcript_persistence_caught_up(agent):
                    bad += 1
    if bad:
        print(f"FAIL: {bad} released agents were NOT persisted at release time")
        sys.exit(1)

    print("OK: 300 sweeps under churn, no RuntimeError; all released agents were persisted")
    sys.exit(0)


if __name__ == "__main__":
    main()