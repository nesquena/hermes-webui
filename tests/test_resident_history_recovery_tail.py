"""Bounded done-tail recovery keeps already-loaded resident history.

Only the done-settlement slice is covered here. These tests execute the real
helpers and listener statements from static/messages.js. The new merge helpers
are optional in the Node harness so the same behavior assertions can run on
unmodified upstream without turning the red control into a harness error.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import textwrap

REPO = pathlib.Path(__file__).resolve().parent.parent
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(src: str, name: str) -> str:
    start = src.find(f"function {name}(")
    assert start >= 0, f"{name} not found in source JavaScript"
    if start >= 6 and src[start - 6:start] == "async ":
        start -= 6
    brace = src.find("{", start)
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        depth += (src[i] == "{") - (src[i] == "}")
        i += 1
    assert depth == 0, f"{name} braces unbalanced"
    return src[start:i]


def _optional_function(src: str, name: str) -> str:
    return _extract_function(src, name) if f"function {name}(" in src else ""


def _optional_load_all() -> list[str]:
    """Load the real consumer on the PR; stay harness-compatible on master."""
    if "async function _ensureAllMessagesLoaded(" not in SESSIONS_JS:
        return []
    fn = _extract_function(SESSIONS_JS, "_ensureAllMessagesLoaded")
    return [
        fn,
        _optional_function(SESSIONS_JS, "_transcriptFreshnessSnapshot"),
        _optional_function(SESSIONS_JS, "_transcriptIsFresh"),
    ]


def _pieces() -> list[str]:
    names = (
        "_messageIdentityKey",
        "_carryForwardEphemeralTurnFields",
        "_finalAnswerIsInTranscript",
        "_snapshotHasUsableFinalAssistantRow",
        "_streamRecoveryControlMessageText",
        "_streamRecoveryControlMessage",
        "_isMarkerOnlyAssistantMessage",
        "_filterRecoveryControlMessages",
        "_adoptDoneSnapshotMessages",
    )
    return [
        *[_extract_function(MESSAGES_JS, name) for name in names],
        _optional_function(MESSAGES_JS, "_residentMergeIdentityKey"),
        _optional_function(MESSAGES_JS, "_mergeResidentPrefix"),
        _optional_function(MESSAGES_JS, "_doneResidentMergeOptions"),
        "const _EPHEMERAL_TURN_FIELDS=['_turnUsage','_turnDuration','_turnTps','_gatewayRouting','_statusCard','_anchor_stream_id','_anchor_activity_scene'];",
        _extract_function(MESSAGES_JS, "_isHistoricalAnchorActivityScene"),
        "function msgContent(m){return (m && m.content)||'';}",
        "function _isPreservedCompressionTaskListMarkerOnlyText(t){return /active task list was preserved/i.test(String(t||''));}",
        *_optional_load_all(),
    ]


def _run_node(case: str) -> dict:
    assert NODE, "node is required for these behavioural regressions"
    script = "\n".join([*_pieces(), textwrap.dedent(case)])
    result = subprocess.run(
        [NODE, "-e", "(async()=>{\n" + script + "\n})().catch(err=>{console.error(err);process.exit(1);});"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


def _sample_messages() -> list[dict]:
    return [
        {"role": "user", "_ts": "1", "content": "old question 1"},
        {"role": "assistant", "_ts": "2", "content": "old answer 1"},
        {"role": "user", "_ts": "3", "content": "old question 2"},
        {"role": "assistant", "_ts": "4", "content": "old answer 2"},
        {"role": "user", "_ts": "5", "content": "new question"},
        {"role": "assistant", "_ts": "6", "content": "The streamed final answer."},
    ]


def test_bounded_tail_retains_prefix_once_and_is_idempotent() -> None:
    js = r'''
        const pre=[{role:'user',_ts:'1',content:'old question 1'},{role:'assistant',_ts:'2',content:'old answer 1'},{role:'user',_ts:'3',content:'old question 2'},{role:'assistant',_ts:'4',content:'old answer 2'},{role:'user',_ts:'5',content:'new question'},{role:'assistant',_ts:'6',content:'The streamed final answer.'}];
        const settled=pre.slice(2);
        const opts={allowResidentPrefixMerge:true};
        const first=_adoptDoneSnapshotMessages(pre,settled,'The streamed final answer.',opts);
        const second=_adoptDoneSnapshotMessages(first.messages,settled,'The streamed final answer.',opts);
        const texts=first.messages.map(m=>m.content);
        const keys=first.messages.map(m=>m.role+'|'+m._ts);
        console.log(JSON.stringify({
          count:first.messages.length,secondCount:second.messages.length,
          noDuplicates:new Set(keys).size===keys.length,
          keepsOld:texts.includes('old question 1')&&texts.includes('old answer 1'),
          keepsIdentity:first.messages[0]===pre[0]&&first.messages[1]===pre[1],
          tailPresent:texts.includes('The streamed final answer.'),
          firstMerged:first.mergedResidentPrefix===true,secondMerged:second.mergedResidentPrefix===true
        }));
    '''
    expected = {
        "count": 6,
        "secondCount": 6,
        "noDuplicates": True,
        "keepsOld": True,
        "keepsIdentity": True,
        "tailPresent": True,
        "firstMerged": True,
        "secondMerged": True,
    }
    assert _run_node(js) == expected


def test_only_contiguous_bounded_overlap_merges_other_cases_server_wins() -> None:
    js = r'''
        const opts={allowResidentPrefixMerge:true};
        const pre=[{role:'user',_ts:'1',content:'one'},{role:'assistant',_ts:'2',content:'two'},{role:'user',_ts:'3',content:'three'},{role:'assistant',_ts:'4',content:'four'}];
        const suffix=[pre[2],pre[3]];
        const unrelated=[{role:'user',_ts:'49',content:'new question'},{role:'assistant',_ts:'50',content:'new turn'}];
        const covered=_adoptDoneSnapshotMessages(pre,suffix,'four',opts);
        const replace=_adoptDoneSnapshotMessages(pre,unrelated,'new turn',opts);
        console.log(JSON.stringify({
          suffixCount:covered.messages.length,
          suffixPrefix:covered.messages[0]===pre[0]&&covered.messages[1]===pre[1],
          unrelatedReplaced:replace.messages.length===2&&replace.messages[0]===unrelated[0]&&replace.messages[1]===unrelated[1]
        }));
    '''
    assert _run_node(js) == {"suffixCount": 4, "suffixPrefix": True, "unrelatedReplaced": True}


def test_unkeyed_ambiguous_and_noncontiguous_matches_fail_closed() -> None:
    js = r'''
        const opts={allowResidentPrefixMerge:true};
        const repeatA={role:'assistant',_ts:'2',content:'repeat'};
        const repeatB={role:'assistant',_ts:'2',content:'repeat'};
        const pre=[{role:'user',_ts:'1',content:'old'},repeatA,{role:'assistant',_ts:'3',content:'unique'},repeatB,{role:'assistant',_ts:'4',content:'final'}];
        const duplicate=_adoptDoneSnapshotMessages(pre,[repeatB,pre[4]],'final',opts);
        const unkeyed=_adoptDoneSnapshotMessages(pre,[{role:'assistant',content:'tail'}],'',opts);
        const noncontiguous=_adoptDoneSnapshotMessages(pre,[repeatA,{role:'assistant',_ts:'9',content:'different'}],'',opts);
        console.log(JSON.stringify({
          duplicateSkipped:duplicate.mergedResidentPrefix===false&&duplicate.messages.length===2,
          unkeyedSkipped:unkeyed.mergedResidentPrefix===false&&unkeyed.messages.length===1,
          noncontiguousSkipped:noncontiguous.mergedResidentPrefix===false&&noncontiguous.messages.length===2
        }));
    '''
    assert _run_node(js) == {"duplicateSkipped": True, "unkeyedSkipped": True, "noncontiguousSkipped": True}


def test_older_identical_answer_cannot_satisfy_current_turn() -> None:
    js = r'''
        const pre=[{role:'user',_ts:'1',content:'old question'},{role:'assistant',_ts:'2',content:'yes'},{role:'user',_ts:'3',content:'new question'}];
        const settled=[pre[2]];
        const r=_adoptDoneSnapshotMessages(pre,settled,'yes',{allowResidentPrefixMerge:true});
        console.log(JSON.stringify({count:r.messages.length,lastRole:r.messages[r.messages.length-1].role,lastText:r.messages[r.messages.length-1].content,texts:r.messages.map(m=>m.content)}));
    '''
    got = _run_node(js)
    assert got == {"count": 4, "lastRole": "assistant", "lastText": "yes", "texts": ["old question", "yes", "new question", "yes"]}


def _listener(
    prev: list[dict],
    settled: dict,
    *,
    pre_generation=None,
    pre_oldest_idx=0,
    streamed_answer=None,
    pre_messages_truncated=False,
    load_all=False,
) -> dict:
    start = MESSAGES_JS.find("let _doneFinalAnswer=")
    end = MESSAGES_JS.find("if(typeof _hydrateTodosFromSession", start)
    assert start >= 0 and end > start, "real done settle statements not found"
    statements = MESSAGES_JS[start:end]
    case = f"""
        const _residentMessages={json.dumps(prev)};
        const _residentSession={{session_id:'A',title:'resident'}};
        if({json.dumps(pre_generation)}!==null) _residentSession.transcript_generation={json.dumps(pre_generation)};
        const S={{session:_residentSession,messages:_residentMessages,activeStreamId:'stream-one'}};
        const d={{session:{json.dumps(settled)},stream_id:'stream-one'}};
        const activeSid='A',completedSid='A';
        const _settledStreamId='stream-one';
        const _pendingTitleUpdates=new Map();
        const _pendingTitleExpectedCurrent=new Map();
        function applySessionTitleUpdate(){{}}
        let _oldestIdx={pre_oldest_idx};
        let _messagesTruncated={json.dumps(pre_messages_truncated)};
        const _streamDisplay=()=>({json.dumps(streamed_answer)});
        {statements}
        const _loadAllApiCalls=[];
        let _loadingOlder=false;
        const _loadingSessionId=null,_messagesGeneration=0;
        const _bumpMessagesGeneration=()=>{{}};
        const _syncToolCallsForLoadedMessages=()=>{{}};
        const _advanceVisibleBaselineForServerKnownRows=()=>{{}};
        const window={{}};
        async function api(path){{_loadAllApiCalls.push(path);return null;}}
        const _result=()=>({{
          sessionId:S.session&&S.session.session_id,
          sessionTitle:S.session&&S.session.title,
          texts:S.messages.map(m=>m.content),
          prefixIdentity:S.messages[0]===_residentMessages[0],
          oldestIdx:_oldestIdx,
          messagesTruncated:_messagesTruncated
        }});
    """
    if load_all:
        case += r'''
        (async()=>{
          const watchdog=setTimeout(()=>{console.error('load-all case stalled');process.exit(1);},5000);
          const loadAllResult=await _ensureAllMessagesLoaded({rejectIfStale:true});
          clearTimeout(watchdog);
          console.log(JSON.stringify({..._result(),loadAllResult,loadAllRequests:_loadAllApiCalls.length}));
        })().catch(err=>{console.error(err);process.exit(1);});
        '''
    else:
        case += "console.log(JSON.stringify(_result()));"
    return _run_node(case)


def test_real_done_listener_keeps_prefix_and_resident_paging_origin() -> None:
    prev = _sample_messages()[:4]
    got = _listener(prev, {"session_id": "A", "messages": prev[2:], "transcript_generation": 0, "_messages_offset": 72, "message_count": 74}, pre_generation=0, pre_oldest_idx=70)
    assert got["texts"] == [m["content"] for m in prev]
    assert got["prefixIdentity"] is True
    assert got["oldestIdx"] == 70
    assert got["messagesTruncated"] is True


def test_merged_window_completeness_tracks_selected_origin_and_real_load_all() -> None:
    prev = _sample_messages()
    for pre_oldest_idx, settled_offset, expected_truncated, expected_requests in (
        (0, 2, False, 0),
        (10, 12, True, 1),
    ):
        got = _listener(
            prev,
            {
                "session_id": "A",
                "messages": prev[2:],
                "transcript_generation": 7,
                "_messages_offset": settled_offset,
                "_messages_truncated": True,
                "message_count": pre_oldest_idx + len(prev),
            },
            pre_generation=7,
            pre_oldest_idx=pre_oldest_idx,
            load_all=True,
        )
        assert got["texts"] == [m["content"] for m in prev]
        assert got["prefixIdentity"] is True
        assert got["oldestIdx"] == pre_oldest_idx
        assert got["messagesTruncated"] is expected_truncated
        assert got["loadAllResult"] is not expected_truncated
        assert got["loadAllRequests"] == expected_requests


def test_empty_origin_zero_retention_real_load_all_short_circuits() -> None:
    prev = _sample_messages()
    got = _listener(
        prev,
        {"session_id": "A", "messages": [], "message_count": len(prev), "transcript_generation": 7},
        pre_generation=7,
        pre_oldest_idx=0,
        load_all=True,
    )
    assert got["texts"] == [m["content"] for m in prev]
    assert got["oldestIdx"] == 0
    assert got["messagesTruncated"] is False
    assert got["loadAllResult"] is True
    assert got["loadAllRequests"] == 0


def test_unmarked_shorter_snapshot_stays_server_wins() -> None:
    prev = _sample_messages()[:4]
    got = _listener(prev, {"session_id": "A", "messages": prev[2:]}, pre_oldest_idx=70)
    assert got["texts"] == [m["content"] for m in prev[2:]]
    assert got["oldestIdx"] == 0


def test_newer_generation_does_not_resurrect_deleted_nonempty_prefix() -> None:
    prev = _sample_messages()[:4]
    got = _listener(prev, {"session_id": "A", "title": "after-edit", "messages": prev[2:], "transcript_generation": 2, "_messages_offset": 2, "message_count": 2}, pre_generation=1, pre_oldest_idx=70)
    assert got["texts"] == [m["content"] for m in prev[2:]]
    assert got["sessionTitle"] == "after-edit"
    assert got["oldestIdx"] == 2


def test_present_but_malformed_window_metadata_fails_closed() -> None:
    prev = _sample_messages()[:4]
    for bad in ("garbage", -2, 2.5, True):
        got = _listener(prev, {"session_id": "A", "messages": prev[2:], "transcript_generation": 0, "_messages_offset": bad}, pre_generation=0, pre_oldest_idx=70)
        assert got["texts"] == [m["content"] for m in prev[2:]], bad
        assert got["oldestIdx"] == 0, bad
    for bad_count in ("garbage", 3.5, "4", True):
        got = _listener(prev, {"session_id": "A", "messages": prev[2:], "transcript_generation": 0, "_messages_offset": 2, "message_count": bad_count}, pre_generation=0, pre_oldest_idx=70)
        assert got["texts"] == [m["content"] for m in prev[2:]], bad_count
        assert got["oldestIdx"] == 2, bad_count
    for bad_count in ("garbage", 3.5, "4", True):
        got = _listener(prev, {"session_id": "A", "messages": prev[2:], "transcript_generation": 0, "message_count": bad_count}, pre_generation=0, pre_oldest_idx=70)
        assert got["texts"] == [m["content"] for m in prev[2:]], bad_count
        assert got["oldestIdx"] == 0, bad_count


def test_offset_without_count_cannot_enable_prefix_merge() -> None:
    prev = _sample_messages()[:4]
    got = _listener(prev, {"session_id": "A", "messages": prev[2:], "transcript_generation": 0, "_messages_offset": 72}, pre_generation=0, pre_oldest_idx=70)
    assert got["texts"] == [m["content"] for m in prev[2:]]
    assert got["oldestIdx"] == 72


def test_unknown_or_mixed_generation_cannot_enable_prefix_merge() -> None:
    prev = _sample_messages()[:4]
    done = {
        "session_id": "A",
        "messages": prev[2:],
        "transcript_generation": 1,
        "_messages_offset": 2,
        "message_count": 4,
    }
    unknown_resident = _listener(prev, done, pre_oldest_idx=0)
    assert unknown_resident["texts"] == [m["content"] for m in prev[2:]]
    assert unknown_resident["oldestIdx"] == 2

    no_done_generation = dict(done)
    no_done_generation.pop("transcript_generation")
    no_done_generation["message_count"] = 4
    mixed = _listener(prev, no_done_generation, pre_generation=0, pre_oldest_idx=0)
    assert mixed["texts"] == [m["content"] for m in prev[2:]]
    assert mixed["oldestIdx"] == 2


def test_authoritative_offset_and_origin_mismatch_disallow_merge() -> None:
    prev = _sample_messages()[:4]
    mismatched = _listener(
        prev,
        {"session_id": "A", "messages": prev[2:], "transcript_generation": 0, "_messages_offset": 2, "message_count": 4},
        pre_generation=0,
        pre_oldest_idx=10,
    )
    assert mismatched["texts"] == [m["content"] for m in prev[2:]]
    assert mismatched["oldestIdx"] == 2

    contradictory_zero = _listener(
        prev,
        {"session_id": "A", "messages": prev[2:], "transcript_generation": 0, "_messages_offset": 0, "_messages_truncated": True, "message_count": 2},
        pre_generation=0,
        pre_oldest_idx=10,
    )
    assert contradictory_zero["texts"] == [m["content"] for m in prev[2:]]
    assert contradictory_zero["oldestIdx"] == 0


def test_merged_window_count_uses_candidate_before_answer_reattachment() -> None:
    pre = _sample_messages()[:3]
    tail = [pre[2], {"role": "assistant", "_ts": "4", "content": "final answer"}]
    options = "{allowResidentPrefixMerge:true,residentMergeWindow:{residentOrigin:0,settledOffset:2,messageCount:4}}"
    got = _run_node(
        f"""
        const pre={json.dumps(pre)};
        const tail={json.dumps(tail)};
        const r=_adoptDoneSnapshotMessages(pre,tail,'final answer',{options});
        console.log(JSON.stringify({{count:r.messages.length,merged:r.mergedResidentPrefix===true,texts:r.messages.map(m=>m.content)}}));
        """
    )
    assert got["merged"] is True
    assert got["count"] == 4
    assert got["texts"] == ["old question 1", "old answer 1", "old question 2", "final answer"]

    replay = _run_node(
        r'''
        const pre=[{role:'user',_ts:'1',content:'old'},{role:'assistant',_ts:'2',content:'prior'},{role:'user',_ts:'3',content:'current'}];
        const opts={allowResidentPrefixMerge:true,residentMergeWindow:{residentOrigin:0,settledOffset:2,messageCount:3}};
        const first=_adoptDoneSnapshotMessages(pre,[pre[2]],'final',opts);
        const second=_adoptDoneSnapshotMessages(first.messages,[pre[2]],'final',opts);
        console.log(JSON.stringify({first:first.messages.length,second:second.messages.length,secondMerged:second.mergedResidentPrefix===true,secondText:second.messages.map(m=>m.content)}));
        '''
    )
    assert replay["first"] == 4
    assert replay["second"] == 4
    assert replay["secondMerged"] is True
    assert replay["secondText"] == ["old", "prior", "current", "final"]


def test_empty_snapshot_keeps_resident_origin_instead_of_derived_end() -> None:
    prev = _sample_messages()
    got = _listener(prev, {"session_id": "A", "messages": [], "message_count": 6, "transcript_generation": 0}, pre_generation=0, pre_oldest_idx=0)
    assert got["texts"] == [m["content"] for m in prev]
    assert got["oldestIdx"] == 0


def test_negative_resident_origin_disallows_merge() -> None:
    pre = _sample_messages()[:4]
    got = _run_node(
        f"""
        const pre={json.dumps(pre)};
        const r=_adoptDoneSnapshotMessages(pre,pre.slice(2),'four',{{allowResidentPrefixMerge:true,residentMergeWindow:{{residentOrigin:-2,settledOffset:0,messageCount:2}}}});
        console.log(JSON.stringify({{merged:r.mergedResidentPrefix===true,count:r.messages.length}}));
        """
    )
    assert got == {"merged": False, "count": 2}


def test_empty_snapshot_with_live_answer_preserves_paging_and_truncation() -> None:
    prev = _sample_messages()[:-1]
    got = _listener(
        prev,
        {"session_id": "A", "messages": [], "message_count": 6},
        pre_generation=0,
        pre_oldest_idx=70,
        streamed_answer="new final answer",
        pre_messages_truncated=True,
    )
    assert got["texts"] == [m["content"] for m in prev] + ["new final answer"]
    assert got["oldestIdx"] == 70
    assert got["messagesTruncated"] is True
