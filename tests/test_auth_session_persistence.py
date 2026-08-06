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
from unittest import mock

# Isolate state dir so tests never touch real sessions
_TEST_STATE = Path(tempfile.mkdtemp())
os.environ["HERMES_WEBUI_STATE_DIR"] = str(_TEST_STATE)

sys.path.insert(0, str(Path(__file__).parent.parent))

import api.auth as auth


class TestSessionPersistence(unittest.TestCase):
    """Sessions survive a simulated process restart (module reload)."""

    def setUp(self) -> None:
        # Bind api.auth's STATE_DIR-derived globals to THIS file's _TEST_STATE,
        # regardless of import/reload order. pytest-shard distributes individual
        # test items, so a shard may run a key/session test WITHOUT any of the
        # _simulate_restart() tests that (as a reload side effect) rebind
        # auth.STATE_DIR to _TEST_STATE. Without this, auth.STATE_DIR keeps
        # conftest's TEST_STATE_DIR: _load_key() then reads the wrong dir (so the
        # sentinel key is "missing", no OSError fires, and assertLogs sees no
        # WARNING), and the warning's "STATE_DIR=..." never contains _TEST_STATE.
        # Patching the actual bindings the code reads makes every test in this
        # class self-contained. Saved values are restored in tearDown.
        self._saved_state_dir = auth.STATE_DIR
        self._saved_sessions_file = auth._SESSIONS_FILE
        self._saved_login_attempts_file = auth._LOGIN_ATTEMPTS_FILE
        auth.STATE_DIR = _TEST_STATE
        auth._SESSIONS_FILE = _TEST_STATE / '.sessions.json'
        auth._LOGIN_ATTEMPTS_FILE = _TEST_STATE / '.login_attempts.json'
        auth._sessions.clear()
        auth._PBKDF2_KEY_CACHE = None
        auth._SIGNING_KEY_CACHE = None
        for name in ('.sessions.json', '.signing_key', '.pbkdf2_key'):
            path = _TEST_STATE / name
            if path.exists():
                path.unlink()

    def tearDown(self) -> None:
        auth.STATE_DIR = self._saved_state_dir
        auth._SESSIONS_FILE = self._saved_sessions_file
        auth._LOGIN_ATTEMPTS_FILE = self._saved_login_attempts_file
        auth._sessions.clear()
        auth._PBKDF2_KEY_CACHE = None
        auth._SIGNING_KEY_CACHE = None

    def _simulate_restart(self) -> None:
        """Reload auth module to simulate a fresh process start.

        api.auth does `from api.config import STATE_DIR` at module level, so
        `_SESSIONS_FILE` is computed from api.config.STATE_DIR at reload time.
        We temporarily override api.config.STATE_DIR so the reload uses the
        test state dir without reloading api.config itself, which would
        invalidate imported references like STREAM_PARTIAL_TEXT in other tests.
        """
        import api.config as _config

        saved_state_dir = _config.STATE_DIR
        _config.STATE_DIR = _TEST_STATE
        try:
            importlib.reload(auth)
        finally:
            _config.STATE_DIR = saved_state_dir

    def test_session_survives_restart(self) -> None:
        """A session created before restart should still verify after reload."""
        cookie = auth.create_session()
        self.assertTrue(auth.verify_session(cookie))
        self._simulate_restart()
        self.assertTrue(
            auth.verify_session(cookie),
            "Session must survive process restart via persisted .sessions.json",
        )

    def test_invalidated_session_does_not_survive_restart(self) -> None:
        """Invalidating a session must be reflected after reload."""
        cookie = auth.create_session()
        auth.invalidate_session(cookie)
        self._simulate_restart()
        self.assertFalse(
            auth.verify_session(cookie),
            "Invalidated session must not be reinstated after restart",
        )

    def test_expired_sessions_pruned_on_load(self) -> None:
        """Sessions that expire between restarts must not be loaded."""
        sessions_file = _TEST_STATE / '.sessions.json'
        now = time.time()
        sessions_file.write_text(json.dumps({
            "expired_token": now - 10,
            "valid_token": now + 3600,
        }))
        self._simulate_restart()
        self.assertNotIn("expired_token", auth._sessions)
        self.assertIn("valid_token", auth._sessions)

    def test_sessions_file_permissions(self) -> None:
        """Sessions file must stay strict on POSIX and still be created on Windows."""
        auth.create_session()
        sessions_file = auth._SESSIONS_FILE
        self.assertTrue(sessions_file.exists(), ".sessions.json was not created")
        if os.name == "nt":
            self.assertTrue(sessions_file.is_file(), ".sessions.json was not written as a file")
            return
        mode = oct(sessions_file.stat().st_mode & 0o777)
        self.assertEqual(
            mode,
            oct(0o600),
            f".sessions.json permissions {mode} - expected 0o600",
        )

    def test_malformed_sessions_file_starts_fresh(self) -> None:
        """A corrupt sessions file must not crash auth, start with an empty dict."""
        sessions_file = _TEST_STATE / '.sessions.json'
        sessions_file.write_text("not valid json {{{{")
        with self.assertLogs('api.auth', level='WARNING') as captured:
            self._simulate_restart()
        self.assertEqual(
            auth._sessions,
            {},
            "Corrupt sessions file must result in empty session dict",
        )
        warning = '\n'.join(captured.output)
        self.assertIn('Ignoring malformed auth session store', warning)
        self.assertIn(str(sessions_file), warning)
        self.assertIn(str(_TEST_STATE), warning)

    def test_session_read_failure_warns_with_state_dir_and_starts_fresh(self) -> None:
        """Unreadable sessions files must warn and start with an empty dict."""
        sessions_file = _TEST_STATE / '.sessions.json'
        sessions_file.write_text(json.dumps({"token": time.time() + 3600}))
        with mock.patch.object(Path, 'read_text', side_effect=OSError('read failed')):
            with self.assertLogs('api.auth', level='WARNING') as captured:
                self._simulate_restart()
        self.assertEqual(auth._sessions, {})
        warning = '\n'.join(captured.output)
        self.assertIn('Auth session store read failed', warning)
        self.assertIn(str(sessions_file), warning)
        self.assertIn(str(_TEST_STATE), warning)

    def test_session_decode_failure_warns_with_state_dir_and_starts_fresh(self) -> None:
        """Invalid UTF-8 in the sessions file must warn and start with an empty dict."""
        sessions_file = _TEST_STATE / '.sessions.json'
        sessions_file.write_bytes(b'\xff')
        with self.assertLogs('api.auth', level='WARNING') as captured:
            self._simulate_restart()
        self.assertEqual(auth._sessions, {})
        warning = '\n'.join(captured.output)
        self.assertIn('Ignoring malformed auth session store', warning)
        self.assertIn(str(sessions_file), warning)
        self.assertIn(str(_TEST_STATE), warning)

    def test_session_recursion_failure_warns_with_state_dir_and_starts_fresh(self) -> None:
        """Deeply nested JSON must warn and fall back to an empty session table."""
        sessions_file = _TEST_STATE / '.sessions.json'
        depth = 50000
        sessions_file.write_text('{"token":' * depth + '0' + '}' * depth)
        with self.assertLogs('api.auth', level='WARNING') as captured:
            self._simulate_restart()
        self.assertEqual(auth._sessions, {})
        warning = '\n'.join(captured.output)
        self.assertIn('Ignoring malformed auth session store', warning)
        self.assertIn('RecursionError', warning)
        self.assertIn(str(sessions_file), warning)
        self.assertIn(str(_TEST_STATE), warning)

    def test_session_save_failure_warns_with_state_dir_and_keeps_in_process_session(self) -> None:
        """Write failures must warn but keep the live session usable in-process."""
        sentinel_token = 'deadbeef' * 8
        with mock.patch.object(auth.secrets, 'token_hex', return_value=sentinel_token):
            with mock.patch.object(auth.os, 'replace', side_effect=OSError('replace failed')):
                with self.assertLogs('api.auth', level='WARNING') as captured:
                    cookie = auth.create_session()
        self.assertTrue(auth.verify_session(cookie))
        warning = '\n'.join(captured.output)
        self.assertIn('Auth session persistence failed', warning)
        self.assertIn('.sessions.json', warning)
        self.assertIn(str(_TEST_STATE), warning)
        self.assertNotIn(sentinel_token, warning)
        self.assertNotIn(cookie, warning)

    def test_signing_key_read_failure_warns_with_state_dir(self) -> None:
        """Unreadable signing keys must warn and fall back to a fresh key."""
        key_file = _TEST_STATE / '.signing_key'
        sentinel_key = 'secret-key-material'
        key_file.write_text(sentinel_key)
        with mock.patch.object(Path, 'read_bytes', side_effect=OSError('read failed')):
            with self.assertLogs('api.auth', level='WARNING') as captured:
                key = auth._load_key('.signing_key')
        self.assertIsInstance(key, bytes)
        self.assertEqual(len(key), 32)
        warning = '\n'.join(captured.output)
        self.assertIn('Auth key read failed', warning)
        self.assertIn('.signing_key', warning)
        self.assertIn(str(_TEST_STATE), warning)
        self.assertNotIn(sentinel_key, warning)

    def test_signing_key_persist_failure_warns_with_state_dir(self) -> None:
        """Key write failures must warn and still return a generated key."""
        with mock.patch.object(Path, 'write_bytes', side_effect=OSError('write failed')):
            with self.assertLogs('api.auth', level='WARNING') as captured:
                key = auth._load_key('.signing_key')
        self.assertIsInstance(key, bytes)
        self.assertEqual(len(key), 32)
        warning = '\n'.join(captured.output)
        self.assertIn('Auth key persistence failed', warning)
        self.assertIn('.signing_key', warning)
        self.assertIn(str(_TEST_STATE), warning)

    def test_sessions_file_wrong_type_starts_fresh(self) -> None:
        """A sessions file containing a non-dict must be ignored."""
        sessions_file = _TEST_STATE / '.sessions.json'
        sessions_file.write_text(json.dumps(["list", "not", "dict"]))
        with self.assertLogs('api.auth', level='WARNING') as captured:
            self._simulate_restart()
        self.assertEqual(auth._sessions, {})
        warning = '\n'.join(captured.output)
        self.assertIn('Ignoring malformed auth session store', warning)
        self.assertIn(str(sessions_file), warning)
        self.assertIn(str(_TEST_STATE), warning)


if __name__ == "__main__":
    unittest.main()
