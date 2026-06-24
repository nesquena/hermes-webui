import json
import pathlib
import shutil
import subprocess

import pytest


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
NODE = shutil.which("node")


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


_SWITCH_PANEL_DRIVER = r"""
const fs = require('fs');

function extractFunction(src, signature) {
  const start = src.indexOf(signature);
  if (start < 0) throw new Error(signature + ' not found');
  let depth = 0;
  let bodyStart = src.indexOf('{', src.indexOf(')', start));
  for (let i = bodyStart; i < src.length; i++) {
    const ch = src[i];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(signature + ' body not closed');
}

function createClassList(initial = []) {
  const set = new Set(initial);
  return {
    add(...names) { names.forEach(name => set.add(name)); },
    remove(...names) { names.forEach(name => set.delete(name)); },
    toggle(name, force) {
      if (force === undefined) {
        if (set.has(name)) set.delete(name);
        else set.add(name);
        return set.has(name);
      }
      if (force) set.add(name);
      else set.delete(name);
      return !!force;
    },
    contains(name) { return set.has(name); },
    toArray() { return Array.from(set.values()); },
  };
}

const src = fs.readFileSync(process.argv[2], 'utf8');
const args = JSON.parse(process.argv[3]);
const toggleCalls = [];
const storage = new Map();

const navButtons = ['chat', 'terminal'].map(panel => ({
  dataset: { panel },
  classList: createClassList(panel === 'chat' ? ['active'] : []),
}));
const panelChat = { classList: createClassList(['panel-view', 'active']) };
const panelTerminal = { classList: createClassList(['panel-view']) };
const mainEl = { classList: createClassList([]) };
const sidebar = { classList: createClassList([]) };
const byId = {
  panelChat,
  panelTerminal,
};

globalThis._currentPanel = args.currentPanel || 'chat';
globalThis._currentSettingsSection = 'conversation';
globalThis.MAIN_VIEW_PANELS = ['settings','skills','memory','tasks','kanban','workspaces','profiles','insights','terminal','logs','plugin'];
globalThis.$ = (id) => byId[id] || null;
globalThis.document = {
  querySelectorAll(selector) {
    if (selector === '[data-panel]') return navButtons;
    if (selector === '.panel-view') return [panelChat, panelTerminal];
    return [];
  },
  querySelector(selector) {
    if (selector === 'main.main') return mainEl;
    if (selector === '.sidebar') return sidebar;
    return null;
  },
};
globalThis.localStorage = {
  getItem(key) { return storage.has(key) ? storage.get(key) : null; },
  setItem(key, value) { storage.set(key, String(value)); },
};
globalThis._canStartComposerTerminal = () => args.canStart !== false;
globalThis._isSidebarCollapsed = () => false;
globalThis._isDesktopWidth = () => true;
globalThis.expandSidebar = () => {};
globalThis.toggleSidebar = () => {};
globalThis._beforePanelSwitch = () => true;
globalThis._beginSettingsPanelSession = () => {};
globalThis._kanbanStopPolling = () => {};
globalThis._syncSidebarAria = () => {};
globalThis.loadCrons = async () => {};
globalThis.loadKanban = async () => {};
globalThis.loadSkills = async () => {};
globalThis.loadMemory = async () => {};
globalThis.loadWorkspacesPanel = async () => {};
globalThis.loadProfilesPanel = async () => {};
globalThis.loadTodos = () => {};
globalThis.loadInsights = async () => {};
globalThis.loadLogs = async () => {};
globalThis._syncLogsAutoRefresh = () => {};
globalThis._syncSystemHealthMonitorVisibility = () => {};
globalThis.switchSettingsSection = () => {};
globalThis.loadSettingsPanel = () => {};
globalThis._resyncChatSidebarAfterPanelSwitch = () => {};
globalThis.syncTopbar = () => {};
globalThis.syncAppTitlebar = () => {};
globalThis.TERMINAL_UI = { open: false };
globalThis.toggleComposerTerminal = async (_force, opts) => {
  toggleCalls.push(opts || {});
  return args.toggleResult;
};

eval(extractFunction(src, 'async function switchPanel'));

(async () => {
  const result = await switchPanel('terminal', { fromRailClick: false });
  process.stdout.write(JSON.stringify({
    result,
    currentPanel: globalThis._currentPanel,
    toggleCalls,
    mainClasses: mainEl.classList.toArray(),
    panelTerminalActive: panelTerminal.classList.contains('active'),
    panelChatActive: panelChat.classList.contains('active'),
    terminalNavActive: navButtons[1].classList.contains('active'),
    chatNavActive: navButtons[0].classList.contains('active'),
  }));
})().catch(err => {
  process.stderr.write(String(err && err.stack ? err.stack : err));
  process.exit(1);
});
"""


node_test = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.fixture(scope="module")
def switch_panel_driver(tmp_path_factory):
    path = tmp_path_factory.mktemp("terminal_first_class_nav") / "switch_panel_driver.js"
    path.write_text(_SWITCH_PANEL_DRIVER, encoding="utf-8")
    return str(path)


def _run_switch_panel_case(driver_path: str, payload: dict) -> dict:
    result = subprocess.run(
        [NODE, driver_path, str(REPO_ROOT / "static" / "panels.js"), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    return json.loads(result.stdout)


def test_terminal_nav_registers_page_view_and_locale_label():
    html = _read("static/index.html")
    panels_js = _read("static/panels.js")
    i18n_js = _read("static/i18n.js")
    style_css = _read("static/style.css")

    assert 'data-panel="terminal"' in html
    assert "switchPanel('terminal',{fromRailClick:true})" in html
    assert 'id="mainTerminal"' in html
    assert 'id="panelTerminal"' in html
    assert "terminal: 'tab_terminal'" in panels_js
    assert "terminal: 'tab_terminal'" not in html
    assert "tab_terminal:" in i18n_js
    assert "showing-terminal" in style_css
    assert "main.main.showing-terminal > #mainTerminal{display:flex;overflow:hidden;}" in style_css


def test_terminal_page_mode_reuses_the_existing_runtime():
    panels_js = _read("static/panels.js")
    terminal_js = _read("static/terminal.js")

    assert "MAIN_VIEW_PANELS = ['settings','skills','memory','tasks','kanban','workspaces','profiles','insights','terminal','logs','plugin'];" in panels_js
    assert "await toggleComposerTerminal(true, { mode: 'page' });" in panels_js
    assert "await toggleComposerTerminal(true, { mode: 'dock' });" in panels_js
    assert "const desiredMode=opts.mode==='page'?'page':'dock';" in terminal_js
    assert "_terminalSetPresentationMode(desiredMode)" in terminal_js
    assert terminal_js.count("new window.Terminal(") == 1
    assert terminal_js.count("new EventSource(") == 1


@node_test
def test_switch_panel_activates_terminal_page_mode_on_success(switch_panel_driver):
    data = _run_switch_panel_case(switch_panel_driver, {
        "currentPanel": "chat",
        "canStart": True,
        "toggleResult": True,
    })

    assert data["result"] is True
    assert data["currentPanel"] == "terminal"
    assert data["toggleCalls"] == [{"mode": "page"}]
    assert "showing-terminal" in data["mainClasses"]
    assert data["panelTerminalActive"] is True
    assert data["terminalNavActive"] is True


@node_test
def test_switch_panel_rolls_back_when_terminal_page_start_fails(switch_panel_driver):
    data = _run_switch_panel_case(switch_panel_driver, {
        "currentPanel": "chat",
        "canStart": True,
        "toggleResult": False,
    })

    assert data["result"] is True
    assert data["currentPanel"] == "chat"
    assert data["toggleCalls"] == [{"mode": "page"}]
    assert "showing-terminal" not in data["mainClasses"]
    assert data["panelChatActive"] is True
    assert data["chatNavActive"] is True


def test_terminal_page_layout_scopes_dock_only_ui():
    style_css = _read("static/style.css")
    terminal_js = _read("static/terminal.js")

    assert "#panelTerminal .composer-terminal-panel.is-page-mode" in style_css
    assert "#panelTerminal .composer-terminal-panel.is-page-mode .composer-terminal-dock{display:none;}" in style_css
    assert "#panelTerminal .composer-terminal-panel.is-page-mode .composer-terminal-resize-handle" in style_css
    assert "TERMINAL_UI.mode==='page'" in terminal_js
    assert "messages.classList.remove('terminal-open');" in terminal_js
    assert "messages.classList.remove('terminal-collapsed');" in terminal_js
