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
        ("insertSavedPromptIntoComposer", "\n\nlet _savedPromptsCache"),
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
        {_new_session_function()}

        let _newSessionInFlight = null;
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
    initial_session = json.dumps(
        {"session_id": "old-session", "workspace": "/workspace", "message_count": 2}
        if has_session
        else None
    )
    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        {authority_source}
        {function_source}

        let _newSessionInFlight = null;
        let _sessionSourceFilter = 'webui';
        let _activeProject = null;
        const NO_PROJECT_FILTER = '__none__';
        let _messagesTruncated = false;
        let _oldestIdx = 0;
        const saves = [];
        let createCalls = 0;
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
          if ({str(fail_create).lower()}) throw new Error('create failed');
          const lateText = {json.dumps(late_text)};
          if(lateText !== null) {{
            const destinationText = lateText.replace('draft owned by the old session', '');
            _composerSetText(lateText, destinationText);
          }}
          if({str(late_file).lower()}) _composerAddFiles([lateFile]);
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
        function autoResize() {{}}
        function renderTray() {{}}
        function updateSendBtn() {{}}
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
        const _draftSaveTimer = null;
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
    start = SESSIONS_JS.index("S.session=data.session;S.messages=data.session.messages||[]")
    restore = SESSIONS_JS.index("_restoreComposerDraft(S.session.composer_draft)", start)
    drain = SESSIONS_JS.index("_drainComposerOwnershipTransition(composerTransition)", restore)
    first_await = SESSIONS_JS.index("await _saveComposerDraftNow(", drain)

    assert "await " not in SESSIONS_JS[restore:drain]
    assert restore < drain < first_await


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


def test_failed_handoff_replays_full_source_form_not_destination_delta():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")
    authority = _composer_authority_helpers()
    script = textwrap.dedent(
        f"""
        const msg={{value:'source draft'}};
        const S={{pendingFiles:[]}};
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
    assert "_composerAppendText(addition,null,producer,null,'block')" in MESSAGES_JS
    assert "_composerAddFiles(accepted)" in UI_JS
    assert "_composerRemoveFile(f,S.session&&S.session.session_id)" in UI_JS
    assert "const captureProducerHandle=_micComposerProducerToken" in BOOT_JS
    assert "const capturePrefixSnapshot=_prefix" in BOOT_JS
    assert "if(isCurrentProducer)_applyDeferredServerSttFlip()" in BOOT_JS
    assert "_transcribeBlob(blob,prefixSnapshot,captureProducerHandle)" in BOOT_JS


def test_superseded_media_callbacks_route_payload_but_cannot_send_current_composer():
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
        const ta={{value:''}};
        const S={{pendingFiles:[]}};
        const window={{_micPendingSend:true}};
        let sends=0;
        let resizes=0;
        let trayRenders=0;
        let toasts=0;
        const routed=[];
        class File {{
          constructor(_parts,name,options){{this.name=name;this.type=options.type;}}
        }}
        function _composerAddFiles(files,_sid,handle){{
          routed.push({{kind:'file',name:files[0].name,handle}});
        }}
        function _composerSetText(value,transition,_sid,handle){{
          routed.push({{kind:'text',value,transition,handle}});
        }}
        function renderTray(){{trayRenders++;}}
        function send(){{sends++;}}
        function autoResize(){{resizes++;}}
        function showToast(){{toasts++;}}
        function t(value){{return value;}}
        {helpers}
        (async()=>{{
          await _sendRawAudio({{type:'audio/webm'}},handleA);
          _commitTranscript('late transcript',undefined,handleA);
          process.stdout.write(JSON.stringify({{
            routed,sends,resizes,trayRenders,toasts,pendingSend:window._micPendingSend
          }}));
        }})().catch(error=>{{console.error(error);process.exit(1);}});
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["routed"][0]["kind"] == "file"
    assert result["routed"][0]["handle"] == {"producerToken": "A"}
    assert result["routed"][1] == {
        "kind": "text",
        "value": "late transcript",
        "transition": "late transcript",
        "handle": {"producerToken": "A"},
    }
    assert result["sends"] == 0
    assert result["resizes"] == 0
    assert result["trayRenders"] == 0
    assert result["toasts"] == 0
    assert result["pendingSend"] is True


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


def test_voice_mode_callback_uses_immutable_lifecycle_producer_token():
    start = BOOT_JS.index("function _startListening(){")
    end = BOOT_JS.index("\n  function _voiceModeSend(){", start)
    lifecycle = BOOT_JS[start:end]

    assert "const lifecycleProducerToken=" in lifecycle
    assert "null,lifecycleProducerToken" in lifecycle
    assert "_recognition=new SpeechRecognition()" in lifecycle
    assert "const lifecycleRecognition=_recognition" in lifecycle
    assert lifecycle.count("_recognition!==lifecycleRecognition") >= 3


def test_dictation_callback_keeps_lifecycle_local_text_and_handle():
    start = BOOT_JS.index("function _ensureSpeechRecognition(")
    end = BOOT_JS.index("\n\n  if(!_forceMediaRecorder)", start)
    lifecycle = BOOT_JS[start:end]

    assert "const lifecycleProducerHandle=" in lifecycle
    assert "let lifecycleFinalText=''" in lifecycle
    assert "let _prefixForLifecycle=_prefix" in lifecycle
    assert "if(recognition!==sr)return" in lifecycle


def test_late_dictation_lifecycle_a_callback_executes_with_handle_a_after_b_starts():
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
        lifecycleA.onresult(result('late-a'));
        lifecycleB.onresult(result('live-b'));
        process.stdout.write(JSON.stringify({{calls,finalText:_finalText,resizes}}));
        """
    )
    proc = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["calls"] == [
        {"value": "A: late-a", "transition": "late-a", "handle": {"producerToken": "A"}},
        {"value": "B: live-b", "transition": "live-b", "handle": {"producerToken": "B"}},
    ]
    assert result["finalText"] == "live-b"
    assert result["resizes"] == 1


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
        let _draftSaveTimer=null;
        const remembered=[];
        function clearTimeout(){{}}
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
        let _draftSaveTimer=null;
        const _composerDraftKnownPayloadSessions=new Set();
        const writes=[];
        function clearTimeout(){{}}
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
