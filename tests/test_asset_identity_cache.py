"""Deterministic state/lifecycle coverage for shared static-tree freshness."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
from queue import Queue
import tempfile
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


class ObservedCondition(threading.Condition):
    """Observe actual waiter registration rather than guessing with sleeps."""
    def __init__(self):
        super().__init__(threading.Lock())
        self.wait_started = Queue()

    def wait(self, timeout=None):
        self.wait_started.put(threading.get_ident())
        return super().wait(timeout)

    def registered(self, count):
        seen = set()
        while len(seen) < count:
            seen.add(self.wait_started.get(timeout=5))


class AssetIdentityCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cache = AssetIdentityCache(clock=self.clock, wait_seconds=5)
        self.root = Path("static")

    def test_warm_result_reuses_completed_content_identity(self):
        calls = []
        compute = lambda root: calls.append(root) or "first"
        self.assertEqual(self.cache.get(self.root, compute), "first")
        for _ in range(100):
            self.assertEqual(self.cache.get(self.root, compute), "first")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.cache.snapshot()["cache_hits"], 100)

    def test_expiry_boundary_recomputes(self):
        self.assertEqual(self.cache.get(self.root, lambda _: "first"), "first")
        self.clock.advance(0.999)
        self.assertEqual(self.cache.get(self.root, lambda _: "second"), "first")
        self.clock.advance(0.001)
        self.assertEqual(self.cache.get(self.root, lambda _: "second"), "second")

    def test_freshness_starts_at_completion_not_scan_start(self):
        def slow_scan(_):
            self.clock.advance(30)
            return "first"
        self.cache.get(self.root, slow_scan)
        self.clock.advance(0.5)
        self.assertEqual(self.cache.get(self.root, lambda _: "second"), "first")
        self.assertEqual(self.cache.snapshot()["refreshes"], 1)

    def test_failure_is_shared_until_short_expiry_then_recovers(self):
        calls = []
        def fail(_):
            calls.append(1)
            raise PermissionError("do-not-export-this-path")
        self.assertIsNone(self.cache.get(self.root, fail))
        self.clock.advance(0.249)
        self.assertIsNone(self.cache.get(self.root, lambda _: "repaired"))
        self.clock.advance(0.001)
        self.assertEqual(self.cache.get(self.root, lambda _: "repaired"), "repaired")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.cache.snapshot()["refresh_failures"], 1)

    def test_ordinary_exception_classes_all_fail_closed(self):
        for error in (PermissionError, FileNotFoundError, OSError, RuntimeError):
            with self.subTest(error=error):
                cache = AssetIdentityCache()
                def fail(_, error=error):
                    raise error("private-detail")
                self.assertIsNone(cache.get(self.root, fail))
                self.assertEqual(cache.snapshot()["in_flight"], 0)
                self.assertEqual(cache.snapshot()["refresh_failures"], 1)

    def test_invalid_compute_result_cannot_authorize_identity(self):
        for value in (None, "", 10, {}, b"digest"):
            with self.subTest(value=value):
                cache = AssetIdentityCache()
                self.assertIsNone(cache.get(self.root, lambda _, value=value: value))
                self.assertEqual(cache.snapshot()["refresh_failures"], 1)

    def _burst(self, *, failure=False, expired=False):
        cache = self.cache
        observed = ObservedCondition()
        cache._condition = observed
        if expired:
            cache.get(self.root, lambda _: "old")
            self.clock.advance(1)
        started, release = threading.Event(), threading.Event()
        calls = []
        def scan(_):
            calls.append(1)
            started.set()
            if not release.wait(5):
                raise AssertionError("scan was not released")
            if failure:
                raise PermissionError("private-path")
            return "fresh"
        with ThreadPoolExecutor(max_workers=17) as pool:
            leader = pool.submit(cache.get, self.root, scan)
            try:
                self.assertTrue(started.wait(5))
                followers = [pool.submit(cache.get, self.root, scan) for _ in range(16)]
                observed.registered(16)
                self.assertEqual(cache.snapshot()["current_waiters"], 16)
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
            expected = None if failure else "fresh"
            self.assertEqual(leader.result(5), expected)
            for future in followers:
                self.assertEqual(future.result(5), expected)
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.snapshot()["shared_results"], 16)
        self.assertEqual(cache.snapshot()["current_waiters"], 0)
        self.assertEqual(cache.snapshot()["in_flight"], 0)

    def test_cold_burst_has_exactly_one_scan(self):
        self._burst()

    def test_expired_burst_has_exactly_one_scan(self):
        self._burst(expired=True)

    def test_failed_burst_has_exactly_one_scan(self):
        self._burst(failure=True)

    def test_hashing_runs_outside_coordination_lock(self):
        started, release = threading.Event(), threading.Event()
        def scan(_):
            started.set()
            self.assertTrue(release.wait(5))
            return "fresh"
        with ThreadPoolExecutor(max_workers=2) as pool:
            leader = pool.submit(self.cache.get, self.root, scan)
            try:
                self.assertTrue(started.wait(5))
                # This thread must acquire the same non-reentrant lock while
                # the scanning thread is still inside the filesystem callback.
                snap = pool.submit(self.cache.snapshot).result(2)
                self.assertEqual(snap["in_flight"], 1)
            finally:
                release.set()
            self.assertEqual(leader.result(5), "fresh")

    def test_waiter_cap_fails_closed_without_extra_scan(self):
        cache = AssetIdentityCache(max_waiters=2, wait_seconds=5)
        observed = ObservedCondition()
        cache._condition = observed
        started, release = threading.Event(), threading.Event()
        calls = []
        def scan(_):
            calls.append(1)
            started.set()
            self.assertTrue(release.wait(5))
            return "fresh"
        with ThreadPoolExecutor(max_workers=4) as pool:
            leader = pool.submit(cache.get, self.root, scan)
            try:
                self.assertTrue(started.wait(5))
                followers = [pool.submit(cache.get, self.root, scan) for _ in range(2)]
                observed.registered(2)
                self.assertIsNone(pool.submit(cache.get, self.root, scan).result(2))
                self.assertEqual(cache.snapshot()["peak_waiters"], 2)
                self.assertEqual(cache.snapshot()["wait_rejections"], 1)
                self.assertEqual(calls, [1])
            finally:
                release.set()
            self.assertEqual(leader.result(5), "fresh")
            self.assertEqual([f.result(5) for f in followers], ["fresh"] * 2)

    def test_wait_timeout_never_starts_a_replacement_scan(self):
        cache = AssetIdentityCache(wait_seconds=0.02, clock=self.clock)
        started, release = threading.Event(), threading.Event()
        calls = []
        def scan(_):
            calls.append(1)
            started.set()
            self.assertTrue(release.wait(5))
            return "fresh"
        with ThreadPoolExecutor(max_workers=1) as pool:
            leader = pool.submit(cache.get, self.root, scan)
            try:
                self.assertTrue(started.wait(5))
                self.assertIsNone(cache.get(self.root, scan))
                self.assertIsNone(cache.get(Path("other-root"), scan))
                self.assertEqual(cache.snapshot()["wait_timeouts"], 2)
                self.assertEqual(cache.snapshot()["current_waiters"], 0)
                self.assertEqual(cache.snapshot()["in_flight"], 1)
                self.assertEqual(calls, [1])
            finally:
                release.set()
            self.assertEqual(leader.result(5), "fresh")
        self.assertEqual(cache.get(self.root, lambda _: "wrong"), "fresh")

    def test_expired_success_is_not_served_on_timeout(self):
        cache = AssetIdentityCache(wait_seconds=0.02, clock=self.clock)
        cache.get(self.root, lambda _: "old")
        self.clock.advance(1)
        started, release = threading.Event(), threading.Event()
        def scan(_):
            started.set()
            self.assertTrue(release.wait(5))
            return "new"
        with ThreadPoolExecutor(max_workers=1) as pool:
            leader = pool.submit(cache.get, self.root, scan)
            try:
                self.assertTrue(started.wait(5))
                self.assertIsNone(cache.get(self.root, scan))
            finally:
                release.set()
            self.assertEqual(leader.result(5), "new")

    def test_base_exception_releases_waiters_and_allows_retry(self):
        observed = ObservedCondition()
        self.cache._condition = observed
        started, release = threading.Event(), threading.Event()
        def abort(_):
            started.set()
            self.assertTrue(release.wait(5))
            raise KeyboardInterrupt("test-only cancellation")
        with ThreadPoolExecutor(max_workers=2) as pool:
            leader = pool.submit(self.cache.get, self.root, abort)
            try:
                self.assertTrue(started.wait(5))
                follower = pool.submit(self.cache.get, self.root, lambda _: "bad")
                observed.registered(1)
            finally:
                release.set()
            with self.assertRaises(KeyboardInterrupt):
                leader.result(5)
            self.assertIsNone(follower.result(5))
        self.assertEqual(self.cache.snapshot()["in_flight"], 0)
        self.assertEqual(self.cache.snapshot()["current_waiters"], 0)
        self.clock.advance(0.25)
        self.assertEqual(self.cache.get(self.root, lambda _: "recovered"), "recovered")

    def test_different_roots_serialize_without_identity_leak(self):
        observed = ObservedCondition()
        self.cache._condition = observed
        started, release = threading.Event(), threading.Event()
        calls = []
        def first(_):
            calls.append("first-start")
            started.set()
            self.assertTrue(release.wait(5))
            calls.append("first-end")
            return "identity-a"
        def second(_):
            calls.append("second")
            return "identity-b"
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.cache.get, Path("a"), first)
            try:
                self.assertTrue(started.wait(5))
                b = pool.submit(self.cache.get, Path("b"), second)
                observed.registered(1)
                self.assertEqual(calls, ["first-start"])
            finally:
                release.set()
            self.assertEqual(a.result(5), "identity-a")
            self.assertEqual(b.result(5), "identity-b")
        self.assertEqual(calls, ["first-start", "first-end", "second"])

    def test_spurious_wakeup_does_not_start_another_scan(self):
        observed = ObservedCondition()
        self.cache._condition = observed
        started, release = threading.Event(), threading.Event()
        calls = []
        def scan(_):
            calls.append(1)
            started.set()
            self.assertTrue(release.wait(5))
            return "fresh"
        with ThreadPoolExecutor(max_workers=2) as pool:
            leader = pool.submit(self.cache.get, self.root, scan)
            try:
                self.assertTrue(started.wait(5))
                follower = pool.submit(self.cache.get, self.root, scan)
                observed.registered(1)
                with observed:
                    observed.notify_all()
                observed.registered(1)
                self.assertFalse(follower.done())
                self.assertEqual(calls, [1])
            finally:
                release.set()
            self.assertEqual(leader.result(5), "fresh")
            self.assertEqual(follower.result(5), "fresh")

    def test_bounded_root_entries(self):
        for index in range(100):
            self.cache.get(Path(f"root-{index}"), lambda root: str(root))
            self.assertLessEqual(self.cache.snapshot()["entries"], 4)
        self.assertEqual(self.cache.snapshot()["evictions"], 96)

    def test_failure_entries_also_obey_capacity(self):
        def fail(_):
            raise FileNotFoundError("no file")
        for index in range(100):
            self.cache.get(Path(f"missing-{index}"), fail)
        self.assertEqual(self.cache.snapshot()["entries"], 4)
        self.assertEqual(self.cache.snapshot()["evictions"], 96)

    def test_lru_reuse_and_eviction(self):
        cache = AssetIdentityCache(max_entries=2, clock=self.clock)
        for root in ("a", "b", "a", "c"):
            cache.get(Path(root), lambda root: str(root))
        self.assertEqual(cache.snapshot()["refreshes"], 3)
        cache.get(Path("a"), lambda _: "wrong")
        self.assertEqual(cache.snapshot()["refreshes"], 3)
        cache.get(Path("b"), lambda _: "b-again")
        self.assertEqual(cache.snapshot()["refreshes"], 4)

    def test_root_change_never_reuses_another_roots_identity(self):
        a, b = Path("a"), Path("b")
        self.assertEqual(self.cache.get(a, lambda _: "A"), "A")
        self.assertEqual(self.cache.get(b, lambda _: "B"), "B")
        self.assertEqual(self.cache.get(a, lambda _: "bad"), "A")

    def test_wall_clock_changes_do_not_affect_freshness(self):
        self.cache.get(self.root, lambda _: "A")
        with patch("time.time", return_value=-1e20):
            self.assertEqual(self.cache.get(self.root, lambda _: "bad"), "A")
        self.clock.advance(1)
        with patch("time.time", return_value=1e20):
            self.assertEqual(self.cache.get(self.root, lambda _: "B"), "B")

    def test_warm_hit_has_no_static_filesystem_operations(self):
        self.cache.get(self.root, lambda _: "A")
        with patch.object(Path, "resolve", side_effect=AssertionError("resolve")), \
             patch.object(Path, "stat", side_effect=AssertionError("stat")), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("read")), \
             patch("os.scandir", side_effect=AssertionError("walk")):
            self.assertEqual(self.cache.get(self.root, lambda _: "bad"), "A")

    def test_relative_and_absolute_paths_share(self):
        self.cache.get(self.root, lambda _: "A")
        self.assertEqual(self.cache.get(self.root.absolute(), lambda _: "bad"), "A")
        self.assertEqual(self.cache.snapshot()["refreshes"], 1)

    @unittest.skipUnless(os.name == "posix", "symlink traversal requires POSIX")
    def test_symlink_parent_traversal_is_not_lexically_collapsed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "a" / "assets").mkdir(parents=True)
            (base / "b" / "nested").mkdir(parents=True)
            (base / "b" / "assets").mkdir()
            (base / "a" / "link").symlink_to(base / "b" / "nested", target_is_directory=True)
            root = base / "a" / "link" / ".." / "assets"
            actual = self.cache.get(root, lambda path: str(path.resolve()))
            self.assertEqual(actual, str(base / "b" / "assets"))

    def test_telemetry_is_fixed_cardinality_and_detached(self):
        self.cache.get(Path("private-root-not-for-metrics"), lambda _: "private-digest")
        snap = self.cache.snapshot()
        self.assertEqual(snap["requests"], 1)
        self.assertEqual(snap["refresh_successes"], 1)
        self.assertEqual(snap["entries"], 1)
        self.assertGreaterEqual(snap["total_refresh_seconds"], 0)
        self.assertNotIn("private", repr(snap))
        self.assertTrue(all(isinstance(v, (int, float)) for v in snap.values()))
        snap["requests"] = -1
        self.assertEqual(self.cache.snapshot()["requests"], 1)
        keys = set(snap)
        for index in range(20):
            self.cache.get(Path(str(index)), lambda _: "digest")
        self.assertEqual(set(self.cache.snapshot()), keys)

    def test_failed_root_does_not_poison_another_completed_root(self):
        self.cache.get(Path("healthy"), lambda _: "healthy-digest")
        def fail(_):
            raise OSError("failure")
        self.assertIsNone(self.cache.get(Path("failed"), fail))
        self.assertEqual(self.cache.get(Path("healthy"), lambda _: "wrong"), "healthy-digest")

    def test_publication_failure_also_releases_the_flight(self):
        calls = [0]
        def broken_clock():
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("clock failed during publication")
            return 100.0
        cache = AssetIdentityCache(clock=broken_clock)
        with self.assertRaisesRegex(RuntimeError, "clock failed"):
            cache.get(self.root, lambda _: "unpublished")
        self.assertEqual(cache.snapshot()["in_flight"], 0)
        self.assertEqual(cache.snapshot()["entries"], 0)
        self.assertEqual(cache.get(self.root, lambda _: "retry"), "retry")

    def test_interrupted_wait_releases_waiter_slot_not_leader(self):
        class InterruptedCondition(threading.Condition):
            def wait(self, timeout=None):
                raise KeyboardInterrupt("follower interrupted")
        self.cache._condition = InterruptedCondition(threading.Lock())
        started, release = threading.Event(), threading.Event()
        def scan(_):
            started.set()
            self.assertTrue(release.wait(5))
            return "finished"
        with ThreadPoolExecutor(max_workers=1) as pool:
            leader = pool.submit(self.cache.get, self.root, scan)
            try:
                self.assertTrue(started.wait(5))
                with self.assertRaises(KeyboardInterrupt):
                    self.cache.get(self.root, lambda _: "wrong")
                self.assertEqual(self.cache.snapshot()["current_waiters"], 0)
                self.assertEqual(self.cache.snapshot()["in_flight"], 1)
            finally:
                release.set()
            self.assertEqual(leader.result(5), "finished")

    def test_invalid_limits_rejected(self):
        for option in ("freshness_seconds", "failure_seconds", "wait_seconds"):
            for value in (0, -1, float("nan"), float("inf")):
                with self.subTest(option=option, value=value), self.assertRaises(ValueError):
                    AssetIdentityCache(**{option: value})
        for option in ("max_entries", "max_waiters"):
            for value in (0, -1, 1.5):
                with self.subTest(option=option, value=value), self.assertRaises(ValueError):
                    AssetIdentityCache(**{option: value})


if __name__ == "__main__":
    unittest.main()
