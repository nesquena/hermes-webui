"""Regression guard for CLI import refresh overwriting active transcript."""

import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"could not extract {signature}")


def test_sse_import_cli_guard_skips_shorter_transcript_overwrite():
    """The SSE import refresh path should refuse stale/shorter transcripts."""
    start = SESSIONS_JS.index("function startGatewaySSE")
    stop = SESSIONS_JS.index("function stopGatewaySSE", start)
    sse_block = SESSIONS_JS[start:stop]

    assert "const prev = S.messages.length;" in sse_block
    assert "const next = res.session.messages.filter(m => m && m.role);" in sse_block
    assert "if (next.length < prev) return;" in sse_block
    assert "if (prev > 0 && !_isCliImportRefreshPrefixMatch(S.messages, next)) return;" in sse_block
    # #3306 added an ephemeral-field carry-forward before the assignment, so the
    # replace RHS is now `_nextToAssign` (= carry-forward of `next`). The guard
    # invariants above are what this test protects; the wholesale replace remains.
    assert "S.messages = _nextToAssign;" in sse_block
    assert "S.session.message_count = next.length;" in sse_block
    assert "renderMessages({preserveScroll:true});" in sse_block


def test_sse_import_cli_refresh_prefix_helper_ignores_timestamps():
    """Refresh-prefix helper used by SSE should compare messages without timestamp keys."""
    assert "function _normalizeMessageForCliImportComparison(message)" in SESSIONS_JS
    assert "delete clone.timestamp;" in SESSIONS_JS
    assert "delete clone._ts;" in SESSIONS_JS
    assert "function _isCliImportRefreshPrefixMatch(localMessages, freshMessages)" in SESSIONS_JS
    assert "_normalizeMessageForCliImportComparison" in SESSIONS_JS
    assert "localMessages.length > freshMessages.length" in SESSIONS_JS


def test_sse_growing_import_refreshes_a_paged_tail_with_bounded_loader():
    """A long foreign session must show its new turn without loading all history."""
    helpers = "\n".join([
        _function_body(SESSIONS_JS, "async function _ensureMessagesLoaded"),
        _function_body(SESSIONS_JS, "function startGatewaySSE"),
    ])
    script = f"""
globalThis.window=globalThis;
globalThis.location={{href:'http://example.test/'}};
globalThis.document={{hidden:false,addEventListener(){{}}}};
window._showCliSessions=true;
let _gatewaySSE=null;
let _gatewayProbeInFlight=false;
let _gatewaySSEWarningShown=false;
let _gatewayFallbackPollMs=30000;
let _loadSessionGeneration=7;
let _loadingSessionId='foreign';
let _pendingCarryForwardSnapshot=null;
let _messagesTruncated=true;
let _oldestIdx=2;
let _msgLimitMax=500;
const _MSG_LIMIT_MAX=500;
const S={{
  activeProfile:'default',busy:false,activeStreamId:null,toolCalls:[],lastUsage:{{}},
  session:{{session_id:'foreign',session_source:'cli',message_count:4}},
  messages:[
    {{role:'user',content:'tail question',timestamp:3}},
    {{role:'assistant',content:'tail answer',timestamp:4}},
  ],
}};
const calls=[];
let renders=0;
class FakeEventSource{{
  constructor(){{this.listeners={{}};this.readyState=1;window.gatewaySource=this;}}
  addEventListener(name,callback){{this.listeners[name]=callback;}}
  emit(name,data){{this.listeners[name]({{data:JSON.stringify(data)}});}}
  close(){{this.readyState=2;}}
}}
globalThis.EventSource=FakeEventSource;
function stopGatewaySSE(){{if(_gatewaySSE)_gatewaySSE.close();_gatewaySSE=null;}}
function stopGatewayPollFallback(){{}}
function _installSidebarSseFocusHook(){{}}
function _sidebarSseBackgrounded(){{return false;}}
function _isDuplicateGatewaySessionSnapshot(){{return true;}}
function _isExternalSession(){{return true;}}
function _externalImportPayload(){{return {{session_id:'foreign'}};}}
function _isCliImportRefreshPrefixMatch(local,fresh){{
  return local.every((message,index)=>JSON.stringify(message)===JSON.stringify(fresh[index]));
}}
function _messageReloadLimitForSession(){{return 2;}}
function _captureSameSessionForceReloadHint(){{}}
function _clearSameSessionForceReloadHint(){{}}
function _syncToolCallsForLoadedMessages(){{}}
function clearLiveToolCards(){{}}
function clearVisibleMessageRowCache(){{}}
function _setSessionViewedCount(){{}}
function renderSessionList(){{}}
function renderMessages(){{renders+=1;}}
function highlightCode(){{}}
function api(url){{
  calls.push(url);
  if(url==='/api/session/import_cli'){{
    return Promise.resolve({{session:{{messages:[
      {{role:'user',content:'old question',timestamp:1}},
      {{role:'assistant',content:'old answer',timestamp:2}},
      ...S.messages,
      {{role:'user',content:'new turn',timestamp:5}},
    ]}}}});
  }}
  if(url.includes('/api/session?')){{
    return Promise.resolve({{session:{{
      messages:[
        {{role:'assistant',content:'tail answer',timestamp:4}},
        {{role:'user',content:'new turn',timestamp:5}},
      ],
      message_count:5,_messages_truncated:true,_messages_offset:3,_msg_limit_max:500,
    }}}});
  }}
  throw new Error('unexpected URL '+url);
}}
{helpers}
(async()=>{{
  startGatewaySSE();
  gatewaySource.emit('sessions_changed',{{sessions:[{{session_id:'foreign'}}]}});
  await new Promise(resolve=>setTimeout(resolve,0));
  await new Promise(resolve=>setTimeout(resolve,0));
  process.stdout.write(JSON.stringify({{calls,renders,messages:S.messages}}));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    result = json.loads(proc.stdout)

    assert any("msg_limit=2" in call for call in result["calls"])
    assert [message["content"] for message in result["messages"]] == ["tail answer", "new turn"]
    assert result["renders"] == 1
