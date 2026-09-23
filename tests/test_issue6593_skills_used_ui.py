"""Browser-facing regression coverage for the Skills Used Artifacts section."""

import json
import io
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

import api.models as models
import api.routes as routes
from api.models import Session


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _extract_function(source, name, *, async_function=False):
    marker = ("async function " if async_function else "function ") + name + "("
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"Could not extract {name}")


def _run_node(source):
    if NODE is None:
        pytest.skip("node is not installed")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(source)
        script = handle.name
    try:
        completed = subprocess.run(
            [NODE, script],
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        return completed.stdout.strip()
    finally:
        Path(script).unlink(missing_ok=True)


def test_use_and_bundle_resolution_send_owning_session_id():
    commands = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")
    cmd_use = _extract_function(commands, "cmdUse", async_function=True)
    bundle = _extract_function(commands, "resolveBundleCommand", async_function=True)
    output = _run_node(
        "\n".join(
            [
                "const calls=[];",
                "const S={session:null,messages:[]};",
                "let _forcedSkillDirectivePending=null;",
                "const newSession=async()=>{S.session={session_id:'created-owner'};};",
                "const renderSessionList=async()=>{};",
                "const renderMessages=()=>{};",
                "const showToast=()=>{};",
                "const api=async (url,options={})=>{calls.push({url,body:options.body?JSON.parse(options.body):null}); if(url==='/api/skills') return {skills:[{name:'server-skill'}]}; if(url.startsWith('/api/skills/content')) return {content:'body'}; return {message:'resolved'};};",
                cmd_use,
                bundle,
                "(async()=>{await cmdUse('server-skill'); S.session=null; await resolveBundleCommand('/bundle request'); console.log(JSON.stringify(calls));})();",
            ]
        )
    )
    calls = json.loads(output)
    content_call = next(call for call in calls if call["url"].startswith("/api/skills/content?"))
    assert "session_id=created-owner" in content_call["url"]
    bundle_call = next(call for call in calls if call["body"] and call["body"].get("command"))
    assert bundle_call["body"] == {"command": "/bundle request", "session_id": "created-owner"}


def test_empty_use_returns_before_installing_pending_directive():
    commands = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")
    cmd_use = _extract_function(commands, "cmdUse", async_function=True)
    output = _run_node(
        "\n".join(
            [
                "const S={session:null,messages:[]};",
                "let _forcedSkillDirectivePending=null;",
                "let newSessions=0; let apiCalls=0;",
                "const newSession=async()=>{newSessions+=1;};",
                "const renderSessionList=async()=>{};",
                "const renderMessages=()=>{};",
                "const showToast=()=>{};",
                "const api=async()=>{apiCalls+=1;return {};};",
                cmd_use,
                "(async()=>{await cmdUse(''); console.log(JSON.stringify({pending:_forcedSkillDirectivePending,newSessions,apiCalls,messages:S.messages}));})();",
            ]
        )
    )
    result = json.loads(output)
    assert result["pending"] is None
    assert result["newSessions"] == 0
    assert result["apiCalls"] == 0
    assert "Usage:" in result["messages"][0]["content"]


def test_session_skill_use_renders_in_artifacts():
    workspace = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    usage = _extract_function(workspace, "_sessionSkillUsage")
    refresh = _extract_function(workspace, "_refreshSessionSkillUsageForOwner", async_function=True)
    output = _run_node(
        "\n".join(
            [
                "const S={session:{session_id:'owner',skill_provenance:{}},messages:[],toolCalls:[]};",
                "let rendered=0;",
                "const _isSessionCurrentPane=id=>id==='owner';",
                "const renderSessionArtifacts=()=>{rendered+=1;};",
                "const api=async url=>{if(!url.includes('messages=0')||!url.includes('resolve_model=0')) throw new Error('missing detail flags'); return {session:{skill_provenance:{'review-skill':2}}};};",
                usage,
                refresh,
                "const refreshSessionSkillUsage=_refreshSessionSkillUsageForOwner;",
                "(async()=>{await refreshSessionSkillUsage('owner'); console.log(JSON.stringify({usage:_sessionSkillUsage(),rendered,S}));})();",
            ]
        )
    )
    result = json.loads(output)
    assert result["usage"] == [{"name": "review-skill", "count": 2}]
    assert result["rendered"] == 1
    assert result["S"]["session"]["skill_provenance"] == {"review-skill": 2}


def test_use_persists_then_artifacts_open_fetches_and_renders_repeated_count(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    models.SESSIONS.clear()
    session = Session(session_id="issue6593-e2e", profile="default")
    session.save(touch_updated_at=False, skip_index=True)
    monkeypatch.setattr(routes, "get_session", lambda _sid: Session.load(session.session_id))
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(
        routes,
        "_skill_view_from_active_dir",
        lambda _name: {"success": True, "name": "review-skill", "content": "body"},
    )
    payload = {}
    monkeypatch.setattr(routes, "j", lambda _handler, body, **_kwargs: payload.update(body) or True)

    class Handler:
        command = "GET"
        headers = {}
        rfile = io.BytesIO()
        wfile = io.BytesIO()

        def _safe_webui_print(self, *_args, **_kwargs):
            return None

    parsed = SimpleNamespace(
        path="/api/skills/content",
        query=urlencode({"name": "client-name", "session_id": session.session_id}),
    )
    assert routes.handle_get(Handler(), parsed) is True
    assert routes.handle_get(Handler(), parsed) is True
    detail = Session.load(session.session_id).compact()
    assert detail["skill_provenance"] == {"review-skill": 2}

    workspace = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    switch_tab = _extract_function(workspace, "switchWorkspacePanelTab")
    refresh = _extract_function(workspace, "_refreshSessionSkillUsageForOwner", async_function=True)
    usage = _extract_function(workspace, "_sessionSkillUsage")
    renderer = _extract_function(workspace, "renderSessionArtifacts")
    output = _run_node(
        "\n".join(
            [
                f"const detail={json.dumps({'session': detail})};",
                "const root={innerHTML:'',hidden:true,querySelector(){return null;}}; const count={textContent:''};",
                "const tab={classList:{toggle(){}},setAttribute(){}};",
                "const $=id=>id==='workspaceArtifacts'?root:id==='workspaceArtifactsCount'?count:id==='workspaceArtifactsTab'?tab:null;",
                f"const S={{session:{{session_id:{json.dumps(session.session_id)},workspace:'',skill_provenance:{{}}}},messages:[],toolCalls:[]}};",
                "let _workspacePanelActiveTab='files';",
                "const _setWorkspacePanelTabDataset=()=>{};",
                "const collectSessionArtifacts=()=>[];",
                "const _isSessionCurrentPane=id=>S.session&&S.session.session_id===id;",
                "const t=key=>key==='insights_skill_usage_skills_used'?'Skills Used':'Uses';",
                "const esc=value=>String(value);",
                "const api=async()=>detail;",
                usage,
                renderer,
                refresh,
                switch_tab,
                "(async()=>{switchWorkspacePanelTab('artifacts'); await new Promise(resolve=>setTimeout(resolve,0)); console.log(JSON.stringify({html:root.innerHTML,count:S.session.skill_provenance['review-skill'],badge:count.textContent}));})();",
            ]
        )
    )
    result = json.loads(output)
    assert result["count"] == 2
    assert result["badge"] == "1"
    assert "review-skill" in result["html"]
    assert "Uses: 2" in result["html"]


def test_artifacts_open_refreshes_owner_and_rejects_stale_response():
    workspace = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    refresh = _extract_function(workspace, "_refreshSessionSkillUsageForOwner", async_function=True)
    output = _run_node(
        "\n".join(
            [
                "const S={session:{session_id:'A',skill_provenance:{A:1}}};",
                "let currentPane='A'; let rendered=0; let resolveA;",
                "const _isSessionCurrentPane=id=>id===currentPane;",
                "const renderSessionArtifacts=()=>{rendered+=1;};",
                "const api=async url=>new Promise(resolve=>{resolveA=()=>resolve({session:{session_id:'A',skill_provenance:{A:9}}});});",
                refresh,
                "const refreshSessionSkillUsage=_refreshSessionSkillUsageForOwner;",
                "(async()=>{const pending=refreshSessionSkillUsage('A'); currentPane='B'; S.session={session_id:'B',skill_provenance:{B:2}}; resolveA(); await pending; console.log(JSON.stringify({S,rendered}));})();",
            ]
        )
    )
    result = json.loads(output)
    assert result == {"S": {"session": {"session_id": "B", "skill_provenance": {"B": 2}}}, "rendered": 0}


def test_skills_used_disclosure_renders_counts_at_supported_widths():
    workspace = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    usage = _extract_function(workspace, "_sessionSkillUsage")
    renderer = _extract_function(workspace, "renderSessionArtifacts")
    output = _run_node(
        "\n".join(
            [
                "const root={innerHTML:''}; const count={textContent:''};",
                "const $=id=>id==='workspaceArtifacts'?root:id==='workspaceArtifactsCount'?count:null;",
                "const S={session:{session_id:'owner',workspace:'' ,skill_provenance:{'zeta <script>':2,'alpha':4}},messages:[],toolCalls:[]};",
                "const collectSessionArtifacts=()=>[];",
                "const t=key=>key==='insights_skill_usage_skills_used'?'Skills Used':'Uses';",
                "const esc=value=>String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;');",
                usage,
                renderer,
                "renderSessionArtifacts(); console.log(JSON.stringify({html:root.innerHTML,count:count.textContent}));",
            ]
        )
    )
    result = json.loads(output)
    html = result["html"]
    assert result["count"] == "2"
    assert "<details" in html and "open" in html
    assert html.index("alpha") < html.index("zeta")
    assert "zeta &lt;script&gt;" in html
    assert "<script>" not in html
    assert "Uses: 2" in html and "Uses: 4" in html

    try:
        from playwright.sync_api import sync_playwright
        from tests._layout_helpers import assert_layout_sane
    except Exception:
        pytest.skip("playwright is not installed")
    css = "\n".join(
        line for line in (ROOT / "static" / "style.css").read_text(encoding="utf-8").splitlines()
        if "workspace-artifact" in line
    )
    harness = f"""
        <style>
          :root {{ --border: #444; --muted: #9da3b4; --text: #f1f1f1; --surface-subtle: #202331; --accent: #8ab4f8; --font-mono: ui-monospace, monospace; }}
          body {{ margin: 0; background: #11131b; color: var(--text); font: 13px system-ui, sans-serif; }}
          #workspaceArtifacts {{ box-sizing: border-box; width: 100%; max-width: 360px; height: 280px; padding: 8px; overflow: auto; }}
          {css}
        </style>
        <div id="workspaceArtifacts"></div><span id="workspaceArtifactsCount"></span>
        <script>
          const S={{session:{{session_id:'owner',workspace:'',skill_provenance:{{'zeta <script>':2,alpha:4}}}},messages:[],toolCalls:[]}};
          const $=id=>document.getElementById(id);
          const collectSessionArtifacts=()=>[];
          const t=key=>key==='insights_skill_usage_skills_used'?'Skills Used':'Uses';
          const esc=value=>String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;').replace(/'/g,'&#39;');
          const openArtifactPath=()=>{{}};
          {usage}
          {renderer}
          renderSessionArtifacts();
        </script>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(viewport={"width": 1024, "height": 600}, device_scale_factor=1)
            page.set_content(harness)
            for width in (1024, 480):
                page.set_viewport_size({"width": width, "height": 320})
                assert page.locator(".workspace-artifact-skill-row").count() == 2
                assert page.locator(".workspace-artifact-skill-name").first.inner_text() == "alpha"
                disclosure = page.locator("details.workspace-artifact-skills")
                disclosure.locator("summary").click()
                assert disclosure.evaluate("element => !element.open")
                page.evaluate("renderSessionArtifacts()")
                assert disclosure.evaluate("element => !element.open")
                disclosure.locator("summary").click()
                assert disclosure.evaluate("element => element.open")
                assert_layout_sane(page, "#workspaceArtifacts", checks=["overlap", "clip", "container-escape", "raw-string"])
                page.screenshot(path=str(ROOT.parent / ".claude" / "pr-sweep" / "runs" / "webui-6593-2-respec" / f"skills-used-{width}.png"))
        finally:
            browser.close()


def test_artifacts_empty_and_files_only_states_remain_usable():
    workspace = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    usage = _extract_function(workspace, "_sessionSkillUsage")
    renderer = _extract_function(workspace, "renderSessionArtifacts")
    output = _run_node(
        "\n".join(
            [
                "const root={innerHTML:''}; const count={textContent:''};",
                "const $=id=>id==='workspaceArtifacts'?root:id==='workspaceArtifactsCount'?count:null;",
                "const S={session:{session_id:'owner',workspace:'',skill_provenance:{}},messages:[],toolCalls:[]};",
                "let artifacts=[]; const collectSessionArtifacts=()=>artifacts;",
                "const t=key=>key==='insights_skill_usage_skills_used'?'Skills Used':'Uses';",
                "const esc=value=>String(value);",
                usage,
                renderer,
                "renderSessionArtifacts(); const empty=root.innerHTML; S.session.skill_provenance={review:1}; renderSessionArtifacts(); const skills=root.innerHTML; artifacts=[{path:'README.md',source:'write_file'}]; S.session.skill_provenance={}; renderSessionArtifacts(); const files=root.innerHTML; console.log(JSON.stringify({empty,skills,files}));",
            ]
        )
    )
    result = json.loads(output)
    assert "No artifacts detected" in result["empty"]
    assert "workspace-artifact-skills" in result["skills"]
    assert "No artifacts detected" not in result["skills"]
    assert "README.md" in result["files"]


def test_active_stream_conflict_keeps_resolved_use_payload():
    messages = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    send_start = messages.index("async function send(){")
    send_end = messages.index("\nasync function startRegeneration", send_start)
    send = messages[send_start:send_end]
    script = textwrap.dedent(
        """
        const assert = require('assert');
        const _recoverCompressedSend = async () => false;
        const input = {value:'hello'};
        const queued = [];
        const S = {
          session:{session_id:'A',workspace:'/a',profile:'work',model:'model-a',model_provider:'provider-a',title:'A'},
          messages:[],pendingFiles:[],toolCalls:[],activeProfile:'work',busy:false,activeStreamId:null,
        };
        const INFLIGHT = {};
        const _pendingSelections = [];
        let _forcedSkillDirectivePending = {
          sessionId:'A',
          promise:Promise.resolve({directive:'/use writer',name:'writer',content:'follow the guide'}),
        };
        let _sendInProgress = false;
        let _sendInProgressSid = null;
        let _queueDrainSid = null;
        const localStorage = {setItem(){},removeItem(){}};
        const history = {replaceState(){}};
        const window = {_defaultModel:'model-a',_activeProvider:'provider-a'};
        const document = {querySelector(){return null;}};
        const $ = id => id === 'msg' ? input : null;
        function _composerTextWithPendingSelections(){return input.value;}
        function _chatPayloadModelState(){return {model:S.session.model,model_provider:S.session.model_provider};}
        function _flushSelectionBlocksToComposer(){}
        function shouldInterceptCompressionRecoveryContinuation(){return false;}
        function _dismissHandoffHint(){}
        function _clearStaleBusyStateBeforeSend(){}
        function renderTray(){}
        function autoResize(){}
        function _clearComposerDraft(){return Promise.resolve();}
        function setComposerStatus(){}
        function clearLiveToolCards(){}
        function renderMessages(){}
        function setBusy(value){S.busy=value;}
        function ensureLiveWorklogShell(){}
        function appendThinking(){}
        function _runOptionalPreStartUiStep(_label,fn){if(fn)fn();}
        function _runOptionalPostStartUiStep(_label,fn){if(fn)fn();}
        function upsertActiveSessionForLocalTurn(){}
        function renderSessionListFromCache(){}
        function startApprovalPolling(){}
        function startClarifyPolling(){}
        function _fetchYoloState(){}
        function updateSendBtn(){}
        function applySessionTitleUpdate(){}
        function _readPendingSessionModel(){return null;}
        function _clearPendingSessionModel(){}
        function _writePersistedModelState(){}
        function _applyModelToDropdown(){}
        function syncTopbar(){}
        function syncModelChip(){}
        function stopApprovalPolling(){}
        function stopClarifyPolling(){}
        function hideApprovalCard(){}
        function hideClarifyCard(){}
        function removeThinking(){}
        function clearOptimisticSessionStreaming(){}
        function renderSessionList(){}
        function markInflight(){}
        function saveInflightState(){}
        function clearInflightState(){}
        function queueSessionMessage(sid,payload){queued.push({sid,payload});}
        function _clearComposerAfterQueuedSelectionSend(){}
        function updateQueueBadge(){}
        function showToast(){}
        function stopSessionStream(){}
        async function loadSession(sid){assert.strictEqual(sid,'A');}
        async function newSession(){throw new Error('unexpected new session');}
        async function uploadPendingFiles(){return [];}
        async function api(){throw new Error('Session already has an active stream');}
        %(send)s
        (async()=>{
          await send();
          assert.strictEqual(queued.length,1);
          assert.strictEqual(queued[0].sid,'A');
          assert.strictEqual(queued[0].payload.files.length,0);
          assert.ok(queued[0].payload.text.includes('/use writer'));
          assert.ok(queued[0].payload.text.includes('[FORCED SKILL CONTEXT: writer]'));
          assert.ok(queued[0].payload.text.includes('follow the guide'));
          assert.ok(queued[0].payload.text.endsWith('hello'));
          assert.strictEqual(_forcedSkillDirectivePending,null);
        })().catch(error=>{console.error(error);process.exit(1);});
        """
    ) % {"send": send}
    _run_node(script)


def test_real_page_layout_probe_is_available_for_reality_gate():
    pytest.importorskip("playwright.sync_api")
