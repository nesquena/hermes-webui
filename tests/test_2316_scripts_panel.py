"""Tests for #2316: Scripts panel — list and raw endpoint for ~/.hermes/scripts/."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request

import pytest

from tests.conftest import TEST_STATE_DIR, TEST_BASE

pytestmark = pytest.mark.usefixtures("test_server")
REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"
NODE = shutil.which("node")


def _clear_scripts_dir():
    """Clear the scripts directory before test."""
    scripts_dir = TEST_STATE_DIR / "scripts"
    if scripts_dir.exists():
        shutil.rmtree(scripts_dir)


def _run_node(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=REPO_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _extract_func_script(js: str) -> str:
    return f"""
const src = {js!r};
function extractFunc(name) {{
  const re = new RegExp('(?:async\\\\s+)?function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
"""


def test_scripts_list_empty():
    """GET /api/scripts/list should return empty array if directory doesn't exist."""
    _clear_scripts_dir()
    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())
    assert data["scripts"] == []


def test_scripts_list_with_python_and_shell():
    """GET /api/scripts/list should return .py and .sh files with docstrings."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # Create a Python script with a docstring
    py_script = scripts_dir / "hello.py"
    py_script.write_text(
        '"""Say hello to the user."""\nprint("Hello world")\n',
        encoding="utf-8"
    )

    # Create a shell script with leading comments
    sh_script = scripts_dir / "backup.sh"
    sh_script.write_text(
        "#!/bin/bash\n# Backup the project\n# Run this daily\ntar -czf backup.tar.gz .\n",
        encoding="utf-8"
    )

    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())

    assert len(data["scripts"]) == 2
    scripts_by_name = {s["name"]: s for s in data["scripts"]}

    assert "hello.py" in scripts_by_name
    assert scripts_by_name["hello.py"]["description"] == "Say hello to the user."

    assert "backup.sh" in scripts_by_name
    assert scripts_by_name["backup.sh"]["description"] == "Backup the project Run this daily"


def test_scripts_list_filters_non_script_files():
    """GET /api/scripts/list should ignore non-script file types."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # Create various files
    (scripts_dir / "script.py").write_text('"""A script."""\npass', encoding="utf-8")
    (scripts_dir / "readme.txt").write_text("Not a script", encoding="utf-8")
    (scripts_dir / "config.json").write_text("{}", encoding="utf-8")

    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())

    assert len(data["scripts"]) == 1
    assert data["scripts"][0]["name"] == "script.py"


def test_scripts_list_skips_symlink_escape():
    """GET /api/scripts/list must not follow a symlinked entry outside scripts/."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    outside = TEST_STATE_DIR / "outside-secret.py"
    outside.write_text('"""Outside."""\npass\n', encoding="utf-8")

    link = scripts_dir / "leak.py"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())

    assert data["scripts"] == []


def test_scripts_raw_returns_source():
    """GET /api/scripts/raw?path=<name> should return file source."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    content = "#!/bin/bash\necho 'test'\n"
    (scripts_dir / "test.sh").write_text(content, encoding="utf-8")

    url = TEST_BASE + "/api/scripts/raw?path=test.sh"
    with urllib.request.urlopen(url, timeout=5) as r:
        data = json.loads(r.read())

    assert data["name"] == "test.sh"
    assert data["source"] == content


def test_scripts_raw_rejects_unsupported_file_types():
    """GET /api/scripts/raw should 400 for files outside the script allowlist."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "config.json").write_text("{}", encoding="utf-8")

    url = TEST_BASE + "/api/scripts/raw?path=config.json"
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(url, timeout=5)

    assert exc_info.value.code == 400


def test_scripts_raw_path_traversal_blocked():
    """GET /api/scripts/raw?path=../../../etc/passwd should return 400."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    url = TEST_BASE + "/api/scripts/raw?path=../../../etc/passwd"
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(url, timeout=5)

    assert exc_info.value.code == 400


def test_scripts_raw_missing_path_param():
    """GET /api/scripts/raw without ?path should return 400."""
    _clear_scripts_dir()
    url = TEST_BASE + "/api/scripts/raw"
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(url, timeout=5)

    assert exc_info.value.code == 400


def test_scripts_raw_nonexistent_file():
    """GET /api/scripts/raw?path=nonexistent should return 404."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    url = TEST_BASE + "/api/scripts/raw?path=nonexistent.py"
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(url, timeout=5)

    assert exc_info.value.code == 404


def test_scripts_list_returns_sorted_order():
    """GET /api/scripts/list should return scripts in alphabetical order."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # Create scripts in non-alphabetical order
    for name in ["zebra.sh", "apple.py", "middle.bash"]:
        (scripts_dir / name).write_text("#!/bin/bash\n# Script\n", encoding="utf-8")

    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())

    names = [s["name"] for s in data["scripts"]]
    assert names == ["apple.py", "middle.bash", "zebra.sh"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_switch_to_profile_clears_scripts_cache_before_panel_reload():
    """Profile switch must null `_scriptsData` before the panel reload hook runs."""
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let _profileSwitchGeneration = 0;
let _scriptsData = ['stale'];
let _skillsData = ['old'];
let _workspaceList = ['old'];
let _showAllProfiles = true;
const localStorage = { removed: [], removeItem(key){ this.removed.push(key); } };
const window = {};
const S = { activeProfile: 'default', session: null, messages: [] };
const panelLoads = [];
function $(id){ return null; }
async function api(url, opts){
  if (url !== '/api/profile/switch') throw new Error('unexpected api: ' + url);
  return { active: 'work', is_default: false };
}
async function renderSessionList(){}
function syncTopbar(){}
function loadDir(){ return Promise.resolve(); }
function showToast(){}
function t(key){ return key; }
async function _profileSwitchPanelLoad(){ panelLoads.push(_scriptsData); }
function _refreshProfileSwitchBackground(){}
function animateNextSessionListRefresh(){}
eval(extractFunc('switchToProfile'));
(async () => {
  await switchToProfile('work');
  console.log(JSON.stringify({
    activeProfile: S.activeProfile,
    scriptsData: _scriptsData,
    panelLoads,
    removed: localStorage.removed,
  }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    result = json.loads(_run_node(source))
    assert result["activeProfile"] == "work"
    assert result["scriptsData"] is None
    assert result["panelLoads"] == [None]
    assert result["removed"] == ["hermes-webui-model"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_profile_switch_panel_load_prefers_scripts_subtab_fetch():
    """Tasks panel reload should refetch Scripts only when the Scripts subtab is active."""
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let _currentPanel = 'tasks';
let _tasksSubtab = 'scripts';
const calls = [];
async function loadSkills(){ calls.push('skills'); }
async function loadMemory(){ calls.push('memory'); }
async function loadScripts(){ calls.push('scripts'); }
async function loadCrons(){ calls.push('crons'); }
async function loadKanban(){ calls.push('kanban'); }
async function loadProfilesPanel(){ calls.push('profiles'); }
async function loadWorkspacesPanel(){ calls.push('workspaces'); }
eval(extractFunc('_profileSwitchPanelLoad'));
(async () => {
  await _profileSwitchPanelLoad();
  _tasksSubtab = 'jobs';
  await _profileSwitchPanelLoad();
  console.log(JSON.stringify(calls));
})().catch(err => { console.error(err); process.exit(1); });
"""
    calls = json.loads(_run_node(source))
    assert calls == ["scripts", "crons"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_scripts_panel_persists_loaded_source_across_rerender():
    """Loaded script source should be cached on the record and reused after rerender."""
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
function escapeHtml(value) {
  return String(value == null ? '' : value).replace(/[&<>\"']/g, ch => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[ch]
  ));
}
function unescapeHtml(value) {
  return String(value)
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '\"')
    .replace(/&#39;/g, \"'\")
    .replace(/&amp;/g, '&');
}
class FakeClassList {
  constructor() { this.items = new Set(); }
  add(name) { this.items.add(name); }
  remove(name) { this.items.delete(name); }
  toggle(name) {
    if (this.items.has(name)) { this.items.delete(name); return false; }
    this.items.add(name);
    return true;
  }
  contains(name) { return this.items.has(name); }
}
class FakeElement {
  constructor(kind='div') {
    this.kind = kind;
    this.children = [];
    this.style = {};
    this.listeners = {};
    this.classList = new FakeClassList();
    this._innerHTML = '';
    this._textContent = '';
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  addEventListener(type, handler) {
    this.listeners[type] = handler;
  }
  querySelector(selector) {
    if (selector === '.script-header') return this.header || null;
    if (selector === '.script-source') return this.source || null;
    if (selector === 'code') return this.code || null;
    return null;
  }
  set innerHTML(html) {
    this._innerHTML = html;
    this.children = [];
    this.header = null;
    this.source = null;
    this.code = null;
    if (!html) return;
    if (html.includes('script-header')) {
      const header = new FakeElement('header');
      const source = new FakeElement('source');
      const code = new FakeElement('code');
      const match = html.match(/<code class="[^"]*">([\\s\\S]*)<\\/code>/);
      code.textContent = match ? unescapeHtml(match[1]) : '';
      source.style.display = 'none';
      source.querySelector = selector => selector === 'code' ? code : null;
      this.header = header;
      this.source = source;
      this.code = code;
    }
  }
  get innerHTML() { return this._innerHTML; }
  set textContent(value) { this._textContent = String(value); }
  get textContent() { return this._textContent; }
}
const box = new FakeElement('box');
const document = { createElement(){ return new FakeElement(); } };
const window = { Prism: null };
function $(id){ return id === 'scriptsList' ? box : null; }
function esc(value){ return escapeHtml(value); }
function t(key){
  if (key === 'scripts_no_scripts') return 'No scripts';
  if (key === 'scripts_load_error') return 'Failed to load source.';
  return key;
}
let apiCalls = 0;
async function api(url) {
  apiCalls += 1;
  if (url !== '/api/scripts/raw?path=test.sh') throw new Error('unexpected url: ' + url);
  return { source: '#!/bin/bash\\necho test\\n' };
}
eval(extractFunc('_renderScriptsList'));
(async () => {
  const scripts = [{ name: 'test.sh', description: '' }];
  _renderScriptsList(scripts);
  const first = box.children[0];
  first.classList.toggle('expanded');
  await first.querySelector('.script-header').listeners.click();
  _renderScriptsList(scripts);
  const second = box.children[0];
  second.classList.toggle('expanded');
  await second.querySelector('.script-header').listeners.click();
  console.log(JSON.stringify({
    apiCalls,
    cachedSource: scripts[0].source,
    rerenderedSource: second.querySelector('.script-source').querySelector('code').textContent,
    loaded: scripts[0]._loaded,
  }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    result = json.loads(_run_node(source))
    assert result["apiCalls"] == 1
    assert result["cachedSource"] == "#!/bin/bash\necho test\n"
    assert result["rerenderedSource"] == "#!/bin/bash\necho test\n"
    assert result["loaded"] is True
