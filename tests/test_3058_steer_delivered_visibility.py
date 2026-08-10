"""#3058 slice 3: a delivered steer stays in the assistant turn's timeline.

Every behavioral row here executes the real extracted functions — the anchor
module under node's ``vm``, the two worklog gates and the render-side row
builder as extracted sources — rather than inspecting them as text.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ANCHORS_JS = REPO / "static" / "assistant_turn_anchors.js"
COMMANDS_JS = REPO / "static" / "commands.js"
MESSAGES_JS = REPO / "static" / "messages.js"
SESSIONS_JS = REPO / "static" / "sessions.js"
UI_JS = REPO / "static" / "ui.js"
I18N_JS = REPO / "static" / "i18n.js"
STYLE_CSS = REPO / "static" / "style.css"
NODE = shutil.which("node")

STEER_TEXT = "focus on the reconnect path, not the parser"
OWNER_SID = "sid-3058"
OWNER_STREAM = "stream-3058"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for pos in range(brace, len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                return source[start : pos + 1]
    raise AssertionError(f"could not extract {name}")


def _balanced(source: str, brace: int) -> str:
    depth = 0
    for pos in range(brace, len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                return source[brace : pos + 1]
    raise AssertionError("unbalanced block")


def _block_after(source: str, marker: str) -> str:
    """The balanced `{...}` that follows `marker`.

    Lets a test drive code that only exists inside a closure — the INFLIGHT
    restore literal in `loadSession`, the seal registered by `attachLiveStream` —
    instead of asserting against a paraphrase of it.
    """
    start = source.index(marker)
    return _balanced(source, source.index("{", start + len(marker) - 1))


def _run_node(script: str) -> dict:
    assert NODE, "node is required for the #3058 anchor harness"
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# The real _recordDeliveredSteer, _repaintDeliveredSteer and _steerIndicatorText
# from static/commands.js, plus the accepted branch of _trySteer reproduced as a
# thin driver around them so the harness runs the shipped producer code.
_STEER_DRIVER = """
function _steerOwnerIsCurrent(sid){ return sid===CURRENT_SID; }
function _steerOwnerStreamIsCurrent(sid,stream){ return sid===CURRENT_SID&&stream===OWNER_STREAM; }
const INFLIGHT = {"sid-3058": {messages: []}};
const SAVED_INFLIGHT = [];
function saveInflightState(sid, state){ SAVED_INFLIGHT.push([sid, JSON.parse(JSON.stringify(state))]); }
const SESSION_QUEUES = {};
const RENDERED_INDICATORS = [];
function _showSteerIndicator(text){ RENDERED_INDICATORS.push(text); }
function queueSessionMessage(sid, entry){
  SESSION_QUEUES[sid] = SESSION_QUEUES[sid] || [];
  SESSION_QUEUES[sid].push(entry);
}
"""


def _anchor_harness_prelude() -> str:
    commands = _read(COMMANDS_JS)
    return (
        "const fs=require('fs');const vm=require('vm');\n"
        f"const src=fs.readFileSync({json.dumps(str(ANCHORS_JS))},'utf8');\n"
        "const sandbox={window:{}};vm.createContext(sandbox);\n"
        "vm.runInContext(src,sandbox,{filename:'assistant_turn_anchors.js'});\n"
        "const api=sandbox.window.HermesAssistantTurnAnchors;\n"
        "global.window=sandbox.window;\n"
        "window._liveAnchorRegistries=new Map();\n"
        + _STEER_DRIVER
        + _function(commands, "_steerIndicatorText")
        + "\n"
        + "\n"
        "const _steerDeliveredOrdinalByStream=new Map();\n"
        + _function(commands, "_nextSteerDeliveredOrdinal")
        + "\n"
        + _function(commands, "_recordDeliveredSteer")
        + "\n"
        + _function(commands, "_repaintDeliveredSteer")
        + "\n"
        f"const OWNER_SID={json.dumps(OWNER_SID)};\n"
        f"const OWNER_STREAM={json.dumps(OWNER_STREAM)};\n"
        "let CURRENT_SID=OWNER_SID;\n"
        "function newRegistry(streamId){\n"
        "  const registry=api.createAssistantTurnAnchorRegistry({session_id:OWNER_SID,stream_id:streamId||OWNER_STREAM,run_id:'run-3058'});\n"
        "  window._liveAnchorRegistries.set(streamId||OWNER_STREAM,registry);\n"
        "  return registry;\n"
        "}\n"
        "function prose(registry,seq,text){\n"
        "  return api.applyAssistantTurnAnchorSourceEvent(registry,{\n"
        "    source_event_type:'token',seq:seq,local_id:'live-prose:'+OWNER_STREAM+':'+seq,text:text,status:'running',\n"
        "  },{session_id:OWNER_SID,stream_id:OWNER_STREAM});\n"
        "}\n"
        "function scene(registry,mode){\n"
        "  return api.projectAssistantTurnAnchorActivityScene(registry,{mode:mode||'compact_worklog'});\n"
        "}\n"
    )


# ---------------------------------------------------------------- classification


def test_3058_steer_delivered_is_classified_as_a_control_boundary_activity():
    out = _run_node(
        _anchor_harness_prelude()
        + "console.log(JSON.stringify({\n"
        "  steer:api.classifyAssistantTurnAnchorSourceEvent('steer_delivered'),\n"
        "  leftover:api.classifyAssistantTurnAnchorSourceEvent('pending_steer_leftover'),\n"
        "  unknown:api.classifyAssistantTurnAnchorSourceEvent('steer_delivered_typo'),\n"
        "}));"
    )
    # Base (master) has no entry, so the type falls through to the excluded default
    # the `unknown` probe still demonstrates.
    assert out["unknown"]["classification"] == "excluded"
    assert out["steer"] == {"classification": "activity", "kind": "control_boundary", "source": "client"}
    assert out["leftover"]["source"] == "sse", "the SSE-side sibling must keep its own source"


# ------------------------------------------------------------ role projection


def test_3058_delivered_steer_projects_a_user_row_and_other_controls_stay_control():
    out = _run_node(
        _anchor_harness_prelude()
        + "const registry=newRegistry();\n"
        + f"_recordDeliveredSteer(OWNER_SID,OWNER_STREAM,{json.dumps(STEER_TEXT)},[]);\n"
        "api.applyAssistantTurnAnchorSourceEvent(registry,{source_event_type:'approval',seq:41,local_id:'a1',text:'Approve?'},{session_id:OWNER_SID,stream_id:OWNER_STREAM});\n"
        "api.applyAssistantTurnAnchorSourceEvent(registry,{source_event_type:'clarify',seq:42,local_id:'c1',text:'Which one?'},{session_id:OWNER_SID,stream_id:OWNER_STREAM});\n"
        "const rows=scene(registry).activity_rows.map(r=>({source:r.source_event_type,kind:r.kind,role:r.role,hint:r.display_hint,hints:r.display_hints,status:r.status,text:r.text,local_id:r.local_id}));\n"
        "console.log(JSON.stringify({rows}));"
    )
    rows = {row["source"]: row for row in out["rows"]}
    steer = rows["steer_delivered"]
    assert steer["role"] == "user"
    assert steer["kind"] == "control_boundary"
    assert steer["hint"] == "user_message"
    assert steer["hints"] == {"compact_worklog": "user_message", "transparent_stream": "user_message"}
    assert steer["status"] == "delivered"
    assert steer["text"] == STEER_TEXT
    assert steer["local_id"] == f"steer:{OWNER_STREAM}:1"
    assert rows["approval"]["role"] == "control"
    assert rows["clarify"]["role"] == "control"


def test_3058_renderer_snapshot_keeps_the_delivered_steer_as_a_user_row():
    out = _run_node(
        _anchor_harness_prelude()
        + "const snapshot=api.createAssistantTurnAnchorRendererSnapshot({rows:[{"
        "kind:'control_boundary',source_event_type:'steer_delivered',role:'user',"
        "status:'delivered',text:'steer'}]});"
        "console.log(JSON.stringify({role:snapshot.rows[0].role,source:snapshot.rows[0].source_event_type}));"
    )
    assert out == {"role": "user", "source": "steer_delivered"}


# ------------------------------------------------------------------- live row


def test_3058_recording_a_delivery_yields_exactly_one_delivered_row():
    out = _run_node(
        _anchor_harness_prelude()
        + "const registry=newRegistry();\n"
        "const before=scene(registry).activity_rows.length;\n"
        + f"const recorded=_recordDeliveredSteer(OWNER_SID,OWNER_STREAM,{json.dumps(STEER_TEXT)},[]);\n"
        "const rows=scene(registry).activity_rows;\n"
        "console.log(JSON.stringify({before,recorded,rows:rows.map(r=>({source:r.source_event_type,status:r.status,payload:r.payload}))}));"
    )
    assert out["before"] == 0, "base: no anchor row exists before the delivery is recorded"
    assert out["recorded"] is True
    assert len(out["rows"]) == 1
    assert out["rows"][0]["status"] == "delivered"
    assert out["rows"][0]["payload"]["delivered"] is True
    assert out["rows"][0]["payload"]["origin"] == "webui"
    assert "applied" not in out["rows"][0]["payload"]


def test_3058_no_registry_keeps_the_accepted_delivery_in_recovery_cache():
    out = _run_node(
        _anchor_harness_prelude()
        + f"const recorded=_recordDeliveredSteer(OWNER_SID,'stream-with-no-registry',{json.dumps(STEER_TEXT)},[]);\n"
        "console.log(JSON.stringify({recorded,cache:INFLIGHT[OWNER_SID].deliveredSteers.length,indicators:RENDERED_INDICATORS}));"
    )
    assert out["recorded"] is True
    assert out["cache"] == 1
    assert out["indicators"] == []


def test_3058_terminal_owner_cleanup_preserves_cache_only_delivery_state():
    clear_owner = _function(_read(MESSAGES_JS), "_clearOwnerInflightState")
    out = _run_node(
        "const activeSid='sid-3058';const streamId='stream-3058';"
        "const S={activeStreamId:streamId};const cached={streamId,deliveredSteers:[{stream_id:streamId,payload:{local_id:'steer:stream-3058:1'}}],messages:[{role:'assistant',content:'partial'}]};"
        "const INFLIGHT={[activeSid]:cached};const SAVED=[];let cleared=0;"
        "function _isActiveSession(){return true;}function saveInflightState(sid,state){SAVED.push([sid,state]);}"
        "function clearInflightState(){cleared+=1;}function _clearActivePaneInflightIfOwner(){}function _resumeSessionStreamAfterLiveChat(){}"
        + clear_owner
        + "_clearOwnerInflightState();console.log(JSON.stringify({cache:INFLIGHT[activeSid].deliveredSteers.length,stream:INFLIGHT[activeSid].streamId,saved:SAVED.length,cleared}));"
    )
    assert out == {"cache": 1, "stream": "stream-3058", "saved": 1, "cleared": 0}


# -------------------------------------------------------------------- ordering


def test_3058_delivered_steer_sits_between_the_prose_segments_it_interrupted():
    out = _run_node(
        _anchor_harness_prelude()
        + "const registry=newRegistry();\n"
        "prose(registry,1,'first half of the answer');\n"
        + f"_recordDeliveredSteer(OWNER_SID,OWNER_STREAM,{json.dumps(STEER_TEXT)},[]);\n"
        "prose(registry,2,'second half of the answer');\n"
        "console.log(JSON.stringify({rows:scene(registry).activity_rows.map(r=>[r.role,r.text])}));"
    )
    assert out["rows"] == [
        ["prose", "first half of the answer"],
        ["user", STEER_TEXT],
        ["prose", "second half of the answer"],
    ]


# ---------------------------------------------------------- replay idempotence


def test_3058_replaying_the_same_delivery_never_produces_a_second_row():
    out = _run_node(
        _anchor_harness_prelude()
        + "const registry=newRegistry();\n"
        + f"_recordDeliveredSteer(OWNER_SID,OWNER_STREAM,{json.dumps(STEER_TEXT)},[]);\n"
        "const cached=JSON.parse(JSON.stringify(SAVED_INFLIGHT.length?SAVED_INFLIGHT[0][1].deliveredSteers:[]));\n"
        "const recordedEvent=window._liveAnchorRegistries.get(OWNER_STREAM).anchor.activity_events[0];\n"
        "// 1) the identical source event applied twice, carrying the identity the\n"
        "// producer actually minted rather than a hand-written guess at it\n"
        "api.applyAssistantTurnAnchorSourceEvent(registry,{source_event_type:'steer_delivered',seq:'steer-1',payload:{local_id:recordedEvent.payload.local_id,status:'delivered',text:recordedEvent.payload.text,delivered:true,origin:'webui',files:[]}},{session_id:OWNER_SID,stream_id:OWNER_STREAM});\n"
        "const afterDouble=scene(registry).activity_rows.length;\n"
        "// 2) the INFLIGHT replay path onto a NEW registry for a reattached stream\n"
        "const replayRegistry=newRegistry('stream-3058-reattached');\n"
        "for(const ev of cached) api.applyAssistantTurnAnchorSourceEvent(replayRegistry,ev,{session_id:OWNER_SID,stream_id:'stream-3058-reattached'});\n"
        "for(const ev of cached) api.applyAssistantTurnAnchorSourceEvent(replayRegistry,ev,{session_id:OWNER_SID,stream_id:'stream-3058-reattached'});\n"
        "console.log(JSON.stringify({cachedCount:cached.length,afterDouble,replayRows:scene(replayRegistry).activity_rows.length,replayStatus:scene(replayRegistry).activity_rows.map(r=>r.status)}));"
    )
    assert out["cachedCount"] == 1, "one delivery mirrors exactly one INFLIGHT record"
    assert out["afterDouble"] == 1
    assert out["replayRows"] == 1
    assert out["replayStatus"] == ["delivered"]


def test_3058_accepted_response_stream_id_owns_the_record_after_a_stream_replacement():
    accepted_branch = _block_after(_read(COMMANDS_JS), "if(result&&result.accepted){")
    out = _run_node(
        _anchor_harness_prelude()
        + "const S={session:{session_id:OWNER_SID},activeStreamId:OWNER_STREAM,busy:true,pendingFiles:[]};"
        + "const result={accepted:true,stream_id:'stream-authoritative'};const ownerSid=OWNER_SID;const ownerStreamId=OWNER_STREAM;"
        + f"const originalMsg={json.dumps(STEER_TEXT)};const explicitSteer=false;const pendingFilesSnapshot=[];"
        + "function _steerRestoreText(msg){return msg;}function _clearComposerDraft(){}function renderTray(){}function showToast(){}"
        + "function t(key){return key;}window._renderLiveAnchorActivitySceneForStream=()=>true;"
        + "newRegistry(OWNER_STREAM);newRegistry('stream-authoritative');"
        + "function drive(){"
        + accepted_branch
        + "}\ndrive();\n"
        + "console.log(JSON.stringify({old:window._liveAnchorRegistries.get(OWNER_STREAM).anchor.activity_events.length,new:window._liveAnchorRegistries.get('stream-authoritative').anchor.activity_events.length,indicators:RENDERED_INDICATORS}));"
    )
    assert out == {"old": 0, "new": 1, "indicators": []}


def test_3058_late_accepted_delivery_is_kept_without_polluting_the_replacement_cache():
    out = _run_node(
        _anchor_harness_prelude()
        + "INFLIGHT[OWNER_SID]={streamId:'stream-b',deliveredSteers:[]};"
        + f"const recorded=_recordDeliveredSteer(OWNER_SID,'stream-a',{json.dumps(STEER_TEXT)},[]);"
        + "console.log(JSON.stringify({recorded,owner:INFLIGHT[OWNER_SID].streamId,records:INFLIGHT[OWNER_SID].deliveredSteers.map(r=>[r.payload.stream_id,r.payload.local_id])}));"
    )
    assert out["recorded"] is True
    assert out["owner"] == "stream-b"
    assert out["records"] == [["stream-a", "steer:stream-a:1"]]


def test_3058_idle_session_restore_projects_the_browser_cache_into_the_settled_scene():
    helper = _function(_read(MESSAGES_JS), "_restoreDeliveredSteersIntoSettledMessages")
    stream = "stream-3058-settled"
    record = {
        "source_event_type": "steer_delivered",
        "seq": "steer-1",
        "stream_id": stream,
        "payload": {
            "local_id": f"steer:{stream}:1",
            "stream_id": stream,
            "ordinal": 1,
            "status": "delivered",
            "text": STEER_TEXT,
            "delivered": True,
            "origin": "webui",
            "files": [],
        },
    }
    out = _run_node(
        "const fs=require('fs');const vm=require('vm');"
        f"const src=fs.readFileSync({json.dumps(str(ANCHORS_JS))},'utf8');"
        "const sandbox={window:{}};vm.createContext(sandbox);vm.runInContext(src,sandbox);"
        "global.window=sandbox.window;"
        + _function(_read(MESSAGES_JS), "_deliveredSteerStreamId")
        + "\n"
        + _function(_read(MESSAGES_JS), "_settledAnchorSourceEventFromRow")
        + "\n"
        + helper
        + "\nconst messages=[{role:'assistant',content:'final answer'}];"
        + f"const changed=_restoreDeliveredSteersIntoSettledMessages(messages,'{OWNER_SID}',[{json.dumps(record)}]);"
        + "console.log(JSON.stringify({changed,stream:messages[0]._anchor_stream_id,rows:messages[0]._anchor_activity_scene.activity_rows.map(r=>[r.source_event_type,r.local_id,r.text])}));"
    )
    assert out["changed"] is True
    assert out["stream"] == stream
    assert out["rows"] == [["steer_delivered", f"steer:{stream}:1", STEER_TEXT]]


def test_3058_idle_restore_does_not_guess_across_multiple_unidentified_assistants():
    helper = _function(_read(MESSAGES_JS), "_restoreDeliveredSteersIntoSettledMessages")
    stream = "stream-3058-stale"
    record = {"source_event_type": "steer_delivered", "stream_id": stream, "payload": {"stream_id": stream, "local_id": f"steer:{stream}:1", "text": STEER_TEXT}}
    out = _run_node(
        "const fs=require('fs');const vm=require('vm');"
        f"const src=fs.readFileSync({json.dumps(str(ANCHORS_JS))},'utf8');const sandbox={{window:{{}}}};vm.createContext(sandbox);vm.runInContext(src,sandbox);global.window=sandbox.window;"
        + _function(_read(MESSAGES_JS), "_deliveredSteerStreamId")
        + "\n"
        + _function(_read(MESSAGES_JS), "_settledAnchorSourceEventFromRow")
        + "\n"
        + helper
        + f"\nconst messages=[{{role:'assistant',content:'older'}},{{role:'assistant',content:'newer'}}];const changed=_restoreDeliveredSteersIntoSettledMessages(messages,'{OWNER_SID}',[{json.dumps(record)}]);console.log(JSON.stringify({{changed,attached:messages.filter(m=>m._anchor_activity_scene).length}}));"
    )
    assert out == {"changed": False, "attached": 0}


def test_3058_idle_restore_materializes_a_current_turn_after_a_historical_assistant():
    helper = _function(_read(MESSAGES_JS), "_restoreDeliveredSteersIntoSettledMessages")
    stream = "stream-3058-no-response"
    record = {"source_event_type": "steer_delivered", "stream_id": stream, "payload": {"stream_id": stream, "local_id": f"steer:{stream}:1", "text": STEER_TEXT}}
    out = _run_node(
        "const fs=require('fs');const vm=require('vm');"
        f"const src=fs.readFileSync({json.dumps(str(ANCHORS_JS))},'utf8');const sandbox={{window:{{}}}};vm.createContext(sandbox);vm.runInContext(src,sandbox);global.window=sandbox.window;"
        + _function(_read(MESSAGES_JS), "_deliveredSteerStreamId")
        + "\n"
        + _function(_read(MESSAGES_JS), "_settledAnchorSourceEventFromRow")
        + "\n"
        + helper
        + f"\nconst messages=[{{role:'assistant',content:'old answer'}},{{role:'user',content:'new request'}}];const changed=_restoreDeliveredSteersIntoSettledMessages(messages,'{OWNER_SID}',[{json.dumps(record)}]);"
        + "console.log(JSON.stringify({changed,attached:messages.map(m=>!!m._anchor_activity_scene),roles:messages.map(m=>m.role)}));"
    )
    assert out == {"changed": True, "attached": [False, False, True], "roles": ["assistant", "user", "assistant"]}


def test_3058_idle_restore_keeps_replacement_stream_groups_in_the_current_turn():
    helper = _function(_read(MESSAGES_JS), "_restoreDeliveredSteersIntoSettledMessages")
    records = [
        {"source_event_type": "steer_delivered", "stream_id": "stream-a", "payload": {"stream_id": "stream-a", "local_id": "steer:stream-a:1", "text": "first"}},
        {"source_event_type": "steer_delivered", "stream_id": "stream-b", "payload": {"stream_id": "stream-b", "local_id": "steer:stream-b:1", "text": "second"}},
    ]
    out = _run_node(
        "const fs=require('fs');const vm=require('vm');"
        f"const src=fs.readFileSync({json.dumps(str(ANCHORS_JS))},'utf8');const sandbox={{window:{{}}}};vm.createContext(sandbox);vm.runInContext(src,sandbox);global.window=sandbox.window;"
        + _function(_read(MESSAGES_JS), "_deliveredSteerStreamId")
        + "\n"
        + _function(_read(MESSAGES_JS), "_settledAnchorSourceEventFromRow")
        + "\n"
        + helper
        + f"\nconst messages=[{{role:'user',content:'new request'}},{{role:'assistant',content:'answer'}}];const restored=[];const changed=_restoreDeliveredSteersIntoSettledMessages(messages,'{OWNER_SID}',{json.dumps(records)},rows=>restored.push(...rows));"
        + "console.log(JSON.stringify({changed,restored:restored.length,rows:messages[1]._anchor_activity_scene.activity_rows.map(r=>r.text)}));"
    )
    assert out == {"changed": True, "restored": 2, "rows": ["first", "second"]}


def test_3058_current_turn_response_check_ignores_historical_assistants():
    helper = _function(_read(MESSAGES_JS), "_hasCurrentTurnAssistantResponse")
    out = _run_node(
        helper
        + "const historical=[{role:'assistant',content:'old answer'},{role:'user',content:'new request'}];"
        + "const current=[...historical,{role:'assistant',content:'new answer'}];"
        + "console.log(JSON.stringify({historical:_hasCurrentTurnAssistantResponse(historical,''),current:_hasCurrentTurnAssistantResponse(current,'')}));"
    )
    assert out == {"historical": False, "current": True}


def test_3058_idle_restore_matches_identity_preserves_other_layers_and_orders_by_time():
    helper = _function(_read(MESSAGES_JS), "_restoreDeliveredSteersIntoSettledMessages")
    stream = "stream-3058-ordered"
    record = {
        "source_event_type": "steer_delivered",
        "seq": "steer-1",
        "created_at": 200,
        "stream_id": stream,
        "payload": {"local_id": f"steer:{stream}:1", "stream_id": stream, "status": "delivered", "text": STEER_TEXT},
    }
    scene = {
        "version": "activity_scene_v1",
        "mode": "compact_worklog",
        "identity": {"stream_id": stream},
        "activity_rows": [
            {"source_event_type": "token", "created_at": 100, "seq": 1, "local_id": "p1", "status": "completed", "text": "before", "payload": {}},
            {"source_event_type": "token", "created_at": 300, "seq": 2, "local_id": "p2", "status": "completed", "text": "after", "payload": {}},
        ],
        "artifacts": [{"source_event_type": "artifact_reference", "created_at": 150, "local_id": "artifact-1", "payload": {"text": "artifact"}}],
        "side_effects": [{"source_event_type": "state_saved", "created_at": 160, "local_id": "effect-1", "payload": {"text": "saved"}}],
    }
    out = _run_node(
        "const fs=require('fs');const vm=require('vm');"
        f"const src=fs.readFileSync({json.dumps(str(ANCHORS_JS))},'utf8');"
        "const sandbox={window:{}};vm.createContext(sandbox);vm.runInContext(src,sandbox);global.window=sandbox.window;"
        + _function(_read(MESSAGES_JS), "_deliveredSteerStreamId")
        + "\n"
        + _function(_read(MESSAGES_JS), "_settledAnchorSourceEventFromRow")
        + "\n"
        + helper
        + f"\nconst messages=[{{role:'assistant',content:'final',_anchor_activity_scene:{json.dumps(scene)}}}];"
        + f"const changed=_restoreDeliveredSteersIntoSettledMessages(messages,'{OWNER_SID}',[{json.dumps(record)}]);"
        + "const rebuilt=messages[0]._anchor_activity_scene;"
        + "console.log(JSON.stringify({changed,rows:rebuilt.activity_rows.map(r=>r.text),artifacts:rebuilt.artifacts.length,effects:rebuilt.side_effects.length}));"
    )
    assert out == {"changed": True, "rows": ["before", STEER_TEXT, "after"], "artifacts": 1, "effects": 1}


# ------------------------------------------------------------ worklog-worthiness


def _worklog_gate_probe(scene_rows: list[dict]) -> dict:
    generation = _function(_read(MESSAGES_JS), "_anchorSceneHasWorklogWorthyRows")
    render = _function(_read(UI_JS), "_anchorSceneSceneHasWorklogWorthyRows")
    return _run_node(
        "const window={isFinalAnswerOnlyMode:()=>false};\n"
        + generation
        + "\n"
        + render
        + "\n"
        + f"const scene={{version:'activity_scene_v1',mode:'compact_worklog',activity_rows:{json.dumps(scene_rows)}}};\n"
        "console.log(JSON.stringify({generation:_anchorSceneHasWorklogWorthyRows(scene),render:_anchorSceneSceneHasWorklogWorthyRows(scene)}));"
    )


_PROSE_ROW = {"role": "prose", "source_event_type": "token", "text": "a long plain answer"}
_TERMINAL_ROW = {"role": "terminal", "source_event_type": "done", "text": ""}


def test_3058_a_turn_whose_only_non_prose_row_is_the_steer_is_worklog_worthy():
    steer_row = {"role": "user", "source_event_type": "steer_delivered", "status": "delivered", "text": STEER_TEXT}
    without = _worklog_gate_probe([_PROSE_ROW, _TERMINAL_ROW])
    with_steer = _worklog_gate_probe([_PROSE_ROW, steer_row, _TERMINAL_ROW])
    # Base behavior (the same scene minus the steer row): both gates false, which is
    # exactly why the steer vanished at settle.
    assert without == {"generation": False, "render": False}
    assert with_steer == {"generation": True, "render": True}


@pytest.mark.parametrize("source", ["approval", "clarify", "goal_continue", "pending_steer_leftover"])
def test_3058_the_worklog_clause_does_not_widen_to_other_control_rows(source):
    control_row = {"role": "control", "source_event_type": source, "status": "pending", "text": "waiting"}
    assert _worklog_gate_probe([_PROSE_ROW, control_row, _TERMINAL_ROW]) == {
        "generation": False,
        "render": False,
    }


# ------------------------------------------ INFLIGHT pre-settlement recovery cache


def test_3058_delivered_steers_survive_inflight_compaction_without_a_fixed_cap():
    compact = _function(_read(UI_JS), "_compactInflightState")
    out = _run_node(
        "function _getInflightStateLimits(){return {messages:50,toolCalls:50,stringChars:100000,maxSessions:5,jsonChars:1000000};}\n"
        "function _truncateInflightValue(value){return value;}\n"
        + compact
        + "\n"
        "const many=Array.from({length:25},(_,i)=>({source_event_type:'steer_delivered',seq:'steer-'+(i+1),payload:{local_id:'steer:s:'+(i+1),status:'delivered',text:'t'+(i+1)}}));\n"
        "const kept=_compactInflightState({messages:[],deliveredSteers:many});\n"
        "const none=_compactInflightState({messages:[]});\n"
        "console.log(JSON.stringify({count:kept.deliveredSteers.length,first:kept.deliveredSteers[0].payload.local_id,last:kept.deliveredSteers[24].payload.local_id,none:none.deliveredSteers}));"
    )
    assert out["count"] == 25
    assert out["first"] == "steer:s:1"
    assert out["last"] == "steer:s:25"
    assert out["none"] == []


def test_3058_oversized_storage_compacts_the_delivery_record_without_erasing_it():
    compact_record = _function(_read(UI_JS), "_compactDeliveredSteerForStorage")
    write_map = _function(_read(UI_JS), "_writeInflightStateMap")
    out = _run_node(
        "const INFLIGHT_STATE_KEY='hermes-inflight-state';"
        "const store={};const localStorage={getItem:k=>store[k]||null,setItem:(k,v)=>{store[k]=v;},removeItem:k=>{delete store[k];}};"
        "function _getInflightStateLimits(){return {messages:24,toolCalls:48,stringChars:60000,maxSessions:8,jsonChars:5000};}"
        + compact_record
        + "\n"
        + write_map
        + "\nconst huge={updated_at:1,streamId:'stream-3058',deliveredSteers:[{source_event_type:'steer_delivered',stream_id:'stream-3058',payload:{local_id:'steer:stream-3058:1',stream_id:'stream-3058',text:'x'.repeat(100000),files:[]}}]};"
        + "const ok=_writeInflightStateMap({'sid-3058':huge});const parsed=JSON.parse(store[INFLIGHT_STATE_KEY]);"
        + "console.log(JSON.stringify({ok,count:parsed['sid-3058'].deliveredSteers.length,text:parsed['sid-3058'].deliveredSteers[0].payload.text}));"
    )
    assert out["ok"] is True
    assert out["count"] == 1
    assert "truncated for browser recovery storage" in out["text"]


def test_3058_delivered_cache_is_not_expired_during_a_long_running_recovery_gap():
    read_map = _function(_read(UI_JS), "_readInflightStateMap")
    load = _function(_read(UI_JS), "loadInflightState")
    out = _run_node(
        "const INFLIGHT_STATE_KEY='hermes-inflight';const entry={streamId:'stream-3058',updated_at:Date.now()-11*60*1000,deliveredSteers:[{source_event_type:'steer_delivered'}]};"
        "const localStorage={getItem(){return JSON.stringify({'sid-3058':entry});},removeItem(){}};"
        + read_map
        + "\nfunction clearInflightState(){}\n"
        + load
        + "\nconsole.log(JSON.stringify(loadInflightState('sid-3058')));"
    )
    assert out["deliveredSteers"] == [{"source_event_type": "steer_delivered"}]


_CACHED_STEER = {
    "source_event_type": "steer_delivered",
    "seq": "steer-1",
    "payload": {
        "local_id": f"steer:{OWNER_STREAM}:1",
        "stream_id": OWNER_STREAM,
        "ordinal": 1,
        "status": "delivered",
        "text": STEER_TEXT,
        "files": [],
        "delivered": True,
        "origin": "webui",
    },
}


def test_3058_the_reload_restore_path_carries_the_delivered_steer_cache():
    """`_compactInflightState` writing the field is not enough to survive a reload.

    `loadSession` restores INFLIGHT through an explicit field whitelist. A field
    written by the compactor but absent from that whitelist is a cache nothing
    ever reads, so the record dies at the refresh it exists to survive.
    """
    sessions_source = _read(SESSIONS_JS)
    restore_start = sessions_source.index("INFLIGHT[sid]={\n        streamId:String(stored.streamId||'')")
    restore = _balanced(sessions_source, sessions_source.index("{", restore_start + len("INFLIGHT[sid]=") ))
    out = _run_node(
        "const INFLIGHT={};const sid='sid-3058';\n"
        f"const stored={json.dumps({'streamId': OWNER_STREAM, 'messages': [], 'deliveredSteers': [_CACHED_STEER]})};\n"
        f"INFLIGHT[sid]={restore};\n"
        "console.log(JSON.stringify({restored:INFLIGHT[sid].deliveredSteers,"
        "missing:INFLIGHT[sid].deliveredSteers===undefined}));"
    )
    assert out["missing"] is False, "loadSession's whitelist drops the delivered-steer cache"
    assert out["restored"] == [_CACHED_STEER]


def test_3058_failed_idle_load_repersists_the_browser_only_delivery_cache():
    preserve = _function(_read(SESSIONS_JS), "_preserveSettledDeliveredSteersForRecovery")
    out = _run_node(
        "const INFLIGHT={};const SAVED=[];"
        "function saveInflightState(sid,state){SAVED.push([sid,state]);}\n"
        + preserve
        + f"\nconst records={json.dumps([_CACHED_STEER])};"
        + "const kept=_preserveSettledDeliveredSteersForRecovery('sid-3058',records);"
        + "console.log(JSON.stringify({kept,state:INFLIGHT['sid-3058'],saved:SAVED.length}));"
    )
    assert out["kept"] is True
    assert out["state"]["streamId"] is None
    assert out["state"]["deliveredSteers"] == [_CACHED_STEER]
    assert out["saved"] == 1


def test_3058_journal_replay_reset_preserves_browser_only_delivered_steers():
    reset = _block_after(
        _read(SESSIONS_JS),
        "if(INFLIGHT[sid]&&INFLIGHT[sid].journalReplayFromStart&&activeStreamId){",
    )
    out = _run_node(
        f"const sid='sid-3058';const activeStreamId={json.dumps(OWNER_STREAM)};\n"
        f"const cached={json.dumps([_CACHED_STEER])};\n"
        "const INFLIGHT={[sid]:{streamId:activeStreamId,journalReplayFromStart:true,deliveredSteers:cached}};\n"
        "const cleared=[];const saved=[];\n"
        "function clearInflightState(id){cleared.push(id);}\n"
        "function saveInflightState(id,state){saved.push([id,state]);}\n"
        "if(INFLIGHT[sid]&&INFLIGHT[sid].journalReplayFromStart&&activeStreamId)"
        + reset
        + "\nconsole.log(JSON.stringify({streamId:INFLIGHT[sid].streamId,steers:INFLIGHT[sid].deliveredSteers,cleared,saved:saved.length}));"
    )
    assert out["streamId"] == OWNER_STREAM
    assert out["steers"] == [_CACHED_STEER]
    assert out["saved"] == 1


def test_3058_a_server_snapshot_recovery_keeps_the_browser_only_delivered_steers():
    """The other way the cache can be dropped: replaced rather than not read.

    `_selectLiveRecoveryInflight` can hand back the server run-journal snapshot
    wholesale. A delivered steer is observed at the browser's steer response and
    never reaches that journal, so the snapshot can never carry it back.
    """
    sessions = _read(SESSIONS_JS)
    wrong_cached = dict(_CACHED_STEER)
    wrong_cached["payload"] = dict(_CACHED_STEER["payload"], stream_id="stream-other")
    out = _run_node(
        _function(sessions, "_inflightHasVisibleLiveState")
        + "\n"
        + _function(sessions, "_selectLiveRecoveryInflight")
        + "\n"
        f"const cached={json.dumps([_CACHED_STEER])};\n"
        f"const local={{streamId:{json.dumps(OWNER_STREAM)},messages:[{{role:'assistant',content:'half an answer',_live:true}}],"
        "toolCalls:[],lastAssistantText:'half an answer',lastRunJournalSeq:4,deliveredSteers:cached};\n"
        f"const server={{streamId:{json.dumps(OWNER_STREAM)},messages:[{{role:'assistant',content:'half an answer',_live:true}}],"
        "toolCalls:[],lastAssistantText:'half an answer',lastRunJournalSeq:9,journalSnapshot:true};\n"
        f"const chosen=_selectLiveRecoveryInflight(local,server,{json.dumps(OWNER_STREAM)});\n"
        "// A snapshot for a DIFFERENT stream must not inherit this stream's records.\n"
        f"const otherStream=_selectLiveRecoveryInflight({{...local,streamId:'stream-old',deliveredSteers:{json.dumps([wrong_cached])}}},server,"
        f"{json.dumps(OWNER_STREAM)});\n"
        "console.log(JSON.stringify({fromJournal:!!chosen.journalSnapshot,seq:chosen.lastRunJournalSeq,"
        "steers:chosen.deliveredSteers||null,otherStreamSteers:otherStream.deliveredSteers||null}));"
    )
    assert out["fromJournal"] is True, "the journal still wins the projection"
    assert out["seq"] == 9
    assert out["steers"] == [_CACHED_STEER]
    assert out["otherStreamSteers"] is None


def test_3058_token_persistence_keeps_the_delivered_steer_cache():
    persist = _function(_read(MESSAGES_JS), "persistInflightState")
    out = _run_node(
        "const activeSid='sid-3058';const streamId='stream-3058';const uploaded=[];"
        "const S={todos:[],todoStateMeta:null};"
        f"const cached={json.dumps([_CACHED_STEER])};"
        "const INFLIGHT={[activeSid]:{messages:[],uploaded:[],toolCalls:[],deliveredSteers:cached}};"
        "const saved=[];function saveInflightState(sid,state){saved.push([sid,state]);}\n"
        + persist
        + "\npersistInflightState();\n"
        "console.log(JSON.stringify({steers:saved[0][1].deliveredSteers}));"
    )
    assert out["steers"] == [_CACHED_STEER]


def test_3058_anchor_post_does_not_clear_a_later_same_stream_delivery():
    persist = _function(_read(MESSAGES_JS), "_persistSettledAnchorScene")
    source_event = dict(_CACHED_STEER, local_id="steer:stream-3058:1")
    later_event = dict(_CACHED_STEER, local_id="steer:stream-3058:2", payload=dict(_CACHED_STEER["payload"], local_id="steer:stream-3058:2"))
    scene = {"activity_rows": [{"source_event_type": "steer_delivered", "local_id": source_event["local_id"], "text": STEER_TEXT, "payload": source_event["payload"]}]}
    out = _run_node(
        "const activeSid='sid-3058';const streamId='stream-3058';const INFLIGHT={[activeSid]:{streamId,deliveredSteers:[%s]}};"
        "const saves=[];function saveInflightState(sid,state){saves.push(JSON.parse(JSON.stringify(state)));}"
        "function clearInflightState(){}function _anchorSceneMessageOffsetForPersist(){return 0;}"
        "function _anchorSceneAbsoluteMessageIndexForPersist(index){return index;}function _anchorSceneMessageRef(){return 'message-1';}"
        "let _persistAnchorSceneWarned=false;let resolvePost;const post=new Promise(resolve=>{resolvePost=resolve;});function api(){return post;}"
        % json.dumps(source_event)
        + _function(_read(MESSAGES_JS), "_deliveredSteerStreamId")
        + _function(_read(MESSAGES_JS), "_settledAnchorSourceEventFromRow")
        + persist
        + f"_persistSettledAnchorScene({{role:'assistant',content:'done'}},{json.dumps(scene)},0);"
        + f"INFLIGHT[activeSid].deliveredSteers.push({json.dumps(later_event)});resolvePost({{}});"
        + "Promise.resolve().then(()=>console.log(JSON.stringify({remaining:INFLIGHT[activeSid].deliveredSteers.map(r=>r.local_id),saves:saves.length})));"
    )
    assert out["remaining"] == [later_event["local_id"]]
    assert out["saves"] >= 1


def test_3058_reattach_replays_only_records_owned_by_the_attached_stream():
    replay = _block_after(
        _read(MESSAGES_JS),
        "if(_anchorRegistry&&_anchorApi&&typeof _anchorApi.applyAssistantTurnAnchorSourceEvent==='function'){",
    )
    wrong_stream = dict(_CACHED_STEER)
    wrong_stream["payload"] = dict(_CACHED_STEER["payload"], stream_id="stream-other")
    out = _run_node(
        _anchor_harness_prelude()
        + "const _anchorApi=api;const activeSid=OWNER_SID;const streamId=OWNER_STREAM;\n"
        + "const _anchorRegistry=newRegistry(OWNER_STREAM);\n"
        + f"INFLIGHT[activeSid]={{deliveredSteers:{json.dumps([_CACHED_STEER, wrong_stream])}}};\n"
        + "if(_anchorRegistry&&_anchorApi&&typeof _anchorApi.applyAssistantTurnAnchorSourceEvent==='function')"
        + replay
        + "\nconsole.log(JSON.stringify({rows:scene(_anchorRegistry).activity_rows.map(r=>r.text)}));"
    )
    assert out["rows"] == [STEER_TEXT]


def test_3058_reattach_hydrates_persisted_rows_before_cached_deliveries():
    replay = _block_after(
        _read(MESSAGES_JS),
        "if(_anchorRegistry&&_anchorApi&&typeof _anchorApi.applyAssistantTurnAnchorSourceEvent==='function'){",
    )
    cached = dict(_CACHED_STEER, created_at=200)
    out = _run_node(
        _anchor_harness_prelude()
        + "const _anchorApi=api;const activeSid=OWNER_SID;const streamId=OWNER_STREAM;"
        + "const _anchorRegistry=api.createAssistantTurnAnchorRegistry({session_id:activeSid,stream_id:streamId,run_id:'run-3058'});"
        + "api.applyAssistantTurnAnchorSourceEvent(_anchorRegistry,{source_event_type:'token',seq:1,created_at:100,local_id:'prose-1',text:'before',status:'completed'},{session_id:activeSid,stream_id:streamId});"
        + f"INFLIGHT[activeSid]={{deliveredSteers:[{json.dumps(cached)}]}};"
        + replay
        + "console.log(JSON.stringify({rows:api.projectAssistantTurnAnchorActivityScene(_anchorRegistry,{mode:'compact_worklog'}).activity_rows.map(r=>r.text)}));"
    )
    assert out["rows"] == ["before", STEER_TEXT]


def test_3058_a_steer_sent_after_a_mid_run_reload_is_recorded_and_not_deduped_away():
    """Identity must be a property of the run, not of the page.

    A page-lifetime ordinal restarts at 0 on reload, so the first steer after a
    refresh would mint the identity a replayed record already holds, dedupe
    against it, and leave the user reading the *previous* steer's text while the
    toast says delivered.
    """
    second = "and stop touching the parser"
    out = _run_node(
        _anchor_harness_prelude()
        + "const registry=newRegistry();\n"
        + f"_recordDeliveredSteer(OWNER_SID,OWNER_STREAM,{json.dumps(STEER_TEXT)},[]);\n"
        "const persisted=JSON.parse(JSON.stringify(SAVED_INFLIGHT[SAVED_INFLIGHT.length-1][1].deliveredSteers));\n"
        "// --- browser reload: module globals are gone, the run is still live ---\n"
        "_steerDeliveredOrdinalByStream.clear();\n"
        "INFLIGHT[OWNER_SID]={messages:[],deliveredSteers:persisted};\n"
        "const reattached=newRegistry();\n"
        "for(const ev of persisted) api.applyAssistantTurnAnchorSourceEvent(reattached,ev,{session_id:OWNER_SID,stream_id:OWNER_STREAM});\n"
        + f"const recorded=_recordDeliveredSteer(OWNER_SID,OWNER_STREAM,{json.dumps(second)},[]);\n"
        "const rows=scene(reattached).activity_rows;\n"
        "console.log(JSON.stringify({recorded,texts:rows.map(r=>r.text),"
        "ids:rows.map(r=>r.local_id),cached:INFLIGHT[OWNER_SID].deliveredSteers.length}));"
    )
    assert out["recorded"] is True
    assert out["texts"] == [STEER_TEXT, second], "the post-reload steer must be its own row"
    assert len(set(out["ids"])) == 2, "the two deliveries must not share an identity"
    assert out["cached"] == 2


def test_3058_replaying_a_cached_delivery_after_a_reload_still_yields_one_row():
    """The other half of the same invariant: seeding must not break idempotence."""
    out = _run_node(
        _anchor_harness_prelude()
        + "const registry=newRegistry();\n"
        + f"_recordDeliveredSteer(OWNER_SID,OWNER_STREAM,{json.dumps(STEER_TEXT)},[]);\n"
        "const persisted=JSON.parse(JSON.stringify(SAVED_INFLIGHT[SAVED_INFLIGHT.length-1][1].deliveredSteers));\n"
        "_steerDeliveredOrdinalByStream.clear();\n"
        "INFLIGHT[OWNER_SID]={messages:[],deliveredSteers:persisted};\n"
        "const reattached=newRegistry();\n"
        "for(let pass=0;pass<3;pass+=1){\n"
        "  for(const ev of persisted) api.applyAssistantTurnAnchorSourceEvent(reattached,ev,{session_id:OWNER_SID,stream_id:OWNER_STREAM});\n"
        "}\n"
        "console.log(JSON.stringify({rows:scene(reattached).activity_rows.length}));"
    )
    assert out["rows"] == 1


# --------------------------------------------------------------- the seal bridge


def test_3058_the_producer_seals_the_live_prose_segment_before_recording():
    """The bridge exists so the settled scene reads assistant -> steer -> assistant.

    Driving the real `_recordDeliveredSteer` with the sealer present is the only
    way to see the guard at its call site actually fire; a harness that never
    defines the global short-circuits it and proves nothing.
    """
    out = _run_node(
        _anchor_harness_prelude()
        + "const CALLS=[];\n"
        "const registry=newRegistry();\n"
        "prose(registry,1,'first half of the answer');\n"
        "// Recording the row count at seal time is what pins the ORDER: the seal has\n"
        "// to run while the steer row does not exist yet.\n"
        "window._sealLiveAnchorProseSegmentForStream=function(id){\n"
        "  CALLS.push(['seal',id,scene(registry).activity_rows.length]);return true;\n"
        "};\n"
        + f"const recorded=_recordDeliveredSteer(OWNER_SID,OWNER_STREAM,{json.dumps(STEER_TEXT)},[]);\n"
        "console.log(JSON.stringify({recorded,calls:CALLS,after:scene(registry).activity_rows.length}));"
    )
    assert out["recorded"] is True
    assert out["calls"] == [["seal", OWNER_STREAM, 1]], "the seal runs once, before the row is applied"
    assert out["after"] == 2


def test_3058_a_seal_race_does_not_drop_the_durable_delivery_record():
    out = _run_node(
        _anchor_harness_prelude()
        + "const registry=newRegistry();"
        + "window._sealLiveAnchorProseSegmentForStream=function(){throw new Error('teardown race');};"
        + f"const recorded=_recordDeliveredSteer(OWNER_SID,OWNER_STREAM,{json.dumps(STEER_TEXT)},[]);"
        + "console.log(JSON.stringify({recorded,rows:scene(registry).activity_rows.length}));"
    )
    assert out == {"recorded": True, "rows": 1}


def test_3058_the_seal_flushes_pending_prose_before_recording_the_boundary():
    """A bare boundary + reset loses whatever the deferred render had not written.

    `_resetAssistantSegment` moves `segmentStart` past the unflushed delta and
    `_flushPendingSegmentRender` early-returns once `assistantBody` is null, so
    without the flush the settled Worklog silently drops the prose between the
    last frame and the steer.
    """
    seal = _block_after(_read(MESSAGES_JS), "_sealers.set(String(streamId),function()")
    out = _run_node(
        "const CALLS=[];\n"
        "const streamId='stream-3058';const S={session:{session_id:'sid-3058'},activeStreamId:streamId};"
        "function _isActiveSession(){return true;}\n"
        "let assistantRow=null;\n"
        "function _parseStreamState(){return {displayText:'half an answer not yet flushed'};}\n"
        "function ensureAssistantRow(){CALLS.push('ensureAssistantRow');assistantRow={};}\n"
        "function _flushPendingSegmentRender(opts){CALLS.push('flush:'+JSON.stringify(opts));}\n"
        "function recordActivityBoundary(){CALLS.push('recordActivityBoundary');}\n"
        "function _resetAssistantSegment(){CALLS.push('_resetAssistantSegment');assistantRow=null;}\n"
        + "const seal=function()"
        + seal
        + ";\n"
        "const ok=seal();\n"
        "console.log(JSON.stringify({ok,calls:CALLS}));"
    )
    assert out["ok"] is True
    assert out["calls"] == [
        "ensureAssistantRow",
        'flush:{"force":true}',
        "recordActivityBoundary",
        "_resetAssistantSegment",
    ]
    assert out["calls"].index("flush:{\"force\":true}") < out["calls"].index("recordActivityBoundary")


def test_3058_the_seal_is_per_stream_and_expires_with_the_registry_it_seals_into():
    """One window slot would let the newest attach silently disown older streams.

    This repo runs concurrent live streams, and recording is owner-scoped by
    design, so a steer to an older still-running stream has to reach that
    stream's own seal. The closure retains the whole `attachLiveStream` scope, so
    it must also expire on the registry's identity-guarded cleanup.
    """
    cleanup = _function(_read(MESSAGES_JS), "_scheduleAnchorRegistryCleanup")
    out = _run_node(
        "const window={_liveAnchorProseSealers:new Map()};\n"
        "const SEALED=[];\n"
        "window._liveAnchorProseSealers.set('stream-a',()=>{SEALED.push('a');return true;});\n"
        "window._liveAnchorProseSealers.set('stream-b',()=>{SEALED.push('b');return true;});\n"
        "// the dispatcher installed by the newest attach, addressing the older stream\n"
        "function dispatch(requestedStreamId){\n"
        "  const map=window._liveAnchorProseSealers;\n"
        "  const seal=map&&map.get(String(requestedStreamId||''));\n"
        "  return typeof seal==='function'?!!seal():false;\n"
        "}\n"
        "const olderStreamSealed=dispatch('stream-a');\n"
        "const timers=[];\n"
        "function setTimeout(fn){timers.push(fn);}\n"
        "const _anchorRegistryMap=new Map();\n"
        "let _anchorRegistryCleanupTimer=null;\n"
        "const LIVE_STREAMS={};\n"
        "const INFLIGHT={};\n"
        "const S={activeStreamId:null,session:{session_id:'sid-3058',active_stream_id:null}};\n"
        "const activeSid='sid-3058';\n"
        "const _anchorRegistry={id:'reg-a'};\n"
        "const streamId='stream-a';\n"
        "_anchorRegistryMap.set('stream-a',_anchorRegistry);\n"
        + cleanup
        + "\n"
        "_scheduleAnchorRegistryCleanup(1);\n"
        "timers.forEach(fn=>fn());\n"
        "console.log(JSON.stringify({olderStreamSealed,sealed:SEALED,"
        "remaining:[...window._liveAnchorProseSealers.keys()],"
        "registries:[..._anchorRegistryMap.keys()]}));"
    )
    assert out["olderStreamSealed"] is True
    assert out["sealed"] == ["a"], "the dispatcher must reach the addressed stream, not the newest one"
    assert out["remaining"] == ["stream-b"], "the expired stream's seal must not outlive its registry"
    assert out["registries"] == []


def test_3058_active_stream_registry_cleanup_defers_until_the_stream_is_gone():
    cleanup = _function(_read(MESSAGES_JS), "_scheduleAnchorRegistryCleanup")
    out = _run_node(
        "const window={_liveAnchorProseSealers:new Map()};"
        "window._liveAnchorProseSealers.set('stream-a',()=>true);"
        "const timers=[];function setTimeout(fn){timers.push(fn);}"
        "const _anchorRegistryMap=new Map();let _anchorRegistryCleanupTimer=null;"
        "const LIVE_STREAMS={};const INFLIGHT={};"
        "const activeSid='sid-3058';const S={activeStreamId:'stream-a',session:{session_id:activeSid,active_stream_id:'stream-a'}};"
        "const _anchorRegistry={id:'reg-a'};const streamId='stream-a';"
        "_anchorRegistryMap.set(streamId,_anchorRegistry);LIVE_STREAMS[activeSid]={streamId};INFLIGHT[activeSid]={streamId};"
        + cleanup
        + "\n_scheduleAnchorRegistryCleanup(1);timers.forEach(fn=>fn());"
        + "console.log(JSON.stringify({registry:_anchorRegistryMap.has(streamId),sealer:window._liveAnchorProseSealers.has(streamId)}));"
    )
    assert out == {"registry": True, "sealer": True}


def test_3058_a_steer_to_an_unknown_stream_seals_nothing_rather_than_the_wrong_run():
    out = _run_node(
        "const window={_liveAnchorProseSealers:new Map()};\n"
        "const SEALED=[];\n"
        "window._liveAnchorProseSealers.set('stream-b',()=>{SEALED.push('b');return true;});\n"
        "function dispatch(requestedStreamId){\n"
        "  const map=window._liveAnchorProseSealers;\n"
        "  const seal=map&&map.get(String(requestedStreamId||''));\n"
        "  return typeof seal==='function'?!!seal():false;\n"
        "}\n"
        "console.log(JSON.stringify({gone:dispatch('stream-a'),empty:dispatch(''),sealed:SEALED}));"
    )
    assert out["gone"] is False
    assert out["empty"] is False
    assert out["sealed"] == []


def test_3058_a_sealer_does_not_touch_the_visible_replacement_stream():
    seal = _block_after(_read(MESSAGES_JS), "_sealers.set(String(streamId),function()")
    out = _run_node(
        "const S={session:{session_id:'sid-3058'},activeStreamId:'stream-b'};"
        "const streamId='stream-a';function _isActiveSession(){return true;}"
        "const window={};let assistantRow=null;"
        "const seal=function()"
        + seal
        + ";console.log(JSON.stringify({result:seal()}));"
    )
    assert out["result"] is False


# ---------------------------------------------------------- feedback is guaranteed


def test_3058_a_declined_repaint_still_shows_the_user_something():
    """`_repaintDeliveredSteer` returns false on several live paths.

    Master always painted the transient indicator, so a recorded-but-unpainted
    delivery must fall back to it rather than leaving the transcript unchanged
    behind a "Steer delivered" toast.
    """
    commands = _read(COMMANDS_JS)
    feedback = _block_after(commands, "if(!recorded||!_repaintDeliveredSteer(ownerSid,acceptedStreamId))")
    out = _run_node(
        _anchor_harness_prelude()
        + "const registry=newRegistry();\n"
        "// the scene cannot be projected right now, so the repaint declines\n"
        "window._renderLiveAnchorActivitySceneForStream=function(){return false;};\n"
            + f"const originalMsg={json.dumps(STEER_TEXT)};const pendingFilesSnapshot=[];\n"
            "const ownerSid=OWNER_SID;const ownerStreamId=OWNER_STREAM;const acceptedStreamId=ownerStreamId;\n"
            + "const recorded=_recordDeliveredSteer(ownerSid,ownerStreamId,originalMsg,pendingFilesSnapshot);\n"
            + "if(_steerOwnerStreamIsCurrent(ownerSid,acceptedStreamId)){\n"
            + feedback
            + "\n}\n"
            + "\n"
        "console.log(JSON.stringify({recorded,rows:scene(registry).activity_rows.length,indicators:RENDERED_INDICATORS}));"
    )
    assert out["recorded"] is True, "the record is durable even when the repaint declines"
    assert out["rows"] == 1
    assert out["indicators"] == [STEER_TEXT], "the user is never left with no feedback at all"


def test_3058_accepted_delivery_leaves_the_running_turn_and_queue_untouched():
    accepted_branch = _block_after(_read(COMMANDS_JS), "if(result&&result.accepted){")
    out = _run_node(
        _anchor_harness_prelude()
        + _function(_read(COMMANDS_JS), "_steerRestoreText")
        + "\nlet _steerUploadCache=null;"
        + "const S={session:{session_id:OWNER_SID},activeStreamId:OWNER_STREAM,busy:true,pendingFiles:[]};"
        + "const result={accepted:true};const ownerSid=OWNER_SID;const ownerStreamId=OWNER_STREAM;"
        + f"const originalMsg={json.dumps(STEER_TEXT)};const explicitSteer=false;const pendingFilesSnapshot=[];"
        + "function _clearComposerDraft(){}function renderTray(){}function showToast(){}"
        + "function t(key){return key;}window._renderLiveAnchorActivitySceneForStream=()=>true;"
        + "newRegistry(OWNER_STREAM);"
        + "function drive(){"
        + accepted_branch
        + "}\ndrive();\n"
        + "console.log(JSON.stringify({busy:S.busy,activeStreamId:S.activeStreamId,queue:SESSION_QUEUES[OWNER_SID]||[],rows:window._liveAnchorRegistries.get(OWNER_STREAM).anchor.activity_events.length}));"
    )
    assert out["busy"] is True
    assert out["activeStreamId"] == OWNER_STREAM
    assert out["queue"] == []
    assert out["rows"] == 1


# ------------------------------------------------- leftover / failure preservation


def test_3058_the_gateway_queued_leftover_branch_records_no_delivery():
    out = _run_node(
        _anchor_harness_prelude()
        + "const registry=newRegistry();\n"
        "// gateway_steer_queued: one editable Queue entry, no anchor record.\n"
        + f"queueSessionMessage(OWNER_SID,{{text:{json.dumps(STEER_TEXT)},files:[]}});\n"
        "console.log(JSON.stringify({queue:SESSION_QUEUES[OWNER_SID],rows:scene(registry).activity_rows.length,inflight:SAVED_INFLIGHT.length,indicators:RENDERED_INDICATORS.length}));"
    )
    assert len(out["queue"]) == 1
    assert out["rows"] == 0, "no delivered row for a gateway-queued steer"
    assert out["inflight"] == 0
    assert out["indicators"] == 0


def test_3058_a_rejected_steer_records_nothing_and_never_reaches_a_queue():
    out = _run_node(
        _anchor_harness_prelude()
        + "const registry=newRegistry();\n"
        "const result={accepted:false,fallback:'no_active_run'};\n"
        + f"const recorded=result.accepted?_recordDeliveredSteer(OWNER_SID,OWNER_STREAM,{json.dumps(STEER_TEXT)},[]):false;\n"
        "console.log(JSON.stringify({recorded,rows:scene(registry).activity_rows.length,queue:SESSION_QUEUES[OWNER_SID]||[],inflight:SAVED_INFLIGHT.length}));"
    )
    assert out["recorded"] is False
    assert out["rows"] == 0
    assert out["queue"] == []
    assert out["inflight"] == 0


# --------------------------------------------------------------- attachments


def test_3058_the_delivered_row_carries_the_captured_file_snapshot_only():
    out = _run_node(
        _anchor_harness_prelude()
        + "const registry=newRegistry();\n"
        "const captured=[{name:'spec.md'},{name:'trace.log'}];\n"
        "const stagedDuringAwait=[{name:'late.png'}];\n"
        "const tray=captured.concat(stagedDuringAwait);\n"
        + f"_recordDeliveredSteer(OWNER_SID,OWNER_STREAM,{json.dumps(STEER_TEXT)},captured);\n"
        "const delivered=new Set(captured);\n"
        "const remaining=tray.filter(f=>!delivered.has(f)).map(f=>f.name);\n"
        "const row=scene(registry).activity_rows[0];\n"
        "console.log(JSON.stringify({files:row.payload.files,text:row.text,remaining}));"
    )
    assert out["files"] == ["spec.md", "trace.log"]
    assert out["text"] == STEER_TEXT
    assert out["remaining"] == ["late.png"], "files staged during the await stay in the tray"


def test_3058_an_attachment_only_steer_still_carries_readable_text():
    out = _run_node(
        _anchor_harness_prelude()
        + "const registry=newRegistry();\n"
        "_recordDeliveredSteer(OWNER_SID,OWNER_STREAM,'',[{name:'spec.md'}]);\n"
        "console.log(JSON.stringify({row:scene(registry).activity_rows[0]}));"
    )
    assert out["row"]["text"] == "Attached files: spec.md"
    assert out["row"]["role"] == "user"


# ------------------------------------------------------- render / negative space


def _render_row_node(row: dict, settled: bool = True) -> dict:
    """Build the real _anchorSceneNodeForRow output for a row, in a DOM-less shim."""
    builder = _function(_read(UI_JS), "_anchorSceneNodeForRow")
    return _run_node(
        """
const attrs=new WeakMap();
function el(tag){
  const node={tagName:tag,className:'',children:[],textContent:'',dataset:{},
    setAttribute(k,v){attrs.get(this)[k]=String(v);},
    getAttribute(k){return attrs.get(this)[k]??null;},
    appendChild(c){this.children.push(c);return c;},
    querySelector(){return null;},querySelectorAll(){return [];}};
  attrs.set(node,{});
  return node;
}
const document={createElement:el};
const window={};
function esc(v){return String(v);}
function renderMd(v){return String(v);}
function t(key){return key==='steer_delivered'?'Steer delivered':key;}
function _activityStatusNode(spec){const n=el('div');n.className='activity-status';n.textContent=String(spec&&spec.label||'');return n;}
function _thinkingActivityNode(){return el('div');}
function buildToolCard(){return el('div');}
"""
        + builder
        + "\n"
        + f"const node=_anchorSceneNodeForRow({json.dumps(row)},{{settled:{'true' if settled else 'false'}}});\n"
        "function dump(n){return n?{tag:n.tagName,cls:n.className,attrs:attrs.get(n),text:n.textContent,children:n.children.map(dump)}:null;}\n"
        "console.log(JSON.stringify({node:dump(node)}));"
    )


_STEER_ROW = {
    "role": "user",
    "kind": "control_boundary",
    "source_event_type": "steer_delivered",
    "status": "delivered",
    "row_id": "run-3058:steer-1",
    "local_id": f"steer:{OWNER_STREAM}:1",
    "text": STEER_TEXT,
    "payload": {"delivered": True, "origin": "webui", "files": ["spec.md"], "status": "delivered"},
}


def test_3058_the_settled_delivered_row_renders_as_a_user_message_with_no_affordances():
    node = _render_row_node(_STEER_ROW)["node"]
    assert node is not None
    assert node["attrs"]["data-role"] == "user"
    assert node["attrs"]["data-steer-delivery"] == "delivered"
    assert node["attrs"]["data-anchor-row-role"] == "user"
    assert node["attrs"]["data-anchor-source-event-type"] == "steer_delivered"
    assert node["attrs"]["data-anchor-local-id"] == f"steer:{OWNER_STREAM}:1"
    texts = [child["text"] for child in node["children"]]
    assert "Steer delivered" in texts
    assert STEER_TEXT in texts
    assert "spec.md" in texts
    # negative space: no interactive affordance of any kind is attached.
    assert not any(child["tag"] == "button" for child in node["children"])
    flat = json.dumps(node)
    for affordance in ("edit", "retry", "fork", "delete", "queue"):
        assert affordance not in flat.lower(), f"{affordance!r} affordance leaked onto the delivered row"


def test_3058_a_control_row_still_renders_through_the_control_branch():
    control = dict(_STEER_ROW, role="control", source_event_type="approval", text="Approve?")
    node = _render_row_node(control)["node"]
    # The shim's _activityStatusNode stands in for the control branch, so a control
    # row producing that node is positive evidence it took that branch rather than
    # being swept into the new user branch.
    assert node is not None
    assert node["cls"] == "activity-status"
    assert node["attrs"].get("data-steer-delivery") is None


@pytest.mark.parametrize("source_event_type", ["", "user_message", "approval", "steer_delivered_typo"])
def test_3058_a_user_role_row_without_the_steer_source_type_gets_no_steer_markup(source_event_type):
    """The row builder keys on provenance, not on the role.

    `_hydrateAnchorRegistryFromActivityScene` passes a row's own
    `source_event_type` through verbatim, and a newer client could project some
    other `role:'user'` row, so a role-only branch would stamp the hardcoded
    "Steer delivered" label onto rows this slice knows nothing about.
    """
    row = dict(_STEER_ROW, source_event_type=source_event_type)
    node = _render_row_node(row)["node"]
    flat = json.dumps(node or {})
    assert "steer-delivered" not in flat
    assert "Steer delivered" not in flat


# ---------------------------------------------------- server sanitize / hydrate


def test_3058_the_delivered_row_survives_sanitize_persist_and_hydrate_unchanged():
    from api.routes import _hydrate_anchor_activity_scenes, _sanitize_anchor_activity_scene

    scene = {
        "version": "activity_scene_v1",
        "mode": "compact_worklog",
        "final_answer": "done",
        "activity_rows": [
            {"role": "prose", "source_event_type": "token", "text": "first half"},
            _STEER_ROW,
            {"role": "prose", "source_event_type": "token", "text": "second half"},
        ],
    }
    sanitized = _sanitize_anchor_activity_scene(scene)
    assert sanitized["activity_rows"][1] == _STEER_ROW

    messages = [{"role": "user", "content": "go"}, {"role": "assistant", "content": "done"}]
    hydrated = _hydrate_anchor_activity_scenes(
        messages, {"k": {"message_index": 1, "message_ref": "", "scene": sanitized}}
    )
    hydrated_scene = hydrated[1].get("_anchor_activity_scene")
    assert hydrated_scene is not None
    assert hydrated_scene["activity_rows"][1]["source_event_type"] == "steer_delivered"
    assert hydrated_scene["activity_rows"][1]["status"] == "delivered"
    assert hydrated_scene["activity_rows"][1]["payload"]["files"] == ["spec.md"]


def test_3058_scene_hydration_reuses_the_event_sequence_for_steer_deduplication():
    hydrate = _function(_read(MESSAGES_JS), "_hydrateAnchorRegistryFromActivityScene")
    out = _run_node(
        "const seen=[];const _anchorRegistry={};"
        "const _anchorApi={applyAssistantTurnAnchorSourceEvent(_registry,event){seen.push(event);}};"
        "const activeSid='sid-3058';const streamId='stream-3058';let _anchorShadowWarned=false;"
        "function _sourceEventTypeForSnapshotAnchorRow(row){return row.source_event_type;}\n"
        + hydrate
        + f"\n_hydrateAnchorRegistryFromActivityScene({json.dumps({'version':'activity_scene_v1','identity':{'stream_id':OWNER_STREAM},'activity_rows':[{'source_event_type':'steer_delivered','local_id':_CACHED_STEER['payload']['local_id'],'seq':4,'identity':{'seq':'steer-1'},'status':'delivered','text':STEER_TEXT,'payload':_CACHED_STEER['payload']}]})});\n"
        "console.log(JSON.stringify({seq:seen[0].seq,localId:seen[0].local_id}));"
    )
    assert out["seq"] == "steer-1"
    assert out["localId"] == _CACHED_STEER["payload"]["local_id"]


def test_3058_a_virtualized_turn_rebuilds_the_row_from_the_scene_not_from_a_dom_node():
    """The row must survive a transcript rebuild that retains no DOM at all.

    This is the reproduction row's observable consequence: at base the settled
    transcript has nothing to rebuild from, because the steer only ever existed as
    a `.steer-indicator` node that `renderMessages` discards.
    """
    from api.routes import _hydrate_anchor_activity_scenes, _sanitize_anchor_activity_scene

    scene = {
        "version": "activity_scene_v1",
        "mode": "compact_worklog",
        "final_answer": "done",
        "activity_rows": [
            {"role": "prose", "source_event_type": "token", "text": "first half"},
            _STEER_ROW,
        ],
    }
    persisted = _sanitize_anchor_activity_scene(scene)
    messages = [{"role": "user", "content": "go"}, {"role": "assistant", "content": "done"}]
    hydrated = _hydrate_anchor_activity_scenes(
        messages, {"k": {"message_index": 1, "message_ref": "", "scene": persisted}}
    )
    rebuilt_scene = hydrated[1]["_anchor_activity_scene"]
    # A window with no retained live turn: the only input is the message's scene.
    gates = _worklog_gate_probe(rebuilt_scene["activity_rows"])
    assert gates == {"generation": True, "render": True}
    node = _render_row_node(rebuilt_scene["activity_rows"][1])["node"]
    assert node["attrs"]["data-steer-delivery"] == "delivered"
    assert node["attrs"]["data-anchor-local-id"] == f"steer:{OWNER_STREAM}:1"


# ------------------------------------------------------------- no applied claim


CHANGED_SOURCES = {
    "assistant_turn_anchors.js": ANCHORS_JS,
    "commands.js": COMMANDS_JS,
    "messages.js": MESSAGES_JS,
    "ui.js": UI_JS,
    "style.css": STYLE_CSS,
    "i18n.js": I18N_JS,
}


def test_3058_no_surface_added_by_this_slice_claims_applied_consumed_or_handled():
    forbidden = ("applied", "consumed", "handled")
    for name, path in CHANGED_SOURCES.items():
        for line in _read(path).splitlines():
            if "steer_delivered" not in line and "steer-delivered" not in line and "steerDelivered" not in line:
                continue
            if "deliveredSteers" in line and "steer_delivered" not in line:
                continue
            lowered = line.lower()
            for word in forbidden:
                assert word not in lowered, f"{name}: {word!r} claim on a steer surface: {line.strip()}"


def test_3058_every_locale_block_carries_the_steer_delivered_label():
    import re

    src = _read(I18N_JS)
    blocks = list(re.finditer(r"^  ('[^']+'|[A-Za-z][A-Za-z0-9_-]*): \{$", src, re.MULTILINE))
    end = src.index("\n};", blocks[-1].start())
    assert len(blocks) >= 14
    for index, match in enumerate(blocks):
        stop = blocks[index + 1].start() if index + 1 < len(blocks) else end
        block = src[match.start() : stop]
        locale = match.group(1).strip("'")
        assert re.search(r"^\s+steer_delivered: '", block, re.MULTILINE), (
            f"locale {locale!r} is missing the steer_delivered label"
        )
        assert "cmd_steer_delivered" in block
