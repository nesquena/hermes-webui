"""Regression tests for #4759: parallelize first-load sidebar boot fetches."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(source_text: str, function_name: str) -> str:
    marker = f"async function {function_name}("
    start = source_text.find(marker)
    if start < 0:
        marker = f"function {function_name}("
        start = source_text.find(marker)
    assert start >= 0, f"{function_name}() not found"
    brace_start = source_text.find("{", start)
    assert brace_start >= 0, f"{function_name} body not found"

    depth = 0
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False

    for index in range(brace_start, len(source_text)):
        char = source_text[index]
        nxt = source_text[index + 1] if index + 1 < len(source_text) else ""

        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if char == "*" and nxt == "/":
                in_block_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            continue
        if char == "/" and nxt == "/":
            in_line_comment = True
            continue
        if char == "/" and nxt == "*":
            in_block_comment = True
            continue
        if char in ("'", '"', "`"):
            in_string = char
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source_text[start : index + 1]

    raise AssertionError(f"could not extract {function_name}()")


def _run_node(script: str):
    completed = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout.strip())


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_cold_boot_starts_projects_fetch_before_sessions_resolve():
    """The project request must start before the session request settles."""
    fetch_helper = _extract_function(SESSIONS_JS, "_loadSidebarSessionListPayload")
    script = f"""
    const calls = [];
    let resolveSessions;
    let resolveProjects;

    global._showAllProfiles = false;
    global._allProjects = [];
    global._sessionListHasLoadedOnce = false;
    global.api = (url) => {{
  if (url.startsWith('/api/projects')) {{
    calls.push('projects');
    return new Promise(resolve => {{
      resolveProjects = resolve;
    }});
  }}
  if (url.startsWith('/api/sessions')) {{
    calls.push('sessions');
    return new Promise(resolve => {{
      resolveSessions = resolve;
    }});
  }}
  return Promise.reject(new Error('unexpected endpoint ' + url));
}};
{fetch_helper}

(async () => {{
  const run = _loadSidebarSessionListPayload('?v=1', {{
    timeoutToast:false,
    timeoutMs:90000,
    retries:1,
    retryTimeouts:true,
    retryStatuses:[502,503,504],
  }});

  await Promise.resolve();
  const orderAtSettleBoundary = calls.slice();

  resolveSessions({{
    sessions:[{{session_id:'s1'}}],
    other_profile_count:0,
    archived_count:0,
    archived_webui_count:0,
    archived_cli_count:0,
  }});
  resolveProjects({{projects:[]}});
  const payload = await run;
  console.log(JSON.stringify({{orderAtSettleBoundary,payload,calls:calls}}));
}})().catch(error => {{
  console.error(error);
  process.exit(1);
}});
"""

    body = _run_node(script)
    assert body["orderAtSettleBoundary"] == ["projects", "sessions"]
    assert body["payload"]["sessData"]["sessions"] == [{"session_id": "s1"}]
    assert body["payload"]["projData"]["projects"] == []


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_project_failure_falls_back_without_blocking_session_payload():
    """Project endpoint failure should still return session payload and fallback projects."""
    fetch_helper = _extract_function(SESSIONS_JS, "_loadSidebarSessionListPayload")
    script = f"""
    const calls = [];
    global._showAllProfiles = true;
    global._allProjects = [];
    global._sessionListHasLoadedOnce = false;
    global.api = (url) => {{
  if (url.startsWith('/api/projects')) {{
    calls.push('projects');
    return Promise.reject(new Error('project endpoint down'));
  }}
  if (url.startsWith('/api/sessions')) {{
    calls.push('sessions');
    return Promise.resolve({{
      sessions:[{{session_id:'s1'}}],
      other_profile_count:0,
      archived_count:0,
      archived_webui_count:0,
      archived_cli_count:0,
    }});
  }}
  return Promise.reject(new Error('unexpected endpoint ' + url));
}};
{fetch_helper}

(async () => {{
  const payload = await _loadSidebarSessionListPayload('?v=1', {{
    timeoutToast:false,
    timeoutMs:90000,
    retries:1,
    retryTimeouts:true,
    retryStatuses:[502,503,504],
  }});
  console.log(JSON.stringify({{calls, payload}}));
}})().catch(error => {{
  console.error(error);
  process.exit(1);
}});
"""

    body = _run_node(script)
    assert body["calls"] == ["projects", "sessions"]
    assert body["payload"]["sessData"]["sessions"][0]["session_id"] == "s1"
    assert body["payload"]["projData"]["projects"] == []


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_existing_session_request_options_are_preserved():
    """The cold-load /api/sessions request keeps the existing timeout/retry policy."""
    fetch_helper = _extract_function(SESSIONS_JS, "_loadSidebarSessionListPayload")
    refresh_fn = _extract_function(SESSIONS_JS, "_runRenderSessionListRefresh")
    script = f"""
global._SESSION_LIST_BOOT_TIMEOUT_MS = 90000;
global._sessionListHasLoadedOnce = false;
global._renderSessionListGen = 1;
global._profileSwitchListEmbargo = false;
global._pendingSessionListPayload = null;
global._showAllProfiles = false;
global._allProjects = [];
global._contentSearchResults = [];
global.$ = () => ({{ value: '' }});
global._isSessionListUserInteracting = () => false;
global._schedulePendingSessionListApply = () => {{}};
global._showSessionListLoadError = error => {{ throw new Error(error && error.message ? error.message : String(error)); }};
global._applySessionListPayload = () => {{}};
global._sessionListQueryString = () => '?v=1';
const calls = [];
global.api = (url, opts) => {{
  calls.push({{endpoint:url.replace('/api/',''), opts}});
  if(url.startsWith('/api/sessions')){{
    return Promise.resolve({{
      sessions:[{{session_id:'s1'}}],
      other_profile_count:0,
      archived_count:0,
      archived_webui_count:0,
      archived_cli_count:0,
    }});
  }}
  return Promise.resolve({{projects:[]}});
}};
{fetch_helper}
{refresh_fn}

(async () => {{
  await _runRenderSessionListRefresh({{}}, 1);
  const sessionCall = calls.find(entry => entry.endpoint === 'sessions?') || calls.find(entry => entry.endpoint.startsWith('sessions'));
  const projectCall = calls.find(entry => entry.endpoint === 'projects?') || calls.find(entry => entry.endpoint.startsWith('projects'));
  console.log(JSON.stringify({{
    sessionOpts: sessionCall ? sessionCall.opts : null,
    projectOpts: projectCall ? projectCall.opts : null,
    calls,
  }}));
}})().catch(error => {{
  console.error(error);
  process.exit(1);
}});
"""

    body = _run_node(script)
    session_opts = body["sessionOpts"] or {}
    project_opts = body["projectOpts"] or {}

    assert session_opts["timeoutToast"] is False
    assert session_opts["timeoutMs"] == 90000
    assert session_opts["retries"] == 1
    assert session_opts["retryTimeouts"] is True
    assert session_opts["retryStatuses"] == [502, 503, 504]
    assert project_opts == {"timeoutToast": False}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_warm_refresh_parallelizes_projects_fetch_and_keeps_minimal_session_opts():
    """Warm refresh uses minimal session opts, but still starts projects immediately."""
    fetch_helper = _extract_function(SESSIONS_JS, "_loadSidebarSessionListPayload")
    script = f"""
const calls = [];
let resolveSessions;
let resolveProjects;
global._showAllProfiles = false;
global._allProjects = [];
global._sessionListHasLoadedOnce = true;
global.api = (url, opts) => {{
  if (url.startsWith('/api/projects')) {{
    calls.push({{ endpoint:'projects', opts: opts || null }});
    return new Promise(resolve => {{
      resolveProjects = resolve;
    }});
  }}
  if (url.startsWith('/api/sessions')) {{
    calls.push({{ endpoint:'sessions', opts: opts || null }});
    return new Promise(resolve => {{
      resolveSessions = resolve;
    }});
  }}
  return Promise.reject(new Error('unexpected endpoint ' + url));
}};
{fetch_helper}

(async () => {{
  const run = _loadSidebarSessionListPayload('?v=1', {{
    timeoutToast:false,
    timeoutMs:90000,
    retries:1,
    retryTimeouts:true,
    retryStatuses:[502,503,504],
  }});

  await Promise.resolve();
  const orderAtSettleBoundary = calls.map(call => call.endpoint);
  const sessionOpts = calls.find(call => call.endpoint === 'sessions').opts;
  const projectOpts = calls.find(call => call.endpoint === 'projects').opts;

  resolveSessions({{
    sessions:[{{session_id:'warm-1'}}],
    other_profile_count:0,
    archived_count:0,
    archived_webui_count:0,
    archived_cli_count:0,
  }});
  resolveProjects({{projects:[{{name:'demo'}}]}});
  const payload = await run;
  console.log(JSON.stringify({{
    orderAtSettleBoundary,
    sessionOpts,
    projectOpts,
    payload,
  }}));
}})().catch(error => {{
  console.error(error);
  process.exit(1);
}});
"""

    body = _run_node(script)
    assert body["orderAtSettleBoundary"] == ["projects", "sessions"]
    assert body["sessionOpts"] == {"timeoutToast": False}
    assert body["projectOpts"] == {"timeoutToast": False}
    assert body["payload"]["sessData"]["sessions"] == [{"session_id": "warm-1"}]
    assert body["payload"]["projData"]["projects"] == [{"name": "demo"}]
