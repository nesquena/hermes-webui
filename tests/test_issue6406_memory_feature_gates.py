import json
import pathlib
import shutil
import subprocess
from types import SimpleNamespace

import api.profiles
import api.routes as routes
import pytest


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
NODE = shutil.which("node")


def _setup_home(tmp_path, monkeypatch, config):
    home = tmp_path / "home"
    memories = home / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text("memory body", encoding="utf-8")
    (memories / "USER.md").write_text("user body", encoding="utf-8")
    (home / "SOUL.md").write_text("soul body", encoding="utf-8")
    monkeypatch.setattr(api.profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(routes, "get_config", lambda: config)
    monkeypatch.setattr(routes, "_memory_project_context_workspace", lambda _parsed: tmp_path)
    monkeypatch.setattr(routes, "_external_notes_sources_enabled", lambda: True)
    captured = {}
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: captured.setdefault("payload", payload))
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: captured.setdefault("error", (message, status)),
    )
    return home, captured


def _read(home, captured):
    routes._handle_memory_read(object(), SimpleNamespace(query=""))
    return captured["payload"]


def _write(section, captured):
    captured.pop("error", None)
    captured.pop("payload", None)
    routes._handle_memory_write(object(), {"section": section, "content": "changed"})
    return captured.get("error")


def _panel_rows(data):
    panels = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    sections_start = panels.index("const MEMORY_SECTIONS = [")
    sections_end = panels.index("];", sections_start) + 2
    helper_start = panels.index("function _memorySectionMeta(key)")
    helper_end = panels.index("function _memorySectionLabel", helper_start)
    loop_start = panels.index("for (const s of MEMORY_SECTIONS) {")
    loop_end = panels.index("    if (_currentMemorySection && _memoryMode !== 'edit')", loop_start)
    script = (
        "const sections = " + json.dumps(panels[sections_start:sections_end]) + ";\n"
        "const helper = " + json.dumps(panels[helper_start:helper_end]) + ";\n"
        "const loop = " + json.dumps(panels[loop_start:loop_end].rsplit("    }", 1)[0]) + ";\n"
        "let _memoryData = " + json.dumps(data) + ";\n"
        + r"""
const nodes = {memoryPanel: {appended: [], innerHTML: '', appendChild(node) { this.appended.push(node); }}};
const panel = nodes.memoryPanel;
let _currentMemorySection = 'memory';
const document = {createElement() { return {innerHTML: '', title: '', classList: {add() {}}}; }};
function _memorySectionLabel(meta) { return meta.key; }
function _memorySectionPath() { return ''; }
function li() { return ''; }
function esc(value) { return String(value); }
function openMemorySection() {}
eval(sections + "\n" + helper + "\nfunction renderRows() {" + loop + "}\nrenderRows();");
console.log(JSON.stringify(nodes.memoryPanel.appended.map(button => button.innerHTML.replace(/<[^>]+>/g, ''))));
"""
    )
    completed = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


_PANEL_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2];

function extractFunc(name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  const braceStart = src.indexOf('{', start);
  let depth = 0;
  let inString = null;
  let escaped = false;
  let inLineComment = false;
  let inBlockComment = false;
  for (let i = braceStart; i < src.length; i++) {
    const ch = src[i];
    const next = i + 1 < src.length ? src[i + 1] : '';
    if (inLineComment) {
      if (ch === '\n') inLineComment = false;
      continue;
    }
    if (inBlockComment) {
      if (ch === '*' && next === '/') {
        inBlockComment = false;
        i++;
      }
      continue;
    }
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === inString) inString = null;
      continue;
    }
    if (ch === '/' && next === '/') {
      inLineComment = true;
      i++;
      continue;
    }
    if (ch === '/' && next === '*') {
      inBlockComment = true;
      i++;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === '`') {
      inString = ch;
      continue;
    }
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(name + ' brace scan failed');
}

function extractConst(name) {
  const start = src.indexOf('const ' + name + ' = [');
  if (start < 0) throw new Error(name + ' not found');
  const end = src.indexOf('];', start);
  if (end < 0) throw new Error(name + ' terminator not found');
  return src.slice(start, end + 2);
}

function makeClassList(owner) {
  return {
    add(name) { owner._classes.add(name); },
    remove(name) { owner._classes.delete(name); },
    contains(name) { return owner._classes.has(name); },
  };
}

function makeEl(id = '') {
  const el = {
    id,
    children: [],
    _classes: new Set(),
    classList: null,
    style: {},
    innerHTML: '',
    textContent: '',
    title: '',
    value: '',
    onclick: null,
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    querySelector(selector) {
      if (selector === '.main-view-header') return this._header || null;
      return null;
    },
    focus() {
      this.focused = true;
    },
  };
  el.classList = makeClassList(el);
  return el;
}

let _memoryData = null;
let _currentMemorySection = null;
let _memoryMode = 'empty';
let _notesSourcesData = null;
let _notesSearchResults = [];
let _notesPreviewNote = null;
let _notesSearchError = '';
let _notesSearchLoading = false;
let _notesSelectedSource = 'joplin';
let S = {session: null};

const nodes = {
  memoryPanel: makeEl('memoryPanel'),
  memoryDetailTitle: makeEl('memoryDetailTitle'),
  memoryDetailBody: makeEl('memoryDetailBody'),
  memoryDetailEmpty: makeEl('memoryDetailEmpty'),
  mainMemory: makeEl('mainMemory'),
  btnEditMemoryDetail: makeEl('btnEditMemoryDetail'),
  btnCancelMemoryDetail: makeEl('btnCancelMemoryDetail'),
  btnSaveMemoryDetail: makeEl('btnSaveMemoryDetail'),
  memEditContent: makeEl('memEditContent'),
  memEditError: makeEl('memEditError'),
};
nodes.memoryDetailBody.style.display = 'none';
nodes.memoryDetailEmpty.style.display = '';
nodes.memEditError.style.display = 'none';
nodes.mainMemory._header = makeEl('memoryHeader');
nodes.mainMemory._header.style.display = 'none';

global.document = {
  createElement() {
    return makeEl();
  },
  querySelectorAll(selector) {
    if (selector === '#memoryPanel .side-menu-item') return nodes.memoryPanel.children;
    return [];
  },
};
global.$ = (id) => nodes[id] || null;
global.t = (key) => key;
global.esc = (value) => String(value == null ? '' : value);
global.li = () => '';
global.showToast = () => {
  global.__toastCalls = (global.__toastCalls || 0) + 1;
};
global._memorySectionLabel = (meta) => meta.key;
global._memorySectionPath = () => '';
global._closeMobileSidebarAfterPanelSelection = () => {
  global.__closeCalls = (global.__closeCalls || 0) + 1;
};
global.__renderDetailCalls = [];
global.__renderEditCalls = [];
global._renderMemoryDetail = (section) => {
  global.__renderDetailCalls.push(section);
  _memoryMode = 'read';
  nodes.memoryDetailBody.innerHTML = 'detail:' + section;
  nodes.memoryDetailBody.style.display = '';
  nodes.memoryDetailEmpty.style.display = 'none';
};
global._renderMemoryEdit = (section) => {
  global.__renderEditCalls.push(section);
  _memoryMode = 'edit';
};
global.loadNotesSources = async () => {
  global.__notesCalls = (global.__notesCalls || 0) + 1;
  return {};
};
global._setMemoryHeaderButtons = (mode) => {
  nodes.mainMemory._header.style.display = mode === 'empty' ? 'none' : 'flex';
};

let payload = null;
const writes = [];
global.api = async (url, opts) => {
  if (url === '/api/memory' || url.startsWith('/api/memory?')) return payload;
  if (url === '/api/memory/write') {
    writes.push(JSON.parse(opts.body));
    return {};
  }
  throw new Error('unexpected url: ' + url);
};

eval(extractConst('MEMORY_SECTIONS').replace('const MEMORY_SECTIONS', 'var MEMORY_SECTIONS'));
for (const name of [
  '_memorySectionMeta',
  '_memorySectionEnabled',
  '_renderMemoryEmpty',
  'loadMemory',
  'openMemorySection',
  'editCurrentMemory',
  'submitMemorySave',
]) {
  eval(extractFunc(name));
}

function rowLabels() {
  return nodes.memoryPanel.children.map((node) => node.innerHTML.replace(/<[^>]+>/g, ''));
}

async function runStaleReset() {
  payload = {
    memory_enabled: false,
    user_profile_enabled: true,
    external_notes_enabled: true,
    user: 'user body',
    soul: '',
    project_context: '',
  };
  _currentMemorySection = 'memory';
  _memoryMode = 'edit';
  nodes.memoryDetailTitle.textContent = 'Memory';
  nodes.memoryDetailBody.innerHTML = 'stale body';
  nodes.memoryDetailBody.style.display = '';
  nodes.memoryDetailEmpty.style.display = 'none';
  nodes.mainMemory._header.style.display = 'flex';
  await loadMemory(false);
  return {
    section: _currentMemorySection,
    mode: _memoryMode,
    title: nodes.memoryDetailTitle.textContent,
    body: nodes.memoryDetailBody.innerHTML,
    bodyDisplay: nodes.memoryDetailBody.style.display,
    emptyDisplay: nodes.memoryDetailEmpty.style.display,
    headerDisplay: nodes.mainMemory._header.style.display,
    renderDetailCalls: global.__renderDetailCalls.slice(),
    rows: rowLabels(),
  };
}

async function runGuards() {
  payload = {
    memory_enabled: false,
    user_profile_enabled: true,
    external_notes_enabled: true,
    memory: '',
    user: 'user body',
    soul: '',
    project_context: '',
  };
  _memoryData = payload;
  const blocked = makeEl();
  const allowed = makeEl();
  await openMemorySection('memory', blocked);
  const blockedState = {
    current: _currentMemorySection,
    renderDetailCalls: global.__renderDetailCalls.slice(),
    closeCalls: global.__closeCalls || 0,
    blockedActive: blocked.classList.contains('active'),
  };
  await openMemorySection('user', allowed);
  const openState = {
    current: _currentMemorySection,
    renderDetailCalls: global.__renderDetailCalls.slice(),
    closeCalls: global.__closeCalls || 0,
    allowedActive: allowed.classList.contains('active'),
  };
  _currentMemorySection = 'memory';
  editCurrentMemory();
  const blockedEditCalls = global.__renderEditCalls.slice();
  _currentMemorySection = 'user';
  editCurrentMemory();
  const editState = {
    renderEditCalls: global.__renderEditCalls.slice(),
  };
  nodes.memEditContent.value = 'changed';
  _currentMemorySection = 'memory';
  await submitMemorySave();
  const blockedSaveWrites = writes.slice();
  _currentMemorySection = 'user';
  await submitMemorySave();
  return {
    blockedState,
    openState,
    blockedEditCalls,
    editState,
    blockedSaveWrites,
    writes,
    toastCalls: global.__toastCalls || 0,
    finalRenderDetailCalls: global.__renderDetailCalls.slice(),
  };
}

(async () => {
  const out = scenario === 'stale_reset' ? await runStaleReset() : await runGuards();
  process.stdout.write(JSON.stringify(out));
})().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""


def _panel_behavior(scenario: str) -> dict:
    completed = subprocess.run(
        [NODE, "-e", _PANEL_DRIVER, str(REPO_ROOT / "static" / "panels.js"), scenario],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_reporter_repro_both_flags_disabled_blank_payload_hide_rows_and_block_writes(tmp_path, monkeypatch):
    home, captured = _setup_home(tmp_path, monkeypatch, {"memory": {"memory_enabled": False, "user_profile_enabled": False}})
    payload = _read(home, captured)
    assert payload["memory"] == payload["user"] == ""
    assert payload["memory_mtime"] is payload["user_mtime"] is None
    assert payload["memory_enabled"] is payload["user_profile_enabled"] is False
    assert _write("memory", captured) == ("Memory is disabled", 403)
    assert _write("user", captured) == ("User profile is disabled", 403)
    assert (home / "memories" / "MEMORY.md").read_text(encoding="utf-8") == "memory body"
    assert (home / "memories" / "USER.md").read_text(encoding="utf-8") == "user body"


def test_memory_flag_only_gates_memory_surface(tmp_path, monkeypatch):
    home, captured = _setup_home(tmp_path, monkeypatch, {"memory": {"memory_enabled": False, "user_profile_enabled": True}})
    payload = _read(home, captured)
    assert payload["memory"] == ""
    assert payload["user"] == "user body"
    assert _write("memory", captured) == ("Memory is disabled", 403)
    assert _write("user", captured) is None
    assert (home / "memories" / "USER.md").read_text(encoding="utf-8") == "changed"


def test_user_flag_only_gates_user_surface(tmp_path, monkeypatch):
    home, captured = _setup_home(tmp_path, monkeypatch, {"memory": {"memory_enabled": True, "user_profile_enabled": False}})
    payload = _read(home, captured)
    assert payload["memory"] == "memory body"
    assert payload["user"] == ""
    assert _write("memory", captured) is None
    assert _write("user", captured) == ("User profile is disabled", 403)


def test_absent_flags_default_to_enabled(tmp_path, monkeypatch):
    home, captured = _setup_home(tmp_path, monkeypatch, {})
    payload = _read(home, captured)
    assert payload["memory"] == "memory body"
    assert payload["user"] == "user body"
    assert payload["memory_enabled"] is payload["user_profile_enabled"] is True
    assert _write("memory", captured) is None
    assert _write("user", captured) is None


def test_string_flags_follow_existing_truthy_config_semantics(tmp_path, monkeypatch):
    home, captured = _setup_home(
        tmp_path,
        monkeypatch,
        {"memory": {"memory_enabled": "false", "user_profile_enabled": "yes"}},
    )
    payload = _read(home, captured)
    assert payload["memory"] == ""
    assert payload["user"] == "user body"
    assert payload["memory_enabled"] is False
    assert payload["user_profile_enabled"] is True
    assert _write("memory", captured) == ("Memory is disabled", 403)
    assert _write("user", captured) is None
    assert (home / "memories" / "USER.md").read_text(encoding="utf-8") == "changed"


def test_soul_project_context_and_external_notes_remain_available(tmp_path, monkeypatch):
    home, captured = _setup_home(tmp_path, monkeypatch, {"memory": {"memory_enabled": False, "user_profile_enabled": False}})
    payload = _read(home, captured)
    assert payload["soul"] == "soul body"
    assert payload["project_context"] == ""
    assert payload["external_notes_enabled"] is True
    assert _write("soul", captured) is None
    assert (home / "SOUL.md").read_text(encoding="utf-8") == "changed"


@pytest.mark.skipif(NODE is None, reason="node is required for panel row coverage")
def test_panel_rows_follow_memory_feature_flags():
    assert _panel_rows({
        "memory_enabled": False,
        "user_profile_enabled": False,
        "external_notes_enabled": True,
    }) == ["soul", "project_context", "external_notes"]
    assert _panel_rows({
        "memory_enabled": False,
        "user_profile_enabled": True,
        "external_notes_enabled": True,
    }) == ["user", "soul", "project_context", "external_notes"]
    assert _panel_rows({
        "memory_enabled": True,
        "user_profile_enabled": False,
        "external_notes_enabled": True,
    }) == ["memory", "soul", "project_context", "external_notes"]
    assert _panel_rows({
        "memory_enabled": True,
        "user_profile_enabled": True,
        "external_notes_enabled": True,
    }) == ["memory", "user", "soul", "project_context", "external_notes"]


@pytest.mark.skipif(NODE is None, reason="node is required for panel row coverage")
def test_missing_payload_flags_leave_sections_visible():
    assert _panel_rows({"external_notes_enabled": False}) == ["memory", "user", "soul", "project_context"]


@pytest.mark.skipif(NODE is None, reason="node is required for panel behavior coverage")
def test_disabled_section_refresh_clears_stale_detail_state():
    out = _panel_behavior("stale_reset")
    assert out["section"] is None
    assert out["mode"] == "empty"
    assert out["title"] == ""
    assert out["body"] == ""
    assert out["bodyDisplay"] == "none"
    assert out["emptyDisplay"] == ""
    assert out["headerDisplay"] == "none"
    assert out["renderDetailCalls"] == []
    assert out["rows"] == ["user", "soul", "project_context", "external_notes"]


@pytest.mark.skipif(NODE is None, reason="node is required for panel behavior coverage")
def test_open_edit_and_save_guards_respect_disabled_sections():
    out = _panel_behavior("guards")
    assert out["blockedState"] == {
        "current": None,
        "renderDetailCalls": [],
        "closeCalls": 0,
        "blockedActive": False,
    }
    assert out["openState"] == {
        "current": "user",
        "renderDetailCalls": ["user"],
        "closeCalls": 1,
        "allowedActive": True,
    }
    assert out["blockedEditCalls"] == []
    assert out["editState"]["renderEditCalls"] == ["user"]
    assert out["blockedSaveWrites"] == []
    assert out["writes"] == [{"section": "user", "content": "changed"}]
    assert out["toastCalls"] == 1
    assert out["finalRenderDetailCalls"] == ["user", "user"]
