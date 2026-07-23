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


def test_scripts_list_iterdir_oserror_returns_empty(monkeypatch):
    """Direct list walk failures should degrade to an empty result, not a 500."""
    import api.routes as routes

    class _ScriptsDir:
        def exists(self):
            return True

        def iterdir(self):
            raise PermissionError("scripts dir unreadable")

    captured = {}

    monkeypatch.setattr(routes, "_hermes_scripts_dir", lambda: _ScriptsDir())
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200: captured.setdefault(
            "result", {"handler": handler, "payload": payload, "status": status}
        ),
    )

    handler = object()
    routes._handle_scripts_list(handler)

    assert captured["result"] == {
        "handler": handler,
        "payload": {"scripts": []},
        "status": 200,
    }


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


def test_scripts_list_skips_leaf_swap_after_resolve(monkeypatch):
    import api.routes as routes

    if os.open not in getattr(os, "supports_dir_fd", set()):
        pytest.skip("anchored leaf-swap proof requires dir_fd support")

    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    target = scripts_dir / "race.py"
    target.write_text('"""Inside."""\npass\n', encoding="utf-8")
    outside = TEST_STATE_DIR / "outside-list-secret.py"
    outside.write_text('"""Outside."""\npass\n', encoding="utf-8")

    try:
        os.symlink(str(outside), str(scripts_dir / "probe.py"))
        (scripts_dir / "probe.py").unlink()
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    original = routes._read_anchored_file_bytes
    swapped = False
    captured = {}

    def swapping_read(
        root, resolved_target, max_bytes=routes.MAX_FILE_BYTES, allow_prefix=False
    ):
        nonlocal swapped
        if not swapped:
            swapped = True
            target.unlink()
            os.symlink(str(outside), str(target))
        return original(root, resolved_target, max_bytes, allow_prefix)

    monkeypatch.setattr(routes, "_read_anchored_file_bytes", swapping_read)
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200: captured.setdefault(
            "result", {"handler": handler, "payload": payload, "status": status}
        ),
    )

    handler = object()
    routes._handle_scripts_list(handler)

    assert captured["result"] == {
        "handler": handler,
        "payload": {"scripts": []},
        "status": 200,
    }


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


def test_scripts_raw_rejects_leaf_swap_after_resolve(monkeypatch):
    import api.routes as routes

    if os.open not in getattr(os, "supports_dir_fd", set()):
        pytest.skip("anchored leaf-swap proof requires dir_fd support")

    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    target = scripts_dir / "race.py"
    target.write_text("print('inside')\n", encoding="utf-8")
    outside = TEST_STATE_DIR / "outside-raw-secret.py"
    outside.write_text("print('outside')\n", encoding="utf-8")

    try:
        os.symlink(str(outside), str(scripts_dir / "probe.py"))
        (scripts_dir / "probe.py").unlink()
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    original = routes._read_anchored_file_bytes
    swapped = False
    failures = []

    def swapping_read(
        root, resolved_target, max_bytes=routes.MAX_FILE_BYTES, allow_prefix=False
    ):
        nonlocal swapped
        if not swapped:
            swapped = True
            target.unlink()
            os.symlink(str(outside), str(target))
        return original(root, resolved_target, max_bytes, allow_prefix)

    monkeypatch.setattr(routes, "_read_anchored_file_bytes", swapping_read)
    monkeypatch.setattr(routes, "bad", lambda handler, msg, status=400: failures.append((msg, status)))

    routes._handle_scripts_raw(object(), type("Parsed", (), {"query": "path=race.py"})())

    assert failures == [("script not found", 404)]


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


def test_scripts_resolver_failure_fails_closed(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    global_home = TEST_STATE_DIR / "global-home"
    (global_home / "scripts").mkdir(parents=True, exist_ok=True)
    (global_home / "scripts" / "secret.py").write_text("secret", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(global_home))
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: (_ for _ in ()).throw(RuntimeError("resolver secret")))
    results = []
    monkeypatch.setattr(routes, "bad", lambda handler, msg, status=400: results.append((msg, status)))

    routes._handle_scripts_list(object())
    routes._handle_scripts_raw(object(), type("Parsed", (), {"query": "path=secret.py"})())

    assert results == [("scripts unavailable", 503), ("scripts unavailable", 503)]


def test_scripts_list_description_is_bounded():
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "long.py").write_text(
        '"""' + "x" * 2000 + '"""\n' + ("# filler\n" * 10000),
        encoding="utf-8",
    )

    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())

    assert [script["name"] for script in data["scripts"]] == ["long.py"]
    assert len(data["scripts"][0]["description"]) == 512


def test_scripts_raw_rejects_oversized_file():
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "large.py").write_bytes(b"x" * 400001)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(TEST_BASE + "/api/scripts/raw?path=large.py", timeout=5)

    assert exc_info.value.code == 413


def test_scripts_raw_skips_symlink_swap_escape():
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    outside = TEST_STATE_DIR / "outside-raw-secret.py"
    outside.write_text("outside", encoding="utf-8")
    link = scripts_dir / "race.py"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(TEST_BASE + "/api/scripts/raw?path=race.py", timeout=5)

    assert exc_info.value.code == 404


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_scripts_list_profile_generation_rejects_out_of_order_responses():
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let _scriptsData = null;
let _scriptsGeneration = 0;
let _scriptsRequestId = 0;
const S = { activeProfile: 'a' };
const box = { innerHTML: '' };
const renders = [];
const pending = [];
function $(id){ return id === 'scriptsList' ? box : null; }
function esc(value){ return String(value); }
function t(key){ return key; }
function _renderScriptsList(scripts){ renders.push(scripts.map(s => s.name)); }
function api(){ return new Promise(resolve => pending.push(resolve)); }
eval(extractFunc('_invalidateScriptsRequests'));
eval(extractFunc('_scriptsOwner'));
eval(extractFunc('_scriptsOwns'));
eval(extractFunc('loadScripts'));
(async () => {
  const first = loadScripts();
  S.activeProfile = 'b';
  _invalidateScriptsRequests();
  const second = loadScripts();
  pending[1]({ scripts: [{ name: 'b.py' }] });
  await second;
  pending[0]({ scripts: [{ name: 'a-secret.py' }] });
  await first;
  console.log(JSON.stringify({ data: _scriptsData.map(s => s.name), renders }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    result = json.loads(_run_node(source))
    assert result == {"data": ["b.py"], "renders": [["b.py"]]}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_scripts_raw_profile_generation_rejects_stale_record_commit():
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
  setAttribute(name, value) {
    this[name] = String(value);
  }
  querySelector(selector) {
    if (selector === '.script-header') return this.header || null;
    if (selector === '.script-source') return this.source || null;
    if (selector === '.script-expand') return this.expand || null;
    if (selector === 'code') return this.code || null;
    return null;
  }
  set innerHTML(html) {
    this._innerHTML = html;
    this.children = [];
    this.header = null;
    this.source = null;
    this.code = null;
    this.expand = null;
    if (!html) return;
    if (html.includes('script-header')) {
      const header = new FakeElement('header');
      const source = new FakeElement('source');
      const code = new FakeElement('code');
      const expand = new FakeElement('expand');
      const match = html.match(/<code class="[^"]*">([\\s\\S]*)<\\/code>/);
      code.textContent = match ? unescapeHtml(match[1]) : '';
      source.style.display = 'none';
      source.querySelector = selector => selector === 'code' ? code : null;
      this.header = header;
      this.source = source;
      this.code = code;
      header.querySelector = selector => selector === '.script-expand' ? expand : null;
      this.expand = expand;
    }
  }
  get innerHTML() { return this._innerHTML; }
  set textContent(value) { this._textContent = String(value); }
  get textContent() { return this._textContent; }
}
let _scriptsData = null;
let _scriptsGeneration = 0;
let _scriptsRequestId = 0;
const S = { activeProfile: 'a' };
const box = new FakeElement('box');
const document = { createElement(){ return new FakeElement(); } };
const window = { Prism: null };
function $(id){ return id === 'scriptsList' ? box : null; }
function esc(value){ return escapeHtml(value); }
function t(key){
  if (key === 'scripts_no_scripts') return 'No scripts';
  if (key === 'scripts_load_error') return 'Failed to load source.';
  if (key === 'loading') return 'Loading...';
  return key;
}
let resolver = null;
async function api(url) {
  if (url !== '/api/scripts/raw?path=a-secret.py') throw new Error('unexpected url: ' + url);
  return new Promise(resolve => {
    resolver = resolve;
  });
}
eval(extractFunc('_invalidateScriptsRequests'));
eval(extractFunc('_scriptsOwner'));
eval(extractFunc('_scriptsOwns'));
eval(extractFunc('_renderScriptsList'));
(async () => {
  const stale = { name: 'a-secret.py', description: '' };
  _scriptsData = [stale];
  _renderScriptsList(_scriptsData);
  const card = box.children[0];
  const clickPromise = card.querySelector('.script-header').listeners.click();
  S.activeProfile = 'b';
  _invalidateScriptsRequests();
  _scriptsData = [{ name: 'b.py' }];
  resolver({ source: '#!/bin/bash\\necho stolen\\n' });
  await clickPromise;
  console.log(JSON.stringify({
    current: _scriptsData[0].name,
    staleSource: stale.source || null,
    staleLoaded: !!stale._loaded,
    staleText: card.querySelector('.script-source').querySelector('code').textContent,
  }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    assert json.loads(_run_node(source)) == {
        "current": "b.py",
        "staleSource": None,
        "staleLoaded": False,
        "staleText": "Loading...",
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_scripts_refresh_reenables_after_stale_raw_click():
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
  setAttribute(name, value) {
    this[name] = String(value);
  }
  querySelector(selector) {
    if (selector === '.script-header') return this.header || null;
    if (selector === '.script-source') return this.source || null;
    if (selector === '.script-expand') return this.expand || null;
    if (selector === 'code') return this.code || null;
    return null;
  }
  set innerHTML(html) {
    this._innerHTML = html;
    this.children = [];
    this.header = null;
    this.source = null;
    this.code = null;
    this.expand = null;
    if (!html) return;
    if (html.includes('script-header')) {
      const header = new FakeElement('header');
      const source = new FakeElement('source');
      const code = new FakeElement('code');
      const expand = new FakeElement('expand');
      const match = html.match(/<code class="[^"]*">([\\s\\S]*)<\\/code>/);
      code.textContent = match ? unescapeHtml(match[1]) : '';
      source.style.display = 'none';
      source.querySelector = selector => selector === 'code' ? code : null;
      this.header = header;
      this.source = source;
      this.code = code;
      header.querySelector = selector => selector === '.script-expand' ? expand : null;
      this.expand = expand;
    }
  }
  get innerHTML() { return this._innerHTML; }
  set textContent(value) { this._textContent = String(value); }
  get textContent() { return this._textContent; }
}
let _scriptsData = [{ name: 'old.py', description: '' }];
let _scriptsGeneration = 0;
let _scriptsRequestId = 0;
let _scriptsRawRequestId = 0;
const S = { activeProfile: 'a' };
const box = new FakeElement('box');
const refreshBtn = { style: {}, disabled: false };
const document = { createElement(){ return new FakeElement(); } };
const window = { Prism: null };
let pendingList = null;
let pendingRaw = null;
function $(id){
  return {
    scriptsList: box,
    scriptsRefreshBtn: refreshBtn,
  }[id] || null;
}
function esc(value){ return escapeHtml(value); }
function t(key){
  if (key === 'scripts_no_scripts') return 'No scripts';
  if (key === 'scripts_load_error') return 'Failed to load source.';
  if (key === 'loading') return 'Loading...';
  if (key === 'error_prefix') return 'Error: ';
  return key;
}
async function api(url) {
  if (url === '/api/scripts/list') {
    return new Promise(resolve => { pendingList = resolve; });
  }
  if (url === '/api/scripts/raw?path=old.py') {
    return new Promise(resolve => { pendingRaw = resolve; });
  }
  throw new Error('unexpected url: ' + url);
}
eval(extractFunc('_invalidateScriptsRequests'));
eval(extractFunc('_scriptsOwner'));
eval(extractFunc('_scriptsOwns'));
eval(extractFunc('_renderScriptsList'));
eval(extractFunc('loadScripts'));
(async () => {
  _renderScriptsList(_scriptsData);
  const staleCard = box.children[0];
  const reloadPromise = loadScripts(true);
  const clickPromise = staleCard.querySelector('.script-header').listeners.click();
  pendingList({ scripts: [{ name: 'fresh.py', description: '' }] });
  await reloadPromise;
  pendingRaw({ source: 'echo old\\n' });
  await clickPromise;
  console.log(JSON.stringify({
    data: _scriptsData.map(s => s.name),
    disabled: refreshBtn.disabled,
    opacity: refreshBtn.style.opacity || '',
    children: box.children.length,
  }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    assert json.loads(_run_node(source)) == {
        "data": ["fresh.py"],
        "disabled": False,
        "opacity": "",
        "children": 1,
    }


def test_scripts_accessibility_contract_is_complete():
    html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    assert 'role="tablist"' in html
    assert 'role="tab"' in html
    assert 'handleTasksSubtabKeydown(event)' in html
    assert 'aria-selected="true"' in html
    assert 'role="tabpanel"' in html
    assert "function handleTasksSubtabKeydown" in js
    assert '<button type="button" class="script-header"' in js
    assert 'aria-expanded' in js
    assert 'script-expand' in js
    assert "t('loading')" in js


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_tasks_subtab_keyboard_navigation_drives_real_tab_behavior():
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let _tasksSubtab = 'jobs';
let loadScriptsCalls = 0;
class FakeClassList {
  constructor(active) { this.items = new Set(active ? ['active'] : []); }
  toggle(name, enabled) {
    if (enabled) this.items.add(name);
    else this.items.delete(name);
  }
}
function makeTab(id, active) {
  return {
    id,
    attrs: { 'aria-selected': String(active) },
    classList: new FakeClassList(active),
    tabIndex: active ? 0 : -1,
    focus(){ globalThis.focused.push(this.id); },
    setAttribute(name, value){ this.attrs[name] = String(value); },
  };
}
globalThis.focused = [];
const jobsTab = makeTab('tasksSubtabJobs', true);
const scriptsTab = makeTab('tasksSubtabScripts', false);
const jobsPane = { style: {}, setAttribute(name, value){ this[name] = String(value); } };
const scriptsPane = { style: { display: 'none' }, setAttribute(name, value){ this[name] = String(value); } };
const jobsActions = { style: {} };
const scriptsActions = { style: { display: 'none' } };
const document = {
  querySelectorAll(selector){
    if (selector === '.tasks-subtab') return [jobsTab, scriptsTab];
    return [];
  }
};
function $(id){
  return {
    tasksJobsPane: jobsPane,
    tasksScriptsPane: scriptsPane,
    tasksJobActions: jobsActions,
    tasksScriptActions: scriptsActions,
  }[id] || null;
}
async function loadScripts(){ loadScriptsCalls += 1; }
eval(extractFunc('switchTasksSubtab'));
eval(extractFunc('handleTasksSubtabKeydown'));
function press(currentTarget, key) {
  const event = {
    key,
    currentTarget,
    prevented: false,
    preventDefault(){ this.prevented = true; },
  };
  handleTasksSubtabKeydown(event);
  return event.prevented;
}
switchTasksSubtab('jobs');
const endPrevented = press(jobsTab, 'End');
const homePrevented = press(scriptsTab, 'Home');
const rightPrevented = press(jobsTab, 'ArrowRight');
console.log(JSON.stringify({
  endPrevented,
  homePrevented,
  rightPrevented,
  focused,
  loadScriptsCalls,
  jobsSelected: jobsTab.attrs['aria-selected'],
  scriptsSelected: scriptsTab.attrs['aria-selected'],
  jobsTabIndex: jobsTab.tabIndex,
  scriptsTabIndex: scriptsTab.tabIndex,
  jobsPaneDisplay: jobsPane.style.display || '',
  scriptsPaneDisplay: scriptsPane.style.display || '',
}));
"""
    assert json.loads(_run_node(source)) == {
        "endPrevented": True,
        "homePrevented": True,
        "rightPrevented": True,
        "focused": ["tasksSubtabScripts", "tasksSubtabJobs", "tasksSubtabScripts"],
        "loadScriptsCalls": 2,
        "jobsSelected": "false",
        "scriptsSelected": "true",
        "jobsTabIndex": -1,
        "scriptsTabIndex": 0,
        "jobsPaneDisplay": "none",
        "scriptsPaneDisplay": "",
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_failed_profile_switch_reloads_visible_scripts_owner():
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let _profileSwitchGeneration = 0;
let _scriptsData = [{ name: 'old.py' }];
let _scriptsGeneration = 0;
let _scriptsRequestId = 0;
let _currentPanel = 'tasks';
let _tasksSubtab = 'scripts';
let invalidations = 0;
let reloads = [];
const S = { activeProfile: 'old', session: null, messages: [] };
const window = {};
function $(id){ return null; }
function _invalidateScriptsRequests(){ invalidations++; _scriptsData = null; }
async function api(){ throw new Error('switch rejected'); }
function showToast(){}
function t(key){ return key; }
function _invalidateSessionListRenders(){}
function _setProfileSwitchListEmbargo(){}
function showSessionListSkeleton(){}
function bumpWorkspaceTreeGen(){}
function _refreshProfileSwitchBackground(){}
function renderSessionListFromCache(){}
async function loadScripts(force){ reloads.push(force); }
eval(extractFunc('switchToProfile'));
(async () => {
  await switchToProfile('new');
  console.log(JSON.stringify({ profile: S.activeProfile, invalidations, reloads }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    assert json.loads(_run_node(source)) == {
        "profile": "old", "invalidations": 1, "reloads": [True]
    }


@pytest.mark.parametrize("width,height", [(1280, 720), (480, 320)])
def test_tasks_panes_scroll_in_chromium(width, height):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(TEST_BASE, wait_until="domcontentloaded")
        page.wait_for_selector("body", timeout=10000)
        result = page.evaluate("""
          () => {
            const panel = document.querySelector('#panelTasks');
            panel.style.cssText = 'display:flex;position:fixed;inset:0;height:' + window.innerHeight + 'px;width:100%;';
            const checks = [];
            for (const [paneId, listId] of [['tasksJobsPane', 'cronList'], ['tasksScriptsPane', 'scriptsList']]) {
              const pane = document.querySelector('#' + paneId);
              const list = document.querySelector('#' + listId);
              pane.style.display = 'flex';
              list.innerHTML = Array.from({length: 80}, (_, i) => '<div style="height:24px">row ' + i + '</div>').join('');
              const before = list.scrollTop;
              list.scrollTop = list.scrollHeight;
              const paneRect = pane.getBoundingClientRect();
              const listRect = list.getBoundingClientRect();
              checks.push({
                scrollable: list.scrollHeight > list.clientHeight,
                overflow: getComputedStyle(list).overflowY,
                moved: list.scrollTop > before,
                contained: listRect.bottom <= paneRect.bottom + 1,
              });
            }
            return checks;
          }
        """)
        browser.close()

    assert result == [
        {"scrollable": True, "overflow": "auto", "moved": True, "contained": True},
        {"scrollable": True, "overflow": "auto", "moved": True, "contained": True},
    ]


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
function _clearCronDetail(){}
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
  setAttribute(name, value) {
    this[name] = String(value);
  }
  querySelector(selector) {
    if (selector === '.script-header') return this.header || null;
    if (selector === '.script-source') return this.source || null;
    if (selector === '.script-expand') return this.expand || null;
    if (selector === 'code') return this.code || null;
    return null;
  }
  set innerHTML(html) {
    this._innerHTML = html;
    this.children = [];
    this.header = null;
    this.source = null;
    this.code = null;
    this.expand = null;
    if (!html) return;
    if (html.includes('script-header')) {
      const header = new FakeElement('header');
      const source = new FakeElement('source');
      const code = new FakeElement('code');
      const expand = new FakeElement('expand');
      const match = html.match(/<code class="[^"]*">([\\s\\S]*)<\\/code>/);
      code.textContent = match ? unescapeHtml(match[1]) : '';
      source.style.display = 'none';
      source.querySelector = selector => selector === 'code' ? code : null;
      this.header = header;
      this.source = source;
      this.code = code;
      header.querySelector = selector => selector === '.script-expand' ? expand : null;
      this.expand = expand;
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
  await first.querySelector('.script-header').listeners.click();
  _renderScriptsList(scripts);
  const second = box.children[0];
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

@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_scripts_panel_keeps_source_hidden_if_card_collapses_before_fetch_settles():
    """Late async source loads must honor the card's current expansion state."""
    js = PANELS_JS_PATH.read_text(encoding="utf-8")
    source = f"""{_extract_func_script(js)}
function escapeHtml(value) {{
  return String(value == null ? '' : value).replace(/[&<>\"']/g, ch => (
    {{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}}[ch]
  ));
}}
function unescapeHtml(value) {{
  return String(value)
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '\"')
    .replace(/&#39;/g, \"'\")
    .replace(/&amp;/g, '&');
}}
class FakeClassList {{
  constructor() {{ this.items = new Set(); }}
  add(name) {{ this.items.add(name); }}
  remove(name) {{ this.items.delete(name); }}
  toggle(name) {{
    if (this.items.has(name)) {{ this.items.delete(name); return false; }}
    this.items.add(name);
    return true;
  }}
  contains(name) {{ return this.items.has(name); }}
}}
class FakeElement {{
  constructor(kind='div') {{
    this.kind = kind;
    this.children = [];
    this.style = {{}};
    this.listeners = {{}};
    this.classList = new FakeClassList();
    this._innerHTML = '';
    this._textContent = '';
  }}
  appendChild(child) {{
    this.children.push(child);
    return child;
  }}
  addEventListener(type, handler) {{
    this.listeners[type] = handler;
  }}
  setAttribute(name, value) {{
    this[name] = String(value);
  }}
  querySelector(selector) {{
    if (selector === '.script-header') return this.header || null;
    if (selector === '.script-source') return this.source || null;
    if (selector === '.script-expand') return this.expand || null;
    if (selector === 'code') return this.code || null;
    return null;
  }}
  set innerHTML(html) {{
    this._innerHTML = html;
    this.children = [];
    this.header = null;
    this.source = null;
    this.code = null;
    this.expand = null;
    if (!html) return;
    if (html.includes('script-header')) {{
      const header = new FakeElement('header');
      const source = new FakeElement('source');
      const code = new FakeElement('code');
      const expand = new FakeElement('expand');
      const match = html.match(/<code class="[^"]*">([\\s\\S]*)<\\/code>/);
      code.textContent = match ? unescapeHtml(match[1]) : '';
      source.style.display = 'none';
      source.querySelector = selector => selector === 'code' ? code : null;
      this.header = header;
      this.source = source;
      this.code = code;
      header.querySelector = selector => selector === '.script-expand' ? expand : null;
      this.expand = expand;
    }}
  }}
  get innerHTML() {{ return this._innerHTML; }}
  set textContent(value) {{ this._textContent = String(value); }}
  get textContent() {{ return this._textContent; }}
}}
const box = new FakeElement('box');
const document = {{ createElement(){{ return new FakeElement(); }} }};
const window = {{ Prism: null }};
function $(id){{ return id === 'scriptsList' ? box : null; }}
function esc(value){{ return escapeHtml(value); }}
function t(key){{
  if (key === 'scripts_no_scripts') return 'No scripts';
  if (key === 'scripts_load_error') return 'Failed to load source.';
  return key;
}}
let resolver = null;
async function api(url) {{
  if (url !== '/api/scripts/raw?path=test.sh') throw new Error('unexpected url: ' + url);
  return new Promise(resolve => {{
    resolver = resolve;
  }});
}}
eval(extractFunc('_renderScriptsList'));
(async () => {{
  const scripts = [{{ name: 'test.sh', description: '' }}];
  _renderScriptsList(scripts);
  const card = box.children[0];
  const clickPromise = card.querySelector('.script-header').listeners.click();
  card.querySelector('.script-header').listeners.click();
  resolver({{ source: '#!/bin/bash\\necho test\\n' }});
  await clickPromise;
  console.log(JSON.stringify({{
    display: card.querySelector('.script-source').style.display,
    cachedSource: scripts[0].source,
    loaded: scripts[0]._loaded,
  }}));
}})().catch(err => {{ console.error(err); process.exit(1); }});
"""
    result = json.loads(_run_node(source))
    assert result["display"] == "none"
    assert result["cachedSource"] == "#!/bin/bash\necho test\n"
    assert result["loaded"] is True
