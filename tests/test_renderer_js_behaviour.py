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
import re
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
// tests assert against (https image, bare file:// image). The full renderer
// (ui.js) is preferred on the real page — these nodes only cover what is
// reachable from a standalone renderMd() invocation.
function _inlineMediaHtmlForRef(ref){
  const r = String(ref || '');
  if (/^https?:\/\//.test(r)) return `<img class="msg-media-img" src="${esc(r)}" alt="image" loading="lazy">`;
  if (/^file:\/\//.test(r)){
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
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return result.stdout



class TestSessionInternalLinks:
    """Drive renderMd() so session:// hardening covers the real sanitizer path."""

    def test_session_scheme_renders_same_origin_internal_anchor(self, driver_path):
        out = _render(driver_path, "[Open session](session://abc123)")
        assert 'class="session-link"' in out
        assert 'href="/app/session/abc123"' in out
        assert 'target="_blank"' not in out
        assert 'rel="noopener"' not in out

    def test_hostile_session_scheme_collapses_to_encoded_session_path(self, driver_path):
        out = _render(driver_path, "[bad](session://javascript:alert(1))")
        assert 'class="session-link"' in out
        assert 'href="/app/session/javascript%3Aalert(1"' in out
        assert 'href="javascript:' not in out
        assert 'target="_blank"' not in out

    def test_unrelated_same_origin_session_segment_is_not_allowlisted(self, driver_path):
        out = _render(driver_path, '<a class="session-link" href="/anything/foo/session/abc">bad</a>')
        assert 'href="/anything/foo/session/abc"' not in out
        assert '<a>bad</a>' in out

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
        # New shape: recursive renderMd wraps content in <p> (CommonMark-correct).
        assert "<blockquote><p>Hello world</p></blockquote>" in out, (
            f"`> Hello world` must render as <blockquote><p>Hello world</p></blockquote> "
            f"with no leading space.  Got: {out!r}."
        )

    def test_multiline_blockquote_no_leading_space(self, driver_path):
        out = _render(driver_path, "> Line one\n> Line two").strip()
        # New shape: single paragraph with <br> between soft-wrapped lines.
        assert "<blockquote><p>Line one<br>Line two</p></blockquote>" in out, (
            f"Multi-line blockquote must strip the space after each `>` and "
            f"render as a single paragraph.  Got: {out!r}"
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


class TestRendererSanitization:
    """Raw/model-provided HTML must not survive with executable attributes or schemes."""

    @pytest.mark.parametrize(
        "payload, forbidden",
        [
            ('<img src=x onerror=alert(1)>', 'onerror'),
            ('<span onclick=alert(1)>click</span>', 'onclick'),
            ('<div onmouseover=alert(1)>hover</div>', 'onmouseover'),
            ('<a href="javascript:alert(1)">x</a>', 'javascript:'),
        ],
    )
    def test_raw_html_dangerous_attributes_and_schemes_are_removed(self, driver_path, payload, forbidden):
        out = _render(driver_path, payload).lower()
        assert forbidden not in out, f"dangerous HTML survived sanitization: {out!r}"
        assert 'alert(1)' not in out, f"executable payload text should not remain executable: {out!r}"

    def test_generated_image_markdown_uses_delegated_lightbox_not_inline_js(self, driver_path):
        out = _render(driver_path, "![capy](https://example.com/capy.png)").lower()
        assert '<img' in out and 'msg-media-img' in out
        assert 'onclick' not in out
        assert '_openimglightbox' not in out

    def test_media_token_image_uses_delegated_lightbox_not_inline_js(self, driver_path):
        out = _render(driver_path, "MEDIA:https://example.com/capy.png").lower()
        assert '<img' in out and 'msg-media-img' in out
        assert 'onclick' not in out
        assert '_openimglightbox' not in out

    def test_incomplete_raw_html_tag_is_escaped_before_paragraph_wrapping(self, driver_path):
        out = _render(driver_path, '<img src=x onerror=alert(1)//').lower()
        assert '&lt;img' in out
        assert '<img' not in out
        assert 'onerror' not in out or '&lt;img' in out


class TestCommonLLMShapes:

    def test_commonmark_table_is_not_wrapped_in_paragraph(self, driver_path):
        src = (
            "| 升级时段 | 人数 |\n"
            "|---------|------|\n"
            "| 5/15（发布当天） | ~30 人 |\n"
            "| 5/16（今天） | ~10 人 |"
        )
        out = _render(driver_path, src)
        assert "<table><thead>" in out
        assert "<th>升级时段</th>" in out
        assert "<td>5/15（发布当天）</td>" in out
        assert "<td>~10 人</td>" in out
        assert "<p><table" not in out, (
            f"Markdown tables are block elements and must not be paragraph-wrapped: {out!r}"
        )

    def test_table_between_paragraphs_stays_block_level(self, driver_path):
        src = (
            "Before the table.\n\n"
            "| Key | Value |\n"
            "| --- | --- |\n"
            "| A | B |\n\n"
            "After the table."
        )
        out = _render(driver_path, src)
        assert "<p>Before the table.</p>" in out
        assert "<table><thead>" in out
        assert "<p>After the table.</p>" in out
        assert "<p><table" not in out
        assert "</table></p>" not in out

    def test_table_pipe_inside_inline_code_is_protected(self, driver_path):
        """Pipes inside backtick code in table cells must not create extra columns."""
        src = (
            "| field | expr |\n"
            "| --- | --- |\n"
            "| set | `updates.model = modelState.model || null` |\n"
        )
        out = _render(driver_path, src)
        # Must be exactly 2 cells in the data row
        assert "<th>field</th>" in out
        assert "<th>expr</th>" in out
        # The code cell should contain both pipes
        assert "<code>" in out
        assert "||" in out
        # Must NOT split into extra cells
        assert out.count("<td>") == 2, (
            f"Expected exactly 2 <td> cells, got {out.count('<td>')}: {out!r}"
        )

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
        assert "<blockquote><p>Note this.</p></blockquote>" in out
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


class TestMarkdownListsWithLatex:
    """Drive the real renderer through the list path that shares the KaTeX placeholders."""

    def test_plain_lists_still_render_markers(self, driver_path):
        out = _render(driver_path, "- one\n- two\n\n1. alpha\n2. beta")
        assert "<ul><li>one</li><li>two</li></ul>" in out
        assert '<ol><li value="1">alpha</li><li value="2">beta</li></ol>' in out

    def test_continuation_line_stays_inside_same_list_item(self, driver_path):
        out = _render(driver_path, "- first line\n  second line\n- next item")
        assert "<ul>" in out
        assert "<li>first line\nsecond line</li>" in out, out
        assert "<li>next item</li>" in out

    def test_nested_indentation_stays_in_list(self, driver_path):
        out = _render(driver_path, "- parent\n  - child")
        assert "<ul>" in out
        assert "<li>parent</li>" in out
        assert '<li style="margin-left:16px">child</li>' in out

    def test_display_math_line_stays_inside_list_item(self, driver_path):
        src = "- intro\n\n  $$x^2$$\n\n  continuation"
        out = _render(driver_path, src)
        assert "<ul>" in out and "</ul>" in out
        assert "<p>continuation</p>" not in out, out
        assert "<div class=\"katex-block\" data-katex=\"display\">x^2</div>" in out
        assert "<li>intro\n<div class=\"katex-block\" data-katex=\"display\">x^2</div>\ncontinuation</li>" in out, out

    def test_mixed_markdown_and_latex_ordered_list_preserves_all_items(self, driver_path):
        src = "1. **First** with $x$\n2. $$y$$\n3. tail"
        out = _render(driver_path, src)
        assert "<ol>" in out and "</ol>" in out
        assert "<strong>First</strong>" in out
        assert "<span class=\"katex-inline\" data-katex=\"inline\">x</span>" in out
        assert "<div class=\"katex-block\" data-katex=\"display\">y</div>" in out
        assert '<li value="3">tail</li>' in out


# ─────────────────────────────────────────────────────────────────────────────
# Block-level constructs INSIDE blockquotes — the six bugs documented in
# blockquote-rendering-bugs.md. Each test feeds the exact input from the
# bug report and asserts the rendered HTML structure.
#
# Root cause of all six: every block-level pass (fenced code, headings, hr,
# ordered lists) used to run BEFORE the blockquote handler, on > -prefixed
# lines those passes don't recognise. The fix moved blockquote handling to a
# pre-pass that strips > prefixes and recursively renders the inner content.
# ─────────────────────────────────────────────────────────────────────────────


class TestBugFencedCodeInBlockquote:
    """Bug 1: fenced code blocks inside blockquotes leaked > prefixes inside
    the rendered <pre>, broke the <blockquote> wrapper, and sometimes left
    raw <pre>/<div class="pre-header"> as visible text."""

    def test_fenced_code_inside_blockquote_renders_pre(self, driver_path):
        src = (
            "> Here is some code:\n"
            ">\n"
            "> ```python\n"
            "> x = 1\n"
            "> y = 2\n"
            "> ```\n"
            ">\n"
            "> That was the code."
        )
        out = _render(driver_path, src)
        assert "<pre>" in out and "</pre>" in out, (
            f"Fenced code inside blockquote must render as <pre>: {out!r}"
        )
        # The > prefixes must be stripped from the code content, not preserved
        # inside the <pre>.
        assert "&gt; x = 1" not in out, (
            f"Code content inside <pre> must not contain &gt; prefixes: {out!r}"
        )
        # Raw <pre> or pre-header tags must NOT appear as visible text
        assert "&lt;pre&gt;" not in out
        assert "&lt;div class=&quot;pre-header" not in out
        # Single <blockquote> wrapping everything (not split by the <pre>)
        assert out.count("<blockquote>") == 1, (
            f"Expected ONE <blockquote>, got {out.count('<blockquote>')}: {out!r}"
        )

    def test_fenced_code_with_lang_class(self, driver_path):
        src = "> ```python\n> x = 1\n> ```"
        out = _render(driver_path, src)
        assert 'class="language-python"' in out
        assert "x = 1" in out


class TestFencedCodeFenceLength:
    """CommonMark §4.5 requires the closer to be at least as long as the opener."""

    def test_five_backtick_outer_fence_preserves_inner_triple_fence(self, driver_path):
        src = (
            "- optionally also support fenced code blocks\n\n"
            "`````md\n"
            "`md\n"
            "```novelcrafter\n"
            "{#if novel.hasSeries}\n"
            "...\n"
            "{#endif}\n"
            "```\n"
            "`````\n\n"
            "That is much more correct than pretending"
        )
        out = _render(driver_path, src)
        assert out.count("<pre class=\"md-source-block\">") == 1
        assert out.count("</pre>") == 1
        assert '<div class="pre-header">md</div>' in out
        assert "```novelcrafter" in out
        assert "{#if novel.hasSeries}" in out
        assert "That is much more correct than pretending" in out
        assert "<p>`````" not in out
        assert "<br>`````" not in out

    def test_four_backtick_outer_fence_preserves_inner_triple_fence(self, driver_path):
        out = _render(driver_path, "````md\n```inner\nfoo\n```\n````\n")
        assert out.count("<pre class=\"md-source-block\">") == 1
        assert out.count("</pre>") == 1
        assert '<div class="pre-header">md</div>' in out
        assert "```inner" in out
        assert "foo" in out
        assert "<p>````" not in out

    def test_three_backtick_fence_still_renders_language_class(self, driver_path):
        out = _render(driver_path, "```js\nconsole.log('ok')\n```")
        assert out.count("<pre>") == 1
        assert '<div class="pre-header">js</div>' in out
        assert 'class="language-js"' in out
        assert "console.log(&#39;ok&#39;)" in out


class TestBugBlankContinuationInBlockquote:
    """Bug 2: blank > lines between paragraphs fragmented the blockquote into
    separate elements with literal > characters between them."""

    def test_three_paragraphs_one_blockquote(self, driver_path):
        src = (
            "> First paragraph of the quote.\n"
            ">\n"
            "> Second paragraph of the quote.\n"
            ">\n"
            "> Third paragraph of the quote."
        )
        out = _render(driver_path, src)
        # All three paragraphs in ONE <blockquote>
        assert out.count("<blockquote>") == 1, (
            f"Expected ONE <blockquote>, got {out.count('<blockquote>')}: {out!r}"
        )
        assert "First paragraph" in out
        assert "Second paragraph" in out
        assert "Third paragraph" in out
        # No literal > between paragraphs (would indicate fragmented blockquote)
        text_only = re.sub(r"<[^>]+>", "", out)
        assert ">" not in text_only, (
            f"Literal > in rendered text indicates fragmented blockquote: {text_only!r}"
        )


class TestBugHeadingsInsideBlockquote:
    """Bug 3: # headings inside blockquotes rendered as literal '##' text
    because the heading pass ran before the blockquote pass."""

    def test_h2_inside_blockquote(self, driver_path):
        src = (
            "> ## Bug description\n"
            ">\n"
            "> The widget is broken.\n"
            ">\n"
            "> ## Steps to reproduce\n"
            ">\n"
            "> Click the button."
        )
        out = _render(driver_path, src)
        assert "<h2>Bug description</h2>" in out, (
            f"## inside blockquote must render as <h2>: {out!r}"
        )
        assert "<h2>Steps to reproduce</h2>" in out
        # No literal '##' as visible text
        text_only = re.sub(r"<[^>]+>", "", out)
        assert "##" not in text_only, (
            f"Literal ## in rendered text — heading pass missed it: {text_only!r}"
        )

    def test_h1_h2_h3_all_render(self, driver_path):
        src = "> # H1\n> ## H2\n> ### H3"
        out = _render(driver_path, src)
        assert "<h1>H1</h1>" in out
        assert "<h2>H2</h2>" in out
        assert "<h3>H3</h3>" in out


class TestBugOrderedListInsideBlockquote:
    """Bug 4: ordered (numbered) lists inside blockquotes rendered as plain
    text — the OL pass had no equivalent of the UL branch in the old
    blockquote handler."""

    def test_ordered_list_renders_as_ol(self, driver_path):
        src = (
            "> Steps to reproduce:\n"
            ">\n"
            "> 1. Open the app\n"
            "> 2. Click the button\n"
            "> 3. Observe the crash"
        )
        out = _render(driver_path, src)
        assert "<ol>" in out and "</ol>" in out, (
            f"Numbered list inside blockquote must render as <ol>: {out!r}"
        )
        # All three list items present
        for item in ["Open the app", "Click the button", "Observe the crash"]:
            assert f">{item}</li>" in out, (
                f"Missing <li>{item}</li> in {out!r}"
            )


class TestBugHorizontalRuleInsideBlockquote:
    """Bug 6: --- inside a blockquote rendered as literal text instead of <hr>."""

    def test_hr_renders_inside_blockquote(self, driver_path):
        src = "> Above the rule\n>\n> ---\n>\n> Below the rule"
        out = _render(driver_path, src)
        assert "<hr>" in out, (
            f"--- inside blockquote must render as <hr>: {out!r}"
        )
        assert "Above the rule" in out
        assert "Below the rule" in out
        # No literal '---' as text
        text_only = re.sub(r"<[^>]+>", "", out)
        assert "---" not in text_only, (
            f"Literal --- in rendered text: {text_only!r}"
        )


class TestBugComplexBlockquoteAllFeatures:
    """Bug 5 (worst-case): a blockquote with headings, paragraphs, inline code,
    fenced code, and an ordered list. Old behaviour collapsed the entire thing
    into a monospace blob with raw markdown syntax leaking everywhere."""

    def test_complex_blockquote_renders_all_constructs(self, driver_path):
        src = (
            "> ## Description\n"
            ">\n"
            "> The widget is broken when X happens.\n"
            ">\n"
            "> ## Root cause\n"
            ">\n"
            "> The `MIME_MAP` in `api/config.py` is missing entries.\n"
            ">\n"
            "> ## Fix\n"
            ">\n"
            "> Add two entries:\n"
            ">\n"
            "> ```python\n"
            '> ".html": "text/html",\n'
            '> ".htm": "text/html",\n'
            "> ```\n"
            ">\n"
            "> ## Workflow rules\n"
            ">\n"
            "> 1. Never edit the file directly\n"
            "> 2. Create a worktree\n"
            "> 3. Run the tests\n"
            ">\n"
            "> Target branch is `master`."
        )
        out = _render(driver_path, src)
        # Multiple <h2> headings
        assert out.count("<h2>") >= 4, (
            f"Expected at least 4 <h2> headings, got {out.count('<h2>')}: {out!r}"
        )
        # Fenced code block
        assert "<pre>" in out
        assert 'class="language-python"' in out
        # Ordered list
        assert "<ol>" in out
        # Inline code
        assert "<code>MIME_MAP</code>" in out
        assert "<code>api/config.py</code>" in out
        assert "<code>master</code>" in out
        # No literal markdown syntax leaking
        text_only = re.sub(r"<[^>]+>", "", out)
        assert "##" not in text_only, f"Literal ## in {text_only!r}"
        # Single <blockquote> wraps everything
        assert out.count("<blockquote>") == 1, (
            f"Expected ONE <blockquote>, got {out.count('<blockquote>')}: {out!r}"
        )
        # No raw <pre>/<div class="pre-header"> as escaped text
        assert "&lt;pre&gt;" not in out
        assert "&lt;div class=&quot;pre-header" not in out


class TestBlockquoteRegressionsDontTouchOutsideContent:
    """Make sure the blockquote pre-pass doesn't grab > -prefixed lines that
    sit inside a non-blockquote fenced code block (e.g. shell prompts in
    ```bash``` examples)."""

    def test_shell_prompt_in_bash_fence_not_treated_as_blockquote(self, driver_path):
        src = "```bash\n> echo hello\n```"
        out = _render(driver_path, src)
        # The > line is part of the bash code, not a blockquote
        assert "<blockquote>" not in out, (
            f"> line inside ```bash``` must NOT become a blockquote: {out!r}"
        )
        assert "<pre>" in out
        # Escaped > preserved as code content
        assert "&gt; echo hello" in out

    def test_two_separate_blockquotes_stay_separate(self, driver_path):
        src = "> First quote\n\nSome plain text.\n\n> Second quote"
        out = _render(driver_path, src)
        assert out.count("<blockquote>") == 2, (
            f"Two separated blockquotes must stay separate: {out!r}"
        )
        assert "Some plain text." in out

    def test_nested_double_blockquote(self, driver_path):
        src = "> outer line\n> > inner line"
        out = _render(driver_path, src)
        # Should produce nested <blockquote><blockquote>
        assert out.count("<blockquote>") == 2, (
            f"Expected 2 <blockquote>: {out!r}"
        )


class TestBlockquoteEntityEncodedInput:
    """Blockquotes sent as HTML-entity-encoded text must still render correctly.
    LLMs sometimes emit &gt; instead of > — the entity-decode pass must run
    BEFORE the blockquote pre-pass, not after it."""

    def test_amp_gt_prefix_becomes_blockquote(self, driver_path):
        src = "&gt; Hello quote"
        out = _render(driver_path, src)
        assert "<blockquote>" in out, (
            f"&gt;-prefixed line must render as <blockquote>: {out!r}"
        )
        text_only = re.sub(r"<[^>]+>", "", out)
        assert "Hello quote" in text_only
        # Should not see a literal > or &gt; in the rendered text
        assert "&gt;" not in out, f"&gt; should have been decoded: {out!r}"

    def test_amp_gt_fenced_code_in_blockquote(self, driver_path):
        src = "&gt; ```python\n&gt; x = 1\n&gt; ```"
        out = _render(driver_path, src)
        assert "<blockquote>" in out, (
            f"Entity-encoded blockquote with fenced code must render: {out!r}"
        )
        assert "<pre>" in out, f"Fenced code inside entity-encoded blockquote must render: {out!r}"


class TestMermaidToolOutputGuard:
    """Line-numbered tool excerpts must not be auto-rendered as Mermaid."""

    def test_line_numbered_mermaid_fence_renders_as_code_block(self, driver_path):
        src = "```mermaid\n23|flowchart TB\n24|    A --> B\n```"
        out = _render(driver_path, src)
        assert 'class="mermaid-block"' not in out, (
            f"Line-numbered read_file excerpts are not valid Mermaid and must not auto-render: {out!r}"
        )
        assert '<div class="pre-header">mermaid</div>' in out
        assert '<pre><code class="language-mermaid">' in out
        assert '23|flowchart TB' in out

    def test_valid_mermaid_fence_still_creates_mermaid_block(self, driver_path):
        out = _render(driver_path, "```mermaid\nflowchart TB\n    A --> B\n```")
        assert 'class="mermaid-block"' in out, (
            f"Valid Mermaid fences should still be queued for Mermaid rendering: {out!r}"
        )
        assert 'flowchart TB' in out

    def test_valid_mermaid_c4_fence_still_creates_mermaid_block(self, driver_path):
        out = _render(driver_path, "```mermaid\nC4Context\n    title System Context\n```")
        assert 'class="mermaid-block"' in out, (
            f"Valid C4 Mermaid fences should still be queued for Mermaid rendering: {out!r}"
        )
        assert 'C4Context' in out

    def test_valid_mermaid_frontmatter_fence_still_creates_mermaid_block(self, driver_path):
        out = _render(driver_path, "```mermaid\n---\ntitle: Demo\n---\nflowchart TB\n    A --> B\n```")
        assert 'class="mermaid-block"' in out, (
            f"Valid Mermaid fences with frontmatter should still be queued for Mermaid rendering: {out!r}"
        )
        assert 'title: Demo' in out

    def test_prose_mention_of_mermaid_fence_renders_as_code_block(self, driver_path):
        src = "```mermaid\n` fence should not be auto-rendered too aggressively.\n\nSome prose, not a diagram.\n```"
        out = _render(driver_path, src)
        assert 'class="mermaid-block"' not in out, (
            f"Prose captured by a mermaid fence is not valid Mermaid and must not auto-render: {out!r}"
        )
        assert '<div class="pre-header">mermaid</div>' in out
        assert '<pre><code class="language-mermaid">' in out
        assert 'Some prose, not a diagram.' in out


class TestRawPreCodePreservation:
    """Raw <pre><code> HTML from model output should remain structurally intact."""

    def test_multiline_pre_code_blocks_do_not_degrade_to_backticks(self, driver_path):
        src = (
            "<pre><code>line 1\n"
            "line 2\n"
            "</code></pre>\n\n"
            "After paragraph.\n\n"
            "<pre><code>line 3\n"
            "line 4\n"
            "</code></pre>\n\n"
            "Done."
        )
        out = _render(driver_path, src)
        assert out.count("<pre>") == 2 and out.count("</pre>") == 2, (
            f"Expected two balanced <pre> blocks, got: {out!r}"
        )
        assert out.count("<code>") == 2 and out.count("</code>") == 2, (
            f"Expected two balanced <code> blocks, got: {out!r}"
        )
        assert "`line 1" not in out and "line 2\n`</pre>" not in out, (
            f"<code> content inside <pre> must not be rewritten to backticks: {out!r}"
        )
        assert "After paragraph." in out and "Done." in out


class TestHeadingLevelsH1ThroughH6:
    """Issue #1258 — `####`, `#####`, `######` previously fell through the
    heading pass and emitted as literal text starting with `#`.  Pin all six
    levels so a future edit cannot silently regress h4–h6 again."""

    def test_all_six_heading_levels_render(self, driver_path):
        src = "# H1\n## H2\n### H3\n#### H4\n##### H5\n###### H6"
        out = _render(driver_path, src)
        assert "<h1>H1</h1>" in out, f"h1 missing: {out!r}"
        assert "<h2>H2</h2>" in out, f"h2 missing: {out!r}"
        assert "<h3>H3</h3>" in out, f"h3 missing: {out!r}"
        assert "<h4>H4</h4>" in out, f"h4 missing: {out!r}"
        assert "<h5>H5</h5>" in out, f"h5 missing: {out!r}"
        assert "<h6>H6</h6>" in out, f"h6 missing: {out!r}"

    def test_h6_does_not_partial_match_as_lower_level(self, driver_path):
        """Replacers must run longest-first; otherwise `###### H6` could be
        captured by the `^### ` rule and emit `<h3>### H6</h3>`."""
        out = _render(driver_path, "###### H6")
        assert "<h6>H6</h6>" in out, f"h6 must not be partial-matched: {out!r}"
        assert "<h3>" not in out and "###" not in out

    def test_h4_inline_markdown_still_processes(self, driver_path):
        out = _render(driver_path, "#### **bold** in h4")
        assert "<h4><strong>bold</strong> in h4</h4>" in out, (
            f"inline markdown inside h4 must still render: {out!r}"
        )


class TestBareFileUrlMediaRendering:
    """#3219/#3234: bare file:// artifact links render as media, but file://
    inside fenced/inline code stays literal (the new pass runs AFTER code-stash)."""

    def test_bare_file_url_becomes_media(self, driver_path):
        out = _render(driver_path, "Here is the screenshot file:///tmp/shot.png done")
        # Routed through /api/media as an inline image, not left as a raw path.
        assert "api/media?path=" in out
        assert "msg-media-img" in out or "<img" in out

    def test_file_url_inside_fenced_code_stays_literal(self, driver_path):
        out = _render(driver_path, "```\nfile:///tmp/shot.png\n```")
        # Inside a code fence it must remain literal text, NOT become an <img>.
        assert "file:///tmp/shot.png" in out
        assert "<img" not in out
        assert "api/media?path=" not in out

    def test_file_url_inside_inline_code_stays_literal(self, driver_path):
        out = _render(driver_path, "run `open file:///tmp/shot.png` now")
        assert "file:///tmp/shot.png" in out
        assert "<img" not in out
        assert "api/media?path=" not in out

    def test_markdown_anchor_file_link_uses_link_path_not_media(self, driver_path):
        out = _render(driver_path, "[the file](file:///tmp/shot.png)")
        # Labeled anchors keep the normal link path (routed to /api/media as a link,
        # not auto-loaded as an <img>).
        assert "<img" not in out
