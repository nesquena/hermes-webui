"""Behavioral regression coverage for the New Session composer boundary."""

import json
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).parents[1]
SESSIONS_JS = ROOT.joinpath("static", "sessions.js").read_text(encoding="utf-8")
UI_JS = ROOT.joinpath("static", "ui.js").read_text(encoding="utf-8")
BOOT_JS = ROOT.joinpath("static", "boot.js").read_text(encoding="utf-8")
MESSAGES_JS = ROOT.joinpath("static", "messages.js").read_text(encoding="utf-8")
PANELS_JS = ROOT.joinpath("static", "panels.js").read_text(encoding="utf-8")
WORKSPACE_JS = ROOT.joinpath("static", "workspace.js").read_text(encoding="utf-8")


def _function(source: str, name: str, next_marker: str) -> str:
    start = source.find(f"function {name}(")
    end = source.find(next_marker, start)
    assert start != -1 and end != -1, f"{name} function not found"
    return source[start:end].strip()


def _set_new_session_pending_function() -> str:
    return _function(SESSIONS_JS, "_setNewSessionPending", "\n\nasync function newSession(")


def _add_files_function() -> str:
    return _function(
        UI_JS, "addFiles", "\nconst _uploadPendingFilesProgressBySession"
    )


def _new_session_function() -> str:
    start = SESSIONS_JS.find("async function newSession(")
    end = SESSIONS_JS.find("\n\n/**", start)
    assert start != -1 and end != -1, "newSession function not found"
    return SESSIONS_JS[start:end]


def _wait_for_new_session_navigation_function() -> str:
    start = SESSIONS_JS.find("let _contextTransitionGeneration=0;")
    end = SESSIONS_JS.find("\nconst _newSessionPendingText", start)
    assert start != -1 and end != -1, "context-transition helper block not found"
    return SESSIONS_JS[start:end]


def _async_function(source: str, name: str, next_marker: str) -> str:
    start = source.find(f"async function {name}(")
    end = source.find(next_marker, start)
    assert start != -1 and end != -1, f"{name} function not found"
    return source[start:end].strip()


def _blank_page_mint_function(name: str) -> str:
    sources = {
        "promptWorkspacePath": (
            PANELS_JS,
            "\n\nasync function switchToWorkspace(",
        ),
        "switchToWorkspace": (
            PANELS_JS,
            "\n\n// ── Profile panel + dropdown",
        ),
        "promptNewFile": (
            UI_JS,
            "\n\nasync function promptNewFolder(",
        ),
        "promptNewFolder": (
            UI_JS,
            "\n\nfunction _syncComposerFiles(",
        ),
    }
    source, marker = sources[name]
    return _async_function(source, name, marker)


def _switch_to_profile_function() -> str:
    return _async_function(
        PANELS_JS,
        "switchToProfile",
        "\n\nfunction openProfileCreate(",
    )


def _load_dir_function() -> str:
    return _async_function(
        WORKSPACE_JS,
        "loadDir",
        "\n\nfunction refreshWorkspacePanel(",
    )


def _load_session_function() -> str:
    start = SESSIONS_JS.find("async function loadSession(")
    end = SESSIONS_JS.find("\n\n// ── Handoff hint logic", start)
    assert start != -1 and end != -1, "loadSession function not found"
    return SESSIONS_JS[start:end]


def _open_sidebar_session_function() -> str:
    start = SESSIONS_JS.find("async function _openSidebarSession(")
    end = SESSIONS_JS.find("\n\nfunction _isReadOnlySession", start)
    assert start != -1 and end != -1, "_openSidebarSession function not found"
    return SESSIONS_JS[start:end]


def _restore_composer_draft_function() -> str:
    return _function(
        SESSIONS_JS, "_restoreComposerDraft", "\n\n// Clear the saved draft"
    )


def _composer_draft_helpers() -> str:
    start = SESSIONS_JS.find("// ── Composer draft persistence")
    end = SESSIONS_JS.find("const SESSION_VIEWED_COUNTS_KEY", start)
    assert start != -1 and end != -1, "composer draft helper block not found"
    return SESSIONS_JS[start:end]


def _composer_authority_helpers() -> str:
    start = UI_JS.index("let _composerOwnershipTransition=null;")
    end = UI_JS.index("const OFFLINE_RECHECK_MS", start)
    return UI_JS[start:end]


def _review_race_production_helpers() -> str:
    names_and_markers = (
        ("_appendComposerText", "\n\nfunction insertSavedPromptIntoComposer"),
        ("insertSavedPromptIntoComposer", "\n\nfunction _seedSelectedTextRefineDraft"),
        ("_restoreComposerDraftAfterFailedSend", "\n\nasync function send("),
        ("_stashClarifyDraft", "\n\nfunction _resetClarifyCardState("),
    )
    return "\n\n".join(
        _function(MESSAGES_JS, name, marker) for name, marker in names_and_markers
    )


def _run_review_race_harness(schedule: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")

    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        {_composer_authority_helpers()}
        {_review_race_production_helpers()}
        {_wait_for_new_session_navigation_function()}
        {_new_session_function()}

        let _sessionSourceFilter = 'webui';
        let _activeProject = null;
        const NO_PROJECT_FILTER = '__none__';
        let _messagesTruncated = false;
        let _oldestIdx = 0;
        let _clarifySessionId = 'old-session';
        let _clarifySignature = 'sig';
        let resolveClear;
        const clearPromise = new Promise(resolve => {{ resolveClear = resolve; }});
        const saves = [];
        const sourceFile = {{name:'source.txt',size:1,type:'text/plain'}};
        const restoredFile = {{name:'restored.txt',size:2,type:'text/plain'}};
        const msg = {{
          value:'source draft', focus(){{}}, setSelectionRange(){{}}, dispatchEvent(){{}}
        }};
        const clarifyInput = {{value:'clarify answer'}};
        const elements = {{
          msg,
          clarifyInput,
          clarifySubmit: {{classList:{{contains(){{return false;}}}}}},
          btnNewChat: {{disabled:false,setAttribute(){{}}}},
          btnTitlebarNewChat: {{disabled:false,setAttribute(){{}}}},
          composerStatus: {{textContent:''}},
          modelSelect: {{value:''}},
        }};
        const $ = id => elements[id] || null;
        const S = {{
          session:{{session_id:'old-session',profile:'default',workspace:'/workspace',message_count:1}},
          messages:[{{role:'user',content:'existing'}}], pendingFiles:[sourceFile],
          toolCalls:[],activeProfile:'default',_profileSwitchWorkspace:null,
          _profileDefaultWorkspace:null,_pendingSessionToolsets:null,busy:false,activeStreamId:null,
        }};
        const window = {{_defaultModel:null}};
        const localStorage = {{setItem(){{}},getItem(){{return null;}},removeItem(){{}}}};
        const sessionStorage = {{setItem(){{}}}};
        const document = {{createElement(){{return {{dataset:{{}}}};}},getElementById:id=>elements[id]||null}};
        class Event {{ constructor(type,opts){{this.type=type;this.opts=opts;}} }}

        function _setNewSessionPending(){{}}
        function _newSessionPendingText(){{return 'Starting';}}
        function showToast(){{}}
        function setComposerStatus(){{}}
        function updateQueueBadge(){{}}
        function clearLiveToolCards(){{}}
        function autoResize(){{}}
        function updateSendBtn(){{}}
        function renderTray(){{}}
        function _rememberComposerPendingFiles(){{}}
        function _saveComposerDraftNow(sid,text,files,profile){{
          saves.push({{sid,text,files:[...(files||[])].map(f=>f.name),profile:profile||null}});
          return Promise.resolve();
        }}
        function _restoreComposerDraft(draft){{
          msg.value=draft&&typeof draft.text==='string'?draft.text:'';
          S.pendingFiles=[];
        }}
        async function api(path){{
          assert.strictEqual(path,'/api/session/new');
          const schedule={json.dumps(schedule)};
          if(schedule==='failed-send'){{
            msg.value='';
            S.pendingFiles=[];
            _restoreComposerDraftAfterFailedSend('failed restore',[restoredFile],'old-session',clearPromise);
          }}else if(schedule==='failed-send-newer-source'){{
            msg.value='';
            S.pendingFiles=[];
            _restoreComposerDraftAfterFailedSend('failed restore',[restoredFile],'old-session',clearPromise);
            _composerAppendText('newer clarify','old-session','clarify-newer','default','block');
          }}else if(schedule==='clarify'||schedule==='clarify-terminal'||schedule==='clarify-abort'){{
            _stashClarifyDraft(schedule==='clarify-terminal'?'terminal':'expired');
            if(schedule==='clarify-abort')throw new Error('create failed');
          }}else if(schedule==='voice-clarify-abort'){{
            _composerSetText('source draft voice','source draft voice',null,'voice-producer');
            _stashClarifyDraft('expired');
            throw new Error('create failed');
          }}else if(schedule==='voice-then-prompt'){{
            _composerSetText('source draft voice','voice');
            insertSavedPromptIntoComposer('saved prompt');
          }}else if(schedule==='prompt-then-voice'){{
            insertSavedPromptIntoComposer('saved prompt');
            _composerSetText('source draft voice','voice');
          }}else if(schedule==='voice-prompt-voice'){{
            _composerSetText('source draft interim','interim',null,'voice-producer');
            insertSavedPromptIntoComposer('saved prompt');
            _composerSetText('source draft final','final',null,'voice-producer');
          }}else if(schedule==='voice-prompt-voice-abort'){{
            _composerSetText('source draft interim','interim',null,'voice-producer');
            insertSavedPromptIntoComposer('saved prompt');
            _composerSetText('source draft final','final',null,'voice-producer');
            throw new Error('create failed');
          }}
          return {{session:{{session_id:'new-session',profile:'default',workspace:'/workspace',
            messages:[],composer_draft:{{text:'',files:[]}},message_count:0}}}};
        }}
        function _hydrateTodosFromSession(){{}}
        function _rememberNewChatDraftSession(){{}}
        function _setActiveSessionUrl(){{}}
        function startSessionStream(){{}}
        function _setSessionViewedCount(){{}}
        function setStatus(){{}}
        function syncTopbar(){{}}
        function renderMessages(){{}}
        function loadDir(){{return Promise.resolve();}}
        function refreshSessionList(){{return Promise.resolve();}}

        (async()=>{{
          let error=null;
          try{{await newSession();}}catch(err){{error=err.message;}}
          if({json.dumps(schedule)}==='failed-send-after-swap'){{
            _restoreComposerDraftAfterFailedSend('failed restore',[restoredFile],'old-session',clearPromise);
          }}
          if({json.dumps(schedule)}==='failed-send'||{json.dumps(schedule)}==='failed-send-after-swap'||{json.dumps(schedule)}==='failed-send-newer-source'){{
            resolveClear();
            await Promise.resolve();await Promise.resolve();await Promise.resolve();
          }}
          if({json.dumps(schedule)}==='late-clarify-after-swap'){{
            _clarifySessionId='old-session';
            _stashClarifyDraft('expired');
            await Promise.resolve();await Promise.resolve();
          }}
          process.stdout.write(JSON.stringify({{
            error,
            activeSid:S.session&&S.session.session_id,
            text:msg.value,
            files:S.pendingFiles.map(f=>f.name),
            saves,
          }}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = subprocess.run(
        [node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def _run_pending_file_ownership_harness() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")

    helper_source = _composer_draft_helpers()
    script = textwrap.dedent(
        f"""
        let _loadingSessionId = null;
        let trayRenders = 0;
        const oldFile = {{
          name:'private.pdf', size:42, type:'application/pdf', lastModified:1, slice(){{}}
        }};
        const msg = {{value:'old draft'}};
        const S = {{
          activeProfile:'default',
          activeProfileIsDefault:true,
          session:{{session_id:'old-session', composer_draft:{{text:'', files:[]}}}},
          pendingFiles:[oldFile],
        }};
        const $ = id => id === 'msg' ? msg : null;
        const localStorage = {{getItem(){{return null;}}, setItem(){{}}, removeItem(){{}}}};
        function api() {{ return Promise.resolve({{}}); }}
        function autoResize() {{}}
        function updateSendBtn() {{}}
        function renderTray() {{ trayRenders += 1; }}
        {helper_source}

        (async () => {{
          await _saveComposerDraftNow('old-session', msg.value, S.pendingFiles);

          S.session = {{session_id:'new-session', composer_draft:{{text:'', files:[]}}}};
          _restoreComposerDraft(S.session.composer_draft);
          const fresh = {{text:msg.value, files:S.pendingFiles.map(file => file.name)}};

          S.session = {{session_id:'old-session', composer_draft:{{
            text:'old draft', files:[{{name:'private.pdf', size:42, type:'application/pdf'}}]
          }}}};
          _restoreComposerDraft(S.session.composer_draft, 'old-session');
          const restored = {{text:msg.value, files:S.pendingFiles.map(file => file.name)}};

          S.activeProfile = 'other-profile';
          S.session = {{session_id:'old-session', composer_draft:{{text:'', files:[]}}}};
          _restoreComposerDraft(S.session.composer_draft, 'old-session');
          const otherProfile = S.pendingFiles.map(file => file.name);

          S.activeProfile = 'visible-profile';
          S.session = {{session_id:'visible-session', profile:'visible-profile'}};
          _rememberComposerPendingFiles('background-session', [oldFile], 'source-profile');
          S.activeProfile = 'source-profile';
          S.session = {{
            session_id:'background-session', profile:'source-profile',
            composer_draft:{{text:'background draft', files:[]}}
          }};
          S.pendingFiles = [];
          _restoreComposerDraft(S.session.composer_draft, 'background-session');
          const backgroundRestored = S.pendingFiles.map(file => file.name);

          S.activeProfile = 'default';
          S.session = {{session_id:'old-session', profile:'default', composer_draft:{{text:'', files:[]}}}};
          _forgetComposerPendingFiles('old-session');
          S.pendingFiles = [];
          _restoreComposerDraft(S.session.composer_draft, 'old-session');
          const afterForget = S.pendingFiles.map(file => file.name);

          process.stdout.write(JSON.stringify({{
            fresh, restored, otherProfile, backgroundRestored, afterForget, trayRenders
          }}));
        }})().catch(err => {{console.error(err); process.exit(1);}});
        """
    )
    proc = subprocess.run(
        [node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _run_new_session_harness(
    *,
    fail_create: bool,
    late_text: str | None = None,
    late_file: bool = False,
    has_session: bool = True,
    fail_save_on_call: int | None = None,
) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")

    function_source = _new_session_function()
    authority_source = _composer_authority_helpers()
    add_files_source = _add_files_function()
    initial_session = json.dumps(
        {"session_id": "old-session", "workspace": "/workspace", "message_count": 2}
        if has_session
        else None
    )
    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        {authority_source}
        {add_files_source}
        {_wait_for_new_session_navigation_function()}
        {function_source}

        let _sessionSourceFilter = 'webui';
        let _activeProject = null;
        const NO_PROJECT_FILTER = '__none__';
        let _messagesTruncated = false;
        let _oldestIdx = 0;
        const saves = [];
        let createCalls = 0;
        let trayRenders = 0;
        let sendButtonUpdates = 0;
        let autoResizeCalls = 0;
        const MAX_UPLOAD_BYTES = 1024;
        const pendingFile = {{ name: 'private.pdf', size: 42, type: 'application/pdf' }};
        const lateFile = {{ name: 'late-audio.webm', size: 7, type: 'audio/webm' }};
        const msg = {{ value: 'draft owned by the old session', focus() {{}} }};
        const elements = {{
          msg,
          btnNewChat: {{ disabled: false, setAttribute() {{}} }},
          btnTitlebarNewChat: {{ disabled: false, setAttribute() {{}} }},
          composerStatus: {{ textContent: '' }},
          modelSelect: {{ value: '' }},
        }};
        const $ = id => elements[id] || null;
        const S = {{
          session: {initial_session},
          messages: [{{ role: 'user', content: 'existing conversation' }}],
          pendingFiles: [pendingFile],
          toolCalls: [],
          activeProfile: 'default',
          _profileSwitchWorkspace: null,
          _profileDefaultWorkspace: null,
          _pendingSessionToolsets: null,
          busy: false,
          activeStreamId: null,
        }};
        let visibleTray = S.pendingFiles.map(file => file.name);
        const window = {{ _defaultModel: null }};
        const localStorage = {{ setItem() {{}}, getItem() {{ return null; }}, removeItem() {{}} }};
        const document = {{ createElement() {{ return {{ dataset: {{}} }}; }} }};

        function _setNewSessionPending() {{}}
        function _newSessionPendingText() {{ return 'Starting'; }}
        function showToast() {{}}
        function setComposerStatus() {{}}
        function updateQueueBadge() {{}}
        function clearLiveToolCards() {{}}
        function _saveComposerDraftNow(sid, text, files, _profile, opts) {{
          saves.push({{ sid, text, files }});
          if(saves.length === {json.dumps(fail_save_on_call)}) {{
            const failed=Promise.reject(new Error('draft save failed'));
            return opts&&opts.rejectOnError?failed:failed.catch(()=>{{}});
          }}
          return Promise.resolve();
        }}
        function _restoreComposerDraft(draft) {{
          const text = draft && typeof draft.text === 'string' ? draft.text : '';
          msg.value = text;
          S.pendingFiles = [];
        }}
        async function api(path) {{
          assert.strictEqual(path, '/api/session/new');
          createCalls += 1;
          const lateText = {json.dumps(late_text)};
          if(lateText !== null) {{
            const destinationText = lateText.replace('draft owned by the old session', '');
            _composerSetText(lateText, destinationText);
          }}
          if({str(late_file).lower()}) addFiles([lateFile]);
          if ({str(fail_create).lower()}) throw new Error('create failed');
          return {{ session: {{
            session_id: 'new-session', profile:'default', workspace: '/workspace', messages: [],
            composer_draft: {{ text: '', files: [] }}, message_count: 0,
          }} }};
        }}
        function _hydrateTodosFromSession() {{}}
        function _rememberNewChatDraftSession() {{}}
        function _setActiveSessionUrl() {{}}
        function startSessionStream() {{}}
        function _setSessionViewedCount() {{}}
        function autoResize() {{ autoResizeCalls += 1; }}
        function renderTray() {{
          trayRenders += 1;
          visibleTray = S.pendingFiles.map(file => file.name);
        }}
        function updateSendBtn() {{ sendButtonUpdates += 1; }}
        function setStatus() {{}}
        function syncTopbar() {{}}
        function renderMessages() {{}}
        function loadDir() {{ return Promise.resolve(); }}
        function refreshSessionList() {{ return Promise.resolve(); }}

        (async () => {{
          let error = null;
          try {{ await newSession(); }} catch (err) {{ error = err.message; }}
          process.stdout.write(JSON.stringify({{
            error,
            value: msg.value,
            activeSid: S.session && S.session.session_id,
            pendingFileNames: S.pendingFiles.map(file => file.name),
            lateFileIdentity: S.pendingFiles.includes(lateFile),
            sourceFileIdentity: S.pendingFiles.includes(pendingFile),
            visibleTray,
            trayRenders,
            sendButtonUpdates,
            autoResizeCalls,
            createCalls,
            saves,
          }}));
        }})().catch(err => {{ console.error(err); process.exit(1); }});
        """
    )
    proc = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _run_blank_page_settlement_harness(entry: str, *, reject_pending: bool) -> dict:
    """Settle the held New Chat, then let the real blank-page caller continue."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")

    calls = {
        "promptWorkspacePath": "promptWorkspacePath()",
        "switchToWorkspace": "switchToWorkspace('/workspace-b', 'Workspace B')",
        "promptNewFile": "promptNewFile('.')",
        "promptNewFolder": "promptNewFolder('.')",
    }
    script = textwrap.dedent(
        f"""
        {_wait_for_new_session_navigation_function()}
        {_blank_page_mint_function(entry)}

        function deferred(){{
          let resolve,reject;
          const promise=new Promise((res,rej)=>{{resolve=res;reject=rej;}});
          return {{promise,resolve,reject}};
        }}
        const pending=deferred();
        const heldIntent=_claimContextTransition('held-new-session');
        _newSessionInFlight=(async()=>{{
          await heldIntent.previous;
          try{{return await pending.promise;}}
          finally{{_newSessionInFlight=null;heldIntent.release();}}
        }})();
        _newSessionInFlight.catch(()=>{{}});
        let newSessionCalls=0;
        const apiCalls=[];
        const S={{
          session:null,messages:[],pendingFiles:[],activeProfile:'default',
          _profileDefaultWorkspace:'/workspace-a',_profileSwitchWorkspace:null,
          _pendingSessionToolsets:['tools'],currentDir:'.',busy:false,_dirCache:{{}},
        }};
        const window={{_newChatOnWorkspaceSwitch:false}};
        function t(key){{return key;}}
        function showToast(){{}}
        function setStatus(){{}}
        function syncTopbar(){{}}
        function renderMessages(){{}}
        function renderSessionList(){{return Promise.resolve();}}
        function showPromptDialog(){{return Promise.resolve(null);}}
        function _workspacePathIsReadOnly(){{return false;}}
        function _workspaceCreateTargetLabel(value){{return value;}}
        function _workspaceJoinTargetPath(dir,name){{return `${{dir}}/${{name}}`;}}
        function closeWsDropdown(){{}}
        function bumpWorkspaceTreeGen(){{}}
        function loadDir(){{return Promise.resolve();}}
        function getWorkspaceFriendlyName(path){{return path;}}
        let _currentPanel='chat';
        async function newSession(){{
          newSessionCalls+=1;
          S.session={{
            session_id:'recovery-session',profile:S.activeProfile,
            workspace:S._profileSwitchWorkspace||S._profileDefaultWorkspace,
            messages:[],composer_draft:{{text:'',files:[]}},message_count:0,
          }};
          S.messages=[];
          S._profileSwitchWorkspace=null;
          return S.session;
        }}
        function api(path,options){{
          apiCalls.push(path);
          if(path==='/api/session/update')return Promise.resolve({{}});
          throw new Error(`unexpected API call: ${{path}}`);
        }}

        (async()=>{{
          const action=Promise.resolve({calls[entry]});
          for(let i=0;i<20;i++)await Promise.resolve();
          const before=[...apiCalls];
          if({str(reject_pending).lower()})pending.reject(new Error('create failed'));
          else{{
            S.session={{
              session_id:'settled-session',profile:'default',workspace:'/workspace-a',
              messages:[],composer_draft:{{text:'',files:[]}},message_count:0,
            }};
            pending.resolve();
          }}
          await action;
          process.stdout.write(JSON.stringify({{
            before,apiCalls,newSessionCalls,
            activeSid:S.session&&S.session.session_id,
            activeProfile:S.session&&S.session.profile,
            workspace:S.session&&S.session.workspace,
            newSessionInFlight:_newSessionInFlight!==null,
            blankMintInFlight:_blankPageSessionInFlight!==null,
          }}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = subprocess.run(
        [node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


@pytest.mark.parametrize(
    "entry",
    ["promptWorkspacePath", "switchToWorkspace", "promptNewFile", "promptNewFolder"],
)
@pytest.mark.parametrize("reject_pending", [False, True])
def test_blank_page_callers_resume_safely_after_new_session_settlement(
    entry, reject_pending
):
    result = _run_blank_page_settlement_harness(
        entry, reject_pending=reject_pending
    )
    assert result["before"] == []
    assert "/api/session/new" not in result["apiCalls"]
    assert result["newSessionCalls"] == (1 if reject_pending else 0)
    assert result["activeSid"] == (
        "recovery-session" if reject_pending else "settled-session"
    )
    assert result["activeProfile"] == "default"
    assert result["newSessionInFlight"] is False
    assert result["blankMintInFlight"] is False


def _run_existing_session_workspace_interleave_harness(*, reject_create: bool) -> dict:
    """Compose real New Chat and workspace switching around held API responses."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")

    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        {_composer_authority_helpers()}
        {_wait_for_new_session_navigation_function()}
        {_new_session_function()}
        {_blank_page_mint_function("switchToWorkspace")}

        function deferred(){{
          let resolve,reject;
          const promise=new Promise((res,rej)=>{{resolve=res;reject=rej;}});
          return {{promise,resolve,reject}};
        }}
        async function spinUntil(predicate){{
          for(let i=0;i<200;i++){{
            if(predicate())return;
            await Promise.resolve();
          }}
          throw new Error('timed out waiting for controlled schedule');
        }}

        let _sessionSourceFilter='webui';
        let _activeProject=null;
        const NO_PROJECT_FILTER='__none__';
        let _messagesTruncated=false;
        let _oldestIdx=0;
        const create=deferred();
        const update=deferred();
        const apiCalls=[];
        const updateBodies=[];
        const backend={{'source-session':'/workspace-a'}};
        const msg={{value:'source draft',focus(){{}}}};
        const elements={{
          msg,
          btnNewChat:{{disabled:false,setAttribute(){{}}}},
          btnTitlebarNewChat:{{disabled:false,setAttribute(){{}}}},
          composerStatus:{{textContent:''}},
          modelSelect:{{value:''}},
        }};
        const $=id=>elements[id]||null;
        const S={{
          session:{{
            session_id:'source-session',profile:'default',workspace:'/workspace-a',
            message_count:1,composer_draft:{{text:'source draft',files:[]}},
          }},
          messages:[{{role:'user',content:'A'}}],pendingFiles:[],toolCalls:[],
          activeProfile:'default',_profileSwitchWorkspace:null,
          _profileDefaultWorkspace:'/workspace-a',_pendingSessionToolsets:null,
          busy:false,activeStreamId:null,_dirCache:{{}},currentDir:'.',
        }};
        const window={{_defaultModel:null,_newChatOnWorkspaceSwitch:false}};
        const localStorage={{setItem(){{}},getItem(){{return null;}},removeItem(){{}}}};
        const document={{createElement(){{return {{dataset:{{}}}};}}}};

        function api(path,options){{
          apiCalls.push(path);
          if(path==='/api/session/new')return create.promise;
          if(path==='/api/session/update'){{
            const body=JSON.parse(options.body);
            updateBodies.push(body);
            return update.promise.then(()=>{{backend[body.session_id]=body.workspace;return {{}};}});
          }}
          throw new Error(`unexpected API call: ${{path}}`);
        }}
        function _setNewSessionPending(){{}}
        function _newSessionPendingText(){{return 'Starting';}}
        function showToast(){{}}
        function setComposerStatus(){{}}
        function updateQueueBadge(){{}}
        function clearLiveToolCards(){{}}
        function _saveComposerDraftNow(){{return Promise.resolve();}}
        function _restoreComposerDraft(draft){{msg.value=(draft&&draft.text)||'';S.pendingFiles=[];}}
        function _hydrateTodosFromSession(){{}}
        function _rememberNewChatDraftSession(){{}}
        function _setActiveSessionUrl(){{}}
        function startSessionStream(){{}}
        function _setSessionViewedCount(){{}}
        function autoResize(){{}}
        function renderTray(){{}}
        function updateSendBtn(){{}}
        function setStatus(){{}}
        function syncTopbar(){{}}
        function renderMessages(){{}}
        function loadDir(){{return Promise.resolve();}}
        function refreshSessionList(){{return Promise.resolve();}}
        function closeWsDropdown(){{}}
        function bumpWorkspaceTreeGen(){{}}
        function t(key){{return key;}}
        function getWorkspaceFriendlyName(path){{return path;}}
        let _currentPanel='chat';

        (async()=>{{
          const creating=newSession(false);
          await spinUntil(()=>apiCalls.includes('/api/session/new'));
          const switching=switchToWorkspace('/workspace-b','Workspace B');
          for(let i=0;i<20;i++)await Promise.resolve();
          const updateStartedBeforeCreateSettled=updateBodies.length>0;

          let creationError=null;
          if({str(reject_create).lower()}){{
            create.reject(new Error('create failed'));
            try{{await creating;}}catch(error){{creationError=error.message;}}
          }}else{{
            backend['new-session']='/workspace-a';
            create.resolve({{session:{{
              session_id:'new-session',profile:'default',workspace:'/workspace-a',
              messages:[],composer_draft:{{text:'',files:[]}},message_count:0,
            }}}});
            await creating;
          }}
          await spinUntil(()=>updateBodies.length===1);
          update.resolve();
          await switching;

          process.stdout.write(JSON.stringify({{
            updateStartedBeforeCreateSettled,
            updateBodies,
            creationError,
            activeSid:S.session&&S.session.session_id,
            clientWorkspace:S.session&&S.session.workspace,
            backend,
            newSessionInFlight:_newSessionInFlight!==null,
          }}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = subprocess.run(
        [node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


@pytest.mark.parametrize("reject_create", [False, True])
def test_existing_session_workspace_waits_for_new_chat_owner_before_update(
    reject_create,
):
    result = _run_existing_session_workspace_interleave_harness(
        reject_create=reject_create
    )

    assert result["updateStartedBeforeCreateSettled"] is False
    expected_sid = "source-session" if reject_create else "new-session"
    assert [body["session_id"] for body in result["updateBodies"]] == [expected_sid]
    assert result["creationError"] == ("create failed" if reject_create else None)
    assert result["activeSid"] == expected_sid
    assert result["clientWorkspace"] == "/workspace-b"
    assert result["backend"][expected_sid] == "/workspace-b"
    other_sid = "new-session" if reject_create else "source-session"
    assert result["backend"].get(other_sid) in (None, "/workspace-a")
    assert result["newSessionInFlight"] is False


def _run_workspace_opt_in_new_chat_harness() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    script = textwrap.dedent(
        f"""
        {_wait_for_new_session_navigation_function()}
        {_new_session_function()}
        {_blank_page_mint_function("switchToWorkspace")}
        let _activeProject=null;const NO_PROJECT_FILTER='__none__';let _sessionSourceFilter='webui';let _messagesTruncated=false;let _oldestIdx=0;
        const apiCalls=[];let newBody=null;
        const S={{session:{{session_id:'source-session',profile:'default',workspace:'/workspace-a',message_count:1}},messages:[{{role:'user',content:'A'}}],pendingFiles:[],toolCalls:[],activeProfile:'default',_profileDefaultWorkspace:'/workspace-a',_profileSwitchWorkspace:null,_pendingSessionToolsets:null,busy:false,activeStreamId:null,currentDir:'.',_dirCache:{{}}}};
        const window={{_defaultModel:null,_newChatOnWorkspaceSwitch:true}};
        const elements={{msg:{{value:'',focus(){{}}}},btnNewChat:{{disabled:false,setAttribute(){{}}}},btnTitlebarNewChat:{{disabled:false,setAttribute(){{}}}},composerStatus:{{textContent:''}},modelSelect:{{value:''}}}};const $=id=>elements[id]||null;
        const localStorage={{setItem(){{}},getItem(){{return null;}},removeItem(){{}}}};const document={{createElement(){{return {{dataset:{{}}}};}}}};
        function api(path,options){{
          apiCalls.push(path);
          if(path!=='/api/session/new')throw new Error(`unexpected ${{path}}`);
          newBody=JSON.parse(options.body);
          return Promise.resolve({{session:{{session_id:'new-session',profile:'default',workspace:newBody.workspace,messages:[],composer_draft:{{text:'',files:[]}},message_count:0}}}});
        }}
        function _setNewSessionPending(){{}} function _newSessionPendingText(){{return 'Starting';}}
        function showToast(){{}} function setComposerStatus(){{}} function updateQueueBadge(){{}}
        function clearLiveToolCards(){{}} function _saveComposerDraftNow(){{return Promise.resolve();}}
        function _restoreComposerDraft(){{S.pendingFiles=[];}} function _hydrateTodosFromSession(){{}}
        function _rememberNewChatDraftSession(){{}} function _setActiveSessionUrl(){{}}
        function startSessionStream(){{}} function _setSessionViewedCount(){{}} function autoResize(){{}}
        function renderTray(){{}} function updateSendBtn(){{}} function setStatus(){{}}
        function syncTopbar(){{}} function renderMessages(){{}} function loadDir(){{return Promise.resolve();}}
        function refreshSessionList(){{return Promise.resolve();}} function closeWsDropdown(){{}}
        function bumpWorkspaceTreeGen(){{}} function t(k){{return k;}} function getWorkspaceFriendlyName(p){{return p;}}
        let _currentPanel='chat';
        (async()=>{{
          await switchToWorkspace('/workspace-b','B');
          await _waitForContextTransitionSettlement();
          process.stdout.write(JSON.stringify({{apiCalls,newBody,activeSid:S.session.session_id,workspace:S.session.workspace,newSessionInFlight:_newSessionInFlight!==null}}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc=subprocess.run([node,"-e",script],cwd=ROOT,text=True,capture_output=True,timeout=30)
    assert proc.returncode==0,proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def test_opt_in_workspace_switch_joins_canonical_new_session_transaction():
    result = _run_workspace_opt_in_new_chat_harness()
    assert result["apiCalls"] == ["/api/session/new"]
    assert result["newBody"]["workspace"] == "/workspace-b"
    assert result["newBody"]["prev_session_id"] == "source-session"
    assert result["activeSid"] == "new-session"
    assert result["workspace"] == "/workspace-b"
    assert result["newSessionInFlight"] is False



def _run_double_workspace_context_harness() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    script = textwrap.dedent(
        f"""
        {_wait_for_new_session_navigation_function()}
        {_blank_page_mint_function("switchToWorkspace")}
        function deferred(){{let resolve;const promise=new Promise(r=>{{resolve=r;}});return {{promise,resolve}};}}
        async function spinUntil(predicate){{for(let i=0;i<200;i++){{if(predicate())return;await Promise.resolve();}}throw new Error('timeout');}}
        const updates=[];const backend={{'source-session':'/workspace-a'}};
        const S={{session:{{session_id:'source-session',profile:'default',workspace:'/workspace-a'}},messages:[],busy:false,currentDir:'.',_dirCache:{{}},_profileSwitchWorkspace:null}};
        const window={{_newChatOnWorkspaceSwitch:false}};
        const elements={{composerWsDropdown:null}};const $=id=>elements[id]||null;
        function api(path,options){{
          if(path!=='/api/session/update')throw new Error(`unexpected ${{path}}`);
          const body=JSON.parse(options.body);const d=deferred();updates.push({{body,d}});
          return d.promise.then(()=>{{backend[body.session_id]=body.workspace;return {{}};}});
        }}
        function closeWsDropdown(){{}} function bumpWorkspaceTreeGen(){{}} function loadDir(){{return Promise.resolve();}}
        function syncTopbar(){{}} function showToast(){{}} function setStatus(){{}} function t(k){{return k;}}
        function getWorkspaceFriendlyName(p){{return p;}} function renderMessages(){{}}
        (async()=>{{
          const first=switchToWorkspace('/workspace-b','B');
          await spinUntil(()=>updates.length===1);
          const second=switchToWorkspace('/workspace-c','C');
          for(let i=0;i<20;i++)await Promise.resolve();
          const beforeFirstSettle=updates.map(x=>x.body.workspace);
          updates[0].d.resolve();
          await spinUntil(()=>updates.length===2);
          const betweenSettles=updates.map(x=>x.body.workspace);
          updates[1].d.resolve();
          await Promise.all([first,second]);
          process.stdout.write(JSON.stringify({{beforeFirstSettle,betweenSettles,finalWorkspace:S.session.workspace,backend,updates:updates.map(x=>x.body)}}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc=subprocess.run([node,"-e",script],cwd=ROOT,text=True,capture_output=True,timeout=30)
    assert proc.returncode==0,proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def test_two_workspace_intents_are_serialized_and_latest_wins():
    result = _run_double_workspace_context_harness()
    assert result["beforeFirstSettle"] == ["/workspace-b"]
    assert result["betweenSettles"] == ["/workspace-b", "/workspace-c"]
    assert result["finalWorkspace"] == "/workspace-c"
    assert [item["workspace"] for item in result["updates"]] == [
        "/workspace-b", "/workspace-c"
    ]
    assert result["backend"]["source-session"] == "/workspace-c"


def _run_profile_double_context_harness() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    script = textwrap.dedent(
        f"""
        {_wait_for_new_session_navigation_function()}
        {_switch_to_profile_function()}
        function deferred(){{let resolve;const promise=new Promise(r=>{{resolve=r;}});return {{promise,resolve}};}}
        async function spinUntil(predicate){{for(let i=0;i<200;i++){{if(predicate())return;await Promise.resolve();}}throw new Error('timeout');}}
        const switches=[];const S={{session:{{session_id:'source-session',profile:'default',workspace:'/workspace-a'}},messages:[],activeProfile:'default',activeProfileIsDefault:true,_pendingSessionToolsets:null}};
        let _profileSwitchGeneration=0;let _profileSwitchOpeningExistingSession=false;let _workspacePanelMode='closed';
        let _renamingSid=null;let _skillsData=null;let _workspaceList=null;let _sessionListSkeletonActive=false;
        const window={{}};const localStorage={{removeItem(){{}}}};
        const elements={{profileChip:{{classList:{{add(){{}},remove(){{}}}},disabled:false}},profileChipLabel:{{textContent:'default'}},titlebarProfileBtn:{{classList:{{add(){{}},remove(){{}}}},disabled:false}},titlebarProfileLabel:{{textContent:'default'}}}};
        const $=id=>elements[id]||null;
        function api(path,options){{
          if(path!=='/api/profile/switch')throw new Error(`unexpected ${{path}}`);
          const name=JSON.parse(options.body).name;const d=deferred();switches.push({{name,d}});
          return d.promise;
        }}
        function closeSessionActionMenu(){{}} function _invalidateSessionListRenders(){{}}
        function _setProfileSwitchListEmbargo(){{}} function showSessionListSkeleton(){{}}
        function bumpWorkspaceTreeGen(){{}} function t(k){{return k;}} function startGatewaySSE(){{}}
        function applyBotName(){{}} function _clearPersistedModelState(){{}}
        function refreshProfileTransitionReasoningChip(){{}} function animateNextSessionListRefresh(){{}}
        function renderSessionList(){{return Promise.resolve();}} function _openProfileSwitchSessionBrowser(){{}}
        function syncTopbar(){{}} function clearWorkspaceTreeSkeleton(){{}}
        function showToast(){{}} function _profileSwitchPanelLoad(){{return Promise.resolve();}}
        function _refreshProfileSwitchBackground(){{}} function renderSessionListFromCache(){{}}
        (async()=>{{
          const first=switchToProfile('beta');
          await spinUntil(()=>switches.length===1);
          const second=switchToProfile('gamma');
          for(let i=0;i<20;i++)await Promise.resolve();
          const beforeFirstSettle=switches.map(x=>x.name);
          switches[0].d.resolve({{active:'beta',is_default:false,default_model:null,default_workspace:null}});
          await spinUntil(()=>switches.length===2);
          const betweenSettles=switches.map(x=>x.name);
          switches[1].d.resolve({{active:'gamma',is_default:false,default_model:null,default_workspace:null}});
          await Promise.all([first,second]);
          process.stdout.write(JSON.stringify({{beforeFirstSettle,betweenSettles,finalProfile:S.activeProfile,switches:switches.map(x=>x.name)}}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc=subprocess.run([node,"-e",script],cwd=ROOT,text=True,capture_output=True,timeout=30)
    assert proc.returncode==0,proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def test_two_profile_intents_are_serialized_and_latest_wins():
    result = _run_profile_double_context_harness()
    assert result["beforeFirstSettle"] == ["beta"]
    assert result["betweenSettles"] == ["beta", "gamma"]
    assert result["finalProfile"] == "gamma"
    assert result["switches"] == ["beta", "gamma"]


def _run_stale_context_repaint_harness(
    entry: str, *, navigate_directory: bool = False, advance_tree_generation: bool = False
) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    calls = {
        "promptWorkspacePath": "promptWorkspacePath()",
        "promptNewFile": "promptNewFile('.')",
        "promptNewFolder": "promptNewFolder('.')",
    }
    prompt_value = "/workspace-added" if entry == "promptWorkspacePath" else "created-item"
    response = (
        "{workspaces:[{path:'/workspace-added',name:'Added'}]}"
        if entry == "promptWorkspacePath"
        else "{}"
    )
    script = textwrap.dedent(
        f"""
        {_wait_for_new_session_navigation_function()}
        {_blank_page_mint_function(entry)}
        function deferred(){{let resolve;const promise=new Promise(r=>{{resolve=r;}});return {{promise,resolve}};}}
        async function spinUntil(predicate){{for(let i=0;i<200;i++){{if(predicate())return;await Promise.resolve();}}throw new Error('timeout');}}
        const held=deferred();let apiCall=null;const toasts=[];const statuses=[];const loads=[];const opens=[];const switches=[];let confirms=0;let _wsTreeGen=0;
        let _workspaceList=[];
        const S={{session:{{session_id:'source-session',profile:'default',workspace:'/workspace-a'}},messages:[],activeProfile:'default',_profileDefaultWorkspace:'/workspace-a',currentDir:'.',_dirCache:{{}},busy:false}};
        function api(path,options){{apiCall={{path,body:options&&options.body?JSON.parse(options.body):{{}}}};return held.promise;}}
        function showPromptDialog(){{return Promise.resolve('{prompt_value}');}}
        function showConfirmDialog(){{confirms+=1;return Promise.resolve(false);}}
        function showToast(...args){{toasts.push(args.join(' '));}} function setStatus(value){{statuses.push(value);}}
        function t(k){{return k;}} function _workspacePathIsReadOnly(){{return false;}}
        function _workspaceCreateTargetLabel(v){{return v;}} function _workspaceJoinTargetPath(d,n){{return d==='.'?n:`${{d}}/${{n}}`;}}
        function loadDir(path){{loads.push(path);return Promise.resolve();}} function openFile(path){{opens.push(path);}}
        function renderWorkspacesPanel(){{}} function getWorkspaceFriendlyName(p){{return p;}}
        async function switchToWorkspace(path,name){{switches.push([path,name]);}}
        (async()=>{{
          const action={calls[entry]};
          await spinUntil(()=>apiCall!==null);
          if({str(navigate_directory).lower()}) S.currentDir='replacement-dir';
          else if({str(advance_tree_generation).lower()}) _wsTreeGen+=1;
          else S.session={{session_id:'replacement-session',profile:'default',workspace:'/workspace-z'}};
          held.resolve({response});
          await action;
          process.stdout.write(JSON.stringify({{apiCall,toasts,statuses,loads,opens,switches,confirms,activeSid:S.session.session_id,workspaceList:_workspaceList}}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc=subprocess.run([node,"-e",script],cwd=ROOT,text=True,capture_output=True,timeout=30)
    assert proc.returncode==0,proc.stderr or proc.stdout
    return json.loads(proc.stdout)


@pytest.mark.parametrize("entry", ["promptWorkspacePath", "promptNewFile", "promptNewFolder"])
def test_stale_context_response_never_repaints_replacement_owner(entry):
    result = _run_stale_context_repaint_harness(entry)
    assert result["activeSid"] == "replacement-session"
    assert result["toasts"] == []
    assert result["statuses"] == []
    assert result["loads"] == []
    assert result["opens"] == []
    assert result["switches"] == []
    assert result["confirms"] == 0
    assert result["workspaceList"] == []
    if entry != "promptWorkspacePath":
        assert result["apiCall"]["body"]["session_id"] == "source-session"


@pytest.mark.parametrize("entry", ["promptNewFile", "promptNewFolder"])
@pytest.mark.parametrize("stale_boundary", ["directory", "tree-generation"])
def test_file_create_completion_never_repaints_newer_workspace_state(
    entry, stale_boundary
):
    result = _run_stale_context_repaint_harness(
        entry,
        navigate_directory=stale_boundary == "directory",
        advance_tree_generation=stale_boundary == "tree-generation",
    )

    assert result["activeSid"] == "source-session"
    assert result["apiCall"]["body"]["session_id"] == "source-session"
    assert result["loads"] == [], "the completed request must not reload the old directory"
    assert result["opens"] == [], "the completed request must not open into the new directory"
    assert result["confirms"] == 0, "a stale folder completion must not ask a follow-up"


def _run_held_directory_reload_harness(entry: str, response_order: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    calls = {
        "promptNewFile": "promptNewFile('.')",
        "promptNewFolder": "promptNewFolder('.')",
    }
    script = textwrap.dedent(
        f"""
        {_wait_for_new_session_navigation_function()}
        {_blank_page_mint_function(entry)}
        {_load_dir_function()}
        function deferred(){{let resolve;const promise=new Promise(r=>{{resolve=r;}});return {{promise,resolve}};}}
        async function spinUntil(predicate){{for(let i=0;i<200;i++){{if(predicate())return;await Promise.resolve();}}throw new Error('timeout');}}
        const listA=deferred();const listB=deferred();const listRequests=[];const projections=[];const opens=[];let confirms=0;let _wsTreeGen=0;let _wsNavigationGen=0;
        const S={{session:{{session_id:'source-session',profile:'default',workspace:'/workspace-a'}},messages:[],activeProfile:'default',_profileDefaultWorkspace:'/workspace-a',currentDir:'A',entries:[],_dirCache:{{}},_expandedDirs:new Set(),busy:false}};
        function api(path,options){{
          if(path==='/api/file/create'||path==='/api/file/create-dir')return Promise.resolve({{}});
          const requested=new URL(path,'http://localhost').searchParams.get('path');
          listRequests.push(requested);
          if(requested==='A')return listA.promise;
          if(requested==='B')return listB.promise;
          throw new Error(`unexpected API path: ${{path}}`);
        }}
        function showPromptDialog(){{return Promise.resolve('created-item');}}
        function showConfirmDialog(){{confirms+=1;return Promise.resolve(false);}}
        function showToast(){{}} function setStatus(value){{throw new Error(value);}}
        function t(k){{return k;}} function _workspacePathIsReadOnly(){{return false;}}
        function _workspaceCreateTargetLabel(v){{return v;}} function _workspaceJoinTargetPath(d,n){{return d==='.'?n:`${{d}}/${{n}}`;}}
        function _workspaceRouteForPath(){{return null;}} function _restoreExpandedDirs(){{}}
        function renderBreadcrumb(){{projections.push(`breadcrumb:${{S.currentDir}}:${{S.entries.map(x=>x.name).join(',')}}`);}}
        function renderFileTree(){{projections.push(`tree:${{S.currentDir}}:${{S.entries.map(x=>x.name).join(',')}}`);}}
        function renderSessionArtifacts(){{projections.push(`artifacts:${{S.currentDir}}:${{S.entries.map(x=>x.name).join(',')}}`);}}
        function clearPreview(){{projections.push(`preview:${{S.currentDir}}`);}}
        function refreshOpenPreviewIfMutated(){{return Promise.resolve();}} function _refreshGitBadge(){{}}
        function _workspaceEscapeGrantForPath(){{return null;}} function openFile(path){{opens.push(path);}}
        function renderWorkspacesPanel(){{}} let _workspaceList=[];
        (async()=>{{
          const action={calls[entry]};
          await spinUntil(()=>listRequests.includes('A'));
          if('{response_order}'==='current'){{
            listA.resolve({{entries:[{{name:'from-A'}}]}});
            await action;
            process.stdout.write(JSON.stringify({{currentDir:S.currentDir,entries:S.entries.map(x=>x.name),projections,opens,confirms}}));
            return;
          }}
          const navigationB=loadDir('B');
          await spinUntil(()=>S.currentDir==='B');
          if('{response_order}'==='A-then-B'){{
            listA.resolve({{entries:[{{name:'from-A'}}]}});
            await Promise.resolve();await Promise.resolve();
            listB.resolve({{entries:[{{name:'from-B'}}]}});
          }}else{{
            listB.resolve({{entries:[{{name:'from-B'}}]}});
            await navigationB;
            listA.resolve({{entries:[{{name:'from-A'}}]}});
          }}
          await Promise.all([action,navigationB]);
          process.stdout.write(JSON.stringify({{currentDir:S.currentDir,entries:S.entries.map(x=>x.name),projections,opens,confirms}}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = subprocess.run(
        [node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


@pytest.mark.parametrize("entry", ["promptNewFile", "promptNewFolder"])
@pytest.mark.parametrize("response_order", ["A-then-B", "B-then-A"])
def test_held_create_reload_cannot_overwrite_newer_directory(entry, response_order):
    result = _run_held_directory_reload_harness(entry, response_order)
    assert result["currentDir"] == "B"
    assert result["entries"] == ["from-B"]
    assert not any("from-A" in projection for projection in result["projections"])
    assert result["opens"] == []
    assert result["confirms"] == 0


@pytest.mark.parametrize(
    ("entry", "expected_opens", "expected_confirms"),
    [
        ("promptNewFile", ["created-item"], 0),
        ("promptNewFolder", [], 1),
    ],
)
def test_current_create_reload_commits_and_runs_follow_up(
    entry, expected_opens, expected_confirms
):
    result = _run_held_directory_reload_harness(entry, "current")
    assert result["currentDir"] == "A"
    assert result["entries"] == ["from-A"]
    assert any("from-A" in projection for projection in result["projections"])
    assert result["opens"] == expected_opens
    assert result["confirms"] == expected_confirms


def test_profile_owned_new_chat_cannot_deadlock_with_a_queued_new_chat():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")

    script = textwrap.dedent(
        f"""
        {_wait_for_new_session_navigation_function()}
        {_new_session_function()}
        {_switch_to_profile_function()}

        function deferred(){{
          let resolve;
          const promise=new Promise(res=>{{resolve=res;}});
          return {{promise,resolve}};
        }}
        async function spinUntil(predicate){{
          for(let i=0;i<200;i++){{if(predicate())return;await Promise.resolve();}}
          throw new Error('timed out waiting for controlled schedule');
        }}

        let _sessionSourceFilter='webui';
        let _activeProject=null;
        const NO_PROJECT_FILTER='__none__';
        let _messagesTruncated=false;
        let _oldestIdx=0;
        let _profileSwitchGeneration=0;
        let _profileSwitchOpeningExistingSession=false;
        let _workspacePanelMode='closed';
        let _renamingSid=null;
        let _skillsData=null;
        let _workspaceList=null;
        let _sessionListSkeletonActive=false;
        const profileSwitch=deferred();
        const apiCalls=[];
        let createCalls=0;
        const msg={{value:'source draft',focus(){{}}}};
        const elements={{
          msg,
          btnNewChat:{{disabled:false,setAttribute(){{}}}},
          btnTitlebarNewChat:{{disabled:false,setAttribute(){{}}}},
          composerStatus:{{textContent:''}},
          modelSelect:{{value:''}},
          profileChip:{{classList:{{add(){{}},remove(){{}}}},disabled:false}},
          profileChipLabel:{{textContent:'default'}},
          titlebarProfileBtn:{{classList:{{add(){{}},remove(){{}}}},disabled:false}},
          titlebarProfileLabel:{{textContent:'default'}},
        }};
        const $=id=>elements[id]||null;
        const S={{
          session:{{
            session_id:'source-session',profile:'default',workspace:'/workspace-a',
            message_count:1,composer_draft:{{text:'source draft',files:[]}},
          }},
          messages:[{{role:'user',content:'A'}}],pendingFiles:[],toolCalls:[],
          activeProfile:'default',activeProfileIsDefault:true,
          _profileSwitchWorkspace:null,_profileDefaultWorkspace:'/workspace-a',
          _pendingSessionToolsets:null,busy:false,activeStreamId:null,
          _dirCache:{{}},currentDir:'.',
        }};
        const window={{_defaultModel:null}};
        const localStorage={{setItem(){{}},getItem(){{return null;}},removeItem(){{}}}};
        const document={{createElement(){{return {{dataset:{{}}}};}}}};

        function api(path,options){{
          apiCalls.push(path);
          if(path==='/api/profile/switch')return profileSwitch.promise;
          if(path==='/api/session/new'){{
            createCalls+=1;
            const body=JSON.parse(options.body);
            return Promise.resolve({{session:{{
              session_id:'new-session',profile:body.profile,workspace:body.workspace,
              messages:[],composer_draft:{{text:'',files:[]}},message_count:0,
            }}}});
          }}
          throw new Error(`unexpected API call: ${{path}}`);
        }}
        function _setNewSessionPending(){{}}
        function _newSessionPendingText(){{return 'Starting';}}
        function showToast(){{}}
        function setComposerStatus(){{}}
        function updateQueueBadge(){{}}
        function clearLiveToolCards(){{}}
        function _saveComposerDraftNow(){{return Promise.resolve();}}
        function _restoreComposerDraft(draft){{msg.value=(draft&&draft.text)||'';S.pendingFiles=[];}}
        function _hydrateTodosFromSession(){{}}
        function _rememberNewChatDraftSession(){{}}
        function _setActiveSessionUrl(){{}}
        function startSessionStream(){{}}
        function _setSessionViewedCount(){{}}
        function autoResize(){{}}
        function renderTray(){{}}
        function updateSendBtn(){{}}
        function setStatus(){{}}
        function syncTopbar(){{}}
        function renderMessages(){{}}
        function loadDir(){{return Promise.resolve();}}
        function refreshSessionList(){{return Promise.resolve();}}
        function closeSessionActionMenu(){{}}
        function _invalidateSessionListRenders(){{}}
        function _setProfileSwitchListEmbargo(){{}}
        function showSessionListSkeleton(){{}}
        function bumpWorkspaceTreeGen(){{}}
        function t(key){{return key;}}
        function startGatewaySSE(){{}}
        function applyBotName(){{}}
        function _clearPersistedModelState(){{}}
        function refreshProfileTransitionReasoningChip(){{}}
        function animateNextSessionListRefresh(){{}}
        function renderSessionList(){{return Promise.resolve();}}
        function _openProfileSwitchSessionBrowser(){{}}
        function clearWorkspaceTreeSkeleton(){{}}
        function _profileSwitchPanelLoad(){{return Promise.resolve();}}
        function _refreshProfileSwitchBackground(){{}}
        function renderSessionListFromCache(){{}}

        (async()=>{{
          const switching=switchToProfile('beta');
          await spinUntil(()=>apiCalls.includes('/api/profile/switch'));
          const queuedNewChat=newSession(false);
          for(let i=0;i<20;i++)await Promise.resolve();
          const createStartedBeforeProfileSettled=createCalls>0;
          profileSwitch.resolve({{
            active:'beta',is_default:false,default_model:null,default_workspace:null,
          }});
          const completed=await Promise.race([
            Promise.all([switching,queuedNewChat]).then(()=>true),
            new Promise(resolve=>setTimeout(()=>resolve(false),100)),
          ]);
          process.stdout.write(JSON.stringify({{
            completed,createStartedBeforeProfileSettled,createCalls,
            activeSid:S.session&&S.session.session_id,
            activeProfile:S.activeProfile,
          }}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = subprocess.run(
        [node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    assert result["createStartedBeforeProfileSettled"] is False
    assert result["completed"] is True, "queued New Chat and its parent intent deadlocked"
    assert result["createCalls"] == 1
    assert result["activeSid"] == "new-session"
    assert result["activeProfile"] == "beta"


def _run_profile_switch_settlement_harness(*, reject_pending: bool) -> dict:
    """Run real switchToProfile through success/failure settlement to completion."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")

    script = textwrap.dedent(
        f"""
        {_wait_for_new_session_navigation_function()}
        {_switch_to_profile_function()}

        function deferred(){{
          let resolve,reject;
          const promise=new Promise((res,rej)=>{{resolve=res;reject=rej;}});
          return {{promise,resolve,reject}};
        }}
        const pending=deferred();
        const heldIntent=_claimContextTransition('held-new-session');
        _newSessionInFlight=(async()=>{{
          await heldIntent.previous;
          try{{return await pending.promise;}}
          finally{{_newSessionInFlight=null;heldIntent.release();}}
        }})();
        _newSessionInFlight.catch(()=>{{}});
        let _profileSwitchGeneration=0;
        let _profileSwitchOpeningExistingSession=false;
        let _workspacePanelMode='closed';
        let _renamingSid=null;
        let _skillsData=null;
        let _workspaceList=null;
        let _sessionListSkeletonActive=false;
        const apiCalls=[];
        const toasts=[];
        let newSessionCalls=0;
        const S={{
          session:{{session_id:'source-session',profile:'default',workspace:'/workspace-a'}},
          messages:[{{role:'user',content:'A'}}],activeProfile:'default',
          activeProfileIsDefault:true,_pendingSessionToolsets:['tools'],
        }};
        const window={{}};
        const localStorage={{removeItem(){{}}}};
        const elements={{
          profileChip:{{classList:{{add(){{}},remove(){{}}}},disabled:false}},
          profileChipLabel:{{textContent:'default'}},
          titlebarProfileBtn:{{classList:{{add(){{}},remove(){{}}}},disabled:false}},
          titlebarProfileLabel:{{textContent:'default'}},
        }};
        const $=id=>elements[id]||null;
        function closeSessionActionMenu(){{}}
        function _invalidateSessionListRenders(){{}}
        function _setProfileSwitchListEmbargo(){{}}
        function showSessionListSkeleton(){{}}
        function bumpWorkspaceTreeGen(){{}}
        function t(key){{return key;}}
        function startGatewaySSE(){{}}
        function applyBotName(){{}}
        function _clearPersistedModelState(){{}}
        function refreshProfileTransitionReasoningChip(){{}}
        function animateNextSessionListRefresh(){{}}
        function renderSessionList(){{return Promise.resolve();}}
        function _openProfileSwitchSessionBrowser(){{}}
        function syncTopbar(){{}}
        function clearWorkspaceTreeSkeleton(){{}}
        function showToast(...args){{toasts.push(args.map(String).join(' '));}}
        function _profileSwitchPanelLoad(){{return Promise.resolve();}}
        function _refreshProfileSwitchBackground(){{}}
        function renderSessionListFromCache(){{}}
        async function newSession(){{
          newSessionCalls+=1;
          S.session={{
            session_id:'profile-session',profile:S.activeProfile,
            workspace:'/workspace-a',messages:[],message_count:0,
          }};
          S.messages=[];
          return S.session;
        }}
        function api(path){{
          apiCalls.push(path);
          if(path==='/api/profile/switch')return Promise.resolve({{
            active:'beta',is_default:false,default_model:null,default_workspace:null,
          }});
          throw new Error(`unexpected API call: ${{path}}`);
        }}

        (async()=>{{
          const switching=switchToProfile('beta');
          for(let i=0;i<20;i++)await Promise.resolve();
          const before={{
            apiCalls:[...apiCalls],generation:_profileSwitchGeneration,
            pending:S._pendingSessionToolsets,
          }};
          if({str(reject_pending).lower()})pending.reject(new Error('create failed'));
          else{{
            S.session={{
              session_id:'settled-session',profile:'default',workspace:'/workspace-a',
              messages:[],message_count:0,
            }};
            S.messages=[];
            pending.resolve();
          }}
          const switched=await switching;
          process.stdout.write(JSON.stringify({{
            before,switched,apiCalls,generation:_profileSwitchGeneration,
            activeProfile:S.activeProfile,sessionProfile:S.session&&S.session.profile,
            activeSid:S.session&&S.session.session_id,
            newSessionInFlight:_newSessionInFlight!==null,toasts,newSessionCalls,
          }}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = subprocess.run(
        [node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


@pytest.mark.parametrize("reject_pending", [False, True])
def test_profile_switch_resumes_with_consistent_profile_session_ownership(
    reject_pending,
):
    result = _run_profile_switch_settlement_harness(
        reject_pending=reject_pending
    )
    assert result["before"] == {
        "apiCalls": [], "generation": 0, "pending": ["tools"]
    }
    assert result["switched"] is True
    assert result["apiCalls"] == ["/api/profile/switch"]
    assert result["generation"] == 1
    assert result["activeProfile"] == "beta"
    assert result["sessionProfile"] == "beta"
    assert result["activeSid"] == "profile-session"
    assert result["newSessionCalls"] == 1
    assert result["toasts"] == ["profile_switched_new_conversation"]
    assert result["newSessionInFlight"] is False


def _run_new_session_load_interleave_harness(
    *, fail_create: bool, clarify_block: bool = False
) -> dict:
    """Run production newSession/loadSession with a controllable create promise."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")

    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        {_composer_authority_helpers()}
        {_set_new_session_pending_function()}
        {_restore_composer_draft_function()}
        {_wait_for_new_session_navigation_function()}
        {_new_session_function()}
        {_load_session_function()}
        {_open_sidebar_session_function()}

        let _loadingSessionId = null;
        let _loadSessionGeneration = 0;
        let _sessionSourceFilter = 'webui';
        let _activeProject = null;
        const NO_PROJECT_FILTER = '__none__';
        let _messagesTruncated = false;
        let _oldestIdx = 0;
        let _loadingOlder = false;
        let _pendingCarryForwardSnapshot = null;
        let _messageUserUnpinned = false;
        let _scrollPinned = true;
        const INFLIGHT = {{}};

        function deferred() {{
          let resolve;
          let reject;
          const promise = new Promise((res, rej) => {{ resolve = res; reject = rej; }});
          return {{promise, resolve, reject}};
        }}
        async function spinUntil(predicate) {{
          for(let i=0;i<200;i++){{
            if(predicate()) return;
            await Promise.resolve();
          }}
          throw new Error('timed out waiting for controlled schedule');
        }}

        const create = deferred();
        const metadata = deferred();
        const messages = deferred();
        const apiCalls = [];
        const saves = [];
        const focusEvents = [];
        const sidebarProfileCalls = [];
        const sourceFile = {{name:'source-a.txt',size:1,type:'text/plain'}};
        const controls = {{}};
        const msg = controls.msg = {{
          value:'draft A', disabled:false,
          focus(){{
            focusEvents.push({{
              sid:S.session&&S.session.session_id,
              disabled:this.disabled===true,
              reasons:this._composerDisabledReasons
                ? [...this._composerDisabledReasons].sort()
                : [],
            }});
          }},
          setSelectionRange(){{}}, dispatchEvent(){{}},
        }};
        for(const id of [
          'fileInput','btnAttach','btnSavedPrompts','btnMic','btnVoiceMode',
          'btnNewChat','btnTitlebarNewChat'
        ]) controls[id]={{disabled:false,setAttribute(){{}}}};
        controls.composerStatus={{textContent:''}};
        controls.modelSelect={{value:''}};
        controls.msgInner={{innerHTML:''}};
        const $ = id => controls[id] || null;
        const document = {{
          getElementById:id=>controls[id]||null,
          createElement(){{return {{dataset:{{}},appendChild(){{}}}};}},
          querySelectorAll(){{return [];}}
        }};
        const S = {{
          session:{{
            session_id:'session-a',profile:'default',workspace:'/workspace-a',
            message_count:1,composer_draft:{{text:'draft A',files:[]}}
          }},
          messages:[{{role:'user',content:'A'}}],pendingFiles:[sourceFile],toolCalls:[],
          activeProfile:'default',activeProfileIsDefault:true,
          _profileSwitchWorkspace:null,_profileDefaultWorkspace:null,
          _pendingSessionToolsets:null,busy:false,activeStreamId:null,lastUsage:{{}},
        }};
        const window={{_defaultModel:null}};
        const localStorage={{setItem(){{}},getItem(){{return null;}},removeItem(){{}}}};
        const history={{replaceState(){{}}}};

        function api(path) {{
          apiCalls.push(String(path));
          if(path==='/api/session/new') return create.promise;
          if(path==='/api/session?session_id=session-b&messages=0&resolve_model=0'){{
            return metadata.promise;
          }}
          if(path==='/controlled/messages/session-b') return messages.promise;
          throw new Error(`unexpected API call: ${{path}}`);
        }}
        function _saveComposerDraftNow(sid,text,files,profile){{
          saves.push({{sid,text,files:[...(files||[])].map(file=>file.name),profile:profile||null}});
          return Promise.resolve();
        }}
        async function _ensureMessagesLoaded(sid){{
          const data=await api(`/controlled/messages/${{sid}}`);
          S.messages=data.session.messages||[];
          S.toolCalls=data.session.tool_calls||[];
        }}
        function _composerDraftHasPayload(text,files){{return !!(text||(files&&files.length));}}
        function _isComposerDraftRestoreSuppressed(){{return false;}}
        function _clearComposerDraftRestoreSuppression(){{}}
        function _restoreComposerPendingFiles(){{S.pendingFiles=[];}}
        function _newSessionPendingText(){{return 'Starting';}}
        function setComposerStatus(text){{controls.composerStatus.textContent=text;}}
        function showToast(){{}}
        function updateQueueBadge(){{}}
        function clearLiveToolCards(){{}}
        function autoResize(){{}}
        function renderTray(){{}}
        function updateSendBtn(){{}}
        function _rememberNewChatDraftSession(){{}}
        function _setActiveSessionUrl(){{}}
        function startSessionStream(){{}}
        function _setSessionViewedCount(){{}}
        function setStatus(){{}}
        function syncTopbar(){{}}
        function renderMessages(){{}}
        function loadDir(){{return Promise.resolve();}}
        function refreshSessionList(){{return Promise.resolve();}}
        function _rearmActiveSessionStream(){{}}
        function stopApprovalPolling(){{}}
        function hideApprovalCard(){{}}
        function stopSessionStream(){{}}
        let _yoloEnabled=false;
        function _updateYoloPill(){{}}
        function stopClarifyPolling(){{}}
        function hideClarifyCard(){{}}
        function _clearSameSessionForceReloadHint(){{}}
        function closeOtherLiveStreams(){{}}
        function _clearEmptyComposerModelOverride(){{}}
        function _hydrateTodosFromSession(){{}}
        function _resolveSessionModelForDisplaySoon(){{}}
        function _applyPendingSessionModelForSession(){{}}
        function _acknowledgeSessionVisit(){{}}
        function _serverLiveSnapshotInflight(){{return null;}}
        function _selectLiveRecoveryInflight(){{return null;}}
        function _mergePendingSessionMessage(){{return false;}}
        function startApprovalPolling(){{}}
        function _deferWorkspaceRefreshForSession(){{}}
        function _renderPendingPromptsForActiveSession(){{}}
        function renderSessionArtifacts(){{}}
        function _isMessagingSession(){{return false;}}
        function _hideHandoffHint(){{}}
        function _sessionVisitHasUnreadState(){{return false;}}
        function _clearDeferredActiveSessionExternalRefresh(){{}}
        function _isExternalSession(){{return false;}}
        function _ensureSidebarSessionProfile(session){{
          sidebarProfileCalls.push(session&&session.session_id);
          return Promise.resolve(false);
        }}
        function renderSessionListFromCache(){{}}

        const createdSession={{
          session_id:'new-session',profile:'default',workspace:'/workspace-new',
          messages:[],composer_draft:{{text:'',files:[]}},message_count:0,
        }};
        const sessionB={{
          session_id:'session-b',profile:'default',workspace:'/workspace-b',
          messages:[],composer_draft:{{text:'draft B',files:[]}},message_count:1,
          active_stream_id:null,
        }};

        (async()=>{{
          let newError=null;
          const creating=newSession().catch(error=>{{newError=error.message;}});
          await spinUntil(()=>apiCalls.includes('/api/session/new'));
          const loading=_openSidebarSession(sessionB);
          for(let i=0;i<12;i++) await Promise.resolve();
          const loadStartedBeforeCreateSettled=apiCalls.includes(
            '/api/session?session_id=session-b&messages=0&resolve_model=0'
          );
          const sidebarProfileStartedBeforeCreateSettled=sidebarProfileCalls.length>0;

          if(loadStartedBeforeCreateSettled){{
            metadata.resolve({{session:sessionB}});
            await spinUntil(()=>apiCalls.includes('/controlled/messages/session-b'));
            messages.resolve({{session:{{...sessionB,messages:[{{role:'assistant',content:'B'}}]}}}});
            await loading;
          }}

          if({str(clarify_block).lower()}){{
            _composerControlSetDisabledReason(msg,'clarify',true);
          }}
          if({str(fail_create).lower()}) create.reject(new Error('create failed'));
          else create.resolve({{session:createdSession}});
          await creating;

          if(!loadStartedBeforeCreateSettled){{
            await spinUntil(()=>apiCalls.includes(
              '/api/session?session_id=session-b&messages=0&resolve_model=0'
            ));
            metadata.resolve({{session:sessionB}});
            await spinUntil(()=>apiCalls.includes('/controlled/messages/session-b'));
            messages.resolve({{session:{{...sessionB,messages:[{{role:'assistant',content:'B'}}]}}}});
            await loading;
          }}

          process.stdout.write(JSON.stringify({{
            newError,loadStartedBeforeCreateSettled,sidebarProfileStartedBeforeCreateSettled,
            activeSid:S.session&&S.session.session_id,
            text:msg.value,files:S.pendingFiles.map(file=>file.name),
            disabled:msg.disabled===true,
            reasons:msg._composerDisabledReasons?[...msg._composerDisabledReasons].sort():[],
            focusEvents,apiCalls,saves,
          }}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = subprocess.run(
        [node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


@pytest.mark.parametrize("fail_create", [False, True])
def test_sidebar_navigation_waits_for_new_session_settlement(fail_create):
    result = _run_new_session_load_interleave_harness(fail_create=fail_create)

    assert result["loadStartedBeforeCreateSettled"] is False, (
        "loadSession must not start switching composer ownership while New Chat owns it"
    )
    assert result["sidebarProfileStartedBeforeCreateSettled"] is False, (
        "the sidebar wrapper must wait before profile/import navigation side effects"
    )
    assert result["activeSid"] == "session-b"
    assert result["text"] == "draft B"
    assert result["files"] == []
    assert result["newError"] == ("create failed" if fail_create else None)


def test_failed_create_focuses_restored_owner_after_new_session_reason_is_released():
    result = _run_new_session_load_interleave_harness(fail_create=True)

    assert result["focusEvents"] == [
        {"sid": "session-a", "disabled": False, "reasons": []}
    ]


def test_failed_create_does_not_focus_when_clarify_reason_still_disables_composer():
    result = _run_new_session_load_interleave_harness(
        fail_create=True, clarify_block=True
    )

    assert result["focusEvents"] == []
    assert result["disabled"] is True
    assert result["reasons"] == ["clarify"]


def test_pending_files_follow_their_session_owner_across_new_session_boundary():
    result = _run_pending_file_ownership_harness()

    assert result["fresh"] == {"text": "", "files": []}
    assert result["restored"] == {
        "text": "old draft",
        "files": ["private.pdf"],
    }
    assert result["otherProfile"] == []
    assert result["backgroundRestored"] == ["private.pdf"]
    assert result["afterForget"] == []
    assert result["trayRenders"] >= 3


def test_successful_new_session_saves_old_draft_and_starts_with_blank_composer():
    result = _run_new_session_harness(fail_create=False)

    assert result["error"] is None
    assert result["activeSid"] == "new-session"
    assert result["saves"] == [
        {
            "sid": "old-session",
            "text": "draft owned by the old session",
            "files": [
                {"name": "private.pdf", "size": 42, "type": "application/pdf"}
            ],
        }
    ]
    assert result["value"] == "", (
        "a newly created session must not inherit the previous session's unsent prompt"
    )
    assert result["pendingFileNames"] == []


def test_source_draft_rejection_aborts_before_session_creation():
    result = _run_new_session_harness(
        fail_create=False,
        fail_save_on_call=1,
    )

    assert result["error"] == "draft save failed"
    assert result["createCalls"] == 0
    assert result["activeSid"] == "old-session"
    assert result["value"] == "draft owned by the old session"
    assert result["pendingFileNames"] == ["private.pdf"]


def test_destination_draft_failure_does_not_orphan_created_session_or_drop_input():
    source_text = "draft owned by the old session"
    result = _run_new_session_harness(
        fail_create=False,
        late_text=f"{source_text} voice addition",
        late_file=True,
        fail_save_on_call=2,
    )

    assert result["error"] is None
    assert result["createCalls"] == 1
    assert result["activeSid"] == "new-session"
    assert result["value"] == " voice addition"
    assert result["pendingFileNames"] == ["late-audio.webm"]
    assert result["lateFileIdentity"] is True
    assert [save["sid"] for save in result["saves"]] == [
        "old-session",
        "new-session",
    ]


def test_first_send_without_previous_session_transfers_entire_composer():
    result = _run_new_session_harness(fail_create=False, has_session=False)

    assert result["activeSid"] == "new-session"
    assert result["value"] == "draft owned by the old session"
    assert result["pendingFileNames"] == ["private.pdf"]
    assert result["sourceFileIdentity"] is True
    assert result["saves"] == [
        {
            "sid": "new-session",
            "text": "draft owned by the old session",
            "files": [
                {"name": "private.pdf", "size": 42, "type": "application/pdf"}
            ],
        }
    ]


def test_programmatic_input_arriving_in_flight_moves_only_the_delta_to_new_session():
    source_text = "draft owned by the old session"
    result = _run_new_session_harness(
        fail_create=False,
        late_text=f"{source_text} voice addition",
        late_file=True,
    )

    assert result["activeSid"] == "new-session"
    assert result["value"] == " voice addition"
    assert source_text not in result["value"]
    assert result["pendingFileNames"] == ["late-audio.webm"]
    assert result["lateFileIdentity"] is True
    assert result["saves"][0]["sid"] == "old-session"
    assert result["saves"][0]["text"] == source_text
    assert result["saves"][1] == {
        "sid": "new-session",
        "text": " voice addition",
        "files": [
            {"name": "late-audio.webm", "size": 7, "type": "audio/webm"}
        ],
    }


def test_new_session_pending_freezes_and_restores_composer_controls():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    assert node is not None

    source = _set_new_session_pending_function()
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        {authority}
        {source}
        const ids = [
          'btnNewChat','btnTitlebarNewChat','msg','fileInput','btnAttach',
          'btnSavedPrompts','btnMic','btnVoiceMode'
        ];
        const elements = Object.fromEntries(ids.map(id => [id, {{
          disabled: id === 'btnMic', setAttribute() {{}}
        }}]));
        elements.composerStatus = {{textContent:''}};
        const $ = id => elements[id] || null;
        function _newSessionPendingText() {{ return 'Starting'; }}
        function setComposerStatus(text) {{ elements.composerStatus.textContent = text; }}

        _setNewSessionPending(true);
        const during = Object.fromEntries(ids.map(id => [id, elements[id].disabled]));
        _setNewSessionPending(false);
        const after = Object.fromEntries(ids.map(id => [id, elements[id].disabled]));
        process.stdout.write(JSON.stringify({{during, after}}));
        """
    )
    proc = subprocess.run(
        [node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert all(result["during"].values())
    assert result["after"]["msg"] is False
    assert result["after"]["fileInput"] is False
    assert result["after"]["btnAttach"] is False
    assert result["after"]["btnMic"] is True, "pre-existing disabled state must survive"


def test_files_dropped_while_new_session_is_pending_are_replayed_after_settlement():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    assert node is not None

    source = _add_files_function()
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const S = {{pendingFiles:[]}};
        const MAX_UPLOAD_BYTES = 1024;
        let trayRenders = 0;
        const elements = {{msg:{{value:''}}}};
        const $ = id => elements[id] || null;
        function renderTray() {{ trayRenders += 1; }}
        function _showUploadTooLarge() {{ throw new Error('unexpected large file'); }}
        {authority}
        {source}
        const file = {{name:'late.txt', size:4}};

        const token=_beginComposerOwnershipTransition('old-session','default');
        addFiles([file]);
        const immediate = S.pendingFiles.map(item => item.name);
        _drainComposerOwnershipTransition(token);
        renderTray();
        process.stdout.write(JSON.stringify({{
          immediate,
          after: S.pendingFiles.map(item => item.name),
          trayRenders,
        }}));
        """
    )
    proc = subprocess.run(
        [node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["immediate"] == []
    assert result["after"] == ["late.txt"]
    assert result["trayRenders"] == 1


def test_hidden_owner_callback_cannot_cancel_visible_owner_pending_draft_save():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")

    draft_helpers = _composer_draft_helpers()
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const msg = {{value:'foreground-unsaved'}};
        const S = {{
          session:{{
            session_id:'owner-b',profile:'beta',
            composer_draft:{{text:'baseline-b',files:[]}}
          }},
          activeProfile:'beta',activeProfileIsDefault:false,pendingFiles:[]
        }};
        const $ = id => id === 'msg' ? msg : null;
        const localStorage = {{getItem(){{return null;}},setItem(){{}},removeItem(){{}}}};
        const posts = [];
        function api(path, options) {{
          if(path !== '/api/session/draft') throw new Error(`unexpected path: ${{path}}`);
          posts.push(JSON.parse(options.body));
          return Promise.resolve({{ok:true}});
        }}
        function renderTray() {{}}
        function autoResize() {{}}
        function updateSendBtn() {{}}
        {draft_helpers}
        {authority}

        _rememberComposerOwnerState('owner-a','alpha',{{
          text:'baseline-a',files:[],revision:1,
        }},1);
        _saveComposerDraft('owner-b','foreground-unsaved',[],'beta');
        _composerSetText('background-a','background-a','owner-a','late-a','alpha');

        setTimeout(() => {{
          process.stdout.write(JSON.stringify(posts));
        }}, _DRAFT_SAVE_DELAY_MS + 100);
        """
    )
    proc = subprocess.run(
        [node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    posts = json.loads(proc.stdout)
    assert [(post["session_id"], post["text"]) for post in posts] == [
        ("owner-a", "background-a"),
        ("owner-b", "foreground-unsaved"),
    ]


def test_draft_debounce_is_scoped_by_profile_and_session_id():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")

    draft_helpers = _composer_draft_helpers()
    script = textwrap.dedent(
        f"""
        const S = {{
          session:{{session_id:'shared',profile:'beta',composer_draft:{{text:'',files:[]}}}},
          activeProfile:'beta',activeProfileIsDefault:false,pendingFiles:[]
        }};
        const $ = () => null;
        const localStorage = {{getItem(){{return null;}},setItem(){{}},removeItem(){{}}}};
        const posts = [];
        function api(_path, options) {{
          posts.push(JSON.parse(options.body));
          return Promise.resolve({{ok:true}});
        }}
        {draft_helpers}

        _saveComposerDraft('shared','alpha-draft',[],'alpha');
        _saveComposerDraft('shared','beta-draft',[],'beta');
        setTimeout(() => {{
          process.stdout.write(JSON.stringify(posts));
        }}, _DRAFT_SAVE_DELAY_MS + 100);
        """
    )
    proc = subprocess.run(
        [node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert [post["text"] for post in json.loads(proc.stdout)] == [
        "alpha-draft",
        "beta-draft",
    ]


def test_clearing_hidden_owner_draft_cannot_cancel_visible_owner_pending_save():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")

    draft_helpers = _composer_draft_helpers()
    script = textwrap.dedent(
        f"""
        const S = {{
          session:{{
            session_id:'owner-b',profile:'beta',
            composer_draft:{{text:'baseline-b',files:[]}}
          }},
          activeProfile:'beta',activeProfileIsDefault:false,pendingFiles:[]
        }};
        const $ = () => null;
        const localStorage = {{getItem(){{return null;}},setItem(){{}},removeItem(){{}}}};
        const posts = [];
        function api(_path, options) {{
          posts.push(JSON.parse(options.body));
          return Promise.resolve({{ok:true}});
        }}
        {draft_helpers}

        _saveComposerDraft('owner-b','foreground-unsaved',[],'beta');
        _clearComposerDraft('owner-a','background-a',[],'alpha');
        setTimeout(() => {{
          process.stdout.write(JSON.stringify(posts));
        }}, _DRAFT_SAVE_DELAY_MS + 100);
        """
    )
    proc = subprocess.run(
        [node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert [
        (post["session_id"], post.get("text")) for post in json.loads(proc.stdout)
    ] == [
        ("owner-a", ""),
        ("owner-b", "foreground-unsaved"),
    ]


def test_draft_writes_for_one_session_are_serialized():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    assert node is not None

    source = _function(
        SESSIONS_JS,
        "_queueComposerDraftWrite",
        "\n\nfunction _composerPendingFilesOwnerKey(",
    )
    script = textwrap.dedent(
        f"""
        const _composerDraftWriteBySid = new Map();
        {source}
        const order = [];
        let releaseFirst;
        const first = _queueComposerDraftWrite('sid-1', () => {{
          order.push('first-start');
          return new Promise(resolve => {{
            releaseFirst = () => {{ order.push('first-finish'); resolve(); }};
          }});
        }});
        const second = _queueComposerDraftWrite('sid-1', async () => {{ order.push('second-start'); }});
        (async () => {{
          await Promise.resolve();
          await Promise.resolve();
          const beforeRelease = [...order];
          releaseFirst();
          await Promise.all([first, second]);
          process.stdout.write(JSON.stringify({{beforeRelease, after:order}}));
        }})().catch(error => {{ console.error(error); process.exit(1); }});
        """
    )
    proc = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    assert result["beforeRelease"] == ["first-start"]
    assert result["after"] == ["first-start", "first-finish", "second-start"]


def test_draft_write_queue_recovers_after_rejection_and_cleans_up():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    assert node is not None

    source = _function(
        SESSIONS_JS,
        "_queueComposerDraftWrite",
        "\n\nfunction _composerPendingFilesOwnerKey(",
    )
    script = textwrap.dedent(
        f"""
        const _composerDraftWriteBySid = new Map();
        {source}
        const order = [];
        const first = _queueComposerDraftWrite('sid-1', async () => {{
          order.push('first');
          throw new Error('write failed');
        }});
        const second = _queueComposerDraftWrite('sid-1', async () => {{ order.push('second'); }});
        (async () => {{
          let firstRejected = false;
          try {{ await first; }} catch (_) {{ firstRejected = true; }}
          await second;
          await Promise.resolve();
          process.stdout.write(JSON.stringify({{
            firstRejected,
            order,
            queueSize: _composerDraftWriteBySid.size,
          }}));
        }})().catch(error => {{ console.error(error); process.exit(1); }});
        """
    )
    proc = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    assert result == {
        "firstRejected": True,
        "order": ["first", "second"],
        "queueSize": 0,
    }


def test_immediate_draft_save_can_fail_closed_at_owner_boundary():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    assert node is not None

    source = _function(
        SESSIONS_JS,
        "_saveComposerDraftNow",
        "\n\n// Restore composer draft from server",
    )
    script = textwrap.dedent(
        f"""
        {source}
        const S = {{session:null}};
        function _clearComposerDraftSaveTimer() {{}}
        const _composerDraftKnownPayloadSessions = new Set();
        function _rememberComposerPendingFiles() {{}}
        function _composerDraftFilesForPersist(files) {{ return files; }}
        function _composerDraftHasPayload(text, files) {{ return !!(text || files.length); }}
        function _clearComposerDraftRestoreSuppression() {{}}
        function _sessionComposerDraftHasPayload() {{ return false; }}
        function _rememberComposerDraftPayloadState() {{}}
        function _syncComposerOwnerStateFromDraft() {{}}
        function _queueComposerDraftWrite(_sid, write) {{ return Promise.resolve().then(write); }}
        function api() {{ return Promise.reject(new Error('draft endpoint unavailable')); }}
        (async () => {{
          let strictRejected = false;
          try {{
            await _saveComposerDraftNow('strict-sid', 'draft', [], 'default', {{rejectOnError:true}});
          }} catch (_) {{ strictRejected = true; }}
          let softResolved = false;
          await _saveComposerDraftNow('soft-sid', 'draft', [], 'default').then(() => {{ softResolved = true; }});
          process.stdout.write(JSON.stringify({{strictRejected, softResolved}}));
        }})().catch(error => {{ console.error(error); process.exit(1); }});
        """
    )
    proc = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert json.loads(proc.stdout) == {"strictRejected": True, "softResolved": True}


def test_all_draft_post_paths_use_the_per_session_write_queue():
    assert SESSIONS_JS.count("enqueue(sid,()=>api('/api/session/draft'") == 3


def test_owner_swap_restore_and_mutation_drain_have_no_await_gap():
    start = SESSIONS_JS.index("S.session=data.session;")
    adopt = SESSIONS_JS.index("_adoptRegenerationRevision(data.session)", start)
    messages = SESSIONS_JS.index("S.messages=data.session.messages||[]", adopt)
    restore = SESSIONS_JS.index("_restoreComposerDraft(S.session.composer_draft)", start)
    drain = SESSIONS_JS.index("_drainComposerOwnershipTransition(composerTransition)", restore)
    first_await = SESSIONS_JS.index("await _saveComposerDraftNow(", drain)

    assert start < adopt < messages < restore < drain < first_await
    executable = "\n".join(
        line.split("//", 1)[0] for line in SESSIONS_JS[start:drain].splitlines()
    )
    assert "await " not in executable


def test_ordered_programmatic_callbacks_preserve_latest_replacement_and_raw_file():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const msg={{value:'source text'}};
        const S={{pendingFiles:[]}};
        const $=id=>id==='msg'?msg:null;
        {authority}
        const raw={{name:'voice-input.webm'}};
        const token=_beginComposerOwnershipTransition('old-session','default');
        _composerSetText('Hello world','Hello world');
        _composerSetText('Hello','Hello');
        _composerAddFiles([raw]);
        msg.value='';
        S.pendingFiles=[];
        _drainComposerOwnershipTransition(token);
        process.stdout.write(JSON.stringify({{text:msg.value,files:S.pendingFiles.map(f=>f.name)}}));
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"text": "Hello", "files": ["voice-input.webm"]}


def test_failed_send_restore_during_create_survives_late_clear_settlement():
    result = _run_review_race_harness("failed-send")

    assert result["activeSid"] == "new-session"
    assert result["text"] == ""
    assert result["files"] == []
    source_saves = [save for save in result["saves"] if save["sid"] == "old-session"]
    assert source_saves[-1] == {
        "sid": "old-session",
        "text": "failed restore",
        "files": ["restored.txt"],
        "profile": "default",
    }


def test_failed_send_restore_after_owner_swap_stays_with_source_session():
    result = _run_review_race_harness("failed-send-after-swap")

    assert result["activeSid"] == "new-session"
    assert result["text"] == ""
    assert result["files"] == []
    source_saves = [save for save in result["saves"] if save["sid"] == "old-session"]
    assert source_saves[-1]["text"] == "failed restore"
    assert source_saves[-1]["files"] == ["restored.txt"]


def test_late_failed_send_persist_does_not_overwrite_newer_source_revision():
    result = _run_review_race_harness("failed-send-newer-source")

    assert result["activeSid"] == "new-session"
    assert result["text"] == ""
    source_saves = [save for save in result["saves"] if save["sid"] == "old-session"]
    assert source_saves[-1]["text"] == "failed restore\n\nnewer clarify"
    assert source_saves[-1]["files"] == ["restored.txt"]


def test_clarify_rescue_during_create_is_persisted_to_its_source_owner():
    result = _run_review_race_harness("clarify")

    assert result["activeSid"] == "new-session"
    assert result["text"] == ""
    source_saves = [save for save in result["saves"] if save["sid"] == "old-session"]
    assert source_saves[-1]["text"] == "source draft\n\nclarify answer"
    assert source_saves[-1]["files"] == ["source.txt"]


def test_clarify_terminal_rescue_during_create_is_persisted_to_source_owner():
    result = _run_review_race_harness("clarify-terminal")

    assert result["activeSid"] == "new-session"
    source_saves = [save for save in result["saves"] if save["sid"] == "old-session"]
    assert source_saves[-1]["text"] == "source draft\n\nclarify answer"


def test_late_clarify_rescue_after_owner_swap_never_mutates_new_session():
    result = _run_review_race_harness("late-clarify-after-swap")

    assert result["activeSid"] == "new-session"
    assert result["text"] == ""
    source_saves = [save for save in result["saves"] if save["sid"] == "old-session"]
    assert source_saves[-1]["text"] == "source draft\n\nclarify answer"


def test_clarify_409_after_owner_swap_keeps_captured_old_session_owner():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    stash = _function(MESSAGES_JS, "_stashClarifyDraft", "\n\nfunction _resetClarifyCardState(")
    hide = _function(MESSAGES_JS, "hideClarifyCard", "\n\nfunction _clarifySetControlsDisabled(")
    respond_start = MESSAGES_JS.index("async function respondClarify(")
    respond_end = MESSAGES_JS.index("\n\nvar _clarifyEventSource", respond_start)
    respond = MESSAGES_JS[respond_start:respond_end]
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const msg={{value:'old draft'}};
        const clarifyInput={{value:'late answer',focus(){{}}}};
        const classList={{contains(){{return false;}},remove(){{}}}};
        const elements={{
          msg,clarifyInput,clarifySubmit:{{classList}},clarifyCard:{{classList}},
          clarifyQuestion:{{textContent:''}},clarifyChoices:{{innerHTML:''}}
        }};
        const $=id=>elements[id]||null;
        const S={{
          session:{{session_id:'old-session',profile:'default'}},
          activeProfile:'default',pendingFiles:[]
        }};
        const sessionStorage={{setItem(){{}}}};
        const saves=[];
        let _clarifySessionId='old-session';
        let _clarifyOwnerProfile='default';
        let _clarifySignature='sig';
        let _clarifyId='clarify-1';
        function _saveComposerDraftNow(sid,text,files,profile){{
          saves.push({{sid,text,files:[...(files||[])],profile}});return Promise.resolve();
        }}
        function _clarifySetControlsDisabled(){{}}
        function _clearClarifyPendingForSession(){{}}
        function _resetClarifyCardState(){{
          _clarifySignature='';_clarifyOwnerProfile=null;_clarifyId=null;
        }}
        function _setPromptFlyoutHidden(){{}}
        function _syncClarifyTranscriptSpace(){{}}
        function unlockComposerForClarify(){{}}
        function autoResize(){{}}
        function updateSendBtn(){{}}
        function setComposerStatus(){{}}
        function setStatus(){{}}
        function showToast(){{}}
        let rejectClarify;
        function api(){{
          return new Promise((_resolve,reject)=>{{rejectClarify=reject;}});
        }}
        {authority}
        {stash}
        {hide}
        {respond}

        const pendingResponse=respondClarify();
        const transition=_beginComposerOwnershipTransition('old-session','default');
        _bindComposerOwnershipDestination(transition,'new-session','default');
        S.session={{session_id:'new-session',profile:'default'}};
        msg.value='';
        _drainComposerOwnershipTransition(transition);
        const clarifyError=new Error('expired');
        clarifyError.status=409;
        rejectClarify(clarifyError);

        pendingResponse.then(()=>Promise.resolve()).then(()=>{{
          process.stdout.write(JSON.stringify({{text:msg.value,saves}}));
        }}).catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["text"] == ""
    assert result["saves"][-1]["sid"] == "old-session"
    assert result["saves"][-1]["text"] == "old draft\n\nlate answer"


def test_clarify_rescue_survives_aborted_session_creation():
    result = _run_review_race_harness("clarify-abort")

    assert result["error"] == "create failed"
    assert result["activeSid"] == "old-session"
    assert result["text"] == "source draft\n\nclarify answer"
    assert result["files"] == ["source.txt"]
    source_saves = [save for save in result["saves"] if save["sid"] == "old-session"]
    assert source_saves[-1]["text"] == "source draft\n\nclarify answer"


def test_abort_shadow_preserves_destination_voice_then_source_clarify_order():
    result = _run_review_race_harness("voice-clarify-abort")

    assert result["error"] == "create failed"
    assert result["activeSid"] == "old-session"
    assert result["text"] == "source draft voice\n\nclarify answer"


@pytest.mark.parametrize(
    ("schedule", "expected"),
    [
        ("voice-then-prompt", "voice\n\nsaved prompt\n\n"),
        ("prompt-then-voice", "saved prompt\n\nvoice"),
    ],
)
def test_saved_prompt_and_voice_producers_compose_in_arrival_order(schedule, expected):
    result = _run_review_race_harness(schedule)

    assert result["activeSid"] == "new-session"
    assert result["text"] == expected


def test_voice_final_replaces_its_interim_slot_without_dropping_saved_prompt():
    result = _run_review_race_harness("voice-prompt-voice")

    assert result["activeSid"] == "new-session"
    assert result["text"] == "final\n\nsaved prompt\n\n"


def test_aborted_create_preserves_voice_final_and_interleaved_saved_prompt():
    result = _run_review_race_harness("voice-prompt-voice-abort")

    assert result["error"] == "create failed"
    assert result["activeSid"] == "old-session"
    assert result["text"] == "source draft final\n\nsaved prompt\n\n"


def test_abort_after_owner_change_persists_source_without_mutating_visible_composer():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const sourceFile={{name:'source.txt'}};
        const visibleFile={{name:'visible.txt'}};
        const msg={{value:'source draft'}};
        const S={{
          session:{{session_id:'session-a',profile:'default'}},
          activeProfile:'default',pendingFiles:[sourceFile]
        }};
        const $=id=>id==='msg'?msg:null;
        const saves=[];
        function _saveComposerDraftNow(sid,text,files,profile){{
          saves.push({{sid,text,files:files.map(file=>file.name),profile}});
          return Promise.resolve();
        }}
        function renderTray(){{throw new Error('hidden abort must not render');}}
        function autoResize(){{throw new Error('hidden abort must not resize');}}
        function updateSendBtn(){{throw new Error('hidden abort must not repaint send');}}
        {authority}

        const token=_beginComposerOwnershipTransition('session-a','default');
        _composerSetText('source draft plus late input','late input');
        S.session={{session_id:'session-b',profile:'default'}};
        msg.value='visible B';
        S.pendingFiles=[visibleFile];
        const restoredVisible=_abortComposerOwnershipTransition(token);
        Promise.resolve().then(()=>Promise.resolve()).then(()=>{{
          process.stdout.write(JSON.stringify({{
            restoredVisible,text:msg.value,
            files:S.pendingFiles.map(file=>file.name),saves,
          }}));
        }});
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert json.loads(proc.stdout) == {
        "restoredVisible": False,
        "text": "visible B",
        "files": ["visible.txt"],
        "saves": [
            {
                "sid": "session-a",
                "text": "source draft plus late input",
                "files": ["source.txt"],
                "profile": "default",
            }
        ],
    }


def test_failed_handoff_replays_full_source_form_not_destination_delta():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const msg={{value:'source draft'}};
        const S={{
          session:{{session_id:'old-session',profile:'default'}},
          activeProfile:'default',pendingFiles:[]
        }};
        const $=id=>id==='msg'?msg:null;
        {authority}
        const token=_beginComposerOwnershipTransition('old-session','default');
        _composerSetText('source draft voice addition','voice addition');
        _abortComposerOwnershipTransition(token);
        process.stdout.write(msg.value);
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "source draft voice addition"


def test_new_session_and_clarify_disabled_reasons_do_not_unlock_each_other():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const input={{disabled:false}};
        {authority}
        _composerControlSetDisabledReason(input,'new-session',true);
        _composerControlSetDisabledReason(input,'clarify',true);
        _composerControlSetDisabledReason(input,'new-session',false);
        const afterNewSession=input.disabled;
        _composerControlSetDisabledReason(input,'clarify',false);
        const afterClarify=input.disabled;
        process.stdout.write(JSON.stringify({{afterNewSession,afterClarify}}));
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"afterNewSession": True, "afterClarify": False}


def test_async_composer_producers_route_through_ownership_authority():
    assert "_composerAddFiles([file],null,producerHandle)" in BOOT_JS
    assert BOOT_JS.count("_composerSetText(") >= 5
    assert "_composerAppendText(text,null,producer,null,'block')" in MESSAGES_JS
    assert "_composerAddFiles(accepted)" in UI_JS
    assert "_composerRemoveFile(f,S.session&&S.session.session_id)" in UI_JS
    assert "const captureProducerHandle=_micComposerProducerToken" in BOOT_JS
    assert "const capturePrefixSnapshot=_prefix" in BOOT_JS
    assert "if(isCurrentProducer)_applyDeferredServerSttFlip()" in BOOT_JS
    assert "_transcribeBlob(blob,prefixSnapshot,captureProducerHandle)" in BOOT_JS


def test_superseded_media_callbacks_drop_payload_before_composer_mutation():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    start = BOOT_JS.index("function _micProducerIsCurrent(")
    end = BOOT_JS.index("\n\n  function _isServerSttUnavailable", start)
    helpers = BOOT_JS[start:end]
    script = textwrap.dedent(
        f"""
        const handleA={{producerToken:'A'}};
        const handleB={{producerToken:'B'}};
        let _micComposerProducerToken=handleB;
        let _dictationAppend=false;
        let _prefix='B prefix';
        const ta={{value:'seed'}};
        const S={{pendingFiles:[]}};
        const window={{_micPendingSend:false}};
        let sends=0;
        let resizes=0;
        let trayRenders=0;
        let toasts=0;
        let composerText=ta.value;
        const composerFiles=[];
        const routed=[];
        class File {{
          constructor(_parts,name,options){{this.name=name;this.type=options.type;}}
        }}
        function _composerAddFiles(files,_sid,handle){{
          composerFiles.push(...files.map(file=>file.name));
          routed.push({{kind:'file',name:files[0].name,handle}});
        }}
        function _composerSetText(value,transition,_sid,handle){{
          composerText=value;
          ta.value=value;
          routed.push({{kind:'text',value,transition,handle}});
        }}
        function renderTray(){{trayRenders++;}}
        function send(){{sends++;}}
        function autoResize(){{resizes++;}}
        function showToast(){{toasts++;}}
        function t(value){{return value;}}
        {helpers}
        (async()=>{{
          _commitTranscript('live-b',undefined,handleB);
          await _sendRawAudio({{type:'audio/webm'}},handleB);
          const beforeLateA={{
            composerText,
            composerFiles:[...composerFiles],
            routed:JSON.parse(JSON.stringify(routed)),
          }};
          await _sendRawAudio({{type:'audio/webm'}},handleA);
          _commitTranscript('late transcript',undefined,handleA);
          process.stdout.write(JSON.stringify({{
            beforeLateA,
            afterLateA:{{composerText,composerFiles,routed}},
            sends,resizes,trayRenders,toasts,pendingSend:window._micPendingSend
          }}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["beforeLateA"] == result["afterLateA"]
    assert result["afterLateA"]["composerText"] == "live-b"
    assert len(result["afterLateA"]["composerFiles"]) == 1
    assert result["afterLateA"]["routed"][0] == {
        "kind": "text",
        "value": "live-b",
        "transition": "live-b",
        "handle": {"producerToken": "B"},
    }
    assert result["afterLateA"]["routed"][1]["kind"] == "file"
    assert result["afterLateA"]["routed"][1]["handle"] == {"producerToken": "B"}
    assert result["sends"] == 0
    assert result["resizes"] == 1
    assert result["trayRenders"] == 1
    assert result["toasts"] == 1
    assert result["pendingSend"] is False


def test_superseded_get_user_media_completion_cannot_clear_new_recording_state():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    start = BOOT_JS.index("async function _startMicCapture(")
    end = BOOT_JS.index("\n\n  async function _toggleMicCapture", start)
    start_capture = BOOT_JS[start:end]
    script = textwrap.dedent(
        f"""
        let resolveA,resolveB;
        const streamA={{getTracks(){{return [{{stop(){{}}}}];}}}};
        const streamB={{getTracks(){{return [{{stop(){{}}}}];}}}};
        const pending=[
          new Promise(resolve=>{{resolveA=()=>resolve(streamA);}}),
          new Promise(resolve=>{{resolveB=()=>resolve(streamB);}}),
        ];
        const navigator={{mediaDevices:{{getUserMedia(){{return pending.shift();}}}}}};
        class FakeRecorder {{
          constructor(stream){{this.stream=stream;this.mimeType='audio/webm';this.state='inactive';}}
          start(){{this.state='recording';}}
        }}
        FakeRecorder.isTypeSupported=()=>true;
        const window={{MediaRecorder:FakeRecorder,_micActive:false,_micPendingSend:false}};
        const MediaRecorder=FakeRecorder;
        let _micStartSeq=0;
        let _isRecording=false;
        let _finalText='';
        let _prefix='';
        let _micComposerProducerToken=null;
        let _forceMediaRecorder=true;
        let _rawAudioMode=false;
        let _canRecordAudio=true;
        let _micHoldActive=false;
        let recognition=null;
        let mediaStream=null;
        let mediaRecorder=null;
        let audioChunks=[];
        let _activeCaptureMode=null;
        const ta={{value:''}};
        let producerSequence=0;
        function _newComposerProducerHandle(){{
          producerSequence++;
          return Object.freeze({{producerToken:String(producerSequence)}});
        }}
        function _micProducerIsCurrent(handle){{return handle===_micComposerProducerToken;}}
        function _micButtonAvailable(){{return true;}}
        function _stopMic(){{_micStartSeq++;_isRecording=false;window._micActive=false;}}
        function _micOriginNeedsSecureContext(){{return false;}}
        function _stopTracks(stream){{if(stream)stream.getTracks().forEach(track=>track.stop());}}
        function _setRecording(active){{window._micActive=active;}}
        function _applyDeferredServerSttFlip(){{}}
        function _sendRawAudio(){{return Promise.resolve();}}
        function _transcribeBlob(){{return Promise.resolve();}}
        function showToast(){{}}
        function t(value){{return value;}}
        {start_capture}

        (async()=>{{
          const a=_startMicCapture();
          _micStartSeq++;
          _isRecording=false;
          window._micActive=false;
          const b=_startMicCapture();
          resolveB();
          await b;
          const beforeLateA={{isRecording:_isRecording,micActive:window._micActive}};
          resolveA();
          await a;
          process.stdout.write(JSON.stringify({{
            beforeLateA,
            afterLateA:{{isRecording:_isRecording,micActive:window._micActive}},
            currentProducer:_micComposerProducerToken.producerToken,
          }}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "beforeLateA": {"isRecording": True, "micActive": True},
        "afterLateA": {"isRecording": True, "micActive": True},
        "currentProducer": "2",
    }


def test_superseded_recorder_callbacks_drop_payload_before_buffer_mutation():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    start = BOOT_JS.index("      const captureChunks=audioChunks;")
    end = BOOT_JS.index("      _activeCaptureMode=captureMode;", start)
    recorder_callbacks = BOOT_JS[start:end]
    script = textwrap.dedent(
        f"""
        const handleA={{producerToken:'A'}};
        const handleB={{producerToken:'B'}};
        let _micComposerProducerToken=handleB;
        let audioChunks=[];
        let _isRecording=true;
        const window={{_micPendingSend:false}};
        const streamA={{getTracks(){{return [{{stop(){{}}}}];}}}};
        const streamB={{getTracks(){{return [{{stop(){{}}}}];}}}};
        const captureStream=streamA;
        let mediaStream=streamB;
        const recorder={{mimeType:'audio/webm'}};
        let mediaRecorder={{mimeType:'audio/webm'}};
        const captureProducerHandle=handleA;
        const capturePrefixSnapshot='A prefix';
        const captureMode='media-raw';
        const mimeType='audio/webm';
        const blobs=[];
        const downstream=[];
        class Blob {{
          constructor(parts,options){{
            this.size=parts.length;
            this.type=options.type;
            blobs.push(parts.map(part=>part.label));
          }}
        }}
        function _micProducerIsCurrent(handle){{return handle===_micComposerProducerToken;}}
        function _setRecording(){{}}
        function _stopTracks(stream){{if(stream)stream.getTracks().forEach(track=>track.stop());}}
        function _sendRawAudio(blob,handle){{
          downstream.push({{kind:'raw',size:blob.size,handle}});
          return Promise.resolve();
        }}
        function _transcribeBlob(blob,_prefix,handle){{
          downstream.push({{kind:'transcribe',size:blob.size,handle}});
          return Promise.resolve();
        }}
        function _applyDeferredServerSttFlip(){{}}
        function showToast(){{}}
        function t(value){{return value;}}
        {recorder_callbacks}

        (async()=>{{
          recorder.ondataavailable({{data:{{size:1,label:'late-a'}}}});
          await recorder.onstop();
          process.stdout.write(JSON.stringify({{
            buffered:captureChunks.length,
            blobs,
            downstream,
          }}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "buffered": 0,
        "blobs": [],
        "downstream": [],
    }


def test_voice_mode_callback_uses_immutable_lifecycle_producer_token():
    start = BOOT_JS.index("function _startListening(){")
    end = BOOT_JS.index("\n  function _voiceModeSend(){", start)
    lifecycle = BOOT_JS[start:end]

    assert "const lifecycleProducerToken=" in lifecycle
    assert "null,lifecycleProducerToken" in lifecycle
    assert "_recognition=new SpeechRecognition()" in lifecycle
    assert "const lifecycleRecognition=_recognition" in lifecycle
    assert lifecycle.count("_recognition!==lifecycleRecognition") >= 3


def test_voice_mode_lifecycle_b_text_survives_late_a_callback():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    start = BOOT_JS.index("function _startListening(){")
    end = BOOT_JS.index("\n  function _voiceModeSend(){", start)
    start_listening = BOOT_JS[start:end]
    script = textwrap.dedent(
        f"""
        class FakeSpeechRecognition {{ start(){{}} }}
        const SpeechRecognition=FakeSpeechRecognition;
        const localStorage={{getItem(){{return 'false';}}}};
        const _locale={{_speech:'en-US'}};
        const ta={{value:''}};
        let _voiceModeActive=true;
        let _voiceModeState='idle';
        let _recognition=null;
        let _voiceComposerProducerToken='voice-mode';
        let _silenceTimer=null;
        let producerSequence=0;
        let composerText='';
        let resizes=0;
        const calls=[];
        function _newComposerProducerHandle(){{
          producerSequence++;
          return Object.freeze({{producerToken:String(producerSequence)}});
        }}
        function _micOriginNeedsSecureContext(){{return false;}}
        function _clearBrowserTtsRecovery(){{}}
        function _setState(state){{_voiceModeState=state;}}
        function _deactivate(){{_voiceModeActive=false;}}
        function _voiceModeSend(){{}}
        function _voiceSilenceMs(){{return 1800;}}
        function _micToastKeyForRecognitionError(){{return null;}}
        function showToast(){{}}
        function t(value){{return value;}}
        function clearTimeout(){{}}
        function setTimeout(){{return 1;}}
        function autoResize(){{resizes++;}}
        function _composerSetText(value,transition,_owner,handle){{
          composerText=value;
          ta.value=value;
          calls.push({{value,transition,handle}});
        }}
        {start_listening}

        _startListening();
        const lifecycleA=_recognition;
        lifecycleA.onstart();
        _startListening();
        const lifecycleB=_recognition;
        lifecycleB.onstart();

        const result=text=>({{resultIndex:0,results:[Object.assign([{{transcript:text}}],{{isFinal:true}})]}});
        lifecycleB.onresult(result('live-b'));
        const beforeLateA={{composerText,calls:JSON.parse(JSON.stringify(calls)),resizes}};
        lifecycleA.onresult(result('late-a'));
        process.stdout.write(JSON.stringify({{
          beforeLateA,
          afterLateA:{{composerText,calls,resizes}},
        }}));
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["beforeLateA"] == result["afterLateA"]
    assert result["afterLateA"] == {
        "composerText": "live-b",
        "calls": [{
            "value": "live-b",
            "transition": "live-b",
            "handle": {"producerToken": "2"},
        }],
        "resizes": 1,
    }


def test_dictation_callback_keeps_lifecycle_local_text_and_handle():
    start = BOOT_JS.index("function _ensureSpeechRecognition(")
    end = BOOT_JS.index("\n\n  if(!_forceMediaRecorder)", start)
    lifecycle = BOOT_JS[start:end]

    assert "const lifecycleProducerHandle=" in lifecycle
    assert "let lifecycleFinalText=''" in lifecycle
    assert "let _prefixForLifecycle=_prefix" in lifecycle
    assert "if(recognition!==sr)return" in lifecycle


def test_late_dictation_lifecycle_a_callbacks_are_dropped_after_b_starts():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    start = BOOT_JS.index("function _ensureSpeechRecognition(")
    end = BOOT_JS.index("\n\n  if(!_forceMediaRecorder)", start)
    ensure_speech = BOOT_JS[start:end]
    script = textwrap.dedent(
        f"""
        class FakeSpeechRecognition {{ start(){{}} }}
        const SpeechRecognition=FakeSpeechRecognition;
        let recognition=null;
        let _micComposerProducerToken='initial';
        let _prefix='A: ';
        let _finalText='';
        let _micRestartCount=0;
        const _micMaxRestarts=20;
        let _speechStopRequested=false;
        let _isRecording=true;
        let _activeCaptureMode='speech';
        const window={{_micActive:true,_micPendingSend:false}};
        const ta={{value:''}};
        let composerText='';
        const calls=[];
        function _micDictationContinuous(){{return false;}}
        function _micShouldRestartDictation(){{return false;}}
        function _releaseMicWakeLock(){{return Promise.resolve();}}
        function _setRecording(){{}}
        function _applyDeferredServerSttFlip(){{}}
        function _micToastKeyForRecognitionError(){{return null;}}
        function showToast(){{}}
        function t(value){{return value;}}
        function send(){{}}
        let resizes=0;
        function autoResize(){{resizes++;}}
        function _composerSetText(value,transition,owner,handle){{
          composerText=value;
          ta.value=value;
          calls.push({{value,transition,handle}});
        }}
        {ensure_speech}

        const handleA={{producerToken:'A'}};
        recognition=_ensureSpeechRecognition(handleA);
        const lifecycleA=recognition;
        lifecycleA.onstart();

        _prefix='B: ';
        const handleB={{producerToken:'B'}};
        recognition=_ensureSpeechRecognition(handleB);
        const lifecycleB=recognition;
        lifecycleB.onstart();

        const result=text=>({{resultIndex:0,results:[Object.assign([{{transcript:text}}],{{isFinal:true}})]}});
        lifecycleB.onresult(result('live-b'));
        const beforeLateA={{composerText,calls:JSON.parse(JSON.stringify(calls)),finalText:_finalText,resizes}};
        lifecycleA.onresult(result('late-a'));
        lifecycleA.onend();
        process.stdout.write(JSON.stringify({{
          beforeLateA,
          afterLateA:{{composerText,calls,finalText:_finalText,resizes}},
        }}));
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["beforeLateA"] == result["afterLateA"]
    assert result["afterLateA"] == {
        "composerText": "B: live-b",
        "calls": [{
            "value": "B: live-b",
            "transition": "live-b",
            "handle": {"producerToken": "B"},
        }],
        "finalText": "live-b",
        "resizes": 1,
    }


def test_immediate_draft_save_refreshes_owner_authority_with_profile_and_revision():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    sync_owner = _function(
        SESSIONS_JS,
        "_syncComposerOwnerStateFromDraft",
        "\n\nfunction _sessionComposerDraftHasPayload",
    )
    save_now = _function(
        SESSIONS_JS,
        "_saveComposerDraftNow",
        "\n\n// Restore composer draft from server",
    )
    script = textwrap.dedent(
        f"""
        const S={{session:{{session_id:'sid-a',profile:'work'}},activeProfile:'work'}};
        function _clearComposerDraftSaveTimer(){{}}
        const remembered=[];
        function _rememberComposerPendingFiles(){{}}
        function _composerDraftFilesForPersist(files){{return files.map(file=>file.name);}}
        function _composerDraftHasPayload(text,files){{return !!(text||files.length);}}
        function _clearComposerDraftRestoreSuppression(){{}}
        function _sessionComposerDraftHasPayload(){{return false;}}
        const _composerDraftKnownPayloadSessions=new Set();
        function _queueComposerDraftWrite(_sid,write){{return Promise.resolve().then(write);}}
        function api(){{return Promise.resolve({{ok:true}});}}
        function _rememberComposerDraftPayloadState(){{}}
        function _composerRememberedOwnerSnapshot(){{
          return {{generation:7,revision:3,text:'older',files:[],profile:'work',session_id:'sid-a'}};
        }}
        function _rememberComposerOwnerState(sid,profile,state,generation){{
          remembered.push({{sid,profile,state,generation}});
        }}
        {sync_owner}
        {save_now}

        _saveComposerDraftNow('sid-a','draft A',[{{name:'a.txt'}}]).then(()=>{{
          process.stdout.write(JSON.stringify(remembered));
        }});
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    remembered = json.loads(proc.stdout)
    assert remembered == [{
        "sid": "sid-a",
        "profile": "work",
        "state": {"text": "draft A", "files": [{"name": "a.txt"}], "revision": 4},
        "generation": 7,
    }]


def test_ordinary_switch_save_r2_invalidates_deferred_failed_send_r1_snapshot():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    authority = _composer_authority_helpers()
    sync_owner = _function(
        SESSIONS_JS,
        "_syncComposerOwnerStateFromDraft",
        "\n\nfunction _sessionComposerDraftHasPayload",
    )
    save_now = _function(
        SESSIONS_JS,
        "_saveComposerDraftNow",
        "\n\n// Restore composer draft from server",
    )
    script = textwrap.dedent(
        f"""
        const msg={{value:''}};
        const S={{
          session:{{session_id:'old-session',profile:'default',composer_draft:{{}}}},
          activeProfile:'default',pendingFiles:[]
        }};
        const $=id=>id==='msg'?msg:null;
        function _clearComposerDraftSaveTimer(){{}}
        const _composerDraftKnownPayloadSessions=new Set();
        const writes=[];
        function _rememberComposerPendingFiles(){{}}
        function _composerDraftFilesForPersist(files){{return files.map(f=>f.name||f);}}
        function _composerDraftHasPayload(text,files){{return !!(text||files.length);}}
        function _clearComposerDraftRestoreSuppression(){{}}
        function _sessionComposerDraftHasPayload(){{return false;}}
        function _rememberComposerDraftPayloadState(){{}}
        function _queueComposerDraftWrite(_sid,write){{return Promise.resolve().then(write);}}
        function api(_path,options){{
          writes.push(JSON.parse(options.body));return Promise.resolve({{ok:true}});
        }}
        {authority}
        {sync_owner}
        {save_now}

        const tx=_beginComposerOwnershipTransition('old-session','default');
        const producer=_newComposerProducerToken('failed-send');
        _composerSetText('r1','r1','old-session',producer,'default');
        const r1Snapshot=_composerOwnerSnapshot('old-session','default');
        _bindComposerOwnershipDestination(tx,'new-session','default');
        S.session={{session_id:'new-session',profile:'default',composer_draft:{{}}}};
        msg.value='';
        _drainComposerOwnershipTransition(tx);

        S.session={{session_id:'old-session',profile:'default',composer_draft:{{}}}};
        msg.value='r2';
        _saveComposerDraftNow('old-session','r2',[],'default').then(()=>{{
          S.session={{session_id:'new-session',profile:'default',composer_draft:{{}}}};
          msg.value='';
          if(_composerOwnerSnapshotIsCurrent(r1Snapshot)){{
            return _saveComposerDraftNow(
              'old-session',r1Snapshot.text,r1Snapshot.files,r1Snapshot.profile
            );
          }}
        }}).then(()=>{{
          process.stdout.write(JSON.stringify({{
            writes,current:_composerRememberedOwnerSnapshot('old-session','default')
          }}));
        }}).catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["writes"][-1]["text"] == "r2"
    assert [write["text"] for write in result["writes"]].count("r1") == 1
    assert result["current"]["text"] == "r2"
    assert result["current"]["revision"] > 1


def test_owner_snapshot_fails_closed_when_authority_is_missing_or_revision_changed():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const S={{session:null,activeProfile:'default',pendingFiles:[]}};
        const $=()=>null;
        {authority}
        const snapshot={{
          session_id:'sid-a',profile:'default',generation:4,revision:2,text:'r2',files:[]
        }};
        const missing=_composerOwnerSnapshotIsCurrent(snapshot);
        _rememberComposerOwnerState('sid-a','default',snapshot,4);
        const exact=_composerOwnerSnapshotIsCurrent(snapshot);
        _rememberComposerOwnerState('sid-a','default',{{
          text:'r3',files:[],revision:3,
        }},4);
        const newer=_composerOwnerSnapshotIsCurrent(snapshot);
        process.stdout.write(JSON.stringify({{missing,exact,newer}}));
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"missing": False, "exact": True, "newer": False}


def test_foreign_owner_cannot_join_null_source_transition():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const msg={{value:''}};
        const S={{session:null,activeProfile:'beta',pendingFiles:[]}};
        const $=id=>id==='msg'?msg:null;
        const saves=[];
        function _saveComposerDraftNow(sid,text,files,profile){{
          saves.push({{sid,text,files:[...(files||[])].map(f=>f.name),profile}});
          return Promise.resolve();
        }}
        {authority}
        _rememberComposerOwnerState('owner-a','alpha',{{
          text:'A text',files:[{{name:'a.txt'}}],revision:1,
        }},1);
        const lifecycleA=Object.freeze({{
          producerToken:'voice-a',generation:null,ownerRole:'owner',
          ownerSid:'owner-a',ownerProfile:'alpha',
        }});
        const transitionB=_beginComposerOwnershipTransition(null,'beta');
        _composerSetText('late A','late A',null,lifecycleA);
        _bindComposerOwnershipDestination(transitionB,'owner-b','beta');
        S.session={{session_id:'owner-b',profile:'beta'}};
        _drainComposerOwnershipTransition(transitionB);
        Promise.resolve().then(()=>Promise.resolve()).then(()=>{{
          process.stdout.write(JSON.stringify({{text:msg.value,saves}}));
        }});
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["text"] == ""
    assert result["saves"][-1] == {
        "sid": "owner-a", "text": "late A", "files": ["a.txt"], "profile": "alpha"
    }


def test_non_visible_owner_uses_full_remembered_baseline_for_late_file():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const msg={{value:'B text'}};
        const original={{name:'original.txt'}};
        const late={{name:'late.webm'}};
        const S={{session:{{session_id:'owner-b',profile:'beta'}},activeProfile:'beta',pendingFiles:[]}};
        const $=id=>id==='msg'?msg:null;
        const saves=[];
        function _saveComposerDraftNow(sid,text,files,profile){{
          saves.push({{sid,text,files:[...(files||[])].map(f=>f.name),profile}});
          return Promise.resolve();
        }}
        {authority}
        _rememberComposerOwnerState('owner-a','alpha',{{
          text:'A text',files:[original],revision:4,
        }},2);
        _composerAddFiles([late],'owner-a','raw-a','alpha');
        Promise.resolve().then(()=>Promise.resolve()).then(()=>{{
          process.stdout.write(JSON.stringify({{text:msg.value,saves}}));
        }});
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["text"] == "B text"
    assert result["saves"][-1] == {
        "sid": "owner-a",
        "text": "A text",
        "files": ["original.txt", "late.webm"],
        "profile": "alpha",
    }


def test_non_visible_owner_without_authoritative_baseline_fails_closed():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const msg={{value:'visible draft'}};
        const S={{
          session:{{session_id:'visible',profile:'beta'}},activeProfile:'beta',pendingFiles:[]
        }};
        const $=id=>id==='msg'?msg:null;
        const saves=[];
        function _saveComposerDraftNow(sid,text,files,profile){{
          saves.push({{sid,text,files:files.map(file=>file.name),profile}});
          return Promise.resolve();
        }}
        {authority}
        const late={{name:'late.webm'}};
        _composerAddFiles([late],'forgotten-owner','raw-a','alpha');
        process.stdout.write(JSON.stringify({{
          visibleText:msg.value,
          visibleFiles:S.pendingFiles.map(file=>file.name),
          remembered:_composerRememberedOwnerSnapshot('forgotten-owner','alpha'),
          saves,
        }}));
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "visibleText": "visible draft",
        "visibleFiles": [],
        "remembered": None,
        "saves": [],
    }


def test_same_session_id_different_profile_is_non_visible_owner():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const msg={{value:'beta text'}};
        const S={{
          session:{{session_id:'shared',profile:'work'}},
          activeProfile:'work',activeProfileIsDefault:true,pendingFiles:[]
        }};
        const $=id=>id==='msg'?msg:null;
        const saves=[];
        function _saveComposerDraftNow(sid,text,files,profile){{
          saves.push({{sid,text,profile}});return Promise.resolve();
        }}
        {authority}
        _rememberComposerOwnerState('shared','default',{{text:'default text',files:[],revision:1}},1);
        _composerAppendText(' late','shared','default-producer','default');
        Promise.resolve().then(()=>Promise.resolve()).then(()=>{{
          process.stdout.write(JSON.stringify({{text:msg.value,saves}}));
        }});
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["text"] == "beta text"
    assert result["saves"][-1] == {
        "sid": "shared", "text": "default text late", "profile": "default"
    }


def test_unresolved_generation_callback_is_dropped_instead_of_using_visible_owner():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const msg={{value:'visible'}};
        const S={{session:{{session_id:'visible',profile:'default'}},activeProfile:'default',pendingFiles:[]}};
        const $=id=>id==='msg'?msg:null;
        {authority}
        const stale=Object.freeze({{
          producerToken:'stale',generation:999,ownerRole:'destination',
          ownerSid:null,ownerProfile:'default',
        }});
        _composerSetText('stale text','stale text',null,stale);
        process.stdout.write(msg.value);
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "visible"


def test_owner_file_references_release_on_clear_and_forget_on_delete():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const S={{session:null,activeProfile:'default',pendingFiles:[]}};
        const $=()=>null;
        {authority}
        const file={{name:'private.bin'}};
        _rememberComposerOwnerState('sid-a','default',{{text:'draft',files:[file],revision:1}},1);
        _rememberComposerSettledOwners(1,{{sourceSid:'sid-a',destinationSid:'sid-b'}});
        _releaseComposerOwnerFiles('sid-a');
        const afterRelease=_composerRememberedOwnerSnapshot('sid-a','default');
        _rememberComposerOwnerState('sid-a','default',{{text:'draft',files:[file],revision:2}},1);
        _forgetComposerOwnerState('sid-a');
        process.stdout.write(JSON.stringify({{
          releasedFiles:afterRelease.files.length,
          ownerExists:!!_composerRememberedOwnerSnapshot('sid-a','default'),
          generationExists:_composerSettledOwnersByGeneration.has(1),
        }}));
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "releasedFiles": 0, "ownerExists": False, "generationExists": False
    }
    assert "_releaseComposerOwnerFiles(sid)" in _function(
        SESSIONS_JS, "_clearComposerDraft", "\n\nconst SESSION_VIEWED_COUNTS_KEY"
    )
    assert "_forgetComposerOwnerState(sid)" in _function(
        SESSIONS_JS, "deleteSession", "\n\n// ── Project helpers"
    )
    assert "ids.forEach(_forgetComposerOwnerState)" in SESSIONS_JS


def test_all_ordinary_draft_saves_refresh_owner_revision_authority():
    debounced = _function(SESSIONS_JS, "_saveComposerDraft", "\n\nfunction _composerDraftHasPayload")
    immediate = _function(
        SESSIONS_JS, "_saveComposerDraftNow", "\n\n// Restore composer draft from server"
    )
    assert "_syncComposerOwnerStateFromDraft" in debounced
    assert "_syncComposerOwnerStateFromDraft" in immediate


def test_late_lifecycle_a_callback_cannot_join_transition_b():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const msg={{value:'owner A'}};
        const S={{
          session:{{session_id:'owner-a',profile:'default'}},
          activeProfile:'default',pendingFiles:[]
        }};
        const $=id=>id==='msg'?msg:null;
        const saves=[];
        function _saveComposerDraftNow(sid,text,files,profile){{
          saves.push({{sid,text,files:[...(files||[])],profile}});
          return Promise.resolve();
        }}
        {authority}

        const lifecycleA=_newComposerProducerHandle('voice-a');
        _rememberComposerOwnerState('owner-a','default',{{
          text:'owner A',files:[],revision:0,
        }},0);
        S.session={{session_id:'owner-b',profile:'default'}};
        msg.value='owner B';
        const transitionB=_beginComposerOwnershipTransition('owner-b','default');
        const lifecycleB=_newComposerProducerHandle('voice-b');

        _composerSetText('late A','late A',null,lifecycleA);
        _bindComposerOwnershipDestination(transitionB,'owner-b-new','default');
        S.session={{session_id:'owner-b-new',profile:'default'}};
        msg.value='';
        _drainComposerOwnershipTransition(transitionB);
        _composerSetText('late B','late B',null,lifecycleB);

        Promise.resolve().then(()=>Promise.resolve()).then(()=>{{
          process.stdout.write(JSON.stringify({{text:msg.value,saves}}));
        }});
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["text"] == "late B"
    assert result["saves"][-1]["sid"] == "owner-a"
    assert result["saves"][-1]["text"] == "late A"


def test_behavior_harness_uses_fixed_literal_source_only():
    forbidden = "ev" + "al("  # keep the scanner token out of this harness too
    assert forbidden not in Path(__file__).read_text()


def test_programmatic_send_waits_for_new_session_owner_before_capturing_payload():
    start = MESSAGES_JS.index("async function send(){")
    duplicate_guard = MESSAGES_JS.index(
        "if(typeof _sendInProgress!=='undefined'&&_sendInProgress) return", start
    )
    transition_wait = MESSAGES_JS.index("await _newSessionInFlight", start)
    capture = MESSAGES_JS.index("_sendInProgress = true", start)

    assert duplicate_guard < transition_wait < capture, (
        "a duplicate Voice Mode callback must stop before waiting, while the owning "
        "send must wait for the session transition before capturing payload"
    )


def test_failed_new_session_keeps_old_session_composer_visible():
    result = _run_new_session_harness(fail_create=True)

    assert result["error"] == "create failed"
    assert result["activeSid"] == "old-session"
    assert result["saves"] == [
        {
            "sid": "old-session",
            "text": "draft owned by the old session",
            "files": [
                {"name": "private.pdf", "size": 42, "type": "application/pdf"}
            ],
        }
    ]
    assert result["value"] == "draft owned by the old session"
    assert result["pendingFileNames"] == ["private.pdf"]


def test_failed_new_session_reconciles_file_dropped_during_create_with_visible_tray():
    result = _run_new_session_harness(fail_create=True, late_file=True)

    assert result["error"] == "create failed"
    assert result["activeSid"] == "old-session"
    assert result["pendingFileNames"] == ["private.pdf", "late-audio.webm"]
    assert result["lateFileIdentity"] is True
    assert result["visibleTray"] == ["private.pdf", "late-audio.webm"], (
        "a retained staged file must be visible after New Chat creation aborts"
    )
    assert result["trayRenders"] >= 1
    assert result["sendButtonUpdates"] >= 1
    assert result["autoResizeCalls"] >= 1
