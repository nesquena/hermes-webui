"""Phase 3 — Neo mobile bottom-nav and shell polish contract tests.

Verify:
- Bottom-nav present in HTML with 5 items (Dashboard, Chat, Projetos, Tarefas, Mais)
- Bottom-nav hidden by default, shown at ≤640px
- Bottom-nav items have data-panel attributes for switchPanel integration
- Hero-card reduced height on mobile (≤160px)
- Topbar-chips have mask/fade on mobile
- Body has padding-bottom for bottom-nav clearance on mobile
- syncMobileNav function exists in boot.js
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")


def _mobile_640_block():
    start = CSS.find("@media(max-width:640px)")
    return CSS[start:start + 8000]


def test_bottom_nav_present():
    assert 'id="neoMobileNav"' in HTML, \
        "Bottom-nav element must be present in HTML"
    assert 'class="neo-mobile-nav"' in HTML, \
        "Bottom-nav must use neo-mobile-nav class"


def test_bottom_nav_5_items():
    nav_start = HTML.find('id="neoMobileNav"')
    nav_block = HTML[nav_start:nav_start + 3000]
    items = nav_block.count('neo-mobile-nav-item')
    assert items >= 5, f"Bottom-nav must have 5 items, found {items}"


def test_bottom_nav_data_panels():
    nav_start = HTML.find('id="neoMobileNav"')
    nav_block = HTML[nav_start:nav_start + 3000]
    assert 'data-panel="dashboard"' in nav_block
    assert 'data-panel="chat"' in nav_block
    assert 'data-panel="projects"' in nav_block
    assert 'data-panel="tasks"' in nav_block
    assert 'data-panel="more"' in nav_block


def test_bottom_nav_hidden_by_default():
    base_rule = re.search(r'\.neo-mobile-nav\{[^}]*display:none', CSS)
    assert base_rule, "neo-mobile-nav must be display:none by default"


def test_bottom_nav_visible_mobile():
    block = _mobile_640_block()
    assert ".neo-mobile-nav{display:flex" in block, \
        "neo-mobile-nav must be display:flex in mobile breakpoint"


def test_composer_padding_bottom_mobile():
    block = _mobile_640_block()
    assert "padding-bottom:" in block, \
        "composer-wrap must have padding-bottom on mobile for bottom-nav clearance"


def test_hero_card_reduced_mobile():
    block = _mobile_640_block()
    assert ".hero-card{" in block, "hero-card must have mobile override"
    hero_match = re.search(r'\.hero-card\{[^}]*height:[^}]*160px', block)
    assert hero_match, "hero-card must be max 160px on mobile"


def test_topbar_chips_fade_mobile():
    block = _mobile_640_block()
    assert "mask-image:" in block or "-webkit-mask-image:" in block, \
        "topbar-chips must have mask/fade on mobile for scroll indication"


def test_sync_mobile_nav_js():
    assert "function syncMobileNav(" in BOOT_JS, \
        "syncMobileNav must be defined in boot.js"


def test_bottom_nav_touch_targets():
    base_rule = re.search(r'\.neo-mobile-nav-item\{[^}]*min-width:48px', CSS)
    assert base_rule, "neo-mobile-nav-item must have min-width 48px for touch targets"


def test_bottom_nav_safe_area():
    base_rule = re.search(r'\.neo-mobile-nav\{[^}]*safe-area-inset-bottom', CSS)
    assert base_rule, "neo-mobile-nav must respect safe-area-inset-bottom"
