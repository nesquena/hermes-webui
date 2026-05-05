"""Neo Sprint 5: static contract tests for the Projects Command Center frontend.

These tests are intentionally static — no servidor up — porque o objetivo é
prevenir regressões estruturais no DOM, no script wiring e no CSS Neo. As
rotas HTTP já são cobertas em ``tests/test_neo_projects_api.py``.
"""

import re
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO / "static" / "index.html"
KANBAN_JS = REPO / "static" / "kanban.js"
PANELS_JS = REPO / "static" / "panels.js"
STYLE_CSS = REPO / "static" / "style.css"
I18N_JS = REPO / "static" / "i18n.js"


# ── Helpers ─────────────────────────────────────────────────────────────────

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── kanban.js asset checks ──────────────────────────────────────────────────

def test_kanban_js_exists_and_compiles():
    assert KANBAN_JS.exists(), "static/kanban.js must exist for Sprint 5"
    out = subprocess.run(
        ["node", "--check", str(KANBAN_JS)],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, f"kanban.js syntax check failed:\n{out.stderr}"


def test_kanban_js_exposes_public_entry_point():
    src = _read(KANBAN_JS)
    assert "window.loadProjectsCommandCenter" in src, (
        "kanban.js must expose loadProjectsCommandCenter on window"
    )
    assert "fetchSnapshot" in src
    assert "/api/projects" in src
    assert "/api/project-tasks" in src


def test_kanban_js_implements_drag_and_drop():
    src = _read(KANBAN_JS)
    assert "_onDragStart" in src
    assert "_onDragEnd" in src
    assert "_bindDropTargets" in src
    # Optimistic UI with rollback
    assert "rollback" in src or "prevStatus" in src


# ── index.html DOM checks ───────────────────────────────────────────────────

def test_index_loads_kanban_js_before_panels_js():
    src = _read(INDEX_HTML)
    pos_kanban = src.find('static/kanban.js')
    pos_panels = src.find('static/panels.js')
    assert pos_kanban != -1, "kanban.js must be referenced from index.html"
    assert pos_panels != -1, "panels.js must be referenced from index.html"
    assert pos_kanban < pos_panels, (
        "kanban.js must be loaded BEFORE panels.js so switchPanel('projects') "
        "can call loadProjectsCommandCenter()"
    )


def test_main_projects_has_full_command_center_dom():
    src = _read(INDEX_HTML)
    # Header pieces
    assert 'id="mainProjects"' in src
    assert 'data-i18n="projects_title"' in src
    assert 'id="projectsNewProjectBtn"' in src
    # View toggle (Kanban / Lista)
    assert 'data-view="kanban"' in src
    assert 'data-view="list"' in src
    # Status pills with all four statuses + total
    for status in ("all", "backlog", "em_andamento", "em_revisao", "concluido"):
        assert f'data-status-filter="{status}"' in src, f"missing status pill {status}"
    # Filters popover (Sprint 5 polish replaces the old inline bar)
    assert 'id="projectsFiltersPopover"' in src
    assert 'id="projectsFilterText"' in src                # search input
    assert 'id="projectsFilterDimStatus"' in src
    assert 'id="projectsFilterDimProjects"' in src
    assert 'id="projectsFilterDimPriorities"' in src
    assert 'id="projectsFilterDimSources"' in src
    assert 'id="projectsFilterDimOwners"' in src
    assert 'id="projectsFilterDue"' in src
    assert 'id="projectsFiltersApply"' in src
    assert 'id="projectsFiltersClear"' in src
    # Kanban columns
    assert 'id="projectsKanban"' in src
    for status in ("backlog", "em_andamento", "em_revisao", "concluido"):
        assert f'data-drop-target="{status}"' in src, f"missing kanban drop target {status}"
        assert f'data-add-status="{status}"' in src, f"missing add-task button for {status}"
    # List view
    assert 'id="projectsList"' in src
    assert 'id="projectsListBody"' in src
    # Modais
    assert 'id="projectsProjectModal"' in src
    assert 'id="projectsProjectForm"' in src
    assert 'id="projectsTaskModal"' in src
    assert 'id="projectsTaskForm"' in src
    # Empty state
    assert 'id="projectsEmptyState"' in src


def test_index_does_not_keep_old_placeholder():
    src = _read(INDEX_HTML)
    # The Sprint 2 placeholder must be replaced by the full command center.
    assert "neo_projects_placeholder" not in src or "id=\"projectsKanban\"" in src, (
        "Old placeholder copy must not coexist outside the projects panel"
    )
    # Spot-check: placeholder div should be gone from #mainProjects.
    main_block = re.search(
        r'<div id="mainProjects"[^>]*>.*?</div>\s*<!-- NEO Sprint 5: Project create modal -->',
        src,
        re.DOTALL,
    )
    if main_block:
        assert "neo-placeholder-panel" not in main_block.group(0), (
            "neo-placeholder-panel must not remain inside #mainProjects"
        )


# ── panels.js wiring ────────────────────────────────────────────────────────

def test_panels_js_calls_load_projects_command_center():
    src = _read(PANELS_JS)
    pattern = re.compile(
        r"nextPanel\s*===\s*'projects'.*?loadProjectsCommandCenter\(\)",
        re.DOTALL,
    )
    assert pattern.search(src), (
        "switchPanel('projects') must call loadProjectsCommandCenter() when the "
        "function is defined"
    )


def test_panels_js_keeps_projects_in_neo_shell():
    src = _read(PANELS_JS)
    assert "NEO_SHELL_PANELS" in src
    # 'projects' precisa estar no Set para herdar dashboard-shell-mode
    shell_match = re.search(r"NEO_SHELL_PANELS\s*=\s*new\s*Set\(\[(.*?)\]\)", src, re.DOTALL)
    assert shell_match, "NEO_SHELL_PANELS Set must exist"
    assert "'projects'" in shell_match.group(1), (
        "'projects' must remain in NEO_SHELL_PANELS so the dashboard shell renders"
    )


# ── style.css Neo skin checks ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "selector",
    [
        ".projects-view-toggle",
        ".projects-status-pills",
        ".projects-pill",
        ".projects-filters",
        ".projects-kanban",
        ".kanban-column",
        ".kanban-card",
        ".kanban-card-dragging",
        ".kanban-column-list.drop-active",
        ".projects-list",
        ".projects-list-table",
        ".projects-modal",
        ".projects-modal-card",
        ".projects-modal-form",
    ],
)
def test_style_has_projects_command_center_selectors(selector):
    src = _read(STYLE_CSS)
    assert selector in src, f"style.css is missing Sprint 5 selector: {selector}"


def test_style_uses_neo_tokens_not_hardcoded_colors():
    """Sprint 5 styling must lean on the Neo design tokens (var(--accent),
    var(--surface), etc.) so light/dark and skin switching keep working."""
    src = _read(STYLE_CSS)
    # Locate the Sprint 5 block and count token usages.
    block_start = src.find("NEO Sprint 5 — Projects Command Center")
    assert block_start != -1, "Sprint 5 CSS section header not found"
    block = src[block_start:]
    for token in ("var(--accent)", "var(--surface)", "var(--border)", "var(--text)", "var(--muted)"):
        assert token in block, f"Sprint 5 CSS must reference {token}"


# ── Kanban data-status border colors (slate / amber / blue / green) ────────

def test_style_paints_each_kanban_column_with_distinct_color():
    src = _read(STYLE_CSS)
    block = src[src.find("NEO Sprint 5 — Projects Command Center"):]
    for status, color_hex in [
        ("backlog", "#94A3B8"),       # slate
        ("em_andamento", "#F59E0B"),  # amber
        ("em_revisao", "#3B82F6"),    # blue
        ("concluido", "#22C55E"),     # green
    ]:
        rule = f'.kanban-column[data-status="{status}"]'
        assert rule in block, f"missing rule {rule}"
        # The next 80 chars after the rule should mention the expected color
        idx = block.find(rule)
        assert color_hex.lower() in block[idx:idx + 200].lower(), (
            f"kanban column {status} must use accent {color_hex}"
        )


# ── i18n coverage ──────────────────────────────────────────────────────────

REQUIRED_PROJECTS_KEYS = [
    "projects_title",
    "projects_subtitle",
    "projects_kanban",
    "projects_list",
    "projects_filters",
    "projects_new",
    "projects_total",
    "projects_col_backlog",
    "projects_col_in_progress",
    "projects_col_in_review",
    "projects_col_completed",
    "projects_priority_low",
    "projects_priority_medium",
    "projects_priority_high",
    "projects_create_title",
    "projects_create_name",
    "projects_create_color",
    "projects_create_description",
    "projects_create_default_source",
    "projects_create_submit",
    "projects_task_create_title",
    "projects_task_edit_title",
    "projects_task_save",
    "projects_task_external_ref",
    "projects_task_ref_none",
    "projects_filter_all_projects",
    "projects_filter_all_priorities",
    "projects_filter_all_sources",
    "projects_filter_all_owners",
    "projects_filter_text_placeholder",
    "projects_empty_title",
    "projects_empty_sub",
    "projects_source_local",
    "projects_unassigned",
    "projects_need_project_first",
    "projects_error_load",
    "projects_error_save",
    "projects_error_name_required",
    "projects_error_title_required",
    "projects_error_project_required",
    "projects_toast_project_created",
    "projects_toast_task_created",
    "projects_toast_task_saved",
    "projects_list_id",
    "projects_list_task",
    "projects_list_project",
    "projects_list_priority",
    "projects_list_owner",
    "projects_list_status",
    "projects_list_source",
    "projects_list_per_page",
    "projects_list_pager_summary",
    "projects_list_pager_empty",
    "projects_filter_search_placeholder",
    "projects_filter_dim_status",
    "projects_filter_dim_projects",
    "projects_filter_dim_priorities",
    "projects_filter_dim_sources",
    "projects_filter_dim_owners",
    "projects_filter_due_label",
    "projects_filter_due_any",
    "projects_filter_due_overdue",
    "projects_filter_due_week",
    "projects_filter_due_none",
    "projects_filters_clear_all",
    "projects_filters_apply",
]


@pytest.mark.parametrize("key", REQUIRED_PROJECTS_KEYS)
def test_i18n_has_projects_key_in_en(key):
    src = _read(I18N_JS)
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:\s*['\"]", re.MULTILINE)
    assert pattern.search(src), f"i18n.js missing key {key}"

# ── Sort & pagination (Neo Sprint 5 list view) ─────────────────────────────

def test_index_has_sortable_headers_and_pager():
    """List view must expose sortable column headers and a footer pager."""
    src = _read(INDEX_HTML)
    # Each list column header carries data-sort-key + sortable class + a11y.
    for key in ("task_id", "title", "project", "priority", "owner", "status", "source"):
        assert f'data-sort-key="{key}"' in src, f"missing sortable header for {key}"
    # Sortable hook + a11y
    assert "projects-list-sortable" in src
    assert 'aria-sort="none"' in src
    # Pager footer
    assert 'id="projectsListPager"' in src
    assert 'id="projectsListPagerPrev"' in src
    assert 'id="projectsListPagerNext"' in src
    assert 'id="projectsListPagerPage"' in src
    assert 'id="projectsListPageSize"' in src
    assert 'id="projectsListPagerSummary"' in src


def test_kanban_js_implements_sort_and_pagination():
    src = _read(KANBAN_JS)
    # Sort plumbing
    assert "_sortValue" in src
    assert "_compareTasks" in src
    assert "STATUS_RANK" in src
    assert "PRIORITY_RANK" in src
    assert "_renderListSortIndicators" in src
    assert "_onListSortClick" in src
    # Pagination plumbing
    assert "PAGE_SIZE_OPTIONS" in src
    assert "_renderListPager" in src
    assert "_onListPageSizeChange" in src
    assert "_onListPagerPrev" in src
    assert "_onListPagerNext" in src
    # Persisted prefs (sortKey/sortDir/pageSize, but NOT page index)
    assert "LIST_PREFS_KEY" in src
    assert "_persistListPrefs" in src
    # Filter changes must reset page to 1 to avoid empty paginated views.
    assert "state.list.page = 1" in src


@pytest.mark.parametrize(
    "selector",
    [
        ".projects-list-sortable",
        ".projects-list-sort-indicator",
        ".projects-list-pager",
        ".projects-list-pager-info",
        ".projects-list-pager-controls",
        ".projects-list-pager-size",
        ".projects-list-pager-nav",
        ".projects-list-pager-btn",
        ".projects-list-pager-page",
    ],
)
def test_style_has_list_sort_and_pager_selectors(selector):
    src = _read(STYLE_CSS)
    assert selector in src, f"style.css is missing list selector: {selector}"

# ── Sprint 5 polish: popover filters + read-only pills + full-height layout ─

def test_filters_popover_dom_structure():
    src = _read(INDEX_HTML)
    # Anchor wraps the trigger button
    assert 'projects-filters-anchor' in src
    assert 'aria-haspopup="dialog"' in src
    # Popover with a11y role + close + apply + clear-all
    assert 'role="dialog"' in src
    assert 'projects-filters-popover-header' in src
    assert 'projects-filters-popover-tools' in src
    assert 'projects-filters-search' in src
    assert 'projects-filters-grid' in src
    assert 'projects-filters-popover-footer' in src
    # Each dimension is declared with data-filter-dim
    for dim in ('status', 'project_id', 'priority', 'source', 'owner', 'due'):
        assert f'data-filter-dim="{dim}"' in src, f"missing dim {dim}"


def test_old_inline_filters_bar_is_removed():
    src = _read(INDEX_HTML)
    assert 'id="projectsFiltersBar"' not in src, (
        "Inline filters bar should be replaced by the popover"
    )
    assert 'id="projectsFilterProject"' not in src
    assert 'id="projectsFilterPriority"' not in src
    assert 'id="projectsFilterSource"' not in src
    assert 'id="projectsFilterOwner"' not in src


def test_kanban_js_uses_set_based_multi_select_filters():
    src = _read(KANBAN_JS)
    assert "new Set()" in src, "filters dimensions must be Sets"
    # State filter slots
    for dim in ("status", "project_id", "priority", "source", "owner"):
        # Either initialized as Set or used as Set later
        assert f"f.{dim}.size" in src or f"state.filters.{dim}" in src
    assert "due:" in src
    assert "task.external_ref" in src
    assert "task.due_date" in src
    # Helpers exist
    assert "_renderFilterDimensions" in src
    assert "_onFilterCheckboxChange" in src
    assert "_clearAllFilters" in src
    assert "_toggleFiltersPopover" in src
    assert "_activeFilterCount" in src


def test_kanban_js_pills_drive_status_filter_state():
    """Status pills are a shortcut filter as specified by Sprint 5."""
    src = _read(KANBAN_JS)
    assert "btn.dataset.statusFilter" in src
    assert "_onStatusPillClick" in src
    assert "state.filters.status" in src


@pytest.mark.parametrize(
    "selector",
    [
        ".projects-filters-anchor",
        ".projects-filters-popover",
        ".projects-filters-popover-header",
        ".projects-filters-popover-title",
        ".projects-filters-popover-tools",
        ".projects-filters-clear",
        ".projects-filters-popover-close",
        ".projects-filters-search",
        ".projects-filters-grid",
        ".projects-filters-dim",
        ".projects-filters-options",
        ".projects-filters-option",
        ".projects-filters-popover-footer",
        ".projects-filters-apply",
        ".projects-filter-btn-badge",
    ],
)
def test_style_has_popover_selectors(selector):
    src = _read(STYLE_CSS)
    assert selector in src, f"style.css is missing popover selector: {selector}"


def test_style_makes_pills_interactive():
    """Sprint 5 status pills must be clickable filter shortcuts."""
    src = _read(STYLE_CSS)
    idx = src.find(".projects-pill{")
    assert idx > 0
    block = src[idx:idx + 600]
    assert "cursor:pointer" in block
    assert "pointer-events:none" not in block
    assert ".projects-pill.active" in src


def test_sprint5_has_hu04_evidence_docs():
    """Sprint 5 docs should record implementation evidence for delivered HU-04 scope."""
    for hu in ("HU-04.1", "HU-04.2", "HU-04.3", "HU-04.4", "HU-04.5", "HU-04.6", "HU-04.7", "HU-04.8"):
        path = REPO / "docs" / "neo" / "evidencias" / hu / "README.md"
        assert path.exists(), f"missing evidence doc for {hu}"


def test_style_makes_projects_panel_full_height():
    src = _read(STYLE_CSS)
    # The shell rule that gives #mainProjects flex:1/min-height:0/height:100%
    assert "main.main.showing-projects > #mainProjects" in src
    # Body, kanban and column-list should support inner scroll
    for needle in (
        ".projects-body",
        "min-height:0",
        ".kanban-column-list",
        "overflow-y:auto",
    ):
        assert needle in src, f"missing layout token: {needle}"


def test_views_are_mutually_exclusive_on_render():
    """Both Kanban and List exist in the DOM; CSS hides the inactive one and
    JS renders only the active branch in renderAll()."""
    src_css = _read(STYLE_CSS)
    assert ".projects-kanban[hidden]" in src_css
    assert ".projects-list[hidden]" in src_css
    src_js = _read(KANBAN_JS)
    assert "if (state.view === 'kanban') renderKanban();" in src_js
    assert "else renderList();" in src_js

# ── HU-04.9 / 04.10: archive + refs (Sprint 5 P1) ──────────────────────────

@pytest.mark.parametrize(
    "key",
    [
        "projects_archive",
        "projects_unarchive",
        "projects_archived",
        "projects_archive_project",
        "projects_unarchive_project",
        "projects_show_archived",
        "projects_save",
        "projects_edit_title",
        "projects_task_links",
        "projects_task_refs_sessions",
        "projects_task_refs_github",
        "projects_task_refs_obsidian",
        "projects_task_refs_empty",
        "projects_toast_project_saved",
        "projects_toast_project_archived",
        "projects_toast_task_archived",
    ],
)
def test_i18n_has_archive_keys_in_en(key):
    src = _read(I18N_JS)
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:\s*[\'\"]", re.MULTILINE)
    assert pattern.search(src), f"i18n.js missing key {key}"


def test_index_has_archive_toggle_and_buttons():
    src = _read(INDEX_HTML)
    # Show-archived toggle inside filters popover
    assert 'id="projectsFilterShowArchived"' in src
    # Project archive button on the project edit modal
    assert 'id="projectsProjectArchiveBtn"' in src
    # Task archive button on the task edit modal
    assert 'id="projectsTaskArchiveBtn"' in src
    # Hidden project_id in the project form (signals edit vs create)
    assert 'name="project_id"' in src
    # Refs fieldset for HU-04.9
    assert 'id="projectsTaskRefsFieldset"' in src
    for group in ("sessions", "github", "obsidian"):
        assert f'data-ref-group="{group}"' in src
        assert f'data-ref-list="{group}"' in src


def test_kanban_js_supports_archive_and_refs():
    src = _read(KANBAN_JS)
    # Archive plumbing
    assert "_onArchiveProjectClick" in src
    assert "_onArchiveTaskClick" in src
    assert "showArchived" in src
    assert "include_archived=1" in src
    # Refs plumbing
    assert "_renderTaskRefs" in src
    # Project edit support
    assert "/api/projects/${encodeURIComponent(projectId)}" in src or            "PATCH" in src  # PATCH route for project
    # Card archive visual state
    assert "kanban-card-archived" in src
    assert "chip-archived" in src


def test_api_projects_route_supports_include_archived_query():
    """Backend route honors ?include_archived=1 (HU-04.10)."""
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    assert "include_archived" in src, (
        "GET /api/projects must accept ?include_archived=1 to power "
        "the \"Mostrar arquivados\" toggle"
    )


def test_neo_projects_module_snapshot_has_include_archived_kwarg():
    src = (REPO / "api" / "projects.py").read_text(encoding="utf-8")
    assert "def snapshot(*, include_archived" in src, (
        "neo_projects.snapshot() must expose include_archived keyword"
    )


@pytest.mark.parametrize(
    "selector",
    [
        ".kanban-card-archived",
        ".chip-archived",
        ".projects-task-refs",
        ".projects-task-refs-chip",
        ".projects-modal-btn.danger",
        ".projects-filters-archived-toggle",
    ],
)
def test_style_has_archive_and_refs_selectors(selector):
    src = _read(STYLE_CSS)
    assert selector in src, f"style.css is missing selector: {selector}"
