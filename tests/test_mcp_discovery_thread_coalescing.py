"""Runtime regression tests for the WebUI's profile-scoped MCP readiness.

Exercises the REAL production path — `api.streaming._ensure_mcp_discovery` /
`_mcp_wait_readiness` / `_mcp_retry_discovery` — with fake discovery payloads.
These map 1:1 onto the readiness contract the maintainer review demanded:

1. A discovery that finishes AFTER any fixed-timeout guess (e.g. 4s) must
   still be present for the first tool-bearing turn — the turn waits on the
   shared readiness event, not a timed join.
2. Several same-profile turns while one discovery is pending must share ONE
   owner thread and resolve on the SAME event, not each pay a fresh wait.
3. Different profiles keep independent readiness/tool registries.
4. Failure has an explicit surfaced outcome (status == 'failed'), and a
   later explicit retry completes without touching an in-flight Agent
   snapshot (hermes-agent only applies tools at the next turn's prologue).
"""
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from api import streaming  # noqa: E402


def _make_discover(duration: float, fail: bool = False):
    """Return (discover_fn, started_flag)."""
    started = threading.Event()

    def _discover():
        started.set()
        if fail:
            time.sleep(duration or 0)
            raise RuntimeError("simulated connect failure")
        if duration:
            time.sleep(duration)

    return _discover, started


class TestFirstTurnCompleteness:
    def test_turn_waits_for_discovery_past_old_4s_boundary(self):
        """A server connecting at 4.5s must not be omitted from the first turn.

        The old bounded-join design cut off at a fixed 4.0s; the readiness
        contract waits on the shared event until discovery actually
        completes, so the agent snapshot that follows sees the tools.
        """
        discover, started = _make_discover(4.5)
        readiness = streaming._ensure_mcp_discovery(
            "profile-a", discover, "t-slow"
        )
        t0 = time.monotonic()
        status = streaming._mcp_wait_readiness(readiness)
        elapsed = time.monotonic() - t0
        assert started.is_set()
        assert status == "completed"
        assert elapsed >= 4.5, (
            f"readiness resolved before discovery finished: {elapsed:.2f}s"
        )

    def test_completed_readiness_does_not_wait(self):
        """A resolved profile returns immediately — no per-turn wait."""
        discover, started = _make_discover(0.05)
        readiness = streaming._ensure_mcp_discovery(
            "profile-a2", discover, "t-fast"
        )
        assert streaming._mcp_wait_readiness(readiness) == "completed"
        t0 = time.monotonic()
        assert streaming._mcp_wait_readiness(readiness) == "completed"
        assert time.monotonic() - t0 < 0.2, (
            "a completed profile must never make the turn wait again"
        )


class TestSharedReadiness:
    def test_same_profile_turns_share_one_thread_and_one_wait(self):
        """Concurrent same-profile turns: ONE thread, ONE shared wait."""
        discover, started = _make_discover(1.5)
        r1 = streaming._ensure_mcp_discovery("profile-b", discover, "t1")
        r2 = streaming._ensure_mcp_discovery("profile-b", discover, "t2")
        assert r1.thread is r2.thread, "both turns must share the owner thread"
        t0 = time.monotonic()
        s1 = streaming._mcp_wait_readiness(r1)
        s2 = streaming._mcp_wait_readiness(r2)
        elapsed = time.monotonic() - t0
        assert s1 == s2 == "completed"
        assert elapsed < 3.0, (
            f"two turns paid ~2x the discovery wait: {elapsed:.2f}s"
        )

    def test_completed_profile_is_not_re_discovered(self):
        """A completed profile must not be re-run by a later turn."""
        runs = []

        def _discover():
            runs.append(1)

        r = streaming._ensure_mcp_discovery("profile-b2", _discover, "t")
        assert streaming._mcp_wait_readiness(r) == "completed"
        streaming._ensure_mcp_discovery("profile-b2", _discover, "t2")
        assert len(runs) == 1, "completed profile must not re-run discovery"


class TestProfileIsolation:
    def test_different_profiles_independent(self):
        """Different profiles keep independent readiness and threads."""
        d1, s1 = _make_discover(0.3)
        d2, s2 = _make_discover(0.6)
        ra = streaming._ensure_mcp_discovery("profile-x", d1, "tx")
        rb = streaming._ensure_mcp_discovery("profile-y", d2, "ty")
        assert ra is not rb
        assert ra.thread is not rb.thread
        assert streaming._mcp_wait_readiness(ra) == "completed"
        assert streaming._mcp_wait_readiness(rb) == "completed"


class TestFailureAndRetry:
    def test_failure_surfaces_explicit_status(self):
        """A failed discovery must surface status='failed', never hang."""
        discover, started = _make_discover(0.1, fail=True)
        readiness = streaming._ensure_mcp_discovery("profile-z", discover, "tz")
        assert streaming._mcp_wait_readiness(readiness) == "failed"

    def test_explicit_retry_after_failure_completes(self):
        """An explicit retry re-runs discovery and completes."""
        fail_disc, _ = _make_discover(0.05, fail=True)
        r = streaming._ensure_mcp_discovery("profile-w", fail_disc, "tw1")
        assert streaming._mcp_wait_readiness(r) == "failed"
        old_thread = r.thread
        ok_disc, ok_started = _make_discover(0.05)
        r2 = streaming._mcp_retry_discovery("profile-w", ok_disc, "tw2")
        assert r2.thread is not old_thread, "retry must run a fresh owner thread"
        assert streaming._mcp_wait_readiness(r2) == "completed"
        assert ok_started.is_set()
