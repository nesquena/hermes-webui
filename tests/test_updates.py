"""Tests for self-update diagnostics (api/updates.py)."""
from unittest.mock import MagicMock, patch

import api.updates as updates


def _fake_git_for_release_fetch_failure(args, cwd, timeout=10):
    if args == ['fetch', 'origin', '--tags']:
        return 'would clobber existing tag v0.50.294', False
    if args == ['tag', '--list', 'v*', '--sort=-v:refname']:
        return 'v0.51.106\nv0.51.103', True
    if args == ['describe', '--tags', '--abbrev=0']:
        return 'v0.51.103', True
    if args == ['remote', 'get-url', 'origin']:
        return 'https://github.com/nesquena/hermes-webui.git', True
    raise AssertionError(f'unexpected git args: {args!r}')


def test_check_repo_reports_release_gap_even_when_tag_fetch_fails(tmp_path):
    """A tag fetch error must not collapse the UI state to "up to date"."""
    (tmp_path / '.git').mkdir()
    with patch.object(updates, '_run_git', side_effect=_fake_git_for_release_fetch_failure):
        info = updates._check_repo(tmp_path, 'webui')

    assert info is not None
    assert info['behind'] == 1
    assert info['current_version'] == 'v0.51.103'
    assert info['latest_version'] == 'v0.51.106'
    assert info['stale_check'] is True
    assert 'would clobber existing tag' in info['error']


def test_check_repo_fetch_failure_without_tags_is_not_up_to_date(tmp_path):
    """If release tags cannot be read, behind is unknown rather than zero."""
    (tmp_path / '.git').mkdir()

    def fake_git(args, cwd, timeout=10):
        if args == ['fetch', 'origin', '--tags']:
            return 'network unavailable', False
        if args == ['tag', '--list', 'v*', '--sort=-v:refname']:
            return '', True
        raise AssertionError(f'unexpected git args: {args!r}')

    with patch.object(updates, '_run_git', side_effect=fake_git):
        info = updates._check_repo(tmp_path, 'webui')

    assert info is not None
    assert info['behind'] is None
    assert info['stale_check'] is True
    assert info['error'] == 'fetch failed: network unavailable'


def test_run_git_returns_stderr_on_failure(tmp_path):
    """When a git command fails, _run_git should return stderr (not empty string)."""
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout='',
            stderr="fatal: 'origin/master' does not appear to be a git repository\n",
        )
        out, ok = updates._run_git(['pull', '--ff-only', 'origin/master'], tmp_path)

    assert ok is False
    assert "does not appear to be a git repository" in out


def test_run_git_returns_stdout_when_no_stderr(tmp_path):
    """If stderr is empty on failure, fall back to stdout."""
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=128,
            stdout='Already up to date.',
            stderr='',
        )
        out, ok = updates._run_git(['pull'], tmp_path)

    assert ok is False
    assert 'Already up to date' in out


def test_run_git_returns_exit_code_when_no_output(tmp_path):
    """If both stdout and stderr are empty, report the exit code."""
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout='',
            stderr='',
        )
        out, ok = updates._run_git(['status'], tmp_path)

    assert ok is False
    assert 'status 1' in out


def test_split_remote_ref_splits_tracking_ref():
    """_split_remote_ref should correctly split origin/branch."""
    assert updates._split_remote_ref('origin/master') == ('origin', 'master')
    assert updates._split_remote_ref('origin/feature/foo') == ('origin', 'feature/foo')
    assert updates._split_remote_ref('master') == (None, 'master')
