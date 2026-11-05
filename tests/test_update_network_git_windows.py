"""Native Windows oracles for the updater's network Git cleanup (Job Object).

Collected by pytest on every platform (everything Windows-specific skips off
Windows) and run natively on Windows CI with:

    python -m unittest discover -s tests -p test_update_network_git_windows.py -v

No mock stands in for the process tree: fixtures are real Python children and
grandchildren with a ready/allow-exit file handshake, and reaping is proven
with persistent ``WaitForSingleObject`` handles opened while both processes
are alive, never by reopening a PID after it may have exited. The mocked
regressions are cross-platform and deterministic: they pin the failure-path
ordering (terminate, confirmed job close, and only then the pipe drain) with
reader-end sentinels that must never be closed, and they pin the guarded PID
fallback's access rights so identity is always read from the very handle that
would terminate.

These tests are self-contained: they exercise only the shipped runner and real
fixture processes. Nothing here reads repository history, shells out to ``git``,
or depends on an external command being installed.
"""
import contextlib
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# api/__init__.py is a docstring-only package marker, so this import stays
# stdlib-only and works in the no-install CI job.
from api import subprocess_utils  # noqa: E402  (after the sys.path insert above)

PROCESS_TERMINATE = 0x0001
PROCESS_SYNCHRONIZE = 0x00100000
#: ``GetProcessTimes`` rejects a handle opened without a query right, so the
#: guarded PID fallback asks for it in the same ``OpenProcess`` call it will
#: terminate through. Persistent oracle handles never read creation times and
#: deliberately keep the narrower mask.
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
#: ``OpenProcess`` reports a nonexistent PID as an invalid parameter.
ERROR_INVALID_PARAMETER = 87

# Fixture modes: "child" spawns a grandchild that silently holds the captured
# pipes, records both PIDs, prints a partial line, then either hangs ("hang")
# or waits for the test's allow-exit file ("exit-root") before exiting and
# leaving the grandchild alive with the pipes. "grand" is the grandchild
# payload: it sleeps long enough to outlive every deadline and cleanup. The
# last argv item carries the fixture source itself so the child can respawn it
# without extra files.
FIXTURE_CODE = r'''
import ctypes, json, os, pathlib, subprocess, sys, time
mode = sys.argv[1]
marker_dir = sys.argv[2]
hang = float(sys.argv[3])
child_mode = sys.argv[4] if len(sys.argv) > 4 else 'hang'
markers = pathlib.Path(marker_dir)


def creation_ticks(source):
    """Win32 creation time of a process, read from a live handle.

    The value is the identity token the test's PID fallback compares, so it
    is taken from a handle that cannot be retargeted: the current process
    pseudo-handle, or the grandchild's own inherited ``Popen._handle``.
    """
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    handle = ctypes.c_void_p(int(source))
    times = [ctypes.c_ulonglong() for _ in range(4)]
    if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        raise OSError(ctypes.get_last_error(), 'GetProcessTimes failed')
    return times[0].value


if mode == 'grand':
    time.sleep(hang)
else:
    markers.mkdir(parents=True, exist_ok=True)
    (markers / 'ran').touch()
    grand = subprocess.Popen(
        [sys.executable, '-I', '-c', sys.argv[5], 'grand', marker_dir, sys.argv[3]],
        shell=False,
    )
    payload = json.dumps({
        'child_pid': os.getpid(),
        'grand_pid': grand.pid,
        # Identity pairing for the guarded PID fallback: never reopen a PID
        # and kill it on the strength of a number alone.
        'child_created': creation_ticks(-1),
        'grand_created': creation_ticks(grand._handle),
    })
    tmp = markers / 'child.json.tmp'
    tmp.write_text(payload)
    tmp.replace(markers / 'child.json')
    (markers / 'ready').touch()
    print('remote: partial output', flush=True)
    if child_mode == 'hang':
        time.sleep(hang)
    else:
        end = time.monotonic() + 90.0
        while not (markers / 'allow-exit').exists() and time.monotonic() < end:
            time.sleep(0.02)
    sys.exit(0)
'''.strip() + '\n'

ECHO_CODE = r'''
import os, sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
rc = int(sys.argv[1])
if rc:
    sys.stderr.write('stderr-line héllo\n')
if len(sys.argv) > 2:
    print('args:' + '|'.join(sys.argv[2:]))
    print('env:' + os.environ.get('FIXTURE_VAR', ''))
    print('cwd:' + os.getcwd())
sys.exit(rc)
'''.strip() + '\n'


def _test_kernel32():
    """Bind only the oracle entry points the fixtures need."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.TerminateProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    return kernel32


_KERNEL32 = None


def _kernel32():
    global _KERNEL32
    if _KERNEL32 is None:
        _KERNEL32 = _test_kernel32()
    return _KERNEL32


def _popen_native_handle(process):
    """The stable HANDLE a Windows ``Popen`` keeps open for its whole lifetime.

    Taken from the object itself: reopening ``process.pid`` could race a PID
    recycle, while this handle cannot be retargeted onto another process.
    """
    raw = process._handle
    value = getattr(raw, 'handle', raw)
    try:
        native = int(value)
    except (TypeError, ValueError) as exc:
        raise AssertionError(
            f'cannot read the bootstrap process HANDLE: {exc!r}'
        ) from None
    if not native:
        raise AssertionError('the bootstrap process HANDLE is null')
    return ctypes.c_void_p(native)


def _open_process_handle(pid):
    """Open a persistent oracle handle; failure is a test failure, not "gone"."""
    raw_handle = _kernel32().OpenProcess(
        PROCESS_TERMINATE | PROCESS_SYNCHRONIZE, False, int(pid),
    )
    if not raw_handle:
        raise AssertionError(
            f'OpenProcess(PROCESS_TERMINATE|PROCESS_SYNCHRONIZE, pid={pid}) '
            f'failed: winerror={ctypes.get_last_error()}'
        )
    return ctypes.c_void_p(raw_handle)


def _wait_for_handle(handle, timeout_ms=0):
    """Return True when the handle is signaled, False for a live timeout.

    ``WAIT_FAILED`` is a harness failure and must never be read as "gone".
    """
    result = _kernel32().WaitForSingleObject(handle, int(timeout_ms))
    if result == WAIT_FAILED:
        raise AssertionError(
            f'WaitForSingleObject failed: winerror={ctypes.get_last_error()}'
        )
    if result == WAIT_OBJECT_0:
        return True
    if result == WAIT_TIMEOUT:
        return False
    raise AssertionError(f'unexpected WaitForSingleObject result: 0x{result:08X}')


def _wait_handle_signaled(handle, deadline_s=10.0):
    end = time.monotonic() + deadline_s
    while True:
        if _wait_for_handle(handle, 0):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.05)


def _wait_handles_signaled(handles, deadline_s=10.0):
    end = time.monotonic() + deadline_s
    while True:
        if all(_wait_for_handle(handle, 0) for handle in handles):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.05)


def _terminate_handle(handle):
    if not _kernel32().TerminateProcess(handle, 1):
        raise AssertionError(
            f'TerminateProcess failed: winerror={ctypes.get_last_error()}'
        )


def _close_handle(handle):
    if not _kernel32().CloseHandle(handle):
        raise AssertionError(f'CloseHandle failed: winerror={ctypes.get_last_error()}')


def _cleanup_handle(handle):
    """Fallback cleanup for an owned handle: terminate, wait, then close."""
    if handle is None:
        return
    try:
        if not _wait_for_handle(handle, 0):
            _terminate_handle(handle)
            if not _wait_handle_signaled(handle, 10.0):
                raise AssertionError('process did not exit after TerminateProcess')
    finally:
        _close_handle(handle)


def _run_cleanups(*steps):
    """Run every fallback cleanup step even when an earlier one fails.

    A raising ``TerminateProcess``/``CloseHandle`` must never short-circuit the
    rest of the teardown and strand the remaining processes. Cleanup failures
    stay loud: when something was already propagating they are chained onto it
    (with the primary failure kept in the message) instead of masking it.
    """
    pending = sys.exc_info()[1]
    failures = []
    for step in steps:
        try:
            step()
        except BaseException as exc:  # noqa: BLE001 - reported after all steps
            failures.append(exc)
    if not failures:
        return
    detail = 'fallback cleanup failed: ' + '; '.join(repr(exc) for exc in failures)
    if pending is not None:
        raise AssertionError(f'{detail}; primary failure: {pending!r}') from pending
    raise AssertionError(detail) from failures[0]


def _cleanup_pid(pid, expected_created=None):
    """Guarded fallback cleanup for a fixture PID with no persistent handle.

    Reopening a bare PID is only safe when the handle is proved to still be
    the *same* process: the creation time read from this handle must equal the
    one the fixture recorded from a live handle. ``OpenProcess`` failing is the
    only "already gone" signal accepted here, and only for the documented
    invalid-PID error; a missing identity or a mismatch closes the handle and
    fails instead of terminating a reused PID.

    The handle requests ``PROCESS_QUERY_LIMITED_INFORMATION`` alongside
    terminate/synchronize because ``GetProcessTimes`` refuses a handle without
    a query right — and the identity read must come from the very handle that
    would terminate, otherwise the check could pass on one process while the
    kill lands on another.
    """
    if expected_created is None:
        raise AssertionError(
            f'pid {pid}: refusing fallback termination without a recorded '
            'creation time (a bare PID may have been reused)'
        )
    ctypes.set_last_error(0)
    raw_handle = _kernel32().OpenProcess(
        PROCESS_TERMINATE | PROCESS_SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
        False, int(pid),
    )
    if not raw_handle:
        last_error = ctypes.get_last_error()
        if last_error == ERROR_INVALID_PARAMETER:
            return
        raise AssertionError(
            f'OpenProcess(pid={pid}) failed during cleanup: '
            f'winerror={last_error} (not a missing process)'
        )
    handle = ctypes.c_void_p(raw_handle)
    try:
        actual_created = _process_creation_ticks(handle, pid)
        if actual_created != int(expected_created):
            raise AssertionError(
                f'pid {pid} is not the fixture process any more: creation time '
                f'{actual_created} != recorded {expected_created}; refusing to kill'
            )
        if not _wait_for_handle(handle, 0):
            _terminate_handle(handle)
            if not _wait_handle_signaled(handle, 10.0):
                raise AssertionError(f'pid {pid} did not exit after TerminateProcess')
    finally:
        _close_handle(handle)


def _process_creation_ticks(handle, pid):
    """Win32 creation time of the process behind ``handle`` (identity token)."""
    kernel32 = _kernel32()
    times = [ctypes.c_ulonglong() for _ in range(4)]
    if not kernel32.GetProcessTimes(
        handle, *(ctypes.byref(value) for value in times),
    ):
        raise AssertionError(
            f'GetProcessTimes(pid={pid}) failed: winerror={ctypes.get_last_error()}'
        )
    return times[0].value


def _read_child_fixture(markers):
    return json.loads((Path(markers) / 'child.json').read_text())


def _wait_for_child_fixture(markers, deadline_s=20.0):
    """Wait until the child has spawned the grandchild and both are live."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if ((Path(markers) / 'child.json').exists()
                and (Path(markers) / 'ready').exists()):
            try:
                return _read_child_fixture(markers)
            except ValueError:
                pass
        time.sleep(0.05)
    raise AssertionError('fixture child/grandchild ready handshake never appeared')


def _cleanup_fixture_pids(markers, covered=()):
    """Kill fixture PIDs that were never covered by a persistent handle.

    Each PID is paired with the creation time its fixture recorded from a live
    handle, so the fallback cannot terminate a process that merely reused the
    number after the fixture exited.
    """
    path = Path(markers) / 'child.json'
    if not path.exists():
        return
    try:
        child = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    for prefix in ('grand', 'child'):
        pid = child.get(f'{prefix}_pid')
        if not pid or pid in covered:
            continue
        created = child.get(f'{prefix}_created')
        if created is None:
            raise AssertionError(
                f'{prefix} pid {pid}: fixture recorded no creation time; '
                'refusing to terminate by PID alone'
            )
        _cleanup_pid(pid, created)


def _stop_popen(process):
    """Finite fallback reaping for a real Popen that should already be gone."""
    if process is None:
        return
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError('process did not exit after kill') from exc


def _stop_thread(thread):
    if thread is not None and thread.is_alive():
        thread.join(timeout=30)


def _wait_popen_exit(process, deadline_s=10.0):
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if process.poll() is not None:
            return True
        time.sleep(0.05)
    return process.poll() is not None


def _run_in_thread(target):
    """Run a runner call off-thread and record how long the call itself took."""
    outcome = {}

    def runner():
        started = time.monotonic()
        try:
            outcome['result'] = target()
        except BaseException as exc:  # noqa: BLE001 - the test inspects the type
            outcome['error'] = exc
        finally:
            outcome['elapsed'] = time.monotonic() - started

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread, outcome


def _hang_fixture_argv(markers, child_mode, hang='150'):
    """Build a fixture argv whose processes outlive every test deadline."""
    return [
        sys.executable, '-I', '-c', FIXTURE_CODE, 'child', str(markers),
        hang, child_mode, FIXTURE_CODE,
    ]


def _normalized_path(value):
    """Canonical, case-folded form so 8.3 names and casing cannot mismatch."""
    return os.path.normcase(os.path.realpath(str(value)))


class _FakeKernel32:
    """Failure-injection double for the job API; the bootstrap stays real.

    Every knob defaults to the healthy path, so the native setup-failure test
    keeps its original shape while the mocked ordering regression drives the
    cleanup knobs and reads ``events`` for call ordering.
    """

    def __init__(self, create_ok=True, config_ok=True, assign_ok=True, create_exc=None,
                 terminate_ok=True, terminate_exc=None, close_results=None,
                 close_exc=None, events=None):
        self.create_ok = create_ok
        self.config_ok = config_ok
        self.assign_ok = assign_ok
        self.create_exc = create_exc
        self.terminate_ok = terminate_ok
        self.terminate_exc = terminate_exc
        self.close_results = list(close_results) if close_results else None
        self.close_exc = close_exc
        self.job = None
        self.terminated = 0
        self.closed = False
        self.events = events if events is not None else []
        self.close_calls = 0
        self.close_handles = []

    def CreateJobObjectW(self, attributes, name):
        self.events.append('create')
        if self.create_exc is not None:
            raise self.create_exc
        if not self.create_ok:
            return 0
        self.job = 0x5A5A5A5A
        return self.job

    def SetInformationJobObject(self, job, info_class, ref, size):
        self.events.append('configure')
        return 1 if self.config_ok else 0

    def AssignProcessToJobObject(self, job, handle):
        self.events.append('assign')
        return 1 if self.assign_ok else 0

    def TerminateJobObject(self, job, exit_code):
        self.terminated += 1
        self.events.append('terminate')
        if self.terminate_exc is not None:
            raise self.terminate_exc
        return 1 if self.terminate_ok else 0

    def CloseHandle(self, handle):
        self.close_calls += 1
        self.close_handles.append(handle)
        self.events.append('close_job')
        if self.close_exc is not None:
            raise self.close_exc
        result = self.close_results.pop(0) if self.close_results else True
        if handle == self.job and result:
            self.closed = True
        return 1 if result else 0


ARGV = ['git', 'fetch', 'origin']


class _FakePipe:
    """Pipe end; ``raises`` marks a reader close as the forbidden action."""

    def __init__(self, name, events, raises=False):
        self.name = name
        self.events = events
        self.raises = raises
        self.closed = False
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.closed = True
        self.events.append(f'close_{self.name}')
        if self.raises:
            raise AssertionError(f'{self.name}.close() must never run before the drain')


class _FakeProcess:
    """Gate bootstrap double: scripted communicate, real cleanup surface."""

    def __init__(self, script, events=None):
        self.events = events if events is not None else []
        self.script = list(script)
        self.stdin = _FakePipe('stdin', self.events)
        self.stdout = _FakePipe('stdout', self.events, raises=True)
        self.stderr = _FakePipe('stderr', self.events, raises=True)
        self._handle = 4242
        self.returncode = 0
        self.kill_calls = 0
        self.wait_calls = []

    def communicate(self, input=None, timeout=None):
        shown = None if timeout is None else round(timeout, 3)
        self.events.append(f'communicate(input={input!r}, timeout={shown})')
        action = self.script.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action

    def kill(self):
        self.kill_calls += 1
        self.events.append('kill')

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        self.events.append(f'wait({timeout})')
        return self.returncode


def _raise_binder():
    raise OSError(5, 'kernel32 binder failed')


class JobCleanupOrderingTests(unittest.TestCase):
    """Cross-platform regression for the failure-path cleanup ordering.

    Deterministic and native-free: the kernel32 binder and ``Popen`` are
    patched on the module under test, and ``ctypes.get_last_error`` is created
    as a stub where the OS lacks it, so the same assertions run on every
    platform. The reader-end pipes are raising sentinels, which is the point:
    closing a Windows reader while a writer still holds it blocks the harness,
    so that close must never run before the drain has been given its chance.
    """

    def _run(self, fake, process, timeout=2.5):
        # One shared log: the fake kernel and the fake process are two halves
        # of the same ordering oracle.
        fake.events = process.events
        with mock.patch.object(
                subprocess_utils, '_windows_kernel32', lambda: fake), \
             mock.patch.object(
                 subprocess_utils.subprocess, 'Popen', lambda *a, **k: process), \
             mock.patch.object(ctypes, 'get_last_error', lambda: 0, create=True):
            try:
                return subprocess_utils.run_network_command_windows(
                    ARGV, cwd='/tmp', env={}, timeout=timeout), None
            except BaseException as exc:  # noqa: BLE001 - the test asserts the type
                return None, exc

    @classmethod
    def _notes(cls, exc):
        """Cleanup notes, following the ``from exc`` link on a rebuilt timeout.

        The failure path re-shapes ``TimeoutExpired`` with the original budget
        and partial output, so its notes sit on the wrapped cause; every other
        error keeps them on itself.
        """
        found = list(getattr(exc, '__notes__', []))
        cause = exc.__cause__
        if cause is not None:
            found.extend(getattr(cause, '__notes__', []))
        return found

    @staticmethod
    def _communicate_indexes(events):
        return [i for i, event in enumerate(events) if event.startswith('communicate')]

    def test_double_timeout_never_closes_readers_and_stops_dead(self):
        fake = _FakeKernel32()
        process = _FakeProcess([
            subprocess.TimeoutExpired(ARGV, 2.5, output='partial out', stderr='partial err'),
            subprocess.TimeoutExpired(ARGV, None),
        ])
        result, exc = self._run(fake, process)

        self.assertIsNone(result, 'a timed-out command must not return a result')
        self.assertIsInstance(exc, subprocess.TimeoutExpired)
        self.assertEqual(exc.cmd, ARGV, 'the original argv must survive both timeouts')
        self.assertEqual(exc.timeout, 2.5, 'the original budget must survive')
        self.assertEqual(exc.output, 'partial out', 'partial stdout must survive')
        self.assertEqual(exc.stderr, 'partial err', 'partial stderr must survive')

        # The forbidden action: a reader-end close on Windows joins a thread
        # parked in ReadFile while a writer still holds the pipe.
        self.assertEqual(process.stdout.close_calls, 0, 'stdout.close() ran before the drain')
        self.assertEqual(process.stderr.close_calls, 0, 'stderr.close() ran before the drain')

        events = process.events
        second = self._communicate_indexes(events)[1]
        self.assertLess(events.index('terminate'), events.index('close_job'),
                        'terminate must precede the job close')
        self.assertLess(events.index('close_job'), second,
                        'the job must be closed BEFORE the drain communicate')
        self.assertEqual(events.count('close_stdin'), 1, 'the gate must be closed exactly once')
        self.assertEqual(process.kill_calls, 1, 'the stuck bootstrap must be killed once')
        self.assertEqual(process.wait_calls, [5], 'the reap must be bounded and finite')
        self.assertEqual(fake.close_calls, 1, 'a confirmed close must not be repeated')
        self.assertTrue(
            any('pipe drain timed out' in note for note in self._notes(exc)),
            f'the drain failure must be reported: {self._notes(exc)}',
        )

    def test_terminate_false_or_raising_keeps_the_original_error(self):
        cases = [
            ('returns-false', dict(terminate_ok=False), 'TerminateJobObject failed'),
            ('raises', dict(terminate_exc=ctypes.ArgumentError('bad job argument')),
             'TerminateJobObject raised'),
        ]
        for label, knobs, diagnostic in cases:
            with self.subTest(case=label):
                fake = _FakeKernel32(**knobs)
                process = _FakeProcess([
                    subprocess.TimeoutExpired(ARGV, 2.5, output='partial out'),
                    ('drained out', ''),
                ])
                result, exc = self._run(fake, process)

                self.assertIsNone(result)
                self.assertIsInstance(exc, subprocess.TimeoutExpired,
                                      'a cleanup failure must not replace the timeout')
                self.assertEqual(exc.cmd, ARGV)
                self.assertEqual(exc.timeout, 2.5)
                self.assertEqual(fake.terminated, 1, 'terminate must be attempted exactly once')
                self.assertEqual(fake.close_calls, 1, 'cleanup must advance to the close')
                self.assertTrue(
                    any(diagnostic in note for note in self._notes(exc)),
                    f'{label}: diagnostic lost: {self._notes(exc)}',
                )
                self.assertLess(process.events.index('terminate'),
                                self.events_close(process.events),
                                'terminate must precede the job close')
                self.assertEqual(process.kill_calls, 0, 'a drained bootstrap must not be killed')
                self.assertEqual(process.stdout.close_calls, 0,
                                 'the successful drain must not close reader ends')

    def test_baseexception_survives_every_cleanup_failure(self):
        cases = [
            ('close-false', dict(close_results=[False, False]), 'CloseHandle failed'),
            ('close-raises', dict(close_exc=ctypes.ArgumentError('bad handle')),
             'CloseHandle raised'),
        ]
        for label, knobs, diagnostic in cases:
            with self.subTest(case=label):
                fake = _FakeKernel32(**knobs)
                process = _FakeProcess([KeyboardInterrupt('simulated cancel'), ('out', '')])
                result, exc = self._run(fake, process)

                self.assertIsNone(result)
                self.assertIsInstance(exc, KeyboardInterrupt,
                                      f'{label}: cleanup masked the cancellation')
                self.assertEqual(str(exc), 'simulated cancel')
                self.assertLessEqual(fake.close_calls, 2,
                                     f'{label}: close attempts must stay bounded')
                self.assertEqual(fake.close_calls, 2,
                                 f'{label}: the failure path gets one bounded retry')
                self.assertTrue(
                    any(diagnostic in note for note in self._notes(exc)),
                    f'{label}: diagnostic lost: {self._notes(exc)}',
                )
                self.assertEqual(process.events.count('close_stdin'), 1,
                                 f'{label}: cleanup must still advance to the drain')

    def test_close_unconfirmed_then_confirmed_does_not_duplicate(self):
        for label, results in (('failure-path', [False, True]), ('success-path', [False, True])):
            with self.subTest(case=label):
                fake = _FakeKernel32(close_results=list(results))
                script = (
                    [KeyboardInterrupt('simulated cancel'), ('out', '')]
                    if label == 'failure-path' else [('out', '')]
                )
                process = _FakeProcess(script)
                result, exc = self._run(fake, process)

                self.assertEqual(fake.close_calls, 2, f'{label}: exactly one retry')
                self.assertEqual(fake.close_handles, [0x5A5A5A5A, 0x5A5A5A5A],
                                 f'{label}: the same job handle must be retried')
                if label == 'failure-path':
                    self.assertIsNone(result)
                    self.assertIsInstance(exc, KeyboardInterrupt)
                    self.assertTrue(any('CloseHandle failed' in note
                                        for note in self._notes(exc)))
                else:
                    self.assertIsNone(result, 'an unconfirmed success close must not return')
                    self.assertIsInstance(exc, OSError)
                    self.assertIn('unconfirmed', str(exc))
                    self.assertEqual(fake.terminated, 1,
                                     'the success path must terminate before giving up')
                    self.assertTrue(any('final CloseHandle confirmed' in note
                                        for note in self._notes(exc)))

    def test_success_path_close_failure_is_an_error_never_a_result(self):
        cases = [
            ('false-twice', dict(close_results=[False, False]), 2),
            ('raises', dict(close_exc=ctypes.ArgumentError('bad handle')), 2),
        ]
        for label, knobs, expected_closes in cases:
            with self.subTest(case=label):
                fake = _FakeKernel32(**knobs)
                process = _FakeProcess([('ok out', 'ok err')])
                result, exc = self._run(fake, process)

                self.assertIsNone(result, f'{label}: success must not be reported')
                self.assertIsInstance(exc, OSError, f'{label}: expected an explicit OSError')
                self.assertEqual(fake.close_calls, expected_closes,
                                 f'{label}: close attempts must stay bounded')
                self.assertEqual(fake.terminated, 1, f'{label}: terminate must run first')
                self.assertTrue(
                    any('CloseHandle' in note for note in self._notes(exc)),
                    f'{label}: diagnostic lost: {self._notes(exc)}',
                )

    def test_success_path_closes_once_and_returns_completed_process(self):
        fake = _FakeKernel32()
        process = _FakeProcess([('ok out', 'ok err')])
        result, exc = self._run(fake, process)

        self.assertIsNone(exc)
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual(result.args, ARGV)
        self.assertEqual(result.stdout, 'ok out')
        self.assertEqual(result.stderr, 'ok err')
        self.assertEqual(fake.close_calls, 1, 'a confirmed close is final')
        self.assertEqual(fake.terminated, 0, 'a healthy run must not terminate the job')
        self.assertEqual(process.stdout.close_calls, 0)
        self.assertEqual(process.stderr.close_calls, 0)

    @staticmethod
    def events_close(events):
        return events.index('close_job')


def _as_handle(value):
    """Plain integer for a HANDLE recorded by a fake kernel32.

    The oracle passes ``ctypes.c_void_p`` wrappers around, so fake recorders
    normalise them; comparing a wrapper to an int would fail for the wrong
    reason.
    """
    return getattr(value, 'value', value)


class FallbackPidGuardTests(unittest.TestCase):
    """Cross-platform oracle for the guarded PID fallback's access rights.

    The fake enforces the documented Win32 contract instead of trusting the
    caller: ``GetProcessTimes`` fails unless the handle was opened with a
    query right, so a fallback that asked only for terminate/synchronize fails
    here exactly as it would on Windows. No process is ever killed — the
    fakes make ``TerminateProcess`` countable, and the guard must not reach it
    on an identity mismatch.
    """

    HANDLE = 0xBEEF

    class _FakeProcessKernel32:
        """Minimal kernel32 double: records rights, identity and kills."""

        def __init__(self, created, wait_result=WAIT_TIMEOUT):
            self.created = created
            self.wait_result = wait_result
            self.requested_access = []
            self.opened_pids = []
            self.terminated = []
            self.closed = []

        def OpenProcess(self, access, inherit, pid):
            self.requested_access.append(access)
            self.opened_pids.append(pid)
            return FallbackPidGuardTests.HANDLE

        def GetProcessTimes(self, handle, *refs):
            # Kernel rule: a handle opened without a query right is rejected.
            if not self.requested_access[-1] & PROCESS_QUERY_LIMITED_INFORMATION:
                return 0
            refs[0]._obj.value = self.created
            return 1

        def TerminateProcess(self, handle, exit_code):
            self.terminated.append(_as_handle(handle))
            # A killed process becomes signaled; without this the guard's
            # bounded wait would spin for its full deadline.
            self.wait_result = WAIT_OBJECT_0
            return 1

        def WaitForSingleObject(self, handle, timeout_ms):
            return self.wait_result

        def CloseHandle(self, handle):
            self.closed.append(_as_handle(handle))
            return 1

    @staticmethod
    @contextlib.contextmanager
    def _patched(fake):
        """Install the fake kernel and the Windows-only last-error stubs.

        The fallback is Windows-only, but its decision logic is not, so the
        two ``ctypes`` last-error helpers are created where the OS lacks them
        and everything else is a plain module-attribute swap.
        """
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(sys.modules[__name__], '_kernel32', lambda: fake))
            stack.enter_context(
                mock.patch.object(ctypes, 'set_last_error', lambda value: None, create=True))
            stack.enter_context(
                mock.patch.object(ctypes, 'get_last_error', lambda: 0, create=True))
            yield

    def test_fallback_identity_guard_and_query_right(self):
        """One oracle for the fallback's rights and its refusal to kill a reuse."""
        with self.subTest(case='matching-identity-requests-query-right'):
            fake = self._FakeProcessKernel32(created=777)
            with self._patched(fake):
                _cleanup_pid(4321, 777)

            self.assertEqual(len(fake.requested_access), 1,
                             'the fallback must open exactly one handle')
            access = fake.requested_access[0]
            self.assertTrue(
                access & PROCESS_QUERY_LIMITED_INFORMATION,
                f'GetProcessTimes needs a query right; got 0x{access:X}',
            )
            self.assertTrue(access & PROCESS_TERMINATE, 'it must still be able to terminate')
            self.assertTrue(access & PROCESS_SYNCHRONIZE, 'it must still be able to wait')
            self.assertEqual(fake.terminated, [self.HANDLE],
                             'a matching identity is the one case that may kill')
            self.assertEqual(fake.closed, [self.HANDLE], 'the handle must be closed')

        with self.subTest(case='identity-mismatch-refuses-and-closes'):
            fake = self._FakeProcessKernel32(created=999)
            with self._patched(fake):
                with self.assertRaises(AssertionError) as caught:
                    _cleanup_pid(4321, 777)

            message = str(caught.exception)
            self.assertIn('not the fixture process any more', message)
            self.assertIn('refusing to kill', message)
            self.assertEqual(fake.terminated, [],
                             'a reused PID must never reach TerminateProcess')
            self.assertEqual(fake.closed, [self.HANDLE],
                             'the still-open handle must be closed before failing')

        with self.subTest(case='missing-identity-opens-nothing'):
            fake = self._FakeProcessKernel32(created=777)
            with self._patched(fake):
                with self.assertRaises(AssertionError) as caught:
                    _cleanup_pid(4321, None)

            self.assertIn('without a recorded creation time', str(caught.exception))
            self.assertEqual(fake.requested_access, [],
                             'no identity means no handle may be opened at all')
            self.assertEqual(fake.terminated, [], 'nothing may be killed by PID alone')
            self.assertEqual(fake.closed, [], 'no handle was opened, so none is closed')


class ImportHygieneTests(unittest.TestCase):
    """Cheap cross-platform guards so pytest collection stays meaningful."""

    def test_runner_imports_without_windows(self):
        # The import above is itself the oracle: a module that bound WinDLL or
        # pulled dependencies at import time would have failed collection on
        # this OS. The binder must also stay a plain lazy callable.
        self.assertTrue(callable(subprocess_utils.run_network_command_windows))
        self.assertTrue(callable(subprocess_utils._windows_kernel32))


#: Bytes the startup marker holds once the gate interpreter has begun running.
#: Content, not mere existence, is asserted so the oracle cannot be satisfied by
#: a stray empty file.
GATE_STARTED_BYTES = b'gate-ready'


def _gate_code_with_startup_marker(marker):
    """Prepend a one-statement startup marker to the production gate program.

    The write runs *before* the original program's gate-byte read, so the file
    appearing proves the interpreter that executes the gate is already running.
    The original program is kept byte-for-byte as the suffix, so the gate-byte
    protocol is untouched: only the moment we can observe startup changes.

    ``repr`` embeds the path as a safe Python literal, which matters on Windows
    where the separator is a backslash and temporary paths may contain spaces.
    """
    original = subprocess_utils._WINDOWS_GATE_CODE
    prefix = (
        'import pathlib as _gate_startup_path\n'
        f'_gate_startup_path.Path({str(marker)!r}).write_bytes({GATE_STARTED_BYTES!r})\n'
    )
    return prefix + original


class _StartupGateKernel32:
    """Real kernel32 with one bounded wait in front of the job assignment.

    Every other entry point is delegated to the *same* bound DLL object, so the
    job, handle, terminate and close calls are the production calls, not stand-
    ins. ``AssignProcessToJobObject`` waits for the startup marker and then
    invokes the real API with the arguments it was given.

    Waiting here is what makes the ordering deterministic. If the launcher runs
    the gate in the process that was started, the marker is written by that same
    process and the assignment still covers the whole tree. If the launcher
    creates a real child first, the child (and everything it later spawns) is
    already outside the job by the time the assignment lands, so the reused
    reaping oracle must fail. Neither outcome is assumed; both are observable.
    """

    POLL_S = 0.01

    def __init__(self, real_kernel32, marker, deadline_s=10.0):
        self._real = real_kernel32
        self._marker = Path(marker)
        self._deadline_s = deadline_s
        self.assignment_calls = 0
        self.marker_wait_s = None
        self.marker_seen_before_assignment = False

    def __getattr__(self, name):
        # Only reached for names this proxy does not define, so the job,
        # terminate and close entry points are the real bound functions.
        return getattr(self._real, name)

    def AssignProcessToJobObject(self, job, process_handle):
        self.assignment_calls += 1
        started = time.monotonic()
        deadline = started + self._deadline_s
        while not self._marker.exists():
            if time.monotonic() >= deadline:
                raise AssertionError(
                    'the gate interpreter never started within '
                    f'{self._deadline_s}s: no startup marker at {self._marker}. '
                    'A silent pass here would make this oracle vacuous, so the '
                    'missing startup is reported instead of assigning anyway.'
                )
            time.sleep(self.POLL_S)
        self.marker_wait_s = time.monotonic() - started
        self.marker_seen_before_assignment = True
        return self._real.AssignProcessToJobObject(job, process_handle)


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object oracles are native-only')
class WindowsJobRunnerTests(unittest.TestCase):

    def test_success_preserves_argv_env_cwd_output_and_exitcode(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = [
                sys.executable, '-I', '-c', ECHO_CODE, '0',
                'ünï cøde ✔', 'two words',
            ]
            result = subprocess_utils.run_network_command_windows(
                fixture, cwd=tmp, env=dict(os.environ, FIXTURE_VAR='våł'), timeout=30,
            )
            self.assertIsInstance(result, subprocess.CompletedProcess)
            self.assertEqual(result.args, fixture)
            self.assertEqual(result.returncode, 0)
            stdout = result.stdout.splitlines()
            self.assertEqual(len(stdout), 3, stdout)
            self.assertEqual(stdout[0], 'args:ünï cøde ✔|two words')
            self.assertEqual(stdout[1], 'env:våł')
            self.assertTrue(stdout[2].startswith('cwd:'), stdout[2])
            reported = stdout[2][len('cwd:'):].strip()
            self.assertEqual(_normalized_path(reported), _normalized_path(tmp))
            self.assertEqual(result.stderr, '')

    def test_nonzero_exit_and_stderr_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = [sys.executable, '-I', '-c', ECHO_CODE, '7']
            result = subprocess_utils.run_network_command_windows(
                fixture, cwd=tmp, env=os.environ, timeout=30,
            )
            self.assertEqual(result.returncode, 7)
            self.assertIn('stderr-line héllo', result.stderr)

    def test_spawn_failure_preserves_a_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess_utils.run_network_command_windows(
                ['definitely-missing-git-ᛗ.exe'], cwd=tmp, env=os.environ, timeout=30,
            )
            self.assertEqual(result.returncode, 127)
            self.assertTrue(result.stderr.strip(), 'spawn failure must keep a diagnostic')

    def test_timeout_reaps_tree_when_gate_interpreter_starts_before_assignment(self):
        """The reaping oracle with the job assignment moved behind gate startup.

        The ordinary venv run only fails if the launcher happens to spawn a
        real child before the parent assigns the job, so on its own it can pass
        by luck. Here the assignment is held until the gate interpreter has
        written its first-instruction marker: for a launcher that redirects to
        a real child, that child is running and outside the job before the
        assignment lands, which is precisely the regression.

        The whole body is the existing reaping test invoked unchanged inside
        the patches, so the fixtures, held handles, timeout, partial output,
        bootstrap and unrelated-sentinel assertions cannot drift away from it.
        No new process, cleanup or reaping mechanism is introduced.
        """
        real_binder = subprocess_utils._windows_kernel32
        bound = []
        with tempfile.TemporaryDirectory() as tmp:
            startup_marker = Path(tmp) / 'gate-started'

            def binding_proxy():
                proxy = _StartupGateKernel32(real_binder(), startup_marker)
                bound.append(proxy)
                return proxy

            with mock.patch.object(
                    subprocess_utils, '_WINDOWS_GATE_CODE',
                    _gate_code_with_startup_marker(startup_marker)), \
                 mock.patch.object(
                     subprocess_utils, '_windows_kernel32', binding_proxy):
                # The reused oracle's own assertions are the payload: if the
                # startup ordering lets the tree escape, it fails here.
                self.test_timeout_stops_living_root_tree_keeps_output_and_spares_unrelated()

            # Prove the ordering was actually forced rather than assumed: the
            # marker existed before the real assignment ran, and the assignment
            # happened exactly once through this proxy.
            self.assertEqual(len(bound), 1, 'the runner must bind kernel32 exactly once')
            self.assertEqual(bound[0].assignment_calls, 1,
                             'the job assignment must run exactly once')
            self.assertTrue(bound[0].marker_seen_before_assignment,
                            'the assignment was never held behind gate startup')
            self.assertIsNotNone(bound[0].marker_wait_s,
                                 'no bounded wait was performed for the startup marker')
            self.assertTrue(startup_marker.exists(),
                            'the gate interpreter never wrote its startup marker')

    def test_timeout_stops_living_root_tree_keeps_output_and_spares_unrelated(self):
        with tempfile.TemporaryDirectory() as tmp:
            markers = Path(tmp)
            unrelated = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(150)'])
            child_handle = None
            grand_handle = None
            covered = set()
            bootstrap_procs = []
            bootstrap_handles = []
            thread = None
            real_popen = subprocess.Popen

            def recording_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                bootstrap_procs.append(process)
                # Capture the gate bootstrap's own HANDLE while it still owns
                # it; the same handle is waited on after the runner returns.
                bootstrap_handles.append(_popen_native_handle(process))
                return process

            try:
                with mock.patch.object(subprocess_utils.subprocess, 'Popen', recording_popen):
                    fixture = _hang_fixture_argv(markers, 'hang')
                    thread, outcome = _run_in_thread(
                        lambda: subprocess_utils.run_network_command_windows(
                            fixture, cwd=tmp, env=dict(os.environ, FIXTURE_VAR='x'),
                            timeout=12.0,
                        )
                    )
                    child = _wait_for_child_fixture(markers)
                    # Hold both processes with persistent handles before the
                    # runner's deadline can cancel anything.
                    child_handle = _open_process_handle(child['child_pid'])
                    covered.add(child['child_pid'])
                    grand_handle = _open_process_handle(child['grand_pid'])
                    covered.add(child['grand_pid'])
                    self.assertFalse(_wait_for_handle(child_handle, 0),
                                     'fixture root must be alive before cancellation')
                    self.assertFalse(_wait_for_handle(grand_handle, 0),
                                     'fixture grandchild must be alive before cancellation')
                    thread.join(timeout=30)
                self.assertFalse(thread.is_alive(), 'runner did not return after the deadline')
                self.assertLess(outcome.get('elapsed', float('inf')), 25,
                                'cancellation was not bounded')
                error = outcome.get('error')
                self.assertIsInstance(error, subprocess.TimeoutExpired)
                self.assertEqual(error.cmd, fixture)
                self.assertEqual(error.timeout, 12.0)
                # Four independent postconditions, each in its own subTest:
                # a missing partial output is a distinct defect from an
                # unreaped tree, and one failing must not hide the others.
                with self.subTest(oracle='partial-output'):
                    self.assertIn('partial output', str(error.output))
                # Same handles, after cancellation: the whole tree is gone.
                with self.subTest(oracle='tree-reaped'):
                    self.assertTrue(
                        _wait_handles_signaled([child_handle, grand_handle], 10.0),
                        'job cancellation must signal the exact root and grandchild handles',
                    )
                with self.subTest(oracle='bootstrap-reaped'):
                    self.assertEqual(len(bootstrap_handles), 1,
                                     'expected exactly one real bootstrap')
                    self.assertTrue(
                        _wait_handle_signaled(bootstrap_handles[0], 10.0),
                        'the real bootstrap must be reaped too',
                    )
                with self.subTest(oracle='unrelated-alive'):
                    # The unrelated sentinel must be untouched by the job cleanup.
                    self.assertIsNone(unrelated.poll(), 'unrelated sentinel was killed')
            finally:
                _run_cleanups(
                    lambda: _cleanup_handle(child_handle),
                    lambda: _cleanup_handle(grand_handle),
                    lambda: _cleanup_fixture_pids(markers, covered),
                    *[lambda process=process: _stop_popen(process)
                      for process in bootstrap_procs],
                    lambda: _stop_thread(thread),
                    lambda: _stop_popen(unrelated),
                )

    def test_timeout_reaps_tree_after_direct_child_already_exited(self):
        with tempfile.TemporaryDirectory() as tmp:
            markers = Path(tmp)
            unrelated = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(150)'])
            child_handle = None
            grand_handle = None
            covered = set()
            bootstrap_procs = []
            bootstrap_handles = []
            thread = None
            real_popen = subprocess.Popen

            def recording_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                bootstrap_procs.append(process)
                bootstrap_handles.append(_popen_native_handle(process))
                return process

            try:
                with mock.patch.object(subprocess_utils.subprocess, 'Popen', recording_popen):
                    fixture = _hang_fixture_argv(markers, 'exit-root')
                    thread, outcome = _run_in_thread(
                        lambda: subprocess_utils.run_network_command_windows(
                            fixture, cwd=tmp, env=dict(os.environ, FIXTURE_VAR='x'),
                            timeout=12.0,
                        )
                    )
                    child = _wait_for_child_fixture(markers)
                    child_handle = _open_process_handle(child['child_pid'])
                    covered.add(child['child_pid'])
                    grand_handle = _open_process_handle(child['grand_pid'])
                    covered.add(child['grand_pid'])
                    self.assertFalse(_wait_for_handle(child_handle, 0),
                                     'fixture root must be gated before release')
                    self.assertFalse(_wait_for_handle(grand_handle, 0),
                                     'fixture grandchild must be alive before release')
                    # Release the gated root only after both handles exist.
                    (markers / 'allow-exit').write_text('1')
                    self.assertTrue(
                        _wait_handle_signaled(child_handle, 5.0),
                        'fixture root must exit before cancellation',
                    )
                    self.assertFalse(
                        _wait_for_handle(grand_handle, 0),
                        'grandchild must hold the pipe before cancellation',
                    )
                    thread.join(timeout=30)
                self.assertFalse(thread.is_alive(), 'runner did not return after the deadline')
                self.assertLess(outcome.get('elapsed', float('inf')), 25,
                                'cancellation was not bounded')
                error = outcome.get('error')
                self.assertIsInstance(error, subprocess.TimeoutExpired)
                self.assertEqual(error.timeout, 12.0)
                # Partial output proves the pipes reached EOF, i.e. the job
                # reaped the orphan that was holding them.
                self.assertIn('partial output', str(error.output))
                self.assertTrue(
                    _wait_handle_signaled(grand_handle, 10.0),
                    'the orphaned pipe holder must be reaped by the job',
                )
                self.assertEqual(len(bootstrap_handles), 1, 'expected exactly one real bootstrap')
                self.assertTrue(
                    _wait_handle_signaled(bootstrap_handles[0], 10.0),
                    'the real bootstrap must be reaped too',
                )
                self.assertIsNone(unrelated.poll(), 'unrelated sentinel was killed')
            finally:
                _run_cleanups(
                    lambda: _cleanup_handle(child_handle),
                    lambda: _cleanup_handle(grand_handle),
                    lambda: _cleanup_fixture_pids(markers, covered),
                    *[lambda process=process: _stop_popen(process)
                      for process in bootstrap_procs],
                    lambda: _stop_thread(thread),
                    lambda: _stop_popen(unrelated),
                )

    def test_job_setup_failure_fails_closed_without_starting_git(self):
        cases = [
            ('binder-raises', None, 'binder'),
            ('create-raises', _FakeKernel32(create_exc=OSError(1, 'CreateJobObjectW exploded')), 'fake'),
            ('create-returns-zero', _FakeKernel32(create_ok=False), 'fake'),
            ('configure-false', _FakeKernel32(config_ok=False), 'fake'),
            ('assign-false', _FakeKernel32(assign_ok=False), 'fake'),
        ]
        for label, fake, mode in cases:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as tmp:
                    markers = Path(tmp)
                    argv = _hang_fixture_argv(markers, 'hang')
                    spawned = []
                    real_popen = subprocess.Popen

                    def recording_popen(*args, _real=real_popen, _spawned=spawned, **kwargs):
                        # Both names are bound as defaults so the closure cannot
                        # read a later subTest's rebinding of the loop variables.
                        process = _real(*args, **kwargs)
                        _spawned.append(process)
                        return process

                    try:
                        binder_patch = (
                            mock.patch.object(subprocess_utils, '_windows_kernel32', _raise_binder)
                            if mode == 'binder'
                            else mock.patch.object(
                                subprocess_utils, '_windows_kernel32', lambda _fake=fake: _fake,
                            )
                        )
                        with binder_patch, mock.patch.object(
                            subprocess_utils.subprocess, 'Popen', recording_popen,
                        ):
                            with self.assertRaises(OSError):
                                subprocess_utils.run_network_command_windows(
                                    argv, cwd=tmp, env=os.environ, timeout=30,
                                )
                        # Fail closed before the gate: the command never started.
                        self.assertFalse((markers / 'ran').exists(), 'Git must not start')
                        # The job must be created and configured BEFORE the
                        # bootstrap exists, so a kernel/config failure cannot
                        # leave even a gate-only child behind. Assignment is
                        # the one step that needs a live PID, so that case
                        # spawns exactly one bootstrap and must still reap it.
                        expected_spawns = 1 if label == 'assign-false' else 0
                        self.assertEqual(
                            len(spawned), expected_spawns,
                            f'{label}: the job must be created/configured before spawning',
                        )
                        for process in spawned:
                            self.assertTrue(
                                _wait_popen_exit(process, 10.0),
                                f'{label}: the real bootstrap must not leak',
                            )
                        if fake is not None and fake.job is not None:
                            self.assertTrue(fake.closed, 'the job handle must close on failure')
                    finally:
                        _run_cleanups(
                            *[lambda process=process: _stop_popen(process)
                              for process in spawned],
                            lambda _markers=markers: _cleanup_fixture_pids(_markers),
                        )

    def test_baseexception_cancellation_cleans_the_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            markers = Path(tmp)
            unrelated = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(150)'])
            child_handle = None
            grand_handle = None
            covered = set()
            bootstrap_procs = []
            bootstrap_handles = []
            thread = None
            real_popen = subprocess.Popen

            def interrupting_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                bootstrap_procs.append(process)
                bootstrap_handles.append(_popen_native_handle(process))
                original = process.communicate

                def communicate(*cargs, **ckwargs):
                    try:
                        return original(*cargs, **ckwargs)
                    except subprocess.TimeoutExpired:
                        # Simulate a non-timeout cancellation racing a real run.
                        raise KeyboardInterrupt from None

                process.communicate = communicate
                return process

            try:
                with mock.patch.object(subprocess_utils.subprocess, 'Popen', interrupting_popen):
                    fixture = _hang_fixture_argv(markers, 'hang')
                    thread, outcome = _run_in_thread(
                        lambda: subprocess_utils.run_network_command_windows(
                            fixture, cwd=tmp, env=os.environ, timeout=12.0,
                        )
                    )
                    child = _wait_for_child_fixture(markers)
                    child_handle = _open_process_handle(child['child_pid'])
                    covered.add(child['child_pid'])
                    grand_handle = _open_process_handle(child['grand_pid'])
                    covered.add(child['grand_pid'])
                    self.assertFalse(_wait_for_handle(child_handle, 0),
                                     'fixture root must be alive before cancellation')
                    self.assertFalse(_wait_for_handle(grand_handle, 0),
                                     'fixture grandchild must be alive before cancellation')
                    thread.join(timeout=30)
                self.assertFalse(thread.is_alive(), 'runner did not return after the deadline')
                self.assertLess(outcome.get('elapsed', float('inf')), 25,
                                'cancellation was not bounded')
                self.assertIsInstance(outcome.get('error'), KeyboardInterrupt)
                # Assert on the same handles BEFORE any fallback reaping: the
                # job must have signalled them on its own.
                self.assertTrue(
                    _wait_handles_signaled([child_handle, grand_handle], 10.0),
                    'a BaseException cancellation must still reap the tree',
                )
                self.assertEqual(len(bootstrap_handles), 1, 'expected exactly one real bootstrap')
                self.assertTrue(
                    _wait_handle_signaled(bootstrap_handles[0], 10.0),
                    'the real bootstrap must be reaped too',
                )
                self.assertIsNone(unrelated.poll(), 'unrelated sentinel was killed')
            finally:
                _run_cleanups(
                    lambda: _cleanup_handle(child_handle),
                    lambda: _cleanup_handle(grand_handle),
                    lambda: _cleanup_fixture_pids(markers, covered),
                    *[lambda process=process: _stop_popen(process)
                      for process in bootstrap_procs],
                    lambda: _stop_thread(thread),
                    lambda: _stop_popen(unrelated),
                )


if __name__ == '__main__':
    unittest.main()
