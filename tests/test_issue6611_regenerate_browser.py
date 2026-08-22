import json
import subprocess
from pathlib import Path

from tests._issue6611_fixture import load_issue6611_fixture


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
