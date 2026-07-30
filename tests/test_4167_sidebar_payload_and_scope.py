"""Regression tests for #4167 sidebar payload and scope guards."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ROOT = Path(__file__).resolve().parent.parent


def test_sidebar_allowlist_preserves_read_only_and_gateway_routing():
    import api.routes as routes

    allow = routes._SIDEBAR_SESSION_RESPONSE_FIELDS
    assert "read_only" in allow, "read_only dropped -> read-only sessions render writable"
    assert "is_read_only" in allow, "is_read_only dropped -> read-only detection misses"
    assert "gateway_routing" in allow, "gateway_routing dropped -> sidebar model label degrades"


def test_sidebar_response_item_preserves_read_only_flag():
    """_sidebar_session_response_item() must carry read_only through."""
    import api.routes as routes

    fn = getattr(routes, "_sidebar_session_response_item", None)
    if fn is None:
        assert "read_only" in routes._SIDEBAR_SESSION_RESPONSE_FIELDS
        return
    row = fn({
        "session_id": "s1",
        "title": "t",
        "read_only": True,
        "gateway_routing": {"provider": "anthropic", "model": "claude"},
        "not_allowed_field": "x",
    })
    assert row.get("read_only") is True
    assert row.get("gateway_routing") == {"provider": "anthropic", "model": "claude"}
    assert "not_allowed_field" not in row


def test_failed_refresh_clears_cache_on_profile_scope_change():
    """The catch path must reject cached rows from a mismatched sidebar scope."""
    src = (_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    assert "_allSessionsScope" in src, "cache is not scope-tagged"
    assert re.search(r"_scopeMatches", src), "catch path does not gate fallback on scope match"
    assert re.search(r"_allSessions\s*=\s*\[\]", src), "catch path does not clear stale rows on scope mismatch"
    assert "excludeHidden: _sessionListExcludeHiddenEnabled()" in src, (
        "scope tagging must include the hidden-filter query mode"
    )
    assert "_allSessionsScope.excludeHidden === _curScope.excludeHidden" in src, (
        "catch path must reject cached rows fetched under the wrong hidden-filter mode"
    )
