"""Regression tests for PR #8235 — bold nested inside italic/strikethrough must
not be escaped a second time by the later emphasis passes in renderMd().

Before the fix, ``*a **b** c*`` rendered ``<em>a &lt;strong&gt;b&lt;/strong&gt; c</em>``
(literal angle-bracket tags visible to the user) and an apostrophe inside the
bold span rendered as a literal ``&amp;#39;`` entity. The same shape broke in
every inlineMd() path (blockquote, list item, heading, table cell), and an
italic span nested inside strikethrough (``~~*a*~~``) leaked literal ``<em>``
tags because the tag stash ran before the italic pass.

These tests drive the REAL ``static/ui.js`` through node, like
``tests/test_issue1446_glued_heading_lift.py``, and also pin the XSS boundary:
raw HTML inside nested emphasis must still be escaped, because the fix only
protects renderer-emitted tag tokens, never user content.

One case also pins the token-collision guard: the emphasis stash must not use a
token that message text can reproduce. ``renderMd`` normalizes raw NUL bytes to
U+FFFD before any token exists, so text cannot carry a literal token; the
per-call random prefix is kept as defence in depth, and because ``\\x00B``
belongs to the autolink stash. A literal ``\\x00B0\\x00`` in a message is plain
text and must not disturb a following link.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parent.parent
UI_JS_PATH = REPO / "static" / "ui.js"
NODE = shutil.which("node")

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
// Older revisions predate this helper; the harness must still load the file so
// the same suite can run against a base revision for before/after comparison.
try { eval(extractFunc('_mdImageHtml')); } catch (e) {}
eval(extractFunc('renderMd'));
let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => { process.stdout.write(renderMd(buf)); });
"""

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("renderer8235_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _render(driver_path: str, markdown: str) -> str:
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        input=markdown, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return result.stdout


def _no_literal_tags(out: str) -> None:
    assert "&lt;strong&gt;" not in out and "&lt;/strong&gt;" not in out, out
    assert "&lt;em&gt;" not in out and "&lt;del&gt;" not in out, out
    assert "&amp;#39;" not in out and "&amp;quot;" not in out and "&amp;amp;" not in out, out


_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'"}


def _decode_entities(value: str) -> str:
    """Decode one HTML entity level, the way a browser reads an attribute."""
    return re.sub(r"&(?:amp|lt|gt|quot|#39);", lambda m: _ENTITIES[m.group(0)], value)


def _attr(html: str, name: str) -> str:
    """The decoded value of the first ``name="..."`` attribute."""
    m = re.search(name + r'="([^"]*)"', html)
    assert m, html
    return _decode_entities(m.group(1))


# ── The reported bug ──────────────────────────────────────────────────────────

def test_bold_inside_italic_renders_real_strong(driver_path):
    out = _render(driver_path, "*a **b** c*")
    assert "<em>a <strong>b</strong> c</em>" in out, out
    _no_literal_tags(out)


def test_blockquoted_italic_quote_with_bold_phrase(driver_path):
    src = '> *"I wouldn\'t recommend **setting alerts** on this data."*'
    out = _render(driver_path, src)
    assert "<blockquote>" in out and "<strong>setting alerts</strong>" in out, out
    assert "wouldn&#39;t" in out, out
    _no_literal_tags(out)


def test_apostrophe_inside_bold_inside_italic_not_double_escaped(driver_path):
    out = _render(driver_path, "*a **it's** b*")
    assert "<em>a <strong>it&#39;s</strong> b</em>" in out, out
    _no_literal_tags(out)


def test_ampersand_inside_bold_inside_italic_not_double_escaped(driver_path):
    out = _render(driver_path, "*a **b & c** d*")
    assert "<strong>b &amp; c</strong>" in out, out
    _no_literal_tags(out)


def test_bold_inside_strikethrough(driver_path):
    out = _render(driver_path, "~~a **b** c~~")
    assert "<del>a <strong>b</strong> c</del>" in out, out


def test_italic_inside_strikethrough(driver_path):
    """The italic pass emits <em>/</em> AFTER the first stash; they must be
    stashed too or the strikethrough pass escapes them into literal text."""
    out = _render(driver_path, "~~*a*~~")
    assert "<del><em>a</em></del>" in out, out
    _no_literal_tags(out)


def test_bold_inside_italic_inside_strikethrough(driver_path):
    out = _render(driver_path, "~~*a **b** c*~~")
    assert "<del><em>a <strong>b</strong> c</em></del>" in out, out
    _no_literal_tags(out)


@pytest.mark.parametrize("src,wrapper", [
    ("- item *x **y** z*", "<li>"),
    ("# *h **b** i*", "<h1>"),
    ("| *a **b** c* |\n|---|\n| *d **e** f* |", "<td>"),
    ("> > *a **b** c*", "<blockquote><blockquote>"),
])
def test_inline_md_paths_render_nested_emphasis(driver_path, src, wrapper):
    out = _render(driver_path, src)
    assert wrapper in out, out
    assert "<em>" in out and "<strong>" in out, out
    _no_literal_tags(out)


def test_bold_label_inside_italic_link(driver_path):
    out = _render(driver_path, "*[**b**](https://e.com)*")
    assert '<a href="https://e.com"' in out and "<strong>b</strong></a>" in out, out
    _no_literal_tags(out)


# ── Token-collision guard (review finding) ───────────────────────────────────

def test_literal_nul_token_in_text_does_not_break_a_link(driver_path):
    """renderMd() does not strip NUL bytes from its input. A message containing
    a literal ``\\x00B0\\x00`` used to be restored as a stashed tag, so the
    following link lost its href and the attribute text leaked into the page."""
    out = _render(driver_path, "[x](https://example.com/\x00B0\x00) **y**")
    assert 'href="https://example.com/' in out, out


def test_literal_nul_token_in_text_does_not_break_a_link_with_emphasis(driver_path):
    out = _render(driver_path, "[x](https://example.com/\x00B0\x00) *a **b** c*")
    assert 'href="https://example.com/' in out, out
    _no_literal_tags(out)


# ── Emphasized link whose destination contains bold markers (review finding) ─

@pytest.mark.parametrize("src", [
    "*[x](https://example.com/**b**)*",      # the exact reviewed input
    "[x](https://example.com/**b**)",        # same destination without italic
    "*[x](https://example.com/**b**/c)*",    # markers mid-path
])
def test_link_with_bold_markers_in_url_keeps_a_clickable_href(driver_path, src):
    """The emphasis stash restores renderer-emitted ``<strong>`` before links are
    built, so a destination containing bold markers reached ``_markdownAnchor``
    with markup in it. Interpolated raw, that closed the ``href`` attribute
    early: the anchor lost its href and ``" target="_blank" rel="noopener">``
    leaked into the page as text. The destination must be attribute-escaped.

    The destination itself is still mangled by the emphasis pass (the ``**``
    markers become escaped ``<strong>`` tags, exactly as on master) — this test
    pins that the link stays clickable and well-formed, not the marker text.
    """
    out = _render(driver_path, src)
    anchor = re.search(r"<a\b[^>]*>x</a>", out)
    assert anchor, out
    tag = anchor.group(0)
    open_tag = tag[:tag.index(">") + 1]
    assert re.search(r'href="https://example\.com/[^"]*b[^"]*"', open_tag), out
    assert open_tag.count("<") == 1, out           # no markup inside the attributes
    assert out.count('target="_blank"') == 1, out  # no attribute text leaked out


def test_link_destination_with_quotes_cannot_add_attributes(driver_path):
    """The same shape with quotes in the destination: the old raw interpolation
    dropped everything from the first quote into the page text. The escaped
    destination keeps one well-formed anchor and no extra attributes."""
    out = _render(driver_path, '*[x](https://e.com/**a**"onmouseover="alert(1))*')
    anchor = re.search(r"<a\b[^>]*>x</a>", out)
    assert anchor, out
    assert "onmouseover=alert(1)>" not in anchor.group(0), out
    assert 'href="https://e.com/' in anchor.group(0), out


# ── Destination fidelity (gate review finding) ───────────────────────────────

def test_link_destination_with_angle_bracket_survives(driver_path):
    """The destination is escaped for the attribute and then re-emitted by the
    final tag sanitizer. Escaping the already-escaped text escaped ``<`` twice,
    so ``?q=1<2`` decoded back to ``?q=1&lt;2`` — a destination the renderer
    preserved before the emphasis stash existed. The attribute must decode to the
    destination the author wrote."""
    out = _render(driver_path, "[x](https://example.com/search?q=1<2)")
    assert _attr(out, "href") == "https://example.com/search?q=1<2", out
    assert "&amp;lt;" not in out, out


def test_link_destination_with_ampersand_query_unchanged(driver_path):
    """The ordinary query-string control: one level of escaping, decoded once."""
    out = _render(driver_path, "[x](https://e.com/?a=1&b=2)")
    assert _attr(out, "href") == "https://e.com/?a=1&b=2", out


@pytest.mark.parametrize("src", [
    "![pic](https://app.example/**b**.png)",
    "*![pic](https://app.example/**b**.png)*",
])
def test_image_with_emphasis_markers_in_url_still_renders(driver_path, src):
    """Restored emphasis tags must not reach the src attribute raw. The <img>
    stash stops at the first ``>``, so a raw tag inside src cut the image tag
    short and dumped ``b</strong>.png" alt="pic"`` into the transcript as text."""
    out = _render(driver_path, src)
    tag = re.search(r"<img\b[^>]*>", out)
    assert tag, out
    assert out.count("<img") == 1, out
    assert 'alt="pic"' in tag.group(0), out
    assert _attr(tag.group(0), "src").endswith(".png"), out
    # nothing from the tag may survive as visible text
    assert "alt=" not in out.replace(tag.group(0), ""), out


def test_adjacent_backtick_token_text_stays_plain(driver_path):
    """A token-shaped string next to inline code is message text, not a token:
    ``renderMd`` normalizes raw NULs, so it must render literally."""
    out = _render(driver_path, "`a`I0`b`")
    assert "<code>a</code>I0<code>b</code>" in out, out


def test_link_destination_with_math_token_keeps_no_markup(driver_path):
    """A stash token inside a destination must be dropped, not restored as
    markup: the math pass would otherwise insert ``<span class="katex-inline">``
    into the quoted href after the sanitizer ran and break the link."""
    out = _render(driver_path, "[x](https://e.com/$a$)")
    assert "katex" not in out, out
    assert _attr(out, "href") == "https://e.com/", out


def test_image_destination_with_angle_bracket_survives(driver_path):
    """The image sanitizer re-emits the src _mdImageHtml() already escaped; it
    must decode one level too, or ``?q=1<2`` decodes to ``?q=1&lt;2`` and the
    request goes to the wrong URL."""
    out = _render(driver_path, "![p](https://app.example/image.png?q=1<2)")
    tag = re.search(r"<img\b[^>]*>", out)
    assert tag, out
    assert _attr(tag.group(0), "src") == "https://app.example/image.png?q=1<2", out


# ── No regression for the previously-working shapes ──────────────────────────

@pytest.mark.parametrize("src,expected", [
    ("**a *b* c**", "<strong>a <em>b</em> c</strong>"),
    ("***a*** *b*", "<strong><em>a</em></strong> <em>b</em>"),
    ("**`code`**", "<strong><code>code</code></strong>"),
    ("*`a*b`*", "<em><code>a*b</code></em>"),
    ("*a ~~b~~ c*", "<em>a <del>b</del> c</em>"),
    ('*say "hi" & bye*', "<em>say &quot;hi&quot; &amp; bye</em>"),
])
def test_existing_emphasis_shapes_unchanged(driver_path, src, expected):
    assert expected in _render(driver_path, src)


# ── XSS boundary: user HTML inside nested emphasis is still escaped ───────────

@pytest.mark.parametrize("src,forbidden", [
    ("*<img src=x onerror=alert(1)>*", "<img"),
    ("**<svg onload=alert(1)>**", "<svg"),
    ('*<a href="javascript:alert(1)">x</a>*', "<a"),
    ("*x</strong><img src=x onerror=alert(1)>*", "<img"),
    ("*&lt;script&gt;alert(1)&lt;/script&gt;*", "<script"),
    ("*<strong onclick=alert(1)>x</strong>*", "onclick=alert(1)>"),
    ("<img src=x onerror=**alert(1)**>", "<img"),
    ("~~*<img src=x onerror=alert(1)>*~~", "<img"),
])
def test_raw_html_inside_nested_emphasis_stays_escaped(driver_path, src, forbidden):
    out = _render(driver_path, src)
    assert forbidden not in out, out


def test_entity_encoded_script_stays_text(driver_path):
    out = _render(driver_path, "*&lt;script&gt;alert(1)&lt;/script&gt;*")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out, out
