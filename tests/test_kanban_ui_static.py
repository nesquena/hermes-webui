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
    locale_blocks = re.findall(r"\n\s*([a-z]{2}(?:-[A-Z]{2})?): \{(.*?)\n\s*\},", I18N, flags=re.S)
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
    locale_blocks = re.findall(r"\n\s*([a-z]{2}(?:-[A-Z]{2})?): \{(.*?)\n\s*\},", I18N, flags=re.S)
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
