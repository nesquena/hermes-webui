"""Runtime regression tests for the WebUI's background MCP discovery helper.

Exercises the REAL production path — `api.streaming._run_mcp_discovery_background`
— with a fake discovery payload, verifying the ordering that the static tests
in `test_mcp_discovery_nonblocking.py` can only pin structurally:

1. The stream worker's bounded join waits for discovery to finish when the
   server is reachable-but-slow, so the agent snapshot that follows still
   sees the registered tools (first-turn completeness).
2. The join is BOUNDED: an unreachable/slow server does not stall the turn
   beyond the join window.
3. Threads are coalesced per profile home — rapid messages reuse one live
   discovery thread instead of accumulating daemons.
"""
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from api import streaming  # noqa: E402


def _make_discover(duration: float, release: threading.Event | None = None):
    """Return (discover_fn, started_flag) — discover_fn sleeps `duration` then exits."""
    started = threading.Event()

    def _discover():
        started.set()
        if duration:
            time.sleep(duration)
        if release is not None:
            release.wait(timeout=10)

    return _discover, started


class TestBoundedDiscoveryJoin:
    def test_join_waits_for_reachable_slow_server(self):
        """A server that connects within the window lands in THIS turn's snapshot.

        The helper's join must return only after discovery finished, so the
        agent build that follows sees the tools.
        """
        discover, started = _make_discover(0.4)
        t0 = time.monotonic()
        thread = streaming._run_mcp_discovery_background(
            "profile-a", discover, "t-wait", join_timeout=5.0
        )
        elapsed = time.monotonic() - t0
        assert started.is_set()
        assert elapsed >= 0.4, f"join returned before discovery finished: {elapsed:.2f}s"
        assert not thread.is_alive(), "discovery thread should have completed"

    def test_join_is_bounded_for_unreachable_server(self):
        """A server that never connects must not stall the turn past the window."""
        discover, started = _make_discover(10.0)
        t0 = time.monotonic()
        thread = streaming._run_mcp_discovery_background(
            "profile-b", discover, "t-bounded", join_timeout=0.5
        )
        elapsed = time.monotonic() - t0
        assert started.is_set()
        assert elapsed < 2.0, f"join blocked far past the bound: {elapsed:.2f}s"
        assert thread.is_alive(), (
            "slow discovery thread should still be running in the background"
        )
        thread.join(timeout=1.0)  # let the daemon finish; it sleeps 10s → join times out
        assert thread.is_alive()  # still sleeping; test ends, daemon dies with process


class TestCoalescing:
    def test_live_thread_reused_per_profile(self):
        """Two rapid messages for the same profile share ONE discovery thread."""
        discover, started = _make_discover(1.0)
        first = streaming._run_mcp_discovery_background(
            "profile-c", discover, "t-first", join_timeout=0.1
        )
        assert first.is_alive()
        second = streaming._run_mcp_discovery_background(
            "profile-c", discover, "t-second", join_timeout=0.1
        )
        assert second is first, "live thread for the same profile must be reused"
        first.join(timeout=3.0)

    def test_finished_thread_is_replaced(self):
        """A finished discovery thread is replaced by a fresh spawn."""
        discover, started = _make_discover(0.05)
        first = streaming._run_mcp_discovery_background(
            "profile-d", discover, "t-first", join_timeout=1.0
        )
        assert not first.is_alive()
        second = streaming._run_mcp_discovery_background(
            "profile-d", discover, "t-second", join_timeout=1.0
        )
        assert second is not first, "finished thread must be replaced"
