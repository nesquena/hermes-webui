"""data:image URI rendering in chat markdown (base64 images must render, not dump text).

Covers the renderer fixes for model-emitted base64 images:
  - ![alt](data:image/...;base64,...)   -> inline <img>  (was: raw base64 text)
  - <img src="data:image/...">          -> inline <img>  (was: swallowed entirely)
  - MEDIA:data:image/...                -> inline <img>  (was: broken api/media link)
  - ![alt](file:///x.png)               -> media <img>   (was: "!<a>" anchor bug)
Safety invariants:
  - data:text/html and every non-image data: scheme stays inert text / blocked
  - oversized data:image URIs (> _DATA_IMAGE_MAX_LEN) degrade to truncated text
  - event-handler attributes on raw <img> are stripped
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQ"
    "GAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_URI = f"data:image/png;base64,{PNG_B64}"

_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
global.window = {};
global.document = { createElement: () => ({ innerHTML: '', textContent: '' }), baseURI: 'http://127.0.0.1:8787/' };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;
const _PDF_EXTS=/\.pdf$/i;
const _HTML_EXTS=/\.html?$/i;
const _CSV_EXTS=/\.(csv|tsv)$/i;
const _EXCALIDRAW_EXTS=/\.excalidraw$/i;
const _mediaKindForName=(name='')=>{
  const clean=String(name||'').split('?')[0].toLowerCase();
  if(_AUDIO_EXTS.test(clean)) return 'audio';
  if(_VIDEO_EXTS.test(clean)) return 'video';
  if(_IMAGE_EXTS.test(clean)) return 'image';
  return '';
};
const _mediaPlayerHtml=(k,s,n)=>`<${k} src="${esc(s)}"></${k}>`;
const t = k => k;
const S = {};

// The data-image consts and predicate are production code. Extract/eval them
// in source order so the renderer functions run against the REAL policy, not
// a test stand-in.
for (const name of ['_DATA_IMAGE_RE', '_DATA_IMAGE_SVG_RE', '_DATA_IMAGE_MAX_LEN']) {
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
eval(extractFunc('_isSafeDataImageUri'));
eval(extractFunc('_dataImageHtml'));
eval(extractFunc('_mdImageHtml'));
eval(extractFunc('_inlineMediaHtmlForRef'));
eval(extractFunc('_matchBacktickFenceLine'));
eval(extractFunc('_isBacktickFenceClose'));
eval(extractFunc('renderMd'));

let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => {
  const input = process.argv[3] === '--lone-surrogate'
    ? '![x]\n(/home/joe/.hermes/cache/images/x\uD800.png)'
    : buf;
  process.stdout.write(renderMd(input));
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("data_uri_renderer") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _render(driver_path: str, markdown: str, *driver_args: str) -> str:
    result = subprocess.run(
        [NODE, driver_path, str(REPO_ROOT / "static" / "ui.js"), *driver_args],
        input=markdown,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout


def test_markdown_data_image_renders_img(driver_path):
    html = _render(driver_path, f"![chart]({PNG_URI})")
    assert f'src="{PNG_URI}"' in html
    assert 'class="msg-media-img"' in html
    assert "![chart]" not in html, "base64 must not appear as raw markdown text"


def test_raw_img_tag_with_data_image_survives(driver_path):
    html = _render(driver_path, f'<img src="{PNG_URI}">')
    assert f'src="{PNG_URI}"' in html, "raw <img data:image> must not be swallowed"


def test_media_token_with_data_image_renders_img(driver_path):
    html = _render(driver_path, f"MEDIA:{PNG_URI}")
    assert f'src="{PNG_URI}"' in html
    assert "api/media?path=data" not in html, "data: URI must never route to api/media"


def test_markdown_image_with_line_break_before_bare_cache_path_renders_inline(driver_path):
    html = _render(driver_path, "![Photorealistic cat on a tightrope]\n(/home/joe/.hermes/cache/images/mai_test.png)")
    assert "api/media?path=%2Fhome%2Fjoe%2F.hermes%2Fcache%2Fimages%2Fmai_test.png" in html
    assert '<img' in html
    assert "![Photorealistic" not in html


@pytest.mark.parametrize(
    "markdown",
    [
        "`![x](/home/joe/.hermes/cache/images/x.png)`",
        "- `![x](/home/joe/.hermes/cache/images/x.png)`",
        "| code |\n| --- |\n| `![x](/home/joe/.hermes/cache/images/x.png)` |",
    ],
    ids=["paragraph", "list", "table"],
)
def test_markdown_bare_cache_image_inside_inline_code_stays_code(driver_path, markdown):
    html = _render(driver_path, markdown)
    assert "api/media" not in html
    assert "<img" not in html
    assert "<code>![x](/home/joe/.hermes/cache/images/x.png)</code>" in html


@pytest.mark.parametrize(
    "code_markup",
    [
        "<code>![x](/home/joe/.hermes/cache/images/x.png)</code>",
        "&lt;code&gt;![x](/home/joe/.hermes/cache/images/x.png)&lt;/code&gt;",
    ],
    ids=["raw-html-code", "entity-escaped-code"],
)
def test_markdown_image_inside_code_markup_stays_literal(driver_path, code_markup):
    html = _render(driver_path, f"See {code_markup} for the syntax.")
    assert "api/media" not in html
    assert "<img" not in html
    assert "![x](/home/joe/.hermes/cache/images/x.png)" in html


def test_markdown_lone_surrogate_cache_path_stays_inert(driver_path):
    # The surrogate is constructed inside Node; text-mode stdin would replace it
    # with U+FFFD before renderMd() can exercise encodeURIComponent().
    html = _render(driver_path, "", "--lone-surrogate")
    assert "api/media" not in html
    assert "<img" not in html
    assert "![x]" in html and "cache/images/" in html


@pytest.mark.parametrize(
    "target,expected",
    [
        ("https://example.test/chart.png", "src=\"https://example.test/chart.png\""),
        ("file:///tmp/chart.png", "api/media?path=%2Ftmp%2Fchart.png"),
        (PNG_URI, PNG_URI),
    ],
    ids=["https", "file", "data-image"],
)
def test_explicit_markdown_image_in_table_cell_still_renders(driver_path, target, expected):
    html = _render(driver_path, f"| image |\n| --- |\n| ![chart]({target}) |")
    assert "<table>" in html
    assert "<img" in html
    assert expected in html


def test_markdown_root_relative_web_image_stays_unchanged(driver_path):
    html = _render(driver_path, "![logo](/images/logo.png)")
    assert "api/media" not in html
    assert "file:///images/logo.png" not in html
    assert "![logo](/images/logo.png)" in html


def test_markdown_protocol_relative_image_stays_unchanged(driver_path):
    html = _render(driver_path, "![remote](//cdn.example/image.png)")
    assert "api/media" not in html
    assert "file://" not in html
    assert "![remote](//cdn.example/image.png)" in html


def test_markdown_blank_line_does_not_join_image_paragraphs(driver_path):
    html = _render(driver_path, "![label]\n\n(/home/joe/.hermes/cache/images/mai_test.png)")
    assert "api/media" not in html
    assert '<img' not in html


def test_markdown_encoded_cache_traversal_stays_inert(driver_path):
    html = _render(driver_path, "![x]\n(/home/joe/.hermes/cache/images/%2e%2e%2fsecrets.png)")
    assert "api/media" not in html
    assert '<img' not in html


def test_markdown_backslash_cache_traversal_stays_inert(driver_path):
    html = _render(driver_path, "![x]\n(/home/joe/.hermes/cache/images/..%5csecrets.png)")
    assert "api/media" not in html
    assert '<img' not in html


def test_markdown_percent_filename_renders_inline(driver_path):
    html = _render(driver_path, "![photo]\n(/home/joe/.hermes/cache/images/photo%25done.png)")
    assert "api/media?path=%2Fhome%2Fjoe%2F.hermes%2Fcache%2Fimages%2Fphoto%25done.png" in html
    assert '<img' in html


def test_markdown_double_encoded_slash_traversal_stays_inert(driver_path):
    html = _render(driver_path, "![x]\n(/home/joe/.hermes/cache/images/%252e%252e%252fprivate.png)")
    assert "api/media" not in html
    assert '<img' not in html


def test_markdown_double_encoded_backslash_traversal_stays_inert(driver_path):
    html = _render(driver_path, "![x]\n(/home/joe/.hermes/cache/images/%252e%252e%255cprivate.png)")
    assert "api/media" not in html
    assert '<img' not in html


def test_markdown_tilde_cache_path_stays_inert(driver_path):
    html = _render(driver_path, "![x]\n(~/cache/images/mai_test.png)")
    assert "api/media" not in html
    assert '<img' not in html



def test_markdown_file_image_renders_media_img_not_anchor_bug(driver_path):
    html = _render(driver_path, "![chart](file:///tmp/chart.png)")
    assert "api/media?path=%2Ftmp%2Fchart.png" in html
    assert "<img" in html
    assert "!<a" not in html, 'the "!<a>" rendering bug must stay fixed'


def test_https_image_unchanged(driver_path):
    html = _render(driver_path, "![capy](https://example.com/capy.png)")
    assert "<img" in html and 'src="https://example.com/capy.png"' in html
    assert "msg-media-img" in html


def test_media_local_path_unchanged(driver_path):
    html = _render(driver_path, "MEDIA:/tmp/chart.png")
    assert "api/media?path=%2Ftmp%2Fchart.png" in html
    assert 'class="msg-media-img"' in html


def test_data_text_html_markdown_stays_inert(driver_path):
    uri = "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="
    html = _render(driver_path, f"![x]({uri})")
    assert "<script" not in html
    assert 'src="data:text/html' not in html


def test_data_text_html_raw_img_stays_blocked(driver_path):
    uri = "data:text/html;base64,PHNjcmlwdD48L3NjcmlwdD4="
    html = _render(driver_path, f'<img src="{uri}">')
    assert 'src="data:text/html' not in html
    assert "<script" not in html


def test_media_token_non_image_data_uri_inert(driver_path):
    uri = "data:application/octet-stream;base64,AAAA"
    html = _render(driver_path, f"MEDIA:{uri}")
    assert "api/media?path=data" not in html
    assert 'src="data:application' not in html


def test_oversized_data_image_degrades_to_text(driver_path):
    big = "data:image/png;base64," + "A" * (2 * 1024 * 1024 + 100)
    html = _render(driver_path, f"![x]({big})")
    assert "<img" not in html
    assert len(html) < 5000, "oversized data URI must be truncated, not echoed"


def test_onerror_attribute_stripped_from_data_image(driver_path):
    html = _render(driver_path, f'<img src="{PNG_URI}" onerror="alert(1)">')
    assert "onerror" not in html
    assert f'src="{PNG_URI}"' in html


def test_image_between_raw_code_elements_still_renders(driver_path):
    markdown = "<code>first`last</code> ![x](https://example.test/a.png) <code>end</code>"
    html = _render(driver_path, markdown)
    assert html.count("<img") == 1
    assert 'src="https://example.test/a.png"' in html
