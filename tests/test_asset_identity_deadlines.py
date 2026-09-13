"""Delayed follower freshness and absolute-wait-deadline regressions."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from api.asset_identity_cache import AssetIdentityCache


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds=1.0):
        self.now += seconds


class AssetIdentityDeadlineTests(unittest.TestCase):
    """Model a notified follower not being scheduled until after a deadline."""

    def delayed_follower(self, *, other_root=False, expire_freshness=False,
                         expire_wait=False, failure=False):
        from types import SimpleNamespace
        import api.asset_identity_cache as cache_module

        fresh_clock, wait_clock = Clock(), Clock()
        cache = AssetIdentityCache(clock=fresh_clock, wait_seconds=5)
        started, release, waiting = (threading.Event() for _ in range(3))
        calls = []

        class DelayedCondition(threading.Condition):
            def wait(self, timeout=None):
                waiting.set()
                return super().wait(timeout)

            def wait_for(self, predicate, timeout=None):
                completed = super().wait_for(predicate, timeout)
                if completed:
                    # Completion really happened. Advance the clocks to model
                    # scheduling/lock contention before this follower resumes.
                    if expire_freshness:
                        fresh_clock.advance(2)
                    if expire_wait:
                        wait_clock.advance(6)
                return completed

        cache._condition = DelayedCondition(threading.Lock())

        def compute(root):
            calls.append(root.name)
            started.set()
            if not release.wait(5):
                raise AssertionError("test failed to release leader")
            if failure:
                raise PermissionError("controlled scan failure")
            return "fresh"

        # Patch this module's clock handle, not threading's real timeout clock.
        with patch.object(cache_module, "time", SimpleNamespace(monotonic=wait_clock)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                leader = pool.submit(cache.get, Path("a"), compute)
                try:
                    self.assertTrue(started.wait(5))
                    follower = pool.submit(cache.get, Path("b" if other_root else "a"), compute)
                    self.assertTrue(waiting.wait(5))
                finally:
                    release.set()
                self.assertEqual(leader.result(5), None if failure else "fresh")
                result = follower.result(5)
        return result, calls, cache.snapshot()

    def test_completed_generation_cannot_bypass_freshness(self):
        result, calls, stats = self.delayed_follower(expire_freshness=True)
        self.assertIsNone(result, "a delayed follower received an expired identity")
        self.assertEqual(calls, ["a"], "expiry must not turn followers into new scans")
        self.assertEqual(stats.get("expired_shared_results", 0), 1)
        self.assertEqual(stats["current_waiters"], 0)
        self.assertEqual(stats["in_flight"], 0)

    def test_completed_generation_cannot_bypass_wait_budget(self):
        result, calls, stats = self.delayed_follower(expire_wait=True)
        self.assertIsNone(result, "completion must not erase the follower deadline")
        self.assertEqual(calls, ["a"])
        self.assertEqual(stats["wait_timeouts"], 1)

    def test_other_root_cannot_start_hashing_after_wait_budget(self):
        result, calls, stats = self.delayed_follower(other_root=True, expire_wait=True)
        self.assertIsNone(result, "expired cross-root follower must not become a leader")
        self.assertEqual(calls, ["a"], "a second root scanned after its wait deadline")
        self.assertEqual(stats["wait_timeouts"], 1)

    def test_unexpired_completed_generation_is_shared(self):
        result, calls, stats = self.delayed_follower()
        self.assertEqual(result, "fresh")
        self.assertEqual(calls, ["a"])
        self.assertEqual(stats["shared_results"], 1)

    def test_cross_root_follower_with_budget_can_scan_its_own_root(self):
        result, calls, stats = self.delayed_follower(other_root=True)
        self.assertEqual(result, "fresh")
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(stats["refreshes"], 2)

    def test_delayed_negative_generation_still_fails_closed(self):
        result, calls, stats = self.delayed_follower(expire_freshness=True, failure=True)
        self.assertIsNone(result)
        self.assertEqual(calls, ["a"])
        self.assertEqual(stats.get("expired_shared_results", 0), 1)
        self.assertEqual(stats["refresh_failures"], 1)

    def test_boolean_entry_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            AssetIdentityCache(max_entries=True)

    def test_boolean_waiter_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            AssetIdentityCache(max_waiters=True)


if __name__ == "__main__":
    unittest.main()
