"""Behavioural tests that drive the ACTUAL renderMd() in static/ui.js via node.

The Python mirrors in test_blockquote_rendering.py and
test_renderer_comprehensive.py validate intent, but they can drift from the
JS.  Twice now (PR #1073 commit 94d63d0 — phantom <br>; PR #1073 commit
04e7b53 — leading-space-in-blockquote prefix-strip regex) the Python mirror
was correct while the JS was not, so the static-mirror tests passed even
though the live UI was broken.

This file closes that gap by spawning ``node`` on the real ui.js and
asserting the rendered HTML for the most common LLM-output shapes.
Add a case here whenever the renderer fix targets a class of input the
Python mirror cannot exercise faithfully.
"""
import os
import shutil
import subprocess
import sys
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
global.document = { createElement: () => ({ innerHTML: '', textContent: '' }) };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

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
eval(extractFunc('renderMd'));

let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => { process.stdout.write(renderMd(buf)); });
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    """Write the node driver to a tmp file (works around `node -e` arg quirks)."""
    p = tmp_path_factory.mktemp("renderer_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _render(driver_path, markdown: str) -> str:
    """Run renderMd against the actual ui.js and return the rendered HTML."""
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


# ─────────────────────────────────────────────────────────────────────────────
# Blockquote prefix strip — the bug commit 04e7b53 introduced was a one-char
# regex regression where `^>[\t]?` (only tab) replaced `^>[ \t]?` (space or
# tab), producing leading-space artifacts and breaking lists-in-quotes
# because the list-detection regex `^(  )?[-*+]` couldn't match the
# space-prefixed lines.  These tests exercise the actual JS so the regex
# can't silently regress to tab-only again.
# ─────────────────────────────────────────────────────────────────────────────


class TestBlockquotePrefixStrip:
    """Drive the actual renderMd to confirm `> ` is fully stripped."""

    def test_single_line_blockquote_no_leading_space(self, driver_path):
        out = _render(driver_path, "> Hello world").strip()
        assert "<blockquote>Hello world</blockquote>" in out, (
            f"`> Hello world` must render as <blockquote>Hello world</blockquote> "
            f"with no leading space.  Got: {out!r}.  Likely cause: prefix-strip "
            f"regex consumes only \\t, not space."
        )

    def test_multiline_blockquote_no_leading_space(self, driver_path):
        out = _render(driver_path, "> Line one\n> Line two").strip()
        assert ">Line one\nLine two<" in out, (
            f"Multi-line blockquote must strip the space after each `>`.  "
            f"Got: {out!r}"
        )
        # Belt-and-braces: there must be no space-after-newline-in-content
        assert "\n " not in out.replace("</blockquote>", ""), (
            f"Inner content of blockquote should not contain leading-space "
            f"lines.  Got: {out!r}"
        )

    def test_list_inside_blockquote_renders_as_ul(self, driver_path):
        """The PR explicitly added 'lists inside blockquotes' as a feature.
        With the prefix-strip bug, the list-detection regex can't match the
        space-prefixed lines, so the list never renders.  This pins it."""
        out = _render(driver_path, "> Steps:\n> - one\n> - two")
        assert "<ul>" in out, (
            f"`> - item` lines inside a blockquote must render as a <ul>.  "
            f"Got: {out!r}.  Likely cause: prefix-strip leaves a leading "
            f"space, list regex `^(  )?[-*+] ` can't match one-space prefix."
        )
        assert "<li>one</li>" in out
        assert "<li>two</li>" in out

    def test_task_list_inside_blockquote(self, driver_path):
        """Task lists inside blockquotes render checkbox spans, not literal [x]."""
        out = _render(driver_path, "> - [x] done\n> - [ ] todo")
        assert 'class="task-done"' in out, (
            f"`- [x]` inside a blockquote must produce a task-done span.  "
            f"Got: {out!r}"
        )
        assert 'class="task-todo"' in out


# ─────────────────────────────────────────────────────────────────────────────
# Common LLM output shapes — sanity-check the most frequent constructs render
# the way a user would expect.
# ─────────────────────────────────────────────────────────────────────────────


class TestCommonLLMShapes:

    def test_strikethrough_outside_quote(self, driver_path):
        out = _render(driver_path, "This was ~~outdated~~ but is now fine.")
        assert "<del>outdated</del>" in out

    def test_strikethrough_inside_blockquote(self, driver_path):
        out = _render(driver_path, "> This is ~~wrong~~ actually")
        assert "<blockquote>" in out and "<del>wrong</del>" in out

    def test_top_level_task_list(self, driver_path):
        out = _render(driver_path, "- [x] done\n- [ ] todo\n- regular item")
        assert 'class="task-done"' in out
        assert 'class="task-todo"' in out
        assert "regular item" in out

    def test_nested_blockquote_recurses(self, driver_path):
        out = _render(driver_path, ">>> deeply nested")
        assert out.count("<blockquote>") == 3
        assert out.count("</blockquote>") == 3

    def test_quote_then_heading(self, driver_path):
        out = _render(driver_path, "> Note this.\n\n## Heading")
        assert "<blockquote>Note this.</blockquote>" in out
        assert "<h2>Heading</h2>" in out

    def test_crlf_does_not_leak_carriage_return(self, driver_path):
        out = _render(driver_path, "Line1\r\nLine2\r\nLine3")
        assert "\r" not in out, f"CRLF must be normalised; got {out!r}"

    def test_llm_multiparagraph_quote_with_list(self, driver_path):
        """The shape an LLM emits when summarising decisions inside a quote."""
        src = (
            "> Here are the key points:\n"
            ">\n"
            "> - Point one\n"
            "> - Point two\n"
            ">\n"
            "> And a closing remark."
        )
        out = _render(driver_path, src)
        assert "<blockquote>" in out
        assert "<ul>" in out
        assert "<li>Point one</li>" in out
        assert "<li>Point two</li>" in out
        assert "And a closing remark." in out
        # No leading-space artifacts in the quoted text
        assert "\n " not in out.replace("</blockquote>", "")
