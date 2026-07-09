"""Embedded workspace terminal support for Hermes Web UI.

The terminal is intentionally independent from the agent execution path.  It
starts a shell with an explicit cwd/env per process and never mutates
process-global os.environ, which avoids expanding the session-env race tracked
in the agent execution layer.
"""

from __future__ import annotations

import errno
import atexit
import codecs
import os
import queue
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

_TERMINAL_SUPPORTED = sys.platform != "win32"

if _TERMINAL_SUPPORTED:
    import fcntl
    import select
    import termios
else:
    fcntl = None  # type: ignore[assignment]
    select = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]


def _set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _winsize(rows: int, cols: int) -> bytes:
    rows = max(8, min(int(rows or 24), 80))
    cols = max(20, min(int(cols or 80), 240))
    return struct.pack("HHHH", rows, cols, 0, 0)


def _safe_close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


@dataclass
class TerminalSession:
    session_id: str
    workspace: str
    proc: subprocess.Popen
    master_fd: int
    rows: int = 24
    cols: int = 80
    output: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=2000))
    closed: threading.Event = field(default_factory=threading.Event)
    reader: threading.Thread | None = None
    # Serializes fd-touching ops (os.write, resize ioctl) against os.close, so a
    # write can never land on a master_fd that was closed and whose number was
    # already recycled by a concurrent openpty — that would inject the user's
    # keystrokes into a foreign fd. Holders re-check ``closed`` under this lock
    # and bail if the terminal has been torn down.
    io_lock: threading.Lock = field(default_factory=threading.Lock)
    # Wall-clock of the last input written or output produced. Drives which
    # terminal the cap evicts first (least-recently-active): a shell abandoned
    # at its prompt has neither, so it sorts oldest and is evicted before an
    # actively used one.
    last_activity: float = field(default_factory=time.time)

    def is_alive(self) -> bool:
        return not self.closed.is_set() and self.proc.poll() is None

    def put_output(self, event: str, payload: dict) -> None:
        self.last_activity = time.time()
        try:
            self.output.put_nowait((event, payload))
        except queue.Full:
            # Keep the terminal responsive by dropping the oldest queued chunk.
            try:
                self.output.get_nowait()
            except queue.Empty:
                pass
            try:
                self.output.put_nowait((event, payload))
            except queue.Full:
                pass


_TERMINALS: dict[str, TerminalSession] = {}
_LOCK = threading.RLock()
# Hard cap on concurrently live embedded terminals. Each holds a shell process,
# a pty master fd, and a reader thread; a client that drops its output stream
# without POSTing /api/terminal/close (tab close, crash, network drop) leaves
# the shell running (no PDEATHSIG — see the note below), so without a ceiling
# these accumulate over a long uptime toward fd/thread exhaustion (#4633). The
# cap evicts the least-recently-active terminal to make room. Generous enough
# that real interactive use never trips it.
_MAX_TERMINALS = 32
_spawn_queue: queue.Queue = queue.Queue()
_spawn_supervisor_started = False
_spawn_supervisor_lock = threading.Lock()
_spawn_supervisor_thread: threading.Thread | None = None
_terminal_descendant_reaper_lock = threading.Lock()
_TERMINAL_DESCENDANT_REAPER_LIMIT = 64


@dataclass
class _SpawnRequest:
    kwargs: dict
    done: threading.Event = field(default_factory=threading.Event)
    timed_out: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    proc: subprocess.Popen | None = None
    error: BaseException | None = None


def _reap_abandoned_spawn(proc: subprocess.Popen) -> bool:
    if proc.poll() is not None:
        return True
    try:
        os.killpg(proc.pid, signal.SIGHUP)
    except (OSError, ProcessLookupError):
        try:
            proc.terminate()
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
        try:
            proc.wait(timeout=1.0)
        except (subprocess.TimeoutExpired, ProcessLookupError):
            pass
    if proc.poll() is None:
        print("terminal abandoned spawn cleanup failed", flush=True)
        return False
    return True


def _reap_terminal_descendants(
    terminal_pgid: int,
    limit: int = _TERMINAL_DESCENDANT_REAPER_LIMIT,
) -> int:
    """Reap exited descendants that still belong to a terminal-owned process group."""
    if not _TERMINAL_SUPPORTED:
        return 0
    try:
        terminal_pgid = abs(int(terminal_pgid))
    except (TypeError, ValueError):
        return 0
    if terminal_pgid <= 0:
        return 0
    reaped = 0
    with _terminal_descendant_reaper_lock:
        for _ in range(max(0, int(limit))):
            try:
                pid, _status = os.waitpid(-terminal_pgid, os.WNOHANG)
            except (ChildProcessError, OSError):
                break
            if pid == 0:
                break
            reaped += 1
    return reaped


def _spawn_supervisor_loop() -> None:
    while True:
        request = None
        try:
            request = _spawn_queue.get()
            try:
                proc = subprocess.Popen(**request.kwargs)
                with request.lock:
                    if request.timed_out.is_set():
                        _reap_abandoned_spawn(proc)
                    else:
                        request.proc = proc
                    request.done.set()
            except BaseException as exc:
                with request.lock:
                    try:
                        request.error = exc
                    except BaseException:
                        pass
                    request.done.set()
        except BaseException as exc:
            if request is not None:
                try:
                    request.error = exc
                except BaseException:
                    pass
                try:
                    request.done.set()
                except BaseException:
                    pass
            time.sleep(0.01)


def _spawn_supervisor_entry() -> None:
    while True:
        try:
            _spawn_supervisor_loop()
        except BaseException:
            time.sleep(0.01)
            pass


def _ensure_spawn_supervisor() -> None:
    global _spawn_supervisor_started, _spawn_supervisor_thread
    with _spawn_supervisor_lock:
        if _spawn_supervisor_started and _spawn_supervisor_thread and _spawn_supervisor_thread.is_alive():
            return
        thread = threading.Thread(target=_spawn_supervisor_entry, daemon=True)
        thread.start()
        _spawn_supervisor_thread = thread
        _spawn_supervisor_started = True


if _TERMINAL_SUPPORTED:
    _ensure_spawn_supervisor()


# NOTE on parent-death-signal: a previous version of this module set
# PR_SET_PDEATHSIG via a preexec_fn to terminate orphaned PTY shells when the
# WebUI process crashed.  That broke every Linux user (#2853): WebUI runs a
# ThreadingHTTPServer, so the Popen call happens on a short-lived per-request
# thread, and PR_SET_PDEATHSIG is per-thread.  The PTY shell registered the
# spawning thread as its "parent" and was killed with SIGTERM the instant that
# thread joined — within ~10 ms of opening the terminal — surfacing as the
# `[terminal closed]` banner.  The graceful path is covered by
# `atexit.register(close_all_terminals)` and the explicit `close_terminal`
# call sites; hard kills of the WebUI process leak the shell, which is the
# tradeoff for working on Linux at all.


def _decode_terminal_output(decoder, data: bytes) -> str:
    """Decode PTY bytes without stripping terminal control sequences."""
    return decoder.decode(data)


def _shell_path() -> str:
    shell = os.environ.get("SHELL") or ""
    if shell and Path(shell).exists():
        return shell
    return shutil.which("zsh") or shutil.which("bash") or shutil.which("sh") or "/bin/sh"


def _shell_argv(shell: str) -> list[str]:
    name = Path(shell).name
    if name in {"zsh", "bash", "sh"}:
        return [shell, "-i"]
    return [shell]


def _reader_loop(term: TerminalSession) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        while not term.closed.is_set():
            if term.proc.poll() is not None:
                break
            try:
                ready, _, _ = select.select([term.master_fd], [], [], 0.25)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                data = os.read(term.master_fd, 8192)
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):
                    break
                raise
            if not data:
                break
            text = _decode_terminal_output(decoder, data)
            if text:
                term.put_output("output", {"text": text})
    except Exception as exc:
        term.put_output("terminal_error", {"error": str(exc)})
    finally:
        term.closed.set()
        code = term.proc.poll()
        _reap_terminal_descendants(term.proc.pid)
        term.put_output("terminal_closed", {"exit_code": code})
        # The shell has exited (or its pty broke): retire the session so its
        # master fd and _TERMINALS entry are released. Previously only an
        # explicit close_terminal() / restart / atexit did this, so a shell that
        # exited on its own (user typed `exit`, process died) leaked its master
        # fd and dict entry for the rest of the WebUI's uptime. ``expected=term``
        # makes this a no-op if a restart already replaced the entry with a new
        # terminal for the same session id.
        close_terminal(term.session_id, expected=term)


def _set_size(term: TerminalSession, rows: int, cols: int) -> None:
    term.rows = max(8, min(int(rows or term.rows or 24), 80))
    term.cols = max(20, min(int(cols or term.cols or 80), 240))
    # The ioctl touches master_fd, so guard it against a concurrent close (and
    # the fd-number recycling that can follow) with the same io_lock as writes.
    with term.io_lock:
        if not term.closed.is_set():
            try:
                fcntl.ioctl(term.master_fd, termios.TIOCSWINSZ, _winsize(term.rows, term.cols))
            except OSError:
                pass
    try:
        if term.proc.poll() is None:
            os.killpg(term.proc.pid, signal.SIGWINCH)
    except (OSError, ProcessLookupError):
        pass


def _enforce_terminal_cap(*, exclude_sid: str | None = None) -> None:
    """Evict terminals until there is room under ``_MAX_TERMINALS``.

    Picks a victim under the lock but closes it *outside* the lock (close_terminal
    may spend up to a couple of seconds killing/​waiting on the shell). Prefers a
    dead-process terminal, else the least-recently-active one — an abandoned
    shell idle at its prompt sorts oldest and goes first. ``exclude_sid`` is the
    session about to reuse/replace its own entry, so it never evicts itself.
    """
    if not _TERMINAL_SUPPORTED:
        return
    # Bounded loop: at most the current population; guards against a pathological
    # spin if close_terminal somehow can't remove an entry.
    for _ in range(_MAX_TERMINALS + 1):
        victim_sid = None
        victim_term = None
        with _LOCK:
            # Reuse/restart of an existing sid replaces in place — no growth.
            if exclude_sid in _TERMINALS:
                return
            if len(_TERMINALS) < _MAX_TERMINALS:
                return
            candidates = [
                (sid, term) for sid, term in _TERMINALS.items() if sid != exclude_sid
            ]
            if not candidates:
                return
            dead = [(sid, term) for sid, term in candidates if not term.is_alive()]
            victim_sid, victim_term = (
                dead[0] if dead else min(candidates, key=lambda kv: kv[1].last_activity)
            )
        # ``expected=victim_term`` so that if this sid was restarted/replaced in
        # the gap between picking it and closing it, we don't tear down the new
        # (possibly active) terminal — symmetric with the reader-loop retire.
        close_terminal(victim_sid, expected=victim_term)


def start_terminal(session_id: str, workspace: Path, rows: int = 24, cols: int = 80, restart: bool = False) -> TerminalSession:
    """Start or return the embedded terminal for a WebUI session."""
    if not _TERMINAL_SUPPORTED:
        raise NotImplementedError("Embedded terminal is not supported on Windows")
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    cwd = str(Path(workspace).expanduser().resolve())
    if not Path(cwd).is_dir():
        raise ValueError("workspace is not a directory")

    # Enforce the cap before spawning. Done outside the main lock below (the
    # eviction's process teardown must not run while _LOCK is held for the whole
    # spawn), and skipped for a same-sid reuse/restart, which replaces rather
    # than adds an entry.
    _enforce_terminal_cap(exclude_sid=sid)

    with _LOCK:
        current = _TERMINALS.get(sid)
        if current and current.is_alive() and not restart and current.workspace == cwd:
            _set_size(current, rows, cols)
            return current
        if current:
            close_terminal(sid)

        master_fd, slave_fd = os.openpty()
        # Build a safe env: allowlist common shell vars, strip API keys and secrets.
        # The PTY shell is an interactive UI surface — do not leak server credentials.
        _SAFE_ENV_KEYS = {
            "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL",
            "LC_CTYPE", "LC_MESSAGES", "LANGUAGE", "TZ", "TMPDIR", "TEMP",
            "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        }
        env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
        env.update(
            {
                "TERM": "xterm-256color",
                "COLORTERM": "truecolor",
                "COLUMNS": str(cols),
                "LINES": str(rows),
                "PWD": cwd,
                "HERMES_WEBUI_TERMINAL": "1",
            }
        )
        shell = _shell_path()
        # Keep the shell in its own process group for explicit cleanup via
        # close_terminal()/close_all_terminals(); do not use PDEATHSIG here.
        request = _SpawnRequest(
            {
                "args": _shell_argv(shell),
                "cwd": cwd,
                "env": env,
                "stdin": slave_fd,
                "stdout": slave_fd,
                "stderr": slave_fd,
                "close_fds": True,
                # Required so cleanup can signal the whole interactive shell tree.
                "start_new_session": True,
            }
        )
        _ensure_spawn_supervisor()
        _spawn_queue.put(request)
        try:
            if not request.done.wait(timeout=5.0):
                timed_out = False
                with request.lock:
                    if not request.done.is_set():
                        request.timed_out.set()
                        timed_out = True
                if timed_out:
                    raise TimeoutError("terminal spawn timeout - supervisor unresponsive")
            if request.error:
                raise request.error
            proc = request.proc
            if proc is None:
                raise RuntimeError("terminal spawn failed without process")
        except BaseException:
            _safe_close_fd(master_fd)
            _safe_close_fd(slave_fd)
            raise
        os.close(slave_fd)
        _set_nonblocking(master_fd)

        term = TerminalSession(
            session_id=sid,
            workspace=cwd,
            proc=proc,
            master_fd=master_fd,
            rows=rows,
            cols=cols,
        )
        _set_size(term, rows, cols)
        term.reader = threading.Thread(target=_reader_loop, args=(term,), daemon=True)
        term.reader.start()
        _TERMINALS[sid] = term
        return term


def get_terminal(session_id: str) -> TerminalSession | None:
    if not _TERMINAL_SUPPORTED:
        return None
    with _LOCK:
        term = _TERMINALS.get(str(session_id or ""))
        if term and term.is_alive():
            return term
        return term


def write_terminal(session_id: str, data: str) -> None:
    if not _TERMINAL_SUPPORTED:
        raise NotImplementedError("Embedded terminal is not supported on Windows")
    term = get_terminal(session_id)
    if not term or not term.is_alive():
        raise KeyError("terminal not running")
    # Re-check ``closed`` under io_lock and write while holding it, so the fd
    # can't be closed (and its number recycled by another openpty) between the
    # check and the write — which would inject this input into a foreign fd.
    with term.io_lock:
        if term.closed.is_set():
            raise KeyError("terminal not running")
        os.write(term.master_fd, str(data or "").encode("utf-8", errors="replace"))
    term.last_activity = time.time()


def resize_terminal(session_id: str, rows: int, cols: int) -> None:
    if not _TERMINAL_SUPPORTED:
        raise NotImplementedError("Embedded terminal is not supported on Windows")
    term = get_terminal(session_id)
    if not term:
        raise KeyError("terminal not running")
    _set_size(term, rows, cols)


def close_terminal(session_id: str, *, expected: TerminalSession | None = None) -> bool:
    """Tear down the terminal for *session_id*: kill the shell, close the pty
    master fd, reap descendants, and drop the ``_TERMINALS`` entry.

    ``expected`` guards the retire-from-reader-loop path: only act if the live
    entry is still that exact terminal, so an old reader thread finishing after
    a restart cannot tear down the *new* terminal that replaced it (the old
    one's fd was already closed by the restart's own close_terminal call).
    """
    if not _TERMINAL_SUPPORTED:
        return False
    sid = str(session_id or "")
    with _LOCK:
        if expected is not None and _TERMINALS.get(sid) is not expected:
            return False
        term = _TERMINALS.pop(sid, None)
    if not term:
        return False
    term.closed.set()
    try:
        if term.proc.poll() is None:
            try:
                os.killpg(term.proc.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            try:
                term.proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(term.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    term.proc.wait(timeout=1.0)
                except (subprocess.TimeoutExpired, ProcessLookupError):
                    pass
    finally:
        # ``closed`` is already set above, so a writer/​resizer blocked on io_lock
        # will see it and bail rather than touch the fd we are about to close.
        with term.io_lock:
            try:
                os.close(term.master_fd)
            except OSError:
                pass
        _reap_terminal_descendants(term.proc.pid)
    return True


def close_all_terminals() -> None:
    """Best-effort reap of embedded shells during graceful WebUI shutdown."""
    with _LOCK:
        session_ids = list(_TERMINALS)
    for session_id in session_ids:
        close_terminal(session_id)


atexit.register(close_all_terminals)
