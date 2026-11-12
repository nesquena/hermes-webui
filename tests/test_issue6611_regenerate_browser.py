import json
import subprocess
from pathlib import Path

from tests._issue6611_fixture import load_issue6611_fixture
from tests.js_source_extract import extract_function


ROOT = Path(__file__).parents[1]


def _start_regeneration_source():
    source = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    start = source.index("async function startRegeneration(")
    end = source.index("\nconst LIVE_STREAMS=", start)
    return source[start:end]


def _run_node(scenario, *, with_metadata=False):
    function_source = _start_regeneration_source()
    initial_messages = load_issue6611_fixture()["rows"]
    if with_metadata:
        initial_messages[0].update({"attachments": ["proof.txt"], "custom": "keep"})
    messages_json = json.dumps(initial_messages)
    script = f"""
const result={{renders:0,busy:[],attached:[],bodies:[],thinking:0}};
let S={{session:{{session_id:'s1',regeneration_revision:'rev-1'}},messages:{messages_json}}};
const INFLIGHT={{}};
function renderMessages(){{result.renders++;}}
function setBusy(v){{result.busy.push(v);}}
function ensureLiveWorklogShell(){{result.thinking++;}}
function appendThinking(){{result.thinking++;}}
function removeThinking(){{result.thinking--;}}
function setComposerStatus(){{}}
function clearInflightState(){{}}
function markInflight(sid,streamId){{result.marked=[sid,streamId];}}
function saveInflightState(){{}}
function showLiveRunStatus(){{}}
function updateSendBtn(){{}}
function renderSessionList(){{}}
function applySessionTitleUpdate(){{}}
function attachLiveStream(sid,streamId,files){{result.attached.push([sid,streamId,files]);}}
{function_source}
async function api(_path, options){{
  result.bodies.push(JSON.parse(options.body));
  if('{scenario}'==='reject') throw new Error('typed rejection');
  if('{scenario}'==='switch'){{
    S.session={{session_id:'s2'}};
    S.messages=[{{role:'user',content:'other session'}}];
  }}
  return {{stream_id:'stream-1',pending_started_at:123,title:'Title'}};
}}
(async()=>{{
  try{{await startRegeneration('s1','rev-1');}}catch(error){{result.error=error.message;}}
  result.messages=S.messages;
  result.session=S.session;
  result.inflight=Object.keys(INFLIGHT);
  process.stdout.write(JSON.stringify(result));
}})();
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_reporter_flow_keeps_one_prompt_and_adopts_one_accepted_stream():
    result = _run_node("success", with_metadata=True)
    assert [row["role"] for row in result["messages"]] == ["user"]
    assert result["messages"][0]["content"] == "same prompt"
    assert result["messages"][0]["attachments"] == ["proof.txt"]
    assert result["messages"][0]["custom"] == "keep"
    assert result["attached"] == [["s1", "stream-1", []]]
    assert result["bodies"] == [
        {"session_id": "s1", "regenerate": True, "regeneration_revision": "rev-1"}
    ]


def test_issue_artifact_regeneration_leaves_one_user_row():
    fixture = load_issue6611_fixture()
    assert fixture["issue"] == 6611
    artifact_rows = fixture["rows"]
    result = _run_node("success")
    assert [row["role"] for row in result["messages"]].count("user") == 1
    assert [row["role"] for row in result["messages"]] == ["user"]
    assert result["messages"][0]["content"] == artifact_rows[0]["content"]


def test_normal_full_load_adopts_and_clears_regeneration_revision():
    source = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    start = source.index("function _adoptRegenerationRevision(")
    end = source.index("\n}\n\nasync function _restoreRememberedNewChatDraftSession", start) + 2
    function_source = source[start:end]
    script = f"""
let S={{session:{{session_id:'s1',regeneration_revision:'old'}}}};
{function_source}
_adoptRegenerationRevision({{session_id:'s1',regeneration_revision:'fresh'}});
if(S.session.regeneration_revision!=='fresh') throw new Error('fresh revision was not adopted');
_adoptRegenerationRevision({{session_id:'s1'}});
if(Object.prototype.hasOwnProperty.call(S.session,'regeneration_revision')) throw new Error('stale revision survived replacement');
process.stdout.write('revision adoption ok');
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True, check=True)
    assert result.stdout == "revision adoption ok"


def test_typed_rejection_restores_the_complete_local_transcript():
    result = _run_node("reject")
    assert [row["role"] for row in result["messages"]] == ["user", "assistant"]
    assert result["error"] == "typed rejection"
    assert result["busy"][-1] is False
    assert result["attached"] == []


def test_delayed_response_never_attaches_to_a_newly_selected_session():
    result = _run_node("switch")
    assert result["session"]["session_id"] == "s2"
    assert result["messages"] == [{"role": "user", "content": "other session"}]
    assert result["attached"] == []


def test_regenerate_response_has_no_truncate_or_generic_send_reentry():
    source = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    body = source[
        source.index("async function regenerateResponse"):
        source.index("// postProcessRenderedMessages")
    ]
    assert "startRegeneration(initialSid" in body
    assert "/api/session/truncate" not in body
    assert "await send(" not in body


def test_regenerate_response_loads_the_full_session_before_requiring_revision():
    source = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    body = source[
        source.index("async function regenerateResponse"):
        source.index("// postProcessRenderedMessages")
    ]
    assert "if(!S.session || S.busy || !S.session.regeneration_revision) return;" not in body
    assert body.index("await _ensureAllMessagesLoaded()") < body.index(
        "if(!S.session.regeneration_revision)"
    )


def test_metadata_without_revision_loads_messages_before_regenerating():
    sessions = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    ui = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    functions = "\n".join([
        extract_function(sessions, "_adoptRegenerationRevision"),
        extract_function(sessions, "_ensureMessagesLoaded", "async function"),
        extract_function(sessions, "_ensureAllMessagesLoaded", "async function"),
        extract_function(ui, "regenerateResponse", "async function"),
    ])
    script = f"""
const assert=require('node:assert/strict');
const window={{}}, INFLIGHT={{}};
let S, _messagesTruncated, _oldestIdx, _loadingOlder=false;
let _loadingSessionId='s1', _loadSessionGeneration=1, _msgLimitMax=500;
const _MSG_LIMIT_MAX=500;
function _clearSameSessionForceReloadHint(){{}}
function _messageReloadLimitForSession(){{return 30;}}
function _syncToolCallsForLoadedMessages(){{}}
function clearLiveToolCards(){{}}
function _setSessionViewedCount(){{}}
function _bumpMessagesGeneration(){{}}
function t(key){{return key;}}
function setStatus(message){{throw new Error(message);}}
const rows=[{{role:'user',content:'prompt'}},{{role:'assistant',content:'answer'}}];
let requests, accepted, truncated;
async function api(path){{
  requests.push(path);
  if(path.includes('messages=0')) return {{session:{{session_id:'s1',messages:[],message_count:2}}}};
  const tail=truncated&&!path.includes('msg_limit=all');
  return {{session:{{session_id:'s1',messages:tail?rows.slice(1):rows,message_count:2,
    _messages_truncated:tail,_messages_offset:tail?1:0,
    ...(tail?{{}}:{{regeneration_revision:'full-revision'}})}}}};
}}
async function startRegeneration(sid,revision){{accepted.push([sid,revision]);}}
{functions}
(async()=>{{
  for(truncated of [false,true]){{
    requests=[]; accepted=[]; _messagesTruncated=false; _oldestIdx=0;
    const metadata=await api('/api/session?session_id=s1&messages=0&resolve_model=0');
    S={{session:metadata.session,messages:[],busy:false}};
    assert.equal(S.session.regeneration_revision,undefined);
    await _ensureMessagesLoaded('s1');
    assert.equal(S.session.regeneration_revision,truncated?undefined:'full-revision');
    await regenerateResponse({{closest:()=>({{dataset:{{msgIdx:String(S.messages.length-1)}}}})}});
    assert.deepEqual(accepted,[['s1','full-revision']]);
    assert.equal(requests.length,truncated?3:2);
    if(truncated) assert.ok(requests[2].includes('messages=1&resolve_model=0&msg_limit=all'));
    assert.equal(_loadingOlder,false);
  }}
  process.stdout.write('metadata -> messages -> regenerate ok (complete and truncated)');
}})().catch(error=>{{console.error(error);process.exitCode=1;}});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True, check=True)
    assert result.stdout == "metadata -> messages -> regenerate ok (complete and truncated)"
