"""Regression tests for bootstrap port-availability preflight (issue #8111)."""

from __future__ import annotations

import contextlib
import errno
import http.server
import shutil
import socket
import ssl
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap  # noqa: E402


class _StubSocket:
    """Controlled socket so bind outcomes never depend on machine settings."""

    instances: list[_StubSocket] = []

    def __init__(
        self,
        family: int = socket.AF_INET,
        sock_type: int = socket.SOCK_STREAM,
        *args: object,
        **kwargs: object,
    ) -> None:
        self.family = family
        self.sock_type = sock_type
        self.bind_calls: list[tuple] = []
        self.opts: list[tuple] = []
        self.closed = False
        _StubSocket.instances.append(self)

    def setsockopt(self, *args: object, **kwargs: object) -> None:
        self.opts.append(tuple(args))

    def bind(self, address: tuple) -> None:
        self.bind_calls.append(address)

    def close(self) -> None:
        self.closed = True


class _BusySocket(_StubSocket):
    """bind() reports the port as taken, whatever the host allows."""

    def bind(self, address: tuple) -> None:
        self.bind_calls.append(address)
        raise OSError(errno.EADDRINUSE, "Address already in use")


class _RefusingSocket(_StubSocket):
    """bind() fails like a non-local or otherwise invalid address."""

    def bind(self, address: tuple) -> None:
        self.bind_calls.append(address)
        raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")


def _occupy(host: str = "127.0.0.1") -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def test_available_port_passes() -> None:
    sock, port = _occupy()
    sock.close()
    # Port just released; preflight should not raise.
    bootstrap._check_port_available("127.0.0.1", port)


def test_occupied_port_raises_with_suggestion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.socket, "socket", _BusySocket)
    monkeypatch.setattr(
        bootstrap, "_port_is_available", lambda host, candidate: candidate == 9101
    )
    with pytest.raises(RuntimeError) as excinfo:
        bootstrap._check_port_available("127.0.0.1", 9099)
    message = str(excinfo.value)
    assert "Port 9099 on 127.0.0.1 is already in use" in message
    assert "./start.sh 9101" in message
    assert "HERMES_WEBUI_PORT=9101" in message
    assert "no configuration was changed" in message


def test_no_free_alternative_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.socket, "socket", _BusySocket)
    monkeypatch.setattr(bootstrap, "_port_is_available", lambda host, candidate: False)
    with pytest.raises(RuntimeError) as excinfo:
        bootstrap._check_port_available("127.0.0.1", 9099)
    assert "No free alternative port" in str(excinfo.value)


def test_find_free_port_skips_taken_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    taken = {9000, 9001}
    monkeypatch.setattr(
        bootstrap, "_port_is_available", lambda host, candidate: candidate not in taken
    )
    assert bootstrap._find_free_port("127.0.0.1", 8999) == 9002


def test_wildcard_host_kept_for_bind_check() -> None:
    assert bootstrap._bind_host_for_check("") == "0.0.0.0"
    assert bootstrap._bind_host_for_check("0.0.0.0") == "0.0.0.0"
    assert bootstrap._bind_host_for_check("::") == "::"
    assert bootstrap._bind_host_for_check("[::]") == "::"
    assert bootstrap._bind_host_for_check("192.168.1.5") == "192.168.1.5"


@pytest.mark.parametrize("host", ["", "0.0.0.0"])
def test_ipv4_wildcard_check_binds_wildcard_address(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    _StubSocket.instances = []
    monkeypatch.setattr(bootstrap.socket, "socket", _StubSocket)
    bootstrap._check_port_available(host, 8787)
    stub = _StubSocket.instances[-1]
    assert stub.family == socket.AF_INET
    assert stub.bind_calls == [("0.0.0.0", 8787)]


@pytest.mark.parametrize("host", ["::", "[::]"])
def test_ipv6_wildcard_check_binds_wildcard_address(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    _StubSocket.instances = []
    monkeypatch.setattr(bootstrap.socket, "socket", _StubSocket)
    bootstrap._check_port_available(host, 8787)
    stub = _StubSocket.instances[-1]
    assert stub.family == socket.AF_INET6
    assert stub.bind_calls == [("::", 8787)]


def test_specific_host_checked_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    _StubSocket.instances = []
    monkeypatch.setattr(bootstrap.socket, "socket", _StubSocket)
    bootstrap._check_port_available("192.168.1.5", 8787)
    assert _StubSocket.instances[-1].bind_calls == [("192.168.1.5", 8787)]


def test_invalid_host_reports_bind_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.socket, "socket", _RefusingSocket)
    with pytest.raises(RuntimeError) as excinfo:
        bootstrap._check_port_available("203.0.113.1", 8787)
    assert "Cannot bind 203.0.113.1:8787" in str(excinfo.value)


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="bind semantics for reused/overlapping addresses differ by platform",
)
def test_real_occupied_loopback_port_raises() -> None:
    sock, port = _occupy("127.0.0.1")
    try:
        with pytest.raises(RuntimeError) as excinfo:
            bootstrap._check_port_available("127.0.0.1", port)
        assert "already in use" in str(excinfo.value)
    finally:
        sock.close()


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="bind semantics for reused/overlapping addresses differ by platform",
)
def test_real_wildcard_check_catches_interface_only_listener() -> None:
    sock, port = _occupy("127.0.0.1")
    try:
        with pytest.raises(RuntimeError) as excinfo:
            bootstrap._check_port_available("0.0.0.0", port)
        assert "already in use" in str(excinfo.value)
    finally:
        sock.close()


# ---------- review follow-up (#8112): an occupied port may be our own WebUI --


def _raise_in_use(*_args: object, **_kwargs: object) -> None:
    raise bootstrap.PortInUseError(
        "Port 8787 on 127.0.0.1 is already in use by another service."
    )


def _stub_main_up_to_preflight(monkeypatch: pytest.MonkeyPatch, argv: list) -> None:
    """Stub what main() touches before the preflight; pin argv and supervisor env."""
    monkeypatch.setattr(bootstrap, "ensure_supported_platform", lambda: None)
    monkeypatch.setattr(bootstrap, "open_browser", lambda url: None)
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"] + argv)
    for name in (
        "INVOCATION_ID",
        "JOURNAL_STREAM",
        "NOTIFY_SOCKET",
        "XPC_SERVICE_NAME",
        "SUPERVISOR_ENABLED",
        "HERMES_WEBUI_FOREGROUND",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("host", "probe_url"),
    [
        ("", "http://127.0.0.1:8787/health"),
        ("0.0.0.0", "http://127.0.0.1:8787/health"),
        ("127.0.0.1", "http://127.0.0.1:8787/health"),
        ("::", "http://[::1]:8787/health"),
        ("[::]", "http://[::1]:8787/health"),
        ("localhost", "http://localhost:8787/health"),
    ],
)
def test_already_serving_scheme_probes_the_configured_address(
    monkeypatch: pytest.MonkeyPatch, host: str, probe_url: str
) -> None:
    """The probe keeps the configured address, not the browser's localhost.

    ``localhost`` is what the browser URL needs (session + passkey rpId), but
    with ``HTTP_PROXY`` set and ``NO_PROXY=127.0.0.1`` only the numeric address
    bypasses the proxy, so a health check on ``localhost`` would be answered by
    the proxy instead of the running WebUI (review #8112).
    """
    seen: list = []

    def fake_wait(url: str, timeout: float = 0.0, **kwargs: object) -> str:
        assert kwargs.get("markers") == bootstrap._HERMES_HEALTH_MARKERS
        seen.append((url, timeout))
        return "http"

    monkeypatch.setattr(bootstrap, "wait_for_health", fake_wait)
    assert bootstrap._already_serving_scheme(host, 8787) == "http"
    assert seen == [(probe_url, 1.0)]


@pytest.mark.parametrize("host", ["::", "[::]"])
def test_already_serving_scheme_wildcard_ipv6_also_finds_an_ipv4_own_instance(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """Senior gate on #8112: on dual-stack Linux, binding ``::`` conflicts with
    an own WebUI on 127.0.0.1:8787 that [::1] never reaches. Probing only [::1]
    reported a foreign conflict and advised a second instance on the same state
    dir; the IPv4 loopback probe finds the running WebUI instead."""
    seen: list = []

    def fake_wait(url: str, timeout: float = 0.0, **kwargs: object) -> str:
        seen.append(url)
        return "http" if url.startswith("http://127.0.0.1:") else ""

    monkeypatch.setattr(bootstrap, "wait_for_health", fake_wait)
    assert bootstrap._already_serving_scheme(host, 8787) == "http"
    assert seen == ["http://[::1]:8787/health", "http://127.0.0.1:8787/health"]


def test_already_serving_scheme_ipv4_host_does_not_probe_ipv6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list = []

    def fake_wait(url: str, timeout: float = 0.0, **kwargs: object) -> str:
        seen.append(url)
        return ""

    monkeypatch.setattr(bootstrap, "wait_for_health", fake_wait)
    assert bootstrap._already_serving_scheme("0.0.0.0", 8787) == ""
    assert seen == ["http://127.0.0.1:8787/health"]


def test_probe_host_keeps_the_configured_address() -> None:
    """``_probe_host`` must not collapse into ``_url_host``.

    The browser origin stays ``localhost`` (session in localStorage, passkey
    rpId), while the probes stay on the address the user's ``NO_PROXY`` covers.
    """
    assert bootstrap._probe_host("127.0.0.1") == "127.0.0.1"
    assert bootstrap._probe_host("localhost") == "localhost"
    assert bootstrap._probe_host("") == "127.0.0.1"
    assert bootstrap._probe_host("0.0.0.0") == "127.0.0.1"
    assert bootstrap._probe_host("::") == "[::1]"
    assert bootstrap._probe_host("[::]") == "[::1]"
    assert bootstrap._probe_host("::1") == "[::1]"
    assert bootstrap._probe_host("[::1]") == "[::1]"
    assert bootstrap._probe_host("192.168.1.5") == "192.168.1.5"
    # the browser URLs are untouched by the probe fix
    assert bootstrap._url_host("127.0.0.1") == "localhost"
    assert bootstrap._url_host("::1") == "[::1]"


def test_already_serving_scheme_empty_when_nothing_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap, "wait_for_health", lambda url, timeout=0.0, **kwargs: ""
    )
    assert bootstrap._already_serving_scheme("127.0.0.1", 8787) == ""


def test_occupied_port_with_healthy_webui_reports_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _stub_main_up_to_preflight(monkeypatch, ["--no-browser"])
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    monkeypatch.setattr(bootstrap, "_already_serving_scheme", lambda host, port: "http")

    assert bootstrap.main() == 0
    assert "already running" in capsys.readouterr().out


def test_occupied_port_with_healthy_webui_opens_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_main_up_to_preflight(monkeypatch, [])
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    monkeypatch.setattr(bootstrap, "_already_serving_scheme", lambda host, port: "http")
    opened: list = []
    monkeypatch.setattr(bootstrap, "open_browser", opened.append)

    assert bootstrap.main() == 0
    assert opened == ["http://localhost:" + str(bootstrap.DEFAULT_PORT)]


def test_occupied_port_by_foreign_listener_still_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_main_up_to_preflight(monkeypatch, ["--no-browser"])
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    monkeypatch.setattr(bootstrap, "_already_serving_scheme", lambda host, port: "")

    with pytest.raises(RuntimeError, match="already in use"):
        bootstrap.main()


def test_occupied_port_in_foreground_reports_our_own_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreground launch against OUR WebUI must not advise a second one.

    "./start.sh <other port>" would start a second WebUI on the same state
    dir - the outcome #8111 exists to prevent (review #8112, should-fix).
    """
    _stub_main_up_to_preflight(monkeypatch, ["--foreground"])
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    monkeypatch.setattr(bootstrap, "_already_serving_scheme", lambda host, port: "http")

    with pytest.raises(RuntimeError) as excinfo:
        bootstrap.main()
    message = str(excinfo.value)
    assert (
        f"Hermes WebUI is already running at http://localhost:{bootstrap.DEFAULT_PORT}"
        in message
    )
    assert "Stop that instance first" in message
    assert "start.sh" not in message


def test_occupied_port_under_supervisor_reports_our_own_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A systemd launch (INVOCATION_ID) gets the same own-instance message."""
    _stub_main_up_to_preflight(monkeypatch, ["--no-browser"])
    monkeypatch.setenv("INVOCATION_ID", "3f5b1c0e2a4d4f7e9c8b1a2d3e4f5061")
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    monkeypatch.setattr(bootstrap, "_already_serving_scheme", lambda host, port: "http")

    with pytest.raises(RuntimeError) as excinfo:
        bootstrap.main()
    message = str(excinfo.value)
    assert "Hermes WebUI is already running at" in message
    assert "Stop that instance first" in message
    assert "start.sh" not in message


def test_occupied_port_in_foreground_by_foreign_listener_keeps_port_advice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign listener under a foreground launch keeps the conflict text."""
    _stub_main_up_to_preflight(monkeypatch, ["--foreground"])
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    monkeypatch.setattr(bootstrap, "_already_serving_scheme", lambda host, port: "")

    with pytest.raises(RuntimeError, match="already in use"):
        bootstrap.main()


def test_port_conflict_raises_the_dedicated_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a conflict is recoverable; main() catches just this type."""
    monkeypatch.setattr(bootstrap.socket, "socket", _BusySocket)
    monkeypatch.setattr(bootstrap, "_port_is_available", lambda host, candidate: False)
    with pytest.raises(bootstrap.PortInUseError):
        bootstrap._check_port_available("127.0.0.1", 9099)


def _stdlib_ssl_context_class() -> type:
    """The stdlib ``ssl.SSLContext``, even after ``truststore.inject_into_ssl()``.

    Hermes Agent's ``agent/ssl_verify.py`` injects truststore process-wide, and an
    earlier test in the same pytest process can import it. truststore's
    ``SSLContext.wrap_socket`` then verifies the peer chain even on this
    server-side listening socket and raises. Walk back to the stdlib class
    rather than patching global SSL state.
    """
    cls = ssl.SSLContext
    while cls.__module__ != "ssl" and cls.__bases__:
        cls = cls.__bases__[0]
    return cls


@contextlib.contextmanager
def _serve(
    body: bytes, status: int = 200, cert: str | None = None, key: str | None = None
) -> Iterator[int]:
    """A /health listener that answers ``status`` with exactly ``body``.

    Yields the port; the server runs on a background thread. ``cert``/``key``
    wrap the socket in TLS, making it an HTTPS-only listener.
    """

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    with http.server.HTTPServer(("127.0.0.1", 0), _Handler) as httpd:
        if cert and key:
            ctx = _stdlib_ssl_context_class()(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield port
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


def test_foreign_status_ok_listener_is_not_the_webui() -> None:
    """A generic status-ok body is a foreign listener, not our WebUI (#8112)."""
    with _serve(b'{"status": "ok"}') as port:
        assert bootstrap._already_serving_scheme("127.0.0.1", port) == ""


def test_webui_health_payload_still_takes_the_running_path() -> None:
    """The WebUI payload from api/routes.py::_handle_health is recognised."""
    body = (
        b'{"status": "ok", "sessions": 0, "server_started_at": 1.0, '
        b'"uptime_seconds": 1.0}'
    )
    with _serve(body) as port:
        assert bootstrap._already_serving_scheme("127.0.0.1", port) == "http"


def test_invalid_bind_address_is_not_recovered_as_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EADDRNOTAVAIL stays an error even when a probe would answer.

    With a remote --host the bind fails locally while a health probe can still
    reach the machine named there; reporting that as "already running" would
    hide a bootstrap that started nothing at all.
    """
    _stub_main_up_to_preflight(monkeypatch, ["--no-browser"])
    monkeypatch.setattr(bootstrap.socket, "socket", _RefusingSocket)
    monkeypatch.setattr(bootstrap, "_already_serving_scheme", lambda host, port: "http")

    with pytest.raises(RuntimeError, match="Cannot bind"):
        bootstrap.main()


_DEGRADED_WEBUI_BODY = (
    b'{"status": "degraded", "sessions": 1, "server_started_at": 1.0}'
)


def test_degraded_webui_payload_is_still_our_instance() -> None:
    """A degraded own WebUI answers 503 with its own payload (#8112, low)."""
    with _serve(_DEGRADED_WEBUI_BODY, status=503) as port:
        assert bootstrap._already_serving_scheme("127.0.0.1", port) == "http"


def test_degraded_foreign_answer_is_not_our_instance() -> None:
    with _serve(b'{"status": "degraded"}', status=503) as port:
        assert bootstrap._already_serving_scheme("127.0.0.1", port) == ""


def test_degraded_answer_is_not_ready_for_the_health_wait() -> None:
    """The readiness wait keeps treating 503 as not-ready."""
    with _serve(_DEGRADED_WEBUI_BODY, status=503) as port:
        url = f"http://127.0.0.1:{port}/health"
        assert not bootstrap._health_ok(url, markers=bootstrap._HERMES_HEALTH_MARKERS)
        assert bootstrap._health_ok(
            url, markers=bootstrap._HERMES_HEALTH_MARKERS, accept_degraded=True
        )


# ---------- review follow-up (#8112, second round): URL hosts ----------------


def test_url_host_maps_wildcards_and_brackets_ipv6() -> None:
    """Loopback stays localhost, so the browser origin cannot move.

    Passkeys are bound to the hostname (WebAuthn rpId) and the session
    lives in that origin's localStorage, so --host 127.0.0.1 must keep
    opening localhost.
    """
    assert bootstrap._url_host("") == "localhost"
    assert bootstrap._url_host("0.0.0.0") == "localhost"
    assert bootstrap._url_host("::") == "localhost"
    assert bootstrap._url_host("[::]") == "localhost"
    assert bootstrap._url_host("127.0.0.1") == "localhost"
    assert bootstrap._url_host("localhost") == "localhost"
    assert bootstrap._url_host("::1") == "[::1]"
    assert bootstrap._url_host("[::1]") == "[::1]"
    assert bootstrap._url_host("192.168.1.5") == "192.168.1.5"


def test_already_serving_scheme_brackets_ipv6_probe_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--host ::1` must probe http://[::1]:port/health, not http://::1:port."""
    seen: list = []

    def fake_wait(url: str, timeout: float = 0.0, **kwargs: object) -> str:
        seen.append(url)
        assert kwargs.get("tls_unknown") is True
        return "http"

    monkeypatch.setattr(bootstrap, "wait_for_health", fake_wait)
    assert bootstrap._already_serving_scheme("::1", 8787) == "http"
    assert seen == ["http://[::1]:8787/health"]


@pytest.mark.parametrize(
    "host, url_host",
    [("::1", "[::1]"), ("0.0.0.0", "localhost"), ("127.0.0.1", "localhost")],
)
def test_occupied_port_url_uses_a_reachable_host(
    monkeypatch: pytest.MonkeyPatch, host: str, url_host: str
) -> None:
    """The opened URL of a running own WebUI must be openable, not http://::1."""
    _stub_main_up_to_preflight(monkeypatch, ["--host", host])
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    monkeypatch.setattr(bootstrap, "_already_serving_scheme", lambda h, p: "https")
    opened: list = []
    monkeypatch.setattr(bootstrap, "open_browser", opened.append)

    assert bootstrap.main() == 0
    assert opened == [f"https://{url_host}:{bootstrap.DEFAULT_PORT}"]


def test_occupied_port_in_foreground_names_bracketed_ipv6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_main_up_to_preflight(monkeypatch, ["--foreground", "--host", "::1"])
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    monkeypatch.setattr(bootstrap, "_already_serving_scheme", lambda h, p: "http")

    with pytest.raises(RuntimeError) as excinfo:
        bootstrap.main()
    assert (
        f"Hermes WebUI is already running at http://[::1]:{bootstrap.DEFAULT_PORT}"
        in str(excinfo.value)
    )


# ---------- review follow-up (#8112, second round): bind flags -------------


@pytest.mark.parametrize("probe", ["_check_port_available", "_port_is_available"])
def test_windows_preflight_binds_like_the_server(
    monkeypatch: pytest.MonkeyPatch, probe: str
) -> None:
    """On Windows the check must not reuse the address (server.py's own flags).

    With SO_REUSEADDR the check binds a port another listener holds and the
    real bind fails later, after dependencies and state exist (review #8112).
    """
    _StubSocket.instances = []
    monkeypatch.setattr(bootstrap.socket, "socket", _StubSocket)
    monkeypatch.setattr(sys, "platform", "win32")
    if probe == "_check_port_available":
        bootstrap._check_port_available("127.0.0.1", 8787)
    else:
        assert bootstrap._port_is_available("127.0.0.1", 8787) is True
    opts = _StubSocket.instances[-1].opts
    assert (socket.SOL_SOCKET, socket.SO_REUSEADDR, 0) in opts
    assert (socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1) in opts


def test_posix_preflight_keeps_address_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off Windows the flags are unchanged: reuse on, no exclusive flag."""
    _StubSocket.instances = []
    monkeypatch.setattr(bootstrap.socket, "socket", _StubSocket)
    monkeypatch.setattr(sys, "platform", "linux")
    bootstrap._check_port_available("127.0.0.1", 8787)
    opts = _StubSocket.instances[-1].opts
    assert (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in opts
    assert not any(o[1] == getattr(socket, "SO_EXCLUSIVEADDRUSE", -5) for o in opts)


# ---------- review follow-up (#8112, second round): HTTPS-only WebUI --------

_WEBUI_HEALTH_BODY = (
    b'{"status": "ok", "sessions": 0, "server_started_at": 1.0, '
    b'"uptime_seconds": 1.0}'
)


def _self_signed_cert(tmp_path: Path) -> tuple[str, str]:
    # The certificate comes from the openssl CLI; without it (a stock Windows
    # dev box) skip rather than fail with FileNotFoundError (Greptile on #8133).
    if shutil.which("openssl") is None:
        pytest.skip("openssl CLI not on PATH")
    cert = str(tmp_path / "cert.pem")
    key = str(tmp_path / "key.pem")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key,
            "-out", cert, "-days", "1", "-nodes", "-subj", "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def test_https_only_webui_is_recognised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An own WebUI serving HTTPS is ours even with no TLS env in this shell.

    The running instance may have taken its TLS settings from another launcher
    or an earlier shell; probing HTTP alone reported the healthy instance as a
    foreign conflict (review #8112, fix 3).
    """
    cert, key = _self_signed_cert(tmp_path)
    monkeypatch.delenv("HERMES_WEBUI_TLS_CERT", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TLS_KEY", raising=False)
    with _serve(_WEBUI_HEALTH_BODY, cert=cert, key=key) as port:
        assert bootstrap._already_serving_scheme("127.0.0.1", port) == "https"
