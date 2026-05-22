"""Tests for self-update diagnostics (api/updates.py)."""
from unittest.mock import MagicMock, patch

import api.updates as updates


def _fake_git_for_release_fetch_failure(args, cwd, timeout=10):
    if args == ['fetch', 'origin', '--tags', '--force']:
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


def test_check_repo_redacts_credentialed_fetch_failure(tmp_path):
    """Update-check errors must not expose credentials from git remotes."""
    (tmp_path / '.git').mkdir()
    secret = 'ghp_' + 'A' * 36
    raw_error = (
        "fatal: unable to access "
        f"'https://ash:{secret}@github.com/private/repo.git/': "
        "Authentication failed"
    )

    def fake_git(args, cwd, timeout=10):
        if args == ['fetch', 'origin', '--tags', '--force']:
            return raw_error, False
        if args == ['tag', '--list', 'v*', '--sort=-v:refname']:
            return '', True
        raise AssertionError(f'unexpected git args: {args!r}')

    with patch.object(updates, '_run_git', side_effect=fake_git):
        info = updates._check_repo(tmp_path, 'webui')

    assert info is not None
    assert info['behind'] is None
    assert info['stale_check'] is True
    assert secret not in info['error']
    assert 'ash:' not in info['error']
    assert '<redacted>' in info['error']
    assert 'Authentication failed' in info['error']


def test_check_repo_fetch_failure_without_tags_is_not_up_to_date(tmp_path):
    """If release tags cannot be read, behind is unknown rather than zero."""
    (tmp_path / '.git').mkdir()

    def fake_git(args, cwd, timeout=10):
        if args == ['fetch', 'origin', '--tags', '--force']:
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


# ---------------------------------------------------------------------------
# #2756 — Update check fails with "would clobber existing tag" when an
# upstream release tag was moved.
#
# All three fetch-tag call sites in api/updates.py must use --force so the
# WebUI (a release-tracking consumer that never pushes tags) always defers
# to whatever the remote says a release tag points to. Without --force,
# any remote re-tag (e.g. squash-merge that re-points a release tag at a
# new SHA) jams the update path indefinitely.
# ---------------------------------------------------------------------------


def test_check_repo_fetches_tags_with_force(tmp_path):
    """_check_repo must pass --force to git fetch --tags (regression for #2756)."""
    (tmp_path / '.git').mkdir()

    seen_args = []

    def fake_git(args, cwd, timeout=10):
        seen_args.append(args)
        if args[:2] == ['fetch', 'origin']:
            # Force a fetch failure path so we don't have to mock the rest of
            # the release/branch logic; the assertion is about the args shape.
            return '', False
        if args == ['tag', '--list', 'v*', '--sort=-v:refname']:
            return '', True
        raise AssertionError(f'unexpected git args: {args!r}')

    with patch.object(updates, '_run_git', side_effect=fake_git):
        updates._check_repo(tmp_path, 'webui')

    fetch_calls = [a for a in seen_args if a[:2] == ['fetch', 'origin']]
    assert fetch_calls, 'expected at least one fetch call'
    for call in fetch_calls:
        assert '--tags' in call, f'fetch should include --tags: {call!r}'
        assert '--force' in call, (
            f'fetch should include --force to recover from remote re-tags '
            f'(see #2756): {call!r}'
        )


def test_apply_force_update_fetches_tags_with_force(tmp_path):
    """apply_force_update must pass --force to git fetch --tags (#2756)."""
    (tmp_path / '.git').mkdir()

    seen_args = []

    def fake_git(args, cwd, timeout=10):
        seen_args.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', False  # short-circuit; we just want the args shape.
        raise AssertionError(f'unexpected git args: {args!r}')

    with patch.object(updates, '_run_git', side_effect=fake_git), \
         patch.object(updates, 'REPO_ROOT', tmp_path), \
         patch.object(updates, '_active_stream_count', return_value=0):
        updates.apply_force_update('webui')

    fetch_calls = [a for a in seen_args if a[:2] == ['fetch', 'origin']]
    assert fetch_calls, 'expected at least one fetch call'
    for call in fetch_calls:
        assert '--tags' in call and '--force' in call, (
            f'apply_force_update fetch should be --tags --force (see #2756): {call!r}'
        )


def test_apply_update_fetches_tags_with_force(tmp_path):
    """apply_update must pass --force to git fetch --tags (#2756)."""
    (tmp_path / '.git').mkdir()

    seen_args = []

    def fake_git(args, cwd, timeout=10):
        seen_args.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', False  # short-circuit on fetch failure.
        raise AssertionError(f'unexpected git args: {args!r}')

    with patch.object(updates, '_run_git', side_effect=fake_git), \
         patch.object(updates, 'REPO_ROOT', tmp_path), \
         patch.object(updates, '_active_stream_count', return_value=0):
        updates.apply_update('webui')

    fetch_calls = [a for a in seen_args if a[:2] == ['fetch', 'origin']]
    assert fetch_calls, 'expected at least one fetch call'
    for call in fetch_calls:
        assert '--tags' in call and '--force' in call, (
            f'apply_update fetch should be --tags --force (see #2756): {call!r}'
        )


def test_check_repo_recovers_from_remote_retag(tmp_path):
    """End-to-end: a remote-retag scenario should now succeed (#2756).

    Before the fix, `git fetch origin --tags` would return "would clobber
    existing tag v0.51.5" indefinitely. With --force the fetch succeeds and
    the regular up-to-date / behind path runs.
    """
    (tmp_path / '.git').mkdir()

    def fake_git(args, cwd, timeout=10):
        # The --force flag makes the fetch succeed even when local tags
        # diverge from remote tags. Refuse to honor a plain --tags fetch
        # (no --force) so the test fails loudly if the regression returns.
        if args == ['fetch', 'origin', '--tags']:
            return (
                ' ! [rejected]        v0.51.5    -> v0.51.5    '
                '(would clobber existing tag)'
            ), False
        if args == ['fetch', 'origin', '--tags', '--force']:
            return '', True
        if args == ['tag', '--list', 'v*', '--sort=-v:refname']:
            return 'v0.51.110\nv0.51.109', True
        if args == ['describe', '--tags', '--abbrev=0']:
            return 'v0.51.110', True
        if args == ['describe', '--tags', '--always']:
            return 'v0.51.110', True
        if args == ['remote', 'get-url', 'origin']:
            return 'https://github.com/nesquena/hermes-webui.git', True
        # Branch-check fallback is fine to no-op for this assertion.
        return '', True

    with patch.object(updates, '_run_git', side_effect=fake_git):
        info = updates._check_repo(tmp_path, 'webui')

    assert info is not None
    assert info.get('error') is None, (
        f'expected clean update check, got error: {info.get("error")!r}'
    )
    assert info.get('stale_check') is not True, (
        'fetch with --force should have succeeded, not marked stale'
    )
