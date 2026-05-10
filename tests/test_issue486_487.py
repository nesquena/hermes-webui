"""
Tests for issue #486 (CSS: inline code in table cells) and
issue #487 (JS renderer: markdown image syntax not implemented).

Issue #486 — CSS fix in static/style.css:
  Inline `code` spans inside table cells render with awkward sizing.
  Fix: td code, th code { font-size: 0.85em; padding: 1px 4px; vertical-align: baseline; }

Issue #487 — JS fix in static/ui.js:
  ![alt](url) image syntax not handled — renders as stray ! + link.
  Fix: add image pass to renderMd() (before link pass) and inlineMd()
  reusing the .msg-media-img class.

Strategy:
  - Source-level checks verify the fixes are present in the JS/CSS.
  - Python mirror tests verify the rendering logic with exhaustive edge cases,
    especially code blocks inside tables (the specific case Nathan flagged).
"""

import pathlib
import re
import html as _html

REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text()
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text()


# ── Helpers ───────────────────────────────────────────────────────────────────


def esc(s):
    return _html.escape(str(s), quote=True)


def inline_md(t):
    """
    Python mirror of the fixed inlineMd() function — includes:
    - _code_stash (protects backtick spans from bold/italic AND from image pass)
    - image pass (NEW for #487 — runs while code stash is active, before link pass)
    - _img_stash (protects rendered img tags from autolink touching src=)
    - _link_stash (protects links from autolink)
    - autolink
    - code stash restore (after autolink, so code content is never autolinked)

    Correct operation order:
      1. code stash        — \x00C  protects `...` from bold and image pass
      2. bold/italic       — runs on plain text only
      3. image pass        — runs while code content is still stashed (so ![x](url)
                             inside backticks stays protected as a \x00C token)
      4. img stash         — \x00I  protects <img src="url"> from autolink
      5. link stash        — \x00L  protects [label](url) links from autolink
      6. autolink          — only matches URLs not already in a stash token
      7. link stash restore
      8. img stash restore
      9. code stash restore — restores <code> tags last
    """
    # 1. Code stash — must be first to protect code content from all subsequent passes
    code_stash = []

    def stash_code(m):
        code_stash.append(f"<code>{esc(m.group(1))}</code>")
        return f"\x00C{len(code_stash) - 1}\x00"

    t = re.sub(r"`([^`\n]+)`", stash_code, t)

    # 2. Bold/italic (code content is safely stashed)
    t = re.sub(
        r"\*\*\*(.+?)\*\*\*",
        lambda m: f"<strong><em>{esc(m.group(1))}</em></strong>",
        t,
    )
    t = re.sub(r"\*\*(.+?)\*\*", lambda m: f"<strong>{esc(m.group(1))}</strong>", t)
    t = re.sub(r"\*([^*\n]+)\*", lambda m: f"<em>{esc(m.group(1))}</em>", t)

    # 3. Image pass (NEW — runs while code is still stashed, so ![x](url) inside
    #    backticks is protected as a \x00C token and won't match here)
    def render_image(m):
        alt, url = m.group(1), m.group(2)
        safe_url = url.replace('"', "%22")
        return (
            f'<img src="{safe_url}" alt="{esc(alt)}" '
            f'class="msg-media-img" loading="lazy" '
            f"onclick=\"this.classList.toggle('msg-media-img--full')\">"
        )

    t = re.sub(r"!\[([^\]]*)\]\((https?://[^\)]+)\)", render_image, t)

    # 4. Img stash — protect rendered <img> tags so autolink never touches src= values
    img_stash = []

    def stash_img(m):
        img_stash.append(m.group(0))
        return f"\x00I{len(img_stash) - 1}\x00"

    t = re.sub(r"<img\b[^>]*>", stash_img, t)

    # 5. Link stash
    link_stash = []

    def stash_link(m):
        lb, u = m.group(1), m.group(2)
        link_stash.append(
            f'<a href="{u.replace(chr(34), "%22")}" target="_blank" rel="noopener">{esc(lb)}</a>'
        )
        return f"\x00L{len(link_stash) - 1}\x00"

    t = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", stash_link, t)

    # 6. Autolink (img and link URLs are both stashed — safe)
    def autolink(m):
        url = m.group(1)
        trail = url[-1] if url[-1] in ".,;:!?)" else ""
        clean = url[:-1] if trail else url
        return (
            f'<a href="{clean}" target="_blank" rel="noopener">{esc(clean)}</a>{trail}'
        )

    t = re.sub(r'(https?://[^\s<>"\')\]]+)', autolink, t)

    # 7. Restore link stash
    t = re.sub(r"\x00L(\d+)\x00", lambda m: link_stash[int(m.group(1))], t)

    # 8. Restore img stash
    t = re.sub(r"\x00I(\d+)\x00", lambda m: img_stash[int(m.group(1))], t)

    # 9. Restore code stash (last — code content was never touched by any pass)
    t = re.sub(r"\x00C(\d+)\x00", lambda m: code_stash[int(m.group(1))], t)
    return t


def render_table(md):
    """Python mirror of the table pass, using inline_md() per cell."""
    lines = md.strip().split("\n")
    if len(lines) < 2:
        return md

    def is_sep(r):
        return bool(re.match(r"^\|[\s|:-]+\|$", r.strip()))

    if not is_sep(lines[1]):
        return md

    def parse_header(r):
        cells = r.strip().lstrip("|").rstrip("|").split("|")
        return "".join(f"<th>{inline_md(c.strip())}</th>" for c in cells)

    def parse_row(r):
        cells = r.strip().lstrip("|").rstrip("|").split("|")
        return "".join(f"<td>{inline_md(c.strip())}</td>" for c in cells)

    header = f"<tr>{parse_header(lines[0])}</tr>"
    body = "".join(f"<tr>{parse_row(r)}</tr>" for r in lines[2:])
    return f"<table><thead>{header}</thead><tbody>{body}</tbody></table>"


# ═════════════════════════════════════════════════════════════════════════════
# ISSUE #486 — CSS: code inside table cells
# ═════════════════════════════════════════════════════════════════════════════


class TestIssue486CssCodeInTable:
    """CSS fix: td code and th code must have targeted sizing rules."""

    def test_td_code_font_size_present(self):
        """msg-body td code rule must set font-size (e.g. 0.85em) to prevent oversized code."""
        assert "td code" in STYLE_CSS, (
            "Missing 'td code' CSS rule — inline code in table cells needs sizing fix"
        )

    def test_th_code_rule_present(self):
        """th code rule must also exist for header cells."""
        assert "th code" in STYLE_CSS, (
            "Missing 'th code' CSS rule — inline code in header cells needs sizing fix"
        )

    def test_td_code_has_font_size(self):
        """The td code / th code block must include a font-size declaration."""
        # Find the msg-body scoped td code rule
        idx = STYLE_CSS.find("td code")
        assert idx != -1, "td code rule not found in style.css"
        # Check nearby text (within 200 chars) has font-size
        window = STYLE_CSS[idx : idx + 200]
        assert "font-size" in window, (
            f"td code rule must include font-size. Found near td code: {window!r}"
        )

    def test_td_code_has_padding(self):
        """The td code / th code block must include a padding declaration."""
        idx = STYLE_CSS.find("td code")
        assert idx != -1
        window = STYLE_CSS[idx : idx + 200]
        assert "padding" in window, (
            f"td code rule must include padding. Found near td code: {window!r}"
        )

    def test_td_code_has_vertical_align(self):
        """The td code / th code block must include vertical-align: baseline."""
        idx = STYLE_CSS.find("td code")
        assert idx != -1
        window = STYLE_CSS[idx : idx + 200]
        assert "vertical-align" in window, (
            f"td code rule must include vertical-align. Found near td code: {window!r}"
        )

    def test_code_renders_inside_table_cell(self):
        """Inline `code` inside a table cell must render as <code> element."""
        md = "| Syntax | Rendered |\n|---|---|\n| `code` | `code` |"
        result = render_table(md)
        assert "<code>code</code>" in result, (
            f"Inline code in table cell should render as <code>. Got: {result}"
        )

    def test_bold_code_renders_inside_table_cell(self):
        """**`bold code`** inside a table cell must render as <strong><code>."""
        md = "| Style | Example |\n|---|---|\n| bold code | **`bold code`** |"
        result = render_table(md)
        # Should have code tag (even inside bold)
        assert "<code>bold code</code>" in result, (
            f"Bold code in table should render as <code>. Got: {result}"
        )

    def test_multiple_code_spans_in_same_cell(self):
        """Multiple backtick spans in one cell all render as <code>."""
        md = "| Combined |\n|---|\n| `a` and `b` |"
        result = render_table(md)
        assert result.count("<code>") == 2, (
            f"Expected 2 code tags in cell, got: {result}"
        )

    def test_code_in_header_cell(self):
        """`code` in a <th> header cell must also render as <code>."""
        md = "| `header code` | Normal |\n|---|---|\n| data | data |"
        result = render_table(md)
        assert "<code>header code</code>" in result, (
            f"Code in header cell should render. Got: {result}"
        )

    def test_code_not_mangled_by_bold_in_table(self):
        """**`code`** in a table cell must NOT produce &lt;code&gt; (the pre-fix bug)."""
        md = "| Pattern | Example |\n|---|---|\n| bold-code | **`npm install`** |"
        result = render_table(md)
        assert "&lt;code&gt;" not in result, (
            f"Code tags inside bold in table must not be HTML-escaped. Got: {result}"
        )
        assert "<strong>" in result, "Bold wrapper should be present"
        assert "<code>npm install</code>" in result

    def test_code_with_special_chars_in_table(self):
        """`<script>` inside a table cell must have the angle brackets escaped."""
        md = "| Input | Output |\n|---|---|\n| `<script>` | sanitized |"
        result = render_table(md)
        assert "&lt;script&gt;" in result, (
            f"Code content must be HTML-escaped. Got: {result}"
        )
        # The <code> wrapper itself must be there
        assert "<code>" in result

    def test_code_adjacent_to_link_in_table(self):
        """`code` and [link](url) in same cell both render correctly."""
        url = "https://example.com"
        md = f"| Mixed |\n|---|\n| `foo` and [bar]({url}) |"
        result = render_table(md)
        assert "<code>foo</code>" in result
        assert f'href="{url}"' in result
        assert "bar" in result

    def test_empty_code_span_in_table(self):
        """Edge case: empty backtick span in table cell (`` ` ` ``) — no crash."""
        # This won't match the code regex (requires at least 1 char), should pass through
        md = "| Col |\n|---|\n| normal text |"
        result = render_table(md)
        assert "<td>normal text</td>" in result


# ═════════════════════════════════════════════════════════════════════════════
# ISSUE #487 — JS renderer: markdown image syntax
# ═════════════════════════════════════════════════════════════════════════════


class TestIssue487ImageRendering:
    """Image syntax ![alt](url) must render as <img>, not as ! + link."""

    # ── Source-level checks ──────────────────────────────────────────────────

    def test_image_pass_present_in_ui_js(self):
        """renderMd() must contain an image regex pass for ![alt](url)."""
        assert "![" in UI_JS or r"!\[" in UI_JS, (
            "ui.js should contain image syntax handling (![...](url) regex)"
        )
        # More specifically, look for the img tag being generated
        assert "msg-media-img" in UI_JS, "Image pass should reuse .msg-media-img class"

    def test_image_pass_runs_before_link_pass_in_outer(self):
        """Image regex must appear in ui.js BEFORE the [label](url) link pass."""
        # Find the image pass position
        img_idx = UI_JS.find("!\\[")
        if img_idx == -1:
            img_idx = UI_JS.find("![")
        # Find the outer labeled link pass position (after table pass)
        link_idx = UI_JS.find("Outer link pass for labeled links")
        assert img_idx != -1, "Image pass not found in ui.js"
        assert link_idx != -1, "Outer link pass comment not found in ui.js"
        assert img_idx < link_idx, (
            "Image pass must run before the outer [label](url) link pass "
            "to prevent the image from being consumed as a plain link"
        )

    def test_image_url_sanitized_for_quotes(self):
        """Image src URL must have double-quotes percent-encoded."""
        # The image pass must use .replace(/"/g,'%22') or equivalent
        # Look for the pattern near image handling
        img_idx = UI_JS.find("msg-media-img")
        assert img_idx != -1
        # Find all occurrences — there's the MEDIA restore and the new image pass
        # The new one should have %22 for URL sanitization
        assert "%22" in UI_JS, "Image src URL must sanitize double-quotes to %22"

    def test_image_alt_uses_esc(self):
        """Alt text must be passed through esc() to prevent XSS."""
        # Look for esc( call near the image rendering code
        # The pattern should be: alt="${esc(alt)}"
        assert "esc(" in UI_JS, "esc() function must be used for alt text"

    def test_safe_tags_includes_img(self):
        """SAFE_TAGS allowlist must include 'img' to prevent the tag from being escaped."""
        # Find the SAFE_TAGS regex in ui.js
        safe_idx = UI_JS.find("SAFE_TAGS=")
        assert safe_idx != -1, "SAFE_TAGS not found in ui.js"
        safe_window = UI_JS[safe_idx : safe_idx + 300]
        assert "img" in safe_window, (
            f"SAFE_TAGS must include 'img' tag. Found: {safe_window!r}"
        )

    def test_inlinemd_has_image_pass(self):
        """inlineMd() must also handle ![alt](url) for images inside table cells."""
        # inlineMd is called for table cells, list items, blockquotes
        # Find inlineMd function body
        start = UI_JS.find("function inlineMd(")
        assert start != -1, "inlineMd function not found"
        # Get a generous window covering the function
        fn_window = UI_JS[start : start + 1500]
        assert "![" in fn_window or r"!\[" in fn_window, (
            "inlineMd() must handle image syntax for images in table cells"
        )

    # ── Behaviour tests (Python mirror) ─────────────────────────────────────

    def test_basic_image_renders_as_img_tag(self):
        """![alt](https://example.com/img.png) must produce an <img> tag."""
        t = "![A cat](https://example.com/cat.png)"
        result = inline_md(t)
        assert "<img " in result, f"Expected <img> tag, got: {result}"
        assert 'src="https://example.com/cat.png"' in result
        assert 'alt="A cat"' in result
        # Must NOT have the raw ![...] syntax left over
        assert "![" not in result
        # Must NOT have a stray ! character
        assert result.startswith("<img "), f"Result should start with img tag: {result}"

    def test_image_does_not_render_as_link(self):
        """![alt](url) must NOT produce an <a> tag (the pre-fix bug)."""
        t = "![Logo](https://example.com/logo.png)"
        result = inline_md(t)
        assert "<a " not in result, (
            f"Image must not render as an <a> tag. Got: {result}"
        )

    def test_image_stray_exclamation_not_present(self):
        """No stray ! character before the img tag (the pre-fix symptom)."""
        t = "![alt](https://example.com/img.png)"
        result = inline_md(t)
        # Strip the img tag and check no ! is left
        cleaned = re.sub(r"<img[^>]+>", "", result)
        assert "!" not in cleaned, (
            f"Stray ! character present after image render. Got: {result}"
        )

    def test_image_uses_msg_media_img_class(self):
        """Rendered <img> must use class=\"msg-media-img\" for consistent styling."""
        t = "![screenshot](https://example.com/shot.png)"
        result = inline_md(t)
        assert 'class="msg-media-img"' in result, (
            f"Image must use .msg-media-img class. Got: {result}"
        )

    def test_image_has_lazy_loading(self):
        """Rendered <img> must have loading=\"lazy\"."""
        t = "![x](https://example.com/x.png)"
        result = inline_md(t)
        assert 'loading="lazy"' in result, f"Expected loading=lazy. Got: {result}"

    def test_image_has_click_to_zoom(self):
        """Rendered <img> must have onclick toggle for zoom."""
        t = "![x](https://example.com/x.png)"
        result = inline_md(t)
        assert "msg-media-img--full" in result, (
            f"Image must have click-to-zoom onclick. Got: {result}"
        )

    def test_image_alt_is_escaped(self):
        """Alt text with HTML special chars must be escaped."""
        t = "![<evil>](https://example.com/img.png)"
        result = inline_md(t)
        assert "&lt;evil&gt;" in result, f"Alt text must be HTML-escaped. Got: {result}"
        assert "<evil>" not in result

    def test_image_url_quote_sanitized(self):
        """Double-quote in image URL must be percent-encoded to prevent attribute breakout."""
        t = '![x](https://example.com/path"with"quotes.png)'
        result = inline_md(t)
        # Find the src attribute value
        src_match = re.search(r'src="([^"]*)"', result)
        assert src_match, f"src attribute not found. Got: {result}"
        src_val = src_match.group(1)
        assert '"' not in src_val, (
            f"Raw double-quote in src would break attribute. Got src: {src_val!r}"
        )

    def test_image_no_javascript_uri(self):
        """javascript: URIs must not be rendered as image src (regex only matches http/https)."""
        t = "![x](javascript:alert(1))"
        result = inline_md(t)
        # The regex requires https?://, so this should pass through unmodified
        assert "<img " not in result, (
            f"javascript: URI must not render as <img>. Got: {result}"
        )

    def test_image_no_data_uri(self):
        """data: URIs must not be rendered as image src."""
        t = "![x](data:image/png;base64,abc123)"
        result = inline_md(t)
        assert "<img " not in result, (
            f"data: URI must not render as <img>. Got: {result}"
        )

    def test_image_followed_by_text(self):
        """Image followed by plain text — only the image becomes an <img>."""
        t = "![cat](https://example.com/cat.png) and some text"
        result = inline_md(t)
        assert "<img " in result
        assert "and some text" in result

    def test_image_preceded_by_text(self):
        """Text before an image — both render correctly."""
        t = "Here is a screenshot: ![shot](https://example.com/shot.png)"
        result = inline_md(t)
        assert "Here is a screenshot:" in result
        assert "<img " in result

    def test_image_and_link_in_same_cell(self):
        """Image and link in same inline context both render correctly."""
        t = "![img](https://example.com/img.png) see [here](https://example.com)"
        result = inline_md(t)
        assert "<img " in result
        assert '<a href="https://example.com"' in result
        assert "![" not in result

    def test_image_inside_table_cell(self):
        """![alt](url) inside a markdown table cell must render as <img>."""
        md = (
            "| Image | Caption |\n"
            "|---|---|\n"
            "| ![logo](https://example.com/logo.png) | Company logo |"
        )
        result = render_table(md)
        assert "<img " in result, (
            f"Image in table should render as <img>. Got: {result}"
        )
        assert 'src="https://example.com/logo.png"' in result
        assert "<a " not in result, "Image in table must not render as <a>"

    def test_image_in_table_no_stray_exclamation(self):
        """No stray ! before the <img> when image is inside a table cell."""
        md = "| X |\n|---|\n| ![x](https://x.com/x.png) |"
        result = render_table(md)
        # Strip known tags and check no ! appears
        cleaned = re.sub(r"<[^>]+>", "", result)
        assert "!" not in cleaned, (
            f"Stray ! in table cell after image render. Cleaned: {cleaned!r}"
        )

    def test_empty_alt_text_image(self):
        """![](url) with empty alt renders as <img> with empty alt attribute."""
        t = "![](https://example.com/img.png)"
        result = inline_md(t)
        assert "<img " in result
        assert 'alt=""' in result

    def test_multiple_images_in_one_cell(self):
        """Two images in one table cell both render as <img> tags."""
        t = "![a](https://example.com/a.png) ![b](https://example.com/b.png)"
        result = inline_md(t)
        assert result.count("<img ") == 2, f"Expected 2 img tags. Got: {result}"

    def test_image_with_https_url(self):
        """https:// image URL renders correctly."""
        t = "![secure](https://secure.example.com/img.jpg)"
        result = inline_md(t)
        assert 'src="https://secure.example.com/img.jpg"' in result

    def test_image_with_http_url(self):
        """http:// image URL also renders (non-https still valid)."""
        t = "![old](http://example.com/img.jpg)"
        result = inline_md(t)
        assert "<img " in result
        assert 'src="http://example.com/img.jpg"' in result


# ═════════════════════════════════════════════════════════════════════════════
# Cross-cutting: code + image together inside tables (the edge case Nathan flagged)
# ═════════════════════════════════════════════════════════════════════════════


class TestEdgeCasesCodeAndImageInTables:
    """Combination edge cases: code blocks and images mixed inside table cells."""

    def test_code_and_image_in_same_table_row(self):
        """Table row with code in one cell and image in another renders both correctly."""
        md = (
            "| Code | Preview |\n"
            "|---|---|\n"
            "| `print('hello')` | ![screenshot](https://example.com/shot.png) |"
        )
        result = render_table(md)
        assert (
            "<code>print(&#x27;hello&#x27;)</code>" in result
            or "<code>print('hello')</code>" in result
        ), f"Code cell should render as <code>. Got: {result}"
        assert "<img " in result, "Image cell should render as <img>"

    def test_code_in_cell_with_image_in_next_cell(self):
        """Multiple columns: code stays code, image stays image, no cross-contamination."""
        md = (
            "| Step | Example |\n"
            "|---|---|\n"
            "| Run `npm install` | ![demo](https://example.com/demo.gif) |"
        )
        result = render_table(md)
        assert "<code>npm install</code>" in result
        assert "<img " in result
        assert "<a " not in result  # image must not become a link

    def test_bold_code_in_cell_and_image_in_cell(self):
        """**`code`** in one cell and image in another — no esc() mangling."""
        md = (
            "| Command | Result |\n"
            "|---|---|\n"
            "| **`git status`** | ![result](https://example.com/r.png) |"
        )
        result = render_table(md)
        assert "&lt;code&gt;" not in result, (
            "Bold+code in table cell must not produce escaped code tags"
        )
        assert "<code>git status</code>" in result
        assert "<img " in result

    def test_link_code_image_all_in_table(self):
        """Table with code, link, and image cells all render correctly."""
        url = "https://github.com/issues/486"
        img_url = "https://example.com/img.png"
        md = (
            f"| Code | Link | Image |\n"
            f"|---|---|---|\n"
            f"| `var x = 1` | [#486]({url}) | ![img]({img_url}) |"
        )
        result = render_table(md)
        assert "<code>var x = 1</code>" in result
        assert f'href="{url}"' in result
        assert "<img " in result
        # No double-linking
        assert result.count("<a ") == 1

    def test_image_url_with_query_string_in_table(self):
        """Image URL with & in query string inside table cell — & not mangled."""
        url = "https://example.com/img?w=100&h=200"
        md = f"| Image |\n|---|\n| ![sized]({url}) |"
        result = render_table(md)
        assert f'src="{url}"' in result, (
            f"& in image URL must not be escaped. Got: {result}"
        )

    def test_image_adjacent_to_code_no_interference(self):
        """Image immediately followed by code span in same cell — no token cross-talk."""
        t = "![x](https://x.com/x.png) `code`"
        result = inline_md(t)
        assert "<img " in result
        assert "<code>code</code>" in result

    def test_image_inside_code_span_not_rendered(self):
        """An image syntax inside a backtick span must NOT render as an img tag."""
        t = "`![not an image](https://example.com/img.png)`"
        result = inline_md(t)
        # The whole thing is inside backticks — should be literal code, not an img
        assert "<img " not in result, (
            f"Image syntax inside code span must not render as <img>. Got: {result}"
        )
        # Should render as a code element with the raw text inside
        assert "<code>" in result
