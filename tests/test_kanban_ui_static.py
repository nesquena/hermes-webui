from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
COMPACT_INDEX = re.sub(r"\s+", "", INDEX)
COMPACT_PANELS = re.sub(r"\s+", "", PANELS)
COMPACT_STYLE = re.sub(r"\s+", "", STYLE)


def test_kanban_has_native_sidebar_rail_and_mobile_tab():
    assert 'data-panel="kanban"' in INDEX
    assert 'data-i18n-title="tab_kanban"' in INDEX
    assert 'onclick="switchPanel(\'kanban\')"' in INDEX
    assert 'data-label="Kanban"' in INDEX
    kanban_section = INDEX[INDEX.find('id="mainKanban"'):INDEX.find('id="mainWorkspaces"')]
    assert "<iframe" not in kanban_section.lower()


def test_kanban_has_sidebar_panel_and_main_board_mounts():
    assert '<div class="panel-view" id="panelKanban">' in INDEX
    assert 'id="kanbanSearch"' in INDEX
    assert 'id="kanbanAssigneeFilter"' in INDEX
    assert 'id="kanbanTenantFilter"' in INDEX
    assert 'id="kanbanIncludeArchived"' in INDEX
    assert 'id="kanbanList"' in INDEX
    assert '<div id="mainKanban" class="main-view">' in INDEX
    assert 'id="kanbanBoard"' in INDEX
    assert 'id="kanbanTaskPreview"' in INDEX


def test_switch_panel_lazy_loads_kanban_and_toggles_main_view():
    assert "'kanban'" in re.search(r"\[[^\]]+\]\.forEach\(p => \{\s*mainEl\.classList", PANELS).group(0)
    assert "if (nextPanel === 'kanban') await loadKanban();" in PANELS
    assert "if (_currentPanel === 'kanban') await loadKanban();" in PANELS


def test_kanban_frontend_uses_relative_api_endpoints():
    assert "'/api/kanban/board" in PANELS
    assert "api('/api/kanban/tasks/" in PANELS
    assert "api('/api/kanban/config" in PANELS
    assert "fetch('/api/kanban" not in PANELS
    assert "kanbanTaskPreview" in PANELS
    assert "classList.add('selected')" in PANELS


def test_kanban_task_detail_renders_read_only_sections():
    assert "function _kanbanRenderTaskDetail" in PANELS
    for payload_key in ("data.comments", "data.events", "data.links", "data.runs"):
        assert payload_key in PANELS
    for section_class in (
        "kanban-detail-section",
        "kanban-detail-comments",
        "kanban-detail-events",
        "kanban-detail-links",
        "kanban-detail-runs",
    ):
        assert section_class in PANELS
    assert "method: 'POST'" not in PANELS[PANELS.find("async function loadKanbanTask"):PANELS.find("function loadTodos")]



def test_kanban_write_mvp_has_native_controls_and_api_calls():
    assert 'id="kanbanNewTaskBtn"' in INDEX
    assert "async function createKanbanTask" in PANELS
    assert "async function updateKanbanTask" in PANELS
    assert "async function addKanbanComment" in PANELS
    assert "api('/api/kanban/tasks'," in PANELS
    assert "method: 'POST'" in PANELS
    assert "'/api/kanban/tasks/' + encodeURIComponent(taskId) + '/patch'" in PANELS
    assert "'/api/kanban/tasks/' + encodeURIComponent(taskId) + '/comments'" in PANELS
    assert "kanban-status-actions" in PANELS
    assert "kanban-comment-form" in PANELS


def test_kanban_board_has_native_css_classes():
    for selector in (
        ".kanban-board",
        ".kanban-column",
        ".kanban-card",
        ".kanban-card-title",
        ".kanban-meta",
        ".kanban-readonly",
    ):
        assert selector in STYLE
    assert "overflow-x:auto" in COMPACT_STYLE


def test_kanban_i18n_keys_exist_in_every_locale_block():
    locale_blocks = re.findall(r"\n\s*([a-z]{2}(?:-[A-Z]{2})?): \{(.*?)\n\s*\},", I18N, flags=re.S)
    assert len(locale_blocks) >= 8
    required_keys = [
        "tab_kanban",
        "kanban_board",
        "kanban_search_tasks",
        "kanban_all_assignees",
        "kanban_all_tenants",
        "kanban_include_archived",
        "kanban_visible_tasks",
        "kanban_no_matching_tasks",
        "kanban_unavailable",
        "kanban_read_only",
        "kanban_empty",
        "kanban_comments_count",
        "kanban_events_count",
        "kanban_links",
        "kanban_runs_count",
        "kanban_no_comments",
        "kanban_no_events",
        "kanban_no_runs",
        "kanban_new_task",
        "kanban_add_comment",
    ]
    missing = [
        f"{locale}:{key}"
        for locale, body in locale_blocks
        for key in required_keys
        if re.search(rf"\b{re.escape(key)}\s*:", body) is None
    ]
    assert missing == []



def test_kanban_dashboard_parity_core_controls_are_native():
    assert 'id="kanbanOnlyMine"' in INDEX
    assert 'id="kanbanBulkBar"' in INDEX
    assert 'id="kanbanStats"' in INDEX
    assert "async function nudgeKanbanDispatcher" in PANELS
    assert "async function bulkUpdateKanban" in PANELS
    assert "async function refreshKanbanEvents" in PANELS
    for endpoint in (
        "'/api/kanban/stats'",
        "'/api/kanban/assignees'",
        "'/api/kanban/events'",
        "'/api/kanban/dispatch'",
        "'/api/kanban/tasks/bulk'",
        "'/api/kanban/tasks/' + encodeURIComponent(taskId) + '/log'",
        "'/api/kanban/tasks/' + encodeURIComponent(taskId) + '/block'",
        "'/api/kanban/tasks/' + encodeURIComponent(taskId) + '/unblock'",
    ):
        assert endpoint in PANELS
    assert "setInterval(refreshKanbanEvents" in PANELS
    assert "prompt(" not in PANELS
    assert "confirm(" not in PANELS


def test_kanban_dashboard_parity_i18n_keys_exist():
    locale_blocks = re.findall(r"\n\s*([a-z]{2}(?:-[A-Z]{2})?): \{(.*?)\n\s*\},", I18N, flags=re.S)
    required_keys = [
        "kanban_only_mine",
        "kanban_bulk_action",
        "kanban_nudge_dispatcher",
        "kanban_stats",
        "kanban_worker_log",
        "kanban_block",
        "kanban_unblock",
    ]
    missing = [
        f"{locale}:{key}"
        for locale, body in locale_blocks
        for key in required_keys
        if re.search(rf"\b{re.escape(key)}\s*:", body) is None
    ]
    assert missing == []



def test_kanban_ui_parity_polish_adds_card_metadata_quick_actions_and_swimlanes():
    for symbol in (
        "function _kanbanRenderProfileLanes",
        "function _kanbanCardQuickActions",
        "function quickKanbanCardAction",
        "function _kanbanRenderMarkdown",
        "function _kanbanCardStalenessClass",
        "function dragKanbanTask",
        "function dropKanbanTask",
    ):
        assert symbol in PANELS
    for token in (
        "kanban-profile-lanes",
        "kanban-card-topline",
        "kanban-card-actions",
        "kanban-card-id",
        "kanban-card-assignee",
        "draggable=\"true\"",
        "ondrop=\"dropKanbanTask",
        "onkeydown=\"if(event.key==='Enter'||event.key===' ')",
    ):
        assert token in PANELS
    assert "target=\"_blank\" rel=\"noopener noreferrer\"" in PANELS
    assert "javascript:" not in PANELS.lower()


def test_kanban_ui_parity_polish_css_and_i18n_exist():
    for selector in (
        ".kanban-profile-lanes",
        ".kanban-profile-lane",
        ".kanban-card-actions",
        ".kanban-card-action",
        ".kanban-card-topline",
        ".kanban-card-stale-amber",
        ".kanban-card-stale-red",
        ".kanban-column.drop-target",
        ".hermes-kanban-md",
    ):
        assert selector in STYLE
    locale_blocks = re.findall(r"\n\s*([a-z]{2}(?:-[A-Z]{2})?): \{(.*?)\n\s*\},", I18N, flags=re.S)
    required_keys = ["kanban_lanes_by_profile", "kanban_card_start", "kanban_card_complete", "kanban_card_archive", "kanban_unassigned"]
    missing = [
        f"{locale}:{key}"
        for locale, body in locale_blocks
        for key in required_keys
        if re.search(rf"\b{re.escape(key)}\s*:", body) is None
    ]
    assert missing == []
