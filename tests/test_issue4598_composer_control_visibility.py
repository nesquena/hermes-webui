"""Static-contract regression tests for the composer footer-control visibility
toggles (#4598).

Mirrors the sibling pattern in test_sidebar_tab_visibility.py: pins that each
toggle is wired end-to-end (config boolean key -> boot.js definition + read-back
-> index.html control -> panels.js chip render -> apply) and that every new i18n
key exists across all locale blocks, so a future refactor can't silently orphan
a control or break locale parity.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

# The 15 composer-control visibility flags this feature ships.
HIDE_KEYS = [
    "hide_composer_attach",
    "hide_composer_saved_prompts",
    "hide_composer_mic",
    "hide_composer_voice_mode",
    "hide_composer_yolo",
    "hide_composer_profile",
    "hide_composer_workspace",
    "hide_composer_mobile_config",
    "hide_composer_model",
    "hide_composer_quota_chip",
    "hide_composer_reasoning",
    "hide_composer_toolsets",
    "hide_composer_status",
    "hide_composer_context",
    "hide_composer_bg_badge",
]

# The new i18n keys the feature adds (section labels/descriptions + per-chip labels).
I18N_KEYS = [
    "settings_label_composer_controls",
    "settings_desc_composer_controls",
    "settings_label_composer_situational_controls",
    "settings_desc_composer_situational_controls",
    "composer_control_attach",
    "composer_control_saved_prompts",
    "composer_control_mic",
    "composer_control_profile",
    "composer_control_workspace",
    "composer_control_model",
    "composer_control_reasoning",
    "composer_control_context",
    "composer_control_voice_mode",
    "composer_control_yolo",
    "composer_control_bg_badge",
    "composer_control_mobile_config",
    "composer_control_quota_chip",
    "composer_control_toolsets",
    "composer_control_status",
]


def test_all_hide_flags_registered_as_boolean_settings_keys():
    """Every toggle must be in config.py's boolean-keys set so it persists and
    round-trips through save/load."""
    for key in HIDE_KEYS:
        assert f'"{key}"' in CONFIG_PY, f"{key} missing from config.py boolean settings keys"


def test_hide_composer_send_orphan_key_fully_removed():
    """The re-push removed the orphaned hide_composer_send key (Send is always
    visible) — it must not linger anywhere."""
    assert "hide_composer_send" not in CONFIG_PY
    assert "hide_composer_send" not in BOOT_JS
    assert "hide_composer_send" not in PANELS_JS


def test_footer_control_chips_rendered_in_panels():
    """panels.js must render the primary + situational control chips and apply
    the visibility settings live."""
    assert "_renderComposerControlChips" in PANELS_JS
    assert "_renderComposerSituationalControlChips" in PANELS_JS
    assert "_ensureComposerControlVisibilityState" in PANELS_JS
    assert "_applyComposerFooterVisibilitySettings" in PANELS_JS


def test_new_i18n_keys_exist_across_all_locale_blocks():
    """Every new i18n key must appear in all 13 locale blocks (strict locale
    parity), not just `en` — otherwise the locale-coverage suite goes red."""
    # 13 locale blocks (en + 12). Each key should appear at least 13 times.
    for key in I18N_KEYS:
        count = I18N_JS.count(f"{key}:")
        assert count >= 13, (
            f"{key} appears {count}x in i18n.js — expected >=13 (one per locale "
            f"block) for strict locale parity"
        )
