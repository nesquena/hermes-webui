"""Regression coverage for foreign-session complete-history consumers on PR #6494.

The backend now bounds initial foreign-session loads. These tests pin the two
frontend consumers that still need complete history before they can claim
complete-session output: Markdown download and the Artifacts tab.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
TRANSCRIPT_FN = MESSAGES_JS[MESSAGES_JS.index("function transcript("):MESSAGES_JS.index("let _composerAutoResizeRaf=0;")]
ENSURE_ALL_FN = SESSIONS_JS[SESSIONS_JS.index("async function _ensureAllMessagesLoaded() {"):SESSIONS_JS.index("const SESSION_ARCHIVED_PAGE_SIZE")]
WORKSPACE_CONSUMER_BLOCK = WORKSPACE_JS[WORKSPACE_JS.index("function _normalizeArtifactPath("):WORKSPACE_JS.index("async function _workspacePathExists(")]
WORKSPACE_ARTIFACT_CONSTS = "\n".join([
    r"const ARTIFACT_IGNORE_RE = /(^|\/)(?:\.git|\.hg|\.svn|node_modules|\.venv|venv|__pycache__|dist|build|\.next|\.cache)(?:\/|$)/;",
    "const ARTIFACT_MUTATION_TOOLS = new Set(['write_file','patch','edit_file','create_file','mcp_filesystem_write_file','mcp_filesystem_edit_file']);",
])
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
    switch_session: bool = False,
    fail_load: bool = False,
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
  }}));
}})().catch((err) => {{
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
}});
"""


def _artifact_script(
    *,
    switch_context: bool = False,
    fail_load: bool = False,
    active_stream: bool = False,
    seed_existing_dom: bool = False,
) -> str:
    workspace_path = str(REPO / "static" / "workspace.js")
    initial_html = '<button data-artifact-path="previous/file.txt"></button>' if seed_existing_dom else ""
    initial_count = "1" if seed_existing_dom else ""
    if fail_load:
        ensure_body = "ensureCalls += 1; throw new Error('load failed');"
    elif switch_context:
        ensure_body = (
            "ensureCalls += 1; "
            "S.session = { session_id: 'foreign-2', workspace: '/ws' }; "
            "S.messages = fullMessages; "
            "_workspacePanelActiveTab = 'files'; "
            "_messagesTruncated = false;"
        )
    else:
        ensure_body = "ensureCalls += 1; S.messages = fullMessages; _messagesTruncated = false;"
    return f"""
const fs = require('fs');
const workspaceSrc = fs.readFileSync({json.dumps(workspace_path)}, 'utf8');
function extractConst(name) {{
  const match = workspaceSrc.match(new RegExp(`const ${{name}} = .*?;`));
  if (!match) throw new Error(`missing const ${{name}}`);
  return match[0];
}}
function extractFunction(signature) {{
  const start = workspaceSrc.indexOf(signature);
  if (start < 0) throw new Error(`missing function ${{signature}}`);
  const brace = workspaceSrc.indexOf('{{', start);
  let depth = 0;
  for (let i = brace; i < workspaceSrc.length; i++) {{
    const ch = workspaceSrc[i];
    if (ch === '{{') depth++;
    else if (ch === '}}') {{
      depth--;
      if (depth === 0) return workspaceSrc.slice(start, i + 1);
    }}
  }}
  throw new Error(`unterminated function ${{signature}}`);
}}
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
let _workspacePanelActiveTab = 'artifacts';
const root = {{ innerHTML: {json.dumps(initial_html)}, isConnected: true }};
const count = {{ textContent: {json.dumps(initial_count)} }};
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
  return key === 'workspace_artifact_source_session' ? 'session' : key;
}}
const S = {{
  session: {{ session_id: 'foreign-1', workspace: '/ws' }},
  messages: [fullMessages[1]],
  toolCalls: [],
  busy: {str(active_stream).lower()},
  activeStreamId: {json.dumps('live-1' if active_stream else None)},
}};
async function _ensureAllMessagesLoaded() {{ {ensure_body} }}
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
    sessionId: S.session && S.session.session_id,
    tab: _workspacePanelActiveTab,
    truncated: _messagesTruncated,
  }}));
}})().catch((err) => {{
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
}});
"""


def test_download_handler_requires_full_history_before_serializing_markdown():
    assert "$('btnDownload').onclick=async()=>{" in BOOT_JS
    assert "if(S.busy||S.activeStreamId) return;" in BOOT_JS
    assert "await _ensureAllMessagesLoaded();" in BOOT_JS
    assert "if(!S.session||S.session.session_id!==sid||_messagesTruncated||S.busy||S.activeStreamId)return;" in BOOT_JS

    result = _run_node(_download_script())

    assert result["ensureCalls"] == 1
    assert result["clicked"] is True
    assert result["downloadName"] == "hermes-foreign-1.md"
    assert "OLD_TRANSCRIPT_UNIQUE" in result["downloadedText"]
    assert result["truncated"] is False


def test_download_handler_aborts_stale_or_failed_full_load():
    stale = _run_node(_download_script(switch_session=True))
    assert stale["ensureCalls"] == 1
    assert stale["clicked"] is False
    assert stale["downloadedText"] is None
    assert stale["sessionId"] == "foreign-2"

    failed = _run_node(_download_script(fail_load=True))
    assert failed["ensureCalls"] == 1
    assert failed["clicked"] is False
    assert failed["downloadedText"] is None

    active = _run_node(_download_script(active_stream=True))
    assert active["ensureCalls"] == 0
    assert active["clicked"] is False
    assert active["downloadedText"] is None
    assert active["truncated"] is True


def test_artifacts_renderer_requires_full_history_before_collecting():
    assert "typeof _ensureAllMessagesLoaded === 'function'" in WORKSPACE_JS
    assert "if(S.busy || S.activeStreamId) return;" in WORKSPACE_JS
    assert "if(!S.session || S.session.session_id !== sid || _messagesTruncated || S.busy || S.activeStreamId) return;" in WORKSPACE_JS
    assert "if(_workspacePanelActiveTab !== 'artifacts') return;" in WORKSPACE_JS

    result = _run_node(_artifact_script(seed_existing_dom=True))

    assert result["ensureCalls"] == 1
    assert result["htmlBeforeAwait"] == ""
    assert result["countBeforeAwait"] == ""
    assert result["count"] == "2"
    assert 'data-artifact-path="old/deep.txt"' in result["html"]
    assert 'data-artifact-path="new/live.txt"' in result["html"]
    assert result["truncated"] is False


def test_artifacts_renderer_aborts_stale_or_failed_full_load():
    stale = _run_node(_artifact_script(switch_context=True))
    assert stale["ensureCalls"] == 1
    assert stale["html"] == ""
    assert stale["count"] == ""
    assert stale["sessionId"] == "foreign-2"
    assert stale["tab"] == "files"

    failed = _run_node(_artifact_script(fail_load=True))
    assert failed["ensureCalls"] == 1
    assert failed["html"] == ""
    assert failed["count"] == ""

    active = _run_node(_artifact_script(active_stream=True, seed_existing_dom=True))
    assert active["ensureCalls"] == 0
    assert active["htmlBeforeAwait"] == ""
    assert active["countBeforeAwait"] == ""
    assert active["html"] == ""
    assert active["count"] == ""
    assert active["truncated"] is True


def _ensure_all_messages_loaded_script(*, live_during_load: bool = False) -> str:
    sessions_path = str(REPO / "static" / "sessions.js")
    return f"""
const fs = require('fs');
const sessionsSrc = fs.readFileSync({json.dumps(sessions_path)}, 'utf8');
function extractFunction(source, name) {{
  const start = source.indexOf(`async function ${{name}}(`);
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
let _messagesTruncated = true;
let _loadingOlder = false;
let _loadingSessionId = null;
let _oldestIdx = 99;
let bumpCalls = 0;
let syncCalls = 0;
const originalMessages = [{{ role: 'assistant', content: 'tail latest', _transient: true }}];
const fullMessages = [
  {{ role: 'user', content: 'older' }},
  {{ role: 'assistant', content: 'tail latest' }},
];
const S = {{
  session: {{ session_id: 'foreign-1', message_count: 1 }},
  messages: originalMessages.slice(),
  busy: false,
  activeStreamId: null,
}};
const window = {{
  _carryForwardEphemeralTurnFields: (_current, incoming) => incoming,
}};
function _bumpMessagesGeneration() {{
  bumpCalls += 1;
}}
function _syncToolCallsForLoadedMessages() {{
  syncCalls += 1;
}}
async function api() {{
  if ({str(live_during_load).lower()}) {{
    S.busy = true;
    S.activeStreamId = 'live-1';
  }}
  return {{
    session: {{
      messages: fullMessages,
      tool_calls: [{{ id: 'tc-1' }}],
      message_count: fullMessages.length,
    }},
  }};
}}
{ENSURE_ALL_FN}
(async () => {{
  await _ensureAllMessagesLoaded();
  console.log(JSON.stringify({{
    messages: S.messages,
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


def test_ensure_all_messages_loaded_bails_if_live_stream_starts_mid_fetch():
    result = _run_node(_ensure_all_messages_loaded_script(live_during_load=True))

    assert result["messages"] == [{"role": "assistant", "content": "tail latest", "_transient": True}]
    assert result["truncated"] is True
    assert result["busy"] is True
    assert result["activeStreamId"] == "live-1"
    assert result["bumpCalls"] == 0
    assert result["syncCalls"] == 0
    assert result["oldestIdx"] == 99
    assert result["count"] == 1
