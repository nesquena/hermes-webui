"""Regression tests: auth sessions persist across process restarts.

_sessions is an in-memory dict. Without persistence, any restart (launchd,
systemd, container) invalidates all active browser sessions and floods clients
with 401s until they clear cookies. The HMAC signing key already persists to
STATE_DIR; this PR persists the session table using the same pattern.
"""
import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Isolate state dir so tests never touch real sessions
_TEST_STATE = Path(tempfile.mkdtemp())
os.environ["HERMES_WEBUI_STATE_DIR"] = str(_TEST_STATE)

sys.path.insert(0, str(Path(__file__).parent.parent))

import api.auth as auth


class TestSessionPersistence(unittest.TestCase):
    """Sessions survive a simulated process restart (module reload)."""

    def setUp(self) -> None:
        auth._sessions.clear()
        sessions_file = _TEST_STATE / '.sessions.json'
        if sessions_file.exists():
            sessions_file.unlink()

    def _simulate_restart(self) -> None:
        """Reload auth module to simulate a fresh process start."""
        importlib.reload(auth)

    def test_session_survives_restart(self) -> None:
        """A session created before restart should still verify after reload."""
        cookie = auth.create_session()
        self.assertTrue(auth.verify_session(cookie))
        self._simulate_restart()
        self.assertTrue(auth.verify_session(cookie),
                        "Session must survive process restart via persisted .sessions.json")

    def test_invalidated_session_does_not_survive_restart(self) -> None:
        """Invalidating a session must be reflected after reload."""
        cookie = auth.create_session()
        auth.invalidate_session(cookie)
        self._simulate_restart()
        self.assertFalse(auth.verify_session(cookie),
                         "Invalidated session must not be reinstated after restart")

    def test_expired_sessions_pruned_on_load(self) -> None:
        """Sessions that expire between restarts must not be loaded."""
        sessions_file = _TEST_STATE / '.sessions.json'
        # Write a sessions file with one expired and one valid entry
        now = time.time()
        sessions_file.write_text(json.dumps({
            "expired_token": now - 10,
            "valid_token": now + 3600,
        }))
        self._simulate_restart()
        self.assertNotIn("expired_token", auth._sessions)
        self.assertIn("valid_token", auth._sessions)

    def test_sessions_file_permissions(self) -> None:
        """Sessions file must be owner-read-only (0600)."""
        auth.create_session()
        sessions_file = _TEST_STATE / '.sessions.json'
        self.assertTrue(sessions_file.exists(), ".sessions.json was not created")
        mode = oct(sessions_file.stat().st_mode & 0o777)
        self.assertEqual(mode, oct(0o600),
                         f".sessions.json permissions {mode} — expected 0o600")

    def test_malformed_sessions_file_starts_fresh(self) -> None:
        """A corrupt sessions file must not crash auth — start with empty dict."""
        sessions_file = _TEST_STATE / '.sessions.json'
        sessions_file.write_text("not valid json {{{{")
        self._simulate_restart()
        self.assertEqual(auth._sessions, {},
                         "Corrupt sessions file must result in empty session dict")

    def test_sessions_file_wrong_type_starts_fresh(self) -> None:
        """A sessions file containing a non-dict must be ignored."""
        sessions_file = _TEST_STATE / '.sessions.json'
        sessions_file.write_text(json.dumps(["list", "not", "dict"]))
        self._simulate_restart()
        self.assertEqual(auth._sessions, {})


if __name__ == "__main__":
    unittest.main()
