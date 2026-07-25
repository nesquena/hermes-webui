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
          if({json.dumps(schedule)}==='failed-send'||{json.dumps(schedule)}==='failed-send-after-swap'){{
            resolveClear();
            await Promise.resolve();await Promise.resolve();await Promise.resolve();
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
    assert "_composerAddFiles([file],null,_micComposerProducerToken)" in BOOT_JS
    assert BOOT_JS.count("_composerSetText(") >= 5
    assert "_composerAppendText(addition,null,producer,null,'block')" in MESSAGES_JS
    assert "_composerAddFiles(accepted)" in UI_JS
    assert "_composerRemoveFile(f,S.session&&S.session.session_id)" in UI_JS


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
