"""Dependency-light helpers for launching child processes consistently."""

from __future__ import annotations

import ctypes
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit


def windows_hide_flags() -> int:
    """Hide a short-lived console child on Win32 and remain a POSIX no-op.

    ``CREATE_NO_WINDOW`` keeps captured stdout and stderr connected, unlike
    detaching the process. Passing ``0`` elsewhere preserves the subprocess
    default. See #5692.
    """
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


# Windows Job Object support (stdlib ctypes only; see run_network_command_windows).
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WINDOWS_JOB_KILL_EXITCODE = 1


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _windows_kernel32():
    """Bind the five kernel32 entry points the job runner needs."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    ]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.TerminateJobObject.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


# The gate bootstrap makes the Job assignment race-free: the updater assigns
# the bootstrap to the job BEFORE the gate byte is sent, and Git (with every
# helper and grandchild it spawns) is born inside the job instead of joining
# one after a Popen round trip. The bootstrap must not write to stdout/stderr;
# Git inherits those pipes directly. Closing stdin without the gate byte (or
# any job/kernel failure before the gate) leaves Git unstarted.
_WINDOWS_GATE_CODE = """\
import subprocess, sys
if sys.stdin.buffer.read(1) != b'1':
    sys.exit(0)
try:
    rc = subprocess.call(
        sys.argv[1:],
        stdin=subprocess.DEVNULL,
        shell=False,
        creationflags=0x08000000,  # CREATE_NO_WINDOW, matching windows_hide_flags()
    )
except OSError as exc:
    sys.stderr.write(str(exc) + '\\n')
    sys.exit(127)
sys.exit(rc)
"""


def _windows_job_terminate(kernel32, job, cleanup):
    """Best-effort TerminateJobObject; never raises, always reports."""
    try:
        if not kernel32.TerminateJobObject(job, _WINDOWS_JOB_KILL_EXITCODE):
            cleanup.append(
                f"TerminateJobObject failed (WinError {ctypes.get_last_error()})")
    except Exception as term_exc:
        cleanup.append(f"TerminateJobObject raised: {term_exc!r}")


def _windows_close_job(kernel32, job, cleanup):
    """One CloseHandle attempt; True only when the close is confirmed."""
    try:
        if kernel32.CloseHandle(job):
            return True
        cleanup.append(f"CloseHandle failed (WinError {ctypes.get_last_error()})")
    except Exception as close_exc:
        cleanup.append(f"CloseHandle raised: {close_exc!r}")
    return False


def _windows_abort_drain(process):
    """Close the gate and reap the bootstrap after job termination.

    Returns ``(stdout, stderr, note)``. The gate has not been (successfully)
    delivered in any failure path, so closing stdin makes an unassigned
    bootstrap exit without starting Git, and the bounded ``communicate`` then
    drains whatever was written and reaps the direct child. When the drain
    still fails, a writer is alive against our reader ends: closing the text
    wrapper would block on Windows (its close joins a reader thread parked in
    ReadFile, which only finishes once every writer handle is gone), so the
    reader handles are left to the ``Popen`` finalizer and the direct child is
    killed and reaped — an unassigned bootstrap never got the gate byte and has
    no tree, a job member is already dead, and taskkill is never used. Every
    step is best-effort: a raise is captured as a note and cleanup advances.
    """
    if process is None:
        return None, None, ""
    notes = []
    # The gate has not been (successfully) delivered in any failure path;
    # closing stdin makes an unassigned bootstrap exit without starting Git.
    if process.stdin is not None and not process.stdin.closed:
        try:
            process.stdin.close()
        except Exception as close_exc:  # cleanup must always advance
            notes.append(f"gate stdin close raised: {close_exc!r}")
    try:
        stdout, stderr = process.communicate(timeout=5)
        return stdout, stderr, "; ".join(notes)
    except subprocess.TimeoutExpired:
        notes.append("pipe drain timed out; reader handles left to the finalizer")
    except Exception as drain_exc:  # the caller's original error must survive
        notes.append(f"cleanup drain failed: {drain_exc!r}")
    try:
        process.kill()
    except Exception as kill_exc:
        notes.append(f"kill raised: {kill_exc!r}")
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        notes.append("direct child did not exit after kill")
    except Exception as wait_exc:
        notes.append(f"wait raised: {wait_exc!r}")
    return None, None, "; ".join(notes)


def run_network_command_windows(argv, *, cwd, env, timeout):
    """Run ``argv`` under a per-call Job Object and hard-stop the tree on timeout.

    Replaces the ``taskkill /T`` approach that could not find descendants once
    their direct parent had exited: job membership survives any member's exit,
    so terminating the job — and KILL_ON_JOB_CLOSE when the last handle closes —
    reaps Git helpers and grandchildren no matter who already left. BREAKAWAY
    is never allowed, so spawned helpers cannot escape.

    The job is created and configured BEFORE the gate bootstrap
    (``_WINDOWS_GATE_CODE``) is spawned, so a binder or kernel failure can
    never leak a started child; the bootstrap is then assigned to the job
    before the gate byte, so no Git process can exist outside the job. The
    bootstrap interpreter is ``sys._base_executable`` — on a Windows venv
    ``sys.executable`` is ``venvlauncher.exe``, whose real-interpreter child
    would be spawned before the assignment — and a frozen runtime or an
    unusable base path fails closed with OSError before any kernel or spawn
    work. Every
    failure fails closed before the gate: the tree is terminated and the job
    handle closed BEFORE the pipes are touched, a stuck unassigned bootstrap is
    killed and reaped, and the original error propagates. Cleanup steps are
    independent — a raise or false return at any step is captured as a
    diagnostic on the original error and cleanup still advances — and the job
    handle is dropped only on a confirmed close, otherwise one bounded final
    close is attempted. A success-path close that stays unconfirmed raises
    OSError after the same bounded terminate-and-close cleanup instead of
    returning the result. ``timeout`` covers bootstrap startup plus the
    command; on expiry the external ``subprocess.TimeoutExpired(argv, timeout,
    output, stderr)`` shape and the partial output are preserved.
    """
    started = time.monotonic()
    # sys.executable on a Windows venv is venvlauncher.exe, which spawns the
    # real interpreter before AssignProcessToJobObject can run, leaving Git
    # outside the job; bootstrap with the base interpreter, never a fallback
    # that would reintroduce the race.
    bootstrap = getattr(sys, "_base_executable", None)
    if getattr(sys, "frozen", False):
        raise OSError(
            "Windows job runner needs a standard CPython runtime; frozen "
            "executables do not provide a usable gate bootstrap interpreter"
        )
    if not isinstance(bootstrap, str) or not bootstrap:
        raise OSError(
            "Windows job runner needs sys._base_executable to name the gate "
            "bootstrap interpreter"
        )
    base_interpreter = Path(bootstrap)
    if not base_interpreter.is_absolute() or not base_interpreter.is_file():
        raise OSError(
            "sys._base_executable is not an absolute path to an existing "
            f"interpreter: {bootstrap!r}"
        )
    kernel32 = _windows_kernel32()
    job = None
    process = None
    exc = None
    try:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        limits = _JobObjectExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        # The bootstrap is owned only from here: a kernel failure above never
        # leaves one behind.
        process = subprocess.Popen(
            [bootstrap, "-I", "-c", _WINDOWS_GATE_CODE, *argv],
            cwd=str(cwd), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=windows_hide_flags(),
        )
        # Popen._handle is private, but CPython keeps the real process HANDLE
        # open for the Popen's whole lifetime, so the assignment cannot race a
        # pid recycle the way an OpenProcess(pid) round trip could.
        if not kernel32.AssignProcessToJobObject(job, ctypes.c_void_p(int(process._handle))):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        remaining = max(0.0, timeout - (time.monotonic() - started))
        stdout, stderr = process.communicate(input="1", timeout=remaining)
    except BaseException as raised:
        exc = raised
    if exc is not None:
        # Failure path: terminate, then close, the job BEFORE any pipe work.
        # Each cleanup step is independent — a raise or false return is noted
        # and cleanup still advances — so nothing masks the original error,
        # and KILL_ON_JOB_CLOSE on the last handle sweeps up whatever a failed
        # terminate left behind.
        cleanup = []
        if job:
            _windows_job_terminate(kernel32, job, cleanup)
            if not _windows_close_job(kernel32, job, cleanup):
                # Unconfirmed first close: retain ownership for the single
                # bounded final attempt; never discard a live handle.
                if _windows_close_job(kernel32, job, cleanup):
                    job = None
            else:
                job = None
        try:
            stdout, stderr, drain_note = _windows_abort_drain(process)
        except Exception as drain_exc:  # cleanup must always advance
            stdout, stderr = None, None
            drain_note = f"cleanup drain raised: {drain_exc!r}"
        if drain_note:
            cleanup.append(drain_note)
        if cleanup:
            exc.add_note("windows job cleanup: " + "; ".join(cleanup))
        if isinstance(exc, subprocess.TimeoutExpired):
            raise subprocess.TimeoutExpired(
                argv, timeout,
                output=stdout if stdout is not None else exc.stdout,
                stderr=stderr if stderr is not None else exc.stderr,
            ) from exc
        raise exc
    cleanup = []
    if not _windows_close_job(kernel32, job, cleanup):
        # Success must never return while the job close is unconfirmed: kill
        # whatever the job still holds, make the single bounded final close
        # attempt, and surface the failure instead of the command result.
        _windows_job_terminate(kernel32, job, cleanup)
        if _windows_close_job(kernel32, job, cleanup):
            cleanup.append("final CloseHandle confirmed after terminate")
        error = OSError("job close unconfirmed after a successful command")
        error.add_note("windows job cleanup: " + "; ".join(cleanup))
        raise error
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


# Environment variables git honours that let a WebUI-spawned child do something
# other than what the caller asked: the askpass/proxy entries turn a fail-closed
# error into an interactive prompt or helper command, and the GIT_DIR/work-tree/
# config entries point Git at state other than the checkout supplied by the caller.
GIT_ENV_SCRUB_KEYS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_SSH_VARIANT",
    "GIT_PROXY_COMMAND",
)
GIT_ENV_SCRUB_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")

_CREDENTIAL_IN_URL_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@\s'\"]+)@")
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:access_token|oauth_token|private_token|client_secret|app_secret|"
    r"api[_-]?key|token|password|secret|auth|key)=)[^&\s\"]+",
    re.IGNORECASE,
)
_URL_REMOTE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_REMOTE_HELPER_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*::")
_SCP_SSH_REMOTE_RE = re.compile(
    r"^(?:[^/@:\s]+@)?(?:\[[^\[\]/\s]+\]|[^/@:\s]+):(?!:).+$"
)
_SHELL_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")



def clean_git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a Git environment without inherited prompts or redirections.

    ``GIT_TERMINAL_PROMPT=0`` prevents Git's built-in terminal prompt, but Git
    consults inherited askpass helpers first, so those entries must be removed.
    ``SSH_AUTH_SOCK`` is deliberately retained so non-interactive SSH agent
    authentication continues to work.
    """
    env = os.environ.copy()
    if extra:
        env.update(extra)
    for key in GIT_ENV_SCRUB_KEYS:
        env.pop(key, None)
    for key in list(env):
        if key.startswith(GIT_ENV_SCRUB_PREFIXES):
            env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Git Credential Manager can use GUI/browser prompts independently of Git's
    # terminal prompt. Cached credentials still work when interaction is disabled.
    env["GCM_INTERACTIVE"] = "never"
    return env


def _scoped_git_config_values(
    cwd: str | Path,
    env: dict[str, str],
    key: str,
    *,
    executable: str,
    trusted_only: bool = False,
) -> tuple[tuple[str, str], ...]:
    if trusted_only:
        return tuple(
            (scope, value)
            for scope, _key, value in _scoped_git_config_entries(
                cwd, env, f"^{re.escape(key)}$", executable=executable,
            )
        )
    try:
        result = subprocess.run(
            [
                executable, "config", "--includes", "--show-scope", "-z",
                "--get-all", key,
            ],
            cwd=str(cwd), shell=False, capture_output=True, timeout=10,
            env=env, creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if result.returncode == 129:
        # Git before 2.26 has no --show-scope. Explicit reads preserve the
        # system/global/local/worktree order used for first-match gitProxy.
        values = []
        for scope in ("system", "global", "local", "worktree"):
            try:
                scoped = subprocess.run(
                    [executable, "config", f"--{scope}", "--includes", "-z", "--get-all", key],
                    cwd=str(cwd), shell=False, capture_output=True, timeout=10,
                    env=env, creationflags=windows_hide_flags(),
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if scoped.returncode == 0:
                output = scoped.stdout or b""
                if isinstance(output, bytes):
                    output = output.decode("utf-8", errors="replace")
                values.extend((scope, value) for value in output.removesuffix("\0").split("\0"))
        return tuple(values)
    if result.returncode != 0:
        return ()
    raw_output = result.stdout or b""
    if isinstance(raw_output, str):
        raw_output = raw_output.encode("utf-8", errors="replace")
    raw_fields = raw_output.split(b"\0")
    if raw_fields and raw_fields[-1] == b"":
        raw_fields.pop()
    fields = [value.decode("utf-8", errors="replace") for value in raw_fields]
    if len(fields) % 2:
        return ()
    return tuple(zip(fields[0::2], fields[1::2], strict=True))


def _scoped_git_config_entries(
    cwd: str | Path,
    env: dict[str, str],
    pattern: str,
    *,
    executable: str,
) -> tuple[tuple[str, str, str], ...]:
    """Read direct system/global entries, without Git 2.26's scope option.

    Includes are intentionally disabled: their apparent scope does not establish
    ownership of the included file. Executable settings must be in a primary
    system/global config file, not in a checkout-selected include.
    """
    entries = []
    for scope in ("system", "global"):
        try:
            result = subprocess.run(
                [executable, "config", f"--{scope}", "--no-includes", "-z",
                 "--get-regexp", pattern],
                cwd=str(cwd), shell=False, capture_output=True, timeout=10,
                env=env, creationflags=windows_hide_flags(),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        output = result.stdout or b""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        for entry in output.rstrip("\0").split("\0"):
            key, separator, value = entry.partition("\n")
            if separator:
                entries.append((scope, key, value))
    return tuple(entries)


def trusted_git_credential_config(
    cwd: str | Path,
    env: dict[str, str],
    *,
    executable: str = "git",
) -> tuple[tuple[str, str], ...]:
    """Read credential-helper entries from trusted system and user scopes.

    This preserves both generic ``credential.helper`` entries and URL-scoped
    entries such as ``credential.https://github.com.helper``. Repository and
    worktree config are deliberately excluded. Git performs its normal URL
    matching after the trusted entries are re-applied on the command line.
    """
    return tuple(
        (key, value)
        for scope, key, value in _scoped_git_config_entries(
            cwd,
            env,
            r"^credential(\..*)?\.helper$",
            executable=executable,
        )
        if scope in {"system", "global"}
    )


def _git_shell_path(executable: str, cwd: str | Path, env: dict[str, str]) -> str:
    """Use Git's shell, including its bundled shell on native Windows."""
    try:
        result = subprocess.run(
            [executable, "var", "GIT_SHELL_PATH"], cwd=str(cwd), env=env,
            capture_output=True, text=True, timeout=10,
            creationflags=windows_hide_flags(),
        )
        if result.returncode == 0 and (result.stdout or "").strip():
            return result.stdout.strip()
        # Older Git has no GIT_SHELL_PATH variable. Its POSIX default is /bin/sh;
        # Git for Windows installs usr/bin/sh.exe alongside its mingw tree.
        if sys.platform == "win32":
            result = subprocess.run(
                [executable, "--exec-path"], cwd=str(cwd), env=env,
                capture_output=True, text=True, timeout=10,
                creationflags=windows_hide_flags(),
            )
            if result.returncode == 0:
                for parent in Path(result.stdout.strip()).parents:
                    shell = parent / "usr" / "bin" / "sh.exe"
                    if shell.is_file():
                        return str(shell)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "/bin/sh" if sys.platform != "win32" else "git-shell-path-is-unavailable"


def noninteractive_git_env(
    cwd: str | Path,
    env: dict[str, str],
    *,
    executable: str = "git",
    args: list[str] | None = None,
) -> dict[str, str]:
    """Force SSH batch mode; probe custom commands only for SSH destinations."""
    if args is not None:
        urls = _remote_urls_for_command(args, cwd, env, executable=executable)
        if not urls or not any(_is_ssh_remote(url) for url in urls):
            # Keep checkout SSH commands suppressed even when no probe is needed.
            return {**env, "GIT_SSH_COMMAND": "ssh -oBatchMode=yes", "GIT_SSH_VARIANT": "ssh"}
    trusted_commands = tuple(
        value
        for scope, value in _scoped_git_config_values(
            cwd, env, "core.sshCommand", executable=executable, trusted_only=True,
        )
        if scope in {"system", "global"}
    )
    trusted_variants = tuple(
        value
        for scope, value in _scoped_git_config_values(
            cwd, env, "ssh.variant", executable=executable, trusted_only=True,
        )
        if scope in {"system", "global"}
    )
    ssh_command = trusted_commands[-1] if trusted_commands else "ssh"
    try:
        lexer = shlex.shlex(ssh_command, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        if sys.platform == "win32":
            lexer.escape = ""
        command_words = list(lexer)
    except ValueError:
        command_words = []
    variant = trusted_variants[-1].strip().lower() if trusted_variants else "auto"
    if variant == "auto":
        # Git interprets core.sshCommand as a shell command on every platform.
        executable_words = list(command_words)
        while executable_words and _SHELL_ASSIGNMENT_RE.match(executable_words[0]):
            executable_words.pop(0)
        if executable_words and executable_words[0] in {"command", "exec"}:
            executable_words.pop(0)
            while executable_words and executable_words[0].startswith("-"):
                executable_words.pop(0)
        if executable_words and executable_words[0] == "env":
            executable_words.pop(0)
            while executable_words and (
                executable_words[0].startswith("-")
                or _SHELL_ASSIGNMENT_RE.match(executable_words[0])
            ):
                executable_words.pop(0)
        executable_name = executable_words[0] if executable_words else ""
        executable_name = executable_name.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1]
        executable_name = executable_name.lower().removesuffix(".exe")
        variant = {
            "ssh": "ssh",
            "plink": "plink",
            "putty": "putty",
            "tortoiseplink": "tortoiseplink",
        }.get(executable_name, "unsupported")
    if variant == "unsupported" and command_words:
        # Like Git's auto detection, ask the trusted command to print its
        # configuration, not connect. Never probe checkout-selected commands.
        try:
            with subprocess.Popen(
                [_git_shell_path(executable, cwd, env), "-c", f"{ssh_command} -G -oBatchMode=yes localhost"],
                cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=(os.name != "nt"),
                creationflags=windows_hide_flags(),
            ) as probe:
                try:
                    if probe.wait(timeout=5) == 0:
                        variant = "ssh"
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        os.killpg(probe.pid, signal.SIGKILL)
                    else:
                        probe.kill()
                    probe.wait()
        except (OSError, subprocess.TimeoutExpired):
            pass
    if variant not in {"ssh", "plink", "putty", "tortoiseplink"}:
        # Git's "simple" variant and unknown custom transports have no general
        # non-interactive flag. Do not run one when the no-prompt invariant
        # cannot be established.
        ssh_command = "git-ssh-variant-is-not-supported-by-hermes-webui"
        variant = "simple"
        batch_option = ""
    else:
        batch_option = (
            "-batch"
            if variant in {"plink", "putty", "tortoiseplink"}
            else "-oBatchMode=yes"
        )
    if variant == "ssh":
        for index, word in enumerate(command_words):
            option = ""
            if word == "-o" and index + 1 < len(command_words):
                option = command_words[index + 1]
            elif word.startswith("-o"):
                option = word[2:]
            parts = re.split(r"[=\s]+", option.strip(), maxsplit=1)
            if len(parts) == 2 and parts[0].lower() == "batchmode" and parts[1].lower() != "yes":
                ssh_command = "git-ssh-command-disables-batch-mode"
                variant = "simple"
                batch_option = ""
                break
    if batch_option:
        ssh_command = f"{ssh_command} {batch_option}"
    configured = dict(env)
    configured["GIT_SSH_COMMAND"] = ssh_command
    configured["GIT_SSH_VARIANT"] = variant
    return configured


def noninteractive_git_argv(
    args: list[str],
    *,
    executable: str = "git",
    credential_config: tuple[tuple[str, str], ...] = (),
) -> list[str]:
    """Build Git argv that cannot prompt or run checkout-controlled helpers."""
    argv = [executable]
    for key, value in (
        ("core.askPass", ""),
        ("protocol.ext.allow", "never"),
        ("credential.helper", ""),
        ("credential.interactive", "false"),
    ):
        argv.extend(["-c", f"{key}={value}"])
    for key, value in credential_config:
        argv.extend(["-c", f"{key}={value}"])
    argv.extend(args)
    return argv


def _git_config_value(
    cwd: str | Path,
    env: dict[str, str],
    key: str,
    *,
    executable: str,
) -> str | None:
    try:
        result = subprocess.run(
            [executable, "config", "--get", key],
            cwd=str(cwd),
            shell=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def _explicit_remote_arg(args: list[str]) -> str | None:
    if not args or args[0] not in {"fetch", "pull", "push", "ls-remote"}:
        return None
    consumes_value = {
        "--depth", "--deepen", "--shallow-since", "--shallow-exclude",
        "--negotiation-tip", "--upload-pack", "--receive-pack", "--server-option",
    }
    skip_next = False
    for value in args[1:]:
        if skip_next:
            skip_next = False
            continue
        if value in consumes_value:
            skip_next = True
            continue
        if value.startswith("-"):
            continue
        return value
    return None


def _remote_urls_for_command(
    args: list[str],
    cwd: str | Path,
    env: dict[str, str],
    *,
    executable: str,
) -> tuple[str, ...] | None:
    """Resolve every push destination, or the single fetch/pull destination."""
    remote = _explicit_remote_arg(args)
    push = bool(args and args[0] == "push")
    if not remote:
        branch_name = ""
        try:
            head = subprocess.run(
                [executable, "symbolic-ref", "--quiet", "--short", "HEAD"],
                cwd=str(cwd), shell=False, capture_output=True, text=True,
                timeout=10, env=env, creationflags=windows_hide_flags(),
            )
        except (OSError, subprocess.TimeoutExpired):
            head = None
        if head is not None and head.returncode == 0:
            branch_name = (head.stdout or "").strip()
        keys = []
        if push:
            if branch_name:
                keys.append(f"branch.{branch_name}.pushRemote")
            keys.append("remote.pushDefault")
        if branch_name:
            keys.append(f"branch.{branch_name}.remote")
        for key in keys:
            remote = _git_config_value(cwd, env, key, executable=executable)
            if remote:
                break
        remote = remote or "origin"
    if remote == ".":
        return ()
    # Named push remotes use pushurl (and pushInsteadOf), not their fetch URL.
    # An explicit URL destination is not a remote name, so keep Git's own
    # resolution (ls-remote --get-url echoes it back) as the fallback.
    if push:
        try:
            resolved = subprocess.run(
                [executable, "remote", "get-url", "--push", "--all", remote],
                cwd=str(cwd), shell=False, capture_output=True, text=True,
                timeout=10, env=env, creationflags=windows_hide_flags(),
            )
            if resolved.returncode == 0:
                return tuple((resolved.stdout or "").splitlines()) or None
        except (OSError, subprocess.TimeoutExpired):
            return None
    # --get-url performs only Git's configured URL rewrite; it does not contact
    # the remote. This exposes a repo-local url.*.insteadOf that turns an
    # apparently HTTPS remote into git:// before the proxy guard decides.
    try:
        resolved = subprocess.run(
            [executable, "ls-remote", "--get-url", remote],
            cwd=str(cwd), shell=False, capture_output=True, text=True,
            timeout=10, env=env, creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if resolved.returncode != 0:
        return None
    url = (resolved.stdout or "").strip()
    return (url,) if url else None


def repository_git_proxy_blocks(
    args: list[str],
    cwd: str | Path,
    env: dict[str, str],
    *,
    executable: str = "git",
) -> bool:
    """Return whether repo config could proxy this command's git:// remote.

    ``core.gitProxy`` is multi-valued and a command-line empty value does not
    mask a lower-scope entry. Deny only a git:// network operation whose active
    checkout supplies that key; legitimate git:// remotes without such a local
    override remain usable.
    """
    if not args or args[0] not in {"fetch", "pull", "push", "ls-remote"}:
        return False
    proxy_values = _scoped_git_config_values(
        cwd, env, "core.gitProxy", executable=executable,
    )
    if not any(scope in {"local", "worktree"} for scope, _value in proxy_values):
        return False
    urls = _remote_urls_for_command(args, cwd, env, executable=executable)
    # A local proxy plus an unresolvable active URL is not safe to pass through:
    # the subsequent network command may resolve more successfully and execute it.
    if urls is None:
        return True
    return any(_repository_proxy_selected(url, proxy_values) for url in urls)


def _repository_proxy_selected(url: str, proxy_values: tuple[tuple[str, str], ...]) -> bool:
    """Whether this destination selects a checkout-controlled proxy."""
    if not url.lower().startswith("git://"):
        return False
    try:
        # Git matches core.gitProxy's DOMAIN against parse_connect_url()'s
        # URL-decoded hostandport, before brackets and an explicit port are
        # removed for the connection. Preserve their spelling and case here.
        hostandport = unquote(urlsplit(url).netloc)
    except (UnicodeError, ValueError):
        return True
    if not hostandport:
        return True

    # Git considers all scopes in config order and selects the first value whose
    # optional ``for DOMAIN`` suffix matches the complete hostandport or a suffix
    # following a dot. Only reject when that selected value came from the repo.
    selected_proxy: tuple[str, str] | None = None
    for scope, value in proxy_values:
        for_pos = value.find(" for ")
        if for_pos < 0:
            selected_proxy = (scope, value)
            break
        domain = value[for_pos + 5 :]
        suffix_start = len(hostandport) - len(domain)
        if (
            suffix_start >= 0
            and hostandport.endswith(domain)
            and (suffix_start == 0 or hostandport[suffix_start - 1] == ".")
        ):
            selected_proxy = (scope, value[:for_pos])
            break
    if selected_proxy is None:
        return False
    scope, command = selected_proxy
    return scope in {"local", "worktree"} and command not in {"", "none"}


def sanitize_git_diagnostic(
    output: str,
    *,
    sensitive_paths: tuple[str | Path, ...] = (),
    limit: int = 300,
) -> str:
    """Remove credentials and caller-named private paths from Git diagnostics."""
    if not output:
        return ""
    sanitized = str(output)
    for path in sorted((str(value) for value in sensitive_paths if value), key=len, reverse=True):
        sanitized = sanitized.replace(path, "<redacted-path>")
        try:
            sanitized = sanitized.replace(str(Path(path).expanduser().resolve()), "<redacted-path>")
        except (OSError, ValueError):
            pass
    sanitized = _CREDENTIAL_IN_URL_RE.sub(r"\1<redacted>@", sanitized)
    sanitized = _GITHUB_TOKEN_RE.sub("<redacted>", sanitized)
    sanitized = _QUERY_SECRET_RE.sub(r"\1<redacted>", sanitized)
    sanitized = sanitized.strip()
    if len(sanitized) > limit:
        sanitized = sanitized[:limit].rstrip() + "…"
    return sanitized


def _is_ssh_remote(remote: str) -> bool:
    if _REMOTE_HELPER_RE.match(remote):
        return False
    if _URL_REMOTE_RE.match(remote):
        return remote.lower().startswith("ssh://")
    # A Windows drive prefix is a local path, not an scp host.
    if re.match(r"^[A-Za-z]:[\\/]", remote):
        return False
    return _SCP_SSH_REMOTE_RE.match(remote) is not None


def is_safe_diagnostic_remote(remote: str) -> bool:
    """Accept only built-in network transports that cannot name remote helpers."""
    value = remote.strip()
    if _REMOTE_HELPER_RE.match(value):
        return False
    if _URL_REMOTE_RE.match(value):
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            _port = parsed.port
        except ValueError:
            return False
        return bool(hostname) and parsed.scheme.lower() in {"http", "https", "ssh", "git"}
    return _SCP_SSH_REMOTE_RE.match(value) is not None
