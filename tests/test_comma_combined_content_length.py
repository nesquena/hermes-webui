"""Comma-combined ``Content-Length`` must not over-close a healthy connection.

RFC 9110 §5.3 lets a list-valued field arrive either as repeated header lines or
as a single comma-combined line, and the two spellings are equivalent. So
``Content-Length: 0, 0`` describes the same bodyless request as two separate
``Content-Length: 0`` headers, and rejecting it must keep the keep-alive.

``_declared_content_lengths()`` originally ran ``isdigit()`` across the whole raw
value, so the comma failed the check, the field read as *unreadable*, and every
helper-backed rejection armed ``Connection: close`` on a request with nothing
queued behind it. That is the over-close half of this PR's contract — the mirror
image of the bug it fixes, and the one that silently drops pooled connections.

The dangerous direction has to stay closed, so the disagreeing and malformed
spellings are asserted here alongside the recoverable ones: ``0, 42`` genuinely
cannot frame a body and must still be refused with the close armed.
"""

import pytest

from api.helpers import request_declares_body, unreadable_content_length


class _Hdrs(dict):
    """email.message-like: ``get()`` returns the first value, ``get_all()`` all."""

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


class _H:
    def __init__(self, headers):
        self.headers = headers


def _handler(cl):
    return _H(_Hdrs({"Content-Length": cl}))


@pytest.mark.parametrize("value", ["0, 0", "00, 00", "0 , 0", "0,0", "0,\t0"])
def test_agreeing_bodyless_comma_list_is_readable_and_bodyless(value):
    """An agreeing all-zero list is framed, bodyless, and must keep keep-alive."""
    h = _handler(value)
    assert unreadable_content_length(h) is False, (
        f"Content-Length {value!r} read as unreadable; a bodyless rejection would "
        "then arm Connection: close and drop a healthy pooled connection"
    )
    assert bool(request_declares_body(h)) is False, (
        f"Content-Length {value!r} declares no body, so nothing is queued"
    )


@pytest.mark.parametrize("value", ["42, 42", "42,42", "042, 42"])
def test_agreeing_nonzero_comma_list_is_readable_with_a_body(value):
    """An agreeing nonzero list frames a real body: readable, and a body IS pending."""
    h = _handler(value)
    assert unreadable_content_length(h) is False, value
    assert bool(request_declares_body(h)) is True, value


@pytest.mark.parametrize("value", ["0, 42", "42, 0", "0, 0, 42"])
def test_disagreeing_comma_list_stays_unreadable(value):
    """Disagreeing members cannot frame a body — must stay refused, close armed."""
    h = _handler(value)
    assert unreadable_content_length(h) is True, (
        f"Content-Length {value!r} disagrees; there is no honest length to drain, "
        "so it must be refused with the connection armed for close"
    )
    assert bool(request_declares_body(h)) is True, value


@pytest.mark.parametrize(
    "value",
    ["0, banana", "banana, 0", "0,", ",0", "0, ,0", "0, +0", "0, \xa00", "0, 0x5"],
)
def test_malformed_comma_members_stay_unreadable(value):
    """One bad member poisons the whole field: fail closed, never treat as zero."""
    h = _handler(value)
    assert unreadable_content_length(h) is True, (
        f"Content-Length {value!r} has an unparseable member and must fail closed"
    )
    assert bool(request_declares_body(h)) is True, value


def test_repeated_headers_and_comma_list_agree():
    """The two RFC spellings of the same field must behave identically."""
    repeated = _H(_Hdrs({"Content-Length": ["0", "0"]}))
    combined = _handler("0, 0")
    assert unreadable_content_length(repeated) == unreadable_content_length(combined)
    assert bool(request_declares_body(repeated)) == bool(request_declares_body(combined))

    repeated_bad = _H(_Hdrs({"Content-Length": ["0", "42"]}))
    combined_bad = _handler("0, 42")
    assert unreadable_content_length(repeated_bad) is True
    assert unreadable_content_length(combined_bad) is True
