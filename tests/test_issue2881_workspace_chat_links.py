"""Regression coverage for workspace:// chat links opening workspace preview (#2881)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.fixture(scope="module")
def browser():
    if sync_playwright is None:
        pytest.skip("Playwright is unavailable")
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip("Playwright Chromium is unavailable")
        instance = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        yield instance
        instance.close()

_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
global.window = {};
global.document = { createElement: () => ({ innerHTML: '', textContent: '' }) };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
eval(extractFunc('_matchBacktickFenceLine'));
eval(extractFunc('_isBacktickFenceClose'));
eval(extractFunc('renderMd'));

let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => { process.stdout.write(renderMd(buf)); });
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("issue2881_renderer") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _render(driver_path: str, markdown: str) -> str:
    result = subprocess.run(
        [NODE, driver_path, str(REPO_ROOT / "static" / "ui.js")],
        input=markdown,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout


def _function_source(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated {name}")


def _workspace_click_delegate(source: str) -> str:
    marker = "document.addEventListener('click', e => {"
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 3]
    raise AssertionError("workspace click delegate is unterminated")


def test_render_md_rewrites_workspace_links_to_internal_anchor(driver_path):
    html = _render(driver_path, "[Open plan](workspace://notes/plan.md)")

    assert 'href="#workspace=notes%2Fplan.md"' in html
    assert "workspace://notes/plan.md" not in html
    assert ">Open plan</a>" in html


def test_render_md_does_not_autolink_raw_workspace_urls(driver_path):
    html = _render(driver_path, "Open workspace://notes/plan.md manually")

    assert '<a href="#workspace=' not in html
    assert "workspace://notes/plan.md" in html


def test_workspace_link_click_delegate_opens_workspace_preview():
    assert 'a[href^="#workspace="]' in UI_JS
    assert "decodeURIComponent" in UI_JS
    assert "openArtifactPath(rel)" in UI_JS
    assert "async function openArtifactPath(path)" in (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    assert "/api/list?session_id=" in (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    assert "file_open_failed" in (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("workspace", ["", None])
@pytest.mark.parametrize("renderer", ["settled", "streaming"])
def test_production_workspace_link_clicks_open_relative_artifacts(
    browser, driver_path, workspace, renderer
):
    workspace_js = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    for rel in ("reports/output.md", "README"):
        page = browser.new_page(viewport={"width": 1024, "height": 600}, device_scale_factor=2)
        try:
            page_errors = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            if renderer == "settled":
                link = _render(driver_path, f"[Open artifact](workspace://{rel})")
            else:
                link = '<a href="__STREAMING_LINK__">Open artifact</a>'
            page.set_content(f"<main id='transcript'>{link}</main>")
            page.add_script_tag(
                content=f"""
                window.S = {{session: {{session_id:'session-a', workspace: {json.dumps(workspace)}}}, messages: [], toolCalls: []}};
                window.$ = id => document.getElementById(id);
                window.esc = value => String(value).replace(/[&<>\\"']/g, c =>
                  ({{'&':'&amp;','<':'&lt;','>':'&gt;','\\"':'&quot;',"'":'&#39;'}}[c]));
                window.t = key => key;
                window.switchWorkspacePanelTab = tab => {{ window.activeTab = tab; }};
                window.setStatus = value => {{ window.lastStatus = value; }};
                window.openFile = value => {{ window.openedPath = value; }};
                window.__apiCalls = [];
                """
            )
            page.add_script_tag(content=workspace_js)
            page.add_script_tag(
                content="""
                window.switchWorkspacePanelTab = tab => { window.activeTab = tab; };
                window.openFile = value => { window.openedPath = value; };
                window.api = async url => {
                  window.__apiCalls.push(url);
                  const path = decodeURIComponent(url.match(/[?&]path=([^&]*)/)[1]);
                  const name = path === 'reports' ? 'output.md' : 'README';
                  return {entries:[{name, path:name}]};
                };
                """
            )
            if renderer == "streaming":
                page.add_script_tag(content=_function_source(MESSAGES_JS, "_smdLinkHref"))
                page.evaluate(
                    "rel => document.querySelector('a').setAttribute('href', _smdLinkHref('workspace://' + rel))",
                    rel,
                )
            page.add_script_tag(content=_workspace_click_delegate(UI_JS))
            page.locator("a[href^='#workspace=']").click()
            page.wait_for_timeout(500)
            assert not page_errors, page_errors
            state = page.evaluate("() => ({opened: window.openedPath || null, status: window.lastStatus || null, calls: window.__apiCalls, href: document.querySelector('a').getAttribute('href')})")
            assert state["opened"] == rel, state
            assert page.evaluate("() => window.lastStatus || null") is None
            assert page.evaluate("() => window.__apiCalls") == [
                f"/api/list?session_id=session-a&path={'reports' if '/' in rel else '.'}"
            ]
        finally:
            page.close()


def test_streaming_markdown_rewrites_workspace_links_before_sanitizing():
    assert "function _smdLinkHref" in MESSAGES_JS
    assert "workspace:\\/\\/" in MESSAGES_JS
    assert "'#workspace='" in MESSAGES_JS
    assert "_smdLinkHref(v)" in MESSAGES_JS
    assert "_smdLinkHref(value)" in MESSAGES_JS
