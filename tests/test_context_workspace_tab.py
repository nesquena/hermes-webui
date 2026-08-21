# Focused static coverage for the workspace-panel Context tab and the
# "Goal finish" button (feature: context brief in the workspace panel).
import json
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
WORKSPACE = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
UI = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_workspace_context_tab_markup_present():
    assert 'id="workspaceContextTab"' in INDEX
    assert "switchWorkspacePanelTab('context')" in INDEX
    assert 'id="workspaceContextPanel"' in INDEX
    assert 'data-i18n="tab_context"' in INDEX


def test_switch_workspace_panel_tab_handles_context():
    assert "tab === 'context' ? 'context'" in WORKSPACE
    assert "$('workspaceContextTab')" in WORKSPACE
    assert "$('workspaceContextPanel')" in WORKSPACE
    assert "loadWorkspaceContextBrief()" in WORKSPACE


def test_workspace_brief_loader_reuses_shared_loader():
    assert "async function loadWorkspaceContextBrief(force)" in PANELS
    assert "_loadBriefInto($('workspaceContextPanel'), force)" in PANELS
    assert "async function _loadBriefInto(panel, force)" in PANELS
    assert "renderContextBrief(data && data.brief, panel)" in PANELS
    # brief payload retained on the host element for the goal button
    assert "panel._briefData = brief" in PANELS


def test_loader_uses_per_panel_sequence():
    # Two visible panels must not cancel each other's in-flight responses.
    assert "panel._briefReqSeq = (panel._briefReqSeq || 0) + 1" in PANELS
    assert "_contextBriefReqSeq" not in PANELS


def test_context_tab_hides_file_chrome_via_css():
    # Without a data-active-tab="context" rule the file tree stayed visible
    # under the brief (independent review finding).
    assert '.rightpanel[data-active-tab="context"]' in STYLE
    rule = STYLE.split('.rightpanel[data-active-tab="context"]', 1)[1]
    assert "#wsEmptyState" in rule
    assert ".file-tree" in rule


def test_goal_finish_button_and_handler():
    assert PANELS.count("context_goal_finish") >= 2  # both llm variants
    assert "async function _contextBriefGoalFinish(btn)" in PANELS
    assert "async function _hydrateContextBriefGoalFinish(sid, goalText)" in PANELS
    assert "await cmdGoal(goalText)" in PANELS
    assert "it.status === 'pending' || it.status === 'in_progress'" in PANELS
    # non-blocking app dialog, never the browser-native confirm
    assert "showConfirmDialog({" in PANELS
    assert "window.confirm" not in PANELS
    assert "context_goal_finish_none" in PANELS


def _goal_finish_handler_body() -> str:
    return PANELS.split("async function _contextBriefGoalFinish", 1)[1].split(
        "\nasync function _pollContextBriefJob", 1
    )[0]


def test_goal_finish_hydrates_via_cmd_goal_not_raw_post():
    # Raw POST /api/goal starts the server turn but leaves the open tab idle
    # until reload. Goal finish must reuse composer cmdGoal so SSE attaches.
    body = _goal_finish_handler_body()
    assert "await _hydrateContextBriefGoalFinish(sid, goalText)" in body
    assert "await cmdGoal(goalText)" in body
    assert "api('/api/goal'" not in body
    assert "role:'user'" in body
    assert "content:goalText" in body
    assert "renderMessages()" in body


def test_goal_finish_guards_stale_session():
    # Never post todos from a stale panel into a different conversation.
    body = PANELS.split("async function _contextBriefGoalFinish", 1)[1]
    assert "host.dataset.briefSid !== sid" in body
    assert "context_goal_finish_stale" in body


def test_goal_finish_i18n_all_locales():
    for key in (
        "context_goal_finish:",
        "context_goal_finish_prefix:",
        "context_goal_finish_confirm:",
        "context_goal_finish_none:",
        "context_goal_finish_started:",
        "context_goal_finish_stale:",
    ):
        assert I18N.count(key) >= 3, key  # en + fr + zh-Hant


def test_goal_finish_styles():
    assert ".ctx-brief-actions" in STYLE
    assert ".ctx-brief-btn-goal" in STYLE


def test_workspace_context_refresh_hooks():
    # Non-forced refresh: the briefLoaded guard dedupes (no POST per render).
    assert UI.count("loadWorkspaceContextBrief()") >= 2
    assert "loadWorkspaceContextBrief(true)" not in UI


def _extract_hydrate_fn() -> str:
    start = PANELS.index("async function _hydrateContextBriefGoalFinish")
    end = PANELS.index("\nasync function _pollContextBriefJob")
    return PANELS[start:end]


def test_hydrate_context_brief_goal_finish_echoes_prompt_and_calls_cmd_goal():
    fn_src = _extract_hydrate_fn()
    script = f"""
const vm = require('vm');
const fnSrc = {json.dumps(fn_src)};
const calls = [];
const S = {{ session: {{ session_id: 'sid-1' }}, messages: [] }};
const ctx = {{
  S,
  Date,
  cmdGoal: async (args) => {{ calls.push({{cmd: 'cmdGoal', args}}); }},
  renderMessages: () => {{ calls.push({{cmd: 'renderMessages'}}); }},
  showToast: (msg) => {{ calls.push({{cmd: 'toast', msg}}); }},
  t: (key) => key,
}};
vm.createContext(ctx);
vm.runInContext(fnSrc, ctx);
(async () => {{
  const ok = await vm.runInContext(
    `_hydrateContextBriefGoalFinish('sid-1', 'Finish remaining work')`,
    ctx
  );
  const skipped = await vm.runInContext(
    `_hydrateContextBriefGoalFinish('other-sid', 'should not echo')`,
    ctx
  );
  process.stdout.write(JSON.stringify({{
    ok, skipped,
    messages: S.messages,
    calls,
  }}));
}})().catch((err) => {{
  console.error(err && err.stack || err);
  process.exit(1);
}});
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        proc = subprocess.run(
            ["node", str(script_path)],
            check=True,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
    finally:
        script_path.unlink(missing_ok=True)
    result = json.loads(proc.stdout)
    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["messages"][0]["role"] == "user"
    assert result["messages"][0]["content"] == "Finish remaining work"
    assert result["calls"][0]["cmd"] == "renderMessages"
    assert result["calls"][1] == {"cmd": "cmdGoal", "args": "Finish remaining work"}
    assert result["calls"][2] == {"cmd": "cmdGoal", "args": "should not echo"}
    assert len(result["messages"]) == 1


def test_goal_finish_force_refreshes_brief_before_composing():
    # Review #7000 finding 2: the cached brief can predate same-session todo
    # changes; the handler must force-fetch before reading actionable todos.
    m = re.search(r"async function _contextBriefGoalFinish\(btn\)\{(.*?)\n\}", PANELS, re.S)
    assert m, "handler not found"
    body = m.group(1)
    assert "await _loadBriefInto(host, true);" in body, "no forced refresh before composing the goal"
    compose = body.index("const brief = host._briefData;")
    refresh = body.index("await _loadBriefInto(host, true);")
    assert refresh < compose, "refresh must happen before reading _briefData"


def test_loader_drops_brief_data_at_load_start():
    # Review #7000 finding 2: actionable payload is invalidated as soon as a
    # (re)load starts, so no consumer can act on data older than that attempt.
    m = re.search(r"async function _loadBriefInto\(panel, force\)\{(.*?)\n\}", PANELS, re.S)
    assert m, "loader not found"
    body = m.group(1)
    cleared = body.index("panel.dataset.briefLoaded = '';")
    nulled = body.index("panel._briefData = null;")
    assert cleared < nulled, "_briefData must be dropped with the load start"


def test_goal_finish_revalidates_session_after_confirm_dialog():
    # The confirm dialog is async: a session switch while it is open must not
    # start the goal in a different conversation than the brief it came from.
    m = re.search(r"async function _contextBriefGoalFinish\(btn\)\{(.*?)\n\}", PANELS, re.S)
    body = m.group(1)
    dialog = body.index("await showConfirmDialog(")
    reval = body.index("_contextBriefSid() !== sid", dialog)
    call = body.index("_hydrateContextBriefGoalFinish(sid, goalText)")
    assert dialog < reval < call, "missing post-dialog session revalidation"


def test_context_tab_hides_preview_area():
    # Review #7000 finding 3: an open file preview must not leak into the
    # Context tab (.preview-area.visible is display:flex).
    rule = '.rightpanel[data-active-tab="context"]'
    assert f'{rule} #previewArea' in STYLE
    assert f'{rule} .preview-area.visible' in STYLE
