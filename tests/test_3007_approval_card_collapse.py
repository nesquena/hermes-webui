from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _compact(text: str) -> str:
    return "".join(text.split())


def test_toggle_approval_card_collapsed_defined():
    assert "function toggleApprovalCardCollapsed(" in MESSAGES_JS


def test_sync_approval_collapse_button_defined():
    assert "function _syncApprovalCollapseButton(" in MESSAGES_JS


def test_sync_approval_transcript_space_defined():
    assert "function _syncApprovalTranscriptSpace(" in MESSAGES_JS


def test_approval_collapsed_toggle_in_messages_js():
    compact_js = _compact(MESSAGES_JS)
    assert 'card.classList.toggle("collapsed",collapsed)' in compact_js


def test_approval_collapsed_cleared_in_hide():
    # hideApprovalCard must reset collapse state so next approval opens expanded
    compact_js = _compact(MESSAGES_JS)
    assert 'card.classList.remove("collapsed")' in compact_js


def test_messages_approval_open_in_css():
    assert ".messages.approval-open" in STYLE_CSS


def test_messages_approval_collapsed_in_css():
    assert ".messages.approval-collapsed" in STYLE_CSS


def test_approval_dock_height_padding_in_css():
    compact_css = _compact(STYLE_CSS)
    assert "padding-bottom:var(--approval-dock-height,72px)" in compact_css


def test_approval_card_collapsed_header_margin_in_css():
    assert ".approval-card.collapsed .approval-header" in STYLE_CSS


def test_approval_card_collapsed_desc_hidden_in_css():
    assert ".approval-card.collapsed .approval-desc" in STYLE_CSS


def test_approval_collapse_button_in_html():
    assert 'id="approvalCollapse"' in INDEX_HTML


def test_approval_collapse_aria_expanded_in_html():
    assert 'aria-expanded="true"' in INDEX_HTML


def test_approval_collapse_onclick_in_html():
    assert 'onclick="toggleApprovalCardCollapsed()"' in INDEX_HTML


def test_sync_approval_transcript_space_called_in_show_and_hide():
    # Must be called from both showApprovalCard and hideApprovalCard
    compact_js = _compact(MESSAGES_JS)
    assert compact_js.count("_syncApprovalTranscriptSpace(") >= 3  # show, hide, toggle
    assert "_syncApprovalTranscriptSpace(null)" in MESSAGES_JS
    # show mirrors the merged clarify card: mark visible first, then sync immediately so the transcript reserves space on first paint
    assert 'card.classList.add("visible");_syncApprovalCollapseButton(card);_syncApprovalTranscriptSpace(card,{immediate:true})' in compact_js
