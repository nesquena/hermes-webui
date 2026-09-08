"""Production-composed RESPEC-4 lifecycle regressions."""

from __future__ import annotations

from pathlib import Path

from tests.js_source_extract import extract_function

from tests.test_foreign_session_full_history_consumers import (
    BACKGROUND_SRC,
    MESSAGES_JS,
    SESSIONS_JS,
    _normal_send_chat_start_switch_script,
    _run_node,
)

BG_RECORD_SRC = extract_function(MESSAGES_JS, "_backgroundResultRecord")
BG_PROJECT_SRC = extract_function(MESSAGES_JS, "projectBackgroundResultsForOwner")


def _idle_source() -> str:
    start = MESSAGES_JS.index("  function _setActivePaneIdleIfOwner()")
    end = MESSAGES_JS.index("  function persistInflightState()", start)
    return MESSAGES_JS[start:end]


def test_respec4_submission_abort_recovers_original_draft():
    result = _run_node(_normal_send_chat_start_switch_script("upload"))
    assert result["restoreArgs"] is not None


def test_respec4_terminal_settlement_invalidates_full_history_ticket():
    capture = extract_function(SESSIONS_JS, "_captureTranscriptReplacement")
    current = extract_function(SESSIONS_JS, "_transcriptReplacementIsCurrent")
    commit = extract_function(SESSIONS_JS, "_commitTranscriptReplacement")
    settle = extract_function(SESSIONS_JS, "settleTranscriptReplacement")
    script = f"""
let _messagesGeneration = 0;
const S = {{session: {{session_id: 'session-a'}}, messages: ['old']}};
let _loadSessionGeneration = 1;
function _bumpMessagesGeneration() {{ _messagesGeneration += 1; }}
{capture}
{current}
{commit}
{settle}
"""
    script += """
const pending = _captureTranscriptReplacement();
const terminalWriter = () => typeof settleTranscriptReplacement === 'function'
  ? settleTranscriptReplacement(() => { S.messages = ['new turn']; })
  : (S.messages = ['new turn']);
terminalWriter();
console.log(JSON.stringify({current: _transcriptReplacementIsCurrent(pending), messages: S.messages}));
"""
    result = _run_node(script)
    assert result == {"current": False, "messages": ["new turn"]}


def test_respec4_background_receipt_survives_projection_loss():
    script = f"""
let _messagesGeneration = 0;
let resolveStatus = null;
const S = {{session: {{session_id: 'parent'}}, messages: [], busy: false, activeStreamId: null}};
const _bgPollTimers = {{}};
const _bgPollDrains = new Map();
const _bgResultCustody = new Map();
const hidden = [];
function _isSessionCurrentPane(sid) {{ return S.session.session_id === sid; }}
function hideBackgroundBadge(id) {{ hidden.push(id); }}
function showBackgroundBadge() {{}}
function renderMessages() {{}}
function showToast() {{}}
function t(key) {{ return key; }}
function api() {{ return new Promise(resolve => {{ resolveStatus = resolve; }}); }}
function setTimeout(fn) {{ return 1; }}
{BACKGROUND_SRC}
{BG_RECORD_SRC}
{BG_PROJECT_SRC}
startBackgroundPolling('parent', 'task-1', 'prompt');
(async () => {{
  await Promise.resolve();
  S.session = {{session_id: 'other'}};
  resolveStatus({{results: [{{task_id: 'task-1', answer: 'RESULT_LOST_AFTER_REJECTED_FIRST_READ'}}]}});
  await Promise.resolve();
  S.session = {{session_id: 'parent'}};
  if (typeof projectBackgroundResultsForOwner === 'function') projectBackgroundResultsForOwner('parent');
  console.log(JSON.stringify({{messages: S.messages, hidden}}));
}})().catch(error => {{ console.error(error.stack || String(error)); process.exit(1); }});
"""
    result = _run_node(script)
    assert result["messages"][0]["content"].endswith("RESULT_LOST_AFTER_REJECTED_FIRST_READ")


def test_respec4_parent_drain_keeps_running_sibling_polling():
    script = f"""
let pending = [];
let timers = [];
const S = {{session: {{session_id: 'parent'}}, messages: [], busy: false, activeStreamId: null}};
const _bgPollTimers = {{}};
const _bgPollDrains = new Map();
const _bgResultCustody = new Map();
const hidden = [];
function _isSessionCurrentPane(sid) {{ return S.session.session_id === sid; }}
function hideBackgroundBadge(id) {{ hidden.push(id); }}
function renderMessages() {{}}
function showToast() {{}}
function t(key) {{ return key; }}
function _bumpMessagesGeneration() {{}}
function api() {{ return new Promise(resolve => pending.push(resolve)); }}
function setTimeout(fn) {{ timers.push(fn); return timers.length; }}
{BACKGROUND_SRC}
{BG_RECORD_SRC}
{BG_PROJECT_SRC}
startBackgroundPolling('parent', 'task-1', 'one');
startBackgroundPolling('parent', 'task-2', 'two');
(async () => {{
  await Promise.resolve();
  pending.shift()({{results: [{{task_id: 'task-1', answer: 'first'}}]}});
  await Promise.resolve();
  if (!timers.length) throw new Error('running sibling did not schedule a follow-up drain');
  timers.shift()();
  await Promise.resolve();
  pending.shift()({{results: [{{task_id: 'task-2', answer: 'second'}}]}});
  await Promise.resolve();
  console.log(JSON.stringify({{messages: S.messages, hidden}}));
}})().catch(error => {{ console.error(error.stack || String(error)); process.exit(1); }});
"""
    result = _run_node(script)
    assert [message["content"].split("\n\n", 1)[-1] for message in result["messages"]] == ["first", "second"]


def test_respec4_background_projection_rebuilds_after_transcript_replace():
    script = f"""
const S = {{session: {{session_id: 'parent'}}, messages: []}};
const _bgResultCustody = new Map();
function _isSessionCurrentPane(sid) {{ return S.session.session_id === sid; }}
function _bumpMessagesGeneration() {{}}
function hideBackgroundBadge() {{}}
function renderMessages() {{}}
function showToast() {{}}
function t(key) {{ return key; }}
{BG_RECORD_SRC}
{BG_PROJECT_SRC}
_backgroundResultRecord('parent', {{task_id: 'task-1', answer: 'answer'}}, 'prompt');
projectBackgroundResultsForOwner('parent');
S.messages = [];
projectBackgroundResultsForOwner('parent');
console.log(JSON.stringify(S.messages));
"""
    result = _run_node(script)
    assert len(result) == 1
    assert result[0]["_backgroundTaskId"] == "task-1"


def test_respec4_accepted_background_start_registers_off_pane():
    commands = Path(__file__).resolve().parents[1] / "static" / "commands.js"
    source = commands.read_text(encoding="utf-8")
    function = extract_function(source, "cmdBackground", "async function")
    script = f"""
let resolveStart = null;
let polling = null;
const S = {{session: {{session_id: 'parent'}}}};
function t(key) {{ return key; }}
function showToast() {{}}
function _commandOwnerIsCurrent() {{ return false; }}
function startBackgroundPolling(...args) {{ polling = args; }}
function showBackgroundBadge() {{}}
function api() {{ return new Promise(resolve => {{ resolveStart = resolve; }}); }}
{function}
(async () => {{
  const promise = cmdBackground('prompt');
  await Promise.resolve();
  S.session = {{session_id: 'other'}};
  resolveStart({{task_id: 'task-1'}});
  await promise;
  console.log(JSON.stringify({{polling}}));
}})().catch(error => {{ console.error(error.stack || String(error)); process.exit(1); }});
"""
    result = _run_node(script)
    assert result["polling"] == ["parent", "task-1", "prompt"]


def test_respec4_owner_projection_runs_after_transcript_load():
    load_marker = "if (_isCurrentLoad()) _loadingSessionId = null;"
    projection_marker = "if(typeof projectSubmittedPayloadForOwner==='function') projectSubmittedPayloadForOwner(sid, _acceptedDraft);"
    assert SESSIONS_JS.index(load_marker) < SESSIONS_JS.index(projection_marker)
    assert SESSIONS_JS.index("renderMessages(sameSessionForceReload?{preserveScroll:true}:undefined);") < SESSIONS_JS.index(projection_marker)


def test_respec4_failed_artifacts_record_can_retry():
    script = """
let reads = 0;
let _artifactsFullHistoryRequest = null;
function _readFullSessionSnapshot() {
  reads += 1;
  return Promise.resolve().then(() => { throw new Error('failed'); });
}
"""
    script += extract_function(
        (Path(__file__).resolve().parents[1] / "static" / "workspace.js").read_text(encoding="utf-8"),
        "_artifactsFullHistoryRequestFor",
    )
    script += """
(async () => {
  const first = _artifactsFullHistoryRequestFor('session-a', 1, 1);
  try { await first.promise; } catch (_) {}
  const second = _artifactsFullHistoryRequestFor('session-a', 1, 1);
  try { await second.promise; } catch (_) {}
  console.log(JSON.stringify({reads, distinct: first !== second}));
})().catch(error => { console.error(error.stack || String(error)); process.exit(1); });
"""
    result = _run_node(script)
    assert result == {"reads": 2, "distinct": True}


def test_respec4_cancel_idle_cleanup_keeps_newer_stream_busy():
    script = f"""
const S = {{session: {{session_id: 'session-a'}}, busy: true, activeStreamId: 'stream-2'}};
const INFLIGHT = {{'session-a': {{streamId: 'stream-2'}}}};
let busyCalls = 0;
function _isActiveSession() {{ return true; }}
function setBusy(value) {{ if (!value) busyCalls += 1; S.busy = value; }}
function setComposerStatus() {{}}
function setStatus() {{}}
{_idle_source()}
_setActivePaneIdleIfOwner('stream-1');
console.log(JSON.stringify({{busy: S.busy, busyCalls}}));
"""
    result = _run_node(script)
    assert result == {"busy": True, "busyCalls": 0}
