"""Regression coverage for foreign-session full-history consumers on PR #6494."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
TRANSCRIPT_FN = MESSAGES_JS[
    MESSAGES_JS.index("function transcript(") : MESSAGES_JS.index("let _composerAutoResizeRaf=0;")
]
ENSURE_ALL_FN = SESSIONS_JS[
    SESSIONS_JS.index("async function _ensureAllMessagesLoaded() {") : SESSIONS_JS.index("const SESSION_ARCHIVED_PAGE_SIZE")
]
WORKSPACE_HELPERS = WORKSPACE_JS[
    WORKSPACE_JS.index("function _workspaceTodosTabIsActive(){") : WORKSPACE_JS.index("function _resetWorkspaceTodosRenderCache(){")
]
WORKSPACE_CONSUMER_BLOCK = WORKSPACE_JS[
    WORKSPACE_JS.index("function _normalizeArtifactPath(") : WORKSPACE_JS.index("async function _workspacePathExists(")
]
WORKSPACE_ARTIFACT_CONSTS = "\n".join(
    [
        r"const ARTIFACT_IGNORE_RE = /(^|\/)(?:\.git|\.hg|\.svn|node_modules|\.venv|venv|__pycache__|dist|build|\.next|\.cache)(?:\/|$)/;",
        "const ARTIFACT_MUTATION_TOOLS = new Set(['write_file','patch','edit_file','create_file','mcp_filesystem_write_file','mcp_filesystem_edit_file']);",
    ]
)
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(script: str) -> dict:
    proc = subprocess.run(
        [NODE, "-e", script],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def _download_script(
    *,
    fail_load: bool = False,
    switch_session: bool = False,
    active_stream: bool = False,
) -> str:
    boot_path = str(REPO / "static" / "boot.js")
    messages_path = str(REPO / "static" / "messages.js")
    if fail_load:
        ensure_body = "ensureCalls += 1; throw new Error('load failed');"
    elif switch_session:
        ensure_body = (
            "ensureCalls += 1; "
            "S.session = { session_id: 'foreign-2', workspace: '/ws', model: 'model' }; "
            "S.messages = fullMessages; "
            "_messagesTruncated = false;"
        )
    else:
        ensure_body = "ensureCalls += 1; S.messages = fullMessages; _messagesTruncated = false;"
    return f"""
const fs = require('fs');
const bootSrc = fs.readFileSync({json.dumps(boot_path)}, 'utf8');
const messagesSrc = fs.readFileSync({json.dumps(messages_path)}, 'utf8');
function extractFunction(source, name) {{
  const start = source.indexOf(`function ${{name}}(`);
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
function extractAssignment(source, marker, nextMarker) {{
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`missing assignment ${{marker}}`);
  const end = source.indexOf(nextMarker, start);
  if (end < 0) throw new Error(`missing next marker ${{nextMarker}}`);
  return source.slice(start, end).trim();
}}
const transcriptSrc = extractFunction(messagesSrc, 'transcript');
const downloadAssign = extractAssignment(bootSrc, "$('btnDownload').onclick=", "$('btnExportJSON').onclick=");
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
const btns = {{ btnDownload: {{}}, btnExportJSON: {{}} }};
function $(id) {{ return btns[id]; }}
function t(key) {{ return translations[key] || key; }}
function showToast(message, duration, kind) {{
  toasts.push({{ message, duration, kind: kind || null }});
}}
let _messagesTruncated = true;
const S = {{
  session: {{ session_id: 'foreign-1', workspace: '/ws', model: 'model' }},
  messages: [fullMessages[1]],
  busy: {str(active_stream).lower()},
  activeStreamId: {json.dumps('live-1' if active_stream else None)},
}};
async function _ensureAllMessagesLoaded() {{ {ensure_body} }}
{TRANSCRIPT_FN}
eval(downloadAssign);
(async () => {{
  const ret = btns.btnDownload.onclick();
  if (ret && typeof ret.then === 'function') await ret;
  console.log(JSON.stringify({{
    ensureCalls,
    clicked,
    downloadName,
    downloadedText,
    revokedHref,
    sessionId: S.session && S.session.session_id,
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
) -> str:
    workspace_path = str(REPO / "static" / "workspace.js")
    initial_html = '<button data-artifact-path="previous/file.txt"></button>' if seed_existing_dom else ""
    initial_count = "1" if seed_existing_dom else ""
    if fail_load:
        ensure_body = "ensureCalls += 1; throw new Error('load failed');"
    else:
        ensure_body = "ensureCalls += 1; S.messages = fullMessages; _messagesTruncated = false;"
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
async function _ensureAllMessagesLoaded() {{ {ensure_body} }}
{WORKSPACE_HELPERS}
{WORKSPACE_ARTIFACT_CONSTS}
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
    truncated: _messagesTruncated,
  }}));
}})().catch((err) => {{
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
}});
"""


def _ensure_all_messages_loaded_script(*, goal_turn_during_fetch: bool = False) -> str:
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
const ensureAllSrc = extractFunction(sessionsSrc, '_ensureAllMessagesLoaded');
const cmdGoalSrc = extractFunction(commandsSrc, 'cmdGoal');
let _messagesTruncated = true;
let _loadingOlder = false;
let _loadingSessionId = null;
let _oldestIdx = 99;
let _messagesGeneration = 0;
let bumpCalls = 0;
let syncCalls = 0;
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
function _bumpMessagesGeneration() {{
  bumpCalls += 1;
  _messagesGeneration = (_messagesGeneration + 1) % 2147483647;
}}
function _syncToolCallsForLoadedMessages(_messages, toolCalls) {{
  syncCalls += 1;
  S.toolCalls = Array.isArray(toolCalls) ? toolCalls.map((tc) => ({{ ...tc }})) : [];
}}
let resolveFullLoad = null;
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
}}
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
    return {{
      message: 'Goal status',
      stream_id: 'goal-1',
      pending_started_at: 123,
    }};
  }}
  throw new Error('Unexpected API call: ' + String(url));
}}
{ENSURE_ALL_FN}
eval(cmdGoalSrc);
(async () => {{
  const ensurePromise = _ensureAllMessagesLoaded();
  if ({str(goal_turn_during_fetch).lower()}) {{
    await cmdGoal('ship it');
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
    oldestIdx: _oldestIdx,
    count: S.session && S.session.message_count,
  }}));
}})().catch((err) => {{
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
}});
"""


def test_download_handler_uses_localized_feedback_and_full_history_gate():
    assert "showToast((typeof t==='function'&&t('download_transcript_busy_full'))" in BOOT_JS
    assert "showToast((typeof t==='function'&&t('download_transcript_failed_full'))" in BOOT_JS
    assert "showToast((typeof t==='function'&&t('download_transcript_preparing_full'))" in BOOT_JS
    assert "showToast((typeof t==='function'&&t('download_transcript_changed_full'))" in BOOT_JS
    assert "await _ensureAllMessagesLoaded();" in BOOT_JS

    result = _run_node(_download_script())

    assert result["ensureCalls"] == 1
    assert result["clicked"] is True
    assert result["downloadName"] == "hermes-foreign-1.md"
    assert "OLD_TRANSCRIPT_UNIQUE" in result["downloadedText"]
    assert result["truncated"] is False
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


def test_artifacts_renderer_uses_placeholder_preserved_count_and_full_load():
    result = _run_node(_artifact_script(seed_existing_dom=True))

    assert result["ensureCalls"] == 1
    assert "Loading full history…" in result["htmlBeforeAwait"]
    assert result["countBeforeAwait"] == "1"
    assert result["count"] == "2"
    assert 'data-artifact-path="old/deep.txt"' in result["html"]
    assert 'data-artifact-path="new/live.txt"' in result["html"]
    assert result["truncated"] is False


def test_artifacts_renderer_falls_back_to_partial_list_after_failure():
    result = _run_node(_artifact_script(fail_load=True, seed_existing_dom=True))

    assert result["ensureCalls"] == 1
    assert "Loading full history…" in result["htmlBeforeAwait"]
    assert result["countBeforeAwait"] == "1"
    assert result["count"] == "1"
    assert 'data-artifact-path="new/live.txt"' in result["html"]
    assert 'data-artifact-path="old/deep.txt"' not in result["html"]
    assert result["truncated"] is True


def test_artifacts_renderer_keeps_placeholder_during_live_turn_on_truncated_session():
    result = _run_node(_artifact_script(active_stream=True, seed_existing_dom=True))

    assert result["ensureCalls"] == 0
    assert "Loading full history…" in result["htmlBeforeAwait"]
    assert "Loading full history…" in result["html"]
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
    assert result["oldestIdx"] == 99
    assert result["count"] == 2


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


def test_messages_generation_wiring_covers_full_load_live_turn_claims_and_same_session_replacements():
    assert "const startGeneration = _messagesGeneration;" in SESSIONS_JS
    assert "if (_messagesGeneration !== startGeneration) return;" in SESSIONS_JS
    assert "_bumpMessagesGeneration();\n  S.messages = msgs;" in SESSIONS_JS
    assert "if(activeStreamId) _bumpMessagesGeneration();\n    S.activeStreamId=activeStreamId;" in SESSIONS_JS
    assert "S.busy=true;\n      _bumpMessagesGeneration();\n      S.activeStreamId=activeStreamId;" in SESSIONS_JS
    assert "if(typeof _bumpMessagesGeneration==='function') _bumpMessagesGeneration();\n    S.messages.push(userMsg);renderMessages();setBusy(true);" in MESSAGES_JS
    assert "if(streamId&&typeof _bumpMessagesGeneration==='function') _bumpMessagesGeneration();\n  S.activeStreamId = streamId;" in MESSAGES_JS
    assert "S.busy = true;\n    if(typeof _bumpMessagesGeneration==='function') _bumpMessagesGeneration();\n    S.activeStreamId = streamId;" in MESSAGES_JS
    assert "S.busy = true;\n        if(typeof _bumpMessagesGeneration==='function') _bumpMessagesGeneration();\n        S.activeStreamId = streamId;" in MESSAGES_JS
    assert "if(typeof _bumpMessagesGeneration==='function') _bumpMessagesGeneration();\n      S.messages.push({role:'assistant',content:msg,_ts:Date.now()/1000,_goalStatus:true,_transient:true});" in COMMANDS_JS
    assert "if(typeof _bumpMessagesGeneration==='function') _bumpMessagesGeneration();\n    appendThinking();setBusy(true);" in COMMANDS_JS
    assert "if(typeof _bumpMessagesGeneration==='function') _bumpMessagesGeneration();\n      S.messages=data.session.messages||[];" in COMMANDS_JS
    assert "if(typeof _bumpMessagesGeneration==='function') _bumpMessagesGeneration();\n      S.messages=live.session.messages||[];" in COMMANDS_JS
    assert "if(data&&data.session){if(typeof _bumpMessagesGeneration==='function') _bumpMessagesGeneration();S.messages=data.session.messages||[];" in COMMANDS_JS
    assert "if(typeof _bumpMessagesGeneration==='function') _bumpMessagesGeneration();\n    S.messages = data.session.messages || [];" in UI_JS
    assert "if(typeof _bumpMessagesGeneration==='function') _bumpMessagesGeneration();\n    S.messages = [];" in PANELS_JS


def test_terminal_paths_route_artifacts_refresh_through_shared_idle_helper():
    assert "if(typeof _workspaceArtifactsTabIsActive==='function'&&_workspaceArtifactsTabIsActive()){" in MESSAGES_JS
    assert "if(typeof scheduleRenderSessionArtifacts==='function') scheduleRenderSessionArtifacts();" in MESSAGES_JS
    assert "renderSessionList();\n        _setActivePaneIdleIfOwner();" in MESSAGES_JS
    assert "_setActivePaneIdleIfOwner();\n      renderSessionList(); // clear streaming indicator immediately on apperror" in MESSAGES_JS
    assert "finally{\n          _setActivePaneIdleIfOwner();\n        }" in MESSAGES_JS
    assert "renderSessionList();\n      _setActivePaneIdleIfOwner();\n      return returnStatus?'restored':true;" in MESSAGES_JS
    assert "_setActivePaneIdleIfOwner();\n  }\n\n  (async()=>{" in MESSAGES_JS


def test_locale_blocks_cover_loading_and_download_feedback_keys():
    locale_count = I18N_JS.count("download_transcript:")
    assert locale_count == 15

    for key in [
        "workspace_artifact_loading_full_history:",
        "download_transcript_preparing_full:",
        "download_transcript_busy_full:",
        "download_transcript_failed_full:",
        "download_transcript_changed_full:",
    ]:
        assert I18N_JS.count(key) == locale_count, f"{key} must exist in every locale block"

    assert "workspace_artifact_loading_full_history: 'Loading full history…'" in I18N_JS
    assert "download_transcript_preparing_full: 'Preparing full transcript…'" in I18N_JS
    assert "download_transcript_busy_full: 'Wait for the current response to finish before downloading the full transcript.'" in I18N_JS
