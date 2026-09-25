"""Regression guard for #6112 — `done` settle must never blank the transcript.

#6112 (Bug: final assistant reply disappears at stream settlement but returns after
reload): the answer is visible for the whole live stream, is persisted correctly on
the server (`active_stream_id: null`, `finish_reason=stop`, no `apperror` /
`no_response` in the logs), yet the pane goes empty at `done`/settle and a reload
brings the same answer back. The maintainer narrowed the issue to client-side
live-to-settled reconciliation, and kept it on the NORMAL `done` / `stream_end`
settlement case (the cancel sibling is #6920/#6955, the stream_end-without-done
branch is PR #6120).

Root cause locked by this file
------------------------------
`static/messages.js`, the `done` listener, settled-snapshot adoption:

    S.session=d.session;
    S.messages=_carryForwardEphemeralTurnFields(S.messages||[], d.session.messages||[]);

`_carryForwardEphemeralTurnFields(prev, next)` returns `next` UNCONDITIONALLY when
either side is empty:

    if(!prevMessages.length||!nextMessages.length) return nextMessages;

So a `done` frame whose `session.messages` is absent, `null`, or an empty list does
not "reconcile" the transcript — it REPLACES the whole visible transcript with `[]`.
Every message the reader was just watching, including the streamed final answer, is
dropped from `S.messages`, and the settled `renderMessages()` rebuild paints an empty
pane.

Nothing downstream recovers it:

* `_filterRecoveryControlMessages([])` returns `[]` — still empty.
* the #373 no-reply guard is suppressed because the guard is
  `... && !assistantText`, and `assistantText` still holds the streamed text — so no
  "**No response received.**" card is pushed either. The pane is simply blank, which
  is precisely the reported "no error card" observation in #6112.
* a reload re-fetches `/api/session`, which reads the intact server-side transcript,
  so the answer reappears — exactly the reported "returns after reload".

The payload shape is not hypothetical: `api/streaming.py::_ephemeral_session_payload`
builds `{'session_id': ..., 'messages': messages if isinstance(messages, list) else []}`
— a non-list result yields `messages: []` — and any `done` frame replayed from the run
journal without an embedded transcript has the same effect.

Second variant, same symptom: the snapshot is NON-empty but simply never received the
in-flight assistant turn — which is what context compression produces when the session
rotates at the turn boundary. The answer then lives only in the live segment, the
settled `renderMessages()` rebuild throws it away, and the reader is left with exactly
what both reporters screenshotted: "only the submitted user message followed by a
collapsed Context compaction row, with no final answer".

This is the same failure class the `stream_end` path was already hardened for in
#5224 / #3195 (`preserveVisibleOnShorterTerminalSnapshot`): a shorter or empty server
snapshot must not silently drop a visible transcript. The `done` path had no such
guard.

Two rules fix it, both in `static/messages.js::_adoptDoneSnapshotMessages`, which the
`done` listener now runs as a settle guard immediately after the existing #3018
carry-forward assignment and before `_filterRecoveryControlMessages`:

1. only a snapshot that actually carries messages may REPLACE a populated transcript —
   an empty/absent one has nothing to reconcile;
2. only a snapshot that still contains the streamed final answer may become the settled
   transcript — otherwise the answer is re-attached to the snapshot's tail.

A non-empty snapshot that already contains the answer keeps today's server-wins
behaviour byte-for-byte, so session rotation, trimming, and recovery filtering are
unaffected.

The tests are BEHAVIOURAL: they extract the real adoption helper plus the
`_carryForwardEphemeralTurnFields` machinery from static/messages.js and execute them
in Node, then assert on the resulting transcript.
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
NODE = shutil.which("node")

ANSWER = "The final answer that was streamed to the user."


def _extract_function(src: str, name: str, prefix: str = "function") -> str:
    marker = f"{prefix} {name}("
    start = src.find(marker)
    assert start >= 0, f"{name} not found in static/messages.js"
    brace = src.find("{", start)
    assert brace >= 0, f"{name} opening brace not found"
    depth = 1
    i = brace + 1
    while i < len(src) and depth > 0:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} braces unbalanced"
    return src[start:i]


def _done_listener_src() -> str:
    start = MESSAGES_JS.find("source.addEventListener('done',e=>{")
    assert start != -1, "done listener not found"
    end = MESSAGES_JS.find("source.addEventListener('stream_end'", start)
    assert end > start, "done listener end not found"
    return MESSAGES_JS[start:end]


def _harness(extra: str = "") -> dict:
    """Execute the real adoption helpers in Node and return the JSON result."""
    assert NODE, "node is required for behavioural #6112 tests"
    pieces = [
        _extract_function(MESSAGES_JS, "_messageIdentityKey"),
        _extract_function(MESSAGES_JS, "_carryForwardEphemeralTurnFields"),
        _extract_function(MESSAGES_JS, "_adoptDoneSnapshotMessages"),
        _extract_function(MESSAGES_JS, "_finalAnswerIsInTranscript"),
        "const _EPHEMERAL_TURN_FIELDS=['_turnUsage','_turnDuration','_turnTps','_gatewayRouting','_statusCard','_anchor_stream_id','_anchor_activity_scene'];",
        _extract_function(MESSAGES_JS, "_isHistoricalAnchorActivityScene"),
        textwrap.dedent(extra),
    ]
    script = "\n".join(pieces)
    res = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, f"node harness failed:\n{res.stderr}"
    return json.loads(res.stdout.strip())


def _visible_texts(messages) -> list[str]:
    return [str(m.get("content") or "") for m in messages if m.get("role") == "assistant"]


# --------------------------------------------------------------------------- #
# RED: the adoption guard must exist and be wired into the done listener.
# --------------------------------------------------------------------------- #
def test_done_settle_has_snapshot_adoption_guard():
    """The `done` listener must run the settle guard over the adopted transcript.

    The guard has to fire AFTER the #3018 carry-forward assignment (which is where an
    empty/absent `session.messages` blanks the visible transcript) and BEFORE
    `_filterRecoveryControlMessages` and the settled `renderMessages()` rebuild
    (#6112).
    """
    done = _done_listener_src()
    carry_idx = done.find("S.messages=_carryForwardEphemeralTurnFields(S.messages||[], d.session.messages||[])")
    guard_idx = done.find("S.messages=_adoptDoneSnapshotMessages(")
    filter_idx = done.find("S.messages=_filterRecoveryControlMessages")
    assert carry_idx != -1, "the done listener must still do the #3018 carry-forward"
    assert guard_idx != -1, (
        "the done listener must run _adoptDoneSnapshotMessages so an empty payload "
        "cannot blank the transcript, and a payload without the streamed answer cannot "
        "drop it (#6112)"
    )
    assert filter_idx != -1, "the done listener must still filter recovery controls"
    assert carry_idx < guard_idx < filter_idx, (
        "the settle guard must run after the carry-forward and before the recovery "
        "filter / settled render"
    )


# --------------------------------------------------------------------------- #
# Behavioural: run the real helpers against each payload shape.
# --------------------------------------------------------------------------- #
def _run(prev_messages: str, done_session: str, final_answer: str | None = None) -> dict:
    answer_js = "null" if final_answer is None else json.dumps(final_answer)
    return _harness(
        f"""
        const prevMessages = {prev_messages};
        const doneSession = {done_session};
        const finalAnswer = {answer_js};
        const rawMessages = (doneSession && Array.isArray(doneSession.messages)) ? doneSession.messages : [];
        const settledMessages = _carryForwardEphemeralTurnFields(prevMessages, rawMessages);
        const out = _adoptDoneSnapshotMessages(prevMessages, settledMessages, finalAnswer);
        const msgs = Array.isArray(out.messages) ? out.messages : [];
        console.log(JSON.stringify({{
            adopted: !!out.adopted,
            length: msgs.length,
            answers: msgs.filter(m => m && m.role === 'assistant').map(m => String(m.content || '')),
            carriedUsage: msgs.some(m => !!(m && m._turnUsage)),
        }}));
        """
    )


@pytest.mark.skipif(NODE is None, reason="node required for behavioural test")
def test_empty_snapshot_cannot_blank_the_transcript():
    """`done` with `session.messages: []` must keep the streamed final answer."""
    result = _run(
        json.dumps([
            {"role": "user", "content": "the prompt", "_ts": 1},
            {"role": "assistant", "content": ANSWER, "_ts": 2, "_turnUsage": {"output_tokens": 42}},
        ]),
        json.dumps({"session_id": "s1", "messages": []}),
    )
    assert result["adopted"] is False, "an empty settled snapshot must not be adopted"
    assert result["length"] == 2, "the visible transcript must survive the settle"
    assert ANSWER in result["answers"], "the streamed final answer must still be present"


@pytest.mark.skipif(NODE is None, reason="node required for behavioural test")
def test_missing_snapshot_field_cannot_blank_the_transcript():
    """`done` with no `messages` key at all (journal replay shape) must be inert."""
    result = _run(
        json.dumps([
            {"role": "user", "content": "the prompt", "_ts": 1},
            {"role": "assistant", "content": ANSWER, "_ts": 2},
        ]),
        json.dumps({"session_id": "s1"}),
    )
    assert result["adopted"] is False
    assert result["length"] == 2
    assert ANSWER in result["answers"]


@pytest.mark.skipif(NODE is None, reason="node required for behavioural test")
def test_absent_done_session_cannot_blank_the_transcript():
    """A `done` frame with no session object must not throw or wipe the pane."""
    result = _run(
        json.dumps([
            {"role": "user", "content": "the prompt", "_ts": 1},
            {"role": "assistant", "content": ANSWER, "_ts": 2},
        ]),
        "null",
    )
    assert result["adopted"] is False
    assert result["length"] == 2
    assert ANSWER in result["answers"]


@pytest.mark.skipif(NODE is None, reason="node required for behavioural test")
def test_non_empty_snapshot_still_wins_and_carries_ephemeral_fields():
    """Server-wins reconciliation must be unchanged for a real settled snapshot."""
    result = _run(
        json.dumps([
            {"role": "user", "content": "the prompt", "_ts": 1},
            {"role": "assistant", "content": ANSWER, "_ts": 2, "_turnUsage": {"output_tokens": 42}},
        ]),
        json.dumps({
            "session_id": "s1",
            "messages": [
                {"role": "user", "content": "the prompt", "_ts": 1},
                {"role": "assistant", "content": ANSWER, "_ts": 2},
            ],
        }),
    )
    assert result["adopted"] is True, "a populated settled snapshot must still be adopted"
    assert result["length"] == 2
    assert ANSWER in result["answers"]
    assert result["carriedUsage"] is True, (
        "ephemeral turn fields must still be carried onto the adopted snapshot"
    )


@pytest.mark.skipif(NODE is None, reason="node required for behavioural test")
def test_fresh_session_is_not_pinned_to_a_stale_transcript():
    """The guard must not invent content: with nothing visible, nothing is kept."""
    result = _run("[]", json.dumps({"session_id": "s1", "messages": []}))
    assert result["adopted"] is False, (
        "an empty snapshot is never adopted, even with an empty visible transcript"
    )
    assert result["length"] == 0
    assert result["answers"] == []


# --------------------------------------------------------------------------- #
# The streamed final answer itself must survive settlement (#6112 invariant:
# "settlement must not leave the transcript empty until reload").
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(NODE is None, reason="node required for behavioural test")
def test_snapshot_missing_the_streamed_answer_put_it_back():
    """Context-compression rotation: the snapshot is [user] only, no assistant turn.

    This is the exact reported shape — "only the submitted user message followed by a
    collapsed Context compaction row, with no final answer". The answer existed live
    and on the server, but the settled snapshot that arrives at `done` never received
    the in-flight assistant turn, so adopting it verbatim discarded the answer.
    """
    result = _run(
        json.dumps([{"role": "user", "content": "the prompt", "_ts": 1}]),
        json.dumps({
            "session_id": "s1",
            "messages": [
                {"role": "user", "content": "the prompt", "_ts": 1},
                {"role": "assistant", "content": "[your active task list was preserved across context compression]", "_ts": 2},
            ],
        }),
        final_answer=ANSWER,
    )
    assert result["length"] == 3, "the streamed answer must be re-attached to the tail"
    assert result["answers"][-1] == ANSWER, "the final answer must be the last message"


@pytest.mark.skipif(NODE is None, reason="node required for behavioural test")
def test_snapshot_that_already_has_the_answer_is_not_duplicated():
    """The healthy `done` payload must settle exactly as it does today."""
    result = _run(
        json.dumps([{"role": "user", "content": "the prompt", "_ts": 1}]),
        json.dumps({
            "session_id": "s1",
            "messages": [
                {"role": "user", "content": "the prompt", "_ts": 1},
                {"role": "assistant", "content": ANSWER, "_ts": 2},
            ],
        }),
        final_answer=ANSWER,
    )
    assert result["length"] == 2, "the answer must not be appended twice"
    assert result["answers"].count(ANSWER) == 1


@pytest.mark.skipif(NODE is None, reason="node required for behavioural test")
def test_snapshot_storing_more_text_than_was_streamed_is_recognised():
    """The stored answer can be longer than the streamed display text (markup, cleanup)."""
    result = _run(
        json.dumps([{"role": "user", "content": "the prompt", "_ts": 1}]),
        json.dumps({
            "session_id": "s1",
            "messages": [
                {"role": "user", "content": "the prompt", "_ts": 1},
                {"role": "assistant", "content": ANSWER + "\n\nExtra post-processing footer.", "_ts": 2},
            ],
        }),
        final_answer=ANSWER,
    )
    assert result["length"] == 2, "a superset snapshot must not trigger the fallback"
    assert any(ANSWER in a for a in result["answers"]), (
        "the stored answer must remain the one that is rendered"
    )


@pytest.mark.skipif(NODE is None, reason="node required for behavioural test")
def test_no_streamed_answer_leaves_a_populated_snapshot_alone():
    """A cancelled/errored turn with no streamed text keeps the snapshot verbatim."""
    result = _run(
        json.dumps([{"role": "user", "content": "the prompt", "_ts": 1}]),
        json.dumps({
            "session_id": "s1",
            "messages": [
                {"role": "user", "content": "the prompt", "_ts": 1},
                {"role": "assistant", "content": "partial text that was persisted", "_ts": 2},
            ],
        }),
        final_answer=None,
    )
    assert result["length"] == 2
    assert result["answers"] == ["partial text that was persisted"]
