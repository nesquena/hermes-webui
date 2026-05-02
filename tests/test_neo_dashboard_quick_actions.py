"""Neo HU-03.7: Dashboard quick actions grid."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
DASHBOARD_JS = (ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
TASKS_MD = (ROOT / "docs" / "neo" / "TASKS.md").read_text(encoding="utf-8")


def test_dashboard_quick_actions_grid_present():
    assert 'class="dashboard-quick-actions"' in INDEX_HTML
    assert 'id="dashboardQuickActions"' in INDEX_HTML
    for action in [
        "new_project",
        "new_document",
        "new_component",
        "open_terminal",
        "generate_report",
        "deploy_project",
    ]:
        assert f'data-dashboard-action="{action}"' in INDEX_HTML


def test_dashboard_quick_actions_frontend_behavior_present():
    assert "handleDashboardQuickAction" in DASHBOARD_JS
    assert "DASHBOARD_QUICK_ACTION_PROMPTS" in DASHBOARD_JS
    assert "toggleComposerTerminal(true)" in DASHBOARD_JS
    assert "dashboard_action_placeholder" in DASHBOARD_JS
    assert "focusDashboardComposer" in DASHBOARD_JS


def test_dashboard_quick_actions_css_and_i18n_present():
    for selector in [
        ".dashboard-quick-actions",
        ".dashboard-quick-actions h3",
        ".dashboard-quick-action-grid",
        ".dashboard-quick-action",
        ".dashboard-quick-action-icon",
    ]:
        assert selector in STYLE_CSS
    for key in [
        "dashboard_quick_actions",
        "dashboard_action_placeholder",
        "action_new_project",
        "action_new_document",
        "action_new_component",
        "action_open_terminal",
        "action_generate_report",
        "action_deploy_project",
    ]:
        assert key in I18N_JS


def test_hu_03_7_task_tracking_updated():
    section = TASKS_MD.split("### HU-03.7", 1)[1].split("### HU-03.8", 1)[0]
    assert "**Status:** implementada com testes" in section
    assert "- [x] Renderizar Novo Projeto." in section
    assert "- [x] Definir comportamento de placeholders sem backend." in section
