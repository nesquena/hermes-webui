"""Regression coverage for one-shot PWA launch actions on hard refresh."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> dict:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def test_only_exact_new_chat_launch_action_is_consumed_before_hard_refresh():
    source = f"""
const sessionsSrc = {SESSIONS_JS!r};
function extractFunc(src, name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
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
function applyUrl(rel) {{
  const next = new URL(rel, 'https://example.test');
  window.location.href = next.href;
  window.location.pathname = next.pathname;
  window.location.search = next.search;
  window.location.hash = next.hash;
}}
global.window = {{ location: {{}} }};
global.document = {{ baseURI: 'https://example.test/app/' }};
applyUrl('/app/?action=new-chat&action=continue&keep=1&keep=2#frag');
globalThis._sessionUrlForSid = (0, eval)('(' + extractFunc(sessionsSrc, '_sessionUrlForSid') + ')');
const promoted = _sessionUrlForSid('session-123');
const promotedUrl = new URL(promoted, 'https://example.test');
console.log(JSON.stringify({{
  promoted,
  launchActions: promotedUrl.searchParams.getAll('action'),
  keep: promotedUrl.searchParams.getAll('keep'),
}}));
"""
    payload = _run_node(source)

    assert payload == {
        "promoted": "/app/session/session-123?action=continue&keep=1&keep=2#frag",
        "launchActions": ["continue"],
        "keep": ["1", "2"],
    }


def test_stale_new_chat_action_does_not_override_explicit_session_url():
    source = f"""
const bootSrc = {BOOT_JS!r};
function extractFunc(src, name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
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
globalThis._shouldStartFreshPwaChat = (0, eval)(
  '(' + extractFunc(bootSrc, '_shouldStartFreshPwaChat') + ')'
);
console.log(JSON.stringify({{
  freshRootLaunch: _shouldStartFreshPwaChat('new-chat', null),
  staleSessionLaunch: _shouldStartFreshPwaChat('new-chat', 'session-123'),
  ordinarySessionLoad: _shouldStartFreshPwaChat(null, 'session-123'),
}}));
"""
    payload = _run_node(source)

    assert payload == {
        "freshRootLaunch": True,
        "staleSessionLaunch": False,
        "ordinarySessionLoad": False,
    }
    assert "if(_shouldStartFreshPwaChat(pwaLaunchAction,urlSession)){" in BOOT_JS


def test_root_pwa_launch_replaces_source_history_before_boot_reentry():
    source = f"""
const sessionsSrc = {SESSIONS_JS!r};
const bootSrc = {BOOT_JS!r};
function extractFunc(src, name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
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
function applyUrl(rel) {{
  const next = new URL(rel, 'https://example.test');
  window.location.href = next.href;
  window.location.pathname = next.pathname;
  window.location.search = next.search;
  window.location.hash = next.hash;
}}
const entries = ['/app/?source=pwa&action=new-chat'];
let index = 0;
global.window = {{
  location: {{}},
  history: {{
    state: null,
    pushState(state, _title, url) {{
      entries.splice(index + 1);
      entries.push(url);
      index++;
      this.state = state;
      applyUrl(url);
    }},
    replaceState(state, _title, url) {{
      entries[index] = url;
      this.state = state;
      applyUrl(url);
    }},
  }},
}};
global.document = {{ baseURI: 'https://example.test/app/' }};
applyUrl(entries[0]);
globalThis._sessionUrlForSid = (0, eval)('(' + extractFunc(sessionsSrc, '_sessionUrlForSid') + ')');
globalThis._setActiveSessionUrl = (0, eval)('(' + extractFunc(sessionsSrc, '_setActiveSessionUrl') + ')');
globalThis._shouldStartFreshPwaChat = (0, eval)('(' + extractFunc(bootSrc, '_shouldStartFreshPwaChat') + ')');
let newSessionCalls = 0;
let loadSessionCalls = 0;
function simulatedBoot() {{
  const url = new URL(window.location.href);
  const explicitSid = url.pathname.split('/session/')[1] || url.searchParams.get('session');
  if (_shouldStartFreshPwaChat(url.searchParams.get('action'), explicitSid)) newSessionCalls++;
  else if (explicitSid) loadSessionCalls++;
}}
newSessionCalls++;
_setActiveSessionUrl('session-123');
const backTarget = entries[index - 1] || entries[index];
applyUrl(backTarget);
simulatedBoot();
console.log(JSON.stringify({{entries, index, newSessionCalls, loadSessionCalls, href: window.location.href}}));
"""
    payload = _run_node(source)

    assert payload == {
        "entries": ["/app/session/session-123?source=pwa"],
        "index": 0,
        "newSessionCalls": 1,
        "loadSessionCalls": 1,
        "href": "https://example.test/app/session/session-123?source=pwa",
    }


@pytest.mark.parametrize(
    ("initial_url", "expected_url"),
    [
        (
            "/app/session/session-123?action=new-chat&action=continue&keep=1#frag",
            "/app/session/session-123?action=continue&keep=1#frag",
        ),
        (
            "/app/?session=session-123&action=new-chat&action=continue&keep=1#frag",
            "/app/session/session-123?action=continue&keep=1#frag",
        ),
    ],
)
def test_explicit_session_wins_and_replaces_stale_launch_history(initial_url, expected_url):
    source = f"""
const sessionsSrc = {SESSIONS_JS!r};
const bootSrc = {BOOT_JS!r};
function extractFunc(src, name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
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
function applyUrl(rel) {{
  const next = new URL(rel, 'https://example.test');
  Object.assign(window.location, {{href: next.href, pathname: next.pathname, search: next.search, hash: next.hash}});
}}
const calls = [];
global.window = {{location: {{}}, history: {{
  pushState(_state, _title, url) {{ calls.push(['push', url]); applyUrl(url); }},
  replaceState(_state, _title, url) {{ calls.push(['replace', url]); applyUrl(url); }},
}}}};
global.document = {{baseURI: 'https://example.test/app/'}};
applyUrl({initial_url!r});
globalThis._sessionUrlForSid = (0, eval)('(' + extractFunc(sessionsSrc, '_sessionUrlForSid') + ')');
globalThis._setActiveSessionUrl = (0, eval)('(' + extractFunc(sessionsSrc, '_setActiveSessionUrl') + ')');
globalThis._shouldStartFreshPwaChat = (0, eval)('(' + extractFunc(bootSrc, '_shouldStartFreshPwaChat') + ')');
const before = new URL(window.location.href);
const explicitSid = before.pathname.split('/session/')[1] || before.searchParams.get('session');
const shouldStartFresh = _shouldStartFreshPwaChat(before.searchParams.get('action'), explicitSid);
_setActiveSessionUrl(explicitSid);
console.log(JSON.stringify({{shouldStartFresh, calls, finalUrl: window.location.pathname + window.location.search + window.location.hash}}));
"""
    payload = _run_node(source)

    assert payload == {
        "shouldStartFresh": False,
        "calls": [["replace", expected_url]],
        "finalUrl": expected_url,
    }


def test_ordinary_session_navigation_keeps_push_state_semantics():
    source = f"""
const sessionsSrc = {SESSIONS_JS!r};
function extractFunc(src, name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  let i = src.indexOf('{{', start), depth = 1; i++;
  while (depth > 0) {{ if (src[i] === '{{') depth++; else if (src[i] === '}}') depth--; i++; }}
  return src.slice(start, i);
}}
global.window = {{
  location: {{href: 'https://example.test/app/?keep=1', pathname: '/app/', search: '?keep=1', hash: ''}},
  history: {{pushState(_state, _title, url) {{ console.log(JSON.stringify({{method: 'push', url}})); }}, replaceState() {{ throw new Error('unexpected replace'); }}}},
}};
global.document = {{baseURI: 'https://example.test/app/'}};
globalThis._sessionUrlForSid = (0, eval)('(' + extractFunc(sessionsSrc, '_sessionUrlForSid') + ')');
globalThis._setActiveSessionUrl = (0, eval)('(' + extractFunc(sessionsSrc, '_setActiveSessionUrl') + ')');
_setActiveSessionUrl('session-123');
"""
    assert _run_node(source) == {
        "method": "push",
        "url": "/app/session/session-123?keep=1",
    }
