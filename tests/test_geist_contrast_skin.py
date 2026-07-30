"""Geist Contrast skin registration and contrast affordances."""

from pathlib import Path

REPO = Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")


def test_geist_contrast_skin_is_registered_with_matching_key_and_label():
    assert "{name:'Geist Contrast'" in BOOT_JS
    assert "value:'geist-contrast'" in BOOT_JS
    assert "s.value||s.name" in BOOT_JS
    assert "'geist-contrast':1" in INDEX_HTML
    assert '"geist-contrast"' in CONFIG_PY


def test_geist_contrast_slash_theme_uses_skin_value_key():
    assert "const skins=(_SKINS||[]).map(s=>(s.value||s.name).toLowerCase());" in COMMANDS_JS
    assert "skins.includes(val)" in COMMANDS_JS
    assert "showToast(t('theme_set')+appearance.skin)" in COMMANDS_JS


def test_geist_contrast_dark_tokens_use_yellow_accent_with_neutral_surfaces():
    assert ':root.dark[data-skin="geist-contrast"]' in CSS
    assert "--bg:#000000" in CSS
    assert "--surface:#0a0a0a" in CSS
    assert "--text:#ededed" in CSS
    assert "--accent:#FFF175" in CSS
    assert "--accent-text:#f5e65f" in CSS


def test_geist_contrast_selection_is_neutral_not_solid_yellow():
    active_rule = ':root[data-skin="geist-contrast"] .session-item.active{position:relative;border:1px solid var(--border2)!important;}'
    marker_rule = ':root[data-skin="geist-contrast"] .session-item.active::before{content:"";position:absolute;left:6px;top:10px;bottom:10px;width:2px;border-radius:999px;background:var(--accent);}'
    dark_text_rule = ':root.dark[data-skin="geist-contrast"] .session-item.active,\n  :root.dark[data-skin="geist-contrast"] .session-item.active *{color:var(--text)!important;}'
    assert active_rule in CSS
    assert marker_rule in CSS
    assert dark_text_rule in CSS


def test_geist_contrast_solid_accent_controls_use_black_text_in_dark_mode():
    assert ':root[data-skin="geist-contrast"] button.send-btn:not(:disabled)' in CSS
    assert "color:#050505!important" in CSS
    assert ':root[data-skin="geist-contrast"] button.send-btn:disabled{background:var(--surface-subtle)!important;border-color:var(--border)!important;color:var(--muted)!important;opacity:1!important;}' in CSS
    assert ':root.dark[data-skin="geist-contrast"] button.send-btn:disabled svg' in CSS


def test_geist_contrast_composer_chips_are_neutral_until_hovered():
    neutral_rule = ':root[data-skin="geist-contrast"] .profile-chip,\n  :root[data-skin="geist-contrast"] .model-chip,\n  :root[data-skin="geist-contrast"] .reasoning-chip,\n  :root[data-skin="geist-contrast"] .composer-workspace-chip,\n  :root[data-skin="geist-contrast"] .composer-profile-chip,\n  :root[data-skin="geist-contrast"] .composer-model-chip{color:var(--muted)!important;border-color:transparent!important;background:transparent!important;}'
    assert neutral_rule in CSS
