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


def _run_pending_file_ownership_harness() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the browser behavior harness")

    helper_source = json.dumps(_composer_draft_helpers())
    script = textwrap.dedent(
        f"""
        const draftHelpers = {helper_source};
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
        eval(draftHelpers);

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

    function_source = json.dumps(_new_session_function())
    initial_session = json.dumps(
        {"session_id": "old-session", "workspace": "/workspace", "message_count": 2}
        if has_session
        else None
    )
    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        const newSession = eval('(' + {function_source} + ')');

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
        function _saveComposerDraftNow(sid, text, files) {{
          saves.push({{ sid, text, files }});
          if(saves.length === {json.dumps(fail_save_on_call)}) {{
            return Promise.reject(new Error('draft save failed'));
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
          if(lateText !== null) msg.value = lateText;
          if({str(late_file).lower()}) S.pendingFiles.push(lateFile);
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


def test_source_reset_rejection_does_not_transfer_late_input_to_destination():
    source_text = "draft owned by the old session"
    result = _run_new_session_harness(
        fail_create=False,
        late_text=f"{source_text} voice addition",
        late_file=True,
        fail_save_on_call=2,
    )

    assert result["error"] == "draft save failed"
    assert result["createCalls"] == 1
    assert result["activeSid"] == "old-session"
    assert result["value"] == f"{source_text} voice addition"
    assert result["pendingFileNames"] == ["private.pdf", "late-audio.webm"]
    assert [save["sid"] for save in result["saves"]] == [
        "old-session",
        "old-session",
    ]


def test_first_send_without_previous_session_transfers_entire_composer():
    result = _run_new_session_harness(fail_create=False, has_session=False)

    assert result["activeSid"] == "new-session"
    assert result["value"] == "draft owned by the old session"
    assert result["pendingFileNames"] == ["private.pdf"]
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
    assert result["saves"][0]["sid"] == "old-session"
    assert result["saves"][0]["text"] == source_text
    assert result["saves"][1] == result["saves"][0], (
        "the source owner must be reset to its pre-transition snapshot"
    )
    assert result["saves"][2] == {
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

    source = json.dumps(_set_new_session_pending_function())
    script = textwrap.dedent(
        f"""
        const setPending = eval('(' + {source} + ')');
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

        setPending(true);
        const during = Object.fromEntries(ids.map(id => [id, elements[id].disabled]));
        setPending(false);
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

    source = json.dumps(_add_files_function())
    script = textwrap.dedent(
        f"""
        const addFiles = eval('(' + {source} + ')');
        const S = {{pendingFiles:[]}};
        const MAX_UPLOAD_BYTES = 1024;
        let trayRenders = 0;
        function renderTray() {{ trayRenders += 1; }}
        function _showUploadTooLarge() {{ throw new Error('unexpected large file'); }}
        let settle;
        let _newSessionInFlight = new Promise(resolve => {{ settle = resolve; }});
        _newSessionInFlight.then(() => {{ _newSessionInFlight = null; }});
        const file = {{name:'late.txt', size:4}};

        addFiles([file]);
        const immediate = S.pendingFiles.map(item => item.name);
        settle();
        setTimeout(() => {{
          process.stdout.write(JSON.stringify({{
            immediate,
            after: S.pendingFiles.map(item => item.name),
            trayRenders,
          }}));
        }}, 20);
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

    source = json.dumps(
        _function(
            SESSIONS_JS,
            "_queueComposerDraftWrite",
            "\n\nfunction _composerPendingFilesOwnerKey(",
        )
    )
    script = textwrap.dedent(
        f"""
        const _composerDraftWriteBySid = new Map();
        const queueWrite = eval('(' + {source} + ')');
        const order = [];
        let releaseFirst;
        const first = queueWrite('sid-1', () => {{
          order.push('first-start');
          return new Promise(resolve => {{
            releaseFirst = () => {{ order.push('first-finish'); resolve(); }};
          }});
        }});
        const second = queueWrite('sid-1', async () => {{ order.push('second-start'); }});
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

    source = json.dumps(
        _function(
            SESSIONS_JS,
            "_queueComposerDraftWrite",
            "\n\nfunction _composerPendingFilesOwnerKey(",
        )
    )
    script = textwrap.dedent(
        f"""
        const _composerDraftWriteBySid = new Map();
        const queueWrite = eval('(' + {source} + ')');
        const order = [];
        const first = queueWrite('sid-1', async () => {{
          order.push('first');
          throw new Error('write failed');
        }});
        const second = queueWrite('sid-1', async () => {{ order.push('second'); }});
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

    source = json.dumps(
        _function(
            SESSIONS_JS,
            "_saveComposerDraftNow",
            "\n\n// Restore composer draft from server",
        )
    )
    script = textwrap.dedent(
        f"""
        const saveNow = eval('(' + {source} + ')');
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
            await saveNow('strict-sid', 'draft', [], 'default', {{rejectOnError:true}});
          }} catch (_) {{ strictRejected = true; }}
          let softResolved = false;
          await saveNow('soft-sid', 'draft', [], 'default').then(() => {{ softResolved = true; }});
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


def test_late_draft_save_stays_inside_transition_before_auto_send_can_clear_it():
    start = SESSIONS_JS.index("if(lateComposerText||lateComposerFiles.length)")
    awaited_save = SESSIONS_JS.index("await _saveComposerDraftNow(", start)
    transition_tail = SESSIONS_JS.index("S._pendingSessionToolsets=null", start)

    assert awaited_save < transition_tail


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
