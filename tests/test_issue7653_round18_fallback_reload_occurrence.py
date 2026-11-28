"""#7653 greptile P1 — reload-rebuilt ordinary cards keep their saved failure.

The 11-25 greptile finding (P1, 5 minutes after the round-17 head):

> **Failed calls show Completed**
> On reload without a saved activity scene, ``renderMessages()`` rebuilds
> ordinary ``tool_calls`` / ``tool_use`` cards without an ``occurrence``. Saved
> summaries now carry that field, so their failure entries have ``scopes``.
> This check then rejects the rebuilt cards because ``_rowOcc`` is ``null``.
> Even a single failed call loses its saved ``is_error`` and shows Completed.
> Carry the matching occurrence into these cards before requiring an exact
> scope.

The fix: while ``renderMessages()`` rebuilds the fallback cards from the
message transcript (OpenAI ``m.tool_calls`` and Anthropic ``m.content``
``tool_use`` branches), pair each rebuilt card with the occurrence its
persisted journal row names — the journal is indexed by ``<tid>@<owner>`` in
order, and the k-th rebuilt card of a coordinate takes the k-th journal
occurrence, so ``copyLiveToolMetadata``'s scoped check can prove ownership
instead of failing closed on ``_rowOcc == null``.

These tests drive the REAL ``renderMessages()`` fallback block out of
``static/ui.js`` in node (same harness as rounds 10/11/15), so they pin the
shipped rebuild+pairing rather than a restatement of it.

RED control: on the round-17 head (before this fix) the rebuilt cards carry
no occurrence, the scoped check rejects them (``_rowOcc`` null), and the
single failed call keeps ``is_error`` unset — every failure-preservation
assertion below fails.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _ui_fallback_driver() -> str:
    """The real ``renderMessages()`` fallback block — slice it verbatim.

    The block runs from the fallback result indexing through the rebuilt-card
    loop and the ``copyLiveToolMetadata`` transfer (the exact surface greptile
    P1 named). The enclosing closure reads ``S`` / ``anchorOwnedAssistantRawIdxs``
    / a few external helpers, so the harness binds those names itself and
    stubs the presentation helpers exactly like the round-11 driver.
    """
    src = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    start = src.index("const fallbackToolSources=[];") - len("    const resultsByTid={};\n")
    end_marker = "if(S._settledLiveToolMetadata) S._settledLiveToolMetadata=null;"
    end = src.index(end_marker, start) + len(end_marker)
    block = src[start:end]
    return f"""
var PAYLOAD = JSON.parse(require('fs').readFileSync(0, 'utf8') || '{{}}');
const S = {{
  messages: PAYLOAD.messages || [],
  session: PAYLOAD.session || {{}},
  _settledToolIsErrorByTid: PAYLOAD.verdictMap || null,
  _settledLiveToolMetadata: null,
  toolCalls: [],
}};
const anchorOwnedAssistantRawIdxs = new Set();
function _cliToolResultSnippet(x){{ return (typeof x === 'string') ? x : ''; }}
function _cliPatchSnippetFromArgs(){{ return ''; }}
function _cliToolCardSnippet(a, b){{ return a || b || ''; }}
function _cliToolCardHasDiffSnippet(){{ return false; }}
function _toolArgsSnapshot(a){{ return a || {{}}; }}
function _assistantToolAnchorIdxForMessage(messages, rawIdx){{
  // Minimal real semantics: skip back over non-user rows after the turn's
  // user message, mirroring the source helper's anchor resolution.
  const list = Array.isArray(messages) ? messages : [];
  for (let idx = rawIdx; idx >= 0; idx--) {{
    const m = list[idx];
    if (m && m.role === 'user') return idx + 1;
    if (m && (m.role === 'assistant')) return idx;
  }}
  return rawIdx;
}}
function _legacySettledFallbackHasToolMetadata(m){{
  return !!(
    (m && m.tool_calls && m.tool_calls.length) ||
    (Array.isArray(m && m.content) && m.content.some(p => p && p.type === 'tool_use')) ||
    (Array.isArray(m && m._partial_tool_calls) && m._partial_tool_calls.length)
  );
}}
{block}
process.stdout.write(JSON.stringify({{
  toolCalls: S.toolCalls,
  derivedOccurrences: (S.toolCalls || []).map(tc => tc && (tc.occurrence !== undefined ? tc.occurrence : null)),
}}));
"""


def _run(payload: dict) -> dict:
    assert NODE, "node not on PATH"
    result = subprocess.run(
        [NODE, "-e", _ui_fallback_driver()],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"node failed:\n{result.stderr[:2000]}"
    return json.loads(result.stdout)


def _asst_msg(*pairs) -> dict:
    """One assistant message with OpenAI-format tool_calls (possibly one id twice)."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tid,
                "type": "function",
                "function": {"name": "terminal", "arguments": json.dumps({"command": cmd})},
            }
            for tid, cmd in pairs
        ],
    }


def _tool_result(tid: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tid, "content": content}


# ---------------------------------------------------------------------------
# greptile P1: a reload WITHOUT a saved scene rebuilds the card from the
# transcript; the saved summary scopes its failure by owner#occurrence; the
# rebuilt card must carry the journal occurrence so the scope check can hit.
# ---------------------------------------------------------------------------


def test_single_failed_call_keeps_is_error_after_sceneless_reload():
    """The P1 repro: one failed call_0, no saved activity scene.

    journal (session.tool_calls) carries occurrence 0 + is_error true; the
    verdict map scopes by '4#0'. The rebuilt card MUST pair occurrence 0 and
    inherit the failure. Before the fix the rebuilt card had no occurrence,
    the scoped check rejected it (``_rowOcc`` null) and the card rendered
    "Completed".
    """
    out = _run(
        {
            "messages": [
                {"role": "user", "content": "run"},
                _asst_msg(("call_0", "exit 3")),
                _tool_result("call_0", "boom"),
            ],
            "session": {
                "tool_calls": [
                    {"tid": "call_0", "assistant_msg_idx": 1, "occurrence": 0, "is_error": True, "snippet": "boom"},
                ],
            },
            "verdictMap": {"call_0": {"assistant_msg_idx": 1, "is_error": True, "scopes": {"1#0": True}}},
        }
    )
    cards = out["toolCalls"]
    assert len(cards) == 1, f"expected one rebuilt card, got {len(cards)}"
    assert cards[0].get("occurrence") == 0, (
        f"rebuilt card must carry the journal occurrence, got {cards[0].get('occurrence')!r}"
    )
    assert cards[0].get("is_error") is True, (
        "the saved failure must survive the sceneless reload (Failed, not Completed); "
        f"card={json.dumps(cards[0])}"
    )


def test_single_failed_call_keeps_is_error_anthropic_tool_use():
    """The same P1 repro through the Anthropic ``tool_use`` rebuild branch."""
    out = _run(
        {
            "messages": [
                {"role": "user", "content": "run"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "call_0", "name": "terminal", "input": {"command": "exit 3"}}
                    ],
                },
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_0", "content": "boom"}]},
            ],
            "session": {
                "tool_calls": [
                    {"tid": "call_0", "assistant_msg_idx": 1, "occurrence": 0, "is_error": True, "snippet": "boom"},
                ],
            },
            "verdictMap": {"call_0": {"assistant_msg_idx": 1, "is_error": True, "scopes": {"1#0": True}}},
        }
    )
    cards = out["toolCalls"]
    assert len(cards) == 1, f"expected one rebuilt card, got {len(cards)}"
    assert cards[0].get("occurrence") == 0, (
        f"Anthropic rebuilt card must carry the journal occurrence, got {cards[0].get('occurrence')!r}"
    )
    assert cards[0].get("is_error") is True, (
        "the Anthropic-branch saved failure must also survive the sceneless reload"
    )


def test_journal_without_occurrence_uses_owner_level_fallback():
    """Old journal rows without an occurrence pair nothing (fail closed).

    A legacy summary that never recorded the occurrence builds no scopes (the
    round-17 map builder skips them), so the owner-level fallback in
    ``copyLiveToolMetadata`` must apply instead — an owner-only entry with a
    matching row index still upgrades, and an UNMATCHED owner applies nothing.
    """
    out = _run(
        {
            "messages": [
                {"role": "user", "content": "run"},
                _asst_msg(("call_0", "exit 3")),
                _tool_result("call_0", "boom"),
            ],
            "session": {
                "tool_calls": [
                    {"tid": "call_0", "assistant_msg_idx": 1, "is_error": True, "snippet": "boom"},
                ],
            },
            "verdictMap": {"call_0": {"assistant_msg_idx": 1, "is_error": True}},
        }
    )
    cards = out["toolCalls"]
    assert len(cards) == 1
    assert cards[0].get("is_error") is True, (
        "an owner-only persisted verdict must still upgrade the row that proves its owner"
    )


def test_single_failed_call_wrong_owner_applies_nothing():
    """The same shape whose owner does NOT match applies nothing (fail closed).

    Guards the pairing from ever painting a card whose owning assistant
    message cannot be proven to be the failing call's owner.
    """
    out = _run(
        {
            "messages": [
                {"role": "user", "content": "run"},
                _asst_msg(("call_0", "exit 3")),
                _tool_result("call_0", "boom"),
            ],
            "session": {
                "tool_calls": [
                    {"tid": "call_0", "assistant_msg_idx": 9, "occurrence": 0, "is_error": True, "snippet": "boom"},
                ],
            },
            "verdictMap": {"call_0": {"assistant_msg_idx": 9, "is_error": True, "scopes": {"9#0": True}}},
        }
    )
    cards = out["toolCalls"]
    assert len(cards) == 1
    assert cards[0].get("occurrence") is None, (
        "a journal occurrence owned by a different assistant message must NOT be paired"
    )
    assert cards[0].get("is_error") is not True, (
        "unprovable ownership applies no verdict (fail closed, no false red)"
    )


# ---------------------------------------------------------------------------
# Reused-id pair: one assistant message emitting call_0 twice — the k-th
# rebuilt card must take the k-th journal occurrence so a [success, failure]
# pair stays [success, failure] (round-16 item-3 shape, now through the
# sceneless rebuild path).
# ---------------------------------------------------------------------------


def test_reused_id_pairs_occurrences_in_order_success_then_failure():
    """Two occurrences of one id in ONE assistant message.

    journal carries occurrence 0 (success) and occurrence 1 (failure); the
    verdict map scopes only '1#1'. The k-th rebuilt card takes the k-th
    journal occurrence: card 0 -> occurrence 0 (no scope hit, stays
    successful), card 1 -> occurrence 1 (scope hit, Failed).
    """
    out = _run(
        {
            "messages": [
                {"role": "user", "content": "run"},
                _asst_msg(("call_0", "echo FIRST"), ("call_0", "exit 3")),
                _tool_result("call_0", "FIRST"),
                _tool_result("call_0", "3"),
            ],
            "session": {
                "tool_calls": [
                    {"tid": "call_0", "assistant_msg_idx": 1, "occurrence": 0, "is_error": False, "snippet": "FIRST"},
                    {"tid": "call_0", "assistant_msg_idx": 1, "occurrence": 1, "is_error": True, "snippet": "3"},
                ],
            },
            "verdictMap": {"call_0": {"assistant_msg_idx": 1, "is_error": True, "scopes": {"1#1": True}}},
        }
    )
    cards = out["toolCalls"]
    assert len(cards) == 2, f"two occurrences must rebuild two cards, got {len(cards)}"
    ocs = out["derivedOccurrences"]
    assert ocs == [0, 1], f"occurrences must pair in journal order, got {ocs}"
    by_occ = {cards[i].get("occurrence"): cards[i] for i in range(len(cards))}
    assert by_occ[0].get("is_error") is not True, (
        "the first occurrence (success) must NOT inherit the second's failure"
    )
    assert by_occ[1].get("is_error") is True, (
        "the second occurrence (failure) must stay Failed after sceneless reload"
    )