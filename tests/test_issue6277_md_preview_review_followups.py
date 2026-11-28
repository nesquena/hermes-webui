"""Behavioral tests for PR #6277 review follow-ups (items d and e).

Verifies in Node with FakeDOM:
(d) Markdown reference placeholder contains the original session-preserving
    download anchor with download=1, and loading text is hidden until
    loadMarkdownInline() begins the fetch. If the loader is never called
    (e.g. static/workspace.js injecting renderMd() without postProcess),
    the clickable download link remains visible.
(e) loadMarkdownInline() builds URLs via _mediaPreviewUrl() including
    _mediaSnapQuery(el), reusing the URL for download and fallback branches.
    On 401/403/404 errors, _mediaPreviewAllowsDownload() suppresses the
    broken download link.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "_md_inline_dom_base", _HERE / "test_md_inline_preview_dom.py"
)
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)

UI_JS_PATH = base.ROOT / "static" / "ui.js"
pytestmark = pytest.mark.skipif(base.NODE is None, reason="node not on PATH")

_HELPERS_TO_EXTRACT = (
    "_mediaKindForName",
    "_inlineMediaHtmlForRef",
    "_mediaSessionQuery",
    "_mediaSnapQuery",
    "_mediaPreviewUrl",
    "_mediaPreviewErrorKey",
    "_mediaPreviewAllowsDownload",
    "_mdInlineSessionId",
    "_mdInlineMediaUrl",
    "_mdInlineFallbackHtml",
    "_mdInlineDeclaredBytes",
    "_mdInlineReadBounded",
    "_mdInlineFetchPreview",
    "_terminateNestedMdPlaceholders",
    "_postProcessMdInlineSubtree",
    "loadMarkdownInline",
)


def _extract_func_script(js: str) -> str:
    prelude = "const src = " + json.dumps(js) + ";\n"
    body = r"""
function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  let start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  if (src.slice(Math.max(0, start - 6), start) === 'async ') start -= 6;
  let paramEnd = src.indexOf(')', start);
  let i = src.indexOf('{', paramEnd);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
"""
    return prelude + body


def _build_test_driver(body: str) -> str:
    js = UI_JS_PATH.read_text(encoding="utf-8")
    extract_script = _extract_func_script(js)
    evals = "".join(f"eval(extractFunc({json.dumps(fn)}));\n" for fn in _HELPERS_TO_EXTRACT)

    extra_prelude = r"""
const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _PDF_EXTS=/\.pdf$/i;
const _HTML_EXTS=/\.(html?|htm)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm|oga)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;
const _CSV_EXTS=/\.csv$/i;
const _EXCALIDRAW_EXTS=/\.excalidraw$/i;
const _MD_EXTS=/\.(md|markdown|mdown|mkd|mkdn)$/i;
var MD_INLINE_MAX_BYTES = 256 * 1024;

function _dataImageHtml(){ return ''; }
function _mediaPlayerHtml(){ return ''; }
function _remoteImageAllowed(){ return true; }
"""
    return (
        extract_script
        + base._fakedom_prelude()
        + extra_prelude
        + evals
        + "(async () => {\n"
        + body
        + "\n})().catch(e => { console.error(String(e && e.stack || e)); process.exit(1); });\n"
    )


def test_placeholder_contains_download_anchor_and_hides_loading_text_until_fetch():
    """Item (d): Placeholder has original download anchor; loading text is hidden

    until loadMarkdownInline starts the fetch. If loader is not executed, the
    download anchor remains clickable and no permanent loading spinner is shown.
    """
    driver = _build_test_driver(r"""
    // 1. Render placeholder with active session
    const htmlWithSession = _inlineMediaHtmlForRef('docs/guide.md', 'sess-123');
    const c1 = new FakeElement('div');
    c1.innerHTML = htmlWithSession;
    const loadEl1 = c1.querySelector('.md-inline-load');
    const link1 = loadEl1.querySelector('.msg-media-link');
    const loadingEl1 = loadEl1.querySelector('.md-preview-loading');

    // 2. Render placeholder without session
    S = null;
    const htmlNoSession = _inlineMediaHtmlForRef('docs/guide.md');
    const c2 = new FakeElement('div');
    c2.innerHTML = htmlNoSession;
    const loadEl2 = c2.querySelector('.md-inline-load');
    const link2 = loadEl2.querySelector('.msg-media-link');

    // 3. Test that loading text appears once loadMarkdownInline starts
    S = { session: { session_id: 'sess-123' } };
    let fetchStarted = false;
    global.fetch = () => new Promise(resolve => {
      fetchStarted = true;
      // keep pending to observe in-flight loading state
    });

    const c3 = new FakeElement('div');
    c3.innerHTML = _inlineMediaHtmlForRef('docs/guide.md', 'sess-123');
    const loadEl3 = c3.querySelector('.md-inline-load');
    const loadingBefore = loadEl3.querySelector('.md-preview-loading').getAttribute('style') || '';
    const isLoadedBefore = loadEl3.getAttribute('data-loaded');

    loadMarkdownInline(c3);

    const isLoadedAfter = loadEl3.getAttribute('data-loaded');
    const isLoadingAfter = loadEl3.classList.contains('is-loading');
    const loadingAfter = loadEl3.querySelector('.md-preview-loading').getAttribute('style') || '';

    console.log(JSON.stringify({
      hasLinkWithSession: !!link1,
      hrefWithSession: link1 ? link1.getAttribute('href') : null,
      downloadNameWithSession: link1 ? link1.getAttribute('download') : null,
      loadingStyleBefore: loadingEl1 ? (loadingEl1.getAttribute('style') || '') : null,
      hasLinkNoSession: !!link2,
      hrefNoSession: link2 ? link2.getAttribute('href') : null,
      isLoadedBefore: isLoadedBefore,
      loadingBefore: loadingBefore,
      isLoadedAfter: isLoadedAfter,
      isLoadingAfter: isLoadingAfter,
      loadingAfter: loadingAfter,
      fetchStarted: fetchStarted,
    }));
    """)
    out = json.loads(base._run_node(driver).splitlines()[-1])
    assert out["hasLinkWithSession"] is True
    assert out["hrefWithSession"] == "api/media?path=docs%2Fguide.md&session_id=sess-123&download=1"
    assert out["downloadNameWithSession"] == "guide.md"
    assert "display:none" in out["loadingStyleBefore"]

    assert out["hasLinkNoSession"] is True
    assert out["hrefNoSession"] == "api/media?path=docs%2Fguide.md&download=1"

    assert out["isLoadedBefore"] is None
    assert "display:none" in out["loadingBefore"]
    assert out["isLoadedAfter"] == "1"
    assert out["isLoadingAfter"] is True
    assert "display:none" not in out["loadingAfter"]
    assert out["fetchStarted"] is True


def test_load_markdown_inline_honors_snap_query_for_fetch_and_download():
    """Item (e): loadMarkdownInline() includes _mediaSnapQuery(el) in fetch and download URLs."""
    driver = _build_test_driver(r"""
    const snapHex = 'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789';
    let requestedUrl = null;
    global.fetch = (url) => {
      requestedUrl = String(url);
      return Promise.resolve({
        ok: true,
        text: () => Promise.resolve('# Hello Snap'),
      });
    };

    const container = new FakeElement('div');
    container.innerHTML = '<div class="md-inline-load" data-path="report.md" data-snap="' + snapHex + '"></div>';
    loadMarkdownInline(container);

    await new Promise(r => setTimeout(r, 20));

    const wrap = container.querySelector('.md-inline-wrap');
    const headerLink = wrap ? wrap.querySelector('.msg-media-link') : null;

    console.log(JSON.stringify({
      requestedUrl: requestedUrl,
      downloadHref: headerLink ? headerLink.getAttribute('href') : null,
    }));
    """)
    out = json.loads(base._run_node(driver).splitlines()[-1])
    snap_hex = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
    assert f"snap={snap_hex}" in out["requestedUrl"]
    assert "session_id=sess-123" in out["requestedUrl"]
    assert f"snap={snap_hex}" in out["downloadHref"]
    assert "session_id=sess-123" in out["downloadHref"]
    assert "download=1" in out["downloadHref"]


def test_forbidden_and_not_found_suppresses_broken_download_link():
    """Item (e): 403 / 404 responses use _mediaPreviewAllowsDownload() to omit download link."""
    driver = _build_test_driver(r"""
    async function testStatus(status) {
      global.fetch = () => Promise.resolve({
        ok: false,
        status: status,
      });

      const container = new FakeElement('div');
      container.innerHTML = '<div class="md-inline-load" data-path="secret.md"></div>';
      loadMarkdownInline(container);
      await new Promise(r => setTimeout(r, 20));

      const fallback = container.querySelector('.diff-inline-error, .md-inline-fallback');
      const link = fallback ? fallback.querySelector('.msg-media-link') : null;
      return {
        className: fallback ? fallback.className : null,
        text: fallback ? fallback.textContent : null,
        hasLink: !!link,
      };
    }

    const res403 = await testStatus(403);
    const res404 = await testStatus(404);

    // Network error (not 401/403/404) allows download
    global.fetch = () => Promise.reject(new Error('network'));
    const cNet = new FakeElement('div');
    cNet.innerHTML = '<div class="md-inline-load" data-path="net.md"></div>';
    loadMarkdownInline(cNet);
    await new Promise(r => setTimeout(r, 20));
    const fbNet = cNet.querySelector('.md-inline-fallback, .diff-inline-error');
    const linkNet = fbNet ? fbNet.querySelector('.msg-media-link') : null;

    console.log(JSON.stringify({
      res403: res403,
      res404: res404,
      netHasLink: !!linkNet,
      netLinkHref: linkNet ? linkNet.getAttribute('href') : null,
    }));
    """)
    out = json.loads(base._run_node(driver).splitlines()[-1])
    # 403: no download link!
    assert out["res403"]["hasLink"] is False
    assert "media_preview_forbidden" in out["res403"]["text"]
    assert "diff-inline-error" in out["res403"]["className"]

    # 404: no download link!
    assert out["res404"]["hasLink"] is False
    assert "media_preview_not_found" in out["res404"]["text"]
    assert "diff-inline-error" in out["res404"]["className"]

    # Generic network error: download link allowed
    assert out["netHasLink"] is True
    assert "download=1" in out["netLinkHref"]
    assert "session_id=sess-123" in out["netLinkHref"]


def test_oversized_markdown_keeps_snap_and_session_in_fallback():
    """Item (e): Oversized markdown retains session and snap in its fallback download link."""
    driver = _build_test_driver(r"""
    const snapHex = '11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff';
    global.fetch = () => Promise.resolve({
      ok: true,
      text: () => Promise.resolve('x'.repeat(300000)),
    });

    const container = new FakeElement('div');
    container.innerHTML = '<div class="md-inline-load" data-path="big.md" data-snap="' + snapHex + '"></div>';
    loadMarkdownInline(container);
    await new Promise(r => setTimeout(r, 20));

    const fallback = container.querySelector('.md-inline-fallback');
    const link = fallback ? fallback.querySelector('.msg-media-link') : null;

    console.log(JSON.stringify({
      hasFallback: !!fallback,
      hasLink: !!link,
      linkHref: link ? link.getAttribute('href') : null,
    }));
    """)
    out = json.loads(base._run_node(driver).splitlines()[-1])
    assert out["hasFallback"] is True
    assert out["hasLink"] is True
    snap_hex = "11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff"
    assert f"snap={snap_hex}" in out["linkHref"]
    assert "session_id=sess-123" in out["linkHref"]
    assert "download=1" in out["linkHref"]
