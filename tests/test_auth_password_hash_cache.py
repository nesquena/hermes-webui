"""
Tests for get_password_hash() caching (env-var path).

get_password_hash() calls PBKDF2-SHA256 with 600k iterations, which takes
~1 second per invocation.  When HERMES_WEBUI_PASSWORD is set via env var,
the hash never changes during the process lifetime, so the result should
be computed once and cached.

Performance regression: without caching, every HTTP request pays ~1s for
PBKDF2 (check_auth -> is_auth_enabled -> get_password_hash), causing
multi-second API response times.

Thread-safety: under a burst of concurrent requests, only one thread must
compute PBKDF2.  Double-checked locking ensures the others wait and receive
the cached result.
"""
import importlib
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

# Isolate state dir from production — only affects the auth module reload.
# We deliberately do NOT delete api.config from sys.modules (unlike some
# sibling test files that need a fresh config import).  Deleting api.config
# would change its module-level STATE_DIR global and leak into all
# subsequently collected tests (breaking test_pytest_state_isolation.py).
_TEST_STATE = Path(tempfile.mkdtemp())
os.environ["HERMES_WEBUI_STATE_DIR"] = str(_TEST_STATE)

sys.path.insert(0, str(Path(__file__).parent.parent))

# Force a fresh import of the auth module so it picks up the isolated env var.
# The auth module re-executes `from api.config import STATE_DIR, load_settings`
# at import time, but api.config is already in sys.modules — Python just
# rebinds the names from the existing module, keeping the conftest STATE_DIR
# untouched.
import api.auth
importlib.reload(api.auth)
auth = api.auth


class TestPasswordHashCache(unittest.TestCase):
    """Verify that get_password_hash() caches after first computation."""

    def setUp(self):
        # Reset the module-level cache state
        auth._AUTH_HASH_LOCK = threading.Lock()
        auth._AUTH_HASH_COMPUTED = False
        auth._AUTH_HASH_CACHE = None
        # Clear the env var before each test so a dirty environment
        # doesn't cascade across test boundaries
        os.environ.pop('HERMES_WEBUI_PASSWORD', None)

    def _set_env_pw(self, pw: str) -> None:
        os.environ['HERMES_WEBUI_PASSWORD'] = pw

    def test_first_call_returns_hash(self):
        """First call with env var set should return a hex hash string."""
        self._set_env_pw("hunter2")
        h = auth.get_password_hash()
        self.assertIsNotNone(h)
        self.assertIsInstance(h, str)
        self.assertGreater(len(h), 10)

    def test_cache_flag_set_after_first_call(self):
        """_AUTH_HASH_COMPUTED should be True after first call."""
        self._set_env_pw("test-password")
        self.assertFalse(auth._AUTH_HASH_COMPUTED)
        auth.get_password_hash()
        self.assertTrue(auth._AUTH_HASH_COMPUTED)

    def test_cache_hit_is_order_of_magnitude_faster(self):
        """Second invocation must be >>10x faster than the first (sub-millisecond vs ~1s)."""
        self._set_env_pw("a-fairly-long-password-for-benchmarking")
        t0 = time.perf_counter()
        first = auth.get_password_hash()
        t_first = time.perf_counter() - t0
        t0 = time.perf_counter()
        second = auth.get_password_hash()
        t_second = time.perf_counter() - t0
        self.assertEqual(first, second,
                         "Cached hash must match the original")
        self.assertLess(t_second, t_first / 10,
                        f"Cache hit ({t_second*1000:.1f}ms) should be "
                        f">10x faster than first call ({t_first*1000:.1f}ms)")

    def test_subsequent_calls_return_same_hash(self):
        """Multiple calls after caching should all return the identical hash."""
        self._set_env_pw("consistent-password")
        hashes = [auth.get_password_hash() for _ in range(10)]
        self.assertTrue(all(h == hashes[0] for h in hashes),
                        "All cached calls must return the same hash")

    def test_cache_lifetime_is_process_lifetime(self):
        """Cached value persists for the lifetime of the process."""
        self._set_env_pw("persistent-password")
        first = auth.get_password_hash()
        # The env var could change between calls — cache must still
        # return the original value.
        os.environ['HERMES_WEBUI_PASSWORD'] = 'different-password'
        second = auth.get_password_hash()
        self.assertEqual(first, second,
                         "Cache must return the original hash even if "
                         "the env var changes (process-lifetime semantics)")

    def test_multiple_calls_no_env_var(self):
        """When env var is unset, get_password_hash must still work.

        This exercises the settings.json fallback path. The test state
        dir is fresh, so no settings file exists — the result should
        be None (auth disabled).
        """
        # Ensure no env var
        os.environ.pop('HERMES_WEBUI_PASSWORD', None)
        h = auth.get_password_hash()
        self.assertIsNone(h, "With no env var and no settings file, "
                             "hash should be None")
        self.assertTrue(auth._AUTH_HASH_COMPUTED)

    def test_cache_returns_none_when_disabled(self):
        """Once computed as None (no password), cache must keep returning None."""
        os.environ.pop('HERMES_WEBUI_PASSWORD', None)
        h1 = auth.get_password_hash()
        h2 = auth.get_password_hash()
        self.assertIsNone(h1)
        self.assertIsNone(h2)

    def test_cache_independent_of_settings_file(self):
        """Env-var path must not read or depend on settings.json.

        The query count on settings.json before caching is acceptable;
        after caching it must not touch settings at all.
        """
        # Force a hash via env var, then cache it
        self._set_env_pw("env-only")
        auth.get_password_hash()

        # Tamper with the settings load — after caching this should not
        # matter because settings.json is only read inside
        # get_password_hash when COMPUTED is False.
        _original_load = auth.load_settings
        try:
            auth.load_settings = lambda: {"password_hash": "evil"}
            cached = auth.get_password_hash()
            self.assertIsNotNone(cached)
            # The hash should NOT come from the tampered settings
            self.assertNotEqual(cached, "evil",
                                "Cached env-var hash must not be replaced "
                                "by a settings.json value")
        finally:
            auth.load_settings = _original_load


class TestPasswordHashCacheConcurrency(unittest.TestCase):
    """Verify thread-safety: concurrent burst must not duplicate PBKDF2."""

    def setUp(self):
        auth._AUTH_HASH_LOCK = threading.Lock()
        auth._AUTH_HASH_COMPUTED = False
        auth._AUTH_HASH_CACHE = None
        os.environ.pop('HERMES_WEBUI_PASSWORD', None)

    def _set_env_pw(self, pw: str) -> None:
        os.environ['HERMES_WEBUI_PASSWORD'] = pw

    def test_concurrent_burst_only_computes_once(self):
        """Under a burst of N concurrent requests, PBKDF2 runs exactly once.

        Each thread records how many times _hash_password was invoked
        (via a monkey-patched wrapper).  After all threads finish, the
        counter must be exactly 1 and all results identical.
        """
        self._set_env_pw("burst-test-password")

        call_count = 0
        count_lock = threading.Lock()

        original_hash = auth._hash_password
        def counting_hash(pw):
            nonlocal call_count
            with count_lock:
                call_count += 1
            return original_hash(pw)
        auth._hash_password = counting_hash
        try:
            results: list = []
            results_lock = threading.Lock()

            def worker():
                r = auth.get_password_hash()
                with results_lock:
                    results.append(r)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            t0 = time.perf_counter()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            elapsed = time.perf_counter() - t0

            self.assertEqual(call_count, 1,
                             f"Expected 1 PBKDF2 call, got {call_count}. "
                             "Threads are racing on cache population.")
            self.assertEqual(len(set(results)), 1,
                             "All threads must see the same hash")
            # Elapsed time should be ~1s (one PBKDF2), not ~8s (serial).
            # Use a generous 3× bound for slow machines.
            self.assertLess(elapsed, 3.0,
                            f"Burst took {elapsed:.1f}s — threads are likely "
                            f"running PBKDF2 serially under the lock.")
        finally:
            auth._hash_password = original_hash

    def test_concurrent_burst_with_no_env_var(self):
        """Concurrent calls with no env var must all return None."""
        os.environ.pop('HERMES_WEBUI_PASSWORD', None)
        results: list = []
        results_lock = threading.Lock()

        def worker():
            r = auth.get_password_hash()
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertTrue(all(r is None for r in results),
                        "All threads must see None when auth is disabled")


if __name__ == "__main__":
    unittest.main()
