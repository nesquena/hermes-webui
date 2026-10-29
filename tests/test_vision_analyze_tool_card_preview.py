"""vision_analyze tool cards preview the analyzed image in their expanded detail.

A `vision_analyze` call names the image it looked at in `args.image_url`, but
the tool card only rendered text: the read-kind gate in `_toolCardAllowsDetail`
even hid the detail entirely, so the user could not see which picture the agent
analyzed without opening the file by hand.

The card now shows the image inside the expanded detail (DESIGN.md: arguments
and result snippets stay behind expansion), rendered by the same
`_inlineMediaHtmlForRef` helper the MEDIA: pipeline uses, so local files go
through `/api/media` with the session id and remote URLs keep the CSP
img-src placeholder policy. Two names qualify: the bare `vision_analyze` and
`mcp__hermes__vision_analyze`, the name a provider gives it when it serves
Hermes' tools from an MCP server called `hermes`; a same-named tool from any
other MCP server does not.

The image reference must also survive persistence: `image_url` was capped to
the 120-char incidental limit by `_truncate_tool_args` / `_tool_args_snapshot`
(backend) and `_toolArgsSnapshot` (frontend), which cut realistic screenshot
paths and broke the preview after a reload. A capped ref (trailing `...`) is
never rendered.

The render tests drive the real functions extracted from static/ui.js in node.
The browser tests load the real page from the isolated test server. They check
that a collapsed card fetches nothing, and that tool previews stay out of the
image carousel of the message.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

from api.streaming import _truncate_tool_args

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

# Realistic screenshot path longer than the 120-char incidental arg cap.
LONG_PNG = (
    "/home/alice/.hermes/feuille-de-route/2026-10-22/items/mr388/captures/"
    "review-round-2/05-focus-ring-theme-switcher-dark.png"
)
assert len(LONG_PNG) > 120

SESSION_ID = "sess-vision-1"

_FN_NAMES = [
    # tool-card rendering chain
    "_toolDisplayName", "_toolActionKind", "_toolKindIcon", "_toolPathBasename",
    "_decodeToolLabelEntities", "_redactToolTargetLabel", "_shortToolLabel", "_toolI18n",
    "_toolTargetLabel", "_toolReadRangeLabel", "_toolVisibleTargetLabel", "_toolCommandTitle",
    "_toolQueryTitle", "_toolActionLabelText", "_toolArgPreviewValue", "_toolArgPreviewKeyIsHidden",
    "_formatToolArgPreview", "_toolResultOneLiner", "_toolCardPreviewText",
    "_isVisionAnalyzeTool", "_visionToolImageRef", "_visionToolImagePreviewHtml",
    "_hideBrokenToolImagePreview",
    "_toolCardAllowsDetail", "_toolDetailLeadLabel", "_toolDetailLeadText",
    "_isMemorySave", "_isSkillUpdate", "_tcAction", "buildToolCard",
    "_transparentToolRowHasDetail",
    # shared media helpers (MEDIA: pipeline)
    "_mediaKindForName", "_isSafeDataImageUri", "_dataImageHtml",
    "_remoteImageSources", "_remoteImageSourceMatches", "_remoteImageAllowed",
    "_remoteImageReason", "_remoteImagePlaceholderHtml", "_mediaPlayerHtml",
    "_inlineMediaHtmlForRef",
]
_CONST_NAMES = [
    "esc", "_IMAGE_EXTS", "_PDF_EXTS", "_HTML_EXTS", "_SVG_EXTS", "_AUDIO_EXTS",
    "_VIDEO_EXTS", "_CSV_EXTS", "_EXCALIDRAW_EXTS",
    "_DATA_IMAGE_RE", "_DATA_IMAGE_SVG_RE", "_DATA_IMAGE_MAX_LEN",
]


# Top-level ui.js functions close with a column-0 `}`; the extractor grows the
# slice to the first such brace that parses, so nested template literals and
# regex literals inside the helpers cannot derail it. Missing names are skipped.
_DRIVER = r"""
const fs=require('fs');
const vm=require('vm');
const SRC=fs.readFileSync(process.argv[3],'utf8');
function extractFunction(name){
  const m=new RegExp('^function '+name+'\\(','m').exec(SRC);
  if(!m) return '';
  let end=m.index;
  for(;;){
    end=SRC.indexOf('\n}',end+1);
    if(end<0) throw new Error(name+'() body did not close');
    const code=SRC.slice(m.index,end+2);
    try{ new vm.Script(code); return code; }catch(_){}
  }
}
function makeEl(tag){
  const el={tagName:String(tag||'div').toUpperCase(),dataset:{},_attrs:{},innerHTML:''};
  el.setAttribute=(k,v)=>{el._attrs[k]=String(v);};
  el.removeAttribute=k=>{delete el._attrs[k];};
  el.getAttribute=k=>(k in el._attrs?el._attrs[k]:null);
  return el;
}
global.document={createElement:makeEl,baseURI:'http://127.0.0.1:8787/'};
global.window={__HERMES_CONFIG__:{imgSrcExtra:['https://cdn.example.com']}};
global.t=undefined;
global.li=()=>'<svg></svg>';
global.toolIcon=()=>'<svg></svg>';
global._snippetLooksLikeDiff=()=>false;
global._colorDiffLines=s=>s;
global._toolDisclosureIdentity=()=>'';
global.S={session:{session_id:%%SID%%}};
function extractConst(name){
  const m=new RegExp('^const '+name+'=.*;$','m').exec(SRC);
  if(!m) throw new Error('const '+name+' not found');
  return m[0];
}
// One script in the global realm so the extracted functions see the consts.
vm.runInThisContext([
  ...JSON.parse(process.argv[5]).map(extractConst),
  ...JSON.parse(process.argv[4]).map(extractFunction),
].join('\n'));
const cases=JSON.parse(process.argv[2]);
const out={};
for(const [key,tc] of Object.entries(cases)){
  const row=buildToolCard(tc);
  out[key]={
    html:row.innerHTML,
    transparentHasDetail:_transparentToolRowHasDetail(tc),
  };
}
// Broken-preview handler: an <img> inside .tool-card-image-preview that fails
// to load (e.g. /api/media 403 outside the allowed roots) hides its preview;
// any other <img> error is left alone.
function fakeImg(inPreview){
  const wrap={hidden:false};
  return {wrap, img:{tagName:'IMG', closest:sel=>(inPreview&&sel==='.tool-card-image-preview')?wrap:null}};
}
const inside=fakeImg(true), outside=fakeImg(false);
const handler=typeof _hideBrokenToolImagePreview==='function'?_hideBrokenToolImagePreview:null;
if(handler){ handler({target:inside.img}); handler({target:outside.img}); handler({target:null}); }
out.__broken={handler:!!handler, insideHidden:inside.wrap.hidden, outsideHidden:outside.wrap.hidden};
process.stdout.write(JSON.stringify(out));
"""


def _vision(image_url, name="vision_analyze", **extra):
    tc = {
        "name": name,
        "args": {"image_url": image_url, "question": "What does the dialog show?"},
        "snippet": "The dialog shows a dark preview with a focus ring.",
        "done": True,
    }
    tc.update(extra)
    return tc


CASES = {
    "bare_local": _vision(LONG_PNG),
    "mcp_alias_local": _vision(LONG_PNG, name="mcp__hermes__vision_analyze"),
    "other_mcp_server": _vision(LONG_PNG, name="mcp__other__vision_analyze"),
    "running_no_snippet": {
        "name": "vision_analyze",
        "args": {"image_url": "/tmp/shot.png", "question": "q"},
        "done": False,
    },
    "file_uri": _vision("file:///tmp/shot.png"),
    "file_uri_localhost": _vision("file://localhost/tmp/shot.png"),
    "file_uri_remote_authority": _vision("file://fileserver/share/shot.png"),
    "data_image": _vision("data:image/png;base64,iVBORw0KGgo="),
    "remote_not_allowlisted": _vision("https://example.com/cat.png"),
    "remote_allowlisted": _vision("https://cdn.example.com/cat.png"),
    "remote_extensionless": _vision("https://fal.example.com/files/abc123"),
    "remote_pdf": _vision("https://example.com/report.pdf"),
    "remote_signed_url": _vision("https://cdn.example.com/cat.png?token=abc123"),
    "unc_bare_path": _vision("//fileserver/share/shot.png"),
    "capped_ref": _vision(LONG_PNG[:120] + "..."),
    "relative_path": _vision("captures/shot.png"),
    "non_image_file": _vision("/tmp/report.pdf"),
    "hostile_path": _vision('/tmp/"><img src=x onerror=alert(1)>.png'),
    "read_file_png": {
        "name": "read_file",
        "args": {"path": "/tmp/shot.png"},
        "snippet": "binary",
        "done": True,
    },
}


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    if NODE is None:
        pytest.skip("node not on PATH")
    driver = tmp_path_factory.mktemp("vision_preview") / "driver.js"
    driver.write_text(_DRIVER.replace("%%SID%%", json.dumps(SESSION_ID)), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(driver), json.dumps(CASES), str(UI_JS_PATH),
         json.dumps(_FN_NAMES), json.dumps(_CONST_NAMES)],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node driver failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _detail(html: str) -> str:
    """The expanded-detail markup of a card ('' when the card has no detail)."""
    marker = 'class="tool-card-detail"'
    return html.split(marker, 1)[1] if marker in html else ""


def _header(html: str) -> str:
    return html.split('class="tool-card-detail"', 1)[0]


def _media_src(path: str) -> str:
    # encodeURIComponent keeps -_.!~*'() unescaped; esc() then escapes ' and &.
    encoded = quote(path, safe="-_.!~*'()").replace("'", "&#39;")
    return f"api/media?path={encoded}&amp;session_id={SESSION_ID}"


@pytest.mark.parametrize("case", ["bare_local", "mcp_alias_local"])
def test_vision_card_previews_local_image_in_detail(results, case):
    html = results[case]["html"]
    detail = _detail(html)
    assert detail, f"{case}: vision card must expose an expandable detail: {html[:400]}"
    assert f'src="{_media_src(LONG_PNG)}"' in detail, detail[:800]
    assert "<img" not in _header(html), "preview must stay behind expansion (DESIGN.md)"
    assert results[case]["transparentHasDetail"] is True


def test_vision_card_from_other_mcp_server_gets_no_preview(results):
    assert "<img" not in results["other_mcp_server"]["html"]


def test_running_vision_card_already_previews_image(results):
    detail = _detail(results["running_no_snippet"]["html"])
    assert f'src="{_media_src("/tmp/shot.png")}"' in detail, detail[:600]


@pytest.mark.parametrize("case", ["file_uri", "file_uri_localhost"])
def test_local_file_uri_routes_through_api_media(results, case):
    detail = _detail(results[case]["html"])
    assert f'src="{_media_src("/tmp/shot.png")}"' in detail, detail[:600]


def test_file_uri_with_remote_authority_opens_no_detail(results):
    """file://host/... is rejected by _inlineMediaHtmlForRef; the read-kind card
    must not open an image-less detail for it either."""
    case = results["file_uri_remote_authority"]
    assert "tool-card-image-preview" not in case["html"]
    assert 'class="tool-card-detail"' not in case["html"]
    assert case["transparentHasDetail"] is False


def test_broken_preview_image_hides_only_its_preview(results):
    broken = results["__broken"]
    assert broken["handler"], "_hideBrokenToolImagePreview() is missing from ui.js"
    assert broken["insideHidden"] is True
    assert broken["outsideHidden"] is False


def test_data_image_renders_inline(results):
    detail = _detail(results["data_image"]["html"])
    assert '<img class="msg-media-img" src="data:image/png;base64,iVBORw0KGgo="' in detail, detail[:600]


def test_remote_image_outside_csp_allowlist_stays_inert(results):
    detail = _detail(results["remote_not_allowlisted"]["html"])
    assert 'class="tool-card-image-preview"' in detail, detail[:600]
    assert 'href="https://example.com/cat.png"' in detail
    assert "<img" not in detail, "non-allowlisted remote image must not be fetched"


def test_remote_image_on_csp_allowlist_renders_img(results):
    detail = _detail(results["remote_allowlisted"]["html"])
    assert '<img class="msg-media-img" src="https://cdn.example.com/cat.png"' in detail, detail[:600]


def test_extensionless_remote_url_still_previews(results):
    # CDN image URLs often carry no extension (#853); keep the preview.
    assert 'class="tool-card-image-preview"' in _detail(results["remote_extensionless"]["html"])


@pytest.mark.parametrize("case", ["remote_pdf", "remote_signed_url", "unc_bare_path"])
def test_refs_that_must_not_be_fetched_open_no_detail(results, case):
    """A remote non-image, a URL carrying a secret query param (the visible args
    redact it; the preview must not put it back in src/href), and a bare UNC
    path all fail closed: no preview and no image-less detail."""
    html = results[case]["html"]
    assert "tool-card-image-preview" not in html, html[:600]
    assert 'class="tool-card-detail"' not in html
    assert results[case]["transparentHasDetail"] is False


@pytest.mark.parametrize("case", ["capped_ref", "relative_path", "non_image_file"])
def test_unusable_refs_render_no_preview(results, case):
    html = results[case]["html"]
    assert "tool-card-image-preview" not in html, html[:600]
    assert "<img" not in html


def test_hostile_path_is_escaped(results):
    html = results["hostile_path"]["html"]
    assert "<img src=x" not in html
    assert 'class="tool-card-image-preview"' in _detail(html)


def test_only_vision_tools_preview_images(results):
    html = results["read_file_png"]["html"]
    assert "tool-card-image-preview" not in html
    assert "<img" not in html


# ---------- persistence: image_url keeps the long content cap ----------

def test_backend_keeps_long_image_url():
    out = _truncate_tool_args({"image_url": LONG_PNG, "question": "q"})
    assert out["image_url"] == LONG_PNG


_SNAPSHOT_DRIVER = r"""
const fs=require('fs');
const src=fs.readFileSync(process.argv[2],'utf8');
const m=src.match(/function _toolArgsSnapshot\([^]*?\n}/m);
if(!m) throw new Error('_toolArgsSnapshot not found');
eval(m[0]);
process.stdout.write(JSON.stringify(_toolArgsSnapshot(JSON.parse(process.argv[3]))));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_frontend_snapshot_keeps_long_image_url(tmp_path):
    driver = tmp_path / "snapshot.js"
    driver.write_text(_SNAPSHOT_DRIVER, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(driver), str(UI_JS_PATH), json.dumps({"image_url": LONG_PNG})],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["image_url"] == LONG_PNG


# ---------- browser: fetch timing and lightbox isolation ----------
#
# The collapsed detail is `max-height:0; overflow:hidden`, not display:none, so
# `loading="lazy"` alone did not defer the preview: a collapsed card still
# fetched the full image, and a reloaded session fetched every screenshot it
# ever analyzed. Separately, a failed (hidden) preview kept its .msg-media-img,
# which _openImgLightbox collected with the message's own images, so the
# carousel counted it and Previous landed on a broken image. These drive the
# real page against the isolated test server.

_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
_FINAL = "Vision preview browser proof final answer."
_VISION_ROW = '.tool-card-row[data-tool-name="vision_analyze"]'


def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A solid-colour RGB PNG, built without Pillow."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw = (b"\x00" + bytes(rgb) * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture(scope="module")
def media_dir():
    from tests._pytest_port import TEST_STATE_DIR

    # The test server runs with HERMES_HOME=TEST_STATE_DIR, an /api/media root.
    root = Path(TEST_STATE_DIR) / "vision-preview-media"
    root.mkdir(parents=True, exist_ok=True)
    colours = {
        "compact.png": (200, 40, 40),
        "transparent.png": (40, 160, 60),
        "ok.png": (40, 60, 200),
        "report-a.png": (230, 180, 30),
        "report-b.png": (120, 40, 160),
    }
    for name, rgb in colours.items():
        (root / name).write_bytes(_png(160, 90, rgb))
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _vision_messages(image_urls: list[str], final: str = _FINAL) -> list[dict]:
    calls, results = [], []
    for i, url in enumerate(image_urls):
        call_id = f"vision-preview-call-{i}"
        calls.append({
            "id": call_id,
            "type": "function",
            "function": {
                "name": "vision_analyze",
                "arguments": json.dumps({"image_url": url, "question": "What does it show?"}),
            },
        })
        results.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps({"success": True, "analysis": "A settings dialog."}),
        })
    return [
        {"role": "user", "content": "Check the screenshots."},
        {"role": "assistant", "content": None, "tool_calls": calls},
        *results,
        {"role": "assistant", "content": final},
    ]


_IMPORT_AND_LOAD = r"""
async ({title, messages, finalText}) => {
  const r = await fetch('/api/session/import', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title, messages}),
  });
  const d = await r.json();
  if (!r.ok || !d.session || !d.session.session_id) {
    throw new Error('session import failed: ' + JSON.stringify(d));
  }
  await loadSession(d.session.session_id);
  const deadline = Date.now() + 10000;
  while (!((document.querySelector('#msgInner') || {}).innerText || '').includes(finalText)) {
    if (Date.now() > deadline) throw new Error('session never rendered its final text');
    await new Promise(res => setTimeout(res, 50));
  }
  return d.session.session_id;
}
"""

# Give lazy-loading, deferred materialization and idle work a chance to run,
# so "nothing was fetched" means nothing, not "not yet".
_SETTLE = r"""
async () => {
  await new Promise(res => requestAnimationFrame(() => requestAnimationFrame(res)));
  if (typeof requestIdleCallback === 'function') {
    await new Promise(res => requestIdleCallback(res, {timeout: 1000}));
  }
  await new Promise(res => setTimeout(res, 600));
}
"""

_SET_ACTIVITY_MODE = r"""
async (mode) => {
  const r = await fetch('/api/settings', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({chat_activity_display_mode: mode}),
  });
  if (!r.ok) throw new Error('settings update failed: ' + r.status);
}
"""

_GET_ACTIVITY_MODE = (
    "async () => (await (await fetch('/api/settings')).json()).chat_activity_display_mode"
)

# Opens the settled worklog, then any nested tool group ("Read 2 files").
_EXPAND_WORKLOGS = r"""
async () => {
  const heads = '.tool-worklog-summary,.tool-call-group-summary,.tool-worklog-tool-group-head';
  for (let pass = 0; pass < 3; pass++) {
    const closed = Array.from(document.querySelectorAll(heads))
      .filter(b => b.getAttribute('aria-expanded') === 'false');
    if (!closed.length) return;
    closed.forEach(b => b.click());
    await new Promise(res => requestAnimationFrame(() => requestAnimationFrame(res)));
  }
}
"""

# Live Transparent Stream rows (and any path that does not defer) carry a
# built detail while collapsed; materialize a settled row the way the app does
# to reproduce that state without opening it.
_MATERIALIZE_TRANSPARENT_ROW = r"""
(selector) => {
  const row = document.querySelector(selector);
  const transparentRow = row && row.closest('.transparent-event-row');
  if (!transparentRow) throw new Error('no transparent vision row');
  if (transparentRow.getAttribute('data-transparent-detail-deferred') === '1') {
    _materializeTransparentToolDetail(transparentRow);
  }
  const card = transparentRow.querySelector('.tool-card');
  return {
    open: Boolean(card && card.classList.contains('open')),
    previewInDom: Boolean(transparentRow.querySelector('.tool-card-image-preview img')),
  };
}
"""

_PREVIEW_LOADED = r"""
(name) => {
  const img = Array.from(document.querySelectorAll('.tool-card-image-preview img'))
    .find(i => (i.getAttribute('src') || '').includes(name));
  return Boolean(img && img.complete && img.naturalWidth > 0);
}
"""


class _Browser:
    """One headless Chromium page on the test server, with /api/media logging."""

    def __init__(self, pw_module):
        from tests._pytest_port import BASE

        self._cm = pw_module.sync_playwright()
        playwright = self._cm.__enter__()
        self.browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        context = self.browser.new_context(base_url=BASE, viewport={"width": 1280, "height": 900})
        self.page = context.new_page()
        self.errors: list[str] = []
        self.media: list[str] = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.on(
            "request",
            lambda req: self.media.append(req.url) if "/api/media" in req.url else None,
        )
        self.boot()

    def boot(self) -> None:
        self.page.goto("/", wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => typeof S !== 'undefined' && S._bootReady === true", timeout=15000
        )
        self.page.wait_for_function("() => typeof loadSession === 'function'", timeout=15000)
        self.dismiss_onboarding()

    def dismiss_onboarding(self) -> None:
        # The shared test server can present first-run onboarding; skip it
        # through the real UI so it cannot intercept the clicks below.
        overlay = self.page.locator("#onboardingOverlay")
        if overlay.is_visible():
            self.page.locator("#onboardingSkipBtn").click()
            overlay.wait_for(state="hidden", timeout=15000)

    def load(self, title: str, messages: list[dict]) -> str:
        sid = self.page.evaluate(
            _IMPORT_AND_LOAD, {"title": title, "messages": messages, "finalText": _FINAL}
        )
        self.page.evaluate(_SETTLE)
        return sid

    def fetched(self, name: str) -> list[str]:
        return [url for url in self.media if name in url]

    def open_vision_card(self, index: int = 0) -> None:
        self.dismiss_onboarding()
        self.page.locator(f"{_VISION_ROW} .tool-card-header").nth(index).click()

    def close(self) -> None:
        self.browser.close()
        self._cm.__exit__(None, None, None)


@pytest.fixture
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    session = _Browser(pw)
    try:
        yield session
    finally:
        session.close()


def test_compact_worklog_preview_fetches_nothing_until_the_card_opens(browser, media_dir):
    page = browser.page
    assert page.evaluate(_GET_ACTIVITY_MODE) == "compact_worklog"
    browser.load("vision preview compact", _vision_messages([str(media_dir / "compact.png")]))
    assert browser.fetched("compact.png") == [], "fetched while the worklog group was collapsed"

    page.evaluate(_EXPAND_WORKLOGS)
    page.wait_for_selector(_VISION_ROW, state="visible", timeout=5000)
    page.evaluate(_SETTLE)
    assert browser.fetched("compact.png") == [], "fetched while the card was collapsed"

    browser.open_vision_card()
    page.wait_for_function(_PREVIEW_LOADED, arg="compact.png", timeout=10000)
    assert len(browser.fetched("compact.png")) == 1, browser.media
    assert browser.errors == []


def test_transparent_stream_preview_fetches_nothing_until_the_row_opens(browser, media_dir):
    page = browser.page
    previous = page.evaluate(_GET_ACTIVITY_MODE)
    page.evaluate(_SET_ACTIVITY_MODE, "transparent_stream")
    try:
        browser.boot()
        assert page.evaluate("() => chatActivityMode()") == "transparent_stream"
        browser.load(
            "vision preview transparent", _vision_messages([str(media_dir / "transparent.png")])
        )
        assert browser.fetched("transparent.png") == [], "fetched while the row was deferred"

        state = page.evaluate(_MATERIALIZE_TRANSPARENT_ROW, _VISION_ROW)
        assert state == {"open": False, "previewInDom": True}, state
        page.evaluate(_SETTLE)
        assert browser.fetched("transparent.png") == [], "fetched while the built row was collapsed"

        browser.open_vision_card()
        page.wait_for_function(_PREVIEW_LOADED, arg="transparent.png", timeout=10000)
        assert len(browser.fetched("transparent.png")) == 1, browser.media
        assert browser.errors == []
    finally:
        page.evaluate(_SET_ACTIVITY_MODE, previous or "compact_worklog")


def test_tool_previews_stay_out_of_the_message_image_carousel(browser, media_dir):
    """A failed preview (missing file, hidden) and a working one must not join
    the carousel of the message's own images, and vice versa."""
    page = browser.page
    report_a, report_b = media_dir / "report-a.png", media_dir / "report-b.png"
    final = f"{_FINAL}\n\nMEDIA:{report_a}\n\nMEDIA:{report_b}"
    messages = _vision_messages(
        [str(media_dir / "missing.png"), str(media_dir / "ok.png")], final=final
    )
    browser.load("vision preview carousel", messages)
    page.evaluate(_EXPAND_WORKLOGS)
    page.wait_for_selector(_VISION_ROW, state="visible", timeout=5000)
    browser.open_vision_card(0)
    browser.open_vision_card(1)
    page.wait_for_function(_PREVIEW_LOADED, arg="ok.png", timeout=10000)
    page.wait_for_function(
        "() => document.querySelectorAll('.tool-card-image-preview[hidden]').length === 1",
        timeout=10000,
    )

    message_images = page.locator(".msg-media-img:not(.tool-card-image-preview *)")
    assert message_images.count() == 2
    message_images.first.click()
    page.wait_for_selector(".img-lightbox", state="visible", timeout=5000)
    counter = page.locator(".img-lightbox-counter")
    lightbox_img = page.locator(".img-lightbox-canvas img")
    assert counter.inner_text() == "1 / 2"
    # Navigation does not wrap: Previous on the first image must stay put
    # instead of landing on a (broken) tool preview.
    page.locator(".img-lightbox-nav-prev").click()
    assert counter.inner_text() == "1 / 2"
    assert "report-a.png" in (lightbox_img.get_attribute("src") or "")
    page.locator(".img-lightbox-nav-next").click()
    assert counter.inner_text() == "2 / 2"
    assert "report-b.png" in (lightbox_img.get_attribute("src") or "")
    page.keyboard.press("Escape")
    page.wait_for_selector(".img-lightbox", state="detached", timeout=5000)

    page.locator(".tool-card-image-preview:not([hidden]) img").click()
    page.wait_for_selector(".img-lightbox", state="visible", timeout=5000)
    assert page.locator(".img-lightbox-counter").count() == 0, "a tool preview opened a carousel"
    assert "ok.png" in (page.locator(".img-lightbox-canvas img").get_attribute("src") or "")
    page.keyboard.press("Escape")
    assert browser.errors == []
