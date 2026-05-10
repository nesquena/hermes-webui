"""
Tests for auth session lifecycle — session creation, verification, expiry,
and lazy pruning of expired entries.
"""

import time
import unittest
from pathlib import Path
import tempfile
import os

# Isolate state dir so we don't touch real sessions
_TEST_STATE = Path(tempfile.mkdtemp())
os.environ["HERMES_WEBUI_STATE_DIR"] = str(_TEST_STATE)

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

import importlib

# Force re-import of auth module so it picks up our TEST_STATE_DIR
auth = importlib.import_module("api.auth")


class TestSessionPruning(unittest.TestCase):
    """Verify expired session cleanup works correctly."""

    def setUp(self):
        # Clear any leftover sessions from other tests
        auth._sessions.clear()

    def test_session_created_valid(self):
        """A fresh session token should verify as valid."""
        token = auth.create_session()
        self.assertTrue(auth.verify_session(token))

    def test_expired_session_pruned(self):
        """Manually inserting an expired entry should be pruned on next verify_session call."""
        # Insert sessions that have already expired
        auth._sessions["fake_token"] = time.time() - 100
        auth._sessions["another_fake"] = time.time() - 50
        # Insert one valid session (far future)
        auth._sessions["good_token"] = time.time() + 3600

        # _sessions has 3 entries, 2 expired
        self.assertEqual(len(auth._sessions), 3)

        # Call verify_session — this triggers _prune_expired_sessions()
        # Cookie format is token.signature, so we need a dot to pass the early check
        auth.verify_session("fake_token.fake_sig")

        # After verification, only the valid session should remain
        self.assertEqual(len(auth._sessions), 1)
        self.assertIn("good_token", auth._sessions)
        self.assertNotIn("fake_token", auth._sessions)
        self.assertNotIn("another_fake", auth._sessions)

    def test_prune_does_not_remove_valid_sessions(self):
        """_prune_expired_sessions should never remove sessions that are still active."""
        auth._sessions["active_1"] = time.time() + 86400  # 24 hours from now
        auth._sessions["active_2"] = time.time() + 7200  # 2 hours from now
        auth._sessions["expired_1"] = time.time() - 10

        auth._prune_expired_sessions()

        self.assertEqual(len(auth._sessions), 2)
        self.assertIn("active_1", auth._sessions)
        self.assertIn("active_2", auth._sessions)
        self.assertNotIn("expired_1", auth._sessions)

    def test_verify_session_prunes_before_verification(self):
        """verify_session should prune expired entries before checking the target token.

        This ensures that _prune_expired_sessions() is called at the very top
        of verify_session(), so cleanup happens on every auth check.
        """
        auth._sessions["expired_for_test"] = time.time() - 999

        # verify_session with an invalid cookie triggers the full path:
        # _prune_expired_sessions -> signature check -> return False
        result = auth.verify_session("nonexistent.bad_sig")
        self.assertFalse(result)

        # The expired entry should have been cleaned up
        self.assertNotIn("expired_for_test", auth._sessions)

    def test_prune_handles_empty_dict(self):
        """_prune_expired_sessions should be safe on an empty dict."""
        auth._sessions.clear()
        auth._prune_expired_sessions()
        self.assertEqual(len(auth._sessions), 0)

    def test_session_ttl_is_24_hours(self):
        """Newly created sessions should have the expected 24-hour TTL."""
        auth._sessions.clear()
        token_hex = auth.create_session().split(".")[0]
        # The _sessions dict stores token -> expiry_time
        # We can check the expiry is approximately SESSION_TTL seconds from now
        # by looking up the raw entry via the token
        from api.auth import _sessions, SESSION_TTL

        # find our entry
        for t, exp in _sessions.items():
            if t == token_hex:
                # expiry should be within 5 seconds of now + SESSION_TTL
                expected = time.time() + SESSION_TTL
                self.assertAlmostEqual(exp, expected, delta=5)
                break
        else:
            self.fail("Session token not found in _sessions")


class TestSessionInvalidation(unittest.TestCase):
    """Test session logout / invalidation."""

    def setUp(self):
        auth._sessions.clear()

    def test_invalidate_session_removes_token(self):
        """Calling invalidate_session should remove the token from _sessions."""
        token = auth.create_session()
        self.assertTrue(auth.verify_session(token))

        auth.invalidate_session(token)
        # Token should be gone
        self.assertFalse(auth.verify_session(token))

    def test_invalidate_unknown_token_is_safe(self):
        """Invalidating a non-existent token should not raise."""
        auth._sessions.clear()
        auth.invalidate_session("nonexistent_token")
        # Should not raise


if __name__ == "__main__":
    unittest.main()


class TestSessionTtlResolution(unittest.TestCase):
    """Verify the three-layer TTL resolution (env > settings > default)."""

    def setUp(self):
        # Snapshot environment + load_settings so each test starts clean.
        self._saved_env = {k: os.environ.get(k) for k in ("HERMES_WEBUI_SESSION_TTL",)}
        os.environ.pop("HERMES_WEBUI_SESSION_TTL", None)
        self._saved_load_settings = auth.load_settings

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        auth.load_settings = self._saved_load_settings

    def test_env_var_overrides_settings(self):
        """HERMES_WEBUI_SESSION_TTL env var should take priority."""
        os.environ["HERMES_WEBUI_SESSION_TTL"] = "3600"
        from api.auth import _resolve_session_ttl

        self.assertEqual(_resolve_session_ttl(), 3600)

    def test_clamps_minimum(self):
        """Values below 60 seconds fall through to settings/default (do not honor)."""
        os.environ["HERMES_WEBUI_SESSION_TTL"] = "10"
        auth.load_settings = lambda: {}
        from api.auth import _resolve_session_ttl

        # Out-of-range env values are rejected; falls through to default 30 days.
        self.assertEqual(_resolve_session_ttl(), auth.SESSION_TTL)

    def test_clamps_maximum(self):
        """Values above 1 year fall through to settings/default (do not honor)."""
        os.environ["HERMES_WEBUI_SESSION_TTL"] = "100000000"
        auth.load_settings = lambda: {}
        from api.auth import _resolve_session_ttl

        # Out-of-range env values are rejected; falls through to default 30 days.
        self.assertEqual(_resolve_session_ttl(), auth.SESSION_TTL)

    def test_invalid_env_falls_through(self):
        """Non-integer env var falls through to default."""
        os.environ["HERMES_WEBUI_SESSION_TTL"] = "not-a-number"
        auth.load_settings = lambda: {}
        from api.auth import _resolve_session_ttl

        self.assertEqual(_resolve_session_ttl(), auth.SESSION_TTL)

    def test_empty_env_falls_through(self):
        """Empty env var falls through to default."""
        os.environ["HERMES_WEBUI_SESSION_TTL"] = ""
        auth.load_settings = lambda: {}
        from api.auth import _resolve_session_ttl

        self.assertEqual(_resolve_session_ttl(), auth.SESSION_TTL)

    def test_settings_path_returns_value(self):
        """settings.json session_ttl_seconds path works when env is unset."""
        os.environ.pop("HERMES_WEBUI_SESSION_TTL", None)
        auth.load_settings = lambda: {"session_ttl_seconds": 7200}
        from api.auth import _resolve_session_ttl

        self.assertEqual(_resolve_session_ttl(), 7200)

    def test_session_uses_dynamic_ttl(self):
        """Newly created sessions should honor the resolved TTL."""
        auth._sessions.clear()
        os.environ["HERMES_WEBUI_SESSION_TTL"] = "3600"
        token_hex = auth.create_session().split(".")[0]
        from api.auth import _sessions

        for t, exp in _sessions.items():
            if t == token_hex:
                # The resolved env-var value (3600s) should be applied, not
                # the SESSION_TTL fallback default.
                expected = time.time() + 3600
                self.assertAlmostEqual(exp, expected, delta=5)
                break
        else:
            self.fail("Session token not found in _sessions")
