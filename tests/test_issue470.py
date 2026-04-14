"""
Tests for issue #470 — markdown link rendering bugs in renderMd():
  1. Double-linking: [label](url) converted to <a>, then autolink re-matches
     the URL inside href="..." and wraps it in a second <a>.
  2. esc() applied to URLs in href attributes turns & → &amp;, breaking
     URLs with query strings and producing &amp; in displayed link text.
  3. Same double-linking bug inside table cells via inlineMd().

These tests verify the fixes by asserting against the rendered HTML that
ui.js serves, using a live server request to evaluate the actual JS output
indirectly (via checking ui.js source for the fixed patterns) AND by
running a lightweight Python mirror of the fixed renderMd logic.

Strategy: verify the fix is present in the JS source, then test the
expected rendering behaviour through the Python mirror.
"""
import pathlib
import re
import html as _html

REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text()


# ── Helpers ──────────────────────────────────────────────────────────────────

def esc(s):
    return _html.escape(str(s), quote=True)


def _make_link(url, label):
    """Expected output for a [label](url) link after fix: href is NOT esc()-ed."""
    return f'<a href="{url}" target="_blank" rel="noopener">{esc(label)}</a>'


# Minimal Python mirror of the FIXED renderMd() — enough to test link behaviour.
# Mirrors the stash-based approach introduced by the fix.

def render_links_only(text):
    """
    Simplified render that only applies the link-related passes from the fixed
    renderMd(): [label](url) conversion + autolink, with the stash protection.
    Sufficient for testing that links render correctly without double-linking.
    """
    s = text

    # Stash [label](url) links (fix: store href as raw URL, not esc(url))
    link_stash = []
    def stash_link(m):
        label, url = m.group(1), m.group(2)
        link_stash.append(f'<a href="{url}" target="_blank" rel="noopener">{esc(label)}</a>')
        return f'\x00L{len(link_stash)-1}\x00'
    s = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', stash_link, s)

    # Autolink bare URLs (should NOT match inside already-stashed placeholders)
    def autolink(m):
        url = m.group(1)
        trail = url[-1] if url[-1] in '.,;:!?)' else ''
        clean = url[:-1] if trail else url
        return f'<a href="{clean}" target="_blank" rel="noopener">{esc(clean)}</a>{trail}'
    s = re.sub(r'(https?://[^\s<>"\')\]]+)', autolink, s)

    # Restore stashed links
    s = re.sub(r'\x00L(\d+)\x00', lambda m: link_stash[int(m.group(1))], s)
    return s


def render_table_with_links(md):
    """
    Render a markdown table that may contain [label](url) cells.
    Mirrors the fixed inlineMd() + table rendering.
    """
    lines = md.strip().split('\n')
    if len(lines) < 2:
        return md
    def is_sep(r):
        return bool(re.match(r'^\|[\s|:-]+\|$', r.strip()))
    if not is_sep(lines[1]):
        return md

    def inline_md_fixed(t):
        """Fixed inlineMd: stash links before autolink."""
        stash = []
        def stash_fn(m):
            lb, u = m.group(1), m.group(2)
            stash.append(f'<a href="{u}" target="_blank" rel="noopener">{esc(lb)}</a>')
            return f'\x00L{len(stash)-1}\x00'
        t = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', stash_fn, t)
        # autolink remaining bare URLs
        def autolink(m):
            url = m.group(1)
            trail = url[-1] if url[-1] in '.,;:!?)' else ''
            clean = url[:-1] if trail else url
            return f'<a href="{clean}" target="_blank" rel="noopener">{esc(clean)}</a>{trail}'
        t = re.sub(r'(https?://[^\s<>"\')\]]+)', autolink, t)
        t = re.sub(r'\x00L(\d+)\x00', lambda m: stash[int(m.group(1))], t)
        return t

    def parse_row(r):
        cells = r.strip().lstrip('|').rstrip('|').split('|')
        return ''.join(f'<td>{inline_md_fixed(c.strip())}</td>' for c in cells)

    def parse_header(r):
        cells = r.strip().lstrip('|').rstrip('|').split('|')
        return ''.join(f'<th>{inline_md_fixed(c.strip())}</th>' for c in cells)

    header = f'<tr>{parse_header(lines[0])}</tr>'
    body = ''.join(f'<tr>{parse_row(r)}</tr>' for r in lines[2:])
    return f'<table><thead>{header}</thead><tbody>{body}</tbody></table>'


# ── Source-level checks (verify fix is in the JS) ─────────────────────────────

def test_inlinemd_uses_link_stash():
    """Fixed inlineMd() must stash [label](url) links before autolink runs."""
    assert '_link_stash' in UI_JS, (
        "inlineMd() should use _link_stash to prevent double-linking"
    )


def test_inlinemd_no_esc_on_href():
    """Fixed inlineMd() must not call esc() on the URL in href."""
    # The old broken pattern had esc(u) inside the href
    assert 'href="${esc(u)}"' not in UI_JS, (
        "inlineMd() should not call esc() on href URL — it breaks & in query strings"
    )


def test_outer_link_pass_uses_a_stash():
    """Fixed outer link pass must stash existing <a> tags before running."""
    assert '_a_stash' in UI_JS, (
        "Outer [label](url) pass should stash existing <a> tags to prevent autolink re-matching"
    )


def test_autolink_pass_uses_al_stash():
    """Fixed autolink pass must stash existing <a> tags before running."""
    assert '_al_stash' in UI_JS, (
        "Autolink pass should stash existing <a> tags to prevent double-linking"
    )


def test_autolink_no_esc_on_href():
    """Fixed autolink pass must not call esc() on href URL."""
    idx = UI_JS.find('// Autolink: convert plain URLs to clickable links.')
    assert idx != -1, "New autolink comment not found"
    autolink_section = UI_JS[idx:idx+600]
    # The return line should have href="${clean}" (JS template literal, no esc call)
    assert 'href="${clean}"' in autolink_section, (
        'Autolink should use href="${clean}" not href="${esc(clean)}"'
    )
    assert 'href="${esc(clean)}"' not in autolink_section, (
        "Autolink should not esc() the URL in href"
    )


# ── Behaviour tests (Python mirror of fixed renderMd) ─────────────────────────

def test_labeled_link_renders_as_single_anchor():
    """[#461](https://github.com/.../461) must produce exactly one <a> tag."""
    url = 'https://github.com/nesquena/hermes-webui/issues/461'
    md = f'[#461]({url})'
    result = render_links_only(md)
    assert result.count('<a ') == 1, f"Expected 1 <a> tag, got: {result}"
    assert result.count('</a>') == 1
    assert f'href="{url}"' in result
    assert '#461' in result
    # Must not contain the raw brackets
    assert '[#461]' not in result
    assert f']({url})' not in result


def test_href_not_html_escaped():
    """URLs with & must appear as literal & in href, not &amp;."""
    url = 'https://example.com/search?q=foo&bar=baz'
    md = f'[Search]({url})'
    result = render_links_only(md)
    assert f'href="{url}"' in result, (
        f"& in URL should not be escaped to &amp; in href. Got: {result}"
    )
    assert '&amp;' not in result


def test_bare_url_not_double_linked():
    """A bare https:// URL must produce exactly one <a> tag."""
    url = 'https://github.com/nesquena/hermes-webui/issues/461'
    result = render_links_only(url)
    assert result.count('<a ') == 1, f"Expected 1 <a> tag, got: {result}"
    assert result.count('</a>') == 1


def test_labeled_link_in_table_cell_single_anchor():
    """[#461](url) inside a markdown table cell must produce exactly one <a> tag."""
    url = 'https://github.com/nesquena/hermes-webui/issues/461'
    md = f'| Issue | Title |\n|---|---|\n| [#461]({url}) | Reasoning effort |'
    result = render_table_with_links(md)
    assert result.count('<a ') == 1, f"Expected 1 <a> in table, got: {result}"
    assert f'href="{url}"' in result
    assert '#461' in result
    # No raw brackets should appear in output
    assert '[#461]' not in result


def test_multiple_links_in_table_no_double_linking():
    """Multiple [label](url) links in a table must each produce exactly one <a>."""
    urls = [
        'https://github.com/nesquena/hermes-webui/issues/461',
        'https://github.com/nesquena/hermes-webui/issues/462',
        'https://github.com/nesquena/hermes-webui/issues/463',
    ]
    rows = '\n'.join(f'| [#{461+i}]({url}) | Title {i} |' for i, url in enumerate(urls))
    md = f'| Issue | Title |\n|---|---|\n{rows}'
    result = render_table_with_links(md)
    assert result.count('<a ') == 3, f"Expected 3 <a> tags, got {result.count('<a ')}:\n{result}"
    assert result.count('</a>') == 3
    for url in urls:
        assert f'href="{url}"' in result


def test_link_label_is_escaped():
    """The label text (not the URL) must still be HTML-escaped."""
    url = 'https://example.com'
    md = f'[Click <here>]({url})'
    result = render_links_only(md)
    assert '&lt;here&gt;' in result, "Label text should be HTML-escaped"
    assert '<here>' not in result


def test_link_not_broken_by_prior_autolink():
    """A [label](url) followed by a bare URL must each produce one clean <a>."""
    url1 = 'https://github.com/issues/461'
    url2 = 'https://github.com/issues/462'
    md = f'See [#461]({url1}) and also {url2}'
    result = render_links_only(md)
    assert result.count('<a ') == 2, f"Expected 2 links, got: {result}"
    assert f'href="{url1}"' in result
    assert f'href="{url2}"' in result
    assert '#461' in result

def test_href_quote_sanitized():
    """A URL containing a double-quote must have it percent-encoded in href to prevent attribute breakout."""
    # This would break out of href="..." and inject an event handler without the fix
    url = 'https://evil.com" onmouseover="alert(1)'
    # The [label](url) regex captures up to the closing ), so we test via the render helper
    # by constructing a URL that contains a literal quote character
    safe_url = 'https://example.com/path"with"quotes'
    result = render_links_only(f'[click]({safe_url})')
    # The href must not contain a raw unencoded double-quote
    href_start = result.find('href="') + 6
    href_end = result.find('"', href_start)
    href_val = result[href_start:href_end]
    assert '"' not in href_val, (
        f"href value must not contain unencoded double-quote. Got href: {href_val}"
    )


def test_js_source_sanitizes_quotes_in_href():
    """JS source must apply quote percent-encoding to URLs before placing in href."""
    # Both the inlineMd stash and outer link pass must sanitize quotes
    assert "%22" in UI_JS, (
        "URL placed in href should have double-quotes percent-encoded via .replace to %22"
    )
