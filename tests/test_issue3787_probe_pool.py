"""Tests for Issue #3787: Probe-worker pool tail-latency.

Focus on:
- Per-home worker pool size N=2
- Non-blocking acquire returns an idle worker when available
- Returns None immediately when all workers are locked (no blocking wait)
- Scoped invalidation only flushes the active home's workers
- Full invalidation (no provider_id) flushes all workers
- Cleanup iterates nested worker lists correctly
- Lock held across selector handoff prevents TOCTOU races
"""

import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from api.providers import (
    _ACCOUNT_USAGE_WORKERS_PER_HOME,
    _account_usage_worker_pool,
    _account_usage_worker_pool_lock,
    _cleanup_account_usage_probe_workers,
    _close_account_usage_probe_workers,
    _get_account_usage_probe_worker,
    invalidate_account_usage_status_cache,
)


class TestProbeWorkerPoolPerHome(unittest.TestCase):
    """Test per-home worker pool with N=2 configuration."""

    def setUp(self):
        """Clear the pool before each test."""
        with _account_usage_worker_pool_lock:
            _account_usage_worker_pool.clear()

    def tearDown(self):
        """Clean up after each test."""
        with _account_usage_worker_pool_lock:
            _account_usage_worker_pool.clear()

    def test_pool_creates_two_workers_per_home_key(self):
        """Pool should create exactly N=2 workers for each home key."""
        home = Path.home() / ".hermes"
        worker = _get_account_usage_probe_worker(home)
        self.assertIsNotNone(worker)
        worker._lock.release()

        with _account_usage_worker_pool_lock:
            key = str(Path(home))
            workers_list = _account_usage_worker_pool.get(key)
            self.assertIsNotNone(workers_list)
            self.assertEqual(len(workers_list), _ACCOUNT_USAGE_WORKERS_PER_HOME)
            self.assertEqual(_ACCOUNT_USAGE_WORKERS_PER_HOME, 2)

    def test_nonblocking_acquire_returns_idle_worker(self):
        """Non-blocking acquire should return an idle worker when one is free."""
        home = Path("/tmp/test_nonblocking_idle")

        # Populate the pool
        worker_initial = _get_account_usage_probe_worker(home)
        self.assertIsNotNone(worker_initial)
        worker_initial._lock.release()

        with _account_usage_worker_pool_lock:
            key = str(Path(home))
            workers = _account_usage_worker_pool[key]
            self.assertEqual(len(workers), 2)

        # Hold workers[0] locked from a background thread to simulate actual use
        lock_holder = threading.Event()
        release_signal = threading.Event()

        def hold_lock():
            workers[0]._lock.acquire()
            lock_holder.set()
            release_signal.wait(timeout=2.0)
            workers[0]._lock.release()

        thread = threading.Thread(target=hold_lock, daemon=True)
        thread.start()
        lock_holder.wait(timeout=2.0)

        try:
            worker = _get_account_usage_probe_worker(home)
            self.assertIsNotNone(worker)
            self.assertIs(worker, workers[1])
            worker._lock.release()
        finally:
            release_signal.set()
            thread.join(timeout=1.0)

    def test_returns_none_when_all_workers_locked(self):
        """Should return None immediately when all workers are locked from other threads."""
        home = Path("/tmp/test_immediate_none")

        # Populate the pool
        worker_initial = _get_account_usage_probe_worker(home)
        self.assertIsNotNone(worker_initial)
        worker_initial._lock.release()

        with _account_usage_worker_pool_lock:
            key = str(Path(home))
            workers = _account_usage_worker_pool[key]

        # Hold both workers locked from a background thread
        lock_holder = threading.Event()
        release_signal = threading.Event()

        def hold_locks():
            workers[0]._lock.acquire()
            workers[1]._lock.acquire()
            lock_holder.set()
            release_signal.wait(timeout=2.0)
            workers[1]._lock.release()
            workers[0]._lock.release()

        thread = threading.Thread(target=hold_locks, daemon=True)
        thread.start()
        lock_holder.wait(timeout=2.0)

        try:
            start = time.time()
            result = _get_account_usage_probe_worker(home)
            elapsed = time.time() - start

            self.assertIsNone(result)
            # Should return immediately, not block
            self.assertLess(elapsed, 0.1)
        finally:
            release_signal.set()
            thread.join(timeout=1.0)

    def test_scoped_invalidation_only_flushes_active_home(self):
        """Scoped invalidation with provider_id should only flush active home."""
        home1 = Path.home() / ".hermes"
        home2 = Path("/tmp/other_home")

        worker1 = _get_account_usage_probe_worker(home1)
        worker2 = _get_account_usage_probe_worker(home2)

        self.assertIsNotNone(worker1)
        self.assertIsNotNone(worker2)
        worker1._lock.release()
        worker2._lock.release()

        with _account_usage_worker_pool_lock:
            initial_count = len(_account_usage_worker_pool)
            self.assertEqual(initial_count, 2)

        with mock.patch("api.providers._get_hermes_home", return_value=home1):
            invalidate_account_usage_status_cache(provider_id="anthropic")
            time.sleep(0.2)

        with _account_usage_worker_pool_lock:
            remaining_keys = list(_account_usage_worker_pool.keys())
            self.assertNotIn(str(Path(home1)), remaining_keys)
            self.assertIn(str(Path(home2)), remaining_keys)

    def test_full_invalidation_flushes_all_workers(self):
        """Full invalidation (no provider_id) should flush all workers."""
        home1 = Path.home() / ".hermes"
        home2 = Path("/tmp/other_home")

        worker1 = _get_account_usage_probe_worker(home1)
        worker2 = _get_account_usage_probe_worker(home2)

        self.assertIsNotNone(worker1)
        self.assertIsNotNone(worker2)
        worker1._lock.release()
        worker2._lock.release()

        with _account_usage_worker_pool_lock:
            self.assertEqual(len(_account_usage_worker_pool), 2)

        invalidate_account_usage_status_cache(provider_id=None)
        time.sleep(0.2)

        with _account_usage_worker_pool_lock:
            self.assertEqual(len(_account_usage_worker_pool), 0)

    def test_cleanup_iterates_nested_worker_lists(self):
        """Cleanup should properly iterate over nested worker lists."""
        home = Path.home() / ".hermes"

        worker = _get_account_usage_probe_worker(home)
        self.assertIsNotNone(worker)
        worker._lock.release()

        with _account_usage_worker_pool_lock:
            key = str(Path(home))
            workers_list = _account_usage_worker_pool.get(key)
            self.assertEqual(len(workers_list), 2)

        now = time.monotonic() + 100000
        _cleanup_account_usage_probe_workers(now=now, idle_seconds=1.0)

        with _account_usage_worker_pool_lock:
            if _account_usage_worker_pool.get(str(Path(home))):
                remaining = _account_usage_worker_pool.get(str(Path(home)), [])
                self.assertEqual(len(remaining), 0)
            else:
                self.assertNotIn(str(Path(home)), _account_usage_worker_pool)

    def test_partial_cleanup_replenishes_pool(self):
        """When cleanup removes one stale worker but the other is busy, pool replenishes to N=2."""
        home = Path("/tmp/test_replenish")

        worker = _get_account_usage_probe_worker(home)
        self.assertIsNotNone(worker)
        worker._lock.release()

        with _account_usage_worker_pool_lock:
            key = str(Path(home))
            workers = _account_usage_worker_pool[key]
            self.assertEqual(len(workers), 2)

        # Hold workers[1] locked from a background thread (simulating active use)
        lock_holder = threading.Event()
        release_signal = threading.Event()

        def hold_lock():
            workers[1]._lock.acquire()
            lock_holder.set()
            release_signal.wait(timeout=5.0)
            workers[1]._lock.release()

        thread = threading.Thread(target=hold_lock, daemon=True)
        thread.start()
        lock_holder.wait(timeout=2.0)

        try:
            # Run cleanup far in the future; workers[0] is idle and stale,
            # workers[1] is locked so it survives
            now = time.monotonic() + 100000
            _cleanup_account_usage_probe_workers(now=now, idle_seconds=1.0)

            with _account_usage_worker_pool_lock:
                remaining = _account_usage_worker_pool.get(key, [])
                # Pool should be replenished back to 2
                self.assertEqual(len(remaining), _ACCOUNT_USAGE_WORKERS_PER_HOME)
                # The original busy worker should still be present
                self.assertIn(workers[1], remaining)
        finally:
            release_signal.set()
            thread.join(timeout=1.0)

    def test_synchronous_close_flattens_nested_lists(self):
        """Synchronous close should flatten nested lists correctly."""
        home = Path.home() / ".hermes"

        worker = _get_account_usage_probe_worker(home)
        self.assertIsNotNone(worker)
        worker._lock.release()

        with _account_usage_worker_pool_lock:
            key = str(Path(home))
            workers_list = _account_usage_worker_pool.get(key)
            self.assertEqual(len(workers_list), 2)

        _close_account_usage_probe_workers()

        with _account_usage_worker_pool_lock:
            self.assertEqual(len(_account_usage_worker_pool), 0)

    def test_concurrent_selector_no_double_claim(self):
        """Two threads holding returned workers simultaneously never share the same instance."""
        home = Path("/tmp/test_concurrent")

        # Pre-populate pool
        initial = _get_account_usage_probe_worker(home)
        self.assertIsNotNone(initial)
        initial._lock.release()

        with _account_usage_worker_pool_lock:
            workers = _account_usage_worker_pool[str(Path(home))]

        # Lock workers[0] from a background thread so only workers[1] is free
        lock_holder = threading.Event()
        release_signal = threading.Event()

        def hold_first():
            workers[0]._lock.acquire()
            lock_holder.set()
            release_signal.wait(timeout=5.0)
            workers[0]._lock.release()

        holder_thread = threading.Thread(target=hold_first, daemon=True)
        holder_thread.start()
        lock_holder.wait(timeout=2.0)

        # Two threads race; each holds its worker until both have finished grabbing
        results = [None, None]
        barrier = threading.Barrier(2, timeout=2.0)
        both_done = threading.Barrier(2, timeout=2.0)

        def grab(idx):
            barrier.wait()
            w = _get_account_usage_probe_worker(home)
            results[idx] = w
            try:
                both_done.wait()
            except threading.BrokenBarrierError:
                pass
            if w is not None:
                w._lock.release()

        t0 = threading.Thread(target=grab, args=(0,), daemon=True)
        t1 = threading.Thread(target=grab, args=(1,), daemon=True)
        t0.start()
        t1.start()
        t0.join(timeout=3.0)
        t1.join(timeout=3.0)

        try:
            got = [r for r in results if r is not None]
            # At most one thread should have gotten a worker (workers[1]);
            # the other gets None because the lock is already held
            self.assertEqual(len(got), 1, "Expected exactly one winner, got %d" % len(got))
            self.assertIs(got[0], workers[1])
        finally:
            release_signal.set()
            holder_thread.join(timeout=1.0)


class TestWorkerPoolConfiguration(unittest.TestCase):
    """Test that constants are properly configured."""

    def test_workers_per_home_is_two(self):
        """Should have exactly 2 workers per home."""
        self.assertEqual(_ACCOUNT_USAGE_WORKERS_PER_HOME, 2)


if __name__ == "__main__":
    unittest.main()
