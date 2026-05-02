"""Neo HU-03.5: Dashboard embeds the real chat/composer surface."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
DASHBOARD_JS = (ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
TASKS_MD = (ROOT / "docs" / "neo" / "TASKS.md").read_text(encoding="utf-8")


def test_dashboard_has_chat_slot_without_duplicate_composer():
    assert 'id="dashboardChatPanel"' in INDEX_HTML
    assert 'id="dashboardChatMessagesSlot"' in INDEX_HTML
    assert 'id="dashboardChatComposerSlot"' in INDEX_HTML
    assert INDEX_HTML.count('id="msg"') == 1
    assert INDEX_HTML.count('id="composerWrap"') == 1
    assert INDEX_HTML.count('id="messages"') == 1


def test_dashboard_moves_existing_chat_dom_and_restores_it():
    assert "mountDashboardChat" in DASHBOARD_JS
    assert "restoreDashboardChat" in DASHBOARD_JS
    assert "dashboardChatMessagesSlot" in DASHBOARD_JS
    assert "dashboardChatComposerSlot" in DASHBOARD_JS
    assert "chatMessagesAnchor" in DASHBOARD_JS
    assert "chatComposerAnchor" in DASHBOARD_JS
    assert "appendChild(messages)" in DASHBOARD_JS
    assert "appendChild(composer)" in DASHBOARD_JS
    assert "restoreDashboardChat" in PANELS_JS


def test_dashboard_chat_visual_shell_css_present():
    for selector in [
        ".dashboard-chat-panel",
        ".dashboard-chat-header",
        ".dashboard-chat-title",
        ".dashboard-chat-online",
        ".dashboard-chat-body",
        ".dashboard-chat-composer",
    ]:
        assert selector in STYLE_CSS


def test_hu_03_5_task_tracking_started():
    assert "### HU-03.5" in TASKS_MD
    assert "**Status:** em andamento" in TASKS_MD
    assert "- [x] Embutir o mesmo SSE da sessão ativa." in TASKS_MD
    assert "- [x] Reutilizar o composer/toolstrip completo upstream; não criar um segundo composer paralelo em `dashboard.js`." in TASKS_MD
