"""Regression tests for HTTP/1.1 write rejection framing.

A rejected POST can happen before its request body is consumed (authentication
or CSRF failure). Keeping that connection alive leaves the body bytes in
``rfile``; BaseHTTPRequestHandler then parses them as the next request method.
The connection must close after those early rejections.
"""
from types import SimpleNamespace
from typing import Any, cast

import pytest


def test_auth_rejected_write_closes_connection(monkeypatch):
    import api.auth as auth
    import server

    handler = SimpleNamespace(
        path="/api/session/new",
        command="POST",
        close_connection=False,
    )
    monkeypatch.setattr(server, "reset_trusted_auth_request_state", lambda _handler: None)
    monkeypatch.setattr(server, "get_profile_cookie", lambda _handler: None)
    monkeypatch.setattr(server, "clear_request_profile", lambda: None)
    monkeypatch.setattr(auth, "check_auth", lambda _handler, _parsed: False)

    def route_must_not_run(_handler, _parsed):
        raise AssertionError("route ran after auth rejection")

    server.Handler._handle_write(cast(Any, handler), route_must_not_run)

    assert handler.close_connection is True


def test_origin_rejected_csrf_closes_connection(monkeypatch):
    import api.routes as routes

    handler = SimpleNamespace(close_connection=False)
    monkeypatch.setattr(routes, "_check_same_origin_browser_request", lambda _handler: False)

    assert routes._check_csrf(handler) is False
    assert handler.close_connection is True


def test_token_rejected_csrf_closes_connection(monkeypatch):
    import api.auth as auth
    import api.routes as routes

    handler = SimpleNamespace(headers={}, close_connection=False)
    monkeypatch.setattr(routes, "_check_same_origin_browser_request", lambda _handler: True)
    monkeypatch.setattr(routes, "_is_browser_unsafe_request", lambda _handler: True)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda _handler: "session-cookie")
    monkeypatch.setattr(auth, "verify_csrf_token", lambda _cookie, _token: False)

    assert routes._check_csrf(handler) is False
    assert handler.close_connection is True


def test_sidecar_provenance_rejected_closes_connection(monkeypatch):
    """Sidecar proxy provenance rejection also runs before read_body()."""
    import api.routes as routes

    handler = SimpleNamespace(
        path="/api/extensions/ext1/sidecar/proxy",
        command="POST",
        headers={"Content-Length": "15"},
        close_connection=False,
    )
    monkeypatch.setattr(routes, "_match_extension_sidecar_proxy_path", lambda _path: ("ext1", "/proxy"))
    monkeypatch.setattr(routes, "_check_same_origin_browser_request", lambda _handler, **_: False)
    monkeypatch.setattr(routes, "j", lambda _handler, _payload, status=200: None)

    routes._handle_extension_sidecar_proxy(
        handler, SimpleNamespace(path=handler.path, query=""), "POST", read_request_body=True
    )

    assert handler.close_connection is True


def test_sidecar_get_provenance_rejected_keeps_connection(monkeypatch):
    """A body-less GET rejection has nothing unread — keep-alive must survive.

    Closing here would kill a pooled client's connection for no framing
    reason (the deep-review gate's MUST-FIX 2).
    """
    import api.routes as routes

    handler = SimpleNamespace(
        path="/api/extensions/ext1/sidecar/proxy",
        command="GET",
        headers={},
        close_connection=False,
    )
    monkeypatch.setattr(routes, "_match_extension_sidecar_proxy_path", lambda _path: ("ext1", "/proxy"))
    monkeypatch.setattr(routes, "_check_same_origin_browser_request", lambda _handler, **_: False)
    monkeypatch.setattr(routes, "j", lambda _handler, _payload, status=200: None)

    routes._handle_extension_sidecar_proxy(handler, SimpleNamespace(path=handler.path, query=""), "GET")

    assert handler.close_connection is False


# ── Framing, not the method, decides whether a rejection must close ──────────
#
# The re-gate found both halves of the same mistake: `read_request_body` is a
# static per-method flag, so it neither proves a body is pending (a body-less
# DELETE closed a healthy connection) nor proves one is absent (a GET carrying a
# declared body kept the connection and poisoned it). Both directions below run
# against the production handler.


def _rejected_sidecar_call(monkeypatch, method, headers, **kwargs):
    """Drive a provenance-rejected sidecar proxy call; return the handler."""
    import api.routes as routes

    handler = SimpleNamespace(
        path="/api/extensions/ext1/sidecar/proxy",
        command=method,
        headers=headers,
        close_connection=False,
    )
    monkeypatch.setattr(routes, "_match_extension_sidecar_proxy_path", lambda _path: ("ext1", "/proxy"))
    monkeypatch.setattr(routes, "_check_same_origin_browser_request", lambda _handler, **_: False)
    monkeypatch.setattr(routes, "j", lambda _handler, _payload, status=200: None)

    routes._handle_extension_sidecar_proxy(
        cast(Any, handler), SimpleNamespace(path=handler.path, query=""), method, **kwargs
    )
    return handler


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Length": "12"},
        {"Transfer-Encoding": "chunked"},
        {"Content-Length": "banana"},  # unreadable framing proves nothing is absent
    ],
    ids=["content-length", "chunked", "garbage-content-length"],
)
def test_sidecar_get_with_a_declared_body_closes_connection(headers, monkeypatch):
    """A GET may still declare a body — those bytes are unread, so close.

    Reproduced by the gate as a 403 WITHOUT `Connection: close`, followed by
    `501 Unsupported method ('{}GET')` on the same socket.
    """
    handler = _rejected_sidecar_call(monkeypatch, "GET", headers)

    assert handler.close_connection is True


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "headers", [{}, {"Content-Length": "0"}], ids=["no-content-length", "zero-content-length"]
)
def test_bodyless_write_method_rejection_keeps_connection(method, headers, monkeypatch):
    """A write method with no declared body has nothing unread — keep-alive.

    `read_request_body=True` is passed for every one of these (that is what the
    real unsafe-method call site does), so this is exactly the case the old
    per-method gate got wrong in the over-close direction.
    """
    handler = _rejected_sidecar_call(monkeypatch, method, headers, read_request_body=True)

    assert handler.close_connection is False


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, False),
        ({"Content-Length": "0"}, False),
        ({"Content-Length": "00"}, False),  # 1*DIGIT, so an honest zero
        ({"Content-Length": " 0 "}, False),
        ({"Content-Length": "0\t"}, False),
        ({"Content-Length": ""}, True),
        ({"Content-Length": "   "}, True),
        ({"Content-Length": "\t"}, True),
        ({"Content-Length": "12"}, True),
        ({"Content-Length": "-1"}, True),
        ({"Content-Length": "banana"}, True),
        ({"Content-Length": "0x5"}, True),
        ({"Content-Length": "+5"}, True),
        # int() is looser than 1*DIGIT, and every one of these reads as ZERO --
        # where the wrong parse is not merely wrong but silently keep-alive.
        ({"Content-Length": "+0"}, True),
        ({"Content-Length": "-0"}, True),
        ({"Content-Length": "0_0"}, True),
        ({"Content-Length": "+00"}, True),
        ({"Content-Length": "0_0_0"}, True),
        ({"Content-Length": "٠0"}, True),  # ARABIC-INDIC ZERO ahead of an ASCII one
        ({"Content-Length": "٠"}, True),  # ARABIC-INDIC DIGIT ZERO: int() -> 0
        ({"Content-Length": "０"}, True),  # FULLWIDTH DIGIT ZERO: int() -> 0
        ({"Content-Length": "٥"}, True),  # ARABIC-INDIC DIGIT FIVE: int() -> 5
        ({"Content-Length": "\xa00"}, True),  # NBSP-padded zero: int() strips it
        ({"Content-Length": "0\x85"}, True),  # NEL-padded zero: likewise
        ({"content-length": "7"}, True),
        ({"Transfer-Encoding": "chunked"}, True),
        ({"transfer-encoding": "Chunked"}, True),
        # No coding is exempt: http.server decodes none of them (RFC 9112 6.3).
        ({"Transfer-Encoding": "identity"}, True),
        ({"Transfer-Encoding": "IDENTITY "}, True),
        ({"Transfer-Encoding": "Identity"}, True),
        ({"Transfer-Encoding": "\tidentity\t"}, True),
        ({"Transfer-Encoding": "identity, chunked"}, True),
        ({"Transfer-Encoding": "chunked, identity"}, True),
        ({"Transfer-Encoding": "identity,identity"}, True),
        ({"Transfer-Encoding": ","}, True),
        ({"Transfer-Encoding": "banana"}, True),
        ({"Transfer-Encoding": ""}, True),
        ({"Transfer-Encoding": "  "}, True),
        ({"Transfer-Encoding": "\xa0identity"}, True),  # not the token `identity`
        # A coding beside a length: the coding decides, whatever the length says.
        ({"Transfer-Encoding": "identity", "Content-Length": "0"}, True),
        ({"Transfer-Encoding": "identity", "Content-Length": "15"}, True),
    ],
)
def test_request_declares_body_reads_the_framing_headers(headers, expected):
    from api.helpers import request_declares_body

    handler = SimpleNamespace(headers=headers)

    assert request_declares_body(cast(Any, handler)) is expected


def test_request_declares_body_is_case_insensitive_on_a_real_message():
    """Production handlers carry an email.message.Message, not a dict."""
    from email.parser import Parser

    from api.helpers import request_declares_body

    message = Parser().parsestr("Host: x\r\ntransfer-encoding: Chunked\r\n\r\n")

    assert request_declares_body(cast(Any, SimpleNamespace(headers=message))) is True


def test_request_declares_body_tolerates_a_handler_without_headers():
    """Never raise on a rejection path: an exception there becomes a 500."""
    from api.helpers import request_declares_body

    assert request_declares_body(cast(Any, SimpleNamespace())) is False
    assert request_declares_body(cast(Any, SimpleNamespace(headers=None))) is False
    assert request_declares_body(cast(Any, SimpleNamespace(headers=object()))) is False


# ── A duplicated framing header hides the body from a first-value-only read ────
#
# `Message.get()` returns only the FIRST occurrence, so `Content-Length: 0`
# ahead of the real length reads back as an empty body: nothing is drained and
# the real bytes are parsed as the next request line. Verified live against the
# running server before the fix -- 403 then
# `400 Bad request syntax ('{"stale": true}GET /api/health/agent HTTP/1.1')`,
# and on /api/upload a 400 then `400 Bad request syntax ('--x')`.


def _message_with(raw_headers: str):
    """Build the email.message.Message a production handler really carries."""
    from email.parser import Parser

    return SimpleNamespace(headers=Parser().parsestr(raw_headers + "\r\n"))


def test_duplicate_conflicting_content_length_declares_a_body():
    from api.helpers import request_declares_body

    handler = _message_with("Host: x\r\nContent-Length: 0\r\nContent-Length: 15\r\n")

    assert request_declares_body(cast(Any, handler)) is True


def test_duplicate_agreeing_zero_content_length_declares_no_body():
    """Repetition alone is not a conflict -- two honest zeroes still frame nothing."""
    from api.helpers import request_declares_body

    handler = _message_with("Host: x\r\nContent-Length: 0\r\nContent-Length: 0\r\n")

    assert request_declares_body(cast(Any, handler)) is False


@pytest.mark.parametrize(
    "raw",
    [
        "Transfer-Encoding: identity\r\nTransfer-Encoding: chunked\r\n",
        "Transfer-Encoding: chunked\r\nTransfer-Encoding: identity\r\n",
        "Transfer-Encoding: identity\r\nTransfer-Encoding: identity\r\n",
        "Transfer-Encoding: identity\r\ntransfer-encoding: identity\r\n",
    ],
    ids=["identity-then-chunked", "chunked-then-identity", "twice", "twice-mixed-case"],
)
def test_repeated_transfer_encoding_refuses_whatever_the_order(raw):
    """A duplicated header cannot hide an undecodable coding behind an "unframed" one.

    This used to have to look PAST a leading `identity` to find the chunked coding
    (`Message.get()` returns only the first value, so it read as unframed and the
    chunk bytes stayed queued). With no coding exempt the leading value already
    refuses, so no ordering can mask anything -- which is what is pinned here,
    rather than the specific coding a first-value read happened to miss.
    """
    from api.helpers import request_declares_body, unsupported_transfer_encoding

    handler = _message_with("Host: x\r\n" + raw)

    assert unsupported_transfer_encoding(cast(Any, handler)) is not None
    assert request_declares_body(cast(Any, handler)) is True


def test_unreadable_content_length_flags_conflict_and_garbage_only():
    from api.helpers import unreadable_content_length

    assert unreadable_content_length(cast(Any, _message_with("Content-Length: 5\r\n"))) is False
    assert unreadable_content_length(cast(Any, _message_with("Host: x\r\n"))) is False
    assert unreadable_content_length(
        cast(Any, _message_with("Content-Length: 5\r\nContent-Length: 5\r\n"))
    ) is False
    assert unreadable_content_length(
        cast(Any, _message_with("Content-Length: 0\r\nContent-Length: 9\r\n"))
    ) is True
    assert unreadable_content_length(cast(Any, _message_with("Content-Length: nope\r\n"))) is True


def test_sidecar_get_with_duplicate_content_length_closes_connection(monkeypatch):
    """The conflicting-framing GET reaches the production provenance rejection."""
    import api.routes as routes

    from email.parser import Parser

    headers = Parser().parsestr("Host: x\r\nContent-Length: 0\r\nContent-Length: 15\r\n\r\n")
    handler = SimpleNamespace(
        path="/api/extensions/ext1/sidecar/proxy",
        command="GET",
        headers=headers,
        close_connection=False,
    )
    monkeypatch.setattr(routes, "_match_extension_sidecar_proxy_path", lambda _path: ("ext1", "/proxy"))
    monkeypatch.setattr(routes, "_check_same_origin_browser_request", lambda _handler, **_: False)
    monkeypatch.setattr(routes, "j", lambda _handler, _payload, status=200: None)

    routes._handle_extension_sidecar_proxy(
        cast(Any, handler), SimpleNamespace(path=handler.path, query=""), "GET"
    )

    assert handler.close_connection is True


# ── A BLANK framing value is unreadable, not absent ───────────────────────────
#
# `Content-Length:` with nothing after the colon reads back as '' from
# `get_all()`, and the empty value was SKIPPED — so `_declared_content_lengths()`
# reported "no length declared", `request_declares_body()` said no body was
# pending, and the provenance rejection answered 403 with keep-alive intact.
# Reproduced live against the running server on the otherwise-fixed head:
#
#   GET /api/extensions/probe/sidecar/ping  `Content-Length:`  {"stale": true}
#     -> HTTP/1.1 403 Forbidden        (no Connection: close)
#     -> HTTP/1.1 400 Bad request syntax
#            ('{"stale": true}GET /api/health/agent HTTP/1.1')
#
# `Message` collapses `Content-Length:`, `Content-Length:   ` and
# `Content-Length:\t` to the same '', so every blank spelling poisoned the socket
# identically — as did a blank value duplicated, or paired with a real length.
#
# A blank `Transfer-Encoding:` was the same hole one header over: '' reached the
# allowlist of codings that "framed nothing" (since deleted, along with the
# `identity` entry that was its last member), so a header naming no coding at all
# read as "no framing to worry about" and its payload poisoned the socket the same
# way (verified live: 403 then the same bad-syntax 400). Absence is something only
# the wire can say, and an absent header yields no values at all.


_BLANK_FRAMING_HEADERS = [
    "Content-Length:",
    "Content-Length:   ",
    "Content-Length:\t",
    "Content-Length:\r\nContent-Length:",  # duplicated blank
    "Content-Length:\r\nContent-Length: 15",  # blank ahead of a real length
    "Content-Length: 15\r\nContent-Length:",  # and behind one
    "Content-Length: 0\r\nContent-Length:",  # blank behind an honest zero
    "Transfer-Encoding:",
    "Transfer-Encoding:   ",
    "Transfer-Encoding:\r\nTransfer-Encoding: chunked",
]


@pytest.mark.parametrize("raw", _BLANK_FRAMING_HEADERS)
def test_blank_framing_value_declares_a_body(raw):
    """On a real email.message.Message, as the production handler carries."""
    from api.helpers import request_declares_body

    handler = _message_with("Host: x\r\n" + raw + "\r\n")

    assert request_declares_body(cast(Any, handler)) is True


@pytest.mark.parametrize(
    "raw",
    [
        "Content-Length:",
        "Content-Length:   ",
        "Content-Length:\r\nContent-Length:",
        "Content-Length:\r\nContent-Length: 15",
        "Content-Length: 0\r\nContent-Length:",
    ],
)
def test_blank_content_length_is_unreadable(raw):
    """The upload preflight gate must refuse it too — there is no length to drain."""
    from api.helpers import unreadable_content_length

    assert unreadable_content_length(cast(Any, _message_with("Host: x\r\n" + raw + "\r\n"))) is True


def test_blank_transfer_encoding_is_reported_as_an_undecodable_coding():
    """Named, non-empty and never None, so `is not None` callers and the 411 read right."""
    from api.helpers import unsupported_transfer_encoding

    coding = unsupported_transfer_encoding(cast(Any, _message_with("Host: x\r\nTransfer-Encoding:\r\n")))

    assert coding is not None
    assert coding, "an empty return value would be a falsy non-None trap for callers"
    # Only an ABSENT header frames nothing; a present one always names a coding
    # this server cannot decode, `identity` included.
    assert unsupported_transfer_encoding(cast(Any, _message_with("Host: x\r\n"))) is None
    assert unsupported_transfer_encoding(
        cast(Any, _message_with("Host: x\r\nTransfer-Encoding: identity\r\n"))
    ) == "identity"


@pytest.mark.parametrize("raw", _BLANK_FRAMING_HEADERS)
def test_sidecar_get_with_blank_framing_value_closes_connection(raw, monkeypatch):
    """The reported case, through the production provenance rejection."""
    import api.routes as routes

    from email.parser import Parser

    handler = SimpleNamespace(
        path="/api/extensions/ext1/sidecar/proxy",
        command="GET",
        headers=Parser().parsestr("Host: x\r\n" + raw + "\r\n\r\n"),
        close_connection=False,
    )
    monkeypatch.setattr(routes, "_match_extension_sidecar_proxy_path", lambda _path: ("ext1", "/proxy"))
    monkeypatch.setattr(routes, "_check_same_origin_browser_request", lambda _handler, **_: False)
    monkeypatch.setattr(routes, "j", lambda _handler, _payload, status=200: None)

    routes._handle_extension_sidecar_proxy(
        cast(Any, handler), SimpleNamespace(path=handler.path, query=""), "GET"
    )

    assert handler.close_connection is True


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "Content-Length: 0\r\n",
        "Content-Length:  0 \r\n",
        "Content-Length: 0\r\nContent-Length: 0\r\n",
    ],
)
def test_blank_rule_does_not_over_close_a_bodyless_request(raw):
    """The other half of the contract: framing that says "no body" keeps keep-alive.

    No framing header at all, an OWS-padded zero and two AGREEING zeroes are
    honest statements that nothing is queued, and none of them may be swept up by
    the blank rule.
    """
    from api.helpers import request_declares_body

    assert request_declares_body(cast(Any, _message_with("Host: x\r\n" + raw))) is False


# ── A length that only `int()` calls zero is unreadable, not zero ──────────────
#
# The blank rule above closed the "no value" hole; this one closes the "value
# `int()` is too generous about" hole beside it. RFC 9110 is
# `Content-Length = 1*DIGIT` -- an unsigned run of ASCII digits -- but `int()`
# also accepts a leading sign, PEP 515 underscores and non-ASCII digits, and
# strips non-ASCII whitespace. Every spelling of ZERO below therefore parsed to
# an honest `0`, took the "every declared length agrees on zero" keep-alive
# branch, and left the payload queued. Reproduced live on the otherwise-fixed
# head, pipelined down one socket:
#
#   GET /api/extensions/probe/sidecar/ping  `Content-Length: +0`  {"stale": true}
#     -> HTTP/1.1 403 Forbidden        (no Connection: close)
#     -> HTTP/1.1 400 Bad request syntax
#            ('{"stale": true}GET /api/health/agent HTTP/1.1')
#
# byte-for-byte the trace the blank value produced, and on /api/upload a 400
# then `400 Bad request syntax ('--x')`.
#
# A sign is only harmless on a NON-zero value (`+5` parses to 5, declares a body
# and closes anyway), which is exactly why the family looked safe: the mistake is
# invisible until the value is zero.
#
# Non-ASCII digits are unreadable by the same rule. An HTTP/1.1 request line
# decodes as latin-1, so U+0660 cannot cross the wire in a header value (only
# U+00B2/U+00B3/U+00B9 -- superscripts, which `int()` already rejects -- and the
# U+0085/U+00A0 whitespace `int()` strips can); these helpers take any headers
# mapping, though, so the grammar is enforced here rather than relying on the
# transport to filter.

_MALFORMED_ZERO_LENGTHS = [
    "+0",
    "-0",
    "0_0",
    "+00",
    "0_0_0",
    "\xa00",  # NBSP-padded zero: str.strip()/int() erase U+00A0
    "0\x85",  # NEL-padded zero: likewise U+0085
    "٠",  # ARABIC-INDIC DIGIT ZERO -- isdigit() and int() both accept it
    "０",  # FULLWIDTH DIGIT ZERO
    "𝟢",  # MATHEMATICAL MONOSPACE DIGIT ZERO
]


@pytest.mark.parametrize("value", _MALFORMED_ZERO_LENGTHS)
def test_malformed_zero_content_length_declares_a_body(value):
    """On a real email.message.Message, as the production handler carries."""
    from api.helpers import request_declares_body

    handler = _message_with(f"Host: x\r\nContent-Length: {value}\r\n")

    assert request_declares_body(cast(Any, handler)) is True


@pytest.mark.parametrize("value", _MALFORMED_ZERO_LENGTHS)
def test_malformed_zero_content_length_is_unreadable(value):
    """The upload preflight gate must refuse it too -- it is not a length."""
    from api.helpers import unreadable_content_length

    handler = _message_with(f"Host: x\r\nContent-Length: {value}\r\n")

    assert unreadable_content_length(cast(Any, handler)) is True


@pytest.mark.parametrize("value", _MALFORMED_ZERO_LENGTHS)
def test_sidecar_get_with_a_malformed_zero_length_closes_connection(value, monkeypatch):
    """Through the production provenance rejection, not just the helper."""
    handler = _rejected_sidecar_call(
        monkeypatch, "GET", _message_with(f"Host: x\r\nContent-Length: {value}\r\n").headers
    )

    assert handler.close_connection is True


@pytest.mark.parametrize(
    "value",
    ["0", "00", "000", " 0 ", "0\t", "\t0", "0\r\nContent-Length: 0"],
    ids=["zero", "double-zero", "triple-zero", "padded", "tab-behind", "tab-ahead", "two-agreeing"],
)
def test_digit_grammar_does_not_over_close_an_honest_zero(value):
    """The over-close half: `1*DIGIT` zeroes, SP/HTAB padding and all, stay alive.

    `00` is as valid as `0` (`1*DIGIT` sets no leading-zero rule), and RFC 9110
    OWS around a field value is SP/HTAB -- so trimming those must not become
    trimming everything `str.strip()` would.
    """
    from api.helpers import request_declares_body, unreadable_content_length

    handler = _message_with(f"Host: x\r\nContent-Length: {value}\r\n")

    assert request_declares_body(cast(Any, handler)) is False
    assert unreadable_content_length(cast(Any, handler)) is False


def test_absurdly_long_digit_run_is_unreadable_and_never_raises():
    """`1*DIGIT` is necessary but not sufficient: `int()` caps the digit count.

    A run longer than `sys.get_int_max_str_digits()` (4300) raises ValueError, and
    this runs on rejection paths where an exception becomes a spurious 500 -- so
    the parse stays guarded even though every character is now vetted first.
    """
    from api.helpers import request_declares_body, unreadable_content_length

    handler = _message_with("Host: x\r\nContent-Length: " + "0" * 5000 + "\r\n")

    assert request_declares_body(cast(Any, handler)) is True
    assert unreadable_content_length(cast(Any, handler)) is True


# ── The same audit one header over: NO transfer coding is decodable here ───────
#
# `identity` was exempt as a coding that "frames nothing", so `Transfer-Encoding:
# identity` plus payload bytes read as body-less: the sidecar rejection answered
# 403 with keep-alive and the payload became the next request line, and all four
# upload handlers fell through to a length-0 read. RFC 9112 6.3 is explicit --
# a request whose final transfer coding is not `chunked` has a length the
# recipient cannot determine, so it must be refused and the connection closed --
# and http.server decodes no coding at all, `chunked` included. Every spelling in
# the neighbourhood is swept here because three prior rounds each fixed one value
# of a framing rule and left an adjacent one poisoning the socket.
#
# The over-close half of this contract is pinned by the framing table above and by
# `test_blank_rule_does_not_over_close_a_bodyless_request` /
# `test_digit_grammar_does_not_over_close_an_honest_zero`: a request with NO
# framing header, or one whose declared lengths all agree on zero, keeps its
# keep-alive. An absent `Transfer-Encoding` is the only one that frames nothing.


@pytest.mark.parametrize(
    "value",
    [
        "identity",
        "IDENTITY",
        "Identity",
        "identity ",
        "\tidentity\t",
        "\xa0identity",
        "identity\xa0",
        "\xa0identity\xa0",
        "\x85identity",
        "identity, chunked",
        "chunked, identity",
        "identity,identity",
        ",",
        "banana",
    ],
    ids=[
        "identity",
        "upper",
        "title",
        "sp-padded",
        "htab-padded",
        "nbsp-ahead",
        "nbsp-behind",
        "nbsp-both",
        "nel-ahead",
        "identity-then-chunked",
        "chunked-then-identity",
        "identity-list",
        "lone-comma",
        "unknown-coding",
    ],
)
def test_every_spelling_of_a_transfer_coding_declares_a_body(value):
    """Case, OWS, non-OWS padding, comma lists, a bare comma, an unknown coding.

    The ASCII `identity` spellings are the newly-closed hole (they normalized to
    the exempt token); the U+00A0/U+0085-padded ones were the previous round's
    (`str.strip()` erased the padding, so `\\xa0identity` read back as `identity`)
    and they reach the wire because a request line decodes as latin-1. The comma
    forms never matched the exempt token and already refused -- they are pinned so
    a future normalizer that splits the list cannot reopen the hole.
    """
    from api.helpers import request_declares_body, unsupported_transfer_encoding

    handler = _message_with(f"Host: x\r\nTransfer-Encoding: {value}\r\n")

    assert unsupported_transfer_encoding(cast(Any, handler)) is not None
    assert request_declares_body(cast(Any, handler)) is True


@pytest.mark.parametrize("length", ["0", "15"], ids=["zero-length", "real-length"])
def test_transfer_encoding_beside_a_content_length_still_declares_a_body(length):
    """A coding plus a length is unframeable however honest the length looks.

    `Transfer-Encoding: identity` with `Content-Length: 0` was the worst of the
    pair: both halves read as "no body", so the rejection kept the socket and the
    payload behind them was parsed as the next request line. RFC 9112 6.3 refuses
    the combination outright (it is the request-smuggling shape), and the coding
    has to win -- a length cannot describe a body the server cannot decode.
    """
    from api.helpers import request_declares_body

    handler = _message_with(
        f"Host: x\r\nTransfer-Encoding: identity\r\nContent-Length: {length}\r\n"
    )

    assert request_declares_body(cast(Any, handler)) is True


def test_csp_report_rate_limited_closes_connection(monkeypatch):
    """A rate-limited CSP report is dropped before its body is read."""
    import api.routes as routes

    handler = SimpleNamespace(close_connection=False)
    monkeypatch.setattr(routes, "_csp_report_rate_limited", lambda _handler: True)
    monkeypatch.setattr(routes, "_send_no_content", lambda _handler: True)

    assert routes._handle_csp_report(handler) is True
    assert handler.close_connection is True


def test_health_restart_closes_connection(monkeypatch):
    """health/restart never consumes its body on any outcome."""
    import api.routes as routes

    handler = SimpleNamespace(close_connection=False)
    monkeypatch.setattr(routes, "restart_active_profile_gateway", lambda: {"status": "completed"})
    monkeypatch.setattr(routes, "j", lambda _handler, _payload, status=200: True)

    routes._handle_health_restart(handler)
    assert handler.close_connection is True


def test_upload_oversize_rejects_before_read(monkeypatch):
    """An oversized upload's 413 fires before rfile.read() — connection closes."""
    import api.upload as upload

    handler = SimpleNamespace(
        headers={"Content-Type": "multipart/form-data; boundary=x", "Content-Length": "99999999999"},
        close_connection=False,
    )
    monkeypatch.setattr(upload, "j", lambda _handler, _payload, status=200: None)

    upload.handle_upload(handler)

    assert handler.close_connection is True


# ── Multipart framing preflight: chunked uploads must close, empty ones must not ─
#
# All four multipart handlers keyed on Content-Length alone, so a chunked upload
# (which has none) was read as length 0, matched no parts, and was rejected with
# "No file field in request" and keep-alive intact — leaving every chunk byte on
# the socket, which is the exact poisoning this PR exists to prevent.
# http.server never decodes chunked framing, so the only safe answer is 411 with
# the connection armed for close.

_MULTIPART_HANDLERS = [
    "handle_upload",
    "handle_upload_extract",
    "handle_transcribe",
    "handle_workspace_upload",
]


class _NeverRead:
    """An rfile that fails the test if a reject-before-read path reads the body."""

    def read(self, *_args):
        raise AssertionError("the body must not be read on a reject-before-read path")


def _run_multipart_handler(monkeypatch, handler_name, headers, rfile):
    import api.upload as upload

    answered: dict[str, Any] = {}

    def _record(_handler, payload, status=200):
        answered["payload"] = payload
        answered["status"] = status
        return True

    monkeypatch.setattr(upload, "j", _record)
    handler = SimpleNamespace(headers=headers, rfile=rfile, close_connection=False)

    getattr(upload, handler_name)(cast(Any, handler))
    return handler, answered


@pytest.mark.parametrize("handler_name", _MULTIPART_HANDLERS)
def test_chunked_multipart_rejected_before_read_closes_connection(handler_name, monkeypatch):
    handler, answered = _run_multipart_handler(
        monkeypatch,
        handler_name,
        {"Content-Type": "multipart/form-data; boundary=x", "Transfer-Encoding": "chunked"},
        _NeverRead(),
    )

    assert answered["status"] == 411, answered
    assert "Transfer-Encoding" in answered["payload"]["error"], answered
    assert handler.close_connection is True


@pytest.mark.parametrize("handler_name", _MULTIPART_HANDLERS)
def test_bodyless_multipart_rejection_keeps_connection(handler_name, monkeypatch):
    """A well-framed upload with no body is rejected with nothing left unread."""
    handler, answered = _run_multipart_handler(
        monkeypatch,
        handler_name,
        {"Content-Type": "multipart/form-data; boundary=x", "Content-Length": "0"},
        SimpleNamespace(read=lambda *_args: b""),
    )

    assert answered["status"] == 400, answered
    assert handler.close_connection is False


@pytest.mark.parametrize("handler_name", _MULTIPART_HANDLERS)
def test_non_numeric_content_length_still_closes(handler_name, monkeypatch):
    """The pre-existing invalid-Content-Length reject keeps its 400 and its close."""
    handler, answered = _run_multipart_handler(
        monkeypatch,
        handler_name,
        {"Content-Type": "multipart/form-data; boundary=x", "Content-Length": "banana"},
        _NeverRead(),
    )

    assert answered["status"] == 400, answered
    assert answered["payload"]["error"] == "Invalid Content-Length", answered
    assert handler.close_connection is True


@pytest.mark.parametrize("handler_name", _MULTIPART_HANDLERS)
def test_duplicate_content_length_multipart_rejects_before_read(handler_name, monkeypatch):
    """Disagreeing lengths give no honest byte count -- refuse without reading.

    Reading the first value (0) made the body invisible: the handler answered
    "No file field in request" with keep-alive and left the real multipart bytes
    on the socket, where the server parsed them as the next request line.
    """
    from email.parser import Parser

    headers = Parser().parsestr(
        "Content-Type: multipart/form-data; boundary=x\r\n"
        "Content-Length: 0\r\n"
        "Content-Length: 88\r\n\r\n"
    )
    handler, answered = _run_multipart_handler(monkeypatch, handler_name, headers, _NeverRead())

    assert answered["status"] == 400, answered
    assert answered["payload"]["error"] == "Invalid Content-Length", answered
    assert handler.close_connection is True


@pytest.mark.parametrize("handler_name", _MULTIPART_HANDLERS)
@pytest.mark.parametrize(
    "raw",
    ["Content-Length:", "Content-Length:   ", "Content-Length:\r\nContent-Length: 63"],
    ids=["blank", "whitespace-only", "blank-plus-real"],
)
def test_blank_content_length_multipart_rejects_before_read(handler_name, raw, monkeypatch):
    """A blank length reached `int('' or 0)` == 0 and read the body as empty.

    All four handlers then answered 400 "No file field in request" with keep-alive
    and left the whole multipart payload on the socket. Verified live on the
    otherwise-fixed head: 400 then `400 Bad request syntax ('--x')`.
    """
    from email.parser import Parser

    headers = Parser().parsestr(
        "Content-Type: multipart/form-data; boundary=x\r\n" + raw + "\r\n\r\n"
    )
    handler, answered = _run_multipart_handler(monkeypatch, handler_name, headers, _NeverRead())

    assert answered["status"] == 400, answered
    assert answered["payload"]["error"] == "Invalid Content-Length", answered
    assert handler.close_connection is True


@pytest.mark.parametrize("handler_name", _MULTIPART_HANDLERS)
@pytest.mark.parametrize("value", ["+0", "0_0", "\xa00", "٠"], ids=["sign", "underscore", "nbsp", "arabic-indic"])
def test_malformed_zero_content_length_multipart_rejects_before_read(
    handler_name, value, monkeypatch
):
    """A length only `int()` calls zero slipped the gate into `int(...)` == 0.

    All four handlers then read an empty body, answered 400 "No file field in
    request" with keep-alive, and left the whole payload on the socket. Live on
    the otherwise-fixed head: 400 then `400 Bad request syntax ('--x')`.
    """
    from email.parser import Parser

    headers = Parser().parsestr(
        f"Content-Type: multipart/form-data; boundary=x\r\nContent-Length: {value}\r\n\r\n"
    )
    handler, answered = _run_multipart_handler(monkeypatch, handler_name, headers, _NeverRead())

    assert answered["status"] == 400, answered
    assert answered["payload"]["error"] == "Invalid Content-Length", answered
    assert handler.close_connection is True


@pytest.mark.parametrize("handler_name", _MULTIPART_HANDLERS)
@pytest.mark.parametrize(
    "raw",
    [
        "Transfer-Encoding: identity",
        "Transfer-Encoding: IDENTITY ",
        "Transfer-Encoding: \xa0identity",
        "Transfer-Encoding:",
        "Transfer-Encoding: identity\r\nContent-Length: 0",
        "Transfer-Encoding: identity\r\nContent-Length: 88",
        "Transfer-Encoding: banana",
    ],
    ids=[
        "identity",
        "identity-cased-and-padded",
        "nbsp-padded-identity",
        "blank",
        "identity-plus-zero-length",
        "identity-plus-real-length",
        "unknown-coding",
    ],
)
def test_any_transfer_encoding_multipart_rejects_before_read(handler_name, raw, monkeypatch):
    """No coding is decodable by an upload handler, so none may reach `rfile`.

    `identity` (any case, any OWS padding) was exempt as a coding that "frames
    nothing", so each handler fell through to the length-0 read, answered 400 "No
    file field in request" with keep-alive and left the whole multipart payload on
    the socket (live: 400 then `400 Bad request syntax ('--x')`). The `\\xa0`-padded
    spelling and the blank value are the previous round's holes, and the two
    Content-Length pairings are the RFC 9112 6.3 smuggling shape -- the coding has
    to win over any length beside it. `_NeverRead` proves the preflight answers
    before a byte is consumed.
    """
    from email.parser import Parser

    headers = Parser().parsestr(
        "Content-Type: multipart/form-data; boundary=x\r\n" + raw + "\r\n\r\n"
    )
    handler, answered = _run_multipart_handler(monkeypatch, handler_name, headers, _NeverRead())

    assert answered["status"] == 411, answered
    assert "Transfer-Encoding" in answered["payload"]["error"], answered
    assert handler.close_connection is True


# ── Header serialization: exactly one Connection: close, never a crash ────────
#
# The advertisement lives in Handler.end_headers() via
# api.helpers.advertise_connection_close(). Two things went wrong there and
# both are pinned below:
#
#   1. The first cut keyed dedup off a private `_close_advertised` flag it set
#      itself. Every SSE endpoint already sends its own `Connection: close`
#      (which BaseHTTPRequestHandler.send_header turns into
#      close_connection=True), so end_headers() appended a SECOND copy to every
#      stream response.
#   2. It read `self.close_connection` unguarded. That attribute only exists
#      once a request line has been parsed, so the many suite handlers built
#      with `Handler.__new__(Handler)` raised AttributeError instead of
#      emitting headers.


def _render_response_headers(prepare=None) -> str:
    """Drive the production Handler.end_headers() into a byte buffer."""
    import io

    from server import Handler

    handler = Handler.__new__(Handler)
    handler.request_version = "HTTP/1.1"
    handler.close_connection = False
    handler.wfile = io.BytesIO()
    handler._headers_buffer = []
    if prepare is not None:
        prepare(handler)
    Handler.end_headers(handler)
    return handler.wfile.getvalue().decode("latin-1")


def test_armed_close_is_advertised_exactly_once():
    """A reject-before-read must TELL the client the socket is gone."""
    from api.helpers import arm_connection_close

    rendered = _render_response_headers(arm_connection_close)

    assert rendered.count("Connection: close") == 1, rendered


def test_sse_style_explicit_close_is_not_doubled():
    """SSE sends its own Connection: close; end_headers() must not repeat it.

    send_header('Connection', 'close') flips close_connection inside the
    stdlib, so a self-keyed dedup flag would emit the header twice here.
    """

    def prepare(handler):
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Connection", "close")

    rendered = _render_response_headers(prepare)

    assert rendered.count("Connection: close") == 1, rendered


def test_keep_alive_response_advertises_no_close():
    """An ordinary response must keep its keep-alive."""
    rendered = _render_response_headers(
        lambda handler: handler.send_header("Content-Type", "application/json")
    )

    assert "Connection: close" not in rendered, rendered


def test_end_headers_on_handler_stub_without_close_connection(monkeypatch):
    """A partially-built handler stub has no close_connection — don't raise.

    Much of the suite builds handlers with `Handler.__new__(Handler)` and never
    parses a request, so the attribute is simply absent. Reading it unguarded
    turned every such test into an AttributeError.
    """
    from http.server import BaseHTTPRequestHandler

    from server import Handler

    sent: list[tuple[str, str]] = []
    handler = Handler.__new__(Handler)
    handler.send_header = lambda key, value: sent.append((key, value))
    monkeypatch.setattr(BaseHTTPRequestHandler, "end_headers", lambda self: None)

    assert not hasattr(handler, "close_connection")
    Handler.end_headers(handler)  # must not raise

    assert "Content-Security-Policy-Report-Only" in dict(sent)
    assert "Connection" not in dict(sent), "nothing was armed, so nothing to advertise"


def test_advertise_close_tolerates_a_non_iterable_header_buffer():
    """The dedup scan must be as defensive as the two attribute reads.

    A bare ``Mock`` answers every attribute, so ``_headers_buffer`` comes back
    truthy but non-iterable. Iterating it unguarded raised
    ``TypeError: 'Mock' object is not iterable`` from inside ``end_headers()``,
    which ``_handle_write``'s generic ``except`` turns into a spurious 500 —
    the same crash-inside-end_headers class as the missing
    ``close_connection`` guard above.
    """
    from unittest.mock import Mock

    from api.helpers import advertise_connection_close

    handler = Mock()
    advertise_connection_close(handler)  # must not raise

    handler.send_header.assert_called_once_with("Connection", "close")


def test_check_auth_or_close_on_handler_stub_without_close_connection(monkeypatch):
    """check_auth_or_close must not invent close_connection on a stub.

    Arming a handler that never had the attribute would leave a bogus
    keep-alive verdict behind, and reading it unguarded raised AttributeError
    (which _handle_write then converted into a spurious 500).
    """
    import api.auth as auth
    from server import Handler

    handler = Handler.__new__(Handler)
    monkeypatch.setattr(auth, "check_auth", lambda _handler, _parsed: True)

    assert auth.check_auth_or_close(handler, SimpleNamespace(path="/api/x")) is True
    assert not hasattr(handler, "close_connection")


def test_check_auth_or_close_restores_keep_alive_on_success(monkeypatch):
    """A successful auth must not inherit the armed close."""
    import api.auth as auth

    handler = SimpleNamespace(close_connection=False)
    monkeypatch.setattr(auth, "check_auth", lambda _handler, _parsed: True)

    assert auth.check_auth_or_close(cast(Any, handler), SimpleNamespace(path="/api/x")) is True
    assert handler.close_connection is False


def test_arm_connection_close_tolerates_a_read_only_handler():
    """Arming must never turn a 4xx into a 500, even on an odd handler."""
    from api.helpers import arm_connection_close

    class Frozen:
        __slots__ = ()

    arm_connection_close(Frozen())  # must not raise
