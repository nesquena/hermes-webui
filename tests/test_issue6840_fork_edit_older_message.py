import json
import pathlib
import subprocess


ROOT = pathlib.Path(__file__).parents[1]
UI = ROOT / "static/ui.js"
I18N = ROOT / "static/i18n.js"
ROUTES = ROOT / "api/routes.py"


def _function_source(source, signature):
    start = source.index(signature)
    brace = source.index("{", start + len(signature))
    depth = 0
    quote = None
    escaped = False
    in_comment = False
    for pos in range(brace, len(source)):
        char = source[pos]
        if in_comment:
            if char == "\n":
                in_comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if source[pos : pos + 2] == "//":
            in_comment = True
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : pos + 1]
    raise AssertionError(f"unterminated function: {signature}")


def _run_node(scenario):
    source = UI.read_text(encoding="utf-8")
    sessions_source = (ROOT / "static/sessions.js").read_text(encoding="utf-8")
    script = f"""
{_function_source(sessions_source, "function _composerDraftHasPayload")}
{_function_source(sessions_source, "function _restoreComposerDraft(draft, targetSid, opts={})")}
let _loadingSessionId = null;
const _isComposerDraftRestoreSuppressed = () => false;
const _clearComposerDraftRestoreSuppression = () => {{}};
let _sendInProgress = false;
let _sendInProgressSid = null;
let _pendingSelections = [];
const startParentSend = () => {{ _sendInProgress = true; _sendInProgressSid = 'source'; }};
{_function_source(source, "function msgContent")}
{_function_source(source, "function _isRecoveryControlMessageText")}
{_function_source(source, "function _isRecoveryControlMessage(m)")}
{_function_source(source, "function _messageIsRenderable")}
const _isContextCompactionMessage = () => false;
const _isPreservedCompressionTaskListMessage = () => false;
const _messageHasReasoningPayload = () => false;
const _assistantMessageHasVisibleContent = () => false;
{_function_source(source, "function _lastUserMessageIndex")}
let _submitEditInFlight = false;
{_function_source(source, "async function submitEdit")}
const scenario = {json.dumps(scenario)};
const calls = [];
const submissions = [];
const childDrafts = {{}};
let savedDraft = null;
let confirmResult = scenario.confirm !== false;
let ensureCount = 0;
let activeSid = 'source';
const S = {{session: {{session_id: 'source', profile:'default', model:'m', model_provider:'p', workspace:'/w', read_only:!!scenario.readOnly}}, messages: scenario.messages.map(m => ({{content:"visible", ...m}})), pendingFiles:[], busy:false, activeProfile:'default'}};
let _oldestIdx = scenario.oldest || 0;
const _deliberateSessionModelPick = () => null;
const _isBranchableReadOnlySession = (session) => !!session.read_only && !!scenario.branchable;
const _reArmRecoveryPick = (...args) => calls.push(['rearm', ...args]);
const _ensureAllMessagesLoaded = async () => {{ ensureCount++; if(scenario.resetOldest) _oldestIdx = 0; if(scenario.sendDuring==='ensure') startParentSend(); if(ensureCount===2&&scenario.typeDuringEnsure) {{ await new Promise(resolve=>setTimeout(resolve,0)); composer.value='newer draft'; S.pendingFiles=[{{name:'draft.txt'}}]; _pendingSelections=[{{id:'selection-1',name:'Excerpt',text:'keep this context'}}]; }} }};
const showConfirmDialog = async () => {{ calls.push(['confirm']); if(scenario.sendDuring==='confirm') startParentSend(); return confirmResult; }};
const api = async (url, options) => {{ calls.push([url, JSON.parse(options.body)]); if(url.includes('/branch')) {{ childDrafts.child={{text:JSON.parse(options.body).draft_text||'',files:[]}}; if(scenario.branchFailure) throw new Error('branch unavailable'); if(scenario.switchAfterBranch) S.session={{session_id:'other'}}; if(scenario.sendDuring==='branch') startParentSend(); return {{session_id:'child'}}; }} if(scenario.sendDuring==='truncate') startParentSend(); return {{}}; }};
const loadSession = async (sid) => {{ calls.push(['load', sid]); if(scenario.loadFailure) {{ await Promise.resolve(); if(scenario.switchDuringLoad) {{ S.session={{session_id:'other'}}; composer.value='newer draft'; }} if(scenario.loadFailure==='throw') throw new Error('load unavailable'); setStatus('load failed'); return; }} S.session = scenario.contractMismatch ? {{session_id:sid, profile:'other', model:'m', model_provider:'p', workspace:'/w'}} : {{session_id:sid, profile:'default', model:'m', model_provider:'p', workspace:'/w'}}; if(scenario.sendDuring==='load') startParentSend(); if(scenario.modelDrift) S.session.model='normalized-model'; activeSid=sid; if(scenario.recovered) _restoreComposerDraft(childDrafts[sid],sid); }};
const renderSessionList = async () => {{ calls.push(['render-list']); if(scenario.typeDuringListRefresh) {{ await new Promise(resolve=>setTimeout(resolve,0)); composer.value='unconfirmed'; }} }};
const renderMessages = () => calls.push(['render']);
const send = async (options={{}}) => {{ const preserve=!!options.preserveComposerDraft; const text=preserve?String(options.textOverride||'').trim():composer.value.trim(); const files=preserve?[]:S.pendingFiles.map(file=>file.name); const selections=preserve?[]:_pendingSelections.map(selection=>selection.name); if(text) {{ calls.push(['send', S.session.session_id, text]); submissions.push({{text,files,selections}}); }} if(preserve) {{ if(composer.value===text) composer.value=''; }} else {{ composer.value=''; S.pendingFiles=[]; _pendingSelections=[]; }} }};
const showToast = (...args) => calls.push(['toast', ...args]);
const setStatus = (...args) => calls.push(['status', ...args]);
const autoResize = () => calls.push(['resize']);
const renderTray = () => {{}};
const _renderSelectionChips = () => {{}};
const _saveComposerDraftNow = async (sid, text, files) => {{ savedDraft={{sid,text,files:files.map(file=>file.name)}}; }};
const t = (key, value) => key + (value === undefined ? '' : ':' + value);
const composer = {{value:''}};
const $ = () => composer;
(async () => {{ if(scenario.concurrent) await Promise.all([submitEdit(scenario.index, 'edited'), submitEdit(scenario.index, 'edited')]); else await submitEdit(scenario.index, 'edited'); if(scenario.manualSend) await send(); if(scenario.recoverChild) {{ scenario.loadFailure=null; scenario.recovered=true; await loadSession('child'); await send(); }} console.log(JSON.stringify({{calls, session:S.session.session_id, draft:composer.value, childDrafts, submissions, pendingFiles:S.pendingFiles.map(file=>file.name), pendingSelections:_pendingSelections.map(selection=>selection.name), savedDraft}})); }})();
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_older_edit_forks_without_truncating_and_sends_once():
    result = _run_node({"index": 0, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]})
    assert [call[0] for call in result["calls"]] == [
        "confirm",
        "/api/session/branch",
        "load",
        "resize",
        "render-list",
        "rearm",
        "toast",
        "send",
    ]
    assert result["calls"][1][1] == {"session_id": "source", "keep_count": 0, "draft_text": "edited"}
    assert result["session"] == "child"


def test_latest_edit_keeps_existing_truncate_path():
    result = _run_node({"index": 2, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]})
    assert "/api/session/branch" not in [call[0] for call in result["calls"]]
    assert result["calls"][0][0] == "/api/session/truncate"
    assert result["calls"][0][1]["keep_count"] == 2
    assert [call[0] for call in result["calls"]].count("send") == 1


def test_paginated_latest_edit_compares_absolute_keep_count():
    result = _run_node(
        {"index": 2, "oldest": 3, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert "/api/session/branch" not in [call[0] for call in result["calls"]]
    assert result["calls"][0][0] == "/api/session/truncate"
    assert result["calls"][0][1]["keep_count"] == 5
    assert [call[0] for call in result["calls"]].count("send") == 1


def test_branchable_read_only_latest_edit_never_truncates():
    result = _run_node(
        {"index": 2, "readOnly": True, "branchable": True, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert "/api/session/truncate" not in [call[0] for call in result["calls"]]
    assert result["calls"][0][0] == "confirm"
    assert result["calls"][1][0] == "/api/session/branch"
    assert result["calls"][1][1]["keep_count"] == 2
    assert [call[0] for call in result["calls"]].count("send") == 1


def test_cancelled_older_edit_makes_no_request():
    result = _run_node(
        {"index": 0, "confirm": False, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert result["calls"] == [["confirm"]]


def test_send_starting_during_confirmation_aborts_older_edit():
    result = _run_node(
        {"index": 0, "sendDuring": "confirm", "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert [call[0] for call in result["calls"]] == ["confirm"]
    assert result["session"] == "source"


def test_send_starting_during_branch_request_does_not_load_child_or_resubmit():
    result = _run_node(
        {"index": 0, "sendDuring": "branch", "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert [call[0] for call in result["calls"]] == ["confirm", "/api/session/branch"]
    assert result["session"] == "source"


def test_send_starting_during_truncate_request_does_not_resubmit():
    result = _run_node(
        {"index": 2, "sendDuring": "truncate", "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert [call[0] for call in result["calls"]] == ["/api/session/truncate"]


def test_new_draft_during_child_history_load_does_not_replace_edit_send():
    result = _run_node(
        {"index": 0, "typeDuringEnsure": True, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert [call for call in result["calls"] if call[0] == "send"] == [["send", "child", "edited"]]
    assert result["draft"] == "newer draft"
    assert result["submissions"] == [{"text": "edited", "files": [], "selections": []}]
    assert result["pendingFiles"] == ["draft.txt"]
    assert result["pendingSelections"] == ["Excerpt"]
    assert result["savedDraft"] == {"sid": "child", "text": "newer draft", "files": ["draft.txt"]}


def test_branch_failure_reports_error_without_sending():
    result = _run_node(
        {"index": 0, "branchFailure": True, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert result["calls"][0][0] == "confirm"
    assert result["calls"][1][0] == "/api/session/branch"
    assert result["calls"][-1][0] == "status"
    assert "branch_failed" in result["calls"][-1][1]
    assert "send" not in [call[0] for call in result["calls"]]


def test_pane_switch_after_branch_aborts_before_child_load():
    result = _run_node(
        {"index": 0, "switchAfterBranch": True, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert [call[0] for call in result["calls"]] == ["confirm", "/api/session/branch"]


def test_child_contract_mismatch_aborts_before_send():
    result = _run_node(
        {"index": 0, "contractMismatch": True, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert [call[0] for call in result["calls"]][-1] == "status"
    assert result["draft"] == "edited"
    assert [call[0] for call in result["calls"]][-2] == "resize"
    assert "contract changed" in result["calls"][-1][1]
    assert "send" not in [call[0] for call in result["calls"]]


def test_older_edit_uses_absolute_index_when_transcript_is_windowed():
    result = _run_node(
        {"index": 0, "oldest": 3, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert result["calls"][1][1]["keep_count"] == 3


def test_source_contracts_and_locales_are_present():
    source = UI.read_text(encoding="utf-8")
    i18n = I18N.read_text(encoding="utf-8")
    assert "const isEditableUser=isUser&&!(readOnlySession&&!branchableReadOnlySession);" in source
    assert "const lastUserKeepCount=lastUserIdx>=0 ? _oldestIdx+lastUserIdx : -1;" in source
    assert "if(branchableReadOnlySession || (lastUserIdx>=0 && absoluteKeepCount!==lastUserKeepCount)){" in source
    assert source.count("const readOnlySession=typeof _isReadOnlySession==='function'") == 1
    assert "session_id:initialSid" in source
    assert "keep_count:absoluteKeepCount" in source
    assert "/api/session/truncate" in source
    assert "/api/session/delete" not in _function_source(source, "async function submitEdit")
    assert i18n.count("edit_fork_title:") == 15
    assert i18n.count("edit_fork_message:") == 15
    assert i18n.count("edit_fork_confirm:") == 15


def test_empty_branch_is_persisted_for_first_prompt_edit():
    source = ROUTES.read_text(encoding="utf-8")
    branch_block = source[source.index('if parsed.path == "/api/session/branch"') :]
    branch_block = branch_block[: branch_block.index('if parsed.path == "/api/session/compress/start"')]
    assert "branch.save()" in branch_block
    assert "if forked_messages:" not in branch_block


def test_model_display_drift_keeps_edit_and_sends_in_child():
    result = _run_node({"index": 0, "modelDrift": True, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]})
    assert result["draft"] == ""
    assert [call for call in result["calls"] if call[0] == "send"] == [["send", "child", "edited"]]
    assert "status" not in [call[0] for call in result["calls"]]


def test_hidden_trailing_user_rows_keep_latest_visible_edit_on_truncate_path():
    for hidden in ({"role": "user", "content": ""}, {"role": "user", "recovery_control": True}, {"role": "user", "content": "The live worker stopped before this run finished."}):
        result = _run_node({"index": 0, "messages": [{"role": "user"}, {"role": "assistant"}, hidden]})
        assert result["calls"][0] == ["/api/session/truncate", {"session_id": "source", "keep_count": 0}]
        assert [call for call in result["calls"] if call[0] == "send"] == [["send", "source", "edited"]]


def test_concurrent_older_edit_creates_one_child_and_sends_once():
    result = _run_node({"index": 0, "concurrent": True, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]})
    assert [call[0] for call in result["calls"]].count("/api/session/branch") == 1
    assert [call for call in result["calls"] if call[0] == "send"] == [["send", "child", "edited"]]


def test_new_composer_text_during_sidebar_refresh_is_not_sent():
    result = _run_node({"index": 0, "typeDuringListRefresh": True, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]})
    assert [call for call in result["calls"] if call[0] == "send"] == [["send", "child", "edited"]]
    assert result["draft"] == "unconfirmed"


def test_failed_child_load_keeps_edit_owned_by_child_without_parent_submission():
    for failure in ("return", "throw"):
        result = _run_node({"index": 0, "loadFailure": failure, "manualSend": True, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]})
        assert result["session"] == "source"
        assert result["draft"] == ""
        assert result["childDrafts"]["child"]["text"] == "edited"
        assert "resize" not in [call[0] for call in result["calls"]]
        assert "send" not in [call[0] for call in result["calls"]]


def test_failed_child_load_preserves_newer_sessions_composer():
    for failure in ("return", "throw"):
        result = _run_node({"index": 0, "loadFailure": failure, "switchDuringLoad": True, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]})
        assert result["session"] == "other"
        assert result["draft"] == "newer draft"
        assert "resize" not in [call[0] for call in result["calls"]]
        assert "send" not in [call[0] for call in result["calls"]]


def test_failed_child_load_can_recover_edited_draft_and_send_to_child():
    for failure in ("return", "throw"):
        result = _run_node({"index": 0, "loadFailure": failure, "manualSend": True, "recoverChild": True, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]})
        assert result["session"] == "child"
        assert result["draft"] == ""
        assert [call for call in result["calls"] if call[0] == "send"] == [["send", "child", "edited"]]
