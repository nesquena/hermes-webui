"""Phase 2 — Neo mobile composer contract tests.

Verify the mobile composer meets the spec:
- Textarea font-size 16px in mobile breakpoint (iOS zoom prevention)
- Send button 44x44 in mobile breakpoint
- Plus-menu button present with menu items
- Individual chips hidden on mobile (profile/workspace/model/reasoning)
- Context chip present for bottom-sheet trigger
- Bottom-sheet overlay and panel present in HTML
- Bottom-sheet has reasoning segmented control (3 states)
- drop-hint hidden on mobile
- ctx-indicator hidden on mobile
- bg-badge hidden on mobile
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")


def _mobile_640_block():
    start = CSS.find("@media(max-width:640px)")
    return CSS[start:start + 6000]


def test_textarea_font_size_16px_mobile():
    block = _mobile_640_block()
    assert "font-size:16px" in block, \
        "textarea must have font-size:16px in mobile to prevent iOS zoom"


def test_send_btn_44x44_mobile():
    block = _mobile_640_block()
    match = re.search(r'\.send-btn\{[^}]*width:44px[^}]*height:44px', block)
    assert match, "send-btn must be 44x44 in mobile breakpoint"


def test_plus_menu_button_present():
    assert 'id="btnComposerPlus"' in HTML, \
        "Plus-menu button must be present in composer"


def test_plus_menu_items():
    assert 'id="composerPlusMenu"' in HTML, \
        "Plus-menu container must exist"
    menu_start = HTML.find('id="composerPlusMenu"')
    menu_block = HTML[menu_start:menu_start + 2000]
    assert "Anexar arquivo" in menu_block
    assert "Workspace files" in menu_block
    assert "YOLO mode" in menu_block
    assert "Terminal" in menu_block


def test_individual_chips_hidden_mobile():
    block = _mobile_640_block()
    assert ".composer-profile-wrap{display:none" in block, \
        "Profile chip must be hidden on mobile"
    assert ".composer-model-wrap{display:none" in block, \
        "Model chip must be hidden on mobile"


def test_context_chip_present():
    assert 'id="composerContextChip"' in HTML, \
        "Context chip must be present for bottom-sheet trigger"


def test_context_chip_visible_mobile():
    block = _mobile_640_block()
    assert ".composer-context-chip{display:flex" in block, \
        "Context chip must be display:flex on mobile"


def test_bottomsheet_overlay_present():
    assert 'id="composerBottomSheetOverlay"' in HTML, \
        "Bottom-sheet overlay must exist"


def test_bottomsheet_panel_present():
    assert 'id="composerBottomSheet"' in HTML, \
        "Bottom-sheet panel must exist"


def test_bottomsheet_reasoning_segmented():
    assert 'id="bottomSheetReasoning"' in HTML
    sheet_start = HTML.find('id="bottomSheetReasoning"')
    sheet_block = HTML[sheet_start:sheet_start + 500]
    assert 'data-effort="none"' in sheet_block
    assert 'data-effort="medium"' in sheet_block
    assert 'data-effort="high"' in sheet_block


def test_drop_hint_hidden_mobile():
    block = _mobile_640_block()
    assert ".drop-hint{display:none" in block, \
        "drop-hint must be hidden on mobile"


def test_ctx_indicator_hidden_mobile():
    block = _mobile_640_block()
    assert ".ctx-indicator-wrap{display:none" in block, \
        "ctx-indicator must be hidden on mobile"


def test_bg_badge_hidden_mobile():
    block = _mobile_640_block()
    assert ".bg-badge{display:none" in block, \
        "bg-badge must be hidden on mobile"


def test_toggle_composer_plus_menu_js():
    assert "function toggleComposerPlusMenu(" in BOOT_JS, \
        "toggleComposerPlusMenu must be defined in boot.js"


def test_toggle_composer_bottom_sheet_js():
    assert "function toggleComposerBottomSheet(" in BOOT_JS, \
        "toggleComposerBottomSheet must be defined in boot.js"
