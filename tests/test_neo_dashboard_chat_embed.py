"""Neo HU-03.5: Dashboard embeds the real chat/composer surface."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
DASHBOARD_JS = (ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
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


def test_dashboard_focuses_real_composer_after_panel_mount():
    assert "async function focusDashboardComposer" in DASHBOARD_JS
    assert "await switchPanel('dashboard')" in DASHBOARD_JS
    assert "document.getElementById('msg')" in DASHBOARD_JS
    assert ".focus()" in DASHBOARD_JS


def test_direct_chat_panel_restore_contract_is_kept():
    assert 'id="panelChat"' in INDEX_HTML
    assert 'id="mainChat"' in INDEX_HTML
    assert "const prevPanel = _currentPanel" in PANELS_JS
    assert "nextPanel !== 'dashboard'" in PANELS_JS
    assert "restoreDashboardChat()" in PANELS_JS
    assert "main.main:not(.showing-dashboard)" in STYLE_CSS


def test_dashboard_reuses_full_composer_controls():
    for control_id in [
        'id="fileInput"',
        'id="btnAttach"',
        'id="btnMic"',
        'id="profileChip"',
        'id="composerWorkspaceChip"',
        'id="composerModelChip"',
        'id="modelSelect"',
        'id="composerReasoningWrap"',
        'id="composerReasoningChip"',
        'id="btnSend"',
    ]:
        assert control_id in INDEX_HTML

    for dashboard_copy in [
        'id="dashboardFileInput"',
        'id="dashboardBtnAttach"',
        'id="dashboardProfileChip"',
        'id="dashboardComposerModelChip"',
        'id="dashboardBtnSend"',
    ]:
        assert dashboard_copy not in INDEX_HTML


def test_reused_composer_controls_keep_original_handlers():
    assert "$('modelSelect').onchange" in BOOT_JS
    assert "function toggleModelDropdown()" in UI_JS
    assert "function toggleReasoningDropdown()" in UI_JS
    assert "function toggleComposerWsDropdown()" in PANELS_JS
    assert "function toggleProfileDropdown()" in PANELS_JS


def test_reused_attachment_and_send_flow_keep_original_handlers():
    assert "$('btnAttach').onclick=()=>$('fileInput').click();" in BOOT_JS
    assert "$('fileInput').onchange" in BOOT_JS
    assert "addFiles(Array.from(e.target.files))" in BOOT_JS
    assert "$('btnSend').onclick" in BOOT_JS
    assert "handleComposerPrimaryAction" in BOOT_JS
    assert "async function send()" in MESSAGES_JS
    assert "uploadPendingFiles()" in MESSAGES_JS


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


def test_dashboard_chat_scroll_container_can_shrink_inside_grid():
    """Regression guard: the embedded chat must scroll, not clip in dashboard."""

    for selector in [
        ".dashboard-shell",
        ".dashboard-center",
        ".dashboard-chat-panel",
        ".dashboard-chat-body",
        ".dashboard-chat-body .messages",
    ]:
        match = re.search(rf"{re.escape(selector)}\{{([^}}]+)\}}", STYLE_CSS)
        assert match, f"{selector} CSS block missing"
        assert "min-height:0" in match.group(1), (
            f"{selector} must allow nested flex/grid children to shrink so "
            "the embedded #messages element can own vertical scrolling"
        )


def test_dashboard_composer_has_responsive_hardening():
    for selector in [
        ".dashboard-chat-composer .composer-footer",
        ".dashboard-chat-composer .composer-left",
        ".dashboard-chat-composer .composer-right",
        ".dashboard-chat-composer .composer-profile-chip",
        ".dashboard-chat-composer .composer-workspace-chip",
        ".dashboard-chat-composer .composer-model-chip",
        ".dashboard-chat-composer .composer-reasoning-chip",
        ".dashboard-chat-composer .composer-terminal-panel",
        "@media(max-width:1100px)",
        "@media(max-width:760px)",
    ]:
        assert selector in STYLE_CSS

    assert "overflow-x:auto" in STYLE_CSS
    assert "flex-wrap:wrap" in STYLE_CSS


def test_hu_03_5_task_tracking_started():
    assert "### HU-03.5" in TASKS_MD
    assert "**Status:** em andamento" in TASKS_MD
    assert "- [x] Embutir o mesmo SSE da sessão ativa." in TASKS_MD
    assert "- [x] Reutilizar o composer/toolstrip completo upstream; não criar um segundo composer paralelo em `dashboard.js`." in TASKS_MD
    assert "- [x] Manter painel `chat` direto funcional." in TASKS_MD
    assert "- [x] Focar composer ao abrir Dashboard." in TASKS_MD
    assert "- [x] Testar troca de modelo, workspace, profile e effort dentro do Dashboard." in TASKS_MD
    assert "- [x] Testar envio com anexo dentro do Dashboard." in TASKS_MD
    assert "- [x] Validar mobile/tablet: toolstrip pode quebrar linha, mas não pode ocultar controles, cortar labels ou sobrepor elementos." in TASKS_MD
