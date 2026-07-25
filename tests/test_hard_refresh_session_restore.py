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


def test_new_chat_launch_action_is_consumed_before_hard_refresh():
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
applyUrl('/app/?action=new-chat&keep=1#frag');
globalThis._sessionUrlForSid = (0, eval)('(' + extractFunc(sessionsSrc, '_sessionUrlForSid') + ')');
const promoted = _sessionUrlForSid('session-123');
const promotedUrl = new URL(promoted, 'https://example.test');
console.log(JSON.stringify({{
  promoted,
  launchAction: promotedUrl.searchParams.get('action'),
  keep: promotedUrl.searchParams.get('keep'),
}}));
"""
    payload = _run_node(source)

    assert payload == {
        "promoted": "/app/session/session-123?keep=1#frag",
        "launchAction": None,
        "keep": "1",
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
