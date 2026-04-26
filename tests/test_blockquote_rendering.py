"""Regression tests for the blockquote rendering fix (fix/blockquote-rendering).

Root cause: the old rule was `s.replace(/^> (.+)$/gm, ...)` which had three bugs:
  1. `.+` required at least one character — bare `>` lines passed through as literal `>`
  2. Each line became its own `<blockquote>` — no grouping, so 10-line quotes became
     10 stacked `<blockquote>` elements
  3. Fenced code blocks inside blockquotes left orphaned `>` literals after the
     fence-stash pass had consumed the code content

Fix: group consecutive `>` lines into a single `<blockquote>`, handle bare `>` lines
as `<br>`, and strip the `>` prefix before passing each line to `inlineMd()`.
"""
import re
import pathlib

UI_JS = (pathlib.Path(__file__).parent.parent / "static" / "ui.js").read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Python mirror of the new blockquote rule + inlineMd (for behavioural tests)
# ---------------------------------------------------------------------------

import html as _html


def _esc(s):
    return _html.escape(str(s), quote=True)


def _inline_md(t):
    """Minimal inlineMd mirror — bold, italic, inline-code only."""
    t = re.sub(r"`([^`\n]+)`", lambda m: f"<code>{_esc(m.group(1))}</code>", t)
    t = re.sub(r"\*\*\*(.+?)\*\*\*", lambda m: f"<strong><em>{_esc(m.group(1))}</em></strong>", t)
    t = re.sub(r"\*\*(.+?)\*\*", lambda m: f"<strong>{_esc(m.group(1))}</strong>", t)
    t = re.sub(r"\*([^*\n]+)\*", lambda m: f"<em>{_esc(m.group(1))}</em>", t)
    return t


def _apply_blockquote(s):
    """Python mirror of the new group-based blockquote rule in ui.js."""
    def replacer(m):
        block = m.group(0)
        lines = block.split("\n")
        # Drop a lone trailing ">" artifact that the regex can leave
        while lines and lines[-1].strip() in (">", ""):
            if lines[-1].strip() == ">":
                lines.pop()
                break
            lines.pop()
        processed = []
        for l in lines:
            stripped = re.sub(r"^>[ \t]?", "", l)
            if stripped.strip() == "":
                processed.append("<br>")
            else:
                processed.append(_inline_md(stripped))
        inner = "\n".join(processed)
        return f"<blockquote>{inner}</blockquote>"

    return re.sub(r"((?:^>[^\n]*(?:\n|$))+)", replacer, s, flags=re.MULTILINE)


# ---------------------------------------------------------------------------
# Source-level structural tests
# ---------------------------------------------------------------------------

class TestBlockquoteSourceStructure:
    """The new rule must be present in ui.js and the old single-line rule must be gone."""

    def test_old_single_line_rule_removed(self):
        """The old `.+` pattern that skipped blank lines must be gone."""
        assert "replace(/^> (.+)$/gm" not in UI_JS, (
            "Old single-line blockquote rule still present — it misses blank '>'"
            " lines and creates one <blockquote> per line"
        )

    def test_new_group_rule_present(self):
        """The new grouping regex must be present."""
        assert "(?:^>[^\\n]*(?:\\n|$))+" in UI_JS, (
            "New group-based blockquote rule not found in ui.js"
        )

    def test_prefix_strip_present(self):
        """The fix must strip the '> ' prefix from each line."""
        assert "replace(/^>[" in UI_JS or "replace(/^>[ " in UI_JS, (
            "Expected prefix-strip pattern not found in the blockquote block"
        )


# ---------------------------------------------------------------------------
# Behavioural tests (using the Python mirror)
# ---------------------------------------------------------------------------

class TestMultiLineBlockquote:
    """Consecutive > lines must become ONE <blockquote>, not many."""

    def test_single_line_still_works(self):
        out = _apply_blockquote("> Hello world")
        assert out.count("<blockquote>") == 1
        assert "Hello world" in out
        assert ">" not in out.replace("<blockquote>", "").replace("</blockquote>", "")

    def test_two_consecutive_lines_grouped(self):
        src = "> Line one\n> Line two"
        out = _apply_blockquote(src)
        assert out.count("<blockquote>") == 1, (
            f"Expected 1 <blockquote>, got {out.count('<blockquote>')}: {out!r}"
        )

    def test_ten_lines_one_blockquote(self):
        src = "\n".join(f"> Line {i}" for i in range(10))
        out = _apply_blockquote(src)
        assert out.count("<blockquote>") == 1

    def test_two_separate_quotes_stay_separate(self):
        src = "> First quote\n\n> Second quote"
        out = _apply_blockquote(src)
        # Each quote is its own group (separated by a blank line)
        assert out.count("<blockquote>") == 2


class TestBlankContinuationLines:
    """Bare '>' lines (blank continuation) must not appear as literal '>'."""

    def test_bare_gt_line_no_literal(self):
        src = "> Para one\n>\n> Para two"
        out = _apply_blockquote(src)
        assert out.count("<blockquote>") == 1, f"Expected 1 blockquote: {out!r}"
        # No stray '>' outside of HTML tags
        text_only = re.sub(r"<[^>]+>", "", out)
        assert ">" not in text_only, f"Literal '>' in text: {text_only!r}"

    def test_bare_gt_no_space_handled(self):
        """'>' with no space at all should also be consumed, not rendered literally."""
        src = ">no space after"
        out = _apply_blockquote(src)
        assert out.count("<blockquote>") == 1
        text_only = re.sub(r"<[^>]+>", "", out)
        assert ">" not in text_only

    def test_blank_line_becomes_br(self):
        src = "> First\n>\n> Second"
        out = _apply_blockquote(src)
        assert "<br>" in out, f"Expected <br> for blank > line: {out!r}"


class TestInlineMarkdownInsideBlockquote:
    """Bold, italic, and inline code must still render correctly inside a blockquote."""

    def test_bold_inside_blockquote(self):
        out = _apply_blockquote("> This is **important**")
        assert "<strong>" in out
        assert "<blockquote>" in out

    def test_inline_code_inside_blockquote(self):
        out = _apply_blockquote("> Run `git status` first")
        assert "<code>" in out
        assert "<blockquote>" in out

    def test_italic_inside_blockquote(self):
        out = _apply_blockquote("> *emphasis* here")
        assert "<em>" in out
        assert "<blockquote>" in out


class TestNoPhantomTrailingBr:
    """The fix must drop both empty trailing lines (from a trailing \\n in the
    match) and bare '>' artifacts. Without this, the common case — a blockquote
    followed by another paragraph — renders with a phantom <br> right before
    </blockquote>, leaving a visible blank line at the bottom of the quote.
    """

    def test_input_ending_with_newline_no_trailing_br(self):
        """`> Hello\\n` must NOT produce `<blockquote>Hello\\n<br></blockquote>`."""
        out = _apply_blockquote("> Hello\n")
        assert "<br></blockquote>" not in out, (
            f"Trailing <br> leaked inside the blockquote (phantom blank line): {out!r}"
        )

    def test_blockquote_followed_by_paragraph_no_trailing_br(self):
        """The common real-world shape: quote + blank line + paragraph."""
        src = "> Quoted text\n\nNormal paragraph"
        out = _apply_blockquote(src)
        assert "<br></blockquote>" not in out, (
            f"Trailing <br> leaked inside blockquote when followed by paragraph: {out!r}"
        )

    def test_multiline_quote_ending_with_newline_no_trailing_br(self):
        out = _apply_blockquote("> Line one\n> Line two\n")
        assert "<br></blockquote>" not in out, (
            f"Multi-line quote ending with \\n must not leave a trailing <br>: {out!r}"
        )

    def test_quote_with_blank_continuation_then_newline(self):
        """`> A\\n>\\n> B\\n` — the internal `<br>` for the blank line stays,
        but the trailing newline must not add a second `<br>` at the end."""
        out = _apply_blockquote("> A\n>\n> B\n")
        # Internal <br> for the blank-line continuation is intentional
        assert "<br>" in out
        # But there must not be a <br> immediately before the closing tag
        assert "<br></blockquote>" not in out, (
            f"Trailing <br> leaked at end of blockquote: {out!r}"
        )


class TestBlockquoteFollowedByParagraph:
    """A blockquote followed by a normal paragraph must not bleed into each other."""

    def test_non_blockquote_paragraph_untouched(self):
        src = "> Quoted text\n\nNormal paragraph"
        out = _apply_blockquote(src)
        assert out.count("<blockquote>") == 1
        assert "Normal paragraph" in out
        # Normal paragraph must be outside the blockquote
        after_bq = out[out.index("</blockquote>"):]
        assert "Normal paragraph" in after_bq
