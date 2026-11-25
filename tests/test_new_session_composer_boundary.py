"""Behavioral regression coverage for the New Session composer boundary."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap

import pytest


ROOT = Path(__file__).parents[1]
SESSIONS_JS = ROOT.joinpath("static", "sessions.js").read_text(encoding="utf-8")
UI_JS = ROOT.joinpath("static", "ui.js").read_text(encoding="utf-8")
BOOT_JS = ROOT.joinpath("static", "boot.js").read_text(encoding="utf-8")
MESSAGES_JS = ROOT.joinpath("static", "messages.js").read_text(encoding="utf-8")
PANELS_JS = ROOT.joinpath("static", "panels.js").read_text(encoding="utf-8")
WORKSPACE_JS = ROOT.joinpath("static", "workspace.js").read_text(encoding="utf-8")
COMMANDS_JS = ROOT.joinpath("static", "commands.js").read_text(encoding="utf-8")
I18N_JS = ROOT.joinpath("static", "i18n.js").read_text(encoding="utf-8")


def _run_node_script(script: str, *, timeout: int = 30):
    """Run a generated harness from a temp file instead of ``node -e``.

    Each harness carries the whole module prelude, which no longer fits in a
    single argv entry: Linux rejects it with E2BIG (MAX_ARG_STRLEN) and Windows
    with WinError 206. A ``.cjs`` file has no such limit and runs identically.
    """
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", delete=False, encoding="utf-8", newline=chr(10)
    )
    try:
        handle.write(script)
        handle.close()
        return subprocess.run(
            [shutil.which("node"), handle.name],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def _function(source: str, name: str, next_marker: str) -> str:
    start = source.find(f"function {name}(")
    end = source.find(next_marker, start)
    assert start != -1 and end != -1, f"{name} function not found"
    return source[start:end].strip()


def _set_new_session_pending_function() -> str:
    return _function(SESSIONS_JS, "_setNewSessionPending", "\n\nasync function newSession(")


def _add_files_function() -> str:
    # addFiles() routes through the collision-safe pending-file helpers, so the
    # harness needs them too (the merged addFiles keeps master's rename logic).
    return "\n".join([
        _function(UI_JS, "_pendingFileUniqueName", "\n// A pending file's identity"),
        _function(UI_JS, "_pendingFileOrigName", "\nfunction addFiles"),
        _function(UI_JS, "addFiles", "\nconst _uploadPendingFilesProgressBySession"),
    ])


# Module-scope state the extracted newSession()/draft helpers touch. Declared
# with `var` here (and normalised to `var` everywhere else in this file) so a
# harness that also declares one of them stays legal, and the merged layer's
# bindings exist even when a test's own prelude predates them.
_NEW_SESSION_STATE = (
    "var _newSessionInFlight=null;\n"
    "var _newSessionRequest=null;\n"
    "var _loadingSessionId=null;\n"
    "var _loadSessionGeneration=0;\n"
)

_DRAFT_STATE = (
    "var _composerDraftLocalFallbackBySid=new Map();\n"
    "var _composerDraftLatestIntentSequenceBySid=new Map();\n"
    "var _composerDraftIntentSequence=0;\n"
    "var _composerDraftRevBySid=new Map();\n"
    "var _composerDraftLastPayloadBySid=new Map();\n"
)


def _new_session_function() -> str:
    start = SESSIONS_JS.find("async function newSession(")
    end = SESSIONS_JS.find("\n\n/**", start)
    assert start != -1 and end != -1, "newSession function not found"
    # The merged newSession() invalidates a superseded load at the pane hand-off
    # (#8356); a harness must declare that module state to run the source.
    return "var _loadingSessionId=null;\nvar _loadSessionGeneration=0;\n" + SESSIONS_JS[start:end]


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


def _voice_mode_block() -> str:
    sr = BOOT_JS.rindex("const SpeechRecognition=window.SpeechRecognition")
    start = BOOT_JS.rindex("(function(){", 0, sr)
    end = BOOT_JS.index("\n})();", BOOT_JS.index(
        "window._voiceModeImmediateSend=_voiceModeSend;")) + len("\n})();")
    owner_start = BOOT_JS.index("function _composerProducerOwnerState(")
    owner_end = BOOT_JS.index("\n\n(function(){", owner_start)
    return BOOT_JS[owner_start:owner_end] + "\n" + BOOT_JS[start:end]


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
        function _saveComposerDraftNow(sid,text,files,_contId,opts){{
          const profile=(opts&&opts.ownerProfile)||null;
          saves.push({{sid,text,files:[...(files||[])].map(f=>f.name),profile}});
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
          }}else if(schedule==='prompt-then-native'){{
            insertSavedPromptIntoComposer('saved prompt');
            msg.value='source draft typed';
            _composerRecordNativeText();
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
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def _run_pending_file_ownership_harness() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")

    helper_source = _composer_draft_helpers()
    script = textwrap.dedent(
        f"""
        {_composer_authority_helpers()}
        var _loadingSessionId = null;
        let trayRenders = 0;
        const apiCalls = [];
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
        function api(path) {{ apiCalls.push(path); return Promise.resolve({{}}); }}
        function _isReadOnlySession(session) {{
          return !!(session && (session.read_only || session.is_read_only));
        }}
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

          S.session = {{
            session_id:'read-only-session', profile:'default', read_only:true,
            composer_draft:{{text:'', files:[]}}
          }};
          _rememberComposerOwnerState('read-only-session','default',{{
            text:'browser-only draft',files:[oldFile],revision:1,
          }},1);
          S.pendingFiles = [];
          _restoreComposerDraft(S.session.composer_draft, 'read-only-session');
          const readOnlyRestored = {{
            text:msg.value,
            files:S.pendingFiles.map(file => file.name),
            exactFile:S.pendingFiles[0] === oldFile,
          }};
          const callsBeforeDebounce = apiCalls.length;
          _saveComposerDraft('read-only-session','edited browser-only draft',[oldFile]);
          await new Promise(resolve => setTimeout(resolve, _DRAFT_SAVE_DELAY_MS + 20));
          const readOnlyDraftPosts = apiCalls.slice(callsBeforeDebounce)
            .filter(path => path === '/api/session/draft').length;

          process.stdout.write(JSON.stringify({{
            fresh, restored, otherProfile, backgroundRestored, afterForget,
            readOnlyRestored,readOnlyDraftPosts,trayRenders
          }}));
        }})().catch(err => {{console.error(err); process.exit(1);}});
        """
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _run_new_session_harness(
    *,
    fail_create: bool,
    late_text: str | None = None,
    late_file: bool = False,
    late_file_batches: list[list[dict]] | None = None,
    has_session: bool = True,
    fail_save_on_call: int | None = None,
    read_only_source: bool = False,
) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")

    function_source = _new_session_function()
    authority_source = _composer_authority_helpers()
    add_files_source = _add_files_function()
    initial_session = json.dumps(
        {
            "session_id": "old-session",
            "workspace": "/workspace",
            "message_count": 2,
            "read_only": read_only_source,
        }
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
        function _isReadOnlySession(session) {{
          return !!(session && (session.read_only || session.is_read_only));
        }}
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
          const lateBatches = {json.dumps(late_file_batches or [])};
          for(const batch of lateBatches){{
            addFiles(batch.map(spec => new File([spec.content], spec.name, {{type: 'text/plain', lastModified: spec.mtime}})));
          }}
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
            pendingFileSizes: S.pendingFiles.map(file => file.size),
            pendingFileOrigNames: S.pendingFiles.map(file => (file && file._origName) || file.name),
            savedFileNames: saves.map(save => ({{
              sid: save.sid,
              files: (save.files || []).map(file => file && file.name),
            }})),
            lateFileIdentity: S.pendingFiles.includes(lateFile),
            sourceFileIdentity: S.pendingFiles.includes(pendingFile),
            visibleTray,
            trayRenders,
            sendButtonUpdates,
            autoResizeCalls,
            createCalls,
            saves,
            rememberedSource: _composerRememberedOwnerSnapshot('old-session','default'),
          }}));
        }})().catch(err => {{ console.error(err); process.exit(1); }});
        """
    )
    proc = _run_node_script(script, timeout=30)
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
        function _sessionLeftScreen(){{return false;}}
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
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def test_read_only_source_new_chat_keeps_browser_draft_without_server_post():
    result = _run_new_session_harness(
        fail_create=False,
        read_only_source=True,
    )

    assert result["error"] is None
    assert result["createCalls"] == 1
    assert result["activeSid"] == "new-session"
    assert result["saves"] == []
    assert result["rememberedSource"]["text"] == "draft owned by the old session"
    assert [file["name"] for file in result["rememberedSource"]["files"]] == [
        "private.pdf"
    ]


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
    proc = _run_node_script(script, timeout=30)
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
    proc=_run_node_script(script, timeout=30)
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


def _run_queued_workspace_supersession_harness() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    script = textwrap.dedent(
        f"""
        {_wait_for_new_session_navigation_function()}
        {_blank_page_mint_function("switchToWorkspace")}
        function deferred(){{let resolve;const promise=new Promise(r=>{{resolve=r;}});return {{promise,resolve}};}}
        const pending=deferred();
        const heldIntent=_claimContextTransition('held-new-session');
        _newSessionInFlight=(async()=>{{
          await heldIntent.previous;
          try{{return await pending.promise;}}
          finally{{_newSessionInFlight=null;heldIntent.release();}}
        }})();
        const S={{session:{{session_id:'source',workspace:'/workspace-a'}},messages:[{{role:'user'}}],busy:false,_profileSwitchWorkspace:null}};
        const window={{_newChatOnWorkspaceSwitch:true}};
        const $=()=>null;
        let newSessionCalls=0;
        async function newSession(){{newSessionCalls+=1;S.session={{session_id:'workspace-b',workspace:S._profileSwitchWorkspace}};}}
        function closeWsDropdown(){{}} function showToast(){{}} function setStatus(){{}}
        function t(k){{return k;}} function getWorkspaceFriendlyName(p){{return p;}}
        (async()=>{{
          const switching=switchToWorkspace('/workspace-b','B');
          const workspaceClaim=_paneNavigationGeneration;
          const sidebarClaim=_claimPaneNavigation();
          S.session={{session_id:'sidebar-c',workspace:'/workspace-c'}};S.messages=[{{role:'user'}}];
          pending.resolve({{superseded:true}});
          await switching;
          process.stdout.write(JSON.stringify({{workspaceClaim,sidebarClaim,newSessionCalls,activeSid:S.session.session_id,workspace:S.session.workspace}}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def test_queued_workspace_switch_cannot_replace_newer_sidebar_navigation():
    result = _run_queued_workspace_supersession_harness()
    assert result["sidebarClaim"] > result["workspaceClaim"]
    assert result["newSessionCalls"] == 0
    assert result["activeSid"] == "sidebar-c"
    assert result["workspace"] == "/workspace-c"


def _run_queued_workspace_path_supersession_harness(*, supersede: bool) -> dict:
    """A blank-page workspace-path action queued behind a held switch must not
    mint a session after the user has picked a sidebar conversation."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    script = textwrap.dedent(
        f"""
        {_wait_for_new_session_navigation_function()}
        {_blank_page_mint_function("promptWorkspacePath")}
        function deferred(){{let resolve;const promise=new Promise(r=>{{resolve=r;}});return {{promise,resolve}};}}
        async function spinUntil(predicate){{for(let i=0;i<200;i++){{if(predicate())return;await Promise.resolve();}}throw new Error('timeout');}}
        const held=_claimContextTransition('held-profile-switch');
        const S={{session:null,messages:[],activeProfile:'default',_profileDefaultWorkspace:'/workspace-a',busy:false}};
        const window={{}};
        let newSessionCalls=0;let promptCalls=0;
        async function newSession(){{newSessionCalls+=1;S.session={{session_id:'unwanted',workspace:'/workspace-a'}};}}
        function showPromptDialog(){{promptCalls+=1;return Promise.resolve(null);}}
        function showToast(){{}} function setStatus(){{}} function t(k){{return k;}}
        (async()=>{{
          const action=promptWorkspacePath();
          for(let i=0;i<5;i++)await Promise.resolve();
          let sidebarClaim=null;
          if({str(supersede).lower()}){{
            // The user picks a sidebar conversation while the held switch is
            // still running. Its load is still in flight (S.session has not
            // moved yet), so only the newer pane claim can protect it from a
            // queued mint that would install a fresh chat over it.
            sidebarClaim=_claimPaneNavigation();
          }}
          held.release();
          await action;
          process.stdout.write(JSON.stringify({{
            pinnedWorkspaceClaim:_paneNavigationGeneration,
            sidebarClaim,newSessionCalls,promptCalls,
            activeSid:S.session&&S.session.session_id,
          }}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def test_queued_workspace_path_cannot_replace_newer_sidebar_navigation():
    result = _run_queued_workspace_path_supersession_harness(supersede=True)
    assert result["sidebarClaim"] > 0
    assert result["newSessionCalls"] == 0, (
        "a queued blank-page workspace action must not mint a session once a "
        "newer sidebar navigation owns the pane"
    )
    assert result["activeSid"] is None
    assert result["promptCalls"] == 0


def test_workspace_path_still_mints_when_nothing_superseded():
    result = _run_queued_workspace_path_supersession_harness(supersede=False)
    assert result["newSessionCalls"] == 1, (
        "the click-time pane claim must not block the blank-page mint when no "
        "newer navigation happened"
    )
    assert result["activeSid"] == "unwanted"



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
    proc=_run_node_script(script, timeout=30)
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


def _run_profile_double_context_harness(*, reject_second: bool = False) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    script = textwrap.dedent(
        f"""
        {_wait_for_new_session_navigation_function()}
        {_switch_to_profile_function()}
        function deferred(){{let resolve,reject;const promise=new Promise((r,j)=>{{resolve=r;reject=j;}});return {{promise,resolve,reject}};}}
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
          if(name==='default')d.resolve({{active:'default',is_default:true,default_model:null,default_workspace:null}});
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
        function _newSessionResultWasSuperseded(){{return false;}}
        async function newSession(){{
          S.session={{session_id:'replacement',profile:S.activeProfile,workspace:null}};
          S.messages=[];
          return {{status:'committed',session:S.session}};
        }}
        (async()=>{{
          const first=switchToProfile('beta');
          await spinUntil(()=>switches.length===1);
          const second=switchToProfile('gamma');
          for(let i=0;i<20;i++)await Promise.resolve();
          const beforeFirstSettle=switches.map(x=>x.name);
          switches[0].d.resolve({{active:'beta',is_default:false,default_model:null,default_workspace:null}});
          await spinUntil(()=>switches.length===3);
          const betweenSettles=switches.map(x=>x.name);
          if({str(reject_second).lower()})switches[2].d.reject(new Error('gamma failed'));
          else switches[2].d.resolve({{active:'gamma',is_default:false,default_model:null,default_workspace:null}});
          await Promise.all([first,second]);
          process.stdout.write(JSON.stringify({{beforeFirstSettle,betweenSettles,finalProfile:S.activeProfile,sessionProfile:S.session.profile,switches:switches.map(x=>x.name)}}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc=_run_node_script(script, timeout=30)
    assert proc.returncode==0,proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def _run_new_session_load_interleave_harness(
    *, fail_create: bool, clarify_block: bool = False,
    revisit_read_only_source: bool = False,
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

        var _loadingSessionId = null;
        var _loadSessionGeneration = 0;
        var _composerDraftLocalFallbackBySid = new Map();
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
        const targetSid = {json.dumps('session-a' if revisit_read_only_source else 'session-b')};
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
            message_count:1,read_only:{str(revisit_read_only_source).lower()},
            composer_draft:{{text:'draft A',files:[]}}
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
          if(path===`/api/session?session_id=${{targetSid}}&messages=0&resolve_model=0`){{
            return metadata.promise;
          }}
          if(path===`/controlled/messages/${{targetSid}}`) return messages.promise;
          throw new Error(`unexpected API call: ${{path}}`);
        }}
        function _saveComposerDraftNow(sid,text,files,_contId,opts){{
          const profile=(opts&&opts.ownerProfile)||null;
          saves.push({{sid,text,files:[...(files||[])].map(file=>file.name),profile}});
          return Promise.resolve();
        }}
        async function _ensureMessagesLoaded(sid){{
          const data=await api(`/controlled/messages/${{sid}}`);
          S.messages=data.session.messages||[];
          S.toolCalls=data.session.tool_calls||[];
        }}
        function _composerDraftHasPayload(text,files){{return !!(text||(files&&files.length));}}
        function _isReadOnlySession(session){{return !!(session&&(session.read_only||session.is_read_only));}}
        function _isComposerDraftRestoreSuppressed(){{return false;}}
        function _clearComposerDraftRestoreSuppression(){{}}
        function _restoreComposerPendingFiles(){{S.pendingFiles=[];}}
        function _peekComposerPendingFiles(){{return [];}}
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
          session_id:targetSid,profile:'default',workspace:'/workspace-b',
          read_only:{str(revisit_read_only_source).lower()},messages:[],
          composer_draft:{'{text:"",files:[]}' if revisit_read_only_source else '{text:"draft B",files:[]}'},message_count:1,
          active_stream_id:null,
        }};

        (async()=>{{
          let newError=null;
          const creating=newSession().catch(error=>{{newError=error.message;}});
          await spinUntil(()=>apiCalls.includes('/api/session/new'));
          let loading={str(revisit_read_only_source).lower()}?null:_openSidebarSession(sessionB);
          for(let i=0;i<12;i++) await Promise.resolve();
          const loadStartedBeforeCreateSettled=apiCalls.includes(
            `/api/session?session_id=${{targetSid}}&messages=0&resolve_model=0`
          );
          const sidebarProfileStartedBeforeCreateSettled=sidebarProfileCalls.length>0;

          if(loadStartedBeforeCreateSettled){{
            metadata.resolve({{session:sessionB}});
            await spinUntil(()=>apiCalls.includes(`/controlled/messages/${{targetSid}}`));
            messages.resolve({{session:{{...sessionB,messages:[{{role:'assistant',content:'B'}}]}}}});
            await loading;
          }}

          if({str(clarify_block).lower()}){{
            _composerControlSetDisabledReason(msg,'clarify',true);
          }}
          if({str(fail_create).lower()}) create.reject(new Error('create failed'));
          else create.resolve({{session:createdSession}});
          await creating;

          if({str(revisit_read_only_source).lower()}){{
            loading=_openSidebarSession(sessionB);
          }}

          if(!loadStartedBeforeCreateSettled){{
            await spinUntil(()=>apiCalls.includes(
              `/api/session?session_id=${{targetSid}}&messages=0&resolve_model=0`
            ));
            metadata.resolve({{session:sessionB}});
            await spinUntil(()=>apiCalls.includes(`/controlled/messages/${{targetSid}}`));
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
        [node], input=script, cwd=ROOT, text=True, capture_output=True, timeout=30
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


def test_read_only_source_revisit_restores_exact_browser_owned_composer():
    result = _run_new_session_load_interleave_harness(
        fail_create=False,
        revisit_read_only_source=True,
    )

    assert result["activeSid"] == "session-a"
    assert result["text"] == "draft A"
    assert result["files"] == ["source-a.txt"]
    assert not any(
        save["sid"] == "session-a" for save in result["saves"]
    ), "a read-only source must remain browser-owned across New Chat and revisit"


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
    assert result["readOnlyRestored"] == {
        "text": "browser-only draft",
        "files": ["private.pdf"],
        "exactFile": True,
    }
    assert result["readOnlyDraftPosts"] == 0
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


def test_new_session_pending_freezes_only_the_new_chat_controls():
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
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["during"]["btnNewChat"] is True
    assert result["during"]["btnTitlebarNewChat"] is True
    # Unsent input must keep flowing: refusing keystrokes during the create is
    # the input-retention defect this boundary has to avoid, and every composer
    # producer buffers through the ownership transaction instead.
    for producer in ("msg", "fileInput", "btnAttach", "btnSavedPrompts", "btnVoiceMode"):
        assert result["during"][producer] is False, producer
    assert result["after"]["msg"] is False
    assert result["after"]["btnNewChat"] is False
    assert result["after"]["btnMic"] is True, "pre-existing disabled state must survive"


def _dictation_production_helpers() -> str:
    # The real producer, not a test double: _commitTranscript() is what the
    # composer mic calls when a transcript lands (server STT or browser SR).
    return _function(BOOT_JS, "_commitTranscript", "\n  function _isServerSttUnavailable")


def _run_dictation_harness(schedule: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    authority = _composer_authority_helpers()
    commit = _dictation_production_helpers()
    script = textwrap.dedent(
        f"""
        const ta={{value:'source draft'}};
        const S={{
          session:{{session_id:'old-session',profile:'default'}},
          pendingFiles:[],activeProfile:'default'
        }};
        const $=id=>id==='msg'?ta:null;
        {authority}
        {commit}
        let _dictationAppend=true;
        let _prefix='source draft';
        const window={{_micPendingSend:false}};
        function autoResize(){{}}
        function send(){{}}
        function showToast(){{}}
        function t(k){{return k;}}
        function renderTray(){{}}
        function _rememberComposerPendingFiles(){{}}
        const saves=[];
        function _saveComposerDraftNow(sid,text,files,cont,opts){{
          saves.push({{sid,text}});return Promise.resolve();
        }}
        const schedule={json.dumps(schedule)};
        const tx=schedule==='direct'
          ?null
          :_beginComposerOwnershipTransition('old-session','default',{{persistSourceDraft:true}});
        _commitTranscript('voice addition','source draft');
        const afterCommit=ta.value;
        if(tx){{
          _bindComposerOwnershipDestination(tx,'new-session','default');
          if(schedule==='settle'){{
            // The settle restores the destination draft, then drains the buffer.
            ta.value=''; S.pendingFiles=[];
            _drainComposerOwnershipTransition(tx);
          }}else{{
            _abortComposerOwnershipTransition(tx);
          }}
        }}
        process.stdout.write(JSON.stringify({{afterCommit,settled:ta.value,saves}}));
        """
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_dictation_commit_during_new_session_pending_is_retained():
    result = _run_dictation_harness("settle")

    # While the create is pending the transcript is buffered, not painted over
    # the source composer; the settle must land it in the new chat's composer.
    # Only the addition moves: the source chat keeps its own draft text, which
    # was already flushed at the create boundary.
    assert result["afterCommit"] == "source draft"
    assert result["settled"] == " voice addition"


def test_dictation_commit_without_a_transition_still_writes_the_composer():
    result = _run_dictation_harness("direct")

    assert result["afterCommit"] == "source draft voice addition"
    assert result["settled"] == "source draft voice addition"


def test_dictation_commit_is_restored_when_the_create_fails():
    result = _run_dictation_harness("abort")

    assert result["afterCommit"] == "source draft"
    assert result["settled"] == "source draft voice addition"
    source_saves = [save for save in result["saves"] if save["sid"] == "old-session"]
    assert source_saves and source_saves[-1]["text"] == "source draft voice addition"


def _run_native_input_harness(schedule: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const ta={{value:'source draft'}};
        const S={{
          session:{{session_id:'old-session',profile:'default'}},
          pendingFiles:[],activeProfile:'default'
        }};
        const $=id=>id==='msg'?ta:null;
        {authority}
        function renderTray(){{}}
        function autoResize(){{}}
        function updateSendBtn(){{}}
        const saves=[];
        function _saveComposerDraftNow(sid,text,files,cont,opts){{
          saves.push({{sid,text}});return Promise.resolve();
        }}
        const schedule={json.dumps(schedule)};
        const tx=_beginComposerOwnershipTransition('old-session','default',{{persistSourceDraft:true}});
        // A real keystroke/paste while the create request is held.
        ta.value='source draft typed';
        const recorded=_composerRecordNativeText();
        // A saved-prompt append dispatches a synthetic input event without
        // changing the textarea; it must not be mistaken for a keystroke.
        const recordedAgain=_composerRecordNativeText();
        _bindComposerOwnershipDestination(tx,'new-session','default');
        if(schedule==='settle'){{
          ta.value=''; S.pendingFiles=[];
          _drainComposerOwnershipTransition(tx);
        }}else{{
          _abortComposerOwnershipTransition(tx);
        }}
        process.stdout.write(JSON.stringify({{
          recorded,recordedAgain,settled:ta.value,saves
        }}));
        """
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_native_keystrokes_during_new_session_pending_survive_the_settle():
    result = _run_native_input_harness("settle")

    assert result["recorded"] is True
    # The addition is retained for the new chat; the pre-click text stays with
    # the source chat rather than leaking into the destination.
    assert result["settled"] == " typed"


def test_synthetic_producer_input_event_is_not_recorded_as_a_keystroke():
    result = _run_native_input_harness("settle")

    assert result["recordedAgain"] is False


def test_native_keystrokes_are_restored_when_the_create_fails():
    result = _run_native_input_harness("abort")

    assert result["settled"] == "source draft typed"
    source_saves = [save for save in result["saves"] if save["sid"] == "old-session"]
    assert source_saves and source_saves[-1]["text"] == "source draft typed"


def test_dictation_and_voice_mode_writes_route_through_the_composer_transaction():
    commit = _dictation_production_helpers()
    assert "_composerCommitProducerText(committed,'dictation')" in commit
    assert commit.count("ta.value=committed") == 1
    else_index = commit.index("}else{")
    assert commit.index("_composerCommitProducerText(committed,'dictation')") > 0

    voice_start = BOOT_JS.index("_recognition.onresult=(event)=>{")
    voice_end = BOOT_JS.index("_recognition.onend=()=>{", voice_start)
    voice_result = BOOT_JS[voice_start:voice_end]
    assert "_composerCommitProducerText(final||interim,'voice-mode')" in voice_result
    assert voice_result.count("ta.value=final||interim") == 1
    assert voice_result.index("_composerCommitProducerText") < voice_result.index(
        "}else{\n        ta.value=final||interim"
    )

    send_start = BOOT_JS.index("function _voiceModeSend(){")
    send_end = BOOT_JS.index("function _speakResponse(){", send_start)
    assert "_composerPendingOrVisibleText()" in BOOT_JS[send_start:send_end]

    input_start = BOOT_JS.index("$('msg').addEventListener('input',()=>{")
    input_end = BOOT_JS.index("// #5514/#5515", input_start)
    input_handler = BOOT_JS[input_start:input_end]
    assert "_composerRecordNativeText()" in input_handler
    assert "if (!bufferedTransition && sid" in input_handler

    assert else_index > 0


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
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["immediate"] == []
    assert result["after"] == ["late.txt"]
    assert result["trayRenders"] == 1


def test_files_dropped_in_separate_events_during_new_session_pending_share_the_transition_buffer():
    """A second drop event must see the batch the first event buffered.

    While the create request is held the tray still shows the departing chat,
    so addFiles() stages into the ownership-transition buffer. The next event
    has to dedupe against that buffer and pick collision-free names from the
    same view; otherwise one file is staged twice and two different files both
    keep `repeat.txt`.
    """
    result = _run_new_session_harness(
        fail_create=False,
        late_file_batches=[
            [{"name": "repeat.txt", "content": "aaa", "mtime": 1}],
            [{"name": "repeat.txt", "content": "aaa", "mtime": 1}],
            [{"name": "repeat.txt", "content": "bbbb", "mtime": 2}],
        ],
    )

    assert result["activeSid"] == "new-session"
    assert result["pendingFileNames"] == ["repeat.txt", "repeat-2.txt"]
    assert result["pendingFileOrigNames"] == ["repeat.txt", "repeat.txt"]
    assert result["pendingFileSizes"] == [3, 4]
    destination_saves = [
        save for save in result["savedFileNames"] if save["sid"] == "new-session"
    ]
    assert destination_saves
    assert destination_saves[-1]["files"] == ["repeat.txt", "repeat-2.txt"]


def test_handoff_drop_of_a_file_still_pending_on_the_departing_chat_reaches_the_new_chat():
    """A file the departing chat already stages must still reach the new chat.

    During a held New Chat the visible tray belongs to the departing chat; a
    drop in that window is buffered for the destination. Deduping against the
    visible tray therefore swallowed a re-drop of the same file, and the chat
    being created ended up without the attachment.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    assert node is not None

    source = _add_files_function()
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const S = {{pendingFiles: []}};
        const MAX_UPLOAD_BYTES = 1024;
        let trayRenders = 0;
        const elements = {{msg: {{value: ''}}}};
        const $ = id => elements[id] || null;
        function renderTray() {{ trayRenders += 1; }}
        function _showUploadTooLarge() {{ throw new Error('unexpected large file'); }}
        {authority}
        {source}

        const sourceFile = {{name: 'shared.txt', size: 4, lastModified: 7}};
        S.pendingFiles = [sourceFile];
        const token = _beginComposerOwnershipTransition('old-session', 'default');
        addFiles([new File(['aaaa'], 'shared.txt', {{type: 'text/plain', lastModified: 7}})]);
        const bufferedAfterDuplicate = _pendingFilesBufferedByTransition().map(item => item.name);
        const sourceTrayBeforeDrain = S.pendingFiles.map(item => item.name);
        addFiles([new File(['bbbbbb'], 'shared.txt', {{type: 'text/plain', lastModified: 8}})]);
        const bufferedAfterCollision = _pendingFilesBufferedByTransition().map(item => item.name);
        // _restoreComposerDraft clears the departing tray before the drain.
        S.pendingFiles = [];
        _drainComposerOwnershipTransition(token);
        process.stdout.write(JSON.stringify({{
          bufferedAfterDuplicate,
          bufferedAfterCollision,
          sourceTrayBeforeDrain,
          settled: S.pendingFiles.map(item => item.name),
          origNames: S.pendingFiles.map(item => (item && item._origName) || item.name),
        }}));
        """
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["bufferedAfterDuplicate"] == ["shared.txt"], (
        "a drop during the handoff must not be skipped because the departing "
        "chat still stages the same file"
    )
    assert result["bufferedAfterCollision"] == ["shared.txt", "shared-2.txt"]
    assert result["sourceTrayBeforeDrain"] == ["shared.txt"]
    assert result["settled"] == ["shared.txt", "shared-2.txt"]
    assert result["origNames"] == ["shared.txt", "shared.txt"]


def test_pending_file_validation_uses_one_view_for_ordinary_within_batch_and_transition():
    """Ordinary, within-batch and transition drops share the same rules.

    The transition case is the regression: two separate drop events during a
    held New Chat handoff must dedupe and rename against the buffered view
    while preserving File identity, contents and ordering through the drain.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")

    source = _add_files_function()
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        const S = {{pendingFiles: []}};
        const MAX_UPLOAD_BYTES = 1024 * 1024;
        let trayRenders = 0;
        const elements = {{msg: {{value: ''}}}};
        const $ = id => elements[id] || null;
        function renderTray() {{ trayRenders += 1; }}
        function _showUploadTooLarge() {{ throw new Error('unexpected large file'); }}
        {authority}
        {source}

        (async () => {{
          const make = (content, name, mtime) =>
            new File([content], name, {{type: 'text/plain', lastModified: mtime}});

          // Ordinary (no handoff): separate-batch duplicate skipped and a
          // distinct file with a taken name renamed, exactly as before.
          const ordinaryFirst = make('aaa', 'repeat.txt', 1);
          addFiles([ordinaryFirst]);
          addFiles([make('aaa', 'repeat.txt', 1)]);
          addFiles([make('bbbb', 'repeat.txt', 2)]);
          const ordinary = {{
            names: S.pendingFiles.map(file => file.name),
            identity: S.pendingFiles[0] === ordinaryFirst,
          }};
          S.pendingFiles = [];

          // Within one batch: the clone is dropped, the alias is allocated.
          addFiles([
            make('aaa', 'repeat.txt', 1),
            make('aaa', 'repeat.txt', 1),
            make('bbbb', 'repeat.txt', 2),
          ]);
          const withinBatch = S.pendingFiles.map(file => file.name);
          S.pendingFiles = [];

          // Held New Chat: batches land in the transition buffer. The second
          // and third events must see the first batch and each other.
          const token = _beginComposerOwnershipTransition('old-session', 'default');
          const first = make('aaa', 'repeat.txt', 1);
          addFiles([first]);
          addFiles([make('aaa', 'repeat.txt', 1)]);
          addFiles([make('bbbb', 'repeat.txt', 2)]);
          const visibleWhilePending = S.pendingFiles.map(file => file.name);
          _drainComposerOwnershipTransition(token);
          const drained = S.pendingFiles.map(file => file.name);
          const contents = [];
          for (const file of S.pendingFiles) contents.push(await file.text());
          process.stdout.write(JSON.stringify({{
            ordinary, withinBatch, visibleWhilePending, drained, contents,
            identity: S.pendingFiles[0] === first,
            origNames: S.pendingFiles.map(file => (file && file._origName) || file.name),
            trayRenders,
          }}));
        }})().catch(error => {{ console.error(error); process.exit(1); }});
        """
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result["ordinary"] == {"names": ["repeat.txt", "repeat-2.txt"], "identity": True}
    assert result["withinBatch"] == ["repeat.txt", "repeat-2.txt"]
    assert result["visibleWhilePending"] == []
    assert result["drained"] == ["repeat.txt", "repeat-2.txt"]
    assert result["contents"] == ["aaa", "bbbb"]
    assert result["identity"] is True
    assert result["origNames"] == ["repeat.txt", "repeat.txt"]


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
    proc = _run_node_script(script, timeout=30)
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
        var _loadingSessionId=null;
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
    proc = _run_node_script(script, timeout=30)
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
    proc = _run_node_script(script, timeout=30)
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
    proc = _run_node_script(script, timeout=30)
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
    proc = _run_node_script(script, timeout=30)
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
        function _setComposerDraftLocalIntent(t,f){{return {{text:String(t||''),files:Array.isArray(f)?f:[],sequence:1}};}}
        var _composerDraftLocalFallbackBySid=new Map();
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
        function _bumpComposerDraftRevision() {{}}
        function _noteComposerDraftPayload() {{}}
        function _bumpComposerDraftRevision() {{}}
        function _noteComposerDraftPayload() {{}}
        function _queueComposerDraftWrite(_sid, write) {{ return Promise.resolve().then(write); }}
        function api() {{ return Promise.reject(new Error('draft endpoint unavailable')); }}
        function _postComposerDraftPayload() {{ return Promise.reject(new Error('draft endpoint unavailable')); }}
        (async () => {{
          let strictRejected = false;
          try {{
            await _saveComposerDraftNow('strict-sid', 'draft', [], 'default', {{rejectOnError:true}});
          }} catch (_) {{ strictRejected = true; }}
          let softResolved = false;
          await _saveComposerDraftNow('soft-sid', 'draft', [], 'default', {{}}).then(() => {{ softResolved = true; }});
          process.stdout.write(JSON.stringify({{strictRejected, softResolved}}));
        }})().catch(error => {{ console.error(error); process.exit(1); }});
        """
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert json.loads(proc.stdout) == {"strictRejected": True, "softResolved": True}


def test_all_draft_post_paths_use_the_per_session_write_queue():
    """Every draft POST joins one per-session chain.

    The merged draft layer funnels the autosave, the immediate save, the
    submit-clear and the compare-and-clear through the single
    ``_queueComposerDraftWrite`` primitive, so the invariant is stronger than
    counting individual call sites.
    """
    assert SESSIONS_JS.count("function _queueComposerDraftWrite(") == 1
    assert "_queueComposerDraftWrite(targetSid, () =>" in SESSIONS_JS
    assert "_queueComposerDraftWrite(sid, () => api('/api/session/draft'" in SESSIONS_JS
    assert "_composerDraftWriteBySid" in SESSIONS_JS
    assert "enqueue(sid,()=>api('/api/session/draft'" not in SESSIONS_JS


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
    proc = _run_node_script(script, timeout=30)
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
        function _saveComposerDraftNow(sid,text,files,_contId,opts){{
          const profile=(opts&&opts.ownerProfile)||null;
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
    proc = _run_node_script(script, timeout=30)
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
        # A keystroke after a buffered saved prompt is another producer, not a
        # replacement of the whole destination buffer: both must survive.
        ("prompt-then-native", "saved prompt\n\n typed"),
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
        function _saveComposerDraftNow(sid,text,files,_contId,opts){{
          const profile=(opts&&opts.ownerProfile)||null;
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
    proc = _run_node_script(script, timeout=30)
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
    proc = _run_node_script(script, timeout=30)
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
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"afterNewSession": True, "afterClarify": False}


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
        function _setComposerDraftLocalIntent(t,f){{return {{text:String(t||''),files:Array.isArray(f)?f:[],sequence:1}};}}
        var _composerDraftLocalFallbackBySid=new Map();
        const S={{session:{{session_id:'sid-a',profile:'work'}},activeProfile:'work'}};
        function _clearComposerDraftSaveTimer(){{}}
        function _bumpComposerDraftRevision(){{}}
        function _noteComposerDraftPayload(){{}}
        const remembered=[];
        function _rememberComposerPendingFiles(){{}}
        function _composerDraftFilesForPersist(files){{return files.map(file=>file.name);}}
        function _composerDraftHasPayload(text,files){{return !!(text||files.length);}}
        function _clearComposerDraftRestoreSuppression(){{}}
        function _sessionComposerDraftHasPayload(){{return false;}}
        const _composerDraftKnownPayloadSessions=new Set();
        function _queueComposerDraftWrite(_sid,write){{return Promise.resolve().then(write);}}
        function _postComposerDraftPayload(){{return Promise.resolve();}}
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
    proc = _run_node_script(script, timeout=30)
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
        function _setComposerDraftLocalIntent(t,f){{return {{text:String(t||''),files:Array.isArray(f)?f:[],sequence:1}};}}
        var _composerDraftLocalFallbackBySid=new Map();
        const msg={{value:''}};
        const S={{
          session:{{session_id:'old-session',profile:'default',composer_draft:{{}}}},
          activeProfile:'default',pendingFiles:[]
        }};
        const $=id=>id==='msg'?msg:null;
        function _clearComposerDraftSaveTimer(){{}}
        function _bumpComposerDraftRevision(){{}}
        function _noteComposerDraftPayload(){{}}
        const _composerDraftKnownPayloadSessions=new Set();
        const writes=[];
        function _rememberComposerPendingFiles(){{}}
        function _composerDraftFilesForPersist(files){{return files.map(f=>f.name||f);}}
        function _composerDraftHasPayload(text,files){{return !!(text||files.length);}}
        function _clearComposerDraftRestoreSuppression(){{}}
        function _sessionComposerDraftHasPayload(){{return false;}}
        function _rememberComposerDraftPayloadState(){{}}
        function _queueComposerDraftWrite(_sid,write){{return Promise.resolve().then(write);}}
        function _postComposerDraftPayload(sid,draft,intent,contId){{
          return _queueComposerDraftWrite(sid,()=>api('/api/session/draft',{{
            method:'POST',
            body:JSON.stringify({{
              session_id:sid,text:(draft&&draft.text)||'',files:(draft&&draft.files)||[],
              goal_continuation_id:contId||''
            }})
          }}));
        }}
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
        _saveComposerDraftNow('old-session','r2',[],'',{{ownerProfile:'default'}}).then(()=>{{
          S.session={{session_id:'new-session',profile:'default',composer_draft:{{}}}};
          msg.value='';
          if(_composerOwnerSnapshotIsCurrent(r1Snapshot)){{
            return _saveComposerDraftNow(
              'old-session',r1Snapshot.text,r1Snapshot.files,'',{{ownerProfile:r1Snapshot.profile}}
            );
          }}
        }}).then(()=>{{
          process.stdout.write(JSON.stringify({{
            writes,current:_composerRememberedOwnerSnapshot('old-session','default')
          }}));
        }}).catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = _run_node_script(script, timeout=30)
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
    proc = _run_node_script(script, timeout=30)
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
        function _saveComposerDraftNow(sid,text,files,_contId,opts){{
          const profile=(opts&&opts.ownerProfile)||null;
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
    proc = _run_node_script(script, timeout=30)
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
        function _saveComposerDraftNow(sid,text,files,_contId,opts){{
          const profile=(opts&&opts.ownerProfile)||null;
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
    proc = _run_node_script(script, timeout=30)
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
        function _saveComposerDraftNow(sid,text,files,_contId,opts){{
          const profile=(opts&&opts.ownerProfile)||null;
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
    proc = _run_node_script(script, timeout=30)
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
        function _saveComposerDraftNow(sid,text,files,_contId,opts){{
          const profile=(opts&&opts.ownerProfile)||null;
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
    proc = _run_node_script(script, timeout=30)
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
    proc = _run_node_script(script, timeout=30)
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
    proc = _run_node_script(script, timeout=30)
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
    assert "deletedIds.forEach(_forgetComposerOwnerState)" in SESSIONS_JS


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
        function _saveComposerDraftNow(sid,text,files,_contId,opts){{
          const profile=(opts&&opts.ownerProfile)||null;
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
    proc = _run_node_script(script, timeout=30)
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
        "if(typeof _sendInProgress!=='undefined'&&_sendInProgress", start
    )
    transition_wait = MESSAGES_JS.index("await _newSessionInFlight", start)
    capture = MESSAGES_JS.index("_sendInProgress = true", start)

    assert duplicate_guard < transition_wait < capture, (
        "a duplicate Voice Mode callback must stop before waiting, while the owning "
        "send must wait for the session transition before capturing payload"
    )


def test_programmatic_send_stops_when_new_session_creation_rejects():
    start = MESSAGES_JS.index("async function send(){")
    wait = MESSAGES_JS.index("await _newSessionInFlight", start)
    capture = MESSAGES_JS.index("_sendInProgress = true", wait)
    boundary = MESSAGES_JS[wait:capture].replace(" ", "")
    assert "catch(_){return;}" in boundary, (
        "a failed New Chat must not let Send continue into the restored old session"
    )


def test_first_send_keeps_text_typed_after_its_snapshot():
    send_start = MESSAGES_JS.index("async function send(){")
    body = MESSAGES_JS[send_start:]
    # The pre-create snapshot is what separates the submitted message from
    # input that arrives while /api/session/new is in flight.
    assert "const _composerTextBeforeCreate=" in body
    assert "let _composerTypedDuringCreate='';" in body
    assert "if(_createdSessionForThisSend){" in body
    # The surviving text goes back into the textarea instead of being cleared.
    assert "$('msg').value=_composerTypedDuringCreate;" in body
    # Its draft is the newer text; the submitted draft is not cleared over it.
    assert "if (_composerTypedDuringCreate && typeof _saveComposerDraftNow === 'function') {" in body
    assert "} else if (activeSid && typeof _clearComposerDraft === 'function') {" in body


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


def test_failed_new_session_requeues_drained_item_at_front_without_restamping():
    start = UI_JS.index("function setBusy(v){")
    end = UI_JS.index("\n\n// ── Queue chip display", start)
    settle = UI_JS[start:end]

    # Four requeue paths: session switched away, session-owner handoff, send
    # still in flight, and newer composer content during the settle window.
    assert settle.count("_getSessionQueue(sid,true).unshift(next)") == 4
    assert "queueSessionMessage(sid,next)" not in settle
    assert settle.count("_persistSessionQueueStorage(sid,_getSessionQueue(sid,false))") == 4


def test_failed_new_session_resumes_queue_after_clearing_inflight_guard():
    new_session = _new_session_function()
    clear = new_session.index("_newSessionInFlight=null")
    resume = new_session.index("\n        setBusy(false);", clear)

    assert clear < resume
    boundary = new_session[clear:resume]
    assert "focusRestoredComposerAfterAbort" in boundary
    assert "_composerOwnerIsVisible" in boundary
    assert "!S.busy" in boundary


def test_queue_drain_does_not_replace_a_restored_unsent_composer():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    start = UI_JS.index("function setBusy(v){")
    end = UI_JS.index("\n\n// ── Queue chip display", start)
    set_busy = UI_JS[start:end]
    script = textwrap.dedent(
        f"""
        const msg={{value:'restored source draft'}};
        const S={{busy:true,session:{{session_id:'source'}},pendingFiles:[]}};
        let _queueDrainSid='source';
        let shifts=0;
        function $(id){{return id==='msg'?msg:null;}}
        function updateSendBtn(){{}}
        function setStatus(){{}}
        function setComposerStatus(){{}}
        function updateQueueBadge(){{}}
        function shiftQueuedSessionMessage(){{shifts++;return {{text:'queued turn'}};}}
        {set_busy}
        setBusy(false);
        process.stdout.write(JSON.stringify({{shifts,text:msg.value,drainSid:_queueDrainSid}}));
        """
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "shifts": 0, "text": "restored source draft", "drainSid": None,
    }


def test_queue_drain_resumes_once_after_restored_composer_is_cleared():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    start = UI_JS.index("function setBusy(v){")
    end = UI_JS.index("\n\n// ── Queue chip display", start)
    queue_settlement = UI_JS[start:end]
    script = textwrap.dedent(
        f"""
        const msg={{value:'restored source draft'}};
        const S={{busy:true,session:{{session_id:'source'}},pendingFiles:[]}};
        let _queueDrainSid='source';
        const queued=[{{text:'queued turn'}}];
        let shifts=0,sends=0,timers=[];
        function $(id){{return id==='msg'?msg:null;}}
        function updateSendBtn(){{}}
        function setStatus(){{}}
        function setComposerStatus(){{}}
        function updateQueueBadge(){{}}
        function _getSessionQueue(){{return queued;}}
        function _persistSessionQueueStorage(){{}}
        function shiftQueuedSessionMessage(){{shifts++;return queued.shift()||null;}}
        function setTimeout(fn){{timers.push(fn);}}
        function _composerSetText(text){{msg.value=text;}}
        function _composerReplaceFiles(files){{S.pendingFiles=files;}}
        function autoResize(){{}}
        function renderTray(){{}}
        function send(){{sends++;S.busy=true;}}
        {queue_settlement}
        setBusy(false);
        msg.value='';
        _resumeQueuedSessionMessageIfComposerEmpty();
        _resumeQueuedSessionMessageIfComposerEmpty();
        const scheduled=timers.length;
        timers.shift()();
        process.stdout.write(JSON.stringify({{shifts,sends,scheduled,text:msg.value}}));
        """
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "shifts": 1, "sends": 1, "scheduled": 1, "text": "queued turn",
    }


def test_queue_drain_requeues_if_user_types_during_settle_window():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    start = UI_JS.index("function setBusy(v){")
    end = UI_JS.index("\n\n// ── Queue chip display", start)
    queue_settlement = UI_JS[start:end]
    script = textwrap.dedent(
        f"""
        const msg={{value:''}};
        const S={{busy:false,session:{{session_id:'source'}},pendingFiles:[]}};
        let _queueDrainSid='source';
        const queued=[{{text:'queued turn'}}];
        let sends=0,timers=[];
        function $(id){{return id==='msg'?msg:null;}}
        function updateSendBtn(){{}}
        function setStatus(){{}}
        function setComposerStatus(){{}}
        function updateQueueBadge(){{}}
        function _getSessionQueue(){{return queued;}}
        function _persistSessionQueueStorage(){{}}
        function shiftQueuedSessionMessage(){{return queued.shift()||null;}}
        function setTimeout(fn){{timers.push(fn);}}
        function _composerSetText(text){{msg.value=text;}}
        function _composerReplaceFiles(files){{S.pendingFiles=files;}}
        function autoResize(){{}}
        function renderTray(){{}}
        function send(){{sends++;}}
        {queue_settlement}
        setBusy(false);
        msg.value='newer draft';
        timers.shift()();
        process.stdout.write(JSON.stringify({{sends,text:msg.value,queue:queued.map(x=>x.text)}}));
        """
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "sends": 0, "text": "newer draft", "queue": ["queued turn"],
    }


def test_queue_resume_stays_paused_for_unsent_context_cards():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    start = UI_JS.index("function setBusy(v){")
    end = UI_JS.index("\n\n// ── Queue chip display", start)
    queue_settlement = UI_JS[start:end]
    script = textwrap.dedent(
        f"""
        const msg={{value:''}};
        const S={{busy:false,session:{{session_id:'source'}},pendingFiles:[]}};
        const window={{_hasPendingSelections:()=>true}};
        let _queueDrainSid='source',_sendInProgress=false;
        const queued=[{{text:'queued turn'}}];
        let shifts=0,sends=0,timers=[];
        function $(id){{return id==='msg'?msg:null;}}
        function _composerHasContent(){{
          return !!(msg.value.trim()||S.pendingFiles.length||window._hasPendingSelections());
        }}
        function updateSendBtn(){{}}
        function setStatus(){{}}
        function setComposerStatus(){{}}
        function updateQueueBadge(){{}}
        function _getSessionQueue(){{return queued;}}
        function _persistSessionQueueStorage(){{}}
        function shiftQueuedSessionMessage(){{shifts++;return queued.shift()||null;}}
        function setTimeout(fn){{timers.push(fn);}}
        function send(){{sends++;}}
        {queue_settlement}
        _resumeQueuedSessionMessageIfComposerEmpty();
        process.stdout.write(JSON.stringify({{shifts,sends,timers:timers.length,queue:queued.length}}));
        """
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "shifts": 0, "sends": 0, "timers": 0, "queue": 1,
    }


def test_queue_drain_requeues_if_file_upload_starts_during_settle_window():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    start = UI_JS.index("function setBusy(v){")
    end = UI_JS.index("\n\n// ── Queue chip display", start)
    queue_settlement = UI_JS[start:end]
    script = textwrap.dedent(
        f"""
        const msg={{value:''}};
        const S={{busy:false,session:{{session_id:'source'}},pendingFiles:[]}};
        let _queueDrainSid='source',_sendInProgress=false;
        const queued=[{{text:'queued turn'}},{{text:'second turn'}}];
        let sends=0,timers=[];
        function $(id){{return id==='msg'?msg:null;}}
        function _composerHasContent(){{return false;}}
        function updateSendBtn(){{}}
        function setStatus(){{}}
        function setComposerStatus(){{}}
        function updateQueueBadge(){{}}
        function _getSessionQueue(){{return queued;}}
        function _persistSessionQueueStorage(){{}}
        function shiftQueuedSessionMessage(){{return queued.shift()||null;}}
        function setTimeout(fn){{timers.push(fn);}}
        function send(){{sends++;}}
        {queue_settlement}
        setBusy(false);
        _sendInProgress=true;
        timers.shift()();
        process.stdout.write(JSON.stringify({{sends,queue:queued.map(x=>x.text)}}));
        """
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "sends": 0, "queue": ["queued turn", "second turn"],
    }


def test_removing_last_context_card_rechecks_paused_queue():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    remove_context = _function(
        MESSAGES_JS, "_removeNamedContextBlock", "\n\nfunction _clearPendingSelections"
    )
    script = textwrap.dedent(
        f"""
        let _pendingSelections=[{{id:'ctx-1'}}];
        let _selectionIdCounter=1;
        let renders=0,resumes=0;
        function _renderSelectionChips(){{renders++;}}
        function _resumeQueuedSessionMessageIfComposerEmpty(){{resumes++;}}
        {remove_context}
        _removeNamedContextBlock('ctx-1');
        process.stdout.write(JSON.stringify({{
          remaining:_pendingSelections.length,counter:_selectionIdCounter,renders,resumes
        }}));
        """
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "remaining": 0, "counter": 0, "renders": 1, "resumes": 1,
    }


def test_send_finally_releases_guard_before_resuming_owned_queue():
    send = _function(MESSAGES_JS, "send", "\n\nasync function startRegeneration")
    capture = send.index("const _completedSendSid=_sendInProgressSid;")
    release = send.index("_sendInProgress=false;", capture)
    clear_owner = send.index("_sendInProgressSid=null;", release)
    owner_guard = send.index("S.session.session_id===_completedSendSid", clear_owner)
    resume = send.index("_resumeQueuedSessionMessageIfComposerEmpty();", owner_guard)

    assert "_sendInProgressSid=S.session&&S.session.session_id||null;" in send
    assert capture < release < clear_owner < owner_guard < resume


def _run_queued_file_action_supersession_harness(
    entry: str, *, supersede: bool = True
) -> dict:
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
        let newSessionCalls=0;const apiCalls=[];let prompts=0;let toasts=0;
        const S={{session:null,messages:[],activeProfile:'default',_profileDefaultWorkspace:'/workspace-a',currentDir:'.',_dirCache:{{}},busy:false}};
        const $=()=>null;
        function api(path,options){{apiCalls.push(path);return Promise.resolve({{}});}}
        function showPromptDialog(){{prompts+=1;return Promise.resolve('created-item');}}
        function showConfirmDialog(){{return Promise.resolve(false);}}
        function showToast(){{toasts+=1;}} function setStatus(){{}} function t(k){{return k;}}
        function _workspacePathIsReadOnly(){{return false;}}
        function _sessionLeftScreen(){{return false;}}
        function _workspaceCreateTargetLabel(v){{return v;}}
        function _workspaceJoinTargetPath(d,n){{return d==='.'?n:`${{d}}/${{n}}`;}}
        function loadDir(){{return Promise.resolve(true);}} function openFile(){{}}
        async function newSession(){{newSessionCalls+=1;S.session={{session_id:'minted',profile:'default',workspace:'/workspace-a'}};}}
        (async()=>{{
          if({str(supersede).lower()}){{
            // Hold the queue the way a profile switch does, then pick a sidebar
            // conversation while the file action is still waiting for its turn.
            const held=_claimContextTransition('held-switch');
            const action={calls[entry]};
            await Promise.resolve();await Promise.resolve();
            const actionClaim=_paneNavigationGeneration;
            const sidebarClaim=_claimPaneNavigation();
            S.session={{session_id:'sidebar-c',profile:'default',workspace:'/workspace-c'}};
            held.release();
            await action;
            process.stdout.write(JSON.stringify({{actionClaim,sidebarClaim,newSessionCalls,apiCalls,prompts,toasts,activeSid:S.session&&S.session.session_id}}));
            return;
          }}
          await {calls[entry]};
          process.stdout.write(JSON.stringify({{actionClaim:null,sidebarClaim:null,newSessionCalls,apiCalls,prompts,toasts,activeSid:S.session&&S.session.session_id}}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


@pytest.mark.parametrize("entry", ["promptNewFile", "promptNewFolder"])
def test_queued_file_action_cannot_replace_newer_sidebar_navigation(entry):
    """A blank-page create queued behind a held transition must not take over
    the conversation the user picked while it waited."""
    result = _run_queued_file_action_supersession_harness(entry)

    assert result["sidebarClaim"] > result["actionClaim"]
    assert result["newSessionCalls"] == 0
    assert result["apiCalls"] == []
    assert result["prompts"] == 0
    assert result["toasts"] == 0
    assert result["activeSid"] == "sidebar-c"


@pytest.mark.parametrize("entry", ["promptNewFile", "promptNewFolder"])
def test_blank_page_file_action_still_mints_the_session_when_uncontested(entry):
    """The click-time claim must not stop the ordinary blank-page create."""
    result = _run_queued_file_action_supersession_harness(entry, supersede=False)

    assert result["newSessionCalls"] == 1
    assert result["prompts"] == 1
    assert result["activeSid"] == "minted"
    assert result["apiCalls"] == (
        ["/api/file/create"] if entry == "promptNewFile" else ["/api/file/create-dir"]
    )


def _run_bundle_resolve_harness(new_session_body: str, pane_after: str) -> dict:
    """Run the real resolveBundleCommand() against a stubbed newSession()."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    script = "\n".join([
        _function(
            SESSIONS_JS,
            "_newSessionResultWasSuperseded",
            "\n\nfunction _claimContextTransition(",
        ),
        _async_function(
            COMMANDS_JS,
            "resolveBundleCommand",
            "\nfunction _activeSlashCommandOffset(",
        ),
        "const calls=[];",
        "const S={session:null};",
        "async function renderSessionList(){}",
        "const api=async (url,options={})=>{calls.push({url,body:options.body?JSON.parse(options.body):null});return {message:'resolved'};};",
        "async function newSession(){",
        f"  {pane_after}",
        f"  {new_session_body}",
        "}",
        "(async()=>{",
        "  const result=await resolveBundleCommand('/bundle request');",
        "  process.stdout.write(JSON.stringify({result,calls,paneSid:S.session&&S.session.session_id}));",
        "})().catch(error=>{console.error(error);process.exit(1);});",
    ])
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def test_bundle_resolve_stops_when_the_new_session_was_superseded():
    """newSession() reports supersession as status:'superseded'; the old guard
    read a `superseded` boolean that never exists, so the bundle resolve ran
    against the pane that had replaced the abandoned create."""
    result = _run_bundle_resolve_harness(
        "return {status:'superseded',session:null};",
        "S.session={session_id:'sidebar-session'};",
    )

    assert result["result"] == {"superseded": True}
    assert result["calls"] == []
    assert result["paneSid"] == "sidebar-session"


def test_bundle_resolve_keeps_the_session_it_created():
    """The resolve must name the session this call created, not a newer pane."""
    result = _run_bundle_resolve_harness(
        "return {status:'committed',session:{session_id:'created-owner'}};",
        "S.session={session_id:'newer-pane'};",
    )

    assert result["result"] == {"message": "resolved"}
    assert result["calls"] == [
        {
            "url": "/api/commands/bundles/resolve",
            "body": {"command": "/bundle request", "session_id": "created-owner"},
        }
    ]
    assert result["paneSid"] == "newer-pane"


def _run_switch_file_detach_harness() -> dict:
    """Compose the real tray repaint and owner-file helpers to prove a chat
    switch keeps the departing chat's staged files restorable."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    script = "\n".join([
        "const _composerPendingFilesByOwner = new Map();",
        "const S={session:{session_id:'session-a',profile:'default'},activeProfile:'default',pendingFiles:[]};",
        "function $(id){return {innerHTML:'',classList:{add(){},remove(){}},appendChild(){}};}",
        "function updateSendBtn(){}",
        "function esc(value){return String(value);}",
        "function li(){return '';}",
        "function t(key){return key;}",
        "function _mediaKindForName(){return null;}",
        "const _IMAGE_EXTS=/\\.(png|jpe?g)$/i;",
        "const _SVG_EXTS=/\\.svg$/i;",
        "const URL={createObjectURL(){return 'blob:test';},revokeObjectURL(){}};",
        "const document={createElement(){return {className:'',dataset:{},innerHTML:'',appendChild(){},querySelector(){return {onclick:null};}};},querySelectorAll(){return [];}};",
        "function _composerRemoveFile(){}",
        "function _resumeQueuedSessionMessageIfComposerEmpty(){}",
        _function(SESSIONS_JS, "_composerPendingFileIsLive", "\nfunction _rememberComposerPendingFiles("),
        _function(SESSIONS_JS, "_composerPendingFilesOwnerKey", "\nfunction _clearComposerDraftSaveTimer("),
        _function(SESSIONS_JS, "_rememberComposerPendingFiles", "\nfunction _forgetComposerPendingFiles("),
        _function(SESSIONS_JS, "_detachComposerPendingFilesForSwitch", "\n\nasync function loadSession("),
        _function(SESSIONS_JS, "_peekComposerPendingFiles", "\nfunction _restoreComposerPendingFiles("),
        _function(SESSIONS_JS, "_restoreComposerPendingFiles", "\n// Bumped when a non-empty draft"),
        _function(UI_JS, "_syncComposerFiles", "\nfunction renderTray("),
        _function(UI_JS, "renderTray", "\nfunction _uploadTooLargeMessage("),
        "const staged=new File(['A'],'A.txt');",
        "S.pendingFiles=[staged];",
        "_rememberComposerPendingFiles('session-a',[staged]);",
        "_detachComposerPendingFilesForSwitch('session-a');",
        "const afterDetach=S.pendingFiles.length;",
        "const remembered=(_composerPendingFilesByOwner.get(_composerPendingFilesOwnerKey('session-a'))||[]).length;",
        "_restoreComposerPendingFiles('session-a');",
        "process.stdout.write(JSON.stringify({afterDetach,remembered,restored:S.pendingFiles.map(file=>file.name),sameObject:S.pendingFiles[0]===staged}));",
    ])
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def test_staged_files_survive_a_chat_switch_repaint():
    """Returning to a chat must restore the File objects staged there. The
    pre-switch clear repaints the tray while the old chat is still visible, and
    that repaint used to record the temporary empty list as its own files."""
    result = _run_switch_file_detach_harness()

    assert result["afterDetach"] == 0, (
        "the destination composer must not inherit the departing chat's chips"
    )
    assert result["remembered"] == 1, (
        "the departing chat must keep owning its staged files across the repaint"
    )
    assert result["restored"] == ["A.txt"]
    assert result["sameObject"] is True


def test_blank_page_session_mint_forwards_the_requested_workspace_explicitly():
    """A blank-page mint must pass its target workspace to newSession().

    newSession() merges an active project filter's bound workspace into the
    request and gives that explicit option priority over the one-shot
    S._profileSwitchWorkspace slot (see #4676's
    ``test_new_session_explicit_options_beat_active_project_bindings``). A
    blank-page New file / New folder action that only set the shared slot could
    therefore mint its session in the project's pinned workspace instead of the
    profile default it asked for.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    script = textwrap.dedent(
        f"""
        {_wait_for_new_session_navigation_function()}

        const S={{
          session:null,messages:[],pendingFiles:[],activeProfile:'work',
          _profileDefaultWorkspace:'/profile-default',_profileSwitchWorkspace:null,
          _pendingSessionToolsets:['tools'],busy:false,_dirCache:{{}},
        }};
        const calls=[];
        async function newSession(_flash,options){{
          calls.push(options||{{}});
          S.session={{
            session_id:'minted',profile:S.activeProfile,
            workspace:(options&&options.workspace)||null,message_count:0,
          }};
          return S.session;
        }}

        (async()=>{{
          const session=await _ensureBlankPageSession('/profile-default');
          process.stdout.write(JSON.stringify({{
            calls:calls.map(call=>call.workspace||null),
            toolsets:S._pendingSessionToolsets,
            workspace:session&&session.workspace,
          }}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)

    assert result["calls"] == ["/profile-default"]
    assert result["workspace"] == "/profile-default"
    # The blank-page mint must not carry the empty composer's staged toolsets.
    assert result["toolsets"] is None


def _run_transition_tray_remove_harness(profile: str) -> dict:
    """Compose the real tray repaint and ownership transaction, click a chip's
    remove button while a New Chat handoff is pending, then drain the handoff."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    script = "\n".join(
        [
            "const _composerPendingFilesByOwner = new Map();",
            f"const S={{session:{{session_id:'old-session',profile:{profile!r}}},activeProfile:{profile!r},pendingFiles:[]}};",
            "const tray={innerHTML:'',classList:{add(){},remove(){}},children:[],appendChild(child){this.children.push(child);}};",
            "function $(id){return tray;}",
            "function updateSendBtn(){}",
            "function esc(value){return String(value);}",
            "function li(){return '';}",
            "function t(key){return key;}",
            "function _mediaKindForName(){return null;}",
            r"const _IMAGE_EXTS=/\.(png|jpe?g)$/i;",
            r"const _SVG_EXTS=/\.svg$/i;",
            "const URL={createObjectURL(){return 'blob:test';},revokeObjectURL(){}};",
            (
                "const document={createElement(){const chip={className:'',dataset:{},"
                "innerHTML:'',appendChild(){},querySelector(){return chip;}};"
                "return chip;}};"
            ),
            "const saves=[];",
            "function _saveComposerDraftNow(sid,text,files,cont,opts){",
            "  saves.push({sid,text,files:files.map(file=>file.name),profile:opts&&opts.ownerProfile});",
            "  return Promise.resolve();",
            "}",
            "function autoResize(){}",
            "function _composerSetText(value){S.composerText=value;}",
            "function _composerReplaceFiles(files){S.pendingFiles=[...files];}",
            "function _composerAddFiles(files){S.pendingFiles=[...S.pendingFiles,...files];}",
            "function _resumeQueuedSessionMessageIfComposerEmpty(){}",
            _composer_authority_helpers(),
            _function(
                SESSIONS_JS,
                "_composerPendingFileIsLive",
                "\nfunction _rememberComposerPendingFiles(",
            ),
            _function(
                SESSIONS_JS,
                "_composerPendingFilesOwnerKey",
                "\nfunction _clearComposerDraftSaveTimer(",
            ),
            _function(
                SESSIONS_JS,
                "_rememberComposerPendingFiles",
                "\nfunction _forgetComposerPendingFiles(",
            ),
            _function(UI_JS, "_syncComposerFiles", "\nfunction renderTray("),
            _function(UI_JS, "renderTray", "\nfunction _uploadTooLargeMessage("),
            "const fileA=new File(['A'],'a.txt');",
            "const fileB=new File(['BE'],'b.txt');",
            "S.pendingFiles=[fileA,fileB];",
            "const token=_beginComposerOwnershipTransition('old-session',S.session.profile,{persistSourceDraft:true});",
            "renderTray();",
            "const chips=tray.children.slice();",
            "chips[0].onclick();",
            "const afterClick=S.pendingFiles.map(file=>file.name);",
            "_drainComposerOwnershipTransition(token);",
            "process.stdout.write(JSON.stringify({afterClick,saves,chips:chips.length}));",
        ]
    )
    proc = _run_node_script(script, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def test_file_removal_during_new_session_pending_edits_the_source_profile_draft():
    """Removing a chip during a New Chat handoff must edit the departing chat.

    While the create request is held the tray still belongs to the source. In a
    non-default profile the removal has to name that profile; otherwise the
    ownership transaction treats it as a destination change, keeps the file in
    the source draft, and returning to the chat restores the removed chip.
    """
    result = _run_transition_tray_remove_harness("work")

    assert result["chips"] == 2
    assert result["afterClick"] == ["b.txt"], (
        "the removed attachment must leave the visible tray immediately"
    )
    source_saves = [save for save in result["saves"] if save["sid"] == "old-session"]
    assert source_saves, "the source draft must be persisted when the handoff drains"
    assert source_saves[-1]["files"] == ["b.txt"], (
        "the source chat must not restore the removed attachment"
    )
    assert source_saves[-1]["profile"] == "work"
