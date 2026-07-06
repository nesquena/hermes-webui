from __future__ import annotations

import collections
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

import api.config as cfg
import api.models as models
import api.routes as routes
from api.config import get_reasoning_status
from api.gateway_chat import _gateway_reasoning_effort_for_request
from api.models import Session
from api.routes import handle_get, handle_post
from api.streaming import _resolve_turn_reasoning_config


ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = ROOT / "static" / "ui.js"
COMMANDS_JS_PATH = ROOT / "static" / "commands.js"
NODE = shutil.which("node")


class _DummyHandler:
    def __init__(self, body: dict | None = None, *, command: str = "POST"):
        raw = json.dumps(body or {}).encode("utf-8")
        self.command = command
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = tempfile.SpooledTemporaryFile()
        self.rfile.write(raw)
        self.rfile.seek(0)
        self.status = None
        self.response = {}
        self.wfile = tempfile.SpooledTemporaryFile()
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, code: int):
        self.status = code

    def send_header(self, key: str, value: str):
        self.response.setdefault("headers", {})[key] = value

    def end_headers(self):
        pass

    def payload(self) -> dict:
        self.wfile.seek(0)
        return json.loads(self.wfile.read().decode("utf-8"))


@pytest.fixture
def isolated_reasoning_env(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(
        yaml.safe_dump(
            {
                "model": {"default": "gpt-5", "provider": "openai"},
                "agent": {"reasoning_effort": "medium"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    lock = threading.RLock()
    sessions = collections.OrderedDict()
    session_index = sessions_dir / "index.json"
    for mod in (cfg, models, routes):
        monkeypatch.setattr(mod, "SESSION_DIR", sessions_dir, raising=False)
        monkeypatch.setattr(mod, "SESSION_INDEX_FILE", session_index, raising=False)
        monkeypatch.setattr(mod, "LOCK", lock, raising=False)
        monkeypatch.setattr(mod, "SESSIONS", sessions, raising=False)
        monkeypatch.setattr(mod, "SESSIONS_MAX", 100, raising=False)
    monkeypatch.setattr(cfg, "_get_config_path", lambda: cfgfile)
    monkeypatch.setattr(cfg, "reload_config", lambda: None)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *args, **kwargs: True, raising=False)
    return {
        "config_path": cfgfile,
        "sessions_dir": sessions_dir,
        "session_index": session_index,
    }


def _run_reasoning_ui_script(script: str) -> dict:
    if NODE is None:
        pytest.skip("node is required for the reasoning chip UI harness")

    harness = f"""
const fs = require('fs');
const src = fs.readFileSync(process.env.UI_JS_PATH, 'utf8');
function extractBlock(startMarker, endMarker) {{
  const start = src.indexOf(startMarker);
  if (start < 0) throw new Error('start marker not found: ' + startMarker);
  const end = src.indexOf(endMarker, start);
  if (end < 0) throw new Error('end marker not found: ' + endMarker);
  return src.slice(start, end);
}}
class FakeClassList {{
  constructor(owner) {{
    this.owner = owner;
    this._set = new Set();
  }}
  add(...names) {{ names.forEach(name => this._set.add(name)); }}
  remove(...names) {{ names.forEach(name => this._set.delete(name)); }}
  contains(name) {{ return this._set.has(name); }}
  toggle(name, force) {{
    if (force === true) {{ this._set.add(name); return true; }}
    if (force === false) {{ this._set.delete(name); return false; }}
    if (this._set.has(name)) {{ this._set.delete(name); return false; }}
    this._set.add(name);
    return true;
  }}
}}
class FakeElement {{
  constructor(tagName='div') {{
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = Object.create(null);
    this.style = Object.create(null);
    this.attributes = Object.create(null);
    this.classList = new FakeClassList(this);
    this.id = '';
    this.title = '';
    this.ariaLabel = '';
    this._textContent = '';
    this._innerHTML = '';
    this.offsetWidth = 200;
  }}
  appendChild(child) {{
    child.parentNode = this;
    this.children.push(child);
    return child;
  }}
  querySelectorAll(selector) {{
    if (selector !== '.reasoning-option') return [];
    const out = [];
    const walk = node => {{
      for (const child of node.children) {{
        if (child.classList && child.classList.contains('reasoning-option')) out.push(child);
        walk(child);
      }}
    }};
    walk(this);
    return out;
  }}
  getBoundingClientRect() {{
    return {{ left: 0 }};
  }}
  setAttribute(name, value) {{
    this.attributes[String(name)] = String(value);
    if (name === 'aria-label') this.ariaLabel = String(value);
  }}
  get className() {{
    return Array.from(this.classList._set).join(' ');
  }}
  set className(value) {{
    this.classList._set = new Set(String(value ?? '').split(/\\s+/).filter(Boolean));
  }}
  get textContent() {{
    return this._textContent;
  }}
  set textContent(value) {{
    this._textContent = String(value ?? '');
    this._innerHTML = this._textContent;
    this.children = [];
  }}
  get innerHTML() {{
    return this._innerHTML;
  }}
  set innerHTML(value) {{
    this._innerHTML = String(value ?? '');
    this._textContent = this._innerHTML;
    this.children = [];
  }}
}}
const nodes = Object.create(null);
function bind(id, node) {{
  node.id = id;
  nodes[id] = node;
  return node;
}}
const dropdown = bind('composerReasoningDropdown', new FakeElement('div'));
['None', 'Minimal', 'Low', 'Medium', 'High', 'Extra High'].forEach((label, idx) => {{
  const opt = new FakeElement('div');
  opt.classList.add('reasoning-option');
  opt.dataset.effort = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'][idx];
  opt.textContent = label;
  dropdown.appendChild(opt);
}});
bind('composerReasoningWrap', new FakeElement('div'));
bind('composerReasoningLabel', new FakeElement('div'));
bind('composerReasoningChip', new FakeElement('div'));
bind('composerMobileReasoningLabel', new FakeElement('div'));
bind('composerMobileReasoningAction', new FakeElement('div'));
bind('modelSelect', {{ value: 'gpt-5' }});
global.window = {{}};
global.document = {{
  readyState: 'complete',
  addEventListener: () => {{}},
  querySelector: () => null,
  createElement: tag => new FakeElement(tag),
}};
global.$ = id => nodes[id] || null;
const toasts = [];
const fetches = [];
const responses = [];
global.showToast = (msg, ms, type) => toasts.push({{ msg, ms, type }});
global.api = (url, opts) => {{
  fetches.push({{ url, opts: opts ? {{ method: opts.method, body: opts.body }} : null }});
  const next = responses.length ? responses.shift() : null;
  return Promise.resolve(typeof next === 'function' ? next(url, opts) : next);
}};
global.S = {{ session: {{ session_id: 'session-a', model: 'gpt-5', model_provider: 'openai' }} }};
eval(extractBlock('// ── Reasoning effort chip ────────────────────────────────────────────────────', '// ── Session toolsets chip (#493) ───────────────────────────────────────────'));
async function tick() {{
  await new Promise(resolve => setImmediate(resolve));
}}
async function main() {{
{script}
}}
main().then(result => {{
  const snapshot = {{
    fetches,
    toasts,
    label: nodes.composerReasoningLabel.textContent,
    mobileLabel: nodes.composerMobileReasoningLabel.textContent,
    selected: dropdown.querySelectorAll('.reasoning-option').filter(opt => opt.classList.contains('selected')).map(opt => opt.textContent),
    optionCount: dropdown.querySelectorAll('.reasoning-option').length,
  }};
  process.stdout.write(JSON.stringify(Object.assign(snapshot, result && typeof result === 'object' ? result : {{}})));
}}).catch(err => {{
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
}});
"""
    env = os.environ.copy()
    env["UI_JS_PATH"] = str(UI_JS_PATH)
    result = subprocess.run(
        ["node", "-e", harness],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _run_reasoning_command_script(script: str) -> dict:
    if NODE is None:
        pytest.skip("node is required for the reasoning command harness")

    harness = f"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.env.UI_JS_PATH, 'utf8');
const commandsSrc = fs.readFileSync(process.env.COMMANDS_JS_PATH, 'utf8');
function extractBlock(src, startMarker, endMarker) {{
  const start = src.indexOf(startMarker);
  if (start < 0) throw new Error('start marker not found: ' + startMarker);
  const end = src.indexOf(endMarker, start);
  if (end < 0) throw new Error('end marker not found: ' + endMarker);
  return src.slice(start, end);
}}
function extractFunction(src, name) {{
  const rx = new RegExp('function\\\\s+' + name + '\\\\b[\\\\s\\\\S]*?(?=^function\\\\s|\\\\Z)', 'm');
  const match = src.match(rx);
  if (!match) throw new Error('function not found: ' + name);
  return match[0];
}}
class FakeClassList {{
  constructor(owner) {{
    this.owner = owner;
    this._set = new Set();
  }}
  add(...names) {{ names.forEach(name => this._set.add(name)); }}
  remove(...names) {{ names.forEach(name => this._set.delete(name)); }}
  contains(name) {{ return this._set.has(name); }}
  toggle(name, force) {{
    if (force === true) {{ this._set.add(name); return true; }}
    if (force === false) {{ this._set.delete(name); return false; }}
    if (this._set.has(name)) {{ this._set.delete(name); return false; }}
    this._set.add(name);
    return true;
  }}
}}
class FakeElement {{
  constructor(tagName='div') {{
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = Object.create(null);
    this.style = Object.create(null);
    this.attributes = Object.create(null);
    this.classList = new FakeClassList(this);
    this.id = '';
    this.title = '';
    this.ariaLabel = '';
    this._textContent = '';
    this._innerHTML = '';
    this.offsetWidth = 200;
  }}
  appendChild(child) {{
    child.parentNode = this;
    this.children.push(child);
    return child;
  }}
  querySelectorAll(selector) {{
    if (selector !== '.reasoning-option') return [];
    const out = [];
    const walk = node => {{
      for (const child of node.children) {{
        if (child.classList && child.classList.contains('reasoning-option')) out.push(child);
        walk(child);
      }}
    }};
    walk(this);
    return out;
  }}
  getBoundingClientRect() {{
    return {{ left: 0 }};
  }}
  setAttribute(name, value) {{
    this.attributes[String(name)] = String(value);
    if (name === 'aria-label') this.ariaLabel = String(value);
  }}
  get className() {{
    return Array.from(this.classList._set).join(' ');
  }}
  set className(value) {{
    this.classList._set = new Set(String(value ?? '').split(/\\s+/).filter(Boolean));
  }}
  get textContent() {{
    return this._textContent;
  }}
  set textContent(value) {{
    this._textContent = String(value ?? '');
    this._innerHTML = this._textContent;
    this.children = [];
  }}
  get innerHTML() {{
    return this._innerHTML;
  }}
  set innerHTML(value) {{
    this._innerHTML = String(value ?? '');
    this._textContent = this._innerHTML;
    this.children = [];
  }}
}}
const nodes = Object.create(null);
function bind(id, node) {{
  node.id = id;
  nodes[id] = node;
  return node;
}}
const dropdown = bind('composerReasoningDropdown', new FakeElement('div'));
['None', 'Minimal', 'Low', 'Medium', 'High', 'Extra High'].forEach((label, idx) => {{
  const opt = new FakeElement('div');
  opt.classList.add('reasoning-option');
  opt.dataset.effort = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'][idx];
  opt.textContent = label;
  dropdown.appendChild(opt);
}});
bind('composerReasoningWrap', new FakeElement('div'));
bind('composerReasoningLabel', new FakeElement('div'));
bind('composerReasoningChip', new FakeElement('div'));
bind('composerMobileReasoningLabel', new FakeElement('div'));
bind('composerMobileReasoningAction', new FakeElement('div'));
bind('modelSelect', {{ value: 'gpt-5' }});
global.window = {{}};
global.document = {{
  readyState: 'complete',
  addEventListener: () => {{}},
  querySelector: () => null,
  createElement: tag => new FakeElement(tag),
}};
global.$ = id => nodes[id] || null;
global.t = key => key;
global.removeThinking = () => {{}};
global.renderMessages = () => {{}};
const toasts = [];
const fetches = [];
const responses = [];
global.showToast = (msg, ms, type) => toasts.push({{ msg, ms, type }});
global.api = (url, opts) => {{
  fetches.push({{ url, opts: opts ? {{ method: opts.method, body: opts.body }} : null }});
  const next = responses.length ? responses.shift() : null;
  return Promise.resolve(typeof next === 'function' ? next(url, opts) : next);
}};
global.S = {{ session: {{ session_id: 'session-a', model: 'gpt-5', model_provider: 'openai' }} }};
eval(extractBlock(uiSrc, '// ── Reasoning effort chip ────────────────────────────────────────────────────', '// ── Session toolsets chip (#493) ───────────────────────────────────────────'));
eval(extractFunction(commandsSrc, 'cmdReasoning'));
async function tick() {{
  await new Promise(resolve => setImmediate(resolve));
}}
async function main() {{
{script}
}}
main().then(result => {{
  const snapshot = {{
    fetches,
    toasts,
    label: nodes.composerReasoningLabel.textContent,
  }};
  process.stdout.write(JSON.stringify(Object.assign(snapshot, result && typeof result === 'object' ? result : {{}})));
}}).catch(err => {{
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
}});
"""
    env = os.environ.copy()
    env["UI_JS_PATH"] = str(UI_JS_PATH)
    env["COMMANDS_JS_PATH"] = str(COMMANDS_JS_PATH)
    result = subprocess.run(
        ["node", "-e", harness],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_session_reasoning_sidecar_round_trip_and_compact_repr(isolated_reasoning_env):
    sid = "session-sidecar-roundtrip"
    session = Session(
        session_id=sid,
        title="Reasoning",
        model="gpt-5",
        model_provider="openai",
        reasoning_effort="high",
    )
    session.save()

    compact = session.compact()
    assert compact["reasoning_effort"] == "high"

    loaded = Session.load(sid)
    assert loaded is not None
    assert loaded.reasoning_effort == "high"
    assert loaded.compact()["reasoning_effort"] == "high"

    loaded.reasoning_effort = None
    loaded.save()
    cleared = Session.load(sid)
    assert cleared is not None
    assert cleared.reasoning_effort is None
    assert cleared.compact()["reasoning_effort"] is None

    cleared.reasoning_effort = "none"
    cleared.save()
    disabled = Session.load(sid)
    assert disabled is not None
    assert disabled.reasoning_effort == "none"
    assert disabled.compact()["reasoning_effort"] == "none"


def test_reasoning_status_and_streaming_use_session_first_resolution(isolated_reasoning_env):
    session = Session(
        session_id="session-reasoning-status",
        title="Session A",
        model="gpt-5",
        model_provider="openai",
        reasoning_effort="high",
    )
    session.save()

    status = get_reasoning_status(session=session)
    assert status["reasoning_scope"] == "session"
    assert status["has_session_reasoning_override"] is True
    assert status["session_reasoning_effort"] == "high"
    assert status["profile_reasoning_effort"] == "medium"
    assert status["reasoning_effort"] == "high"

    status_handler = _DummyHandler(command="GET")
    handle_get(status_handler, urlparse(f"http://localhost/api/reasoning?session_id={session.session_id}"))
    assert status_handler.status == 200
    route_status = status_handler.payload()
    assert route_status["reasoning_scope"] == "session"
    assert route_status["has_session_reasoning_override"] is True
    assert route_status["reasoning_effort"] == "high"
    assert route_status["profile_reasoning_effort"] == "medium"

    profile_handler = _DummyHandler(command="GET")
    handle_get(profile_handler, urlparse("http://localhost/api/reasoning"))
    assert profile_handler.status == 200
    profile_status = profile_handler.payload()
    assert profile_status["reasoning_scope"] == "profile"
    assert profile_status["reasoning_effort"] == "medium"

    reasoning_config = _resolve_turn_reasoning_config(session)
    assert reasoning_config == {"enabled": True, "effort": "high"}

    other = Session(
        session_id="session-reasoning-inherit",
        title="Session B",
        model="gpt-5",
        model_provider="openai",
    )
    other.save()

    other_status = get_reasoning_status(session=other)
    assert other_status["reasoning_scope"] == "profile"
    assert other_status["has_session_reasoning_override"] is False
    assert other_status["session_reasoning_effort"] is None
    assert other_status["profile_reasoning_effort"] == "medium"
    assert other_status["reasoning_effort"] == "medium"
    assert _resolve_turn_reasoning_config(other) == {"enabled": True, "effort": "medium"}

    pinned_cfg = {
        "model": {"default": "gpt-5", "provider": "openai"},
        "agent": {"reasoning_effort": "high"},
    }
    pinned_status = get_reasoning_status(session=other, config_data=pinned_cfg)
    assert pinned_status["reasoning_scope"] == "profile"
    assert pinned_status["profile_reasoning_effort"] == "high"
    assert pinned_status["reasoning_effort"] == "high"
    assert _resolve_turn_reasoning_config(other, config_data=pinned_cfg) == {"enabled": True, "effort": "high"}


def test_session_reasoning_route_leaves_profile_default_and_global_route_stays_global(isolated_reasoning_env):
    source = Session(
        session_id="session-reasoning-route",
        title="Route Session",
        model="gpt-5",
        model_provider="openai",
    )
    source.save()

    session_handler = _DummyHandler({"session_id": source.session_id, "effort": "high"})
    handle_post(session_handler, urlparse("http://localhost/api/session/reasoning"))
    assert session_handler.status == 200
    session_payload = session_handler.payload()
    assert session_payload["reasoning_scope"] == "session"
    assert session_payload["reasoning_effort"] == "high"
    assert session_payload["profile_reasoning_effort"] == "medium"
    assert Session.load(source.session_id).reasoning_effort == "high"
    assert yaml.safe_load(isolated_reasoning_env["config_path"].read_text(encoding="utf-8"))["agent"]["reasoning_effort"] == "medium"

    clear_handler = _DummyHandler({"session_id": source.session_id, "effort": None})
    handle_post(clear_handler, urlparse("http://localhost/api/session/reasoning"))
    assert clear_handler.status == 200
    clear_payload = clear_handler.payload()
    assert clear_payload["reasoning_scope"] == "profile"
    assert clear_payload["reasoning_effort"] == "medium"
    assert Session.load(source.session_id).reasoning_effort is None

    global_handler = _DummyHandler({"effort": "low"})
    handle_post(global_handler, urlparse("http://localhost/api/reasoning"))
    assert global_handler.status == 200
    global_payload = global_handler.payload()
    assert global_payload["reasoning_scope"] == "profile"
    assert global_payload["reasoning_effort"] == "low"
    assert yaml.safe_load(isolated_reasoning_env["config_path"].read_text(encoding="utf-8"))["agent"]["reasoning_effort"] == "low"
    assert Session.load(source.session_id).reasoning_effort is None


def test_duplicate_session_carries_reasoning_effort(isolated_reasoning_env):
    source = Session(
        session_id="session-reasoning-source",
        title="Source Session",
        model="gpt-5",
        model_provider="openai",
        reasoning_effort="high",
        messages=[{"role": "user", "content": "hello"}],
    )
    source.save()

    handler = _DummyHandler({"session_id": source.session_id})
    handle_post(handler, urlparse("http://localhost/api/session/duplicate"))
    assert handler.status == 200
    payload = handler.payload()["session"]
    assert payload["reasoning_effort"] == "high"
    duplicated = Session.load(payload["session_id"])
    assert duplicated is not None
    assert duplicated.reasoning_effort == "high"
    assert Session.load(source.session_id).reasoning_effort == "high"


def test_branch_session_carries_reasoning_effort(isolated_reasoning_env):
    source = Session(
        session_id="session-reasoning-branch-source",
        title="Source Session",
        model="gpt-5",
        model_provider="openai",
        reasoning_effort="high",
        messages=[{"role": "user", "content": "hello"}],
    )
    source.save()

    handler = _DummyHandler({"session_id": source.session_id})
    handle_post(handler, urlparse("http://localhost/api/session/branch"))
    assert handler.status == 200
    payload = handler.payload()
    branched = Session.load(payload["session_id"])
    assert branched is not None
    assert branched.reasoning_effort == "high"


def test_compression_continuation_carries_reasoning_effort(isolated_reasoning_env):
    source = Session(
        session_id="session-reasoning-compression-source",
        title="Source Session",
        model="gpt-5",
        model_provider="openai",
        reasoning_effort="high",
    )
    source.compression_recovery = {
        "terminal_state": "compression_exhausted",
        "recommended_action": "start_focused_continuation",
        "source_session_id": source.session_id,
    }
    source.save()

    handler = _DummyHandler({"session_id": source.session_id})
    routes._handle_session_compression_recovery_start(handler, {"session_id": source.session_id})
    assert handler.status == 200
    payload = handler.payload()["session"]
    continued = Session.load(payload["session_id"])
    assert continued is not None
    assert continued.reasoning_effort == "high"


def test_gateway_reasoning_effort_prefers_session_override(isolated_reasoning_env):
    session = Session(
        session_id="session-reasoning-gateway",
        title="Gateway Session",
        model="gpt-5",
        model_provider="openai",
        reasoning_effort="high",
    )
    session.save()

    cfg_data = yaml.safe_load(isolated_reasoning_env["config_path"].read_text(encoding="utf-8"))
    assert _gateway_reasoning_effort_for_request(
        cfg_data,
        model="gpt-5",
        model_provider="openai",
        session=session,
    ) == "high"

    session.reasoning_effort = None
    session.save()
    pinned_cfg = {
        "model": {"default": "gpt-5", "provider": "openai"},
        "agent": {"reasoning_effort": "high"},
    }
    assert _gateway_reasoning_effort_for_request(
        pinned_cfg,
        model="gpt-5",
        model_provider="openai",
        session=session,
    ) == "high"


def test_reasoning_chip_cache_is_session_specific_and_session_only_action_requires_session():
    data = _run_reasoning_ui_script(
        """
  responses.push({
    reasoning_effort: 'high',
    reasoning_scope: 'session',
    supported_efforts: ['none', 'medium', 'high'],
    has_session_reasoning_override: true,
  });
  syncReasoningChip();
  await tick();
  const first = {
    url: fetches[0].url,
    label: nodes.composerReasoningLabel.textContent,
    selected: dropdown.querySelectorAll('.reasoning-option').filter(opt => opt.classList.contains('selected')).map(opt => opt.textContent),
  };
  S.session = { session_id: 'session-b', model: 'gpt-5', model_provider: 'openai' };
  responses.push({
    reasoning_effort: 'medium',
    reasoning_scope: 'profile',
    supported_efforts: ['none', 'medium', 'high'],
    has_session_reasoning_override: false,
  });
  syncReasoningChip();
  await tick();
  const second = {
    url: fetches[1].url,
    label: nodes.composerReasoningLabel.textContent,
    selected: dropdown.querySelectorAll('.reasoning-option').filter(opt => opt.classList.contains('selected')).map(opt => opt.textContent),
  };
  S.session = null;
  const before = fetches.length;
  await _sendReasoningEffort('session', 'high');
  const after = fetches.length;
  const guardToast = toasts[toasts.length - 1];
  return {
    first,
    second,
    before,
    after,
    guardToast,
    optionCount: dropdown.querySelectorAll('.reasoning-option').length,
  };
        """
    )

    assert "session_id=session-a" in data["first"]["url"]
    assert data["first"]["label"].startswith("High")
    assert data["first"]["label"].endswith("Session")
    assert "session_id=session-b" in data["second"]["url"]
    assert data["second"]["label"] == "Medium"
    assert data["before"] == data["after"]
    assert data["guardToast"]["type"] == "warning"
    assert "Select a session" in data["guardToast"]["msg"]


def test_profile_reasoning_write_refetches_session_effective_status_when_override_exists():
    data = _run_reasoning_ui_script(
        """
  responses.push({
    reasoning_effort: 'low',
    reasoning_scope: 'profile',
    supported_efforts: ['none', 'medium', 'high'],
    has_session_reasoning_override: false,
  });
  responses.push({
    reasoning_effort: 'high',
    reasoning_scope: 'session',
    supported_efforts: ['none', 'medium', 'high'],
    has_session_reasoning_override: true,
    session_reasoning_effort: 'high',
    profile_reasoning_effort: 'low',
  });
  await _sendReasoningEffort('profile', 'low');
  return {
    fetches,
    label: nodes.composerReasoningLabel.textContent,
    toast: toasts[toasts.length - 1],
  };
        """
    )

    assert data["fetches"][0]["url"] == "/api/reasoning"
    assert "session_id" not in json.loads(data["fetches"][0]["opts"]["body"])
    assert "session_id=session-a" in data["fetches"][1]["url"]
    assert data["label"].startswith("High")
    assert data["label"].endswith("Session")
    assert "session" in data["toast"]["msg"].lower()


def test_session_override_can_be_cleared_back_to_profile_default():
    data = _run_reasoning_ui_script(
        """
  responses.push({
    reasoning_effort: 'high',
    reasoning_scope: 'session',
    supported_efforts: ['none', 'medium', 'high'],
    has_session_reasoning_override: true,
    session_reasoning_effort: 'high',
    profile_reasoning_effort: 'medium',
  });
  syncReasoningChip();
  await tick();
  const clearLabels = dropdown.querySelectorAll('.reasoning-option').filter(opt => opt.dataset.clear).map(opt => opt.textContent);
  responses.push({
    reasoning_effort: 'medium',
    reasoning_scope: 'profile',
    supported_efforts: ['none', 'medium', 'high'],
    has_session_reasoning_override: false,
    session_reasoning_effort: null,
    profile_reasoning_effort: 'medium',
  });
  await _clearSessionReasoningEffort();
  const clearFetch = fetches[fetches.length - 1];
  return {
    clearLabels,
    clearFetch,
    label: nodes.composerReasoningLabel.textContent,
    toast: toasts[toasts.length - 1],
    clearSelected: dropdown.querySelectorAll('.reasoning-option').filter(opt => opt.dataset.clear && opt.classList.contains('selected')).length,
  };
        """
    )

    assert data["clearLabels"] == ["Use profile default"]
    assert data["clearFetch"]["url"] == "/api/session/reasoning"
    body = json.loads(data["clearFetch"]["opts"]["body"])
    assert body["effort"] == ""
    assert body["session_id"] == "session-a"
    assert data["label"] == "Medium"
    assert "profile default" in data["toast"]["msg"].lower()
    assert data["clearSelected"] == 1


def test_clear_action_requires_a_session():
    data = _run_reasoning_ui_script(
        """
  S.session = null;
  const before = fetches.length;
  await _clearSessionReasoningEffort();
  return { before, after: fetches.length, toast: toasts[toasts.length - 1] };
        """
    )

    assert data["before"] == data["after"]
    assert data["toast"]["type"] == "warning"
    assert "Select a session" in data["toast"]["msg"]


def test_profile_write_under_active_override_reports_masked_save_and_highlights_both_groups():
    data = _run_reasoning_ui_script(
        """
  responses.push({
    reasoning_effort: 'medium',
    reasoning_scope: 'profile',
    supported_efforts: ['none', 'medium', 'high'],
    has_session_reasoning_override: false,
  });
  responses.push({
    reasoning_effort: 'high',
    reasoning_scope: 'session',
    supported_efforts: ['none', 'medium', 'high'],
    has_session_reasoning_override: true,
    session_reasoning_effort: 'high',
    profile_reasoning_effort: 'medium',
  });
  await _sendReasoningEffort('profile', 'medium');
  return {
    label: nodes.composerReasoningLabel.textContent,
    toast: toasts[toasts.length - 1],
    selectedScopes: dropdown.querySelectorAll('.reasoning-option').filter(opt => opt.classList.contains('selected') && !opt.dataset.clear).map(opt => opt.dataset.scope),
  };
        """
    )

    msg = data["toast"]["msg"].lower()
    assert "profile default saved: medium" in msg
    assert "high session override" in msg
    assert data["label"].startswith("High")
    assert data["label"].endswith("Session")
    assert "session" in data["selectedScopes"]
    assert "profile" in data["selectedScopes"]


def test_reasoning_slash_status_stays_profile_global_with_active_session():
    data = _run_reasoning_command_script(
        """
  responses.push({
    reasoning_effort: 'medium',
    show_reasoning: true,
  });
  cmdReasoning('');
  await tick();
  return {
    fetches,
    toast: toasts[toasts.length - 1],
  };
        """
    )

    assert data["fetches"][0]["url"] == "/api/reasoning"
    assert "Reasoning effort: medium" in data["toast"]["msg"]


def test_reasoning_slash_write_refetches_active_session_chip_after_profile_save():
    data = _run_reasoning_command_script(
        """
  responses.push({
    reasoning_effort: 'low',
    reasoning_scope: 'profile',
    supported_efforts: ['none', 'medium', 'high'],
    has_session_reasoning_override: false,
  });
  responses.push({
    reasoning_effort: 'high',
    reasoning_scope: 'session',
    supported_efforts: ['none', 'medium', 'high'],
    has_session_reasoning_override: true,
    session_reasoning_effort: 'high',
    profile_reasoning_effort: 'low',
  });
  cmdReasoning('low');
  await tick();
  await tick();
  return {
    fetches,
    label: nodes.composerReasoningLabel.textContent,
    toast: toasts[toasts.length - 1],
  };
        """
    )

    assert data["fetches"][0]["url"] == "/api/reasoning"
    assert data["fetches"][1]["url"].endswith("session_id=session-a")
    assert data["label"].startswith("High")
    assert data["label"].endswith("Session")
    assert "Reasoning effort: low" in data["toast"]["msg"]
