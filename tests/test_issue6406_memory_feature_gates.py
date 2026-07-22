import json
import pathlib
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import api.profiles
import api.routes as routes
import pytest
from tests.js_source_extract import extract_function


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
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: config)
    monkeypatch.setattr(routes, "_memory_project_context_workspace", lambda _parsed: tmp_path)
    monkeypatch.setattr(routes, "_external_notes_sources_enabled", lambda _config_data=None: True)
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


def _panels_source():
    return (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _extract_js_const_array(source, name):
    start = source.index(f"const {name} = [")
    end = source.index("];", start) + 2
    return source[start:end]


def _extract_panel_function(source, name):
    try:
        return extract_function(source, name, prefix="async function")
    except AssertionError:
        return extract_function(source, name)


def _panel_driver_source():
    panels = _panels_source()
    sections = _extract_js_const_array(panels, "MEMORY_SECTIONS")
    functions = "\n".join(
        _extract_panel_function(panels, name)
        for name in [
            "_memorySectionMeta",
            "_memorySectionEnabled",
            "_renderMemoryEmpty",
            "loadMemory",
            "openMemorySection",
            "editCurrentMemory",
            "submitMemorySave",
        ]
    )
    return (
        sections
        + "\n"
        + functions
        + "\n"
        + _PANEL_DRIVER_BODY
    )


@pytest.fixture(scope="session")
def panel_driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("memory-panel-driver") / "panel_driver.js"
    path.write_text(_panel_driver_source(), encoding="utf-8")
    return path


_PANEL_DRIVER_BODY = r"""
const scenario = process.argv[2];
let payload = JSON.parse(process.argv[3] || '{}');

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

const writes = [];
global.api = async (url, opts) => {
  if (url === '/api/memory' || url.startsWith('/api/memory?')) return payload;
  if (url === '/api/memory/write') {
    writes.push(JSON.parse(opts.body));
    if (scenario === 'save_refresh_disabled') {
      payload = {
        memory_enabled: true,
        user_profile_enabled: false,
        external_notes_enabled: true,
        memory: 'memory body',
        user: '',
        soul: '',
        project_context: '',
      };
    }
    return {};
  }
  throw new Error('unexpected url: ' + url);
};

function rowLabels() {
  return nodes.memoryPanel.children.map((node) => node.innerHTML.replace(/<[^>]+>/g, ''));
}

async function runRows() {
  await loadMemory(false);
  return rowLabels();
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

async function runSaveRefreshDisabled() {
  payload = {
    memory_enabled: true,
    user_profile_enabled: true,
    external_notes_enabled: true,
    memory: 'memory body',
    user: 'user body',
    soul: '',
    project_context: '',
  };
  _memoryData = payload;
  const allowed = makeEl();
  await openMemorySection('user', allowed);
  nodes.memEditContent.value = 'changed';
  await submitMemorySave();
  return {
    section: _currentMemorySection,
    mode: _memoryMode,
    title: nodes.memoryDetailTitle.textContent,
    body: nodes.memoryDetailBody.innerHTML,
    bodyDisplay: nodes.memoryDetailBody.style.display,
    emptyDisplay: nodes.memoryDetailEmpty.style.display,
    headerDisplay: nodes.mainMemory._header.style.display,
    renderDetailCalls: global.__renderDetailCalls.slice(),
    writes,
  };
}

(async () => {
  let out;
  if (scenario === 'rows') out = await runRows();
  else if (scenario === 'stale_reset') out = await runStaleReset();
  else if (scenario === 'save_refresh_disabled') out = await runSaveRefreshDisabled();
  else out = await runGuards();
  process.stdout.write(JSON.stringify(out));
})().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""


def _panel_rows(panel_driver_path, data):
    completed = subprocess.run(
        [NODE, str(panel_driver_path), "rows", json.dumps(data)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _panel_behavior(panel_driver_path, scenario: str) -> dict:
    completed = subprocess.run(
        [NODE, str(panel_driver_path), scenario, "{}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_reporter_repro_both_flags_disabled_blank_payload_hide_rows_and_block_writes(tmp_path, monkeypatch):
    home, captured = _setup_home(tmp_path, monkeypatch, {"memory": {"memory_enabled": False, "user_profile_enabled": False}})
    payload = _read(home, captured)
    assert payload["memory"] == payload["user"] == ""
    assert payload["memory_mtime"] is payload["user_mtime"] is None
    assert payload["memory_path"] is payload["user_path"] is None
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


def test_present_memory_block_missing_flags_disable_current_server_surfaces(tmp_path, monkeypatch):
    home, captured = _setup_home(tmp_path, monkeypatch, {"memory": {}})
    payload = _read(home, captured)
    assert payload["memory"] == payload["user"] == ""
    assert payload["memory_enabled"] is payload["user_profile_enabled"] is False
    assert payload["memory_path"] is payload["user_path"] is None
    assert _write("memory", captured) == ("Memory is disabled", 403)
    assert _write("user", captured) == ("User profile is disabled", 403)
    assert (home / "memories" / "MEMORY.md").read_text(encoding="utf-8") == "memory body"
    assert (home / "memories" / "USER.md").read_text(encoding="utf-8") == "user body"


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


def test_concurrent_profiles_keep_request_owned_memory_flags(tmp_path, monkeypatch):
    profiles = {
        "alpha": {
            "config": {"memory": {"memory_enabled": False, "user_profile_enabled": True}},
            "other": "beta",
            "memory_result": ("Memory is disabled", 403),
            "user_result": None,
            "memory_content": "memory body",
            "user_content": "alpha user",
        },
        "beta": {
            "config": {"memory": {"memory_enabled": True, "user_profile_enabled": False}},
            "other": "alpha",
            "memory_result": None,
            "user_result": ("User profile is disabled", 403),
            "memory_content": "beta memory",
            "user_content": "user body",
        },
    }
    tls = threading.local()
    start = threading.Barrier(len(profiles))

    for name in profiles:
        home = tmp_path / name
        memories = home / "memories"
        memories.mkdir(parents=True)
        (memories / "MEMORY.md").write_text("memory body", encoding="utf-8")
        (memories / "USER.md").write_text("user body", encoding="utf-8")
        profiles[name]["home"] = home

    monkeypatch.setattr(api.profiles, "get_active_hermes_home", lambda: tls.home)
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: tls.config)
    monkeypatch.setattr(routes, "get_config", lambda: profiles[tls.name]["config"])
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    monkeypatch.setattr(routes, "bad", lambda _handler, message, status=400: {"error": (message, status)})

    def run(profile_name):
        profile = profiles[profile_name]
        tls.name = profile["other"]
        tls.home = profile["home"]
        tls.config = profile["config"]
        start.wait(timeout=5)
        memory = routes._handle_memory_write(
            object(),
            {"section": "memory", "content": f"{profile_name} memory"},
        )
        user = routes._handle_memory_write(
            object(),
            {"section": "user", "content": f"{profile_name} user"},
        )
        return {
            "memory": memory.get("error") if isinstance(memory, dict) and "error" in memory else None,
            "user": user.get("error") if isinstance(user, dict) and "error" in user else None,
            "memory_content": (profile["home"] / "memories" / "MEMORY.md").read_text(encoding="utf-8"),
            "user_content": (profile["home"] / "memories" / "USER.md").read_text(encoding="utf-8"),
        }

    with ThreadPoolExecutor(max_workers=len(profiles)) as executor:
        results = {
            name: future.result()
            for name, future in {
                name: executor.submit(run, name)
                for name in profiles
            }.items()
        }

    assert results["alpha"] == {
        "memory": profiles["alpha"]["memory_result"],
        "user": profiles["alpha"]["user_result"],
        "memory_content": profiles["alpha"]["memory_content"],
        "user_content": profiles["alpha"]["user_content"],
    }
    assert results["beta"] == {
        "memory": profiles["beta"]["memory_result"],
        "user": profiles["beta"]["user_result"],
        "memory_content": profiles["beta"]["memory_content"],
        "user_content": profiles["beta"]["user_content"],
    }


@pytest.mark.skipif(NODE is None, reason="node is required for panel row coverage")
def test_panel_rows_follow_memory_feature_flags(panel_driver_path):
    assert _panel_rows(panel_driver_path, {
        "memory_enabled": False,
        "user_profile_enabled": False,
        "external_notes_enabled": True,
    }) == ["soul", "project_context", "external_notes"]
    assert _panel_rows(panel_driver_path, {
        "memory_enabled": False,
        "user_profile_enabled": True,
        "external_notes_enabled": True,
    }) == ["user", "soul", "project_context", "external_notes"]
    assert _panel_rows(panel_driver_path, {
        "memory_enabled": True,
        "user_profile_enabled": False,
        "external_notes_enabled": True,
    }) == ["memory", "soul", "project_context", "external_notes"]
    assert _panel_rows(panel_driver_path, {
        "memory_enabled": True,
        "user_profile_enabled": True,
        "external_notes_enabled": True,
    }) == ["memory", "user", "soul", "project_context", "external_notes"]


@pytest.mark.skipif(NODE is None, reason="node is required for panel row coverage")
def test_missing_payload_flags_leave_sections_visible(panel_driver_path):
    assert _panel_rows(panel_driver_path, {"external_notes_enabled": False}) == ["memory", "user", "soul", "project_context"]


@pytest.mark.skipif(NODE is None, reason="node is required for panel behavior coverage")
def test_disabled_section_refresh_clears_stale_detail_state(panel_driver_path):
    out = _panel_behavior(panel_driver_path, "stale_reset")
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
def test_open_edit_and_save_guards_respect_disabled_sections(panel_driver_path):
    out = _panel_behavior(panel_driver_path, "guards")
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


@pytest.mark.skipif(NODE is None, reason="node is required for panel behavior coverage")
def test_save_refresh_does_not_restore_disabled_detail_state(panel_driver_path):
    out = _panel_behavior(panel_driver_path, "save_refresh_disabled")
    assert out["section"] is None
    assert out["mode"] == "empty"
    assert out["title"] == ""
    assert out["body"] == ""
    assert out["bodyDisplay"] == "none"
    assert out["emptyDisplay"] == ""
    assert out["headerDisplay"] == "none"
    assert out["renderDetailCalls"] == ["user"]
    assert out["writes"] == [{"section": "user", "content": "changed"}]
