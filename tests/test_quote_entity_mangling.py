r"""
Regression tests for the quote-entity mangling bug in renderMd().

Root cause: the _al_stash before the outer autolink pass only stashed <a> and
<img> tags.  <pre><code> blocks produced by the fenced-code-block pass were NOT
stashed, so the autolink regex operated inside them.  When a code block
contained a URL followed by &quot; (the esc() form of "), the autolink regex
captured the trailing entity as part of the URL; esc(clean) then
double-escaped the & into &amp;, yielding &amp;quot; in the rendered HTML and
in the copy buffer.

Fix: extend _al_stash regex to also stash <pre\b[^>]*>[\s\S]*?<\/pre>
blocks so the outer autolink scanner never touches code-block content.
"""
import html as _html
import pathlib
import re
import subprocess

REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
UI_JS = UI_JS_PATH.read_text(encoding="utf-8")


# ── helpers: Python mirror of the relevant renderMd() segment ────────────────

def esc(s):
    return _html.escape(str(s), quote=True)


def _render_code_block(md):
    """Simulate the fenced-code-block pass (renderMd lines ~537-541)."""
    def repl(m):
        lang = (m.group(1) or "").strip().lower()
        code = re.sub(r'\n$', '', m.group(2))
        h = f'<div class="pre-header">{esc(lang)}</div>' if lang else ''
        lang_attr = f' class="language-{esc(lang)}"' if lang else ''
        return f'{h}<pre><code{lang_attr}>{esc(code)}</code></pre>'
    return re.sub(r'```([\w+-]*)\n?([\s\S]*?)```', repl, md)


SAFE_TAGS_RE = re.compile(
    r'^</?(strong|em|code|pre|h[1-6]|ul|ol|li|table|thead|tbody|tr|th|td'
    r'|hr|blockquote|p|br|a|img|div|span)([\s>]|$)', re.I
)


def _safe_tags_pass(s):
    return re.sub(
        r'</?[a-zA-Z][^>]*>',
        lambda m: m.group() if SAFE_TAGS_RE.match(m.group()) else esc(m.group()),
        s,
    )


def _autolink(url):
    trail = url[-1] if url[-1] in '.,;:!?)' else ''
    clean = url[:-1] if trail else url
    return f'<a href="{clean}" target="_blank" rel="noopener">{esc(clean)}</a>{trail}'


def _al_stash_and_autolink(s, fixed=True):
    """Simulate the _al_stash + autolink + restore pass.

    fixed=True  → uses the patched regex that also stashes <pre>…</pre>
    fixed=False → uses the original buggy regex (only <a> and <img>)
    """
    al_stash = []

    def stash_fn(m):
        al_stash.append(m.group(0))
        return f'\x00B{len(al_stash)-1}\x00'

    if fixed:
        pattern = r'(<a\b[^>]*>[\s\S]*?</a>|<img\b[^>]*>|<pre\b[^>]*>[\s\S]*?</pre>)'
    else:
        pattern = r'(<a\b[^>]*>[\s\S]*?</a>|<img\b[^>]*>)'

    s = re.sub(pattern, stash_fn, s)
    s = re.sub(r'(https?://[^\s<>"\'\)\]]+)', lambda m: _autolink(m.group(1)), s)
    s = re.sub(r'\x00B(\d+)\x00', lambda m: al_stash[int(m.group(1))], s)
    return s


def render_fixed(md):
    s = _render_code_block(md)
    s = _safe_tags_pass(s)
    s = _al_stash_and_autolink(s, fixed=True)
    return s


def render_buggy(md):
    s = _render_code_block(md)
    s = _safe_tags_pass(s)
    s = _al_stash_and_autolink(s, fixed=False)
    return s


def strip_tags(html):
    """Return text content of HTML (tags removed, entities preserved)."""
    return re.sub(r'<[^>]+>', '', html)


# ── Source-level checks ───────────────────────────────────────────────────────

class TestAlStashSourceFix:

    def test_al_stash_includes_pre_pattern(self):
        """_al_stash regex must stash <pre>…</pre> blocks to protect code from autolink."""
        al_stash_idx = UI_JS.index('const _al_stash=[]')
        al_stash_block = UI_JS[al_stash_idx : al_stash_idx + 300]
        assert '<pre\\b' in al_stash_block, (
            "_al_stash replacement must include an attribute-tolerant <pre\\b[^>]*> "
            "pattern so code blocks are protected from the outer autolink scanner"
        )

    def test_al_stash_pre_regex_uses_lazy_dotall(self):
        """_al_stash must use [\\s\\S]*? (lazy dotall) for the <pre> branch."""
        al_stash_idx = UI_JS.index('const _al_stash=[]')
        al_stash_block = UI_JS[al_stash_idx : al_stash_idx + 300]
        # The pattern <pre>[\s\S]*?<\/pre> must appear in the stash line
        assert r'[\s\S]*?' in al_stash_block, (
            "_al_stash <pre> branch must use [\\s\\S]*? for multi-line matching"
        )
        assert r'<\/pre>' in al_stash_block or '</pre>' in al_stash_block, (
            "_al_stash must close the <pre> branch with </pre>"
        )

    def test_al_stash_still_covers_a_and_img(self):
        """_al_stash must continue to stash <a> and <img> (regression guard for #487b)."""
        al_stash_idx = UI_JS.index('const _al_stash=[]')
        al_stash_block = UI_JS[al_stash_idx : al_stash_idx + 300]
        assert '<a\\b' in al_stash_block or '<a\\\\b' in al_stash_block, (
            "_al_stash must still stash <a> tags"
        )
        assert '<img\\b' in al_stash_block or '<img\\\\b' in al_stash_block, (
            "_al_stash must still stash <img> tags"
        )

    def test_js_syntax_valid(self):
        """ui.js must pass node --check after the fix."""
        result = subprocess.run(
            ['node', '--check', str(UI_JS_PATH)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"node --check failed:\n{result.stderr}"


# ── Behaviour: code blocks with quoted URLs ───────────────────────────────────

class TestCodeBlockQuotedUrlFixed:

    _MD_SIMPLE = '```\nhttps://example.com/path?q="hello"\n```'
    _MD_PYTHON = '```python\nurl = "https://api.example.com/v1?token=\\"abc\\""\n```'
    _MD_BASH   = '```bash\ncurl -H \'Accept: application/json\' "https://api.example.com/"\n```'

    def test_no_amp_quot_in_code_block_url(self):
        """Code block with a quoted URL must not produce &amp;quot; in output."""
        result = render_fixed(self._MD_SIMPLE)
        assert '&amp;quot;' not in result, (
            f"&amp;quot; found in rendered output — quote entity double-escaped:\n{result}"
        )

    def test_no_a_tag_injected_inside_pre(self):
        """The autolink pass must NOT inject <a> tags inside <pre><code> blocks."""
        result = render_fixed(self._MD_SIMPLE)
        # Extract the <pre>…</pre> portion
        pre_match = re.search(r'<pre>[\s\S]*?</pre>', result)
        assert pre_match, "No <pre> block found in rendered output"
        pre_content = pre_match.group(0)
        assert '<a ' not in pre_content, (
            f"<a> tag injected inside <pre> block:\n{pre_content}"
        )

    def test_copy_text_shows_correct_url(self):
        """textContent equivalent of code block must contain the literal URL without mangling."""
        result = render_fixed(self._MD_SIMPLE)
        text = strip_tags(result)
        # Entity-decode to simulate browser textContent
        text = _html.unescape(text)
        assert 'https://example.com/path?q="hello"' in text, (
            f"URL with quotes corrupted in text content:\n{text!r}"
        )

    def test_python_code_block_not_mangled(self):
        """Python code block with double-quoted URL strings must not be mangled."""
        result = render_fixed(self._MD_PYTHON)
        assert '&amp;quot;' not in result, (
            f"&amp;quot; found in Python code block output:\n{result}"
        )
        pre_match = re.search(r'<pre>[\s\S]*?</pre>', result)
        assert pre_match and '<a ' not in pre_match.group(0), (
            "autolink injected inside Python code block"
        )

    def test_bash_code_block_not_mangled(self):
        """Bash code block with a double-quoted URL must not be mangled."""
        result = render_fixed(self._MD_BASH)
        assert '&amp;quot;' not in result, (
            f"&amp;quot; found in bash code block output:\n{result}"
        )

    def test_buggy_pipeline_does_mangle(self):
        """Confirm the unfixed pipeline DOES produce &amp;quot; (proves test catches the bug)."""
        buggy = render_buggy(self._MD_SIMPLE)
        assert '&amp;quot;' in buggy, (
            "Expected buggy pipeline to produce &amp;quot; — test validity check failed"
        )

    def test_buggy_pipeline_does_inject_a_in_pre(self):
        """Confirm the unfixed pipeline DOES inject <a> inside <pre> (proves test catches it)."""
        buggy = render_buggy(self._MD_SIMPLE)
        pre_match = re.search(r'<pre>[\s\S]*?</pre>', buggy)
        assert pre_match and '<a ' in pre_match.group(0), (
            "Expected buggy pipeline to inject <a> inside <pre> — test validity check failed"
        )


# ── Behaviour: non-code autolink is unaffected ───────────────────────────────

class TestNonCodeAutolinkUnaffected:

    def test_bare_url_in_paragraph_still_autolinks(self):
        """A plain URL in running text must still be wrapped in <a> by the fixed pipeline."""
        result = render_fixed("Visit https://example.com for more info.")
        assert '<a href="https://example.com"' in result, (
            f"Bare URL in paragraph not autolinked after fix:\n{result}"
        )

    def test_url_in_paragraph_does_not_double_escape(self):
        """A plain URL with ampersand query params must not be double-escaped."""
        result = render_fixed("See https://example.com/search?a=1&b=2 for results.")
        # The href should contain the raw & (or %26), not &amp;amp;
        assert '&amp;amp;' not in result, (
            f"Ampersand in URL double-escaped in paragraph context:\n{result}"
        )

    def test_multiple_urls_in_paragraph_all_autolinked(self):
        """Multiple bare URLs in a paragraph must each get their own <a> tag."""
        result = render_fixed("See https://foo.com and https://bar.com")
        assert result.count('<a ') >= 2, (
            f"Expected 2 autolinks, got {result.count('<a ')}:\n{result}"
        )

    def test_pre_block_restored_intact(self):
        """After stash+restore the <pre> block must appear verbatim in the output."""
        md = '```python\nprint("hello")\n```'
        result = render_fixed(md)
        assert '<pre>' in result, "No <pre> block in output"
        assert '</pre>' in result, "Unclosed <pre> in output"
        # The code must still contain the escaped quote
        assert '&quot;' in result or 'hello' in result, (
            f"Code block content lost after stash/restore:\n{result}"
        )

    def test_code_block_and_bare_url_in_same_message(self):
        """A message with both a code block and a bare URL must autolink only the URL."""
        md = "```\nhttps://internal.example.com?token=\"abc\"\n```\n\nSee https://docs.example.com"
        result = render_fixed(md)
        # The bare URL in text should be linked
        assert '<a href="https://docs.example.com"' in result, (
            "Bare URL outside code block not autolinked"
        )
        # The URL inside the code block must NOT be linked
        pre_match = re.search(r'<pre>[\s\S]*?</pre>', result)
        assert pre_match and '<a ' not in pre_match.group(0), (
            f"URL inside code block was autolinked:\n{pre_match.group(0)}"
        )
        # And there must be no double-escaped entity
        assert '&amp;quot;' not in result, (
            f"&amp;quot; appeared in mixed message output:\n{result}"
        )


# ── Sanitizer / security expectations ────────────────────────────────────────

class TestSanitizerUnaffected:

    def test_script_tag_in_code_block_escaped(self):
        """<script> inside a code block must be HTML-escaped, not executed."""
        result = render_fixed('```\n<script>alert(1)</script>\n```')
        assert '<script>' not in result, (
            "Raw <script> tag leaked through code block rendering"
        )
        # It should appear escaped inside the pre block
        assert '&lt;script&gt;' in result, (
            f"<script> not escaped in code block output:\n{result}"
        )

    def test_untrusted_tag_outside_code_escaped_by_safe_tags(self):
        """An unknown tag outside a code block must be escaped by the SAFE_TAGS pass."""
        result = render_fixed('<marquee>hello</marquee>')
        assert '<marquee>' not in result, (
            "Untrusted <marquee> tag passed through unescaped"
        )

    def test_javascript_url_not_autolinked(self):
        """javascript: URLs must not be autolinked (regex requires http/https)."""
        result = render_fixed('javascript:alert(1)')
        assert 'href="javascript:' not in result, (
            "javascript: URL was incorrectly autolinked"
        )
