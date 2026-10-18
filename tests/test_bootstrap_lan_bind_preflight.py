"""#8137 part 2: an own WebUI bound only to a LAN/Tailscale address is ours.

An own WebUI started with ``--host 100.x.y.z`` holds the port on that
interface only. A later ``bootstrap.py --host 0.0.0.0`` conflicts on the
wildcard bind, but the preflight probed loopback only, so it reported a foreign
conflict and advised starting a SECOND WebUI on the same state dir - the exact
outcome #8111 exists to prevent.

Part 1 of #8137 (Windows ``WSAEACCES``) is PR #8301 and does not touch these
code paths; part 2 gets its own file so neither PR edits the other's tests.
"""

from __future__ import annotations

import contextlib
import http.server
import socket
import ssl
import subprocess
import shutil
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap  # noqa: E402

# The payload api/routes.py::_handle_health answers. bootstrap identifies OUR
# own server by these markers, not by a generic {"status": "ok"}, so a foreign
# listener answering the same status is still a foreign listener.
_WEBUI_BODY = (
    b'{"status": "ok", "sessions": 0, "server_started_at": 1.0, '
    b'"uptime_seconds": 1.0}'
)
_DEGRADED_BODY = b'{"status": "degraded", "sessions": 1, "server_started_at": 1.0}'
_FOREIGN_BODY = b'{"status": "ok"}'

_WILDCARD_IPV4 = ("", "0.0.0.0")
_WILDCARD_IPV6 = ("::", "[::]")


def _lan_ipv4() -> str | None:
    """A real non-loopback IPv4 address of this machine, or None.

    A UDP ``connect()`` picks the interface the kernel would use for outbound
    traffic and sends no packet. The target is TEST-NET-3 (RFC 5737), which the
    suite's own network isolation in tests/conftest.py allowlists, so this does
    not skip under pytest while still working standalone. ``apmere-usa``'s
    default route is 10.0.0.188; TEST-NET-1 is deliberately NOT used because the
    isolation blocks it.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("203.0.113.1", 9))
            address = probe.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") else address


def _stdlib_ssl_context_class() -> type:
    """The stdlib ``ssl.SSLContext``, even after ``truststore.inject_into_ssl()``.

    Hermes Agent's ``agent/ssl_verify.py`` injects truststore process-wide, and
    an earlier test in the same pytest process can import it; truststore's
    ``wrap_socket`` then verifies the peer chain on this listening socket and
    raises. Walk back to the stdlib class instead of patching global SSL state.
    """
    cls = ssl.SSLContext
    while cls.__module__ != "ssl" and cls.__bases__:
        cls = cls.__bases__[0]
    return cls


def _self_signed_cert(tmp_path: Path) -> tuple[str, str]:
    # From the openssl CLI; without it (a stock Windows dev box) skip rather
    # than fail with FileNotFoundError.
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


@contextlib.contextmanager
def _serve_on(
    host: str,
    body: bytes,
    status: int = 200,
    cert: str | None = None,
    key: str | None = None,
) -> Iterator[int]:
    """A /health listener answering ``body``, bound to ``host`` specifically.

    Unlike a loopback listener this is invisible to 127.0.0.1, which is what
    makes it stand in for a LAN-only or Tailscale-only WebUI.
    """
    if not _bindable(host):
        pytest.skip(f"cannot bind a listener on {host} in this environment")

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    with http.server.HTTPServer((host, 0), _Handler) as httpd:
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


def _bindable(host: str) -> bool:
    """Can this process open a listener on ``host``?"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
    except OSError:
        return False
    return True


@pytest.fixture
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probes must reach a local listener directly, not via HTTP_PROXY.

    urllib honours HTTP_PROXY for a private address unless NO_PROXY covers it,
    which would send the probe to the proxy and make our own WebUI look dead.
    """
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "*")


def _stub_main_up_to_preflight(monkeypatch: pytest.MonkeyPatch, argv: list) -> None:
    """Stub what main() touches before the preflight; pin argv and supervisor env."""
    monkeypatch.setattr(bootstrap, "ensure_supported_platform", lambda: None)
    monkeypatch.setattr(bootstrap, "open_browser", lambda url: None)
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"] + argv)
    for name in (
        "INVOCATION_ID",
        "JOURNAL_STREAM",
        "NOTIFY_SOCKET",
        "XPC_SERVICE_URL",
        "XPC_SERVICE_NAME",
        "SUPERVISOR_ENABLED",
        "HERMES_WEBUI_FOREGROUND",
    ):
        monkeypatch.delenv(name, raising=False)


def _raise_in_use(host: str, port: int) -> None:
    raise bootstrap.PortInUseError(
        f"Port {port} on {host} is already in use by another service."
    )


# ---------- the reported bug: a LAN-only WebUI is not recognised ----------


def test_wildcard_bind_reports_the_lan_only_webui_as_a_conflict(
    no_proxy: None,
) -> None:
    """The preflight really does conflict with a LAN-only listener.

    Without this the probe fix would be untestable: a port nothing holds would
    never reach the interface probe at all.
    """
    lan = _lan_ipv4()
    if lan is None:
        pytest.skip("no non-loopback IPv4 address on this machine")
    with _serve_on(lan, _WEBUI_BODY) as port:
        with pytest.raises(bootstrap.PortInUseError):
            bootstrap._check_port_available("0.0.0.0", port)


def test_lan_only_webui_is_recognised(no_proxy: None) -> None:
    """An own WebUI on a LAN address is ours, not a foreign conflict.

    It holds the port on that interface alone, so the wildcard bind conflicts
    while the loopback probe stays silent (#8137 part 2).
    """
    lan = _lan_ipv4()
    if lan is None:
        pytest.skip("no non-loopback IPv4 address on this machine")
    with _serve_on(lan, _WEBUI_BODY) as port:
        found = bootstrap._already_serving_on_interface("0.0.0.0", port)
    assert found == ("http", lan)


def test_lan_only_https_webui_is_recognised(no_proxy: None, tmp_path: Path) -> None:
    """The running instance may serve HTTPS from another launcher's settings."""
    lan = _lan_ipv4()
    if lan is None:
        pytest.skip("no non-loopback IPv4 address on this machine")
    cert, key = _self_signed_cert(tmp_path)
    with _serve_on(lan, _WEBUI_BODY, cert=cert, key=key) as port:
        found = bootstrap._already_serving_on_interface("0.0.0.0", port)
    assert found == ("https", lan)


def test_foreign_listener_on_a_lan_address_is_not_our_webui(no_proxy: None) -> None:
    """A generic status-ok answer on a LAN address stays a foreign listener."""
    lan = _lan_ipv4()
    if lan is None:
        pytest.skip("no non-loopback IPv4 address on this machine")
    with _serve_on(lan, _FOREIGN_BODY) as port:
        assert bootstrap._already_serving_on_interface("0.0.0.0", port) is None


def test_degraded_lan_only_webui_is_still_our_instance(
    no_proxy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degraded own WebUI answers 503 with our own payload."""
    lan = _lan_ipv4()
    if lan is None:
        pytest.skip("no non-loopback IPv4 address on this machine")
    monkeypatch.delenv("HERMES_WEBUI_TLS_CERT", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TLS_KEY", raising=False)
    with _serve_on(lan, _DEGRADED_BODY, status=503) as port:
        assert bootstrap._already_serving_on_interface("0.0.0.0", port) == ("http", lan)


# ---------- addressing the machine's own interfaces ----------


def test_interface_addresses_exclude_loopback_and_wildcard() -> None:
    """Loopback was already probed and can never identify a LAN-only listener.

    Repeating it would only spend the budget on a duplicate probe.
    """
    addresses = bootstrap._local_interface_addresses(socket.AF_INET)
    assert not any(a.startswith("127.") for a in addresses)
    assert "0.0.0.0" not in addresses
    assert "0.0.0.0" not in addresses


def test_interface_addresses_exclude_169_254_and_unspecified() -> None:
    """Unusable-in-a-URL addresses are dropped: they cannot be announced.

    ``http://[fe80::1]:8787`` parses wrong without a zone id, so recognising a
    link-local listener would print a URL that cannot reach it.
    """
    for family in (socket.AF_INET, socket.AF_INET6):
        for address in bootstrap._local_interface_addresses(family):
            assert not address.startswith("169.254.")
            assert not address.lower().startswith("fe80")


def test_interface_addresses_are_deduplicated() -> None:
    """One probe per address, however many sources reported it."""
    addresses = bootstrap._local_interface_addresses(socket.AF_INET)
    assert len(addresses) == len(set(addresses))


def test_interface_addresses_include_this_machine_lan_address() -> None:
    """The default-route source address must be among the candidates."""
    lan = _lan_ipv4()
    if lan is None:
        pytest.skip("no non-loopback IPv4 address on this machine")
    assert lan in bootstrap._local_interface_addresses(socket.AF_INET)


# ---------- scope: only a wildcard bind can hit this path ----------


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "192.168.1.5", "fd7a:115c::1", "localhost"]
)
def test_specific_host_never_falls_back_to_interface_addresses(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """An explicit --host is probed on exactly that address, nothing else.

    Binding a specific address conflicts only with a listener on that address
    or on a wildcard; the loopback probe already covers both, so widening the
    search could only surprise a user who named an address deliberately.
    """
    monkeypatch.setattr(bootstrap, "wait_for_health", lambda url, **kw: "")
    monkeypatch.setattr(
        bootstrap,
        "_local_interface_addresses",
        lambda family: pytest.fail("interface addresses must not be enumerated"),
    )
    assert bootstrap._already_serving_on_interface(host, 8787) is None


@pytest.mark.parametrize("host", _WILDCARD_IPV4 + _WILDCARD_IPV6)
def test_wildcard_host_does_reach_the_interface_probe(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """A wildcard bind is exactly the case that can meet a LAN-only listener.

    A specific ``--host`` cannot get here (its own address is probed directly),
    so this pins which hosts widen the search.
    """
    seen: list[str] = []

    def fake_wait(url: str, timeout: float = 0.0, **kwargs: object) -> str:
        seen.append(url)
        return ""

    monkeypatch.setattr(
        bootstrap,
        "_local_interface_addresses",
        lambda family: ["10.9.9.9"] if family is socket.AF_INET else [],
    )
    monkeypatch.setattr(bootstrap, "wait_for_health", fake_wait)
    assert bootstrap._already_serving_on_interface(host, 8787) is None
    assert seen == ["http://10.9.9.9:8787/health"]


def test_loopback_webui_is_found_before_any_interface_address(
    no_proxy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loopback WebUI is the common case and must not cost extra probes."""
    monkeypatch.setattr(
        bootstrap,
        "_local_interface_addresses",
        lambda family: pytest.fail("no interface probe should be needed"),
    )
    with _serve_on("127.0.0.1", _WEBUI_BODY) as port:
        assert bootstrap._already_serving_scheme("0.0.0.0", port) == "http"


# ---------- boundedness: many interfaces must not delay startup ----------


def test_interface_probe_is_bounded_by_a_total_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole sweep stops at its deadline instead of trying every address.

    ``wait_for_health`` polls on a deadline but a single request keeps its own
    socket timeout, so an unbounded loop over interfaces could add seconds per
    address to a failing startup.
    """
    monkeypatch.setattr(bootstrap, "_LAN_PROBE_TOTAL_BUDGET", 0.35)
    monkeypatch.setattr(bootstrap, "_LAN_PROBE_PER_ADDRESS", 0.1)
    monkeypatch.setattr(
        bootstrap,
        "_local_interface_addresses",
        lambda family: [f"10.0.0.{i}" for i in range(1, 21)],
    )
    probes: list[str] = []

    def slow_probe(url: str, timeout: float = 0.0, **kwargs: object) -> str:
        probes.append(url)
        time.sleep(0.1)
        return ""

    monkeypatch.setattr(bootstrap, "wait_for_health", slow_probe)

    started = time.monotonic()
    assert bootstrap._already_serving_on_interface("0.0.0.0", 8787) is None
    elapsed = time.monotonic() - started

    assert 0 < len(probes) < 20, probes
    assert elapsed < 2.0, elapsed


def test_each_interface_probe_keeps_the_health_marker_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interface probe identifies our WebUI exactly like the loopback one."""
    seen: list[tuple[str, float, dict]] = []

    def fake_wait(url: str, timeout: float = 0.0, **kwargs: object) -> str:
        seen.append((url, timeout, kwargs))
        return "http" if "10.0.0.7" in url else ""

    monkeypatch.setattr(bootstrap, "_local_interface_addresses", lambda family: ["10.0.0.7"])
    monkeypatch.setattr(bootstrap, "wait_for_health", fake_wait)

    assert bootstrap._already_serving_on_interface("0.0.0.0", 8787) == ("http", "10.0.0.7")
    assert [url for url, _, _ in seen] == ["http://10.0.0.7:8787/health"]
    _, timeout, kwargs = seen[0]
    assert timeout > 0
    assert kwargs["markers"] == bootstrap._HERMES_HEALTH_MARKERS
    assert kwargs["accept_degraded"] is True
    assert kwargs["tls_unknown"] is True


def test_ipv6_interface_addresses_are_bracketed_in_the_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``http://::1:8787/health`` would parse the last group as the port."""
    seen: list[str] = []

    def fake_wait(url: str, timeout: float = 0.0, **kwargs: object) -> str:
        seen.append(url)
        return ""

    monkeypatch.setattr(
        bootstrap,
        "_local_interface_addresses",
        lambda family: ["fd7a:115c::1"] if family is socket.AF_INET6 else [],
    )
    monkeypatch.setattr(bootstrap, "wait_for_health", fake_wait)
    bootstrap._already_serving_on_interface("::", 8787)
    assert seen == ["http://[fd7a:115c::1]:8787/health"]


# ---------- what the user is told: the URL must actually reach it ----------


def test_main_announces_the_lan_address_not_localhost(
    no_proxy: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """``localhost`` is unreachable for a LAN-only listener.

    Nothing is bound to loopback there, so announcing localhost would point at
    a closed port and send the user looking for a server that is not there.
    """
    lan = _lan_ipv4()
    if lan is None:
        pytest.skip("no non-loopback IPv4 address on this machine")
    _stub_main_up_to_preflight(monkeypatch, ["--no-browser", "--host", "0.0.0.0"])
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    with _serve_on(lan, _WEBUI_BODY) as port:
        monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--no-browser", "--host", "0.0.0.0", str(port)])
        assert bootstrap.main() == 0
    out = capsys.readouterr().out
    assert f"http://{lan}:{port}" in out
    assert "localhost" not in out


def test_main_opens_the_browser_on_the_lan_address(
    no_proxy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The announced URL is the one the browser must open."""
    lan = _lan_ipv4()
    if lan is None:
        pytest.skip("no non-loopback IPv4 address on this machine")
    _stub_main_up_to_preflight(monkeypatch, ["--host", "0.0.0.0"])
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    opened: list[str] = []
    monkeypatch.setattr(bootstrap, "open_browser", opened.append)
    with _serve_on(lan, _WEBUI_BODY) as port:
        monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--host", "0.0.0.0", str(port)])
        assert bootstrap.main() == 0
    assert opened == [f"http://{lan}:{port}"]


def test_foreground_launch_names_the_lan_only_instance(
    no_proxy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second server on the same port is refused, naming OUR instance."""
    lan = _lan_ipv4()
    if lan is None:
        pytest.skip("no non-loopback IPv4 address on this machine")
    _stub_main_up_to_preflight(monkeypatch, ["--foreground", "--host", "0.0.0.0"])
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    with _serve_on(lan, _WEBUI_BODY) as port:
        monkeypatch.setattr(
            sys, "argv", ["bootstrap.py", "--foreground", "--host", "0.0.0.0", str(port)]
        )
        with pytest.raises(bootstrap.PortInUseError) as excinfo:
            bootstrap.main()
    message = str(excinfo.value)
    assert f"http://{lan}:{port}" in message
    assert "Stop that instance first" in message
    assert "start.sh" not in message


def test_supervisor_launch_names_the_lan_only_instance(
    no_proxy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A systemd launch (INVOCATION_ID) gets the same own-instance message."""
    lan = _lan_ipv4()
    if lan is None:
        pytest.skip("no non-loopback IPv4 address on this machine")
    monkeypatch.setenv("INVOCATION_ID", "3f5b1c0e2a4d4f7e9c8b1a2d3e4f5061")
    _stub_main_up_to_preflight(monkeypatch, ["--no-browser", "--host", "0.0.0.0"])
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setenv("INVOCATION_ID", "3f5b1c0e2a4d4f7e9c8b1a2d3e4f5061")
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    with _serve_on(lan, _WEBUI_BODY) as port:
        monkeypatch.setattr(
            sys, "argv", ["bootstrap.py", "--no-browser", "--host", "0.0.0.0", str(port)]
        )
        with pytest.raises(bootstrap.PortInUseError) as excinfo:
            bootstrap.main()
    assert f"http://{lan}:{port}" in str(excinfo.value)


def test_foreign_lan_listener_still_advises_another_port(
    no_proxy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Widening the probe must not turn a foreign LAN service into ours."""
    lan = _lan_ipv4()
    if lan is None:
        pytest.skip("no non-loopback IPv4 address on this machine")
    _stub_main_up_to_preflight(monkeypatch, ["--no-browser", "--host", "0.0.0.0"])
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    with _serve_on(lan, _FOREIGN_BODY) as port:
        monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--no-browser", "--host", "0.0.0.0", str(port)])
        with pytest.raises(RuntimeError, match="already in use"):
            bootstrap.main()


# ---------- unchanged behaviour on the paths this must not disturb ----------


@pytest.mark.parametrize("host", _WILDCARD_IPV4)
def test_loopback_webui_on_a_wildcard_still_announces_localhost(
    no_proxy: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, host: str
) -> None:
    """A wildcard-bound WebUI keeps the localhost origin.

    The saved session and the passkey rpId are bound to that origin, so this
    must not move to a numeric address.
    """
    _stub_main_up_to_preflight(monkeypatch, ["--no-browser", "--host", host])
    monkeypatch.setattr(bootstrap, "_check_port_available", _raise_in_use)
    with _serve_on("127.0.0.1", _WEBUI_BODY) as port:
        monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--no-browser", "--host", host, str(port)])
        assert bootstrap.main() == 0
    assert f"http://localhost:{port}" in capsys.readouterr().out


def test_already_serving_scheme_is_unchanged_by_the_interface_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loopback probe keeps probing exactly what it probed before."""
    seen: list[str] = []

    def fake_wait(url: str, timeout: float = 0.0, **kwargs: object) -> str:
        seen.append(url)
        return ""

    monkeypatch.setattr(bootstrap, "wait_for_health", fake_wait)
    assert bootstrap._already_serving_scheme("0.0.0.0", 8787) == ""
    assert seen == ["http://127.0.0.1:8787/health"]