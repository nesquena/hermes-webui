"""Zeus skin registration and dark-surface affordances."""

from pathlib import Path

REPO = Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def test_zeus_skin_is_registered_in_all_files():
    assert "{name:'Zeus'" in BOOT_JS
    assert "zeus:1" in INDEX_HTML
    assert '"zeus"' in CONFIG_PY


def test_zeus_dark_surfaces_are_near_black():
    assert ':root.dark[data-skin="zeus"]' in CSS
    assert "--bg:#0F0F0F" in CSS
    assert "--sidebar:#111111" in CSS
    assert "--surface:#181818" in CSS


def test_zeus_preserves_default_gold_accent():
    # Zeus does NOT redefine --accent; it stays with the default gold.
    # Verify gold-tinted border/focus vars are present instead.
    assert "--border2:rgba(255,215,0,0.18)" in CSS
    assert "--focus-ring:rgba(255,215,0,.4)" in CSS
    assert "--hover-bg:rgba(255,215,0,.06)" in CSS


def test_zeus_active_session_uses_gold_highlight():
    assert ':root.dark[data-skin="zeus"] .session-item.active' in CSS
    assert "border-left:2px solid #FFD700" in CSS


def test_zeus_modals_are_not_navy():
    # Modals/dialogs default to a hardcoded navy gradient — Zeus must override
    assert ':root.dark[data-skin="zeus"] .app-dialog' in CSS
    assert ':root.dark[data-skin="zeus"] .kanban-modal' in CSS
    assert "rgba(24,24,24,.99)" in CSS


def test_zeus_i18n_lists_skin_in_all_locales():
    # Zeus is the last skin in each locale's cmd_theme string, so it appears
    # as `…/zeus)` rather than `/zeus/`. There are 10 locales.
    assert I18N_JS.count("zeus)") == 10
