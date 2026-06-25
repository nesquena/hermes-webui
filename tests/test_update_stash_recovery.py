"""Tests for graceful stash-apply recovery in _apply_update_inner."""
from unittest.mock import patch

import api.updates as updates


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
        patch.object(updates, '_schedule_restart', side_effect=lambda: restart_calls.append(1)),
    ):
        result = updates._apply_update_inner('webui')

    assert result['ok'] is False
    assert result['conflict'] is True
    assert result['message'].startswith('Pull failed:')
    assert 'untracked working tree files would be overwritten' in result['message']
    assert ['stash', 'push', '-m', 'hermes-update-autostash'] not in call_log
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
        if args == ['reset', '--hard', 'origin/master']:
            return '', True
        raise AssertionError(f'unexpected git args: {args!r}')

    restart_calls = []

    with (
        patch.object(updates, 'REPO_ROOT', tmp_path),
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
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
        if args == ['reset', '--hard', 'origin/master']:
            return '', True
        raise AssertionError(f'unexpected git args: {args!r}')

    restart_calls = []

    with (
        patch.object(updates, 'REPO_ROOT', tmp_path),
        patch.object(updates, '_run_git', side_effect=fake_git),
        patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'),
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
        if args == ['stash', 'push', '-m', 'hermes-update-autostash']:
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
        if args == ['stash', 'push', '-m', 'hermes-update-autostash']:
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
        if args == ['stash', 'push', '-m', 'hermes-update-autostash']:
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
        if args == ['stash', 'push', '-m', 'hermes-update-autostash']:
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
        if args == ['stash', 'push', '-m', 'hermes-update-autostash']:
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
        if args == ['stash', 'push', '-m', 'hermes-update-autostash']:
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
        if args == ['stash', 'push', '-m', 'hermes-update-autostash']:
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
        if args == ['stash', 'push', '-m', 'hermes-update-autostash']:
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
        if args == ['stash', 'push', '-m', 'hermes-update-autostash']:
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
        if args == ['stash', 'push', '-m', 'hermes-update-autostash']:
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
