"""
Regression tests for #2476 — gateway SSE reads no longer pin the worker on a
half-open connection and no longer ignore "Stop".

Both gateway SSE read loops used ``urllib.request.urlopen(req, timeout=600)`` and
checked ``cancel_event`` only BETWEEN lines. A half-open gateway (TCP open, zero
bytes) blocked each read for the full 600s, so the worker thread was pinned for
10 minutes and a user's Stop was ignored until then.

The socket now carries a bounded byte-silence budget (``_gateway_read_timeout_secs``,
default 600s, env-tunable) and iteration goes through ``_iter_sse_lines_cancellable``.
A read timeout is TERMINAL: CPython's ``socket.makefile`` latches
``_timeout_occurred`` on the first ``socket.timeout``, so every subsequent read
raises a bare ``OSError`` and the connection cannot be resumed. The generator
therefore treats any read timeout / poisoned-socket error as terminal — surfacing
the user's Stop (yield ``b""``) if pressed, else re-raising so the turn tears down.

``_FakeResp`` models that latch (a ``"TO"`` raises ``socket.timeout`` once, then the
object is poisoned and every further read raises ``OSError``) so the tests reflect
real socket semantics rather than a stateless re-raise.
"""
import socket
import threading

from api.gateway_chat import (
    _GATEWAY_READ_TIMEOUT_DEFAULT,
    _gateway_read_timeout_secs,
    _iter_sse_lines_cancellable,
)


class _FakeResp:
    """Iteration replays a script (matching a real HTTPResponse and the other
    gateway-chat test fakes, which are iterated, not ``readline()``-d).

    A bytes item -> a line. A ``"TO"`` item raises ``socket.timeout`` ONCE and
    then POISONS the object: every subsequent read raises a bare ``OSError``
    ("cannot read from timed out object"), exactly like CPython's ``SocketIO``
    after a read timeout. Exhausted -> EOF (``StopIteration``).
    """

    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self._poisoned = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._poisoned:
            raise OSError("cannot read from timed out object")
        if self._i >= len(self._script):
            raise StopIteration
        item = self._script[self._i]
        self._i += 1
        if item == "TO":
            self._poisoned = True
            raise socket.timeout("read timed out")
        return item


def _drive(script, *, cancel_set=False):
    """Simulate the real SSE loop body: cancel-first, collect non-empty lines."""
    ev = threading.Event()
    if cancel_set:
        ev.set()
    lines = []
    errored = False
    try:
        for raw in _iter_sse_lines_cancellable(_FakeResp(script), ev):
            if ev.is_set():
                lines.append("<CANCEL>")
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            lines.append(line)
    except OSError:  # socket.timeout is an OSError subclass
        errored = True
    return lines, errored


def test_normal_stream_delivers_all_lines_without_abort():
    lines, errored = _drive([b"data: a\n", b"data: b\n", b"data: c\n"])
    assert lines == ["data: a", "data: b", "data: c"]
    assert not errored


def test_stall_with_cancel_is_honored_on_first_timeout():
    """A stalled gateway must not ignore Stop until 600s — cancel fires on the
    first timeout window instead of pinning the worker."""
    lines, errored = _drive(["TO", "TO", "TO"], cancel_set=True)
    assert lines == ["<CANCEL>"]
    assert not errored


def test_stall_without_cancel_aborts_terminally():
    """No cancel: the read timeout is terminal and re-raises so the turn tears
    down (surfaced as an apperror by the caller), not silently retried."""
    lines, errored = _drive(["TO", "TO", "TO"], cancel_set=False)
    assert errored
    assert lines == []


def test_prior_data_is_kept_when_a_later_read_times_out():
    lines, errored = _drive([b"data: x\n", b"data: y\n", "TO"])
    assert lines == ["data: x", "data: y"]
    assert errored


def test_timeout_is_terminal_no_recovery_after_a_silent_window():
    """The key correctness contract vs. the old (buggy) multi-window design:
    a read timeout poisons the socket, so bytes scripted AFTER a "TO" are NEVER
    delivered — one silent window ends the turn (no false 'recovery')."""
    lines, errored = _drive([b"data: x\n", "TO", b"data: never\n"])
    assert lines == ["data: x"]  # data after the timeout is unreachable
    assert errored


def test_slow_but_alive_stream_is_not_aborted():
    """The real 'slow stream' guarantee: as long as SOME byte arrives within the
    timeout window (no read ever times out), the stream is never aborted —
    regardless of total turn length."""
    lines, errored = _drive(
        [b"data: x\n", b"data: y\n", b"data: z\n", b"data: w\n"]
    )
    assert lines == ["data: x", "data: y", "data: z", "data: w"]
    assert not errored


def test_poisoned_oserror_without_cancel_propagates():
    """A bare OSError (the post-timeout poisoned read, not socket.timeout) must
    also be treated as terminal — this is the class the original except clause
    missed."""
    class _PoisonedResp:
        def __iter__(self):
            return self

        def __next__(self):
            raise OSError("cannot read from timed out object")

    ev = threading.Event()
    gen = _iter_sse_lines_cancellable(_PoisonedResp(), ev)
    try:
        next(gen)
        raised = False
    except OSError:
        raised = True
    assert raised


def test_poisoned_oserror_with_cancel_yields_cancel_then_stops():
    class _PoisonedResp:
        def __iter__(self):
            return self

        def __next__(self):
            raise OSError("cannot read from timed out object")

    ev = threading.Event()
    ev.set()
    out = list(_iter_sse_lines_cancellable(_PoisonedResp(), ev))
    assert out == [b""]  # one control frame so the caller emits its cancel event


def test_eof_ends_iteration_cleanly():
    lines, errored = _drive([b"data: only\n"])  # then EOF
    assert lines == ["data: only"]
    assert not errored


class TestReadTimeoutConfig:

    def test_default_is_600s(self, monkeypatch):
        monkeypatch.delenv("HERMES_WEBUI_GATEWAY_READ_TIMEOUT", raising=False)
        assert _gateway_read_timeout_secs() == 600.0
        assert _GATEWAY_READ_TIMEOUT_DEFAULT == 600.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_GATEWAY_READ_TIMEOUT", "300")
        assert _gateway_read_timeout_secs() == 300.0

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_GATEWAY_READ_TIMEOUT", "not-a-number")
        assert _gateway_read_timeout_secs() == 600.0

    def test_non_positive_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_GATEWAY_READ_TIMEOUT", "0")
        assert _gateway_read_timeout_secs() == 600.0

    def test_negative_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_GATEWAY_READ_TIMEOUT", "-5")
        assert _gateway_read_timeout_secs() == 600.0
