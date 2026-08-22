from pathlib import Path
import re

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
UI = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
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
    main_view_panels = re.search(r"const MAIN_VIEW_PANELS = \[([^\]]+)\];", PANELS)
    assert main_view_panels, "MAIN_VIEW_PANELS should define main-view panels"
    assert "'kanban'" in main_view_panels.group(1)
    assert "MAIN_VIEW_PANELS.forEach(p => {" in PANELS
    assert "mainEl.classList.toggle('showing-' + p, nextPanel === p);" in PANELS
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


def test_kanban_dispatcher_no_longer_blocks_default_board():
    """Pin removal of the previous null-board guard in runKanbanDispatcher()."""
    run_source = extract_function(PANELS, "runKanbanDispatcher", prefix="async function")
    assert "if (!_kanbanCurrentBoard)" not in run_source, (
        "runKanbanDispatcher() must not block when _kanbanCurrentBoard is null; "
        "default board should dispatch through a board-less path."
    )


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


def test_kanban_dragging_card_does_not_open_detail_on_drop_click():
    """Regression: drag/drop should move a card without opening task detail."""
    assert "function _kanbanSuppressNextCardClick" in PANELS
    assert "let _kanbanSuppressCardClickUntil" in PANELS
    assert "function openKanbanCard" in PANELS
    assert "function finishKanbanDrag" in PANELS

    drag_fn = re.search(r"function dragKanbanTask\([^)]*\)\{(.*?)\n\}", PANELS, re.DOTALL)
    assert drag_fn, "dragKanbanTask() not found"
    assert "_kanbanSuppressNextCardClick" in drag_fn.group(1), (
        "drag start must arm the click suppressor so the trailing click after "
        "drop cannot open the task detail pane"
    )

    finish_fn = re.search(r"function finishKanbanDrag\([^)]*\)\{(.*?)\n\}", PANELS, re.DOTALL)
    assert finish_fn, "finishKanbanDrag() not found"
    assert "_kanbanSuppressNextCardClick" in finish_fn.group(1), (
        "drag end must refresh the suppressor window before browsers emit a "
        "trailing synthetic click"
    )

    drop_fn = re.search(r"async function dropKanbanTask\([^)]*\)\{(.*?)\n\}", PANELS, re.DOTALL)
    assert drop_fn, "dropKanbanTask() not found"
    drop_body = drop_fn.group(1)
    assert "_kanbanSuppressNextCardClick" in drop_body
    assert "event.stopPropagation()" in drop_body
    assert "updateKanbanTask(taskId, {status}, {openDetail: false})" in drop_body, (
        "drag/drop status updates must refresh the board without opening the task detail"
    )

    update_fn = re.search(r"async function updateKanbanTask\([^)]*\)\{(.*?)\n\}", PANELS, re.DOTALL)
    assert update_fn, "updateKanbanTask() not found"
    update_body = update_fn.group(1)
    assert "const openDetail = !opts || opts.openDetail !== false;" in update_body
    assert "if (openDetail) await loadKanbanTask" in update_body

    card_template = re.search(r"return `<article class=\"kanban-card.*?</article>`;", PANELS, re.DOTALL)
    assert card_template, "Kanban card template not found"
    card_html = card_template.group(0)
    assert "ondragend=\"finishKanbanDrag(event)\"" in card_html
    assert "onclick=\"return openKanbanCard(event," in card_html
    assert "onclick=\"loadKanbanTask" not in card_html, (
        "Kanban cards must not call loadKanbanTask directly from onclick; "
        "drag/drop needs a guarded click path"
    )

    open_fn = re.search(r"function openKanbanCard\([^)]*\)\{(.*?)\n\}", PANELS, re.DOTALL)
    assert open_fn, "openKanbanCard() not found"
    open_body = open_fn.group(1)
    for token in (
        "Date.now()",
        "_kanbanSuppressCardClickUntil",
        "preventDefault",
        "stopPropagation",
        "loadKanbanTask",
    ):
        assert token in open_body


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


def test_kanban_modal_mobile_responsive_css():
    """On narrow phones (<=640px) a tall kanban modal must stay reachable: the
    overlay scrolls (overflow-y:auto) and safe-centers its content
    (align-items:safe center) so an overflowing modal is never clipped above
    the fold where its top can't be scrolled back into view.

    A further short-landscape override (<=640px AND <=480px tall) top-anchors
    the overlay (align-items:flex-start) instead of centering — an even
    stronger anti-clip guarantee for pathological short-landscape viewports
    (e.g. 480x320) where centering a capped modal would push its lower rows
    past the fold. Every overlay override must still scroll."""
    # There are several `.kanban-modal-overlay{...}` rules (skin override, base
    # desktop, the mobile <=640px override, and the short-landscape override).
    # Match against COMPACT_STYLE — whitespace-stripped, like the sibling CSS
    # tests. Every mobile override must be scrollable; the primary phone
    # override must safe-center; a short-landscape override may top-anchor.
    overlay_rules = re.findall(r"\.kanban-modal-overlay\{([^}]*)\}", COMPACT_STYLE)
    assert overlay_rules, "no .kanban-modal-overlay rule found in style.css"
    # The primary phone override safe-centers AND scrolls.
    phone_rules = [r for r in overlay_rules if "align-items:safecenter" in r]
    assert phone_rules, (
        f"a mobile overlay override must safe-center its content so an "
        f"overflowing modal is never clipped above the fold. "
        f"Got rules: {overlay_rules}"
    )
    assert "overflow-y:auto" in phone_rules[-1], (
        f"the phone overlay must be scrollable. Got: {phone_rules[-1]}"
    )
    # Any top-anchored short-landscape override must ALSO scroll (top-anchoring
    # is a valid anti-clip alternative to centering, but it must not drop the
    # overflow-y:auto that keeps the modal reachable).
    for rule in overlay_rules:
        if "align-items:flex-start" in rule:
            assert "overflow-y:auto" in rule, (
                f"a top-anchored overlay override must still scroll. Got: {rule}"
            )


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


def test_kanban_board_switcher_icon_column_clamps_long_labels():
    """Regression for #2458: board metadata may use a short text label in the
    icon/color slot. The menu must keep that label inside its own column instead
    of letting it overlap the board title and count badge.
    """
    rule = re.search(
        r"\.kanban-board-switcher-item-icon\{(?P<body>.*?)\}",
        STYLE,
        flags=re.S,
    )
    assert rule, "missing .kanban-board-switcher-item-icon CSS rule"
    compact = re.sub(r"\s+", "", rule.group("body"))
    for required in (
        "overflow:hidden",
        "text-overflow:ellipsis",
        "white-space:nowrap",
        "max-width:7.5rem",
        "min-width:18px",
    ):
        assert required in compact


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
    fn_source = extract_function(PANELS, "_kanbanSafeColor")
    script = """
const fnSource = __FN__;
const ctx = {};
new Function('out', fnSource + '; out.fn = _kanbanSafeColor;')(ctx);
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
""".replace("__FN__", json.dumps(fn_source))
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


def test_kanban_profile_lanes_explicitly_render_unassigned_lane():
    """Regression: unassigned Ready tasks must not disappear when the board is
    grouped by profile/lane. Mobile users should see an explicit Unassigned
    lane via a stable internal key instead of needing tasks assigned to
    `default` for visibility.
    """
    assert "function _kanbanLaneNames" in PANELS
    assert "function _kanbanRenderProfileLanes" in PANELS
    assert "KANBAN_UNASSIGNED_LANE" in PANELS
    assert "function _kanbanLaneKey" in PANELS
    assert "function _kanbanLaneLabel" in PANELS
    assert "kanban_unassigned" in PANELS

    # Lane key helper returns the constant for tasks without an assignee.
    assert "KANBAN_UNASSIGNED_LANE" in PANELS
    # Lane label helper converts the constant back to a translated string.
    assert "t('kanban_unassigned')" in PANELS

    # _kanbanLaneNames uses _kanbanLaneKey to build the set, not raw assignee.
    lane_names_match = re.search(
        r"function _kanbanLaneNames\(columns\)\{(.*?)\nfunction ",
        PANELS,
        re.DOTALL,
    )
    assert lane_names_match, "_kanbanLaneNames() not found"
    lane_names_body = lane_names_match.group(1)
    assert "_kanbanLaneKey(task)" in lane_names_body
    # Unassigned lane is appended last (after assigned lanes).
    assert "has(KANBAN_UNASSIGNED_LANE)" in lane_names_body

    # _kanbanRenderProfileLanes uses _kanbanLaneKey for filtering and
    # _kanbanLaneLabel for display, and emits the unassigned CSS class.
    render_match = re.search(
        r"function _kanbanRenderProfileLanes\(columns\)\{(.*?)\nfunction ",
        PANELS,
        re.DOTALL,
    )
    assert render_match, "_kanbanRenderProfileLanes() not found"
    render_body = render_match.group(1)
    assert "_kanbanLaneNames(columns)" in render_body
    assert "_kanbanLaneKey(task)" in render_body
    assert "_kanbanLaneLabel(lane)" in render_body
    assert "kanban-profile-lane-unassigned" in render_body
    assert "kanban-profile-lane" in render_body


def test_kanban_hidden_by_filters_ux():
    """When all tasks are filtered by the text search but unfiltered data
    exists, the board should show an explanation and a Clear filters button
    instead of a generic 'No Kanban data' empty state.
    """
    # The helper functions exist.
    assert "function _kanbanHiddenByFiltersHtml" in PANELS
    assert "function clearKanbanFilters" in PANELS

    # The empty-board branch checks unfiltered total.
    render_match = re.search(
        r"function _kanbanRenderBoard\(\)\{(.*?)\nfunction ",
        PANELS,
        re.DOTALL,
    )
    assert render_match, "_kanbanRenderBoard() not found"
    render_body = render_match.group(1)
    assert "_kanbanHiddenByFiltersHtml" in render_body
    assert "unfilteredTotal" in render_body

    # The hidden-html helper references the i18n keys.
    hidden_match = re.search(
        r"function _kanbanHiddenByFiltersHtml\(\)\{(.*?)\n\}",
        PANELS,
        re.DOTALL,
    )
    assert hidden_match, "_kanbanHiddenByFiltersHtml() not found"
    hidden_body = hidden_match.group(1)
    assert "kanban_tasks_hidden_by_filters" in hidden_body
    assert "kanban_clear_filters" in hidden_body
    assert "clearKanbanFilters()" in hidden_body

    # clearKanbanFilters() resets all filter inputs and reloads.
    clear_match = re.search(
        r"function clearKanbanFilters\(\)\{(.*?)\n\}",
        PANELS,
        re.DOTALL,
    )
    assert clear_match, "clearKanbanFilters() not found"
    clear_body = clear_match.group(1)
    assert "kanbanSearch" in clear_body
    assert "kanbanAssigneeFilter" in clear_body
    assert "kanbanTenantFilter" in clear_body
    assert "loadKanban(true)" in clear_body

    # i18n keys exist in every locale.
    locale_blocks = _locale_blocks_with_body(I18N)
    for key in ("kanban_tasks_hidden_by_filters", "kanban_clear_filters"):
        missing = [
            locale
            for locale, body in locale_blocks
            if re.search(rf"\b{re.escape(key)}\s*:", body) is None
        ]
        assert missing == [], f"i18n key '{key}' missing from locales: {missing}"


def test_kanban_unassigned_lane_in_sidebar_meta():
    """Sidebar task list must show 'unassigned' label for tasks without an
    assignee, not silently omit the field.
    """
    meta_match = re.search(
        r"function _kanbanTaskMeta\(task\)\{(.*?)\n\}",
        PANELS,
        re.DOTALL,
    )
    assert meta_match, "_kanbanTaskMeta() not found"
    meta_body = meta_match.group(1)
    # Must emit unassigned label when task.assignee is falsy.
    assert "t('kanban_unassigned')" in meta_body


def test_kanban_card_exposes_next_dispatch_model_override():
    """A task with a model_override must surface the model on the board card and
    in the task meta, so the model the card's NEXT dispatch will use is visible
    at a glance. (The override is a dispatch-time input, so it can never be
    presented as the model an already-running worker is using -- see
    test_kanban_running_card_model_badge_does_not_claim_the_active_run.)"""
    # _kanbanTaskMeta appends a 🧠 chip carrying the override (provider wins the
    # label when both are set, since it disambiguates the backend).
    meta_match = re.search(
        r"function _kanbanTaskMeta\(task\)\{(.*?)\n\}",
        PANELS,
        re.DOTALL,
    )
    assert meta_match, "_kanbanTaskMeta() not found"
    meta_body = meta_match.group(1)
    assert "task.model_override" in meta_body
    # The chip names the MODEL, with the provider only as a qualifier -- the
    # earlier `provider_override || model_override` form would have labelled a
    # bare provider id as the "Model (next dispatch)".
    assert "task.provider_override || task.model_override" not in meta_body
    assert "${task.model_override} (${task.provider_override})" in meta_body
    assert "🧠" in meta_body

    # _kanbanCard renders a .kanban-badge.model chip in the card top line when
    # an override is set, and omits it when there is none.
    card_match = re.search(
        r"function _kanbanCard\(task, status\)\{(.*?)\n\}",
        PANELS,
        re.DOTALL,
    )
    assert card_match, "_kanbanCard() not found"
    card_body = card_match.group(1)
    assert "kanban-badge model" in card_body
    assert "task.model_override ?" in card_body or "task.model_override\n" in card_body

    # Detail panel shows an explicit Model row (override or 'profile default').
    detail_match = re.search(
        r"function _kanbanRenderTaskDetail\(data\)\{(.*?)\n\}",
        PANELS,
        re.DOTALL,
    )
    assert detail_match, "_kanbanRenderTaskDetail() not found"
    detail_body = detail_match.group(1)
    assert "kanban-detail-model" in detail_body
    assert "kanban_no_model_override" in detail_body

    # i18n keys exist (English block) so the labels/tooltips resolve.
    assert "kanban_model:" in I18N
    assert "kanban_provider:" in I18N
    assert "kanban_no_model_override:" in I18N
    assert "kanban_card_model_hint:" in I18N
    assert "kanban_card_model_hint_running:" in I18N
    assert "kanban_model_next_dispatch:" in I18N


def test_kanban_model_badge_static_render_e2e():
    """Execute _kanbanCard() with override present/absent to prove the badge is
    genuinely emitted into the returned HTML, not just referenced in source."""
    import json
    import subprocess

    fn_source = extract_function(PANELS, "_kanbanCard")
    script = (
        "const fnSource = " + json.dumps(fn_source) + ";\n"
        "const out = {};\n"
        "new Function('out', fnSource + '; out.fn = _kanbanCard;')(out);\n"
        "const t = () => '';\n"
        "const esc = (s) => String(s == null ? '' : s)"
                "  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')\n"
                "  .replace(/\\\"/g,'&quot;').replace(/'/g,'&#39;');\n"
                "const _kanbanTaskAge = () => '';\n"
                "const _kanbanTaskBody = (task) => task.body || task.description || task.prompt || '';\n"
                "const _kanbanCardStalenessClass = () => '';\n"
                "const _kanbanTaskTitle = (task) => task.title || task.id || '';\n"
                "const _kanbanCardQuickActions = () => '';\n"
                "const taskWith = { id:'t1', model_override:'gpt-5.6-sol', provider_override:'openai' };\n"
        "const withModel = out.fn(taskWith, 'ready');\n"
        "const without = out.fn({ id:'t2' }, 'todo');\n"
        "out.withModel = withModel;\n"
        "out.without = without;\n"
        "out.hasBadge = /kanban-badge model/.test(withModel) && /🧠 gpt-5.6-sol/.test(withModel);\n"
        "out.noBadge = !/kanban-badge model/.test(without);\n"
        "console.log(JSON.stringify(out));\n"
    )
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"node -e failed: {result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["hasBadge"] is True, "model_override must render a model badge on the card"
    assert payload["noBadge"] is True, "card without model_override must not render a model badge"


def test_kanban_editor_modal_has_model_and_provider_fields():
    """Create/edit task modal must expose a Model selector backed by the shared
    /api/models catalog via the same searchable renderModelDropdown() picker the
    composer + settings use (not free-text, not a bare native select), and the
    submit handler must route the chosen model + provider to the API so users can
    configure the model the card's next dispatch will use from the WebUI."""
    # The model field is a chip trigger + hidden full-catalog <select> + dropdown
    # shell (the renderModelDropdown pattern). No separate provider text field.
    assert 'id="kanbanTaskModalModelChip"' in INDEX
    assert 'id="kanbanTaskModalModel"' in INDEX
    assert 'id="kanbanTaskModalModelDropdown"' in INDEX
    assert 'class="model-dropdown settings-model-dropdown"' in INDEX
    assert 'id="kanbanTaskModalProvider"' not in INDEX

    # Label wired for i18n. The explanatory hint is delivered ONCE, as the
    # picker's sticky scope note (asserted in
    # test_kanban_model_wording_is_dispatch_scoped_everywhere) -- repeating the
    # same kanban_model_hint string as a per-row hint made the Model row the
    # tallest row in the modal and pushed it past the calc(100vh - 48px) cap at
    # 1920x1080 in the longer locales (#6906). The row therefore carries only the
    # label plus the chip, whose empty state reads "Profile default".
    assert 'data-i18n="kanban_model"' in INDEX
    _model_row_at = INDEX.index('<label for="kanbanTaskModalModel"')
    model_row = INDEX[_model_row_at:INDEX.index('<div class="kanban-modal-row">', _model_row_at)]
    assert 'kanban-modal-hint' not in model_row, (
        "the Model row must not duplicate the picker scope note as a row hint "
        "(#6906 modal height cap)"
    )
    assert 'data-i18n="kanban_no_model_override"' in model_row

    # The populator reuses the same /api/models catalog + provider grouping +
    # overflow the composer picker uses (data-extraModels feeds "Show more"),
    # and restores a task's PERSISTED provider on edit so an unrelated edit
    # doesn't rewrite or strip the saved provider pin.
    populate_match = re.search(
        r"function _kanbanPopulateModelSelect\(currentValue, currentProvider\)\{(.*?)\n\}",
        PANELS, re.DOTALL,
    )
    assert populate_match, "_kanbanPopulateModelSelect(currentValue, currentProvider) not found"
    populate_body = populate_match.group(1)
    assert "api/models" in populate_body
    assert "optgroup" in populate_body
    assert "dataset.provider" in populate_body
    assert "dataset.extraModels" in populate_body  # full list: overflow/"Show more"
    assert "kanban_no_model_override" in populate_body
    assert "currentProvider" in populate_body  # edit preserves persisted provider
    assert "dataset.provider = currentProvider ? String(currentProvider) : ''" in populate_body

    # A stale in-flight /api/models populate must not clobber a newer modal's
    # selection (openKanbanCreate fires un-awaited; openKanbanEdit awaits both on
    # the same select). A sequence token drops late responses.
    assert "_kanbanModelPopulateSeq" in PANELS
    populate_tok_src = extract_function(PANELS, "_kanbanPopulateModelSelect")
    assert "++_kanbanModelPopulateSeq" in populate_tok_src
    assert "seq !== _kanbanModelPopulateSeq" in populate_tok_src

    # A selection made while /api/models is still loading (create modal shows
    # immediately, populate fires un-awaited, custom model-ID works without the
    # catalog) must survive the load completing — the tail must not restore the
    # captured default over a live user selection.
    assert "if (sel.value) {" in populate_tok_src
    assert "_kanbanSyncModelChip();" in populate_tok_src

    # openKanbanEdit passes the persisted provider so the override pair survives
    # an unrelated edit unchanged.
    edit_src = extract_function(PANELS, "openKanbanEdit")
    assert "_kanbanPopulateModelSelect(task.model_override || '', task.provider_override || '')" in edit_src

    # The picker drives the shared renderModelDropdown() component with kanban ids.
    assert "function _kanbanOpenModelDropdown" in PANELS
    dropdown_src = extract_function(PANELS, "_kanbanOpenModelDropdown")
    assert "renderModelDropdown({" in dropdown_src
    assert "dropdownId: 'kanbanTaskModalModelDropdown'" in dropdown_src
    assert "selectId: 'kanbanTaskModalModel'" in dropdown_src

    # submitKanbanTaskModal decodes the model select through the shared
    # _modelStateForSelect() (which resolves the bare model + its data-provider,
    # see test_kanban_submit_decodes_picker_provider_prefix_out_of_model_override)
    # and sends both back as model_override/provider_override (create + edit).
    submit_match = re.search(
        r"function submitKanbanTaskModal\(\)\{(.*?)\n\}",
        PANELS, re.DOTALL,
    )
    assert submit_match, "submitKanbanTaskModal() not found"
    submit_body = submit_match.group(1)
    assert "kanbanTaskModalModel" in submit_body
    assert "_modelStateForSelect" in submit_body
    assert "payload.model_override" in submit_body
    assert "payload.provider_override" in submit_body

    # Wiring mounts the chip so it can open the picker.
    assert "_kanbanMountModelChip" in PANELS
    assert "accessKey" not in PANELS  # sanity: unused

    # New i18n keys exist (English block) for labels/hints.
    for key in ("kanban_model", "kanban_model_hint", "kanban_no_model_override"):
        assert f"{key}:" in I18N
    # The free-text-era keys must be gone.
    for key in ("kanban_provider_placeholder", "kanban_provider_hint",
                "kanban_provider_requires_model", "kanban_model_placeholder"):
        assert f"{key}:" not in I18N


# ── #6765 blocker: the model badge describes the NEXT dispatch, not the live run ──
#
# `model_override`/`provider_override` are dispatch-time INPUTS in the agent core:
# the dispatcher passes `-m <model> [--provider <provider>]` when it SPAWNS the
# worker, and `kanban_db.set_model_override()` documents that a change "only takes
# effect on the NEXT dispatch". There is no per-run model snapshot on `task_runs`,
# so the WebUI cannot know what an already-spawned worker is actually using.
#
# Production ordering that used to produce a false claim:
#   worker spawned with model A -> card edited to B -> _task_dict() returns B
#   immediately -> the card presented B as "executes with model B" while the live
#   worker was still A.
#
# Contract: the badge/field always describes the card's NEXT dispatch, and while
# the card is 'running' the wording says so explicitly.

_OLD_EXECUTION_CLAIMS = (
    "Executes with model",
    "Used for how this card executes",
    "how this card executes",
)


def _en_i18n_value(key: str) -> str:
    """Raw English value for a single-quoted i18n key (with \\uXXXX decoded)."""
    en_body = _locale_blocks_with_body(I18N)[0][1]
    m = re.search(rf"^\s*{re.escape(key)}: '(.*?)',$", en_body, re.M)
    assert m, f"English i18n block has no {key}"
    return m.group(1).encode("utf-8").decode("unicode_escape")


def _render_kanban_card(task: dict) -> str:
    """Run the real _kanbanCard() builder under node with the real English
    i18n strings, so the assertions cover the rendered DOM (not just source)."""
    import json
    import subprocess

    strings = {
        key: _en_i18n_value(key)
        for key in (
            "kanban_card_model_hint",
            "kanban_card_model_hint_running",
            "kanban_model_next_dispatch",
            "kanban_unassigned",
            "kanban_card_complete",
            "kanban_card_archive",
        )
    }
    fn_source = extract_function(PANELS, "_kanbanCard")
    script = (
        "const fnSource = " + json.dumps(fn_source) + ";\n"
        "const STRINGS = " + json.dumps(strings) + ";\n"
        "const task = " + json.dumps(task) + ";\n"
        # Faithful copy of i18n.js t(): {0}-style numbered placeholders.
        "const t = (key, ...args) => {\n"
        "  const val = STRINGS[key];\n"
        "  if (val === undefined) return key;\n"
        "  if (args.length) return String(val).replace(/\\{(\\d+)\\}/g, (m, i) => (\n"
        "    Object.prototype.hasOwnProperty.call(args, Number(i)) ? String(args[Number(i)]) : m));\n"
        "  return val;\n"
        "};\n"
        "const esc = (s) => String(s == null ? '' : s)\n"
        "  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')\n"
        "  .replace(/\\\"/g,'&quot;').replace(/'/g,'&#39;');\n"
        "const _kanbanTaskAge = () => '';\n"
        "const _kanbanTaskBody = (x) => x.body || '';\n"
        "const _kanbanRenderMarkdown = (x) => String(x || '');\n"
        "const _kanbanCardStalenessClass = () => '';\n"
        "const _kanbanTaskTitle = (x) => x.title || x.id || '';\n"
        "const _kanbanCardQuickActions = () => '';\n"
        "const out = {};\n"
        "new Function('out', fnSource + '; out.fn = _kanbanCard;')(out);\n"
        "console.log(JSON.stringify({html: out.fn(task, task.status || 'ready')}));\n"
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node -e failed: {result.stderr}"
    return json.loads(result.stdout)["html"]


def _model_badge_title(html: str) -> str:
    m = re.search(r'<span class="kanban-badge model" title="(.*?)">', html)
    assert m, f"no model badge found in rendered card: {html}"
    return m.group(1)


def test_kanban_running_card_model_badge_does_not_claim_the_active_run():
    """Production ordering: a worker was spawned with model A, then the RUNNING
    card's override was edited to B. The bridge returns B immediately, so the
    card must present B as what the NEXT dispatch will use — never as the model
    the active worker is executing with."""
    html = _render_kanban_card({
        "id": "t-run",
        "title": "rate-limited task",
        # 'running' is the real agent status literal (kanban_db.VALID_STATUSES)
        # and the same value _kanbanCardStalenessClass()/_kanbanCardQuickActions()
        # already branch on.
        "status": "running",
        "model_override": "gpt-5.6-sol",
        "provider_override": "openai",
    })
    title = _model_badge_title(html)
    # The badge still names the model...
    assert "🧠 gpt-5.6-sol" in html
    assert "gpt-5.6-sol" in title
    # ...but scoped to the NEXT dispatch, with the active run called out.
    assert "next dispatch" in title.lower(), title
    assert title == _en_i18n_value("kanban_card_model_hint_running").replace(
        "{0}", "gpt-5.6-sol"), title
    # And it must NOT reassert the old execution claim.
    for claim in _OLD_EXECUTION_CLAIMS:
        assert claim not in title, f"running card tooltip still claims {claim!r}"


def test_kanban_non_running_card_model_badge_is_next_dispatch_scoped():
    """A ready/queued card has no live worker at all, so the tooltip is the plain
    next-dispatch wording (still not an 'executes with' claim)."""
    for status in ("ready", "todo", "triage", "blocked"):
        html = _render_kanban_card({
            "id": "t-ready",
            "title": "queued task",
            "status": status,
            "model_override": "gpt-5.6-sol",
        })
        title = _model_badge_title(html)
        assert title == _en_i18n_value("kanban_card_model_hint").replace(
            "{0}", "gpt-5.6-sol"), (status, title)
        assert "next dispatch" in title.lower(), (status, title)
        for claim in _OLD_EXECUTION_CLAIMS:
            assert claim not in title, (status, claim)
        # The running-state wording must not leak onto a card with no live run.
        assert "running with the model it was dispatched with" not in title, status


def test_kanban_model_wording_is_dispatch_scoped_everywhere():
    """Source-level guard for the three display sites + the picker scope note."""
    # (a) English strings carry the dispatch-time semantics, not an execution claim.
    card_hint = _en_i18n_value("kanban_card_model_hint")
    running_hint = _en_i18n_value("kanban_card_model_hint_running")
    modal_hint = _en_i18n_value("kanban_model_hint")
    label = _en_i18n_value("kanban_model_next_dispatch")
    assert "next dispatched" in card_hint or "next dispatch" in card_hint, card_hint
    assert "{0}" in card_hint and "{0}" in running_hint
    assert "next dispatch" in running_hint, running_hint
    assert "next dispatch" in modal_hint, modal_hint
    assert "next dispatch" in label.lower(), label
    for value in (card_hint, running_hint, modal_hint, label):
        for claim in _OLD_EXECUTION_CLAIMS:
            assert claim not in value, f"{value!r} still carries {claim!r}"

    # (b) No stale hard-coded English fallback in panels.js may re-introduce it.
    for claim in _OLD_EXECUTION_CLAIMS:
        assert claim not in PANELS, f"panels.js still hard-codes {claim!r}"

    # (c) _kanbanCard picks the running-state tooltip off the real task status
    #     field (task.status === 'running'), not an invented one.
    card_src = extract_function(PANELS, "_kanbanCard")
    assert "task.status === 'running'" in card_src
    assert "kanban_card_model_hint_running" in card_src
    assert "kanban_card_model_hint'" in card_src

    # (d) meta bits + detail row are labelled as the next-dispatch model.
    meta_src = extract_function(PANELS, "_kanbanTaskMeta")
    assert "t('kanban_model_next_dispatch')" in meta_src
    detail_src = extract_function(PANELS, "_kanbanRenderTaskDetail")
    assert "t('kanban_model_next_dispatch')" in detail_src
    assert "t('kanban_model')" not in detail_src, (
        "the detail Model row must use the next-dispatch label, not the bare 'Model'"
    )
    # The detail row also carries the running-aware tooltip.
    assert "kanban_card_model_hint_running" in detail_src

    # (e) The picker scope note still comes from the (reworded) i18n key.
    dropdown_src = extract_function(PANELS, "_kanbanOpenModelDropdown")
    assert "t('kanban_model_hint')" in dropdown_src


def test_kanban_model_dispatch_keys_present_in_every_locale():
    """Locale parity: the reworded/new kanban model keys must exist in all 15
    locale blocks (partial locales fall back per-key to English, but these are
    correctness-critical wording, so keep them in parity)."""
    blocks = _locale_blocks_with_body(I18N)
    assert len(blocks) == 15, [code for code, _ in blocks]
    required = (
        "kanban_model_next_dispatch",
        "kanban_card_model_hint",
        "kanban_card_model_hint_running",
        "kanban_model_hint",
    )
    for code, body in blocks:
        for key in required:
            assert re.search(rf"^\s*{key}: '", body, re.M), f"{code} locale missing {key}"
        # The old execution claim must be gone from every locale.
        for claim in _OLD_EXECUTION_CLAIMS:
            assert claim not in body, f"{code} locale still carries {claim!r}"


# ── #6765 P1: the picker prefix must not leak into the persisted model_override ──
#
# `_ensureModelOptionInDropdown()` (static/ui.js) synthesizes an option for a
# provider-scoped / custom model ID whose *value* is the picker's internal
# `@<provider>:<model>` representation; the bare model survives only on
# `option.dataset.model` and the provider on `option.dataset.provider`. Reading
# `select.value` raw therefore persists `@provider:model` as the model override,
# and the dispatcher hands that prefixed string to the backend as the model id.
# `_modelStateForSelect()` is the composer's authoritative decoder for exactly
# this (it also handles colon-bearing model ids, #6221), so the kanban submit
# path must decode through it instead of trusting the raw value.


def _run_kanban_submit_model_cases(cases):
    """Run the REAL submitKanbanTaskModal() under node against a stubbed DOM,
    with the REAL _modelStateForSelect()/_getOptionProviderId()/
    _providerFromModelValue() from static/ui.js wired in — so the decode under
    test is production code, not a test reimplementation.

    Each case: {mode, editingId?, options: [{value, model?, provider?}],
    selected: <select value>}. Returns the captured request payloads."""
    import json
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")

    submit_src = extract_function(PANELS, "submitKanbanTaskModal", prefix="async function")
    state_src = extract_function(UI, "_modelStateForSelect")
    option_provider_src = extract_function(UI, "_getOptionProviderId")
    value_provider_src = extract_function(UI, "_providerFromModelValue")

    harness = (
        "const CASES = " + json.dumps(cases) + ";\n"
        # Minimal <option>/<select> stubs shaped like the real DOM surface the
        # decoder touches: option.value + option.dataset, select.options and a
        # live selectedOptions derived from select.value.
        "function makeOption(spec) {\n"
        "  const dataset = {};\n"
        "  if (spec.model !== undefined) dataset.model = spec.model;\n"
        # A real DOMStringMap reads back an ABSENT data-provider as undefined and
        # an explicitly-emptied one as '' — mirror that distinction exactly.
        "  if (spec.provider !== undefined) dataset.provider = spec.provider;\n"
        "  const group = spec.groupProvider === undefined ? null\n"
        "    : {tagName: 'OPTGROUP', dataset: {provider: spec.groupProvider}};\n"
        "  return {value: spec.value, dataset, parentElement: group};\n"
        "}\n"
        "function makeSelect(specs, selected) {\n"
        "  const options = specs.map(makeOption);\n"
        "  return {\n"
        "    id: 'kanbanTaskModalModel', value: selected, options,\n"
        "    get selectedOptions() {\n"
        "      const hit = options.find(o => String(o.value) === String(this.value));\n"
        "      return hit ? [hit] : [];\n"
        "    },\n"
        "    focus() {},\n"
        "  };\n"
        "}\n"
        "function field(value) { return {value, dataset: {}, focus() {}}; }\n"
        "let elements = {};\n"
        "global.document = {getElementById: (id) => elements[id] || null};\n"
        "function t(k) { return k; }\n"
        "let capturedPayload = null;\n"
        "let capturedMethod = null;\n"
        "async function api(url, opts) {\n"
        "  capturedMethod = opts && opts.method;\n"
        "  capturedPayload = opts ? JSON.parse(opts.body) : null;\n"
        "  return {task: {id: 't_saved'}};\n"
        "}\n"
        "async function loadKanban() {}\n"
        "async function loadKanbanTask() {}\n"
        "function _kanbanBoardQuery() { return ''; }\n"
        "function closeKanbanTaskModal() {}\n"
        "let _kanbanTaskModalMode = 'create';\n"
        "let _kanbanTaskModalEditingId = null;\n"
        "let _kanbanTaskModalInitialDisplayedStatus = 'triage';\n"
        + value_provider_src + "\n"
        + option_provider_src + "\n"
        + state_src + "\n"
        + submit_src + "\n"
        "(async () => {\n"
        "  const out = [];\n"
        "  for (const c of CASES) {\n"
        "    capturedPayload = null; capturedMethod = null;\n"
        "    _kanbanTaskModalMode = c.mode;\n"
        "    _kanbanTaskModalEditingId = c.editingId || null;\n"
        "    elements = {\n"
        "      kanbanTaskModalTitleInput: field('Prefixed model task'),\n"
        "      kanbanTaskModalBody: field(''),\n"
        "      kanbanTaskModalStatus: field('triage'),\n"
        "      kanbanTaskModalAssignee: field('agent1'),\n"
        "      kanbanTaskModalTenant: field(''),\n"
        "      kanbanTaskModalPriority: field('0'),\n"
        "      kanbanTaskModalWorkspaceKind: field('scratch'),\n"
        "      kanbanTaskModalWorkspacePath: field(''),\n"
        "      kanbanTaskModalSkills: field(''),\n"
        "      kanbanTaskModalMaxRuntimeSeconds: field(''),\n"
        "      kanbanTaskModalParents: field(''),\n"
        "      kanbanTaskModalModel: makeSelect(c.options, c.selected),\n"
        "      kanbanTaskModalError: {textContent: '', dataset: {}},\n"
        "      kanbanTaskModalSubmit: {disabled: false},\n"
        "    };\n"
        "    await submitKanbanTaskModal();\n"
        "    out.push({payload: capturedPayload, method: capturedMethod,\n"
        "              error: elements.kanbanTaskModalError.textContent});\n"
        "  }\n"
        "  console.log(JSON.stringify(out));\n"
        "})().catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });\n"
    )
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node -e failed: {result.stderr}"
    return json.loads(result.stdout)


def test_kanban_submit_decodes_picker_provider_prefix_out_of_model_override():
    """#6765 P1: a provider-scoped selection whose option was SYNTHESIZED by
    _ensureModelOptionInDropdown carries the internal '@provider:model' string as
    its option value. submitKanbanTaskModal must persist the bare model id (and
    the provider separately) — never the prefixed picker representation, which
    the dispatcher would pass to the backend verbatim as `-m @provider:model`."""
    synthesized = [
        # Exactly what _ensureModelOptionInDropdown() appends: prefixed value,
        # bare model on data-model, provider on data-provider.
        {"value": "@custom:backup:model-a", "model": "model-a", "provider": "custom:backup"},
    ]
    # A colon-bearing model id under a colon-bearing provider — the #6221 shape
    # that naive "split at the last colon" parsing mangles. The decoder must
    # read data-model/data-provider, not re-parse the value.
    colon_model = [
        {"value": "@custom:backup:model-a:free", "model": "model-a:free", "provider": "custom:backup"},
    ]
    # A plain catalog option (bare value, provider on the option) — the path that
    # already worked; it must keep working.
    catalog = [{"value": "gpt-5.6-sol", "provider": "openai"}]

    create, edit, create_colon, edit_colon, create_catalog, edit_catalog, \
        create_cleared, edit_cleared = _run_kanban_submit_model_cases([
            {"mode": "create", "options": synthesized, "selected": "@custom:backup:model-a"},
            {"mode": "edit", "editingId": "t_1", "options": synthesized,
             "selected": "@custom:backup:model-a"},
            {"mode": "create", "options": colon_model, "selected": "@custom:backup:model-a:free"},
            {"mode": "edit", "editingId": "t_1", "options": colon_model,
             "selected": "@custom:backup:model-a:free"},
            {"mode": "create", "options": catalog, "selected": "gpt-5.6-sol"},
            {"mode": "edit", "editingId": "t_1", "options": catalog, "selected": "gpt-5.6-sol"},
            {"mode": "create", "options": synthesized, "selected": ""},
            {"mode": "edit", "editingId": "t_1", "options": synthesized, "selected": ""},
        ])

    # ── create: the prefix is decoded away before the POST ──
    assert create["method"] == "POST", create
    assert create["payload"]["model_override"] == "model-a", create["payload"]
    assert create["payload"]["provider_override"] == "custom:backup", create["payload"]
    assert not create["payload"]["model_override"].startswith("@"), (
        "the picker's @provider: prefix leaked into the persisted model_override"
    )

    # ── edit: same decode, and both fields are always sent ──
    assert edit["method"] == "PATCH", edit
    assert edit["payload"]["model_override"] == "model-a", edit["payload"]
    assert edit["payload"]["provider_override"] == "custom:backup", edit["payload"]
    assert not edit["payload"]["model_override"].startswith("@"), edit["payload"]

    # ── colon-bearing model id survives intact under a colon-bearing provider ──
    for label, case in (("create", create_colon), ("edit", edit_colon)):
        assert case["payload"]["model_override"] == "model-a:free", (label, case["payload"])
        assert case["payload"]["provider_override"] == "custom:backup", (label, case["payload"])

    # ── plain catalog pick unchanged (no prefix to strip, provider preserved) ──
    for label, case in (("create", create_catalog), ("edit", edit_catalog)):
        assert case["payload"]["model_override"] == "gpt-5.6-sol", (label, case["payload"])
        assert case["payload"]["provider_override"] == "openai", (label, case["payload"])

    # ── empty selection: create omits both keys, edit clears both to null ──
    assert "model_override" not in create_cleared["payload"], create_cleared["payload"]
    assert "provider_override" not in create_cleared["payload"], create_cleared["payload"]
    assert edit_cleared["payload"]["model_override"] is None, edit_cleared["payload"]
    assert edit_cleared["payload"]["provider_override"] is None, edit_cleared["payload"]


def test_kanban_submit_uses_the_shared_model_decoder_not_a_raw_value_read():
    """Source guard: the submit path must go through _modelStateForSelect (the
    composer's single authoritative decoder) rather than re-reading
    select.value / selectedOptions[0].dataset.provider directly, so the kanban
    picker can never drift from the composer's resolution."""
    submit_src = extract_function(PANELS, "submitKanbanTaskModal", prefix="async function")
    assert "_modelStateForSelect" in submit_src, (
        "submitKanbanTaskModal must decode the selection through _modelStateForSelect"
    )
    # The raw-value reads that caused #6765 P1 must be gone.
    assert "modelEl.value.trim()" not in submit_src, (
        "submitKanbanTaskModal still reads the picker's raw (possibly @provider:-"
        "prefixed) value as the model override"
    )


def test_kanban_submit_still_does_not_repin_a_provider_the_task_never_had():
    """Guard on the one place the shared resolver diverges from the old raw read
    (5be181a0): _kanbanPopulateModelSelect clears the matched option's OWN
    data-provider to '' to mean "this task has no persisted provider pin", but
    the option still lives under a catalog <optgroup data-provider=...> whose
    provider _getOptionProviderId() would happily inherit. Saving an unrelated
    edit must keep provider_override cleared rather than pinning the catalog
    provider onto a task that never had one."""
    cleared_pin = [{
        "value": "gpt-5.6-sol",
        # Explicitly emptied own pin (a real DOMStringMap reads '' back, not undefined)...
        "provider": "",
        # ...while the enclosing catalog group still names a provider.
        "groupProvider": "openai",
    }]
    # Sanity contrast: when the task DOES carry a persisted pin, it is preserved.
    kept_pin = [{"value": "gpt-5.6-sol", "provider": "openai", "groupProvider": "openai"}]

    edit_cleared, create_cleared, edit_kept = _run_kanban_submit_model_cases([
        {"mode": "edit", "editingId": "t_1", "options": cleared_pin, "selected": "gpt-5.6-sol"},
        {"mode": "create", "options": cleared_pin, "selected": "gpt-5.6-sol"},
        {"mode": "edit", "editingId": "t_1", "options": kept_pin, "selected": "gpt-5.6-sol"},
    ])

    # The model is still sent; only the un-pinned provider stays un-pinned.
    assert edit_cleared["payload"]["model_override"] == "gpt-5.6-sol", edit_cleared["payload"]
    assert edit_cleared["payload"]["provider_override"] is None, (
        "an unrelated edit re-pinned the catalog optgroup's provider onto a task "
        "that has no persisted provider_override"
    )
    assert create_cleared["payload"]["model_override"] == "gpt-5.6-sol", create_cleared["payload"]
    assert "provider_override" not in create_cleared["payload"], create_cleared["payload"]
    assert edit_kept["payload"]["provider_override"] == "openai", edit_kept["payload"]
