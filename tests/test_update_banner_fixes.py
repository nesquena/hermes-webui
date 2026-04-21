"""Tests for update banner fixes — #813 (conflict recovery) and #814 (restart after update).

Covers:
  - conflict error now includes 'conflict: True' flag and actionable git command (#813)
  - successful update returns 'restart_scheduled: True' (#814)
  - _schedule_restart() spawns a daemon thread, does not block (#814)
  - apply_force_update() returns ok on clean reset path (#813)
  - /api/updates/force route exists in routes.py (#813)
  - UI: _showUpdateError and forceUpdate functions exist in ui.js (#813)
  - UI: updateError element and btnForceUpdate element exist in index.html (#813)
  - UI: success toast says 'Restarting' not 'Reloading' (#814)
  - UI: reload timeout bumped to 2500 ms to allow server restart (#814)
"""

import pathlib
import re
import threading
import time
import sys
import os

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding='utf-8')


# ── api/updates.py ────────────────────────────────────────────────────────────

class TestConflictError:
    """#813 — conflict error must include flag + recovery command."""

    def test_conflict_returns_conflict_flag(self, tmp_path, monkeypatch):
        import api.updates as upd

        # Fake a repo with conflict markers in git status output
        (tmp_path / '.git').mkdir()
        conflict_status = 'UU some/file.py'

        calls = []
        def fake_run(args, cwd, timeout=10):
            calls.append(args)
            if args[:2] == ['status', '--porcelain']:
                return conflict_status, True
            if args[0] == 'fetch':
                return '', True
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'origin/master', True
            return '', True

        monkeypatch.setattr(upd, '_run_git', fake_run)
        monkeypatch.setattr(upd, 'REPO_ROOT', tmp_path)
        monkeypatch.setattr(upd, '_AGENT_DIR', tmp_path)

        result = upd.apply_update('webui')
        assert result['ok'] is False
        assert result.get('conflict') is True, "conflict flag must be True"
        assert 'checkout' in result['message'] or 'pull' in result['message'], (
            "conflict message must include recovery command"
        )
        assert 'merge conflict' in result['message'].lower()

    def test_conflict_message_includes_git_command(self, tmp_path, monkeypatch):
        import api.updates as upd

        (tmp_path / '.git').mkdir()

        def fake_run(args, cwd, timeout=10):
            if args[:2] == ['status', '--porcelain']:
                return 'AA conflict.txt', True
            if args[0] == 'fetch':
                return '', True
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'origin/master', True
            return '', True

        monkeypatch.setattr(upd, '_run_git', fake_run)
        monkeypatch.setattr(upd, 'REPO_ROOT', tmp_path)
        monkeypatch.setattr(upd, '_AGENT_DIR', tmp_path)

        result = upd.apply_update('agent')
        # Message must be actionable — should mention git checkout or pull
        msg = result['message']
        assert 'git' in msg.lower(), f"message should mention git: {msg}"


class TestScheduleRestart:
    """#814 — _schedule_restart must exist and be non-blocking."""

    def test_schedule_restart_exists(self):
        from api.updates import _schedule_restart
        assert callable(_schedule_restart)

    def test_schedule_restart_is_nonblocking(self, monkeypatch):
        """_schedule_restart() must return immediately (spawns daemon thread)."""
        import api.updates as upd

        execv_called = []

        def fake_execv(exe, args):
            execv_called.append((exe, args))

        # Monkeypatch os.execv inside the module's thread closure
        import os as _os
        original_execv = _os.execv

        monkeypatch.setattr(_os, 'execv', fake_execv)

        start = time.monotonic()
        upd._schedule_restart(delay=0.05)
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, f"_schedule_restart must return immediately, took {elapsed:.2f}s"
        # Give the thread time to call execv
        time.sleep(0.2)
        assert execv_called, "_schedule_restart must eventually call os.execv"


class TestSuccessfulUpdateReturnsRestartScheduled:
    """#814 — successful apply_update must return restart_scheduled: True."""

    def test_apply_update_returns_restart_scheduled(self, tmp_path, monkeypatch):
        import api.updates as upd

        (tmp_path / '.git').mkdir()

        def fake_run(args, cwd, timeout=10):
            if args[0] == 'fetch':
                return '', True
            if args[:2] == ['status', '--porcelain']:
                return '', True   # clean tree
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'origin/master', True
            if args[0] == 'pull':
                return 'Already up to date.', True
            return '', True

        monkeypatch.setattr(upd, '_run_git', fake_run)
        monkeypatch.setattr(upd, 'REPO_ROOT', tmp_path)
        monkeypatch.setattr(upd, '_AGENT_DIR', tmp_path)
        # Don't actually restart
        monkeypatch.setattr(upd, '_schedule_restart', lambda delay=2.0: None)

        result = upd.apply_update('webui')
        assert result['ok'] is True
        assert result.get('restart_scheduled') is True, (
            "successful update must set restart_scheduled: True"
        )


class TestApplyForceUpdate:
    """#813 — apply_force_update must reset hard and return ok."""

    def test_apply_force_update_ok(self, tmp_path, monkeypatch):
        import api.updates as upd

        (tmp_path / '.git').mkdir()
        ran = []

        def fake_run(args, cwd, timeout=10):
            ran.append(args)
            if args[0] == 'fetch':
                return '', True
            if args[:2] == ['rev-parse', '--abbrev-ref']:
                return 'origin/master', True
            if args[0] == 'checkout':
                return '', True
            if args[0] == 'reset':
                return '', True
            return '', True

        monkeypatch.setattr(upd, '_run_git', fake_run)
        monkeypatch.setattr(upd, 'REPO_ROOT', tmp_path)
        monkeypatch.setattr(upd, '_AGENT_DIR', tmp_path)
        monkeypatch.setattr(upd, '_schedule_restart', lambda delay=2.0: None)

        result = upd.apply_force_update('webui')
        assert result['ok'] is True
        assert result.get('restart_scheduled') is True

        git_cmds = [r[0] for r in ran]
        assert 'reset' in git_cmds, "force update must call git reset --hard"
        assert 'checkout' in git_cmds, "force update must call git checkout . to clear conflicts"

    def test_apply_force_update_rejects_unknown_target(self, tmp_path, monkeypatch):
        import api.updates as upd
        monkeypatch.setattr(upd, 'REPO_ROOT', tmp_path)
        monkeypatch.setattr(upd, '_AGENT_DIR', tmp_path)
        result = upd.apply_force_update('invalid')
        assert result['ok'] is False


# ── api/routes.py ─────────────────────────────────────────────────────────────

class TestForceUpdateRoute:
    """#813 — /api/updates/force route must exist in routes.py."""

    def test_force_route_exists(self):
        src = read('api/routes.py')
        assert '"/api/updates/force"' in src, (
            "routes.py must handle POST /api/updates/force"
        )
        assert 'apply_force_update' in src, (
            "routes.py must import and call apply_force_update"
        )


# ── static/ui.js ──────────────────────────────────────────────────────────────

class TestUiJsUpdateBanner:
    """#813 + #814 — UI must show persistent error, force button, and correct toast."""

    def test_show_update_error_function_exists(self):
        src = read('static/ui.js')
        assert 'function _showUpdateError' in src, (
            "_showUpdateError() must be defined in ui.js"
        )

    def test_force_update_function_exists(self):
        src = read('static/ui.js')
        assert 'function forceUpdate' in src or 'async function forceUpdate' in src, (
            "forceUpdate() must be defined in ui.js"
        )

    def test_force_update_uses_confirm_dialog_not_native(self):
        """forceUpdate() must use showConfirmDialog(), not the banned native confirm()."""
        src = read('static/ui.js')
        m = re.search(r'function forceUpdate\b.*?\n\}', src, re.DOTALL)
        assert m, "forceUpdate() not found"
        fn = m.group(0)
        assert 'showConfirmDialog' in fn, (
            "forceUpdate() must use showConfirmDialog() not the native confirm() "
            "(native confirm is banned by test_sprint33)"
        )
        assert 'confirm(' not in fn.replace('showConfirmDialog(', ''), (
            "forceUpdate() must not use native confirm()"
        )

    def test_force_update_calls_api_updates_force(self):
        src = read('static/ui.js')
        m = re.search(r'function forceUpdate\b.*?\n\}', src, re.DOTALL)
        assert m, "forceUpdate() not found"
        fn = m.group(0)
        assert '/api/updates/force' in fn, (
            "forceUpdate() must POST to /api/updates/force"
        )

    def test_success_toast_says_restarting(self):
        src = read('static/ui.js')
        m = re.search(r'function applyUpdates\b.*?\n\}', src, re.DOTALL)
        assert m, "applyUpdates() not found"
        fn = m.group(0)
        assert 'Restarting' in fn, (
            "success toast must say 'Restarting' (server self-restarts after update)"
        )
        assert 'Reloading' not in fn, (
            "success toast must not say 'Reloading' — server restarts, page reloads after"
        )

    def test_reload_timeout_at_least_2000ms(self):
        """Reload delay must be >= 2000 ms to give the server time to restart."""
        src = read('static/ui.js')
        m = re.search(r'function applyUpdates\b.*?\n\}', src, re.DOTALL)
        assert m, "applyUpdates() not found"
        fn = m.group(0)
        timeouts = re.findall(r'setTimeout\(.*?(\d+)\)', fn)
        assert timeouts, "applyUpdates must have a setTimeout for page reload"
        assert any(int(t) >= 2000 for t in timeouts), (
            f"reload timeout must be >= 2000 ms to survive server restart; found: {timeouts}"
        )

    def test_conflict_response_shows_force_button(self):
        src = read('static/ui.js')
        m = re.search(r'function _showUpdateError\b.*?\n\}', src, re.DOTALL)
        assert m, "_showUpdateError() not found"
        fn = m.group(0)
        assert 'conflict' in fn or 'diverged' in fn, (
            "_showUpdateError must check res.conflict / res.diverged to show force button"
        )
        assert 'btnForceUpdate' in fn or 'forceBtn' in fn, (
            "_showUpdateError must reference the force update button"
        )

    def test_error_displayed_persistently_not_just_toast(self):
        src = read('static/ui.js')
        m = re.search(r'function _showUpdateError\b.*?\n\}', src, re.DOTALL)
        assert m
        fn = m.group(0)
        assert 'updateError' in fn, (
            "_showUpdateError must write to the #updateError element for persistent display"
        )


# ── static/index.html ─────────────────────────────────────────────────────────

class TestIndexHtmlBanner:
    """#813 — update banner HTML must include error element and force button."""

    def test_update_error_element_exists(self):
        src = read('static/index.html')
        assert 'id="updateError"' in src, (
            "index.html must have #updateError element for persistent error display"
        )

    def test_force_update_button_exists(self):
        src = read('static/index.html')
        assert 'id="btnForceUpdate"' in src, (
            "index.html must have #btnForceUpdate button (hidden by default)"
        )

    def test_force_update_button_hidden_by_default(self):
        src = read('static/index.html')
        m = re.search(r'id="btnForceUpdate"[^>]*>', src)
        assert m, "#btnForceUpdate not found"
        tag = m.group(0)
        assert 'display:none' in tag, (
            "#btnForceUpdate must be hidden by default (display:none)"
        )
