"""
Tests for #886: ordered list items always rendered as "1." regardless of position.

Root cause: when LLMs output numbered lists with blank lines between items,
the paragraph-splitter in renderMd() splits the markdown into one chunk per item,
so the ordered-list regex wraps each item in its own <ol>. Each <ol> restarts
at 1, producing "1. 1. 1." instead of "1. 2. 3.".

Fix: emit value="N" on every <li> so the correct ordinal is preserved even when
items end up in separate <ol> containers after the paragraph split.
"""
import os
import re

UI_JS = os.path.join(os.path.dirname(__file__), '..', 'static', 'ui.js')


def get_ui_js():
    return open(UI_JS, encoding='utf-8').read()


class TestOrderedListNumbering:
    def _ordered_list_block(self, src: str) -> str:
        start = src.find("function _renderListBlock(lines, ordered){")
        assert start != -1, "_renderListBlock helper not found in ui.js"
        end = src.find("function _renderLists(src, ordered){", start)
        assert end != -1, "_renderLists helper not found after _renderListBlock"
        return src[start:end]

    def _ordered_list_dispatch_block(self, src: str) -> str:
        start = src.find("s=_renderLists(s,true);")
        assert start != -1, "ordered-list dispatch not found in ui.js"
        return src[max(0, start - 260):start + 80]

    def test_li_value_attr_present_in_ordered_list_block(self):
        """The ordered-list renderer must emit value= on each <li>."""
        src = get_ui_js()
        ol_block = self._ordered_list_block(src)
        assert 'value=' in ol_block, (
            "Ordered-list block must emit value= attribute on <li> elements to "
            "preserve numbering when items are separated by blank lines (#886)"
        )

    def test_li_value_uses_parsed_number(self):
        """The value= must be derived from parseInt of the captured digit, not hardcoded."""
        src = get_ui_js()
        ol_block = self._ordered_list_block(src)
        assert 'parseInt' in ol_block, (
            "Ordered-list block should use parseInt() to parse the list number (#886)"
        )

    def test_numMatch_variable_present(self):
        """The ordered-list branch must still capture digits from the markdown marker."""
        src = get_ui_js()
        ol_block = self._ordered_list_block(src)
        assert "const marker=ordered?'\\\\d+\\\\. ':'[-*+] ';" in ol_block, (
            "Ordered-list block should keep a digit marker pattern for numbered items (#886)"
        )

    def test_valAttr_or_value_template_present(self):
        """The <li> template must include the value attribute conditionally or unconditionally."""
        src = get_ui_js()
        ol_block = self._ordered_list_block(src)
        has_value_attr = 'valueAttr' in ol_block
        has_inline_value = re.search(r'<li.*value=', ol_block)
        assert has_value_attr or has_inline_value, (
            "Ordered-list block must have value= on <li> (via valueAttr var or inline) (#886)"
        )

    def test_ordered_list_comment_references_issue(self):
        """A comment near the OL fix should reference the issue (#886) or the symptom."""
        src = get_ui_js()
        context = self._ordered_list_dispatch_block(src)
        has_comment = '#886' in context or '1. 1. 1.' in context or 'blank lines' in context.lower()
        assert has_comment, (
            "Expected a comment near the OL fix explaining the blank-line issue (#886)"
        )

    def test_list_without_blank_lines_unaffected(self):
        """A compact list should still flow through the ordered-list helper."""
        src = get_ui_js()
        ol_block = self._ordered_list_dispatch_block(src)
        assert "s=_renderLists(s,true);" in ol_block, (
            "Ordered-list rendering should still route through the shared helper"
        )
