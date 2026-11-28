"""#7681 re-gate — the three greptile inline findings on head ``5b4dd815b``.

The 2026-11-11 inline review left two P1s in ``api/compression_anchor.py``
and one P2 in ``static/sessions.js``:

1. **String parts break session listing.** A stored list containing a string
   part (``["hello"]``) made ``_plaintext_content_join_sql()`` raise
   ``malformed JSON``: ``JSON_EACH`` returns the part as unquoted text, and
   the old per-part expression fed it straight into ``JSON_EXTRACT``. Even an
   old message OUTSIDE the requested window could therefore break the whole
   external-session listing and hide conversations.

2. **Sidebar reads all message history.** The grouped decode table scanned
   and copied all of ``messages`` before joining the requested sessions — the
   checked plan was ``MATERIALIZE m`` + ``SCAN mq`` despite
   ``idx_messages_session`` — so even a single-session lookup decoded every
   stored message.

3. **Unknown counts become zero.** ``_mergeOptimisticFirstTurnSessions()``
   turned the server's unknown count (``null``) into ``0`` via
   ``Number(null)`` and persisted it once a streaming row went idle, so a
   single-segment legacy session displayed "0 user turns".

Each test below is revert-sensitive: restoring the head versions of
``api/compression_anchor.py`` / ``api/agent_sessions.py`` /
``static/sessions.js`` makes it fail.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0,
    reason="node not on PATH",
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# The agent's own sentinel encoding for list (multimodal) content: a NUL,
# ``json:`` and the ASCII-escaped JSON of the part list
# (hermes_state SessionDB._encode_content).
_SENTINEL = "\x00json:"


def _encode_content(parts: list) -> str:
    import json

    return _SENTINEL + json.dumps(parts, ensure_ascii=True)


def _state_db_with_string_part(path: Path) -> None:
    """A db whose NEWEST session is wanted and whose older session carries the
    string-part row that used to raise ``malformed JSON``."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, session_source TEXT,
            title TEXT, model TEXT, started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0, parent_session_id TEXT,
            ended_at REAL, end_reason TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            timestamp REAL, _compressed_summary INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL)",
        ("wanted", "tui", "tui", "Wanted", "m", 100.0, 2),
    )
    conn.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?,0)",
        ("w1", "wanted", "user", "real human ask", 100.0),
    )
    conn.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?,0)",
        ("w2", "wanted", "assistant", "reply", 101.0),
    )
    # The old session: the string-part row is the poisoned one. It sits
    # OUTSIDE the requested window on purpose.
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL)",
        ("old", "tui", "tui", "Old", "m", 1.0, 1),
    )
    conn.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?,0)",
        ("o1", "old", "user", _encode_content(["hello"]), 2.0),
    )
    conn.commit()
    conn.close()


# ── finding 1: a string part must not break the listing ──────────────────


def test_string_part_content_does_not_break_the_session_listing(tmp_path) -> None:
    """Drive the real reader with the poisoned row present.

    Pre-fix the grouped decode evaluated ``JSON_EXTRACT(je.value, '$.text')``
    on the unquoted string part and the whole listing raised ``malformed
    JSON`` — including when the poisoned row belonged to a session the caller
    never asked for.
    """
    from api.agent_sessions import read_importable_agent_session_rows

    db = tmp_path / "state.db"
    _state_db_with_string_part(db)

    rows = read_importable_agent_session_rows(db, session_ids=("wanted",))
    by_id = {row["id"]: row for row in rows}
    assert "wanted" in by_id, (
        "the single-session lookup must return the requested session; a "
        "string-part row in ANOTHER session broke the whole listing "
        "(#7681 re-gate finding 1)"
    )
    assert by_id["wanted"]["actual_user_message_count"] == 1


def test_string_parts_decode_to_their_own_text() -> None:
    """The decoded view of every supported part shape, straight from the SQL.

    String parts use ``je.value`` directly; object parts are still parsed for
    ``$.text``; non-text object parts still render as the marker character;
    scalars and nested lists stay blank (mirroring the Python decoder).
    """
    from api.compression_anchor import _plaintext_content_join_sql

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, content TEXT);"
    )
    shapes = {
        "plain": "plain text",
        "text-part": _encode_content([{"type": "text", "text": "structured"}]),
        "string-part": _encode_content(["hello"]),
        "mixed": _encode_content(["a", {"type": "text", "text": "b"}, "c"]),
        "image-part": _encode_content(
            [{"type": "image_url", "image_url": {"url": "http://x"}}]
        ),
        "empty-list": _encode_content([]),
        "blank-string-part": _encode_content([""]),
        "number-part": _encode_content([42]),
    }
    for sid, content in shapes.items():
        conn.execute(
            "INSERT INTO messages(session_id, content) VALUES (?,?)", (sid, content)
        )
    decoded = dict(
        conn.execute(
            "SELECT m.session_id, m._plaintext_content FROM ("
            + _plaintext_content_join_sql("mq.content")
            + ") m"
        ).fetchall()
    )
    assert decoded == {
        "plain": "plain text",
        "text-part": "structured",
        "string-part": "hello",
        "mixed": "abc",
        "image-part": "▇",
        "empty-list": "",
        "blank-string-part": "",
        "number-part": "",
    }, decoded


# ── finding 2: the grouped decode must not scan all of messages ──────────


def _capture_listing_sql(monkeypatch):
    statements = []
    real_connect = sqlite3.connect

    def tracing_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    import api.agent_sessions as agent_sessions

    monkeypatch.setattr(agent_sessions.sqlite3, "connect", tracing_connect)
    return statements


def test_single_session_listing_does_not_scan_all_messages(tmp_path, monkeypatch) -> None:
    """The single-session lookup must seek ``messages`` by session id.

    Pre-fix the grouped table was materialized from a full ``SCAN mq`` of
    ``messages`` before the join, so resolving ONE conversation decoded the
    entire transcript store. The listing statement must carry the session
    restriction and its plan must not scan the messages table.
    """
    import api.agent_sessions as agent_sessions

    db = tmp_path / "state.db"
    _state_db_with_string_part(db)

    statements = _capture_listing_sql(monkeypatch)
    rows = agent_sessions.read_importable_agent_session_rows(
        db, session_ids=("wanted",)
    )
    assert [row["id"] for row in rows] == ["wanted"]

    listing = [s for s in statements if "FROM sessions s" in s]
    assert len(listing) == 1, listing
    statement = listing[0]
    assert "mq.session_id IN (SELECT cx.id FROM candidates cx)" in statement, (
        "the grouped decode must be restricted to the candidate sessions "
        "before grouping (#7681 re-gate finding 2)"
    )

    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + statement)]
    assert not any("SCAN mq" in step for step in plan), (
        "the grouped decode still scans the whole messages table "
        "(#7681 re-gate finding 2): " + str(plan)
    )
    assert any(
        "SEARCH mq USING INDEX idx_messages_session" in step for step in plan
    ), plan


def test_unbounded_listing_restricts_the_grouped_decode_too(tmp_path, monkeypatch) -> None:
    """The ``limit=None`` listing projects through the same candidates CTE."""
    import api.agent_sessions as agent_sessions

    db = tmp_path / "state.db"
    _state_db_with_string_part(db)

    statements = _capture_listing_sql(monkeypatch)
    rows = agent_sessions.read_importable_agent_session_rows(db, limit=None)
    assert {row["id"] for row in rows} == {"wanted", "old"}

    listing = [s for s in statements if "FROM sessions s" in s]
    assert len(listing) == 1, listing
    statement = listing[0]
    assert "WITH candidates AS (" in statement, statement
    assert "mq.session_id IN (SELECT cx.id FROM candidates cx)" in statement, (
        "the unbounded listing must restrict the grouped decode as well "
        "(#7681 re-gate finding 2)"
    )
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + statement)]
    assert not any("SCAN mq" in step for step in plan), plan


# ── finding 3: an unknown server count must not become zero ──────────────

_NODE_STUB = r"""
globalThis.document = {
  documentElement: { lang: '', dataset: {}, setAttribute() {}, getAttribute() { return null; } },
  createElement() {
    return {
      style: {}, dataset: {}, classList: { add() {}, remove() {} },
      appendChild() {}, addEventListener() {}, setAttribute() {},
      querySelector() { return null; },
    };
  },
  querySelector() { return null; }, querySelectorAll() { return []; },
  addEventListener() {}, body: { appendChild() {}, classList: { add() {}, remove() {} } },
  getElementById() { return null; },
};
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.window = {
  addEventListener() {}, document: globalThis.document,
  localStorage: globalThis.localStorage,
  matchMedia() { return { matches: false, addEventListener() {} }; },
  innerWidth: 1280, innerHeight: 800,
  location: { href: 'http://localhost/', search: '', hash: '' },
  navigator: { userAgent: 'node' },
  setTimeout, clearTimeout, setInterval, clearInterval,
};
globalThis.navigator = { userAgent: 'node' };
globalThis.location = { href: 'http://localhost/', search: '', hash: '' };
globalThis.fetch = () => Promise.reject(new Error('no network in test sandbox'));
globalThis.EventSource = class { addEventListener() {} close() {} };
globalThis.alert = () => {};
globalThis.confirm = () => false;
"""


def _run_node(js_body: str) -> str:
    script = _NODE_STUB + "\n" + js_body
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(script)
        tmp = fh.name
    try:
        proc = subprocess.run(
            ["node", tmp], capture_output=True, text=True, cwd=str(REPO_ROOT)
        )
    finally:
        try:
            Path(tmp).unlink()
        except OSError:
            pass
    assert proc.returncode == 0, f"node sandbox failed: {proc.stderr or proc.stdout}"
    return proc.stdout


# The greptile scenario: a legacy single-segment session whose count the
# backend cannot classify (NULL + the unknown marker). The local row was the
# optimistic first-turn row; the server row has since gone idle, so the merge
# takes the server as authoritative.
_UNKNOWN_COUNT_MERGE_BODY = """
// Not mid-send: the optimistic ownership window is over.
_sendInProgress = null;
_sendInProgressSid = null;
S = { session: { session_id: 'sid-legacy' }, busy: false };
INFLIGHT = {};
const local = {
  session_id: 'sid-legacy', title: 'Legacy chat',
  message_count: 3, user_message_count: null,
  last_message_at: 200, updated_at: 200,
  is_streaming: true,
};
_allSessions = [local];
// The server row is idle and reports an UNKNOWN count (null), never 0.
const fetched = [{
  session_id: 'sid-legacy', title: 'Legacy chat',
  message_count: 3, user_message_count: null,
  user_message_count_unknown: 1,
  last_message_at: 200, updated_at: 200,
  is_streaming: false,
}];
const merged = _mergeOptimisticFirstTurnSessions(fetched);
const row = merged.find(s => s && s.session_id === 'sid-legacy');
if (!row) throw new Error('merged row missing');
if (row.user_message_count === 0) {
  throw new Error('the unknown server count was laundered into a confident 0');
}
if (row.user_message_count !== undefined && row.user_message_count !== null) {
  throw new Error('expected the unknown count to stay unknown, got ' + row.user_message_count);
}
// The render gate must omit the label for an unknown count — "0 user turns"
// for a legacy session is the misleading display the finding describes.
if (typeof _sidebarUserTurnCountRenderOK === 'function' && _sidebarUserTurnCountRenderOK(row)) {
  throw new Error('the render gate must omit the label for an unknown count');
}
process.stdout.write('ok');
"""

_KNOWN_COUNT_MERGE_BODY = """
// Mirror: a KNOWN idle server count must still land, unwidened.
_sendInProgress = null;
_sendInProgressSid = null;
S = { session: { session_id: 'sid-known' }, busy: false };
INFLIGHT = {};
const local = {
  session_id: 'sid-known', title: 'Known chat',
  message_count: 9, user_message_count: 2,
  last_message_at: 200, updated_at: 200,
  is_streaming: true,
};
_allSessions = [local];
const fetched = [{
  session_id: 'sid-known', title: 'Known chat',
  message_count: 9, user_message_count: 4,
  last_message_at: 200, updated_at: 200,
  is_streaming: false,
}];
const merged = _mergeOptimisticFirstTurnSessions(fetched);
const row = merged.find(s => s && s.session_id === 'sid-known');
if (!row) throw new Error('merged row missing');
if (row.user_message_count !== 4) {
  throw new Error('a known idle server count must win, got ' + row.user_message_count);
}
process.stdout.write('ok');
"""


def test_unknown_server_user_turn_count_is_not_laundered_into_zero() -> None:
    """The real merge, run in node: unknown stays unknown.

    Pre-fix ``Number(fetched.user_message_count)`` turned the server's
    ``null`` into ``0`` and the idle merge persisted that zero, so the row
    rendered "0 user turns" for a session whose count is simply unknowable.
    """
    js = (REPO_ROOT / "static" / "sessions.js").read_text(
        encoding="utf-8"
    ) + "\n" + _UNKNOWN_COUNT_MERGE_BODY
    assert _run_node(js).strip() == "ok", (
        "the first-turn merge must preserve an unknown server count instead "
        "of writing 0 (#7681 re-gate finding 3)"
    )


def test_known_idle_server_user_turn_count_still_wins() -> None:
    """The mirror guard: the fix must not swallow real counts."""
    js = (REPO_ROOT / "static" / "sessions.js").read_text(
        encoding="utf-8"
    ) + "\n" + _KNOWN_COUNT_MERGE_BODY
    assert _run_node(js).strip() == "ok", (
        "a known idle server user-turn count must still reach the merged row"
    )


def test_raw_server_count_values_resolve_before_arithmetic() -> None:
    """The merge resolves RAW values: null/blank/negative/NaN stay unknown.

    Exercises the resolver through the real merge (it is nested inside the
    function, travelling with the bodies review harnesses extract), so the
    rules are pinned behaviourally rather than by source grep.
    """
    import json

    cases = {
        "null": None,
        "empty-string": "",
        "blank-string": "   ",
        "negative": -1,
        "nan-string": "abc",
        "true": True,
    }
    template = """
const rawCases = %s;
const out = {};
for (const [name, raw] of Object.entries(rawCases)) {
  _sendInProgress = null;
  _sendInProgressSid = null;
  S = { session: { session_id: 'sid-x' }, busy: false };
  INFLIGHT = {};
  _allSessions = [{
    session_id: 'sid-x', title: 'T', message_count: 3,
    last_message_at: 200, updated_at: 200, is_streaming: true,
  }];
  const fetched = [{
    session_id: 'sid-x', title: 'T', message_count: 3,
    user_message_count: raw,
    last_message_at: 200, updated_at: 200, is_streaming: false,
  }];
  if (name === 'absent') delete fetched[0].user_message_count;
  const row = _mergeOptimisticFirstTurnSessions(fetched)[0];
  out[name] = row.user_message_count === undefined ? 'unknown'
    : row.user_message_count === null ? 'unknown'
    : row.user_message_count;
}
process.stdout.write(JSON.stringify(out));
"""
    source = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

    resolved = json.loads(
        _run_node(source + "\n" + template % json.dumps({**cases, "absent": None}))
    )
    assert resolved == {
        **{name: "unknown" for name in cases},
        "absent": "unknown",
    }, resolved

    # And the values that ARE counts must resolve, not swallow.
    known = {"zero": 0, "four": 4, "numeric-string": "7"}
    resolved = json.loads(_run_node(source + "\n" + template % json.dumps(known)))
    assert resolved == {"zero": 0, "four": 4, "numeric-string": 7}, resolved
