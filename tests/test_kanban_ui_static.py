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


def _locale_blocks_with_body(i18n_text: str):
    locale_blocks = re.findall(
        r"\n\s*(?:'(?P<quoted>[a-z]{2}(?:-[A-Z][A-Za-z]+)?)'|(?P<plain>[a-z]{2}(?:-[A-Z]{2})?))\s*:\s*\{(.*?)\n\s*\},",
        i18n_text,
        flags=re.S,
    )
    return [(quoted or plain, body) for quoted, plain, body in locale_blocks]


def test_kanban_has_native_sidebar_rail_and_mobile_tab():
    assert 'data-panel="kanban"' in INDEX
    assert 'data-i18n-title="tab_kanban"' in INDEX
    # Allow either the legacy `switchPanel('kanban')` form or the rail-click-aware
    # `switchPanel('kanban',{fromRailClick:true})` form. The sidebar-collapse PR
    # added the second-arg opts to all rail buttons so the same-active-icon click
    # can toggle the sidebar; legacy callsites elsewhere may still use the bare form.
    assert ('onclick="switchPanel(\'kanban\')"' in INDEX
            or "onclick=\"switchPanel('kanban',{fromRailClick:true})\"" in INDEX), \
        "kanban rail/mobile button must call switchPanel('kanban') (with or without fromRailClick opts)"
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
    # The exact tail varies because the multi-board PR appends
    # _kanbanBoardQuery() to most kanban API URLs. Match with looser
    # substring assertions that survive that suffix.
    assert "api('/api/kanban/tasks'" in PANELS
    assert "method: 'POST'" in PANELS
    assert "'/api/kanban/tasks/' + encodeURIComponent(taskId)" in PANELS
    assert "method: 'PATCH'" in PANELS
    assert "'/api/kanban/tasks/' + encodeURIComponent(taskId) + '/comments'" in PANELS
    assert "kanban-status-actions" in PANELS
    assert "kanban-comment-form" in PANELS


def test_kanban_new_task_header_button_opens_modal():
    """Regression: the panel-head '+' button must open a real `.kanban-modal-overlay`
    create-task modal (matching the existing create-board modal pattern in the same
    file) — NOT silently return when the inline #kanbanNewTaskTitle input is empty.

    Previously the header button was wired straight to createKanbanTask(), which
    silently early-exits on empty title — the button looked completely dead.
    Now the header button calls openKanbanCreate(), which opens the
    #kanbanTaskModal overlay with title / description / status / priority /
    assignee / tenant fields.
    """
    # 1. Header "+" button is wired to openKanbanCreate(), NOT createKanbanTask().
    assert 'id="kanbanNewTaskBtn"' in INDEX
    btn_html = INDEX[INDEX.find('id="kanbanNewTaskBtn"'):]
    btn_html = btn_html[: btn_html.find("</button>") + len("</button>")]
    assert 'onclick="openKanbanCreate()"' in btn_html, (
        "Panel-head '+' button must call openKanbanCreate() (modal), not "
        "createKanbanTask() directly (which silently returns on empty title)."
    )

    # 2. The create-task modal markup exists in index.html, with all the field
    #    ids the JS / API contract expects.
    assert 'id="kanbanTaskModal"' in INDEX
    assert 'class="kanban-modal-overlay"' in INDEX[INDEX.find('id="kanbanTaskModal"') - 80:]
    for field_id in (
        "kanbanTaskModalTitleInput",
        "kanbanTaskModalBody",
        "kanbanTaskModalStatus",
        "kanbanTaskModalPriority",
        "kanbanTaskModalAssignee",
        "kanbanTaskModalTenant",
        "kanbanTaskModalError",
        "kanbanTaskModalSubmit",
    ):
        assert f'id="{field_id}"' in INDEX, f"create-task modal missing #{field_id}"

    # 3. Modal closes via Cancel button AND backdrop click AND ESC.
    assert 'onclick="closeKanbanTaskModal()"' in INDEX
    assert "if(event.target===this)closeKanbanTaskModal()" in INDEX

    # 4. openKanbanCreate() unhides the modal, focuses the title field, populates
    #    assignee/tenant datalists, binds keydown listener.
    assert "function openKanbanCreate()" in PANELS
    open_fn = re.search(
        r"function openKanbanCreate\(\)\{(.*?)\n\}", PANELS, re.DOTALL
    )
    assert open_fn, "openKanbanCreate() not found"
    body = open_fn.group(1)
    assert "modal.hidden = false" in body
    # Assignee is now a <select> populated from /api/profiles + board history,
    # tenant is still a free-text <input> backed by a datalist.
    assert "_kanbanPopulateAssigneeSelect" in body, (
        "openKanbanCreate must populate the assignee <select> from /api/profiles."
    )
    assert "_kanbanPopulateTenantDatalist" in body
    assert "_kanbanTaskModalKey" in body  # ESC + Enter handler attached

    # 5. closeKanbanTaskModal() hides the modal and unbinds the listener.
    assert "function closeKanbanTaskModal()" in PANELS
    close_fn = re.search(
        r"function closeKanbanTaskModal\(\)\{(.*?)\n\}", PANELS, re.DOTALL
    )
    assert close_fn and "modal.hidden = true" in close_fn.group(1)
    assert "removeEventListener('keydown', _kanbanTaskModalKey)" in close_fn.group(1)

    # 6. ESC closes; Enter submits (except in the description textarea).
    assert "function _kanbanTaskModalKey" in PANELS
    key_fn = re.search(
        r"function _kanbanTaskModalKey\([^)]*\)\{(.*?)\n\}", PANELS, re.DOTALL
    )
    assert key_fn
    key_body = key_fn.group(1)
    assert "ev.key === 'Escape'" in key_body
    assert "ev.key === 'Enter'" in key_body
    assert "TEXTAREA" in key_body  # textarea exception preserved

    # 7. submitKanbanTaskModal() POSTs to /api/kanban/tasks, closes modal,
    #    reloads board, opens detail.
    assert "async function submitKanbanTaskModal()" in PANELS
    submit_fn = re.search(
        r"async function submitKanbanTaskModal\(\)\{(.*?)\n\}", PANELS, re.DOTALL
    )
    assert submit_fn, "submitKanbanTaskModal() not found"
    submit_body = submit_fn.group(1)
    assert "api('/api/kanban/tasks'" in submit_body
    assert "method: 'POST'" in submit_body
    assert "JSON.stringify(payload)" in submit_body
    assert "closeKanbanTaskModal()" in submit_body
    assert "loadKanban(true)" in submit_body
    assert "loadKanbanTask" in submit_body

    # 8. Inline quick-add still works for power-users — typing a title + Enter
    #    creates immediately. Empty submit falls through to the modal.
    assert "async function createKanbanTask()" in PANELS
    quick_add = re.search(
        r"async function createKanbanTask\(\)\{(.*?)\n\}", PANELS, re.DOTALL
    )
    assert quick_add
    qa_body = quick_add.group(1)
    assert "openKanbanCreate()" in qa_body, (
        "Empty inline-input submit must open the modal, not silently return."
    )
    assert "api('/api/kanban/tasks'" in qa_body


def test_kanban_task_detail_has_edit_button_and_modal_supports_edit_mode():
    """The Kanban task detail view must surface an Edit button — the previous
    detail view exposed only status-transition buttons (Triage/Todo/Ready/...),
    Block/Unblock, and Add comment, with no way to edit the title, body,
    assignee, tenant, or priority of a task once created.

    Backend supports it (PATCH /api/kanban/tasks/<id> with title/body/assignee/
    tenant/priority — see _patch_task in api/kanban_bridge.py); this regression
    pins the UI surface.
    """
    # 1. _kanbanRenderTaskDetail emits an Edit button wired to openKanbanEdit.
    render_match = re.search(
        r"function _kanbanRenderTaskDetail\(data\)\{(.*?)\n\}", PANELS, re.DOTALL
    )
    assert render_match, "_kanbanRenderTaskDetail() not found"
    render_body = render_match.group(1)
    assert 'class="kanban-edit-btn"' in render_body or "kanban-edit-btn" in render_body, (
        "Task detail view must include the Edit button (.kanban-edit-btn)."
    )
    assert "openKanbanEdit(" in render_body, (
        "Edit button must invoke openKanbanEdit(taskId)."
    )

    # 2. openKanbanEdit() exists and pre-fills the modal from a fetched task.
    open_edit_match = re.search(
        r"async function openKanbanEdit\([^)]*\)\{(.*?)\n\}", PANELS, re.DOTALL
    )
    assert open_edit_match, "openKanbanEdit() not found"
    open_edit_body = open_edit_match.group(1)
    assert "/api/kanban/tasks/" in open_edit_body
    assert "_kanbanTaskModalMode = 'edit'" in open_edit_body
    assert "_kanbanTaskModalEditingId = task.id" in open_edit_body

    # 3. submitKanbanTaskModal branches to PATCH for edit, POST for create.
    submit_match = re.search(
        r"async function submitKanbanTaskModal\(\)\{(.*?)\n\}", PANELS, re.DOTALL
    )
    assert submit_match
    submit_body = submit_match.group(1)
    assert "method: 'PATCH'" in submit_body, (
        "submitKanbanTaskModal must PATCH /api/kanban/tasks/<id> in edit mode."
    )
    assert "method: 'POST'" in submit_body, "Create path still POSTs."
    assert "_kanbanTaskModalEditingId" in submit_body
    # Edit-mode title-bar / button labels.
    assert "kanban_edit_task" in PANELS
    label_match = re.search(
        r"function _kanbanSetTaskModalLabels\([^)]*\)\{(.*?)\n\}", PANELS, re.DOTALL
    )
    assert label_match and "edit" in label_match.group(1)


def test_kanban_edit_mode_preserves_status_when_dropdown_untouched():
    """Regression: editing a task whose real status is non-editable in the
    modal's status dropdown (running/blocked/done/archived → mapped to
    'triage' for display) must NOT silently demote the task on save.

    The dropdown only offers triage/todo/ready, so `_kanbanEditableStatusFor`
    maps any other status to 'triage' for display.  If the user just edits
    the title and saves, the dropdown's 'triage' default would land in the
    PATCH payload and the backend would call `_set_status_direct` which
    reclaims any active worker and demotes the task.

    Fix: track the displayed default in `_kanbanTaskModalInitialDisplayedStatus`
    and only include `status` in the PATCH payload when the user actually
    picked a different value.
    """
    # 1. The tracking variable is declared at module scope.
    assert "_kanbanTaskModalInitialDisplayedStatus" in PANELS, (
        "Edit-mode status preservation requires tracking the initial displayed "
        "status so submit can detect whether the user actually changed it."
    )
    assert 'id="kanbanTaskModalStatusOriginalHint"' in INDEX
    assert "_kanbanSetTaskModalStatusHint" in PANELS
    assert "kanban_status_original_hint" in I18N
    assert ".kanban-status-original-hint" in STYLE

    # 2. openKanbanEdit captures the initial displayed status from the task.
    open_edit_match = re.search(
        r"async function openKanbanEdit\([^)]*\)\{(.*?)\n\}", PANELS, re.DOTALL
    )
    assert open_edit_match, "openKanbanEdit() not found"
    open_edit_body = open_edit_match.group(1)
    assert "_kanbanTaskModalInitialDisplayedStatus" in open_edit_body, (
        "openKanbanEdit must record the initial displayed status."
    )
    assert "_kanbanEditableStatusFor(task.status)" in open_edit_body
    assert "_kanbanSetTaskModalStatusHint(originalStatus, initialDisplayedStatus)" in open_edit_body
    assert "const originalStatus = task.status || initialDisplayedStatus" in open_edit_body

    # 3. Submit's edit branch only sends status when it differs from the
    #    initial displayed value.
    submit_match = re.search(
        r"async function submitKanbanTaskModal\(\)\{(.*?)\n\}", PANELS, re.DOTALL
    )
    assert submit_match
    submit_body = submit_match.group(1)
    assert "statusVal !== _kanbanTaskModalInitialDisplayedStatus" in submit_body, (
        "Edit submit must skip `status` in the payload when the dropdown's "
        "displayed value is unchanged — otherwise running/blocked/done/archived "
        "tasks get silently demoted on save."
    )

    # 4. openKanbanCreate explicitly nulls the tracker (create always sends).
    create_match = re.search(
        r"function openKanbanCreate\(\)\{(.*?)\n\}", PANELS, re.DOTALL
    )
    assert create_match
    create_body = create_match.group(1)
    assert "_kanbanTaskModalInitialDisplayedStatus = null" in create_body, (
        "openKanbanCreate must reset the tracker to null so create-mode "
        "submits always include status in the POST payload."
    )
    assert "_kanbanSetTaskModalStatusHint(null);" in create_body

    # 5. closeKanbanTaskModal clears the tracker so a stale value can't leak
    #    into the next open.
    close_match = re.search(
        r"function closeKanbanTaskModal\(\)\{(.*?)\n\}", PANELS, re.DOTALL
    )
    assert close_match
    close_body = close_match.group(1)
    assert "_kanbanTaskModalInitialDisplayedStatus = null" in close_body
    assert "_kanbanSetTaskModalStatusHint(null, null);" in close_body


def test_kanban_modal_focus_trap_helper_exists():
    """Shared focus-trap helper should exist and attach/remove Tab key handling."""
    assert "function _trapModalFocus" in PANELS
    fn = re.search(r"function _trapModalFocus\([^)]*\)\{(.*?)\n\}", PANELS, re.DOTALL)
    assert fn, "_trapModalFocus() not found"
    fn_body = fn.group(1)
    assert "addEventListener('keydown'" in fn_body
    assert "removeEventListener('keydown'" in fn_body
    assert "ev.key !== 'Tab'" in fn_body or "ev.key === 'Tab'" in fn_body


def test_kanban_task_modal_focus_trap_is_installed_and_removed():
    """Task modal open calls should install focus trap and close should tear it down."""
    create_match = re.search(r"function openKanbanCreate\(\)\{(.*?)\n\}", PANELS, re.DOTALL)
    assert create_match, "openKanbanCreate() not found"
    create_body = create_match.group(1)
    assert "_kanbanTaskModalFocusCleanup = _trapModalFocus(modal);" in create_body
    assert "if (_kanbanTaskModalFocusCleanup) {" in create_body

    edit_match = re.search(r"async function openKanbanEdit\([^)]*\)\{(.*?)\n\}", PANELS, re.DOTALL)
    assert edit_match, "openKanbanEdit() not found"
    edit_body = edit_match.group(1)
    assert "_kanbanTaskModalFocusCleanup = _trapModalFocus(modal);" in edit_body
    assert "if (_kanbanTaskModalFocusCleanup) {" in edit_body

    close_match = re.search(r"function closeKanbanTaskModal\(\)\{(.*?)\n\}", PANELS, re.DOTALL)
    assert close_match, "closeKanbanTaskModal() not found"
    close_body = close_match.group(1)
    assert "if (_kanbanTaskModalFocusCleanup) {" in close_body
    assert "_kanbanTaskModalFocusCleanup = null;" in close_body


def test_kanban_board_modal_focus_trap_is_installed_and_removed():
    """Board modal open calls should install focus trap and close should tear it down."""
    create_board_match = re.search(r"function openKanbanCreateBoard\(\)\{(.*?)\n\}", PANELS, re.DOTALL)
    assert create_board_match, "openKanbanCreateBoard() not found"
    create_board_body = create_board_match.group(1)
    assert "_kanbanBoardModalFocusCleanup = _trapModalFocus(modal);" in create_board_body
    assert "if (_kanbanBoardModalFocusCleanup) {" in create_board_body

    rename_board_match = re.search(r"function openKanbanRenameBoard\(\)\{(.*?)\n\}", PANELS, re.DOTALL)
    assert rename_board_match, "openKanbanRenameBoard() not found"
    rename_board_body = rename_board_match.group(1)
    assert "_kanbanBoardModalFocusCleanup = _trapModalFocus(modal);" in rename_board_body
    assert "if (_kanbanBoardModalFocusCleanup) {" in rename_board_body

    close_board_match = re.search(r"function closeKanbanBoardModal\(\)\{(.*?)\n\}", PANELS, re.DOTALL)
    assert close_board_match, "closeKanbanBoardModal() not found"
    close_board_body = close_board_match.group(1)
    assert "if (_kanbanBoardModalFocusCleanup) {" in close_board_body
    assert "_kanbanBoardModalFocusCleanup = null;" in close_board_body


def test_kanban_assignee_dropdown_uses_select_not_freetext():
    """Assignee must be a <select> populated from /api/profiles + board history,
    not a free-text input. Free-text invites typos that the dispatcher silently
    rejects (kanban_db.py:3567 "if not row[assignee]: skip"), and the dropdown
    makes the dispatcher contract explicit.
    """
    # The modal markup uses <select> for assignee, with a hint span explaining
    # the dispatcher claim contract.
    sel_idx = INDEX.find('id="kanbanTaskModalAssignee"')
    assert sel_idx != -1, "kanbanTaskModalAssignee element not found"
    # Walk back to find the opening tag — it must be a <select>, not <input>.
    start = INDEX.rfind('<', 0, sel_idx)
    tag_open = INDEX[start:sel_idx + 60]
    assert tag_open.startswith('<select'), (
        f"kanbanTaskModalAssignee must be a <select> element, got: {tag_open[:80]!r}"
    )

    # Hint element exists and references the dispatcher claim contract.
    assert 'id="kanbanTaskModalAssigneeHint"' in INDEX
    hint_idx = INDEX.find('id="kanbanTaskModalAssigneeHint"')
    hint_block = INDEX[hint_idx:hint_idx + 400]
    assert "Hermes profile" in hint_block or "data-i18n=\"kanban_assignee_hint\"" in hint_block

    # The populator function loads from /api/profiles and groups options.
    pop_match = re.search(
        r"async function _kanbanPopulateAssigneeSelect\([^)]*\)\{(.*?)\n\}",
        PANELS, re.DOTALL,
    )
    assert pop_match, "_kanbanPopulateAssigneeSelect() not found"
    pop_body = pop_match.group(1)
    assert "_kanbanLoadProfileNames" in pop_body
    assert "<optgroup" in pop_body
    assert 'value=""' in pop_body, (
        "Must include the explicit empty 'Unassigned' fallthrough option."
    )

    # Profile loader hits /api/profiles.
    load_match = re.search(
        r"async function _kanbanLoadProfileNames\(\)\{(.*?)\n\}", PANELS, re.DOTALL
    )
    assert load_match
    assert "/api/profiles" in load_match.group(1)


def test_kanban_run_dispatcher_button_exists_and_is_distinct_from_preview():
    """The previous Kanban UI only exposed `nudgeKanbanDispatcher()` — a
    dry-run preview that never actually spawns workers — leaving users with
    no way to run their tasks from the WebUI. There must now be a real
    runKanbanDispatcher() entry point AND it must call /api/kanban/dispatch
    WITHOUT dry_run=1, and the existing nudge button must still be a dry-run.
    """
    # 1. runKanbanDispatcher() exists and dispatches without dry_run.
    run_match = re.search(
        r"async function runKanbanDispatcher\(\)\{(.*?)\n\}", PANELS, re.DOTALL
    )
    assert run_match, "runKanbanDispatcher() not found"
    run_body = run_match.group(1)
    assert "/api/kanban/dispatch" in run_body
    # The real-run path must NOT contain dry_run=1.
    assert "dry_run=1" not in run_body, (
        "runKanbanDispatcher() must NOT pass dry_run=1 — that's the preview path."
    )
    # It MUST go through showConfirmDialog (not window.confirm) because it
    # spawns workers — and the existing test_kanban_dashboard_parity_core_controls_are_native
    # asserts no window.confirm/prompt calls in panels.js anyway.
    assert "showConfirmDialog" in run_body, (
        "runKanbanDispatcher() must use showConfirmDialog before spawning workers."
    )

    # 2. nudgeKanbanDispatcher() (the existing preview path) still uses dry_run=1.
    nudge_match = re.search(
        r"async function nudgeKanbanDispatcher\(\)\{(.*?)\n\}", PANELS, re.DOTALL
    )
    assert nudge_match
    nudge_body = nudge_match.group(1)
    assert "dry_run=1" in nudge_body, (
        "nudgeKanbanDispatcher() must remain a dry-run preview (dry_run=1)."
    )

    # 3. The board-header has a button wired to runKanbanDispatcher().
    assert 'id="btnKanbanRunDispatcher"' in INDEX
    btn_idx = INDEX.find('id="btnKanbanRunDispatcher"')
    # Search backward to the opening `<button` and forward to `</button>` to
    # capture the full element (class= attribute precedes id= in the markup).
    btn_start = INDEX.rfind('<button', 0, btn_idx)
    btn_end = INDEX.find('</button>', btn_idx) + len('</button>')
    btn_html = INDEX[btn_start:btn_end]
    assert 'onclick="runKanbanDispatcher()"' in btn_html
    # Distinct visual class so users can tell it apart from the preview button.
    assert "kanban-run-dispatch-btn" in btn_html

    # 4. The sidebar bulk bar also has a Run dispatcher button alongside the
    # existing Preview button, so users in the filter pane can also run.
    bulk_idx = INDEX.find("kanbanBulkBar")
    bulk_html = INDEX[bulk_idx:bulk_idx + 1500]
    assert 'onclick="runKanbanDispatcher()"' in bulk_html, (
        "Sidebar bulk bar must also expose Run dispatcher."
    )
    # The dispatch result formatter exists and surfaces concrete numbers.
    assert "function _kanbanFormatDispatchResult" in PANELS
    fmt_match = re.search(
        r"function _kanbanFormatDispatchResult\([^)]*\)\{(.*?)\n\}",
        PANELS, re.DOTALL,
    )
    assert fmt_match
    fmt_body = fmt_match.group(1)
    for token in ("spawned", "skipped_unassigned", "skipped_nonspawnable", "promoted"):
        assert token in fmt_body, f"dispatch summary missing field: {token}"


def test_kanban_dispatcher_inflight_guard_prevents_double_click_toast_confusion():
    """Guard against concurrent dispatch invocations in both nudge and real run paths."""
    assert "let _kanbanIsDispatching = false;" in PANELS
    assert "function _setKanbanDispatcherButtonsDisabled" in PANELS

    run_match = re.search(r"async function runKanbanDispatcher\(\)\{(.*?)\n\}", PANELS, re.DOTALL)
    assert run_match, "runKanbanDispatcher() not found"
    run_body = run_match.group(1)
    assert "_kanbanIsDispatching" in run_body, (
        "runKanbanDispatcher() must check or set _kanbanIsDispatching to block concurrent execution."
    )
    assert "finally" in run_body, "runKanbanDispatcher() must always clear _kanbanIsDispatching in finally."
    assert "_setKanbanDispatcherButtonsDisabled(true)" in run_body, (
        "runKanbanDispatcher() should disable both dispatcher buttons while posting."
    )
    assert "_setKanbanDispatcherButtonsDisabled(false)" in run_body, (
        "runKanbanDispatcher() should re-enable dispatcher buttons when done."
    )

    nudge_match = re.search(r"async function nudgeKanbanDispatcher\(\)\{(.*?)\n\}", PANELS, re.DOTALL)
    assert nudge_match, "nudgeKanbanDispatcher() not found"
    nudge_body = nudge_match.group(1)
    assert "_kanbanIsDispatching" in nudge_body, (
        "nudgeKanbanDispatcher() should also respect the dispatch in-flight guard."
    )
    assert "finally" in nudge_body, "nudgeKanbanDispatcher() should always clear guard in finally."

    assert 'kanban-run-dispatch-btn' in INDEX
    assert 'kanban-nudge-dispatch-btn' in INDEX
    assert 'btnKanbanRunDispatcher' in INDEX
    assert 'btnKanbanPreviewDispatcher' in INDEX


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


def test_kanban_main_view_scrolls_when_task_preview_is_tall():
    """The app shell keeps body overflow hidden, so the Kanban main view
    must own vertical scrolling. Otherwise a selected task with a long body
    can push the board below the viewport with no way to reach it.
    """
    assert re.search(
        r"main\.main\.showing-kanban\s*>\s*#mainKanban\s*\{[^}]*display:flex;[^}]*overflow-y:auto;",
        COMPACT_STYLE,
    ), "Kanban main view must expose a vertical scrollbar when detail content is taller than the viewport"


def test_kanban_i18n_keys_exist_in_every_locale_block():
    locale_blocks = _locale_blocks_with_body(I18N)
    assert len(locale_blocks) >= 9
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


def test_kanban_modal_locale_parity():
    """Parity check for modal-facing Kanban i18n keys.

    Any locale that already contains modal-facing Kanban strings should include the
    same set of modal vocabulary so new additions don't regress into locale gaps.
    """
    locale_blocks = _locale_blocks_with_body(I18N)
    modal_keys = [
        "kanban_title",
        "kanban_description",
        "kanban_description_placeholder",
        "kanban_status",
        "kanban_assignee",
        "kanban_assignee_placeholder",
        "kanban_tenant",
        "kanban_tenant_placeholder",
        "kanban_priority",
        "kanban_priority_hint",
        "kanban_title_required",
        "kanban_status_original_hint",
    ]
    anchor_key = "kanban_status"
    missing = [
        f"{locale}:{key}"
        for locale, body in locale_blocks
        if re.search(rf"\b{re.escape(anchor_key)}\s*:", body) is not None
        for key in modal_keys
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
    # Live event delivery — either the legacy 30s setInterval polling OR
    # the new SSE /api/kanban/events/stream subscription must be present.
    # The multi-board PR replaced setInterval with EventSource as the
    # default, falling back to setInterval after repeated SSE failures.
    assert (
        "setInterval(refreshKanbanEvents" in PANELS
        or "new EventSource" in PANELS
    ), "Kanban must subscribe to live events via SSE or polling"
    assert "prompt(" not in PANELS
    assert "confirm(" not in PANELS


def test_kanban_dashboard_parity_i18n_keys_exist():
    locale_blocks = _locale_blocks_with_body(I18N)
    required_keys = [
        "kanban_only_mine",
        "kanban_bulk_action",
        "kanban_nudge_dispatcher",
        "kanban_work_queue_hint",
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


def test_kanban_lifecycle_controls_do_not_offer_manual_running_start():
    assert "quickKanbanCardAction(event,'${id}','running')" not in PANELS
    assert "kanban_card_start" not in PANELS
    assert "kanban_card_start" not in I18N
    assert '<option value="running">Running</option>' not in INDEX
    assert "Cannot set status to 'running' directly" not in PANELS
    assert "kanban_work_queue_hint" in PANELS
    assert "Preview dispatcher" in INDEX
    assert "Nudge dispatcher" not in INDEX


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
    locale_blocks = _locale_blocks_with_body(I18N)
    required_keys = ["kanban_lanes_by_profile", "kanban_card_complete", "kanban_card_archive", "kanban_unassigned", "kanban_work_queue_hint"]
    missing = [
        f"{locale}:{key}"
        for locale, body in locale_blocks
        for key in required_keys
        if re.search(rf"\b{re.escape(key)}\s*:", body) is None
    ]
    assert missing == []



def test_kanban_review_feedback_static_ui_fixes_exist():
    assert "function closeKanbanTaskDetail" in PANELS
    assert "kanban-back-btn" in PANELS
    assert "function _kanbanFormatTimestamp" in PANELS
    assert "function _kanbanEventSummary" in PANELS
    assert "data.log || {}" in PANELS
    assert ".kanban-task-preview-header" in STYLE
    assert ".kanban-back-btn" in STYLE
    assert "@media (max-width: 640px)" in STYLE
    assert "scroll-snap-type" in STYLE
    assert "kanban-stats-grid" in PANELS


def test_kanban_task_detail_renderer_executes_with_log_and_formats_feedback():
    import json
    import subprocess
    script = """
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync('static/panels.js', 'utf8');
function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>\"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[ch]));
}
const context = {
  console,
  setInterval(){ return 1; },
  document: { querySelectorAll(){ return []; }, getElementById(){ return null; }, addEventListener(){} },
  window: { addEventListener(){} },
  t(key){
    const map = {
      kanban_no_description:'No description', kanban_comments_count:'Comments ({0})', kanban_events_count:'Events ({0})',
      kanban_links:'Links', kanban_runs_count:'Runs ({0})', kanban_worker_log:'Worker log', kanban_empty:'Empty',
      kanban_no_comments:'No comments', kanban_no_events:'No events', kanban_no_runs:'No runs', kanban_add_comment:'Add comment',
      kanban_block:'Block', kanban_unblock:'Unblock', kanban_back_to_board:'Back to board', kanban_task:'Task',
      kanban_status_triage:'Triage', kanban_status_todo:'Todo', kanban_status_ready:'Ready', kanban_status_running:'Running',
      kanban_status_blocked:'Blocked', kanban_status_done:'Done', kanban_status_archived:'Archived'
    };
    return map[key] || key;
  },
  esc, $(){ return null; }, api(){}, showToast(){}, li(){ return ''; }, S: {}
};
vm.createContext(context);
vm.runInContext(src, context);
const html = vm.runInContext(`_kanbanRenderTaskDetail({
  task:{id:'t_1', title:'Demo', status:'ready', body:'Body'},
  comments:[{body:'hello', author:'webui', created_at:1777931496}],
  events:[{kind:'blocked', payload:{reason:'waiting'}, created_at:1777931496}],
  links:{parents:['t_0'], children:[]},
  runs:[],
  log:{content:'worker log'}
})`, context);
console.log(JSON.stringify({html}));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    html = json.loads(result.stdout)["html"]
    assert "worker log" in html
    assert "kanban-back-btn" in html
    assert "Back to board" in html
    assert "1777931496" not in html
    assert "waiting" in html
    assert "ReferenceError" not in html


def test_kanban_readonly_banner_starts_hidden_and_is_toggled_on_load():
    """The 'Read-only view' banner must start hidden in the HTML and only
    become visible when the bridge reports read_only=true. Always-visible
    label is misleading when the kanban_db is fully writable.
    """
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    index_path = os.path.join(here, "..", "static", "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        html = f.read()
    # Banner must be in HTML but default-hidden
    assert 'class="kanban-readonly"' in html
    assert 'data-i18n="kanban_read_only"' in html
    # The banner element must have inline style="display:none" (default-hidden)
    # A naive substring check is sufficient — there is exactly one such element.
    banner_block = html[html.find('class="kanban-readonly"'):html.find('class="kanban-readonly"') + 200]
    assert 'display:none' in banner_block, (
        "Read-only banner must default to display:none in HTML to avoid "
        "flashing the wrong message before loadKanban() resolves the actual "
        "read_only flag from the API."
    )
    # And panels.js must toggle it based on _kanbanBoard.read_only
    panels_path = os.path.join(here, "..", "static", "panels.js")
    with open(panels_path, "r", encoding="utf-8") as f:
        panels = f.read()
    assert ".kanban-readonly" in panels, (
        "panels.js must reference .kanban-readonly to toggle the banner"
    )
    assert "_kanbanBoard.read_only" in panels, (
        "panels.js must consult _kanbanBoard.read_only when toggling the banner"
    )


# ── Multi-board switcher UI tests ───────────────────────────────────────────

def test_kanban_board_switcher_markup_in_index():
    """The board switcher next to the Board title must be in index.html so
    it loads on first paint without a JS round-trip."""
    assert 'id="kanbanBoardSwitcher"' in INDEX
    assert 'id="kanbanBoardSwitcherToggle"' in INDEX
    assert 'id="kanbanBoardSwitcherMenu"' in INDEX
    assert 'id="kanbanBoardSwitcherName"' in INDEX
    # Switcher must be hidden by default — only revealed when ≥1 non-default
    # board exists, otherwise it would clutter single-board deployments.
    assert 'id="kanbanBoardSwitcher"' in INDEX
    assert 'hidden>' in INDEX or 'hidden ' in INDEX  # presence of hidden attr


def test_kanban_board_modal_markup_in_index():
    """The create/rename board modal lives at the bottom of body so the
    fixed-positioned overlay isn't trapped inside any scroll container."""
    for sel in (
        'id="kanbanBoardModal"',
        'id="kanbanBoardModalTitle"',
        'id="kanbanBoardModalName"',
        'id="kanbanBoardModalSlugInput"',
        'id="kanbanBoardModalDesc"',
        'id="kanbanBoardModalIcon"',
        'id="kanbanBoardModalColor"',
        'id="kanbanBoardModalError"',
        'id="kanbanBoardModalSubmit"',
    ):
        assert sel in INDEX
    # Modal must be hidden by default
    assert 'id="kanbanBoardModal" hidden' in INDEX


def test_kanban_board_switcher_handlers_in_panels():
    """Every UI affordance must have a corresponding JS handler."""
    for fn in (
        "async function loadKanbanBoards",
        "function _renderKanbanBoardMenu",
        "function toggleKanbanBoardMenu",
        "async function switchKanbanBoard",
        "function openKanbanCreateBoard",
        "function openKanbanRenameBoard",
        "function closeKanbanBoardModal",
        "async function submitKanbanBoardModal",
        "async function archiveKanbanBoard",
    ):
        assert fn in PANELS, f"Missing handler: {fn}"


def test_kanban_board_switcher_calls_correct_endpoints():
    """The switcher must hit the right REST verbs to round-trip with the
    bridge's multi-board contract."""
    # GET /boards
    assert "api('/api/kanban/boards'" in PANELS
    # POST /boards (create)
    assert "method: 'POST'" in PANELS
    # POST /boards/<slug>/switch
    assert "/api/kanban/boards/' + encodeURIComponent" in PANELS
    assert "/switch'" in PANELS
    # PATCH /boards/<slug>
    assert "method: 'PATCH'" in PANELS
    # DELETE /boards/<slug>
    assert "method: 'DELETE'" in PANELS


def test_kanban_board_param_is_plumbed_into_api_calls():
    """Every existing kanban endpoint call must carry ?board=<slug> when
    a non-default board is active. The shared helper is _kanbanBoardQuery()."""
    assert "_kanbanBoardQuery" in PANELS
    # Spot-check critical call sites
    assert "/api/kanban/board' + (params.toString()" in PANELS  # board with filters
    assert "/api/kanban/config' + _kanbanBoardQuery()" in PANELS
    assert "/api/kanban/stats' + _kanbanBoardQuery()" in PANELS
    assert "/api/kanban/assignees' + _kanbanBoardQuery()" in PANELS


def test_kanban_active_board_persisted_to_localstorage():
    """The last-viewed board slug must persist to localStorage so a refresh
    keeps the user on the same board."""
    assert "KANBAN_BOARD_LS_KEY" in PANELS
    assert "'hermes-kanban-active-board'" in PANELS
    assert "_kanbanGetSavedBoard" in PANELS
    assert "_kanbanSetSavedBoard" in PANELS


def test_kanban_profile_assignee_cache_has_invalidation_path():
    """Kanban assignee suggestions should stay aligned with profile mutations.

    The cache in _kanbanLoadProfileNames() can become stale when profiles are
    created or deleted in the same session. This adds an explicit
    invalidation path and a short TTL so modal opens recover from same-session
    mutations and cross-tab/CLI changes.
    """
    assert "_KANBAN_PROFILE_NAMES_CACHE_TTL_MS" in PANELS
    assert "_kanbanProfileNamesCacheAt" in PANELS
    assert "_invalidateKanbanProfileCache" in PANELS

    load_start = PANELS.find("async function _kanbanLoadProfileNames(){")
    assert load_start != -1, "Missing _kanbanLoadProfileNames() declaration"
    load_end = PANELS.find("\n}\n\nasync function _kanbanPopulateAssigneeSelect", load_start)
    if load_end == -1:
        load_end = PANELS.find("\n}\n\nfunction openKanbanCreate", load_start)
    load_body = PANELS[load_start:load_end] if load_end != -1 else PANELS[load_start:load_start + 2200]
    assert "Date.now() - _kanbanProfileNamesCacheAt" in load_body
    assert "_kanbanProfileNamesCacheAt = Date.now()" in load_body

    save_start = PANELS.find("async function saveProfileForm(){")
    assert save_start != -1, "Missing saveProfileForm() declaration"
    save_end = PANELS.find("\n}\n\n// Back-compat", save_start)
    save_body = PANELS[save_start:save_end if save_end != -1 else save_start + 2000]
    assert "_invalidateKanbanProfileCache();" in save_body, (
        "Profile create flow should invalidate Kanban assignee cache after success."
    )

    delete_start = PANELS.find("async function deleteProfile(name) {")
    assert delete_start != -1, "Missing deleteProfile() declaration"
    delete_end = PANELS.find("\n\n// ── Memory panel", delete_start)
    delete_body = PANELS[delete_start:delete_end if delete_end != -1 else delete_start + 1300]
    assert "_invalidateKanbanProfileCache();" in delete_body, (
        "Profile delete flow should invalidate Kanban assignee cache after success."
    )

    ui_delete_start = PANELS.find("async function deleteCurrentProfile(){")
    assert ui_delete_start != -1, "Missing deleteCurrentProfile() declaration"
    ui_delete_end = PANELS.find("\n\nfunction renderProfileDropdown", ui_delete_start)
    ui_delete_body = PANELS[ui_delete_start:ui_delete_end if ui_delete_end != -1 else ui_delete_start + 1300]
    assert "_invalidateKanbanProfileCache();" in ui_delete_body, (
        "Profile detail delete flow (deleteCurrentProfile) should invalidate Kanban assignee cache after success."
    )


def test_kanban_archive_board_uses_showConfirmDialog():
    """Archive is destructive → must use the styled showConfirmDialog,
    not native confirm() (which can't be styled or i18n'd)."""
    # The archive path
    arch_idx = PANELS.find("async function archiveKanbanBoard")
    assert arch_idx > 0
    # Look at the next 800 chars
    archive_block = PANELS[arch_idx:arch_idx + 800]
    assert "showConfirmDialog" in archive_block
    assert "danger: true" in archive_block


# ── SSE event stream UI tests ───────────────────────────────────────────────

def test_kanban_sse_eventsource_subscription_is_default():
    """The Kanban panel must subscribe to /api/kanban/events/stream via
    EventSource as the default live-update mechanism (the multi-board PR
    replaced 30s polling with SSE for ~300ms latency parity with the
    agent dashboard's WebSocket /events). 30s polling remains as the
    auto-fallback after repeated SSE failures."""
    assert "new EventSource" in PANELS
    assert "/api/kanban/events/stream" in PANELS
    assert "_kanbanStartEventStream" in PANELS
    assert "addEventListener('hello'" in PANELS
    assert "addEventListener('events'" in PANELS


def test_kanban_sse_falls_back_to_polling_on_repeated_failure():
    """After 3 SSE failures the client must fall back to HTTP polling so
    a flaky connection doesn't leave the user with stale data."""
    assert "_kanbanEventSourceFailures" in PANELS
    assert ">= 3" in PANELS  # the failure threshold
    assert "setInterval(refreshKanbanEvents" in PANELS  # the fallback


def test_kanban_sse_torn_down_on_panel_switch():
    """The long-lived SSE connection must close when the user leaves the
    Kanban panel — leaving it open wastes a server thread and a client
    connection slot."""
    assert "_kanbanStopPolling" in PANELS
    # The teardown must be wired into switchPanel
    assert "prevPanel === 'kanban'" in PANELS
    assert "_kanbanStopPolling()" in PANELS


def test_kanban_sse_refresh_is_debounced():
    """A burst of events shouldn't trigger N reloads — must coalesce."""
    assert "_scheduleKanbanRefresh" in PANELS
    assert "_kanbanRefreshScheduled" in PANELS
    # 250ms debounce window
    assert "}, 250)" in PANELS


def test_kanban_board_color_is_validated_against_css_injection():
    """`board.color` is interpolated into a `style=""` attribute on the
    switcher icon. esc() escapes HTML but does NOT prevent CSS-context
    injection: an attacker (with WebUI write access, or via the agent CLI
    which doesn't validate either) could set color to
    `red;background:url('http://attacker/exfil')` and have the malicious
    URL fetched whenever any user opens the board switcher.

    Drive the helper through Node and assert that named colors / hex
    codes are accepted while every CSS-injection shape is rejected.
    """
    import json
    import subprocess
    script = """
const fs = require('fs');
const src = fs.readFileSync('static/panels.js', 'utf8');
const start = src.indexOf('function _kanbanSafeColor');
if (start < 0) { console.error('_kanbanSafeColor missing'); process.exit(2); }
// Grab the function body up to and including the closing `}` line.
const tail = src.slice(start);
const end = tail.indexOf('\\n}\\n') + 2;
const fn = tail.slice(0, end);
const ctx = {};
new Function('out', fn + '; out.fn = _kanbanSafeColor;')(ctx);
const cases = [
  ['#fff', '#fff'],
  ['#3b82f6', '#3b82f6'],
  ['red', 'red'],
  ['Blue', 'Blue'],
  // injection attempts must all collapse to '' so the renderer drops
  // the `color:` rule entirely.
  ["red;background:url('http://attacker/exfil')", ''],
  ['red;background-image:url(http://x)', ''],
  ['expression(alert(1))', ''],
  ['#zzz', ''],
  ['', ''],
  [null, ''],
  [undefined, ''],
];
const results = cases.map(([input, expected]) => ({
  input, expected, actual: ctx.fn(input)
}));
console.log(JSON.stringify(results));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    results = json.loads(result.stdout)
    failures = [r for r in results if r["actual"] != r["expected"]]
    assert not failures, f"_kanbanSafeColor mismatches: {failures}"

    # The renderer must call the helper, not pass b.color through esc()
    # directly into the style attribute.
    assert "_kanbanSafeColor(b.color)" in PANELS
    assert "color:${esc(b.color)}" not in PANELS


def test_kanban_locale_parity():
    """Every kanban_* i18n key in the English locale must exist in all
    non-English locale blocks.  The kanban panel has its own set of ~86
    keys (kanban_board, kanban_task, …) that are rendered via t() — a
    missing key silently falls back to English, which is acceptable for
    content keys but confusing for UI labels the user expects to see
    translated.

    This test catches regressions where a new kanban key is added to the
    English block but not to one or more locale blocks.  Pattern borrowed
    from test_lineage_segment_locale_keys_are_defined_for_sidebar_locales
    in test_session_lineage_collapse.py.

    Refs: #1973
    """
    locale_blocks = _locale_blocks_with_body(I18N)
    assert locale_blocks, "No locale blocks found in i18n.js"

    # Collect the kanban_* keys from the English block.
    en_name = "en"
    en_body = None
    for name, body in locale_blocks:
        if name == en_name:
            en_body = body
            break
    assert en_body is not None, "English locale block not found"

    en_keys = set(re.findall(r"(kanban_\w+)\s*:", en_body))
    assert en_keys, "No kanban_* keys found in English locale"

    # Verify each non-English locale has the same set.
    failures = []
    for name, body in locale_blocks:
        if name == en_name:
            continue
        loc_keys = set(re.findall(r"(kanban_\w+)\s*:", body))
        missing = en_keys - loc_keys
        extra = loc_keys - en_keys
        if missing:
            failures.append(f"{name}: missing {sorted(missing)}")
        if extra:
            failures.append(f"{name}: extra {sorted(extra)}")

    assert not failures, (
        "Kanban i18n key parity violations:\n" + "\n".join(failures)
    )
