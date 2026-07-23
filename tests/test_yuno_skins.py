"""Yuno skin family registration and palette affordances."""

from pathlib import Path

REPO = Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def test_yuno_skin_is_registered_in_all_files():
    assert "{name:'Yuno'" in BOOT_JS
    assert "yuno:1" in INDEX_HTML
    assert '"yuno"' in CONFIG_PY


def test_yuno_has_paired_light_and_dark_palettes():
    assert ':root[data-skin="yuno"]{' in CSS
    assert ':root.dark[data-skin="yuno"]{' in CSS


def test_yuno_light_palette_is_cozy_cream():
    assert "--bg:#FFFBF5" in CSS
    assert "--sidebar:#FFF6E8" in CSS
    assert "--accent:#B7295B" in CSS


def test_yuno_dark_palette_is_charcoal_pink():
    assert "--bg:#18181B" in CSS
    assert "--accent:#FF6BB5" in CSS
    assert "--focus-ring:rgba(255,61,175,.35)" in CSS


def test_yuno_dark_primary_buttons_use_charcoal_text():
    # Yuno's primary-fg rule: bright pink buttons carry dark text in dark mode.
    assert ':root.dark[data-skin="yuno"] .clarify-submit{color:#18181B;}' in CSS


def test_yuno_cyberpunk_and_hc_are_registered_in_all_files():
    assert "value:'yuno-cyberpunk'" in BOOT_JS
    assert "value:'yuno-hc'" in BOOT_JS
    assert "'yuno-cyberpunk':1" in INDEX_HTML
    assert "'yuno-hc':1" in INDEX_HTML
    assert '"yuno-cyberpunk"' in CONFIG_PY
    assert '"yuno-hc"' in CONFIG_PY


def test_yuno_cyberpunk_is_dark_only_neon_magenta():
    assert ':root.dark[data-skin="yuno-cyberpunk"]{' in CSS
    # Dark-only: no light root block should be registered.
    assert ':root[data-skin="yuno-cyberpunk"]{' not in CSS
    assert "--bg:#0A0E27" in CSS
    assert "--accent:#FF1493" in CSS
    assert "--info:#4DEEEA" in CSS


def test_yuno_hc_is_dark_only_high_contrast():
    assert ':root.dark[data-skin="yuno-hc"]{' in CSS
    assert ':root[data-skin="yuno-hc"]{' not in CSS
    assert "--bg:#000000" in CSS
    assert "--border:#FFFFFF" in CSS
    assert "--accent:#FF6BC6" in CSS
    # Solid, non-translucent focus indicator (AAA affordance).
    assert "--focus-ring:#FF6BC6" in CSS


def test_yuno_dark_only_skins_override_hardcoded_dialog_chrome():
    # Base .app-dialog/.kanban-modal styles hardcode navy gradients; every
    # full-palette dark skin must restyle them (zeus precedent).
    for skin in ("yuno-cyberpunk", "yuno-hc"):
        assert f':root.dark[data-skin="{skin}"] .app-dialog{{' in CSS
        assert f':root.dark[data-skin="{skin}"] .kanban-modal{{' in CSS
        assert f':root.dark[data-skin="{skin}"] .kanban-board-switcher-menu{{' in CSS


def test_yuno_i18n_lists_skin_family_in_all_locales():
    assert I18N_JS.count("/zeus/yuno/yuno-cyberpunk/yuno-hc/") == 15
