"""Regression coverage for foreign-session full-history consumers on PR #6494."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function


REPO = Path(os.environ.get("WEBUI_PROOF_REPO", Path(__file__).resolve().parent.parent))
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")


def _maybe_extract(source: str, name: str, prefix: str = "function") -> str:
    try:
        return extract_function(source, name, prefix)
    except AssertionError:
        return ""


I18N_T_FN = _maybe_extract(I18N_JS, "t")


TRANSCRIPT_FN = MESSAGES_JS[
    MESSAGES_JS.index("function transcript(") : MESSAGES_JS.index("let _composerAutoResizeRaf=0;")
]
ENSURE_ALL_FN = SESSIONS_JS[
    SESSIONS_JS.index("let _loadingOlder = false;") : SESSIONS_JS.index("const SESSION_ARCHIVED_PAGE_SIZE")
]
WORKSPACE_HELPERS = WORKSPACE_JS[
    WORKSPACE_JS.index("function _workspaceTodosTabIsActive(){") : WORKSPACE_JS.index("function _resetWorkspaceTodosRenderCache(){")
]
WORKSPACE_ARTIFACT_CACHE = (
    "\nlet _artifactsFullHistoryRequest = null;\n"
    + WORKSPACE_JS[
        WORKSPACE_JS.index("function _setWorkspacePanelTabDataset()") : WORKSPACE_JS.index("const ARTIFACT_IGNORE_RE =")
    ]
)
WORKSPACE_CONSUMER_BLOCK = WORKSPACE_JS[
    WORKSPACE_JS.index("function _normalizeArtifactPath(") : WORKSPACE_JS.index("async function _workspacePathExists(")
]
WORKSPACE_ARTIFACT_CONSTS = "\n".join(
    [
        r"const ARTIFACT_IGNORE_RE = /(^|\/)(?:\.git|\.hg|\.svn|node_modules|\.venv|venv|__pycache__|dist|build|\.next|\.cache)(?:\/|$)/;",
        "const ARTIFACT_MUTATION_TOOLS = new Set(['write_file','patch','edit_file','create_file','mcp_filesystem_write_file','mcp_filesystem_edit_file']);",
    ]
)
DOWNLOAD_ASSIGN = BOOT_JS[
    BOOT_JS.index("let _downloadTranscriptOperation = null;") : BOOT_JS.index("$('btnExportJSON').onclick=")
].strip()
CMD_GOAL_SRC = _maybe_extract(COMMANDS_JS, "cmdGoal", "async function")
CMD_HELP_SRC = _maybe_extract(COMMANDS_JS, "cmdHelp")
SEND_SRC = _maybe_extract(MESSAGES_JS, "send", "async function")
SLASH_START = SEND_SRC.index("  // Slash command intercept")
SLASH_END = SEND_SRC.index("    if(_parsedCmd&&!_cmd){", SLASH_START)
SLASH_BLOCK = SEND_SRC[SLASH_START:SLASH_END].replace(
    "if(!S.session){await newSession();await renderSessionList();}", ""
) + "\n}"
SLASH_HELPERS = SEND_SRC[
    SEND_SRC.index("  const _slashOwnerIsCurrent=") : SLASH_START
]
ASYNC_SLASH_BLOCK = SEND_SRC[
    SEND_SRC.index("  let _slashDisplayTextOverride=") : SEND_SRC.index(
        "\n  if(!S.session){await newSession();await renderSessionList();}\n\n  const activeSid="
        if "\n  if(!S.session){await newSession();await renderSessionList();}\n\n  const activeSid=" in SEND_SRC
        else "\n  ownerSid=await _ensureSessionOwner();\n  if(!ownerSid)return;\n  const activeSid=",
        SEND_SRC.index("  let _slashDisplayTextOverride=")
    )
]
BACKGROUND_SRC = _maybe_extract(MESSAGES_JS, "startBackgroundPolling")
GATEWAY_SRC = _maybe_extract(SESSIONS_JS, "startGatewaySSE")
CAPTURE_SRC = _maybe_extract(SESSIONS_JS, "_captureTranscriptReplacement")
CURRENT_SRC = _maybe_extract(SESSIONS_JS, "_transcriptReplacementIsCurrent")
COMMIT_SRC = _maybe_extract(SESSIONS_JS, "_commitTranscriptReplacement")
ENSURE_OWNER_SRC = _maybe_extract(SESSIONS_JS, "_ensureSessionOwner", "async function")
NEW_SESSION_OWNER_GUARD_SRC = _maybe_extract(SESSIONS_JS, "_newSessionOwnerResponseIsCurrent")
BASE_OWNER_FALLBACK_SRC = """
async function _ensureSessionOwner() {
  if (S.session && S.session.session_id) return S.session.session_id;
  const sid = await newSession();
  await renderSessionList();
  return _isSessionCurrentPane(sid) ? sid : null;
}
"""
APPLY_COMPRESSION_SRC = _maybe_extract(COMMANDS_JS, "_applyManualCompressionResult", "async function")
REFRESH_SRC = _maybe_extract(UI_JS, "refreshSession", "async function")
COMPRESSION_SRC = _maybe_extract(COMMANDS_JS, "_runManualCompression", "async function")
STEER_SRC = COMMANDS_JS[
    COMMANDS_JS.index("function _steerUploadedAttachmentPaths") : COMMANDS_JS.index("async function cmdTitle")
]
POLL_COMPRESSION_SRC = _maybe_extract(COMMANDS_JS, "_pollManualCompressionResult", "async function")
CLEAR_COMPRESSION_SRC = _maybe_extract(COMMANDS_JS, "resumeManualCompressionForSession", "async function")
COMPRESSION_OPERATION_SRC = COMMANDS_JS[
    COMMANDS_JS.index("let _manualCompressionOperation = null;") : COMMANDS_JS.index(
        "async function resumeManualCompressionForSession"
    )
]
CLEAR_SRC = _maybe_extract(PANELS_JS, "clearConversation", "async function")
CLEAR_SERIAL_SRC = "let _clearConversationOperationSerial = 0;" if "_clearConversationOperationSerial" in CLEAR_SRC else ""
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(script: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="webui-6491-node-") as temp_dir:
        script_path = Path(temp_dir) / "case.js"
        script_path.write_text(script, encoding="utf-8")
        proc = subprocess.run(
            [NODE, str(script_path)],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
    assert proc.returncode == 0 and proc.stdout.strip(), proc.stderr or proc.stdout or "node produced no JSON output"
    return json.loads(proc.stdout)


def _download_script(
    *,
    fail_load: bool = False,
    switch_session: bool = False,
    active_stream: bool = False,
    generation_change: bool = False,
) -> str:
    if fail_load:
        snapshot_body = "ensureCalls += 1; throw new Error('load failed');"
    elif switch_session:
        snapshot_body = (
            "ensureCalls += 1; "
            "S.session = { session_id: 'foreign-2', workspace: '/ws', model: 'model' }; "
            "return { session: { session_id: 'foreign-1', workspace: '/ws', model: 'model' }, messages: fullMessages, toolCalls: [] };"
        )
    elif generation_change:
        snapshot_body = "ensureCalls += 1; _messagesGeneration += 1; return { session: S.session, messages: fullMessages, toolCalls: [] };"
    else:
        snapshot_body = "ensureCalls += 1; return { session: S.session, messages: fullMessages, toolCalls: [] };"
    if fail_load:
        legacy_body = "ensureCalls += 1; throw new Error('load failed');"
    elif switch_session:
        legacy_body = "ensureCalls += 1; S.session = { session_id: 'foreign-2', workspace: '/ws', model: 'model' }; S.messages = fullMessages; _messagesTruncated = false;"
    else:
        legacy_body = "ensureCalls += 1; S.messages = fullMessages; _messagesTruncated = false;"
    return f"""
const translations = {{
  download_transcript_preparing_full: 'Preparing full transcript…',
  download_transcript_busy_full: 'Wait for the current response to finish before downloading the full transcript.',
  download_transcript_failed_full: 'Failed to load the full transcript.',
  download_transcript_changed_full: 'The conversation changed while the full transcript was loading. Try again.',
}};
const OLD = 'OLD_TRANSCRIPT_UNIQUE';
const fullMessages = [
  {{ role: 'user', content: OLD }},
  {{ role: 'assistant', content: 'latest tail' }},
];
let ensureCalls = 0;
let clicked = false;
let objectHref = null;
let revokedHref = null;
let downloadName = null;
let downloadedText = null;
const toasts = [];
class Blob {{
  constructor(parts, opts) {{
    downloadedText = parts.join('');
    this.type = opts && opts.type;
  }}
}}
const URL = {{
  createObjectURL: () => {{
    objectHref = 'blob:download';
    return objectHref;
  }},
  revokeObjectURL: (href) => {{
    revokedHref = href;
  }},
}};
const document = {{
  createElement: () => {{
    const anchor = {{
      click: () => {{
        clicked = true;
      }},
    }};
    Object.defineProperty(anchor, 'href', {{
      get() {{ return objectHref; }},
      set(v) {{ objectHref = v; }},
    }});
    Object.defineProperty(anchor, 'download', {{
      get() {{ return downloadName; }},
      set(v) {{ downloadName = v; }},
    }});
    return anchor;
  }},
}};
const btns = {{
  btnDownload: {{
    disabled: false,
    attributes: {{}},
    setAttribute(name, value) {{ this.attributes[name] = String(value); }},
    removeAttribute(name) {{ delete this.attributes[name]; }},
  }},
  btnExportJSON: {{}},
}};
function $(id) {{ return btns[id]; }}
function t(key) {{ return translations[key] || key; }}
function showToast(message, duration, kind) {{
  toasts.push({{ message, duration, kind: kind || null }});
}}
let _messagesTruncated = true;
let _messagesGeneration = 1;
const S = {{
  session: {{ session_id: 'foreign-1', workspace: '/ws', model: 'model' }},
  messages: [fullMessages[1]],
  busy: {str(active_stream).lower()},
  activeStreamId: {json.dumps('live-1' if active_stream else None)},
}};
async function _readFullSessionSnapshot() {{ {snapshot_body} }}
async function _ensureAllMessagesLoaded() {{ {legacy_body} }}
{TRANSCRIPT_FN}
{DOWNLOAD_ASSIGN}
(async () => {{
  const ret = btns.btnDownload.onclick();
  if (ret && typeof ret.then === 'function') await ret;
  console.log(JSON.stringify({{
    ensureCalls,
    clicked,
    downloadName,
    downloadedText,
    revokedHref,
    disabled: btns.btnDownload.disabled,
    ariaBusy: btns.btnDownload.attributes['aria-busy'] || null,
    sessionId: S.session && S.session.session_id,
    messageCount: S.messages.length,
    truncated: _messagesTruncated,
    toasts,
  }}));
}})().catch((err) => {{
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
}});
"""


def _artifact_script(
    *,
    fail_load: bool = False,
    active_stream: bool = False,
    active_tab: str = "artifacts",
    tab_hidden: bool = False,
    panel_hidden: bool = False,
    seed_existing_dom: bool = False,
    generation_abort: bool = False,
) -> str:
    workspace_path = str(REPO / "static" / "workspace.js")
    initial_html = '<button data-artifact-path="previous/file.txt"></button>' if seed_existing_dom else ""
    initial_count = "1" if seed_existing_dom else ""
    if fail_load:
        snapshot_body = "throw new Error('load failed');"
    elif generation_abort:
        snapshot_body = "_bumpMessagesGeneration(); return { session: S.session, messages: fullMessages, toolCalls: [] };"
    else:
        snapshot_body = "return { session: S.session, messages: fullMessages, toolCalls: [] };"
    legacy_body = "ensureCalls += 1; " + ("throw new Error('load failed');" if fail_load else "S.messages = fullMessages; _messagesTruncated = false;")
    return f"""
const fs = require('fs');
const workspaceSrc = fs.readFileSync({json.dumps(workspace_path)}, 'utf8');
const fullMessages = [
  {{
    role: 'assistant',
    tool_calls: [
      {{
        function: {{
          name: 'write_file',
          arguments: JSON.stringify({{ path: 'old/deep.txt' }}),
        }},
      }},
    ],
  }},
  {{
    role: 'assistant',
    tool_calls: [
      {{
        function: {{
          name: 'write_file',
          arguments: JSON.stringify({{ path: 'new/live.txt' }}),
        }},
      }},
    ],
  }},
];
let ensureCalls = 0;
let _messagesTruncated = true;
let _messagesGeneration = 1;
function _bumpMessagesGeneration() {{ _messagesGeneration += 1; return _messagesGeneration; }}
let _workspacePanelActiveTab = {json.dumps(active_tab)};
const rightPanel = {{ dataset: {{ activeTab: {json.dumps(active_tab)} }} }};
const root = {{
  innerHTML: {json.dumps(initial_html)},
  isConnected: true,
  hidden: {str(panel_hidden).lower()},
}};
const count = {{ textContent: {json.dumps(initial_count)} }};
const artifactsTab = {{ hidden: {str(tab_hidden).lower()} }};
const document = {{
  querySelector: (selector) => selector === '.rightpanel' ? rightPanel : null,
  getElementById: (id) => {{
    if (id === 'workspaceArtifactsTab') return artifactsTab;
    if (id === 'workspaceArtifacts') return root;
    return null;
  }},
}};
function $(id) {{
  if (id === 'workspaceArtifacts') return root;
  if (id === 'workspaceArtifactsCount') return count;
  return null;
}}
function esc(value) {{
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}}
function t(key) {{
  if (key === 'workspace_artifact_source_session') return 'session';
  if (key === 'workspace_artifact_loading_full_history') return 'Loading full history…';
  return key;
}}
const S = {{
  session: {{ session_id: 'foreign-1', workspace: '/ws' }},
  messages: [fullMessages[1]],
  toolCalls: [],
  busy: {str(active_stream).lower()},
  activeStreamId: {json.dumps('live-1' if active_stream else None)},
}};
async function _readFullSessionSnapshot() {{ ensureCalls += 1; {snapshot_body} }}
async function _ensureAllMessagesLoaded() {{ {legacy_body} }}
{WORKSPACE_HELPERS}
{WORKSPACE_ARTIFACT_CONSTS}
{WORKSPACE_ARTIFACT_CACHE}
{WORKSPACE_CONSUMER_BLOCK}
(async () => {{
  const ret = renderSessionArtifacts();
  const htmlBeforeAwait = root.innerHTML;
  const countBeforeAwait = count.textContent;
  if (ret && typeof ret.then === 'function') await ret;
  await new Promise((resolve) => setTimeout(resolve, 0));
  console.log(JSON.stringify({{
    ensureCalls,
    htmlBeforeAwait,
    countBeforeAwait,
    html: root.innerHTML,
    count: count.textContent,
    messageCount: S.messages.length,
    truncated: _messagesTruncated,
    generation: _messagesGeneration,
  }}));
}})().catch((err) => {{
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
}});
"""


def _ensure_all_messages_loaded_script(
    *, goal_turn_during_fetch: bool = False, goal_pane_loading_during_response: bool = False
) -> str:
    sessions_path = str(REPO / "static" / "sessions.js")
    commands_path = str(REPO / "static" / "commands.js")
    return f"""
const fs = require('fs');
const sessionsSrc = fs.readFileSync({json.dumps(sessions_path)}, 'utf8');
const commandsSrc = fs.readFileSync({json.dumps(commands_path)}, 'utf8');
function extractFunction(source, name) {{
  let start = source.indexOf(`async function ${{name}}(`);
  if (start < 0) start = source.indexOf(`function ${{name}}(`);
  if (start < 0) throw new Error(`missing function ${{name}}`);
  const brace = source.indexOf('{{', start);
  let depth = 0;
  for (let i = brace; i < source.length; i++) {{
    const ch = source[i];
    if (ch === '{{') depth++;
    else if (ch === '}}') {{
      depth--;
      if (depth === 0) return source.slice(start, i + 1);
    }}
  }}
  throw new Error(`unterminated function ${{name}}`);
}}
const ensureAllSrc = sessionsSrc.slice(
  sessionsSrc.indexOf('let _loadingOlder = false;'),
  sessionsSrc.indexOf('const SESSION_ARCHIVED_PAGE_SIZE')
);
const cmdGoalSrc = extractFunction(commandsSrc, 'cmdGoal');
let _messagesTruncated = true;
let _loadingSessionId = null;
let bumpCalls = 0;
let syncCalls = 0;
let goalMessagesAfterResponse = null;
let goalBusyAfterResponse = null;
const originalMessages = [{{ role: 'assistant', content: 'tail latest', _transient: true }}];
const fullMessages = [
  {{ role: 'user', content: 'older' }},
  {{ role: 'assistant', content: 'tail latest' }},
];
const S = {{
  session: {{ session_id: 'foreign-1', message_count: 1, workspace: '/ws', model: 'goal-model' }},
  messages: originalMessages.slice(),
  toolCalls: [{{ id: 'live-tc' }}],
  busy: false,
  activeStreamId: null,
}};
const window = {{
  _carryForwardEphemeralTurnFields: (_current, incoming) => incoming,
}};
function _syncToolCallsForLoadedMessages(_messages, toolCalls) {{
  syncCalls += 1;
  S.toolCalls = Array.isArray(toolCalls) ? toolCalls.map((tc) => ({{ ...tc }})) : [];
}}
let resolveFullLoad = null;
let resolveGoal = null;
function $(id) {{
  if (id === 'modelSelect') return {{ value: 'goal-model' }};
  return null;
}}
function t(key) {{ return key; }}
function renderMessages() {{}}
function showToast() {{}}
function clearLiveToolCards() {{}}
function appendThinking() {{}}
function setBusy(v) {{ S.busy = v; }}
function setComposerStatus() {{}}
async function renderSessionList() {{}}
async function newSession() {{
  S.session = {{ session_id: 'foreign-1', message_count: 1, workspace: '/ws', model: 'goal-model' }};
  return S.session.session_id;
}}
function _isSessionCurrentPane(sid) {{
  return !!S.session && S.session.session_id === sid && (!_loadingSessionId || _loadingSessionId === sid);
}}
{ENSURE_OWNER_SRC or BASE_OWNER_FALLBACK_SRC}
function markInflight() {{}}
function saveInflightState() {{}}
function startApprovalPolling() {{}}
function startClarifyPolling() {{}}
function _fetchYoloState() {{}}
const INFLIGHT = {{}};
function attachLiveStream(sid, streamId) {{
  if (!S.session || S.session.session_id !== sid || streamId !== 'goal-1') return;
  S.messages = [
    {{ role: 'user', content: 'NEW TURN' }},
    {{ role: 'assistant', content: 'NEW ANSWER' }},
  ];
  S.toolCalls = [{{ id: 'goal-tc' }}];
  S.session.message_count = 2;
  S.busy = false;
  S.activeStreamId = null;
  S.session.active_stream_id = null;
}}
async function api(url) {{
  if (String(url).startsWith('/api/session?')) {{
    return await new Promise((resolve) => {{
      resolveFullLoad = resolve;
    }});
  }}
  if (url === '/api/goal') {{
    return await new Promise((resolve) => {{
      resolveGoal = resolve;
    }});
  }}
  throw new Error('Unexpected API call: ' + String(url));
}}
{ENSURE_ALL_FN}
const _productionBumpMessagesGeneration = _bumpMessagesGeneration;
_bumpMessagesGeneration = function() {{ bumpCalls += 1; return _productionBumpMessagesGeneration(); }};
_oldestIdx = 99;
{CMD_GOAL_SRC}
(async () => {{
  const ensurePromise = _ensureAllMessagesLoaded();
  if ({str(goal_turn_during_fetch).lower()}) {{
    const goalPromise = cmdGoal('ship it');
    await Promise.resolve();
    const goalClaimedBeforeResponse = bumpCalls > 0;
    if (typeof resolveGoal !== 'function') throw new Error('goal request was not started');
    if ({str(goal_pane_loading_during_response).lower()}) _loadingSessionId = 'other-session';
    resolveGoal({{
      message: 'Goal status',
      stream_id: 'goal-1',
      pending_started_at: 123,
    }});
    await goalPromise;
    goalMessagesAfterResponse = S.messages.map((message) => ({{ role: message.role, content: message.content }}));
    goalBusyAfterResponse = S.busy;
    if ({str(goal_pane_loading_during_response).lower()}) _loadingSessionId = null;
    if (goalClaimedBeforeResponse) throw new Error('goal claimed transcript generation before its response');
  }}
  if (typeof resolveFullLoad !== 'function') throw new Error('full-history load was not requested');
  resolveFullLoad({{
    session: {{
      messages: fullMessages,
      tool_calls: [{{ id: 'old-tc' }}],
      message_count: fullMessages.length,
    }},
  }});
  await ensurePromise;
  console.log(JSON.stringify({{
    messages: S.messages,
    toolCalls: S.toolCalls,
    truncated: _messagesTruncated,
    busy: S.busy,
    activeStreamId: S.activeStreamId,
    bumpCalls,
    syncCalls,
    goalMessagesAfterResponse,
    goalBusyAfterResponse,
    oldestIdx: _oldestIdx,
    count: S.session && S.session.message_count,
  }}));
}})().catch((err) => {{
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
}});
"""


def _download_double_invocation_script() -> str:
    return f"""
const fullMessages = [
  {{ role: 'user', content: 'older row' }},
  {{ role: 'assistant', content: 'latest row' }},
];
let ensureCalls = 0;
let pending = [];
let clicked = 0;
let downloadedText = null;
let objectHref = null;
let revokedHref = null;
const toasts = [];
class Blob {{ constructor(parts) {{ downloadedText = parts.join(''); }} }}
const URL = {{
  createObjectURL: () => {{ objectHref = 'blob:double'; return objectHref; }},
  revokeObjectURL: (href) => {{ revokedHref = href; }},
}};
const document = {{
  createElement: () => {{
    const anchor = {{ click: () => {{ clicked += 1; }} }};
    Object.defineProperty(anchor, 'href', {{ get: () => objectHref, set: (value) => {{ objectHref = value; }} }});
    Object.defineProperty(anchor, 'download', {{ set: () => {{}} }});
    return anchor;
  }},
}};
const btns = {{
  btnDownload: {{
    disabled: false,
    attributes: {{}},
    setAttribute(name, value) {{ this.attributes[name] = String(value); }},
    removeAttribute(name) {{ delete this.attributes[name]; }},
  }},
  btnExportJSON: {{}},
}};
function $(id) {{ return btns[id]; }}
function t(key) {{
  return {{
    download_transcript_preparing_full: 'Preparing full transcript…',
    download_transcript_busy_full: 'Wait for the current response to finish before downloading the full transcript.',
    download_transcript_failed_full: 'Failed to load the full transcript.',
    download_transcript_changed_full: 'The conversation changed while the full transcript was loading. Try again.',
  }}[key] || key;
}}
function showToast(message, duration, kind) {{ toasts.push({{ message, duration, kind: kind || null }}); }}
let _messagesTruncated = true;
let _messagesGeneration = 1;
const S = {{
  session: {{ session_id: 'foreign-1', workspace: '/ws', model: 'model' }},
  messages: [fullMessages[1]],
  busy: false,
  activeStreamId: null,
}};
async function _readFullSessionSnapshot() {{
  ensureCalls += 1;
  return await new Promise((resolve) => pending.push(resolve));
}}
async function _ensureAllMessagesLoaded() {{}}
{TRANSCRIPT_FN}
{DOWNLOAD_ASSIGN}
(async () => {{
  const first = btns.btnDownload.onclick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  const beforeSecond = {{ calls: ensureCalls, disabled: btns.btnDownload.disabled, ariaBusy: btns.btnDownload.attributes['aria-busy'] || null, pending: pending.length }};
  const second = btns.btnDownload.onclick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  const duringSecond = {{ calls: ensureCalls, pending: pending.length, toasts: toasts.slice() }};
  for (const resolve of pending.splice(0)) resolve({{ session: S.session, messages: fullMessages, toolCalls: [] }});
  await Promise.all([first, second]);
  console.log(JSON.stringify({{ beforeSecond, duringSecond, ensureCalls, pending: pending.length, clicked, downloadedText, revokedHref, disabled: btns.btnDownload.disabled, ariaBusy: btns.btnDownload.attributes['aria-busy'] || null, toasts }}));
}})().catch((err) => {{ console.error(err.stack || String(err)); process.exit(1); }});
"""


def _artifact_reuse_script() -> str:
    workspace_path = str(REPO / "static" / "workspace.js")
    return f"""
const fs = require('fs');
const workspaceSrc = fs.readFileSync({json.dumps(workspace_path)}, 'utf8');
const fullMessages = [
  {{ role: 'assistant', tool_calls: [{{ function: {{ name: 'write_file', arguments: JSON.stringify({{ path: 'old/deep.txt' }}) }} }}] }},
  {{ role: 'assistant', tool_calls: [{{ function: {{ name: 'write_file', arguments: JSON.stringify({{ path: 'new/live.txt' }}) }} }}] }},
];
let ensureCalls = 0;
let pending = [];
let _messagesGeneration = 1;
let _loadSessionGeneration = 1;
let _messagesTruncated = true;
let _workspacePanelActiveTab = 'artifacts';
const rightPanel = {{ dataset: {{ activeTab: 'artifacts' }} }};
const root = {{ innerHTML: '', isConnected: true, hidden: false }};
const count = {{ textContent: '' }};
const artifactsTab = {{ hidden: false }};
const document = {{
  querySelector: (selector) => selector === '.rightpanel' ? rightPanel : null,
  getElementById: (id) => id === 'workspaceArtifactsTab' ? artifactsTab : id === 'workspaceArtifacts' ? root : null,
}};
function $(id) {{ return id === 'workspaceArtifacts' ? root : id === 'workspaceArtifactsCount' ? count : null; }}
function esc(value) {{ return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }}
function t(key) {{ return key === 'workspace_artifact_source_session' ? 'session' : key === 'workspace_artifact_loading_full_history' ? 'Loading full history…' : key; }}
const S = {{ session: {{ session_id: 'foreign-1', workspace: '/ws' }}, messages: [fullMessages[1]], toolCalls: [], busy: false, activeStreamId: null }};
async function _readFullSessionSnapshot() {{ ensureCalls += 1; return await new Promise((resolve) => pending.push(resolve)); }}
async function _ensureAllMessagesLoaded() {{}}
{WORKSPACE_HELPERS}
{WORKSPACE_ARTIFACT_CONSTS}
{WORKSPACE_ARTIFACT_CACHE}
{WORKSPACE_CONSUMER_BLOCK}
(async () => {{
  const first = renderSessionArtifacts();
  const second = renderSessionArtifacts();
  const samePendingPromise = first === second;
  const beforeResolve = {{ ensureCalls, html: root.innerHTML, count: count.textContent }};
  await new Promise((resolve) => setTimeout(resolve, 0));
  for (const resolve of pending.splice(0)) resolve({{ session: S.session, messages: fullMessages, toolCalls: [] }});
  await first;
  const afterFirst = {{ ensureCalls, html: root.innerHTML, count: count.textContent }};
  const settled = renderSessionArtifacts();
  const settledAgain = renderSessionArtifacts();
  await settled;
  console.log(JSON.stringify({{ samePendingPromise, beforeResolve, afterFirst, ensureCalls, sameSettledPromise: settled === settledAgain, finalHtml: root.innerHTML, finalCount: count.textContent }}));
}})().catch((err) => {{ console.error(err.stack || String(err)); process.exit(1); }});
"""


def _artifact_stale_record_script() -> str:
    workspace_path = str(REPO / "static" / "workspace.js")
    return f"""
const fs = require('fs');
const workspaceSrc = fs.readFileSync({json.dumps(workspace_path)}, 'utf8');
const currentMessages = [{{ role: 'assistant', tool_calls: [{{ function: {{ name: 'write_file', arguments: JSON.stringify({{ path: 'current/live.txt' }}) }} }}] }}];
let ensureCalls = 0;
let pending = [];
let _messagesGeneration = 1;
let _loadSessionGeneration = 1;
let _messagesTruncated = true;
let _workspacePanelActiveTab = 'artifacts';
const rightPanel = {{ dataset: {{ activeTab: 'artifacts' }} }};
const root = {{ innerHTML: '', isConnected: true, hidden: false }};
const count = {{ textContent: '' }};
const artifactsTab = {{ hidden: false }};
const document = {{ querySelector: (selector) => selector === '.rightpanel' ? rightPanel : null, getElementById: (id) => id === 'workspaceArtifactsTab' ? artifactsTab : id === 'workspaceArtifacts' ? root : null }};
function $(id) {{ return id === 'workspaceArtifacts' ? root : id === 'workspaceArtifactsCount' ? count : null; }}
function esc(value) {{ return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }}
function t(key) {{ return key === 'workspace_artifact_source_session' ? 'session' : key; }}
const S = {{ session: {{ session_id: 'foreign-1', workspace: '/ws' }}, messages: currentMessages, toolCalls: [], busy: false, activeStreamId: null }};
async function _readFullSessionSnapshot() {{ ensureCalls += 1; return await new Promise((resolve) => pending.push(resolve)); }}
async function _ensureAllMessagesLoaded() {{}}
{WORKSPACE_HELPERS}
{WORKSPACE_ARTIFACT_CONSTS}
{WORKSPACE_ARTIFACT_CACHE}
{WORKSPACE_CONSUMER_BLOCK}
(async () => {{
  const oldRender = renderSessionArtifacts();
  S.session = {{ session_id: 'foreign-2', workspace: '/ws' }};
  _loadSessionGeneration = 2;
  S.session = {{ session_id: 'foreign-1', workspace: '/ws' }};
  _loadSessionGeneration = 3;
  const newRender = renderSessionArtifacts();
  await new Promise((resolve) => setTimeout(resolve, 0));
  const oldResolve = pending.shift();
  const newResolve = pending.shift();
  newResolve({{ session: S.session, messages: [{{ role: 'assistant', tool_calls: [{{ function: {{ name: 'write_file', arguments: JSON.stringify({{ path: 'fresh/current.txt' }}) }} }}] }}], toolCalls: [] }});
  await newRender;
  oldResolve({{ session: S.session, messages: [{{ role: 'assistant', tool_calls: [{{ function: {{ name: 'write_file', arguments: JSON.stringify({{ path: 'stale/old.txt' }}) }} }}] }}], toolCalls: [] }});
  await oldRender;
  console.log(JSON.stringify({{ ensureCalls, html: root.innerHTML, count: count.textContent }}));
}})().catch((err) => {{ console.error(err.stack || String(err)); process.exit(1); }});
"""


def _slash_command_generation_race_script() -> str:
    sessions_path = str(REPO / "static" / "sessions.js")
    commands_path = str(REPO / "static" / "commands.js")
    messages_path = str(REPO / "static" / "messages.js")
    return f"""
const fs = require('fs');
const sessionsSrc = fs.readFileSync({json.dumps(sessions_path)}, 'utf8');
const commandsSrc = fs.readFileSync({json.dumps(commands_path)}, 'utf8');
const messagesSrc = fs.readFileSync({json.dumps(messages_path)}, 'utf8');
function extractFunction(source, name) {{
  let start = source.indexOf(`async function ${{name}}(`);
  if (start < 0) start = source.indexOf(`function ${{name}}(`);
  const brace = source.indexOf('{{', start);
  let depth = 0;
  for (let i = brace; i < source.length; i++) {{
    if (source[i] === '{{') depth++;
    else if (source[i] === '}}' && --depth === 0) return source.slice(start, i + 1);
  }}
  throw new Error(`unterminated ${{name}}`);
}}
const ensureAllSrc = sessionsSrc.slice(
  sessionsSrc.indexOf('let _loadingOlder = false;'),
  sessionsSrc.indexOf('const SESSION_ARCHIVED_PAGE_SIZE')
);
const cmdHelpSrc = extractFunction(commandsSrc, 'cmdHelp');
const sendSrc = extractFunction(messagesSrc, 'send');
const slashStart = sendSrc.indexOf('  // Slash command intercept');
const slashEnd = sendSrc.indexOf('    if(_parsedCmd&&!_cmd){{', slashStart);
if (slashStart < 0 || slashEnd < 0) throw new Error('slash command seam not found');
const slashBlock = sendSrc.slice(slashStart, slashEnd).replace("if(!S.session){{await newSession();await renderSessionList();}}", '') + '\\n}}';
let _messagesTruncated = true;
let _loadingSessionId = null;
let bumpCalls = 0;
let resolveFullLoad = null;
const S = {{
  session: {{ session_id: 'foreign-1', message_count: 1 }},
  messages: [],
  toolCalls: [],
  pendingFiles: [],
  busy: false,
  activeStreamId: null,
}};
const window = {{}};
function _syncToolCallsForLoadedMessages() {{ throw new Error('stale load must not sync tools'); }}
function $(id) {{ return id === 'msg' ? {{ value: '/help' }} : null; }}
async function _ensureSessionOwner() {{ return S.session && S.session.session_id; }}
function parseCommand(text) {{ return text === '/help' ? {{ name: 'help', args: '' }} : null; }}
function t(key) {{ return key; }}
function renderMessages() {{}}
function showToast() {{}}
function autoResize() {{}}
function hideCmdDropdown() {{}}
function newSession() {{}}
function api(url) {{
  if (String(url).startsWith('/api/session?')) return new Promise(resolve => {{ resolveFullLoad = resolve; }});
  throw new Error('unexpected API '+url);
}}
const COMMANDS = [{{ name: 'help', desc: 'help', noEcho: false, fn: null }}];
{CMD_HELP_SRC}
COMMANDS[0].fn = cmdHelp;
{ENSURE_ALL_FN}
{SLASH_HELPERS}
const _productionBumpMessagesGeneration = _bumpMessagesGeneration;
_bumpMessagesGeneration = function() {{ bumpCalls += 1; return _productionBumpMessagesGeneration(); }};
async function runSlash() {{
  let text = '/help';
  const literalSlash = false;
  const runSlashBody = async function(){{{SLASH_BLOCK}}};
  await runSlashBody();
}}
(async () => {{
  const ensurePromise = _ensureAllMessagesLoaded();
  await runSlash();
  if (typeof resolveFullLoad !== 'function') throw new Error('full-history load was not requested');
  resolveFullLoad({{ session: {{ messages: [{{ role: 'user', content: 'OLD' }}], tool_calls: [] }} }});
  await ensurePromise;
  console.log(JSON.stringify({{ messages: S.messages, bumpCalls, truncated: _messagesTruncated }}));
}})().catch(err => {{ console.error(err.stack || String(err)); process.exit(1); }});
"""


def _replacement_race_script(*, loading_transition: bool = False) -> str:
    sessions_path = str(REPO / "static" / "sessions.js")
    commands_path = str(REPO / "static" / "commands.js")
    ui_path = str(REPO / "static" / "ui.js")
    return f"""
const fs = require('fs');
const sessionsSrc = fs.readFileSync({json.dumps(sessions_path)}, 'utf8');
const commandsSrc = fs.readFileSync({json.dumps(commands_path)}, 'utf8');
const uiSrc = fs.readFileSync({json.dumps(ui_path)}, 'utf8');
function extractFunction(source, name) {{
  let start = source.indexOf(`async function ${{name}}(`);
  if (start < 0) start = source.indexOf(`function ${{name}}(`);
  if (start < 0) throw new Error(`missing function ${{name}}`);
  const brace = source.indexOf('{{', start);
  let depth = 0;
  for (let i = brace; i < source.length; i++) {{
    if (source[i] === '{{') depth++;
    else if (source[i] === '}}' && --depth === 0) return source.slice(start, i + 1);
  }}
  throw new Error(`unterminated ${{name}}`);
}}
const captureSrc = extractFunction(sessionsSrc, '_captureTranscriptReplacement');
const currentSrc = extractFunction(sessionsSrc, '_transcriptReplacementIsCurrent');
const commitSrc = extractFunction(sessionsSrc, '_commitTranscriptReplacement');
const refreshSrc = extractFunction(uiSrc, 'refreshSession');
const compressionSrc = extractFunction(commandsSrc, '_runManualCompression');
let _messagesGeneration = 0;
function _bumpMessagesGeneration() {{ _messagesGeneration += 1; }}
{CAPTURE_SRC}
{CURRENT_SRC}
{COMMIT_SRC}
{REFRESH_SRC}
{COMPRESSION_OPERATION_SRC}
{COMPRESSION_SRC}
const window = {{ _restartingForUpdate: false }};
const S = {{
  session: {{ session_id: 'same-session', workspace: '/ws', messages: [] }},
  messages: [{{ role: 'assistant', content: 'before' }}],
  toolCalls: [],
  busy: false,
  activeStreamId: null,
}};
let _loadingSessionId = null;
function _isSessionCurrentPane(sid) {{
  return !!S.session && S.session.session_id === sid && (!_loadingSessionId || _loadingSessionId === sid);
}}
let resolveRequest = null;
let requestKind = '';
function $(id) {{ return id === 'modelSelect' ? {{ value: 'model' }} : null; }}
function dismissReconnect() {{}}
function syncTopbar() {{}}
function _renderMessagesWithScrollSnapshot() {{}}
function renderMessages() {{}}
function clearLiveToolCards() {{}}
function setBusy(value) {{ S.busy = value; }}
function setComposerStatus() {{}}
function setCompressionUi() {{}}
function clearCompressionUi() {{}}
function _setCompressionSessionLock() {{}}
function _manualCompressionVisibleMessages() {{ return S.messages.slice(); }}
function _compressionAnchorMessageKey() {{ return null; }}
function _pollManualCompressionResult() {{ return Promise.reject(new Error('unexpected poll')); }}
function showToast() {{}}
async function renderSessionList() {{}}
async function api(url) {{
  if (requestKind === 'refresh' && String(url).startsWith('/api/session?')) return await new Promise(resolve => {{ resolveRequest = resolve; }});
  if (requestKind === 'compression' && String(url).startsWith('/api/session?')) return await new Promise(resolve => {{ resolveRequest = resolve; }});
  throw new Error('unexpected API request: ' + String(url));
}}
async function run() {{
  requestKind = 'refresh';
  const refreshPromise = refreshSession();
  if (typeof resolveRequest !== 'function') throw new Error('refresh request not started');
  if (!{str(loading_transition).lower()}) _bumpMessagesGeneration();
  S.messages.push({{ role: 'user', content: 'newer row' }});
  if ({str(loading_transition).lower()}) _loadingSessionId = 'other-session';
  resolveRequest({{ session: {{ session_id: 'same-session', messages: [{{ role: 'assistant', content: 'stale' }}] }} }});
  await refreshPromise;
  if ({str(loading_transition).lower()}) _loadingSessionId = null;
  const refreshMessages = S.messages.map(m => m.content);

  S.messages = [{{ role: 'assistant', content: 'before compression' }}];
  S.busy = false;
  _messagesGeneration = 0;
  requestKind = 'compression';
  const compressionPromise = _runManualCompression('');
  if (typeof resolveRequest !== 'function') throw new Error('compression request not started');
  if (!{str(loading_transition).lower()}) _bumpMessagesGeneration();
  S.messages.push({{ role: 'user', content: 'newer compression row' }});
  if ({str(loading_transition).lower()}) _loadingSessionId = 'other-session';
  resolveRequest({{ session: {{ session_id: 'same-session', messages: [{{ role: 'assistant', content: 'stale compression' }}] }} }});
  await compressionPromise;
  if ({str(loading_transition).lower()}) _loadingSessionId = null;
  console.log(JSON.stringify({{ refreshMessages, compressionMessages: S.messages.map(m => m.content) }}));
}}
run().catch(err => {{ console.error(err.stack || String(err)); process.exit(1); }});
"""


def _async_slash_owner_script(
    command: str,
    *,
    blank_page: bool = False,
    switch_on_completion: bool = False,
    metadata_delay: bool = False,
    delay_render: bool = False,
) -> str:
    command_json = json.dumps(command)
    session_literal = "null" if blank_page else "{ session_id: 'session-a', message_count: 0 }"
    return f"""
const S = {{
  session: {session_literal},
  messages: [],
  pendingFiles: [],
  toolCalls: [],
  busy: false,
  activeStreamId: null,
}};
let generation = 0;
let resolveCommand = null;
let resolveMoa = null;
let resolveMetadata = null;
let resolveRender = null;
let renderCount = 0;
const toasts = [];
function _bumpMessagesGeneration() {{ generation += 1; return generation; }}
function _isSessionCurrentPane(sid) {{ return !!(S.session && S.session.session_id === sid); }}
{ENSURE_OWNER_SRC or BASE_OWNER_FALLBACK_SRC}
function $(id) {{ return id === 'msg' ? {{ value: {command_json} }} : null; }}
function parseCommand(text) {{ return {{ name: text.slice(1).split(/\\s+/)[0], args: text.split(/\\s+/).slice(1).join(' ') }}; }}
function t(key) {{ return key; }}
function renderMessages() {{ renderCount += 1; }}
function showToast(message) {{ toasts.push(String(message)); }}
function autoResize() {{}}
function hideCmdDropdown() {{}}
function clearLiveToolCards() {{}}
function setBusy(value) {{ S.busy = value; }}
function setComposerStatus() {{}}
async function renderSessionList() {{
  if ({str(delay_render).lower()}) return new Promise(resolve => {{ resolveRender = resolve; }});
}}
async function newSession() {{
  await Promise.resolve();
  S.session = {{ session_id: 'session-new', message_count: 0 }};
  S.messages = [];
  return S.session.session_id;
}}
const _AGENT_COMMANDS_RUN_ON_WEBUI = new Set(['agent']);
function getAgentCommandMetadata(name) {{
  const result = {command_json} === '/agent' ? {{ name: 'agent' }}
    : {command_json} === '/plugin' ? {{ name: 'plugin', category: 'Plugin' }}
    : {command_json} === '/moa' ? {{ name: 'moa' }} : null;
  if ({str(metadata_delay).lower()}) return new Promise(resolve => {{ resolveMetadata = () => resolve(result); }});
  return Promise.resolve(result);
}}
function _deferredResult() {{ return new Promise(resolve => {{ resolveCommand = resolve; }}); }}
function handlePetSlashCommand() {{ return _deferredResult().then(() => ({{ handled: true, message: 'pet result' }})); }}
function executeAgentCommand() {{ return _deferredResult().then(() => 'agent result'); }}
function executeAgentPluginCommand() {{ return _deferredResult().then(() => 'plugin result'); }}
function api(url) {{
  if (String(url).includes('/api/commands/moa/resolve')) return new Promise(resolve => {{ resolveMoa = resolve; }});
  throw new Error('unexpected api '+url);
}}
const COMMANDS = [];
{SLASH_HELPERS}
async function runSlash() {{
  let text = {command_json};
  const literalSlash = false;
{ASYNC_SLASH_BLOCK}
}}
function switchToOtherSession() {{
  S.session = {{ session_id: 'session-b', message_count: 1 }};
  S.messages = [{{ role: 'assistant', content: 'session b existing' }}];
}}
(async () => {{
  const pending = runSlash();
  await Promise.resolve();
  await Promise.resolve();
  await new Promise(resolve => setTimeout(resolve, 0));
  if ({str(delay_render).lower()}) {{
    for (let i = 0; i < 20 && !resolveRender; i++) await new Promise(resolve => setTimeout(resolve, 0));
    if (!resolveRender) throw new Error('renderSessionList did not pause');
    switchToOtherSession();
    resolveRender();
    await Promise.resolve();
    await Promise.resolve();
  }}
  if ({str(switch_on_completion).lower()}) switchToOtherSession();
  await Promise.resolve();
  await Promise.resolve();
  if (resolveMetadata) resolveMetadata();
  await Promise.resolve();
  await Promise.resolve();
  if (resolveCommand) resolveCommand();
  if (resolveMoa) resolveMoa({{ usage: '/moa <prompt>' }});
  await pending;
  console.log(JSON.stringify({{ sessionId: S.session && S.session.session_id, messages: S.messages, renderCount, toasts }}));
}})().catch(err => {{ console.error(err.stack || String(err)); process.exit(1); }});
"""


def _clear_overlap_script() -> str:
    return f"""
let _messagesGeneration = 0;
function _bumpMessagesGeneration() {{ _messagesGeneration += 1; return _messagesGeneration; }}
{CAPTURE_SRC}
{CURRENT_SRC}
{COMMIT_SRC}
let _clearConversationOperation = null;
let _loadingSessionId = null;
{CLEAR_SERIAL_SRC}
{CLEAR_SRC}
const S = {{
  session: {{ session_id: 'same-session', messages: [{{ role: 'assistant', content: 'old' }}] }},
  messages: [{{ role: 'assistant', content: 'old' }}],
  toolCalls: [],
}};
const confirms = [];
const clearResolvers = [];
let clearCalls = 0;
let syncCalls = 0;
const clearControl = {{
  disabled: false,
  attrs: {{}},
  setAttribute(name, value) {{ this.attrs[name] = String(value); }},
}};
const toasts = [];
function t(key) {{ return key; }}
function $(id) {{ return id === 'btnClearConvModal' ? clearControl : null; }}
function _syncHermesPanelSessionActions() {{
  syncCalls += 1;
  const visibleMessages = (S.messages || []).filter(message => message && message.role && message.role !== 'tool').length;
  clearControl.disabled = !S.session || visibleMessages === 0 || !!_clearConversationOperation;
}}
function showToast(message) {{ toasts.push(String(message)); }}
function setStatus(message) {{ toasts.push(String(message)); }}
function syncTopbar() {{}}
function renderMessages() {{}}
function showConfirmDialog() {{ return new Promise(resolve => confirms.push(resolve)); }}
async function loadSession() {{ toasts.push('reconcile'); }}
async function api(url) {{
  if (String(url) !== '/api/session/clear') throw new Error('unexpected API request: '+url);
  clearCalls += 1;
  return await new Promise(resolve => clearResolvers.push(resolve));
}}
function _commitTranscriptReplacement(ticket, commit) {{
  if (!_transcriptReplacementIsCurrent(ticket) || ticket.used) return false;
  ticket.used = true;
  _bumpMessagesGeneration();
  commit();
  return true;
}}
(async () => {{
  const first = clearConversation();
  await Promise.resolve();
  if (confirms.length !== 1) throw new Error('first confirmation did not open');
  confirms[0](true);
  for (let i = 0; i < 20 && !clearResolvers.length; i++) await new Promise(resolve => setTimeout(resolve, 0));
  if (clearResolvers.length !== 1) throw new Error('first clear request did not start');
  const pendingState = {{ disabled: clearControl.disabled, busy: clearControl.attrs['aria-busy'] }};
  _syncHermesPanelSessionActions();
  const pendingResyncState = {{ disabled: clearControl.disabled, busy: clearControl.attrs['aria-busy'] }};
  const duplicate = clearConversation();
  await Promise.resolve();
  if (confirms.length !== 2) throw new Error('duplicate confirmation did not open');
  confirms[1](true);
  await Promise.resolve();
  clearResolvers[0]({{ session: {{ session_id: 'same-session', messages: [] }} }});
  await first;
  await Promise.resolve();
  const later = clearConversation();
  await Promise.resolve();
  if (confirms.length !== 3) throw new Error('later confirmation did not open');
  confirms[2](true);
  for (let i = 0; i < 20 && clearResolvers.length < 2; i++) await new Promise(resolve => setTimeout(resolve, 0));
  if (clearResolvers.length !== 2) throw new Error('later clear request did not start');
  clearResolvers[1]({{ session: {{ session_id: 'same-session', messages: [] }} }});
  await later;
  console.log(JSON.stringify({{ clearCalls, messages: S.messages, toasts, pendingState, pendingResyncState, finalState: {{ disabled: clearControl.disabled, busy: clearControl.attrs['aria-busy'] }}, syncCalls, duplicatePending: true }}));
  void duplicate;
}})().catch(err => {{ console.error(err.stack || String(err)); process.exit(1); }});
"""


def _delayed_render_owner_script() -> str:
    owner_block = (
        "if(!S.session){await newSession();await renderSessionList();}"
        "const sid=S.session&&S.session.session_id;"
    )
    owner_src = ""
    if ENSURE_OWNER_SRC:
        owner_src = ENSURE_OWNER_SRC
        owner_block = "const sid=await _ensureSessionOwner();"
    return f"""
const S = {{ session: null, messages: [] }};
let resolveRender = null;
async function renderSessionList() {{ return new Promise(resolve => resolveRender = resolve); }}
async function newSession() {{ S.session = {{ session_id: 'session-a' }}; return 'session-a'; }}
function _isSessionCurrentPane(sid) {{ return !!S.session && S.session.session_id === sid; }}
{owner_src}
async function runAction() {{
  {owner_block}
  if (sid) S.messages.push({{ role: 'user', content: 'action' }});
}}
(async () => {{
  const action = runAction();
  for (let i = 0; i < 20 && !resolveRender; i++) await new Promise(resolve => setTimeout(resolve, 0));
  S.session = {{ session_id: 'session-b' }};
  resolveRender();
  await action;
  console.log(JSON.stringify({{ sessionId: S.session.session_id, messages: S.messages, usedProductionOwner: {str(bool(ENSURE_OWNER_SRC)).lower()} }}));
}})().catch(err => {{ console.error(err.stack || String(err)); process.exit(1); }});
"""


def _creation_await_owner_script() -> str:
    return f"""
let _loadingSessionId = null;
const S = {{ session: null }};
{NEW_SESSION_OWNER_GUARD_SRC}
function installCreatedSession(data, captureOwner) {{
  if (!_newSessionOwnerResponseIsCurrent(data, captureOwner)) return null;
  S.session = data.session;
  return S.session.session_id;
}}
const created = {{ session: {{ session_id: 'session-a' }} }};
const acceptedWithoutTransition = installCreatedSession(created, true);
S.session = null;
_loadingSessionId = 'session-b';
const rejectedDuringLoad = installCreatedSession(created, true);
_loadingSessionId = null;
S.session = {{ session_id: 'session-b' }};
const rejectedAfterInstall = installCreatedSession(created, true);
console.log(JSON.stringify({{ acceptedWithoutTransition, rejectedDuringLoad, rejectedAfterInstall, sessionId: S.session.session_id }}));
"""


def _normal_send_chat_start_switch_script(stage: str = "chat_start") -> str:
    return f"""
let _sendInProgress = false;
let _sendInProgressSid = null;
let _pendingPickMatch = null;
let _forcedSkillDirectivePending = null;
let _queueDrainSid = null;
let _approvalSessionId = null;
let _clarifySessionId = null;
let resolveStart = null;
let resolveUpload = null;
let resolveDirective = null;
let startCalls = 0;
let attachCalls = 0;
let restoreArgs = null;
let clearOptimisticCalls = 0;
const input = {{ value: 'hello', style: {{}} }};
const S = {{
  session: {{ session_id: 'session-a', workspace: '/ws', model: 'model', profile: 'default' }},
  messages: [],
  pendingFiles: [],
  pendingSelections: [],
  toolCalls: [],
  busy: false,
  activeStreamId: null,
  activeProfile: 'default',
}};
const window = {{ _defaultMessageMode: 'steer' }};
const document = {{ querySelector() {{ return null; }} }};
const localStorage = {{ setItem() {{}}, removeItem() {{}}, getItem() {{ return null; }} }};
const INFLIGHT = {{}};
const COMMANDS = [];
const _AGENT_COMMANDS_RUN_ON_WEBUI = new Set();
function $(id) {{ return id === 'msg' ? input : null; }}
function _isSessionCurrentPane(sid) {{ return !!S.session && S.session.session_id === sid; }}
{ENSURE_OWNER_SRC}
function _composerTextWithPendingSelections() {{ return input.value; }}
function _flushSelectionBlocksToComposer() {{}}
function _clearStaleBusyStateBeforeSend() {{ return false; }}
function _clearComposerAfterQueuedSelectionSend() {{}}
function _chatPayloadModelState() {{ return {{ model: 'model', model_provider: null }}; }}
function _dismissHandoffHint() {{}}
function _bumpMessagesGeneration() {{ return 1; }}
function _runOptionalPreStartUiStep(_label, fn) {{ if (typeof fn === 'function') fn(); }}
function _runOptionalPostStartUiStep(_label, fn) {{ if (typeof fn === 'function') fn(); }}
function _clearPendingSessionModel() {{}}
function _clearComposerDraft() {{}}
function _restoreComposerDraftAfterFailedSend(draftText, files, sid) {{ restoreArgs = {{ draftText, sid }}; }}
function _clearOptimisticSessionStreaming() {{ clearOptimisticCalls += 1; }}
function clearOptimisticSessionStreaming() {{ clearOptimisticCalls += 1; }}
function _fetchYoloState() {{}}
function updateSendBtn() {{}}
function setComposerStatus() {{}}
function setStatus() {{}}
function setBusy(value) {{ S.busy = !!value; }}
function renderMessages() {{}}
function renderTray() {{}}
function autoResize() {{}}
function hideCmdDropdown() {{}}
function clearLiveToolCards() {{}}
function ensureLiveWorklogShell() {{}}
function appendThinking() {{}}
function upsertActiveSessionForLocalTurn() {{}}
function markInflight() {{}}
function saveInflightState() {{}}
function startApprovalPolling() {{}}
function startClarifyPolling() {{}}
function stopApprovalPolling() {{}}
function stopClarifyPolling() {{}}
function hideApprovalCard() {{}}
function hideClarifyCard() {{}}
function removeThinking() {{}}
function showToast() {{}}
function syncTopbar() {{}}
function updateQueueBadge() {{}}
function queueSessionMessage() {{}}
function t(key) {{ return key; }}
async function uploadPendingFiles() {{
  if ({json.dumps(stage)} === 'upload') return new Promise(resolve => {{ resolveUpload = resolve; }});
  return [];
}}
function attachLiveStream() {{ attachCalls += 1; }}
function api(url) {{
  if (String(url) !== '/api/chat/start') throw new Error('unexpected API request: ' + String(url));
  startCalls += 1;
  return new Promise((resolve, reject) => {{
    resolveStart = () => {{
      if ({json.dumps(stage)} === 'chat_start_error') {{
        const error = new Error('start failed');
        error.status = 502;
        reject(error);
      }} else resolve({{ stream_id: 'stream-a' }});
    }};
  }});
}}
{SEND_SRC}
(async () => {{
  if ({json.dumps(stage)} === 'directive') {{
    _forcedSkillDirectivePending = {{
      sessionId: 'session-a',
      promise: new Promise(resolve => {{ resolveDirective = resolve; }}),
    }};
  }}
  const sendPromise = send();
  if ({json.dumps(stage)} === 'upload') {{
    for (let i = 0; i < 20 && !resolveUpload; i++) await new Promise(resolve => setTimeout(resolve, 0));
    if (!resolveUpload) throw new Error('upload was not reached');
  }} else if ({json.dumps(stage)} === 'directive') {{
    for (let i = 0; i < 20 && !resolveDirective; i++) await new Promise(resolve => setTimeout(resolve, 0));
    if (!resolveDirective) throw new Error('directive was not reached');
  }} else {{
    for (let i = 0; i < 20 && !resolveStart; i++) await new Promise(resolve => setTimeout(resolve, 0));
    if (!resolveStart) throw new Error('chat/start was not reached');
  }}
  S.session = {{ session_id: 'session-b', workspace: '/ws', model: 'model', profile: 'default' }};
  S.messages = [{{ role: 'assistant', content: 'session b existing' }}];
  S.busy = false;
  S.activeStreamId = null;
  if ({json.dumps(stage)} === 'chat_start_error') INFLIGHT['session-a'] = {{ streamId: 'stream-a' }};
  if ({json.dumps(stage)} === 'upload') resolveUpload([]);
  else if ({json.dumps(stage)} === 'directive') resolveDirective({{ directive: 'forced' }});
  else resolveStart();
  await sendPromise;
  console.log(JSON.stringify({{
    startCalls,
    sessionId: S.session && S.session.session_id,
    messages: S.messages,
    busy: S.busy,
    activeStreamId: S.activeStreamId,
    attachCalls,
    inflightKeys: Object.keys(INFLIGHT),
    restoreArgs,
    clearOptimisticCalls,
  }}));
}})().catch(err => {{ console.error(err.stack || String(err)); process.exit(1); }});
"""


def _steer_loading_switch_script() -> str:
    return f"""
let _loadingSessionId = null;
const file = {{ name: 'draft.txt', size: 1, lastModified: 1 }};
const S = {{
  session: {{ session_id: 'session-a', active_stream_id: 'stream-a', model: 'model' }},
  activeStreamId: 'stream-a',
  pendingFiles: [file],
  busy: true,
}};
let resolveSteer = null;
let indicatorCalls = 0;
let clearDraftCalls = 0;
function _isSessionCurrentPane(sid) {{
  return !!S.session && S.session.session_id === sid && (!_loadingSessionId || _loadingSessionId === sid);
}}
function _chatPayloadModelState() {{ return {{ model: 'model', model_provider: null }}; }}
function _clearComposerDraft() {{ clearDraftCalls += 1; }}
function _saveComposerDraftNow() {{ return Promise.resolve(); }}
function _showSteerIndicator() {{ indicatorCalls += 1; }}
function _showSteerRecovery() {{ indicatorCalls += 100; }}
function renderTray() {{}}
function setComposerStatus() {{}}
function autoResize() {{}}
function showToast() {{}}
function updateQueueBadge() {{}}
function queueSessionMessage() {{}}
function t(key) {{ return key; }}
function api(url) {{
  if (url !== '/api/chat/steer') throw new Error('unexpected API request: ' + url);
  return new Promise(resolve => {{ resolveSteer = resolve; }});
}}
{STEER_SRC}
(async () => {{
  const steerPromise = _trySteer('late steer', false);
  for (let i = 0; i < 20 && !resolveSteer; i++) await new Promise(resolve => setTimeout(resolve, 0));
  if (!resolveSteer) throw new Error('steer request did not start');
  _loadingSessionId = 'session-b';
  resolveSteer({{ accepted: true }});
  await steerPromise;
  console.log(JSON.stringify({{
    sessionId: S.session && S.session.session_id,
    pendingFiles: S.pendingFiles.map(item => item.name),
    indicatorCalls,
    clearDraftCalls,
  }}));
}})().catch(err => {{ console.error(err.stack || String(err)); process.exit(1); }});
"""


def _background_polling_script(*, switch_session: bool = False, change_generation: bool = False) -> str:
    messages_path = str(REPO / "static" / "messages.js")
    return f"""
const fs = require('fs');
const messagesSrc = fs.readFileSync({json.dumps(messages_path)}, 'utf8');
let depth = 0;
const start = messagesSrc.indexOf('function startBackgroundPolling(');
const brace = messagesSrc.indexOf('{{', start);
for (let i = brace; i < messagesSrc.length; i++) {{
  if (messagesSrc[i] === '{{') depth++;
  else if (messagesSrc[i] === '}}' && --depth === 0) {{ var backgroundSrc = messagesSrc.slice(start, i + 1); break; }}
}}
let _messagesGeneration = 0;
let resolveStatus = null;
let timerCount = 0;
const hidden = [];
const toasts = [];
const S = {{ session: {{ session_id: 'parent' }}, messages: [], busy: false, activeStreamId: null }};
const _bgPollTimers = {{}};
function _isSessionCurrentPane(sid) {{ return !!(S.session && S.session.session_id === sid); }}
function _bumpMessagesGeneration() {{ _messagesGeneration += 1; }}
function hideBackgroundBadge(taskId) {{ hidden.push(taskId); }}
function renderMessages() {{ S.rendered = true; }}
function showToast(message) {{ toasts.push(message); }}
function t(key) {{ return key; }}
function $(id) {{ return null; }}
function api() {{ return new Promise(resolve => {{ resolveStatus = resolve; }}); }}
function setTimeout(fn) {{ timerCount += 1; return timerCount; }}
{BACKGROUND_SRC}
startBackgroundPolling('parent', 'task-1', 'prompt');
(async () => {{
  await Promise.resolve();
  if ({str(switch_session).lower()}) S.session = {{ session_id: 'other' }};
  if ({str(change_generation).lower()}) _bumpMessagesGeneration();
  resolveStatus({{ results: [{{ task_id: 'task-1', answer: 'done' }}] }});
  await Promise.resolve();
  console.log(JSON.stringify({{ hidden, messages: S.messages, rendered: !!S.rendered, toasts, timerCount, sessionId: S.session && S.session.session_id }}));
}})().catch(err => {{ console.error(err.stack || String(err)); process.exit(1); }});
"""


def _full_history_writer_race_script(writer: str) -> str:
    sessions_path = str(REPO / "static" / "sessions.js")
    messages_path = str(REPO / "static" / "messages.js")
    if writer == "gateway":
        writer_src = GATEWAY_SRC
        writer_setup = """
const gatewayEvents = {};
class EventSource {
  constructor() {}
  addEventListener(name, handler) { gatewayEvents[name] = handler; }
}
function stopGatewaySSE() { _gatewaySSE = null; }
function stopGatewayPollFallback() {}
function _installSidebarSseFocusHook() {}
function _sidebarSseBackgrounded() { return false; }
function _isDuplicateGatewaySessionSnapshot() { return false; }
function _isExternalSession() { return true; }
function _externalImportPayload() { return {}; }
function _isCliImportRefreshPrefixMatch() { return true; }
function highlightCode() {}
function renderSessionList() {}
let _gatewaySSE = null;
let _gatewaySSEWarningShown = false;
let _gatewayPollTimer = null;
let _gatewayPollVisibilityHandler = null;
let _gatewayProbeInFlight = false;
const document = {
  hidden: false,
  addEventListener() {},
  removeEventListener() {},
};
const location = { href: 'http://localhost/' };
"""
        writer_start = """
startGatewaySSE();
if (typeof gatewayEvents.sessions_changed !== 'function') throw new Error('gateway event handler was not installed');
const ensurePromise = _ensureAllMessagesLoaded();
gatewayEvents.sessions_changed({data: JSON.stringify({sessions: [{session_id: 'parent', updated_at: 2, message_count: 2, source: 'cli'}]})});
await Promise.resolve();
if (typeof resolveImport !== 'function') throw new Error('gateway import request was not started');
resolveImport({session: {session_id: 'parent', messages: [
  {role: 'assistant', content: 'old'},
  {role: 'assistant', content: 'gateway result'},
]}});
await Promise.resolve();
if (typeof resolveFullLoad !== 'function') throw new Error('full-history request was not started');
resolveFullLoad({session: {session_id: 'parent', messages: [
  {role: 'assistant', content: 'old'},
], tool_calls: [], message_count: 1}});
await ensurePromise;
console.log(JSON.stringify({messages: S.messages.map(message => message.content), generation: _messagesGeneration}));
"""
    elif writer == "background":
        writer_src = BACKGROUND_SRC
        writer_setup = """
function hideBackgroundBadge() {}
function renderMessages() {}
function showToast() {}
function t(key) { return key; }
const _bgPollTimers = {};
function setTimeout() { return 1; }
"""
        writer_start = """
const ensurePromise = _ensureAllMessagesLoaded();
startBackgroundPolling('parent', 'task-1', 'prompt');
await Promise.resolve();
if (typeof resolveStatus !== 'function') throw new Error('background status request was not started');
resolveStatus({results: [{task_id: 'task-1', answer: 'background result'}]});
await Promise.resolve();
if (typeof resolveFullLoad !== 'function') throw new Error('full-history request was not started');
resolveFullLoad({session: {session_id: 'parent', messages: [
  {role: 'assistant', content: 'old'},
], tool_calls: [], message_count: 1}});
await ensurePromise;
console.log(JSON.stringify({messages: S.messages.map(message => message.content), generation: _messagesGeneration}));
"""
    else:
        raise ValueError(writer)
    api_body = """
async function api(url) {
  if (String(url).startsWith('/api/session?')) {
    return await new Promise(resolve => { resolveFullLoad = resolve; });
  }
  if (String(url).includes('/api/session/import_cli')) {
    return await new Promise(resolve => { resolveImport = resolve; });
  }
  if (String(url).includes('/api/background/status')) {
    return await new Promise(resolve => { resolveStatus = resolve; });
  }
  throw new Error('unexpected API request: ' + url);
}
"""
    return f"""
const fs = require('fs');
const sessionsSrc = fs.readFileSync({json.dumps(sessions_path)}, 'utf8');
const messagesSrc = fs.readFileSync({json.dumps(messages_path)}, 'utf8');
function extractFunction(source, name) {{
  let start = source.indexOf(`async function ${{name}}(`);
  if (start < 0) start = source.indexOf(`function ${{name}}(`);
  if (start < 0) throw new Error(`missing function ${{name}}`);
  const brace = source.indexOf('{{', start);
  let depth = 0;
  for (let i = brace; i < source.length; i++) {{
    if (source[i] === '{{') depth++;
    else if (source[i] === '}}' && --depth === 0) return source.slice(start, i + 1);
  }}
  throw new Error(`unterminated ${{name}}`);
}}
const ensureAllSrc = sessionsSrc.slice(
  sessionsSrc.indexOf('let _loadingOlder = false;'),
  sessionsSrc.indexOf('const SESSION_ARCHIVED_PAGE_SIZE')
);
let _messagesTruncated = true;
let _loadingSessionId = 'parent';
let resolveFullLoad = null;
let resolveImport = null;
let resolveStatus = null;
const S = {{session: {{session_id: 'parent', message_count: 0}}, messages: [], toolCalls: [], busy: false, activeStreamId: null}};
const window = {{_showCliSessions: true, _carryForwardEphemeralTurnFields: (_current, incoming) => incoming}};
function _syncToolCallsForLoadedMessages() {{}}
function clearLiveToolCards() {{}}
function syncTopbar() {{}}
function _setSessionViewedCount() {{}}
function _isSessionActivelyViewedForList() {{ return true; }}
function _messageRenderableMessageCount() {{ return S.messages.length; }}
function _currentMessageRenderWindowSize() {{ return S.messages.length; }}
function _hydrateTodosFromSession() {{}}
function scheduleTodosRefresh() {{}}
function _setSessionCompletionUnread() {{}}
{api_body}
{ENSURE_ALL_FN}
{writer_setup}
{writer_src}
(async () => {{
{writer_start}
}})().catch(err => {{ console.error(err.stack || String(err)); process.exit(1); }});
"""


def _compression_post_render_generation_script() -> str:
    return f"""
let _messagesGeneration = 0;
function _bumpMessagesGeneration() {{ _messagesGeneration += 1; return _messagesGeneration; }}
{CAPTURE_SRC}
{CURRENT_SRC}
{COMMIT_SRC}
{APPLY_COMPRESSION_SRC}
const S = {{
  session: {{ session_id: 'same-session', workspace: '/ws' }},
  messages: [{{ role: 'assistant', content: 'tail' }}],
  toolCalls: [],
}};
let queueCalls = 0;
let compressionUiCalls = 0;
let composerCalls = 0;
let renderCalls = 0;
let busyCalls = 0;
function $(id) {{ return null; }}
function clearLiveToolCards() {{}}
function syncTopbar() {{}}
function renderMessages() {{ renderCalls += 1; }}
function _isContextCompactionMessage() {{ return false; }}
function msgContent(message) {{ return message && message.content || ''; }}
function updateQueueBadge() {{ queueCalls += 1; }}
function setCompressionUi() {{ compressionUiCalls += 1; }}
function setComposerStatus() {{ composerCalls += 1; }}
function setBusy() {{ busyCalls += 1; }}
function _setCompressionSessionLock() {{}}
async function renderSessionList() {{ _bumpMessagesGeneration(); }}
const ticket = _captureTranscriptReplacement();
(async () => {{
  const applied = await _applyManualCompressionResult(
    {{ session: {{ session_id: 'same-session', workspace: '/ws', messages: [{{ role: 'assistant', content: 'compressed' }}], tool_calls: [] }}, summary: {{}} }},
    '', 1, '/compress', ticket
  );
  console.log(JSON.stringify({{ applied, messages: S.messages.map(message => message.content), queueCalls, compressionUiCalls, composerCalls, renderCalls, busyCalls }}));
}})().catch(err => {{ console.error(err.stack || String(err)); process.exit(1); }});
"""


def _compression_cleanup_race_script(*, resumed: bool = False, newer_stream: bool = False) -> str:
    compression_entry = "resumeManualCompressionForSession('same-session')" if resumed else "_runManualCompression('')"
    return f"""
let _messagesGeneration = 0;
function _bumpMessagesGeneration() {{ _messagesGeneration += 1; return _messagesGeneration; }}
{CAPTURE_SRC}
{CURRENT_SRC}
{COMMIT_SRC}
{POLL_COMPRESSION_SRC}
{CLEAR_COMPRESSION_SRC}
{COMPRESSION_OPERATION_SRC}
{COMPRESSION_SRC}
const window = {{ _compressionUi: null }};
const S = {{
  session: {{ session_id: 'same-session', workspace: '/ws', messages: [] }},
  messages: [{{ role: 'assistant', content: 'old' }}],
  toolCalls: [],
  busy: false,
  activeStreamId: null,
}};
let compressionLock = null;
let composerStatus = '';
let statusCalls = 0;
let resolveFirstStatus = null;
let resolveStatus = null;
function _manualCompressionSleep() {{ return Promise.resolve(); }}
function _manualCompressionVisibleMessages() {{ return S.messages.filter(message => message && message.role !== 'tool'); }}
function _compressionAnchorMessageKey() {{ return 'anchor'; }}
function msgContent(message) {{ return message && message.content || ''; }}
function _isContextCompactionMessage() {{ return false; }}
function clearLiveToolCards() {{}}
function renderMessages() {{}}
function renderSessionList() {{}}
function setBusy(value) {{ S.busy = value; }}
function setComposerStatus(value) {{ composerStatus = value || ''; }}
function setCompressionUi(state) {{ window._compressionUi = state; if (state && state.sessionId) compressionLock = state.sessionId; }}
function clearCompressionUi() {{ window._compressionUi = null; compressionLock = null; }}
function _setCompressionSessionLock(value) {{ compressionLock = value || null; }}
function t(key) {{ return key; }}
async function _applyManualCompressionResult() {{ return false; }}
async function api(url) {{
  if (String(url).includes('/api/session/compress/status')) {{
    statusCalls += 1;
    return await new Promise(resolve => {{
      if ({str(resumed).lower()} && statusCalls === 1) resolveFirstStatus = resolve;
      else resolveStatus = resolve;
    }});
  }}
  if (String(url).startsWith('/api/session?')) return {{ session: {{ ...S.session, messages: S.messages }} }};
  if (String(url) === '/api/session/compress/start') return {{ status: 'running' }};
  throw new Error('unexpected API request: ' + url);
}}
(async () => {{
  const operation = {compression_entry};
  await new Promise(resolve => setTimeout(resolve, 0));
  if (resolveFirstStatus) resolveFirstStatus({{ status: 'running' }});
  for (let i = 0; i < 20 && !resolveStatus; i++) await new Promise(resolve => setTimeout(resolve, 0));
  if (!resolveStatus) throw new Error('compression poll did not start');
  _bumpMessagesGeneration();
  S.messages.push({{ role: 'user', content: 'newer writer' }});
  if ({str(newer_stream).lower()}) {{ S.busy = true; S.activeStreamId = 'stream-new'; composerStatus = 'streaming'; }}
  resolveStatus({{ status: 'done', session: {{ ...S.session, messages: [{{ role: 'assistant', content: 'compressed' }}] }}, summary: {{}} }});
  await operation;
  await Promise.resolve();
  console.log(JSON.stringify({{
    busy: S.busy,
    activeStreamId: S.activeStreamId,
    compressionUi: window._compressionUi,
    compressionLock: compressionLock,
    composerStatus,
  }}));
}})().catch(err => {{ console.error(err.stack || String(err)); process.exit(1); }});
"""


def _clear_conversation_race_script(race_stage: str) -> str:
    return f"""
let _messagesGeneration = 0;
function _bumpMessagesGeneration() {{ _messagesGeneration += 1; return _messagesGeneration; }}
{CAPTURE_SRC}
{CURRENT_SRC}
{COMMIT_SRC}
let _clearConversationOperation = null;
let _loadingSessionId = null;
{CLEAR_SERIAL_SRC}
{CLEAR_SRC}
const S = {{
  session: {{ session_id: 'same-session', messages: [{{ role: 'assistant', content: 'old' }}] }},
  messages: [{{ role: 'assistant', content: 'old' }}],
  toolCalls: [],
}};
let resolveConfirm = null;
let resolveClear = null;
let reconcileCalls = 0;
const toasts = [];
function t(key) {{ return key; }}
function $(id) {{ return null; }}
function showToast(message) {{ toasts.push(String(message)); }}
function setStatus(message) {{ toasts.push(String(message)); }}
function syncTopbar() {{}}
function renderMessages() {{}}
function showConfirmDialog() {{ return new Promise(resolve => {{ resolveConfirm = resolve; }}); }}
function _isSessionCurrentPane(sid) {{
  return !!S.session && S.session.session_id === sid && (!_loadingSessionId || _loadingSessionId === sid);
}}
async function loadSession() {{ reconcileCalls += 1; }}
async function api(url) {{
  if (String(url) === '/api/session/clear') return await new Promise(resolve => {{ resolveClear = resolve; }});
  throw new Error('unexpected API request: ' + url);
}}
function _commitTranscriptReplacement(ticket, commit) {{
  if (!_transcriptReplacementIsCurrent(ticket) || ticket.used) return false;
  ticket.used = true;
  _bumpMessagesGeneration();
  commit();
  return true;
}}
(async () => {{
  const clearPromise = clearConversation();
  await Promise.resolve();
  if (!resolveConfirm) throw new Error('confirmation did not open');
  if ({json.dumps(race_stage)} === 'confirm') {{
    _bumpMessagesGeneration();
    S.messages.push({{ role: 'user', content: 'newer writer' }});
  }}
  resolveConfirm(true);
  for (let i = 0; i < 20 && !resolveClear; i++) await new Promise(resolve => setTimeout(resolve, 0));
  if (!resolveClear) throw new Error('clear request did not start');
  if ({json.dumps(race_stage)} === 'post') {{
    _bumpMessagesGeneration();
    S.messages.push({{ role: 'user', content: 'newer writer' }});
  }}
  if ({json.dumps(race_stage)} === 'switched') {{
    S.session = {{ session_id: 'other-session', messages: [{{ role: 'assistant', content: 'other pane' }}] }};
    S.messages = [{{ role: 'assistant', content: 'other pane' }}];
    S.toolCalls = [{{ id: 'other-tool' }}];
  }}
  if ({json.dumps(race_stage)} === 'loading') {{
    _loadingSessionId = 'other-session';
    S.toolCalls = [{{ id: 'same-tool' }}];
  }}
  resolveClear({{ session: {{ session_id: 'same-session', messages: [] }} }});
  await clearPromise;
  await new Promise(resolve => setTimeout(resolve, 0));
  console.log(JSON.stringify({{ sessionId: S.session && S.session.session_id, messages: S.messages, toolCalls: S.toolCalls, toasts, reconcileCalls }}));
}})().catch(err => {{ console.error(err.stack || String(err)); process.exit(1); }});
"""


def test_download_handler_uses_localized_feedback_and_full_history_gate():
    if "_readFullSessionSnapshot(sid)" not in BOOT_JS:
        pytest.skip("respec-only download contract is absent on the base checkout")
    assert "showToast((typeof t==='function'&&t('download_transcript_busy_full'))" in BOOT_JS
    assert "showToast((typeof t==='function'&&t('download_transcript_failed_full'))" in BOOT_JS
    assert "showToast((typeof t==='function'&&t('download_transcript_preparing_full'))" in BOOT_JS
    assert "showToast((typeof t==='function'&&t('download_transcript_changed_full'))" in BOOT_JS
    assert "await _readFullSessionSnapshot(sid);" in BOOT_JS

    result = _run_node(_download_script())

    assert result["ensureCalls"] == 1
    assert result["clicked"] is True
    assert result["disabled"] is False
    assert result["ariaBusy"] is None
    assert result["downloadName"] == "hermes-foreign-1.md"
    assert "OLD_TRANSCRIPT_UNIQUE" in result["downloadedText"]
    assert result["truncated"] is True
    assert result["messageCount"] == 1
    assert result["toasts"] == [
        {
            "message": "Preparing full transcript…",
            "duration": 2000,
            "kind": None,
        }
    ]


def test_download_handler_emits_feedback_for_busy_changed_and_failed_states():
    active = _run_node(_download_script(active_stream=True))
    assert active["ensureCalls"] == 0
    assert active["clicked"] is False
    assert active["downloadedText"] is None
    assert active["toasts"] == [
        {
            "message": "Wait for the current response to finish before downloading the full transcript.",
            "duration": 3000,
            "kind": "warning",
        }
    ]

    switched = _run_node(_download_script(switch_session=True))
    assert switched["ensureCalls"] == 1
    assert switched["clicked"] is False
    assert switched["downloadedText"] is None
    assert switched["sessionId"] == "foreign-2"
    assert switched["toasts"] == [
        {
            "message": "Preparing full transcript…",
            "duration": 2000,
            "kind": None,
        },
        {
            "message": "The conversation changed while the full transcript was loading. Try again.",
            "duration": 3000,
            "kind": "warning",
        },
    ]

    failed = _run_node(_download_script(fail_load=True))
    assert failed["ensureCalls"] == 1
    assert failed["clicked"] is False
    assert failed["downloadedText"] is None
    assert failed["toasts"] == [
        {
            "message": "Preparing full transcript…",
            "duration": 2000,
            "kind": None,
        },
        {
            "message": "Failed to load the full transcript.",
            "duration": 4000,
            "kind": "error",
        },
    ]

    changed_generation = _run_node(_download_script(generation_change=True))
    assert changed_generation["ensureCalls"] == 1
    assert changed_generation["clicked"] is False
    assert changed_generation["downloadedText"] is None
    assert changed_generation["toasts"] == [
        {
            "message": "Preparing full transcript…",
            "duration": 2000,
            "kind": None,
        },
        {
            "message": "The conversation changed while the full transcript was loading. Try again.",
            "duration": 3000,
            "kind": "warning",
        },
    ]


def test_download_handler_owns_one_pending_operation_and_cleans_busy_state():
    result = _run_node(_download_double_invocation_script())

    assert result["beforeSecond"] == {
        "calls": 1,
        "disabled": True,
        "ariaBusy": "true",
        "pending": 1,
    }
    assert result["duringSecond"]["calls"] == 1
    assert result["duringSecond"]["pending"] == 1
    assert result["duringSecond"]["toasts"][-1]["message"] == "Preparing full transcript…"
    assert result["ensureCalls"] == 1
    assert result["clicked"] == 1
    assert result["pending"] == 0
    assert result["disabled"] is False
    assert result["ariaBusy"] is None


def test_artifacts_renderer_preserves_partial_count_before_full_load():
    result = _run_node(_artifact_script(seed_existing_dom=True))

    assert result["ensureCalls"] == 1
    assert "Loading full history…" not in result["htmlBeforeAwait"]
    assert 'data-artifact-path="new/live.txt"' in result["htmlBeforeAwait"]
    assert result["countBeforeAwait"] == "1"
    assert result["count"] == "2"
    assert 'data-artifact-path="old/deep.txt"' in result["html"]
    assert 'data-artifact-path="new/live.txt"' in result["html"]
    assert result["truncated"] is True
    assert result["messageCount"] == 1


def test_artifacts_renderer_falls_back_to_partial_list_after_failure():
    result = _run_node(_artifact_script(fail_load=True, seed_existing_dom=True))

    assert result["ensureCalls"] == 1
    assert "Loading full history…" not in result["htmlBeforeAwait"]
    assert 'data-artifact-path="new/live.txt"' in result["htmlBeforeAwait"]
    assert result["countBeforeAwait"] == "1"
    assert result["count"] == "1"
    assert 'data-artifact-path="new/live.txt"' in result["html"]
    assert 'data-artifact-path="old/deep.txt"' not in result["html"]
    assert result["truncated"] is True


def test_artifacts_renderer_keeps_live_content_during_live_turn_on_truncated_session():
    result = _run_node(_artifact_script(active_stream=True, seed_existing_dom=True))

    assert result["ensureCalls"] == 0
    assert "Loading full history…" not in result["htmlBeforeAwait"]
    assert "Loading full history…" not in result["html"]
    assert 'data-artifact-path="new/live.txt"' in result["html"]
    assert result["countBeforeAwait"] == "1"
    assert result["count"] == "1"
    assert result["truncated"] is True


def test_artifacts_renderer_skips_full_load_when_panel_is_not_really_visible():
    hidden_states = [
        {"active_tab": "files", "tab_hidden": False, "panel_hidden": False},
        {"active_tab": "artifacts", "tab_hidden": True, "panel_hidden": False},
        {"active_tab": "artifacts", "tab_hidden": False, "panel_hidden": True},
    ]

    for state in hidden_states:
        result = _run_node(_artifact_script(seed_existing_dom=True, **state))
        assert result["ensureCalls"] == 0
        assert 'data-artifact-path="new/live.txt"' in result["html"]
        assert result["count"] == "1"
        assert result["truncated"] is True


def test_artifacts_renderer_repaints_after_fulfilled_generation_abort():
    result = _run_node(_artifact_script(seed_existing_dom=True, generation_abort=True))

    assert result["ensureCalls"] == 1
    assert "Loading full history…" not in result["htmlBeforeAwait"]
    assert result["countBeforeAwait"] == "1"
    assert result["count"] == "1"
    assert 'data-artifact-path="new/live.txt"' in result["html"]
    assert 'data-artifact-path="old/deep.txt"' not in result["html"]
    assert result["truncated"] is True


def test_artifacts_renderer_deduplicates_pending_and_settled_same_generation_snapshot():
    result = _run_node(_artifact_reuse_script())

    assert result["samePendingPromise"] is True
    assert result["beforeResolve"]["ensureCalls"] == 1
    assert result["beforeResolve"]["count"] == "1"
    assert result["afterFirst"]["count"] == "2"
    assert result["ensureCalls"] == 1
    assert result["sameSettledPromise"] is True
    assert 'data-artifact-path="old/deep.txt"' in result["finalHtml"]


def test_artifacts_renderer_rejects_stale_record_completion_by_identity():
    result = _run_node(_artifact_stale_record_script())

    assert result["ensureCalls"] == 2
    assert 'data-artifact-path="fresh/current.txt"' in result["html"]
    assert 'data-artifact-path="stale/old.txt"' not in result["html"]


def test_artifacts_renderer_rejects_completion_after_load_visit_changes():
    result = _run_node(_artifact_load_visit_guard_script())

    assert result["ensureCalls"] == 1
    assert 'data-artifact-path="current/live.txt"' in result["html"]
    assert 'data-artifact-path="stale/old.txt"' not in result["html"]


def test_ensure_all_messages_loaded_preserves_settled_turn_that_finished_mid_fetch():
    result = _run_node(_ensure_all_messages_loaded_script(goal_turn_during_fetch=True))

    assert result["messages"] == [
        {"role": "user", "content": "NEW TURN"},
        {"role": "assistant", "content": "NEW ANSWER"},
    ]
    assert result["toolCalls"] == [{"id": "goal-tc"}]
    assert result["truncated"] is True
    assert result["busy"] is False
    assert result["activeStreamId"] is None
    assert result["bumpCalls"] >= 1
    assert result["syncCalls"] == 0
    assert result["goalMessagesAfterResponse"] == [
        {"role": "user", "content": "NEW TURN"},
        {"role": "assistant", "content": "NEW ANSWER"},
    ]
    assert result["goalBusyAfterResponse"] is False
    assert result["oldestIdx"] == 99
    assert result["count"] == 2


def test_goal_command_rejects_result_during_newer_pane_load():
    result = _run_node(
        _ensure_all_messages_loaded_script(
            goal_turn_during_fetch=True, goal_pane_loading_during_response=True
        )
    )

    assert result["goalMessagesAfterResponse"] == [
        {"role": "assistant", "content": "tail latest"}
    ]
    assert result["goalBusyAfterResponse"] is False
    assert result["messages"] == [
        {"role": "user", "content": "older"},
        {"role": "assistant", "content": "tail latest"},
    ]
    assert result["toolCalls"] == [{"id": "old-tc"}]


def test_ensure_all_messages_loaded_still_hydrates_when_generation_is_stable():
    result = _run_node(_ensure_all_messages_loaded_script())

    assert result["messages"] == [
        {"role": "user", "content": "older"},
        {"role": "assistant", "content": "tail latest"},
    ]
    assert result["toolCalls"] == [{"id": "old-tc"}]
    assert result["truncated"] is False
    assert result["busy"] is False
    assert result["activeStreamId"] is None
    assert result["bumpCalls"] == 1
    assert result["syncCalls"] == 1
    assert result["oldestIdx"] == 0
    assert result["count"] == 2


def test_local_slash_echo_claims_generation_before_pending_full_history_commits():
    result = _run_node(_slash_command_generation_race_script())

    assert [{"role": m["role"], "content": m["content"]} for m in result["messages"]] == [
        {"role": "user", "content": "/help"},
        {"role": "assistant", "content": "available_commands\n  /help — help"},
    ]
    assert result["bumpCalls"] >= 2
    assert result["truncated"] is True


@pytest.mark.parametrize(
    ("command", "blank_page", "switch_on_completion", "expected_messages"),
    [
        ("/pet", True, False, ["/pet", "pet result"]),
        ("/agent", False, True, ["session b existing"]),
        ("/plugin", True, False, ["/plugin", "plugin result"]),
        ("/moa", True, False, ["/moa", "/moa <prompt>"]),
    ],
)
def test_async_slash_completions_bind_blank_and_current_pane_owners(
    command, blank_page, switch_on_completion, expected_messages
):
    result = _run_node(
        _async_slash_owner_script(
            command,
            blank_page=blank_page,
            switch_on_completion=switch_on_completion,
        )
    )

    assert [message["content"] for message in result["messages"]] == expected_messages
    if switch_on_completion:
        assert result["sessionId"] == "session-b"
    else:
        assert result["sessionId"] == "session-new"


def test_blank_page_slash_metadata_claims_owner_before_session_switch():
    result = _run_node(
        _async_slash_owner_script(
            "/plugin",
            blank_page=True,
            switch_on_completion=True,
            metadata_delay=True,
        )
    )

    assert result["sessionId"] == "session-b"
    assert [message["content"] for message in result["messages"]] == [
        "session b existing"
    ]


def test_blank_page_slash_rejects_owner_after_delayed_sidebar_render():
    result = _run_node(_delayed_render_owner_script())

    assert result["sessionId"] == "session-b"
    assert result["messages"] == []
    assert result["usedProductionOwner"] is True


def test_new_session_owner_guard_rejects_late_creation_install():
    result = _run_node(_creation_await_owner_script())

    assert result == {
        "acceptedWithoutTransition": "session-a",
        "rejectedDuringLoad": None,
        "rejectedAfterInstall": None,
        "sessionId": "session-b",
    }


@pytest.mark.parametrize("stage", ["upload", "directive", "chat_start"])
def test_normal_send_rejects_late_owner_result_after_pane_switch(stage):
    result = _run_node(_normal_send_chat_start_switch_script(stage))

    assert result["startCalls"] == (1 if stage == "chat_start" else 0)
    assert result["sessionId"] == "session-b"
    assert result["messages"] == [{"role": "assistant", "content": "session b existing"}]
    assert result["busy"] is False
    assert result["activeStreamId"] is None
    assert result["attachCalls"] == 0
    assert result["inflightKeys"] == (["session-a"] if stage == "chat_start" else [])


def test_normal_send_restores_failed_owner_draft_after_pane_switch():
    result = _run_node(_normal_send_chat_start_switch_script("chat_start_error"))

    assert result["startCalls"] == 1
    assert result["sessionId"] == "session-b"
    assert result["messages"] == [{"role": "assistant", "content": "session b existing"}]
    assert result["restoreArgs"] == {"draftText": "hello", "sid": "session-a"}
    assert result["clearOptimisticCalls"] == 1
    assert result["inflightKeys"] == []


def test_steer_does_not_mutate_old_pane_during_newer_load():
    result = _run_node(_steer_loading_switch_script())

    assert result["sessionId"] == "session-a"
    assert result["pendingFiles"] == ["draft.txt"]
    assert result["indicatorCalls"] == 0
    assert result["clearDraftCalls"] == 1


def test_clear_refuses_overlapping_confirmed_operation():
    result = _run_node(_clear_overlap_script())

    assert result["clearCalls"] == 2
    assert result["messages"] == []
    assert "conversation_cleared" in result["toasts"]
    assert result["pendingState"] == {"disabled": True, "busy": "true"}
    assert result["pendingResyncState"] == {"disabled": True, "busy": "true"}
    assert result["finalState"] == {"disabled": True, "busy": "false"}
    assert result["syncCalls"] == 3


def test_clear_conversation_leaves_switched_pane_untouched():
    result = _run_node(_clear_conversation_race_script("switched"))

    assert result["sessionId"] == "other-session"
    assert result["messages"] == [{"role": "assistant", "content": "other pane"}]
    assert result["toolCalls"] == [{"id": "other-tool"}]
    assert result["toasts"] == []
    assert result["reconcileCalls"] == 0


def test_send_owner_guard_covers_upload_directive_and_chat_start_awaits():
    upload_idx = SEND_SRC.index("uploaded=await uploadPendingFiles(")
    upload_guard_idx = SEND_SRC.index("if(!_slashOwnerIsCurrent(activeSid))return;", upload_idx)
    directive_idx = SEND_SRC.index("const _directivePayload = await _pending.promise;")
    directive_guard_idx = SEND_SRC.index("if(!_slashOwnerIsCurrent(activeSid))return;", directive_idx)
    chat_start_idx = SEND_SRC.index("const startData=await api('/api/chat/start'")
    catch_idx = SEND_SRC.index("}catch(e){", chat_start_idx)
    catch_guard_idx = SEND_SRC.index("if(!_slashOwnerIsCurrent(activeSid)) return;", catch_idx)
    success_guard_idx = SEND_SRC.index(
        "if(!_slashOwnerIsCurrent(activeSid)) return;", catch_guard_idx + 1
    )
    stream_state_idx = SEND_SRC.index("S.activeStreamId = streamId;", chat_start_idx)

    assert upload_idx < upload_guard_idx
    assert directive_idx < directive_guard_idx
    assert catch_idx < catch_guard_idx < success_guard_idx < stream_state_idx
    assert "queueSessionMessage(ownerSid" in SEND_SRC
    assert "updateQueueBadge(ownerSid)" in SEND_SRC
    assert "const activeSid=await _ensureSessionOwner();" in CMD_GOAL_SRC


def test_background_polling_rejects_stale_owner_or_generation_and_keeps_retry():
    current = _run_node(_background_polling_script())
    assert current["hidden"] == ["task-1"]
    assert current["messages"][0]["content"].endswith("done")
    assert current["rendered"] is True
    assert current["toasts"] == ["bg_complete"]
    assert current["timerCount"] == 0

    switched = _run_node(_background_polling_script(switch_session=True))
    assert switched["hidden"] == []
    assert switched["messages"] == []
    assert switched["rendered"] is False
    assert switched["toasts"] == []
    assert switched["timerCount"] == 1

    newer_writer = _run_node(_background_polling_script(change_generation=True))
    assert newer_writer["hidden"] == []
    assert newer_writer["messages"] == []
    assert newer_writer["rendered"] is False
    assert newer_writer["toasts"] == []
    assert newer_writer["timerCount"] == 1


@pytest.mark.parametrize("writer", ["gateway", "background"])
def test_active_transcript_writers_invalidate_pending_full_history_loads(writer):
    result = _run_node(_full_history_writer_race_script(writer))

    if writer == "gateway":
        assert result["messages"] == ["old", "gateway result"]
    else:
        assert len(result["messages"]) == 1
        assert result["messages"][0].endswith("background result")
    assert result["generation"] == 1


def test_writer_graph_covers_gateway_and_background_generation_authorities():
    assert "const replacementTicket = _captureTranscriptReplacement();" in SESSIONS_JS
    assert "_commitTranscriptReplacement(replacementTicket, () =>" in SESSIONS_JS
    assert "startGatewaySSE" in SESSIONS_JS
    assert "import_cli" in SESSIONS_JS
    assert "function startBackgroundPolling" in MESSAGES_JS
    assert "const requestGeneration=typeof _messagesGeneration==='number'" in MESSAGES_JS
    assert "if(typeof _bumpMessagesGeneration==='function') _bumpMessagesGeneration();\n            S.messages.push(msg);" in MESSAGES_JS
    assert "S.messages = _nextToAssign;" in SESSIONS_JS
    assert "S.messages.push(msg);" in MESSAGES_JS


def test_compression_stops_post_render_updates_when_generation_changes():
    result = _run_node(_compression_post_render_generation_script())

    assert result["applied"] is False
    assert result["messages"] == ["compressed"]
    assert result["queueCalls"] == 0
    assert result["compressionUiCalls"] == 0
    assert result["composerCalls"] == 0
    assert result["busyCalls"] == 0
    assert result["renderCalls"] == 1


@pytest.mark.parametrize("resumed", [False, True])
def test_compression_operation_cleans_ui_after_newer_same_session_writer(resumed):
    result = _run_node(_compression_cleanup_race_script(resumed=resumed))

    assert result["busy"] is False
    assert result["activeStreamId"] is None
    assert result["compressionUi"] is None
    assert result["compressionLock"] is None
    assert result["composerStatus"] == ""


def test_compression_operation_preserves_newer_stream_busy_state():
    result = _run_node(_compression_cleanup_race_script(newer_stream=True))

    assert result["busy"] is True
    assert result["activeStreamId"] == "stream-new"
    assert result["compressionUi"] is None
    assert result["compressionLock"] is None
    assert result["composerStatus"] == "streaming"


@pytest.mark.parametrize("race_stage", ["confirm", "post", "loading", "switched"])
def test_clear_conversation_keeps_server_and_local_settlement_visible(race_stage):
    result = _run_node(_clear_conversation_race_script(race_stage))

    if race_stage == "confirm":
        assert result["messages"] == []
        assert "conversation_cleared" in result["toasts"]
        assert result["reconcileCalls"] == 0
    elif race_stage == "post":
        assert result["messages"] == [
            {"role": "assistant", "content": "old"},
            {"role": "user", "content": "newer writer"},
        ]
        assert any("changed" in toast.lower() for toast in result["toasts"])
        assert result["reconcileCalls"] == 1
    elif race_stage == "loading":
        assert result["sessionId"] == "same-session"
        assert result["messages"] == [{"role": "assistant", "content": "old"}]
        assert result["toolCalls"] == [{"id": "same-tool"}]
        assert result["toasts"] == []
        assert result["reconcileCalls"] == 0
    else:
        assert result["sessionId"] == "other-session"
        assert result["messages"] == [{"role": "assistant", "content": "other pane"}]
        assert result["toolCalls"] == [{"id": "other-tool"}]
        assert result["reconcileCalls"] == 0


def test_same_session_refresh_and_compression_reject_older_replacements():
    if "function _captureTranscriptReplacement()" not in SESSIONS_JS:
        pytest.skip("respec-only replacement seam is absent on the base checkout")
    result = _run_node(_replacement_race_script())

    assert result["refreshMessages"] == ["before", "newer row"]
    assert result["compressionMessages"] == ["before compression", "newer compression row"]


def test_refresh_and_compression_reject_results_during_newer_pane_load():
    result = _run_node(_replacement_race_script(loading_transition=True))

    assert result["refreshMessages"] == ["before", "newer row"]
    assert result["compressionMessages"] == ["before compression", "newer compression row"]


def test_messages_generation_wiring_covers_full_load_live_turn_claims_and_same_session_replacements():
    if "function _captureTranscriptReplacement()" not in SESSIONS_JS:
        pytest.skip("respec-only replacement seam is absent on the base checkout")
    assert "function _captureTranscriptReplacement()" in SESSIONS_JS
    assert "function _commitTranscriptReplacement(ticket, commit)" in SESSIONS_JS
    assert "async function _readFullSessionSnapshot(sid)" in SESSIONS_JS
    assert "_claimTranscriptWrite" not in "\n".join([SESSIONS_JS, COMMANDS_JS, MESSAGES_JS, UI_JS, WORKSPACE_JS])
    assert "const replacementTicket = _captureTranscriptReplacement();" in SESSIONS_JS
    assert "if (!_transcriptReplacementIsCurrent(replacementTicket)) return;" in SESSIONS_JS
    assert "_commitTranscriptReplacement(replacementTicket, () =>" in SESSIONS_JS
    assert "if(activeStreamId) _bumpMessagesGeneration();\n    S.activeStreamId=activeStreamId;" in SESSIONS_JS
    assert "S.busy=true;\n      _bumpMessagesGeneration();\n      S.activeStreamId=activeStreamId;" in SESSIONS_JS
    assert "if(typeof _bumpMessagesGeneration==='function') _bumpMessagesGeneration();\n    S.messages.push(userMsg);renderMessages();setBusy(true);" in MESSAGES_JS
    assert "if(streamId&&typeof _bumpMessagesGeneration==='function') _bumpMessagesGeneration();\n  S.activeStreamId = streamId;" in MESSAGES_JS
    assert "S.busy = true;\n    if(typeof _bumpMessagesGeneration==='function') _bumpMessagesGeneration();\n    S.activeStreamId = streamId;" in MESSAGES_JS
    assert "S.busy = true;\n        if(typeof _bumpMessagesGeneration==='function') _bumpMessagesGeneration();\n        S.activeStreamId = streamId;" in MESSAGES_JS
    assert "_commitTranscriptReplacement(replacementTicket, () =>" in COMMANDS_JS
    assert "const replacementTicket=typeof _captureTranscriptReplacement==='function'" in COMMANDS_JS
    assert "const refreshTicket=typeof _captureTranscriptReplacement==='function'" in UI_JS
    assert "function transcript()" in MESSAGES_JS
    assert "const sessionInput = arguments.length > 0 ? arguments[0] : null;" in MESSAGES_JS
    assert "function collectSessionArtifacts()" in WORKSPACE_JS
    assert "const messagesInput = arguments.length > 0 ? arguments[0] : null;" in WORKSPACE_JS
    assert "const requestGeneration=typeof _messagesGeneration==='number'" in MESSAGES_JS
    assert "ticket.committedGeneration = _messagesGeneration;" in SESSIONS_JS
    assert "ticket.committedGeneration!==undefined" in COMMANDS_JS
    assert "const _ensureSlashOwner=async()=>" in MESSAGES_JS
    assert "if(!_slashOwnerIsCurrent(_metadataSid))return;" in MESSAGES_JS
    assert "const _metadataSid=await _ensureSlashOwner();" in MESSAGES_JS
    assert "_manualCompressionOperation" in COMMANDS_JS
    assert "_clearConversationOperation" in PANELS_JS
    assert "_readFullSessionSnapshot(sid)" in BOOT_JS
    assert "_renderNow(snapshot.messages, snapshot.toolCalls);" in WORKSPACE_JS
    assert "_commitTranscriptReplacement(clearTicket, () =>" in PANELS_JS


def test_terminal_paths_route_artifacts_refresh_through_shared_idle_helper():
    if "scheduleRenderSessionArtifacts" not in MESSAGES_JS:
        pytest.skip("respec-only Artifacts settlement hook is absent on the base checkout")
    assert "if(typeof _workspaceArtifactsTabIsActive==='function'&&_workspaceArtifactsTabIsActive()){" in MESSAGES_JS
    assert "if(typeof scheduleRenderSessionArtifacts==='function') scheduleRenderSessionArtifacts();" in MESSAGES_JS
    assert "renderSessionList();\n        _setActivePaneIdleIfOwner();" in MESSAGES_JS
    assert "_setActivePaneIdleIfOwner();\n      renderSessionList(); // clear streaming indicator immediately on apperror" in MESSAGES_JS
    assert "finally{\n          _setActivePaneIdleIfOwner();\n        }" in MESSAGES_JS
    assert "renderSessionList();\n      _setActivePaneIdleIfOwner();\n      return returnStatus?'restored':true;" in MESSAGES_JS
    assert "_setActivePaneIdleIfOwner();\n  }\n\n  (async()=>{" in MESSAGES_JS


def test_locale_blocks_cover_loading_and_download_feedback_keys():
    if "workspace_artifact_loading_full_history" not in I18N_JS:
        pytest.skip("respec-only locale keys are absent on the base checkout")
    locale_count = I18N_JS.count("download_transcript:")
    assert locale_count == 15

    keys = [
        "workspace_artifact_loading_full_history:",
        "download_transcript_preparing_full:",
        "download_transcript_busy_full:",
        "download_transcript_failed_full:",
        "download_transcript_changed_full:",
    ]
    for key in keys:
        assert I18N_JS.count(key) == 1, f"{key} must be owned by en only"

    assert "workspace_artifact_loading_full_history: 'Loading full history…'" in I18N_JS
    assert "download_transcript_preparing_full: 'Preparing full transcript…'" in I18N_JS
    assert "download_transcript_busy_full: 'Wait for the current response to finish before downloading the full transcript.'" in I18N_JS


def test_locale_runtime_falls_back_to_english_for_missing_non_english_keys():
    if "workspace_artifact_loading_full_history" not in I18N_JS:
        pytest.skip("respec-only locale keys are absent on the base checkout")
    assert "const val = _locale[key] ?? LOCALES.en[key];" in I18N_JS
    script = f"""
const source = {json.dumps(I18N_JS)};
function extractFunction(source, name) {{
  const start = source.indexOf(`function ${{name}}(`);
  if (start < 0) throw new Error(`missing function ${{name}}`);
  const brace = source.indexOf('{{', start);
  let depth = 0;
  for (let i = brace; i < source.length; i++) {{
    if (source[i] === '{{') depth++;
    else if (source[i] === '}}' && --depth === 0) return source.slice(start, i + 1);
  }}
  throw new Error(`unterminated ${{name}}`);
}}
const tSrc = extractFunction(source, 't');
const keys = [
  'download_transcript_preparing_full',
  'download_transcript_busy_full',
  'workspace_artifact_loading_full_history',
];
const english = {{}};
for (const key of keys) {{
  const match = source.match(new RegExp(key + "\\\\s*:\\\\s*'([^']*)'"));
  if (!match) throw new Error('missing English source value for ' + key);
  english[key] = match[1];
}}
const LOCALES = {{ en: english, es: {{}} }};
let _locale = LOCALES.es;
{I18N_T_FN}
console.log(JSON.stringify({{
  preparing: t('download_transcript_preparing_full'),
  busy: t('download_transcript_busy_full'),
  loading: t('workspace_artifact_loading_full_history'),
  matchesEnglish: t('download_transcript_preparing_full') === english.download_transcript_preparing_full
    && t('download_transcript_busy_full') === english.download_transcript_busy_full
    && t('workspace_artifact_loading_full_history') === english.workspace_artifact_loading_full_history,
  missingInSpanish: Object.prototype.hasOwnProperty.call(LOCALES.es, 'download_transcript_preparing_full'),
}}));
"""
    result = _run_node(script)
    assert result["matchesEnglish"] is True
    assert result["missingInSpanish"] is False


def _artifact_load_visit_guard_script() -> str:
    workspace_path = str(REPO / "static" / "workspace.js")
    return f"""
const fs = require('fs');
const workspaceSrc = fs.readFileSync({json.dumps(workspace_path)}, 'utf8');
const currentMessages = [{{ role: 'assistant', tool_calls: [{{ function: {{ name: 'write_file', arguments: JSON.stringify({{ path: 'current/live.txt' }}) }} }}] }}];
let ensureCalls = 0;
let pending = [];
let _messagesGeneration = 1;
let _loadSessionGeneration = 1;
let _messagesTruncated = true;
let _workspacePanelActiveTab = 'artifacts';
const rightPanel = {{ dataset: {{ activeTab: 'artifacts' }} }};
const root = {{ innerHTML: '', isConnected: true, hidden: false }};
const count = {{ textContent: '' }};
const artifactsTab = {{ hidden: false }};
const document = {{ querySelector: (selector) => selector === '.rightpanel' ? rightPanel : null, getElementById: (id) => id === 'workspaceArtifactsTab' ? artifactsTab : id === 'workspaceArtifacts' ? root : null }};
function $(id) {{ return id === 'workspaceArtifacts' ? root : id === 'workspaceArtifactsCount' ? count : null; }}
function esc(value) {{ return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }}
function t(key) {{ return key === 'workspace_artifact_source_session' ? 'session' : key; }}
const S = {{ session: {{ session_id: 'foreign-1', workspace: '/ws' }}, messages: currentMessages, toolCalls: [], busy: false, activeStreamId: null }};
async function _readFullSessionSnapshot() {{ ensureCalls += 1; return await new Promise((resolve) => pending.push(resolve)); }}
async function _ensureAllMessagesLoaded() {{}}
{WORKSPACE_HELPERS}
{WORKSPACE_ARTIFACT_CONSTS}
{WORKSPACE_ARTIFACT_CACHE}
{WORKSPACE_CONSUMER_BLOCK}
(async () => {{
  const render = renderSessionArtifacts();
  await new Promise((resolve) => setTimeout(resolve, 0));
  _loadSessionGeneration = 2;
  const resolveSnapshot = pending.shift();
  resolveSnapshot({{ session: S.session, messages: [{{ role: 'assistant', tool_calls: [{{ function: {{ name: 'write_file', arguments: JSON.stringify({{ path: 'stale/old.txt' }}) }} }}] }}], toolCalls: [] }});
  await render;
  console.log(JSON.stringify({{ ensureCalls, html: root.innerHTML, count: count.textContent }}));
}})().catch((err) => {{ console.error(err.stack || String(err)); process.exit(1); }});
"""
