"""Storage full must not surface as a generic "Internal server error" (#9167).

Repro shape from the issue: with the WebUI state directory full, ``POST /api/chat/start``
raised ``OSError: [Errno 28] No space left on device`` and the handler answered
HTTP 500 ``{"error": "Internal server error"}``. The user had no way to know that
freeing space fixes it.
"""
import errno
from types import SimpleNamespace
from typing import Any, cast

import pytest


def _drive_write(monkeypatch, exc, path="/api/chat/start"):
    """Run the production _handle_write with a route that raises ``exc``; return j() calls."""
    import api.helpers
    import server

    calls = []
    monkeypatch.setattr(server, "reset_trusted_auth_request_state", lambda _handler: None)
    monkeypatch.setattr(server, "get_profile_cookie", lambda _handler: None)
    monkeypatch.setattr(server, "clear_request_profile", lambda: None)
    monkeypatch.setattr(server, "check_auth_or_close", lambda _handler, _parsed: True)
    record = lambda _handler, body, status=200, **_kw: calls.append((status, body))  # noqa: E731
    monkeypatch.setattr(api.helpers, "j", record)
    monkeypatch.setattr(server, "j", record)

    handler = SimpleNamespace(path=path, command="POST", headers={}, close_connection=False,
                              _safe_webui_print=lambda _msg: None)

    def route(_handler, _parsed):
        raise exc

    server.Handler._handle_write(cast(Any, handler), route)
    return calls


def _drive_get(monkeypatch, exc, path="/api/session"):
    """Run the production do_GET with handle_get raising ``exc``; return j() calls."""
    import api.helpers
    import server

    calls = []
    monkeypatch.setattr(server, "reset_trusted_auth_request_state", lambda _handler: None)
    monkeypatch.setattr(server, "get_profile_cookie", lambda _handler: None)
    monkeypatch.setattr(server, "clear_request_profile", lambda: None)
    monkeypatch.setattr(server, "check_auth_or_close", lambda _handler, _parsed: True)
    record = lambda _handler, body, status=200, **_kw: calls.append((status, body))  # noqa: E731
    monkeypatch.setattr(api.helpers, "j", record)
    monkeypatch.setattr(server, "j", record)

    def handle_get(_handler, _parsed):
        raise exc

    monkeypatch.setattr(server, "handle_get", handle_get)
    handler = server.Handler.__new__(server.Handler)
    handler.path, handler.command, handler.headers = path, "GET", {}
    handler._safe_webui_print = lambda _msg: None
    server.Handler.do_GET(handler)
    return calls


def _enospc():
    return OSError(errno.ENOSPC, "No space left on device")


def _wrapped_enospc():
    try:
        raise _enospc()
    except OSError as inner:
        try:
            raise RuntimeError("could not persist session") from inner
        except RuntimeError as outer:
            return outer


def test_chat_start_enospc_reports_storage_full(monkeypatch):
    """The exact shape from #9167: POST /api/chat/start hitting Errno 28."""
    assert _drive_write(monkeypatch, _enospc()) == [
        (507, {"error": "Storage on the server is full. Free up space and try again."})]


@pytest.mark.parametrize("make_exc", [_wrapped_enospc, lambda: OSError(errno.EDQUOT, "Disk quota exceeded")],
                         ids=["raised-from-enospc", "edquot"])
def test_write_storage_full_variants(monkeypatch, make_exc):
    status, body = _drive_write(monkeypatch, make_exc())[0]
    assert status == 507 and "Storage on the server is full" in body["error"]


def test_get_path_enospc_reports_storage_full(monkeypatch):
    status, body = _drive_get(monkeypatch, _enospc())[0]
    assert status == 507 and "Storage on the server is full" in body["error"]


@pytest.mark.parametrize("exc", [ValueError("boom"), OSError(errno.EACCES, "Permission denied")],
                         ids=["value-error", "other-oserror"])
def test_other_errors_keep_generic_500(monkeypatch, exc):
    assert _drive_write(monkeypatch, exc) == [(500, {"error": "Internal server error"})]
