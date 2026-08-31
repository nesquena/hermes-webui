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
import collections
import hashlib
import json
import os
import queue
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

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


# Bounds both the replay backlog and each subscriber's live queue: how many
# output chunks are buffered before the oldest is dropped. One value so the
# catch-up backlog and the per-viewer queue stay in lockstep.
_OUTPUT_BUFFER_MAXLEN = 2000
_MANAGED_OUTPUT_BACKLOG_BYTES = 4 * 1024 * 1024
_MANAGED_OUTPUT_BACKLOG_MAX_RECORDS = 2000
_MANAGED_TERMINAL_MAX = 2
_MANAGED_TERMINAL_MAX_VIEWERS = 8
_MANAGED_TERMINAL_IDLE_SECONDS = 6 * 60 * 60
_MANAGED_CAPABILITY_TTL_SECONDS = 10 * 60
_MANAGED_CAPABILITY_OPERATIONS = frozenset({"stream", "input", "stop"})
_MANAGED_RUNNER_READINESS_MAX_BYTES = 256
_MANAGED_RUNNER_READINESS_TIMEOUT_SECONDS = 5.0


class ManagedTerminalLimitError(RuntimeError):
    pass


class ManagedTerminalViewerLimitError(RuntimeError):
    pass


class ManagedTerminalStartError(RuntimeError):
    def __init__(self, state: str):
        self.state = state
        self.recovery = "stop_locally" if state == "active_elsewhere" else None
        super().__init__(state)


class _ManagedSubscriberQueue(queue.Queue):
    """A non-blocking, byte- and record-bounded managed viewer queue."""

    def __init__(self, byte_limit: int, record_limit: int):
        super().__init__(maxsize=0)
        self.byte_limit = byte_limit
        self.record_limit = record_limit
        self.buffered_bytes = 0
        self.lagged = False

    def publish(self, item: tuple, generation: str | None) -> bool:
        size = _managed_output_size(item[1], item[2])
        with self.not_full:
            if self.lagged:
                return False
            if (
                self.buffered_bytes + size > self.byte_limit
                or self._qsize() >= self.record_limit
            ):
                dropped = len(self.queue)
                self.queue.clear()
                self.unfinished_tasks = max(0, self.unfinished_tasks - dropped)
                reset = (
                    item[0],
                    "terminal_reset",
                    {"generation": generation},
                )
                self._put(reset)
                self.unfinished_tasks += 1
                self.buffered_bytes = _managed_output_size(reset[1], reset[2])
                self.lagged = True
                self.not_empty.notify()
                return False
            self._put(item)
            self.unfinished_tasks += 1
            self.buffered_bytes += size
            self.not_empty.notify()
            return True

    def _get(self):
        item = super()._get()
        self.buffered_bytes = max(
            0,
            self.buffered_bytes - _managed_output_size(item[1], item[2]),
        )
        return item


@dataclass
class ManagedTerminalSingleton:
    fd: int
    path: Path

    def close(self) -> None:
        if self.fd < 0:
            return
        fd, self.fd = self.fd, -1
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            _safe_close_fd(fd)


def acquire_managed_terminal_singleton(
    path: Path,
) -> ManagedTerminalSingleton | None:
    """Acquire the WebUI-process bridge lock without following replacements."""
    if not _TERMINAL_SUPPORTED:
        return None
    lock_path = Path(path)
    if not lock_path.is_absolute():
        return None
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
        opened = os.fstat(fd)
        named = os.lstat(lock_path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or opened.st_dev != named.st_dev
            or opened.st_ino != named.st_ino
        ):
            _safe_close_fd(fd)
            return None
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        after = os.fstat(fd)
        named_after = os.lstat(lock_path)
        if (
            after.st_dev != named_after.st_dev
            or after.st_ino != named_after.st_ino
            or not stat.S_ISREG(named_after.st_mode)
        ):
            fcntl.flock(fd, fcntl.LOCK_UN)
            _safe_close_fd(fd)
            return None
        return ManagedTerminalSingleton(fd=fd, path=lock_path)
    except OSError:
        try:
            _safe_close_fd(fd)
        except UnboundLocalError:
            pass
        return None


@dataclass
class TerminalSession:
    session_id: str
    workspace: str
    proc: subprocess.Popen
    master_fd: int
    rows: int = 24
    cols: int = 80
    # Output is fanned out to every attached viewer. Each SSE consumer
    # subscribe()s its own queue; put_output broadcasts to all of them plus a
    # bounded backlog that a newly attaching consumer replays. Two tabs/windows
    # on the same session therefore each get the FULL byte stream — previously a
    # single shared queue was read destructively, so two consumers split the
    # output between them and only one saw terminal_closed.
    _subscribers: list = field(default_factory=list)
    _backlog: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=_OUTPUT_BUFFER_MAXLEN)
    )
    _sub_lock: threading.Lock = field(default_factory=threading.Lock)
    _next_output_seq: int = 1
    closed: threading.Event = field(default_factory=threading.Event)
    reader: threading.Thread | None = None
    reader_started: bool = False
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
    # Wall-clock of when the terminal last had zero attached viewers, or None
    # while at least one is attached. The reaper closes a terminal that has been
    # unwatched for longer than the idle grace: a client that drops its output
    # stream without POSTing /api/terminal/close (tab close, crash, network drop)
    # otherwise leaves the shell running forever (no PDEATHSIG). A terminal is
    # born unwatched, so a spawn nobody ever attaches to is reaped too. The grace
    # spans transient reconnects (a tab refresh re-attaches and clears it).
    unwatched_since: float | None = field(default_factory=time.time)
    kind: str = "shell"
    generation: str | None = None
    handle: str | None = None
    argv: tuple[str, ...] = ()
    pgid: int | None = None
    persistent_when_unwatched: bool = False
    runner_owns_lease: bool = False
    lease_owner_pid: int | None = None
    owned_pgid_verified: bool = False
    backlog_bytes: int = 0
    _capabilities: dict[str, tuple[str, str, float]] = field(default_factory=dict)
    _activity_lock: threading.Lock = field(default_factory=threading.Lock)
    _activity_epoch: int = 0

    def is_alive(self) -> bool:
        if self.closed.is_set():
            return False
        if self.kind == "claude_code" and self.owned_pgid_verified:
            return _owned_group_exists(self)
        return self.proc.poll() is None

    def subscribe(
        self,
        after_seq: int | None = None,
        generation: str | None = None,
    ) -> queue.Queue:
        """Attach a viewer: return a queue seeded with the current backlog and
        registered to receive all subsequent output.

        A reconnecting EventSource supplies its last received sequence so only
        newer backlog entries are replayed. A new viewer leaves ``after_seq``
        unset and receives the full bounded backlog.
        """
        managed = self.kind == "claude_code"
        q: queue.Queue = (
            _ManagedSubscriberQueue(
                _MANAGED_OUTPUT_BACKLOG_BYTES,
                _MANAGED_OUTPUT_BACKLOG_MAX_RECORDS,
            )
            if managed
            else queue.Queue(maxsize=_OUTPUT_BUFFER_MAXLEN)
        )
        reset = False
        with self._sub_lock:
            if managed and len(self._subscribers) >= _MANAGED_TERMINAL_MAX_VIEWERS:
                raise ManagedTerminalViewerLimitError("managed terminal viewer limit")
            if managed:
                floor = self._backlog[0][0] if self._backlog else self._next_output_seq
                latest = self._next_output_seq - 1
                reset = generation != self.generation or (
                    after_seq is not None
                    and (after_seq < floor or after_seq > latest)
                )
            if reset:
                reset_item = (
                    self._next_output_seq - 1,
                    "terminal_reset",
                    {"generation": self.generation},
                )
                if managed:
                    q.publish(reset_item, self.generation)
                else:
                    q.put_nowait(reset_item)
            else:
                for item in self._backlog:
                    if after_seq is None or item[0] > after_seq:
                        if managed:
                            if not q.publish(item, self.generation):
                                break
                        else:
                            q.put_nowait(item)
            self._subscribers.append(q)
            self.unwatched_since = None  # a viewer is attached
        if reset:
            _signal_owned_group(self, signal.SIGWINCH)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass
            if not self._subscribers:
                self.unwatched_since = time.time()

    def put_output(self, event: str, payload: dict) -> None:
        with self._sub_lock:
            with self._activity_lock:
                self.last_activity = time.time()
                self._activity_epoch += 1
            item = (self._next_output_seq, event, payload)
            self._next_output_seq += 1
            self._backlog.append(item)
            if self.kind == "claude_code":
                self.backlog_bytes += _managed_output_size(event, payload)
                while (
                    self._backlog
                    and (
                        self.backlog_bytes > _MANAGED_OUTPUT_BACKLOG_BYTES
                        or len(self._backlog)
                        > _MANAGED_OUTPUT_BACKLOG_MAX_RECORDS
                    )
                ):
                    _seq, dropped_event, dropped_payload = self._backlog.popleft()
                    self.backlog_bytes -= _managed_output_size(
                        dropped_event, dropped_payload
                    )
            # Keep sequence assignment, backlog append, and non-blocking fanout in
            # one publication order. Releasing this lock before fanout lets two
            # producers enqueue seq N+1 before seq N to a live subscriber.
            lagged = []
            for q in self._subscribers:
                if self.kind == "claude_code":
                    if not q.publish(item, self.generation):
                        lagged.append(q)
                    continue
                try:
                    q.put_nowait(item)
                except queue.Full:
                    # Slow viewer: drop its oldest chunk to stay responsive.
                    # Isolated per subscriber, so one lagging tab can't starve
                    # another; all queue operations remain non-blocking.
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(item)
                    except queue.Full:
                        pass
            for q in lagged:
                self._subscribers.remove(q)
            if lagged and not self._subscribers:
                self.unwatched_since = time.time()


def _managed_output_size(event: str, payload: dict) -> int:
    if event == "output" and isinstance(payload.get("text"), str):
        return len(payload["text"].encode("utf-8", errors="replace"))
    return len(
        json.dumps(
            {"event": event, "payload": payload},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="replace")
    )


def _verified_process_group_exists(proc: subprocess.Popen, pgid: int | None) -> bool:
    """Check a PGID that was verified as the spawned leader's immediately after spawn."""
    if pgid is None or pgid <= 0 or pgid != proc.pid:
        return False
    if proc.poll() is None:
        try:
            return os.getpgid(proc.pid) == pgid
        except ProcessLookupError:
            pass
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                return False
    try:
        os.killpg(pgid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False


def _owned_group_exists(term: TerminalSession) -> bool:
    """Check the immutable process group that was verified immediately after spawn."""
    if not term.owned_pgid_verified:
        return False
    return _verified_process_group_exists(term.proc, term.pgid)


def _signal_verified_process_group(
    proc: subprocess.Popen,
    pgid: int | None,
    sig: int,
) -> bool:
    if not _verified_process_group_exists(proc, pgid):
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except (OSError, ProcessLookupError):
        return False


def _signal_owned_group(term: TerminalSession, sig: int) -> bool:
    """Signal only the runner-owned group verified immediately after spawn."""
    if not term.owned_pgid_verified:
        return False
    return _signal_verified_process_group(term.proc, term.pgid, sig)


def _wait_for_verified_process_group_exit(
    proc: subprocess.Popen,
    pgid: int | None,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while _verified_process_group_exists(proc, pgid):
        _reap_terminal_descendants(pgid or proc.pid)
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


_TERMINALS: dict[str, TerminalSession] = {}
_LOCK = threading.RLock()
_MANAGED_START_RESERVATIONS = 0
_MANAGED_RESERVED_HANDLES: set[str] = set()
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

# Idle-terminal reaper: proactively close terminals whose viewers have all gone
# away, instead of leaving an abandoned shell running until the cap evicts it.
# A terminal unwatched (zero attached output streams) for longer than the grace
# is closed; the grace spans a tab refresh / brief network drop so a real
# reconnect keeps the session. Dead-process terminals are swept too as a
# belt-and-suspenders for the reader-loop retire.
_TERMINAL_IDLE_GRACE_SECONDS = 900  # 15 min unwatched -> reap
_TERMINAL_REAPER_INTERVAL_SECONDS = 60
_terminal_reaper_started = False
_terminal_reaper_lock = threading.Lock()
_terminal_reaper_thread: threading.Thread | None = None
_terminal_reaper_stop = threading.Event()


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


def _safe_terminal_env(
    cwd: str,
    rows: int,
    cols: int,
    *,
    managed: bool = False,
) -> dict[str, str]:
    safe_keys = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LANGUAGE",
        "TZ",
        "TMPDIR",
        "TEMP",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
    if managed:
        safe_keys.add("HERMES_WEBUI_CLAUDE_STORES_FILE")
    env = {key: value for key, value in os.environ.items() if key in safe_keys}
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
    if managed:
        env["HERMES_WEBUI_MANAGED_TERMINAL"] = "claude_code"
    return env


def _spawn_pty_process(
    *,
    argv: tuple[str, ...] | list[str],
    cwd: str,
    env: dict[str, str],
    slave_fd: int,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.Popen:
    kwargs = {
        "args": argv,
        "cwd": cwd,
        "env": env,
        "stdin": slave_fd,
        "stdout": slave_fd,
        "stderr": slave_fd,
        "close_fds": True,
        "start_new_session": True,
    }
    if pass_fds:
        kwargs["pass_fds"] = pass_fds
    request = _SpawnRequest(kwargs)
    _ensure_spawn_supervisor()
    _spawn_queue.put(request)
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
    if request.proc is None:
        raise RuntimeError("terminal spawn failed without process")
    return request.proc


def _read_managed_runner_readiness(fd: int) -> str:
    deadline = time.monotonic() + _MANAGED_RUNNER_READINESS_TIMEOUT_SECONDS
    payload = bytearray()
    try:
        while b"\n" not in payload:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "ownership_unknown"
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                return "ownership_unknown"
            chunk = os.read(fd, _MANAGED_RUNNER_READINESS_MAX_BYTES + 1 - len(payload))
            if not chunk:
                return "ownership_unknown"
            payload.extend(chunk)
            if len(payload) > _MANAGED_RUNNER_READINESS_MAX_BYTES:
                return "ownership_unknown"
        line, remainder = bytes(payload).split(b"\n", 1)
        if remainder:
            return "ownership_unknown"
        record = json.loads(line.decode("utf-8"))
        if not isinstance(record, dict) or set(record) != {"state"}:
            return "ownership_unknown"
        state = record["state"]
        if state == "ownership_conflict":
            return "active_elsewhere"
        if state in {
            "ready",
            "active_elsewhere",
            "invalid_session",
            "ownership_unknown",
        }:
            return state
        return "ownership_unknown"
    except (OSError, UnicodeError, ValueError):
        return "ownership_unknown"
    finally:
        _safe_close_fd(fd)


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
            if term.kind != "claude_code" and term.proc.poll() is not None:
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
        _reap_terminal_descendants(term.pgid or term.proc.pid)
        term.put_output("terminal_closed", {"exit_code": code})
        # The shell has exited (or its pty broke): retire the session so its
        # master fd and _TERMINALS entry are released. Previously only an
        # explicit close_terminal() / restart / atexit did this, so a shell that
        # exited on its own (user typed `exit`, process died) leaked its master
        # fd and dict entry for the rest of the WebUI's uptime. ``expected=term``
        # makes this a no-op if a restart already replaced the entry with a new
        # terminal for the same session id.
        _close_registered_terminal(term.session_id, expected=term, allow_managed=True)


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
    if term.kind == "claude_code":
        _signal_owned_group(term, signal.SIGWINCH)
    else:
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
            generic = [
                (sid, term)
                for sid, term in _TERMINALS.items()
                if term.kind != "claude_code"
            ]
            if len(generic) < _MAX_TERMINALS:
                return
            candidates = [
                (sid, term) for sid, term in generic if sid != exclude_sid
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


def _terminals_to_reap(now: float) -> list[tuple[str, TerminalSession]]:
    """Return (sid, term) pairs the reaper should close: a dead process, or a
    terminal unwatched for longer than the idle grace. Pure/snapshotted under
    the lock so it can be unit-tested without threads."""
    victims = []
    with _LOCK:
        for sid, term in _TERMINALS.items():
            if not term.is_alive():
                victims.append((sid, term))
                continue
            unwatched = term.unwatched_since
            if unwatched is not None and _terminal_idle_expired(term, now):
                victims.append((sid, term))
    return victims


def _terminal_idle_expired(term: TerminalSession, now: float) -> bool:
    if term.unwatched_since is None:
        return False
    if term.kind == "claude_code" and term.persistent_when_unwatched:
        with term._activity_lock:
            idle_since = max(term.unwatched_since, term.last_activity)
        return (now - idle_since) >= _MANAGED_TERMINAL_IDLE_SECONDS
    return (now - term.unwatched_since) >= _TERMINAL_IDLE_GRACE_SECONDS


def _claim_reap_victim(sid: str, term: TerminalSession, now: float) -> TerminalSession | None:
    """Atomically remove *term* from the registry, or refuse.

    ``_terminals_to_reap`` snapshots victims and then releases ``_LOCK``, so by
    the time we get here a viewer may have reconnected: ``subscribe()`` appends
    to ``_subscribers`` and clears ``unwatched_since`` under the terminal's
    ``_sub_lock``, which the selection pass never held. Object identity alone —
    what ``close_terminal(expected=…)`` checks — is still true in that case, so
    the reaper would kill a terminal that now has a live viewer.

    The claim therefore re-establishes the *whole* selection predicate while
    holding both locks, and takes the entry out of ``_TERMINALS`` in the same
    critical section. A concurrent ``attach_terminal()`` acquires the same two
    locks in the same order, so exactly one of the two wins: either the viewer
    is attached (and we refuse) or the entry is already gone (and the attach
    reports "not running").

    Returns the claimed terminal — the caller owns its teardown — or ``None``.
    """
    with _LOCK:
        if _TERMINALS.get(sid) is not term:
            return None
        # A dead process is reaped unconditionally: it cannot come back to life,
        # and an attached viewer only means someone is watching a corpse.
        if term.is_alive():
            with term._sub_lock:
                if term._subscribers:
                    return None
                with term._activity_lock:
                    if term.unwatched_since is None:
                        return None
                    if term.kind == "claude_code" and term.persistent_when_unwatched:
                        idle_since = max(term.unwatched_since, term.last_activity)
                        if (now - idle_since) < _MANAGED_TERMINAL_IDLE_SECONDS:
                            return None
                    elif (now - term.unwatched_since) < _TERMINAL_IDLE_GRACE_SECONDS:
                        return None
        del _TERMINALS[sid]
    return term


def _reap_idle_terminals(now: float) -> int:
    """Close every terminal selected by ``_terminals_to_reap`` that is *still*
    idle when claimed. Returns the count closed."""
    reaped = 0
    for sid, term in _terminals_to_reap(now):
        claimed = _claim_reap_victim(sid, term, now)
        if claimed is None:
            continue
        # Process/fd teardown runs after both locks are released: killpg + wait
        # can take seconds and must not block attaches or spawns.
        _teardown_terminal(claimed)
        reaped += 1
    return reaped


def _terminal_reaper_loop() -> None:
    while not _terminal_reaper_stop.wait(_TERMINAL_REAPER_INTERVAL_SECONDS):
        try:
            # Wall-clock, consistent with unwatched_since / last_activity.
            _reap_idle_terminals(time.time())
        except Exception:
            # Never let a transient error kill the reaper thread.
            pass


def _ensure_terminal_reaper() -> None:
    global _terminal_reaper_started, _terminal_reaper_thread
    if not _TERMINAL_SUPPORTED:
        return
    with _terminal_reaper_lock:
        if (
            _terminal_reaper_started
            and _terminal_reaper_thread is not None
            and getattr(_terminal_reaper_thread, "is_alive", lambda: False)()
        ):
            return
        thread = threading.Thread(target=_terminal_reaper_loop, daemon=True)
        thread.start()
        _terminal_reaper_thread = thread
        _terminal_reaper_started = True


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
        if current and current.kind == "claude_code":
            raise KeyError("terminal not running")
        if current and current.is_alive() and not restart and current.workspace == cwd:
            _set_size(current, rows, cols)
            return current
        if current:
            close_terminal(sid)

        master_fd, slave_fd = os.openpty()
        # Build a safe env: allowlist common shell vars, strip API keys and secrets.
        # The PTY shell is an interactive UI surface — do not leak server credentials.
        env = _safe_terminal_env(cwd, rows, cols)
        shell = _shell_path()
        # Keep the shell in its own process group for explicit cleanup via
        # close_terminal()/close_all_terminals(); do not use PDEATHSIG here.
        _ensure_terminal_reaper()
        try:
            proc = _spawn_pty_process(
                argv=_shell_argv(shell),
                cwd=cwd,
                env=env,
                slave_fd=slave_fd,
            )
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
            argv=tuple(_shell_argv(shell)),
            pgid=proc.pid,
        )
        _set_size(term, rows, cols)
        term.reader = threading.Thread(target=_reader_loop, args=(term,), daemon=True)
        term.reader.start()
        _TERMINALS[sid] = term
        return term


def start_managed_terminal(
    public_session_id: str,
    workspace: Path,
    rows: int = 24,
    cols: int = 80,
) -> TerminalSession:
    """Start the fixed Task 2 runner under a persistent managed PTY."""
    global _MANAGED_START_RESERVATIONS
    if not _TERMINAL_SUPPORTED:
        raise NotImplementedError("Managed terminals are not supported on Windows")
    public_id = str(public_session_id or "").strip()
    if not public_id:
        raise ValueError("public_session_id is required")
    cwd = str(Path(workspace).expanduser().resolve())
    if not Path(cwd).is_dir():
        raise ValueError("workspace is not a directory")

    with _LOCK:
        managed_count = sum(
            term.kind == "claude_code" and term.is_alive()
            for term in _TERMINALS.values()
        )
        if managed_count + _MANAGED_START_RESERVATIONS >= _MANAGED_TERMINAL_MAX:
            raise ManagedTerminalLimitError("managed terminal limit")
        handle = secrets.token_urlsafe(32)
        while handle in _TERMINALS or handle in _MANAGED_RESERVED_HANDLES:
            handle = secrets.token_urlsafe(32)
        _MANAGED_RESERVED_HANDLES.add(handle)
        _MANAGED_START_RESERVATIONS += 1

    generation = str(uuid4())
    master_fd = slave_fd = readiness_read_fd = readiness_write_fd = -1
    proc = None
    term = None
    actual_pgid = None
    rolled_back = False
    published = False
    try:
        master_fd, slave_fd = os.openpty()
        readiness_read_fd, readiness_write_fd = os.pipe()
        runner_path = Path(__file__).with_name("claude_code_runner.py").resolve()
        argv = (
            sys.executable,
            str(runner_path),
            public_id,
            str(readiness_write_fd),
        )
        env = _safe_terminal_env(cwd, rows, cols, managed=True)
        proc = _spawn_pty_process(
            argv=argv,
            cwd=cwd,
            env=env,
            slave_fd=slave_fd,
            pass_fds=(readiness_write_fd,),
        )
        _safe_close_fd(slave_fd)
        slave_fd = -1
        _safe_close_fd(readiness_write_fd)
        readiness_write_fd = -1

        try:
            actual_pgid = os.getpgid(proc.pid)
        except OSError:
            actual_pgid = None
        if actual_pgid != proc.pid:
            raise ManagedTerminalStartError("ownership_unknown")

        term = TerminalSession(
            session_id=handle,
            workspace=cwd,
            proc=proc,
            master_fd=master_fd,
            rows=rows,
            cols=cols,
            kind="claude_code",
            generation=generation,
            handle=handle,
            argv=argv,
            pgid=actual_pgid,
            persistent_when_unwatched=True,
            runner_owns_lease=True,
            lease_owner_pid=proc.pid,
            owned_pgid_verified=True,
            _backlog=collections.deque(),
        )
        _set_nonblocking(master_fd)
        try:
            state = _read_managed_runner_readiness(readiness_read_fd)
        finally:
            _safe_close_fd(readiness_read_fd)
            readiness_read_fd = -1
        if state != "ready":
            raise ManagedTerminalStartError(state)

        _ensure_terminal_reaper()
        _set_size(term, rows, cols)
        term.reader = threading.Thread(target=_reader_loop, args=(term,), daemon=True)
        term.reader.start()
        term.reader_started = True
        with _LOCK:
            if term.closed.is_set():
                raise ManagedTerminalStartError("ownership_unknown")
            _TERMINALS[handle] = term
            published = True
        return term
    except BaseException:
        if term is not None:
            _teardown_managed_terminal(term)
            rolled_back = True
        elif proc is not None and actual_pgid == proc.pid:
            _teardown_verified_managed_spawn(proc, actual_pgid, master_fd)
            rolled_back = True
        elif proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except (AttributeError, OSError, subprocess.TimeoutExpired):
                pass
        raise
    finally:
        if not rolled_back and not published:
            _safe_close_fd(master_fd)
        _safe_close_fd(slave_fd)
        _safe_close_fd(readiness_read_fd)
        _safe_close_fd(readiness_write_fd)
        with _LOCK:
            _MANAGED_RESERVED_HANDLES.discard(handle)
            _MANAGED_START_RESERVATIONS -= 1


def _authorised_managed_terminal(
    *,
    handle: str,
    generation: str,
    capability: str,
    operation: str,
    allow_generation_mismatch: bool = False,
) -> TerminalSession:
    now = time.monotonic()
    digest = hashlib.sha256(str(capability or "").encode("utf-8")).hexdigest()
    with _LOCK:
        term = _TERMINALS.get(str(handle or ""))
        if (
            term is None
            or term.kind != "claude_code"
            or term.handle != handle
            or (not allow_generation_mismatch and term.generation != generation)
            or not term.is_alive()
        ):
            raise KeyError("terminal not found")
        record = term._capabilities.get(digest)
        if record is None:
            raise KeyError("terminal not found")
        record_operation, record_generation, expires_at = record
        if expires_at <= now:
            term._capabilities.pop(digest, None)
            raise KeyError("terminal not found")
        if record_operation != operation or record_generation != term.generation:
            raise KeyError("terminal not found")
        return term


def issue_terminal_capability(
    handle: str,
    generation: str,
    operation: str,
) -> str:
    if operation not in _MANAGED_CAPABILITY_OPERATIONS:
        raise ValueError("invalid terminal capability operation")
    now = time.monotonic()
    with _LOCK:
        term = _TERMINALS.get(str(handle or ""))
        if (
            term is None
            or term.kind != "claude_code"
            or term.handle != handle
            or term.generation != generation
            or not term.is_alive()
        ):
            raise KeyError("terminal not found")
        term._capabilities = {
            digest: record
            for digest, record in term._capabilities.items()
            if record[2] > now
        }
        capability = secrets.token_urlsafe(32)
        digest = hashlib.sha256(capability.encode("utf-8")).hexdigest()
        term._capabilities[digest] = (
            operation,
            generation,
            now + _MANAGED_CAPABILITY_TTL_SECONDS,
        )
        return capability


def attach_managed_terminal(
    *,
    handle: str,
    generation: str,
    capability: str,
    after_seq: int | None = None,
) -> tuple[TerminalSession, queue.Queue]:
    with _LOCK:
        term = _authorised_managed_terminal(
            handle=handle,
            generation=generation,
            capability=capability,
            operation="stream",
            allow_generation_mismatch=True,
        )
        return term, term.subscribe(after_seq=after_seq, generation=generation)


def write_managed_terminal(
    *,
    handle: str,
    generation: str,
    capability: str,
    data: str,
) -> None:
    term = _authorised_managed_terminal(
        handle=handle,
        generation=generation,
        capability=capability,
        operation="input",
    )
    with term._activity_lock:
        with term.io_lock:
            if term.closed.is_set():
                raise KeyError("terminal not found")
            os.write(
                term.master_fd,
                str(data or "").encode("utf-8", errors="replace"),
            )
        term.last_activity = time.time()
        term._activity_epoch += 1


def resize_managed_terminal(
    *,
    handle: str,
    generation: str,
    capability: str,
    rows: int,
    cols: int,
) -> None:
    term = _authorised_managed_terminal(
        handle=handle,
        generation=generation,
        capability=capability,
        operation="input",
    )
    _set_size(term, rows, cols)


def stop_managed_terminal(
    *,
    handle: str,
    generation: str,
    capability: str,
) -> bool:
    with _LOCK:
        term = _authorised_managed_terminal(
            handle=handle,
            generation=generation,
            capability=capability,
            operation="stop",
        )
        if _TERMINALS.get(handle) is not term:
            raise KeyError("terminal not found")
        del _TERMINALS[handle]
    _teardown_terminal(term)
    return True


def get_terminal(session_id: str) -> TerminalSession | None:
    if not _TERMINAL_SUPPORTED:
        return None
    with _LOCK:
        term = _TERMINALS.get(str(session_id or ""))
        if term and term.kind == "claude_code":
            return None
        if term and term.is_alive():
            return term
        return term


def attach_terminal(
    session_id: str, after_seq: int | None = None
) -> tuple[TerminalSession, queue.Queue] | None:
    """Attach a viewer atomically against the idle reaper.

    ``get_terminal()`` followed by ``term.subscribe()`` leaves a window: the
    reaper can claim and tear the terminal down in between, so the viewer ends
    up subscribed to a corpse and the caller reports a live stream that will
    never produce output. Doing the lookup and the subscribe inside the same
    ``_LOCK`` section — the same lock, in the same order, that
    ``_claim_reap_victim`` takes — makes the two mutually exclusive:

    * attach wins → ``_subscribers`` is non-empty and ``unwatched_since`` is
      ``None`` before the reaper can revalidate, so the reap is refused;
    * reap wins → the entry is already out of ``_TERMINALS``, so this returns
      ``None`` and the route answers "terminal not running" instead of hanging.

    Registration is the authority, deliberately: both the reaper and
    ``close_terminal()`` remove the entry *before* tearing the terminal down, so
    "still in ``_TERMINALS``" is exactly the condition that cannot race. A
    terminal that is registered but already flagged ``closed`` (its shell exited
    and the reader loop has not retired it yet) still attaches, so the viewer
    receives the ``terminal_closed`` event instead of a bare 404.

    Returns ``(term, queue)`` or ``None``.
    """
    if not _TERMINAL_SUPPORTED:
        return None
    sid = str(session_id or "")
    with _LOCK:
        term = _TERMINALS.get(sid)
        if term is None or term.kind == "claude_code":
            return None
        return term, term.subscribe(after_seq=after_seq)


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
    return _close_registered_terminal(
        session_id,
        expected=expected,
        allow_managed=False,
    )


def _close_registered_terminal(
    session_id: str,
    *,
    expected: TerminalSession | None = None,
    allow_managed: bool,
) -> bool:
    if not _TERMINAL_SUPPORTED:
        return False
    sid = str(session_id or "")
    with _LOCK:
        term = _TERMINALS.get(sid)
        if expected is not None and term is not expected:
            return False
        if term is None or (
            getattr(term, "kind", "shell") == "claude_code" and not allow_managed
        ):
            return False
        del _TERMINALS[sid]
    _teardown_terminal(term)
    return True


def _teardown_terminal(term: TerminalSession) -> None:
    """Kill the shell, close the pty master fd and reap descendants.

    Split out of ``close_terminal`` so a caller that has already claimed the
    registry entry (the idle reaper) can run the teardown without re-entering
    the registry lookup — and, importantly, without holding any lock while
    ``killpg``/``wait`` block for up to ~2.5s.
    """
    if term.kind == "claude_code":
        _teardown_managed_terminal(term)
        return
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


def _teardown_managed_terminal(term: TerminalSession) -> None:
    """Stop only the verified runner-owned group, then drain and close its PTY."""
    if term.owned_pgid_verified:
        _stop_verified_managed_group(term.proc, term.pgid)

    reader = term.reader
    if (
        reader is not None
        and term.reader_started
        and reader is not threading.current_thread()
    ):
        try:
            reader.join(timeout=1.0)
        except RuntimeError:
            pass
    elif not term.reader_started:
        _drain_managed_pty_fd(term.master_fd)

    term.closed.set()
    with term.io_lock:
        _safe_close_fd(term.master_fd)
    _reap_terminal_descendants(term.pgid or term.proc.pid)


def _stop_verified_managed_group(
    proc: subprocess.Popen,
    pgid: int | None,
) -> None:
    escalation = (
        (signal.SIGHUP, 0.75),
        (signal.SIGTERM, 0.75),
        (signal.SIGKILL, 1.0),
    )
    for sig, timeout in escalation:
        if not _signal_verified_process_group(proc, pgid, sig):
            break
        if _wait_for_verified_process_group_exit(proc, pgid, timeout):
            break

    try:
        proc.wait(timeout=0)
    except (AttributeError, OSError, subprocess.TimeoutExpired, ProcessLookupError):
        pass
    _reap_terminal_descendants(pgid or proc.pid)


def _drain_managed_pty_fd(fd: int) -> None:
    remaining = _MANAGED_OUTPUT_BACKLOG_BYTES
    while fd >= 0 and remaining > 0:
        try:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                return
            data = os.read(fd, min(8192, remaining))
        except ValueError:
            return
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                return
            return
        if not data:
            return
        remaining -= len(data)


def _teardown_verified_managed_spawn(
    proc: subprocess.Popen,
    pgid: int,
    master_fd: int,
) -> None:
    """Roll back a verified spawn before a TerminalSession can own it."""
    try:
        _stop_verified_managed_group(proc, pgid)
        _drain_managed_pty_fd(master_fd)
    finally:
        _safe_close_fd(master_fd)
        _reap_terminal_descendants(pgid)


def close_all_terminals() -> None:
    """Best-effort reap of embedded shells during graceful WebUI shutdown."""
    with _LOCK:
        session_ids = list(_TERMINALS)
    for session_id in session_ids:
        with _LOCK:
            term = _TERMINALS.get(session_id)
        if getattr(term, "kind", "shell") == "claude_code":
            _close_registered_terminal(session_id, allow_managed=True)
        else:
            close_terminal(session_id)


atexit.register(close_all_terminals)
