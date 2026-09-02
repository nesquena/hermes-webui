"""Regression coverage for the static-shell cache-bust token.

The token is derived from bundle (name, size, mtime_ns) and must change when
any static shell asset changes, even when WEBUI_VERSION is a constant string
(non-git installs). Without it, the service-worker cache name never changes
and stale bundles survive indefinitely; hard refresh does not bypass
service-worker caches.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def asset_token():
    """Return the real _assets_cache_bust_token bound to this worktree."""
    from api import routes

    return routes._assets_cache_bust_token


def test_token_does_not_raise_on_missing_static_root(tmp_path, asset_token):
    """Fail-soft: a missing or empty static root returns a string, not an error."""
    token = asset_token(tmp_path / "does-not-exist")
    assert isinstance(token, str)
    assert token != ""


def test_token_changes_when_bundle_mtime_changes(tmp_path, asset_token):
    """A bundle edit (mtime change) must produce a different cache token."""
    static_root = tmp_path / "static"
    static_root.mkdir()
    bundle = static_root / "ui.js"
    bundle.write_text("console.log(1)", encoding="utf-8")
    first = asset_token(static_root)
    time.sleep(0.02)
    bundle.touch()
    second = asset_token(static_root)
    assert first != second, "token must change when a bundle file changes"


def test_token_carries_fingerprint_suffix(tmp_path, asset_token):
    """The token must differ from the bare constant version value."""
    static_root = tmp_path / "static"
    static_root.mkdir()
    (static_root / "ui.js").write_text("x", encoding="utf-8")
    (static_root / "index.html").write_text("<html></html>", encoding="utf-8")
    token = asset_token(static_root)
    assert "%2Ba" in token, "token must carry the asset fingerprint suffix (URL-encoded)"
    assert token != "unknown", "token must not be the bare constant version"


def test_sw_route_uses_bundle_fingerprint_token():
    """Source anchor: the /sw.js route must derive its cache name from the token."""
    routes_src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    idx = routes_src.find('"/sw.js"')
    assert idx != -1
    block = routes_src[idx:idx + 1000]
    assert "_assets_cache_bust_token(static_root)" in block, (
        "sw.js route must derive its cache name from the bundle fingerprint"
    )
