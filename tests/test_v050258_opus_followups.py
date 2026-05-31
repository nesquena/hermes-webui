"""Regression tests for v0.50.258 Opus pre-release follow-up.

PR #1419 introduced server-side `?next=` redirect after session expiry. The
initial implementation built the outer `next` parameter via:

    _next = quote(path, safe='/:@!$&\'()*+,;=')
    if query:
        _next += '?' + query
    location = 'login?next=' + quote(_next, safe='/:@!$&\'()*+,;=?')

Two problems with this shape:

1. The inner `?` was kept literal because both `quote()` calls had `?` in
   their `safe` set. Combined with `&` also being kept literal, paths with
   multi-param queries (`/api/sessions?limit=50&offset=0`) round-tripped as
   `/api/sessions?limit=50` — the rest got eaten as a top-level outer query
   parameter the login page ignored.

2. Attacker-controlled paths with embedded `&next=...` could inject a second
   top-level `next` parameter. Browsers' URLSearchParams.get() returns the
   first-match (safe), Python's parse_qs returns last-match (unsafe). The
   downstream `_safeNextPath()` rejects non-`/` prefixes which closed the
   actual exploit, but the parser-divergence is a footgun.

Fix: percent-encode the entire `path?query` blob with `safe='/'`, so `?`,
`&`, `=` all get encoded. The outer `next` then holds exactly one
path-with-query string that the browser auto-decodes once.
"""

from __future__ import annotations

import urllib.parse as _urlparse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


# ── 1: source-level pin: redirect uses safe='/' encoding ────────────────────


def test_login_redirect_uses_path_only_safe_encoding():
    """The check_auth redirect must encode `?` and `&` so multi-param query
    strings round-trip correctly. Negative pattern guards revert to the
    original `safe='/:@!$&\'()*+,;=?'` shape."""
    src = (REPO / "api" / "auth.py").read_text(encoding="utf-8")

    redirect_idx = src.find("login?next=")
    assert redirect_idx != -1, "login redirect missing"
    block = src[max(0, redirect_idx - 1200) : redirect_idx + 600]

    # Must use safe='/' (path-separators only).
    assert "safe='/'" in block, (
        "check_auth must encode `?` and `&` in the next= parameter so multi-param "
        "query strings round-trip. safe='/' is the correct encoding shape."
    )

    # Must NOT use the broad safe set that keeps `?` and `&` literal.
    assert "safe='/:@!$&\\'()*+,;=?'" not in block, (
        "check_auth must not use the broad safe='/:@!$&\\'()*+,;=?' encoding — "
        "that keeps `?` and `&` literal, which truncates multi-param queries "
        "and creates a parser-divergence footgun."
    )


# ── 2: behavioral round-trip ────────────────────────────────────────────────


def _build_redirect_like_check_auth(path: str, query: str) -> str:
    """Mirror api.auth.check_auth's redirect construction so we can assert
    the round-trip without spinning up a server."""
    _path_with_query = path or "/"
    if query:
        _path_with_query += "?" + query
    _next = _urlparse.quote(_path_with_query, safe="/")
    return "login?next=" + _next


def _browser_searchparams_get_next(location: str) -> str:
    """Mirror the browser's URLSearchParams.get('next') behaviour."""
    parsed = _urlparse.urlparse("https://host" + location)
    qs = _urlparse.parse_qs(parsed.query, keep_blank_values=True)
    values = qs.get("next", [])
    return values[0] if values else None


def test_redirect_roundtrip_simple_path():
    location = _build_redirect_like_check_auth("/foo/bar", "")
    assert _browser_searchparams_get_next(location) == "/foo/bar"


def test_redirect_roundtrip_single_query_param():
    location = _build_redirect_like_check_auth("/foo/bar", "baz=qux")
    assert _browser_searchparams_get_next(location) == "/foo/bar?baz=qux"


def test_redirect_roundtrip_multi_query_params():
    """REGRESSION: pre-fix, this round-tripped to `/api/sessions?limit=50`
    (offset got eaten as a top-level outer query)."""
    location = _build_redirect_like_check_auth("/api/sessions", "limit=50&offset=0")
    got = _browser_searchparams_get_next(location)
    assert got == "/api/sessions?limit=50&offset=0", (
        f"multi-param query round-trip broken: got {got!r}, expected the full string"
    )


def test_redirect_roundtrip_attacker_controlled_next_injection_neutralized():
    """REGRESSION: pre-fix, an attacker-controlled `&next=https://evil.com`
    in the source query injected a second top-level `next` parameter.
    Browsers parse first-match (benign), Python parses last-match (the evil
    value) — parser-divergence footgun even if downstream guards reject it."""
    location = _build_redirect_like_check_auth(
        "/admin", "action=foo&next=https://evil.com"
    )
    got = _browser_searchparams_get_next(location)
    # The entire string is preserved as the FIRST `next` value.
    assert got == "/admin?action=foo&next=https://evil.com"
    # And there is exactly ONE top-level `next` parameter.
    parsed = _urlparse.urlparse("https://host" + location)
    qs = _urlparse.parse_qs(parsed.query, keep_blank_values=True)
    assert len(qs.get("next", [])) == 1, (
        f"expected exactly one top-level `next` parameter, got {qs.get('next')}"
    )
    # _safeNextPath() in login.js (charAt(0)==='/' and charAt(1)!=='/') would
    # accept this as a valid same-origin path. The /admin page receives the
    # benign embedded query and the evil URL never becomes a redirect target.


def test_redirect_session_ttl_30_days():
    """Pin the SESSION_TTL constant to the 30-day value introduced by #1419."""
    src = (REPO / "api" / "auth.py").read_text(encoding="utf-8")
    assert "SESSION_TTL = 86400 * 30" in src, (
        "SESSION_TTL must be 30 days (86400 * 30) per #1419. Reverting to "
        "24h would re-introduce the daily-kick-out UX regression."
    )
