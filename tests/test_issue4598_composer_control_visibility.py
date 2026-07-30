"""Static-contract regression tests for the composer footer-control visibility
toggles (#4598).

Mirrors the sibling pattern in test_sidebar_tab_visibility.py: pins that each
toggle is wired end-to-end (config boolean key -> boot.js definition + read-back
-> index.html control -> panels.js chip render -> apply) and that every new i18n
key exists across all locale blocks, so a future refactor can't silently orphan
a control or break locale parity.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

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


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    for idx in range(brace, len(source)):
        char = source[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"Could not extract {name}")


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


def test_composer_control_order_frontend_contracts():
    """Footer reorder must coexist with the now-required situational chips."""
    assert "composer_control_order" in CONFIG_PY
    assert "composer_control_order" in BOOT_JS
    assert "composer_control_order" in PANELS_JS
    assert "_sanitizeComposerControlOrder" in BOOT_JS
    assert "_orderedComposerControlDefs" in BOOT_JS
    assert "_applyComposerControlOrder" in BOOT_JS
    assert "orderSelector" in BOOT_JS
    assert "orderGroup" in BOOT_JS
    assert "_COMPOSER_CONTROL_ORDER_LS_KEY" in PANELS_JS
    assert "_orderedComposerControlDefsForSettings" in PANELS_JS
    assert "_wireComposerControlChipDrag" in PANELS_JS
    assert "_moveComposerControlOrderKey" in PANELS_JS
    assert "_composerControlDropAllowed" in PANELS_JS
    assert "_renderComposerSituationalControlChips" in PANELS_JS
    assert "composerSituationalControlsChips" in PANELS_JS
    assert "#composerControlsChips .tab-visibility-chip" in STYLE_CSS
    assert "#composerSituationalControlsChips .tab-visibility-chip" in STYLE_CSS

    render_primary = _extract_function(PANELS_JS, "_renderComposerControlChips")
    render_situational = _extract_function(PANELS_JS, "_renderComposerSituationalControlChips")
    for body in (render_primary, render_situational):
        assert "_orderedComposerControlDefsForSettings" in body
        assert "_wireComposerControlChipDrag" in body

    move_body = _extract_function(PANELS_JS, "_moveComposerControlOrderKey")
    assert "_renderComposerControlChips()" in move_body
    assert "_renderComposerSituationalControlChips()" in move_body
    assert "_scheduleAppearanceAutosave()" in move_body


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

    assert "Drag chips to reorder each footer group." in INDEX_HTML
    assert "Reordering is not supported." not in INDEX_HTML
    assert "Reordering is not supported." not in I18N_JS
    assert "不支持重新排序" not in I18N_JS


def test_composer_control_order_is_validated_and_deduped():
    """The backend persists only known composer control order keys."""
    import api.config as config

    original_settings_file = config.SETTINGS_FILE
    test_state_dir = ROOT / ".tmp-test-issue4598"
    try:
        config.SETTINGS_FILE = test_state_dir / "settings.json"
        test_state_dir.mkdir(exist_ok=True)
        saved = config.save_settings(
            {
                "composer_control_order": [
                    "hide_composer_model",
                    "bogus",
                    "hide_composer_attach",
                    "hide_composer_model",
                    42,
                    "hide_composer_context",
                ]
            }
        )
        assert saved["composer_control_order"] == [
            "hide_composer_model",
            "hide_composer_attach",
            "hide_composer_context",
        ]
        persisted = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
        assert persisted["composer_control_order"] == saved["composer_control_order"]
    finally:
        config.SETTINGS_FILE = original_settings_file
        shutil.rmtree(test_state_dir, ignore_errors=True)


def test_composer_control_order_move_updates_both_chip_groups():
    """A same-footer-group drag saves order and re-renders both chip groups."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the frontend behavior harness")

    helpers = "\n".join(
        _extract_function(PANELS_JS, name)
        for name in [
            "_composerControlDefsForSettings",
            "_getComposerControlOrder",
            "_setComposerControlOrder",
            "_orderedComposerControlDefsForSettings",
            "_composerControlOrderGroupKey",
            "_composerControlDropAllowed",
            "_moveComposerControlOrderKey",
        ]
    )
    script = textwrap.dedent(
        f"""
        global.window = {{}};
        global.localStorage = {{
          _items: {{}},
          getItem(key) {{ return this._items[key] || null; }},
          setItem(key, value) {{ this._items[key] = String(value); }},
        }};
        const _COMPOSER_CONTROL_ORDER_LS_KEY = 'hermes-webui-composer-control-order';
        window._COMPOSER_CONTROL_TOGGLE_DEFS = [
          {{ key: 'hide_composer_attach', orderGroup: 'left' }},
          {{ key: 'hide_composer_context', orderGroup: 'right' }},
        ];
        window._COMPOSER_SITUATIONAL_CONTROL_TOGGLE_DEFS = [
          {{ key: 'hide_composer_voice_mode', orderGroup: 'left' }},
          {{ key: 'hide_composer_status', orderGroup: 'right' }},
        ];
        window._sanitizeComposerControlOrder = function(order) {{
          const allowed = new Set(window._COMPOSER_CONTROL_TOGGLE_DEFS.concat(window._COMPOSER_SITUATIONAL_CONTROL_TOGGLE_DEFS).map(def => def.key));
          const out = [];
          (Array.isArray(order) ? order : []).forEach(key => {{
            if (allowed.has(key) && !out.includes(key)) out.push(key);
          }});
          return out;
        }};
        window._applyComposerControlOrder = function(order) {{ window._appliedOrder = order.slice(); }};
        let primaryRenders = 0;
        let situationalRenders = 0;
        let autosaves = 0;
        function _renderComposerControlChips() {{ primaryRenders += 1; }}
        function _renderComposerSituationalControlChips() {{ situationalRenders += 1; }}
        function _scheduleAppearanceAutosave() {{ autosaves += 1; }}
        {helpers}
        if (!_moveComposerControlOrderKey('hide_composer_voice_mode', 'hide_composer_attach')) throw new Error('same-group move failed');
        if (JSON.stringify(window._composerControlOrder) !== JSON.stringify(['hide_composer_voice_mode','hide_composer_attach','hide_composer_context','hide_composer_status'])) throw new Error('unexpected order ' + JSON.stringify(window._composerControlOrder));
        if (primaryRenders !== 1 || situationalRenders !== 1 || autosaves !== 1) throw new Error('render/autosave counts wrong');
        if (_moveComposerControlOrderKey('hide_composer_status', 'hide_composer_attach')) throw new Error('cross-group move should be rejected');
        """
    )
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
