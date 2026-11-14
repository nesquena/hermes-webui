"""#8989 — `done` must keep a bounded tail window instead of the full transcript.

What happens (verified on origin/master @ 356872452):

* opening a session loads a bounded tail (`_INITIAL_MSG_LIMIT = 30`,
  `msg_limit` -> `_messages_truncated: true`, `_messages_offset: N`);
* when the turn settles, the server embeds the FULL transcript in the `done`
  payload (`api/streaming.py::_session_payload_with_full_messages`);
* the client's `done` listener adopts it wholesale, so the full payload has no
  `_messages_truncated` / `_messages_offset` and the session silently becomes
  "fully loaded" until a reload. Measured on a ~1,100-message session:
  `S.messages` 53 -> 1,064, `#messages.scrollHeight` 3,744px -> 110,616px,
  `renderMessages()` 156ms vs 22ms on a 1,714-message copy.

The fix keeps the resident window bounded: the `done` listener slices the settled
transcript to the same width a same-session reload asks for
(`_messageReloadLimitForSession()`, and the same shape as #7925's widening
re-fetch `max(heldLen, message_count - _oldestIdx, 30)`), and carries the slice's
absolute offset / truncation flag so Load-earlier paging keeps working.

These tests execute the REAL done-listener statements plus the real
`_loadOlderMessages()` from static/, so the RED control is the production
behaviour on unmodified upstream: the fix helper is optional in the harness
(`typeof` guard in the listener), exactly like `_doneResidentMergeOptions`.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required to execute the real done listener")

STREAMED_ANSWER = "The streamed final answer for the new turn."


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


def _done_listener_statements() -> str:
    """The real `done` settle statements, from the answer capture to the filter."""
    start = MESSAGES_JS.find("let _doneFinalAnswer=")
    end = MESSAGES_JS.find("if(typeof _hydrateTodosFromSession", start)
    assert start >= 0 and end > start, "real done settle statements not found"
    return MESSAGES_JS[start:end]


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
    pieces = [
        *[_extract_function(MESSAGES_JS, name) for name in names],
        _optional_function(MESSAGES_JS, "_residentMergeIdentityKey"),
        _optional_function(MESSAGES_JS, "_mergeResidentPrefix"),
        _optional_function(MESSAGES_JS, "_doneResidentMergeOptions"),
        # The fix under test. Absent on upstream -> the listener's `typeof`
        # guard keeps the legacy adoption, which is the RED control.
        _optional_function(MESSAGES_JS, "_boundedDoneTailWindow"),
        _extract_function(SESSIONS_JS, "_loadOlderMessages"),
        _extract_function(SESSIONS_JS, "_messageReloadLimitForSession"),
        "const _EPHEMERAL_TURN_FIELDS=['_turnUsage','_turnDuration','_turnTps','_gatewayRouting','_statusCard','_anchor_stream_id','_anchor_activity_scene'];",
        _extract_function(MESSAGES_JS, "_isHistoricalAnchorActivityScene"),
        "function msgContent(m){return (m && m.content)||'';}",
        "function _isPreservedCompressionTaskListMarkerOnlyText(t){return /active task list was preserved/i.test(String(t||''));}",
    ]
    return [p for p in pieces if p]


def _run_node(case: str) -> dict:
    script = "\n".join([*_pieces(), textwrap.dedent(case)])
    result = subprocess.run(
        [NODE, "-e", "(async()=>{\n" + script + "\n})().catch(err=>{console.error(err);process.exit(1);});"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


def _transcript(total: int, start_ts: int = 1) -> list[dict]:
    """A long, identity-stable transcript: alternating user/assistant rows."""
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"transcript row {i}",
            "_ts": str(start_ts + i),
        }
        for i in range(total)
    ]


def _new_turn() -> list[dict]:
    """The turn that just settled: user -> tool call -> tool result -> answer."""
    return [
        {"role": "user", "content": "the new question", "_ts": "9001"},
        {
            "role": "assistant",
            "content": "",
            "_ts": "9002",
            "tool_calls": [{"id": "call-1", "function": {"name": "grep"}}],
        },
        {"role": "tool", "content": "grep output", "_ts": "9003", "tool_call_id": "call-1"},
        {"role": "assistant", "content": STREAMED_ANSWER, "_ts": "9004"},
    ]


_RUNTIME_STUBS = [
    "const _INITIAL_MSG_LIMIT=30;",
    "const _MSG_LIMIT_MAX=500;",
    "let _msgLimitMax=_MSG_LIMIT_MAX;",
    "const MESSAGE_RENDER_WINDOW_DEFAULT=50;",
    "let _messageRenderWindowSize=50;",
    "let _loadingOlder=false;",
    "let _loadingSessionId=null;",
    "let _messagesGeneration=0;",
    "let _scrollPinned=false;",
    "let _programmaticScroll=false;",
    "let _programmaticScrollSetAt=0;",
    "const window={};",
    "window._carryForwardEphemeralTurnFields=_carryForwardEphemeralTurnFields;",
    "const _syncToolCallsForLoadedMessages=()=>{};",
    "const _advanceVisibleBaselineForServerKnownRows=()=>{};",
    "const _bumpMessagesGeneration=()=>{};",
    "const _currentMessageRenderWindowSize=()=>_messageRenderWindowSize;",
    "function renderMessages(){}",
    "function $(sel){return null;}",
    "const _sameTranscriptMessage=(a,b)=>!!(a&&b)&&String(a._ts)===String(b._ts);",
    "const performance={now:()=>0};",
    "const requestAnimationFrame=()=>{};",
    "const requests=[];",
    "async function api(path){requests.push(path);return olderResponse;}",
]


# --------------------------------------------------------------------------- #
# The scenario harness: a long session opened as a bounded tail, then `done`.
# --------------------------------------------------------------------------- #
def _settle(
    *,
    full_total: int = 200,
    resident_rows: int = 53,
    resident_message_count: int | None = None,
    resident_generation: int | None = 5,
    done_generation: int | None = 5,
    resident_truncated: bool = True,
    resident_oldest_idx: int | None = None,
    done_session_extra: dict | None = None,
    older_page_rows: int = 30,
    run_load_older: bool = True,
) -> dict:
    full = _transcript(full_total)
    turn = _new_turn()
    settled = full + turn
    resident = full[full_total - resident_rows:]
    if resident_oldest_idx is None:
        resident_oldest_idx = full_total - resident_rows
    if resident_message_count is None:
        resident_message_count = full_total

    resident_session = {
        "session_id": "A",
        "title": "long session",
        "message_count": resident_message_count,
        "transcript_generation": resident_generation,
        "tool_calls": [],
    }
    done_session = {
        "session_id": "A",
        "title": "long session",
        # _session_payload_with_full_messages(): the FULL transcript plus the
        # count of exactly that array — no truncation flag, no offset.
        "messages": settled,
        "message_count": len(settled),
        "transcript_generation": done_generation,
        "tool_calls": [],
    }
    if done_session_extra:
        done_session.update(done_session_extra)

    older_page = full[max(0, resident_oldest_idx - older_page_rows): resident_oldest_idx]
    older_response = {
        "session": {
            "session_id": "A",
            "messages": older_page + resident + turn,
            "message_count": len(older_page) + resident_rows + len(turn),
            "_messages_truncated": True,
            "_messages_offset": resident_oldest_idx - len(older_page),
            "tool_calls": [],
        }
    }

    case = "\n".join(
        [
            *_RUNTIME_STUBS,
            "const olderResponse=" + json.dumps(older_response) + ";",
            "const residentSession=" + json.dumps(resident_session) + ";",
            "const residentMessages=" + json.dumps(resident) + ";",
            "const S={session:residentSession,messages:residentMessages,activeStreamId:'stream-one'};",
            "const d={session:" + json.dumps(done_session) + ",stream_id:'stream-one'};",
            "const activeSid='A',completedSid='A';",
            "const _settledStreamId='stream-one';",
            "const _pendingTitleUpdates=new Map();",
            "const _pendingTitleExpectedCurrent=new Map();",
            "function applySessionTitleUpdate(){}",
            "let _oldestIdx=" + json.dumps(resident_oldest_idx) + ";",
            "let _messagesTruncated=" + json.dumps(resident_truncated) + ";",
            "const _streamDisplay=()=>" + json.dumps(STREAMED_ANSWER) + ";",
            _done_listener_statements(),
            "const afterDone={",
            "  count:S.messages.length,",
            "  settledCount:" + json.dumps(len(settled)) + ",",
            "  oldestIdx:_oldestIdx,",
            "  messagesTruncated:_messagesTruncated,",
            "  residentTs:S.messages.slice(0," + json.dumps(resident_rows) + ").map(m=>m._ts).join(','),",
            "  expectedResidentTs:" + json.dumps(",".join(m["_ts"] for m in resident)) + ",",
            "  tailTs:S.messages.slice(-" + json.dumps(len(turn)) + ").map(m=>m._ts).join(','),",
            "  expectedTailTs:" + json.dumps(",".join(m["_ts"] for m in turn)) + ",",
            "  answerInTranscript:S.messages.some(m=>m.role==='assistant'&&m.content===" + json.dumps(STREAMED_ANSWER) + "),",
            "};",
        ]
    )
    if run_load_older:
        case += "\n".join(
            [
                "await _loadOlderMessages();",
                "const afterLoadOlder={",
                "  requests:requests.slice(),",
                "  count:S.messages.length,",
                "  oldestIdx:_oldestIdx,",
                "  messagesTruncated:_messagesTruncated,",
                "  headTs:S.messages.length?S.messages[0]._ts:null,",
                "  expectedHeadTs:" + json.dumps(older_page[0]["_ts"]) + ",",
                "};",
                "console.log(JSON.stringify({afterDone:afterDone,afterLoadOlder:afterLoadOlder}));",
            ]
        )
    else:
        case += "\nconsole.log(JSON.stringify({afterDone:afterDone}));"
    return _run_node(case)


# --------------------------------------------------------------------------- #
# THE regression: the settled full transcript must not become the resident set.
# --------------------------------------------------------------------------- #
def test_done_full_snapshot_keeps_bounded_tail_window() -> None:
    after = _settle()["afterDone"]
    assert after["count"] < after["settledCount"], (
        "the full settled transcript must not be adopted wholesale (#8989)"
    )
    # width = max(_INITIAL_MSG_LIMIT, loaded renderable rows, loaded + appended)
    assert after["count"] == 53 + 4, after
    assert after["residentTs"] == after["expectedResidentTs"], "every resident row must survive"
    assert after["tailTs"] == after["expectedTailTs"], "the settled turn must be at the tail"
    assert after["answerInTranscript"] is True, "#6112: the streamed answer must survive"
    assert after["messagesTruncated"] is True, "the Load-earlier gate must stay open"
    assert after["oldestIdx"] == 200 + 4 - 57, after


def test_done_full_snapshot_keeps_load_earlier_working() -> None:
    """Older-messages loading must still page off the settled window origin."""
    load = _settle()["afterLoadOlder"]
    assert load["requests"], "Load earlier must still issue a paging request after `done`"
    assert "msg_before=147" in load["requests"][0] or "msg_limit=87" in load["requests"][0], load["requests"]
    assert load["count"] == 53 + 4 + 30, load
    assert load["oldestIdx"] == 117, load
    assert load["messagesTruncated"] is True, load
    assert load["headTs"] == load["expectedHeadTs"], "the paged-in head must be prepended"


def test_done_full_snapshot_is_idempotent_for_repeated_turns() -> None:
    """A second identical settle must neither grow nor shrink the window."""
    first = _settle(run_load_older=False)["afterDone"]
    second = _settle(
        run_load_older=False,
        resident_rows=first["count"],
        resident_message_count=first["settledCount"],
        resident_oldest_idx=first["oldestIdx"],
    )["afterDone"]
    assert second["count"] == first["count"], (first, second)
    assert second["oldestIdx"] == first["oldestIdx"], (first, second)
    assert second["messagesTruncated"] is True
    assert second["tailTs"] == second["expectedTailTs"]


def test_done_full_snapshot_keeps_intentionally_loaded_history() -> None:
    """A reader who paged in older rows keeps them (never collapsed to 30)."""
    after = _settle(resident_rows=120, resident_oldest_idx=80)["afterDone"]
    assert after["count"] == 124, after
    assert after["residentTs"] == after["expectedResidentTs"], after
    assert after["oldestIdx"] == 204 - 124, after
    assert after["messagesTruncated"] is True


def test_short_settled_transcript_is_adopted_whole() -> None:
    """Nothing to trim when the whole transcript already fits the window."""
    after = _settle(
        full_total=10, resident_rows=6, resident_oldest_idx=4, run_load_older=False
    )["afterDone"]
    assert after["count"] == after["settledCount"] == 14, after
    assert after["messagesTruncated"] is False, after
    assert after["oldestIdx"] == 0, after


def test_untruncated_resident_window_still_adopts_everything() -> None:
    """A fully loaded session (Load all / short session) keeps server-wins."""
    after = _settle(
        resident_rows=200,
        resident_oldest_idx=0,
        resident_truncated=False,
        run_load_older=False,
    )["afterDone"]
    assert after["count"] == after["settledCount"] == 204, after
    assert after["messagesTruncated"] is False, after
    assert after["oldestIdx"] == 0, after


def test_rotated_generation_still_settles_into_a_bounded_window() -> None:
    """Compression/rotation: the window stays bounded and the answer survives."""
    after = _settle(done_generation=9, run_load_older=False)["afterDone"]
    assert after["count"] == 57, after
    assert after["messagesTruncated"] is True, after
    assert after["oldestIdx"] == 147, after
    assert after["answerInTranscript"] is True, after


# --------------------------------------------------------------------------- #
# Adjacent faces: the existing settle behaviours must be untouched.
# --------------------------------------------------------------------------- #
def _bare_listener(resident: list[dict], done_session: dict, *, oldest_idx: int, truncated: bool, answer: str) -> dict:
    return _run_node(
        "\n".join(
            [
                *_RUNTIME_STUBS,
                "const olderResponse=null;",
                "const residentSession={session_id:'A',message_count:200,transcript_generation:5,tool_calls:[]};",
                "const residentMessages=" + json.dumps(resident) + ";",
                "const S={session:residentSession,messages:residentMessages,activeStreamId:'stream-one'};",
                "const d={session:" + json.dumps(done_session) + ",stream_id:'stream-one'};",
                "const activeSid='A',completedSid='A';",
                "const _settledStreamId='stream-one';",
                "const _pendingTitleUpdates=new Map();",
                "const _pendingTitleExpectedCurrent=new Map();",
                "function applySessionTitleUpdate(){}",
                "let _oldestIdx=" + json.dumps(oldest_idx) + ";",
                "let _messagesTruncated=" + json.dumps(truncated) + ";",
                "const _streamDisplay=()=>" + json.dumps(answer) + ";",
                _done_listener_statements(),
                "console.log(JSON.stringify({count:S.messages.length,oldestIdx:_oldestIdx,messagesTruncated:_messagesTruncated,headTs:S.messages.length?S.messages[0]._ts:null,tailContent:S.messages.length?S.messages[S.messages.length-1].content:null}));",
            ]
        )
    )


def test_already_windowed_done_payload_still_merges_resident_prefix() -> None:
    """A bounded settled payload keeps the existing prefix-merge behaviour."""
    full = _transcript(200)
    got = _bare_listener(
        full[70:130],
        {
            "session_id": "A",
            "messages": full[128:],
            "message_count": 200,
            "transcript_generation": 5,
            "_messages_offset": 128,
            "_messages_truncated": True,
            "tool_calls": [],
        },
        oldest_idx=70,
        truncated=True,
        answer="",
    )
    assert got["count"] == 130, got
    assert got["headTs"] == full[70]["_ts"], got
    assert got["oldestIdx"] == 70, got
    assert got["messagesTruncated"] is True, got


def test_empty_done_snapshot_still_keeps_resident_transcript() -> None:
    """#6112 stays fixed: an empty settled snapshot must not blank the pane."""
    full = _transcript(200)
    resident = full[147:199]
    got = _bare_listener(
        resident,
        {"session_id": "A", "messages": [], "message_count": 204, "transcript_generation": 5, "tool_calls": []},
        oldest_idx=147,
        truncated=True,
        answer=STREAMED_ANSWER,
    )
    assert got["count"] == len(resident) + 1, got
    assert got["oldestIdx"] == 147, got
    assert got["messagesTruncated"] is True, got
    assert got["tailContent"] == STREAMED_ANSWER, got


# --------------------------------------------------------------------------- #
# The width rule must stay the ONE rule: the done-settle width must equal the
# same-session reload width (_messageReloadLimitForSession) and the #7925
# widening-re-fetch shape (max(heldLen, message_count - _oldestIdx, 30)).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "loaded_renderable,loaded_rows,appended,previous_count",
    [
        (30, 30, 0, 200),
        (53, 53, 4, 1000),
        (120, 130, 2, 1000),
        (5, 40, 0, 500),
        (500, 500, 1, 900),
        (10, 12, 3, 100),
    ],
)
def test_done_window_width_matches_the_reload_width_rule(
    loaded_renderable, loaded_rows, appended, previous_count
) -> None:
    if not _optional_function(MESSAGES_JS, "_boundedDoneTailWindow"):
        pytest.skip("the #8989 helper is not present on this snapshot")
    current_count = previous_count + appended
    oldest_idx = previous_count - loaded_rows
    pre_rows = [
        {
            "role": "tool" if i < loaded_rows - loaded_renderable else "user",
            "content": f"r{i}",
            "_ts": str(i),
        }
        for i in range(loaded_rows)
    ]
    settled_rows = [
        {"role": "user", "content": f"s{i}", "_ts": str(10000 + i)} for i in range(current_count)
    ]
    got = _run_node(
        "\n".join(
            [
                "const _INITIAL_MSG_LIMIT=30;",
                "const _sameSessionForceReloadHint={",
                "  session_id:'A',",
                "  loaded_renderable_count:" + json.dumps(loaded_renderable) + ",",
                "  loaded_message_count:" + json.dumps(loaded_rows) + ",",
                "  message_count:" + json.dumps(previous_count) + ",",
                "  truncated:true,",
                "};",
                "const S={session:{session_id:'A',message_count:" + json.dumps(current_count) + "}};",
                "const reloadWidth=_messageReloadLimitForSession('A');",
                "const pre=" + json.dumps(pre_rows) + ";",
                "const dSession={messages:" + json.dumps(settled_rows)
                + ",message_count:" + json.dumps(current_count) + "};",
                "const win=_boundedDoneTailWindow("
                + json.dumps({"session_id": "A", "message_count": previous_count})
                + ","
                + json.dumps(oldest_idx)
                + ",true,pre,dSession);",
                "console.log(JSON.stringify({reloadWidth:reloadWidth,doneWidth:win?win.messages.length:null,offset:win?win.offset:null,truncated:win?win.truncated:null}));",
            ]
        )
    )
    assert got["doneWidth"] == got["reloadWidth"], got
    # #7925 widening shape: max(heldLen, message_count - _oldestIdx, 30).
    assert got["doneWidth"] == max(loaded_rows, current_count - oldest_idx, 30), got
    assert got["offset"] == current_count - got["doneWidth"], got
    assert got["truncated"] is (got["offset"] > 0), got
