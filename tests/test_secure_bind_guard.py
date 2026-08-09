"""Fail-closed non-loopback bind when authentication is disabled."""

import pytest


def test_loopback_hosts_are_allowed_without_auth():
    from api.auth import require_secure_bind

    for host in ("127.0.0.1", "::1", "localhost", "[::1]"):
        require_secure_bind(host, auth_enabled=False, allow_insecure=False)


def test_non_loopback_refused_without_auth():
    from api.auth import require_secure_bind

    with pytest.raises(SystemExit) as exc:
        require_secure_bind("0.0.0.0", auth_enabled=False, allow_insecure=False)
    msg = str(exc.value)
    assert "Refusing to bind" in msg
    assert "HERMES_WEBUI_ALLOW_INSECURE_BIND" in msg


def test_non_loopback_allowed_with_auth():
    from api.auth import require_secure_bind

    require_secure_bind("0.0.0.0", auth_enabled=True, allow_insecure=False)


def test_non_loopback_allowed_with_escape_hatch():
    from api.auth import require_secure_bind

    require_secure_bind("0.0.0.0", auth_enabled=False, allow_insecure=True)


def test_is_loopback_bind_host():
    from api.auth import is_loopback_bind_host

    assert is_loopback_bind_host("127.0.0.1")
    assert is_loopback_bind_host("::1")
    assert is_loopback_bind_host("[::1]")
    assert not is_loopback_bind_host("0.0.0.0")
    assert not is_loopback_bind_host("::")
    assert not is_loopback_bind_host("192.168.1.10")


def test_server_py_uses_require_secure_bind():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "server.py").read_text()
    assert "require_secure_bind" in text
    assert "allow_insecure_bind" in text
    assert "HERMES_WEBUI_ALLOW_INSECURE_BIND" in text
