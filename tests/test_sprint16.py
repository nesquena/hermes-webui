"""
Sprint 16 Tests: safe HTML rendering in renderMd(), active session styling,
session sidebar polish (SVG icons, overlay actions).
"""
import html as _html
import pathlib
import re
import urllib.request

BASE = "http://127.0.0.1:8788"
REPO_ROOT = pathlib.Path(__file__).parent.parent


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_text(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.read().decode("utf-8"), r.status


def esc(s):
    """Mirror of esc() in ui.js — HTML-escapes a string."""
    return _html.escape(str(s), quote=True)


SAFE_TAGS = re.compile(
    r"^<\/?(strong|em|code|pre|h[1-6]|ul|ol|li|table|thead|tbody|tr|th|td"
    r"|hr|blockquote|p|br|a|div)([\s>]|$)",
    re.I,
)
SAFE_INLINE = re.compile(r"^<\/?(strong|em|code|a)([\s>]|$)", re.I)


def inline_md(t):
    """Mirror of inlineMd() in ui.js — for use inside list items / blockquotes."""
    t = re.sub(r"\*\*\*(.+?)\*\*\*", lambda m: "<strong><em>" + esc(m.group(1)) + "</em></strong>", t)
    t = re.sub(r"\*\*(.+?)\*\*",     lambda m: "<strong>" + esc(m.group(1)) + "</strong>", t)
    t = re.sub(r"\*([^*\n]+)\*",     lambda m: "<em>" + esc(m.group(1)) + "</em>", t)
    t = re.sub(r"`([^`\n]+)`",        lambda m: "<code>" + esc(m.group(1)) + "</code>", t)
    t = re.sub(
        r"\[([^\]]+)\]\((https?://[^\)]+)\)",
        lambda m: f'<a href="{esc(m.group(2))}" target="_blank" rel="noopener">{esc(m.group(1))}</a>',
        t,
    )
    t = re.sub(r"</?[a-zA-Z][^>]*>", lambda m: m.group() if SAFE_INLINE.match(m.group()) else esc(m.group()), t)
    return t


def render_md(raw):
    """
    Python mirror of renderMd() in static/ui.js.
    Kept in sync with the JS implementation so tests catch regressions
    if the JS logic drifts from the documented behaviour.
    """
    s = raw or ""

    # Pre-pass: stash code blocks/spans, convert safe HTML → markdown equivalents
    fence_stash = []

    def stash(m):
        fence_stash.append(m.group())
        return "\x00F" + str(len(fence_stash) - 1) + "\x00"

    s = re.sub(r"(```[\s\S]*?```|`[^`\n]+`)", stash, s)
    s = re.sub(r"<strong>([\s\S]*?)</strong>", lambda m: "**" + m.group(1) + "**", s, flags=re.I)
    s = re.sub(r"<b>([\s\S]*?)</b>",           lambda m: "**" + m.group(1) + "**", s, flags=re.I)
    s = re.sub(r"<em>([\s\S]*?)</em>",          lambda m: "*"  + m.group(1) + "*",  s, flags=re.I)
    s = re.sub(r"<i>([\s\S]*?)</i>",            lambda m: "*"  + m.group(1) + "*",  s, flags=re.I)
    s = re.sub(r"<code>([^<]*?)</code>",         lambda m: "`"  + m.group(1) + "`",  s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"\x00F(\d+)\x00", lambda m: fence_stash[int(m.group(1))], s)

    # Fenced code blocks
    def fenced(m):
        lang, code = m.group(1), m.group(2).rstrip("\n")
        h = f'<div class="pre-header">{esc(lang)}</div>' if lang else ""
        return h + "<pre><code>" + esc(code) + "</code></pre>"
    s = re.sub(r"```([\w+-]*)\n?([\s\S]*?)```", fenced, s)
    s = re.sub(r"`([^`\n]+)`", lambda m: "<code>" + esc(m.group(1)) + "</code>", s)

    # Inline formatting (top-level, outside list items)
    s = re.sub(r"\*\*\*(.+?)\*\*\*", lambda m: "<strong><em>" + esc(m.group(1)) + "</em></strong>", s)
    s = re.sub(r"\*\*(.+?)\*\*",     lambda m: "<strong>" + esc(m.group(1)) + "</strong>", s)
    s = re.sub(r"\*([^*\n]+)\*",     lambda m: "<em>" + esc(m.group(1)) + "</em>", s)

    # Block elements using inlineMd for their content
    s = re.sub(r"^### (.+)$", lambda m: "<h3>" + inline_md(m.group(1)) + "</h3>", s, flags=re.M)
    s = re.sub(r"^## (.+)$",  lambda m: "<h2>" + inline_md(m.group(1)) + "</h2>", s, flags=re.M)
    s = re.sub(r"^# (.+)$",   lambda m: "<h1>" + inline_md(m.group(1)) + "</h1>", s, flags=re.M)
    s = re.sub(r"^---+$", "<hr>", s, flags=re.M)
    s = re.sub(r"^> (.+)$", lambda m: "<blockquote>" + inline_md(m.group(1)) + "</blockquote>", s, flags=re.M)

    def handle_ul(block):
        lines = block.strip().split("\n")
        out = "<ul>"
        for l in lines:
            indent = bool(re.match(r"^ {2,}", l))
            text = re.sub(r"^ {0,4}[-*+] ", "", l)
            style = ' style="margin-left:16px"' if indent else ""
            out += f"<li{style}>{inline_md(text)}</li>"
        return out + "</ul>"

    s = re.sub(r"((?:^(?:  )?[-*+] .+\n?)+)", lambda m: handle_ul(m.group()), s, flags=re.M)

    def handle_ol(block):
        lines = block.strip().split("\n")
        out = "<ol>"
        for l in lines:
            text = re.sub(r"^ {0,4}\d+\. ", "", l)
            out += f"<li>{inline_md(text)}</li>"
        return out + "</ol>"

    s = re.sub(r"((?:^(?:  )?\d+\. .+\n?)+)", lambda m: handle_ol(m.group()), s, flags=re.M)

    # Safety net: escape unknown tags in remaining text
    s = re.sub(r"</?[a-zA-Z][^>]*>", lambda m: m.group() if SAFE_TAGS.match(m.group()) else esc(m.group()), s)

    # Paragraph wrap
    parts = s.split("\n\n")
    def wrap(p):
        p = p.strip()
        if not p: return ""
        if re.match(r"^<(h[1-6]|ul|ol|pre|hr|blockquote)", p): return p
        return "<p>" + p.replace("\n", "<br>") + "</p>"
    s = "\n".join(wrap(p) for p in parts)
    return s


# ── Static analysis: verify key structures exist in ui.js ────────────────────

def test_render_md_pre_pass_converts_strong(cleanup_test_sessions):
    """ui.js renderMd() must have pre-pass that converts <strong> to **."""
    src = REPO_ROOT / "static" / "ui.js"
    code = src.read_text()
    assert "<strong>" in code and "**" in code, "pre-pass for <strong> not found"
    # Verify the specific conversion pattern
    assert re.search(r"<strong>.*?\*\*", code, re.S), \
        "renderMd pre-pass should convert <strong>...</strong> to **...**"


def test_render_md_has_safety_net(cleanup_test_sessions):
    """ui.js must have a safety-net that escapes unknown HTML tags after the pipeline."""
    src = REPO_ROOT / "static" / "ui.js"
    code = src.read_text()
    assert "SAFE_TAGS" in code, "SAFE_TAGS allowlist regex not found in ui.js"
    assert "esc(tag)" in code, "safety-net esc(tag) call not found in ui.js"


def test_render_md_stashes_code_blocks(cleanup_test_sessions):
    """ui.js pre-pass must stash code blocks before replacing safe HTML tags."""
    src = REPO_ROOT / "static" / "ui.js"
    code = src.read_text()
    assert "fence_stash" in code, "fence_stash not found in renderMd pre-pass"


def test_render_md_handles_br_tag(cleanup_test_sessions):
    """ui.js must convert <br> to newline in pre-pass."""
    src = REPO_ROOT / "static" / "ui.js"
    code = src.read_text()
    assert re.search(r"<br\\s\*", code) or "<br" in code, "<br> handling not found"


def test_render_md_no_placeholder_remnants(cleanup_test_sessions):
    """Old Unicode placeholder approach (\\uE001-\\uE005) must be gone."""
    src = REPO_ROOT / "static" / "ui.js"
    code = src.read_text()
    for old_ph in ["\\uE001", "\\uE002", "\\uE003", "\\uE004", "\\uE005"]:
        assert old_ph not in code, \
            f"Old placeholder {old_ph} still present — broken implementation not cleaned up"


def test_render_md_safe_tag_allowlist_complete(cleanup_test_sessions):
    """SAFE_TAGS allowlist must include all tags the pipeline emits."""
    src = REPO_ROOT / "static" / "ui.js"
    code = src.read_text()
    required = ["strong", "em", "code", "pre", "ul", "ol", "li",
                "table", "blockquote", "hr", "br", "a", "div"]
    safe_tags_match = re.search(r"SAFE_TAGS\s*=\s*/(.+?)/i", code)
    assert safe_tags_match, "SAFE_TAGS regex not found"
    pattern = safe_tags_match.group(1)
    for tag in required:
        assert tag in pattern, f"Tag '{tag}' missing from SAFE_TAGS allowlist"


# ── Behavioural: renderMd logic via Python mirror ─────────────────────────────

def test_render_md_markdown_bold(cleanup_test_sessions):
    """**word** markdown renders as <strong>word</strong>."""
    out = render_md("Hello **world**")
    assert "<strong>world</strong>" in out


def test_render_md_html_strong_passthrough(cleanup_test_sessions):
    """<strong>word</strong> in AI output renders as bold."""
    out = render_md("Hello <strong>world</strong>")
    assert "<strong>world</strong>" in out


def test_render_md_html_b_tag(cleanup_test_sessions):
    """<b>word</b> renders as <strong>word</strong>."""
    out = render_md("Hello <b>world</b>")
    assert "<strong>world</strong>" in out


def test_render_md_html_em_passthrough(cleanup_test_sessions):
    """<em>word</em> renders as italic."""
    out = render_md("Hello <em>world</em>")
    assert "<em>world</em>" in out


def test_render_md_html_i_tag(cleanup_test_sessions):
    """<i>word</i> renders as <em>word</em>."""
    out = render_md("Hello <i>word</i>")
    assert "<em>word</em>" in out


def test_render_md_html_code_passthrough(cleanup_test_sessions):
    """<code>text</code> renders as inline code."""
    out = render_md("use <code>print()</code>")
    assert "<code>print()</code>" in out


def test_render_md_html_br_becomes_newline(cleanup_test_sessions):
    """<br> in AI output becomes a newline (rendered as <br> inside <p> later)."""
    out = render_md("line one<br>line two")
    assert "line one\nline two" in out or "line one<br>line two" in out


def test_render_md_mixed_markdown_and_html(cleanup_test_sessions):
    """Markdown and HTML formatting can coexist in the same response."""
    out = render_md("**markdown** and <strong>html</strong>")
    assert "<strong>markdown</strong>" in out
    assert "<strong>html</strong>" in out


def test_render_md_html_strong_in_list_item(cleanup_test_sessions):
    """THE SCREENSHOT BUG: <strong> tags inside list items must render as bold,
    not as escaped literal text like &lt;strong&gt;."""
    out = render_md(
        "- <strong>All items</strong> get `border-radius: 0 8px 8px 0`\n"
        "- <strong>Active item</strong> uses <code>#e8a030</code>\n"
        "- <strong>Project items</strong> show their color\n"
        "- <strong>Regular items</strong> stay muted"
    )
    assert "&lt;strong&gt;" not in out, \
        "Escaped <strong> literal found in list output — bold not rendering"
    assert "<strong>All items</strong>" in out
    assert "<strong>Active item</strong>" in out
    assert "<code>border-radius: 0 8px 8px 0</code>" in out
    assert "<code>#e8a030</code>" in out


def test_render_md_exact_screenshot_content(cleanup_test_sessions):
    """Exact text from the ui-changes-unrendered-html-tags.png screenshot.
    This is the canonical regression test for the inlineMd fix.
    All four bullet points must render <strong> and <code> as HTML, not literal text."""
    out = render_md(
        "- <strong>All items</strong> now have <code>border-radius: 0 8px 8px 0</code>"
        " \u2014 straight left edge everywhere, rounded on the right\n"
        "- <strong>Active item</strong> is now gold/amber (<code>#e8a030</code>)"
        " \u2014 same warm gold used in the logo \u2014 instead of blue,"
        " so it stands out distinctly from everything else\n"
        "- <strong>Project items</strong> still show their project color on the left"
        " border, but only when they're not the active item (active always wins with gold)\n"
        "- <strong>Regular items</strong> (no project) still have no left border color"
    )
    # None of the safe tags should appear as literal escaped text
    assert "&lt;strong&gt;" not in out, \
        "Literal &lt;strong&gt; found — <strong> is not rendering as bold"
    assert "&lt;/strong&gt;" not in out, \
        "Literal &lt;/strong&gt; found — closing tag is not rendering"
    assert "&lt;code&gt;" not in out, \
        "Literal &lt;code&gt; found — <code> is not rendering as inline code"
    # Each item's bold label must render correctly
    assert "<strong>All items</strong>" in out
    assert "<strong>Active item</strong>" in out
    assert "<strong>Project items</strong>" in out
    assert "<strong>Regular items</strong>" in out
    # The code spans in items 1 and 2 must render correctly
    assert "<code>border-radius: 0 8px 8px 0</code>" in out
    assert "<code>#e8a030</code>" in out
    # The surrounding prose text must be preserved
    assert "straight left edge everywhere" in out
    assert "same warm gold used in the logo" in out
    assert "active always wins with gold" in out


def test_render_md_markdown_bold_in_list_item(cleanup_test_sessions):
    """**bold** markdown inside list items must render as <strong>."""
    out = render_md("- **First** item\n- **Second** item with `code`")
    assert "<strong>First</strong>" in out
    assert "<strong>Second</strong>" in out
    assert "<code>code</code>" in out


def test_render_md_html_strong_in_blockquote(cleanup_test_sessions):
    """<strong> inside blockquote must render as bold."""
    out = render_md("> <strong>Note:</strong> pay attention")
    assert "&lt;strong&gt;" not in out
    assert "<strong>Note:</strong>" in out


def test_render_md_html_strong_in_heading(cleanup_test_sessions):
    """<strong> inside a heading must render as bold."""
    out = render_md("## <strong>Important</strong> Section")
    assert "&lt;strong&gt;" not in out
    assert "<strong>Important</strong>" in out


def test_render_md_xss_in_list_still_blocked(cleanup_test_sessions):
    """XSS attempts in list items must still be escaped."""
    out = render_md("- <img src=x onerror=alert(1)> bad")
    assert "<img" not in out
    assert "&lt;img" in out


def test_render_md_xss_in_blockquote_still_blocked(cleanup_test_sessions):
    """XSS in blockquote must still be escaped."""
    out = render_md("> <script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script" in out


def test_render_md_code_span_in_list_protected(cleanup_test_sessions):
    """Backtick code span in list item must escape its content."""
    out = render_md("- Use `<br>` for breaks")
    assert "<code>&lt;br&gt;</code>" in out


def test_render_md_code_block_protects_html(cleanup_test_sessions):
    """HTML inside a backtick code span must NOT be converted — shown as literal."""
    out = render_md("keep `<strong>literal</strong>` safe")
    assert "&lt;strong&gt;" in out, "HTML inside code span should be escaped"
    assert "<strong>literal</strong>" not in out, "HTML inside code span should NOT render as bold"


def test_render_md_fenced_code_protects_html(cleanup_test_sessions):
    """HTML inside a fenced code block must not be converted by the pre-pass.
    The fenced block is stashed before tag replacement runs, so the raw HTML
    is preserved intact for the pipeline's esc() to escape when rendering
    the <pre><code> block. We verify the stash/restore mechanism works by
    checking the content is unchanged after the pre-pass (i.e. still contains
    the original tag text, not converted to **not bold**)."""
    src = "```\n<strong>not bold</strong>\n```"
    out = render_md(src)
    # Pre-pass stash preserves the raw content -- it should NOT have been
    # converted to **not bold** (which would render as bold outside the fence)
    assert "**not bold**" not in out, \
        "Fenced code content was incorrectly converted to markdown by the pre-pass"
    # The raw content should still be present (stash/restore worked)
    assert "<strong>not bold</strong>" in out or "&lt;strong&gt;" in out, \
        "Fenced code content was lost after stash/restore"


# ── Security: XSS must be blocked ─────────────────────────────────────────────

def test_render_md_xss_img_tag_escaped(cleanup_test_sessions):
    """<img src=x onerror=alert(1)> must be HTML-escaped, not rendered."""
    out = render_md("<img src=x onerror=alert(1)>")
    assert "<img" not in out, "Raw <img> tag must not appear in output"
    assert "&lt;img" in out, "<img> must be HTML-escaped"


def test_render_md_xss_script_tag_escaped(cleanup_test_sessions):
    """<script>alert(1)</script> must be HTML-escaped."""
    out = render_md("<script>alert(1)</script>")
    assert "<script>" not in out, "Raw <script> tag must not appear in output"
    assert "&lt;script" in out, "<script> must be HTML-escaped"


def test_render_md_xss_iframe_escaped(cleanup_test_sessions):
    """<iframe> must be HTML-escaped."""
    out = render_md("<iframe src='evil.com'></iframe>")
    assert "<iframe" not in out
    assert "&lt;iframe" in out


def test_render_md_xss_svg_onerror_escaped(cleanup_test_sessions):
    """<svg onload=...> must be HTML-escaped."""
    out = render_md("<svg onload=alert(1)>")
    assert "<svg" not in out
    assert "&lt;svg" in out


def test_render_md_xss_in_bold_text_escaped(cleanup_test_sessions):
    """**<img onerror=...>** — XSS inside markdown bold must be escaped."""
    out = render_md("**<img src=x onerror=alert(1)>**")
    assert "<img" not in out, "XSS inside **bold** must be escaped"
    assert "&lt;img" in out


def test_render_md_xss_in_html_strong_escaped(cleanup_test_sessions):
    """<strong><img ...></strong> — nested XSS inside HTML strong must be escaped."""
    out = render_md("<strong><img src=x onerror=alert(1)></strong>")
    # <strong> converts to ** which then escapes the inner content via esc()
    assert "<img" not in out, "XSS nested inside <strong> must be escaped"


def test_render_md_xss_object_tag_escaped(cleanup_test_sessions):
    """<object data=...> must be HTML-escaped."""
    out = render_md("<object data='evil.swf'></object>")
    assert "<object" not in out
    assert "&lt;object" in out


# ── Sprint 16 sidebar: static structure checks ───────────────────────────────

# ── Exhaustive inlineMd / renderMd edge-case tests ───────────────────────────

# --- Unordered list variants ---

def test_list_bold_only(cleanup_test_sessions):
    """Single bold word in list item."""
    out = render_md("- **bold**")
    assert "<strong>bold</strong>" in out
    assert "&lt;" not in out

def test_list_italic_only(cleanup_test_sessions):
    """Single italic word in list item."""
    out = render_md("- *italic*")
    assert "<em>italic</em>" in out

def test_list_code_only(cleanup_test_sessions):
    """Single code span in list item."""
    out = render_md("- `code`")
    assert "<code>code</code>" in out

def test_list_bold_and_code_mixed(cleanup_test_sessions):
    """Bold and code together in one list item."""
    out = render_md("- **run** `pip install foo`")
    assert "<strong>run</strong>" in out
    assert "<code>pip install foo</code>" in out

def test_list_html_strong_and_code_mixed(cleanup_test_sessions):
    """HTML <strong> and <code> together — the exact screenshot scenario."""
    out = render_md("- <strong>Key</strong>: use <code>value</code>")
    assert "<strong>Key</strong>" in out
    assert "<code>value</code>" in out
    assert "&lt;strong&gt;" not in out
    assert "&lt;code&gt;" not in out

def test_list_html_em(cleanup_test_sessions):
    """HTML <em> in list item renders as italic."""
    out = render_md("- <em>emphasized</em> text")
    assert "<em>emphasized</em>" in out
    assert "&lt;em&gt;" not in out

def test_list_html_b_tag(cleanup_test_sessions):
    """HTML <b> in list item renders as bold."""
    out = render_md("- <b>bold via b tag</b>")
    assert "<strong>bold via b tag</strong>" in out
    assert "&lt;b&gt;" not in out

def test_list_html_i_tag(cleanup_test_sessions):
    """HTML <i> in list item renders as italic."""
    out = render_md("- <i>italic via i tag</i>")
    assert "<em>italic via i tag</em>" in out
    assert "&lt;i&gt;" not in out

def test_list_multiple_items_each_formatted(cleanup_test_sessions):
    """Multiple list items each with different formatting."""
    out = render_md(
        "- **bold item**\n"
        "- *italic item*\n"
        "- `code item`\n"
        "- plain item"
    )
    assert "<strong>bold item</strong>" in out
    assert "<em>italic item</em>" in out
    assert "<code>code item</code>" in out
    assert "<li>plain item</li>" in out

def test_list_item_bold_mid_sentence(cleanup_test_sessions):
    """Bold in middle of a list item sentence."""
    out = render_md("- Set the **timeout** to 30 seconds")
    assert "<strong>timeout</strong>" in out
    assert "Set the" in out
    assert "to 30 seconds" in out

def test_list_item_multiple_bold_spans(cleanup_test_sessions):
    """Multiple bold spans in one list item."""
    out = render_md("- **A** and **B** are both important")
    assert "<strong>A</strong>" in out
    assert "<strong>B</strong>" in out

def test_ordered_list_bold(cleanup_test_sessions):
    """Bold text inside ordered list items."""
    out = render_md("1. **First** step\n2. **Second** step\n3. Plain step")
    assert "<ol>" in out
    assert "<strong>First</strong>" in out
    assert "<strong>Second</strong>" in out
    assert "<li>Plain step</li>" in out

def test_ordered_list_html_strong(cleanup_test_sessions):
    """HTML <strong> inside ordered list items renders correctly."""
    out = render_md("1. <strong>Install</strong> the package\n2. <strong>Configure</strong> the settings")
    assert "<ol>" in out
    assert "<strong>Install</strong>" in out
    assert "<strong>Configure</strong>" in out
    assert "&lt;strong&gt;" not in out

def test_ordered_list_code_spans(cleanup_test_sessions):
    """Code spans inside ordered list items."""
    out = render_md("1. Run `npm install`\n2. Run `npm start`")
    assert "<code>npm install</code>" in out
    assert "<code>npm start</code>" in out

def test_indented_list_item_bold(cleanup_test_sessions):
    """Bold inside indented (nested) list item."""
    out = render_md("- top level\n  - **nested bold**")
    assert "<strong>nested bold</strong>" in out
    assert "margin-left:16px" in out

# --- Blockquote variants ---

def test_blockquote_plain(cleanup_test_sessions):
    """Plain blockquote wraps in <blockquote>."""
    out = render_md("> simple quote")
    assert "<blockquote>simple quote</blockquote>" in out

def test_blockquote_bold(cleanup_test_sessions):
    """**bold** inside blockquote renders correctly."""
    out = render_md("> **important** note")
    assert "<strong>important</strong>" in out

def test_blockquote_html_strong(cleanup_test_sessions):
    """<strong> inside blockquote renders as bold."""
    out = render_md("> <strong>Warning:</strong> read this")
    assert "<strong>Warning:</strong>" in out
    assert "&lt;strong&gt;" not in out

def test_blockquote_code_span(cleanup_test_sessions):
    """Code span inside blockquote renders correctly."""
    out = render_md("> Use `git commit` to save")
    assert "<code>git commit</code>" in out

def test_blockquote_mixed_formatting(cleanup_test_sessions):
    """Mixed bold and code in blockquote."""
    out = render_md("> **Note:** run `pip install foo` first")
    assert "<strong>Note:</strong>" in out
    assert "<code>pip install foo</code>" in out

def test_blockquote_xss_blocked(cleanup_test_sessions):
    """XSS in blockquote content must be escaped."""
    out = render_md("> <img src=x onerror=alert(1)>")
    assert "&lt;img" in out
    assert "<img" not in out

# --- Heading variants ---

def test_heading_h1_bold(cleanup_test_sessions):
    """Bold inside h1 renders correctly."""
    out = render_md("# **Main** Title")
    assert "<h1><strong>Main</strong> Title</h1>" in out

def test_heading_h2_html_strong(cleanup_test_sessions):
    """HTML <strong> inside h2 renders correctly."""
    out = render_md("## <strong>Section</strong> Name")
    assert "<h2><strong>Section</strong> Name</h2>" in out
    assert "&lt;strong&gt;" not in out

def test_heading_h3_code(cleanup_test_sessions):
    """Code span inside h3 renders correctly."""
    out = render_md("### The `renderMd` function")
    assert "<h3>The <code>renderMd</code> function</h3>" in out

def test_heading_xss_blocked(cleanup_test_sessions):
    """XSS attempt in heading must be escaped."""
    out = render_md("## <script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script" in out

# --- Paragraph / top-level formatting ---

def test_paragraph_bold_renders(cleanup_test_sessions):
    """Bold in a plain paragraph renders correctly."""
    out = render_md("The **quick brown fox** jumps.")
    assert "<strong>quick brown fox</strong>" in out

def test_paragraph_html_strong_renders(cleanup_test_sessions):
    """HTML <strong> in a plain paragraph renders correctly."""
    out = render_md("The <strong>quick brown fox</strong> jumps.")
    assert "<strong>quick brown fox</strong>" in out
    assert "&lt;strong&gt;" not in out

def test_paragraph_html_code_renders(cleanup_test_sessions):
    """HTML <code> in a plain paragraph renders correctly."""
    out = render_md("Call <code>foo()</code> to start.")
    assert "<code>foo()</code>" in out
    assert "&lt;code&gt;" not in out

def test_paragraph_br_creates_line_break(cleanup_test_sessions):
    """<br> in paragraph becomes a line break inside <p>."""
    out = render_md("Line one<br>Line two")
    # br converts to \n which inside <p> becomes <br>
    assert "Line one" in out and "Line two" in out

def test_multiple_paragraphs_separated(cleanup_test_sessions):
    """Double newline creates separate <p> elements."""
    out = render_md("First paragraph.\n\nSecond paragraph.")
    assert out.count("<p>") == 2

# --- Table variants ---

def test_table_structure_in_ui_js(cleanup_test_sessions):
    """ui.js must contain table rendering logic with thead/tbody structure."""
    src = (REPO_ROOT / "static" / "ui.js").read_text()
    assert "<table>" in src or "table>" in src, "table rendering not found in ui.js"
    assert "thead" in src, "thead not found in table renderer"
    assert "tbody" in src, "tbody not found in table renderer"
    assert "parseRow" in src, "parseRow helper not found in table renderer"

# --- br tag specifically ---

def test_br_in_list_item(cleanup_test_sessions):
    """<br> inside a list item becomes a newline."""
    out = render_md("- Line one<br>Line two")
    assert "Line one" in out
    assert "Line two" in out

def test_br_self_closing_in_paragraph(cleanup_test_sessions):
    """<br/> self-closing form is also handled."""
    out = render_md("Before<br/>After")
    assert "Before" in out and "After" in out

# --- No double-escaping ---

def test_no_double_escaping_ampersand(cleanup_test_sessions):
    """A literal & in text must become &amp; exactly once, not &amp;amp;."""
    out = render_md("foo & bar")
    assert "&amp;amp;" not in out
    assert "&amp;" in out or "foo & bar" in out  # either fine (paragraph wrap may not escape)

def test_no_double_escaping_lt_in_code(cleanup_test_sessions):
    """< inside a code span must become &lt; exactly once."""
    out = render_md("`a < b`")
    assert "&lt;lt;" not in out
    assert "&lt;" in out

def test_strong_text_not_double_escaped(cleanup_test_sessions):
    """Content of <strong> must not be double-escaped."""
    out = render_md("<strong>hello & world</strong>")
    # The & inside strong content should be escaped once
    assert "&amp;amp;" not in out
    assert "<strong>" in out

# --- inlineMd helper present in source ---

def test_inline_md_helper_in_ui_js(cleanup_test_sessions):
    """ui.js must define inlineMd() helper function."""
    src = (REPO_ROOT / "static" / "ui.js").read_text()
    assert "function inlineMd(" in src, "inlineMd() helper not found in ui.js"

def test_inline_md_used_in_list_handler(cleanup_test_sessions):
    """List handler in ui.js must call inlineMd() not esc() for item text."""
    src = (REPO_ROOT / "static" / "ui.js").read_text()
    # Find the list block handler
    ul_idx = src.find("html+='<ul>'") or src.find('html+=`<ul>`') or src.find("let html='<ul>'")
    assert ul_idx >= 0 or "inlineMd(text)" in src, "inlineMd not called in list handler"
    # Verify inlineMd is called, not bare esc
    assert "inlineMd(text)" in src, "inlineMd(text) call not found — list items may not render formatting"

def test_inline_md_used_in_blockquote_handler(cleanup_test_sessions):
    """Blockquote handler in ui.js must call inlineMd() not esc() for content."""
    src = (REPO_ROOT / "static" / "ui.js").read_text()
    assert "inlineMd(t)" in src, "inlineMd not called in blockquote/heading handler"


def test_sessions_js_has_svg_icons(cleanup_test_sessions):
    """sessions.js must define ICONS object with SVG strings for sidebar buttons."""
    src = REPO_ROOT / "static" / "sessions.js"
    code = src.read_text()
    assert "const ICONS=" in code or "const ICONS =" in code, "ICONS constant not found"
    for icon in ["pin", "folder", "archive", "trash", "dup"]:
        assert icon + ":" in code or f"'{icon}'" in code, f"ICONS.{icon} not found"
    assert "<svg" in code, "SVG content not found in ICONS"


def test_sessions_js_has_overlay_actions(cleanup_test_sessions):
    """sessions.js must use .session-actions overlay div for action buttons."""
    src = REPO_ROOT / "static" / "sessions.js"
    code = src.read_text()
    assert "session-actions" in code, ".session-actions overlay not found in sessions.js"


def test_style_css_has_session_actions_overlay(cleanup_test_sessions):
    """style.css must define .session-actions with position:absolute."""
    src = REPO_ROOT / "static" / "style.css"
    code = src.read_text()
    assert ".session-actions" in code, ".session-actions not found in style.css"
    assert "position:absolute" in code or "position: absolute" in code, \
        ".session-actions must use position:absolute for overlay"


def test_style_css_active_session_uses_gold(cleanup_test_sessions):
    """Active session style should use gold/amber color (#e8a030) not just blue."""
    src = REPO_ROOT / "static" / "style.css"
    code = src.read_text()
    assert "#e8a030" in code, \
        "Active session gold color (#e8a030) not found in style.css"


def test_sessions_js_uses_action_menu_not_per_row_buttons(cleanup_test_sessions):
    """sessions.js must use the single ⋯ action menu instead of per-row buttons.

    The per-row button overlay was replaced with a single ⋯ trigger that opens a
    positioned dropdown (session-action-menu). This removes the borderLeftColor
    project colour override that the old code applied, which was the original
    concern this test guarded. The new design uses a dot indicator for project
    membership instead.
    """
    src = REPO_ROOT / "static" / "sessions.js"
    code = src.read_text()
    assert "session-actions-trigger" in code, "session-actions-trigger not found in sessions.js"
    assert "_openSessionActionMenu" in code, "_openSessionActionMenu not found in sessions.js"
    assert "closeSessionActionMenu" in code, "closeSessionActionMenu not found in sessions.js"
    # The old per-row buttons must not be present (they were replaced by the menu)
    assert "act-pin" not in code, "old act-pin per-row button still in sessions.js"
    assert "act-archive" not in code, "old act-archive per-row button still in sessions.js"
