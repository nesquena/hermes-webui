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


def test_yuno_i18n_lists_skin_in_all_locales():
    assert I18N_JS.count("/zeus/yuno") == 15
