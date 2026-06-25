"""Regression test for issue #3851: mobile jump-to-question visibility.

Ensures that .msg-question-jump-btn remains visible on mobile screens
instead of being completely hidden with display: none.
"""
import re
from pathlib import Path

_CSS_PATH = Path(__file__).resolve().parents[1] / "static" / "style.css"


def test_msg_question_jump_btn_mobile_not_display_none():
    """Assert the mobile media query for .msg-question-jump-btn does NOT hide it."""
    css_content = _CSS_PATH.read_text(encoding='utf-8')

    mobile_pattern = r'@media\s*\([^)]*max-width\s*:\s*600px[^)]*\)\s*\{[^}]*\.msg-question-jump-btn[^}]*\}'
    mobile_match = re.search(mobile_pattern, css_content, re.DOTALL)

    assert mobile_match is not None, (
        "No mobile media query found for .msg-question-jump-btn"
    )

    mobile_rule = mobile_match.group(0)
    assert 'display: none' not in mobile_rule and 'display:none' not in mobile_rule, (
        f"Mobile rule for .msg-question-jump-btn should not have display: none. "
        f"Found: {mobile_rule}"
    )


def test_msg_question_jump_btn_mobile_has_visible_styling():
    """Assert the mobile media query provides compact visible styling for the button."""
    css_content = _CSS_PATH.read_text(encoding='utf-8')

    pattern = r'@media\s*\([^)]*max-width\s*:\s*600px[^)]*\)\s*\{[^}]*\.msg-question-jump-btn\s*\{([^}]*)\}'
    match = re.search(pattern, css_content, re.DOTALL)

    assert match is not None, "No .msg-question-jump-btn rule inside a mobile media query"

    rule_body = match.group(1)
    assert 'padding' in rule_body, (
        f"Mobile .msg-question-jump-btn rule should have padding for compact styling. "
        f"Found: {rule_body.strip()}"
    )


def test_msg_question_jump_btn_text_span_hidden_on_mobile():
    """Assert that the text span is hidden inside the mobile media query, not globally."""
    css_content = _CSS_PATH.read_text(encoding='utf-8')

    pattern = r'@media\s*\([^)]*max-width\s*:\s*600px[^)]*\)\s*\{.*?\.msg-question-jump-btn\s+span:last-child\s*\{\s*display\s*:\s*none'
    assert re.search(pattern, css_content, re.DOTALL) is not None, (
        "Missing span:last-child { display: none; } inside a mobile media query. "
        "The rule must be scoped to @media (max-width: 600px), not applied globally."
    )
