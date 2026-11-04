#!/usr/bin/env python3
"""Concurrency smoke: AgentCacheGovernor.sweep_pressure must not crash under cache churn.

Reproduces Manny7717's conditions: 2000 cache entries + a churn thread
(concurrent insert/evict) + the governor repeatedly running sweep_pressure.
The old code raised
RuntimeError: dictionary changed size during iteration (swallowed by the sweep's
except clause, so the pressure pass silently stopped working);
after the fix the sweep must complete steadily, releasing only
"inactive + persisted" transcripts.

Run (per AGENTS.md), either form:
    ./scripts/test.sh tests/concurrency_smoke_governor.py     # pytest collects test_*
    python tests/concurrency_smoke_governor.py                # standalone, prints stats
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


def run_smoke(passes: int = 300, n: int = 2000, unflushed_every: int = 7) -> dict:
    """Run the churn smoke; returns {passes, churn_ops, dropped, errors, unpersisted}.

    The cache lock is SHARED with the churn thread — production callers all take
    it, and a churn writer on a different lock still races the locked snapshot.

    ``unflushed_every`` plants agents whose transcript has NOT reached the
    session DB.  They are what makes the ``unpersisted_at_claim`` assertion mean
    anything: with an all-flushed population the persistence gate can never fire
    (measured: removing the gate still reported 0, while un-flushed fixtures
    reported 480 claimed-unpersisted), so the assertion passed no matter what
    the gate did.
    """
    cache = {}
    for i in range(n):
        cache[f"s{i}"] = (FakeAgent(f"s{i}", flushed=not (unflushed_every and i % unflushed_every == 0)), object())
    lock = threading.Lock()
    stop = threading.Event()
    stats = {"churn": 0, "evicted": 0, "unpersisted_at_claim": 0}

    def _claim_hook(key, agent, soft):
        # Runs at claim time, while the transcript is still resident: this is
        # the only moment the persistence gate can be observed directly.  A
        # shed transcript that was NOT fully on disk would lose history on the
        # next rebuild, so count it here (the post-hoc check below cannot see
        # it: an emptied list is trivially 'caught up').
        if soft and not transcript_persistence_caught_up(agent):
            stats["unpersisted_at_claim"] += 1

    gov = AgentCacheGovernor(
        cache, lock,
        idle_ttl_secs=0,          # pressure path only
        memory_high_mb=1,         # force over-budget (production code reads RSS; rss_mb overrides)
        protect_recent=0,
        eviction_hook=_claim_hook,
    )

    def churn():
        i = 0
        while not stop.is_set():
            with lock:
                if len(cache) > n:
                    # evict an arbitrary entry
                    key = next(iter(cache))
                    cache.pop(key, None)
                flushed = not (unflushed_every and i % unflushed_every == 0)
                cache[f"churn{i % 500}"] = (FakeAgent(f"churn{i % 500}", flushed=flushed), object())
                i += 1
                stats["churn"] += 1

    t = threading.Thread(target=churn, daemon=True)
    t.start()

    errors = []
    start = time.time()
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

    # Nothing shed may have been un-persisted at claim time (see
    # _claim_hook); the post-hoc sweep below is a second, weaker net for
    # entries that are still cached with an emptied transcript.
    bad = 0
    unflushed_remaining = 0
    with lock:
        for key, entry in cache.items():
            agent = entry[0] if isinstance(entry, tuple) and entry else entry
            if agent is None:
                continue
            if getattr(agent, "_session_messages", None) == []:
                if not transcript_persistence_caught_up(agent):
                    bad += 1
            elif not transcript_persistence_caught_up(agent):
                # Still resident and still behind the DB: the gate kept it.
                unflushed_remaining += 1

    return {
        "passes": passes,
        "churn_ops": stats["churn"],
        "dropped": stats["evicted"],
        "errors": errors,
        "unpersisted": bad + stats["unpersisted_at_claim"],
        "unflushed_remaining": unflushed_remaining,
        "unflushed_present": stats["unpersisted_at_claim"] + unflushed_remaining,
        "elapsed": elapsed,
    }


def test_pressure_sweeps_under_churn_stay_clean():
    """The documented command must actually exercise the governor.

    A module that only defines main() is collected by pytest without running
    anything, so the documented smoke command used to pass while checking
    nothing.  This is the pytest entry point; main() keeps the script form.

    The un-flushed fixtures are not decoration: they are the only way the
    ``unpersisted`` assertion can fail.  ``unflushed_present > 0`` proves the
    population really contained un-flushed transcripts (so a gate that stopped
    working would have claimed them), and ``dropped > 0`` proves the flushed
    majority was still shed — the smoke is neither vacuous nor inert.
    """
    result = run_smoke(passes=60, n=400)
    assert not result["errors"], result["errors"][0]
    assert result["unflushed_present"] > 0, (
        "no un-flushed agent was ever present: the persistence-gate assertion "
        "is vacuous"
    )
    assert result["unpersisted"] == 0, (
        f"{result['unpersisted']} released agents were NOT persisted at release time"
    )
    assert result["dropped"] > 0, "the smoke ran but never released a transcript"


def main():
    result = run_smoke()
    print(f"passes={result['passes']} churn_ops={result['churn_ops']} "
          f"dropped_total={result['dropped']} "
          f"unflushed_present={result['unflushed_present']} "
          f"elapsed={result['elapsed']:.1f}s")
    if result["errors"]:
        print(f"FAIL: {len(result['errors'])} errors, first: {result['errors'][0]}")
        sys.exit(1)
    if result["unflushed_present"] == 0:
        print("FAIL: no un-flushed fixture ever entered the cache — the gate "
              "assertion below is vacuous")
        sys.exit(1)
    if result["unpersisted"]:
        print(f"FAIL: {result['unpersisted']} released agents were NOT persisted at release time")
        sys.exit(1)

    print("OK: 300 sweeps under churn, no RuntimeError; all released agents were persisted")
    sys.exit(0)


if __name__ == "__main__":
    main()
