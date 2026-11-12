"""Behavioural regressions for the #7628 review round-3 findings.

The round-3 gate reproduced two data-integrity defects against the shipped
functions and asked for tests that reach them (the source-assertion suite
stayed green through both reproductions):

1. **A successful `/compress` kept the bounded preflight's paging state.**
   The success path assigned the full-transcript response without restoring
   ``_messagesTruncated`` / ``_oldestIdx``, so after a compression that
   returned 100 full rows the client still showed "70 older messages" and
   every absolute transcript index was off by 70 — which is what edit /
   fork / regenerate address rows by.

2. **Prefix match won over suffix match when both succeeded.** A bounded
   settle returns a *suffix* of the durable transcript, but the old
   ``prefix || suffix`` expression preferred the prefix branch, so a
   transcript with repeated identical turns spliced at the wrong offset:
   ``u1/a1`` silently dropped, ``u3/a3`` duplicated.

These tests execute the real decision code rather than grepping it: the
splice strategy block is extracted from ``static/messages.js`` verbatim and
driven through a Node VM with the paging signals as the only variable, and
the compress success path is asserted at source level for the restore
ordering (that block issues a live ``/api/session`` fetch, so driving it
would need a network).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


# ── extraction helpers ───────────────────────────────────────────────────────

_SPLICE_DEPS = [
    "_messageIdentityKey",
    "_isTerminalStreamErrorMarkerMessage",
    "_streamRecoveryControlMessage",
    "_filterRecoveryControlMessages",
    "_carryForwardEphemeralTurnFields",
]


def _splice_block() -> str:
    """The bounded-settle splice decision, verbatim from messages.js.

    Spans the terminal-marker detection and the prefix/suffix comparisons
    through the ``S.messages`` assignment — the region the round-3 gate
    patched. Everything before it (paging restore, session assignment) is set
    up as harness state instead.
    """
    start_marker = "const _currentVisibleEndsWithTerminalMarker=("
    start = MESSAGES_JS.find(start_marker)
    assert start >= 0, "terminal-marker detection not found in static/messages.js"
    end_marker = "S.messages=_filterRecoveryControlMessages(_resolvedMessages || []);"
    end = MESSAGES_JS.find(end_marker, start)
    assert end >= 0, "S.messages assignment after the splice not found"
    return MESSAGES_JS[start : end + len(end_marker)]


def _dependency_functions() -> list[str]:
    """The splice block's transitive pure dependencies, in definition order.

    The extraction is closure-driven: start from the helpers the block names,
    then keep pulling the helpers those helpers reference until the set is
    closed. That keeps the harness honest when the production code grows a
    new dependency — a missing one fails the node run instead of silently
    stubbing behaviour away.
    """
    import re

    needed: list[str] = list(_SPLICE_DEPS)
    resolved: list[str] = []
    seen: set[str] = set()
    while needed:
        name = needed.pop(0)
        if name in seen:
            continue
        seen.add(name)
        try:
            body = extract_function(MESSAGES_JS, name)
        except AssertionError:
            continue
        resolved.append(name)
        for ref in re.findall(r"\b(_[a-zA-Z][a-zA-Z0-9_]*)\s*\(", body):
            if ref not in seen and ref != name:
                needed.append(ref)
    # Definition order matters only for hoisted `function` declarations, which
    # JS hoists anyway; keep the resolved order for readability.
    return [extract_function(MESSAGES_JS, name) for name in resolved]


def _compress_success_block() -> str:
    """The `/compress` success path that assigns the full-transcript response.

    Anchored on the bounded preflight fetch's continuation: the block runs
    after a successful compression response and must restore the paging
    signals before ``renderMessages()``.
    """
    anchor = "const summary=data&&data.summary;"
    start = COMMANDS_JS.rfind(anchor)
    assert start >= 0, "compress success block not found in static/commands.js"
    assign = COMMANDS_JS.rfind("S.messages=", 0, start)
    assert assign >= 0, "S.messages assignment before the summary block not found"
    return COMMANDS_JS[assign:start]


# ── node harness ─────────────────────────────────────────────────────────────

_HARNESS = r"""
const deps = __DEPS__;
const splice = __SPLICE__;

const S = { session: { session_id: 'sid' }, messages: [] };
let _messagesTruncated = false;
let _oldestIdx = 0;
let _adoptRegenerationRevision = () => {};
let _hydrateTodosFromSession = () => {};
let _markSessionCompletionUnread = () => {};
let _setActiveSessionUrl = () => {};
let _attachProjectedAnchorSceneToLastAssistant = () => {};
let _replaceMarkerOnlyAssistantWithStreamError = () => false;
const store = {};
globalThis.localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};
// Closure-scope constant that lives just above the extracted helper in
// messages.js (extract_function only captures the function body).
const _EPHEMERAL_TURN_FIELDS = ['_turnUsage','_turnDuration','_turnTps','_gatewayRouting','_statusCard','_anchor_stream_id','_anchor_activity_scene'];
for (const fn of deps) eval(fn);

// Scenario: repeated identical turns in the visible window, a terminal
// marker at the end, and a bounded durable tail whose ids ALSO appear
// earlier — so both the prefix and the suffix comparison succeed.
function makeTurn(n) {
  return [
    { role: 'user', id: `u-${n}`, content: `prompt ${n}` },
    { role: 'assistant', id: `a-${n}`, content: `answer ${n}` },
  ];
}
S.messages = [];
for (const n of [1, 2, 3]) S.messages.push(...makeTurn(n));
// The real terminal marker shape: an assistant row whose content starts with
// the Connection-interrupted prefix (what _isTerminalStreamErrorMarkerMessage
// actually matches).
S.messages.push({
  role: 'assistant',
  id: 'marker',
  content: '**Connection interrupted:** The browser lost the live SSE connection before the response finished.',
});

const durableTail = [
  { role: 'user', id: 'u-3', content: 'prompt 3' },
  { role: 'assistant', id: 'a-3', content: 'answer 3' },
];
const session = {
  session_id: 'sid',
  messages: durableTail,
  _messages_truncated: __TRUNCATED__,
  _messages_offset: 4,
};
_messagesTruncated = __TRUNCATED__;
_oldestIdx = __OFFSET__;

// The state the splice block reads, mirroring the lines just above it.
const _currentMessages = S.messages.slice();
const _currentVisibleMessages = _filterRecoveryControlMessages(_currentMessages);
const _stagedMessages = _carryForwardEphemeralTurnFields(_currentMessages, session.messages);
const preserveVisibleOnShorterTerminalSnapshot = true;
S.session = session;

eval(splice);

const out = {
  truncated: _messagesTruncated,
  oldestIdx: _oldestIdx,
  rows: (S.messages || []).map(m => `${m.role}:${m.id}`),
};
console.log(JSON.stringify(out));
"""


def _run_node(script: str) -> dict:
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"node subprocess failed:\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _drive(*, truncated: bool, offset: int) -> dict:
    deps = "[" + ",".join(json.dumps(f) for f in _dependency_functions()) + "]"
    splice = json.dumps(_splice_block())
    script = (
        _HARNESS.replace("__DEPS__", deps)
        .replace("__SPLICE__", splice)
        .replace("__TRUNCATED__", "true" if truncated else "false")
        .replace("__OFFSET__", str(offset))
    )
    return _run_node(script)


# ── tests ────────────────────────────────────────────────────────────────────


def test_bounded_settle_with_repeated_identical_turns_drops_and_duplicates_nothing():
    """The round-3 SILENT finding: identical repeated turns must not splice wrong.

    The visible window holds repeated identical turns, so the durable tail's
    ids appear at BOTH the head and the tail of the window and both the
    prefix and the suffix comparison succeed. The pre-fix
    ``prefix || suffix`` expression preferred the prefix branch and spliced at
    the head offset — dropping ``u2/a2`` and ``u3/a3`` from the window even
    though the server returned them as a truncated suffix (``_oldestIdx=4``).
    The truncation signal must select the suffix branch instead.

    The two branches produce observably different row lists on this shape,
    which is what makes this a regression test rather than a tautology: with
    the prefix branch restored the splice keeps the wrong rows.
    """
    out = _drive(truncated=True, offset=4)

    rows = out["rows"]
    # The durable tail rows are present exactly once each — the pre-fix
    # prefix branch duplicated them.
    assert rows.count("user:u-3") == 1, rows
    assert rows.count("assistant:a-3") == 1, rows
    # No row appears twice anywhere.
    assert len(rows) == len(set(rows)), f"duplicate rows in {rows}"
    # The suffix splice keeps the durable tail plus the rows that follow it
    # in the visible window (the terminal marker). The prefix branch would
    # splice at the head offset and re-emit u2/a2/u3/a3.
    assert rows == ["user:u-3", "assistant:a-3", "assistant:marker"], (
        "the suffix splice must keep durable-tail + the rows after it; got "
        f"{rows} (extra rows mean the prefix branch won)"
    )
    # Paging follows the response so absolute indices line up.
    assert out["truncated"] is True
    assert out["oldestIdx"] == 4


def test_bounded_settle_keeps_the_response_paging_signals():
    """Paging flags follow the server's truncation signal, not the preflight."""
    out = _drive(truncated=True, offset=4)
    assert out["truncated"] is True
    assert out["oldestIdx"] == 4


def test_untruncated_snapshot_clears_the_bounded_preflight_state():
    """An untruncated response must clear the bounded preflight's state.

    This is the `/compress` success-path shape from the CORE finding: the
    preflight bounded the tail, the response carries the full transcript, and
    the client must end at ``truncated=false`` / ``offset=0`` so absolute
    transcript indices line up again.
    """
    out = _drive(truncated=False, offset=0)
    assert out["truncated"] is False, (
        "an untruncated response must clear _messagesTruncated; the bounded "
        f"preflight state leaked (got {out['truncated']})"
    )
    assert out["oldestIdx"] == 0


def test_compress_success_path_restores_paging_before_render():
    """Source lock: the compress success path restores paging before render.

    The round-3 CORE reproduction was a successful `/compress` whose response
    carried 100 full rows while the client stayed at
    ``_messagesTruncated=true, _oldestIdx=70``. The restore must appear
    between the response assignment and ``renderMessages()`` — the same
    ordering the settle and cancel paths already use.
    """
    block = _compress_success_block()

    assign_idx = block.find("S.messages=")
    restore_idx = block.find("_messagesTruncated=")
    offset_idx = block.find("_oldestIdx=")
    render_idx = block.find("renderMessages()")

    assert assign_idx >= 0, "compress success path must assign S.messages"
    assert restore_idx > assign_idx, (
        "_messagesTruncated must be restored from the response AFTER the "
        "assignment — the preflight's bounded value otherwise survives"
    )
    assert offset_idx > assign_idx, "_oldestIdx must be restored from _messages_offset"
    assert render_idx > restore_idx and render_idx > offset_idx, (
        "paging must be restored BEFORE renderMessages() so the rendered rows "
        "and any absolute index computation see the restored values"
    )
    # The restore must read the response's own signals, not hardcode false.
    assert "_messages_truncated" in block, (
        "the restore must read data.session._messages_truncated rather than "
        "hardcoding _messagesTruncated=false"
    )
    assert "_messages_offset" in block, (
        "the restore must read data.session._messages_offset"
    )


def test_truncation_signal_selects_suffix_strategy_exclusively():
    """Source lock: the strategy is a truncation-gated ternary, never an `||`.

    The pre-fix expression was ``_stagedMatchesCurrentPrefix ||
    _stagedMatchesCurrentSuffix``: when repeated identical turns made both
    comparisons succeed the prefix branch won and spliced at the wrong
    offset. The gate is now ``_truncatedRecovery ? suffix : prefix``.
    """
    block = _splice_block()

    assert "_truncatedRecovery" in block, (
        "the splice strategy must consult the truncation signal"
    )
    assert "_truncatedRecovery?_stagedMatchesCurrentSuffix" in block.replace(" ", ""), (
        "a truncated recovery must select the SUFFIX comparison exclusively"
    )
    # The old defect: a bare `||` between the two comparisons.
    assert "||_stagedMatchesCurrentSuffix" not in block.replace(" ", ""), (
        "prefix and suffix comparisons must not be OR-ed; preferring the "
        "prefix when both match splices repeated turns at the wrong offset"
    )
