from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _function_body(name: str, *, limit: int = 5000) -> str:
    start = MESSAGES_JS.index(f"function {name}")
    return MESSAGES_JS[start : start + limit]


def test_inactive_prompt_flyouts_are_hidden_from_accessibility_tree_initially():
    """Approval/clarify flyouts are not persistent dialogs when no prompt is pending."""
    approval_start = INDEX.index('id="approvalCard"')
    approval_tag = INDEX[INDEX.rfind("<div", 0, approval_start) : INDEX.index(">", approval_start) + 1]
    clarify_start = INDEX.index('id="clarifyCard"')
    clarify_tag = INDEX[INDEX.rfind("<div", 0, clarify_start) : INDEX.index(">", clarify_start) + 1]

    for tag in (approval_tag, clarify_tag):
        assert " hidden" in tag
        assert 'aria-hidden="true"' in tag
        assert " inert" in tag

    assert 'role="alertdialog"' in approval_tag
    assert 'role="dialog"' in clarify_tag


def test_prompt_flyout_hidden_css_guard_keeps_hidden_flyouts_display_none():
    """Future display rules must not override hidden prompt flyouts."""
    compact_style = STYLE.replace(" ", "")

    assert ".approval-card[hidden],.clarify-card[hidden]{display:none!important;}" in compact_style


def test_prompt_flyout_hidden_helper_toggles_hidden_aria_and_inert():
    body = _function_body("_setPromptFlyoutHidden", limit=800)

    assert 'card.setAttribute("aria-hidden", "true")' in body
    assert 'card.setAttribute("aria-hidden", "false")' in body
    assert 'card.setAttribute("inert", "")' in body
    assert 'card.removeAttribute("inert")' in body
    assert "card.hidden = true" in body
    assert "card.hidden = false" in body
    assert "void card.offsetHeight" in body


def test_approval_show_and_hide_toggle_accessibility_visibility():
    hide_body = _function_body("hideApprovalCard")
    show_body = _function_body("showApprovalCard")

    assert 'card.classList.remove("visible")' in hide_body
    assert "_setPromptFlyoutHidden(card, true)" in hide_body
    assert "_setPromptFlyoutHidden(card, false)" in show_body
    assert 'card.classList.add("visible")' in show_body
    assert show_body.index("_setPromptFlyoutHidden(card, false)") < show_body.index('card.classList.add("visible")')


def test_clarify_show_and_hide_toggle_accessibility_visibility():
    ensure_body = _function_body("_ensureClarifyCardDom")
    hide_body = _function_body("hideClarifyCard")
    show_body = _function_body("showClarifyCard")

    assert "_setPromptFlyoutHidden(card, true)" in ensure_body
    assert 'card.classList.remove("visible")' in hide_body
    assert "_setPromptFlyoutHidden(card, true)" in hide_body
    assert "_setPromptFlyoutHidden(card, false)" in show_body
    assert 'card.classList.add("visible")' in show_body
    assert show_body.index("_setPromptFlyoutHidden(card, false)") < show_body.index('card.classList.add("visible")')
