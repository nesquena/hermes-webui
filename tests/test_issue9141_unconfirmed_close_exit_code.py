"""Issue #9141: an unconfirmed Windows job close must not hide a failed git command.

``run_network_command_windows`` raises its success-path teardown ``OSError``
whatever git's exit code was — "successful" there only means the process did
not raise — so ``_run_git`` cannot decide from the exception text alone. #9138
made that error a warning; this spec pins the missing discriminator: the
command's own result travels on the exception, and only a zero exit keeps the
warning-shaped success.

The pull and fetch cases are driven through the real ``_run_git`` branch code
with the network runner replaced by the exception shape under test (the same
seam ``tests/test_issue9138_windows_cleanup_notes.py`` uses, so they run on any
host). The Windows runner's own contract — attaching returncode/stdout/stderr —
is measured against the real ``run_network_command_windows`` with the job API
and the spawned process faked, which is the boundary
``tests/test_update_network_git_windows.py`` already exercises.
"""

import os
import subprocess
from pathlib import Path

import pytest

import api.subprocess_utils as subprocess_utils
import api.updates as updates

PULL_ARGV = ['pull', '--ff-only', 'origin', 'master']
FATAL_FF = 'fatal: Not possible to fast-forward, aborting.'


def _unconfirmed_close(*, returncode=128, stdout='', stderr=FATAL_FF,
                       notes=('windows job cleanup: CloseHandle failed (WinError 6)',)):
    """Build the teardown error as ``run_network_command_windows`` raises it."""
    error = OSError(updates._WINDOWS_JOB_CLOSE_UNCONFIRMED)
    if returncode is not None:
        error.returncode = returncode
    error.stdout = stdout
    error.stderr = stderr
    for note in notes:
        error.add_note(note)
    return error


def _raise(exc):
    def runner(*args, **kwargs):
        raise exc
    return runner


@pytest.fixture
def stubbed_git_preflight(monkeypatch):
    """Isolate ``_run_git`` from the git subprocesses its pre-flight helpers run."""
    monkeypatch.setattr(updates, '_resolve_git_executable', lambda: 'git')
    monkeypatch.setattr(updates, 'repository_git_proxy_blocks', lambda *a, **k: False)
    monkeypatch.setattr(updates, 'clean_git_env', lambda: {})
    monkeypatch.setattr(updates, 'trusted_git_credential_config', lambda *a, **k: ())
    monkeypatch.setattr(updates, 'noninteractive_git_env', lambda *a, **k: {})
    monkeypatch.setattr(updates, 'noninteractive_git_argv', lambda args, **k: ['git', *args])


def test_failed_pull_with_unconfirmed_close_stays_a_failure(
    tmp_path, monkeypatch, stubbed_git_preflight,
):
    """git exited 128: the unconfirmed teardown must not turn that into success."""
    monkeypatch.setattr(
        updates, '_run_network_git', _raise(_unconfirmed_close()),
    )

    output, ok = updates._run_git(PULL_ARGV, tmp_path)

    assert ok is False
    assert FATAL_FF in output
    assert updates._WINDOWS_JOB_CLEANUP_UNCONFIRMED not in output
    assert 'windows job cleanup: CloseHandle failed (WinError 6)' in output


def test_failed_fetch_with_unconfirmed_close_stays_a_failure(
    tmp_path, monkeypatch, stubbed_git_preflight,
):
    """A failed fetch must not carry on with stale refs."""
    monkeypatch.setattr(
        updates, '_run_network_git',
        _raise(_unconfirmed_close(
            stderr='fatal: could not read Username for https://example.invalid',
        )),
    )

    output, ok = updates._run_git(['fetch', 'origin', '--quiet'], tmp_path)

    assert ok is False
    assert 'could not read Username' in output
    assert updates._WINDOWS_JOB_CLEANUP_UNCONFIRMED not in output
    assert 'windows job cleanup:' in output


def test_unconfirmed_close_without_a_returncode_fails_closed(
    tmp_path, monkeypatch, stubbed_git_preflight,
):
    """A teardown error that cannot prove a zero exit is not a success."""
    monkeypatch.setattr(
        updates, '_run_network_git', _raise(_unconfirmed_close(returncode=None)),
    )

    output, ok = updates._run_git(PULL_ARGV, tmp_path)

    assert ok is False
    assert updates._WINDOWS_JOB_CLEANUP_UNCONFIRMED not in output
    assert 'windows job cleanup:' in output


def test_completed_pull_with_unconfirmed_close_is_still_a_success(
    tmp_path, monkeypatch, stubbed_git_preflight,
):
    """Control: a zero exit keeps the #9138 warning-shaped success."""
    monkeypatch.setattr(
        updates, '_run_network_git',
        _raise(_unconfirmed_close(returncode=0, stdout='', stderr='')),
    )

    output, ok = updates._run_git(PULL_ARGV, tmp_path)

    assert ok is True
    lines = output.splitlines()
    assert lines[0] == updates._WINDOWS_JOB_CLEANUP_UNCONFIRMED
    assert lines[1] == 'windows job cleanup: CloseHandle failed (WinError 6)'


def test_failed_pull_with_a_confirmed_close_is_unchanged(
    tmp_path, monkeypatch, stubbed_git_preflight,
):
    """Control: without a teardown failure the result is git's own, as before."""
    monkeypatch.setattr(
        updates, '_run_network_git',
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 128, '', FATAL_FF),
    )

    output, ok = updates._run_git(PULL_ARGV, tmp_path)

    assert ok is False
    assert output == FATAL_FF


def test_apply_update_does_not_report_a_failed_pull_as_updated(tmp_path, monkeypatch):
    """The user-visible promise: 128 on pull is not 'webui updated successfully'."""
    _git_init(tmp_path)
    restarts = []

    def runner(argv, *, cwd, env, timeout):
        # ``_run_git`` hands the expanded argv: git executable, ``-c`` option
        # pairs and the subcommand somewhere after them.
        if 'pull' in argv:
            raise _unconfirmed_close()
        if 'fetch' in argv:
            return subprocess.CompletedProcess(argv, 0, '', '')
        raise AssertionError(f'unexpected network git: {argv!r}')

    monkeypatch.setattr(updates, '_run_network_git', runner)
    monkeypatch.setattr(updates, 'REPO_ROOT', tmp_path)
    monkeypatch.setattr(updates, '_select_apply_compare_ref', lambda *a, **k: 'origin/master')
    monkeypatch.setattr(updates, '_schedule_restart', lambda *a, **k: restarts.append(a))
    monkeypatch.setattr(
        updates, '_restart_blocker_snapshot',
        lambda *a, **k: {'restart_blocked': False, 'active_streams': 0, 'active_runs': 0},
    )

    result = updates.apply_update('webui')

    assert result['ok'] is False
    assert 'not possible' in result['message'].lower()
    assert 'updated successfully' not in result['message']
    assert restarts == []
    # The update lock is released on the way out.
    assert updates._apply_lock.acquire(blocking=False)
    updates._apply_lock.release()


def _git_init(path: Path) -> None:
    subprocess.run(
        ['git', 'init', '-q', str(path)], check=True,
        env={**os.environ, 'GIT_AUTHOR_NAME': 'test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
             'GIT_COMMITTER_NAME': 'test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'},
    )


# --- the Windows runner's own contract -------------------------------------


class _FakeKernel32:
    def __init__(self, close_ok=False):
        self.close_ok = close_ok
        self.closed = 0
        self.terminated = 0

    def CreateJobObjectW(self, *args):
        return 1

    def SetInformationJobObject(self, *args):
        return 1

    def AssignProcessToJobObject(self, *args):
        return 1

    def TerminateJobObject(self, *args):
        self.terminated += 1
        return 1

    def CloseHandle(self, handle):
        self.closed += 1
        return 1 if self.close_ok else 0


class _FakeProcess:
    """Text-mode process double whose ``communicate`` returns the command result."""

    returncode = 128
    stdout_text = ''
    stderr_text = FATAL_FF + '\n'

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self._handle = 4321
        self.stdin = None
        self.stdout = None
        self.stderr = None

    def communicate(self, input=None, timeout=None):
        return self.stdout_text, self.stderr_text

    def kill(self):
        pass


@pytest.mark.skipif(os.name != 'nt', reason='the Job Object runner is Windows-only')
def test_windows_runner_attaches_the_command_result_to_the_close_failure(monkeypatch):
    """The exception has to carry git's result, or the caller cannot classify it."""
    kernel32 = _FakeKernel32(close_ok=False)
    fake_subprocess = type('FakeSubprocess', (), {})()
    for name in dir(subprocess):
        if not name.startswith('_'):
            setattr(fake_subprocess, name, getattr(subprocess, name))
    fake_subprocess.Popen = _FakeProcess
    monkeypatch.setattr(subprocess_utils, 'subprocess', fake_subprocess)
    monkeypatch.setattr(subprocess_utils, '_windows_kernel32', lambda: kernel32)

    with pytest.raises(OSError) as raised:
        subprocess_utils.run_network_command_windows(
            ['git', *PULL_ARGV], cwd=Path('.'), env={}, timeout=10,
        )

    error = raised.value
    assert str(error).strip() == updates._WINDOWS_JOB_CLOSE_UNCONFIRMED
    assert error.returncode == 128
    assert error.stdout == ''
    assert FATAL_FF in error.stderr
    assert getattr(error, '__notes__', None)
    assert kernel32.terminated == 1
