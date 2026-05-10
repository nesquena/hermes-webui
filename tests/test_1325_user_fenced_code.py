"""Tests for issue #1325 — fenced code blocks in user message bubbles."""

import os
import subprocess
import tempfile

UI_JS = os.path.join(os.path.dirname(__file__), "..", "static", "ui.js")


def _extract_js_functions():
    """Extract esc, fence helpers, and _renderUserFencedBlocks from ui.js."""
    src = open(UI_JS).read()

    def extract_function(name):
        start = src.find(f"function {name}(")
        if start < 0:
            raise AssertionError(f"{name} not found in ui.js")
        i = src.find("{", start)
        depth = 1
        i += 1
        while i < len(src) and depth:
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        return src[start:i]

    esc_line = next(line for line in src.split("\n") if line.startswith("const esc="))
    helper_defs = "\n".join(
        extract_function(name)
        for name in (
            "_matchBacktickFenceLine",
            "_isBacktickFenceClose",
            "_renderUserFencedBlocks",
        )
    )
    return esc_line, helper_defs


def _run_user_render(text_input):
    """Return the HTML output of _renderUserFencedBlocks for the given input text."""
    import json

    esc_def, fn_def = _extract_js_functions()
    js_code = esc_def + "\n" + fn_def + "\n"
    js_code += "var input = JSON.parse(process.argv[2]);\n"
    js_code += "process.stdout.write(_renderUserFencedBlocks(input));\n"
    tf = tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, encoding="utf-8"
    )
    tf.write(js_code)
    tf.close()
    try:
        result = subprocess.run(
            ["node", tf.name, json.dumps(text_input)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(f"node error: {result.stderr}")
        return result.stdout
    finally:
        os.unlink(tf.name)


class TestUserFencedBlocks:
    """Fenced code blocks in user messages should render as <pre><code>."""

    def test_simple_fenced_block(self):
        out = _run_user_render("hello\n```python\nprint(1)\n```\nworld")
        assert '<pre><code class="language-python">' in out
        assert "print(1)" in out
        # Newlines around the fenced block become <br> (same as original plain-text path)
        assert "hello<br>" in out
        assert "<br>world" in out

    def test_fenced_block_escaped_html(self):
        """HTML in code blocks should be escaped."""
        out = _run_user_render("```html\n<div>hi</div>\n```")
        assert "&lt;div&gt;" in out
        # No raw <div> in code content
        assert "<div>" not in out.replace("&lt;div&gt;", "").replace("&gt;", "")

    def test_plain_text_not_interpreted_as_markdown(self):
        """Bold/italic/links in non-fenced text should stay escaped."""
        out = _run_user_render("**bold** and *italic* and <script>alert(1)</script>")
        assert "**bold**" in out
        assert "*italic*" in out
        assert "&lt;script&gt;" in out
        assert "<strong>" not in out

    def test_language_header_shown(self):
        out = _run_user_render("```javascript\nconst x = 1;\n```")
        assert 'class="pre-header"' in out
        assert "javascript" in out

    def test_no_language_no_header(self):
        out = _run_user_render("```\nsome code\n```")
        assert 'class="pre-header"' not in out
        assert "<pre><code>" in out
        assert "some code" in out

    def test_diff_block_colored(self):
        out = _run_user_render("```diff\n+added\n-removed\n```")
        assert "diff-block" in out
        assert "diff-plus" in out
        assert "diff-minus" in out

    def test_multiple_fenced_blocks(self):
        out = _run_user_render("first\n```python\n1\n```\nmiddle\n```js\n2\n```\nlast")
        assert "language-python" in out
        assert "language-js" in out
        assert "first<br>" in out
        assert "<br>last" in out

    def test_fenced_block_with_ampersand(self):
        out = _run_user_render("```python\nx & y\n```")
        assert "x &amp; y" in out

    def test_empty_code_block(self):
        out = _run_user_render("```\n```")
        assert "<pre><code>" in out

    def test_special_chars_outside_blocks_escaped(self):
        out = _run_user_render("a < b > c & d")
        assert "a &lt; b &gt; c &amp; d" in out

    def test_links_not_rendered_in_plain_text(self):
        """URLs in plain text should NOT become clickable links."""
        out = _run_user_render("Check https://example.com for details")
        assert "<a " not in out
        assert "https://example.com" in out

    def test_four_backtick_outer_fence_preserves_inner_triple_fence(self):
        """User-message code fences should follow CommonMark fence-length matching too."""
        out = _run_user_render("````md\n```inner\nfoo\n```\n````")
        assert out.count("<pre>") == 1
        assert out.count("</pre>") == 1
        assert '<div class="pre-header">md</div>' in out
        assert "```inner" in out
        assert "foo" in out
        assert "<br>````" not in out

    def test_inline_backticks_not_touched(self):
        """Inline backticks (single backtick, not fenced block) should remain escaped as text."""
        out = _run_user_render("use `var x = 1` here")
        assert "`var x = 1`" in out
        assert "<code>" not in out
