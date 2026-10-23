"""Tests for graceful stash-apply recovery in _apply_update_inner."""
import subprocess
import sys
from unittest.mock import patch

import pytest

import api.updates as updates


@pytest.fixture(autouse=True)
def _isolate_default_update_checkout(tmp_path, monkeypatch):
    """Mocked updater tests must not inspect the source checkout's ignored files."""
    repo = tmp_path / 'mock-webui'
    (repo / '.git').mkdir(parents=True)
    monkeypatch.setattr(updates, 'REPO_ROOT', repo)


def _behind_sync(**overrides):
    base = {
        'path': '/tmp/repo',
        'branch': 'master',
        'head_sha': 'abc',
        'remote_url': 'https://example.test/repo.git',
        'compare_ref': 'origin/master',
        'compare_sha': 'def',
        'ahead': 0,
        'behind': 1,
        'relationship': 'behind',
        'dirty': False,
        'dirty_tracked': False,
        'modified_files': [],
        'untracked_files': [],
        'modified_count': 0,
        'untracked_count': 0,
        'processes': [],
    }
    base.update(overrides)
    return base


def _is_autostash_push(args) -> bool:
    return (
        len(args) >= 4
        and args[0] == 'stash'
        and args[1] == 'push'
        and args[2] == '-m'
        and str(args[3]).startswith('hermes-update-autostash')
    )


def test_pull_failure_untracked_overwrite_flags_conflict(tmp_path):
    """Untracked overwrite pull failures must surface the existing force-update path."""
    (tmp_path / '.git').mkdir()
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return '', True
        if args[:2] == ['pull', '--ff-only']:
            return (
                'error: The following untracked working tree files would be overwritten by merge:\n'
                '\ttests/test_custom_provider_prefix_collisions.py\n'
                'Please move or remove them before you merge.\n'
                'Aborting',
                False,
            )
        raise AssertionError(f'unexpected git args: {args!r}')

    restart_calls = []

    with (
        patch.object(updates, 'REPO_ROOT', tmp_path),
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
        patch.object(updates, '_describe_checkout_sync', return_value=_behind_sync()),
        patch.object(updates, '_schedule_restart', side_effect=lambda: restart_calls.append(1)),
    ):
        result = updates._apply_update_inner('webui')

    assert result['ok'] is False
    assert result['conflict'] is True
    assert result['message'].startswith('Pull failed:')
    assert 'untracked working tree files would be overwritten' in result['message']
    assert not any(_is_autostash_push(args) for args in call_log)
    assert len(restart_calls) == 0


def test_apply_force_update_removes_untracked_files_before_reset(tmp_path):
    """Force update must clear untracked colliders before reset --hard (#4310)."""
    (tmp_path / '.git').mkdir()
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['checkout', '.']:
            return '', True
        if args == ['clean', '-fd']:
            return '', True
        if args[:2] == ['merge-base', '--is-ancestor']:
            # rewind guard probe: origin/master is NOT an ancestor of HEAD
            # (it's the update target ahead of HEAD) -> allow the reset.
            return '', False
        if args == ['reset', '--hard', 'origin/master']:
            return '', True
        raise AssertionError(f'unexpected git args: {args!r}')

    restart_calls = []

    with (
        patch.object(updates, 'REPO_ROOT', tmp_path),
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
        patch.object(updates, '_describe_checkout_sync', return_value=_behind_sync()),
        patch.object(updates, '_schedule_restart', side_effect=lambda: restart_calls.append(1)),
    ):
        result = updates.apply_force_update('webui')

    assert result['ok'] is True
    assert ['checkout', '.'] in call_log
    assert ['clean', '-fd'] in call_log
    assert ['reset', '--hard', 'origin/master'] in call_log
    assert call_log.index(['checkout', '.']) < call_log.index(['clean', '-fd'])
    assert call_log.index(['clean', '-fd']) < call_log.index(['reset', '--hard', 'origin/master'])
    assert len(restart_calls) == 1


def test_apply_force_update_proceeds_when_clean_fails(tmp_path):
    """A failed `git clean -fd` is NON-FATAL: the reset --hard still applies the
    update (#4914). On Windows a reserved-device-name file (nul/con/prn/…) can
    land in the tree and git can't delete it, so `clean` exits non-zero — but
    that residue is harmless and must not block the force update."""
    (tmp_path / '.git').mkdir()
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['checkout', '.']:
            return '', True
        if args == ['clean', '-fd']:
            return 'warning: failed to remove nul: Invalid argument', False
        if args[:2] == ['merge-base', '--is-ancestor']:
            # rewind guard probe: origin/master is NOT an ancestor of HEAD
            # (it's the update target ahead of HEAD) -> allow the reset.
            return '', False
        if args == ['reset', '--hard', 'origin/master']:
            return '', True
        raise AssertionError(f'unexpected git args: {args!r}')

    restart_calls = []

    with (
        patch.object(updates, 'REPO_ROOT', tmp_path),
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
        patch.object(updates, '_describe_checkout_sync', return_value=_behind_sync()),
        patch.object(updates, '_schedule_restart', side_effect=lambda: restart_calls.append(1)),
    ):
        result = updates.apply_force_update('webui')

    # Clean failed, but reset --hard succeeded → force update must SUCCEED.
    assert result['ok'] is True, result
    assert ['clean', '-fd'] in call_log
    assert ['reset', '--hard', 'origin/master'] in call_log, (
        'reset --hard must still run even though clean -fd failed (#4914)'
    )
    assert len(restart_calls) == 1


def test_stash_apply_conflict_preserves_stash(tmp_path):
    """On stash-apply conflict, stash is preserved and restart is scheduled."""
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return 'M modified_file.py', True
        if _is_autostash_push(args):
            return '', True
        if args[:2] == ['pull', '--ff-only']:
            return 'Already up to date.', True
        if args == ['stash', 'apply']:
            return 'CONFLICT (content): Merge conflict in modified_file.py', False
        if args == ['reset', '--hard', 'HEAD']:
            return '', True
        raise AssertionError(f'unexpected git args: {args!r}')

    restart_calls = []

    with (
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
        patch.object(updates, '_describe_checkout_sync', return_value=_behind_sync()),
        patch.object(updates, '_schedule_restart', side_effect=lambda: restart_calls.append(1)),
    ):
        result = updates._apply_update_inner('webui')

    assert result['ok'] is True
    assert result['stash_conflict'] is True
    assert 'git stash' in result['message']
    assert ['stash', 'apply'] in call_log
    assert ['stash', 'drop'] not in call_log
    assert ['reset', '--hard', 'HEAD'] in call_log
    assert len(restart_calls) == 1


def test_stash_apply_reset_failure_returns_error(tmp_path):
    """If reset cleanup fails, return ok=False and do not restart into a broken tree."""
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return 'M modified_file.py', True
        if _is_autostash_push(args):
            return '', True
        if args[:2] == ['pull', '--ff-only']:
            return 'Already up to date.', True
        if args == ['stash', 'apply']:
            return 'CONFLICT', False
        if args == ['reset', '--hard', 'HEAD']:
            return 'error: could not reset', False
        raise AssertionError(f'unexpected git args: {args!r}')

    restart_calls = []

    with (
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
        patch.object(updates, '_describe_checkout_sync', return_value=_behind_sync()),
        patch.object(updates, '_schedule_restart', side_effect=lambda: restart_calls.append(1)),
    ):
        result = updates._apply_update_inner('webui')

    assert result['ok'] is False
    assert result['stash_conflict'] is True
    assert 'Manual intervention' in result['message']
    assert 'reset --hard HEAD' in result['message']
    assert 'stash drop' not in result['message']
    assert len(restart_calls) == 0
    assert ['reset', '--hard', 'HEAD'] in call_log
    assert ['stash', 'drop'] not in call_log


def test_stash_apply_success_drops_and_restarts(tmp_path):
    """Happy path: stash apply succeeds, stash is dropped, and restart is scheduled."""
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return 'M modified_file.py', True
        if _is_autostash_push(args):
            return '', True
        if args[:2] == ['pull', '--ff-only']:
            return 'Already up to date.', True
        if args == ['stash', 'apply']:
            return '', True
        if args == ['stash', 'drop']:
            return '', True
        raise AssertionError(f'unexpected git args: {args!r}')

    restart_calls = []

    with (
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
        patch.object(updates, '_describe_checkout_sync', return_value=_behind_sync()),
        patch.object(updates, '_schedule_restart', side_effect=lambda: restart_calls.append(1)),
    ):
        result = updates._apply_update_inner('webui')

    assert result['ok'] is True
    assert 'stash_conflict' not in result
    assert ['stash', 'apply'] in call_log
    assert ['stash', 'drop'] in call_log
    assert len(restart_calls) == 1


def test_stash_apply_success_discloses_drop_failure(tmp_path):
    """If stash drop fails after a successful update, disclose the leftover entry."""
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return 'M modified_file.py', True
        if _is_autostash_push(args):
            return '', True
        if args[:2] == ['pull', '--ff-only']:
            return 'Already up to date.', True
        if args == ['stash', 'apply']:
            return '', True
        if args == ['stash', 'drop']:
            return 'error: could not drop stash', False
        raise AssertionError(f'unexpected git args: {args!r}')

    restart_calls = []

    with (
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
        patch.object(updates, '_describe_checkout_sync', return_value=_behind_sync()),
        patch.object(updates, '_schedule_restart', side_effect=lambda: restart_calls.append(1)),
    ):
        result = updates._apply_update_inner('webui')

    assert result['ok'] is True
    assert 'temporary stash entry may still be present' in result['message']
    assert ['stash', 'drop'] in call_log
    assert len(restart_calls) == 1


def test_pull_failure_stash_apply_recovery(tmp_path):
    """If pull fails after stashing, apply restores changes and successful apply drops the stash."""
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return 'M modified_file.py', True
        if _is_autostash_push(args):
            return '', True
        if args[:2] == ['pull', '--ff-only']:
            return 'Some unrecognized git error', False
        if args == ['stash', 'apply']:
            return '', True
        if args == ['stash', 'drop']:
            return '', True
        raise AssertionError(f'unexpected git args: {args!r}')

    restart_calls = []

    with (
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
        patch.object(updates, '_describe_checkout_sync', return_value=_behind_sync()),
        patch.object(updates, '_schedule_restart', side_effect=lambda: restart_calls.append(1)),
    ):
        result = updates._apply_update_inner('webui')

    assert result['ok'] is False
    assert result['message'].startswith('Pull failed:')
    assert 'Local webui modifications were restored to the working tree' in result['message']
    assert ['stash', 'apply'] in call_log
    assert ['stash', 'drop'] in call_log
    assert ['stash', 'pop'] not in call_log
    assert len(restart_calls) == 0


def test_pull_failure_stash_apply_recovery_discloses_drop_failure(tmp_path):
    """If pull fails and stash drop fails after restore, disclose the leftover entry."""
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return 'M modified_file.py', True
        if _is_autostash_push(args):
            return '', True
        if args[:2] == ['pull', '--ff-only']:
            return 'Some unrecognized git error', False
        if args == ['stash', 'apply']:
            return '', True
        if args == ['stash', 'drop']:
            return 'error: could not drop stash', False
        raise AssertionError(f'unexpected git args: {args!r}')

    with (
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
        patch.object(updates, '_describe_checkout_sync', return_value=_behind_sync()),
        patch.object(updates, '_schedule_restart'),
    ):
        result = updates._apply_update_inner('webui')

    assert result['ok'] is False
    assert 'Local webui modifications were restored to the working tree' in result['message']
    assert 'temporary stash entry may still be present' in result['message']
    assert ['stash', 'drop'] in call_log


def test_pull_failure_stash_apply_recovery_warns_before_diverged_reset(tmp_path):
    """Diverged recovery must warn when local changes were restored before reset advice."""
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return 'M modified_file.py', True
        if _is_autostash_push(args):
            return '', True
        if args[:2] == ['pull', '--ff-only']:
            return 'Not possible to fast-forward, aborting.', False
        if args == ['stash', 'apply']:
            return '', True
        if args == ['stash', 'drop']:
            return '', True
        raise AssertionError(f'unexpected git args: {args!r}')

    with (
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
        patch.object(updates, '_describe_checkout_sync', return_value=_behind_sync()),
        patch.object(updates, '_schedule_restart'),
    ):
        result = updates._apply_update_inner('webui')

    assert result['ok'] is False
    assert result['diverged'] is True
    assert 'Local webui modifications were restored to the working tree' in result['message']
    assert 'save or stash them before running destructive recovery commands' in result['message']
    assert result['message'].index('save or stash') < result['message'].index('reset --hard')
    assert ['stash', 'drop'] in call_log


def test_pull_failure_stash_apply_conflict_cleans_worktree(tmp_path):
    """If restoring local changes conflicts after pull failure, clean markers and preserve stash."""
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return 'M modified_file.py', True
        if _is_autostash_push(args):
            return '', True
        if args[:2] == ['pull', '--ff-only']:
            return 'Some unrecognized git error', False
        if args == ['stash', 'apply']:
            return 'CONFLICT (content): Merge conflict in modified_file.py', False
        if args == ['reset', '--hard', 'HEAD']:
            return '', True
        raise AssertionError(f'unexpected git args: {args!r}')

    restart_calls = []

    with (
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
        patch.object(updates, '_describe_checkout_sync', return_value=_behind_sync()),
        patch.object(updates, '_schedule_restart', side_effect=lambda: restart_calls.append(1)),
    ):
        result = updates._apply_update_inner('webui')

    assert result['ok'] is False
    assert result['stash_conflict'] is True
    assert result['message'].startswith('Pull failed, and your local webui modifications conflicted')
    assert 'index and tracked files were restored to HEAD' in result['message']
    assert 'Pull error: Some unrecognized git error' in result['message']
    assert ['stash', 'apply'] in call_log
    assert ['reset', '--hard', 'HEAD'] in call_log
    assert ['stash', 'drop'] not in call_log
    assert len(restart_calls) == 0


def test_pull_failure_stash_apply_conflict_preserves_diverged_flag(tmp_path):
    """A combined restore conflict must not hide the force-update affordance."""
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return 'M modified_file.py', True
        if _is_autostash_push(args):
            return '', True
        if args[:2] == ['pull', '--ff-only']:
            return 'Not possible to fast-forward, aborting.', False
        if args == ['stash', 'apply']:
            return 'CONFLICT (content): Merge conflict in modified_file.py', False
        if args == ['reset', '--hard', 'HEAD']:
            return '', True
        raise AssertionError(f'unexpected git args: {args!r}')

    with (
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
        patch.object(updates, '_describe_checkout_sync', return_value=_behind_sync()),
        patch.object(updates, '_schedule_restart'),
    ):
        result = updates._apply_update_inner('webui')

    assert result['ok'] is False
    assert result['stash_conflict'] is True
    assert result['diverged'] is True
    assert ['reset', '--hard', 'HEAD'] in call_log


def test_pull_failure_stash_apply_conflict_reset_failure_returns_error(tmp_path):
    """If pull-failure rollback cleanup fails, return an explicit manual recovery error."""
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return 'M modified_file.py', True
        if _is_autostash_push(args):
            return '', True
        if args[:2] == ['pull', '--ff-only']:
            return 'Some unrecognized git error', False
        if args == ['stash', 'apply']:
            return 'CONFLICT (content): Merge conflict in modified_file.py', False
        if args == ['reset', '--hard', 'HEAD']:
            return 'error: could not reset', False
        raise AssertionError(f'unexpected git args: {args!r}')

    restart_calls = []

    with (
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
        patch.object(updates, '_describe_checkout_sync', return_value=_behind_sync()),
        patch.object(updates, '_schedule_restart', side_effect=lambda: restart_calls.append(1)),
    ):
        result = updates._apply_update_inner('webui')

    assert result['ok'] is False
    assert result['stash_conflict'] is True
    assert 'Manual intervention needed' in result['message']
    assert 'reset --hard HEAD' in result['message']
    assert 'Pull error: Some unrecognized git error' in result['message']
    assert ['stash', 'apply'] in call_log
    assert ['reset', '--hard', 'HEAD'] in call_log
    assert ['stash', 'drop'] not in call_log
    assert len(restart_calls) == 0


def test_agent_ahead_creates_backup_and_safe_resets(tmp_path):
    """Agent updates with local-only commits backup HEAD then reset to origin."""
    (tmp_path / '.git').mkdir()
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return '', True
        if args[:2] == ['branch', 'hermes-update-backup'] or (
            len(args) >= 2 and args[0] == 'branch' and str(args[1]).startswith('hermes-update-backup/')
        ):
            return '', True
        if args[:2] == ['rev-parse', 'HEAD']:
            return 'localdeadbeef', True
        if args == ['reset', '--hard', 'origin/main']:
            return '', True
        raise AssertionError(f'unexpected git args: {args!r}')

    restart_calls = []
    gateway_calls = []

    with (
        patch.object(updates, '_AGENT_DIR', tmp_path),
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/main'),
        patch.object(
            updates,
            '_describe_checkout_sync',
            return_value=_behind_sync(
                relationship='ahead',
                ahead=1,
                behind=3,
                compare_ref='origin/main',
            ),
        ),
        patch.object(updates, '_schedule_restart', side_effect=lambda: restart_calls.append(1)),
        patch.object(
            updates,
            '_ensure_gateway_restart_for_agent_update',
            side_effect=lambda: gateway_calls.append(1) or (True, {'status': 'restarted'}),
        ),
    ):
        result = updates._apply_update_inner('agent')

    assert result['ok'] is True
    assert result['safe_reset'] is True
    assert result['backup_branch']
    assert result['backup_branch'].startswith('hermes-update-backup/')
    assert ['reset', '--hard', 'origin/main'] in call_log
    assert any(args[0] == 'branch' for args in call_log)
    assert len(restart_calls) == 1
    assert len(gateway_calls) == 1


def test_webui_ahead_fails_closed_after_backup(tmp_path):
    """WebUI keeps intentional local commits; backup then fail with recovery path."""
    (tmp_path / '.git').mkdir()
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return '', True
        if args[0] == 'branch' and str(args[1]).startswith('hermes-update-backup/'):
            return '', True
        if args[:2] == ['rev-parse', 'HEAD']:
            return 'localdeadbeef', True
        raise AssertionError(f'unexpected git args: {args!r}')

    with (
        patch.object(updates, 'REPO_ROOT', tmp_path),
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
        patch.object(
            updates,
            '_describe_checkout_sync',
            return_value=_behind_sync(relationship='ahead', ahead=2, behind=5),
        ),
        patch.object(updates, '_schedule_restart'),
    ):
        result = updates._apply_update_inner('webui')

    assert result['ok'] is False
    assert result['diverged'] is True
    assert result['backup_branch']
    assert 'preserved on backup branch' in result['message']
    assert ['reset', '--hard', 'origin/master'] not in call_log


def test_identical_sync_is_idempotent_noop(tmp_path):
    (tmp_path / '.git').mkdir()
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        raise AssertionError(f'unexpected git args: {args!r}')

    with (
        patch.object(updates, 'REPO_ROOT', tmp_path),
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
        patch.object(
            updates,
            '_describe_checkout_sync',
            return_value=_behind_sync(relationship='identical', ahead=0, behind=0),
        ),
        patch.object(updates, '_schedule_restart'),
    ):
        result = updates._apply_update_inner('webui')

    assert result['ok'] is True
    assert result['up_to_date'] is True
    assert result['relationship'] == 'identical'
    assert not any(args[:2] == ['pull', '--ff-only'] for args in call_log)


def test_diagnose_checkout_reports_sync_fields(tmp_path):
    (tmp_path / '.git').mkdir()

    with (
        patch.object(updates, '_AGENT_DIR', tmp_path),
        patch.object(
            updates,
            '_select_apply_compare_ref',
            return_value='origin/main',
        ),
        patch.object(
            updates,
            '_describe_checkout_sync',
            return_value=_behind_sync(
                path=str(tmp_path),
                branch='main',
                relationship='behind',
                ahead=0,
                behind=2,
                compare_ref='origin/main',
                modified_files=['foo.py'],
                modified_count=1,
            ),
        ),
    ):
        report = updates.diagnose_checkout('agent')

    assert report['ok'] is True
    assert report['target'] == 'agent'
    assert report['relationship'] == 'behind'
    assert report['behind'] == 2
    assert report['modified_files'] == ['foo.py']


# ── Real-git lossless snapshot regressions (reviewer exact-head blockers) ─────


_DIRTY_BYTES = b'dirty-tracked-UNIQUE-\x00-bytes\n'
_UNTRACKED_LOCAL = b'untracked-local-UNIQUE-\x00-bytes\n'
_UNTRACKED_UPSTREAM = b'untracked-upstream-OVERWRITE\n'
_TRACKED_UPSTREAM = b'tracked-upstream-changed\n'
_MUTATING_GIT = {'reset', 'checkout', 'clean', 'pull', 'merge', 'rebase'}


# Windows spawns a console host for every git child unless this flag is set.
# These tests run hundreds of git commands, so without it a local run carpets
# the desktop with popup windows. Zero on POSIX, where subprocess only rejects
# a NON-zero creationflags value.
_NO_CONSOLE_WINDOW = (
    getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0
)


def _git_raw(repo, args, check=True):
    proc = subprocess.run(
        ['git', *args],
        cwd=str(repo),
        capture_output=True,
        check=False,
        creationflags=_NO_CONSOLE_WINDOW,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f'git {args} failed: {proc.stderr.decode("utf-8", "replace")}'
        )
    return proc


def _git(repo, *args):
    return _git_raw(repo, args).stdout.decode('utf-8', 'replace').strip()


def _git_bytes(repo, *args):
    return _git_raw(repo, args).stdout


def _configure_git(repo):
    # One process instead of four: config --local accepts repeated pairs via
    # separate invocations only, so write the file directly.
    config = repo / ('config' if (repo / 'HEAD').exists() else '.git/config')
    with open(config, 'a', encoding='utf-8') as fh:
        fh.write(
            '[user]\n\temail = t@t.co\n\tname = Test\n'
            '[commit]\n\tgpgsign = false\n'
            '[core]\n\tautocrlf = false\n'
        )


def _make_agent_pair(tmp_path):
    """Bare origin + one working clone, built without intermediate seed clones."""
    origin = tmp_path / 'origin.git'
    _git(tmp_path, 'init', '--bare', '-q', '-b', 'main', str(origin))
    agent = tmp_path / 'agent'
    agent.mkdir()
    _git(agent, 'init', '-q', '-b', 'main')
    _configure_git(agent)
    (agent / 'README').write_bytes(b'base-readme\n')
    (agent / 'tracked.txt').write_bytes(b'tracked-clean\n')
    _git(agent, 'add', 'README', 'tracked.txt')
    _git(agent, 'commit', '-q', '-m', 'base')
    _git(agent, 'remote', 'add', 'origin', str(origin))
    _git(agent, 'push', '-q', '-u', 'origin', 'main')
    return origin, agent


def _origin_work_clone(tmp_path, origin):
    """Reused upstream working clone — one per test, not one per upstream commit."""
    work = tmp_path / 'origin-work'
    if not work.exists():
        _git(tmp_path, 'clone', '-q', str(origin), str(work))
        _configure_git(work)
    return work


def _advance_origin(tmp_path, origin, files, message):
    work = _origin_work_clone(tmp_path, origin)
    for name, content in files.items():
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        _git(work, 'add', name)
    _git(work, 'commit', '-q', '-m', message)
    _git(work, 'push', '-q', 'origin', 'HEAD:main')


def _patch_agent_update(monkeypatch, agent, *, gateway=None, module=None):
    module = updates if module is None else module
    monkeypatch.setattr(module, '_AGENT_DIR', agent)
    monkeypatch.setattr(module, 'REPO_ROOT', agent)
    monkeypatch.setattr(
        module, '_select_apply_compare_ref',
        lambda path, channel='stable', target=None: 'origin/main',
    )
    monkeypatch.setattr(module, '_schedule_restart', lambda delay=2.0: None)
    if gateway is None:
        gateway = lambda: (True, {'status': 'completed'})
    monkeypatch.setattr(module, '_ensure_gateway_restart_for_agent_update', gateway)


def _prepare_history(tmp_path, history, *, dirty_tracked=False, untracked_collider=False):
    origin, agent = _make_agent_pair(tmp_path)
    if history in {'ahead', 'diverged'}:
        (agent / 'local-only.txt').write_bytes(b'local-only-commit\n')
        _git(agent, 'add', 'local-only.txt')
        _git(agent, 'commit', '-q', '-m', 'local-only')

    origin_files = {}
    if history in {'behind', 'diverged'}:
        origin_files['upstream-new.txt'] = b'upstream-new\n'
    if untracked_collider:
        origin_files['collide.txt'] = _UNTRACKED_UPSTREAM
    if dirty_tracked and history in {'behind', 'diverged'}:
        origin_files['tracked.txt'] = _TRACKED_UPSTREAM
    if origin_files:
        _advance_origin(tmp_path, origin, origin_files, f'upstream-{history}')

    if dirty_tracked:
        (agent / 'tracked.txt').write_bytes(_DIRTY_BYTES)
    if untracked_collider:
        (agent / 'collide.txt').write_bytes(_UNTRACKED_LOCAL)
    return origin, agent


def _recovery_has_bytes(agent, result, rel, expected):
    branch = result.get('recovery_ref') or result.get('backup_branch')
    assert branch, f'expected recovery ref, got {result!r}'
    assert _git_bytes(agent, 'show', f'{branch}:{rel}') == expected
    # The recovery ref itself must still exist after the update attempt.
    _git(agent, 'rev-parse', '--verify', branch)
    return branch


@pytest.mark.parametrize('force', [False, True])
@pytest.mark.parametrize('rel,ignored', [
    ('.hermes-local', False), ('.github/local.json', False),
    (' local file.txt', False), ('ignored.bin', True),
    ('cache/local.bin', True),
])
def test_real_repo_review_colliders(tmp_path, monkeypatch, force, rel, ignored):
    origin, agent = _make_agent_pair(tmp_path)
    if ignored:
        (agent / '.git/info/exclude').write_text(rel.split('/')[0] + '\n')
    _advance_origin(tmp_path, origin, {rel: _UNTRACKED_UPSTREAM}, 'track-collider')
    # Divergence exercises the normal Agent safe-reset as well as force reset.
    (agent / 'local-only.txt').write_bytes(b'local commit\n')
    _git(agent, 'add', 'local-only.txt')
    _git(agent, 'commit', '-q', '-m', 'local')
    local = agent / rel
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(_UNTRACKED_LOCAL)
    head = _git(agent, 'rev-parse', 'HEAD')
    index = (agent / '.git/index').read_bytes()
    _patch_agent_update(monkeypatch, agent)
    result = (updates.apply_force_update('agent') if force
              else updates._apply_update_inner('agent'))
    if ignored:
        assert result['ok'] is False, result
        assert _git(agent, 'rev-parse', 'HEAD') == head
        assert (agent / '.git/index').read_bytes() == index
        assert local.read_bytes() == _UNTRACKED_LOCAL
    else:
        assert result['ok'] is True, result
        _recovery_has_bytes(agent, result, rel, _UNTRACKED_LOCAL)


@pytest.mark.parametrize('force', [False, True])
@pytest.mark.parametrize('local_rel,target_rel', [
    ('cache/item', 'cache'), ('cache', 'cache/item'),
])
def test_real_repo_ignored_file_directory_colliders(
    tmp_path, monkeypatch, force, local_rel, target_rel,
):
    origin, agent = _make_agent_pair(tmp_path)
    (agent / '.git/info/exclude').write_text('cache\n')
    _advance_origin(tmp_path, origin, {target_rel: b'upstream\n'}, 'track-cache')
    local = agent / local_rel
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(_UNTRACKED_LOCAL)
    head = _git(agent, 'rev-parse', 'HEAD')
    index = (agent / '.git/index').read_bytes()
    _patch_agent_update(monkeypatch, agent)
    result = (updates.apply_force_update('agent') if force
              else updates._apply_update_inner('agent'))
    assert result['ok'] is False, result
    assert _git(agent, 'rev-parse', 'HEAD') == head
    assert (agent / '.git/index').read_bytes() == index
    assert local.read_bytes() == _UNTRACKED_LOCAL


def test_real_repo_snapshot_staged_and_dirty_names(tmp_path):
    _origin, agent = _make_agent_pair(tmp_path)
    names = ['.local', ' leading space', 'unicode-\u00e9']
    if sys.platform != 'win32':
        names += ['line\nbreak', 'tab\tname', 'quote"name', 'carriage\rreturn']
    for name in names:
        (agent / name).write_bytes(b'base\n')
    _git(agent, 'add', '--', *names)
    _git(agent, 'commit', '-q', '-m', 'unusual tracked names')
    for name in names:
        (agent / name).write_bytes(_DIRTY_BYTES)
    _git(agent, 'add', '--', names[0])
    sync = updates._describe_checkout_sync(agent, 'HEAD')
    snapshot = updates._create_update_recovery_snapshot(agent, sync)
    assert snapshot['ok'], snapshot
    for name in names:
        _recovery_has_bytes(agent, {'recovery_ref': snapshot['branch']}, name, _DIRTY_BYTES)


def test_real_repo_snapshot_preserves_bytes_despite_text_attributes(tmp_path):
    _origin, agent = _make_agent_pair(tmp_path)
    (agent / '.gitattributes').write_bytes(b'tracked.txt text eol=lf\n')
    _git(agent, 'add', '.gitattributes')
    _git(agent, 'commit', '-q', '-m', 'normalize text')
    dirty = b'exact CRLF bytes\r\nsecond line\r\n'
    (agent / 'tracked.txt').write_bytes(dirty)
    sync = updates._describe_checkout_sync(agent, 'HEAD')
    snapshot = updates._create_update_recovery_snapshot(agent, sync)
    assert snapshot['ok'], snapshot
    _recovery_has_bytes(agent, {'recovery_ref': snapshot['branch']}, 'tracked.txt', dirty)


@pytest.mark.parametrize('force', [False, True])
def test_real_repo_noncolliding_ignored_file_survives(tmp_path, monkeypatch, force):
    _origin, agent = _prepare_history(tmp_path, 'diverged')
    (agent / '.git/info/exclude').write_text('cache.bin\n')
    (agent / 'cache.bin').write_bytes(_UNTRACKED_LOCAL)
    _patch_agent_update(monkeypatch, agent)
    result = (updates.apply_force_update('agent') if force
              else updates._apply_update_inner('agent'))
    assert result['ok'], result
    assert (agent / 'cache.bin').read_bytes() == _UNTRACKED_LOCAL


@pytest.mark.parametrize('force', [False, True])
@pytest.mark.parametrize('failure', ['dirty', 'staged', 'untracked', 'empty', 'status'])
def test_real_repo_enumeration_failure_is_closed(tmp_path, monkeypatch, force, failure):
    _origin, agent = _prepare_history(tmp_path, 'diverged', dirty_tracked=True)
    head = _git(agent, 'rev-parse', 'HEAD')
    index = (agent / '.git/index').read_bytes()
    real_run = updates._run_git_env

    def fail_diff(args, cwd, **kwargs):
        if ((failure == 'dirty' and args[0] == 'diff' and '--cached' not in args)
                or (failure == 'staged' and '--cached' in args)
                or (failure == 'untracked' and args[0] == 'ls-files' and '--ignored' not in args)
                or (failure == 'status' and args[0] == 'status' and '-z' in args)):
            return 'injected enumeration failure', False
        if failure == 'empty' and args[0] in {'diff', 'ls-files'}:
            return '', True
        return real_run(args, cwd, **kwargs)

    monkeypatch.setattr(updates, '_run_git_env', fail_diff)
    _patch_agent_update(monkeypatch, agent)
    result = (updates.apply_force_update('agent') if force
              else updates._apply_update_inner('agent'))
    assert result['ok'] is False, result
    assert _git(agent, 'rev-parse', 'HEAD') == head
    assert (agent / '.git/index').read_bytes() == index
    assert (agent / 'tracked.txt').read_bytes() == _DIRTY_BYTES


@pytest.mark.parametrize('reimport_updates', [False, True])
def test_diagnose_route_omits_secrets(tmp_path, monkeypatch, reimport_updates):
    import importlib
    import json
    from types import SimpleNamespace
    from urllib.parse import urlparse
    import api.routes as routes

    if reimport_updates:
        import api
        # Other tests can re-import this module after our collection-time import.
        # Preserve both import registries so this scenario cannot leak outward.
        monkeypatch.setattr(api, 'updates', sys.modules['api.updates'])
        monkeypatch.delitem(sys.modules, 'api.updates')
        importlib.import_module('api.updates')

    route_updates = importlib.import_module('api.updates')

    _origin, agent = _make_agent_pair(tmp_path)
    _git(agent, 'remote', 'set-url', 'origin',
         'https://user:USERINFO_SECRET@example.test/repo.git?token=QUERY_SECRET')
    _patch_agent_update(monkeypatch, agent, module=route_updates)
    monkeypatch.setattr(routes, '_handle_extension_sidecar_proxy', lambda *a: False)
    monkeypatch.setattr(routes, 'j', lambda handler, payload: payload)
    real_run = route_updates.subprocess.run

    def fake_ps(args, **kwargs):
        if args[0] == 'ps':
            return SimpleNamespace(returncode=0, stdout=
                f'123 python {agent}/server.py --api-key ARGV_SECRET\n')
        return real_run(args, **kwargs)

    monkeypatch.setattr(route_updates.subprocess, 'run', fake_ps)
    payload = routes.handle_get(SimpleNamespace(headers={}),
                                urlparse('/api/updates/diagnose?target=agent'))
    encoded = json.dumps(payload)
    assert payload['ok'] is True, payload
    assert payload['processes'] == [{'pid': 123}]
    for secret in ('USERINFO_SECRET', 'QUERY_SECRET', 'ARGV_SECRET'):
        assert secret not in encoded


def test_standalone_diagnose_omits_secrets(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from scripts import diagnose_agent_checkout as diagnostic

    _origin, agent = _make_agent_pair(tmp_path)
    _git(agent, 'remote', 'set-url', 'origin',
         'https://user:USERINFO_SECRET@example.test/repo.git?token=QUERY_SECRET')
    real_run = diagnostic.subprocess.run

    def fake_ps(args, **kwargs):
        if args[0] == 'ps':
            return SimpleNamespace(returncode=0, stdout=
                f'123 python {agent}/server.py --api-key ARGV_SECRET\n')
        return real_run(args, **kwargs)

    monkeypatch.setattr(diagnostic.subprocess, 'run', fake_ps)
    payload = diagnostic._standalone_diagnose(agent)
    encoded = json.dumps(payload)
    assert payload['ok'] is True
    assert payload['processes'] == [{'pid': 123}]
    for secret in ('USERINFO_SECRET', 'QUERY_SECRET', 'ARGV_SECRET'):
        assert secret not in encoded


@pytest.mark.parametrize('history', ['behind', 'ahead', 'diverged'])
@pytest.mark.parametrize('dirt', ['dirty_tracked', 'untracked_collider', 'both'])
def test_real_repo_normal_update_preserves_local_bytes_or_aborts(tmp_path, monkeypatch, history, dirt):
    """Normal Agent apply must not destroy dirty tracked or untracked collider bytes."""
    dirty_tracked = dirt in {'dirty_tracked', 'both'}
    untracked_collider = dirt in {'untracked_collider', 'both'}
    _origin, agent = _prepare_history(
        tmp_path, history,
        dirty_tracked=dirty_tracked,
        untracked_collider=untracked_collider,
    )
    head_before = _git(agent, 'rev-parse', 'HEAD')
    _patch_agent_update(monkeypatch, agent)

    result = updates._apply_update_inner('agent')

    if untracked_collider:
        # Upstream began tracking collide.txt. Local bytes must survive in the
        # recovery artifact (or the update must abort before HEAD moves).
        if not result.get('ok'):
            assert _git(agent, 'rev-parse', 'HEAD') == head_before
            assert (agent / 'collide.txt').read_bytes() == _UNTRACKED_LOCAL
        else:
            branch = _recovery_has_bytes(agent, result, 'collide.txt', _UNTRACKED_LOCAL)
            recovered = _git_bytes(agent, 'show', f'{branch}:collide.txt')
            assert recovered == _UNTRACKED_LOCAL
            assert recovered != _UNTRACKED_UPSTREAM
            assert 'collide.txt' in (result.get('message') or '') or result.get('untracked_colliders')
            assert _git(agent, 'rev-parse', 'HEAD') == _git(agent, 'rev-parse', 'origin/main')

    if dirty_tracked and result.get('ok') and not result.get('stash_conflict'):
        # Stash restore may put dirty bytes back when they do not collide.
        restored = (agent / 'tracked.txt').read_bytes()
        if restored != _DIRTY_BYTES:
            _recovery_has_bytes(agent, result, 'tracked.txt', _DIRTY_BYTES)
    elif dirty_tracked and result.get('ok') and result.get('stash_conflict'):
        _recovery_has_bytes(agent, result, 'tracked.txt', _DIRTY_BYTES)
        assert 'git -C' in result['message']
    elif dirty_tracked and not result.get('ok'):
        assert (agent / 'tracked.txt').read_bytes() == _DIRTY_BYTES or result.get('backup_branch')


@pytest.mark.parametrize('history', ['behind', 'ahead', 'diverged'])
@pytest.mark.parametrize('dirt', ['dirty_tracked', 'both'])
def test_real_repo_force_update_recovery_reloads_dirty_bytes(tmp_path, monkeypatch, history, dirt):
    """Force update must put exact dirty tracked bytes on the returned recovery ref."""
    _origin, agent = _prepare_history(
        tmp_path, history,
        dirty_tracked=True,
        untracked_collider=(dirt == 'both'),
    )
    wt_before = (agent / 'tracked.txt').read_bytes()
    assert wt_before == _DIRTY_BYTES
    _patch_agent_update(monkeypatch, agent)

    result = updates.apply_force_update('agent')

    if result.get('refused_rewind'):
        # Ahead of origin/main: rewind guard must not mutate dirty bytes.
        assert (agent / 'tracked.txt').read_bytes() == _DIRTY_BYTES
        return

    assert result.get('backup_branch') or result.get('recovery_ref'), result
    _recovery_has_bytes(agent, result, 'tracked.txt', _DIRTY_BYTES)
    if dirt == 'both' and (agent / 'collide.txt').exists() is False:
        _recovery_has_bytes(agent, result, 'collide.txt', _UNTRACKED_LOCAL)


@pytest.mark.parametrize('history', ['behind', 'ahead', 'diverged'])
def test_real_repo_snapshot_failure_aborts_before_mutation(tmp_path, monkeypatch, history):
    _origin, agent = _prepare_history(
        tmp_path, history, dirty_tracked=True, untracked_collider=True,
    )
    head_before = _git(agent, 'rev-parse', 'HEAD')
    dirty_before = (agent / 'tracked.txt').read_bytes()
    untracked_before = (agent / 'collide.txt').read_bytes()
    mutating = []
    real_run = updates._run_git

    def spy(args, cwd, timeout=10):
        if args and args[0] in _MUTATING_GIT:
            mutating.append(list(args))
        return real_run(args, cwd, timeout=timeout)

    monkeypatch.setattr(updates, '_run_git', spy)
    monkeypatch.setattr(
        updates, '_create_update_recovery_snapshot',
        lambda path, sync=None: {'ok': False, 'error': 'injected snapshot failure'},
    )
    _patch_agent_update(monkeypatch, agent)

    result = updates._apply_update_inner('agent')
    assert result['ok'] is False
    assert 'aborted before mutating' in result['message'] or 'recovery snapshot' in result['message']
    assert _git(agent, 'rev-parse', 'HEAD') == head_before
    assert (agent / 'tracked.txt').read_bytes() == dirty_before
    assert (agent / 'collide.txt').read_bytes() == untracked_before
    assert mutating == []

    mutating.clear()
    result_force = updates.apply_force_update('agent')
    assert result_force['ok'] is False
    assert _git(agent, 'rev-parse', 'HEAD') == head_before
    assert (agent / 'tracked.txt').read_bytes() == dirty_before
    if result_force.get('refused_rewind'):
        assert (agent / 'collide.txt').read_bytes() == untracked_before
    else:
        assert 'aborted' in result_force['message']
        assert not any(args and args[0] in {'reset', 'checkout', 'clean'} for args in mutating)


@pytest.mark.parametrize('history', ['behind', 'ahead', 'diverged'])
def test_real_repo_stash_restore_conflict_retains_recovery_ref(tmp_path, monkeypatch, history):
    origin, agent = _prepare_history(
        tmp_path, history, dirty_tracked=True, untracked_collider=False,
    )
    # Ensure origin also changed tracked.txt so stash restore conflicts after pull/reset.
    if history == 'ahead':
        _advance_origin(tmp_path, origin, {'tracked.txt': _TRACKED_UPSTREAM}, 'conflict-ahead')
    _patch_agent_update(monkeypatch, agent)

    result = updates._apply_update_inner('agent')
    branch = result.get('recovery_ref') or result.get('backup_branch')
    assert branch, result
    _git(agent, 'rev-parse', '--verify', branch)
    assert _git_bytes(agent, 'show', f'{branch}:tracked.txt') == _DIRTY_BYTES
    if result.get('stash_conflict') or not result.get('ok'):
        assert 'git -C' in result['message']
        assert branch in result['message']


@pytest.mark.parametrize('history', ['behind', 'ahead', 'diverged'])
def test_real_repo_gateway_restart_failure_retains_recovery_ref(tmp_path, monkeypatch, history):
    _origin, agent = _prepare_history(
        tmp_path, history, dirty_tracked=True, untracked_collider=True,
    )
    _patch_agent_update(
        monkeypatch, agent,
        gateway=lambda: (False, {
            'status': 'failed',
            'message': 'gateway restart exploded',
        }),
    )

    result = updates._apply_update_inner('agent')
    assert result['ok'] is False
    assert result.get('gateway_restart') == 'failed'
    branch = result.get('recovery_ref') or result.get('backup_branch')
    assert branch, result
    _git(agent, 'rev-parse', '--verify', branch)
    assert _git_bytes(agent, 'show', f'{branch}:tracked.txt') == _DIRTY_BYTES
    assert _git_bytes(agent, 'show', f'{branch}:collide.txt') == _UNTRACKED_LOCAL
    assert branch in result['message']
    assert 'git -C' in result['message']

    # Force update needs its own checkout: the apply above already moved this
    # one to upstream, so a force run there would have nothing dirty to protect.
    force_root = tmp_path / 'force'
    force_root.mkdir()
    _origin2, agent2 = _prepare_history(
        force_root, history, dirty_tracked=True, untracked_collider=True,
    )
    _patch_agent_update(
        monkeypatch, agent2,
        gateway=lambda: (False, {
            'status': 'failed',
            'message': 'gateway restart exploded',
        }),
    )

    force = updates.apply_force_update('agent')
    if force.get('refused_rewind'):
        assert (agent2 / 'tracked.txt').read_bytes() == _DIRTY_BYTES
        return
    assert force.get('gateway_restart') == 'failed'
    force_branch = force.get('recovery_ref') or force.get('backup_branch')
    assert force_branch, force
    _git(agent2, 'rev-parse', '--verify', force_branch)
    assert _git_bytes(agent2, 'show', f'{force_branch}:tracked.txt') == _DIRTY_BYTES
    assert _git_bytes(agent2, 'show', f'{force_branch}:collide.txt') == _UNTRACKED_LOCAL
    assert force_branch in force['message']


def test_real_repo_local_commit_plus_untracked_collider_with_newly_tracked_upstream(
    tmp_path, monkeypatch,
):
    """Exact reviewer probe: local-only commit + untracked path origin starts tracking."""
    _origin, agent = _prepare_history(
        tmp_path, 'diverged', dirty_tracked=False, untracked_collider=True,
    )
    assert (agent / 'local-only.txt').read_bytes() == b'local-only-commit\n'
    assert (agent / 'collide.txt').read_bytes() == _UNTRACKED_LOCAL
    head_before = _git(agent, 'rev-parse', 'HEAD')
    _patch_agent_update(monkeypatch, agent)

    result = updates._apply_update_inner('agent')
    if not result.get('ok'):
        assert _git(agent, 'rev-parse', 'HEAD') == head_before
        assert (agent / 'collide.txt').read_bytes() == _UNTRACKED_LOCAL
        return
    branch = _recovery_has_bytes(agent, result, 'collide.txt', _UNTRACKED_LOCAL)
    assert _git_bytes(agent, 'show', f'{branch}:local-only.txt') == b'local-only-commit\n'
    recovered = _git_bytes(agent, 'show', f'{branch}:collide.txt')
    assert recovered == _UNTRACKED_LOCAL
    assert recovered != _UNTRACKED_UPSTREAM
