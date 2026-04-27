"""Accessibility regressions for flyout dialogs hidden by default.

Approval and clarification cards are visually hidden until a pending tool approval
or clarify request arrives. Hidden dialogs must also be hidden from assistive
technology, otherwise screen readers announce inactive controls from the
accessibility tree.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "static" / "index.html"
MESSAGES = ROOT / "static" / "messages.js"


def test_initial_hidden_approval_and_clarify_cards_are_removed_from_a11y_tree():
    html = INDEX.read_text(encoding="utf-8")

    assert 'id="approvalCard"' in html
    assert 'id="clarifyCard"' in html

    approval_start = html.index('id="approvalCard"')
    approval_tag = html[html.rfind("<div", 0, approval_start):html.find(">", approval_start) + 1]
    clarify_start = html.index('id="clarifyCard"')
    clarify_tag = html[html.rfind("<div", 0, clarify_start):html.find(">", clarify_start) + 1]

    for tag in (approval_tag, clarify_tag):
        assert 'aria-hidden="true"' in tag
        assert "inert" in tag


def test_show_and_hide_approval_card_toggle_a11y_visibility():
    src = MESSAGES.read_text(encoding="utf-8")
    hide_block = src[src.index("function hideApprovalCard"):src.index("function showApprovalCard")]
    show_block = src[src.index("function showApprovalCard"):src.index("async function respondApproval")]

    assert 'card.setAttribute("aria-hidden", "true")' in hide_block
    assert "card.setAttribute(\"inert\", \"\")" in hide_block or "card.inert = true" in hide_block
    assert 'card.removeAttribute("aria-hidden")' in show_block
    assert "card.removeAttribute(\"inert\")" in show_block or "card.inert = false" in show_block


def test_show_and_hide_clarify_card_toggle_a11y_visibility():
    src = MESSAGES.read_text(encoding="utf-8")
    create_block = src[src.index("function _ensureClarifyCardDom"):src.index("function _clearClarifyHideTimer")]
    hide_block = src[src.index("function hideClarifyCard"):src.index("function _clarifySetControlsDisabled")]
    show_block = src[src.index("function showClarifyCard"):src.index("async function respondClarify")]

    assert 'card.setAttribute("aria-hidden", "true")' in create_block
    assert "card.setAttribute(\"inert\", \"\")" in create_block or "card.inert = true" in create_block
    assert 'card.setAttribute("aria-hidden", "true")' in hide_block
    assert "card.setAttribute(\"inert\", \"\")" in hide_block or "card.inert = true" in hide_block
    assert 'card.removeAttribute("aria-hidden")' in show_block
    assert "card.removeAttribute(\"inert\")" in show_block or "card.inert = false" in show_block
