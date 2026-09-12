"""Browser-level regression coverage for expired reverse-proxy auth sessions."""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
WORKSPACE_JS = ROOT / "static" / "workspace.js"


def _extract_function(source: str, name: str) -> str:
    markers = (f"async function {name}(", f"function {name}(")
    start = next((source.find(marker) for marker in markers if source.find(marker) >= 0), -1)
    assert start >= 0, f"{name}() function must exist"
    paren_depth = 0
    close_paren = -1
    for index in range(source.find("(", start), len(source)):
        char = source[index]
        if char == "(":
            paren_depth += 1
        elif char == ")":
            paren_depth -= 1
            if paren_depth == 0:
                close_paren = index
                break
    brace = source.find("{", close_paren)
    assert brace >= 0
    depth = 0
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    for index in range(brace, len(source)):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            continue
        if block_comment:
            if char == "*" and following == "/":
                block_comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char == "/" and following == "/":
            line_comment = True
            continue
        if char == "/" and following == "*":
            block_comment = True
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"{name}() function body did not terminate")


def _node(script: str) -> dict:
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def test_fetch_wrapper_marks_same_origin_subrequests_for_access_401s():
    """The shared fetch chokepoint must opt into Cloudflare's expired-session 401 contract."""
    source = UI_JS.read_text(encoding="utf-8")
    patch_fetch = _extract_function(source, "_patchOfflineFetch")
    redirect = _extract_function(source, "_redirectIfUnauth")
    is_abort = _extract_function(source, "_isAbortError")
    browser_online = _extract_function(source, "_browserReportsOnline")
    script = textwrap.dedent(
        f"""
        let captured=null;
        global.location={{href:'https://hermes.example/session/abc',origin:'https://hermes.example'}};
        global.document={{baseURI:'https://hermes.example/'}};
        global.navigator={{onLine:true}};
        global.window={{
          location:global.location,
          fetch:async(input,init)=>{{captured={{url:String(input),headers:Object.fromEntries(new Headers(init&&init.headers).entries())}};return {{ok:true,status:200}};}},
        }};
        let _offlineFetchPatched=false;
        let _offlineRawFetch=null;
        const _showOfflineBannerIfProbeFails=()=>Promise.resolve(false);
        {browser_online}
        {is_abort}
        {redirect}
        {patch_fetch}
        (async()=>{{
          _patchOfflineFetch();
          await window.fetch('/api/sessions',{{headers:{{'X-Caller-Header':'preserved'}}}});
          process.stdout.write(JSON.stringify(captured));
        }})().catch(err=>{{console.error(err);process.exit(1);}});
        """
    )
    captured = _node(script)
    assert captured["headers"]["x-requested-with"] == "XMLHttpRequest"
    assert captured["headers"]["x-caller-header"] == "preserved"


def test_auth_redirect_helper_reloads_current_url_only_once():
    """Parallel expired subrequests must share one top-level navigation through the proxy."""
    source = UI_JS.read_text(encoding="utf-8")
    redirect = _extract_function(source, "_redirectIfUnauth")
    script = textwrap.dedent(
        f"""
        let reloads=0;
        global.location={{href:'https://hermes.example/session/abc?view=chat',origin:'https://hermes.example',pathname:'/session/abc',search:'?view=chat',reload:()=>{{reloads+=1;}}}};
        global.window={{location:global.location}};
        let _authReloadStarted=false;
        {redirect}
        const handled=[_redirectIfUnauth({{status:401}}),_redirectIfUnauth({{status:401}})];
        process.stdout.write(JSON.stringify({{reloads,handled,href:location.href}}));
        """
    )
    observed = _node(script)
    assert observed == {
        "reloads": 1,
        "handled": [True, True],
        "href": "https://hermes.example/session/abc?view=chat",
    }


def test_resume_health_probe_marks_ajax_and_reloads_on_401():
    """The raw health probe bypasses the fetch wrapper, so it must carry the same auth contract."""
    source = UI_JS.read_text(encoding="utf-8")
    health_url = _extract_function(source, "_offlineHealthUrl")
    probe = _extract_function(source, "_probeOfflineRecovery")
    redirect = _extract_function(source, "_redirectIfUnauth")
    script = textwrap.dedent(
        f"""
        let reloads=0;
        let capturedHeaders={{}};
        global.location={{href:'https://hermes.example/session/abc',origin:'https://hermes.example',reload:()=>{{reloads+=1;}}}};
        global.document={{baseURI:'https://hermes.example/'}};
        global.window={{location:global.location,fetch:async()=>{{throw new Error('wrapped fetch should not run');}}}};
        let _authReloadStarted=false;
        let _offlineHealthProbePromise=null;
        const _offlineRawFetch=async(_url,opts)=>{{
          capturedHeaders=Object.fromEntries(new Headers(opts&&opts.headers).entries());
          return {{ok:false,status:401}};
        }};
        const OFFLINE_HEALTH_TIMEOUT_MS=10000;
        {health_url}
        {redirect}
        {probe}
        (async()=>{{
          const ok=await _probeOfflineRecovery();
          process.stdout.write(JSON.stringify({{ok,reloads,headers:capturedHeaders}}));
        }})().catch(err=>{{console.error(err);process.exit(1);}});
        """
    )
    observed = _node(script)
    assert observed["ok"] is False
    assert observed["reloads"] == 1
    assert observed["headers"]["x-requested-with"] == "XMLHttpRequest"


def test_resume_events_share_one_inflight_auth_probe():
    """Focus/pageshow/visibility bursts on iOS should probe once, not stampede the edge."""
    source = UI_JS.read_text(encoding="utf-8")
    health_url = _extract_function(source, "_offlineHealthUrl")
    probe = _extract_function(source, "_probeOfflineRecovery")
    redirect = _extract_function(source, "_redirectIfUnauth")
    init = _extract_function(source, "initOfflineMonitor")
    script = textwrap.dedent(
        f"""
        const windowListeners={{}};
        const documentListeners={{}};
        let fetchCalls=0;
        let releaseFetch;
        global.location={{href:'https://hermes.example/session/abc',origin:'https://hermes.example',reload:()=>{{}}}};
        global.navigator={{onLine:true}};
        global.document={{
          baseURI:'https://hermes.example/',hidden:false,visibilityState:'visible',
          addEventListener:(name,handler)=>{{documentListeners[name]=handler;}},
        }};
        global.window={{
          location:global.location,
          addEventListener:(name,handler)=>{{windowListeners[name]=handler;}},
        }};
        let _authReloadStarted=false;
        let _offlineHealthProbePromise=null;
        let _offlineVisible=false;
        const _offlineRawFetch=()=>{{fetchCalls+=1;return new Promise(resolve=>{{releaseFetch=resolve;}});}};
        const OFFLINE_HEALTH_TIMEOUT_MS=10000;
        const _patchOfflineFetch=()=>{{}};
        const _browserReportsOnline=()=>true;
        const _showOfflineBannerIfProbeFails=()=>Promise.resolve(false);
        const checkOfflineRecoveryNow=()=>Promise.resolve(false);
        {health_url}
        {redirect}
        {probe}
        {init}
        (async()=>{{
          initOfflineMonitor();
          windowListeners.focus();
          windowListeners.pageshow();
          documentListeners.visibilitychange();
          await Promise.resolve();
          const callsWhilePending=fetchCalls;
          releaseFetch({{ok:true,status:200}});
          await Promise.resolve();await Promise.resolve();
          process.stdout.write(JSON.stringify({{callsWhilePending,events:{{window:Object.keys(windowListeners),document:Object.keys(documentListeners)}}}}));
        }})().catch(err=>{{console.error(err);process.exit(1);}});
        """
    )
    observed = _node(script)
    assert observed["callsWhilePending"] == 1
    assert "focus" in observed["events"]["window"]
    assert "pageshow" in observed["events"]["window"]
    assert "visibilitychange" in observed["events"]["document"]


def test_api_401_uses_shared_reload_without_rewriting_deep_link():
    """api() must not race the wrapper's top-level reload with a /login assignment."""
    source = WORKSPACE_JS.read_text(encoding="utf-8")
    api = _extract_function(source, "api")
    script = textwrap.dedent(
        f"""
        let helperCalls=0;
        global.location={{href:'https://hermes.example/session/abc?view=chat',pathname:'/session/abc',search:'?view=chat'}};
        global.window={{location:global.location}};
        global.document={{baseURI:'https://hermes.example/'}};
        global.fetch=async()=>({{ok:false,status:401,headers:{{get:()=>''}},text:async()=>''}});
        global._redirectIfUnauth=(response)=>{{helperCalls+=1;return response.status===401;}};
        {api}
        (async()=>{{
          const result=await api('/api/sessions',{{retries:0}});
          process.stdout.write(JSON.stringify({{helperCalls,href:location.href,result:result===undefined?'undefined':result}}));
        }})().catch(err=>{{console.error(err);process.exit(1);}});
        """
    )
    observed = _node(script)
    assert observed == {
        "helperCalls": 1,
        "href": "https://hermes.example/session/abc?view=chat",
        "result": "undefined",
    }


def test_api_401_opt_out_leaves_navigation_to_bootstrap_owner():
    """The existing redirect401:false contract must survive proxy recovery support."""
    source = WORKSPACE_JS.read_text(encoding="utf-8")
    api = _extract_function(source, "api")
    script = textwrap.dedent(
        f"""
        let helperCalls=0;
        global.location={{href:'https://hermes.example/session/abc',pathname:'/session/abc',search:''}};
        global.window={{location:global.location}};
        global.document={{baseURI:'https://hermes.example/'}};
        global.fetch=async()=>({{ok:false,status:401,headers:{{get:()=>''}},text:async()=>''}});
        global._redirectIfUnauth=()=>{{helperCalls+=1;return true;}};
        {api}
        (async()=>{{
          const result=await api('/api/profile/active',{{redirect401:false,retries:0}});
          process.stdout.write(JSON.stringify({{helperCalls,href:location.href,result:result===undefined?'undefined':result}}));
        }})().catch(err=>{{console.error(err);process.exit(1);}});
        """
    )
    observed = _node(script)
    assert observed == {
        "helperCalls": 0,
        "href": "https://hermes.example/session/abc",
        "result": "undefined",
    }


def test_api_marks_requests_before_dom_content_loaded():
    """api() cannot depend on the later global fetch patch during boot."""
    source = WORKSPACE_JS.read_text(encoding="utf-8")
    api = _extract_function(source, "api")
    script = textwrap.dedent(
        f"""
        let captured={{}};
        global.location={{href:'https://hermes.example/',pathname:'/',search:''}};
        global.window={{location:global.location}};
        global.document={{baseURI:'https://hermes.example/'}};
        global.fetch=async(_url,opts)=>{{
          captured=Object.fromEntries(new Headers(opts&&opts.headers).entries());
          return {{ok:true,status:200,headers:{{get:()=> 'application/json'}},json:async()=>({{ok:true}}),text:async()=>''}};
        }};
        {api}
        (async()=>{{
          await api('/api/settings',{{headers:{{'X-Caller-Header':'preserved'}},retries:0}});
          process.stdout.write(JSON.stringify(captured));
        }})().catch(err=>{{console.error(err);process.exit(1);}});
        """
    )
    observed = _node(script)
    assert observed["x-requested-with"] == "XMLHttpRequest"
    assert observed["x-caller-header"] == "preserved"


def test_api_preserves_empty_headers_opt_out_for_form_data():
    """Workspace upload needs the browser to synthesize multipart Content-Type and boundary."""
    source = WORKSPACE_JS.read_text(encoding="utf-8")
    api = _extract_function(source, "api")
    script = textwrap.dedent(
        f"""
        let captured={{}};
        global.location={{href:'https://hermes.example/',pathname:'/',search:''}};
        global.window={{location:global.location}};
        global.document={{baseURI:'https://hermes.example/'}};
        global.fetch=async(_url,opts)=>{{
          captured=Object.fromEntries(new Headers(opts&&opts.headers).entries());
          return {{ok:true,status:200,headers:{{get:()=> 'application/json'}},json:async()=>({{ok:true}}),text:async()=>''}};
        }};
        {api}
        (async()=>{{
          const form=new FormData();form.append('file',new Blob(['payload']), 'test.txt');
          await api('/api/workspace/upload',{{method:'POST',body:form,headers:{{}},retries:0}});
          process.stdout.write(JSON.stringify(captured));
        }})().catch(err=>{{console.error(err);process.exit(1);}});
        """
    )
    observed = _node(script)
    assert observed["x-requested-with"] == "XMLHttpRequest"
    assert "content-type" not in observed


def test_api_form_data_transport_generates_multipart_boundary():
    """A real Fetch/FormData request must retain its synthesized multipart boundary."""
    source = WORKSPACE_JS.read_text(encoding="utf-8")
    api = _extract_function(source, "api")
    script = textwrap.dedent(
        f"""
        const http=require('node:http');
        const server=http.createServer((req,res)=>{{
          const chunks=[];
          req.on('data',chunk=>chunks.push(chunk));
          req.on('end',()=>{{
            const observed={{headers:req.headers,body:Buffer.concat(chunks).toString('utf8')}};
            res.writeHead(200,{{'Content-Type':'application/json'}});
            res.end(JSON.stringify(observed));
          }});
        }});
        server.listen(0,'127.0.0.1',async()=>{{
          try{{
            const origin=`http://127.0.0.1:${{server.address().port}}/`;
            global.location={{href:origin,origin,pathname:'/',search:''}};
            global.window={{location:global.location}};
            global.document={{baseURI:origin}};
            {api}
            const form=new FormData();form.append('file',new Blob(['boundary payload']), 'test.txt');
            const observed=await api('/api/workspace/upload',{{method:'POST',body:form,headers:{{}},retries:0}});
            process.stdout.write(JSON.stringify(observed));
            server.close();
          }}catch(err){{console.error(err);server.close(()=>process.exit(1));}}
        }});
        """
    )
    observed = _node(script)
    content_type = observed["headers"]["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    assert observed["headers"]["x-requested-with"] == "XMLHttpRequest"
    assert "boundary payload" in observed["body"]
