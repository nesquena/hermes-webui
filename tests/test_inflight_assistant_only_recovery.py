"""Regression tests for assistant-only journal recovery (#6649 review 2026-10-05).

The real HTTP journal projection (api/routes.py) clears ``messages`` whenever
``last_assistant_text`` is present, so the recovered INFLIGHT snapshot is
assistant-only (no user row). These tests exercise the production composition
without manually injecting a user: the active turn's authoritative pending
prompt identity is recovered from the session, and the timestamp-only boundary
guard keeps a previous turn's repeated answer from claiming the current turn.
"""
import json
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_decl(src, name):
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"{name}() not found"
    brace = src.find("){", start)
    assert brace != -1, f"{name}() body not found"
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


def _reattach_helper_src():
    start = SESSIONS_JS.find("function _messageComparableText")
    end = SESSIONS_JS.find("// Load older messages", start)
    assert start != -1 and end != -1
    return SESSIONS_JS[start:end]


_PENDING_STUB = """\
function getPendingSessionMessage(session){
  const text = String(session && session.pending_user_message || '').trim();
  if(!text) return null;
  return { role:'user', content:text, _ts:session.pending_started_at, _pending:true };
}
"""


def _run_node(script):
    assert NODE, "node not on PATH"
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_assistant_only_journal_recovery_preserves_current_live_reply():
    """Finding 1: an assistant-only snapshot carries no user row, so the current
    turn's pending prompt identity must be recovered from the session BEFORE
    prepare/merge. Without it the current live reply that repeats a previous
    turn's answer ("Done.") is text-deduped away and the turn vanishes."""
    body = """\
const assert = require('assert');

// Previous turn settled in the base; the current turn lives ONLY in the
// assistant-only journal snapshot (no user row) + the session's pending identity.
let base = [
  {role:'user', content:'old question'},
  {role:'assistant', content:'Done.'},
];
let inflight = [
  {role:'assistant', _live:true, content:'current work'},
];
let session = { pending_user_message:'current question', pending_started_at:2, active_stream_id:'s1' };
let prepared = _prepareRunningLiveTail(base, inflight, session);

// Assistant-only recovery threaded the authoritative current user in.
assert.strictEqual(inflight.length, 2, 'recovered user threaded before the live assistant: ' + JSON.stringify(inflight.map(m => m.role)));
assert.strictEqual(inflight[0].role, 'user');
assert.strictEqual(inflight[0].content, 'current question');
assert.ok(inflight[1]._live);

// The previous turn's answer must NOT be treated as the current turn.
assert.strictEqual(prepared, false, 'previous-turn answer is not current-turn ownership');
if(prepared){ base = _dropCurrentTurnAssistantMessages(base); }
let merged = _mergeInflightTailMessages(base, inflight);
let rows = merged.map(m => m.role + ':' + m.content);
assert.strictEqual(rows.length, 4, 'expected [old, Done., current question, current work]: ' + JSON.stringify(rows));
assert.strictEqual(rows[1], 'assistant:Done.');
assert.strictEqual(rows[3], 'assistant:current work');
assert.ok(merged[3]._live, 'the current reply must stay the live row');
"""
    _run_node(_PENDING_STUB + _reattach_helper_src() + "\n" + body)


def test_assistant_only_previous_answer_repeated_by_current_reply_is_not_hidden():
    """Finding 2: old UNSTAMPED `again` + old settled `previous answer`, then a
    current timestamped `again` with live `current work`. Text equality must not
    reclassify the previous turn's answer as the current turn's — `current work`
    must survive (timestamp-only boundary guard)."""
    body = """\
const assert = require('assert');

// Old UNSTAMPED 'again' turn, THEN the user resubmits 'again' (timestamped) ->
// current live 'current work'. The recovered current user is stamped with the
// pending_started_at identity.
let base = [
  {role:'user', content:'again'},
  {role:'assistant', content:'previous answer'},
];
let session = { pending_user_message:'again', pending_started_at:2, active_stream_id:'s1' };
let inflight = [
  {role:'assistant', _live:true, content:'current work'},
];
let prepared = _prepareRunningLiveTail(base, inflight, session);
assert.strictEqual(inflight.length, 2, 'recovered current user threaded in');
assert.strictEqual(inflight[0]._ts, 2, 'recovered user carries the pending_started_at identity');
// The old unstamped 'again' (base) must NOT dedup against the current stamped
// 'again' -> the previous settled answer is NOT the current turn.
assert.strictEqual(prepared, false, 'timestamp-only repeat must not claim current-turn ownership');
if(prepared){ base = _dropCurrentTurnAssistantMessages(base); }
let merged = _mergeInflightTailMessages(base, inflight);
let rows = merged.map(m => m.role + ':' + m.content);
assert.strictEqual(rows.length, 4, 'expected 4 rows incl. current work: ' + JSON.stringify(rows));
assert.strictEqual(rows[3], 'assistant:current work');
assert.ok(merged[3]._live, 'current work stays live');
"""
    _run_node(_PENDING_STUB + _reattach_helper_src() + "\n" + body)


def test_assistant_only_same_turn_prefix_reconciles_with_identity():
    """Finding 1 positive composition: when the base ALREADY carries the current
    turn's user (authoritative identity proves same-turn ownership), an
    assistant-only snapshot whose live text is a strict extension of the
    persisted current-turn prefix is progress — the full live text survives."""
    body = (
        "const assert = require('assert');\n"
        "\n"
        "// Base carries the CURRENT turn's authoritative user (timestamp ==\n"
        "// pending_started_at via the start-sync) + the eager persisted prefix.\n"
        "let base = [\n"
        "  {role:'user', content:'export the data', timestamp:100},\n"
        "  {role:'assistant', content:'Preparing export', timestamp:101},\n"
        "];\n"
        "let session = { pending_user_message:'export the data', pending_started_at:100, active_stream_id:'s1' };\n"
        "let inflight = [\n"
        "  {role:'assistant', _live:true, content:'Preparing export\\n\\nExport is ready'},\n"
        "];\n"
        "let prepared = _prepareRunningLiveTail(base, inflight, session);\n"
        "assert.strictEqual(prepared, true, 'strict extension with proven same-turn identity is progress');\n"
        "assert.strictEqual(inflight[1].content, 'Preparing export\\n\\nExport is ready', 'full live text kept');\n"
        "if(prepared){ base = _dropCurrentTurnAssistantMessages(base); }\n"
        "let merged = _mergeInflightTailMessages(base, inflight);\n"
        "let rows = merged.map(m => m.role + ':' + m.content);\n"
        "assert.strictEqual(rows.length, 2, 'user + single live assistant: ' + JSON.stringify(rows));\n"
        "assert.strictEqual(rows[1], 'assistant:Preparing export\\n\\nExport is ready');\n"
    )
    _run_node(_PENDING_STUB + _reattach_helper_src() + "\n" + body)


def test_http_projection_assistant_only_snapshot_produces_no_user_row():
    """Pin the production precondition behind finding 1: the real HTTP journal
    projection clears `messages` when last_assistant_text is present, so
    _serverLiveSnapshotInflight reconstructs an assistant-only tail. Regression
    coverage exercises this exact shape without manually adding a user."""
    from api import routes

    snapshot = routes._runtime_journal_snapshot_for_session_payload(
        {
            "stream_id": "stream-1",
            "last_seq": 5,
            "last_event_id": "stream-1:5",
            "messages": [
                {"role": "user", "content": "hidden-user"},
                {"role": "assistant", "content": "current work", "_live": True, "_ts": 4.0},
            ],
            "last_assistant_text": "current work",
            "last_reasoning_text": "",
        }
    )
    assert snapshot.get("messages") == []
    script = "\n".join(
        [
            "const assert=require('assert');",
            _function_decl(SESSIONS_JS, "_serverLiveSnapshotToolId"),
            _function_decl(SESSIONS_JS, "_serverLiveSnapshotInflight"),
            f"const snapshot={json.dumps(snapshot)};",
            "const live = _serverLiveSnapshotInflight(snapshot, []);",
            "assert.ok(live, 'snapshot must reconstruct a live inflight');",
            "const users = (live.messages||[]).filter(m => m.role === 'user');",
            "assert.strictEqual(users.length, 0, 'projection yields an assistant-only tail: user rows cleared');",
            "assert.strictEqual(live.messages.length, 1);",
            "assert.strictEqual(live.messages[0].role, 'assistant');",
            "assert.strictEqual(live.messages[0].content, 'current work');",
        ]
    )
    _run_node(script)