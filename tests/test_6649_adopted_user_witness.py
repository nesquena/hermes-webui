"""#6649 maintainer review 2026-10-10 — adopted-user witness + active-turn identity.

The blocking finding: ``_prepareRunningLiveTail`` resolves the pending prompt
against the loaded base transcript, ``getPendingSessionMessage`` ADOPTS the
row that is already there and returns ``null``, and the ``_alreadyInBase``
guard then skips the recovery WITHOUT carrying that adopted row forward as
the current turn's ownership witness. ``turnUser`` stays ``null``,
``settledBelongsToCurrentTurn`` stays false, ``_prepareRunningLiveTail``
returns false, the caller skips the conditional drop, and
``_mergeInflightTailMessages`` appends the fuller live assistant next to the
persisted prefix — the same turn renders TWO assistant rows.

The named acceptance work: ``_hasInflightTailUserDuplicate`` still used strict
timestamp equality and ignored the authoritative active-turn identity rules
master already uses in ``static/ui.js`` — the 1e-6 precision-only epsilon,
the ``_active_turn_user`` marker and the ``_active_turn_token``. Sub-µs float
drift (``100`` vs ``100.0000005``) on the user-bearing local recovery path
therefore still produced two rows.

These tests drive the REAL extracted components through the literal
loadSession recovery order (prepare -> conditional drop -> merge ->
mergePending), exactly as the review probe did, plus direct
``_hasInflightTailUserDuplicate`` unit shapes for the identity rules.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _node_env() -> dict:
    env = dict(os.environ)
    env.pop("NODE_CHANNEL_FD", None)
    env.pop("HERMES_WEBUI_TEST_STATE_DIR", None)
    return env


def _run_node(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        check=False,
        env=_node_env(),
    )


def _function_decl(src: str, name: str):
    marker = f"function {name}("
    start = src.find(marker)
    if start == -1:
        return None
    brace = src.find("){", start)
    if brace == -1:
        return None
    brace += 1
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name}() body did not close"
    return src[start:i]


def _reattach_helper_src() -> str:
    start = SESSIONS_JS.find("function _messageComparableText")
    end = SESSIONS_JS.find("// Load older messages", start)
    assert start != -1 and end != -1
    return SESSIONS_JS[start:end]


def _pending_resolver_src() -> str:
    """The REAL pending resolver (getPendingSessionMessage and everything it
    closes over) from ui.js, exactly as static/index.html loads them."""
    start = UI_JS.find("const _PENDING_ACTIVE_TURN_TS_EPSILON")
    assert start != -1, "the epsilon constant is gone from static/ui.js"
    end = UI_JS.find("async function checkInflightOnBoot", start)
    assert end != -1
    block = UI_JS[start:end]
    for name, src in (
        ("_sameTranscriptMessage", SESSIONS_JS),
        ("_currentTailUserMessage", SESSIONS_JS),
        ("_pendingCurrentTailUserMessage", UI_JS),
        ("_isContextCompactionMessage", UI_JS),
        ("_isContextCompactionText", UI_JS),
        ("_messageTimestampSeconds", UI_JS),
        ("_activeTurnTokenMatches", UI_JS),
        ("_pendingActiveTurnUserMessage", UI_JS),
    ):
        decl = _function_decl(src, name)
        if decl is not None:
            block = decl + "\n" + block
    return block


_FALLBACK_HELPERS = """
if (typeof msgContent !== 'function') {
  var msgContent = function (m) {
    if (!m) return '';
    if (Array.isArray(m.content)) {
      return m.content
        .filter(function (p) { return p && p.type === 'text'; })
        .map(function (p) { return String(p.text || ''); })
        .join('');
    }
    return m.content == null ? '' : String(m.content);
  };
}
"""

# The literal loadSession recovery order driven against the REAL extracted
# components (prepare -> conditional drop -> merge -> pending merge).
_DRIVER = """
function _driveCase(label, session, base, inflight) {
  var S = { session: session, messages: base };
  var prepared = _prepareRunningLiveTail(S.messages, inflight, S.session);
  if (prepared) S.messages = _dropCurrentTurnAssistantMessages(S.messages);
  S.messages = _mergeInflightTailMessages(S.messages, inflight);
  _mergePendingSessionMessage(S.session, S.messages);
  var rows = S.messages.map(function (m) {
    return String(m.role) + ':' + String(m.content == null ? '' : m.content);
  });
  var assistants = S.messages.filter(function (m) {
    return String(m.role) === 'assistant';
  }).length;
  return { label: label, prepared: prepared, assistants: assistants, rows: rows };
}
"""


def _harness(body: str) -> str:
    return "\n".join([
        "const assert = require('assert');",
        _reattach_helper_src(),
        _FALLBACK_HELPERS,
        _pending_resolver_src(),
        _DRIVER,
        _function_decl(SESSIONS_JS, "_hasInflightTailUserDuplicate") or "",
        _function_decl(SESSIONS_JS, "_prepareRunningLiveTail") or "",
        _function_decl(SESSIONS_JS, "_mergeInflightTailMessages") or "",
        _function_decl(SESSIONS_JS, "_mergePendingSessionMessage") or "",
        body,
    ])


def _run_payload(body: str) -> dict:
    out = _run_node(_harness(body))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


# 1. R0 — adopted-user prefix/full-live (the review's fixed blocking witness).
def test_adopted_user_prefix_full_live_reconciles_to_one_assistant():
    """Base holds user 'export the data' @100 + persisted assistant
    'Preparing export' @101; the journal is assistant-only with the fuller
    live 'Preparing export\\n\\nExport is ready'. The literal sequence must
    yield exactly ONE user + ONE fuller assistant."""
    body = """
const result = _driveCase(
  'R0_adopted_user_full_live',
  { session_id: 'sid-1', active_stream_id: 's1', pending_started_at: 100,
    pending_user_message: 'export the data', messages: [] },
  [
    { role: 'user', content: 'export the data', timestamp: 100 },
    { role: 'assistant', content: 'Preparing export', timestamp: 101 },
  ],
  [
    { role: 'assistant', content: 'Preparing export\\n\\nExport is ready', _live: true },
  ]
);
console.log(JSON.stringify(result));
assert.strictEqual(result.prepared, true,
  'prepared must be true: the adopted base row owns the current turn');
assert.strictEqual(result.assistants, 1,
  'one turn must render one assistant, not prefix + live copy: ' + JSON.stringify(result.rows));
assert.deepStrictEqual(result.rows, __EXPECTED__);
"""
    expected = [
        "user:export the data",
        "assistant:Preparing export\n\nExport is ready",
    ]
    payload = _run_payload(body.replace("__EXPECTED__", json.dumps(expected)))
    assert payload["rows"] == expected, payload["rows"]
    assert payload["assistants"] == 1, payload["rows"]
    assert payload["prepared"] is True, payload


# 2. U0 — user-bearing local recovery with sub-µs timestamp drift.
def test_user_bearing_sub_microsecond_drift_collapses_to_one_turn():
    """Inflight carries its own user row at 100.0000005 while the base row is
    at exactly 100 and pending_started_at is 100.0000005. Strict equality
    scores different turns (2 assistants); the precision-only epsilon scores
    the same turn (1 assistant, 1 user)."""
    body = """
const result = _driveCase(
  'U0_user_bearing_drift',
  { session_id: 'sid-2', active_stream_id: 's2',
    pending_started_at: 100.0000005, pending_user_message: 'export the data', messages: [] },
  [
    { role: 'user', content: 'export the data', timestamp: 100 },
    { role: 'assistant', content: 'Preparing export', timestamp: 101 },
  ],
  [
    { role: 'user', content: 'export the data', timestamp: 100.0000005 },
    { role: 'assistant', content: 'Preparing export\\n\\nExport is ready', _live: true },
  ]
);
console.log(JSON.stringify(result));
assert.strictEqual(result.prepared, true,
  'prepared must be true: drift inside the epsilon is still one turn');
assert.strictEqual(result.assistants, 1,
  'drifted same-prompt double stamp must render one assistant: ' + JSON.stringify(result.rows));
assert.strictEqual(result.rows.length, 2,
  'duplicate drifted user rows must collapse to one: ' + JSON.stringify(result.rows));
assert.deepStrictEqual(result.rows, __EXPECTED__);
"""
    expected = [
        "user:export the data",
        "assistant:Preparing export\n\nExport is ready",
    ]
    payload = _run_payload(body.replace("__EXPECTED__", json.dumps(expected)))
    assert payload["rows"] == expected, payload["rows"]
    assert payload["assistants"] == 1, payload["rows"]


# 3a. _active_turn_user marker, no timestamp.
def test_active_turn_user_marker_without_timestamp_dedupes():
    body = """
const existing = { role: 'user', content: 'export the data', _active_turn_user: true };
const candidate = { role: 'user', content: 'export the data' };
const dup = _hasInflightTailUserDuplicate([existing], candidate);
console.log(JSON.stringify({ dup }));
"""
    payload = _run_payload(body)
    assert payload["dup"] is True, (
        "the _active_turn_user marker makes the row authoritative — same text "
        "without any timestamp must dedupe"
    )


# 3b. _active_turn_token: equal collapses, different stays separate.
def test_active_turn_token_equality_decides_the_turn():
    body = """
const tokA = { role: 'user', content: 'go', _active_turn_token: 's1:100' };
const tokB = { role: 'user', content: 'go', _active_turn_token: 's1:100' };
const different = { role: 'user', content: 'go', _active_turn_token: 's2:200' };
const sameTurn = _hasInflightTailUserDuplicate([tokA], tokB);
const otherTurn = _hasInflightTailUserDuplicate([tokA], different);
console.log(JSON.stringify({ sameTurn, otherTurn }));
"""
    payload = _run_payload(body)
    assert payload["sameTurn"] is True, (
        "equal active_turn_token + equal text is the same turn"
    )
    assert payload["otherTurn"] is False, (
        "a different active_turn_token is a different turn even for identical "
        "text (distinct-turn protection)"
    )


# 4. Distinct-ID protection: never collapse two differently-id'ed rows.
def test_distinct_ids_never_collapse_even_with_matching_text():
    body = """
const a = { id: 'm1', role: 'user', content: 'go', _ts: 100 };
const b = { id: 'm2', role: 'user', content: 'go', _ts: 100 };
const same = { id: 'm1', role: 'user', content: 'go', _ts: 99.5 };
console.log(JSON.stringify({
  distinct: _hasInflightTailUserDuplicate([a], b),
  repeated: _hasInflightTailUserDuplicate([a], same),
}));
"""
    payload = _run_payload(body)
    assert payload["distinct"] is False, "distinct ids must never match"
    assert payload["repeated"] is True, "the same id repeated is the same row"


# 5. Controls that must keep passing (SHOULD-FIX + epsilon bounds).
def test_asymmetric_stamp_without_completed_answer_is_one_turn():
    body = """
const existing = { role: 'user', content: 'go' };
const candidate = { role: 'user', content: 'go', _ts: 1234.5 };
console.log(JSON.stringify({ dup: _hasInflightTailUserDuplicate([existing], candidate) }));
"""
    payload = _run_payload(body)
    assert payload["dup"] is True


def test_asymmetric_stamp_across_a_completed_answer_stays_two_rows():
    body = """
const candidate = { role: 'user', content: 'go', _ts: 1234.5 };
const list = [
  { role: 'user', content: 'go' },
  { role: 'assistant', content: 'previous answer' },
];
console.log(JSON.stringify({ dup: _hasInflightTailUserDuplicate(list, candidate) }));
"""
    payload = _run_payload(body)
    assert payload["dup"] is False


def test_whole_second_drift_is_a_different_turn():
    body = """
const existing = { role: 'user', content: 'go', _ts: 1000.0 };
const candidate = { role: 'user', content: 'go', _ts: 1001.0 };
console.log(JSON.stringify({ dup: _hasInflightTailUserDuplicate([existing], candidate) }));
"""
    payload = _run_payload(body)
    assert payload["dup"] is False


def test_exact_same_timestamp_still_matches():
    body = """
const existing = { role: 'user', content: 'go', _ts: 1000.0 };
const candidate = { role: 'user', content: 'go', _ts: 1000.0 };
console.log(JSON.stringify({ dup: _hasInflightTailUserDuplicate([existing], candidate) }));
"""
    payload = _run_payload(body)
    assert payload["dup"] is True


# 6. Literal same-text 'Done.' case: the current reply must not be hidden by
#    the recovery even though it repeats a previous turn's answer text.
def test_literal_same_text_done_is_not_hidden():
    body = """
const result = _driveCase(
  'same_text_done_previous_turn',
  { session_id: 's', active_stream_id: 's1', pending_started_at: 2,
    pending_user_message: 'go', messages: [] },
  [
    { role: 'user', content: 'previous question' },
    { role: 'assistant', content: 'Done.' },
  ],
  [
    { role: 'assistant', content: 'Done.', _live: true },
  ]
);
console.log(JSON.stringify(result));
assert.deepStrictEqual(result.rows, __EXPECTED__,
  'previous settled turn + current recovered turn: ' + JSON.stringify(result.rows));
"""
    expected = [
        "user:previous question",
        "assistant:Done.",
        "user:go",
        "assistant:Done.",
    ]
    payload = _run_payload(body.replace("__EXPECTED__", json.dumps(expected)))
    assert payload["rows"] == expected, payload["rows"]
    assert payload["assistants"] == 2, payload["rows"]


# 7. Deferred-save / no-pending control.
def test_deferred_save_no_pending_is_harmless():
    body = """
const result = _driveCase(
  'deferred_no_pending',
  { session_id: 's', pending_user_message: '', messages: [] },
  [
    { role: 'user', content: 'q1' },
    { role: 'assistant', content: 'a1' },
  ],
  [
    { role: 'user', content: 'q2' },
    { role: 'assistant', content: 'a2', _live: true },
  ]
);
console.log(JSON.stringify(result));
assert.deepStrictEqual(result.rows, __EXPECTED__);
"""
    expected = ["user:q1", "assistant:a1", "user:q2", "assistant:a2"]
    payload = _run_payload(body.replace("__EXPECTED__", json.dumps(expected)))
    assert payload["rows"] == expected, payload["rows"]


# 8. Distinct-prompt control: never collapse the pending prompt to a previous
#    turn's different text.
def test_distinct_prompt_is_never_collapsed_to_the_pending_prompt():
    body = """
const result = _driveCase(
  'distinct_prompt_stays_separate',
  { session_id: 's', active_stream_id: 's1', pending_started_at: 50,
    pending_user_message: 'b', messages: [] },
  [
    { role: 'user', content: 'a', timestamp: 10 },
    { role: 'assistant', content: 'answer-a', timestamp: 11 },
  ],
  [
    { role: 'assistant', content: 'answer-b', _live: true },
  ]
);
console.log(JSON.stringify(result));
assert.deepStrictEqual(result.rows, __EXPECTED__);
"""
    expected = ["user:a", "assistant:answer-a", "user:b", "assistant:answer-b"]
    payload = _run_payload(body.replace("__EXPECTED__", json.dumps(expected)))
    assert payload["rows"] == expected, payload["rows"]