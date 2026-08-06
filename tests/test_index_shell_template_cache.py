"""Regression tests for the app-shell template cache (_render_index_shell_base).

The `/`, `/index.html`, and `/session/<id>` routes are the hottest navigations.
Each previously re-read the ~190 KB static/index.html from disk and re-ran the
two process-constant token substitutions (__WEBUI_VERSION__,
__MAX_UPLOAD_BYTES__) on every request. _render_index_shell_base() caches the
partially rendered template, keyed by (size, mtime_ns) like _STATIC_CACHE, while
the per-session CSRF token and per-request extension tags stay per-request.

These tests guard the load-bearing invariant: caching must change no observable
output, only avoid redundant work.
"""
from __future__ import annotations

import json
import os
import time
from urllib.parse import quote

import api.config as api_config
import api.routes as routes
from api.updates import WEBUI_VERSION


def _old_inline_render(csrf_token: str) -> str:
    """Reproduce the pre-cache inline render exactly, for equivalence checks."""
    index_path = api_config.get_index_html_path()
    return (
        index_path.read_text(encoding="utf-8")
        .replace("__WEBUI_VERSION__", quote(WEBUI_VERSION, safe=""))
        .replace("__MAX_UPLOAD_BYTES__", str(routes.MAX_UPLOAD_BYTES))
        .replace("__CSRF_TOKEN_JSON__", json.dumps(csrf_token))
    )


def test_base_substitutes_process_constants_but_not_csrf():
    base = routes._render_index_shell_base()
    assert "__WEBUI_VERSION__" not in base
    assert "__MAX_UPLOAD_BYTES__" not in base
    # CSRF must remain a placeholder — it varies per request and is applied by
    # the caller, not baked into the shared cache.
    assert "__CSRF_TOKEN_JSON__" in base


def test_cached_render_is_byte_identical_to_old_inline():
    csrf = "test-csrf-abc123"
    new = routes._render_index_shell_base().replace(
        "__CSRF_TOKEN_JSON__", json.dumps(csrf)
    )
    assert new == _old_inline_render(csrf)


def test_csrf_token_varies_per_request():
    a = routes._render_index_shell_base().replace(
        "__CSRF_TOKEN_JSON__", json.dumps("AAA")
    )
    b = routes._render_index_shell_base().replace(
        "__CSRF_TOKEN_JSON__", json.dumps("BBB")
    )
    assert json.dumps("AAA") in a
    assert json.dumps("BBB") in b
    assert a != b


def test_second_call_returns_cached_object():
    # Warm the cache, then assert the identical object comes back (no re-read).
    first = routes._render_index_shell_base()
    second = routes._render_index_shell_base()
    assert first is second


def test_cache_invalidates_on_mtime_change(tmp_path, monkeypatch):
    # Point the module at a temp copy so we can mutate its mtime safely.
    src = api_config.get_index_html_path().read_text(encoding="utf-8")
    fake = tmp_path / "index.html"
    fake.write_text(src, encoding="utf-8")
    monkeypatch.setattr(api_config, "get_index_html_path", lambda: fake)
    # Reset the shared cache so this test is order-independent.
    monkeypatch.setattr(routes, "_INDEX_SHELL_CACHE", {})

    routes._render_index_shell_base()
    sig_before = routes._INDEX_SHELL_CACHE["base"][0]

    future = time.time() + 5
    os.utime(fake, (future, future))
    routes._render_index_shell_base()
    sig_after = routes._INDEX_SHELL_CACHE["base"][0]

    assert sig_after != sig_before


def test_cache_invalidates_on_index_path_change(tmp_path, monkeypatch):
    first = tmp_path / "first.html"
    second = tmp_path / "second.html"
    first.write_text("alpha __WEBUI_VERSION__ __MAX_UPLOAD_BYTES__ __CSRF_TOKEN_JSON__", encoding="utf-8")
    second.write_text("bravo __WEBUI_VERSION__ __MAX_UPLOAD_BYTES__ __CSRF_TOKEN_JSON__", encoding="utf-8")
    stamp = time.time() + 5
    os.utime(first, (stamp, stamp))
    os.utime(second, (stamp, stamp))
    selected = {"path": first}
    monkeypatch.setattr(api_config, "get_index_html_path", lambda: selected["path"])
    monkeypatch.setattr(routes, "_INDEX_SHELL_CACHE", {})

    assert "alpha" in routes._render_index_shell_base()
    selected["path"] = second
    rendered = routes._render_index_shell_base()

    assert "bravo" in rendered
    assert "alpha" not in rendered
