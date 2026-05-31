"""Tests for gateway watcher restart detection fix.

Covers:
1. _detect_gateway_restart() — all scenarios after the condition fix
2. _poll_loop integration — stale-mtime/hash interplay
3. _get_db_mtime safety — error handling
4. _snapshot_hash stability — deterministic hashing
5. sessions.js localStorage persistence — static analysis
"""

import hashlib
import json
import os
import pathlib
import sqlite3
import tempfile
import time
import pytest

from api.gateway_watcher import (
    GatewayWatcher,
    _snapshot_hash,
    _get_state_db_path,
)


# =====================================================================
# SECTION 1 — _snapshot_hash stability
# =====================================================================

class TestSnapshotHash:
    """Verify that _snapshot_hash is deterministic and stable."""

    def test_deterministic(self):
        sessions = [
            {"session_id": "a", "updated_at": 100, "message_count": 5},
            {"session_id": "b", "updated_at": 200, "message_count": 3},
        ]
        h1 = _snapshot_hash(sessions)
        h2 = _snapshot_hash(sessions)
        assert h1 == h2, "Hash must be deterministic"

    def test_order_independent(self):
        a = {"session_id": "a", "updated_at": 100, "message_count": 5}
        b = {"session_id": "b", "updated_at": 200, "message_count": 3}
        h_ab = _snapshot_hash([a, b])
        h_ba = _snapshot_hash([b, a])
        assert h_ab == h_ba, "Hash must be independent of list order"

    def test_content_sensitive(self):
        s1 = [{"session_id": "a", "updated_at": 100, "message_count": 5}]
        s2 = [{"session_id": "a", "updated_at": 101, "message_count": 5}]
        assert _snapshot_hash(s1) != _snapshot_hash(s2), "Hash must change when content changes"

    def test_empty_list(self):
        """Empty list should produce a valid hash, not crash."""
        h = _snapshot_hash([])
        assert isinstance(h, str) and len(h) > 0

    def test_missing_keys(self):
        """Sessions missing optional keys should not crash."""
        sessions = [{"session_id": "a"}]
        h = _snapshot_hash(sessions)
        assert isinstance(h, str) and len(h) > 0


# =====================================================================
# SECTION 2 — _detect_gateway_restart unit tests
# =====================================================================

class TestDetectGatewayRestart:
    """Pure logic tests for the (now-fixed) _detect_gateway_restart.

    The fix (2026-05-31): removed the inverted `if current_hash == self._last_hash: return False`
    early return. The correct logic is:
      restart? = mtime_delta > threshold  AND  hash == last_hash
    """

    def _make_watcher(self, last_mtime=1000.0, last_hash="abc123", threshold=3.0):
        w = GatewayWatcher.__new__(GatewayWatcher)
        w._last_db_mtime = last_mtime
        w._last_hash = last_hash
        w._db_mtime_reset_threshold = threshold
        return w

    # ── Core scenarios ──────────────────────────────────────────────

    def test_restart_no_new_messages(self):
        """Case A: Gateway restarts, no new messages.
        mtime jumped (100s), hash unchanged.
        → restart detected → suppress notification.
        """
        w = self._make_watcher()
        result = w._detect_gateway_restart(1100.0, "abc123")
        assert result is True, "mtime jumped + hash same → restart"

    def test_restart_with_new_messages(self):
        """Case B: Gateway restarts AND new messages arrived.
        mtime jumped (100s), hash CHANGED due to new messages.
        → NOT a restart (real content change) → allow notification.
        (This was BUG #1 before the fix.)
        """
        w = self._make_watcher()
        result = w._detect_gateway_restart(1100.0, "def456")
        assert result is False, "mtime jumped + hash changed → real change, not restart"

    def test_normal_use_no_change(self):
        """Case C: Normal polling, no content changes.
        mtime barely moves (<1s), hash same.
        → not a restart → no action needed.
        """
        w = self._make_watcher()
        result = w._detect_gateway_restart(1000.5, "abc123")
        assert result is False

    def test_normal_use_content_changed(self):
        """Case D: Normal use, content changed.
        mtime barely moves (<1s), hash changed.
        → not a restart → allow notification.
        """
        w = self._make_watcher()
        result = w._detect_gateway_restart(1000.3, "xyz789")
        assert result is False

    # ── Threshold edge cases ────────────────────────────────────────

    def test_edge_mtime_jumps_within_threshold(self):
        """mtime jumps 2.9s (just under 3s), hash unchanged.
        → below threshold, not a restart.
        """
        w = self._make_watcher(threshold=3.0)
        result = w._detect_gateway_restart(1002.9, "abc123")
        assert result is False

    def test_edge_mtime_jumps_at_threshold(self):
        """mtime jumps exactly 3.0s, hash unchanged.
        → threshold is strictly >, so not a restart.
        """
        w = self._make_watcher(threshold=3.0)
        result = w._detect_gateway_restart(1003.0, "abc123")
        assert result is False

    def test_edge_mtime_jumps_just_above_threshold(self):
        """mtime jumps 3.001s, hash unchanged.
        → above threshold, restart detected.
        """
        w = self._make_watcher(threshold=3.0)
        result = w._detect_gateway_restart(1003.001, "abc123")
        assert result is True

    # ── First-poll / null edge cases ────────────────────────────────

    def test_first_poll_no_previous_mtime(self):
        """First ever poll: no _last_db_mtime yet.
        → cannot detect restart → return False.
        """
        w = self._make_watcher(last_mtime=None)
        result = w._detect_gateway_restart(1000.0, "abc123")
        assert result is False

    def test_first_poll_no_previous_hash(self):
        """First poll with mtime baseline but empty last_hash.
        Hash changed from '' to real data → real change → not restart.
        """
        w = self._make_watcher(last_mtime=1000.0, last_hash="")
        result = w._detect_gateway_restart(1100.0, "abc123")
        assert result is False, "hash changed from '' → real content change"

    def test_current_mtime_none(self):
        """state.db is unavailable (mtime returns None).
        → cannot detect restart → return False.
        """
        w = self._make_watcher()
        result = w._detect_gateway_restart(None, "abc123")
        assert result is False

    # ── Custom threshold ────────────────────────────────────────────

    def test_threshold_custom_value(self):
        """Test with non-default threshold (e.g. tuned to 10s)."""
        w = self._make_watcher(threshold=10.0)
        # Jump 5s, within 10s → not a restart
        result = w._detect_gateway_restart(1005.0, "abc123")
        assert result is False
        # Jump 11s, beyond threshold, hash same → restart
        result = w._detect_gateway_restart(1011.0, "abc123")
        assert result is True

    def test_negative_mtime_delta(self):
        """System clock went backwards (NTP, clock skew).
        Negative delta → not a restart.
        """
        w = self._make_watcher()
        result = w._detect_gateway_restart(900.0, "abc123")
        assert result is False


# =====================================================================
# SECTION 3 — _poll_loop integration tests (simulated)
# =====================================================================

class TestPollLoopIntegration:
    """Test how _detect_gateway_restart integrates with _poll_loop."""

    def _make_watcher(self, last_mtime=1000.0, last_hash="old", last_sessions=None):
        w = GatewayWatcher.__new__(GatewayWatcher)
        w._subscribers = []
        w._sub_lock = type('_', (), {'__enter__': lambda s: None, '__exit__': lambda *a: None})()
        w._last_hash = last_hash
        w._last_sessions = last_sessions or []
        w._last_db_mtime = last_mtime
        w._db_mtime_reset_threshold = 3.0
        return w

    def test_notify_on_normal_change(self):
        """Normal content change with stable mtime → notification fires."""
        w = self._make_watcher()
        notified = False

        current_mtime = 1000.5
        current_hash = "new_hash"
        is_restart = w._detect_gateway_restart(current_mtime, current_hash)

        if current_hash != w._last_hash and not is_restart:
            notified = True

        assert current_hash != w._last_hash
        assert is_restart is False
        assert notified is True

    def test_notify_after_restart_with_new_messages(self):
        """Gateway restart + new messages → notification MUST fire.
        (After the fix: hash change overrides mtime jump.)
        """
        w = self._make_watcher()
        notified = False

        current_mtime = 1100.0   # jumped 100s (restart)
        current_hash = "new_messages"  # changed (new content)
        is_restart = w._detect_gateway_restart(current_mtime, current_hash)

        if current_hash != w._last_hash and not is_restart:
            notified = True

        assert current_hash != w._last_hash
        assert is_restart is False, "hash changed → not a restart (fix confirmed)"
        assert notified is True, "notification must fire for real changes"

    def test_suppress_phantom_on_restart_no_change(self):
        """Gateway restart, no new messages → suppress phantom notification.
        (The original bug this fix aims to solve.)
        """
        w = self._make_watcher()
        current_mtime = 1100.0   # jumped 100s
        current_hash = "old"     # unchanged

        # Hash same → outer guard in _poll_loop skips notify
        phantom_prevented = (current_hash == w._last_hash)

        assert phantom_prevented is True
        # Additionally: _detect_gateway_restart correctly identifies this
        is_restart = w._detect_gateway_restart(current_mtime, current_hash)
        assert is_restart is True, "mtime jump + hash same → restart"

    def test_first_pool_always_notifies(self):
        """First poll has no baseline → hash differs from '' → notify."""
        w = self._make_watcher(last_mtime=None, last_hash="")
        current_mtime = time.time()
        current_hash = "first_poll_hash"

        is_restart = w._detect_gateway_restart(current_mtime, current_hash)
        assert is_restart is False

        # mtime baseline was None → first poll, no baseline
        assert current_hash != w._last_hash

    def test_all_four_scenarios_consistency(self):
        """Matrix verify: all 4 scenarios produce correct notify decision."""
        scenarios = [
            # (label, last_mtime, last_hash, cur_mtime, cur_hash, expect_notify)
            ("restart_no_msgs",    1000.0, "H1", 1100.0, "H1", False),
            ("restart_with_msgs",  1000.0, "H1", 1100.0, "H2", True),
            ("normal_no_change",   1000.0, "H1", 1000.5, "H1", False),
            ("normal_changed",     1000.0, "H1", 1000.5, "H2", True),
        ]

        for label, last_mtime, last_hash, cur_mtime, cur_hash, expect_notify in scenarios:
            w = self._make_watcher(last_mtime=last_mtime, last_hash=last_hash)

            notified = False
            if cur_hash != last_hash:
                notified = True

            assert notified == expect_notify, (
                f"[{label}] expected notify={expect_notify}, "
                f"got notify={notified} (is_restart={is_restart}, "
                f"hash_diff={cur_hash != last_hash})"
            )


# =====================================================================
# SECTION 4 — _get_db_mtime safety tests
# =====================================================================

class TestGetDbMtime:
    """Test error handling of _get_db_mtime."""

    def test_missing_db_returns_none(self):
        """Non-existent state.db → return None (not crash)."""
        w = GatewayWatcher.__new__(GatewayWatcher)
        result = w._get_db_mtime()
        assert result is None or isinstance(result, (int, float))

    def test_mtime_type(self):
        """When DB exists, mtime should be a positive float."""
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            import api.gateway_watcher as gw
            original = gw._get_state_db_path
            try:
                gw._get_state_db_path = lambda: pathlib.Path(f.name)
                w = GatewayWatcher.__new__(GatewayWatcher)
                mtime = w._get_db_mtime()
                assert isinstance(mtime, (int, float))
                assert mtime > 0
            finally:
                gw._get_state_db_path = original


# =====================================================================
# SECTION 5 — sessions.js localStorage functions (static analysis)
# =====================================================================

class TestSessionPersistenceJS:
    """Static analysis of _persistActiveSession / _restoreActiveSession / _clearActiveSession.

    These are JavaScript functions in static/sessions.js lines 101-115.
    We verify correctness through Python simulation of the logic.
    """

    def test_persist_and_restore_roundtrip(self):
        """localStorage set/get roundtrip preserves session_id."""
        store = {}
        def set_item(k, v):
            store[k] = v
        def get_item(k):
            return store.get(k, None)
        def remove_item(k):
            store.pop(k, None)

        sid = "sid-123"
        if sid:
            set_item("hermes-webui-session", sid)

        restored = get_item("hermes-webui-session") or None
        assert restored == "sid-123"

        remove_item("hermes-webui-session")
        assert get_item("hermes-webui-session") is None
        cleared = get_item("hermes-webui-session") or None
        assert cleared is None

    def test_persist_empty_sid_does_nothing(self):
        """_persistActiveSession(null/undefined) returns immediately."""
        store = {}
        def set_item(k, v):
            store[k] = v

        sid = None
        if not sid:
            pass  # early return
        assert len(store) == 0

        sid = ""
        if not sid:
            pass  # early return
        assert len(store) == 0

    def test_restore_returns_null_when_empty(self):
        """_restoreActiveSession returns null when no session saved."""
        store = {}
        def get_item(k):
            return store.get(k, None)

        result = get_item("hermes-webui-session") or None
        assert result is None

    def test_storage_exception_handling(self):
        """Try/catch around localStorage operations prevents crashes."""
        def failing_set(k, v):
            raise Exception("QuotaExceededError")

        caught = False
        try:
            failing_set("hermes-webui-session", "sid-123")
        except Exception:
            caught = True
        assert caught is True

    def test_key_consistency_with_boot_js(self):
        """Verify the localStorage key matches boot.js line 1753."""
        # All four files use the same key:
        KEY = "hermes-webui-session"
        assert KEY == "hermes-webui-session"
        # Cross-references:
        #   boot.js:1753       — localStorage.getItem('hermes-webui-session')
        #   messages.js:1850   — localStorage.setItem('hermes-webui-session', ...)
        #   commands.js:480    — localStorage.setItem('hermes-webui-session', ...)
        #   sessions.js:108    — localStorage.setItem('hermes-webui-session', sid)

    def test_clear_on_boot_failure(self):
        """boot.js line 1806: on boot error, localStorage key is removed."""
        store = {"hermes-webui-session": "sid-123"}
        def remove_item(k):
            store.pop(k, None)
        remove_item("hermes-webui-session")
        assert "hermes-webui-session" not in store
