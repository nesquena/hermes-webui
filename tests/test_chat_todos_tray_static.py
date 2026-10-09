"""Static frontend tests for the in-chat todos tray (feat/todos-in-chat).

These verify that the chat-embedded task tray wiring stays intact:
  - the tray DOM exists in index.html next to the message shell
  - the settings checkbox exists and is wired in loadSettingsPanel
  - the scheduler fans out to renderChatTodos on every todo_state refresh
  - the render path escapes user content and marks terminal states
  - the rail-hide helper targets [data-panel="todos"] with nav-tab-hidden
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


def _read_static(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_chat_todos_tray_markup_exists_in_message_shell():
    idx = _read_static("static/index.html")
    assert 'id="chatTodosPanel"' in idx
    assert 'id="chatTodosHead"' in idx
    assert 'id="chatTodosSummary"' in idx
    assert 'id="chatTodosBody"' in idx
    # ONE progress label (reviewer re-gate 2026-10-07T18:08:02Z): the separate
    # counter span duplicated the same active count, so it must be gone.
    assert 'id="chatTodosCounter"' not in idx
    # The tray must live inside the messages shell, before the messages node,
    # so it stays pinned at the top of the chat area (not inside the scroller).
    shell = idx.find('class="messages-shell"')
    tray = idx.find('id="chatTodosPanel"')
    messages = idx.find('id="messages"')
    assert shell != -1 and tray != -1 and messages != -1
    assert shell < tray < messages


def test_chat_todos_settings_checkbox_wired_in_settings_panel():
    idx = _read_static("static/index.html")
    panels = _read_static("static/panels.js")

    assert 'id="settingsChatTodosInChat"' in idx
    assert 'settings_label_chat_todos_in_chat' in idx
    assert 'settings_desc_chat_todos_in_chat' in idx
    assert "const chatTodosCb=$('settingsChatTodosInChat')" in panels
    assert "_chatTodosToggleEnabled(this.checked)" in panels
    assert "typeof chatTodosEnabled==='function'" in panels


def test_chat_todos_checkbox_handler_does_not_autosave_the_appearance_payload():
    """Reviewer re-gate 2026-10-08T23:19:27Z (static/panels.js:9355): the
    Settings tray checkbox only drives the tray preference (localStorage); the
    appearance autosave it used to schedule POSTed this client's stale
    hidden_tabs mirror and overwrote a newer server preference set by another
    client (the reviewer verified it through real HTTP). An explicit
    visibility-chip edit keeps its own autosave."""
    panels = _read_static("static/panels.js")
    handler = _extract(
        panels,
        "const chatTodosCb=$('settingsChatTodosInChat');",
        "const autoScrollFollowCb=",
    )
    assert "_chatTodosToggleEnabled(this.checked)" in handler
    assert "_scheduleAppearanceAutosave" not in handler, (
        "the tray checkbox handler must not autosave the appearance payload"
    )
    # Positive control: the explicit visibility-chip edit still autosaves.
    chip = _extract(
        panels,
        "function _toggleTabVisibilityChip(panel){",
        "function _toggleDashboardVisibilityChip",
    )
    assert "_scheduleAppearanceAutosave()" in chip


def test_i18n_keys_registered_in_english_locale():
    i18n = _read_static("static/i18n.js")
    assert "settings_label_chat_todos_in_chat: 'Show task list in chat'" in i18n
    assert "settings_desc_chat_todos_in_chat: 'Show a collapsible task list" in i18n


def test_scheduler_fans_out_to_chat_todos_renderer():
    ui = _read_static("static/ui.js")
    block_start = ui.find("function scheduleTodosRefresh()")
    block_end = ui.find("function _resetTodosRenderCache()", block_start)
    assert block_start != -1 and block_end != -1
    scheduler = ui[block_start:block_end]
    # Both the non-RAF fallback and the RAF path must call renderChatTodos.
    assert scheduler.count("renderChatTodos()") >= 2


def test_chat_todos_renderer_escapes_content_and_marks_terminal_states():
    ui = _read_static("static/ui.js")
    start = ui.find("function renderChatTodos()")
    end = ui.find("function toggleChatTodos()", start)
    assert start != -1 and end != -1
    render = ui[start:end]

    # Guard against running in non-DOM contexts (node VM tests).
    assert "typeof $!=='function'" in render
    # The tray renders through the ONE shared row renderer instead of hand-rolling
    # its own markup (reviewer re-gate 2026-10-08T03:10:50Z, "tidy the shared-row
    # renderer"), so escaping + terminal-state styling live there now.
    assert (
        "renderTodoRows(todos,{metadata:false,compact:true,rowClass:'chat-todos-row'})" in render
    )
    shared = _extract_row_renderer(ui)
    assert "esc(todoContent(td))" in shared
    assert "line-through" in shared
    assert "status==='completed'" in shared and "status==='cancelled'" in shared
    # Summary/counter derive from statuses, not from message text.
    assert "status!=='completed'" in ui and "status!=='cancelled'" in ui


def _extract_row_renderer(ui: str) -> str:
    """The shared renderTodoRow body (the one renderer all three surfaces use)."""
    start = ui.find("function renderTodoRow(todo,options={}){")
    end = ui.find("function renderTodoRows(", start)
    assert start != -1 and end != -1
    return ui[start:end]


def test_rail_hide_helper_targets_todos_panel_and_bounces_to_chat():
    ui = _read_static("static/ui.js")
    start = ui.find("function _syncChatTodosRailVisibility()")
    end = ui.find("function _chatTodosToggleEnabled", start)
    assert start != -1 and end != -1
    helper = ui[start:end]

    assert 'querySelectorAll(\'[data-panel="todos"]\')' in helper
    assert "nav-tab-hidden" in helper
    assert "switchPanel('chat'" in helper


def test_chat_todos_pref_is_opt_in_by_default():
    # Maintainer review 2026-10-07T10:30:07Z: "Default the tray to OFF (opt-in)
    # ... On upgrade every existing user loses the sidebar Todos tab and gets a
    # floating overlay in the transcript, while the code comment says opt-in."
    ui = _read_static("static/ui.js")
    start = ui.find("function _chatTodosReadPref()")
    end = ui.find("function _chatTodosWritePref", start)
    assert start != -1 and end != -1
    pref = ui[start:end]

    assert "if(v===null) return false;" in pref  # default: opt-in / OFF
    assert "return v==='1';" in pref
    assert "return true" not in pref

    idx = _read_static("static/index.html")
    # The boot IIFE may only pre-hide the sidebar Todos tab when the user
    # explicitly opted in; a missing key must leave the tab alone.
    assert "if(ct==='1'&&p.indexOf('todos')===-1)p.push('todos')" in idx
    assert "ct!=='0'" not in idx


def test_chat_todos_pref_persists_explicit_disabled():
    # Review blocker: toggling OFF then reloading must stay OFF. The writer must
    # store an explicit '0' instead of removing the key (which would collide
    # with the null => enabled first-use default).
    ui = _read_static("static/ui.js")
    start = ui.find("function _chatTodosWritePref")
    end = ui.find("function chatTodosEnabled()", start)
    assert start != -1 and end != -1
    writer = ui[start:end]

    assert "localStorage.setItem(CHAT_TODOS_LS_KEY,v?'1':'0')" in writer
    assert "removeItem(CHAT_TODOS_LS_KEY)" not in writer


def test_chat_todos_aria_initial_collapsed():
    idx = _read_static("static/index.html")
    # Tray markup is hidden + collapsed by default; ARIA must match.
    assert 'id="chatTodosHead"' in idx
    head_start = idx.find('id="chatTodosHead"')
    # The head button's initial aria-expanded must be false (collapsed) and
    # must own the body region for a11y tree correctness.
    head_snippet = idx[head_start - 200 : head_start + 400]
    assert 'aria-expanded="false"' in head_snippet
    assert 'aria-controls="chatTodosBody"' in head_snippet
    # toggleChatTodos must flip aria-expanded to stay in sync.
    ui = _read_static("static/ui.js")
    toggle_start = ui.find("function toggleChatTodos()")
    assert toggle_start != -1
    toggle = ui[toggle_start : toggle_start + 400]
    assert "_syncChatTodosExpanded(isOpen)" in toggle
    # ...through the one shared helper, so the settings toggle path cannot
    # leave a stale aria-expanded behind (maintainer review 2026-10-07).
    helper_start = ui.find("function _syncChatTodosExpanded(")
    assert helper_start != -1
    helper = ui[helper_start : helper_start + 500]
    assert "setAttribute('aria-expanded',open?'true':'false')" in helper


def test_chat_todos_toggle_off_then_on_clears_aria_expanded():
    # [SILENT] finding: expanding the tray, turning the preference off, then on
    # again removed `open` but left aria-expanded="true" on the header.
    ui = _read_static("static/ui.js")
    start = ui.find("function _chatTodosToggleEnabled(")
    end = ui.find("function _chatTodosSummary(", start)
    assert start != -1 and end != -1
    block = ui[start:end]

    assert "_syncChatTodosExpanded(false)" in block
    # The enable path must also (re)start collapsed.
    assert "tray.hidden=!checked" in block


def test_chat_todos_toggle_does_not_rebuild_the_transcript():
    # [SHOULD-FIX] "Turning the tray on re-renders the whole transcript ...
    # took 256 ms at 300 messages." The tray is an independent strip, so
    # toggling it must not call renderMessages().
    ui = _read_static("static/ui.js")
    start = ui.find("function _chatTodosToggleEnabled(")
    end = ui.find("function _chatTodosSummary(", start)
    assert start != -1 and end != -1
    block = ui[start:end]

    assert "renderMessages(" not in block


def test_chat_todos_dead_force_hidden_and_progress_css_are_removed():
    ui = _read_static("static/ui.js")
    css = _read_static("static/style.css")

    assert "_chatTodosForceHidden" not in ui
    assert ".chat-todos-progress" not in css


def test_chat_todos_content_wraps_long_unbroken_tokens():
    css = _read_static("static/style.css")
    ui = _read_static("static/ui.js")
    # Long unbroken tokens must not blow the row out. The wrap rule now lives in
    # the shared row renderer, so every Todos surface (sidebar panel, workspace
    # tab, in-chat tray) inherits it from one place.
    shared = _extract_row_renderer(ui)
    assert "overflow-wrap:anywhere" in shared
    assert ".chat-todos-row .todos-content" not in css
    assert "@media(prefers-reduced-motion:reduce){.chat-todos-head{transition:none;}" in css


def test_chat_todos_tray_strings_are_localized():
    ui = _read_static("static/ui.js")
    start = ui.find("function _chatTodosSummary(")
    end = ui.find("function toggleChatTodos()", start)
    assert start != -1 and end != -1
    block = ui[start:end]

    assert "t('todos_tray_summary_done',total)" in block
    assert "t('todos_tray_summary_active',active,total)" in block
    # No hardcoded English summaries survive.
    assert "'All done'" not in block
    assert "running`" not in block

    idx = _read_static("static/index.html")
    # ONE progress label: the summary span is generated text, so applyLocaleToDOM
    # must not own it — no data-i18n on it (reviewer re-gate 2026-10-07T18:08:02Z)
    # — and the duplicated counter span is gone.
    assert 'id="chatTodosSummary" data-i18n' not in idx
    assert 'id="chatTodosCounter"' not in idx
    assert "todos_tray_open_count" not in ui
    assert "todos_tray_open_count" not in _read_static("static/i18n.js")


def test_chat_todos_closed_chevron_points_down():
    # Every other collapsed disclosure in the app points down when closed
    # (reviewer re-gate 2026-10-07T18:08:02Z).
    idx = _read_static("static/index.html")
    head = idx[idx.find('id="chatTodosChevron"') :]
    snippet = head[: head.find("</span>")]
    assert '<polyline points="6 9 12 15 18 9"/>' in snippet  # down when closed
    assert '<polyline points="18 15 12 9 6 15"/>' not in snippet
    css = _read_static("static/style.css")
    # ...and it flips to up when the tray is open.
    assert ".chat-todos.open .chat-todos-chevron{transform:rotate(180deg);}" in css


def test_chat_todos_chip_reflects_the_tray_forced_hide():
    # [SHOULD-FIX] "With the tray on, the chip reports ON while the tab is
    # hidden, and two clicks leave it ON with the tab still hidden."
    panels = _read_static("static/panels.js")
    assert "function _tabVisibilityChipForcedOff(panel){" in panels
    assert "return panel==='todos'&&typeof chatTodosEnabled==='function'&&chatTodosEnabled();" in panels
    assert "var isOff=hidden.indexOf(panel)!==-1||_tabVisibilityChipForcedOff(panel);" in panels
    chip_start = panels.find("function _toggleTabVisibilityChip(panel)")
    chip_end = panels.find("function _toggleDashboardVisibilityChip", chip_start)
    assert chip_start != -1 and chip_end != -1
    handler = panels[chip_start:chip_end]
    assert "if(_tabVisibilityChipForcedOff(panel)){" in handler
    assert "_chatTodosToggleEnabled(false)" in handler


def test_chat_todos_hidden_tab_collision():
    # Desktop absolute tray: the sidebar Todos nav entry must stay hidden
    # whenever the in-chat tray is enabled, regardless of the per-profile
    # hidden_tabs setting. Otherwise a profile switch can restore it.
    panels = _read_static("static/panels.js")
    idx = _read_static("static/index.html")
    # panels.js re-applies the preference inside the applied-visibility pass.
    assert "if(panel==='todos'&&chatTodosOn) shouldHide=true;" in panels
    assert "chatTodosOn=(typeof chatTodosEnabled==='function'?chatTodosEnabled():false)" in panels
    # The synchronous boot IIFE in index.html must also hide the Todos tab
    # before first paint when the preference is default/enabled.
    assert "hermes-webui-chat-todos" in idx
    assert "p.indexOf('todos')===-1)p.push('todos')" in idx


def test_chat_todos_i18n_keys_in_all_locales():
    src = _read_static("static/i18n.js")
    # Extract en block keys that are chat-todos specific
    expected = {
        "settings_label_chat_todos_in_chat",
        "settings_desc_chat_todos_in_chat",
        # The tray-owned workspace-tab field keeps its visible-but-disabled note
        # (reviewer re-gate 2026-10-08T03:10:50Z).
        "settings_note_workspace_todos_tab_disabled",
        # Tray strings moved behind t() (maintainer review 2026-10-07).
        "todos_tray_summary_active",
        "todos_tray_summary_done",
    }
    # LOCALES segmentation: each locale starts at "  <code>: {" and ends before
    # the next locale header. Using header boundaries avoids a fragile
    # balanced-brace scan that trips on `${...}` template literals inside i18n
    # (many _label helpers contain them). The same contract is verified by the
    # per-locale parity tests (test_chinese_locale.py etc.) which use a full
    # quote-aware extractor — here we assert presence of the 6 chat-todos keys.
    header_re = re.compile(r"^\s+'?([a-zA-Z-]+)'?\s*:\s*\{", re.MULTILINE)
    locale_headers = [
        (m.start(), m.group(1))
        for m in header_re.finditer(src)
        if "_lang" in src[m.end() : m.end() + 800]
    ]
    assert len(locale_headers) >= 14, f"expected >=14 locales, got {locale_headers}"
    for i, (start, locale_key) in enumerate(locale_headers):
        end = locale_headers[i + 1][0] if i + 1 < len(locale_headers) else len(src)
        block = src[start:end]
        missing = sorted(k for k in expected if k not in block)
        assert not missing, f"{locale_key} missing chat-todos keys: {missing}"


def test_chat_todos_desktop_tray_is_an_in_flow_strip():
    # Reviewer re-gate 2026-10-07T18:08:02Z: "Put the desktop tray in the flow,
    # like the phone layout already is." A collapsed ~35px strip owns the top
    # band of .messages-shell and pushes the scroller down; nothing floats over
    # the transcript and the alignment variants are gone.
    css = _read_static("static/style.css")
    assert ".chat-todos{flex:0 0 auto;width:100%;" in css
    assert ".chat-todos{position:absolute" not in css
    assert "data-align" not in css
    assert (
        ".chat-todos-head{display:flex;align-items:center;gap:8px;width:100%;min-height:35px;" in css
    )
    # Expanded, the body grows in place inside the same flex item, capped by a
    # width AND a height term so a short / landscape viewport cannot let the tray
    # swallow the transcript (reviewer re-gate 2026-10-08T08:44:28Z, ask 3).
    assert (
        ".chat-todos-body{max-height:min(240px,40vh);overflow-y:auto;border-top:1px solid var(--border);" in css
    )
    # The floating Start jump pill is anchored to the shell's top-right, so it
    # must drop below the strip instead of painting over it — by the strip's
    # LIVE height, because a fixed offset only cleared the collapsed band and
    # let the pill cover the expanded task rows (re-gate 2026-10-07T20:13:25Z).
    assert (
        ".messages-shell.chat-todos-visible #jumpToSessionStartBtn{top:calc(var(--chat-todos-h,36px) + 7px);}"
        in css
    )
    # The shell marker class is driven from the render path, not by hand, and it
    # also publishes the strip's measured height for that offset.
    ui = _read_static("static/ui.js")
    assert "function _syncChatTodosShellClass(visible){" in ui
    assert "shell.classList.toggle('chat-todos-visible',!!visible)" in ui
    # The measurement is factored out so the observer below can republish it.
    assert "function _publishChatTodosHeight(){" in ui
    assert "shell.style.setProperty('--chat-todos-h',h+'px')" in ui
    # The in-flow strip resizes the transcript, so every layout-changing path
    # also re-pins the reader (re-gate 2026-10-07T20:13:25Z, [SILENT]).
    assert "function _repinChatTodosTranscript(){" in ui
    assert "_repinChatTodosTranscript();" in ui
    assert ui.count("_repinChatTodosTranscript();") >= 5
    assert "if(typeof _repinMessagesAfterComposerResize==='function') _repinMessagesAfterComposerResize();" in ui


def test_chat_todos_desktop_has_no_alignment_setting():
    # Reviewer re-gate 2026-10-07T18:08:02Z: "Drop the alignment setting."
    idx = _read_static("static/index.html")
    ui = _read_static("static/ui.js")
    panels = _read_static("static/panels.js")
    for src in (idx, ui, panels):
        assert "chatTodosAlign" not in src
        assert "_pickChatTodosAlign" not in src
        assert "_syncChatTodosAlignRadios" not in src
    assert "chat-todos-align-group" not in idx
    assert "hermes-webui-chat-todos-align" not in ui


def test_workspace_todos_tab_follows_the_in_chat_tray():
    # Reviewer re-gate 2026-10-07T18:08:02Z, item 6: while the in-chat tray is
    # on, the workspace "Show Todos tab" surface must follow it so the two
    # settings cannot contradict each other.
    panels = _read_static("static/panels.js")
    start = panels.find("function _applyWorkspaceTodosTabVisibility(){")
    assert start != -1
    end = panels.find("\nfunction ", start + 10)
    assert end != -1
    block = panels[start:end]
    assert "const trayOn=(typeof chatTodosEnabled==='function')&&chatTodosEnabled();" in block
    assert "const want=!!window._workspaceTodosTab&&!trayOn;" in block
    assert "if(tab) tab.hidden=!want;" in block
    assert "settingsWorkspaceTodosTabField" in block
    # ui.js re-applies it whenever the tray preference changes.
    ui = _read_static("static/ui.js")
    assert "_applyWorkspaceTodosTabVisibility()" in ui
    idx = _read_static("static/index.html")
    assert 'id="settingsWorkspaceTodosTabField"' in idx


def test_rail_hide_helper_does_not_clobber_a_user_hidden_tab():
    """Greptile P1 (2026-10-07T07:24:18Z): disabling the in-chat tray must not
    force-show a Todos entry the user hid independently via hidden_tabs.

    The helper used to `classList.toggle('nav-tab-hidden', !!enabled)`, which
    REMOVED the class whenever the tray was off — resurrecting a tab the user
    had deliberately hidden. Tray-off must defer to the canonical visibility
    owner instead of asserting its own show/hide.
    """
    ui = _read_static("static/ui.js")
    start = ui.find("function _syncChatTodosRailVisibility()")
    end = ui.find("function _chatTodosToggleEnabled", start)
    assert start != -1 and end != -1
    helper = ui[start:end]

    # Never unconditionally reveal the tab...
    assert "classList.toggle('nav-tab-hidden',!!enabled)" not in helper
    assert "classList.remove('nav-tab-hidden')" not in helper
    # ...tray-off hands visibility back to the canonical owner (hidden_tabs)...
    assert "_applyTabVisibility(_getHiddenTabs())" in helper
    # ...and tray-on still suppresses the duplicate sidebar surface + bounces.
    assert "classList.add('nav-tab-hidden')" in helper
    assert "switchPanel('chat'" in helper


# ── Behavior probes: the real frontend functions, run under node ──────────
# The maintainer reproduced the imported-list regression "with the real
# frontend functions", so these extract the shipped source of the functions
# under test and run them instead of asserting on their text.


def _extract(source: str, start_marker: str, end_marker: str) -> str:
    start = source.find(start_marker)
    assert start != -1, f"missing {start_marker!r}"
    end = source.find(end_marker, start)
    assert end != -1, f"missing {end_marker!r} after {start_marker!r}"
    return source[start:end]


def _run_node(tmp_path: Path, name: str, script: str) -> str:
    if shutil.which("node") is None:
        pytest.skip("node is required for the frontend behavior probe")
    script_path = tmp_path / name
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


_CURRENT_TODOS_PROBE = """
const S = {todos: [], todoStateMeta: null, messages: [], session: null};
__LEGACY__
__HELPER__
function assert(cond, msg) { if (!cond) throw new Error(msg); }

// Case A — the regression: hydration installs an empty S.todos for an imported
// session whose tasks only exist as role:"tool" messages, with no todoStateMeta.
S.session = {messages: [{role: 'tool', content: JSON.stringify({todos: [{id: 'a', content: 'imported', status: 'pending'}]})}]};
S.messages = S.session.messages;
S.todos = [];
S.todoStateMeta = null;
let got = _currentTodos();
assert(got.length === 1 && got[0].id === 'a', 'imported tool-message list must fall back to the legacy renderer');

// Case B — an explicit empty snapshot still wins (no spurious legacy revival).
S.todoStateMeta = {ts: 1, source: 'cold-load', version: 1};
assert(_currentTodos().length === 0, 'explicit empty snapshot must win');

// Case C — an explicit non-empty snapshot is returned as-is.
S.todos = [{id: 'x', content: 'X', status: 'pending'}];
got = _currentTodos();
assert(got.length === 1 && got[0].id === 'x', 'explicit snapshot must be returned');

// Case D — no snapshot AND no legacy messages => empty.
S.todoStateMeta = null;
S.todos = [];
S.session = {messages: []};
S.messages = [];
assert(_currentTodos().length === 0, 'no signal and no legacy list => empty');
console.log('ok');
"""


def test_current_todos_falls_back_to_legacy_for_imported_tool_lists(tmp_path):
    ui = _read_static("static/ui.js")
    panels = _read_static("static/panels.js")
    helper = _extract(ui, "function _currentTodos(){", "function _chatTodosSummary(")
    legacy = panels[panels.find("function _legacyTodosFromMessages() {"):]
    legacy = legacy[: legacy.find("\n}") + 2]
    assert legacy.rstrip().endswith("}")
    script = _CURRENT_TODOS_PROBE.replace("__LEGACY__", legacy).replace("__HELPER__", helper)
    assert _run_node(tmp_path, "current_todos_probe.js", script).strip() == "ok"


_SUMMARY_PROBE = """
const table = {
  todos_no_active: 'No active task list in this session.',
  todos_tray_summary_active: '{0} active · {1} total',
  todos_tray_summary_done: 'All done · {0} total',
};
function t(key, ...args) {
  let v = table[key];
  if (v === undefined) return key;
  if (args.length) v = String(v).replace(/\\{(\\d+)\\}/g, (m, i) => (args[i] !== undefined ? String(args[i]) : m));
  return v;
}
__HELPER__
function assert(cond, msg) { if (!cond) throw new Error(msg); }
const mixed = _chatTodosSummary([{status: 'pending'}, {status: 'in_progress'}, {status: 'completed'}]);
assert(mixed.text === '2 active · 3 total', 'localized active/total summary');
assert(mixed.active === 2 && mixed.total === 3, 'counts derive from statuses');
const done = _chatTodosSummary([{status: 'completed'}, {status: 'cancelled'}]);
assert(done.text === 'All done · 2 total', 'localized all-done summary');
assert(done.active === 0 && done.total === 2, 'terminal-only counts');
assert(_chatTodosSummary([]).text === 'No active task list in this session.', 'empty summary uses i18n');
console.log('ok');
"""


def test_chat_todos_summary_uses_localized_placeholders(tmp_path):
    ui = _read_static("static/ui.js")
    helper = _extract(ui, "function _chatTodosSummary(", "function renderChatTodos(){")
    script = _SUMMARY_PROBE.replace("__HELPER__", helper)
    assert _run_node(tmp_path, "summary_probe.js", script).strip() == "ok"


_EXPANDED_PROBE = """
const head = {attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }};
const tray = {
  classes: new Set(),
  classList: {
    add(c) { tray.classes.add(c); },
    remove(c) { tray.classes.delete(c); },
    contains(c) { return tray.classes.has(c); },
  },
};
function $(id) { return id === 'chatTodosPanel' ? tray : (id === 'chatTodosHead' ? head : null); }
__HELPER__
function assert(cond, msg) { if (!cond) throw new Error(msg); }
_syncChatTodosExpanded(true);
assert(tray.classes.has('open'), 'expand adds .open');
assert(head.attrs['aria-expanded'] === 'true', 'expand sets aria-expanded=true');
_syncChatTodosExpanded(false);
assert(!tray.classes.has('open'), 'collapse removes .open');
assert(head.attrs['aria-expanded'] === 'false', 'collapse sets aria-expanded=false');
console.log('ok');
"""


def test_sync_chat_todos_expanded_keeps_aria_in_sync(tmp_path):
    ui = _read_static("static/ui.js")
    helper = _extract(ui, "function _syncChatTodosExpanded(", "function _chatTodosToggleEnabled(")
    script = _EXPANDED_PROBE.replace("__HELPER__", helper)
    assert _run_node(tmp_path, "expanded_probe.js", script).strip() == "ok"


def test_chat_todos_locales_keep_diacritics():
    # [SHOULD-FIX] "Eight locales ship diacritic-stripped text (it, es, pt, fr,
    # cs, tr, pl, vi). Vietnamese is unreadable as shipped."
    src = _read_static("static/i18n.js")
    required = {
        "attività": "it",
        "attivo": "it",
        "área": "es",
        "duplicación": "es",
        "recolhível": "pt",
        "duplicação": "pt",
        "tâches": "fr",
        "Activée": "fr",
        "latérale": "fr",
        "úkolů": "cs",
        "sbalitelný": "cs",
        "duplicitě": "cs",
        "üst kısmında": "tr",
        "görev": "tr",
        "önlemek": "tr",
        "Pokaż": "pl",
        "Wyświetla": "pl",
        "bảng Todos": "vi",
        "trùng lặp": "vi",
    }
    for needle, locale in required.items():
        assert needle in src, f"{locale} lost its diacritics: {needle!r}"
    # Robustness floor: the share of non-ASCII characters in the vi block must
    # not collapse back to pure ASCII.
    start = src.find("\n  vi: {")
    assert start != -1
    vi_block = src[start : src.find("\n  },", start)]
    non_ascii = sum(1 for ch in vi_block if ord(ch) > 127)
    assert non_ascii > 200, f"vi locale looks diacritic-stripped ({non_ascii} non-ASCII chars)"


# ── Re-gate 2026-10-07T14:33:07Z (head cfd6f65c) — the three remaining items ──
# Each pins one finding, and where the maintainer reproduced it "in Chromium"
# the probe runs the shipped function under node instead of asserting on text.


def test_chat_todos_settings_toggle_repaints_the_visibility_chips():
    # [SILENT] static/ui.js:10503 — "Enabling the tray through Settings hides
    # Todos while its visibility chip still reports ON." The tray owns the hide,
    # so the chip must be re-rendered whenever the tray preference changes.
    ui = _read_static("static/ui.js")
    start = ui.find("function _chatTodosToggleEnabled(checked){")
    end = ui.find("function _chatTodosSummary(", start)
    assert start != -1 and end != -1, "missing _chatTodosToggleEnabled block"
    handler = ui[start:end]
    enable_at = handler.find("_setChatTodosEnabled(checked);")
    repaint_at = handler.find("_renderTabVisibilityChips()")
    assert enable_at != -1 and repaint_at != -1, "chips are never repainted on toggle"
    assert enable_at < repaint_at, "repaint must follow the preference write"
    assert "typeof _renderTabVisibilityChips==='function'" in handler


def test_chat_todos_chip_enable_clears_an_independent_hidden_tabs_bit():
    # [SILENT] static/panels.js:7878 — "With Todos independently hidden and the
    # tray enabled, clicking the OFF visibility chip disables the tray but
    # leaves Todos hidden." The explicit chip-enable branch must drop the
    # independent hidden_tabs entry in the same click.
    panels = _read_static("static/panels.js")
    start = panels.find("function _toggleTabVisibilityChip(panel){")
    end = panels.find("function _toggleDashboardVisibilityChip", start)
    assert start != -1 and end != -1, "missing _toggleTabVisibilityChip block"
    handler = panels[start:end]
    forced_at = handler.find("if(_tabVisibilityChipForcedOff(panel)){")
    tray_off_at = handler.find("_chatTodosToggleEnabled(false)", forced_at)
    assert forced_at != -1 and tray_off_at != -1
    branch = handler[forced_at:tray_off_at]
    assert "_setHiddenTabs(" in branch, "forced-off branch never clears hidden_tabs"
    assert "_getHiddenTabs()" in branch


def test_apply_locale_repaints_the_chat_todos_summary():
    # [SILENT] static/index.html:446 — "Opening Settings erases the task summary:
    # applyLocaleToDOM() overwrites the dynamic summary through
    # data-i18n=\"tab_todos\"." The live value must be repainted after restamping.
    src = _read_static("static/i18n.js")
    start = src.find("function applyLocaleToDOM() {")
    end = src.find("// Apply saved locale immediately", start)
    assert start != -1 and end != -1, "missing applyLocaleToDOM"
    body = src[start:end]
    aria_at = body.find("[data-i18n-aria-label]")
    repaint_at = body.find("typeof renderChatTodos === 'function'")
    sync_at = body.find("syncWorkspacePanelUI()")
    assert repaint_at != -1, "applyLocaleToDOM never repaints the todos summary"
    assert aria_at < repaint_at < sync_at, "repaint must follow the locale restamp"


_TOGGLE_CHIPS_PROBE = """
function assert(cond, msg) { if (!cond) throw new Error(msg); }
let chipsRendered = 0;
let enabledSet = null;
let expandedCalls = [];
let renderCalls = 0;
function _setChatTodosEnabled(v) { enabledSet = !!v; }
function _syncChatTodosExpanded(v) { expandedCalls.push(!!v); }
function renderChatTodos() { renderCalls++; }
let autosaves = 0;
function _scheduleAppearanceAutosave() { autosaves++; }
function $() { return null; }
var _renderTabVisibilityChips = function () { chipsRendered++; };
__HELPER__
_chatTodosToggleEnabled(true);
assert(enabledSet === true, 'tray preference must be written');
assert(chipsRendered === 1, 'enabling the tray must repaint the visibility chips');
assert(expandedCalls.length === 1 && expandedCalls[0] === false, 're-enable restarts collapsed');
assert(renderCalls === 1, 'tray contents are repainted');
_chatTodosToggleEnabled(false);
assert(enabledSet === false, 'disabling writes through');
assert(chipsRendered === 2, 'disabling must repaint the chips too');
// Reviewer re-gate 2026-10-08T23:19:27Z (static/ui.js:11074): the tray toggle
// must NOT schedule an appearance autosave — that save POSTs this client's
// hidden_tabs mirror, which can still hold another profile's stale snapshot,
// clobbering a newer server preference.
assert(autosaves === 0, 'toggling the tray must not autosave the appearance payload');
console.log('ok');
"""


def test_chat_todos_toggle_repaints_chips_probe(tmp_path):
    ui = _read_static("static/ui.js")
    helper = _extract(
        ui, "function _chatTodosToggleEnabled(checked){", "function _chatTodosSummary("
    )
    script = _TOGGLE_CHIPS_PROBE.replace("__HELPER__", helper)
    assert _run_node(tmp_path, "toggle_chips_probe.js", script).strip() == "ok"


_CHIP_FORCED_OFF_PROBE = """
function assert(cond, msg) { if (!cond) throw new Error(msg); }
const _ALWAYS_VISIBLE_TABS = new Set(['chat', 'settings']);
let hidden = ['todos', 'notes'];
let applied = null;
let trayToggles = [];
let chips = 0;
let chatTodosOn = true;
function _getHiddenTabs() { return hidden.slice(); }
function _setHiddenTabs(v) { hidden = v.slice(); }
function _applyTabVisibility(h) { applied = h.slice(); }
function _renderTabVisibilityChips() { chips++; }
function _scheduleAppearanceAutosave() {}
function chatTodosEnabled() { return chatTodosOn; }
function _chatTodosToggleEnabled(v) { chatTodosOn = !!v; trayToggles.push(!!v); }
__HELPER__
_toggleTabVisibilityChip('todos');
assert(hidden.indexOf('todos') === -1, 'chip-enable must clear the independent hidden_tabs bit');
assert(hidden.indexOf('notes') !== -1, 'other hidden tabs must be untouched');
assert(trayToggles.length === 1 && trayToggles[0] === false, 'the tray is what gets disabled');
assert(chatTodosOn === false, 'tray preference is off');
assert(chips === 1, 'the chip row is re-rendered');
console.log('ok');
"""


def test_chat_todos_chip_enable_clears_hidden_tabs_probe(tmp_path):
    panels = _read_static("static/panels.js")
    helper = _extract(
        panels, "function _toggleTabVisibilityChip(panel){", "function _toggleDashboardVisibilityChip"
    )
    forced_off = _extract(
        panels, "function _tabVisibilityChipForcedOff(panel){", "function _renderTabVisibilityChips(){"
    )
    script = _CHIP_FORCED_OFF_PROBE.replace("__HELPER__", helper + "\n" + forced_off)
    assert _run_node(tmp_path, "chip_forced_off_probe.js", script).strip() == "ok"


_LOCALE_SUMMARY_PROBE = """
function assert(cond, msg) { if (!cond) throw new Error(msg); }
let repainted = 0;
let workspaceSynced = 0;
function renderChatTodos() { repainted++; }
function syncWorkspacePanelUI() { workspaceSynced++; }
function syncAppTitlebar() {}
function t(k) { return k; }
const document = { querySelectorAll() { return []; } };
__HELPER__
applyLocaleToDOM();
assert(repainted === 1, 'applyLocaleToDOM must repaint the chat-todos summary');
assert(workspaceSynced === 1, 'the other post-restamp syncs still run');
console.log('ok');
"""


def test_apply_locale_repaint_probe(tmp_path):
    src = _read_static("static/i18n.js")
    helper = _extract(
        src, "function applyLocaleToDOM() {", "// Apply saved locale immediately"
    )
    script = _LOCALE_SUMMARY_PROBE.replace("__HELPER__", helper)
    assert _run_node(tmp_path, "locale_summary_probe.js", script).strip() == "ok"


# ── Tray box lifecycle: the pill must follow the ACTUAL box ───────────────
# Re-gate 2026-10-07T23:49:23Z: "make pill placement follow the tray's actual
# box through those two schedules" — (1) resizing ACROSS the tray breakpoint and
# (2) a hidden tray becoming visible after Settings edits hydrate the list. Both
# bypass renderChatTodos()/toggleChatTodos(), where the height used to be
# measured, so the published --chat-todos-h stranded a stale value (236px while
# the strip was 276px; 77px while it was 276px) and the Start pill painted inside
# the task rows. These probes run the shipped functions under node and drive
# both schedules through the observer, instead of asserting on source text.

_TRAY_HEIGHT_PROBE = """
function assert(cond, msg) { if (!cond) throw new Error(msg); }
const shellClasses = new Set();
const shellStyle = {
  _v: {},
  setProperty(k, v) { this._v[k] = v; },
  removeProperty(k) { delete this._v[k]; },
  getPropertyValue(k) { return this._v[k] || ''; },
};
const shell = {
  classList: {
    add(c) { shellClasses.add(c); },
    remove(c) { shellClasses.delete(c); },
    contains(c) { return shellClasses.has(c); },
    toggle(c, on) { if (on) shellClasses.add(c); else shellClasses.delete(c); return !!on; },
  },
  style: shellStyle,
};
// The strip's real box, mutable so the probe can drive the transitions.
let boxHeight = 36;
const tray = { getBoundingClientRect() { return { height: boxHeight }; } };
function $(id) { return id === 'chatTodosPanel' ? tray : null; }
const document = { querySelector(sel) { return sel === '.messages-shell' ? shell : null; } };
const observers = [];
class ResizeObserver {
  constructor(cb) { this.cb = cb; this.el = null; observers.push(this); }
  observe(el) { this.el = el; }
  disconnect() { this.el = null; }
}
let repins = 0;
function _repinMessagesAfterComposerResize() { repins++; }
function fire() { observers.forEach(function (ro) { ro.cb([]); }); }
__HELPER__

// A. A render publishes the measured collapsed band.
_syncChatTodosShellClass(true);
assert(shellStyle.getPropertyValue('--chat-todos-h') === '36px', 'render publishes the measured strip height');

// The observer is lifecycle-owned: set up once, for the tray element.
_ensureChatTodosResizeObserver();
_ensureChatTodosResizeObserver();
assert(observers.length === 1, 'exactly one lifecycle observer, not one per render');
assert(observers[0].el === tray, 'the observer watches the tray element');

// B. Resize ACROSS the tray breakpoint (393 -> 1440) with no render/toggle call.
// The strip grows, so the published height must follow the real box.
boxHeight = 276;
fire();
assert(shellStyle.getPropertyValue('--chat-todos-h') === '276px',
  'a breakpoint resize republishes the real box instead of stranding 236px');
assert(repins >= 1, 'the observer schedule re-pins the transcript');

// C. Hidden -> visible. While chat is hidden the strip measures 0.
boxHeight = 0;
const hiddenPublished = shellStyle.getPropertyValue('--chat-todos-h');
const repinsWhileHidden = repins;
fire();
assert(shellStyle.getPropertyValue('--chat-todos-h') === hiddenPublished,
  'a hidden (0px) box must not clobber the published height with 0');
assert(repins === repinsWhileHidden, 'a hidden box does not re-pin');
// Returning to chat re-fires the observer with the real box.
boxHeight = 276;
fire();
assert(shellStyle.getPropertyValue('--chat-todos-h') === '276px',
  'hidden -> visible publishes the visible height, not the stale 77px');
assert(repins > repinsWhileHidden, 'hidden -> visible re-pins the transcript too');

// D. Turning the tray off clears the offset and the shell marker class.
_syncChatTodosShellClass(false);
assert(shellStyle.getPropertyValue('--chat-todos-h') === '', 'tray off clears the published height');
assert(!shell.classList.contains('chat-todos-visible'), 'tray off drops the shell marker class');
console.log('ok');
"""


def test_chat_todos_pill_follows_the_tray_box_lifecycle(tmp_path):
    ui = _read_static("static/ui.js")
    helper = _extract(
        ui, "let _chatTodosResizeObserver=null;", "function _syncChatTodosExpanded("
    ) + _extract(
        ui, "function _repinChatTodosTranscript(){", "function renderChatTodos(){"
    ) + _extract(
        # The shipped observer also refreshes the overflow cue now
        # (Greptile P2 2026-10-09T21:45:10Z), so the probe must provide it.
        ui, "function _updateChatTodosScrollCue(){", "function scheduleTodosRefresh(){"
    )
    script = _TRAY_HEIGHT_PROBE.replace("__HELPER__", helper)
    assert _run_node(tmp_path, "tray_height_probe.js", script).strip() == "ok"


def test_chat_todos_box_observer_is_wired_into_the_render_path():
    """Source guard for the probe above: every render (re)arms the observer."""
    ui = _read_static("static/ui.js")
    render = _extract(ui, "function renderChatTodos(){", "function toggleChatTodos(){")
    assert "_ensureChatTodosResizeObserver();" in render
    assert "new ResizeObserver(" in ui
    assert "ro.observe(tray);" in ui


# ── Greptile re-review of the post-master-merge head (2026-10-09T21:45:10Z) ──
# P2 "Overflow fade misses size changes": the tray's resize callback republished
# the height and re-pinned the transcript but never refreshed the bottom fade,
# so a list that became scrollable through a viewport/composer change kept the
# cue hidden while scrollTop was still 0.

_TRAY_CUE_PROBE = """
function assert(cond, msg) { if (!cond) throw new Error(msg); }
const shellClasses = new Set(['chat-todos-visible']);
const shellStyle = {
  _v: {},
  setProperty(k, v) { this._v[k] = v; },
  removeProperty(k) { delete this._v[k]; },
  getPropertyValue(k) { return this._v[k] || ''; },
};
const shell = {
  classList: {
    add(c) { shellClasses.add(c); },
    remove(c) { shellClasses.delete(c); },
    contains(c) { return shellClasses.has(c); },
    toggle(c, on) { if (on) shellClasses.add(c); else shellClasses.delete(c); return !!on; },
  },
  style: shellStyle,
};
const tray = { getBoundingClientRect() { return { height: 276 }; } };
function $(id) { return id === 'chatTodosPanel' ? tray : null; }
const document = { querySelector(sel) { return sel === '.messages-shell' ? shell : null; } };
const observers = [];
class ResizeObserver {
  constructor(cb) { this.cb = cb; this.el = null; observers.push(this); }
  observe(el) { this.el = el; }
  disconnect() { this.el = null; }
}
let repins = 0;
function _repinChatTodosTranscript() { repins++; }
__HELPER__

// Count the shipped calls instead of replacing the implementation.
const _pubShipped = _publishChatTodosHeight;
let pubs = 0;
_publishChatTodosHeight = function () { pubs++; return _pubShipped.apply(null, arguments); };
const _cueShipped = _updateChatTodosScrollCue;
let cues = 0;
_updateChatTodosScrollCue = function () { cues++; return _cueShipped.apply(null, arguments); };

_ensureChatTodosResizeObserver();
assert(observers.length === 1, 'exactly one lifecycle observer');
observers[0].cb([]);
assert(pubs === 1, 'the box change republishes the tray height');
assert(cues === 1, 'the box change must refresh the overflow cue too');

// A hidden tray measures 0: it republishes nothing and must not touch the cue.
shellClasses.delete('chat-todos-visible');
observers[0].cb([]);
assert(cues === 1, 'a hidden tray must not refresh the cue');
console.log('ok');
"""


def test_chat_todos_overflow_cue_follows_the_tray_box(tmp_path):
    """The bottom fade must be refreshed when the tray's box changes.

    Without it, a list that only becomes scrollable through a shorter viewport
    or a taller composer keeps the fade hidden (scrollTop is still 0), so the
    clipped list reads as the complete list.
    """
    ui = _read_static("static/ui.js")
    helper = _extract(
        ui, "let _chatTodosResizeObserver=null;", "function _syncChatTodosExpanded("
    ) + _extract(
        ui, "function _updateChatTodosScrollCue(){", "function scheduleTodosRefresh(){"
    )
    script = _TRAY_CUE_PROBE.replace("__HELPER__", helper)
    assert _run_node(tmp_path, "tray_cue_probe.js", script).strip() == "ok"


def test_the_resize_callback_refreshes_the_overflow_cue():
    """Source guard for the probe above: the cue call sits in the callback."""
    ui = _read_static("static/ui.js")
    callback = _extract(ui, "const ro=new ResizeObserver(function(){", "ro._tray=tray;")
    assert "_updateChatTodosScrollCue();" in callback
    assert "_publishChatTodosHeight();" in callback
    assert "_repinChatTodosTranscript();" in callback


# ── Re-gate 2026-10-08T03:10:50Z (head a63ab699) ──────────────────────────
# One [SHOULD-FIX] and four UX asks. The engineering item is pinned by a node
# probe that drives the shipped functions; the UX asks are pinned on source +
# behaviour where they carry logic (the disabled field, the overflow cue, the
# centred column) so they cannot silently rot.


def test_chat_todos_repin_gates_on_auto_follow_and_a_real_box_growth():
    """[SHOULD-FIX] 1: "The repin yanks the scroll for readers with auto-follow
    OFF ... return early when window._autoScrollFollow === false, and only repin
    when the tray's measured box actually grew."

    The earlier fix re-pinned on every render path, i.e. also on the feature-OFF
    default where the tray is hidden and nothing resized at all.
    """
    ui = _read_static("static/ui.js")
    block = _extract(ui, "function _repinChatTodosTranscript(){", "function renderChatTodos(){")
    assert "window._autoScrollFollow===false) return;" in block
    # The box is measured BEFORE the Auto-follow gate (re-gate 2026-10-08T06:40:51Z):
    # when the gate returned first, a hide with Auto-follow OFF never recorded the
    # zero height, so a later re-show with follow ON compared against the stale
    # pre-hide height, read "not grown", and skipped the re-pin.
    assert block.index("_measureChatTodosTrayHeight()") < block.index(
        "window._autoScrollFollow===false) return;"
    )
    assert "const grew=h>_chatTodosRepinH;" in block
    assert "_chatTodosRepinH=h;" in block
    # The measurement is factored out so the observer and the repin share it;
    # BOTH call sites CEIL it, so the growth test and the published pill offset
    # agree on the same number (reviewer re-gate 2026-10-08T09:54:47Z, must-fix 1:
    # the tray cap leaves fractional heights and rounding down shaved the >=7px
    # Start-pill clearance).
    assert "function _measureChatTodosTrayHeight(){" in ui
    assert ui.count("const h=Math.ceil(_measureChatTodosTrayHeight());") == 2
    assert "Math.round(_measureChatTodosTrayHeight())" not in ui
    publish = _extract(ui, "function _publishChatTodosHeight(){", "function _syncChatTodosShellClass(")
    assert "const h=Math.ceil(_measureChatTodosTrayHeight());" in publish


_REPIN_PROBE = """
function assert(cond, msg) { if (!cond) throw new Error(msg); }
globalThis.window = {_autoScrollFollow: false};
let boxHeight = 0;
const tray = {hidden: false, getBoundingClientRect() { return {height: boxHeight}; }};
function $(id) { return id === 'chatTodosPanel' ? tray : null; }
let repins = 0;
function _repinMessagesAfterComposerResize() { repins++; }
__HELPER__

// 1. Auto-follow OFF: even a real growth must not touch the scroll position.
//    The box is still recorded on the baseline (that is the whole point of the
//    re-gate fix), so the growth below is NOT replayed once follow turns on.
boxHeight = 276;
_repinChatTodosTranscript();
assert(repins === 0, 'auto-follow OFF must never re-pin');

// 2. Auto-follow ON: the next measured growth re-pins exactly once.
window._autoScrollFollow = true;
boxHeight = 400;
_repinChatTodosTranscript();
assert(repins === 1, 'a measured growth re-pins the transcript');
_repinChatTodosTranscript();
assert(repins === 1, 'an unchanged box does not re-pin again');

// 3. Collapsing shrinks the box. A growing viewport cannot strand a pinned
//    reader (scrollHeight - clientHeight only falls, and the browser clamps
//    scrollTop), so a shrink is not a repin either.
boxHeight = 36;
_repinChatTodosTranscript();
assert(repins === 1, 'a shrink must not re-pin');

// 4. Re-expanding grows it again.
boxHeight = 400;
_repinChatTodosTranscript();
assert(repins === 2, 're-expanding re-pins again');

// 5. Feature-OFF default: the tray is hidden, nothing rendered, so no path may
//    pull a reader who scrolled away back to the bottom.
repins = 0;
tray.hidden = true;
boxHeight = 0;
_repinChatTodosTranscript();
_repinChatTodosTranscript();
assert(repins === 0, 'a hidden tray (feature OFF) must not pull the reader down');

// 6. Hide-then-show with Auto-follow toggled (re-gate 2026-10-08T06:40:51Z).
//    Before the fix the follow gate returned BEFORE measuring, so a hide with
//    Auto-follow OFF left the stale pre-hide height on the books; re-showing
//    the tray at the SAME height then read "not grown" and skipped the re-pin,
//    stranding a pinned reader.
repins = 0;
tray.hidden = false;
window._autoScrollFollow = true;
boxHeight = 276;
_repinChatTodosTranscript();
assert(repins === 1, 'baseline: an expanded tray re-pins with follow ON');
window._autoScrollFollow = false;
tray.hidden = true; boxHeight = 0; // hidden WHILE follow is OFF
_repinChatTodosTranscript();
assert(repins === 1, 'a hide with follow OFF must not re-pin');
window._autoScrollFollow = true;
tray.hidden = false; boxHeight = 276; // re-shown at the SAME height
_repinChatTodosTranscript();
assert(repins === 2, 're-show after a follow-OFF hide re-pins (stale baseline fixed)');
console.log('ok');
"""


def test_chat_todos_repin_probe(tmp_path):
    ui = _read_static("static/ui.js")
    helper = _extract(
        ui, "let _chatTodosResizeObserver=null;", "function _syncChatTodosExpanded("
    ) + _extract(
        ui, "function _repinChatTodosTranscript(){", "function renderChatTodos(){"
    )
    script = _REPIN_PROBE.replace("__HELPER__", helper)
    assert _run_node(tmp_path, "chat_todos_repin_probe.js", script).strip() == "ok"


def test_chat_todos_rows_are_centred_on_the_reading_column():
    """UX ask: "Centre the expanded rows within the reading column (the body is
    still full width)." The rows now sit in their own centred column that mirrors
    .messages-inner's width contract, so the row text lines up with the message
    text instead of spanning the whole shell."""
    css = _read_static("static/style.css")
    ui = _read_static("static/ui.js")
    # Same max-width/padding contract as .messages-inner (which is driven by
    # --msg-max), including its <=640px mobile mirror.
    assert ".chat-todos-rows{margin:0 auto;width:100%;padding:0 24px;max-width:var(--msg-max);}" in css
    assert "@media(min-width:1400px){.chat-todos-rows{max-width:calc(var(--msg-max) + 40px);}}" in css
    assert "@media(min-width:1800px){.chat-todos-rows{max-width:calc(var(--msg-max) + 80px);}}" in css
    assert (
        ".chat-todos-rows{max-width:100%;"
        "padding-left:max(10px,env(safe-area-inset-left,0));"
        "padding-right:max(10px,env(safe-area-inset-right,0));}" in css
    )
    # ...and that mobile mirror lives inside the EXISTING <=640px block, right
    # after .messages-inner's own override: a second `@media(max-width:640px)`
    # would have to come first in the file and would break the mobile-containment
    # tests that brace-match the first one (test_issue4553 / test_issue4856).
    inner_mobile = css.index(".messages-inner{padding:12px 10px 20px;")
    mirror = css.index(".chat-todos-rows{max-width:100%;")
    assert inner_mobile < mirror
    assert "@media(" not in css[inner_mobile:mirror]
    # ...and the column it mirrors really is the transcript's.
    assert ".messages-inner{margin:0 auto;width:100%;padding:20px 24px 32px;" in css
    assert ".messages-inner { max-width: var(--msg-max); }" in css
    # The body's own horizontal padding is gone, so the rows' containing block is
    # the same box .messages-inner lives in — that is what makes them agree.
    assert ".chat-todos-body{max-height:min(240px,40vh);overflow-y:auto;border-top:1px solid var(--border);padding:4px 0 8px;}" in css
    assert 'body.innerHTML=`<div class="chat-todos-rows">' in ui


def test_chat_todos_capped_body_has_an_overflow_cue():
    """UX ask: "A mobile overflow cue (fade or scrollbar) when the capped body
    scrolls." The capped body's scrollbar is an overlay the phone only reveals
    mid-scroll, so a truncated list read as complete."""
    idx = _read_static("static/index.html")
    css = _read_static("static/style.css")
    ui = _read_static("static/ui.js")
    assert 'id="chatTodosBodyWrap"' in idx
    assert 'class="chat-todos-scroll-cue"' in idx
    assert ".chat-todos-body-wrap{position:relative;}" in css
    assert ".chat-todos-body-wrap.chat-todos-overflowing .chat-todos-scroll-cue{opacity:1;}" in css
    # Collapsed bodies render nothing, so the cue must not paint there either.
    assert ".chat-todos:not(.open) .chat-todos-scroll-cue{display:none;}" in css
    # The capped (mobile) body is still what the cue exists for.
    assert ".chat-todos-body{max-height:min(200px,40vh);}" in css
    assert "function _wireChatTodosScrollCue(){" in ui
    assert "body.addEventListener('scroll',_updateChatTodosScrollCue,{passive:true})" in ui
    assert "_updateChatTodosScrollCue();" in ui


_SCROLL_CUE_PROBE = """
function assert(cond, msg) { if (!cond) throw new Error(msg); }
const classes = new Set();
const wrap = {classList: {
  toggle(c, on) { if (on) classes.add(c); else classes.delete(c); return !!on; },
  contains(c) { return classes.has(c); },
}};
const body = {scrollTop: 0, listeners: {}, addEventListener(t, f) { this.listeners[t] = f; }};
let sh = 600;
let ch = 200;
Object.defineProperty(body, 'scrollHeight', {get() { return sh; }});
Object.defineProperty(body, 'clientHeight', {get() { return ch; }});
function $(id) { return id === 'chatTodosBody' ? body : (id === 'chatTodosBodyWrap' ? wrap : null); }
__HELPER__
_wireChatTodosScrollCue();
assert(typeof body.listeners.scroll === 'function', 'the body scroll event drives the cue');
_updateChatTodosScrollCue();
assert(classes.has('chat-todos-overflowing'), 'content below the fold shows the cue');
body.scrollTop = 400;
_updateChatTodosScrollCue();
assert(!classes.has('chat-todos-overflowing'), 'the cue clears when the reader reaches the bottom');
sh = 150;
body.scrollTop = 0;
_updateChatTodosScrollCue();
assert(!classes.has('chat-todos-overflowing'), 'nothing to scroll => no cue');
console.log('ok');
"""


def test_chat_todos_scroll_cue_probe(tmp_path):
    ui = _read_static("static/ui.js")
    helper = _extract(ui, "let _chatTodosCueWired=false;", "function scheduleTodosRefresh(){")
    script = _SCROLL_CUE_PROBE.replace("__HELPER__", helper)
    assert _run_node(tmp_path, "chat_todos_scroll_cue_probe.js", script).strip() == "ok"


def test_workspace_todos_setting_sits_next_to_the_tray_setting():
    """UX ask: "Put the tray settings next to each other." The field that the tray
    takes over must be adjacent to the tray toggle, so the cause of its disabled
    state is visible right there."""
    idx = _read_static("static/index.html")
    chat_at = idx.find('id="settingsChatTodosInChat"')
    field_at = idx.find('id="settingsWorkspaceTodosTabField"')
    assert chat_at != -1 and field_at != -1
    assert chat_at < field_at
    between = idx[chat_at:field_at]
    # Only the tray field's own markup separates them: no unrelated settings row
    # (nor a second settings-field id) may sit in between.
    assert between.count('id="settings') == 1
    assert "settingsSessionJumpButtons" not in between
    assert "settingsSessionEndlessScroll" not in between
    assert "settingsWorkspacePanelOpen" not in between


_WORKSPACE_FIELD_PROBE = """
function assert(cond, msg) { if (!cond) throw new Error(msg); }
globalThis.window = {_workspaceTodosTab: true};
const els = {};
function $(id) { return els[id] || null; }
const field = {hidden: true, classList: {
  _s: new Set(),
  toggle(c, on) { if (on) this._s.add(c); else this._s.delete(c); return !!on; },
  contains(c) { return this._s.has(c); },
}};
const box = {disabled: false};
const note = {hidden: false};
const tab = {hidden: false};
els.settingsWorkspaceTodosTabField = field;
els.settingsWorkspaceTodosTab = box;
els.settingsWorkspaceTodosTabNote = note;
els.workspaceTodosTab = tab;
const document = {querySelector() { return null; }};
let trayOn = true;
function chatTodosEnabled() { return trayOn; }
__HELPER__
_applyWorkspaceTodosTabVisibility();
assert(box.disabled === true, 'the field is disabled, not hidden, while the tray is on');
assert(field.hidden === false, 'the field stays visible');
assert(field.classList.contains('is-disabled'), 'the row is dimmed as disabled');
assert(note.hidden === false, 'the explanation is shown');
assert(tab.hidden === true, 'the workspace tab itself still follows the tray');
trayOn = false;
_applyWorkspaceTodosTabVisibility();
assert(box.disabled === false, 'the field re-enables when the tray is off');
assert(field.hidden === false, 'still visible');
assert(!field.classList.contains('is-disabled'), 'the dimming clears');
assert(note.hidden === true, 'the explanation is hidden again');
assert(tab.hidden === false, 'the workspace tab follows the workspace preference');
console.log('ok');
"""


def test_workspace_todos_field_disabled_not_hidden_probe(tmp_path):
    """UX ask: "show the workspace field disabled with an explanation instead of
    hiding it" — hiding it made the two settings silently contradict each other."""
    panels = _read_static("static/panels.js")
    idx = _read_static("static/index.html")
    helper = _extract(
        panels, "function _applyWorkspaceTodosTabVisibility(){", "\nfunction "
    )
    assert "field.hidden=trayOn" not in helper
    assert "box.disabled=!!trayOn" in helper
    assert "field.classList.toggle('is-disabled',!!trayOn)" in helper
    assert "note.hidden=!trayOn" in helper
    assert 'id="settingsWorkspaceTodosTabNote"' in idx
    assert 'data-i18n="settings_note_workspace_todos_tab_disabled"' in idx
    script = _WORKSPACE_FIELD_PROBE.replace("__HELPER__", helper)
    assert _run_node(tmp_path, "workspace_todos_field_probe.js", script).strip() == "ok"


def test_chat_todos_shared_row_renderer_serves_the_tray():
    """UX ask: "Tidy the shared-row renderer and drop the dead CSS." One renderer
    (renderTodoRow) now serves the sidebar panel, the workspace tab and the tray;
    the tray-only class hooks and their CSS are gone."""
    ui = _read_static("static/ui.js")
    css = _read_static("static/style.css")
    shared = _extract_row_renderer(ui)
    assert "opts.rowClass" in shared
    assert "const compact=!!opts.compact;" in shared
    assert 'class="todos-row${rowClass}"' in shared
    # Dead tray-only styling: the row markup comes from the shared renderer now.
    assert ".chat-todos-row .todos-status" not in css
    assert ".chat-todos-row .todos-content" not in css
    assert ".chat-todos-row .todos-meta" not in css
    # ...but the shared renderer's metadata class is a real hook, so the tray can
    # still drop its own trailing border.
    assert ".chat-todos-row:last-child{border-bottom:none;}" in css
    # The sidebar panel / workspace tab keep their metadata rows.
    panels = _read_static("static/panels.js")
    workspace = _read_static("static/workspace.js")
    assert "renderTodoRows(todos, {metadata:true})" in panels
    assert "renderTodoRows(todos, {metadata:true})" in workspace


# ── Round-4 polish (reviewer re-gate 2026-10-08T08:44:28Z) ─────────────────
# The maintainer re-shot the tray in a realistic layout and asked for four
# one/two-line fixes before it goes to Nathan for visual sign-off.


def test_chat_todos_header_takes_the_rows_inset_down_to_640px():
    """Ask 1: "Give the header the rows' inset and move the narrow-header rule
    from 768px to 640px." The header used a 12px inset while the rows and the
    transcript use 24px, so the header icon sat 12px left of the status-icon
    column (14px between 641 and 768px).

    Re-gate 2026-10-08T09:54:47Z (must-fix 2) stopped patching the BUTTON's own
    padding: a fixed 24px inset can only line up with the centred rows when the
    shell happens to be about as wide as --msg-max, and the measured miss was
    -135.5px at 1440, -355.5px at 1920, -595.5px at 2400. The button is now a
    full-width hit band with zero padding and the CONTENT span carries the rows'
    exact column contract, so the icon and the chevron land on the reading
    column at every width.
    """
    css = _read_static("static/style.css")
    idx = _read_static("static/index.html").replace("\r\n", "\n")
    # The button keeps the whole band as its hit target / hover surface...
    assert (
        ".chat-todos-head{display:flex;align-items:center;gap:8px;width:100%;"
        "min-height:35px;padding:0;" in css
    )
    # ...and the inner span takes the rows' --msg-max mirror (three pins).
    assert (
        ".chat-todos-head-inner{display:flex;align-items:center;gap:8px;width:100%;"
        "min-height:35px;margin:0 auto;padding:0 24px;max-width:var(--msg-max);}" in css
    )
    assert (
        "@media(min-width:1400px){.chat-todos-head-inner{max-width:calc(var(--msg-max) + 40px);}}" in css
    )
    assert (
        "@media(min-width:1800px){.chat-todos-head-inner{max-width:calc(var(--msg-max) + 80px);}}" in css
    )
    # The markup really wraps the header's two children in that span.
    assert '<span class="chat-todos-head-inner">' in idx
    head_open = idx.index('class="chat-todos-head"')
    inner_open = idx.index('<span class="chat-todos-head-inner">', head_open)
    chevron = idx.index('class="chat-todos-chevron"', inner_open)
    inner_close = idx.index("</span>\n              </button>", chevron)
    assert inner_open < chevron < inner_close
    # Exactly ONE narrow rule, and it sits in the SAME <=640px block as the rows'
    # 10px safe-area mirror (proved by "no @media between them"), right after it.
    narrow = (
        ".chat-todos-head-inner{max-width:100%;"
        "padding-left:max(10px,env(safe-area-inset-left,0));"
        "padding-right:max(10px,env(safe-area-inset-right,0));}"
    )
    assert css.count(narrow) == 1
    rows_mobile = css.index(".chat-todos-rows{max-width:100%;")
    head_mobile = css.index(narrow)
    assert rows_mobile < head_mobile
    assert "@media(" not in css[rows_mobile:head_mobile]
    # The button itself keeps NO width-dependent padding to fall out of step.
    assert ".chat-todos-head{padding:0 10px;}" not in css
    assert ".chat-todos-head{padding:0 24px;" not in css
    # The 768px block kept only the body cap.
    at768 = css.index("@media(max-width:768px){")
    end768 = css.index(".messages{flex:1;overflow-y:auto;", at768)
    assert ".chat-todos-head" not in css[at768:end768]
    assert ".chat-todos-body{max-height:min(200px,40vh);}" in css[at768:end768]


def test_chat_todos_body_cap_carries_a_height_term():
    """Ask 3: "Cap by height too." The cap depended only on width, so a phone in
    landscape (844x390) got the 240px cap and the expanded tray took ~70% of the
    height, collapsing the transcript to nothing."""
    css = _read_static("static/style.css")
    assert ".chat-todos-body{max-height:min(240px,40vh);" in css
    assert ".chat-todos-body{max-height:min(200px,40vh);}" in css
    # The width-only caps are gone.
    assert ".chat-todos-body{max-height:240px;" not in css
    assert ".chat-todos-body{max-height:200px;}" not in css


def test_chat_todos_tray_is_capped_against_the_messages_shell():
    """Must-fix 1 (reviewer re-gate 2026-10-08T09:54:47Z): "The transcript can
    disappear entirely on short screens." The 40vh BODY cap ignored the space the
    header and the composer take, so 12 rows plus a five-line draft left the
    transcript 0px tall at 844x390 / 1440x420 (103px / 133px with the tray off).
    The tray now owns a cap against .messages-shell and its body shrinks +
    scrolls inside it; the body caps stay as the inner limit."""
    css = _read_static("static/style.css")
    assert (
        ".chat-todos{flex:0 0 auto;width:100%;background:var(--surface);"
        "border-bottom:1px solid var(--border);max-height:calc(50% - 1px);"
        "min-height:0;display:flex;flex-direction:column;overflow:hidden;}" in css
    )
    # 50% resolves against the shell, so the shell must still be the sized flex
    # column the tray is an item of.
    assert (
        ".messages-shell{flex:1;min-height:0;position:relative;"
        "display:flex;flex-direction:column;}" in css
    )
    # The strip ships `hidden`, and the author `display:flex` above would outrank
    # the UA [hidden] rule without this opt-out (the collapsed tray would paint).
    assert ".chat-todos[hidden]{display:none;}" in css
    # The header keeps its band...
    assert (
        ".chat-todos-head{display:flex;align-items:center;gap:8px;width:100%;"
        "min-height:35px;padding:0;" in css
    )
    assert "transition:background .12s;flex-shrink:0;}" in css
    # ...the body wrapper takes the remainder and may shrink BELOW its content
    # (min-height:0 on both it and the body), which is what lets the capped body
    # scroll instead of shoving the transcript off-screen.
    assert ".chat-todos-body-wrap{min-height:0;display:flex;flex-direction:column;}" in css
    assert ".chat-todos-body{min-height:0;}" in css
    # ...and the body's own caps stay as the inner limit.
    assert ".chat-todos-body{max-height:min(240px,40vh);" in css
    assert ".chat-todos-body{max-height:min(200px,40vh);}" in css


def test_chat_todos_overflow_cue_is_strong_enough():
    """Ask 2: the 22px fade landed on a cancelled row that is already
    half-opacity and struck through, so it read as row styling; ~40px."""
    css = _read_static("static/style.css")
    assert (
        ".chat-todos-scroll-cue{position:absolute;left:0;right:0;bottom:0;height:40px;" in css
    )
    assert "height:22px;pointer-events:none" not in css


# The exact tail sentence the disabled workspace-todos card carried in each
# locale before the re-gate; ask 4 dropped it everywhere.
_REMOVED_TODOS_DESC_TAILS = (
    "The sidebar Todos panel remains available",  # en
    "Il pannello Todos della barra laterale rimane disponibile",  # it
    "サイドバーのTodosパネルは引き続き利用できます",  # ja
    "Боковая панель Todos остаётся доступной",  # ru
    "El panel Todos de la barra lateral sigue disponible",  # es
    "Das Todos-Panel in der Seitenleiste bleibt weiterhin verfügbar",  # de
    "侧边栏的待办事项面板仍然可用",  # zh-CN
    "側邊欄的待辦事項面板仍然可用",  # zh-TW
    "O painel Todos da barra lateral continua disponível",  # pt
    "사이드바 Todos 패널은 계속 사용할 수 있습니다",  # ko
    "Le panneau Todos de la barre latérale reste disponible",  # fr
    "Panel Úkoly v bočním panelu zůstává dostupný",  # cs
    "Kenar çubuğu Todos paneli kullanılabilir olmaya devam eder",  # tr
    "Panel Todos na pasku bocznym pozostaje dostępny",  # pl
    "Panel Todos ở sidebar vẫn có sẵn",  # vi
)


def test_workspace_todos_desc_drops_the_contradictory_sidebar_sentence():
    """Ask 4: the disabled "Show Todos tab in workspace panel" card still claimed
    the sidebar Todos panel "remains available regardless", directly under a card
    that says it is hidden. The sentence is gone in EVERY locale, not just
    English."""
    i18n = _read_static("static/i18n.js")
    idx = _read_static("static/index.html")
    for tail in _REMOVED_TODOS_DESC_TAILS:
        assert tail not in i18n, f"locale tail still present: {tail!r}"
    # The HTML fallback text (pre-i18n first paint) drops it too.
    assert "remains available regardless" not in idx
    # ...while the key itself survives in all 15 locales that carried it.
    assert i18n.count("settings_desc_workspace_todos_tab") == 15
    assert (
        "settings_desc_workspace_todos_tab: 'When enabled, a Todos tab appears in the workspace panel.',"
        in i18n
    )


# ── Stale tab-visibility mirror while a profile switch reconciles ──────────
# greptile P1 (2026-10-08T20:06:51Z, static/ui.js:10501): "When a user switches
# between profiles with different hidden_tabs settings and disables the tray
# before the asynchronous settings reconciliation completes, this path
# reapplies the previous profile's global localStorage snapshot." Releasing the
# tray's forced rail hide re-derives visibility from that mirror, so it must be
# skipped until the switch's /api/settings reconciliation lands.

_RAIL_RELEASE_PROBE = """
function assert(cond, msg) { if (!cond) throw new Error(msg); }
function _el() {
  const s = new Set();
  return {classList: {
    add(c) { s.add(c); },
    remove(c) { s.delete(c); },
    contains(c) { return s.has(c); },
    toggle(c, on) { if (on) s.add(c); else s.delete(c); return !!on; },
  }};
}
const todosEls = [_el(), _el()];
globalThis.document = {
  querySelectorAll(sel) { return sel === '[data-panel="todos"]' ? todosEls : []; },
  getElementById() { return null; },
  querySelector() { return null; },
};
let trayOn = false;
let stale = false;
let applied = [];
function chatTodosEnabled() { return trayOn; }
function _getHiddenTabs() { return ['notes']; }
function _applyTabVisibility(h) { applied.push(h.slice()); }
globalThis._tabVisibilitySnapshotStale = function () { return stale; };
__HELPER__

// 1. Settled mirror, tray OFF: visibility still goes back to the canonical
//    owner (the 2026-10-07 greptile P1 behaviour must survive).
_syncChatTodosRailVisibility();
assert(applied.length === 1 && applied[0].join() === 'notes',
  'a settled mirror must still hand visibility back to hidden_tabs');
assert(todosEls.every(function (el) { return !el.classList.contains('nav-tab-hidden'); }),
  'a settled release must not force the class either way');

// 2. THE FIX: the same click while the switch's reconciliation is in flight.
//    The mirror still holds the PREVIOUS profile's list, so re-deriving from it
//    would reimpose that profile's tab visibility on the profile now in effect.
applied = [];
stale = true;
_syncChatTodosRailVisibility();
assert(applied.length === 0,
  'the stale previous-profile mirror must not be re-applied mid-switch');

// 3. The reconciliation lands: the release is live again.
stale = false;
_syncChatTodosRailVisibility();
assert(applied.length === 1 && applied[0].join() === 'notes',
  'the release resumes once the reconciliation settles');

// 4. Tray ON is unaffected: it still force-hides the duplicate rail entry and
//    never re-derives visibility (that is the reconciliation's job).
todosEls.forEach(function (el) { el.classList.remove('nav-tab-hidden'); });
applied = [];
trayOn = true;
stale = true;
_syncChatTodosRailVisibility();
assert(todosEls.every(function (el) { return el.classList.contains('nav-tab-hidden'); }),
  'tray ON still force-hides the Todos rail entry mid-switch');
assert(applied.length === 0, 'tray ON never re-derives tab visibility');

// 5. No guard in scope (panels.js not loaded yet) keeps the previous behaviour.
trayOn = false;
applied = [];
delete globalThis._tabVisibilitySnapshotStale;
_syncChatTodosRailVisibility();
assert(applied.length === 1, 'without the guard the release still defers to hidden_tabs');
console.log('ok');
"""


def test_rail_release_skips_the_stale_mirror_during_a_profile_switch(tmp_path):
    """greptile P1 (2026-10-08T20:06:51Z): the tray-off release must not reapply
    the previous profile's hidden_tabs snapshot while the switch's
    /api/settings reconciliation is still in flight."""
    ui = _read_static("static/ui.js")
    helper = _extract(
        ui, "function _syncChatTodosRailVisibility(){", "let _chatTodosResizeObserver"
    )
    # The re-derive is wrapped in the reconciliation guard...
    guard_at = helper.find("_tabVisibilitySnapshotStale")
    call_at = helper.find("_applyTabVisibility(_getHiddenTabs())")
    assert guard_at != -1, "the tray release never consults the reconciliation guard"
    assert call_at != -1
    assert guard_at < call_at, "the guard must wrap the re-derive, not follow it"
    script = _RAIL_RELEASE_PROBE.replace("__HELPER__", helper)
    assert _run_node(tmp_path, "rail_release_probe.js", script).strip() == "ok"


def _extract_reconcile_guard(panels: str) -> tuple[str, str]:
    """The tray-sync guard's declarations and its two functions.

    ``counter`` carries BOTH counters (the reconciliation depth and the
    switch-in-flight marker); ``guard`` covers _tabVisibilitySnapshotStale()
    through _maybeReplayChatTodosRailSync(), so a probe that only inlines these
    two slices gets the complete replay decision (greptile P1,
    static/panels.js:6783, 2026-10-09T00:15:45Z).
    """
    start = panels.index("let _tabVisReconcilePending = 0;")
    stale_at = panels.index("function _tabVisibilitySnapshotStale(){")
    reconcile_at = panels.index("function _refreshProfileSwitchBackground(gen){")
    counter, guard = panels[start:stale_at], panels[stale_at:reconcile_at]
    assert "let _profileSwitchInFlight = 0;" in counter, (
        "the switch-in-flight marker the replay consults is missing from static/panels.js"
    )
    assert "function _maybeReplayChatTodosRailSync(){" in guard, (
        "the tray rail replay helper is missing from static/panels.js"
    )
    return counter, guard


_RECONCILE_GUARD_PROBE = """
function assert(cond, msg) { if (!cond) throw new Error(msg); }
__COUNTER__
__GUARD__
globalThis.window = {};
const S = {session: null};
let _profileSwitchGeneration = 7;
let fulfill = null;
let fail = null;
let applied = [];
let stored = null;
function api(path) {
  assert(path === '/api/settings', 'unexpected api path: ' + path);
  return new Promise(function (res, rej) { fulfill = res; fail = rej; });
}
function loadWorkspaceList() { return Promise.resolve(); }
function syncTopbar() {}
function _setHiddenTabs(h) { stored = h.slice(); }
function _setTabOrder() {}
function _applyTabOrder() {}
function _applyTabVisibility(h) { applied.push(h.slice()); }
function _ensureComposerControlVisibilityState() {}
function _setComposerControlOrder() { return []; }
function _renderComposerControlChips() {}
function _renderComposerSituationalControlChips() {}
function _applyComposerFooterVisibilitySettings() {}
function _applyTitlebarProfileVisibility() {}
let resyncs = 0;
function _syncChatTodosRailVisibility() { resyncs++; }
__HELPER__
function settled() { return new Promise(function (r) { setImmediate(r); }); }
(async function () {
  assert(_tabVisibilitySnapshotStale() === false, 'idle: the mirror is authoritative');
  _refreshProfileSwitchBackground(_profileSwitchGeneration);
  assert(_tabVisibilitySnapshotStale() === true,
    'an in-flight /api/settings reconciliation makes the mirror stale');
  assert(applied.length === 0 && stored === null, 'nothing is read off the stale mirror');
  assert(resyncs === 0, 'the tray rail sync must not replay inside the stale window');

  fulfill({hidden_tabs: ['todos'], tab_order: ['chat']});
  await settled();
  assert(applied.length === 1 && applied[0].join() === 'todos',
    'the reconciliation applies the server list, not the stale mirror');
  assert(stored.join() === 'todos', 'the mirror is rewritten from the server list');
  assert(_tabVisibilitySnapshotStale() === false, 'the guard releases when it settles');
  assert(resyncs === 1,
    'the release must replay the tray rail sync once the mirror is authoritative');

  // A FAILED reconciliation must release the guard too, otherwise tab
  // visibility stays pinned to the stale window for the rest of the session.
  // The rail sync must also still replay: the release path used to skip it
  // while the mirror was stale and never re-run it, so a tray disabled during
  // a failed reconciliation left the Todos entry hidden (reviewer re-gate
  // 2026-10-08T23:19:27Z, static/panels.js:6766).
  applied = [];
  _refreshProfileSwitchBackground(_profileSwitchGeneration);
  assert(_tabVisibilitySnapshotStale() === true, 'a second reconciliation re-arms the guard');
  fail(new Error('network'));
  await settled();
  assert(_tabVisibilitySnapshotStale() === false, 'a failed reconciliation releases the guard');
  assert(resyncs === 2, 'a failed reconciliation must still replay the tray rail sync');

  // A SUPERSEDED reconciliation (a newer switch bumped the generation) never
  // rewrote the mirror, so while that newer switch is STILL IN FLIGHT its
  // release must NOT replay the rail sync — it can be the last release to see a
  // zero counter while the newer switch is still awaiting its POST, and
  // re-deriving from the stale mirror would reimpose the previous profile's
  // hidden_tabs (greptile P1, 2026-10-08T23:51:52Z).
  var supersededGen = _profileSwitchGeneration;
  _profileSwitchGeneration++;              // the newer switch
  _profileSwitchInFlight = 1;              // ...which is still running
  applied = [];
  _refreshProfileSwitchBackground(supersededGen);
  assert(_tabVisibilitySnapshotStale() === true,
    'a superseded reconciliation still arms the guard');
  fulfill({hidden_tabs: ['notes'], tab_order: ['chat']});
  await settled();
  assert(_tabVisibilitySnapshotStale() === false,
    'a superseded reconciliation still releases the guard');
  assert(applied.length === 0,
    'a superseded reconciliation must not apply its stale snapshot');
  assert(resyncs === 2,
    'a superseded reconciliation must not replay while its newer switch is in flight');

  // ...but once that newer switch FAILS, it will never run a reconciliation of
  // its own: this superseded release IS the last one and the mirror stays
  // authoritative, so the rail sync MUST replay — otherwise a tray disabled
  // during the window left the Todos rail entry stale (greptile P1,
  // static/panels.js:6783, 2026-10-09T00:15:45Z).
  var failedGen = _profileSwitchGeneration;
  _profileSwitchGeneration++;
  _profileSwitchInFlight = 1;              // the newer switch starts...
  applied = [];
  _refreshProfileSwitchBackground(failedGen);
  _profileSwitchInFlight = 0;              // ...and its profile-switch request fails
  fulfill({hidden_tabs: ['notes'], tab_order: ['chat']});
  await settled();
  assert(_tabVisibilitySnapshotStale() === false,
    'a superseded reconciliation still releases the guard');
  assert(applied.length === 0,
    'a superseded reconciliation must not apply its stale snapshot');
  assert(resyncs === 3,
    'a FAILED newer switch must not suppress the replay: the mirror is authoritative again');
  console.log('ok');
})().catch(function (e) { console.error(e && e.stack || e); process.exit(1); });
"""


def test_profile_switch_reconciliation_marks_the_tab_mirror_stale(tmp_path):
    """The guard the tray release consults is armed by the switch's
    /api/settings reconciliation and released on BOTH settle paths (a stranded
    guard would pin tab visibility to the stale window for the session)."""
    panels = _read_static("static/panels.js")
    counter, guard = _extract_reconcile_guard(panels)
    block = _extract(
        panels,
        "function _refreshProfileSwitchBackground(gen){",
        "async function loadProfilesPanel()",
    )
    inc_at = block.find("_tabVisReconcilePending++;")
    fetch_at = block.find("Promise.resolve(api('/api/settings'))")
    release_at = block.find("_tabVisReconcilePending--;")
    assert inc_at != -1 and fetch_at != -1 and release_at != -1
    assert inc_at < fetch_at, "the guard must be armed before the reconciliation fetch"
    assert fetch_at < release_at, "the guard must be released with the reconciliation"
    assert "}).catch(function(){}).then(function(){" in block, (
        "the release must sit after the swallowed rejection so both settle paths clear it"
    )
    # one arm + one release per reconciliation; the release then delegates the
    # replay decision to the guard helper, which is what a probe can exercise
    # (greptile P1, 2026-10-08T23:51:52Z → 2026-10-09T00:15:45Z).
    assert block.count("_tabVisReconcilePending") == 2, (
        "exactly one arm and one release per reconciliation"
    )
    replay_at = block.find("_maybeReplayChatTodosRailSync();", release_at)
    assert replay_at != -1, (
        "the release must request the tray's rail syncing once the mirror is "
        "authoritative (re-gate 2026-10-08T23:19:27Z, static/panels.js:6766)"
    )
    # The helper owns the decision: replay only when no reconciliation is in
    # flight AND no switch is still running to rewrite the mirror.
    assert "if (_tabVisReconcilePending > 0) return;" in guard, (
        "the replay must be gated on the reconciliation counter reaching zero"
    )
    assert "if (_profileSwitchInFlight > 0) return;" in guard, (
        "the replay must be suppressed while a newer switch is still in flight"
    )
    assert "if (typeof _syncChatTodosRailVisibility === 'function') _syncChatTodosRailVisibility();" in guard
    script = (
        _RECONCILE_GUARD_PROBE.replace("__COUNTER__", counter)
        .replace("__GUARD__", guard)
        .replace("__HELPER__", block)
    )
    assert _run_node(tmp_path, "reconcile_guard_probe.js", script).strip() == "ok"


_OVERLAPPING_SWITCHES_PROBE = """
function assert(cond, msg) { if (!cond) throw new Error(msg); }
__COUNTER__
__GUARD__
globalThis.window = {};
const S = {session: null};
let _profileSwitchGeneration = 0;
let pending = [];
let applied = [];
let stored = null;
function api(path) {
  assert(path === '/api/settings', 'unexpected api path: ' + path);
  return new Promise(function (res, rej) { pending.push({res: res, rej: rej}); });
}
function loadWorkspaceList() { return Promise.resolve(); }
function syncTopbar() {}
function _setHiddenTabs(h) { stored = h.slice(); }
function _setTabOrder() {}
function _applyTabOrder() {}
function _applyTabVisibility(h) { applied.push(h.slice()); }
function _ensureComposerControlVisibilityState() {}
function _setComposerControlOrder() { return []; }
function _renderComposerControlChips() {}
function _renderComposerSituationalControlChips() {}
function _applyComposerFooterVisibilitySettings() {}
function _applyTitlebarProfileVisibility() {}
let resyncs = 0;
function _syncChatTodosRailVisibility() { resyncs++; }
__HELPER__
function settled() { return new Promise(function (r) { setImmediate(r); }); }
(async function () {
  // Maintainer re-gate 2026-10-09T00:47:02Z (static/panels.js:6781): "switch to
  // B, then C; receive C's settings; disable the tray; receive B's settings" —
  // the final state used to be tray OFF, hidden_tabs [], guard cleared, Todos
  // still hidden, because C could not replay while B held the counter and B
  // could not replay because its generation was superseded. Both switches have
  // already handed their guard to their own reconciliation, so no switch is in
  // flight any more.
  _profileSwitchGeneration = 1;
  _refreshProfileSwitchBackground(1);              // B's settings fetch
  assert(pending.length === 1, 'B must issue its settings fetch');
  _profileSwitchGeneration = 2;
  _refreshProfileSwitchBackground(2);              // C's settings fetch
  assert(pending.length === 2, 'C must issue its settings fetch');
  assert(_tabVisibilitySnapshotStale() === true,
    'the mirror is stale while both reconciliations are in flight');
  assert(resyncs === 0, 'nothing may replay inside the stale window');

  // C's (authoritative) settings arrive first.
  pending[1].res({hidden_tabs: [], tab_order: ['chat']});
  await settled();
  assert(_tabVisibilitySnapshotStale() === true,
    'B still holds the counter after C settles');
  assert(resyncs === 0,
    'C must not replay while B keeps the counter positive');

  // ...then the SUPERSEDED B settles last and takes the counter to zero.
  pending[0].res({hidden_tabs: ['todos'], tab_order: ['chat']});
  await settled();
  assert(_tabVisibilitySnapshotStale() === false,
    'the guard clears once the counter reaches zero');
  assert(resyncs === 1,
    'the release that brings the counter to zero must replay regardless of which '
    + 'request releases last (got ' + resyncs + ' resyncs)');
  console.log('ok');
})().catch(function (e) { console.error(e && e.stack || e); process.exit(1); });
"""


def test_overlapping_switches_replay_when_the_counter_reaches_zero(tmp_path):
    """Maintainer re-gate 2026-10-09T00:47:02Z, static/panels.js:6781: with two
    overlapping profile switches, the release that reaches a zero counter may
    belong to the SUPERSEDED reconciliation. It must still replay the tray rail
    sync (nothing else ever will), while a superseded release that is NOT the
    last one still must not."""
    panels = _read_static("static/panels.js")
    counter, guard = _extract_reconcile_guard(panels)
    block = _extract(
        panels,
        "function _refreshProfileSwitchBackground(gen){",
        "async function loadProfilesPanel()",
    )
    script = (
        _OVERLAPPING_SWITCHES_PROBE.replace("__COUNTER__", counter)
        .replace("__GUARD__", guard)
        .replace("__HELPER__", block)
    )
    assert _run_node(tmp_path, "overlapping_switches_probe.js", script).strip() == "ok"


def _extract_switch_guard_arms(panels: str) -> tuple[str, str, str]:
    """The three shipped statements of the switch's stale-snapshot guard:
    arm (at S.activeProfile), handoff (to the reconciliation), finally release."""
    switch = _extract(
        panels, "async function switchToProfile(name) {", "function openProfileCreate(){"
    )
    assign_at = switch.find("S.activeProfile = data.active || name;")
    arm = re.search(
        r"_tabVisGuardHeld = true;\s*\n\s*if \(typeof _tabVisReconcilePending === 'number'\) _tabVisReconcilePending\+\+;",
        switch,
    )
    handoff = re.search(
        r"if \(_tabVisGuardHeld && typeof _tabVisReconcilePending === 'number'\) "
        r"_tabVisReconcilePending--;\s*\n\s*_tabVisGuardHeld = false;\s*\n\s*"
        r"_refreshProfileSwitchBackground\(_switchGen\);",
        switch,
    )
    release = re.search(
        r"if \(_tabVisGuardHeld\) \{ _tabVisGuardHeld = false; if \(typeof "
        r"_tabVisReconcilePending === 'number'\) _tabVisReconcilePending--; \}",
        switch,
    )
    assert assign_at != -1 and arm and handoff and release, (
        "the switch must arm the stale-snapshot guard at S.activeProfile, hand it "
        "to the reconciliation, and release it on the other exits"
    )
    assert assign_at < arm.start(), (
        "the guard must be armed when S.activeProfile changes, not later"
    )
    assert arm.start() < handoff.start() < release.start(), (
        "the arm must precede the handoff, which must precede the finally release"
    )
    finally_at = switch.find("} finally {")
    assert finally_at != -1 and finally_at < release.start(), (
        "the switch-owned release must live in the finally block"
    )
    return arm.group(0), handoff.group(0), release.group(0)


_SWITCH_GUARD_PROBE = r"""
function assert(cond, msg) { if (!cond) throw new Error(msg); }
__COUNTER__
__GUARD__
let _tabVisGuardHeld = false;
globalThis.window = {};
const S = {session: null};
let _profileSwitchGeneration = 11;
const _switchGen = _profileSwitchGeneration;
let fulfill = null;
let applied = [];
let stored = null;
let mirror = ['notes'];  // the PREVIOUS profile's hidden_tabs mirror
function api(path) {
  assert(path === '/api/settings', 'unexpected api path: ' + path);
  return new Promise(function (res) { fulfill = res; });
}
function loadWorkspaceList() { return Promise.resolve(); }
function syncTopbar() {}
function _setHiddenTabs(h) { stored = h.slice(); mirror = h.slice(); }
function _setTabOrder() {}
function _applyTabOrder() {}
function _applyTabVisibility(h) { applied.push(h.slice()); }
function _ensureComposerControlVisibilityState() {}
function _setComposerControlOrder() { return []; }
function _renderComposerControlChips() {}
function _renderComposerSituationalControlChips() {}
function _applyComposerFooterVisibilitySettings() {}
function _applyTitlebarProfileVisibility() {}
__RECONCILE__
let trayOn = false;
function chatTodosEnabled() { return trayOn; }
function _getHiddenTabs() { return mirror.slice(); }
const todosEls = [{classList:{_s:new Set(),add(c){this._s.add(c);},remove(c){this._s.delete(c);},contains(c){return this._s.has(c);}}}];
globalThis.document = {
  querySelectorAll(sel) { return sel === '[data-panel="todos"]' ? todosEls : []; },
  getElementById() { return null; },
  querySelector() { return null; },
};
globalThis._tabVisibilitySnapshotStale = _tabVisibilitySnapshotStale;
__RAIL__
function settled() { return new Promise(function (r) { setImmediate(r); }); }
(async function () {
  assert(_tabVisibilitySnapshotStale() === false, 'idle: the mirror is authoritative');
  // The switch arms the guard the instant S.activeProfile changes (SHIPPED).
  __ARM__
  assert(_tabVisibilitySnapshotStale() === true,
    'S.activeProfile changing must arm the guard, not only the later reconciliation');
  // ...so a tray-off click in the gap before the reconciliation starts cannot
  // re-derive visibility from the previous profile's hidden_tabs mirror.
  applied = [];
  _syncChatTodosRailVisibility();
  assert(applied.length === 0,
    'a tray-off release in the gap must not re-derive the previous profile mirror');
  // The switch then hands ownership to the reconciliation (SHIPPED statements).
  __HANDOFF__
  assert(_tabVisibilitySnapshotStale() === true,
    'the handed-off reconciliation keeps the mirror stale while it is in flight');
  fulfill({hidden_tabs: ['todos'], tab_order: ['chat']});
  await settled();
  // The reconciliation applies the server list first; its release then replays
  // the tray rail sync (reviewer re-gate 2026-10-08T23:19:27Z), which re-derives
  // from the NOW-rewritten mirror — so the only list that may ever appear is the
  // server's, never the previous profile's ['notes'].
  assert(applied.length >= 1 && applied[0].join() === 'todos',
    'the reconciliation applies the new profile list off the server, not the stale mirror');
  assert(applied.every(function (h) { return h.join() !== 'notes'; }),
    'nothing may re-derive visibility from the previous profile mirror');
  assert(stored.join() === 'todos', 'the mirror is rewritten from the server list');
  assert(_tabVisibilitySnapshotStale() === false,
    'the switch-armed guard is released once the reconciliation settles');

  // A switch that never reaches the reconciliation must release its own arm,
  // otherwise tab visibility stays pinned to the stale window for the session.
  applied = [];
  __ARM__
  assert(_tabVisibilitySnapshotStale() === true, 're-armed for the non-success exit');
  __FINALLY_RELEASE__
  assert(_tabVisibilitySnapshotStale() === false,
    'a failed / superseded switch releases its own arm');
  console.log('ok');
})().catch(function (e) { console.error(e && e.stack || e); process.exit(1); });
"""


def test_profile_switch_arms_the_tab_mirror_guard_at_active_profile_change(tmp_path):
    """greptile P1 (2026-10-08T22:06:59Z): the tray-off release must be guarded
    from the instant S.activeProfile changes, not only once
    _refreshProfileSwitchBackground() starts — everything between is async, so a
    click landing in the gap re-derived the previous profile's hidden_tabs."""
    panels = _read_static("static/panels.js")
    ui = _read_static("static/ui.js")
    counter, guard = _extract_reconcile_guard(panels)
    reconcile = _extract(
        panels,
        "function _refreshProfileSwitchBackground(gen){",
        "async function loadProfilesPanel()",
    )
    rail = _extract(
        ui, "function _syncChatTodosRailVisibility(){", "let _chatTodosResizeObserver"
    )
    arm, handoff, release = _extract_switch_guard_arms(panels)
    script = (
        _SWITCH_GUARD_PROBE.replace("__COUNTER__", counter)
        .replace("__GUARD__", guard)
        .replace("__RECONCILE__", reconcile)
        .replace("__RAIL__", rail)
        .replace("__ARM__", arm)
        .replace("__HANDOFF__", handoff)
        .replace("__FINALLY_RELEASE__", release)
    )
    assert _run_node(tmp_path, "switch_guard_probe.js", script).strip() == "ok"


