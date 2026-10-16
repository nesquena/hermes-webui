"""Issue #7634 — generated local images must render, not leak alt text + path.

Two distinct defects, both reproduced against the REAL renderMd() in
static/ui.js (not a Python mirror):

  1. Whitespace/newline between ``]`` and ``(``.  The image regex required
     ``]`` and ``(`` to be adjacent, so a model that wrapped the target
     across a line break produced ``![alt]\\n(url)`` and the alt text plus
     the raw path stayed visible in the chat.

  2. Bare local paths.  Hermes image generation returns a filesystem path
     (e.g. ``/home/user/.hermes/cache/images/mai_20260918_101400_2248f18b.png``).
     The regex URL group only accepted a scheme (https?:// | file:// |
     data:image/), so the bare path never matched at all — defect 2 fires
     even on the compact ``![alt](/abs/path.png)`` form.

Fix scope is deliberately narrow: only image extensions under a
``/cache/images/`` segment are promoted to a media reference.  Everything
else the maintainer called out stays inert:

  * ordinary root-relative web assets (``/images/logo.png``) — not an image
  * protocol-relative URLs (``//host/x.png``) — not an image
  * blank-paragraph continuations (``![a]`` + blank line + ``(path)``)
  * literal (``../``) and percent-encoded traversal
  * backslash traversal
  * the ``~/...`` tilde form (URL parsing drops ``~`` as a host)

All of it funnels through the existing session-authorized ``/api/media``
route; no server allowlist is widened.  See ``_mdLocalImagePathRef`` and
the image regex in static/ui.js.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


# Extract/eval the real functions and policy constants out of ui.js so the
# assertions run against production code.  Same pattern as
# tests/test_data_uri_images.py.
_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
global.window = {};
global.document = {
  createElement: () => ({ innerHTML: '', textContent: '' }),
  baseURI: 'http://127.0.0.1:8787/',
  addEventListener: () => {},
  querySelectorAll: () => [],
  readyState: 'complete',
};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function _sessionUrlForSid(sid) { return '/session/' + encodeURIComponent(String(sid || '')); }
const t = k => k;
const li = () => '';
const S = {};
// Remote images are out of scope here; allow so the local-path branch is the
// only thing under test.
function _remoteImageAllowed() { return true; }
function _remoteImagePlaceholderHtml(u) { return `<a href="${esc(u)}">[img]</a>`; }
function _mediaPlayerHtml(k, s, n) { return `<${k} src="${esc(s)}"></${k}>`; }

for (const name of ['_DATA_IMAGE_RE', '_DATA_IMAGE_SVG_RE', '_DATA_IMAGE_MAX_LEN',
                    '_IMAGE_EXTS', '_PDF_EXTS', '_HTML_EXTS', '_SVG_EXTS',
                    '_AUDIO_EXTS', '_VIDEO_EXTS', '_CSV_EXTS',
                    '_EXCALIDRAW_EXTS']) {
  const m = src.match(new RegExp('const ' + name + '=([^\\n]*);'));
  if (!m) throw new Error(name + ' const not found in ui.js');
  globalThis[name] = eval('(' + m[1] + ')');
}

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

for (const name of ['_mediaKindForName', '_isSafeDataImageUri', '_dataImageHtml',
                    '_mdImageHtml', '_mediaTokenParts',
                    '_inlineMediaHtmlForRef', '_matchBacktickFenceLine',
                    '_isBacktickFenceClose', 'renderMd']) {
  eval(extractFunc(name));
}
// Introduced by the #7634 fix. Tolerated as absent so this file fails with
// readable assertion errors (not a driver crash) before the fix lands.
const _mdLocal = src.search(/function\s+_mdLocalImagePathRef\s*\(/);
if (_mdLocal >= 0) eval(extractFunc('_mdLocalImagePathRef'));

let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => { process.stdout.write(renderMd(buf)); });
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("md_image_7634") / "driver.js"
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


CACHE_PNG = "/home/user/.hermes/cache/images/mai_20260918_101400_2248f18b.png"
# The cache path URL-encoded the way api/media receives it.
CACHE_PNG_ENC = "api/media?path=%2Fhome%2Fuser%2F.hermes%2Fcache%2Fimages%2Fmai_20260918_101400_2248f18b.png"


# ── Defect 1: target split across lines ──────────────────────────────────────

def test_split_newline_cache_path_renders_image(driver_path):
    """The exact shape from issue #7634 must render, not leak alt text."""
    html = _render(driver_path, f"![Photorealistic cat on a tightrope]\n({CACHE_PNG})")
    assert CACHE_PNG_ENC in html
    # The raw markdown must be gone: no literal ![alt]( leaked into the chat.
    assert "![" not in html
    # The alt text is preserved as the image's alt attribute (that is the point
    # of the syntax), not as visible leaked text.
    assert 'alt="Photorealistic cat on a tightrope"' in html


def test_split_newline_file_url_renders(driver_path):
    html = _render(driver_path, f"![cat]\n(file://{CACHE_PNG})")
    assert CACHE_PNG_ENC in html
    assert "![" not in html


def test_split_newline_https_image_renders(driver_path):
    html = _render(driver_path, "![cat]\n(https://example.com/cat.png)")
    assert 'src="https://example.com/cat.png"' in html
    assert "![" not in html


def test_split_newline_data_image_renders(driver_path):
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQ"
        "GAhKmMIQAAAABJRU5ErkJggg=="
    )
    html = _render(driver_path, f"![dot]\n(data:image/png;base64,{png_b64})")
    assert "<img" in html
    assert "![" not in html


def test_compact_form_still_renders(driver_path):
    """Regression guard: the already-working compact form is untouched."""
    html = _render(driver_path, f"![cat]({CACHE_PNG})")
    assert CACHE_PNG_ENC in html
    assert "![" not in html


def test_compact_https_form_still_renders(driver_path):
    html = _render(driver_path, "![cat](https://example.com/cat.png)")
    assert 'src="https://example.com/cat.png"' in html


def test_compact_file_url_form_still_renders(driver_path):
    html = _render(driver_path, f"![cat](file://{CACHE_PNG})")
    assert CACHE_PNG_ENC in html


def test_inline_space_between_brackets_renders(driver_path):
    """Symmetric case: whitespace after '(' before the scheme."""
    html = _render(driver_path, "![cat] ( https://example.com/cat.png )")
    assert "<img" in html
    assert "![" not in html


def test_tab_between_brackets_renders(driver_path):
    html = _render(driver_path, "![cat]\t(https://example.com/cat.png)")
    assert "<img" in html
    assert "![" not in html


def test_image_in_table_cell_split_across_lines_renders(driver_path):
    """The inlineMd() call site (tables/lists) needs the same tolerance."""
    md = (
        "| image |\n"
        "| --- |\n"
        "| x |\n"
        "| y |\n\n"
        "![cat]\n(https://example.com/cat.png)"
    )
    html = _render(driver_path, md)
    assert 'src="https://example.com/cat.png"' in html


def test_image_in_list_item_split_across_lines_renders(driver_path):
    html = _render(driver_path, "- item\n\n![cat]\n(https://example.com/cat.png)")
    assert 'src="https://example.com/cat.png"' in html


# ── Defect 2: bare local cache paths ─────────────────────────────────────────

def test_bare_cache_path_compact_renders_via_media_api(driver_path):
    html = _render(driver_path, f"![cat]({CACHE_PNG})")
    assert CACHE_PNG_ENC in html
    assert 'class="msg-media-img"' in html


def test_bare_cache_path_split_across_lines_renders(driver_path):
    html = _render(driver_path, f"![cat]\n({CACHE_PNG})")
    assert CACHE_PNG_ENC in html


@pytest.mark.parametrize(
    "path",
    [
        "/home/u/.hermes/cache/images/a.png",
        "/home/u/.hermes/cache/images/a.jpg",
        "/home/u/.hermes/cache/images/a.jpeg",
        "/home/u/.hermes/cache/images/a.webp",
        "/root/.hermes/cache/images/nested/deep/a.png",
    ],
)
def test_cache_image_extensions_all_promoted(driver_path, path):
    html = _render(driver_path, f"![x]({path})")
    assert "<img" in html
    assert "![" not in html


def test_routes_through_session_authorized_media_route(driver_path):
    """Must never become a bare <img src="/abs/path"> the browser can't load."""
    html = _render(driver_path, f"![cat]({CACHE_PNG})")
    assert "api/media?path=" in html
    assert f'src="{CACHE_PNG}"' not in html


# ── Safety negatives (maintainer-specified) ─────────────────────────────────

def test_root_relative_web_asset_stays_inert(driver_path):
    """`/images/logo.png` is a web asset, not a local artifact."""
    html = _render(driver_path, "![logo](/images/logo.png)")
    assert "api/media?path=" not in html
    assert "![" in html


def test_protocol_relative_url_stays_inert(driver_path):
    html = _render(driver_path, "![x](//evil.example.com/cache/images/a.png)")
    assert "api/media?path=" not in html
    assert "![" in html


def test_blank_line_between_bracket_and_path_stays_inert(driver_path):
    """A blank line starts a new paragraph — must not be bridged."""
    html = _render(driver_path, f"![cat]\n\n({CACHE_PNG})")
    assert "api/media?path=" not in html


def test_literal_traversal_stays_inert(driver_path):
    html = _render(driver_path, "![x](/home/u/.hermes/cache/images/../../../etc/passwd.png)")
    assert "api/media?path=" not in html


def test_percent_encoded_traversal_stays_inert(driver_path):
    """Encoded separators must not pass the raw-string check and decode later."""
    html = _render(driver_path, "![x](/home/u/.hermes/cache/images/%2e%2e%2f%2e%2e%2fetc%2fpasswd.png)")
    assert "api/media?path=" not in html


def test_backslash_traversal_stays_inert(driver_path):
    html = _render(driver_path, "![x](/home/u/.hermes/cache/images/..\\..\\etc\\passwd.png)")
    assert "api/media?path=" not in html


def test_tilde_form_stays_inert(driver_path):
    """URL parsing drops `~` as a host, so this form points at the wrong place."""
    html = _render(driver_path, "![x](~/.hermes/cache/images/a.png)")
    assert "api/media?path=" not in html


def test_non_image_extension_in_cache_stays_inert(driver_path):
    html = _render(driver_path, "![x](/home/u/.hermes/cache/files/a.txt)")
    assert "api/media?path=" not in html


def test_link_without_exclam_is_not_an_image(driver_path):
    html = _render(driver_path, f"[label]\n({CACHE_PNG})")
    assert "<img" not in html


def test_plain_bracket_paren_in_prose_not_an_image(driver_path):
    html = _render(driver_path, "see the note ] and ( more text")
    assert "<img" not in html


def test_compact_image_in_inline_code_span_still_not_promoted(driver_path):
    """A code span is still a code span: the ref must not become a media fetch.

    Note the renderer only recognises single-line backtick spans (the stash
    regex forbids newlines), so this pins the genuine code-span case.
    """
    html = _render(driver_path, f"`see ![{CACHE_PNG}]`")
    assert "api/media?path=" not in html


def test_image_in_fenced_block_is_not_consumed(driver_path):
    md = f"```\n![cat]\n({CACHE_PNG})\n```"
    html = _render(driver_path, md)
    assert "api/media?path=" not in html


def test_data_text_html_stays_blocked(driver_path):
    uri = "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="
    html = _render(driver_path, f"![x]\n({uri})")
    assert "<script" not in html
    assert 'src="data:text/html' not in html


def test_javascript_scheme_stays_blocked(driver_path):
    html = _render(driver_path, "![x]\n(javascript:alert(1))")
    assert 'src="javascript:' not in html