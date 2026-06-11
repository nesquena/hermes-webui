"""Regression coverage for #2539 client-side api() timeout handling."""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_JS = ROOT / "static" / "workspace.js"
SESSIONS_JS = ROOT / "static" / "sessions.js"
UI_JS = ROOT / "static" / "ui.js"
PANELS_JS = ROOT / "static" / "panels.js"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_js_function(src: str, name: str) -> str:
    marker = f"async function {name}("
    start = src.find(marker)
    assert start >= 0, f"{name}() function must exist"
    # The api() signature contains a default object literal (`opts={}`), so the
    # function-body brace is the first `{` after the balanced parameter list.
    paren_depth = 0
    close_paren = -1
    for idx in range(start + len(f"async function {name}"), len(src)):
        ch = src[idx]
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
            if paren_depth == 0:
                close_paren = idx
                break
    assert close_paren > start, f"{name}() parameter list must close"
    brace = src.find("{", close_paren)
    assert brace > close_paren, f"{name}() function body must start with {{"
    depth = 0
    in_string: str | None = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    for idx in range(brace, len(src)):
        ch = src[idx]
        nxt = src[idx + 1] if idx + 1 < len(src) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            continue
        if ch in ("'", '"', "`"):
            in_string = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"could not extract {name}() body")


def _node_eval(script: str, timeout: float = 2.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def test_api_rejects_hung_fetch_with_timeout_and_toast():
    """A hung fetch must reject quickly and surface a recognizable timeout toast."""
    api_fn = _extract_js_function(_source(WORKSPACE_JS), "api")
    script = textwrap.dedent(
        f"""
        const events=[];
        global.document={{baseURI:'http://example.test/hermes/'}};
        global.location={{href:'http://example.test/hermes/',pathname:'/hermes/',search:''}};
        global.window={{location:global.location}};
        global.showToast=(msg,ms,type)=>events.push({{msg:String(msg),ms,type}});
        global.fetch=(url,opts)=>new Promise(()=>{{
          if(opts&&opts.signal)opts.signal.addEventListener('abort',()=>events.push({{aborted:true}}));
        }});
        {api_fn}
        api('/api/sessions',{{timeoutMs:20}})
          .then(()=>{{console.error('resolved unexpectedly');process.exit(2);}})
          .catch(err=>{{
            console.log(JSON.stringify({{message:String(err&&err.message||err),events}}));
            process.exit(0);
          }});
        setTimeout(()=>{{console.error('api did not reject after timeoutMs');process.exit(3);}},250);
        """
    )
    result = _node_eval(script, timeout=1.0)
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip())
    assert "timed out" in payload["message"].lower()
    assert any(event.get("aborted") for event in payload["events"]), payload
    assert any("request timed out" in event.get("msg", "").lower() for event in payload["events"]), payload
    assert any(event.get("type") == "error" for event in payload["events"]), payload


def test_api_rejects_stalled_response_body_with_timeout():
    """The timeout must stay active through JSON/text body consumption, not only headers."""
    api_fn = _extract_js_function(_source(WORKSPACE_JS), "api")
    script = textwrap.dedent(
        f"""
        const events=[];
        global.document={{baseURI:'http://example.test/hermes/'}};
        global.location={{href:'http://example.test/hermes/',pathname:'/hermes/',search:''}};
        global.window={{location:global.location}};
        global.showToast=(msg,ms,type)=>events.push({{msg:String(msg),ms,type}});
        global.fetch=(url,opts)=>Promise.resolve({{
          ok:true,
          headers:{{get:()=> 'application/json'}},
          json:()=>new Promise(()=>{{
            if(opts&&opts.signal)opts.signal.addEventListener('abort',()=>events.push({{aborted:true}}));
          }}),
          text:()=>Promise.resolve('')
        }});
        {api_fn}
        api('/api/sessions',{{timeoutMs:20}})
          .then(()=>{{console.error('resolved unexpectedly');process.exit(2);}})
          .catch(err=>{{
            console.log(JSON.stringify({{message:String(err&&err.message||err),events}}));
            process.exit(0);
          }});
        setTimeout(()=>{{console.error('api body read did not reject after timeoutMs');process.exit(3);}},250);
        """
    )
    result = _node_eval(script, timeout=1.0)
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip())
    assert "timed out" in payload["message"].lower()
    assert any(event.get("aborted") for event in payload["events"]), payload


def test_api_can_suppress_timeout_toast_for_background_pollers():
    """Passive pollers need abort/reject cleanup without a user-visible toast."""
    api_fn = _extract_js_function(_source(WORKSPACE_JS), "api")
    script = textwrap.dedent(
        f"""
        const events=[];
        global.document={{baseURI:'http://example.test/hermes/'}};
        global.location={{href:'http://example.test/hermes/',pathname:'/hermes/',search:''}};
        global.window={{location:global.location}};
        global.showToast=(msg,ms,type)=>events.push({{msg:String(msg),ms,type}});
        global.fetch=(url,opts)=>new Promise(()=>{{
          if(opts&&opts.signal)opts.signal.addEventListener('abort',()=>events.push({{aborted:true}}));
        }});
        {api_fn}
        api('/api/sessions',{{timeoutMs:20,timeoutToast:false}})
          .then(()=>{{console.error('resolved unexpectedly');process.exit(2);}})
          .catch(err=>{{
            console.log(JSON.stringify({{message:String(err&&err.message||err),events}}));
            process.exit(0);
          }});
        setTimeout(()=>{{console.error('api did not reject after timeoutMs');process.exit(3);}},250);
        """
    )
    result = _node_eval(script, timeout=1.0)
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip())
    assert "timed out" in payload["message"].lower()
    assert any(event.get("aborted") for event in payload["events"]), payload
    assert not any("msg" in event for event in payload["events"]), payload


def test_api_has_default_timeout_and_per_call_override_contract():
    src = _source(WORKSPACE_JS)
    body = _extract_js_function(src, "api")
    assert "timeoutMs" in body, "api() must accept opts.timeoutMs as a per-call override"
    assert "timeoutToast" in body, "api() must let passive callers suppress timeout toasts"

    assert "30000" in body, "api() must default browser API calls to a 30s timeout"
    assert "AbortController" in body, "api() must abort hung fetches with AbortController"
    assert "delete fetchOpts.timeoutMs" in body, "api() must strip timeoutMs before calling fetch()"
    assert "delete fetchOpts.timeoutToast" in body, "api() must strip timeoutToast before calling fetch()"

    fetch_call = re.search(r"fetch\(url\.href,\{.*?\.\.\.fetchOpts.*?\}\)", body, re.DOTALL)
    assert fetch_call, "api() must call fetch() with sanitized fetchOpts"
    assert "...opts" not in fetch_call.group(0), "api() must not spread raw opts into fetch()"
    assert "timeoutMs" not in fetch_call.group(0), "api() must not forward timeoutMs to fetch()"


def test_update_flows_keep_explicit_longer_timeouts():
    """Legitimately long update flows should not inherit the generic 30s guard."""
    src = _source(UI_JS)
    panels = _source(PANELS_JS)
    assert "api('/api/updates/check',{method:'POST',body:JSON.stringify({force:true}),timeoutMs:60000})" in panels
    assert "api('/api/updates/summary',{method:'POST',body:JSON.stringify({updates:scopedUpdates,target:target||null}),timeoutMs:60000})" in src
    assert "api('/api/updates/apply',{method:'POST',body:JSON.stringify({target}),timeoutMs:120000})" in src
    assert "api('/api/updates/force',{method:'POST',body:JSON.stringify({target}),timeoutMs:120000})" in src


def test_passive_background_polls_suppress_timeout_toasts():
    """Passive refreshes should be best-effort and not emit generic timeout toasts."""
    workspace = _source(WORKSPACE_JS)
    sessions = _source(SESSIONS_JS)
    messages = _source(ROOT / "static" / "messages.js")
    ui = _source(UI_JS)
    panels = _source(PANELS_JS)

    assert "api('/api/client-events/log',{method:'POST',body:JSON.stringify(payload),timeoutMs:3000,timeoutToast:false})" in workspace
    assert "api('/api/sessions' + allProfilesQS,{timeoutToast:false})" in sessions
    assert "api('/api/projects' + allProfilesQS,{timeoutToast:false})" in sessions
    assert "api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`,{timeoutToast:false})" in sessions
    assert 'api("/api/approval/pending?session_id=" + encodeURIComponent(sid),{timeoutToast:false})' in messages
    assert 'api("/api/clarify/pending?session_id=" + encodeURIComponent(sid),{timeoutToast:false})' in messages
    assert "api('/api/dashboard/status',{timeoutToast:false})" in ui
    assert "api('/api/system/health',{timeoutToast:false})" in ui
    assert "api('/api/health/agent',{timeoutToast:false})" in ui
    assert "api(`/api/crons/status?job_id=${encodeURIComponent(jobId)}`,{timeoutToast:false})" in panels


def test_new_session_inflight_cleanup_still_runs_after_api_rejects():
    """newSession() must keep its finally cleanup path so timeout rejections unpin the UI."""
    src = _source(SESSIONS_JS)
    start = src.find("async function newSession")
    assert start >= 0, "newSession() must exist"
    finally_idx = src.find("}finally{", start)
    assert finally_idx > start, "newSession() must keep a finally cleanup block"
    block = src[finally_idx : src.find("\n}", finally_idx) + 2]
    assert "_newSessionInFlight=null" in block
    assert "_setNewSessionPending(false)" in block
