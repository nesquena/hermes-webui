"""Behavioral checks for bounded terminal session settlement."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.streaming as streaming
from tests.js_source_extract import extract_function


STREAMING_SOURCE = Path("api/streaming.py").read_text(encoding="utf-8")
GATEWAY_SOURCE = Path("api/gateway_chat.py").read_text(encoding="utf-8")
MESSAGES_SOURCE = Path("static/messages.js").read_text(encoding="utf-8")


class _Session(SimpleNamespace):
    def compact(self):
        return {"session_id": self.session_id, "message_count": 1, "title": "stale"}


def _node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required")
    result = subprocess.run([node, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _arrow(source, name):
    start = source.index(f"const {name}=")
    brace = source.index("{", source.index("=", start))
    depth = 1
    end = brace + 1
    while depth:
        depth += source[end] == "{"
        depth -= source[end] == "}"
        end += 1
    return source[source.index("=", start) + 1:end]


def test_terminal_tail_is_raw_bounded_and_authoritative():
    messages = [{"role": "user", "content": f"row-{i}"} for i in range(6000)]
    todo = {"todos": [{"id": "full-only", "status": "in_progress"}]}
    messages.insert(0, {"role": "tool", "content": json.dumps(todo)})
    messages += [
        {"role": "assistant", "content": "hidden", "recovery_control": True},
        {"role": "tool", "content": "{}"},
    ]
    messages += [{"role": "assistant", "content": f"tail-{i}"} for i in range(8)]
    messages.append({"role": "assistant", "content": "settled"})
    payload = streaming._redacted_terminal_session_payload(
        _Session(session_id="tail", messages=messages)
    )
    assert len(payload["messages"]) <= 300
    assert payload["messages"] == messages[payload["_messages_offset"]:]
    assert payload["message_count"] == len(messages)
    assert payload["_messages_truncated"] is True
    assert payload["_tool_calls_truncated"] is False
    assert any(message.get("recovery_control") for message in payload["messages"])
    actual = [m for m in payload["messages"] if streaming.visible_messages_for_anchor([m], auto_compression=True)]
    expected = [m for m in messages if streaming.visible_messages_for_anchor([m], auto_compression=True)][-30:]
    assert actual == expected
    assert payload["todo_state"]["todos"] == todo["todos"]
    assert payload["tool_calls"] == []


def test_terminal_anchor_uses_raw_indexes_for_repeated_objects():
    repeated = {"role": "user", "content": "same"}
    messages = [repeated] * 31 + [{"role": "assistant", "content": "done"}]
    payload = streaming._redacted_terminal_session_payload(_Session(session_id="repeat", messages=messages))
    assert payload["_messages_offset"] == 2
    assert payload["messages"] == messages[2:]


def test_terminal_tool_calls_are_projected_and_bounded():
    messages = [{"role": "user", "content": str(i)} for i in range(1000)]
    calls = [{"id": f"call-{i}", "assistant_msg_idx": 970 + i % 30} for i in range(301)]
    calls += [{"id": "bad", "assistant_msg_idx": "970"}, {"id": "legacy", "assistant_msg_idx": -1}]
    payload = streaming._redacted_terminal_session_payload(
        _Session(session_id="calls", messages=messages, tool_calls=calls)
    )
    assert len(payload["tool_calls"]) == 300
    assert payload["_tool_calls_truncated"] is True
    assert payload["tool_calls"][0]["id"] == "call-2"
    assert payload["tool_calls"][-1]["id"] == "legacy"
    assert all(call["assistant_msg_idx"] == -1 or 0 <= call["assistant_msg_idx"] < 30 for call in payload["tool_calls"])
    filtered_only = streaming._redacted_terminal_session_payload(
        _Session(session_id="calls", messages=messages, tool_calls=calls[:300] + [{"assistant_msg_idx": "970"}])
    )
    assert filtered_only["_tool_calls_truncated"] is False


def test_truncated_terminal_tool_calls_reconcile_with_expanded_browser_state():
    messages = [
        {"role": "user", "content": "run tools"},
        {"role": "assistant", "content": "working"},
        {"role": "assistant", "content": "done"},
    ]
    calls = [
        {
            "id": f"call-{i}",
            "name": "terminal",
            "snippet": f"current-{i}",
            "assistant_msg_idx": 1,
        }
        for i in range(301)
    ]
    payload = streaming._redacted_terminal_session_payload(
        _Session(session_id="calls", messages=messages, tool_calls=calls)
    )
    assert [call["id"] for call in payload["tool_calls"]] == [f"call-{i}" for i in range(1, 301)]

    helper = extract_function(MESSAGES_SOURCE, "_applyEmbeddedTerminalSession")
    incoming = json.loads(json.dumps(payload))
    incoming["_tool_calls_truncated"] = True
    incoming["tool_calls"][0]["snippet"] = "incoming-wins"
    expanded_calls = json.loads(json.dumps(calls))
    expanded_calls[0]["snippet"] = "expanded-prefix"
    script = f"""
const assert=require('node:assert/strict');
const apply=eval('(' + {json.dumps(helper)} + ')');
const incoming={json.dumps(incoming)};
const messages={json.dumps(messages)};
const current={{session_id:'calls',message_count:3,messages,tool_calls:{json.dumps(expanded_calls)}}};
const merged=apply(incoming,current,messages,0);
assert.ok(merged);
assert.equal(merged.tool_calls.length,301);
assert.equal(merged.tool_calls.filter(c=>c.id==='call-0').length,1);
assert.equal(merged.tool_calls.find(c=>c.id==='call-0').snippet,'expanded-prefix');
assert.equal(merged.tool_calls.filter(c=>c.id==='call-1').length,1);
assert.equal(merged.tool_calls.find(c=>c.id==='call-1').snippet,'incoming-wins');
const ordinary=apply(
  {{session_id:'calls',_messages_offset:2,message_count:4,messages:[{{id:'remote-2'}},{{id:'remote-3'}}],tool_calls:[],_tool_calls_truncated:false}},
  {{session_id:'calls',message_count:4,messages:[{{id:'local-0'}},{{id:'local-1'}},{{id:'local-2'}},{{id:'local-3'}}],tool_calls:[{{id:'before',assistant_msg_idx:0}},{{id:'overlap',assistant_msg_idx:2}}]}},
  [{{id:'local-0'}},{{id:'local-1'}},{{id:'local-2'}},{{id:'local-3'}}],0,
);
assert.deepEqual(ordinary.tool_calls.map(c=>c.id),['before']);
const unkeyed=apply(
  {{session_id:'calls',_messages_offset:0,message_count:3,messages,tool_calls:[{{name:'terminal',assistant_msg_idx:1,snippet:'incoming'}}],_tool_calls_truncated:true}},
  {{session_id:'calls',message_count:3,messages,tool_calls:[{{name:'terminal',assistant_msg_idx:1,snippet:'current'}}]}},
  messages,0,
);
assert.deepEqual(unkeyed.tool_calls.map(c=>c.snippet),['incoming']);
const failClosed=apply(
  {{session_id:'calls',_messages_offset:0,message_count:3,messages,tool_calls:[],_tool_calls_truncated:true}},
  {{session_id:'calls',message_count:3,messages,tool_calls:[{{assistant_msg_idx:1,snippet:'nameless'}},{{name:'terminal',assistant_msg_idx:-1}}]}},
  messages,0,
);
assert.deepEqual(failClosed.tool_calls,[]);
assert.equal(apply(
  {{session_id:'calls',_messages_offset:0,message_count:3,messages,tool_calls:[],_tool_calls_truncated:'yes'}},
  {{session_id:'calls',message_count:3,messages}},messages,0,
),null);
console.log('ok');
"""
    assert _node(script) == "ok"


def test_truncated_unkeyed_tool_calls_use_occurrence_counts():
    helper = extract_function(MESSAGES_SOURCE, "_applyEmbeddedTerminalSession")
    script = f"""
const assert=require('node:assert/strict');
const apply=eval('(' + {json.dumps(helper)} + ')');
const messages=[{{id:'user'}},{{id:'assistant'}},{{id:'done'}}];
const incoming={{session_id:'calls',_messages_offset:0,message_count:3,messages,tool_calls:[
  {{name:'terminal',assistant_msg_idx:1,snippet:'incoming-2'}},
  {{name:'terminal',assistant_msg_idx:1,snippet:'incoming-3'}},
],_tool_calls_truncated:true}};
const current={{session_id:'calls',message_count:3,messages,tool_calls:[
  {{name:'terminal',assistant_msg_idx:1,snippet:'current-0'}},
  {{name:'terminal',assistant_msg_idx:1,snippet:'current-1'}},
  {{name:'terminal',assistant_msg_idx:1,snippet:'current-2'}},
  {{name:'terminal',assistant_msg_idx:1,snippet:'current-3'}},
]}};
const merged=apply(incoming,current,messages,0);
assert.ok(merged);
assert.deepEqual(merged.tool_calls.map(c=>c.snippet),['current-0','current-1','incoming-2','incoming-3']);
assert.equal(merged.tool_calls.filter(c=>c.name==='terminal'&&c.assistant_msg_idx===1).length,4);
assert.equal(merged.tool_calls.filter(c=>c.snippet==='incoming-2').length,1);
assert.equal(merged.tool_calls.filter(c=>c.snippet==='incoming-3').length,1);
const mixed=apply(
  {{...incoming,tool_calls:[{{id:'identified',name:'terminal',assistant_msg_idx:1,snippet:'incoming-id'}},incoming.tool_calls[1]]}},
  current,messages,0,
);
assert.deepEqual(mixed.tool_calls.map(c=>c.snippet),['current-0','current-1','current-2','incoming-id','incoming-3']);
console.log('ok');
"""
    assert _node(script) == "ok"


def test_limited_tool_output_uses_the_existing_character_bound():
    from api.session_ops import _tool_message_for_limited_payload

    message = {"role": "tool", "content": "界🙂" * 3000}
    bounded = _tool_message_for_limited_payload(message)
    assert bounded["_content_truncated"] is True
    assert bounded["content"].startswith(message["content"][:4096])
    assert len(bounded["content"]) < len(message["content"])
    assert json.loads(json.dumps(bounded, ensure_ascii=False))["content"] == bounded["content"]
    assert bounded["_content_original_chars"] == len(message["content"])


def test_terminal_helper_raises_and_producers_do_not_emit_null_done():
    class Broken(_Session):
        def compact(self):
            raise RuntimeError("broken compact")

    with pytest.raises(RuntimeError, match="broken compact"):
        streaming._redacted_terminal_session_payload(Broken(session_id="broken", messages=[]))
    start = STREAMING_SOURCE.index("def _redacted_terminal_session_payload")
    end = STREAMING_SOURCE.index("\ndef _best_effort_terminal_session_payload", start)
    assert "except Exception" not in STREAMING_SOURCE[start:end]
    best_effort = STREAMING_SOURCE[end:]
    assert "except Exception" in best_effort[:best_effort.index("\ndef _compact_for_echo_compare")]
    assert "_best_effort_terminal_session_payload(s)" in STREAMING_SOURCE
    assert "_redacted_session_payload_with_full_messages" not in STREAMING_SOURCE
    assert all("session=" not in line for line in GATEWAY_SOURCE.splitlines() if 'put_gateway_event("cancel"' in line)


def test_coordinate_merge_preserves_widths_and_fails_closed():
    helper = extract_function(MESSAGES_SOURCE, "_applyEmbeddedTerminalSession")
    apperror_start = MESSAGES_SOURCE.index("source.addEventListener('apperror'")
    path_start = MESSAGES_SOURCE.index(
        "const currentSid=S.session&&S.session.session_id;", apperror_start
    )
    path_end = MESSAGES_SOURCE.index("if(eventMatchesCurrent){", path_start)
    apperror_session_path = MESSAGES_SOURCE[path_start:path_end]
    script = f"""
const assert=require('node:assert/strict');
const apply=eval('(' + {json.dumps(helper)} + ')');
const _applyEmbeddedTerminalSession=apply;
for(const size of [30,90,500,6868]){{
  const local=Array.from({{length:size}},(_,i)=>({{id:'local-'+i}}));
  const result=apply({{session_id:'same',message_count:999,messages:[{{id:'remote'}}]}},{{session_id:'same',messages:local}},local,0);
  assert.strictEqual(result,null);
}}
const local=[{{id:'user'}}];
const appended=apply({{session_id:'same',_messages_offset:0,message_count:2,messages:[{{id:'user-persisted'}},{{id:'final'}}]}},{{session_id:'same',messages:local}},local,0);
assert.deepEqual(appended.messages.map(m=>m.id),['user-persisted','final']);
assert.strictEqual(apply({{session_id:'same',_messages_offset:0,message_count:1,_messages_truncated:true,messages:[{{id:'bad-flag'}}]}},{{session_id:'same',messages:local}},local,0),null);
const sameWindow=Array.from({{length:30}},(_,i)=>({{id:'local-'+(971+i)}}));
const sameIncoming=Array.from({{length:30}},(_,i)=>({{id:'remote-'+(971+i)}}));
const stale=apply({{session_id:'same',_messages_offset:971,message_count:1000,_messages_truncated:true,messages:sameIncoming.slice(0,29)}},{{session_id:'same',message_count:1001,messages:sameWindow}},sameWindow,971);
  assert.strictEqual(stale,null);
  const union=apply({{session_id:'same',_messages_offset:971,message_count:1001,messages:sameIncoming}},{{session_id:'same',message_count:1001,messages:sameWindow}},sameWindow,971);
  assert.equal(union.messages.length,30); assert.strictEqual(union.messages[0],sameIncoming[0]);
const localOnlyMessages=[{{id:'persisted-0'}},{{id:'persisted-1'}},{{id:'persisted-2'}},{{id:'local-only'}}];
const settled=apply({{session_id:'same',_messages_offset:1,message_count:3,_messages_truncated:true,messages:[{{id:'persisted-1'}},{{role:'assistant',content:'settled final answer'}}]}},{{session_id:'same',message_count:3,messages:localOnlyMessages}},localOnlyMessages,0);
  assert.ok(settled);
  assert.deepEqual(settled.messages,[{{id:'persisted-1'}},{{role:'assistant',content:'settled final answer'}}]);
  assert.strictEqual(settled._messages_offset,1); assert.strictEqual(settled._messages_truncated,true);
const currentCalls=[{{assistant_msg_idx:1,duration:3,snippet:'retained'}},{{id:'same',assistant_msg_idx:0,duration:3}},{{id:'legacy',assistant_msg_idx:-1}}];
  const incomingCalls=[{{id:'same',assistant_msg_idx:0,snippet:'settled'}},{{id:'new',assistant_msg_idx:0}},{{id:'legacy-new',assistant_msg_idx:-1}},{{id:'bad',assistant_msg_idx:'1'}}];
  const withCalls=apply({{session_id:'same',_messages_offset:92,message_count:94,messages:[{{id:'auth'}},{{id:'cancel'}}],tool_calls:incomingCalls}},{{session_id:'same',message_count:93,messages:[{{id:'old'}},{{id:'hidden'}},{{id:'tail'}}],tool_calls:currentCalls}},[{{id:'old'}},{{id:'hidden'}},{{id:'tail'}}],90);
  assert.deepEqual(withCalls.tool_calls.map(c=>[c.id??null,c.assistant_msg_idx,c.duration,c.snippet]),[[null,1,3,'retained'],['same',2,undefined,'settled'],['new',2,undefined,undefined],['legacy-new',-1,undefined,undefined]]);
const omittedCalls=apply({{session_id:'same',_messages_offset:92,message_count:94,messages:[{{id:'auth'}},{{id:'cancel'}}]}},{{session_id:'same',message_count:93,messages:[{{id:'old'}},{{id:'hidden'}},{{id:'tail'}}],tool_calls:currentCalls}},[{{id:'old'}},{{id:'hidden'}},{{id:'tail'}}],90);
  assert.deepEqual(omittedCalls.tool_calls.map(c=>[c.id??null,c.assistant_msg_idx]),[[null,1],['same',0],['legacy',-1]]);
const hidden=[{{id:'old'}},{{id:'hidden',recovery_control:true}},{{id:'tail'}}];
const hiddenUnion=apply({{session_id:'same',_messages_offset:92,message_count:94,messages:[{{id:'auth'}},{{id:'cancel'}}]}},{{session_id:'same',messages:hidden}},hidden,90);
assert.deepEqual(hiddenUnion.messages.map(m=>m.id),['old','hidden','auth','cancel']);
const disjoint=apply({{session_id:'same',_messages_offset:1002,message_count:1003,_messages_truncated:true,messages:[{{role:'assistant',content:'settled final answer'}}],tool_calls:[]}},{{session_id:'same',messages:sameWindow}},sameWindow,910);
  assert.deepEqual(disjoint.messages,[{{role:'assistant',content:'settled final answer'}}]);
  assert.strictEqual(disjoint._messages_offset,1002);
  assert.strictEqual(disjoint._messages_truncated,true);
  assert.deepEqual(disjoint.tool_calls,[]);
const gap=apply({{session_id:'same',_messages_offset:93,message_count:94,messages:[{{id:'gap'}}]}},{{session_id:'same',messages:[{{id:'old'}},{{id:'tail'}}]}},[{{id:'old'}},{{id:'tail'}}],90);
  assert.strictEqual(gap,null);
const sparse=[{{id:'raw-90'}},{{id:'raw-92'}}];
const sparseResult=apply({{session_id:'same',message_count:94,_messages_offset:92,messages:[{{id:'raw-92'}},{{id:'raw-93'}}]}},{{session_id:'same',message_count:94,messages:sparse}},sparse,90);
  assert.strictEqual(sparseResult,null);
assert.strictEqual(apply({{messages:[{{id:'bad'}}]}},null,local,0),null);
const prefix=Array.from({{length:6}},(_,i)=>({{id:'p'+i}}));
const current={{session_id:'before',message_count:6,messages:prefix,tool_calls:[
  {{id:'keep',assistant_msg_idx:3}},{{id:'dup',assistant_msg_idx:4,meta:'old'}}
]}};
const continuation={{session_id:'after',parent_session_id:'before',message_count:8,
  _messages_offset:5,_messages_truncated:true,
  messages:[{{id:'p5'}},{{id:'tail'}},{{id:'done'}}],tool_calls:[
    {{id:'dup',assistant_msg_idx:0,meta:'new'}},{{id:'new',assistant_msg_idx:1}}
]}};
const merged=apply(continuation,current,prefix,0);
assert.deepEqual(merged.messages.map(m=>m.id),['p0','p1','p2','p3','p4','p5','tail','done']);
assert.equal(merged.session_id,'after'); assert.equal(merged.parent_session_id,'before');
assert.equal(merged._messages_offset,0); assert.equal(merged.message_count,8);
assert.equal(merged._messages_truncated,false);
assert.deepEqual(merged.tool_calls.map(c=>[c.id,c.assistant_msg_idx,c.meta]),[
  ['keep',3,undefined],['dup',5,'new'],['new',6,undefined]
]);
for(const parent of ['other','']){{
  const raw=apply({{...continuation,parent_session_id:parent}},current,prefix,0);
  assert.equal(raw.messages.length,3); assert.deepEqual(raw.messages,continuation.messages);
}}
const missingParent={{...continuation}}; delete missingParent.parent_session_id;
assert.equal(apply(missingParent,current,prefix,0).messages.length,3);
const S={{session:{{session_id:'before',message_count:6}},messages:prefix}};
const activeSid='before',_oldestIdx=0;
function applyAppError(d){{
  {apperror_session_path}
  return {{eventMatchesCurrent,_terminalSession}};
}}
const apperror=applyAppError({{old_session_id:'before',new_session_id:'after',session:continuation}});
assert.equal(apperror.eventMatchesCurrent,true);
assert.deepEqual(apperror._terminalSession.messages.map(m=>m.id),merged.messages.map(m=>m.id));
assert.equal(apperror._terminalSession.session_id,'after');
assert.equal(apperror._terminalSession.parent_session_id,'before');
console.log('ok');
"""
    assert _node(script) == "ok"


def test_done_tool_calls_presence_distinguishes_authoritative_empty_from_absent():
    start = MESSAGES_SOURCE.index("const hasMessageToolMetadata=S.messages.some")
    end = MESSAGES_SOURCE.index("if(typeof renderSessionArtifacts", start)
    settlement = MESSAGES_SOURCE[start:end]
    script = f"""
const assert=require('node:assert/strict');
const _mergeSettledToolCallsWithLiveMetadata=rawCalls=>(rawCalls||[]).map(call=>({{...call,done:true}}));
function settle(completedSession, provided){{
  const S={{messages:[{{role:'assistant',content:'answer'}}],toolCalls:[{{id:'live'}}]}};
  const _terminalToolCallsProvided=provided;
  {settlement}
  return S.toolCalls;
}}
assert.deepEqual(settle({{tool_calls:[]}},true),[]);
assert.deepEqual(settle({{}},false),[{{id:'live',done:true}}]);
console.log('ok');
"""
    assert _node(script) == "ok"


def test_cancel_full_get_and_sid_validation_are_behavioral():
    cancel = _arrow(MESSAGES_SOURCE, "_applyCancelSessionPayload")
    merge = extract_function(MESSAGES_SOURCE, "_applyEmbeddedTerminalSession")
    script = f"""
const assert=require('node:assert/strict');
const _applyEmbeddedTerminalSession=eval('(' + {json.dumps(merge)} + ')');
const apply=eval('(' + {json.dumps(cancel)} + ')');
const activeSid='same'; let _oldestIdx=90,_messagesTruncated=true;
const local=Array.from({{length:90}},(_,i)=>({{id:'local-'+i}}));
const S={{session:{{session_id:activeSid}},activeStreamId:'stream',messages:local}};
const _carryForwardEphemeralTurnFields=(a,b)=>b,_attachProjectedAnchorSceneToLastAssistant=()=>{{}},_hydrateTodosFromSession=()=>{{}};
const _isMessagePaneNearBottom=()=>true,_isMessageReaderUnpinned=()=>false,_messageUserUnpinned=false;
const clearLiveToolCards=()=>{{}},assistantText='',removeThinking=()=>{{}},_markSessionViewed=()=>{{}},renderMessages=()=>{{}},scrollToBottom=()=>{{}};
const _setActiveSessionUrl=()=>{{}},localStorage={{setItem(){{}}}};
assert.equal(apply({{session_id:activeSid,_messages_offset:1002,message_count:1003,messages:[{{id:'gap'}}]}}),false);
assert.equal(apply({{messages:[{{id:'missing'}}]}}),false);
assert.equal(apply({{session_id:activeSid,_messages_offset:0,message_count:91,messages:[...local,{{id:'cancel'}}]}},true),true);
assert.equal(_oldestIdx,0); assert.equal(_messagesTruncated,false); assert.equal(S.messages[90].id,'cancel');
assert.equal(S.activeStreamId,null);
assert.equal(apply({{session_id:'rotated',messages:[{{id:'rotated'}}]}}),false);
console.log('ok');
"""
    assert _node(script) == "ok"


def test_full_restore_resets_cursor_and_ephemeral_fields_survive():
    restore = extract_function(MESSAGES_SOURCE, "_restoreSettledSession", prefix="async function")
    carry = extract_function(MESSAGES_SOURCE, "_carryForwardEphemeralTurnFields")
    identity = extract_function(MESSAGES_SOURCE, "_messageIdentityKey")
    script = f"""
const assert=require('node:assert/strict');
(async()=>{{
const activeSid='same',streamId='stream'; let _oldestIdx=90,_messagesTruncated=true,_streamFinalized=false;
const full=Array.from({{length:91}},(_,i)=>({{id:'full-'+i}})); full[89]={{id:'hidden',recovery_control:true}};
const api=async()=>({{session:{{session_id:activeSid,message_count:91,_messages_offset:0,_messages_truncated:false,messages:full,tool_calls:[]}}}});
const S={{session:{{session_id:activeSid,active_stream_id:streamId}},activeStreamId:streamId,messages:[{{id:'window'}}],toolCalls:[]}};
const source={{close(){{}}}},localStorage={{setItem(){{}}}};
const _isActiveSession=()=>true,_isSessionCurrentPane=()=>true,_isSessionActivelyViewed=()=>true,_closeSource=s=>s.close();
const _messageIdentityKey=eval('(' + {json.dumps(identity)} + ')');
const _carryForwardEphemeralTurnFields=(a,b)=>b,_isTerminalStreamErrorMarkerMessage=()=>false;
const _attachProjectedAnchorSceneToLastAssistant=()=>{{}},_hydrateTodosFromSession=()=>{{}},_replaceMarkerOnlyAssistantWithStreamError=()=>false;
const _mergeSettledToolCallsWithLiveMetadata=x=>x,_clearAnchorProseIncrementalNode=()=>{{}},_cancelThrottledSnapshotTimer=()=>{{}};
const _cancelAnimationFramePendingStreamRender=()=>{{}},_streamFadeCleanupReduceMotionListener=()=>{{}},_smdEndParser=()=>{{}},finalizeThinkingCard=()=>{{}};
const _clearOwnerInflightState=()=>{{}},_flushReasoningToAnchor=()=>{{}},_scheduleAnchorRegistryCleanup=()=>{{}},_clearApprovalForOwner=()=>{{}},_clearClarifyForOwner=()=>{{}};
const clearLiveToolCards=()=>{{}},removeThinking=()=>{{}},_markSessionCompletionUnread=()=>{{}},_markSessionViewed=()=>{{}},syncTopbar=()=>{{}},renderMessages=()=>{{}},renderSessionList=()=>{{}},_setActivePaneIdleIfOwner=()=>{{}},_setActiveSessionUrl=()=>{{}};
let _queueDrainSid=null,_persistTimer=null; const assistantText='';
const restore=eval('(' + {json.dumps(restore)} + ')');
assert.equal(await restore(source),true); assert.equal(_oldestIdx,0); assert.equal(_messagesTruncated,false);
assert.equal(S.messages.length,91); assert.equal(S.messages[89].id,'hidden');
const fields=['_turnUsage','_turnDuration','_turnTps','_gatewayRouting','_statusCard','_anchor_stream_id','_anchor_activity_scene'];
const _EPHEMERAL_TURN_FIELDS=fields,_isHistoricalAnchorActivityScene=()=>false;
const carry=eval('(' + {json.dumps(carry)} + ')');
const before={{role:'assistant',content:'answer',_ts:1,_turnUsage:{{x:1}},_turnDuration:2,_turnTps:3,_gatewayRouting:'gw',_statusCard:{{ok:true}},_anchor_stream_id:'s',_anchor_activity_scene:{{id:'a'}}}};
const after={{role:'assistant',content:'answer',_ts:1}}; carry([before],[after]);
for(const field of fields) assert.deepEqual(after[field],before[field]);
console.log('ok'); }})().catch(e=>{{console.error(e);process.exit(1)}});
"""
    assert _node(script) == "ok"


def test_terminal_wiring_reuses_existing_lifecycle_and_coordinate_paths():
    helper = extract_function(MESSAGES_SOURCE, "_applyEmbeddedTerminalSession").lower()
    assert all(word not in helper for word in ("fetch", "await", "settimeout", "setinterval", "owner"))
    assert "_stream_generations" not in MESSAGES_SOURCE.lower()
    assert "_streamgenerationiscurrent" not in MESSAGES_SOURCE.lower()
    assert "allowfinalized" not in MESSAGES_SOURCE.lower()
    assert "applysession:" not in MESSAGES_SOURCE.lower()
    done_start = MESSAGES_SOURCE.index("source.addEventListener('done'")
    done_end = MESSAGES_SOURCE.index("source.addEventListener('stream_end'", done_start)
    done = MESSAGES_SOURCE[done_start:done_end]
    assert ": {session_id:activeSid}" in done
    assert "if(!completedSession){" not in done
    assert "S.messages=_filterRecoveryControlMessages" not in done
    assert "liveDisplayText:typeof _streamDisplay==='function'?_streamDisplay():assistantText" in done
    error_start = MESSAGES_SOURCE.index("source.addEventListener('apperror'")
    error_end = MESSAGES_SOURCE.index("source.addEventListener('warning'", error_start)
    error = MESSAGES_SOURCE[error_start:error_end]
    assert "_terminalRecoveryPromise" not in error
    assert "S.messages=_filterRecoveryControlMessages" not in error
    assert "if(isRecoveryControlMessage){" in error
    assert "S.session.message_count != null" in error
    cancel_start = MESSAGES_SOURCE.index("source.addEventListener('cancel'")
    cancel_end = MESSAGES_SOURCE.index("for(const _runJournalEventName", cancel_start)
    cancel = MESSAGES_SOURCE[cancel_start:cancel_end]
    assert "payloadSid!==activeSessionSid" in cancel
    assert "fullSnapshot=false" in cancel
    assert "const data=await api(" in cancel
    assert "_applyCancelSessionPayload(data.session,true)" in cancel
    assert "const status=await _restoreSettledSession(source,{" not in cancel
    assert "parent_session_id" not in cancel
    restore = extract_function(MESSAGES_SOURCE, "_restoreSettledSession", prefix="async function")
    assert "_filterRecoveryControlMessages" not in restore
    assert "_oldestIdx=_restoredOffset" in restore
    assert "allowFinalized" not in restore
    assert "restoreOwnerIsCurrent" not in restore
