"""
Regression tests for #1154 — fenced code block content leaking into
markdown passes and corrupting subsequent message rendering.

Root cause: fenced code blocks were rendered to <pre><code> early in
the pipeline, BEFORE list/heading/table regexes ran. Lines inside
code blocks that looked like markdown (e.g. `- removed`, `+ added`
in diff blocks) were matched by those regexes, injecting <ul>/<li>
HTML inside <pre>, breaking </pre> closure and corrupting layout.

Fix: fenced blocks are converted to <pre><code> HTML inside the stash
callback and kept as \\x00P tokens until AFTER all markdown passes.
"""

import re
import shutil

import pytest

REPO_ROOT = __import__("pathlib").Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
global.window = {};
global.document = { createElement: () => ({ innerHTML: '', textContent: '' }) };
const esc = s => String(s ?? '').replace(/[&<>\"']/g, c => (
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
eval(extractFunc('_matchBacktickFenceLine'));
eval(extractFunc('_isBacktickFenceClose'));
eval(extractFunc('renderMd'));

let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => { process.stdout.write(renderMd(buf)); });
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("renderer_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _render(driver_path, markdown: str) -> str:
    import subprocess

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


class TestFencedCodeBlockIsolation:
    """Content inside fenced code blocks must never be interpreted as markdown."""

    def test_diff_block_plus_minus_lines_not_treated_as_list(self, driver_path):
        html = _render(
            driver_path, "before\n\n```diff\n- removed line\n+ added line\n```\nafter"
        )
        pre = _extract_pre(html)
        assert pre is not None, "Expected a <pre> block"
        assert "<ul>" not in pre, "List HTML must not appear inside <pre>"
        assert "<li>" not in pre, "List items must not appear inside <pre>"
        assert "- removed line" in pre
        assert "+ added line" in pre
        assert html.count("<pre") == html.count("</pre>"), (
            "Every <pre> must have a matching </pre>"
        )

    def test_bash_block_asterisk_lines_not_treated_as_list(self, driver_path):
        html = _render(driver_path, "```bash\n* not a list item\n* another one\n```")
        pre = _extract_pre(html)
        assert pre is not None
        assert "<ul>" not in pre
        assert "<li>" not in pre

    def test_markdown_block_heading_not_treated_as_h1(self, driver_path):
        html = _render(driver_path, "```markdown\n# Not a heading\n## Also not\n```")
        pre = _extract_pre(html)
        assert pre is not None
        assert "<h1>" not in pre
        assert "<h2>" not in pre
        assert "# Not a heading" in pre

    def test_markdown_block_bold_not_treated_as_strong(self, driver_path):
        html = _render(driver_path, "```text\n**not bold**\n```")
        pre = _extract_pre(html)
        assert pre is not None
        assert "<strong>" not in pre
        assert "**not bold**" in pre

    def test_content_after_code_block_renders_normally(self, driver_path):
        html = _render(
            driver_path,
            "```diff\n- old\n+ new\n```\n\n"
            "# Real Heading\n\n- real list item\n\n**bold text**",
        )
        assert "<h1>Real Heading</h1>" in html
        assert "<ul>" in html
        assert "<strong>bold text</strong>" in html
        pre = _extract_pre(html)
        assert "<ul>" not in pre

    def test_no_escaped_pre_closing_tag(self, driver_path):
        html = _render(
            driver_path, "```diff\n- old line\n+ new line\n```\n\nAfter the block"
        )
        assert "&lt;/pre&gt;" not in html, (
            "Escaped </pre> indicates broken HTML nesting"
        )

    def test_code_block_without_language(self, driver_path):
        html = _render(driver_path, "```\n- item\n+ item\n```\n\n- real list")
        pre = _extract_pre(html)
        assert pre is not None
        assert "<ul>" not in pre
        assert html.count("<pre") == html.count("</pre>")

    def test_inline_backtick_still_works(self, driver_path):
        html = _render(driver_path, "Some **`code`** here")
        assert "<strong><code>code</code></strong>" in html

    def test_inline_backtick_inside_bold(self, driver_path):
        html = _render(driver_path, "Text with `inline code` and **bold**")
        assert "<code>inline code</code>" in html
        assert "<strong>bold</strong>" in html

    def test_large_diff_output_no_corruption(self, driver_path):
        diff_lines = "\n".join(
            f"- old_line_{i}" if i % 2 == 0 else f"+ new_line_{i}" for i in range(50)
        )
        html = _render(driver_path, f"```diff\n{diff_lines}\n```\n\nSummary after diff")
        pre = _extract_pre(html)
        assert pre is not None
        assert "<ul>" not in pre
        assert "<li>" not in pre
        assert html.count("<pre") == html.count("</pre>")
        assert "Summary after diff" in html

    def test_mixed_fenced_and_inline_code(self, driver_path):
        html = _render(
            driver_path,
            "Use `rm -rf` to delete:\n\n```bash\nrm -rf /tmp/stuff\n```\n\nDone.",
        )
        assert "<code>rm -rf</code>" in html
        pre = _extract_pre(html)
        assert pre is not None
        assert "rm -rf /tmp/stuff" in pre


class TestFencedBlockPreHeaderPreserved:
    """Pre-header div (language label) must still be generated."""

    def test_language_header_present(self, driver_path):
        html = _render(driver_path, "```python\ndef hello():\n    pass\n```")
        assert '<div class="pre-header">python</div>' in html

    def test_no_language_no_header(self, driver_path):
        html = _render(driver_path, "```\nsome code\n```")
        assert "pre-header" not in html

    def test_language_class_on_code_tag(self, driver_path):
        html = _render(driver_path, "```javascript\nconsole.log('hi')\n```")
        assert 'class="language-javascript"' in html


def _extract_pre(html):
    m = re.search(r"<pre[^>]*>[\s\S]*?</pre>", html)
    return m.group(0) if m else None
