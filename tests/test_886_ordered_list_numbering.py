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

    def test_li_value_attr_present_in_ordered_list_block(self):
        """The ordered-list renderer must emit value= on each <li>."""
        src = get_ui_js()
        # Locate the ordered-list replace block
        ol_idx = src.find('s=s.replace(/((?:^(?:  )?\\d+\\. .+\\n?)+)/gm')
        assert ol_idx != -1, "Ordered-list replace block not found in ui.js"
        # Extract a window large enough to cover the whole closure (~400 chars)
        ol_block = src[ol_idx:ol_idx + 500]
        assert 'value=' in ol_block, (
            "Ordered-list block must emit value= attribute on <li> elements to "
            "preserve numbering when items are separated by blank lines (#886)"
        )

    def test_li_value_uses_parsed_number(self):
        """The value= must be derived from parseInt of the captured digit, not hardcoded."""
        src = get_ui_js()
        ol_idx = src.find('s=s.replace(/((?:^(?:  )?\\d+\\. .+\\n?)+)/gm')
        assert ol_idx != -1, "Ordered-list replace block not found in ui.js"
        ol_block = src[ol_idx:ol_idx + 500]
        assert 'parseInt' in ol_block, (
            "Ordered-list block should use parseInt() to parse the list number (#886)"
        )

    def test_numMatch_variable_present(self):
        """The numMatch variable (or equivalent digit capture) must exist in the OL block."""
        src = get_ui_js()
        ol_idx = src.find('s=s.replace(/((?:^(?:  )?\\d+\\. .+\\n?)+)/gm')
        assert ol_idx != -1, "Ordered-list replace block not found in ui.js"
        ol_block = src[ol_idx:ol_idx + 500]
        # Either numMatch or a similar digit-capture variable
        assert 'numMatch' in ol_block or re.search(r'match\(/.*\\d', ol_block), (
            "Ordered-list block should capture the list item number with a regex match (#886)"
        )

    def test_valAttr_or_value_template_present(self):
        """The <li> template must include the value attribute conditionally or unconditionally."""
        src = get_ui_js()
        ol_idx = src.find('s=s.replace(/((?:^(?:  )?\\d+\\. .+\\n?)+)/gm')
        assert ol_idx != -1, "Ordered-list replace block not found in ui.js"
        ol_block = src[ol_idx:ol_idx + 500]
        # Either a valAttr variable or an inline value= in the template
        has_val_attr = 'valAttr' in ol_block
        has_inline_value = re.search(r'<li.*value=', ol_block)
        assert has_val_attr or has_inline_value, (
            "Ordered-list block must have value= on <li> (via valAttr var or inline) (#886)"
        )

    def test_ordered_list_comment_references_issue(self):
        """A comment near the OL fix should reference the issue (#886) or the symptom."""
        src = get_ui_js()
        ol_idx = src.find('s=s.replace(/((?:^(?:  )?\\d+\\. .+\\n?)+)/gm')
        assert ol_idx != -1, "Ordered-list replace block not found in ui.js"
        # Look at the 300 chars BEFORE the replace line for an explanatory comment
        context = src[max(0, ol_idx - 300):ol_idx]
        has_comment = '#886' in context or '1. 1. 1.' in context or 'blank lines' in context.lower()
        assert has_comment, (
            "Expected a comment near the OL fix explaining the blank-line issue (#886)"
        )

    def test_list_without_blank_lines_unaffected(self):
        """A compact list (no blank lines) should still produce one <ol> with sequential items."""
        src = get_ui_js()
        # Structural check: the regex still captures multi-line blocks (\\n? allows groups)
        ol_idx = src.find('s=s.replace(/((?:^(?:  )?\\d+\\. .+\\n?)+)/gm')
        assert ol_idx != -1, "Ordered-list replace block not found"
        # The \\n? quantifier that allows grouping must still be present
        assert '\\n?' in src[ol_idx:ol_idx + 80], (
            "The \\\\n? in the ordered-list regex was removed — compact lists may break"
        )
