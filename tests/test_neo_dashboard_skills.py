"""Test HU-09 — Skills Neo embedded in dashboard shell.

Validates:
- panels.js: 'skills' in NEO_SHELL_PANELS
- dashboard.js: mountDashboardSkills / restoreDashboardSkills present
- style.css: shell skills layout rules
- index.html: required skills DOM elements intact
- i18n.js: skills section keys present in en and pt-BR
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

PANELS_JS    = (STATIC / "panels.js").read_text()
DASHBOARD_JS = (STATIC / "dashboard.js").read_text()
STYLE_CSS    = (STATIC / "style.css").read_text()
INDEX_HTML   = (STATIC / "index.html").read_text()


# ── panels.js ──────────────────────────────────────────────────────────────

def test_skills_in_neo_shell_panels():
    assert "'skills'" in PANELS_JS or '"skills"' in PANELS_JS
    assert "NEO_SHELL_PANELS" in PANELS_JS
    line = next(l for l in PANELS_JS.splitlines() if "NEO_SHELL_PANELS" in l and "new Set" in l)
    assert "skills" in line, "skills must be inside NEO_SHELL_PANELS Set literal"


def test_panels_js_calls_mount_restore_skills():
    assert "mountDashboardSkills" in PANELS_JS
    assert "restoreDashboardSkills" in PANELS_JS


# ── dashboard.js ───────────────────────────────────────────────────────────

def test_dashboard_js_syntax():
    import subprocess
    result = subprocess.run(
        ["node", "--check", str(STATIC / "dashboard.js")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"Syntax error: {result.stderr}"


def test_mount_dashboard_skills_defined():
    assert "function mountDashboardSkills()" in DASHBOARD_JS


def test_restore_dashboard_skills_defined():
    assert "function restoreDashboardSkills()" in DASHBOARD_JS


def test_skills_panel_anchor_used():
    assert "skillsPanelAnchor" in DASHBOARD_JS


def test_mount_moves_panel_skills_to_main_skills():
    assert "panelSkills" in DASHBOARD_JS
    assert "mainSkills" in DASHBOARD_JS
    assert "insertBefore" in DASHBOARD_JS


def test_restore_uses_insert_after():
    assert "_insertAfter(skillsPanelAnchor" in DASHBOARD_JS


# ── style.css ──────────────────────────────────────────────────────────────

def test_shell_skills_layout_flex_row():
    assert "dashboard-shell-mode main.main.showing-skills>#mainSkills{flex-direction:row" in STYLE_CSS


def test_shell_skills_panel_width():
    assert "dashboard-shell-mode main.main.showing-skills #mainSkills>#panelSkills{width:260px" in STYLE_CSS


def test_shell_skills_panel_border():
    assert "border-right:1px solid var(--border)" in STYLE_CSS


def test_shell_skills_list_scroll():
    assert "dashboard-shell-mode main.main.showing-skills #mainSkills .skills-list{flex:1;overflow-y:auto" in STYLE_CSS


def test_shell_skills_main_view_flex():
    assert "main-view-body" in STYLE_CSS
    assert "main-view-empty" in STYLE_CSS
    assert "flex:1;min-width:0" in STYLE_CSS


# ── index.html ─────────────────────────────────────────────────────────────

def test_skills_dom_elements_present():
    assert 'id="panelSkills"' in INDEX_HTML
    assert 'id="skillsList"' in INDEX_HTML
    assert 'id="skillsSearch"' in INDEX_HTML
    assert 'id="mainSkills"' in INDEX_HTML


def test_skills_detail_elements_present():
    for el_id in ("skillDetailTitle", "skillDetailBody", "skillDetailEmpty"):
        assert f'id="{el_id}"' in INDEX_HTML, f"Missing element: {el_id}"


def test_skills_action_buttons_present():
    assert 'id="btnEditSkillDetail"' in INDEX_HTML
    assert 'id="btnDeleteSkillDetail"' in INDEX_HTML


# ── i18n ───────────────────────────────────────────────────────────────────

def test_skills_i18n_keys_present():
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
    for key in ("tab_skills", "search_skills", "new_skill", "skills_empty_title", "skills_empty_sub"):
        assert key in data["enKeys"], f"Missing en key: {key}"
        assert key in data["ptKeys"], f"Missing pt-BR key: {key}"
