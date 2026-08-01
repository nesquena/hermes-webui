"""Playwright browser tests for #6645 — workspace directory paging.

Covers:
  1. A directory flagged has_more renders the load-more row.
  2. assert_no_raw_i18n_keys over #fileTree (load_more_entries must resolve to
     real prose, not the key string itself).
  3. assert_layout_sane over #fileTree.
  4. Clicking load-more appends remaining entries with no duplicate paths.
  5. A single-page directory renders no load-more row.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


sys.path.insert(0, str(ROOT / "tests"))
from _layout_helpers import assert_layout_sane, assert_no_raw_i18n_keys

I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _extract_fn(source: str, name: str) -> str:
    """Extract a named JS function (sync or async) by balanced-brace walking."""
    for prefix in (f"async function {name}(", f"function {name}("):
        idx = source.find(prefix)
        if idx >= 0:
            start = idx
            break
    else:
        raise AssertionError(f"{name!r} not found in JS source")
    paren = source.index("(", start)
    paren_depth = 0
    for i in range(paren, len(source)):
        if source[i] == "(":
            paren_depth += 1
        elif source[i] == ")":
            paren_depth -= 1
            if paren_depth == 0:
                break
    brace = source.index("{", i)
    depth = 0
    for i in range(brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError(f"balanced body for {name!r} not found")


def _extract_wrapper(source: str) -> str:
    """Extract the renderFileTree wrapper block from workspace.js."""
    marker = "if(typeof renderFileTree==='function'){"
    idx = source.find(marker)
    if idx < 0:
        raise AssertionError("renderFileTree wrapper not found in workspace.js")
    brace = source.index("{", idx)
    depth = 0
    for i in range(brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[idx : i + 1]
    raise AssertionError("balanced wrapper block not found")


def _build_harness(
    entries: list[dict],
    has_more: bool,
    cursor: str | None,
    next_page: list[dict] | None = None,
) -> str:
    """Return a self-contained HTML page exercising the paging UI components."""
    render_load_more = _extract_fn(WORKSPACE_JS, "_renderLoadMoreRow")
    load_more_dir = _extract_fn(WORKSPACE_JS, "_loadMoreDir")
    capture_request_owner = _extract_fn(WORKSPACE_JS, "_workspaceCaptureRequestOwner")
    capture_dir_request_owner = _extract_fn(WORKSPACE_JS, "_workspaceCaptureDirRequestOwner")
    request_owner_is_current = _extract_fn(WORKSPACE_JS, "_workspaceRequestOwnerIsCurrent")
    route_for_path = _extract_fn(WORKSPACE_JS, "_workspaceRouteForPath")
    route_for_path_rel = _extract_fn(WORKSPACE_JS, "_workspaceRouteForPathRel")
    normalize_rel_path = _extract_fn(WORKSPACE_JS, "_normalizeWorkspaceRelPath")
    wrapper = _extract_wrapper(WORKSPACE_JS)
    render_file_tree = _extract_fn(UI_JS, "renderFileTree")
    render_tree_items = _extract_fn(UI_JS, "_renderTreeItems")

    entries_json = json.dumps(entries)
    next_json = json.dumps(next_page or [])
    cursor_js = json.dumps(cursor)
    has_more_js = "true" if has_more else "false"

    if has_more and next_page is not None:
        api_stub = (
            f"async function api(url){{window._apiUrls.push(url);"
            f"return{{entries:{next_json},has_more:false,cursor:null}};}}"
        )
    else:
        api_stub = (
            "async function api(url){window._apiUrls.push(url);"
            "throw new Error('api() must not be called in this test case');}"
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
{STYLE_CSS}
body{{margin:0;background:#141327;color:#efe7dd;font-family:Inter,system-ui,sans-serif;}}
.test-tree-host{{width:320px;height:600px;display:flex;flex-direction:column;background:#17162b;}}
#fileTree{{flex:1;overflow-y:auto;padding:8px;}}
.file-del-btn{{width:20px;height:20px;min-width:20px;}}
</style>
</head><body>
<div class="test-tree-host">
  <div id="fileTree"></div>
</div>
<script>
{I18N_JS}
window._apiUrls=[];
{api_stub}
const S={{
  session:{{session_id:'s1',workspace:'/ws'}},
  entries:{entries_json},
  currentDir:'.',
  _dirCache:{{}},
  _dirHasMore:{has_more_js},
  _dirCursor:{cursor_js},
  _expandedDirs:new Set(),
  showHiddenWorkspaceFiles:false,
}};
let _wsTreeGen=0;
let _wsDirRequestGen=0;
let _wsArtifactRequestGen=0;
function bumpWorkspaceTreeGen(){{_wsTreeGen+=1;return _wsTreeGen;}}
function $(id){{return document.getElementById(id);}}
function _workspaceEscapeGrantForPath(){{return null;}}
function _workspaceEscapeExactGrant(){{return null;}}
function _showFileContextMenu(){{}}
function _setWsDragData(){{}}
function _clearWsDragData(){{}}
function _clearWorkspaceMoveDragOver(){{}}
function _bindWorkspaceMoveDropTarget(){{}}
function _bindWorkspaceOsUploadDropTarget(){{}}
function fileIcon(){{return '';}}
function li(){{return '';}}
function deleteWorkspaceFile(){{}}
function deleteWorkspaceDir(){{}}
function openFile(){{}}
function loadDir(){{}}
function authorizeWorkspaceEscapeNavigation(){{return Promise.resolve(null);}}
function showConfirmDialog(){{return Promise.resolve(false);}}
{normalize_rel_path}
{route_for_path_rel}
{route_for_path}
{capture_request_owner}
{capture_dir_request_owner}
{request_owner_is_current}
function _visibleWorkspaceEntries(entries){{return Array.isArray(entries)?entries:[];}}
{render_tree_items}
{render_file_tree}
{render_load_more}
{load_more_dir}
{wrapper}
renderFileTree();
</script>
</body></html>"""


# ── Synthetic entry sets ──────────────────────────────────────────────────────
_PAGE_ONE = [{"name": f"file{i:03d}.txt", "path": f"file{i:03d}.txt"} for i in range(5)]
# Entries 3-4 overlap with _PAGE_ONE to exercise the dedup guard in _loadMoreDir.
_PAGE_TWO = [
    {"name": "file003.txt", "path": "file003.txt"},
    {"name": "file004.txt", "path": "file004.txt"},
    {"name": "file005.txt", "path": "file005.txt"},
    {"name": "file006.txt", "path": "file006.txt"},
]


def _launch():
    """Return (playwright, browser); call pytest.skip if either is unavailable."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip(
            "playwright is unavailable; install it and run `playwright install chromium`"
        )

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
    except Exception as exc:
        pw.stop()
        pytest.skip(f"chromium unavailable for browser tests: {exc}")
    return pw, browser


@pytest.mark.parametrize("width", [1280, 768, 400])
def test_load_more_row_appears_when_has_more(width):
    """A directory flagged has_more must render the .ws-load-more-row button."""
    pw, browser = _launch()
    try:
        page = browser.new_page(viewport={"width": width, "height": 600})
        page.set_content(_build_harness(_PAGE_ONE, has_more=True, cursor="test-cursor"))
        rows = page.locator(".ws-load-more-row")
        assert rows.count() == 1, "load-more row must appear when S._dirHasMore=true"
        row_text = rows.inner_text()
        assert str(len(_PAGE_ONE)) in row_text, "load-more row must show the current entry count"
    finally:
        browser.close()
        pw.stop()


@pytest.mark.parametrize("width", [1280, 768, 400])
def test_no_raw_i18n_keys_in_file_tree(width):
    """t('load_more_entries') must resolve to real prose, not the key string."""
    pw, browser = _launch()
    try:
        page = browser.new_page(viewport={"width": width, "height": 600})
        page.set_content(_build_harness(_PAGE_ONE, has_more=True, cursor="test-cursor"))
        assert_no_raw_i18n_keys(page, "#fileTree")
    finally:
        browser.close()
        pw.stop()


@pytest.mark.parametrize("width", [1280, 768, 400])
def test_layout_sane_with_load_more_row(width):
    """The file tree with the load-more row must pass the layout lint sweep."""
    pw, browser = _launch()
    try:
        page = browser.new_page(viewport={"width": width, "height": 600})
        page.set_content(_build_harness(_PAGE_ONE, has_more=True, cursor="test-cursor"))
        assert_layout_sane(page, "#fileTree")
    finally:
        browser.close()
        pw.stop()


@pytest.mark.parametrize("width", [1280, 768, 400])
def test_click_load_more_appends_without_duplicates(width):
    """Clicking load-more must append new entries and deduplicate paths."""
    pw, browser = _launch()
    try:
        page = browser.new_page(viewport={"width": width, "height": 600})
        page.set_content(
            _build_harness(
                _PAGE_ONE, has_more=True, cursor="test-cursor", next_page=_PAGE_TWO
            )
        )
        row = page.locator(".ws-load-more-row")
        assert row.count() == 1, "load-more row must be present before click"
        row.click()
        # _loadMoreDir is async; wait for the DOM repaint to confirm the row is gone.
        page.wait_for_function("() => !document.querySelector('.ws-load-more-row')")
        paths = page.evaluate("() => S.entries.map(e => e.path)")
        assert paths == [
            "file000.txt", "file001.txt", "file002.txt", "file003.txt",
            "file004.txt", "file005.txt", "file006.txt",
        ], f"expected 7 unique entries after append, got {paths}"
        assert "/api/list?session_id=s1&path=.&cursor=test-cursor" in page.evaluate(
            "() => _apiUrls[0]"
        )
        assert page.locator(".ws-load-more-row").count() == 0, (
            "load-more row must be removed after the last page loads"
        )
    finally:
        browser.close()
        pw.stop()


def test_space_key_load_more_prevents_tree_scroll():
    """Keyboard activation must consume Space instead of scrolling the tree."""
    pw, browser = _launch()
    try:
        page = browser.new_page(viewport={"width": 400, "height": 600})
        page.set_content(
            _build_harness(
                _PAGE_ONE, has_more=True, cursor="test-cursor", next_page=_PAGE_TWO
            )
        )
        result = page.locator(".ws-load-more-row").evaluate(
            """row => {
                row.focus();
                const event = new KeyboardEvent('keydown', {
                    key: ' ', bubbles: true, cancelable: true
                });
                const dispatched = row.dispatchEvent(event);
                return {defaultPrevented: event.defaultPrevented, dispatched};
            }"""
        )
        assert result == {"defaultPrevented": True, "dispatched": False}
        page.wait_for_function("() => !document.querySelector('.ws-load-more-row')")
    finally:
        browser.close()
        pw.stop()


@pytest.mark.parametrize("width", [1280, 768, 400])
def test_no_load_more_row_when_single_page(width):
    """A directory that fits one page must not render the load-more row."""
    pw, browser = _launch()
    try:
        page = browser.new_page(viewport={"width": width, "height": 600})
        page.set_content(_build_harness(_PAGE_ONE, has_more=False, cursor=None))
        assert page.locator(".ws-load-more-row").count() == 0, (
            "load-more row must not appear when S._dirHasMore=false"
        )
    finally:
        browser.close()
        pw.stop()
