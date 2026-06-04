"""Tests for graceful stash-pop failure recovery in _apply_update_inner."""
from unittest.mock import patch

import api.updates as updates


def test_stash_pop_conflict_recovers_cleanly(tmp_path):
    """On stash-pop failure, conflict is reset, stash dropped, restart still scheduled."""
    (tmp_path / '.git').mkdir()

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
        if args == ['stash', 'drop']:
            return 'Dropped refs/stash@{0}', True
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
    assert 'local modifications were discarded' in result['message']
    assert result.get('restart_scheduled') is True

    assert ['reset', '--merge'] in call_log
    assert ['stash', 'drop'] in call_log

    pop_idx = call_log.index(['stash', 'pop'])
    reset_idx = call_log.index(['reset', '--merge'])
    drop_idx = call_log.index(['stash', 'drop'])
    assert pop_idx < reset_idx < drop_idx

    assert len(restart_calls) == 1


def test_stash_pop_reset_failure_returns_error(tmp_path):
    """If reset --merge also fails, return ok=False so the app does not restart into a broken tree."""
    (tmp_path / '.git').mkdir()

    def fake_git(args, path, timeout=10):
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
    assert result['stash_conflict'] is True
    assert 'Manual intervention' in result['message']
    assert len(restart_calls) == 0
