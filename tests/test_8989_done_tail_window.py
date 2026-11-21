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
        # The F1 helper is required — a missing helper means the PR's fix was
        # reverted, which must FAIL (RED) rather than silently skip.
        _optional_function(MESSAGES_JS, "_remapDoneToolCallsToResident"),
        _optional_function(MESSAGES_JS, "_mergeResidentPrefix"),
        _optional_function(MESSAGES_JS, "_doneResidentMergeOptions"),
        _optional_function(MESSAGES_JS, "_mergeSettledToolCallsWithLiveMetadata"),
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
# #8994 — the resident array is already filtered, so it is NOT a raw transcript
# span. Two consecutive settlements must not evict ordinary resident rows.
# --------------------------------------------------------------------------- #
_LEGACY_RECOVERY_TEXT = (
    "[System: Your previous response was cut off by a network error. "
    "Continue exactly where you left off.]"
)


def _chain_turn(ts_base: int) -> list[dict]:
    """A four-row settled turn (user -> tool call -> tool result -> answer)."""
    return [
        {"role": "user", "content": f"chain question {ts_base}", "_ts": str(ts_base)},
        {
            "role": "assistant",
            "content": "",
            "_ts": str(ts_base + 1),
            "tool_calls": [{"id": f"call-{ts_base}", "function": {"name": "grep"}}],
        },
        {
            "role": "tool",
            "content": "grep output",
            "_ts": str(ts_base + 2),
            "tool_call_id": f"call-{ts_base}",
        },
        {"role": "assistant", "content": STREAMED_ANSWER, "_ts": str(ts_base + 3)},
    ]


def _settle_chain(
    *,
    recovery_raw_indices: tuple[int, ...] = (),
    second_generation: int | None = 5,
    full_total: int = 200,
    resident_oldest_idx: int = 147,
) -> dict:
    """Two consecutive `done` settlements over one bounded resident window.

    The maintainer's replay for the #8994 resident-history regression: a
    bounded tail at a raw origin with the supported legacy recovery rows
    inside it, then two four-row turns settled back-to-back with
    `transcript_generation` held and authoritative counts 204 then 208, the
    next pending user row appended between the settlements. The second
    settlement runs the REAL done-listener statements again over the state the
    first one left behind, so the resident array it measures is the filtered
    one the production filter produces.
    """
    full = _transcript(full_total)
    for idx in recovery_raw_indices:
        row = dict(full[idx])
        row["content"] = _LEGACY_RECOVERY_TEXT
        full[idx] = row
    turn_one = _chain_turn(9001)
    turn_two = _chain_turn(9101)
    pending_one = {"role": "user", "content": "pending question one", "_ts": "8999"}
    pending_two = {"role": "user", "content": "pending question two", "_ts": "9099"}
    resident = full[resident_oldest_idx:]
    done_one = full + turn_one
    done_two = full + turn_one + turn_two

    def _settle_block(session_var: str, label: str) -> str:
        return (
            f"const d={{session:{session_var},stream_id:'stream-one'}};\n"
            + _done_listener_statements()
            + f"\nafter{label}={{"
            + "count:S.messages.length,"
            + "oldestIdx:_oldestIdx,"
            + "messagesTruncated:_messagesTruncated,"
            + "ts:S.messages.map(m=>m._ts),"
            + "answerInTranscript:S.messages.some(m=>m.role==='assistant'&&m.content==="
            + json.dumps(STREAMED_ANSWER)
            + ")};\n"
        )

    case = "\n".join(
        [
            *_RUNTIME_STUBS,
            "const olderResponse=null;",
            "const doneOneSession=" + json.dumps({
                "session_id": "A",
                "title": "long session",
                "messages": done_one,
                "message_count": len(done_one),
                "transcript_generation": 5,
                "tool_calls": [],
            }) + ";",
            "const doneTwoSession=" + json.dumps({
                "session_id": "A",
                "title": "long session",
                "messages": done_two,
                "message_count": len(done_two),
                "transcript_generation": second_generation,
                "tool_calls": [],
            }) + ";",
            # A cold-loaded bounded session: the authoritative count, the
            # absolute origin mirrored in _messages_offset, and the truncation
            # flag — exactly what loadSession leaves behind.
            "const residentSession=" + json.dumps({
                "session_id": "A",
                "title": "long session",
                "message_count": full_total,
                "transcript_generation": 5,
                "_messages_offset": resident_oldest_idx,
                "_messages_truncated": True,
                "tool_calls": [],
            }) + ";",
            "const residentMessages=" + json.dumps(resident) + ";",
            "const pendingOne=" + json.dumps(pending_one) + ";",
            "const pendingTwo=" + json.dumps(pending_two) + ";",
            "const S={session:residentSession,messages:residentMessages.concat([pendingOne]),activeStreamId:'stream-one'};",
            "const activeSid='A',completedSid='A';",
            "const _settledStreamId='stream-one';",
            "const _pendingTitleUpdates=new Map();",
            "const _pendingTitleExpectedCurrent=new Map();",
            "function applySessionTitleUpdate(){}",
            "let _oldestIdx=" + json.dumps(resident_oldest_idx) + ";",
            "let _messagesTruncated=true;",
            "const _streamDisplay=()=>" + json.dumps(STREAMED_ANSWER) + ";",
            "let afterFirst=null,afterSecond=null;",
            "{",
            _settle_block("doneOneSession", "First"),
            "}",
            # Between settlements the next pending user row is appended, as the
            # existing harness does for the row the reader just sent.
            "S.messages=S.messages.concat([pendingTwo]);",
            "{",
            _settle_block("doneTwoSession", "Second"),
            "}",
            "console.log(JSON.stringify({afterFirst:afterFirst,afterSecond:afterSecond,"
            + "firstSettledCount:" + json.dumps(len(done_one))
            + ",secondSettledCount:" + json.dumps(len(done_two))
            + "}));",
        ]
    )
    return _run_node(case)


def test_consecutive_settlements_keep_resident_rows_past_the_recovery_filter() -> None:
    """#8994 — filtered legacy recovery rows must not shift the raw window.

    `_filterRecoveryControlMessages()` runs at the end of every settle, so the
    resident array the NEXT settle measures is shorter than the raw span its
    absolute origin points into. Measuring the next window from that filtered
    cardinality advances the origin by the filtered count and evicts ordinary
    rows that are still resident: on the maintainer's 200-row replay (legacy
    recovery text at raw 180-182, two consecutive settlements, counts 204 then
    208) the origin marched 146 -> 148 and ordinary rows 146/147 disappeared.
    """
    result = _settle_chain(recovery_raw_indices=(180, 181, 182))
    first, second = result["afterFirst"], result["afterSecond"]
    recovery_ts = {str(181), str(182), str(183)}  # _transcript: raw row i -> _ts str(1 + i)
    ordinary_first = [ts for ts in first["ts"] if ts not in recovery_ts]
    assert first["count"] == 55 and first["oldestIdx"] == 146, first
    assert second["oldestIdx"] == first["oldestIdx"], (first, second)
    assert set(ordinary_first) <= set(second["ts"]), (
        "every ordinary resident timestamp must survive the second settlement",
        sorted(set(ordinary_first) - set(second["ts"])),
    )
    assert second["count"] < result["secondSettledCount"], second
    assert second["messagesTruncated"] is True, second
    assert second["answerInTranscript"] is True, second
    # Trusted raw span (204 - 146) plus the four appended rows, minus the
    # recovery rows the settle filter removes again.
    assert second["count"] == 204 - first["oldestIdx"] + 4 - len(recovery_ts), second


def test_consecutive_settlements_without_recovery_rows_keep_the_window_stable() -> None:
    """No-marker control: the same chain without any recovery rows.

    Without filtered rows the resident cardinality already equals the raw span
    plus the pending row, so the trusted-span term never binds and the window
    behaves exactly as before — this control passes on both trees.
    """
    result = _settle_chain()
    first, second = result["afterFirst"], result["afterSecond"]
    assert first["count"] == 58 and first["oldestIdx"] == 146, first
    assert second["count"] == 63 and second["oldestIdx"] == 145, second
    assert set(first["ts"]) <= set(second["ts"]), second
    assert second["count"] < result["secondSettledCount"], second
    assert second["messagesTruncated"] is True, second
    assert second["answerInTranscript"] is True, second


def test_generation_change_between_settlements_keeps_the_guard() -> None:
    """Generation-change control: rotation renumbers the raw coordinate space.

    Across a transcript_generation change the previous absolute origin must NOT
    be carried into the next window — the resident-cardinality width (the
    behaviour this PR shipped with) stays in force, so the window is still
    bounded, still truncated and the answer still survives.
    """
    result = _settle_chain(recovery_raw_indices=(180, 181, 182), second_generation=9)
    first, second = result["afterFirst"], result["afterSecond"]
    assert first["count"] == 55 and first["oldestIdx"] == 146, first
    assert second["count"] == 60 - 3, second
    assert second["oldestIdx"] == result["secondSettledCount"] - 60, second
    assert second["count"] < result["secondSettledCount"], second
    assert second["messagesTruncated"] is True, second
    assert second["answerInTranscript"] is True, second


# --------------------------------------------------------------------------- #
# #8994 coordinate authority: a stale session offset must not veto _oldestIdx.
# --------------------------------------------------------------------------- #
def _settle_after_widened_page(
    *,
    full_total: int = 200,
    stale_offset: int = 147,
    loaded_oldest_idx: int = 117,
    recovery_raw_indices: tuple[int, ...] = (180, 181),
) -> dict:
    """One settle after Load earlier widened the window, session offset stale.

    The maintainer's replay: a bounded session was SAVED at offset 147, then a
    real older-page load lowered `_oldestIdx` to 117. `_loadOlderMessages()`
    only moves `_oldestIdx`; it never rewrites the saved
    `session._messages_offset`, so the two disagree for the rest of the tab's
    life. Meanwhile the supported legacy recovery rows inside the widened span
    are dropped by the consume filter, so the resident array is shorter than
    the raw span its origin points into.

    Requiring the stale offset to agree with `_oldestIdx` therefore zeroed the
    trusted span and shrank the width back to the filtered resident
    cardinality, evicting ordinary rows at the front of the window that the
    reader still had.
    """
    full = _transcript(full_total)
    for idx in recovery_raw_indices:
        row = dict(full[idx])
        row["content"] = _LEGACY_RECOVERY_TEXT
        full[idx] = row

    resident = [
        row for idx, row in enumerate(full[loaded_oldest_idx:], start=loaded_oldest_idx)
        if idx not in recovery_raw_indices
    ]
    turn = _new_turn()
    settled = full + turn

    resident_session = {
        "session_id": "A",
        "title": "long session",
        "message_count": full_total,
        "transcript_generation": 5,
        "tool_calls": [],
        # Stale by construction: this is the page the session was saved with,
        # not the pager's current position.
        "_messages_offset": stale_offset,
    }
    done_session = {
        "session_id": "A",
        "title": "long session",
        "messages": settled,
        "message_count": len(settled),
        "transcript_generation": 5,
        "tool_calls": [],
    }

    case = "\n".join(
        [
            *_RUNTIME_STUBS,
            "const olderResponse=null;",
            "const residentSession=" + json.dumps(resident_session) + ";",
            "const residentMessages=" + json.dumps(resident) + ";",
            "const S={session:residentSession,messages:residentMessages,activeStreamId:'stream-one'};",
            "const d={session:" + json.dumps(done_session) + ",stream_id:'stream-one'};",
            "const activeSid='A',completedSid='A';",
            "const _settledStreamId='stream-one';",
            "const _pendingTitleUpdates=new Map();",
            "const _pendingTitleExpectedCurrent=new Map();",
            "function applySessionTitleUpdate(){}",
            "let _oldestIdx=" + json.dumps(loaded_oldest_idx) + ";",
            "let _messagesTruncated=true;",
            "const _streamDisplay=()=>" + json.dumps(STREAMED_ANSWER) + ";",
            _done_listener_statements(),
            "console.log(JSON.stringify({",
            "  count:S.messages.length,",
            "  oldestIdx:_oldestIdx,",
            "  messagesTruncated:_messagesTruncated,",
            "  ts:S.messages.map(m=>String(m._ts)),",
            "  answerInTranscript:S.messages.some(m=>m.role==='assistant'&&m.content==="
            + json.dumps(STREAMED_ANSWER)
            + "),",
            "}));",
        ]
    )
    return {"resident": resident, "run": _run_node(case)}


def test_widened_page_with_a_stale_offset_keeps_every_resident_row() -> None:
    """#8994 — `_oldestIdx` is the coordinate authority, not the saved offset.

    After Load earlier widens a bounded window, the saved `_messages_offset`
    can never lower again while `_oldestIdx` has. If the settle demands they
    agree it discards the widened span, rebuilds the width from the filtered
    resident cardinality, and the front of the window falls out of
    `S.messages` even though the reader still had it.
    """
    harness = _settle_after_widened_page()
    run = harness["run"]
    resident_ts = [str(row["_ts"]) for row in harness["resident"]]
    missing = [ts for ts in resident_ts if ts not in set(run["ts"])]

    assert not missing, (
        "settle evicted resident rows the reader still held — a stale "
        f"_messages_offset must not veto the _oldestIdx span: {missing}"
    )
    # Coordinates stay honest: the kept prefix starts exactly at the loaded
    # origin, and the window is still bounded (never the full transcript).
    assert run["oldestIdx"] == 117, run
    assert run["count"] < 200 + 4, run
    assert run["messagesTruncated"] is True, run
    assert run["answerInTranscript"] is True, run


def test_stale_offset_control_without_a_widened_page_is_unchanged() -> None:
    """Control: no widening, offset still equal — the window is identical.

    Proves the authority change only affects the disagreement case; when the
    saved offset and `_oldestIdx` still agree the trusted span resolves to the
    same value as before.
    """
    harness = _settle_after_widened_page(
        stale_offset=147, loaded_oldest_idx=147, recovery_raw_indices=(),
    )
    run = harness["run"]
    resident_ts = [str(row["_ts"]) for row in harness["resident"]]
    missing = [ts for ts in resident_ts if ts not in set(run["ts"])]

    assert not missing, missing
    assert run["oldestIdx"] == 147, run
    assert run["count"] == 200 - 147 + 4, run
    assert run["messagesTruncated"] is True, run
    assert run["answerInTranscript"] is True, run


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


# --------------------------------------------------------------------------- #
# #8994-F1 — the `done` fallback must rebase session tool_calls from the FULL
# transcript coordinate space into the RESIDENT space, and drop owners that did
# not survive the bounded window / recovery filter.
# --------------------------------------------------------------------------- #
def _f1_scenario() -> dict:
    """Full→resident rebase scenario for the F1 helper.

    - settledFull: 204 rows (200-row transcript + a 4-row no-metadata turn).
      Assistant rows sit at odd full indices (1,3,...,199,203).
    - resident: the bounded tail the done handler adopted — full[148:] with
      the raw-161 assistant row (identity 'assistant|162|...') filtered out
      because it was marked recovery_control, exactly as the listener's final
      `_filterRecoveryControlMessages` pass leaves it.
    - toolCalls: session-level summaries in FULL-transcript coordinates.
    """
    full = _transcript(200)
    turn = [
        {"role": "user", "content": "the new question", "_ts": "9001"},
        {"role": "assistant", "content": "intermediate text", "_ts": "9002"},
        {"role": "tool", "content": "grep output", "_ts": "9003", "tool_call_id": "call-1"},
        {"role": "assistant", "content": STREAMED_ANSWER, "_ts": "9004"},
    ]
    settled_full = full + turn
    # The bounded tail the listener adopts is sliced off the SETTLED payload:
    # settled[148:] = the 52-row resident span plus the 4-row turn. Raw-161's
    # assistant row is then evicted by _filterRecoveryControlMessages.
    resident = [dict(m) for m in settled_full[148:]]
    resident = [m for m in resident if str(m["_ts"]) != str(161 + 1)]

    tool_calls = [
        {"assistant_msg_idx": 9, "tid": "t-out", "name": "outside-win", "snippet": "out-summary"},
        {"assistant_msg_idx": 151, "tid": "t-in", "name": "inside-win", "snippet": "in-summary"},
        {"assistant_msg_idx": 161, "tid": "t-rec", "name": "recovery-owner", "snippet": "rec-summary"},
        {"assistant_msg_idx": 203, "tid": "t-answer", "name": "final", "snippet": "answer-summary"},
    ]
    case = "\n".join(
        [
            "const settledFull=" + json.dumps(settled_full) + ";",
            "const resident=" + json.dumps(resident) + ";",
            "const toolCalls=" + json.dumps(tool_calls) + ";",
            "const out=_remapDoneToolCallsToResident(toolCalls,settledFull,resident);",
            "const report=out.map(tc=>{",
            "  const idx=typeof tc.toolIdx==='number'?tc.toolIdx:tc.assistant_msg_idx;",
            "  return {tid:tc.tid,idx:idx,identity:_messageIdentityKey(resident[idx]||{})};",
            "});",
            "console.log(JSON.stringify({report:report,count:out.length,residentCount:resident.length}));",
        ]
    )
    return _run_node(case)


def test_done_tool_calls_rebase_full_to_resident_coordinates() -> None:
    """F1 — settled tool_calls must anchor to the resident array, not the full transcript."""
    got = _f1_scenario()
    report = {r["tid"]: r for r in got["report"]}
    assert "t-in" in report, f"in-window owner must be retained: {got}"
    assert "t-answer" in report, f"settled-answer owner must be retained: {got}"
    # t-out: full index 9 is outside the resident window → dropped.
    assert "t-out" not in report, f"windowed-out owner must be dropped: {got}"
    # t-rec: raw 161 was filtered by the recovery pass → no resident owner → dropped.
    assert "t-rec" not in report, f"recovery-filtered owner must be dropped: {got}"
    # The retained t-in card must point at the SAME message identity it had in
    # the full transcript (raw 151), rebased to its local index.
    # t-in: full index 151 is an assistant row (_transcript puts assistant at
    # odd indices): content 'transcript row 151', _ts '152'.
    full_151_identity = "assistant|152|transcript row 151"
    in_card = report["t-in"]
    assert in_card["identity"] == full_151_identity, (
        f"t-in must rebase to its real resident owner, got {in_card['identity']!r} "
        f"expected {full_151_identity!r}"
    )
    assert 0 <= in_card["idx"] < got["residentCount"], f"t-in index out of bounds: {got}"


def test_done_listener_wires_f1_remap_into_the_fallback() -> None:
    """F1 — the done fallback must call the remap helper (source-shape pin).

    A bare helper that is never wired into the fallback would keep the display
    attribution defect alive while the helper test above stays green. This pin
    proves the wiring exists in the real done-listener fallback branch.
    """
    # The fallback lives AFTER _hydrateTodosFromSession, so it is outside the
    # _done_listener_statements() window. Assert on the module source directly.
    assert "_remapDoneToolCallsToResident" in MESSAGES_JS, (
        "done listener fallback must invoke _remapDoneToolCallsToResident"
    )
    fallback_site = MESSAGES_JS.find("if(!hasMessageToolMetadata&&d.session.tool_calls")
    assert fallback_site >= 0, "done listener fallback branch not found"
    fallback_chunk = MESSAGES_JS[fallback_site:fallback_site + 1200]
    assert "S.toolCalls=d.session.tool_calls.map" not in fallback_chunk, (
        "the raw full-transcript assignment must be gone from the fallback"
    )
    assert "_remapDoneToolCallsToResident(d.session.tool_calls" in fallback_chunk, (
        "the fallback must route through the remap helper"
    )


def _done_fallback_statements() -> str:
    """The real done-listener fallback block (has-message-metadata check + assignment)."""
    start = MESSAGES_JS.find("const hasMessageToolMetadata=S.messages.some")
    end = MESSAGES_JS.find("if(typeof projectSessionArtifactsForOwner", start)
    assert start >= 0 and end > start, "done fallback statements not found"
    return MESSAGES_JS[start:end]


def test_done_fallback_attaches_rebased_tool_cards_to_resident_owners() -> None:
    """F1 — execute the REAL fallback statements over a settled payload.

    The maintainer's repro: a full-transcript tool summary at absolute index 9,
    with a resident window starting at 148, attaches its command/output to an
    unrelated recent assistant at local index 9. The fix rebases before the
    renderer reads the index.
    """
    full = _transcript(200)
    turn = [
        {"role": "user", "content": "the new question", "_ts": "9001"},
        {"role": "assistant", "content": STREAMED_ANSWER, "_ts": "9002"},
    ]
    settled_full = full + turn
    resident = [dict(m) for m in settled_full[148:]]
    tool_calls = [
        # Out-of-window owner (absolute 9) — the maintainer's exact repro.
        {"assistant_msg_idx": 9, "tid": "t-out", "name": "old", "snippet": "out"},
        # In-window owner at absolute 151.
        {"assistant_msg_idx": 151, "tid": "t-in", "name": "grep", "snippet": "in"},
    ]
    case = "\n".join(
        [
            *_RUNTIME_STUBS,
            "const S={toolCalls:[],messages:" + json.dumps(resident) + "};",
            "const d={session:{messages:" + json.dumps(settled_full) + ",tool_calls:"
            + json.dumps(tool_calls) + "}};",
            # The exact fallback block from the done listener, executed verbatim.
            _done_fallback_statements(),
            "const report=(S.toolCalls||[]).map(tc=>{",
            "  const idx=typeof tc.toolIdx==='number'?tc.toolIdx:tc.assistant_msg_idx;",
            "  const owner=S.messages[idx]||{};",
            "  return {tid:tc.tid,idx:idx,identity:_messageIdentityKey(owner)};",
            "});",
            "console.log(JSON.stringify({report:report,residentCount:S.messages.length}));",
        ]
    )
    got = _run_node(case)
    report = {r["tid"]: r for r in got["report"]}
    assert "t-out" not in report, (
        f"windowed-out full-index-9 summary must not attach to unrelated resident "
        f"row {report.get('t-out', {}).get('idx')}: {got}"
    )
    assert "t-in" in report, f"in-window owner must survive: {got}"
    assert report["t-in"]["identity"] == "assistant|152|transcript row 151", (
        f"t-in must map to its real resident owner: {got}"
    )
