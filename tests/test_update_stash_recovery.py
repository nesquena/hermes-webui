"""Tests for graceful stash-pop failure recovery in _apply_update_inner."""
from unittest.mock import patch

import api.updates as updates


def test_stash_pop_conflict_preserves_stash(tmp_path):
    """On stash-pop failure, stash is preserved and no restart is scheduled."""
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return 'M modified_file.py', True
        if args == ['stash']:
            return '', True
        if args[:2] == ['pull', '--ff-only']:
            return 'Already up to date.', True
        if args == ['stash', 'pop']:
            return 'CONFLICT (content): Merge conflict in modified_file.py', False
        if args == ['reset', '--merge']:
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
    assert 'stash@{0}' in result['message']
    assert ['stash', 'drop'] not in call_log
    assert ['reset', '--merge'] in call_log
    assert len(restart_calls) == 0


def test_stash_pop_reset_failure_returns_error(tmp_path):
    """If reset --merge also fails, return ok=False so the app does not restart into a broken tree."""
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return 'M modified_file.py', True
        if args == ['stash']:
            return '', True
        if args[:2] == ['pull', '--ff-only']:
            return 'Already up to date.', True
        if args == ['stash', 'pop']:
            return 'CONFLICT', False
        if args == ['reset', '--merge']:
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
    assert 'stash drop' not in result['message']
    assert len(restart_calls) == 0
    assert ['stash', 'drop'] not in call_log


def test_stash_pop_success_still_restarts(tmp_path):
    """Happy path: stash pop succeeds, restart is scheduled."""
    call_log = []

    def fake_git(args, path, timeout=10):
        call_log.append(args)
        if args[:2] == ['fetch', 'origin']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return 'M modified_file.py', True
        if args == ['stash']:
            return '', True
        if args[:2] == ['pull', '--ff-only']:
            return 'Already up to date.', True
        if args == ['stash', 'pop']:
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
    assert len(restart_calls) == 1
