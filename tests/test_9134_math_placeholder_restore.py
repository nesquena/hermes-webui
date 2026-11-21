"""Behavioural tests for the math-placeholder restore fix (#9134, item 5).

These tests drive the ACTUAL renderMd() in static/ui.js via node — the same
harness as test_renderer_js_behaviour.py — so they exercise the real
production placeholder pipeline (math stash -> inlineMd -> autolink stash ->
restore) instead of a test-owned copy of the regexes.

Bug: the placeholder restores in inlineMd() run one kind at a time. In
`- $x$L0[a](https://f.com)` the math pre-pass stashes `$x$` as the NUL-wrapped
token `\\x00M0\\x00`, and the link pass then stashes `[a](https://f.com)` as
`\\x00L0\\x00`, so the string handed to the restores is
`\\x00M0\\x00L0\\x00L0\\x00`. The per-kind restore `/\\x00L(\\d+)\\x00/g`
starts matching at the MATH placeholder's closing NUL (the one right before
the literal text `L0`), consumes the literal as if it were the placeholder
index, and tears the math token's trailing NUL off. The real link token then
loses its opening NUL and can never be restored: the link is lost, and the
half-consumed token stream leaks NUL characters (plus a bare `B2` index from
the outer autolink stash) into the final HTML.

Fix: restore L and G in a single pass right after the T pass (one
left-to-right scan consumes every token boundary exactly once, so a match can
never start inside a neighbouring token), and give C and O the same
single-pass shape. T stays in its own earlier pass because a restored T entry
can itself contain L/G placeholder text.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
global.window = {};
global.document = { createElement: () => ({ innerHTML: '', textContent: '' }), baseURI: 'http://localhost/app/' };
function _sessionUrlForSid(sid) { return '/app/session/' + encodeURIComponent(String(sid || '')); }
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;
// Minimal stand-in for ui.js' _inlineMediaHtmlForRef used when the driver
// extracts only renderMd(). Mirrors the live UI for the cases the existing
// tests assert against (https image, bare file:// image).
function _inlineMediaHtmlForRef(ref){
  const r = String(ref || '');
  if (/^https?:\/\//.test(r)) return `<img class="msg-media-img" src="${esc(r)}" alt="image" loading="lazy">`;
  if (/^file:\/\//i.test(r)){
    const m = r.replace(/^file:\/\//i, '');
    return `<img class="msg-media-img" src="api/media?path=${encodeURIComponent(m)}" alt="image" loading="lazy">`;
  }
  return `<img class="msg-media-img" src="api/media?path=${encodeURIComponent(r)}" alt="image" loading="lazy">`;
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
eval(extractFunc('_matchBacktickFenceLine'));
eval(extractFunc('_isBacktickFenceClose'));
eval(extractFunc('_mediaTokenParts'));
eval(extractFunc('renderMd'));

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => { process.stdout.write(renderMd(buf)); });
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    """Write the node driver to a tmp file (works around `node -e` arg quirks)."""
    p = tmp_path_factory.mktemp("math_placeholder_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _render(driver_path, markdown: str) -> str:
    """Run renderMd against the actual ui.js and return the rendered HTML."""
    env = dict(os.environ)
    env.pop("NODE_CHANNEL_FD", None)  # node SIGABRTs on exit if it survives
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        input=markdown,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return result.stdout


class TestMathPlaceholderThenLink:
    """#9134 item 5: the reported shape, driven through the real renderMd()."""

    def test_math_placeholder_before_link_keeps_link_and_no_nul(self, driver_path):
        out = _render(driver_path, "- $x$L0[a](https://f.com)")
        assert "\x00" not in out, f"NUL leaked into rendered HTML. Got: {out!r}"
        assert out == (
            '<ul><li><span class="katex-inline" data-katex="inline">x</span>'
            'L0<a href="https://f.com" target="_blank" rel="noopener">a</a></li></ul>'
        ), (
            "math, literal L0 and the link must all survive in order. "
            f"Got: {out!r}"
        )


class TestMathPlaceholderSiblings:
    """The same misread shape for the G (image) and O (block-code) restores.

    The literal text after the math token spells a placeholder index, so the
    per-kind restore starts at the math token's closing NUL. These shapes leak
    NUL into the final HTML on the unfixed tree.

    Each case asserts the FULL rendered HTML, so the neighbouring content is
    pinned in one piece: the math span, the literal marker (`G0`/`O0`),
    and the following element must ALL survive, in order, with no NUL residue.
    On the unfixed #9134 baseline these exact shapes render as
    `B2\\x00G0\\x00` / `T0\\x00x</code>O0\\x00` — the neighbouring content is
    consumed by the misread restore, so every full-HTML assertion below fails
    there and only passes once the restore is fixed.
    """

    def test_math_placeholder_before_image_keeps_image_and_no_nul(self, driver_path):
        out = _render(driver_path, '- $x$G0<img src="https://e.com/x.png">')
        assert "\x00" not in out, f"NUL leaked into rendered HTML. Got: {out!r}"
        assert out == (
            '<ul><li><span class="katex-inline" data-katex="inline">x</span>'
            'G0<img src="https://e.com/x.png" alt=""></li></ul>'
        ), (
            "math, literal G0 and the image must all survive in order. "
            f"Got: {out!r}"
        )

    def test_math_placeholder_before_code_keeps_code_and_no_nul(self, driver_path):
        out = _render(driver_path, "- $x$O0`x`")
        assert "\x00" not in out, f"NUL leaked into rendered HTML. Got: {out!r}"
        assert out == (
            '<ul><li><span class="katex-inline" data-katex="inline">x</span>'
            'O0<code>x</code></li></ul>'
        ), (
            "math, literal O0 and the code span must all survive in order. "
            f"Got: {out!r}"
        )


class TestLinkedImageRestore:
    """Greptile P1: linked images must survive the placeholder restores.

    `- [![alt](https://e.com/x.png)](https://f.com)` stashes the image as a
    `\\x00G0\\x00` token inside the stored link body, so restoring L and G in
    one pass leaves the G token embedded in the anchor (the later G pass never
    rescans restored text). The restore expands G tokens inside the stashed
    link body only, so the linked image survives without re-opening the
    #9134 item-5 boundary misread.

    On the pre-fix head this shape renders as
    `<a href="https://f.com" target="_blank" rel="noopener">\\x00G0\\x00</a>`
    -- the image is lost and NUL leaks into the HTML, so these assertions red.
    """

    def test_linked_image_renders_inside_anchor_with_correct_attrs(self, driver_path):
        out = _render(driver_path, "- [![alt](https://e.com/x.png)](https://f.com)")
        assert "\x00" not in out, f"NUL leaked into rendered HTML. Got: {out!r}"
        a_open = out.find('<a href="https://f.com"')
        a_close = out.find("</a>")
        img_open = out.find("<img")
        img_close = out.find(">", img_open) if img_open >= 0 else -1
        assert a_open >= 0 and a_close > a_open, f"link missing. Got: {out!r}"
        assert img_open >= 0 and img_close > img_open, f"image missing. Got: {out!r}"
        assert a_open < img_open < img_close < a_close, (
            f"the <img> must be nested INSIDE the <a>. Got: {out!r}"
        )
        img_tag = out[img_open:img_close + 1]
        assert 'src="https://e.com/x.png"' in img_tag, f"img src wrong. Got: {img_tag!r}"
        assert 'alt="alt"' in img_tag, f"img alt wrong. Got: {img_tag!r}"
        assert out.count("<a ") == 1, f"link double-wrapped. Got: {out!r}"
        assert out.count("<img ") == 1, f"image duplicated. Got: {out!r}"

    def test_math_before_linked_image_keeps_math_literal_and_link(self, driver_path):
        """Math + literal G0 adjacent to a linked image: all four survive."""
        out = _render(driver_path, "- $x$G0[![alt](https://e.com/x.png)](https://f.com)")
        assert "\x00" not in out, f"NUL leaked into rendered HTML. Got: {out!r}"
        math_idx = out.find('<span class="katex-inline" data-katex="inline">x</span>')
        literal_idx = out.find("G0")
        a_open = out.find('<a href="https://f.com"')
        img_open = out.find("<img")
        assert math_idx >= 0 and literal_idx >= 0 and a_open >= 0 and img_open >= 0
        assert math_idx < literal_idx < a_open < img_open, (
            "math, literal G0, link, then image must keep their order. "
            f"Got: {out!r}"
        )
        assert 'src="https://e.com/x.png"' in out and out.count("<img ") == 1


class TestRegressionControls:
    """Ordinary renders must not change: links, images, code, uppercase tags.

    Every case here renders correctly on the unfixed tree — they are the
    controls that keep the single-pass restore from breaking the normal path.
    """

    def test_plain_markdown_link(self, driver_path):
        out = _render(driver_path, "- [a](https://f.com)")
        assert 'href="https://f.com"' in out
        assert ">a</a>" in out
        assert out.count("<a ") == 1, f"link must not be double-wrapped. Got: {out!r}"

    def test_plain_image(self, driver_path):
        out = _render(driver_path, '- <img src="https://e.com/x.png">')
        assert '<img src="https://e.com/x.png"' in out
        assert out.count("<img ") == 1

    def test_inline_code(self, driver_path):
        out = _render(driver_path, "- `x`")
        assert "<code>x</code>" in out
        assert "\x00" not in out

    def test_block_code(self, driver_path):
        """Block code goes through the _ob_stash / \\x00O restore in renderMd()."""
        out = _render(driver_path, "```\nx\n```")
        assert "<pre><code>x</code></pre>" in out
        assert "\x00" not in out

    def test_uppercase_tag_no_nested_link(self, driver_path):
        """#9134 item 6 control: already fixed on master, must stay fixed."""
        out = _render(driver_path, '- <A HREF="https://e.com">x</A>')
        assert '<a href="https://e.com">x</a>' in out
        assert out.count("<a ") == 1, f"nested link produced. Got: {out!r}"
        assert "\x00" not in out

    def test_unterminated_comment_not_swallowed(self, driver_path):
        """#9134 item 1 control: already fixed on master, must stay fixed."""
        out = _render(driver_path, "- text <!-- more")
        assert "text <!-- more" in out, f"trailing prose was swallowed. Got: {out!r}"
        assert "\x00" not in out
