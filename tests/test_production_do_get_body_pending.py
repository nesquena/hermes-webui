"""``Handler.do_GET`` itself must arm the close when a body is still queued.

This is the production-wired companion to
``test_get_with_body_auth_reject_reuse.py``. That file proves the HTTP-level
consequence using a handler of the same shape; this one asserts the real
``server.Handler.do_GET`` routes its auth rejection through the body-pending-aware
path, so reverting ``server.py`` to plain ``check_auth`` fails a test.

That distinction matters here: the PR's existing GET unit tests all pass with the
residual reverted, because none of them reach ``do_GET``'s auth call.
"""

import types

import pytest


def _handler_double(headers, path="/api/sessions"):
    """A handler stub shaped like the one the reject paths receive."""
    h = types.SimpleNamespace()
    h.path = path
    h.headers = headers
    h.close_connection = False
    h.command = "GET"
    h._headers_buffer = []
    h.responses = []
    h.sent = []
    return h


class _Hdrs(dict):
    def get(self, k, default=None):
        for key, val in self.items():
            if key.lower() == k.lower():
                return val[0] if isinstance(val, list) else val
        return default

    def get_all(self, k, default=None):
        out = []
        for key, val in self.items():
            if key.lower() == k.lower():
                out.extend(val if isinstance(val, list) else [val])
        return out or default


def test_do_get_source_uses_the_body_pending_aware_auth_call():
    """``do_GET`` must not call bare ``check_auth``.

    A source assertion is the right tool for this one: the call is a single line
    inside a method whose surrounding machinery (socket, wfile, routing) would
    have to be faked wholesale to reach it, and faking that is what let the
    residual hide in the first place.
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "server.py"
    text = src.read_text()

    start = text.index("def do_GET")
    end = text.index("def ", start + 10)
    body = text[start:end]

    assert "check_auth_or_close(self, parsed)" in body, (
        "do_GET must route its auth rejection through check_auth_or_close so a "
        "body-bearing GET cannot leave unread bytes on a keep-alive socket"
    )
    assert "check_auth(self, parsed)" not in body, (
        "do_GET still calls bare check_auth — a body-bearing GET that fails auth "
        "will poison the next request on the connection"
    )


def test_check_auth_or_close_arms_only_when_a_body_is_pending():
    """The over-close half, at the production helper."""
    from api.auth import check_auth_or_close
    import api.auth as auth_mod

    calls = {}

    def _deny(handler, parsed):
        calls["hit"] = True
        return False

    real = auth_mod.check_auth
    auth_mod.check_auth = _deny
    try:
        # Body pending -> close armed.
        h = _handler_double(_Hdrs({"Content-Length": "15"}))
        assert check_auth_or_close(h, None) is False
        assert h.close_connection is True, "body-bearing reject must arm the close"

        # Body-less -> keep-alive preserved.
        h2 = _handler_double(_Hdrs({}))
        assert check_auth_or_close(h2, None) is False
        assert h2.close_connection is False, (
            "a body-less reject must NOT close — that drops a healthy keep-alive"
        )

        # Unreadable framing -> fail closed.
        h3 = _handler_double(_Hdrs({"Content-Length": "+0"}))
        assert check_auth_or_close(h3, None) is False
        assert h3.close_connection is True, (
            "unreadable Content-Length must fail closed, not be treated as zero"
        )
    finally:
        auth_mod.check_auth = real


@pytest.mark.parametrize(
    "value",
    ["\xa00", "0\x85", "+0", "-0", "0_0", "\u0660", "-5", ""],
)
def test_adversarial_zero_spellings_declare_a_body(value):
    """Every spelling of "zero" that ``int()`` accepts must still fail closed.

    ``int()`` is far more generous than RFC 9110 ``Content-Length = 1*DIGIT``:
    it tolerates unicode whitespace, a leading sign, PEP-515 underscores and
    non-ASCII digits. Each of those parsed to an honest ``0`` would take the
    keep-alive branch with the body still queued.
    """
    from api.helpers import request_declares_body

    h = _handler_double(_Hdrs({"Content-Length": value}))
    assert request_declares_body(h) is True, (
        f"Content-Length {value!r} was treated as no-body; a queued body would "
        "then poison the next request on this connection"
    )
