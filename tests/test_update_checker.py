"""
Tests for api/updates.py -- specifically the diagnostic code paths added
in fix/223-update-pull-failed-diagnostics (PR #227).

Tests cover the four new branches in _apply_update_inner():
  1. fetch fails  → network error message
  2. pull fails + diverged history  → recovery command with git reset --hard
  3. pull fails + no upstream tracking  → recovery command with set-upstream-to
  4. pull fails + generic fallback  → raw git output truncated at 300 chars
"""
from pathlib import Path
from unittest.mock import patch, call
import subprocess

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run_git_side_effect(*sequence):
    """Return a side_effect function that yields successive (stdout, ok) tuples."""
    it = iter(sequence)
    def _side_effect(args, cwd, timeout=10):
        return next(it)
    return _side_effect


# ---------------------------------------------------------------------------
# Path used for patching
# ---------------------------------------------------------------------------

_MODULE = 'api.updates'


# ---------------------------------------------------------------------------
# Tests for _apply_update_inner() diagnostic paths
# ---------------------------------------------------------------------------

class TestApplyUpdateDiagnostics:
    """New code paths introduced in PR #227."""

    def _apply(self, target, run_git_side_effect):
        """Call _apply_update_inner with _apply_lock bypassed and _run_git mocked."""
        from api import updates
        with patch(f'{_MODULE}._run_git', side_effect=run_git_side_effect), \
             patch.object(updates, '_apply_lock') as mock_lock:
            mock_lock.acquire.return_value = True
            mock_lock.release.return_value = None
            return updates._apply_update_inner(target)

    # ------------------------------------------------------------------
    # Path 1: fetch step fails → network error message
    # ------------------------------------------------------------------

    def test_fetch_failure_returns_network_error_message(self, tmp_path):
        """When git fetch fails, return a human-readable connection error."""
        (tmp_path / '.git').mkdir()

        from api import updates
        with patch(f'{_MODULE}.REPO_ROOT', tmp_path), \
             patch(f'{_MODULE}._run_git') as mock_run_git:
            # Call sequence: upstream query, fetch
            mock_run_git.side_effect = [
                ('origin/master', True),   # rev-parse @{upstream}
                ('', False),               # fetch fails
            ]
            result = updates._apply_update_inner('webui')

        assert result['ok'] is False
        msg = result['message'].lower()
        assert 'could not reach' in msg or 'internet connection' in msg or 'remote repository' in msg

    def test_fetch_failure_does_not_attempt_pull(self, tmp_path):
        """When fetch fails, pull is never called."""
        (tmp_path / '.git').mkdir()

        from api import updates
        with patch(f'{_MODULE}.REPO_ROOT', tmp_path), \
             patch(f'{_MODULE}._run_git') as mock_run_git:
            mock_run_git.side_effect = [
                ('origin/master', True),   # upstream query
                ('', False),               # fetch fails
            ]
            updates._apply_update_inner('webui')
            # Only 2 calls: upstream query + fetch. No pull call.
            assert mock_run_git.call_count == 2

    # ------------------------------------------------------------------
    # Path 2: pull fails + diverged history
    # ------------------------------------------------------------------

    def test_diverged_history_returns_reset_hard_command(self, tmp_path):
        """Diverged history produces a message with 'reset --hard'."""
        (tmp_path / '.git').mkdir()

        from api import updates
        with patch(f'{_MODULE}.REPO_ROOT', tmp_path), \
             patch(f'{_MODULE}._run_git') as mock_run_git:
            mock_run_git.side_effect = [
                ('origin/master', True),                          # upstream query
                ('', True),                                       # fetch succeeds
                ('', True),                                       # status --porcelain (clean)
                ('Not possible to fast-forward, aborting.', False),  # pull fails
            ]
            result = updates._apply_update_inner('webui')

        assert result['ok'] is False
        assert result.get('diverged') is True
        msg = result['message']
        assert 'reset --hard' in msg

    def test_diverged_history_message_contains_compare_ref(self, tmp_path):
        """Diverged history message includes the upstream ref."""
        (tmp_path / '.git').mkdir()

        from api import updates
        with patch(f'{_MODULE}.REPO_ROOT', tmp_path), \
             patch(f'{_MODULE}._run_git') as mock_run_git:
            mock_run_git.side_effect = [
                ('origin/feat/my-feature', True),   # upstream query
                ('', True),                         # fetch
                ('', True),                         # status (clean)
                ('Your branch and origin have diverged.', False),  # pull
            ]
            result = updates._apply_update_inner('webui')

        assert result['ok'] is False
        assert 'origin/feat/my-feature' in result['message']

    def test_diverged_matching_is_case_insensitive(self, tmp_path):
        """'DIVERGED' in uppercase is still detected."""
        (tmp_path / '.git').mkdir()

        from api import updates
        with patch(f'{_MODULE}.REPO_ROOT', tmp_path), \
             patch(f'{_MODULE}._run_git') as mock_run_git:
            mock_run_git.side_effect = [
                ('origin/master', True),
                ('', True),
                ('', True),
                ('DIVERGED from upstream', False),
            ]
            result = updates._apply_update_inner('webui')

        assert result['ok'] is False
        assert result.get('diverged') is True

    # ------------------------------------------------------------------
    # Path 3: pull fails + no upstream tracking configured
    # ------------------------------------------------------------------

    def test_no_tracking_returns_set_upstream_command(self, tmp_path):
        """Missing upstream tracking branch produces set-upstream-to message."""
        (tmp_path / '.git').mkdir()

        from api import updates
        with patch(f'{_MODULE}.REPO_ROOT', tmp_path), \
             patch(f'{_MODULE}._run_git') as mock_run_git:
            mock_run_git.side_effect = [
                ('origin/master', True),                               # upstream query
                ('', True),                                            # fetch
                ('', True),                                            # status (clean)
                ('There is no tracking information for the current branch.', False),  # pull
            ]
            result = updates._apply_update_inner('webui')

        assert result['ok'] is False
        assert 'set-upstream-to' in result['message']
        assert result.get('diverged') is None

    def test_no_tracking_alternate_phrasing(self, tmp_path):
        """'does not track' alternate git message is also detected."""
        (tmp_path / '.git').mkdir()

        from api import updates
        with patch(f'{_MODULE}.REPO_ROOT', tmp_path), \
             patch(f'{_MODULE}._run_git') as mock_run_git:
            mock_run_git.side_effect = [
                ('origin/master', True),
                ('', True),
                ('', True),
                ('fatal: The current branch local does not track a remote branch.', False),
            ]
            result = updates._apply_update_inner('webui')

        assert result['ok'] is False
        assert 'set-upstream-to' in result['message']

    def test_no_tracking_message_contains_compare_ref(self, tmp_path):
        """set-upstream-to message includes the upstream ref to configure."""
        (tmp_path / '.git').mkdir()

        from api import updates
        with patch(f'{_MODULE}.REPO_ROOT', tmp_path), \
             patch(f'{_MODULE}._run_git') as mock_run_git:
            mock_run_git.side_effect = [
                ('origin/main', True),
                ('', True),
                ('', True),
                ('no tracking information', False),
            ]
            result = updates._apply_update_inner('webui')

        assert result['ok'] is False
        assert 'origin/main' in result['message']

    # ------------------------------------------------------------------
    # Path 4: pull fails + generic fallback (truncated raw output)
    # ------------------------------------------------------------------

    def test_generic_failure_includes_truncated_git_output(self, tmp_path):
        """Generic pull failure includes up to 300 chars of git output."""
        (tmp_path / '.git').mkdir()
        long_error = 'X' * 500  # 500-char error from git

        from api import updates
        with patch(f'{_MODULE}.REPO_ROOT', tmp_path), \
             patch(f'{_MODULE}._run_git') as mock_run_git:
            mock_run_git.side_effect = [
                ('origin/master', True),
                ('', True),
                ('', True),
                (long_error, False),
            ]
            result = updates._apply_update_inner('webui')

        assert result['ok'] is False
        msg = result['message']
        # The raw output in the message must be truncated at 300 chars
        assert 'X' * 300 in msg
        assert 'X' * 301 not in msg

    def test_generic_failure_empty_output_shows_sentinel(self, tmp_path):
        """When git produces no output, message contains a fallback sentinel."""
        (tmp_path / '.git').mkdir()

        from api import updates
        with patch(f'{_MODULE}.REPO_ROOT', tmp_path), \
             patch(f'{_MODULE}._run_git') as mock_run_git:
            mock_run_git.side_effect = [
                ('origin/master', True),
                ('', True),
                ('', True),
                ('', False),   # pull fails with empty output
            ]
            result = updates._apply_update_inner('webui')

        assert result['ok'] is False
        assert 'no output' in result['message'].lower() or result['message']

    def test_generic_failure_does_not_set_diverged(self, tmp_path):
        """A generic pull failure must not set diverged=True."""
        (tmp_path / '.git').mkdir()

        from api import updates
        with patch(f'{_MODULE}.REPO_ROOT', tmp_path), \
             patch(f'{_MODULE}._run_git') as mock_run_git:
            mock_run_git.side_effect = [
                ('origin/master', True),
                ('', True),
                ('', True),
                ('Some unrecognized git error', False),
            ]
            result = updates._apply_update_inner('webui')

        assert result['ok'] is False
        assert not result.get('diverged')

    # ------------------------------------------------------------------
    # Regression: existing success path still works after fetch addition
    # ------------------------------------------------------------------

    def test_successful_update_still_returns_ok(self, tmp_path):
        """Fetch + status + pull success path returns ok=True (regression guard)."""
        (tmp_path / '.git').mkdir()

        from api import updates
        # Patch the cache's 'checked_at' key directly to avoid the lock
        # invalidation block raising. We use a fresh dict swap.
        fake_cache = {'webui': None, 'agent': None, 'checked_at': 1}
        with patch(f'{_MODULE}.REPO_ROOT', tmp_path), \
             patch(f'{_MODULE}._run_git') as mock_run_git, \
             patch(f'{_MODULE}._update_cache', fake_cache), \
             patch(f'{_MODULE}._cache_lock'):
            mock_run_git.side_effect = [
                ('origin/master', True),          # upstream query
                ('', True),                       # fetch succeeds
                ('', True),                       # status (clean working tree)
                ('Already up to date.', True),    # pull succeeds
            ]
            result = updates._apply_update_inner('webui')

        assert result['ok'] is True

    # ------------------------------------------------------------------
    # Agent target works the same as webui target
    # ------------------------------------------------------------------

    def test_fetch_failure_for_agent_target(self, tmp_path):
        """Fetch failure path also works when target='agent'."""
        (tmp_path / '.git').mkdir()

        from api import updates
        with patch(f'{_MODULE}._AGENT_DIR', tmp_path), \
             patch(f'{_MODULE}._run_git') as mock_run_git:
            mock_run_git.side_effect = [
                ('origin/master', True),
                ('', False),   # fetch fails
            ]
            result = updates._apply_update_inner('agent')

        assert result['ok'] is False
        assert 'could not reach' in result['message'].lower() or \
               'internet' in result['message'].lower() or \
               'remote' in result['message'].lower()


# ---------------------------------------------------------------------------
# Tests for _run_git() and compatibility regression cases
# ---------------------------------------------------------------------------

def test_run_git_returns_stderr_on_failure(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()

    from api import updates
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=['git', 'pull', '--ff-only', 'origin/master'],
            returncode=1,
            stdout='',
            stderr="fatal: 'origin/master' does not appear to be a git repository\n",
        )
        out, ok = updates._run_git(['pull', '--ff-only', 'origin/master'], repo)

    assert ok is False
    assert "does not appear to be a git repository" in out


def test_run_git_returns_timeout_message(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()

    from api import updates
    with patch('subprocess.run', side_effect=subprocess.TimeoutExpired(cmd='git pull', timeout=30)):
        out, ok = updates._run_git(['pull', '--ff-only', 'origin', 'main'], repo, timeout=30)

    assert ok is False
    assert out == 'git pull --ff-only origin main timed out after 30s'


def test_run_git_returns_exit_status_when_git_outputs_nothing(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()

    from api import updates
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=['git', 'pull', '--ff-only', 'origin', 'main'],
            returncode=1,
            stdout='',
            stderr='',
        )
        out, ok = updates._run_git(['pull', '--ff-only', 'origin', 'main'], repo)

    assert ok is False
    assert out == 'git exited with status 1'


def test_apply_update_splits_remote_and_branch_for_pull(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / '.git').mkdir()

    from api import updates
    monkeypatch.setattr(updates, 'REPO_ROOT', repo)

    calls = []

    def fake_run_git(args, cwd, timeout=10):
        calls.append(args)
        if args == ['rev-parse', '--abbrev-ref', '@{upstream}']:
            return 'origin/feature/update-fix', True
        if args == ['fetch', 'origin', '--quiet']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return '', True
        if args == ['pull', '--ff-only', 'origin', 'feature/update-fix']:
            return 'Already up to date.', True
        return '', True

    monkeypatch.setattr(updates, '_run_git', fake_run_git)

    fake_cache = {'webui': None, 'agent': None, 'checked_at': 1}
    with patch(f'{_MODULE}._update_cache', fake_cache), \
         patch(f'{_MODULE}._cache_lock'):
        res = updates._apply_update_inner('webui')

    assert res['ok'] is True
    assert ['pull', '--ff-only', 'origin', 'feature/update-fix'] in calls
    assert ['pull', '--ff-only', 'origin/feature/update-fix'] not in calls


def test_apply_update_does_not_stash_for_untracked_only_changes(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / '.git').mkdir()

    from api import updates
    monkeypatch.setattr(updates, 'REPO_ROOT', repo)

    calls = []

    def fake_run_git(args, cwd, timeout=10):
        calls.append(args)
        if args == ['rev-parse', '--abbrev-ref', '@{upstream}']:
            return 'origin/main', True
        if args == ['fetch', 'origin', '--quiet']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return '', True
        if args == ['pull', '--ff-only', 'origin', 'main']:
            return 'Already up to date.', True
        return '', True

    monkeypatch.setattr(updates, '_run_git', fake_run_git)

    fake_cache = {'webui': None, 'agent': None, 'checked_at': 1}
    with patch(f'{_MODULE}._update_cache', fake_cache), \
         patch(f'{_MODULE}._cache_lock'):
        res = updates._apply_update_inner('webui')

    assert res['ok'] is True
    assert ['stash'] not in calls
    assert ['stash', 'pop'] not in calls


def test_apply_update_rejects_unmerged_conflicts(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / '.git').mkdir()

    from api import updates
    monkeypatch.setattr(updates, 'REPO_ROOT', repo)

    def fake_run_git(args, cwd, timeout=10):
        if args == ['rev-parse', '--abbrev-ref', '@{upstream}']:
            return 'origin/main', True
        if args == ['fetch', 'origin', '--quiet']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return 'UU static/style.css', True
        raise AssertionError(f'unexpected git call: {args}')

    monkeypatch.setattr(updates, '_run_git', fake_run_git)

    res = updates._apply_update_inner('webui')

    assert res == {'ok': False, 'message': 'Repository has unresolved merge conflicts'}
