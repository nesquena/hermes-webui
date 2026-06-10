"""Tests for Issue #3787: Probe-worker pool tail-latency.

Focus on:
- Per-home worker pool size N=2
- Non-blocking acquire returns an idle worker when available
- Returns None when all workers are locked and timeout expires
- Scoped invalidation only flushes the active home's workers
- Full invalidation (no provider_id) flushes all workers
- Cleanup iterates nested worker lists correctly
"""

import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from api.providers import (
    _ACCOUNT_USAGE_WORKERS_PER_HOME,
    _ACCOUNT_USAGE_WORKER_WAIT_SECONDS,
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

        with _account_usage_worker_pool_lock:
            key = str(Path(home))
            workers_list = _account_usage_worker_pool.get(key)
            self.assertIsNotNone(workers_list)
            self.assertEqual(len(workers_list), _ACCOUNT_USAGE_WORKERS_PER_HOME)
            self.assertEqual(_ACCOUNT_USAGE_WORKERS_PER_HOME, 2)

    def test_nonblocking_acquire_returns_idle_worker(self):
        """Non-blocking acquire should return an idle worker when available."""
        home = Path("/tmp/test_nonblocking_idle")

        # First, populate the pool with two workers
        worker_initial = _get_account_usage_probe_worker(home)
        self.assertIsNotNone(worker_initial)

        # Get reference to the actual workers in pool
        with _account_usage_worker_pool_lock:
            key = str(Path(home))
            workers = _account_usage_worker_pool[key]
            self.assertEqual(len(workers), 2)

        # Hold first worker locked from a background thread to simulate actual use
        lock_holder = threading.Event()
        release_signal = threading.Event()

        def hold_lock():
            workers[0]._lock.acquire()
            lock_holder.set()
            release_signal.wait(timeout=2.0)
            workers[0]._lock.release()

        thread = threading.Thread(target=hold_lock, daemon=True)
        thread.start()
        lock_holder.wait(timeout=2.0)  # Wait until background thread has the lock

        try:
            # Get a worker - should pick the idle one (workers[1])
            worker = _get_account_usage_probe_worker(home)
            self.assertIsNotNone(worker)
            # Worker should be the second one since first is locked
            self.assertIs(worker, workers[1])
        finally:
            release_signal.set()
            thread.join(timeout=1.0)

    def test_returns_none_when_all_workers_locked_and_timeout_expires(self):
        """Should return None when all workers locked and timeout expires."""
        home = Path("/tmp/test_timeout_none")

        # First populate the pool
        worker_initial = _get_account_usage_probe_worker(home)
        self.assertIsNotNone(worker_initial)

        # Get reference to the workers
        with _account_usage_worker_pool_lock:
            key = str(Path(home))
            workers = _account_usage_worker_pool[key]
            self.assertEqual(len(workers), 2)

        # Hold both workers locked from background threads
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
        lock_holder.wait(timeout=2.0)  # Wait until background thread has both locks

        try:
            # Now attempt to get a worker; should timeout and return None
            start = time.time()
            result = _get_account_usage_probe_worker(home)
            elapsed = time.time() - start

            self.assertIsNone(result)
            # Verify timeout was respected (within a small margin)
            self.assertGreaterEqual(elapsed, _ACCOUNT_USAGE_WORKER_WAIT_SECONDS - 0.1)
        finally:
            release_signal.set()
            thread.join(timeout=1.0)

    def test_scoped_invalidation_only_flushes_active_home(self):
        """Scoped invalidation with provider_id should only flush active home."""
        home1 = Path.home() / ".hermes"
        home2 = Path("/tmp/other_home")

        # Create workers for both homes
        worker1 = _get_account_usage_probe_worker(home1)
        worker2 = _get_account_usage_probe_worker(home2)

        self.assertIsNotNone(worker1)
        self.assertIsNotNone(worker2)

        with _account_usage_worker_pool_lock:
            initial_count = len(_account_usage_worker_pool)
            self.assertEqual(initial_count, 2)

        # Mock _get_hermes_home to return home1
        with mock.patch("api.providers._get_hermes_home", return_value=home1):
            invalidate_account_usage_status_cache(provider_id="anthropic")
            # Give async thread time to complete
            time.sleep(0.2)

        with _account_usage_worker_pool_lock:
            remaining_keys = list(_account_usage_worker_pool.keys())
            # home1 should be flushed, home2 should remain
            self.assertNotIn(str(Path(home1)), remaining_keys)
            self.assertIn(str(Path(home2)), remaining_keys)

    def test_full_invalidation_flushes_all_workers(self):
        """Full invalidation (no provider_id) should flush all workers."""
        home1 = Path.home() / ".hermes"
        home2 = Path("/tmp/other_home")

        # Create workers for both homes
        worker1 = _get_account_usage_probe_worker(home1)
        worker2 = _get_account_usage_probe_worker(home2)

        self.assertIsNotNone(worker1)
        self.assertIsNotNone(worker2)

        with _account_usage_worker_pool_lock:
            self.assertEqual(len(_account_usage_worker_pool), 2)

        # Full invalidation with no provider_id
        invalidate_account_usage_status_cache(provider_id=None)
        # Give async thread time to complete
        time.sleep(0.2)

        with _account_usage_worker_pool_lock:
            self.assertEqual(len(_account_usage_worker_pool), 0)

    def test_cleanup_iterates_nested_worker_lists(self):
        """Cleanup should properly iterate over nested worker lists."""
        home = Path.home() / ".hermes"

        # Create workers
        worker = _get_account_usage_probe_worker(home)
        self.assertIsNotNone(worker)

        with _account_usage_worker_pool_lock:
            key = str(Path(home))
            workers_list = _account_usage_worker_pool.get(key)
            self.assertEqual(len(workers_list), 2)

        # Run cleanup with future timestamp (should mark workers as stale)
        now = time.monotonic() + 100000  # Far in the future
        _cleanup_account_usage_probe_workers(now=now, idle_seconds=1.0)

        # Pool should be cleared of stale workers
        with _account_usage_worker_pool_lock:
            if _account_usage_worker_pool.get(str(Path(home))):
                # If key still exists, it should have no workers
                remaining = _account_usage_worker_pool.get(str(Path(home)), [])
                self.assertEqual(len(remaining), 0)
            else:
                # Key should be removed entirely if no workers
                self.assertNotIn(str(Path(home)), _account_usage_worker_pool)

    def test_synchronous_close_flattens_nested_lists(self):
        """Synchronous close should flatten nested lists correctly."""
        home = Path.home() / ".hermes"

        # Create workers
        worker = _get_account_usage_probe_worker(home)
        self.assertIsNotNone(worker)

        with _account_usage_worker_pool_lock:
            key = str(Path(home))
            workers_list = _account_usage_worker_pool.get(key)
            self.assertEqual(len(workers_list), 2)

        # Close all workers synchronously
        _close_account_usage_probe_workers()

        # Pool should be empty
        with _account_usage_worker_pool_lock:
            self.assertEqual(len(_account_usage_worker_pool), 0)

    def test_multiple_gets_return_different_idle_workers(self):
        """Multiple rapid gets should return different workers when available."""
        home = Path("/tmp/test_multiple_gets")

        # First populate the pool
        worker_initial = _get_account_usage_probe_worker(home)
        self.assertIsNotNone(worker_initial)

        # Get reference to the workers
        with _account_usage_worker_pool_lock:
            key = str(Path(home))
            workers = _account_usage_worker_pool[key]
            self.assertEqual(len(workers), 2)

        # Hold first worker locked from background thread
        lock_holder1 = threading.Event()
        release_signal1 = threading.Event()

        def hold_lock1():
            workers[0]._lock.acquire()
            lock_holder1.set()
            release_signal1.wait(timeout=2.0)
            workers[0]._lock.release()

        thread1 = threading.Thread(target=hold_lock1, daemon=True)
        thread1.start()
        lock_holder1.wait(timeout=2.0)

        try:
            # First call should get workers[1] (idle)
            worker1 = _get_account_usage_probe_worker(home)
            self.assertIsNotNone(worker1)
            self.assertIs(worker1, workers[1])

            # Lock workers[1] too from another background thread, then release workers[0]
            lock_holder2 = threading.Event()
            release_signal2 = threading.Event()

            def hold_lock2():
                workers[1]._lock.acquire()
                lock_holder2.set()
                release_signal2.wait(timeout=2.0)
                workers[1]._lock.release()

            thread2 = threading.Thread(target=hold_lock2, daemon=True)
            thread2.start()
            lock_holder2.wait(timeout=2.0)

            # Now release workers[0] but keep workers[1] locked
            release_signal1.set()
            thread1.join(timeout=1.0)

            try:
                # Second call should timeout waiting for workers[1], then get workers[0] which is now free
                # But the timeout in _get_account_usage_probe_worker is 0.5s, and workers[1] holds for 2s,
                # so it should return workers[0] after the timeout
                worker2 = _get_account_usage_probe_worker(home)
                self.assertIsNotNone(worker2)
                # Should get workers[0] since workers[1] is still locked
                self.assertIs(worker2, workers[0])
            finally:
                release_signal2.set()
                thread2.join(timeout=1.0)
        except Exception:
            release_signal1.set()
            thread1.join(timeout=1.0)
            raise


class TestWorkerPoolConfiguration(unittest.TestCase):
    """Test that constants are properly configured."""

    def test_workers_per_home_is_two(self):
        """Should have exactly 2 workers per home."""
        self.assertEqual(_ACCOUNT_USAGE_WORKERS_PER_HOME, 2)

    def test_worker_wait_seconds_is_half(self):
        """Should wait 0.5 seconds for worker availability."""
        self.assertEqual(_ACCOUNT_USAGE_WORKER_WAIT_SECONDS, 0.5)


if __name__ == "__main__":
    unittest.main()
