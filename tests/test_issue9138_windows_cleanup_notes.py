"""Issue #9138: Windows job cleanup notes must reach the user and the log.

``run_network_command_windows`` records its best-effort Job Object teardown
diagnostics with ``add_note`` — on ``exc.__cause__`` on the timeout path and on
the ``OSError`` raised when a successful command's job close stays unconfirmed.
``_run_git`` used to drop both, so the notes never reached the update response
or the log, and the success-path teardown failure was reported as
``git failed to start``.

These tests drive the real ``_run_git`` branch code: only the pre-flight git
helpers are stubbed (they would run git themselves) and the network runner is
replaced by the exception under test, the same way the Windows runner is
exercised on a POSIX host.
"""

import logging
import subprocess
from unittest.mock import patch

import pytest

import api.updates as updates

GITHUB_TOKEN = 'ghp_' + 'A' * 36
RACY_ARGV = [
    'git',
    '-c',
    'credential.https://example.invalid.helper=!leaked-helper-value',
    'fetch',
    'origin',
]


@pytest.fixture
def stubbed_git_preflight(monkeypatch):
    """Isolate ``_run_git`` from the git subprocesses its pre-flight helpers run."""
    monkeypatch.setattr(updates, '_resolve_git_executable', lambda: 'git')
    monkeypatch.setattr(updates, 'repository_git_proxy_blocks', lambda *a, **k: False)
    monkeypatch.setattr(updates, 'clean_git_env', lambda: {})
    monkeypatch.setattr(updates, 'trusted_git_credential_config', lambda *a, **k: ())
    monkeypatch.setattr(updates, 'noninteractive_git_env', lambda *a, **k: {})
    monkeypatch.setattr(updates, 'noninteractive_git_argv', lambda args, **k: ['git', *args])


def _raise(exc):
    def runner(*args, **kwargs):
        raise exc
    return runner


def _timeout_exc(*, notes=(), cmd=None, stderr=b'remote: partial output\n'):
    """Build the timeout shape ``run_network_command_windows`` re-raises from.

    The notes live on the cause, exactly as the Windows runner leaves them.
    """
    cause = subprocess.TimeoutExpired(cmd or list(RACY_ARGV), 10)
    for note in notes:
        cause.add_note(note)
    error = subprocess.TimeoutExpired(
        cmd or list(RACY_ARGV), 10, output=None, stderr=stderr,
    )
    try:
        raise error from cause
    except subprocess.TimeoutExpired as raised:
        return raised


def test_timeout_cleanup_note_reaches_output_and_log(
    tmp_path, monkeypatch, stubbed_git_preflight, caplog,
):
    exc = _timeout_exc(notes=['windows job cleanup: CloseHandle failed (WinError 6)'])
    monkeypatch.setattr(updates, '_run_network_git', _raise(exc))

    with caplog.at_level(logging.WARNING, logger='api.updates'):
        output, ok = updates._run_git(['fetch', 'origin'], tmp_path, timeout=10)

    assert ok is False
    lines = output.splitlines()
    # The deadline stays the first line and the partial output keeps its place.
    assert lines[0] == 'git fetch origin timed out after 10s'
    assert lines[1] == 'remote: partial output'
    assert lines[2] == 'windows job cleanup: CloseHandle failed (WinError 6)'
    logged = [record.getMessage() for record in caplog.records
              if record.levelno == logging.WARNING]
    assert any('CloseHandle failed (WinError 6)' in message for message in logged), logged


def test_timeout_without_notes_keeps_the_previous_output_verbatim(
    tmp_path, monkeypatch, stubbed_git_preflight, caplog,
):
    """Control: a timeout with no cleanup note is byte-identical to before."""
    monkeypatch.setattr(updates, '_run_network_git', _raise(_timeout_exc()))

    with caplog.at_level(logging.WARNING, logger='api.updates'):
        output, ok = updates._run_git(['fetch', 'origin'], tmp_path, timeout=10)

    assert (output, ok) == ('git fetch origin timed out after 10s\nremote: partial output', False)
    assert not [record for record in caplog.records if record.levelno == logging.WARNING]


def test_only_windows_cleanup_notes_are_collected(
    tmp_path, monkeypatch, stubbed_git_preflight, caplog,
):
    """Notes without the cleanup prefix stay out of the message and the log."""
    exc = _timeout_exc(notes=[
        'windows job cleanup: CloseHandle failed (WinError 6)',
        'unrelated note carrying ' + GITHUB_TOKEN,
    ])
    monkeypatch.setattr(updates, '_run_network_git', _raise(exc))

    with caplog.at_level(logging.WARNING, logger='api.updates'):
        output, ok = updates._run_git(['fetch', 'origin'], tmp_path, timeout=10)

    assert ok is False
    assert 'windows job cleanup: CloseHandle failed (WinError 6)' in output
    assert 'unrelated note' not in output
    assert GITHUB_TOKEN not in output
    assert not any('unrelated note' in record.getMessage() for record in caplog.records)


def test_cleanup_note_is_redacted_and_capped(
    tmp_path, monkeypatch, stubbed_git_preflight, caplog,
):
    """The note is a git diagnostic: credentials are scrubbed and it is capped."""
    note = ('windows job cleanup: helper echoed ' + GITHUB_TOKEN
            + ' then ' + 'x' * 400)
    exc = _timeout_exc(notes=[note])
    monkeypatch.setattr(updates, '_run_network_git', _raise(exc))

    with caplog.at_level(logging.WARNING, logger='api.updates'):
        output, ok = updates._run_git(['fetch', 'origin'], tmp_path, timeout=10)

    note_line = output.splitlines()[2]
    assert ok is False
    assert GITHUB_TOKEN not in output
    assert '<redacted>' in note_line
    # ``sanitize_git_diagnostic`` truncates to the limit and appends the ellipsis.
    assert len(note_line) == updates._GIT_DIAGNOSTIC_MAX_CHARS + 1, len(note_line)
    assert note_line.endswith('…')
    assert 'x' * 400 not in output
    for record in caplog.records:
        assert GITHUB_TOKEN not in record.getMessage()
        assert 'x' * 400 not in record.getMessage()


def test_cleanup_note_never_exposes_the_process_argv(
    tmp_path, monkeypatch, stubbed_git_preflight, caplog,
):
    """``str(exc)``/``str(exc.__cause__)`` embed the argv; only notes are read."""
    exc = _timeout_exc(notes=['windows job cleanup: pipe drain timed out'])
    monkeypatch.setattr(updates, '_run_network_git', _raise(exc))

    with caplog.at_level(logging.WARNING, logger='api.updates'):
        output, ok = updates._run_git(['fetch', 'origin'], tmp_path, timeout=10)

    assert ok is False
    assert 'windows job cleanup: pipe drain timed out' in output
    assert 'leaked-helper-value' not in output
    assert 'credential.' not in output
    assert 'Traceback' not in output
    for record in caplog.records:
        message = record.getMessage()
        assert 'leaked-helper-value' not in message
        assert 'credential.' not in message


def test_unconfirmed_job_close_is_a_warning_not_a_start_failure(
    tmp_path, monkeypatch, stubbed_git_preflight, caplog,
):
    """The command finished; only the teardown is unconfirmed (#9138 item 2)."""
    error = OSError('job close unconfirmed after a successful command')
    error.returncode = 0
    error.stdout = ''
    error.stderr = ''
    error.add_note('windows job cleanup: final CloseHandle confirmed after terminate')
    monkeypatch.setattr(updates, '_run_network_git', _raise(error))

    with caplog.at_level(logging.WARNING, logger='api.updates'):
        output, ok = updates._run_git(['pull', '--ff-only', 'origin', 'master'], tmp_path)

    assert ok is True
    lines = output.splitlines()
    assert lines[0] == updates._WINDOWS_JOB_CLEANUP_UNCONFIRMED
    assert lines[1] == 'windows job cleanup: final CloseHandle confirmed after terminate'
    assert 'git failed to start' not in output
    logged = [record.getMessage() for record in caplog.records
              if record.levelno == logging.WARNING]
    assert any('final CloseHandle confirmed after terminate' in message for message in logged), logged


def test_real_start_failure_still_reports_failed_to_start(
    tmp_path, monkeypatch, stubbed_git_preflight,
):
    """Control: an OSError that is not the teardown failure keeps its message."""
    monkeypatch.setattr(
        updates, '_run_network_git', _raise(OSError(13, 'Permission denied')),
    )

    output, ok = updates._run_git(['fetch', 'origin'], tmp_path)

    assert ok is False
    assert output == 'git failed to start: [Errno 13] Permission denied'


def test_deadline_classification_is_unchanged_with_a_cleanup_note():
    """Control: the note never hides the deadline from the fetch classifier."""
    fetch_out = ('git fetch origin timed out after 60s\n'
                 'windows job cleanup: pipe drain timed out')

    assert updates._apply_fetch_failure_message(fetch_out, 'network') == (
        'The fetch was cancelled after 60s — git fetch origin did not finish '
        'before the update deadline.'
    )


def test_ff_only_pull_cleanup_warning_completes_the_update(tmp_path):
    """A pull whose teardown stayed unconfirmed is a completed update (#9138)."""
    (tmp_path / '.git').mkdir()
    warning = (updates._WINDOWS_JOB_CLEANUP_UNCONFIRMED
               + '\nwindows job cleanup: CloseHandle failed (WinError 6)')

    def fake_git(args, cwd, timeout=10):
        if args == ['fetch', 'origin', '--quiet', '--tags', '--force']:
            return '', True
        if args == ['status', '--porcelain', '--untracked-files=no']:
            return '', True
        if args[:2] == ['pull', '--ff-only']:
            return warning, True
        raise AssertionError(f'unexpected git args: {args!r}')

    with patch.object(updates, '_run_git', side_effect=fake_git), \
         patch.object(updates, 'REPO_ROOT', tmp_path), \
         patch.object(updates, '_schedule_restart'), \
         patch.object(updates, '_select_apply_compare_ref', return_value='origin/master'), \
         patch.object(
             updates, '_restart_blocker_snapshot',
             return_value={'restart_blocked': False, 'active_streams': 0, 'active_runs': 0},
         ):
        result = updates.apply_update('webui')

    assert result['ok'] is True
    assert warning in result['message']
    assert 'git failed to start' not in result['message']
    # The update lock is released on the way out.
    assert updates._apply_lock.acquire(blocking=False)
    updates._apply_lock.release()
