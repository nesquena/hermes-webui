"""Regression tests for #5428 — stale WebUI client recovery.

Source-contract tests verify the static files contain the expected anchors.
Node-backed behavioral tests exercise the extracted helpers with a stub DOM.
"""
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
INDEX_HTML = ROOT / "static" / "index.html"
PANELS_JS = ROOT / "static" / "panels.js"
BOOT_JS = ROOT / "static" / "boot.js"


# ---------------------------------------------------------------------------
# Source-contract tests
# ---------------------------------------------------------------------------

def test_index_html_stamps_bundle_version():
    """index.html stamps window.__HERMES_WEBUI_BUNDLE_VERSION__ before deferred scripts."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "window.__HERMES_WEBUI_BUNDLE_VERSION__='__WEBUI_VERSION__';" in html


def test_index_html_has_stale_client_banner():
    """index.html contains the stale-client banner and hard-refresh button."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="staleClientBanner"' in html
    assert 'id="staleClientMessage"' in html
    assert 'id="staleClientVersions"' in html
    assert 'id="btnStaleClientRefresh"' in html
    # Verify the button in the banner calls hardRefreshWebUIClient()
    banner_start = html.index('id="staleClientBanner"')
    segment = html[banner_start : banner_start + 800]
    assert "hardRefreshWebUIClient()" in segment, (
        "btnStaleClientRefresh is not wired to hardRefreshWebUIClient() inside the banner"
    )


def test_boot_js_calls_check_webui_version_skew():
    """boot.js calls checkWebUIVersionSkew(s) after the successful boot settings response."""
    src = BOOT_JS.read_text(encoding="utf-8")
    assert "checkWebUIVersionSkew(s)" in src


def test_panels_js_load_settings_calls_check_webui_version_skew():
    """panels.js calls checkWebUIVersionSkew(settings) from loadSettingsPanel()."""
    src = PANELS_JS.read_text(encoding="utf-8")
    pattern = r"async function loadSettingsPanel\(\)[\s\S]{0,2000}?checkWebUIVersionSkew\(settings\)"
    assert re.search(pattern, src), (
        "checkWebUIVersionSkew(settings) not found within loadSettingsPanel()"
    )


# ---------------------------------------------------------------------------
# Node-backed behavioral harness
# ---------------------------------------------------------------------------

def _extract_skew_helpers(panels_src: str) -> str:
    """Return the version-skew helper block (from _normalizeWebUIVersion to Kanban helpers)."""
    start = panels_src.index("function _normalizeWebUIVersion(")
    end = panels_src.index("function _kanbanLooksLikeStaleClientError(")
    return panels_src[start:end]


def _make_stub(bundle_version: str, extra_globals: str = "") -> str:
    """Return the Node.js preamble that stubs the browser globals."""
    return textwrap.dedent(f"""\
        'use strict';
        const _bannerEl = {{ style: {{ display: '' }} }};
        const _msgEl = {{ _text: '' }};
        const _verEl = {{ _text: '' }};
        Object.defineProperty(_msgEl, 'textContent', {{
            set(v) {{ _msgEl._text = v; }}, get() {{ return _msgEl._text; }}
        }});
        Object.defineProperty(_verEl, 'textContent', {{
            set(v) {{ _verEl._text = v; }}, get() {{ return _verEl._text; }}
        }});
        global.document = {{
            getElementById(id) {{
                if (id === 'staleClientBanner') return _bannerEl;
                if (id === 'staleClientMessage') return _msgEl;
                if (id === 'staleClientVersions') return _verEl;
                return null;
            }},
            addEventListener() {{}},
            hidden: false,
        }};
        global.window = {{
            __HERMES_WEBUI_BUNDLE_VERSION__: {json.dumps(bundle_version)},
            addEventListener() {{}},
        }};
        global.api = () => Promise.resolve({{}});
        {extra_globals}
    """)


_CAPTURE_AND_EXIT = textwrap.dedent("""\
    const _result = {
        display: _bannerEl.style.display,
        message: _msgEl._text,
        versions: _verEl._text,
    };
    process.stdout.write(JSON.stringify(_result) + '\\n');
    process.exit(0);
""")


def _run_harness(stub: str, helpers: str, action: str) -> dict:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for browser helper harness tests")
    script = (
        stub
        + "\n"
        + helpers
        + "\nPromise.resolve().then(async () => {\n"
        + action
        + "\n}).then(() => {\n"
        + _CAPTURE_AND_EXIT
        + "\n}).catch((err) => { console.error(err && err.stack || err); process.exit(1); });"
    )
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, f"node exit {proc.returncode}: {proc.stderr[:500]}"
    return json.loads(proc.stdout.strip())


# ---------------------------------------------------------------------------
# Behavioral tests — positive (mismatch triggers banner)
# ---------------------------------------------------------------------------

def test_node_mismatch_shows_banner():
    """client=v1, server=v2 reveals staleClientBanner and writes both versions."""
    helpers = _extract_skew_helpers(PANELS_JS.read_text(encoding="utf-8"))
    result = _run_harness(
        _make_stub("v1"),
        helpers,
        'checkWebUIVersionSkew({ webui_version: "v2" });',
    )
    assert result["display"] == "flex", f"Banner not shown on mismatch: {result}"
    assert "v1" in result["versions"] and "v2" in result["versions"], (
        f"Version text missing client+server: {result['versions']!r}"
    )


# ---------------------------------------------------------------------------
# Behavioral tests — negative space (no false-positive fires)
# ---------------------------------------------------------------------------

def test_node_equal_versions_no_banner():
    """client=v1, server=v1 keeps banner hidden."""
    helpers = _extract_skew_helpers(PANELS_JS.read_text(encoding="utf-8"))
    result = _run_harness(
        _make_stub("v1"),
        helpers,
        'checkWebUIVersionSkew({ webui_version: "v1" });',
    )
    assert result["display"] != "flex", f"Banner wrongly shown for equal versions: {result}"


def test_node_missing_server_version_no_banner():
    """Missing webui_version in settings keeps banner hidden."""
    helpers = _extract_skew_helpers(PANELS_JS.read_text(encoding="utf-8"))
    result = _run_harness(
        _make_stub("v1"),
        helpers,
        "checkWebUIVersionSkew({});",
    )
    assert result["display"] != "flex", f"Banner wrongly shown for missing server version: {result}"


def test_node_unknown_server_version_no_banner():
    """A server that reports webui_version='unknown' (git-describe failure in a
    Docker/CI image, api/updates.py) must NOT falsely fire the stale-client
    banner against a real client version. (Codex #5480 gate)"""
    helpers = _extract_skew_helpers(PANELS_JS.read_text(encoding="utf-8"))
    for server_val in ("unknown", "UNKNOWN", "Unknown"):
        result = _run_harness(
            _make_stub("v1"),
            helpers,
            f'checkWebUIVersionSkew({{ webui_version: "{server_val}" }});',
        )
        assert result["display"] != "flex", (
            f"Banner wrongly shown for server version {server_val!r}: {result}"
        )


def test_node_placeholder_client_version_no_banner():
    """Unresolved __WEBUI_VERSION__ literal as bundle stamp keeps banner hidden."""
    helpers = _extract_skew_helpers(PANELS_JS.read_text(encoding="utf-8"))
    result = _run_harness(
        _make_stub("__WEBUI_VERSION__"),
        helpers,
        'checkWebUIVersionSkew({ webui_version: "v2" });',
    )
    assert result["display"] != "flex", (
        f"Banner wrongly shown when client version is unresolved placeholder: {result}"
    )


def test_node_null_settings_no_banner():
    """null/undefined settings keeps banner hidden and does not throw."""
    helpers = _extract_skew_helpers(PANELS_JS.read_text(encoding="utf-8"))
    result = _run_harness(
        _make_stub("v1"),
        helpers,
        "checkWebUIVersionSkew(null); checkWebUIVersionSkew(undefined);",
    )
    assert result["display"] != "flex", (
        f"Banner wrongly shown for null/undefined settings: {result}"
    )


def test_node_rejected_settings_fetch_no_banner():
    """The monitor's rejected /api/settings poll is swallowed and keeps the banner hidden."""
    helpers = _extract_skew_helpers(PANELS_JS.read_text(encoding="utf-8"))
    result = _run_harness(
        _make_stub(
            "v1",
            "let _apiCalls = 0; "
            "global.api = () => { _apiCalls += 1; return Promise.reject(new Error('network error')); };",
        ),
        helpers,
        "await Promise.resolve(); if (_apiCalls !== 1) throw new Error('monitor did not poll settings');",
    )
    assert result["display"] != "flex", (
        f"Banner wrongly shown for rejected settings fetch: {result}"
    )
