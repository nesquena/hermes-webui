from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.auth import parse_cookie


@pytest.fixture(autouse=True)
def default_cookie_name(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_COOKIE_NAME", raising=False)


def _handler(cookie_header: str):
    return SimpleNamespace(headers={"Cookie": cookie_header})


def test_cookie_we_do_not_own_does_not_hide_ours():
    # `__sec_id` as seen live: raw JSON in the value, illegal cookie octets. The whole-header
    # parse used to drop every cookie after it, so the session cookie read as absent.
    header = '; '.join(
        [
            '__sec_id={"username":"","type":"email","firstname":""}',
            "authelia_session=abc123",
            "hermes_session=signed.sig",
        ]
    )
    assert parse_cookie(_handler(header)) == "signed.sig"


def test_malformed_only_header_returns_none():
    assert parse_cookie(_handler('__sec_id={"a":"b"}; _ga=quoted"quote')) is None


def test_no_cookie_header_returns_none():
    assert parse_cookie(_handler("")) is None


def test_missing_session_cookie_returns_none():
    assert parse_cookie(_handler("authelia_session=abc123; theme=dark")) is None


def test_duplicate_session_cookies_keep_the_last():
    # unchanged from the whole-header behaviour: newest (last) cookie wins
    assert parse_cookie(_handler("hermes_session=old.sig; hermes_session=new.sig")) == "new.sig"


def test_quoted_value_still_decoded():
    assert parse_cookie(_handler('hermes_session="sp ace"')) == "sp ace"


def test_rfc6265_semicolon_splits_even_inside_quotes():
    """RFC 6265 4.2.1: a quoted cookie-value cannot contain `;`, so `;` separates cookies
    regardless of quotes — a token sitting inside another cookie's quotes is just another
    pair, and the last match wins. Documents the intended semantics (no quoting layer)."""
    header = 'hermes_session=valid.sig; other="x; hermes_session=invalid.sig; y"'
    assert parse_cookie(_handler(header)) == "invalid.sig"
