"""Test HU-08 — Settings Neo embedded in dashboard shell.

Validates:
- panels.js: 'settings' in NEO_SHELL_PANELS
- dashboard.js: mountDashboardSettings / restoreDashboardSettings present
- style.css: shell settings layout rules
- index.html: required settings DOM elements intact
- i18n.js: settings section keys present in en and pt-BR
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

PANELS_JS   = (STATIC / "panels.js").read_text()
DASHBOARD_JS = (STATIC / "dashboard.js").read_text()
STYLE_CSS   = (STATIC / "style.css").read_text()
INDEX_HTML  = (STATIC / "index.html").read_text()


# ── panels.js ──────────────────────────────────────────────────────────────

def test_settings_in_neo_shell_panels():
    assert "'settings'" in PANELS_JS or '"settings"' in PANELS_JS
    assert "NEO_SHELL_PANELS" in PANELS_JS
    line = next(l for l in PANELS_JS.splitlines() if "NEO_SHELL_PANELS" in l and "new Set" in l)
    assert "settings" in line, "settings must be inside NEO_SHELL_PANELS Set literal"


def test_panels_js_calls_mount_restore_settings():
    assert "mountDashboardSettings" in PANELS_JS
    assert "restoreDashboardSettings" in PANELS_JS


# ── dashboard.js ───────────────────────────────────────────────────────────

def test_dashboard_js_syntax():
    import subprocess
    result = subprocess.run(
        ["node", "--check", str(STATIC / "dashboard.js")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"Syntax error: {result.stderr}"


def test_mount_dashboard_settings_defined():
    assert "function mountDashboardSettings()" in DASHBOARD_JS


def test_restore_dashboard_settings_defined():
    assert "function restoreDashboardSettings()" in DASHBOARD_JS


def test_settings_menu_anchor_used():
    assert "settingsMenuAnchor" in DASHBOARD_JS


def test_mount_moves_settings_menu_to_main_settings():
    assert "settingsMenu" in DASHBOARD_JS
    assert "mainSettings" in DASHBOARD_JS
    assert "insertBefore" in DASHBOARD_JS


def test_restore_uses_insert_after():
    assert "_insertAfter(settingsMenuAnchor" in DASHBOARD_JS


# ── style.css ──────────────────────────────────────────────────────────────

def test_shell_settings_layout_flex_row():
    assert "dashboard-shell-mode main.main.showing-settings>#mainSettings{flex-direction:row" in STYLE_CSS


def test_shell_settings_menu_width():
    assert "dashboard-shell-mode main.main.showing-settings #mainSettings>#settingsMenu{width:190px" in STYLE_CSS


def test_shell_settings_menu_kicker():
    assert "CONFIGURAÇÕES" in STYLE_CSS
    assert "settingsMenu::before" in STYLE_CSS


def test_shell_settings_main_flex():
    assert "dashboard-shell-mode main.main.showing-settings #mainSettings>.settings-main{flex:1" in STYLE_CSS


# ── index.html ─────────────────────────────────────────────────────────────

def test_settings_dom_elements_present():
    assert 'id="panelSettings"' in INDEX_HTML
    assert 'id="settingsMenu"' in INDEX_HTML
    assert 'id="mainSettings"' in INDEX_HTML


def test_settings_panes_present():
    for pane_id in (
        "settingsPaneConversation",
        "settingsPaneAppearance",
        "settingsPanePreferences",
        "settingsPaneProviders",
        "settingsPaneSystem",
    ):
        assert f'id="{pane_id}"' in INDEX_HTML, f"Missing pane: {pane_id}"


def test_settings_section_buttons_present():
    for section in ("conversation", "appearance", "preferences", "providers", "system"):
        assert f'data-settings-section="{section}"' in INDEX_HTML


# ── i18n ───────────────────────────────────────────────────────────────────

def test_settings_i18n_keys_present():
    import json, subprocess, textwrap
    i18n_path = STATIC / "i18n.js"
    script = textwrap.dedent(f"""
        const fs = require('fs');
        const vm = require('vm');
        const src = fs.readFileSync({json.dumps(str(i18n_path))}, 'utf8');
        const ctx = {{
          localStorage: {{ getItem: () => null, setItem: () => {{}} }},
          document: {{ documentElement: {{ lang: '' }}, querySelectorAll: () => [] }},
        }};
        vm.createContext(ctx);
        vm.runInContext(src, ctx);
        const out = vm.runInContext(`(() => {{
          const en = LOCALES.en || {{}};
          const pt = LOCALES['pt-BR'] || {{}};
          return {{ enKeys: Object.keys(en), ptKeys: Object.keys(pt) }};
        }})()`, ctx);
        process.stdout.write(JSON.stringify(out));
    """)
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    data = json.loads(proc.stdout)
    for key in ("tab_settings", "settings_section_appearance_title", "settings_section_conversation_title"):
        assert key in data["enKeys"], f"Missing en key: {key}"
        assert key in data["ptKeys"], f"Missing pt-BR key: {key}"
