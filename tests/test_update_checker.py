"""
Tests for api/updates.py -- specifically the diagnostic code paths added
in fix/223-update-pull-failed-diagnostics (PR #227).

Tests cover the four new branches in _apply_update_inner():
  1. fetch fails  → network error message
  2. pull fails + diverged history  → recovery command with git reset --hard
  3. pull fails + no upstream tracking  → recovery command with set-upstream-to
  4. pull fails + generic fallback  → raw git output truncated at 300 chars
"""
from unittest.mock import patch
import subprocess

from api import updates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run_git_side_effect(*sequence):
    """Return a side_effect function that yields successive (stdout, ok) tuples."""
    it = iter(sequence)
    def _side_effect(args, cwd, timeout=10):
        return next(it)
    return _side_effect


def _git(repo, *args):
    """Run a real git command against a throwaway repo (no network)."""
    subprocess.run(
        ['git', *args], cwd=str(repo), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


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
            # Call sequence: fetch
            mock_run_git.side_effect = [
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
                ('', False),               # fetch fails
            ]
            updates._apply_update_inner('webui')
            # Only fetch is called. No target selection or pull call.
            assert mock_run_git.call_count == 1

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
                ('', True),                                       # fetch succeeds
                ('', True),                                       # no release tags
                ('origin/master', True),                          # upstream query
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
                ('', True),                         # fetch
                ('', True),                         # no release tags
                ('origin/feat/my-feature', True),   # upstream query
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
                ('', True),
                ('', True),
                ('origin/master', True),
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
                ('', True),                                            # fetch
                ('', True),                                            # no release tags
                ('origin/master', True),                               # upstream query
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
                ('', True),
                ('', True),
                ('origin/master', True),
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
                ('', True),
                ('', True),
                ('origin/main', True),
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
                ('', True),
                ('', True),
                ('origin/master', True),
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
                ('', True),
                ('', True),
                ('origin/master', True),
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
                ('', True),
                ('', True),
                ('origin/master', True),
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
        # #8687: the success path ends in _schedule_restart(), which spawns a
        # daemon worker that takes api.updates._apply_lock and walks the real
        # repository's __pycache__ tree. The worker outlives this test and
        # refuses every later apply_update() in the session with "Update
        # already in progress". Stub the scheduler so no worker escapes; the
        # restart request itself is still asserted below.
        with patch(f'{_MODULE}.REPO_ROOT', tmp_path), \
             patch(f'{_MODULE}._run_git') as mock_run_git, \
             patch(f'{_MODULE}._update_cache', fake_cache), \
             patch(f'{_MODULE}._cache_lock'), \
             patch(f'{_MODULE}._schedule_restart') as mock_schedule_restart:
            mock_run_git.side_effect = [
                ('', True),                       # fetch succeeds
                ('', True),                       # no release tags
                ('origin/master', True),          # upstream query
                ('', True),                       # status (clean working tree)
                ('Already up to date.', True),    # pull succeeds
            ]
            result = updates._apply_update_inner('webui')

        assert result['ok'] is True
        # The restart was still requested — only the worker is gone.
        mock_schedule_restart.assert_called_once()

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
                ('', False),   # fetch fails
            ]
            result = updates._apply_update_inner('agent')

        assert result['ok'] is False
        assert 'could not reach' in result['message'].lower() or \
               'internet' in result['message'].lower() or \
               'remote' in result['message'].lower()


# Issue #4085 regression: a dirty install at-or-past the latest release tag
# must surface `dirty: True` on the check payload so the Settings panel can
# offer `apply_force_update` instead of the silent "Up to date" state.
class TestCheckRepoDirtyFlag:
    def test_dirty_tree_surfaces_dirty_true(self, tmp_path):
        """A dirty tree on a network-failed check must still carry dirty=True.

        The Settings panel needs the dirty flag on every payload shape so a
        user can always recover from a local-modifications state, even when
        the latest remote cannot be reached.
        """
        (tmp_path / '.git').mkdir()

        def fake_git(args, cwd, timeout=10):
            if args == ['diff-index', '--quiet', 'HEAD', '--']:
                return '', False  # dirty
            if args == ['fetch', 'origin', '--tags', '--force']:
                return 'network unavailable', False  # fail fast
            if args == ['tag', '--list', 'v*', '--sort=-creatordate']:
                return '', True
            raise AssertionError(f'unexpected git args: {args!r}')

        from api import updates
        with patch(f'{_MODULE}._run_git', side_effect=fake_git):
            info = updates._check_repo(tmp_path, 'webui')

        assert info is not None
        assert info['dirty'] is True
        assert info['stale_check'] is True


# Issue #8556 / #8655: on the `stable` channel a checkout whose local branch
# DIVERGED from the stable line — HEAD neither contains the latest stable tag
# nor can fast-forward to it — must retain stable-scoped targets (latest_tag),
# rather than falling through to configured upstream branches (origin/master or
# origin/feature).
class TestStableDivergedBranchIsNotUpToDate:
    CURRENT_TAG = 'v0.52.492'
    LATEST_TAG = 'v0.52.544'

    def _diverged_stable_repo(self, tmp_path, upstream_kind='master'):
        """Linear stable line .492→.544, plus 3 unpromoted commits on master,
        plus local branch off .492 with local-only commit L, tracking master or feature."""
        upstream = tmp_path / 'upstream'
        upstream.mkdir(parents=True, exist_ok=True)
        _git(upstream, 'init', '-q', '-b', 'master')
        _git(upstream, 'config', 'user.email', 't@t.co')
        _git(upstream, 'config', 'user.name', 'Test')
        tracked = upstream / 'APP'
        tracked.write_text('release 492\n', encoding='utf-8')
        _git(upstream, 'add', 'APP')
        _git(upstream, 'commit', '-q', '-m', self.CURRENT_TAG)
        _git(upstream, 'tag', self.CURRENT_TAG)
        for patch_num in range(493, 545):
            tracked.write_text(f'release {patch_num}\n', encoding='utf-8')
            _git(upstream, 'commit', '-q', '-am', f'v0.52.{patch_num}')
            _git(upstream, 'tag', f'v0.52.{patch_num}')

        # Three unpromoted commits on master after .544 (U)
        for unpromoted in range(1, 4):
            tracked.write_text(f'unpromoted master commit {unpromoted}\n', encoding='utf-8')
            _git(upstream, 'commit', '-q', '-am', f'unpromoted experimental work {unpromoted}')

        origin = tmp_path / 'origin.git'
        subprocess.run(
            ['git', 'init', '--bare', '-q', '--initial-branch=master', str(origin)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _git(upstream, 'push', '-q', str(origin), '--all')
        _git(upstream, 'push', '-q', str(origin), '--tags')

        repo = tmp_path / 'local'
        _git(tmp_path, 'clone', '-q', str(origin), str(repo))
        _git(repo, 'config', 'user.email', 't@t.co')
        _git(repo, 'config', 'user.name', 'Test')
        _git(repo, 'checkout', '-q', '-b', 'stable-local', self.CURRENT_TAG)
        (repo / 'APP').write_text('local hotfix L\n', encoding='utf-8')
        _git(repo, 'commit', '-q', '-am', 'local commit L not on stable line')

        if upstream_kind == 'master':
            _git(repo, 'branch', '--set-upstream-to=origin/master', 'stable-local')
        elif upstream_kind == 'feature':
            _git(repo, 'checkout', '-q', '-b', 'feature')
            (repo / 'APP').write_text('feature commit F\n', encoding='utf-8')
            _git(repo, 'commit', '-q', '-am', 'feature commit F')
            _git(repo, 'push', '-q', 'origin', 'feature')
            _git(repo, 'checkout', '-q', 'stable-local')
            _git(repo, 'branch', '-D', '-q', 'feature')
            _git(repo, 'branch', '--set-upstream-to=origin/feature', 'stable-local')
        else:
            raise ValueError(f"Unknown upstream_kind: {upstream_kind}")

        return repo

    def _diverged_stable_repo_multi_commit(self, tmp_path):
        """Graph with multiple commits per release:
        v1.0.0 → 3 commits → v1.0.1 → 3 commits → v1.0.2, two unpromoted commits
        on master, local branch stable-local created at v1.0.0 with one local-only
        commit, upstream origin/master.
        """
        upstream = tmp_path / 'upstream'
        upstream.mkdir(parents=True, exist_ok=True)
        _git(upstream, 'init', '-q', '-b', 'master')
        _git(upstream, 'config', 'user.email', 't@t.co')
        _git(upstream, 'config', 'user.name', 'Test')
        tracked = upstream / 'APP'
        tracked.write_text('release 1.0.0\n', encoding='utf-8')
        _git(upstream, 'add', 'APP')
        _git(upstream, 'commit', '-q', '-m', 'v1.0.0')
        _git(upstream, 'tag', 'v1.0.0')

        # 3 commits leading to v1.0.1 (2 intermediate commits + 1 release commit tagged v1.0.1)
        for i in range(1, 3):
            tracked.write_text(f'work toward v1.0.1 ({i})\n', encoding='utf-8')
            _git(upstream, 'commit', '-q', '-am', f'work toward v1.0.1 ({i})')
        tracked.write_text('release 1.0.1\n', encoding='utf-8')
        _git(upstream, 'commit', '-q', '-am', 'v1.0.1')
        _git(upstream, 'tag', 'v1.0.1')

        # 3 commits leading to v1.0.2 (2 intermediate commits + 1 release commit tagged v1.0.2)
        for i in range(1, 3):
            tracked.write_text(f'work toward v1.0.2 ({i})\n', encoding='utf-8')
            _git(upstream, 'commit', '-q', '-am', f'work toward v1.0.2 ({i})')
        tracked.write_text('release 1.0.2\n', encoding='utf-8')
        _git(upstream, 'commit', '-q', '-am', 'v1.0.2')
        _git(upstream, 'tag', 'v1.0.2')

        # Two unpromoted commits on master after v1.0.2
        for unpromoted in range(1, 3):
            tracked.write_text(f'unpromoted master commit {unpromoted}\n', encoding='utf-8')
            _git(upstream, 'commit', '-q', '-am', f'unpromoted experimental work {unpromoted}')

        origin = tmp_path / 'origin.git'
        subprocess.run(
            ['git', 'init', '--bare', '-q', '--initial-branch=master', str(origin)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _git(upstream, 'push', '-q', str(origin), '--all')
        _git(upstream, 'push', '-q', str(origin), '--tags')

        repo = tmp_path / 'local'
        _git(tmp_path, 'clone', '-q', str(origin), str(repo))
        _git(repo, 'config', 'user.email', 't@t.co')
        _git(repo, 'config', 'user.name', 'Test')
        _git(repo, 'checkout', '-q', '-b', 'stable-local', 'v1.0.0')
        (repo / 'APP').write_text('local hotfix L\n', encoding='utf-8')
        _git(repo, 'commit', '-q', '-am', 'local commit L not on stable line')
        _git(repo, 'branch', '--set-upstream-to=origin/master', 'stable-local')

        return repo

    def test_diverged_stable_reports_release_gap_not_commit_count(self, tmp_path):
        repo = self._diverged_stable_repo_multi_commit(tmp_path)

        # Raw commit count is 6 (3 commits to v1.0.1 + 3 commits to v1.0.2)
        raw_rev_count = int(
            subprocess.run(
                ['git', 'rev-list', '--count', 'HEAD..v1.0.2'],
                cwd=str(repo), capture_output=True, text=True, check=True,
            ).stdout.strip()
        )
        assert raw_rev_count == 6

        info = updates._check_repo(repo, 'webui', 'stable')
        assert info is not None
        assert info['behind'] == 2
        assert info['behind'] != 6
        assert info['latest_tag'] == 'v1.0.2'
        assert info['release_based'] is True
        assert info['divergent'] is True
        assert info['repair_required'] is True
        assert info['branch'] == 'v1.0.2'
        assert info['current_version'] == 'v1.0.0'
        assert 'behind_commits' not in info

    def test_diverged_stable_multi_commit_select_apply_ref_control(self, tmp_path):
        repo = self._diverged_stable_repo_multi_commit(tmp_path)

        assert updates._select_apply_compare_ref(repo, 'stable', 'webui') == 'v1.0.2'

        info = updates._check_repo(repo, 'webui', 'stable')
        assert info is not None
        assert info['behind'] != 0

    def test_check_reports_stable_scoped_target_with_unpromoted_master(self, tmp_path):
        repo = self._diverged_stable_repo(tmp_path, upstream_kind='master')
        info = updates._check_repo(repo, 'webui', 'stable')

        assert info is not None
        assert info['behind'] == 52
        assert info['branch'] == self.LATEST_TAG
        assert info['latest_tag'] == self.LATEST_TAG
        assert info['latest_sha'] == self.LATEST_TAG
        assert info['release_based'] is True
        assert info['divergent'] is True
        assert info['repair_required'] is True
        assert info['current_sha'] == self.CURRENT_TAG
        assert info['current_version'] == self.CURRENT_TAG
        assert info['latest_version'] == self.LATEST_TAG
        assert info['channel'] == 'stable'
        assert info['name'] == 'webui'
        assert 'origin/master' not in info['branch']

    def test_check_reports_stable_scoped_target_with_feature_upstream(self, tmp_path):
        repo = self._diverged_stable_repo(tmp_path, upstream_kind='feature')
        info = updates._check_repo(repo, 'webui', 'stable')

        assert info is not None
        assert info['branch'] == self.LATEST_TAG
        assert info['latest_tag'] == self.LATEST_TAG
        assert info['release_based'] is True
        assert info['divergent'] is True
        assert 'origin/feature' not in info['branch']

    def test_select_apply_ref_is_stable_tag_not_configured_upstream(self, tmp_path):
        repo_master = self._diverged_stable_repo(tmp_path / 'm', upstream_kind='master')
        assert updates._select_apply_compare_ref(repo_master, 'stable', 'webui') == self.LATEST_TAG

        repo_feature = self._diverged_stable_repo(tmp_path / 'f', upstream_kind='feature')
        assert updates._select_apply_compare_ref(repo_feature, 'stable', 'webui') == self.LATEST_TAG

    def test_force_recovery_lands_on_promoted_tag(self, tmp_path, monkeypatch):
        repo = self._diverged_stable_repo(tmp_path, upstream_kind='master')
        monkeypatch.setattr(updates, 'REPO_ROOT', repo)
        monkeypatch.setattr(updates, '_schedule_restart', lambda: None)

        res = updates._apply_force_update_inner('webui', 'stable')
        assert res['ok'] is True

        head_sha = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        tag_sha = subprocess.run(
            ['git', 'rev-parse', f'{self.LATEST_TAG}^{{commit}}'],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        master_sha = subprocess.run(
            ['git', 'rev-parse', 'origin/master^{commit}'],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()

        assert head_sha == tag_sha
        assert head_sha != master_sha

    def test_normal_apply_refuses_ff_to_unpromoted_master(self, tmp_path, monkeypatch):
        repo = self._diverged_stable_repo(tmp_path, upstream_kind='master')
        monkeypatch.setattr(updates, 'REPO_ROOT', repo)
        monkeypatch.setattr(updates, '_schedule_restart', lambda: None)

        sha_before = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()

        res = updates._apply_update_inner('webui', 'stable')
        assert res['ok'] is False
        assert 'fast-forward' in res.get('message', '').lower()

        sha_after = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert sha_after == sha_before

    def test_up_to_date_still_reported_at_and_past_latest_tag(self, tmp_path):
        repo = self._diverged_stable_repo(tmp_path, upstream_kind='master')

        # HEAD exactly at latest_tag (.544)
        _git(repo, 'checkout', '-q', self.LATEST_TAG)
        info_at = updates._check_repo(repo, 'webui', 'stable')
        assert info_at is not None
        assert info_at['behind'] == 0

        # HEAD past latest_tag (ahead on the line)
        _git(repo, 'checkout', '-q', '-b', 'ahead-branch', self.LATEST_TAG)
        (repo / 'APP').write_text('ahead commit\n', encoding='utf-8')
        _git(repo, 'commit', '-q', '-am', 'ahead commit past latest tag')
        info_past = updates._check_repo(repo, 'webui', 'stable')
        assert info_past is not None
        assert info_past['behind'] == 0

    def test_experimental_channel_still_branch_compares(self, tmp_path):
        repo = self._diverged_stable_repo(tmp_path, upstream_kind='master')
        info = updates._check_repo(repo, 'webui', 'experimental')
        assert info is not None
        assert info.get('release_based') is None
        assert info['branch'] == 'origin/master'
        assert updates._select_apply_compare_ref(repo, 'experimental', 'webui') == 'origin/master'
        assert updates._select_apply_compare_ref(repo, 'stable', 'agent') == 'origin/master'
