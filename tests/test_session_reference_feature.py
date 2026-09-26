"""Browser regressions for settled session references and navigation."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
except ImportError:
    sync_playwright = None
    PlaywrightTimeoutError = TimeoutError

ROOT = Path(__file__).resolve().parents[1]


def _post_json(base, path, payload, opener=None):
    request = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    client = opener.open if opener else urllib.request.urlopen
    try:
        with client(request, timeout=10) as response:
            return json.loads(response.read()), response.headers
    except urllib.error.HTTPError as error:
        raise AssertionError(f"{path} returned HTTP {error.code}: {error.read().decode(errors='replace')}") from error


@pytest.mark.skipif(sync_playwright is None, reason="Playwright is unavailable")
def test_session_reference_browser_navigation_uses_server_profiles_and_preserves_view():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    def wait_for_visible_session(page, session_id):
        page.wait_for_function("sid => typeof S !== 'undefined' && S.session && S.session.session_id === sid",
                               arg=session_id, timeout=10000)
    with tempfile.TemporaryDirectory(prefix="hermes-session-reference-") as state:
        state_dir = Path(state)
        workspace_root = state_dir / "workspaces"
        default_workspace = workspace_root / "default"
        ops_workspace = workspace_root / "ops"
        for folder in (workspace_root, default_workspace, ops_workspace):
            folder.mkdir()
        (default_workspace / "source.txt").write_text("source workspace", encoding="utf-8")
        (ops_workspace / "ops.txt").write_text("destination workspace", encoding="utf-8")
        (state_dir / "config.yaml").write_text(json.dumps({
            "profile": {"name": "default"},
            "model": {"default": "gpt-4.1-mini", "provider": "openai"},
            "workspace": str(default_workspace),
        }), encoding="utf-8")
        (state_dir / "settings.json").write_text(json.dumps({"show_cli_sessions": True}), encoding="utf-8")
        env = os.environ.copy()
        for key in list(env):
            if key.endswith("_API_KEY") or key in {"HERMES_WEBUI_PASSWORD", "HERMES_WEBUI_AUTH_TOKEN"}:
                env.pop(key, None)
        env.pop("PYTHONPATH", None)
        env.pop("HERMES_WEBUI_PYTHON", None)
        env.update(HERMES_WEBUI_PORT=str(port), HERMES_WEBUI_HOST="127.0.0.1",
                   HERMES_WEBUI_STATE_DIR=state, HERMES_HOME=state, HERMES_BASE_HOME=state,
                   HERMES_CONFIG_PATH=str(state_dir / "config.yaml"),
                   HERMES_WEBUI_DEFAULT_WORKSPACE=str(workspace_root), HERMES_WEBUI_SKIP_ONBOARDING="1",
                   HERMES_WEBUI_AGENT_DIR=str(state_dir / "no-agent"))
        profile_home = state_dir / "profiles" / "ops"
        profile_home.mkdir(parents=True)
        (profile_home / "config.yaml").write_text(json.dumps({
            "profile": {"name": "ops"},
            "model": {"default": "claude-3-5-haiku", "provider": "anthropic"},
            "workspace": str(ops_workspace),
        }), encoding="utf-8")
        proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], cwd=ROOT,
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(80):
                try:
                    urllib.request.urlopen(base + "/health", timeout=1).close()
                    break
                except (urllib.error.URLError, OSError):
                    time.sleep(.25)
            else:
                pytest.fail("isolated WebUI server did not become healthy")
            def import_session(opener, title, assistant, workspace, model):
                data, _ = _post_json(base, "/api/session/import", {
                    "title": title, "workspace": str(workspace), "model": model,
                    "messages": [{"role": "user", "content": title}, {"role": "assistant", "content": assistant}],
                }, opener)
                return data["session"]["session_id"]

            ops_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            initial_roster = json.load(urllib.request.urlopen(base + "/api/profiles"))
            # Without hermes_cli, the agent-free roster fallback contains only default.
            assert any(p["name"] == "default" and p["is_default"] for p in initial_roster["profiles"]), initial_roster
            switch_data, setup_headers = _post_json(base, "/api/profile/switch", {"name": "ops"}, ops_opener)
            assert switch_data["active"] == "ops"
            assert bool(setup_headers.get("Set-Cookie")), "server profile fixture did not set a browser cookie"
            ops_good = import_session(ops_opener, "Destination session", "Destination transcript", ops_workspace, "claude-3-5-haiku")
            ops_failed = import_session(ops_opener, "Rejected destination", "This transcript must not replace the source", ops_workspace, "claude-3-5-haiku")
            ops_uncertain = import_session(ops_opener, "Unconfirmed destination", "This transcript must stay unavailable", ops_workspace, "claude-3-5-haiku")
            race_a = import_session(None, "Superseded session", "Stale navigation transcript", default_workspace, "gpt-4.1-mini")
            race_b = import_session(None, "Current session", "Current navigation transcript", default_workspace, "gpt-4.1-mini")
            source = import_session(None, "Source session", (
                f"Source transcript. @session:ops/{ops_good} @session:ops/{ops_failed} "
                f"@session:ops/{ops_uncertain} @session:{race_a}"
            ), default_workspace, "gpt-4.1-mini")
            with sync_playwright() as pw:
                launch = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
                if chromium := shutil.which("chromium"):
                    launch["executable_path"] = chromium
                browser = pw.chromium.launch(**launch)
                context = browser.new_context(service_workers="block")
                page = context.new_page()
                page.goto(base, wait_until="domcontentloaded")
                page.wait_for_function("() => typeof S !== 'undefined' && S._bootReady === true")
                page.evaluate("localStorage.setItem('hermes-webui-workspace-panel-pref','open')")
                assert page.evaluate("typeof _openSessionReference === 'function'"), "session reference runtime did not load"
                rendered = page.evaluate(r"""() => {
                  const text='@session:valid_1 @session:ops/other @session:abc.def @session:good. @session:numeric&#95;1 @session:dot&#46;part @session:longNumeric&#00000000000000000000000000000000095; @session:longHex&#x00000000000000000000005f; @session:named&lowbar;1 @session:namedDot&period;part @session:ordinary&amp; &#64;session:entity `@session:code` https://x.test/@session:url <i title="@session:attr">markup</i> hi <i a"b> <img src=x onerror=alert(1)> bye </1 a="x><img src=x onerror=alert(2)>"> <img src="https://example.test/a>b.png" onerror=alert(1)> @session:outside x <= "y and **bold** SESSIONREFESCAPED0TOKEN SESSIONREFESCAPED1TOKEN \\@session:escaped';
                  const assistant=_getCachedRender(text,false); window._renderUserMarkdown=false;
                  const plainUser=_getCachedRender('@session:user_plain',true); window._renderUserMarkdown=true;
                  const markdownUser=_getCachedRender('@session:user_md',true),streaming=_getCachedRender('@session:live',false,{linkSessionReferences:false});
                  const prefix='p'.repeat(260),suffix='s'.repeat(260);
                  const longA=_getCachedRender(prefix+' @session:long_session_A '+suffix,false),longB=_getCachedRender(prefix+' @session:long_session_B '+suffix,false);
                  const stressText='SESSIONREFESCAPED0TOKEN SESSIONREFESCAPED1TOKEN SESSIONREFESCAPED2TOKEN '+Array.from({length:700},(_,i)=>`\\@session:escaped_${i}`).join(' ');
                  const originalIncludes=String.prototype.includes,originalReplaceAll=String.prototype.replaceAll;
                  let markerIncludes=0,markerReplaceAll=0,stressHtml;
                  try{
                    String.prototype.includes=function(search,...args){if(typeof search==='string'&&/^SESSIONREFESCAPED\d+TOKEN$/.test(search))markerIncludes++;return originalIncludes.call(this,search,...args);};
                    String.prototype.replaceAll=function(search,...args){if(typeof search==='string'&&/^SESSIONREFESCAPED\d+TOKEN$/.test(search))markerReplaceAll++;return originalReplaceAll.call(this,search,...args);};
                    stressHtml=_getCachedRender(stressText,false);
                  }finally{String.prototype.includes=originalIncludes;String.prototype.replaceAll=originalReplaceAll;}
                  const stressRefs=stressHtml.match(/@session:escaped_[0-9]+/g)||[];
                  const stress={literalMarkers:['SESSIONREFESCAPED0TOKEN','SESSIONREFESCAPED1TOKEN','SESSIONREFESCAPED2TOKEN'].map(marker=>originalIncludes.call(stressHtml,marker)),escapedCount:stressRefs.length,uniqueEscapedCount:new Set(stressRefs).size,linkCount:(stressHtml.match(/class="session-link"/g)||[]).length,markerIncludes,markerReplaceAll};
                  const boundedId=_getCachedRender('@session:'+'a'.repeat(256),false),oversizedId=_getCachedRender('@session:'+'a'.repeat(257),false);
                  const boundedProfile=_getCachedRender('@session:'+'p'.repeat(64)+'/sid',false),oversizedProfile=_getCachedRender('@session:'+'p'.repeat(65)+'/sid',false);
                  const priorSessions=_allSessions;
                  _allSessions=[
                    {session_id:'alpha-tip',profile:'alpha',_lineage_root_id:'collision-root',_compression_segment_count:2},
                    {session_id:'beta-tip',profile:'beta',_lineage_root_id:'collision-root',_compression_segment_count:3},
                    {session_id:'unknown-tip',_lineage_root_id:'collision-root',_compression_segment_count:4}];
                  const lineage={alpha:_resolveSessionIdFromSidebarLineage('collision-root','alpha'),unscoped:_resolveSessionIdFromSidebarLineage('collision-root')};
                  _allSessions=priorSessions;
                  const original=location.pathname+location.search;
                  const read=search=>{history.replaceState(null,'','/'+search);return {sid:_sessionIdFromLocation(),ambiguous:!!S._ambiguousSessionUrlIntent};};
                  const aliases={duplicate:read('?session=a&session=b'),conflict:read('?session=a&session_id=b'),routeConflict:read('session/route-id?session=query-id'),routeEmpty:read('session/'),routeExtra:read('session/route-id/extra'),routeTrailingSlash:read('session/route-id/'),empty:read('?session='),invalid:read('?session=bad%2Fid'),single:read('?session=a')};
                  history.replaceState(null,'',original);
                return {assistant,plainUser,markdownUser,streaming,longA,longB,stress,boundedId,oversizedId,boundedProfile,oversizedProfile,lineage,aliases};
                }""")
                assert rendered["assistant"].count('class="session-link"') == 5 and 'data-session-id="outside"' in rendered["assistant"]
                assert 'session/other?profile=ops' in rendered["assistant"]
                assert '@session:abc.def' in rendered["assistant"] and 'data-session-id="abc"' not in rendered["assistant"]
                assert 'data-session-id="good"' in rendered["assistant"] and rendered["assistant"].count('<img') == 1 and 'src="https://example.test/a&gt;b.png"' in rendered["assistant"] and '<img src=x onerror' not in rendered["assistant"] and '<strong>bold</strong>' in rendered["assistant"]
                for protected in ("code", "url", "attr", "escaped", "entity", "numeric", "dot", "named", "namedDot", "longNumeric", "longHex"):
                    assert f'data-session-id="{protected}"' not in rendered["assistant"]
                assert "SESSIONREFESCAPED0TOKEN" in rendered["assistant"] and "SESSIONREFESCAPED1TOKEN" in rendered["assistant"]
                assert rendered["boundedId"].count('class="session-link"') == 1
                assert 'class="session-link"' not in rendered["oversizedId"]
                assert rendered["boundedProfile"].count('class="session-link"') == 1
                assert 'class="session-link"' not in rendered["oversizedProfile"]
                assert 'data-session-id="long_session_A"' in rendered["longA"] and 'data-session-id="long_session_A"' not in rendered["longB"]
                assert 'data-session-id="long_session_B"' in rendered["longB"] and 'data-session-id="long_session_B"' not in rendered["longA"]
                assert rendered["stress"]["literalMarkers"] == [True, True, True]
                assert rendered["stress"]["escapedCount"] == rendered["stress"]["uniqueEscapedCount"] == 700
                assert rendered["stress"]["linkCount"] == 0
                assert rendered["stress"]["markerIncludes"] <= 4 and rendered["stress"]["markerReplaceAll"] <= 4
                assert 'data-session-id="user_plain"' in rendered["plainUser"] and 'data-session-id="user_md"' in rendered["markdownUser"]
                assert 'session-link' not in rendered["streaming"]
                assert rendered["lineage"] == {"alpha": "alpha-tip", "unscoped": "unknown-tip"}
                for alias in ("duplicate", "conflict", "routeConflict", "routeEmpty", "routeExtra", "empty", "invalid"):
                    assert rendered["aliases"][alias] == {"sid": None, "ambiguous": True}
                assert rendered["aliases"]["routeTrailingSlash"] == {"sid": "route-id", "ambiguous": False}
                assert rendered["aliases"]["single"] == {"sid": "a", "ambiguous": False}
                roster = page.evaluate("async () => (await fetch('/api/profiles')).json()")
                assert any(p["name"] == "default" and p["is_default"] for p in roster["profiles"])
                boot_requests = []
                def capture_boot_request(request):
                    path = urllib.parse.urlsplit(request.url).path
                    if request.method == "POST" and path.endswith("/api/profile/switch"): boot_requests.append("switch")
                    if request.method == "GET" and path.endswith("/api/session"): boot_requests.append("session")
                page.on("request", capture_boot_request)
                invalid_boots = []
                for query in ("profile=bad%20name", "profile=ops&profile=beta"):
                    page.evaluate("sid => localStorage.setItem('hermes-webui-session',sid)", source)
                    boot_requests.clear()
                    page.goto(base + "/?" + query, wait_until="domcontentloaded")
                    page.wait_for_function("() => typeof S !== 'undefined' && S._bootReady === true")
                    invalid_boots.append({"search": page.evaluate("location.search"), "requests": boot_requests[:]})
                page.remove_listener("request", capture_boot_request)
                assert invalid_boots == [{"search": "?profile=bad%20name", "requests": []},
                                         {"search": "?profile=ops&profile=beta", "requests": []}]
                # Exercise a rejected recovery render through boot.
                session_lists = []
                gateway_streams = []
                def capture_boot_recovery(request):
                    path = urllib.parse.urlsplit(request.url).path
                    if request.method == "GET" and path.endswith("/api/sessions"): session_lists.append(request.url)
                    if path.endswith("/api/sessions/gateway/stream"): gateway_streams.append(request.url)
                boot_page = context.new_page()
                boot_page.add_init_script("""(() => {
                  new MutationObserver((_, observer) => {
                    const script=document.querySelector('script[src*="static/sessions.js"]');
                    if(!script)return; observer.disconnect();
                    script.addEventListener('load', () => {
                      const real=window.renderSessionList; window.__renderListRejects=0;
                      window.renderSessionList=async(...args)=>{
                        await real(...args); if(++window.__renderListRejects===1)throw Error('test render failure');
                      };
                    },{once:true});
                  }).observe(document,{childList:true,subtree:true});
                })()""")
                boot_page.on("request", capture_boot_recovery)
                boot_page.goto(base + "/session/missing-session?profile=default", wait_until="domcontentloaded")
                boot_page.wait_for_function("() => S._bootReady === true && window.__renderListRejects > 0 && !!_gatewaySSE")
                for _ in range(40):
                    if gateway_streams:
                        break
                    boot_page.wait_for_timeout(50)
                assert session_lists and gateway_streams
                boot_page.close()
                switch_responses = []
                def capture_profile_response(response):
                    if urllib.parse.urlsplit(response.url).path.endswith("/api/profile/switch"):
                        name = json.loads(response.request.post_data or "{}").get("name")
                        switch_responses.append((name, (response.all_headers().get("set-cookie") or "").lower()))
                page.on("response", capture_profile_response)
                page.goto(base + f"/session/{source}?profile=default", wait_until="domcontentloaded")
                wait_for_visible_session(page, source)
                page.wait_for_function("text => document.getElementById('fileTree').innerText.includes(text)", arg="source.txt")
                page.locator("#msg").fill("source draft survives failed navigation")
                page.evaluate("_addNamedContextBlock('cleared after successful navigation')")
                page.locator(f'a.session-link[data-session-id="{ops_good}"][data-session-profile="ops"]').click()
                wait_for_visible_session(page, ops_good)
                page.wait_for_function("() => document.getElementById('msgInner').innerText.includes('Destination transcript')")
                assert "Destination transcript" in page.locator("#msgInner").inner_text() and page.locator("#composerSelectionChips").is_hidden()
                assert page.evaluate("S.activeProfile") == "ops"
                try:
                    page.wait_for_function("() => window._defaultModel === 'claude-3-5-haiku'", timeout=10000)
                except PlaywrightTimeoutError as error:
                    state = page.evaluate("() => ({model:window._defaultModel, workspace:S._profileDefaultWorkspace, active:S.activeProfile, session:S.session?.session_id})")
                    raise AssertionError(f"destination defaults did not settle: {state}") from error
                assert page.evaluate("S._profileDefaultWorkspace") == str(ops_workspace)
                assert any(name == "ops" and "hermes_profile=ops;" in header and "httponly" in header for name, header in switch_responses), "browser did not receive the real server profile cookie: " + repr(switch_responses)
                assert any(cookie["name"] == "hermes_profile" and cookie["value"] == "ops" for cookie in context.cookies(base)), "browser did not retain the server-set ops cookie"
                assert page.evaluate("async () => (await (await fetch('/api/profile/active')).json()).name") == "ops"
                assert page.evaluate("location.pathname+location.search") == f"/session/{ops_good}?profile=ops"
                # Cold restore must switch the real ops cookie back to default.
                assert any(cookie["name"] == "hermes_profile" and cookie["value"] == "ops" for cookie in context.cookies(base))
                page.goto(base + f"/session/{source}?profile=default", wait_until="domcontentloaded")
                page.wait_for_function(f"() => S.session && S.session.session_id === {json.dumps(source)}")
                assert page.evaluate("S.activeProfile") == "default"
                assert page.evaluate("async () => (await (await fetch('/api/profile/active')).json()).is_default") is True
                assert any(name == "default" and "hermes_profile=" in header and "httponly" in header for name, header in switch_responses), "cold root-alias navigation did not receive the real server cookie"
                assert any(cookie["name"] == "hermes_profile" and cookie["value"] == "default" for cookie in context.cookies(base))
                alias_posts = []
                def record_alias_post(request):
                    if request.method == "POST" and urllib.parse.urlsplit(request.url).path.endswith("/api/profile/switch"):
                        alias_posts.append(request.url)
                page.on("request", record_alias_post)
                page.evaluate("() => {S.activeProfile='root'; S.activeProfileIsDefault=true}")
                alias_before = page.evaluate("() => ({state:JSON.stringify({session:S.session,messages:S.messages,profile:S.activeProfile,isDefault:S.activeProfileIsDefault,workspace:S._profileDefaultWorkspace,model:window._defaultModel,provider:window._defaultModelProvider}),local:localStorage.getItem('hermes-webui-session'),url:location.href})")
                assert alias_before["local"] == source
                assert page.evaluate("async () => await switchToProfile('default')") is True
                assert alias_posts == []
                assert page.evaluate("() => JSON.stringify({session:S.session,messages:S.messages,profile:S.activeProfile,isDefault:S.activeProfileIsDefault,workspace:S._profileDefaultWorkspace,model:window._defaultModel,provider:window._defaultModelProvider})") == alias_before["state"]
                assert page.evaluate("localStorage.getItem('hermes-webui-session')") == alias_before["local"]
                assert page.evaluate("location.href") == alias_before["url"]
                page.remove_listener("request", record_alias_post)
                page.evaluate("S.activeProfile='default'")
                page.reload(wait_until="domcontentloaded")
                wait_for_visible_session(page, source)
                assert "Source transcript" in page.locator("#msgInner").inner_text()
                assert page.locator("#msg").input_value() == "source draft survives failed navigation"
                page.wait_for_function("() => document.getElementById('fileTree').innerText.includes('source.txt')")
                # Hold background model refresh to check defaults after the real 409 switch.
                held_models = []
                def hold_models(route):
                    held_models.append(route)
                page.route("**/api/models*", hold_models)
                with page.expect_response(lambda response: response.status == 409
                        and urllib.parse.parse_qs(urllib.parse.urlsplit(response.url).query).get("session_id") == [ops_good]
                        and urllib.parse.parse_qs(urllib.parse.urlsplit(response.url).query).get("messages") == ["0"]):
                    page.evaluate("sid => loadSession(sid)", ops_good)
                page.wait_for_function(f"() => S.session && S.session.session_id === {json.dumps(ops_good)} && S.activeProfile === 'ops'")
                for _ in range(40):
                    if held_models:
                        break
                    page.wait_for_timeout(50)
                assert held_models
                assert page.evaluate("window._defaultModel") == "claude-3-5-haiku"
                with page.expect_request(lambda request: request.method == "POST" and request.url.endswith("/api/session/new")) as new_chat_request:
                    page.locator("#btnNewChat").click()
                new_chat_payload = json.loads(new_chat_request.value.post_data or "{}")
                page.wait_for_function(f"() => S.session && S.session.session_id !== {json.dumps(ops_good)}")
                assert new_chat_payload["profile"] == "ops"
                assert new_chat_payload["model"] == "claude-3-5-haiku"
                assert new_chat_payload["model_provider"] == "anthropic"
                assert new_chat_payload["workspace"] == str(ops_workspace)
                for route in held_models:
                    route.continue_()
                page.unroute("**/api/models*")
                page.goto(base + f"/session/{source}?profile=default", wait_until="domcontentloaded")
                page.wait_for_function(f"() => S.session && S.session.session_id === {json.dumps(source)} && S.activeProfile === 'default'")
                page.wait_for_function("() => document.getElementById('fileTree').innerText.includes('source.txt')")
                rejected_loads = []
                def abort_rejected_target(route):
                    request = route.request
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.url).query)
                    if request.method == "GET" and query.get("session_id") == [ops_failed] and query.get("messages") == ["1"]:
                        rejected_loads.append(request.url)
                        route.abort()
                    else:
                        route.continue_()
                page.route("**/api/session*", abort_rejected_target)
                page.locator("#msg").fill("rollback draft")
                page.evaluate("sid => {queueSessionMessage(sid,{text:'source queued item',profile:'default'});_addNamedContextBlock('context restored after failed navigation');updateQueueBadge(sid);}", source)
                page.locator(f'a.session-link[data-session-id="{ops_failed}"][data-session-profile="ops"]').click()
                page.wait_for_function(f"() => !_sessionNavigationRollbackAnchor && S.session && S.session.session_id === {json.dumps(source)} && S.activeProfile === 'default' && document.getElementById('msg').value === 'rollback draft'")
                page.unroute("**/api/session*", abort_rejected_target)
                rollback = page.evaluate("""() => ({sid:S.session&&S.session.session_id,profile:S.activeProfile,
                  messages:S.messages.map(m=>m.content||m),text:document.getElementById('msgInner').innerText,
                  draft:document.getElementById('msg').value,url:location.pathname+location.search,
                  model:window._defaultModel,workspace:S.session&&S.session.workspace,
                  tree:document.getElementById('fileTree').innerText,selectionHidden:document.getElementById('composerSelectionChips').hidden,
                  selectionText:document.querySelector('.selection-context-quote')?.textContent||''})""")
                assert rejected_loads
                assert rollback["sid"] == source and rollback["profile"] == "default" and not rollback["selectionHidden"] and "context restored after failed navigation" in rollback["selectionText"]
                assert "Source transcript" in rollback["text"] and "This transcript must not replace the source" not in rollback["text"]
                assert rollback["draft"] == "rollback draft"
                assert rollback["url"] == f"/session/{source}?profile=default"
                assert rollback["model"] == "gpt-4.1-mini" and rollback["workspace"] == str(default_workspace)
                assert "source.txt" in rollback["tree"]
                assert page.evaluate(f"getQueuedSessionCount({json.dumps(source)})") == 1 and page.evaluate(f"getQueuedSessionCount({json.dumps(ops_failed)})") == 0
                assert "source queued item" in page.locator("#queueChips").inner_text()
                page.evaluate("sid => {shiftQueuedSessionMessage(sid);updateQueueBadge(sid);}", source)
                assert any(name == "default" and "hermes_profile=" in header and "httponly" in header for name, header in switch_responses), "failed target did not restore the real root-profile cookie"
                assert page.evaluate("async () => (await (await fetch('/api/profile/active')).json()).is_default") is True
                page.evaluate("() => {window.__rollbackSnapshot=_captureSessionNavigationView();S._profileCookieOwnershipUncertain=true;}")
                with page.expect_request(lambda request: request.method == "POST" and request.url.endswith("/api/profile/switch")):
                    assert page.evaluate("async () => await _restoreSessionReference(window.__rollbackSnapshot,_sessionNavigationGeneration)") is True
                assert page.evaluate("S._profileCookieOwnershipUncertain") is False
                race_aborts = []
                def abort_superseded(route):
                    request = route.request
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.url).query)
                    if request.method == "GET" and query.get("session_id") == [race_a] and query.get("messages") == ["1"]:
                        race_aborts.append(request.url)
                        route.abort()
                    else:
                        route.continue_()
                page.route("**/api/session*", abort_superseded)
                page.locator(f'a.session-link[data-session-id="{race_a}"]').click()
                for _ in range(40):
                    if race_aborts:
                        break
                    page.wait_for_timeout(50)
                assert race_aborts
                page.locator(f'.session-item[data-sid="{race_b}"]').click()
                wait_for_visible_session(page, race_b)
                page.wait_for_timeout(250)
                page.unroute("**/api/session*", abort_superseded)
                race_state = page.evaluate("""() => ({sid:S.session&&S.session.session_id,text:document.getElementById('msgInner').innerText,
                  url:location.pathname+location.search,local:localStorage.getItem('hermes-webui-session')})""")
                assert len(race_aborts) >= 1
                assert race_state["sid"] == race_b and "Current navigation transcript" in race_state["text"]
                assert race_state["local"] == race_b and f"/session/{race_b}" in race_state["url"]
                page.goto(base + f"/session/{source}?profile=default", wait_until="domcontentloaded")
                wait_for_visible_session(page, source)
                pending_preflight = []
                def hold_reference_metadata(route):
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(route.request.url).query)
                    if route.request.method == "GET" and query.get("session_id") == [race_a] and query.get("messages") == ["0"]:
                        pending_preflight.append(route)
                    else:
                        route.continue_()
                page.route("**/api/session*", hold_reference_metadata)
                page.locator(f'a.session-link[data-session-id="{race_a}"]').click()
                for _ in range(40):
                    if pending_preflight: break
                    page.wait_for_timeout(50)
                assert pending_preflight
                page.locator("#btnNewChat").click()
                page.wait_for_function(f"() => S.session && S.session.session_id !== {json.dumps(source)}")
                new_chat_sid = page.evaluate("S.session.session_id")
                assert page.evaluate("_sessionNavigationRollbackAnchor") is None
                with page.expect_response(lambda response: urllib.parse.parse_qs(urllib.parse.urlsplit(response.url).query).get("session_id") == [race_a]):
                    for route in pending_preflight: route.continue_()
                page.wait_for_timeout(250)
                assert page.evaluate("S.session.session_id") == new_chat_sid
                assert page.evaluate("location.pathname") == f"/session/{new_chat_sid}"
                page.unroute("**/api/session*", hold_reference_metadata)
                page.goto(base + f"/session/{source}?profile=default", wait_until="domcontentloaded")
                wait_for_visible_session(page, source)
                held_target = []
                def hold_target_load(route):
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(route.request.url).query)
                    if query.get("session_id") == [ops_good] and query.get("messages") == ["1"]: held_target.append(route)
                    else: route.continue_()
                page.route("**/api/session*", hold_target_load)
                page.locator(f'a.session-link[data-session-id="{ops_good}"]').first.click()
                for _ in range(40):
                    if held_target: break
                    page.wait_for_timeout(50)
                assert held_target and page.evaluate("S.activeProfile") == "ops" and page.evaluate("!!_sessionNavigationRollbackAnchor"), page.evaluate("({profile:S.activeProfile,anchor:!!_sessionNavigationRollbackAnchor})")
                page.locator("#btnNewChat").click()
                page.wait_for_function(f"() => !_newSessionInFlight && S.session && S.session.session_id !== {json.dumps(source)} && S.session.profile === 'default' && S.activeProfile === 'default'")
                restored_chat = page.evaluate("({sid:S.session.session_id,profile:S.session.profile,model:S.session.model,provider:S.session.model_provider,workspace:S.session.workspace})")
                assert restored_chat["profile"] == "default"
                assert (restored_chat["model"], restored_chat["provider"], restored_chat["workspace"]) == ("gpt-4.1-mini", "openai", str(default_workspace))
                assert any(c.get("value") == "default" for c in context.cookies(base) if c.get("name") == "hermes_profile")
                for route in held_target: route.continue_()
                page.wait_for_timeout(250)
                assert page.evaluate("S.session.session_id") == restored_chat["sid"]
                page.unroute("**/api/session*", hold_target_load)
                page.evaluate("""() => {
                  const render=renderSessionList;let release,released=false;const gate=new Promise(resolve=>release=resolve);
                  window.__releaseSwitchList=()=>{released=true;release()};window.__switchListHeld=false;
                  renderSessionList=async(...args)=>{if(S.activeProfile==='ops'&&!released){window.__switchListHeld=true;await gate;}return render(...args)};
                  window.__switchA=switchToProfile('ops');}""")
                page.wait_for_function("() => window.__switchListHeld && S.activeProfile === 'ops'")
                assert page.evaluate("async () => {window.__switchB=switchToProfile('default');return await window.__switchB}") is True and any(c["name"] == "hermes_profile" and c["value"] == "default" for c in context.cookies(base))
                assert page.evaluate("async () => {window.__releaseSwitchList();return [await window.__switchA,S._profileCookieOwnershipUncertain]}") == [False, False]
                direct_held = []
                resume_direct = [False]
                def fail_direct_switch(route):
                    if json.loads(route.request.post_data or "{}").get("name") == "default" and not resume_direct[0]:
                        direct_held.append(route)
                    else:
                        route.abort()
                page.route("**/api/profile/switch", fail_direct_switch)
                page.evaluate("() => {window.__directSwitch = switchToProfile('ops')}")
                for _ in range(80):
                    if direct_held: break
                    page.wait_for_timeout(50)
                assert direct_held and page.evaluate("S._profileCookieOwnershipUncertain") is True
                assert page.evaluate("async () => {try{await api('/api/session?session_id=blocked');return false;}catch(_){return true;}}") is True
                resume_direct[0] = True
                for route in direct_held: route.abort()
                assert page.evaluate("async () => await window.__directSwitch") is False
                assert page.evaluate("S._profileCookieOwnershipUncertain") is True
                page.unroute("**/api/profile/switch", fail_direct_switch)
                # Failed switchback must keep the old transcript read-only.
                page.goto(base + f"/session/{source}?profile=default", wait_until="domcontentloaded")
                wait_for_visible_session(page, source)
                page.locator("#msg").fill("saved before uncertain switchback")
                uncertain_switches = []
                target_switch_seen = False
                failed_default_switch = False
                def abort_uncertain_rollback(route):
                    nonlocal target_switch_seen, failed_default_switch
                    request = route.request
                    path = urllib.parse.urlsplit(request.url).path
                    if path.endswith("/api/profile/switch") and request.method == "POST":
                        name = json.loads(request.post_data or "{}").get("name")
                        if name == "ops" and not target_switch_seen:
                            target_switch_seen = True
                        elif target_switch_seen and (name == "default" or (failed_default_switch and name == "ops")):
                            failed_default_switch = failed_default_switch or name == "default"
                            uncertain_switches.append(name)
                            route.abort()
                            return
                    else:
                        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.url).query)
                        if request.method == "GET" and query.get("session_id") == [ops_uncertain] and query.get("messages") == ["1"]:
                            route.abort()
                            return
                    route.continue_()
                page.route("**/api/session*", abort_uncertain_rollback)
                page.route("**/api/profile/switch", abort_uncertain_rollback)
                page.locator(f'a.session-link[data-session-id="{ops_uncertain}"][data-session-profile="ops"]').click()
                page.wait_for_function(f"() => S._profileCookieOwnershipUncertain === true && S.session && S.session.session_id === {json.dumps(source)} && S.messages.some(m => String(m.content || m).includes('Source transcript'))")
                page.unroute("**/api/session*", abort_uncertain_rollback)
                page.unroute("**/api/profile/switch", abort_uncertain_rollback)
                assert uncertain_switches and uncertain_switches[0] == "default" and "ops" in uncertain_switches[1:]
                assert page.evaluate("S.activeProfile") == "unconfirmed"
                assert page.locator("#msg").input_value() == "saved before uncertain switchback"
                assert "Source transcript" in page.locator("#msgInner").inner_text()
                assert any(cookie["name"] == "hermes_profile" and cookie["value"] == "ops" for cookie in context.cookies(base)), "failed compensation changed the last confirmed server cookie"
                blocked_requests = []
                page.on("request", lambda request: blocked_requests.append(request.url)
                        if request.method == "POST" and (request.url.endswith("/api/session/new") or request.url.endswith("/api/chat/start")) else None)
                page.locator("#btnSend").click()
                page.locator("#btnNewChat").click()
                page.wait_for_timeout(100)
                assert not blocked_requests, "an operation was sent with unconfirmed profile ownership"
                assert page.evaluate("async () => {try{await api('/api/session?session_id=blocked');return false;}catch(_){return true;}}") is True
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
