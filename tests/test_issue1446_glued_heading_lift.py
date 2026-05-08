"""Regression tests for issue #1446 — glued-bold-heading lift in renderMd().

LLMs in thinking/reasoning mode frequently emit content shaped like:

    Paragraph 1 text text.**Heading to Paragraph 2**

    Paragraph 2 text text.**Heading to Paragraph 3**

    Paragraph 3 text...

The renderer correctly produces (per CommonMark):

    <p>Paragraph 1 text text.<strong>Heading to Paragraph 2</strong></p>
    <p>Paragraph 2 text text.<strong>Heading to Paragraph 3</strong></p>

But the visual effect is a "trailing emphasis on the body text" rather than a
section header for what follows. Cygnus reported this in Discord (May 1 2026,
relayed by @AvidFuturist).

Fix: pre-pass in `renderMd()` (and the Python mirror) lifts the glued bold into
its own paragraph when it sits at the end of a paragraph, follows a sentence
terminator (`.!?`), is reasonably short (≤80 chars), and is followed by a blank
line. Mid-paragraph emphasis like "this is **important** to know." is preserved.

Behavioral tests below split into two sections:

  - Python mirror tests use ``render_md`` from ``test_sprint16`` and verify
    the lift logic in cases where the mirror is faithful to the JS.
  - Node-driver tests at the bottom run against the ACTUAL ``static/ui.js``
    via ``node`` and pin the cases that depend on the JS-specific stash
    structure (fenced code, inline backticks) — these would false-fail
    against the simpler Python mirror.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

import pytest

from tests.test_sprint16 import render_md


REPO = Path(__file__).resolve().parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
UI_JS_PATH = REPO / "static" / "ui.js"
NODE = shutil.which("node")


# ── Behavior tests via the Python mirror ─────────────────────────────────────


def test_glued_bold_after_period_lifts_to_own_paragraph():
    """Sentence-glued **Heading** with period before it must be lifted to its own paragraph."""
    src = "Para text.**Bold heading**\n\nNext para."
    out = render_md(src)
    assert "<p>Para text.</p>" in out, f"Period sentence not isolated: {out!r}"
    assert "<p><strong>Bold heading</strong></p>" in out, (
        f"Lifted bold not in its own paragraph: {out!r}"
    )


def test_glued_bold_after_question_mark_lifts():
    """Glued-bold after `?` should also lift — common in LLM reasoning mode."""
    src = "Why does this happen?**The answer**\n\nNext para."
    out = render_md(src)
    assert "<p>Why does this happen?</p>" in out, out
    assert "<p><strong>The answer</strong></p>" in out, out


def test_glued_bold_after_exclamation_lifts():
    """Glued-bold after `!` should also lift — emphatic transition."""
    src = "Found it!**Section title**\n\nMore text."
    out = render_md(src)
    assert "<p>Found it!</p>" in out, out
    assert "<p><strong>Section title</strong></p>" in out, out


# ── Preserve-emphasis cases (no false positives) ─────────────────────────────


def test_mid_paragraph_bold_unchanged():
    """Bold mid-sentence with no period before and no `\\n\\n` after must NOT be lifted."""
    src = "This is **important** to know."
    out = render_md(src)
    assert "<p>This is <strong>important</strong> to know.</p>" in out, out


def test_trailing_bold_without_period_unchanged():
    """Bold at end of paragraph WITHOUT a sentence-terminator before it must stay inline."""
    src = "Some text **emphasis** here."
    out = render_md(src)
    assert "<strong>emphasis</strong>" in out
    assert "<p><strong>emphasis</strong></p>" not in out


def test_trailing_bold_with_period_after_bold_unchanged():
    """`text **important**.\\n\\n` (period AFTER bold) must NOT trigger the lift —
    the regex requires period IMMEDIATELY before the `**`."""
    src = "This is **important**.\n\nNext."
    out = render_md(src)
    assert "<p>This is <strong>important</strong>.</p>" in out, out
    assert "<p><strong>important</strong></p>" not in out


def test_glued_bold_without_blank_line_unchanged():
    """`text.**Bold**\\nMore text` (single newline, no blank line) must NOT be lifted —
    the regex requires `\\n\\n` after."""
    src = "Para.**Bold**\nMore text on next line."
    out = render_md(src)
    assert "<strong>Bold</strong>" in out
    assert "<p><strong>Bold</strong></p>" not in out


def test_long_bold_phrase_not_lifted():
    """Bold runs longer than 80 chars are likely emphasis prose, not headings — don't lift."""
    long_bold = "x" * 100
    src = f"Para.**{long_bold}**\n\nNext."
    out = render_md(src)
    assert f"<strong>{long_bold}</strong>" in out
    assert f"<p><strong>{long_bold}</strong></p>" not in out


def test_intentional_block_final_bold_with_no_glue_unchanged():
    """`text **bold**\\n\\n` (space before `**`, no glued period) must NOT be lifted."""
    src = "Para text **bold**\n\nNext."
    out = render_md(src)
    assert "<p>Para text <strong>bold</strong></p>" in out, out


# ── Multi-occurrence + paragraph chain ───────────────────────────────────────


def test_chain_of_glued_headings_all_lifted():
    """Chained glued-heading paragraphs should all lift — common LLM thinking-mode shape."""
    src = (
        "First text.**Heading A**\n\n"
        "Second text.**Heading B**\n\n"
        "Third text.\n"
    )
    out = render_md(src)
    assert "<p>First text.</p>" in out, out
    assert "<p><strong>Heading A</strong></p>" in out, out
    assert "<p>Second text.</p>" in out, out
    assert "<p><strong>Heading B</strong></p>" in out, out
    assert "<p>Third text.</p>" in out, out


# ── Source-level structural check on ui.js ───────────────────────────────────


def test_lift_pass_present_in_ui_js_at_correct_position():
    """The lift regex must be present in ui.js before protected-code restores.

    This pins the position so a future cleanup can't accidentally move the lift
    to a place where it would corrupt raw <pre> HTML or fenced code blocks
    (which are stashed as \x00R / \x00P / \x00F tokens at this point and don't
    match the lift regex).
    """
    lift_idx = UI_JS.find(r'(/([.!?])\*\*([^*\n]{1,80})\*\*\n\n/g')
    assert lift_idx > 0, "Glued-bold-heading lift regex not found in static/ui.js"
    raw_pre_restore = UI_JS.find("rawPreStash[+i]")
    fence_restore = UI_JS.find("fence_stash[+i]")
    assert raw_pre_restore > 0 and fence_restore > 0, "stash restore landmarks missing"
    assert lift_idx < raw_pre_restore and lift_idx < fence_restore, (
        "Glued-bold lift must run before rawPreStash and fence_stash restore "
        "so raw <pre> and fenced code are protected. Current ordering broken."
    )


def test_lift_regex_is_single_line_only():
    """The lift's inner-bold-text class must exclude `*` and `\\n` so multi-line/nested bold
    cannot be matched."""
    assert re.search(
        r"\[\^\*\\n\]\{1,80\}",
        UI_JS,
    ), "Lift regex inner class must be `[^*\\n]{1,80}` (single-line, ≤80 chars)"


# ── Node-driver tests (run against the actual JS) ────────────────────────────


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
    if NODE is None:
        pytest.skip("node not on PATH")
    p = tmp_path_factory.mktemp("renderer1446_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _render(driver_path: str, markdown: str) -> str:
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        input=markdown,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return result.stdout


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_real_renderer_lifts_glued_heading(driver_path):
    """Drive the real ui.js renderMd and confirm the lift fires in the basic case."""
    out = _render(driver_path, "Para text.**Bold heading**\n\nNext para.\n")
    assert "<p>Para text.</p>" in out, out
    assert "<p><strong>Bold heading</strong></p>" in out, out


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_real_renderer_protects_fenced_code(driver_path):
    """Glued pattern inside fenced code MUST stay literal — fence_stash is active when the lift runs."""
    src = "```\nsource.**inside-code**\n\nstill-in-code\n```\n"
    out = _render(driver_path, src)
    assert "<strong>inside-code</strong>" not in out, out
    assert "**inside-code**" in out, out


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_real_renderer_protects_raw_pre_html(driver_path):
    """Raw literal <pre> content must stay byte-preserved when it contains the glued trigger."""
    src = "<pre>Para text.**Heading**\n\nNext.</pre>\n"
    out = _render(driver_path, src)
    assert "<pre>Para text.**Heading**\n\nNext.</pre>" in out, out
    assert "<pre>Para text.\n\n**Heading**\n\nNext.</pre>" not in out, out
    assert "<strong>Heading</strong>" not in out, out


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_real_renderer_protects_inline_code(driver_path):
    """Glued pattern inside inline backticks must stay literal."""
    out = _render(driver_path, "Some `code.**glued**` text.\n")
    assert "<code>code.**glued**</code>" in out, out
    assert "<strong>glued</strong>" not in out, out


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_real_renderer_preserves_mid_paragraph_emphasis(driver_path):
    """Mid-paragraph emphasis must stay inline in the real renderer."""
    out = _render(driver_path, "This is **important** to know.\n")
    assert "<p>This is <strong>important</strong> to know.</p>" in out, out


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_real_renderer_chain_of_glued_headings(driver_path):
    """Chain of glued headings — verifies the regex's `g` flag fires multiple times."""
    src = (
        "First text.**Heading A**\n\n"
        "Second text.**Heading B**\n\n"
        "Third text.\n"
    )
    out = _render(driver_path, src)
    assert "<p>First text.</p>" in out, out
    assert "<p><strong>Heading A</strong></p>" in out, out
    assert "<p>Second text.</p>" in out, out
    assert "<p><strong>Heading B</strong></p>" in out, out
