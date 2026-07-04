"""Regression coverage for issue #5501: session-list retry affordance."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
STYLE_CSS = ROOT / "static" / "style.css"
NODE = shutil.which("node")


def _extract_function(source_text, function_name):
    marker = f"function {function_name}("
    start = source_text.index(marker)
    brace_start = source_text.index("{", start)
    depth = 0
    for index in range(brace_start, len(source_text)):
        char = source_text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source_text[start : index + 1]
    raise AssertionError(f"Could not extract {function_name}")


def _run_node(script):
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def _build_node_script(extra_js):
    src = SESSIONS_JS.read_text(encoding="utf-8")
    show_error_fn = _extract_function(src, "_showSessionListLoadError")
    retry_note_fn = _extract_function(src, "_renderSessionListLoadErrorNote")
    return f"""
class FakeClassList {{
  constructor(node) {{
    this.node = node;
    this.names = new Set();
  }}
  _sync() {{
    this.node.className = Array.from(this.names).join(' ');
  }}
  add(...names) {{
    for (const name of names) {{
      if (name) this.names.add(name);
    }}
    this._sync();
  }}
  remove(...names) {{
    for (const name of names) {{
      this.names.delete(name);
    }}
    this._sync();
  }}
  contains(name) {{
    return this.names.has(name);
  }}
  toggle(name, force) {{
    const shouldAdd = force === undefined ? !this.names.has(name) : !!force;
    if (shouldAdd) this.names.add(name);
    else this.names.delete(name);
    this._sync();
    return this.names.has(name);
  }}
}}
class FakeElement {{
  constructor(tag) {{
    this.tagName = tag;
    this.children = [];
    this.parentNode = null;
    this.attributes = {{}};
    this.dataset = {{}};
    this.style = {{}};
    this.className = '';
    this.classList = new FakeClassList(this);
    this.disabled = false;
    this.textContent = '';
    this.onclick = null;
    this.id = '';
    this.title = '';
    this._innerHTML = '';
  }}
  appendChild(child) {{
    this.children.push(child);
    child.parentNode = this;
    return child;
  }}
  setAttribute(name, value) {{
    this.attributes[name] = String(value);
    if (name === 'class') {{
      this.className = String(value);
      this.classList.names = new Set(String(value).split(/\\s+/).filter(Boolean));
    }}
    if (name.startsWith('data-')) {{
      const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[key] = String(value);
    }}
  }}
  getAttribute(name) {{
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }}
  removeAttribute(name) {{
    delete this.attributes[name];
    if (name === 'class') {{
      this.className = '';
      this.classList.names = new Set();
    }}
    if (name.startsWith('data-')) {{
      const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      delete this.dataset[key];
    }}
  }}
  get innerHTML() {{
    return this._innerHTML;
  }}
  set innerHTML(value) {{
    this._innerHTML = String(value);
    this.children = [];
  }}
}}
function deferred() {{
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {{
    resolve = res;
    reject = rej;
  }});
  return {{ promise, resolve, reject }};
}}
function findButton(node) {{
  return (node.children || []).find((child) => child.tagName === 'button') || null;
}}
const list = new FakeElement('div');
list.id = 'sessionList';
console.warn = () => {{}};
global.window = {{ _showCliSessions: false }};
global.document = {{ createElement: (tag) => new FakeElement(tag) }};
global.$ = (id) => id === 'sessionList' ? list : null;
global._allSessions = [];
global._sessionListLoadError = null;
global._sessionListFromCacheCalls = [];
global.renderSessionListCalls = [];
global.renderSessionListFromCache = () => {{
  const phase = global._sessionListLoadError && global._sessionListLoadError.retrying ? 'retrying' : 'idle';
  global._sessionListFromCacheCalls.push(phase);
  list.innerHTML = '';
  const note = _renderSessionListLoadErrorNote();
  if (note) {{
    list.appendChild(note);
    return;
  }}
  for (const session of global._allSessions) {{
    const row = new FakeElement('div');
    row.className = 'session-item';
    row.textContent = session.session_id;
    row.dataset.sid = session.session_id;
    list.appendChild(row);
  }}
}};
const retryFlow = deferred();
global.renderSessionList = (opts) => {{
  global.renderSessionListCalls.push(opts);
  return retryFlow.promise.then(
    () => {{
      global._sessionListLoadError = null;
      global._allSessions = [{{ session_id: 'row-1' }}];
      global.renderSessionListFromCache();
    }},
    (error) => {{
      _showSessionListLoadError(error);
      global._allSessions = [];
      global.renderSessionListFromCache();
      throw error;
    }},
  ).catch(() => {{}});
}};
{show_error_fn}
{retry_note_fn}
{extra_js}
"""


def _render_script(extra_js):
    return _build_node_script(extra_js)


def _load_source():
    return SESSIONS_JS.read_text(encoding="utf-8"), STYLE_CSS.read_text(encoding="utf-8")


def test_retry_button_pending_state_paints_before_settlement():
    if NODE is None:
        pytest.skip("node not on PATH")

    script = _render_script(
        """
_showSessionListLoadError(new Error('backend busy'));
global.renderSessionListFromCache();
const note = list.children[0];
const retry = findButton(note);
const before = {
  noteClass: note.className,
  retryClass: retry.className,
  text: retry.textContent,
  disabled: retry.disabled,
  ariaBusy: retry.getAttribute('aria-busy'),
  phases: [...global._sessionListFromCacheCalls],
  renderCalls: [...global.renderSessionListCalls],
  detail: note.children[1] ? note.children[1].textContent : null,
};
retry.onclick({ stopPropagation() {} });
const pendingNote = list.children[0];
const pendingRetry = findButton(pendingNote);
console.log(JSON.stringify({
  before,
  pending: {
    noteClass: pendingNote.className,
    retryClass: pendingRetry.className,
    text: pendingRetry.textContent,
    disabled: pendingRetry.disabled,
    ariaBusy: pendingRetry.getAttribute('aria-busy'),
    phases: [...global._sessionListFromCacheCalls],
    renderCalls: [...global.renderSessionListCalls],
  },
}));
"""
    )
    body = _run_node(script)

    assert body["before"] == {
        "noteClass": "session-list-error session-empty-note",
        "retryClass": "session-list-error-retry",
        "text": "Retry",
        "disabled": False,
        "ariaBusy": None,
        "phases": ["idle"],
        "renderCalls": [],
        "detail": "backend busy",
    }
    assert body["pending"] == {
        "noteClass": "session-list-error session-empty-note",
        "retryClass": "session-list-error-retry",
        "text": "Retrying...",
        "disabled": True,
        "ariaBusy": "true",
        "phases": ["idle", "retrying"],
        "renderCalls": [{"deferWhileInteracting": False}],
    }


def test_retry_button_mutates_immediately_when_full_repaint_is_blocked():
    if NODE is None:
        pytest.skip("node not on PATH")

    script = _render_script(
        """
_showSessionListLoadError(new Error('backend busy'));
global.renderSessionListFromCache();
const note = list.children[0];
const retry = findButton(note);
global.renderSessionListFromCache = () => {
  global._sessionListFromCacheCalls.push('blocked');
};
retry.onclick({ stopPropagation() {} });
console.log(JSON.stringify({
  sameButton: retry === findButton(list.children[0]),
  text: retry.textContent,
  disabled: retry.disabled,
  ariaBusy: retry.getAttribute('aria-busy'),
  onclick: retry.onclick === null,
  phases: [...global._sessionListFromCacheCalls],
  renderCalls: [...global.renderSessionListCalls],
}));
"""
    )
    body = _run_node(script)

    assert body == {
        "sameButton": True,
        "text": "Retrying...",
        "disabled": True,
        "ariaBusy": "true",
        "onclick": True,
        "phases": ["idle", "blocked"],
        "renderCalls": [{"deferWhileInteracting": False}],
    }


def test_retry_button_success_clears_pending_state_when_settlement_repaint_is_blocked():
    if NODE is None:
        pytest.skip("node not on PATH")

    script = _render_script(
        """
_showSessionListLoadError(new Error('backend busy'));
global.renderSessionListFromCache();
const note = list.children[0];
const retry = findButton(note);
global.renderSessionListFromCache = () => {
  global._sessionListFromCacheCalls.push('blocked');
};
retry.onclick({ stopPropagation() {} });
const pending = {
  text: retry.textContent,
  disabled: retry.disabled,
  ariaBusy: retry.getAttribute('aria-busy'),
  onclick: retry.onclick === null,
};
retryFlow.resolve();
(async () => {
  await new Promise((resolve) => setTimeout(resolve, 0));
  console.log(JSON.stringify({
    pending,
    settled: {
      text: retry.textContent,
      disabled: retry.disabled,
      ariaBusy: retry.getAttribute('aria-busy'),
      onclick: typeof retry.onclick,
    },
    phases: [...global._sessionListFromCacheCalls],
    renderCalls: [...global.renderSessionListCalls],
    error: global._sessionListLoadError,
  }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
    )
    body = _run_node(script)

    assert body["pending"] == {
        "text": "Retrying...",
        "disabled": True,
        "ariaBusy": "true",
        "onclick": True,
    }
    assert body["settled"] == {
        "text": "Retry",
        "disabled": False,
        "ariaBusy": None,
        "onclick": "function",
    }
    assert body["phases"] == ["idle", "blocked", "blocked"]
    assert body["renderCalls"] == [{"deferWhileInteracting": False}]
    assert body["error"] is None


def test_retry_button_failure_restores_click_handler_when_settlement_repaint_is_blocked():
    if NODE is None:
        pytest.skip("node not on PATH")

    script = _render_script(
        """
_showSessionListLoadError(new Error('backend busy'));
global.renderSessionListFromCache();
const note = list.children[0];
const retry = findButton(note);
global.renderSessionListFromCache = () => {
  global._sessionListFromCacheCalls.push('blocked');
};
retry.onclick({ stopPropagation() {} });
const pending = {
  text: retry.textContent,
  disabled: retry.disabled,
  ariaBusy: retry.getAttribute('aria-busy'),
  onclick: retry.onclick === null,
};
retryFlow.reject(new Error('boom'));
(async () => {
  await new Promise((resolve) => setTimeout(resolve, 0));
  console.log(JSON.stringify({
    pending,
    settled: {
      text: retry.textContent,
      disabled: retry.disabled,
      ariaBusy: retry.getAttribute('aria-busy'),
      onclick: typeof retry.onclick,
    },
    phases: [...global._sessionListFromCacheCalls],
    renderCalls: [...global.renderSessionListCalls],
    error: global._sessionListLoadError,
  }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
    )
    body = _run_node(script)

    assert body["pending"] == {
        "text": "Retrying...",
        "disabled": True,
        "ariaBusy": "true",
        "onclick": True,
    }
    assert body["settled"] == {
        "text": "Retry",
        "disabled": False,
        "ariaBusy": None,
        "onclick": "function",
    }
    assert body["phases"] == ["idle", "blocked", "blocked"]
    assert body["renderCalls"] == [{"deferWhileInteracting": False}]
    assert body["error"] == {
        "message": "Could not load conversations.",
        "detail": "boom",
    }


def test_retry_button_success_repaints_rows_after_settlement():
    if NODE is None:
        pytest.skip("node not on PATH")

    script = _render_script(
        """
_showSessionListLoadError(new Error('backend busy'));
global.renderSessionListFromCache();
const note = list.children[0];
const retry = findButton(note);
retry.onclick({ stopPropagation() {} });
const pending = {
  text: findButton(list.children[0]).textContent,
  disabled: findButton(list.children[0]).disabled,
  ariaBusy: findButton(list.children[0]).getAttribute('aria-busy'),
};
retryFlow.resolve();
(async () => {
  await Promise.resolve();
  const rows = list.children.map((child) => ({
    className: child.className,
    text: child.textContent,
  }));
  console.log(JSON.stringify({
    pending,
    phases: [...global._sessionListFromCacheCalls],
    renderCalls: [...global.renderSessionListCalls],
    error: global._sessionListLoadError,
    rows,
  }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
    )
    body = _run_node(script)

    assert body["pending"] == {
        "text": "Retrying...",
        "disabled": True,
        "ariaBusy": "true",
    }
    assert body["phases"] == ["idle", "retrying", "idle"]
    assert body["renderCalls"] == [{"deferWhileInteracting": False}]
    assert body["error"] is None
    assert body["rows"] == [{"className": "session-item", "text": "row-1"}]


def test_retry_button_failure_restores_idle_retry_state():
    if NODE is None:
        pytest.skip("node not on PATH")

    script = _render_script(
        """
_showSessionListLoadError(new Error('backend busy'));
global.renderSessionListFromCache();
const note = list.children[0];
const retry = findButton(note);
retry.onclick({ stopPropagation() {} });
const pending = {
  text: findButton(list.children[0]).textContent,
  disabled: findButton(list.children[0]).disabled,
  ariaBusy: findButton(list.children[0]).getAttribute('aria-busy'),
};
retryFlow.reject(new Error('boom'));
(async () => {
  await Promise.resolve();
  const failureNote = list.children[0];
  const retryAfterFailure = findButton(failureNote);
  console.log(JSON.stringify({
    pending,
    phases: [...global._sessionListFromCacheCalls],
    renderCalls: [...global.renderSessionListCalls],
    error: global._sessionListLoadError,
    failureNote: {
      title: failureNote.children[0] ? failureNote.children[0].textContent : null,
      detail: failureNote.children[1] ? failureNote.children[1].textContent : null,
    },
    retryAfterFailure: {
      text: retryAfterFailure.textContent,
      disabled: retryAfterFailure.disabled,
      ariaBusy: retryAfterFailure.getAttribute('aria-busy'),
    },
    rows: list.children.map((child) => ({
      className: child.className,
      text: child.textContent,
    })),
  }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
    )
    body = _run_node(script)

    assert body["pending"] == {
        "text": "Retrying...",
        "disabled": True,
        "ariaBusy": "true",
    }
    assert body["phases"] == ["idle", "retrying", "idle"]
    assert body["renderCalls"] == [{"deferWhileInteracting": False}]
    assert body["error"] == {
        "message": "Could not load conversations.",
        "detail": "boom",
    }
    assert body["failureNote"] == {
        "title": "Could not load conversations.",
        "detail": "boom",
    }
    assert body["retryAfterFailure"] == {
        "text": "Retry",
        "disabled": False,
        "ariaBusy": None,
    }
    assert body["rows"][0]["className"] == "session-list-error session-empty-note"


def test_session_list_error_css_and_retry_options_are_preserved():
    src, css = _load_source()

    assert ".session-list-error{" in css
    assert ".session-list-error-detail{" in css
    assert ".session-list-error-retry{" in css
    assert ".session-list-error-retry:focus-visible{" in css
    assert ".session-list-error-retry:disabled,.session-list-error-retry[aria-busy=\"true\"]" in css
    assert "opacity:1" in css
    assert "const sessionRequestOpts={" in src
    assert "retries:1," in src
    assert "retryStatuses:[502,503,504]," in src
    boot_gate = src.index("if(!_sessionListHasLoadedOnce){")
    assert src.index("retries:1,") < boot_gate
    assert src.index("retryStatuses:[502,503,504],") < boot_gate
    assert "sessionRequestOpts.timeoutMs=_SESSION_LIST_BOOT_TIMEOUT_MS;" in src
    assert "sessionRequestOpts.retryTimeouts=true;" in src
