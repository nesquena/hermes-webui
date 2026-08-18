"""Regression: the active turn's user bubble must never render BELOW its own turn output.

Reported symptom (WebUI, live): while a turn is running, the user's newest
message is drawn at the very bottom of the transcript, underneath the interim
commentary and tool cards that this same message triggered.

Server truth is correct: the active turn's user row is persisted BEFORE the
turn's assistant/tool rows. The defect is in the browser projection.

`_mergePendingSessionMessage()` (static/sessions.js) merges the sidecar's
`pending_user_message` into `S.messages`. It positions that row relative to the
first `_live` assistant row only:

  * live row present  -> insert before it;
  * live row absent   -> `messages.push(...)` -> lands at the very bottom.

Both branches ignore the fact that the current turn's ALREADY-PERSISTED output
(interim assistant prose + tool rows) is sitting at the tail of the transcript.
So whenever the pending prompt has to be materialized (its transcript row is not
identity-matched, or is not in the returned window at all), the bubble is placed
after that output instead of at the turn boundary.

These tests run the real helpers extracted from the shipped static assets.
"""

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_SRC = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        char = src[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"could not extract function body for {signature!r}")


def _optional_function_body(src: str, signature: str) -> str:
    try:
        return _function_body(src, signature)
    except (ValueError, AssertionError):
        return ""


def _helpers() -> str:
    parts = [
        "const _PENDING_ACTIVE_TURN_TS_EPSILON=1e-6;",
        _function_body(UI_SRC, "function _stripWorkspaceDisplayPrefix"),
        _function_body(UI_SRC, "function msgContent"),
        _function_body(UI_SRC, "function _isContextCompactionText"),
        _function_body(UI_SRC, "function _isContextCompactionMessage"),
        _function_body(UI_SRC, "function _timestampSeconds"),
        _function_body(UI_SRC, "function _firstValidTimestampSeconds"),
        _function_body(UI_SRC, "function _messageTimestampSeconds"),
        _function_body(UI_SRC, "function _activeTurnTokenMatches"),
        _function_body(UI_SRC, "function _pendingCurrentTailUserMessage"),
        _function_body(UI_SRC, "function _pendingActiveTurnUserMessage"),
        _function_body(UI_SRC, "function getPendingSessionMessage"),
        _function_body(SESSIONS_SRC, "function _messageComparableText"),
        _function_body(SESSIONS_SRC, "function _stripAttachedFilesMarker"),
        _function_body(SESSIONS_SRC, "function _stripForcedSkillEnvelope"),
        _function_body(SESSIONS_SRC, "function _normalizeUserTranscriptText"),
        _function_body(SESSIONS_SRC, "function _sameTranscriptMessage"),
        _function_body(SESSIONS_SRC, "function _currentTailUserMessage"),
        _function_body(SESSIONS_SRC, "function _hasCurrentTailUserDuplicate"),
        # The turn-boundary helper is the fix. Its ABSENCE is the regression, so
        # extract it optionally and let the behavioral assertion do the talking.
        _optional_function_body(SESSIONS_SRC, "function _activeTurnInsertionIndex"),
        _function_body(SESSIONS_SRC, "function _mergePendingSessionMessage"),
    ]
    return "\n".join(p for p in parts if p)


_T0 = 1000.0
_PROMPT = "nouveau prompt"


def _run_probe(script_body: str) -> dict:
    script = f"{_helpers()}\n{script_body}"
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert proc.returncode == 0, f"node probe failed: {proc.stderr[:4000]}"
    return json.loads(proc.stdout)


_SESSION_JS = json.dumps(
    {
        "session_id": "s1",
        "active_stream_id": "stream-1",
        "pending_user_message": _PROMPT,
        "pending_started_at": _T0,
        "pending_attachments": [],
    }
)


def _transcript(user_row: dict | None, *, with_live: bool) -> str:
    """Previous settled turn, optional current-turn user row, then the current
    turn's already-persisted interim prose + tool output."""
    rows: list[dict] = [
        {"role": "user", "content": "prompt precedent", "timestamp": _T0 - 200},
        {"role": "assistant", "content": "reponse precedente", "timestamp": _T0 - 150},
    ]
    if user_row is not None:
        rows.append(user_row)
    rows.extend(
        [
            {"role": "assistant", "content": "je verifie X", "timestamp": _T0 + 10},
            {"role": "tool", "content": "{}", "timestamp": _T0 + 11},
            {"role": "assistant", "content": "je verifie Y", "timestamp": _T0 + 30},
            {"role": "tool", "content": "{}", "timestamp": _T0 + 31},
        ]
    )
    if with_live:
        rows.append({"role": "assistant", "content": "streaming", "_live": True})
    return json.dumps(rows)


_PROBE_TAIL = f"""
function idxsOfPrompt(list){{
  const out=[];
  list.forEach((m,i)=>{{ if(m&&m.role==='user'&&String(m.content||'')==={json.dumps(_PROMPT)}) out.push(i); }});
  return out;
}}
function firstTurnOutputIdx(list){{
  return list.findIndex(m=>m&&m.role!=='user'&&Number(m.timestamp)>={_T0});
}}
"""


def _probe(user_row: dict | None, *, with_live: bool) -> dict:
    body = f"""
{_PROBE_TAIL}
const session={_SESSION_JS};
const messages={_transcript(user_row, with_live=with_live)};
const merged=_mergePendingSessionMessage(session, messages);
const idxs=idxsOfPrompt(messages);
const firstOutput=firstTurnOutputIdx(messages);
process.stdout.write(JSON.stringify({{
  merged,
  promptIdxs: idxs,
  firstOutputIdx: firstOutput,
  belowOwnOutput: idxs.length ? (Math.max.apply(null, idxs) > firstOutput && firstOutput >= 0) : false,
  duplicated: idxs.length > 1,
  order: messages.map(m=>`${{m.role}}@${{m.timestamp!==undefined?m.timestamp:'live'}}`),
}}));
"""
    return _run_probe(body)


def test_materialized_pending_prompt_is_not_pushed_below_its_own_turn_output():
    """The current turn's user row is absent from the returned window.

    The prompt must be materialized at the turn boundary (before the interim
    prose it triggered), not appended after it.
    """
    result = _probe(None, with_live=False)
    assert result["merged"] is True, "the pending prompt must be projected"
    assert not result["belowOwnOutput"], (
        "the active turn's user bubble was rendered BELOW the interim commentary "
        f"and tool cards of its own turn: {result['order']}"
    )
    # `firstOutputIdx` is measured AFTER the merge, so the inserted bubble has
    # already shifted it by one. The invariant is adjacency: the prompt sits
    # immediately above the first row its own turn produced.
    assert result["promptIdxs"] == [result["firstOutputIdx"] - 1], (
        "the materialized prompt must sit exactly at the active turn boundary; "
        f"got indices {result['promptIdxs']} with turn output starting at "
        f"{result['firstOutputIdx']} ({result['order']})"
    )


def test_materialized_pending_prompt_stays_above_output_with_live_row():
    """Same, while an assistant `_live` row is streaming at the tail.

    The live row is not the turn boundary: this turn's settled interim rows are
    already above it, so inserting immediately before the live row still places
    the bubble under them.
    """
    result = _probe(None, with_live=True)
    assert result["merged"] is True
    assert not result["belowOwnOutput"], (
        "the active turn's user bubble was rendered below its own turn output "
        f"even though a live row was present: {result['order']}"
    )


def test_unmatched_transcript_row_does_not_duplicate_below_its_own_output():
    """The current turn's user row IS present but is not identity-matched.

    Sub-second timestamp drift defeats the exact-identity match, so the prompt is
    materialized a second time. The duplicate must not land under the turn's own
    output; the existing row is authoritative and must be adopted instead.
    """
    drifted = {"role": "user", "content": _PROMPT, "timestamp": _T0 + 0.4}
    result = _probe(drifted, with_live=False)
    assert not result["duplicated"], (
        "the pending prompt was rendered twice (the transcript row plus a "
        f"materialized copy): {result['order']}"
    )
    assert not result["belowOwnOutput"], (
        f"the duplicated bubble landed below its own turn output: {result['order']}"
    )


def test_unmatched_transcript_row_slightly_before_started_at_is_adopted():
    """Current-turn user row whose timestamp drifted slightly earlier than
    pending_started_at must still be classified as this turn, not history.

    Review 2026-08-18 (CHANGES_REQUESTED): +0.4s drift is adopted, but -0.4s
    made the row look older than the boundary, so the classifier returned i+1
    and the merge inserted a second bubble above the existing row.
    """
    drifted = {"role": "user", "content": _PROMPT, "timestamp": _T0 - 0.4}
    result = _probe(drifted, with_live=False)
    assert not result["duplicated"], (
        "the pending prompt was rendered twice (the transcript row plus a "
        f"materialized copy): {result['order']}"
    )
    assert not result["belowOwnOutput"], (
        f"the adopted bubble landed below its own turn output: {result['order']}"
    )
    assert result["promptIdxs"] == [result["firstOutputIdx"] - 1], (
        "the drifted current-turn user row must sit at the turn boundary; "
        f"got indices {result['promptIdxs']} with turn output starting at "
        f"{result['firstOutputIdx']} ({result['order']})"
    )


def test_timestampless_transcript_row_fails_closed_to_prior_merge_behavior():
    """Same class, with a transcript row carrying no timestamp at all.

    Review 2026-08-17 (CHANGES_REQUESTED): a settled row without a comparable
    timestamp is indistinguishable from history, so the classifier must fail
    closed (-1) and keep the prior merge behaviour (no live row -> append)
    instead of declaring the whole window active and adopting an ambiguous row.
    The transient duplicate bubble is recoverable at settle; swallowing a real
    turn is not.
    """
    untimed = {"role": "user", "content": _PROMPT}
    result = _probe(untimed, with_live=False)
    assert result["merged"] is True, (
        f"fail-closed must keep the prior append behaviour: {result['order']}"
    )
    # The untimed row keeps its place; the pending prompt is appended at the
    # tail exactly as before the boundary classifier existed. Nothing is
    # re-ordered, adopted, or dropped.
    assert result["promptIdxs"] == [2, 7], (
        "an untimed settled row must not be adopted nor re-ordered; the prompt "
        f"must be appended (prior behaviour): {result['order']}"
    )


def test_identity_matched_row_is_still_adopted_without_duplication():
    """Guard the already-working path: an exact identity match must keep
    adopting the transcript row rather than materializing a second bubble."""
    tokened = {
        "role": "user",
        "content": _PROMPT,
        "timestamp": _T0,
        "_active_turn_token": f"stream-1:{_T0}",
    }
    result = _probe(tokened, with_live=False)
    assert result["merged"] is False, "an identity-matched row must be adopted, not re-materialized"
    assert result["promptIdxs"] == [2], result["order"]
    assert not result["duplicated"]
    assert not result["belowOwnOutput"]


def test_pending_prompt_before_any_turn_output_still_appends():
    """No output has been persisted yet for the active turn: the prompt is the
    tail of the transcript and must stay there (no spurious re-ordering)."""
    body = f"""
{_PROBE_TAIL}
const session={_SESSION_JS};
const messages=[
  {{role:'user', content:'prompt precedent', timestamp:{_T0 - 200}}},
  {{role:'assistant', content:'reponse precedente', timestamp:{_T0 - 150}}},
];
const merged=_mergePendingSessionMessage(session, messages);
process.stdout.write(JSON.stringify({{
  merged,
  promptIdxs: idxsOfPrompt(messages),
  total: messages.length,
  order: messages.map(m=>`${{m.role}}@${{m.timestamp!==undefined?m.timestamp:'live'}}`),
}}));
"""
    result = _run_probe(body)
    assert result["merged"] is True
    assert result["promptIdxs"] == [2], result["order"]
    assert result["total"] == 3


def test_pending_prompt_inserts_before_live_row_when_turn_has_no_settled_output():
    """Established contract (#6419): with a live assistant row and no settled
    output for this turn, the prompt goes immediately before the live row."""
    body = f"""
{_PROBE_TAIL}
const session={_SESSION_JS};
const messages=[
  {{role:'user', content:'prompt precedent', timestamp:{_T0 - 200}}},
  {{role:'assistant', content:'reponse precedente', timestamp:{_T0 - 150}}},
  {{role:'assistant', content:'streaming', _live:true}},
];
const merged=_mergePendingSessionMessage(session, messages);
process.stdout.write(JSON.stringify({{
  merged,
  promptIdxs: idxsOfPrompt(messages),
  order: messages.map(m=>`${{m.role}}@${{m.timestamp!==undefined?m.timestamp:'live'}}`),
}}));
"""
    result = _run_probe(body)
    assert result["merged"] is True
    assert result["promptIdxs"] == [2], (
        f"the prompt must precede the live assistant row: {result['order']}"
    )


def test_previous_turn_rows_are_not_treated_as_active_turn_output():
    """Only rows at/after `pending_started_at` belong to the active turn.

    A settled prior turn whose rows sit above the boundary must not pull the
    bubble upward into history.
    """
    body = f"""
{_PROBE_TAIL}
const session={_SESSION_JS};
const messages=[
  {{role:'user', content:'prompt tres ancien', timestamp:{_T0 - 900}}},
  {{role:'assistant', content:'vieille reponse', timestamp:{_T0 - 880}}},
  {{role:'tool', content:'{{}}', timestamp:{_T0 - 870}}},
  {{role:'user', content:'prompt precedent', timestamp:{_T0 - 200}}},
  {{role:'assistant', content:'reponse precedente', timestamp:{_T0 - 150}}},
  {{role:'assistant', content:'je verifie X', timestamp:{_T0 + 10}}},
  {{role:'tool', content:'{{}}', timestamp:{_T0 + 11}}},
];
const merged=_mergePendingSessionMessage(session, messages);
process.stdout.write(JSON.stringify({{
  merged,
  promptIdxs: idxsOfPrompt(messages),
  order: messages.map(m=>`${{m.role}}@${{m.timestamp!==undefined?m.timestamp:'live'}}`),
}}));
"""
    result = _run_probe(body)
    assert result["merged"] is True
    assert result["promptIdxs"] == [5], (
        "the prompt must land at the active turn boundary, not inside the "
        f"previous settled turn: {result['order']}"
    )


# ---------------------------------------------------------------------------
# Review 2026-08-17 (CHANGES_REQUESTED): timestamp unit/format mishandling.
#
# The first classifier skipped untimed and ISO-string timestamps and read
# millisecond epochs as seconds. `sawTimestampedRow ? 0 : -1` then declared the
# entire window active, inserting the current prompt at index 0 above real
# history — or adopting an earlier same-text row, swallowing a turn and losing
# the current attachment. Such transcripts are reachable in production:
# /api/session/import preserves supplied message timestamps and creates an
# ordinary writable session.
#
# Contract pinned here: normalize every compared timestamp through
# _timestampSeconds/_firstValidTimestampSeconds; fail closed (-1 -> prior merge
# behaviour) when any crossed settled row lacks a comparable timestamp; return
# index 0 only when every settled row is demonstrably at/after the boundary.
# ---------------------------------------------------------------------------

# A realistic epoch: ISO strings must round-trip through Date.parse.
_EPOCH = 1755424800.0  # 2025-08-17T10:00:00Z


def _epoch_session(attachments: list | None = None) -> str:
    return json.dumps(
        {
            "session_id": "s1",
            "active_stream_id": "stream-1",
            "pending_user_message": _PROMPT,
            "pending_started_at": _EPOCH,
            "pending_attachments": attachments or [],
        }
    )


def _run_epoch_probe(history_rows_js: str, attachments: list | None = None) -> dict:
    body = f"""
const session={_epoch_session(attachments)};
const messages=[
{history_rows_js}
  {{role:'assistant', content:'je verifie X', timestamp:{_EPOCH + 10}}},
  {{role:'tool', content:'{{}}', timestamp:{_EPOCH + 11}}},
];
const merged=_mergePendingSessionMessage(session, messages);
process.stdout.write(JSON.stringify({{
  merged,
  promptIdxs: messages.reduce((out,m,i)=>{{ if(m&&m.role==='user'&&String(m.content||'')==={json.dumps(_PROMPT)}) out.push(i); return out; }}, []),
  attachmentsByIdx: messages.map(m=>Array.isArray(m&&m.attachments)?m.attachments.map(a=>a&&a.name):null),
  order: messages.map(m=>`${{m.role}}@${{m.timestamp!==undefined?m.timestamp:(m._ts!==undefined?m._ts:'untimed')}}`),
}}));
"""
    return _run_probe(body)


def test_untimed_history_rows_are_never_displaced_below_current_prompt():
    """Untimed settled HISTORY (different text) must stay first.

    The first classifier skipped untimed rows, saw only the timestamped turn
    output at/after the boundary, and returned 0 — inserting the current prompt
    at index 0, above real history. Master kept history first (append). The
    classifier must fail closed to that prior behaviour.
    """
    result = _run_epoch_probe(
        """  {role:'user', content:'prompt precedent'},
  {role:'assistant', content:'reponse precedente'},
"""
    )
    assert result["merged"] is True
    assert result["promptIdxs"] == [4], (
        "with untimed settled history the classifier must fall back to the "
        f"prior append behaviour, never index 0: {result['order']}"
    )
    assert result["order"][0].startswith("user@"), result["order"]


def test_iso_string_history_rows_stay_above_current_prompt():
    """ISO-8601 string timestamps are comparable after normalization.

    The first classifier read them through Number() -> NaN and skipped them like
    untimed rows, so the prompt landed at index 0 above ISO-timestamped history.
    Normalized, they are strictly older than the boundary and history stays
    first, with the prompt at the turn boundary.
    """
    result = _run_epoch_probe(
        """  {role:'user', content:'prompt precedent', timestamp:'2025-08-17T09:00:00Z'},
  {role:'assistant', content:'reponse precedente', timestamp:'2025-08-17T09:01:00Z'},
"""
    )
    assert result["merged"] is True
    assert result["promptIdxs"] == [2], (
        "ISO-timestamped history must stay above the current prompt, which "
        f"belongs at the turn boundary: {result['order']}"
    )


def test_millisecond_history_rows_stay_above_current_prompt():
    """Millisecond epochs are comparable after normalization.

    The first classifier compared raw ms against a seconds boundary, so history
    looked newer than `pending_started_at` and the whole window was declared
    active (index 0). Normalized, ms history is strictly older and stays first.
    """
    result = _run_epoch_probe(
        f"""  {{role:'user', content:'prompt precedent', timestamp:{(_EPOCH - 3600) * 1000}}},
  {{role:'assistant', content:'reponse precedente', timestamp:{(_EPOCH - 3540) * 1000}}},
"""
    )
    assert result["merged"] is True
    assert result["promptIdxs"] == [2], (
        "millisecond-timestamped history must stay above the current prompt: "
        f"{result['order']}"
    )


def test_earlier_identical_prompt_with_different_attachments_is_not_swallowed():
    """An earlier turn with the SAME text but different attachments must keep
    its own bubble AND its own attachments, and the current turn must keep its.

    Reproduced by review on the first classifier: the ms-timestamped history was
    misread as at/after the boundary, index 0 was declared the turn start, and
    sessions.js adopted the earlier same-text row — two turns rendered as one
    and the current attachment was lost (the earlier row already had one, so the
    adoption branch dropped the pending attachment on the floor).
    """
    for label, earlier_ts in (
        ("milliseconds", json.dumps((_EPOCH - 3600) * 1000)),
        ("ISO string", json.dumps("2025-08-17T09:00:00Z")),
    ):
        result = _run_epoch_probe(
            f"""  {{role:'user', content:{json.dumps(_PROMPT)}, timestamp:{earlier_ts}, attachments:[{{name:'ancien.png'}}]}},
  {{role:'assistant', content:'reponse precedente', timestamp:{earlier_ts}}},
""",
            attachments=[{"name": "nouveau.pdf"}],
        )
        assert result["merged"] is True, (label, result["order"])
        assert result["promptIdxs"] == [0, 2], (
            f"[{label}] both the earlier identical prompt and the current one "
            f"must keep their own bubbles: {result['order']}"
        )
        assert result["attachmentsByIdx"][0] == ["ancien.png"], (
            f"[{label}] the earlier turn's attachment must survive untouched: "
            f"{result['attachmentsByIdx']}"
        )
        assert result["attachmentsByIdx"][2] == ["nouveau.pdf"], (
            f"[{label}] the current turn's attachment must not be lost: "
            f"{result['attachmentsByIdx']}"
        )
