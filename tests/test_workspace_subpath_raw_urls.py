"""
Tests for the workspace panel's raw file URLs under a subpath mount.

`_workspaceRouteForPath` builds app-relative "/api/…" strings. Most callers
pass them through `api()`, which strips the leading slash and re-resolves the
URL against `document.baseURI`, so they work under a subpath mount like
`/hermes/`. But several callers use the route DIRECTLY, bypassing `api()`:

  * `previewImg.src`   (image preview)
  * media / pdf / html frame `.src`
  * the download `<a href>` anchor
  * `window.open(...)`   (open-in-browser)

For those, a bare "/api/file/raw?…" resolves to the SERVER ROOT under a subpath
mount and 404s — image previews break and every download fails, while text
previews (which go through api()) keep working. The fix resolves the route
against `document.baseURI` inside `_workspaceRouteForPath` itself.

The first two tests are static source regressions; the third runs the builder
in Node with a subpath `document.baseURI` and asserts the resolved URL keeps
the mount prefix.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
WORKSPACE_JS = ROOT / "static" / "workspace.js"
NODE = shutil.which("node")


def _workspace_js() -> str:
    return WORKSPACE_JS.read_text(encoding="utf-8")


def _route_helper_block() -> str:
    """Slice the workspace-route builders out of workspace.js for a Node harness."""
    src = _workspace_js()
    start = src.find("function _escapeGrantStore(){")
    assert start >= 0, "route helper block start not found in static/workspace.js"
    end = src.find("async function authorizeWorkspaceEscapeNavigation(", start)
    assert end >= 0, "route helper block end not found in static/workspace.js"
    return src[start:end]


class TestWorkspaceSubpathRawUrls:
    def test_route_builder_resolves_against_base_uri(self):
        """_workspaceRouteForPath must resolve routes against document.baseURI."""
        src = _workspace_js()
        assert "document.baseURI" in src, (
            "workspace.js must resolve raw workspace routes against document.baseURI "
            "so previewImg.src / downloads / window.open work under a subpath mount"
        )

    def test_route_builder_keeps_app_relative_strings_internally(self):
        """The literal '/api/...' route strings must remain in _workspaceRouteForPathRel."""
        src = _workspace_js()
        assert "/api/file/raw?session_id=" in src, (
            "the app-relative /api/file/raw route string must remain as the internal "
            "building block inside _workspaceRouteForPathRel (used when document is absent)"
        )

    @pytest.mark.skipif(NODE is None, reason="node not on PATH")
    def test_raw_url_keeps_subpath_prefix(self):
        helper_block = _route_helper_block()
        js = (
            "const helperBlock = "
            + json.dumps(helper_block)
            + ";\n"
            + r"""
global.document = { baseURI: 'https://host.example/pod-123/hermes/' };
const S = { session: { session_id: 'sess-1' }, currentDir: '.', _escapeGrants: Object.create(null) };
const runner = new Function(
  'S', 'URLSearchParams', 'document',
  helperBlock + '; return { _workspaceRouteForPath };'
);
const fns = runner(S, URLSearchParams, global.document);
console.log(JSON.stringify({
  raw: fns._workspaceRouteForPath('sub/dir/plot.png', 'raw', { download: true }),
  img: fns._workspaceRouteForPath('sub/dir/plot.png', 'raw'),
  read: fns._workspaceRouteForPath('sub/dir/plot.png', 'read'),
}));
"""
        )
        out = subprocess.run(
            [NODE, "-e", js], cwd=ROOT, capture_output=True, text=True, check=True
        )
        result = json.loads(out.stdout)
        prefix = "https://host.example/pod-123/hermes/api/"
        assert result["raw"].startswith(prefix), result["raw"]
        assert "download=1" in result["raw"], result["raw"]
        assert result["img"].startswith(prefix), result["img"]
        # Routes normally consumed via api() are still absolute now, which api()
        # tolerates (it strips the leading slash of relative inputs only).
        assert result["read"].startswith(prefix), result["read"]
        # The mount prefix must not be duplicated or dropped.
        assert result["raw"].count("/hermes/") == 1, result["raw"]

    @pytest.mark.skipif(NODE is None, reason="node not on PATH")
    def test_non_http_base_falls_back_to_app_relative(self):
        """about:blank (jsdom's default baseURI) must not crash the builder."""
        helper_block = _route_helper_block()
        js = (
            "const helperBlock = "
            + json.dumps(helper_block)
            + ";\n"
            + r"""
global.document = { baseURI: 'about:blank' };
const S = { session: { session_id: 'sess-1' }, currentDir: '.', _escapeGrants: Object.create(null) };
const runner = new Function(
  'S', 'URLSearchParams', 'document',
  helperBlock + '; return { _workspaceRouteForPath };'
);
const fns = runner(S, URLSearchParams, global.document);
console.log(JSON.stringify({
  raw: fns._workspaceRouteForPath('sub/dir/plot.png', 'raw', { download: true }),
}));
"""
        )
        out = subprocess.run(
            [NODE, "-e", js], cwd=ROOT, capture_output=True, text=True, check=True
        )
        result = json.loads(out.stdout)
        # Non-http(s) base: keep the app-relative form rather than throwing.
        assert result["raw"].startswith("/api/file/raw?session_id="), result["raw"]
