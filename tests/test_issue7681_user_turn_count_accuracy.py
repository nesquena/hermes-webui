def _mirror_predicate():
    """A callable that runs the mirror predicate with the agent blocked.

    The block has to survive the CALL, not just the setup: returning the bare
    function would let the next ``_agent_*`` lookup import the agent again and
    short-circuit the mirror, which is how the finding-5 tests passed with the
    wire bug intact.
    """
    def _blocked_predicate(message):
        # Re-enter the blocked context for the duration of the call.
        sys.path.insert(0, str(REPO_ROOT))
        try:
            with _force_mirror_mode() as anchor_live:
                return anchor_live.is_user_originated_turn(message)
        finally:
            sys.path.pop(0)

    return _blocked_predicate


"""Review-round tests for #7681 (follow-up to #6519).

The first round surfaced ``user_message_count`` in the sidebar meta row, but a
CHANGES_REQUESTED review found the count itself wrong in four ways and badly
presented in two. These tests pin each fix:

1. compressed lineages (frontend collapse dropped non-tip segment counts)
2. imported / unknown-schema sessions (total messages counted as user turns)
3. synthetic compression markers counted as user turns
4. the optimistic pending path never advancing the count
5. layout: the new label was inserted before the existing metadata
6. i18n: five locales shipped malformed plurals

Each test is revert-sensitive: reverting the corresponding production change
makes it fail.
"""

import contextlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_sessions_js() -> str:
    return (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _read_i18n_js() -> str:
    return (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def _read_agent_sessions_py() -> str:
    return (REPO_ROOT / "api" / "agent_sessions.py").read_text(encoding="utf-8")


def _read_models_py() -> str:
    return (REPO_ROOT / "api" / "models.py").read_text(encoding="utf-8")


def _locale_block(src: str, locale_key: str) -> str:
    """Brace-aware extraction of a locale block from i18n.js.

    A naive ``src[a:b]`` slice breaks on nested braces inside arrow-function
    bodies, so walk the brace depth with quote/escape tracking (same approach
    as tests/test_czech_locale.py).
    """
    start_match = re.search(rf"\b['\"]?{re.escape(locale_key)}['\"]?\s*:\s*\{{", src)
    assert start_match, f"{locale_key} locale block not found"
    start = start_match.end() - 1
    depth = 0
    in_single = in_double = in_backtick = False
    escape = False
    for i in range(start, len(src)):
        ch = src[i]
        if escape:
            escape = False
            continue
        if in_single:
            if ch == "\\":
                escape = True
            elif ch == "'":
                in_single = False
            continue
        if in_double:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            continue
        if in_backtick:
            if ch == "\\":
                escape = True
            elif ch == "`":
                in_backtick = False
            continue
        if ch == "'":
            in_single = True
            continue
        if ch == '"':
            in_double = True
            continue
        if ch == "`":
            in_backtick = True
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return src[start + 1 : i]
    raise AssertionError(f"{locale_key} locale block braces are not balanced")


def _make_state_db(path: Path, *, with_roles: bool, with_messages_table: bool = True) -> None:
    """Build a minimal state.db for the agent-session projection tests."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        """
    )
    if with_messages_table:
        if with_roles:
            conn.executescript(
                """
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    role TEXT,
                    content TEXT,
                    timestamp REAL,
                    _compressed_summary INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
                """
            )
        else:
            # No ``role`` column: the projection cannot tell user turns from
            # assistant/tool rows.
            conn.executescript(
                """
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    content TEXT,
                    timestamp REAL
                );
                """
            )
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, session_source, title, model, started_at, message_count,
         parent_session_id, ended_at, end_reason)
        VALUES ('imported_a', 'tui', 'tui', 'Imported A', 'm', 10.0, 4,
                NULL, NULL, NULL)
        """
    )
    if with_messages_table:
        for i in range(4):
            if with_roles:
                conn.execute(
                    "INSERT INTO messages (id, session_id, role, content, timestamp,"
                    " _compressed_summary) VALUES (?,?,?,?,?,?)",
                    (f"m{i}", "imported_a", "user", "hello", 11.0 + i, 0),
                )
            else:
                conn.execute(
                    "INSERT INTO messages (id, session_id, content, timestamp)"
                    " VALUES (?,?,?,?)",
                    (f"m{i}", "imported_a", "hello", 11.0 + i),
                )
    conn.commit()
    conn.close()


# ── Node sandbox helpers ────────────────────────────────────────────────────
#
# Python has no ``vm`` module, and static/sessions.js is ~450 KB (too large for
# ``node -e`` argv), so run the real frontend source through a temp file with
# minimal browser stubs.

_NODE_STUB = r"""
// Minimal browser stubs so static/sessions.js and static/i18n.js can be
// evaluated outside a browser. Both modules only touch these inside
// functions (never at module scope), so no-ops are enough.
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


def _run_node(js_body: str):
    """Run ``js_body`` in Node with the browser stubs and return stdout."""
    import tempfile

    script = _NODE_STUB + "\n" + js_body
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(script)
        tmp = fh.name
    try:
        r = subprocess.run(
            ["node", tmp], capture_output=True, text=True, cwd=str(REPO_ROOT)
        )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if r.returncode != 0:
        raise AssertionError(f"node sandbox failed: {r.stderr or r.stdout}")
    return r.stdout


def _load_i18n_locales() -> dict:
    """Return ``{locale: {str(n): rendered}}`` for the shipping locale functions.

    The i18n.js source and the dump statement are concatenated into one script
    so the module's ``const LOCALES`` binding is in scope.
    """
    js = (
        _read_i18n_js()
        + """
const __out = {};
for (const [loc, table] of Object.entries(LOCALES)) {
  if (!table || typeof table.session_meta_user_turns !== 'function') continue;
  __out[loc] = {};
  for (const n of [0, 1, 2, 3, 4, 5, 11, 21, 22, 25, 101, 111]) {
    __out[loc][String(n)] = String(table.session_meta_user_turns(n));
  }
}
process.stdout.write(JSON.stringify(__out));
"""
    )
    return json.loads(_run_node(js))


# ── Finding 3: synthetic markers counted as user turns ──────────────────────


def test_state_db_user_count_excludes_compression_markers():
    """#7681 finding 3: the state-DB aggregate must not count the synthetic
    compression / task-summary cards the agent persists with role='user'."""
    src = _read_agent_sessions_py()
    assert "COALESCE(m._compressed_summary, 0) = 0" in src, (
        "the state.db user-turn aggregate must exclude rows flagged as "
        "compression summaries (api/agent_sessions.py user_message_count_expr) "
        "— see #7681 finding 3"
    )


def test_sidecar_user_count_excludes_compression_markers():
    """#7681 finding 3: the sidecar walk (Session._compute_user_message_count)
    must classify each row with the shared user-originated-turn predicate, which
    excludes every synthetic card — not just durable-flag compression markers."""
    src = _read_models_py()
    assert (
        "is_user_originated_turn(m)" in src
    ), (
        "Session._compute_user_message_count must use the shared "
        "api.compression_anchor.is_user_originated_turn() predicate — "
        "matching the state.db SQL aggregate — see #7681 finding 2"
    )


def test_count_user_turns_helper_excludes_compression_markers():
    """#7681 finding 3: the visibility helper's row->messages fallback must
    apply the same classification."""
    src = _read_agent_sessions_py()
    assert (
        "if is_user_originated_turn(msg)" in src
    ), (
        "_count_user_turns() must count with is_user_originated_turn() when "
        "falling back to row['messages'] — see #7681 finding 2"
    )


def test_sidecar_count_helper_skips_real_markers():
    """#7681 finding 3: behaviour test — the marker classification must not be
    a vacuous guard: a real marker row must not be counted."""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.compression_anchor import is_context_compression_marker
        from api.models import Session
    finally:
        sys.path.pop(0)

    marker = {
        "role": "user",
        "content": "[context compaction] prior context summary",
        "_compressed_summary": True,
    }
    real_user = {"role": "user", "content": "please fix the sidebar count"}
    assistant = {"role": "assistant", "content": "on it"}

    assert is_context_compression_marker(marker) is True
    assert is_context_compression_marker(real_user) is False
    assert is_context_compression_marker(assistant) is False

    counted = Session._compute_user_message_count([real_user, marker, assistant])
    assert counted == 1, (
        f"expected 1 real user turn, got {counted} — synthetic compression "
        "markers must not inflate the count"
    )


# ── Finding 2: imported / unknown-schema sessions ───────────────────────────


def test_unknown_role_schema_emits_null_not_total():
    """#7681 finding 2: when the messages table has no ``role`` column the
    public count must be NULL ("unknown"), not the total message count."""
    src = _read_agent_sessions_py()
    branch = src[src.index("if 'role' in message_cols:") :]
    branch = branch[: branch.index("last_activity_expr =")]
    assert 'user_message_count_expr = "NULL"' in branch, (
        "the no-role branch must emit NULL for the public user-turn count "
        "instead of the total message count — see #7681 finding 2"
    )
    assert "COUNT(m." not in branch, (
        "the no-role branch must not derive a user-turn count from a total "
        "row count — see #7681 finding 2"
    )


def test_no_messages_table_keeps_estimate_separate():
    """#7681 finding 2: the denormalized fallback keeps its conservative total
    in a separate internal field rather than the public one."""
    src = _read_agent_sessions_py()
    assert "user_message_count_estimate_expr = \"s.message_count\"" in src, (
        "the no-messages-table fallback must carry the conservative total in a "
        "separate ``user_message_count_estimate`` field — see #7681 finding 2"
    )
    assert "AS user_message_count_estimate" in src, (
        "the estimate field must actually be selected so callers can still "
        "read an approximate value — see #7681 finding 2"
    )


def test_read_importable_rows_null_user_count_on_unknown_schema(tmp_path):
    """#7681 finding 2: end-to-end — an imported state.db whose ``messages``
    table lacks ``role`` must yield ``actual_user_message_count is None`` and
    must not fabricate an estimate either."""
    db = tmp_path / "state.db"
    _make_state_db(db, with_roles=False)

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.agent_sessions import read_importable_agent_session_rows
    finally:
        sys.path.pop(0)

    by_id = {row["id"]: row for row in read_importable_agent_session_rows(db)}
    assert "imported_a" in by_id, f"imported row missing: {list(by_id)}"
    row = by_id["imported_a"]
    assert row["actual_user_message_count"] is None, (
        "a schema without roles must emit NULL, not the total message count "
        f"(got {row['actual_user_message_count']!r}) — see #7681 finding 2"
    )
    # Roles-unavailable means "unknown": no fabricated estimate either. The
    # estimate only exists on the no-messages-table branch.
    assert row.get("user_message_count_estimate") is None, (
        "the roles-unavailable branch must not fabricate an estimate"
    )


def test_read_importable_rows_keeps_estimate_without_messages_table(tmp_path):
    """#7681 finding 2: end-to-end — with no messages table at all the public
    count is NULL but the conservative total survives as an internal estimate."""
    db = tmp_path / "state.db"
    _make_state_db(db, with_roles=False, with_messages_table=False)

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.agent_sessions import read_importable_agent_session_rows
    finally:
        sys.path.pop(0)

    by_id = {row["id"]: row for row in read_importable_agent_session_rows(db)}
    row = by_id["imported_a"]
    assert row["actual_user_message_count"] is None, (
        "no messages table means the user-turn count is unknown — see #7681"
    )
    assert row["user_message_count_estimate"] == 4, (
        "the denormalized total must stay available as an internal estimate "
        f"(got {row['user_message_count_estimate']!r}) — see #7681 finding 2"
    )


def test_read_importable_rows_excludes_compression_markers(tmp_path):
    """#7681 finding 3: end-to-end — a state.db with a synthetic compression
    card stored as role='user' must not count it."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            _compressed_summary INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, session_source, title, model, started_at, message_count,
         parent_session_id, ended_at, end_reason)
        VALUES ('cli_a', 'tui', 'tui', 'CLI A', 'm', 10.0, 4, NULL, NULL, NULL)
        """
    )
    for mid, role, content, flag in [
        ("m0", "user", "first question", 0),
        ("m1", "assistant", "answer", 0),
        ("m2", "user", "[context compaction] summary", 1),
        ("m3", "user", "second question", 0),
    ]:
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp,"
            " _compressed_summary) VALUES (?,?,?,?,?,?)",
            (mid, "cli_a", role, content, 11.0, flag),
        )
    conn.commit()
    conn.close()

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.agent_sessions import read_importable_agent_session_rows
    finally:
        sys.path.pop(0)

    by_id = {row["id"]: row for row in read_importable_agent_session_rows(db)}
    assert "cli_a" in by_id, f"cli row missing: {list(by_id)}"
    assert by_id["cli_a"]["actual_user_message_count"] == 2, (
        "expected 2 real user turns (m0 + m3), got "
        f"{by_id['cli_a']['actual_user_message_count']} — the synthetic "
        "compression card must be excluded (see #7681 finding 3)"
    )


# ── Finding 2: sidecar vs state.db parity ──────────────────────────────────
#
# Both user-turn producers must classify a synthetic row matrix identically to
# the shared is_user_originated_turn() predicate. The 14-shape overcount from
# the review (legacy [CONTEXT SUMMARY]: with/without the flag, the continuation
# marker, process-wakeup rows, display_kind hidden / async-delegation rows,
# blank rows, the max-iterations request, TODO re-injection) must yield the
# same answer whether it is walked from the sidecar or aggregated from state.db.

_CONTINUATION_USER_CONTENT = (
    "Continue from the compressed conversation context above. "
    "This marker exists because no human user turn was available."
)
_MAX_ITERATIONS_REQUEST = (
    "You've reached the maximum number of tool-calling iterations allowed. "
    "Please provide a final response summarizing what you've found and accomplished so far, "
    "without calling any more tools."
)


def _synthetic_row_matrix():
    """Content-representable rows covering every overcounted shape from the review.

    Each row uses the CANONICAL spelling the Hermes agent's own predicate
    recognizes, so the sidecar/predicate producer and the state.db SQL producer
    agree on exactly the same set (the two MUST-FIX counters in #7681 finding 2).
    """
    return [
        ("m0", "user", "hello there", 0),
        ("m1", "assistant", "on it", 0),
        ("m2", "user", "please triage those logs", 0),
        # durable flag → excluded by the agent (metadata) and by the SQL (flag).
        ("m3", "user", "[context compaction] prior context summary", 1),
        # legacy summary row WITHOUT the flag → excluded by the agent's
        # legacy-prefix match and by the SQL prefix heuristic.
        ("m4", "user", "[CONTEXT SUMMARY]: the user asked about x", 0),
        # continuation marker → excluded by the agent's exact match and the SQL.
        ("m6", "user", _CONTINUATION_USER_CONTENT, 0),
        # process wake-up notification → excluded by the agent's prefix match.
        ("m7", "user", "[IMPORTANT: Background process 42 finished] done", 0),
        # blank echo → not a human ask.
        ("m8", "user", "", 0),
        # max-iterations request → excluded by the agent's exact match.
        ("m9", "user", _MAX_ITERATIONS_REQUEST, 0),
        # TODO re-injection header + newline → excluded by the agent's
        # TODO_INJECTION_HEADER + "\n" prefix match (the header alone is not).
        ("m10", "user",
            "[Your active task list was preserved across context compression]\nContinue working", 0),
    ]


def test_user_turn_parity_db_vs_sidecar_and_predicate(tmp_path):
    """#7681 finding 2: the SAME row matrix yields the same user-turn count
    through the state.db SQL aggregate, the sidecar walk, and the shared
    is_user_originated_turn() predicate (the local mirror when the agent's own
    predicate is not importable in this venv)."""
    from api.agent_sessions import read_importable_agent_session_rows
    from api.compression_anchor import is_user_originated_turn
    from api.models import Session

    matrix = _synthetic_row_matrix()
    # Sidecar + predicate producers over the message dicts.
    sidecar_messages = [
        {"role": role, "content": content, "_compressed_summary": flag}
        for _mid, role, content, flag in matrix
    ]
    # only m0 and m2 are genuine human turns.
    assert Session._compute_user_message_count(sidecar_messages) == 2, (
        "sidecar producer must count exactly the 2 genuine turns"
    )
    assert sum(is_user_originated_turn(m) for m in sidecar_messages) == 2, (
        "is_user_originated_turn() predicate must match the sidecar count"
    )

    # SQL producer: build a state.db carrying the SAME content matrix.
    # #7681 finding 2: the schema MUST include `display_kind` — the earlier
    # version of this test omitted the column, so the new display_kind guard was
    # silently skipped (`if 'display_kind' in message_cols`) and the test stayed
    # green while the counter still over-reported.
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
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
            timestamp REAL, _compressed_summary INTEGER NOT NULL DEFAULT 0,
            display_kind TEXT
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, session_source, title, model,"
        " started_at, message_count, parent_session_id, ended_at, end_reason)"
        " VALUES ('parity_a', 'tui', 'tui', 'Parity A', 'm', 10.0,"
        " 12, NULL, NULL, NULL)"
    )
    for mid, role, content, flag in matrix:
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp,"
            " _compressed_summary, display_kind) VALUES (?,?,?,?,?,?,?)",
            (mid, "parity_a", role, content, 100.0, flag, None),
        )
    conn.commit()
    conn.close()

    by_id = {r["id"]: r for r in read_importable_agent_session_rows(db)}
    assert "parity_a" in by_id, f"parity row missing: {list(by_id)}"
    assert by_id["parity_a"]["actual_user_message_count"] == 2, (
        "state.db SQL aggregate must equal the sidecar/predicate count for the "
        "same row matrix — got "
        f"{by_id['parity_a']['actual_user_message_count']!r}, expected 2"
    )
    # The rows whose roles were decidable do NOT carry the unknown marker.
    assert not by_id["parity_a"].get("user_message_count_unknown")


def test_user_turn_predicate_excludes_display_kind_async_rows():
    """#7681 finding 2: display_kind hidden / async-delegation rows and blank
    echoes are synthetic even though they carry role='user'; a /steer row is
    human input."""
    from api.compression_anchor import is_user_originated_turn

    async_delegation = {
        "role": "user", "content": "delegated task finished",
        "display_kind": "hidden",
    }
    hidden_scaffolding = {
        "role": "user", "content": "[context compaction] reran the task",
        "display_kind": "hidden",
    }
    steer_row = {"role": "user", "content": "/steer clear", "display_kind": "steer"}
    real_takeover = {"role": "user", "content": "take over from here"}

    assert is_user_originated_turn(async_delegation) is False
    assert is_user_originated_turn(hidden_scaffolding) is False
    assert is_user_originated_turn(steer_row) is True
    assert is_user_originated_turn(real_takeover) is True


# ── Finding 1: compressed lineage total ─────────────────────────────────────


def test_collapse_does_not_fabricate_lineage_turn_total():
    """#7681 finding 1: a collapsed lineage row must NOT expose an undeduplicated
    sum of segment user_message_count as a whole-lineage total (overlapping
    snapshot/continuation segments over-count; an unknown segment degrades the
    sum into a partial). The backend emits NULL for collapsed lineages and the
    renderer omits the label, so the collapsed row must carry no fabricated
    `_lineage_user_message_count` and must not be render-eligible."""
    js = _read_sessions_js() + """\
const segRoot = {
  session_id: 'root', title: 'Root',
  user_message_count: 7, message_count: 12,
  parent_session_id: null, pre_compression_snapshot: true,
};
const segTip = {
  session_id: 'tip', title: 'Tip',
  user_message_count: 2, message_count: 3,
  parent_session_id: 'root',
  _lineage_tip_id: 'tip', _lineage_root_id: 'root',
  _compression_segment_count: 2,
};
const collapsed = _collapseSessionLineageForSidebar([segRoot, segTip]);
if (collapsed.length !== 1) throw new Error('expected 1 collapsed row, got ' + collapsed.length);
const row = collapsed[0];
if (row._lineage_collapsed_count !== 2) throw new Error('collapsed count wrong');
if ('_lineage_user_message_count' in row) {
  throw new Error('collapsed lineage must not fabricate an undeduplicated user-turn total');
}
if (_sidebarUserTurnCountRenderOK(row)) {
  throw new Error('a collapsed row must not be rendered with an arguably-accurate turn count');
}
process.stdout.write('ok');
"""
    assert _run_node(js).strip() == "ok", (
        "a collapsed lineage must not fabricate a lineage total, and must not "
        "be eligible for the user-turn label — see #7681 finding 1"
    )


def test_render_omits_turn_label_for_collapsed_rows():
    """#7681 finding 1: the meta row must render the user-turn label ONLY when
    the count is known-accurate (single segment, known roles, not pending) and
    must omit it for collapsed lineages where the client cannot know a
    deduplicated total."""
    src = _read_sessions_js()
    assert "_sidebarUserTurnCountRenderOK" in src, (
        "sessions.js must gate the user-turn label on an accuracy check — see #7681 finding 1"
    )
    render_idx = src.index("session_meta_user_turns")
    lookup = src[render_idx - 500 : render_idx]
    assert "_sidebarUserTurnCountRenderOK(s)" in lookup, (
        "the render site must consult _sidebarUserTurnCountRenderOK at the "
        "label, not sum across lineage segments"
    )
    assert "_lineage_user_message_count" not in src, (
        "no producer may expose a fabricated _lineage_user_message_count total"
    )


def test_user_turn_label_render_gate_behaviour():
    """#7681 findings 1 & 3: behaviour — single-segment known counts render;
    collapsed / pending / unknown counts are omitted."""
    js = _read_sessions_js() + """\
function check(name, s, expected){
  const got=!!_sidebarUserTurnCountRenderOK(s);
  if(got!==expected) throw new Error(name+': expected '+expected+' got '+got);
}
check('single known', {user_message_count:3}, true);
check('known zero', {user_message_count:0}, true);
check('collapsed browser', {user_message_count:7, _lineage_collapsed_count:2}, false);
check('collapsed server', {user_message_count:7, _compression_segment_count:2}, false);
check('lineage root', {user_message_count:7, _lineage_root_id:'r'}, false);
check('pre-compression snapshot', {user_message_count:7, pre_compression_snapshot:true}, false);
check('pending user message', {user_message_count:7, pending_user_message:'x'}, false);
check('streaming', {user_message_count:7, active_stream_id:'s'}, false);
check('unknown count', {user_message_count:null}, false);
process.stdout.write('ok');
"""
    assert _run_node(js).strip() == "ok", (
        "_sidebarUserTurnCountRenderOK must render only single-segment, known, "
        "non-pending counts — see #7681 findings 1 & 3"
    )


# ── Finding 4: optimistic path ──────────────────────────────────────────────


def test_optimistic_upsert_advances_user_turn_count():
    """#7681 finding 4: the optimistic first-turn row must advance the user-turn
    count instead of leaving it stale until the next poll."""
    src = _read_sessions_js()
    start = src.index("function upsertActiveSessionForLocalTurn")
    end = src.index("function _sessionRowsWithActiveEphemeralSession", start)
    body = src[start:end]
    assert "user_message_count" in body, (
        "upsertActiveSessionForLocalTurn must update user_message_count "
        "optimistically — see #7681 finding 4"
    )
    assert "_isContextCompactionMessage" in body, (
        "the optimistic count must skip synthetic compression cards the same "
        "way the backend does — see #7681 findings 3 and 4"
    )


def test_optimistic_merge_preserves_user_turn_count():
    """#7681 finding 4: the first-turn merge must not drop the local optimistic
    count when the fetched row lags."""
    src = _read_sessions_js()
    start = src.index("function _mergeOptimisticFirstTurnSessions")
    end = src.index("function _isSessionListUserInteracting", start)
    body = src[start:end]
    assert "user_message_count" in body, (
        "_mergeOptimisticFirstTurnSessions must reconcile user_message_count "
        "alongside message_count — see #7681 finding 4"
    )


def test_optimistic_merge_keeps_larger_count_node():
    """#7681 finding 4: behaviour test — when the server row lags behind the
    local optimistic row, the larger (local) count must win.

    The module source and the test body are concatenated into ONE Node script
    so they share the same top-level lexical scope — the module's ``let``
    bindings are not reachable through ``globalThis`` from a separate eval.
    """
    body = """
// Force the 'keep the local optimistic row' branch: an active, busy session
// whose local row carries runtime confirmation.
_sendInProgress = 'sid-1';
_sendInProgressSid = 'sid-1';
S = { session: { session_id: 'sid-1' }, busy: true };
INFLIGHT = {};
const local = {
  session_id: 'sid-1', title: 'New chat',
  message_count: 2, user_message_count: 3,
  last_message_at: 200, updated_at: 200,
  active_stream_id: 'stream-1', pending_user_message: 'hello',
  pending_started_at: 199, is_streaming: true,
};
_allSessions = [local];
const fetched = [{
  session_id: 'sid-1', title: 'New chat',
  message_count: 2, user_message_count: 2,
  last_message_at: 200, updated_at: 200,
  active_stream_id: 'stream-1', pending_user_message: 'hello',
  is_streaming: true,
}];
const merged = _mergeOptimisticFirstTurnSessions(fetched);
const row = merged.find(s => s && s.session_id === 'sid-1');
if (!row) throw new Error('merged row missing');
if (row.user_message_count !== 3) {
  throw new Error('expected the larger local count 3, got ' + row.user_message_count);
}
if (row.message_count !== 2) throw new Error('message_count drifted');
process.stdout.write('ok');
"""
    js = _read_sessions_js() + "\n" + body
    assert _run_node(js).strip() == "ok", (
        "the first-turn merge must keep the larger local user-turn count when "
        "the fetched row lags — see #7681 finding 4"
    )


# ── Finding 5: layout ───────────────────────────────────────────────────────


def test_user_turn_label_appended_after_existing_metadata():
    """#7681 finding 5: the new label must come after the existing metadata so
    it cannot push already-visible model/source/profile info out of the single
    ellipsized meta line."""
    src = _read_sessions_js()
    start = src.index("if(density==='detailed'){")
    end = src.index("sessionText.appendChild(meta);", start)
    body = src[start:end]
    profile_idx = body.index("s.profile) metaBits.push(s.profile)")
    turns_idx = body.index("session_meta_user_turns")
    assert turns_idx > profile_idx, (
        "the user-turn label must be pushed AFTER the existing metadata bits "
        "(model / source / read-only / profile) so narrow sidebars keep showing "
        "what they showed before — see #7681 finding 5"
    )


def test_user_turn_label_does_not_precede_model_metadata():
    """#7681 finding 5: belt-and-braces — the label must not sit between the
    message-count bit and the model bit (the original layout bug)."""
    src = _read_sessions_js()
    start = src.index("if(density==='detailed'){")
    end = src.index("sessionText.appendChild(meta);", start)
    body = src[start:end]
    model_idx = body.index("_formatSessionModelWithGateway(s)")
    turns_idx = body.index("session_meta_user_turns")
    assert turns_idx > model_idx, (
        "the user-turn label must not be inserted before the model metadata "
        "bit — see #7681 finding 5"
    )


# ── Finding 6: i18n plurals ─────────────────────────────────────────────────


def test_italian_user_turns_plural_agreement():
    """#7681 finding 6: Italian must inflect the noun, not just append a suffix
    to a fixed stem ('2 turno utentei' was malformed)."""
    renders = _load_i18n_locales()["it"]
    assert renders["1"] == "1 turno utente", renders["1"]
    assert renders["2"] == "2 turni utente", renders["2"]
    assert renders["5"] == "5 turni utente", renders["5"]


def test_russian_user_turns_uses_plural_helper():
    """#7681 finding 6: Russian 0/21 forms must route through _i18nRuPlural."""
    src = _read_i18n_js()
    block = _locale_block(src, "ru")
    entry = re.search(
        r"session_meta_user_turns\s*:\s*\(n\)\s*=>\s*(.+)", block
    ).group(1)
    assert "_i18nRuPlural(" in entry, (
        f"ru session_meta_user_turns must use _i18nRuPlural, got: {entry!r}"
    )

    renders = _load_i18n_locales()["ru"]
    # CLDR rules via _i18nRuPlural: 1 / 21 / 101 -> one; 2-4 / 22-24 -> few;
    # 0 / 5-20 / 25+ -> many. The pre-fix inline branch rendered "21
    # сообщения" (few) and "0 сообщения" (few), both wrong.
    assert renders["1"] == "1 сообщение пользователя", renders["1"]
    assert renders["21"] == "21 сообщение пользователя", renders["21"]
    assert renders["2"] == "2 сообщения пользователя", renders["2"]
    assert renders["0"] == "0 сообщений пользователя", renders["0"]
    assert renders["5"] == "5 сообщений пользователя", renders["5"]


def test_french_user_turns_pluralizes_both_words():
    """#7681 finding 6: French was '2 message utilisateurs' — the noun must
    agree too."""
    renders = _load_i18n_locales()["fr"]
    assert renders["1"] == "1 message utilisateur", renders["1"]
    assert renders["2"] == "2 messages utilisateurs", renders["2"]


def test_czech_user_turns_uses_plural_helper():
    """#7681 finding 6: Czech must route through _i18nCsPlural so 0 renders as
    '0 zpráv uživatele' rather than '0 zprávy'."""
    src = _read_i18n_js()
    block = _locale_block(src, "cs")
    entry = re.search(
        r"session_meta_user_turns\s*:\s*\(n\)\s*=>\s*(.+)", block
    ).group(1)
    assert "_i18nCsPlural(" in entry, (
        f"cs session_meta_user_turns must use _i18nCsPlural, got: {entry!r}"
    )

    renders = _load_i18n_locales()["cs"]
    assert renders["1"] == "1 zpráva uživatele", renders["1"]
    assert renders["0"] == "0 zpráv uživatele", renders["0"]
    assert renders["3"] == "3 zprávy uživatele", renders["3"]


def test_turkish_user_turns_not_singular_only():
    """#7681 finding 6: Turkish rendered a bare singular for n === 1 and a
    broken stem for everything else. The noun phrase must be consistent."""
    src = _read_i18n_js()
    block = _locale_block(src, "tr")
    entry = re.search(
        r"session_meta_user_turns\s*:\s*\(n\)\s*=>\s*(.+)", block
    ).group(1)
    assert "n === 1 ? '' : 'ı'" not in entry, (
        "tr session_meta_user_turns must not switch on a bare singular stem"
    )
    renders = _load_i18n_locales()["tr"]
    assert renders["1"] == "1 kullanıcı mesajı", renders["1"]
    assert renders["2"] == "2 kullanıcı mesajı", renders["2"]


def test_user_turns_key_present_in_all_fifteen_locales():
    """#6519 guard: every locale must still define the key."""
    src = _read_i18n_js()
    for locale in (
        "en", "it", "ja", "ru", "es", "de", "zh", "zh-Hant",
        "pt", "ko", "fr", "cs", "tr", "pl", "vi",
    ):
        block = _locale_block(src, locale)
        assert "session_meta_user_turns:" in block, (
            f"{locale} must define session_meta_user_turns"
        )


def test_i18n_files_parse_after_plural_fixes():
    """Guard: the plural rewrites must keep i18n.js loadable."""
    for rel in ("static/i18n.js", "static/sessions.js"):
        r = subprocess.run(
            ["node", "--check", str(REPO_ROOT / rel)],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, f"{rel} failed node --check: {r.stderr}"


def test_unknown_turn_count_does_not_hide_untitled_cli_row():
    """#7681 follow-up: an UNKNOWN turn count must not hide a sidebar row.

    Finding 2 keeps ``actual_user_message_count`` NULL when the state.db
    messages table has no ``role`` column — unknown, not a confident wrong
    number. But ``is_cli_session_row_visible()`` used that NULL as a known zero
    in its final untitled-row threshold, so a real untitled CLI session on a
    legacy schema vanished from the sidebar instead of being shown: the row can
    never clear the threshold because the count is missing, not because it is
    low. Hiding a real session is data loss the user can see; showing it with an
    unknown turn count is not.

    Revert-sensitive: removing the unknown-schema escape in
    ``is_cli_session_row_visible()`` makes this fail.
    """
    import api.agent_sessions as ags

    row = {
        "id": "cli_unknown_schema",
        "actual_message_count": 3,          # messages exist and are joinable
        "message_count": 3,
        "actual_user_message_count": None,  # no `role` column → unknown
        "user_message_count": None,
        "user_message_count_unknown": True,  # SQL branch signals roles unavailable
        "messages": [],                     # no sidecar fallback available
        "ended_at": None,
        "end_reason": None,
        "source": "cli",
        "source_tag": "cli",
        "title": None,                      # untitled → hits the threshold branch
    }

    assert ags._count_user_turns(row) == 0
    assert ags.is_cli_session_row_visible(row) is True


def test_webui_row_without_unknown_marker_stays_hidden_by_count():
    """#7681 finding 4: the escape must key on the EXPLICIT unknown marker
    (set only by the no-role / no-messages SQL branches), never on the value
    being absent.

    A legacy WebUI sidecar/index row that simply does not carry
    ``actual_user_message_count`` is not "unknown" — it has an accurate count
    in its own metadata. Previously ``is_cli_session_row_visible()`` escaped
    any row whose count field was missing, which surfaced rows master hid (the
    escape fired on the *value*, so every index row without a count passed the
    gate once it had message_count >= 2). With the escape now keyed on the
    marker, such a row falls through to the real count threshold and stays
    hidden once it cannot prove two user turns.

    Revert-sensitive: keying the escape on the value (None) instead of the
    marker makes this fail and re-surfaces the over-surfacing bug.
    """
    import api.agent_sessions as ags

    row = {
        "id": "webui_legacy_index",
        # A legacy imported/CLI row that comes from an index/sidecar source and
        # never carries actual_user_message_count nor the unknown marker, but
        # does carry enough to hit the untitled threshold.
        "actual_message_count": 5,
        "message_count": 5,
        "actual_user_message_count": None,   # field simply absent
        "user_message_count": None,
        # no "user_message_count_unknown" key → marker must NOT be used
        "messages": [],
        "ended_at": None,
        "end_reason": None,
        "source": "cli",
        "source_tag": "cli",
        "title": None,
    }
    # The row IS CLI-classified (source cli), but the escape must NOT fire
    # because no explicit unknown marker is present — it must fall through to
    # the count threshold and stay hidden without a recoverable user-turn count.
    assert ags._count_user_turns(row) == 0
    assert ags.is_cli_session_row_visible(row) is False


def test_known_zero_turn_count_still_hides_untitled_cli_row():
    """The known-zero path must keep hiding rows — the escape is not a blanket bypass."""
    import api.agent_sessions as ags

    row = {
        "id": "cli_known_zero",
        "actual_message_count": 3,
        "message_count": 3,
        "actual_user_message_count": 0,  # schema HAS roles; there are simply none
        "user_message_count": 0,
        "messages": [],
        "ended_at": None,
        "end_reason": None,
        "source": "cli",
        "source_tag": "cli",
        "title": None,
    }

    assert ags._count_user_turns(row) == 0
    assert ags.is_cli_session_row_visible(row) is False


def test_legacy_single_message_untitled_cli_row_does_not_pass():
    """#7681 finding: a single message must not clear the two-user-turn gate.

    The unknown-schema escape surfaces untitled CLI rows whose turn count is
    genuinely unknown (no ``role`` column), so a real session isn't hidden just
    because an old schema lacks a column. But the escape must not let a row
    that *provably* cannot contain two user turns through the threshold: a
    legacy (roles-unavailable) untitled CLI session with exactly one message is
    not an interactive two-turn conversation, so it must stay hidden just like
    the known-zero path.

    Revert-sensitive: broadening the escape to ignore ``message_count`` (or
    dropping the count guard) makes this fail.
    """
    import api.agent_sessions as ags

    row = {
        "id": "cli_legacy_one_msg",
        "actual_message_count": 1,       # joinable, but only one message
        "message_count": 1,
        "actual_user_message_count": None,  # no `role` column → unknown
        "user_message_count": None,
        "user_message_count_unknown": True,  # SQL branch signals roles unavailable
        "messages": [],                  # no sidecar fallback available
        "ended_at": None,
        "end_reason": None,
        "source": "cli",
        "source_tag": "cli",
        "title": None,                   # untitled → hits the threshold branch
    }

    assert ags._count_user_turns(row) == 0
    # One message cannot contain the two user turns required by the untitled
    # CLI threshold, so an unknown count must still resolve to *hidden* here.
    assert ags.is_cli_session_row_visible(row) is False


# ── Finding 1 (re-gate 2026-10-08): the collapsed row must total EVERY segment ──
#
# The eight-turn reproduction survived SQL projection: the loader drops
# `lineage_user_message_count`, the route rechecks visibility at
# api/routes.py:10882 and removes the row. These tests drive the real loader and
# the real visibility gate instead of grepping source text (the round-3 pins were
# string pins that passed with the wire bug intact).


def _build_compressed_chain_db(path, *, acp: bool = False, root_roles: bool = True):
    """A three-segment compressed ACP/CLI chain whose root holds NO user turns.

    ``root``   : 2 assistant rows (the compacted prefix) -> 0 user turns
    ``mid``    : 3 real user turns
    ``tip``    : 5 real user turns

    A chain that sums only the root reports 0 and vanishes; only the tip
    reports 5; the correct whole-lineage total is 8.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            _compressed_summary INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    source = "acp" if acp else "cli"
    for sid, parent, started, ended, reason in [
        ("root", None, 10.0, 20.0, "compression"),
        ("mid", "root", 21.0, 30.0, "compression"),
        ("tip", "mid", 31.0, None, None),
    ]:
        conn.execute(
            "INSERT INTO sessions (id, source, session_source, title, model,"
            " started_at, message_count, parent_session_id, ended_at, end_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, source, source, f"{sid} title", "m", started, 8, parent, ended, reason),
        )
    rows = []
    # Root: assistant-only prefix, no human turns at all.
    for i, (mid, role, content) in enumerate(
        [("r0", "assistant", "hello"), ("r1", "assistant", "anything else?")]
    ):
        rows.append((mid, "root", role, content, 11.0 + i))
    # Mid segment: three genuine user turns.
    for i, (mid, content) in enumerate(
        [("m0", "first real ask"), ("m1", "second ask"), ("m2", "third ask")]
    ):
        rows.append((mid, "mid", "user", content, 22.0 + i))
    # Tip segment: five genuine user turns.
    for i, (mid, content) in enumerate(
        [("t0", "a"), ("t1", "b"), ("t2", "c"), ("t3", "d"), ("t4", "e")]
    ):
        rows.append((mid, "tip", "user", content, 32.0 + i))
    for mid, sid, role, content, ts in rows:
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp,"
            " _compressed_summary) VALUES (?,?,?,?,?,0)",
            (mid, sid, role, content, ts),
        )
    conn.commit()
    conn.close()


def test_collapsed_lineage_totals_user_turns_across_every_segment(tmp_path):
    """#7681 finding 1 CORE: the collapsed row's visibility count is the sum over
    every continuation segment, not the root's own slice and not the tip's."""
    db = tmp_path / "state.db"
    _build_compressed_chain_db(db, acp=True)

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.agent_sessions import (
            _count_user_turns,
            is_cli_session_row_visible,
            read_importable_agent_session_rows,
        )
    finally:
        sys.path.pop(0)

    rows = read_importable_agent_session_rows(db)
    collapsed = [r for r in rows if r.get("_lineage_root_id")]
    assert len(collapsed) == 1, f"expected one collapsed lineage row, got {[r.get('id') for r in rows]}"
    row = collapsed[0]

    # The DISPLAYED count stays null for a collapsed row.
    assert row.get("actual_user_message_count") is None, (
        "a collapsed row must not display the tip segment's slice as the chain "
        f"total (got {row.get('actual_user_message_count')!r})"
    )
    # ...but the visibility count is the whole-lineage total: 3 + 5 = 8.
    assert row.get("lineage_user_message_count") == 8, (
        f"expected the 3+5 user turns from the mid and tip segments, got "
        f"{row.get('lineage_user_message_count')!r} — the root segment alone "
        "holds none, so a root-only count drops this chain from the sidebar"
    )
    assert _count_user_turns(row) == 8

    # The row the route rechecks must survive the visibility gate.
    assert is_cli_session_row_visible(row) is True, (
        "an ACP chain whose root segment holds no user turns must stay visible"
    )


def test_lineage_count_survives_the_http_row_projection(tmp_path):
    """#7681 finding 1 CORE: the loader's internal count must reach the row the
    route rechecks. models.py builds cli_sessions from the projected rows and
    routes.py runs is_cli_session_row_visible() again at :10882, so a field the
    HTTP row drops is a field the visibility recheck reads as 0."""
    db = tmp_path / "state.db"
    _build_compressed_chain_db(db, acp=True)

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.agent_sessions import (
            is_cli_session_row_visible,
            read_importable_agent_session_rows,
        )
        from api.route_session_list_cache import _SIDEBAR_SESSION_RESPONSE_FIELDS
    finally:
        sys.path.pop(0)

    row = next(r for r in read_importable_agent_session_rows(db) if r.get("_lineage_root_id"))

    # The cache/serialization allowlist must carry the marker through.
    assert "lineage_user_message_count" in _SIDEBAR_SESSION_RESPONSE_FIELDS, (
        "the sidebar response allowlist drops lineage_user_message_count, so the "
        "route's visibility recheck reads it as 0 and removes the chain"
    )
    # And the models.py cli_sessions projection must carry it too.
    models_src = (REPO_ROOT / "api" / "models.py").read_text(encoding="utf-8")
    assert "'lineage_user_message_count': row.get('lineage_user_message_count')," in models_src, (
        "api/models.py's cli_sessions projection drops lineage_user_message_count"
    )

    # The row the route actually rechecks keeps the count.
    http_row = {k: v for k, v in row.items()}
    assert http_row.get("lineage_user_message_count") == 8
    assert is_cli_session_row_visible(http_row) is True


def test_legacy_acp_unknown_count_row_stays_visible(tmp_path):
    """#7681 finding 2 CORE: a state.db with no `role` column returns the ACP row
    on master and dropped it on this branch, because ACP's zero-count rejection
    ran before the unknown escape and `_count_user_turns` reports 0 for NULL."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            content TEXT,
            timestamp REAL
        );
        """
    )
    # Untitled default-looking ACP sessions, three messages each: the shape that
    # must survive when roles are unavailable.
    for sid in ("legacy_a", "legacy_b"):
        conn.execute(
            "INSERT INTO sessions (id, source, session_source, title, model,"
            " started_at, message_count, parent_session_id, ended_at, end_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, "acp", "acp", None, "m", 10.0, 3, None, None, None),
        )
        for i in range(3):
            conn.execute(
                "INSERT INTO messages (id, session_id, content, timestamp)"
                " VALUES (?,?,?,?)",
                (f"{sid}_m{i}", sid, "no role column here", 11.0 + i),
            )
    conn.commit()
    conn.close()

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.agent_sessions import (
            is_cli_session_row_visible,
            read_importable_agent_session_rows,
        )
    finally:
        sys.path.pop(0)

    rows = read_importable_agent_session_rows(db)
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {"legacy_a", "legacy_b"}, (
        f"both legacy ACP sessions must survive the SQL projection, got {sorted(by_id)}"
    )
    for sid, row in by_id.items():
        # The count is genuinely unknown, not a confident zero.
        assert row.get("user_message_count_unknown"), (
            f"{sid}: a state.db without a `role` column must emit the explicit "
            "unknown marker rather than a derived count"
        )
        assert row.get("actual_user_message_count") is None, (
            f"{sid}: the displayed user-turn count must stay NULL when unknown"
        )
        # ...and the row must still be visible.
        assert is_cli_session_row_visible(row) is True, (
            f"{sid}: an unknown count must not be read as a known zero and drop a "
            "real legacy session from the sidebar"
        )


def test_known_zero_turn_count_acp_row_is_still_hidden(tmp_path):
    """Counterpart: a KNOWN zero must keep hiding. The unknown escape must not
    become a blanket pass for every ACP row."""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.agent_sessions import is_cli_session_row_visible
    finally:
        sys.path.pop(0)

    known_zero = {
        "id": "acp_zero",
        "source": "acp",
        "actual_message_count": 4,
        "message_count": 4,
        "actual_user_message_count": 0,
        "user_message_count_unknown": False,
    }
    assert is_cli_session_row_visible(known_zero) is False, (
        "an ACP row with a KNOWN zero user-turn count must stay hidden"
    )


# ── Finding 5 (re-gate 2026-10-08): the fallback classifier must mirror the agent ──
#
# api/compression_anchor.is_user_originated_turn() is the sidecar's mirror of the
# agent's predicate. It is only consulted when the agent module is NOT importable
# (the try/import path above), which is exactly the deployment where the two
# counters must still agree — so the mirror is pinned against the agent's own
# documented split direction rather than against itself.

_DELIM = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
_PRIOR_HEADER = "[PRIOR CONTEXT — for reference only; not a new message]"
_END_MARKER = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"


_DELIM = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
_PRIOR_HEADER = "[PRIOR CONTEXT — for reference only; not a new message]"
_END_MARKER = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"


@contextlib.contextmanager
def _force_mirror_mode():
    """Context manager yielding the sidecar module with the agent unimportable.

    ``is_user_originated_turn`` delegates to ``agent.context_compressor`` whenever
    it imports cleanly, and the pytest environment puts ``~/.hermes/hermes-agent``
    on ``sys.path``. Without this the mirror never executes and a broken mirror
    passes every test — which is exactly how finding 5 shipped twice.

    The block must STAY installed for the whole ``with`` body: ``importlib.reload``
    re-arms the cached ``_AGENT_*_PREDICATE`` globals, so a block removed before
    the call lets the next lookup import the agent again and short-circuit the
    mirror.
    """
    import builtins
    import importlib

    from api import compression_anchor

    # Re-import from disk so a module cached by an earlier test (or by conftest)
    # cannot serve stale code to a RED control.
    importlib.reload(compression_anchor)
    for name in [n for n in list(sys.modules) if n == "agent" or n.startswith("agent.")]:
        del sys.modules[name]

    compression_anchor._AGENT_USER_PREDICATE = None
    compression_anchor._AGENT_USER_PREDICATE_ATTEMPTED = False
    compression_anchor._AGENT_SPLIT_PREDICATE = None
    compression_anchor._AGENT_SPLIT_PREDICATE_ATTEMPTED = False

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "agent" or name.startswith("agent."):
            raise ImportError(f"{name} blocked: pinning the fallback mirror")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _blocked
    try:
        assert compression_anchor._agent_split_user_originated_turn() is None, (
            "could not block the agent import — the mirror would stay untested"
        )
        yield compression_anchor
    finally:
        builtins.__import__ = real_import
        importlib.invalidate_caches()


def _mirror_predicate():
    """A callable that runs the mirror predicate with the agent blocked.

    The block has to survive the CALL, not just the setup: returning the bare
    function would let the next ``_agent_*`` lookup import the agent again and
    short-circuit the mirror, which is how the finding-5 tests passed with the
    wire bug intact.
    """

    def _blocked_predicate(message):
        sys.path.insert(0, str(REPO_ROOT))
        try:
            with _force_mirror_mode() as anchor:
                return anchor.is_user_originated_turn(message)
        finally:
            sys.path.pop(0)

    return _blocked_predicate


def test_flagged_standalone_summary_is_not_a_user_turn():
    """#7681 finding 5: a flagged summary with no live payload is scaffolding.

    The classifier returned ``not _summary_carrier_has_live_user_ask(...)``, so a
    standalone summary counted as a human turn — inflating every compacted
    session's sidebar count. "Standalone" means the pre-delimiter slice is only
    the reference-only header, exactly as in the agent's split.
    """
    predicate = _mirror_predicate()
    row = {
        "role": "user",
        "content": f"{_PRIOR_HEADER}\n\n{_DELIM}\n\n## Summary\nwe discussed X",
        "_compressed_summary": True,
    }
    assert predicate(row) is False, (
        "a flagged standalone summary (nothing live in front of the delimiter) "
        "must not count as a user turn"
    )


def test_embedded_live_ask_in_front_of_the_delimiter_is_a_user_turn():
    """#7681 finding 5: the live payload sits BEFORE the merged delimiter.

    The agent splits with ``content.split(delim, 1)[0]`` and then strips the
    prior-context header; the mirror read ``split(delim, 1)[1]``, i.e. the summary
    block itself, so a real embedded ask was rejected.
    """
    predicate = _mirror_predicate()
    row = {
        "role": "user",
        "content": (
            f"{_PRIOR_HEADER}\n\nreference only\n\nplease also fix the login bug"
            f"\n\n{_DELIM}\n\n## Summary\nwe discussed X"
        ),
        "_compressed_summary": True,
    }
    assert predicate(row) is True, (
        "a live ask sitting in front of the merged delimiter is a real user "
        "turn and must be counted"
    )


def test_carrier_splitter_alone_uses_the_agent_delimiter_direction():
    """#7681 finding 5: pin the splitter itself, not just the verdict.

    The merged-form direction fix is masked when the row also clears
    `_is_internal_user_row`'s live-ask bypass, so this test calls the splitter
    directly and pins the direction the agent uses (`split(delim, 1)[0]`).
    """
    sys.path.insert(0, str(REPO_ROOT))
    with _force_mirror_mode() as anchor:
        split = anchor._summary_carrier_has_live_user_ask
        # Direction-discriminating shapes: `before` and `after` disagree, so
        # reading the summary block AFTER the delimiter answers these backwards.
        live_ask_then_empty_summary = {
            "role": "user",
            "content": f"{_PRIOR_HEADER}\n\nplease fix the login bug\n\n{_DELIM}\n\n",
            "_compressed_summary": True,
        }
        empty_ask_then_summary = {
            "role": "user",
            "content": f"{_PRIOR_HEADER}\n\n{_DELIM}\n\n## Summary\nwe discussed X",
            "_compressed_summary": True,
        }
        assert split(live_ask_then_empty_summary) is True, (
            "a live ask in front of the delimiter is a user turn even when "
            "nothing follows it — reading `split(delim, 1)[1]` answers False here"
        )
        assert split(empty_ask_then_summary) is False, (
            "a summary block behind the delimiter is scaffolding even when it is "
            "non-empty — reading `split(delim, 1)[1]` answers True here"
        )


def test_splitter_handles_list_shaped_carriers():
    """#7681 finding 5: the splitter must not reject a list payload outright.

    `_summary_carrier_has_live_user_ask` returned False for every list content,
    so a multimodal carrier never reached the live-ask check. The agent walks the
    parts and keeps everything before the delimiter part.
    """
    sys.path.insert(0, str(REPO_ROOT))
    with _force_mirror_mode() as anchor:
        split = anchor._summary_carrier_has_live_user_ask
        live_list = {
            "role": "user",
            "content": [
                {"type": "text", "text": f"{_PRIOR_HEADER}\n\nplease fix the login bug"},
                {"type": "text", "text": f"{_DELIM}\n\n## Summary\nwe discussed X"},
            ],
            "_compressed_summary": True,
        }
        header_only_list = {
            "role": "user",
            "content": [
                {"type": "text", "text": _PRIOR_HEADER},
                {"type": "text", "text": f"{_DELIM}\n\n## Summary\nwe discussed X"},
            ],
            "_compressed_summary": True,
        }
        assert split(live_list) is True, (
            "a list-shaped carrier with a live ask before the delimiter must be "
            "classified, not rejected for its payload shape"
        )
        assert split(header_only_list) is False, (
            "a list-shaped carrier whose pre-delimiter slice is only the header "
            "is scaffolding"
        )


def test_prefix_match_alone_does_not_condemn_a_live_carrier():
    """#7681 finding 5: `_is_internal_user_row` matched the merged prior-context
    header and condemned the whole row, including a live ask in front of it."""
    sys.path.insert(0, str(REPO_ROOT))
    with _force_mirror_mode() as anchor:
        internal = anchor._is_internal_user_row
        live_carrier = {
            "role": "user",
            "content": f"{_PRIOR_HEADER}\n\nplease fix the login bug\n\n{_DELIM}\n\n## Summary\nx",
            "_compressed_summary": True,
        }
        assert internal(live_carrier) is False, (
            "a row that carries a prior-context header AND a live ask is not "
            "scaffolding — condemning it drops every embedded ask"
        )
        header_only = {
            "role": "user",
            "content": f"{_PRIOR_HEADER}\n\n{_DELIM}\n\n## Summary\nx",
            "_compressed_summary": True,
        }
        assert internal(header_only) is True, (
            "a header-only prior-context carrier is still scaffolding"
        )


def test_prior_context_header_alone_is_not_a_live_ask():
    """Counterpart: a slice that is only the reference-only header is scaffolding,
    not a live payload — otherwise the header itself would count as a turn."""
    predicate = _mirror_predicate()
    row = {
        "role": "user",
        "content": f"{_PRIOR_HEADER}\n\n{_DELIM}\n\n## Summary\nwe discussed X",
        "_compressed_summary": True,
    }
    assert predicate(row) is False, (
        "the reference-only prior-context header must not be mistaken for a "
        "live user ask"
    )


def test_legacy_end_marker_form_keeps_the_following_ask():
    """Legacy form (no merged delimiter): the live ask follows the end marker."""
    predicate = _mirror_predicate()
    row = {
        "role": "user",
        "content": f"## Summary\nwe discussed X\n\n{_END_MARKER}\n\nnow do the thing",
        "_compressed_summary": True,
    }
    assert predicate(row) is True, "the ask after the legacy end marker is a user turn"
    standalone = {
        "role": "user",
        "content": f"## Summary\nwe discussed X\n\n{_END_MARKER}\n\n",
        "_compressed_summary": True,
    }
    assert predicate(standalone) is False, "a standalone legacy summary is scaffolding"


def test_list_content_carrier_is_classified_not_rejected():
    """#7681 finding 5: the mirror returned False for every list payload, so a
    multimodal carrier never reached the live-ask check at all."""
    predicate = _mirror_predicate()
    carrier = {
        "role": "user",
        "content": [
            {"type": "text", "text": f"{_PRIOR_HEADER}\n\nreference\n\nnow fix the bug"},
            {"type": "text", "text": f"{_DELIM}\n\n## Summary\nwe discussed X"},
        ],
        "_compressed_summary": True,
    }
    assert predicate(carrier) is True, (
        "a list-shaped carrier with a live ask in front of the delimiter must "
        "be classified, not rejected for its payload shape"
    )
    # The agent keeps the pre-delimiter blocks after stripping only the header,
    # so a block of reference text still counts as the carrier's live payload —
    # the mirror must not be stricter than the source of truth.
    reference_only_list = {
        "role": "user",
        "content": [
            {"type": "text", "text": f"{_PRIOR_HEADER}\n\nreference"},
            {"type": "text", "text": f"{_DELIM}\n\n## Summary\nwe discussed X"},
        ],
        "_compressed_summary": True,
    }
    assert predicate(reference_only_list) is True, (
        "the agent keeps pre-delimiter blocks after stripping the header, so a "
        "reference block is still the carrier's live payload"
    )
    # A header-only pre-delimiter slice leaves nothing, which IS scaffolding.
    header_only_list = {
        "role": "user",
        "content": [
            {"type": "text", "text": _PRIOR_HEADER},
            {"type": "text", "text": f"{_DELIM}\n\n## Summary\nwe discussed X"},
        ],
        "_compressed_summary": True,
    }
    assert predicate(header_only_list) is False, (
        "a pre-delimiter slice that is only the reference-only header is scaffolding"
    )


def test_summary_carrier_classification_reads_the_unwrapped_payload():
    """#7681 finding 3b: a summary row is classified from its UNWRAPPED payload.

    The mirror used to reject every summary row, so a carrier that still embeds a
    live ask was invisible while the Agent counted it. Pins the branch directly so
    the verdict cannot be reached through `_is_internal_user_row` instead.
    """
    sys.path.insert(0, str(REPO_ROOT))
    with _force_mirror_mode() as anchor:
        _run = anchor.is_user_originated_turn
        _is_summary = anchor._is_context_summary_message

        live = {
            "role": "user",
            "content": "[PRIOR CONTEXT — for reference only; not a new message]"
                       "\n\nplease also fix the login bug"
                       "\n\n[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
                       "\n\n## Summary\nwe discussed X",
            "_compressed_summary": True,
        }
        standalone = {
            "role": "user",
            "content": "[PRIOR CONTEXT — for reference only; not a new message]"
                       "\n\n[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
                       "\n\n## Summary\nwe discussed X",
            "_compressed_summary": True,
        }
        assert _is_summary(live) is True
        assert _is_summary(standalone) is True
        assert _run(live) is True, (
            "a summary carrier with a live payload in front of the delimiter is "
            "a user turn — the blanket rejection under-counted it"
        )
        assert _run(standalone) is False, (
            "a standalone summary carrier is scaffolding"
        )


def test_blank_user_echo_is_not_a_user_turn():
    """#7681 finding 3: the Agent's `_is_blank_user_turn` excludes blank echoes.

    Without it an empty row persisted for alternation counts as a human turn and
    the aggregate over-reports.
    """
    predicate = _mirror_predicate()
    assert predicate({"role": "user", "content": "   "}) is False
    assert predicate({"role": "user", "content": ""}) is False
    assert predicate({"role": "user", "content": None}) is False
    assert predicate({"role": "user", "content": [{"type": "text", "text": "  "}]}) is False
    # #7681 media-only re-gate: the OpenAI ``image_url`` shape is REAL payload
    # to the agent's ``_blank_part``, so an image_url-only row is a human turn
    # the Agent counts (verified: the installed predicate returns True for
    # ``{'type':'image_url','image_url':{'url':'data:image/png;base64,...'}}``
    # and even for an empty ``image_url`` dict). The mirror must agree.
    assert predicate({"role": "user", "content": [{"type": "image_url", "image_url": {}}]}) is True


def test_blank_user_turn_predicate_itself_excludes_empty_payloads():
    """#7681 finding 3: pin `_is_blank_user_turn` directly.

    The verdict-level test cannot discriminate this branch: an empty row is also
    caught by `_is_internal_user_row`'s "no text" guard, so removing the
    blank-echo branch leaves the verdict unchanged. Pin the predicate itself so
    the branch is not dead code that only looks covered.
    """
    sys.path.insert(0, str(REPO_ROOT))
    with _force_mirror_mode() as anchor:
        blank = anchor._is_blank_user_turn
        assert blank({"role": "user", "content": "   "}) is True
        assert blank({"role": "user", "content": ""}) is True
        assert blank({"role": "user", "content": None}) is True
        assert blank({"role": "user", "content": [{"type": "text", "text": "  "}]}) is True
        assert blank({"role": "user", "content": "a real ask"}) is False
        # A non-text part is real payload, not a blank echo.
        assert blank({"role": "user", "content": [{"type": "image_url", "image_url": {}}]}) is False


# ── Re-gate 2026-10-12: the two supplement carrier shapes (finding 5) ──
#
# The 2026-10-12 re-warm review: `_summary_carrier_has_live_user_ask` /
# `_live_payload_after_prior_context` still tested nonblank text instead of
# classifying the unwrapped candidate. Both assertions run the production
# predicate with the agent import disabled for the ENTIRE call
# (`_mirror_predicate` re-enters the blocked context per call), matching the
# agent's installed source semantics: strip-before-header
# (context_compressor.py:4641-4643), classify the unwrapped candidate
# (:5860-5863), exclude the exact continuation marker (:4444-4446).

_CONTINUATION_MARKER = (
    "Continue from the compressed conversation context above. "
    "This marker exists because no human user turn was available."
)


def test_exact_continuation_marker_payload_is_not_a_live_ask():
    """#7681 finding 5 supplement 1: the exact continuation-marker payload must
    return False with the agent import disabled for the entire predicate call.

    The mirror used to test nonblank text, so a flagged merged carrier whose
    pre-delimiter slice is only the continuation marker counted as a live ask.
    The agent's `_is_synthetic_compression_user_turn` excludes the exact marker
    (context_compressor.py:4444-4446); the unwrapped candidate must be
    CLASSIFIED, not just checked for text.
    """
    predicate = _mirror_predicate()
    row = {
        "role": "user",
        "content": (
            f"{_CONTINUATION_MARKER}"
            f"\n\n{_DELIM}\n\n## Summary\nwe discussed X"
        ),
        "_compressed_summary": True,
    }
    assert predicate(row) is False, (
        "a carrier whose pre-delimiter payload is exactly the continuation "
        "marker is scaffolding in the agent's classifier, not a user turn"
    )


def test_leading_whitespace_plus_header_only_payload_is_not_a_live_ask():
    """#7681 finding 5 supplement 2: strip whitespace BEFORE removing the header.

    A leading tab/newline followed only by the prior-context header used to
    count as a live ask because the header check ran before the strip. The
    agent strips the pre-delimiter slice first (context_compressor.py:4641),
    then removes the header (:4642-4643) — leaving nothing.
    """
    predicate = _mirror_predicate()
    row = {
        "role": "user",
        "content": (
            f"\n\t{_PRIOR_HEADER}"
            f"\n\n{_DELIM}\n\n## Summary\nwe discussed X"
        ),
        "_compressed_summary": True,
    }
    assert predicate(row) is False, (
        "whitespace followed by only the reference-only header is an empty "
        "payload, not a live ask"
    )


# ── Finding 3 (re-gate 2026-10-08): recovery nudges and summary carriers ──
#
# Two defects made the SQL aggregate disagree with the Agent's own count on the
# 23-shape reproduction (7 reported vs 6 counted):
#   a. recovery nudges were still counted as user turns;
#   b. live asks embedded in summary carriers were still excluded.
# Both are pinned here against the agent's classifier, not against webui itself.

# Resolved portably (#7681 re-gate finding 3): the previous hard-coded
# /home/xiaobao paths made the drift guard skip everywhere but the
# contributor's own machine — CI included — so the guard never ran there.
# HERMES_HOME (an explicit agent/env root) and the standard installed
# location under ~/.hermes are the authoritative sources; the oracle is the
# INSTALLED Agent the webui actually runs against.
def _resolve_agent_src_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        candidates.append(Path(hermes_home) / "hermes-agent" / "agent")
        candidates.append(Path(hermes_home) / "agent")
    candidates.append(Path.home() / ".hermes" / "hermes-agent" / "agent")
    return tuple(candidates)

_RECOVERY_NUDGES = {
    "length_continuation_network_stub": (
        "[System: The previous response was cut off by a network error mid-stream — a transport "
        "interruption, NOT a change in your capabilities. Your tools are still fully available; "
        "call them as normal and ignore any earlier claim that you lack tool access. Continue the "
        "task from where you left off. Do not restart or repeat prior text.]"
    ),
    "length_continuation_output_limit": (
        "[System: Your previous response was truncated by the output length limit. Continue "
        "exactly where you left off. Do not restart or repeat prior text. Finish the answer "
        "directly.]"
    ),
    "length_continuation_dropped_tools": (
        "[System: Your previous tool call (Read) was too large and the stream timed out before it "
        "could be delivered. Do NOT retry the same tool call with the same large content. Instead, "
        "break the content into multiple smaller tool calls (e.g. use multiple patch calls or write "
        "smaller files). Each tool call's arguments must be under ~8K tokens to avoid stream "
        "timeouts. The cut was a transport interruption, not a capability change — your tools "
        "remain fully available.]"
    ),
    "codex_incomplete_nudge": (
        "[System: Your previous response contained only internal reasoning and never produced a "
        "visible answer or tool call. Do not keep thinking. Produce your final answer as plain text "
        "now (or make the tool call you were planning).]"
    ),
    "codex_ack_continuation_nudge": (
        "[System: Continue now. Execute the required tool calls and only send your final answer "
        "after completing the task.]"
    ),
    "degenerate_final_nudge": (
        "[System: Your previous message ended the turn with a fragment that is not a usable answer. "
        "If the task is unfinished, continue it and then give the complete answer. If that fragment "
        "WAS your complete answer, send it again exactly as before.]"
    ),
    "dropped_toolcall_nudge": (
        "Your previous turn indicated a tool call but none was included. Do not narrate a plan or "
        "restate intent — issue the actual tool call now to continue the task."
    ),
    "empty_tool_response_nudge": (
        "You just executed tool calls but returned an empty response. Please process the tool "
        "results above and continue with the task."
    ),
}


def test_recovery_nudges_are_excluded_from_the_user_turn_count():
    """#7681 finding 3a: the Agent excludes every recovery nudge; so must we.

    The literals below are read from the agent source when it is available, so the
    table cannot drift silently — a renamed nudge fails this test instead of
    quietly inflating the sidebar count. Both the Python classifier AND the SQL
    aggregate are pinned, because the review's 7-vs-6 came from the SQL side.
    """
    from api.compression_anchor import user_turn_sql_exclusions

    predicate = _mirror_predicate()
    for name, content in _RECOVERY_NUDGES.items():
        assert predicate({"role": "user", "content": content}) is False, (
            f"{name} is scaffolding in the Agent's classifier but counts as a user "
            "turn here — the SQL aggregate over-reports"
        )
    # The counterpart: a real ask is still a user turn.
    assert predicate({"role": "user", "content": "Please fix the login bug"}) is True

    # The SQL twin must agree row for row, or the state.db aggregate and the
    # sidecar counter drift apart — which is the defect this finding is about.
    sql_excludes = user_turn_sql_exclusions(
        "LOWER(LTRIM(COALESCE(m.content, '')))"
    )
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE m (content TEXT)")
    for name, content in _RECOVERY_NUDGES.items():
        conn.execute("DELETE FROM m")
        conn.execute("INSERT INTO m (content) VALUES (?)", (content,))
        excluded = conn.execute(f"SELECT {sql_excludes} FROM m").fetchone()[0]
        assert bool(excluded), (
            f"{name}: the Python classifier excludes it but the SQL aggregate "
            "still counts it — the two counters disagree (#7681 finding 3)"
        )


def test_recovery_nudge_literals_still_match_the_agent_source():
    """Guard against the agent rewording a nudge we mirror by hand.

    The literals in api/compression_anchor.py are hand-copied, so the only way to
    notice the agent changed one is to compare against the agent's own constants.
    They are read with ``ast`` rather than imported: the values are multi-line
    concatenated string literals that never appear verbatim in the file text, and
    importing agent.conversation_loop pulls third-party deps this environment may
    not have.
    """
    import ast

    agent_constants: dict[str, str] = {}
    candidates = _resolve_agent_src_candidates()
    agent_checkout_present = False
    for base in candidates:
        agent_checkout_present = agent_checkout_present or base.is_dir()
        loop_py = base / "conversation_loop.py"
        if not loop_py.exists():
            continue
        tree = ast.parse(loop_py.read_text(encoding="utf-8"), filename=str(loop_py))
        for node in tree.body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
                continue
            if not isinstance(node.value.value, str):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    agent_constants[target.id] = node.value.value
        break

    if not agent_constants:
        # #7681 re-gate finding 3: when a resolved candidate ACTUALLY
        # contains an agent checkout (a present ``agent/`` dir), the guard
        # must FAIL rather than skip — a silently skipped drift guard is the
        # failure mode this test exists to prevent. Skip is reserved for
        # genuinely agent-less environments: plain CI without an Agent
        # install, and the webui test sandbox (the harness points
        # HERMES_HOME at a temp dir that contains no checkout).
        if agent_checkout_present:
            raise AssertionError(
                "resolved agent source(s) contain a checkout but none has "
                f"conversation_loop.py at {[str(c) for c in candidates]} — "
                "the Agent is present, so the drift guard must run"
            )
        import pytest

        pytest.skip("agent conversation_loop.py not available in this environment")

    expected_sources = {
        "length_continuation_network_stub": "_LENGTH_CONTINUATION_NETWORK_STUB",
        "length_continuation_output_limit": "_LENGTH_CONTINUATION_OUTPUT_LIMIT",
        "codex_incomplete_nudge": "_CODEX_INCOMPLETE_NUDGE",
        "codex_ack_continuation_nudge": "_CODEX_ACK_CONTINUATION_NUDGE",
        "degenerate_final_nudge": "_DEGENERATE_FINAL_NUDGE",
        "dropped_toolcall_nudge": "_DROPPED_TOOLCALL_NUDGE_CONTENT",
        "empty_tool_response_nudge": "_EMPTY_TOOL_RESPONSE_NUDGE",
    }
    missing = [attr for attr in expected_sources.values() if attr not in agent_constants]
    assert not missing, (
        "the agent no longer defines the mirrored nudge constant(s) "
        f"{missing} in the resolved agent source — the literal mirror in "
        "api/compression_anchor.py may have drifted; update both sides"
    )
    for name, attr in expected_sources.items():
        assert _RECOVERY_NUDGES[name] == agent_constants[attr], (
            f"{name}: the literal mirrored in api/compression_anchor.py no longer "
            f"equals the agent's {attr} — the exclusion is stale"
        )


def test_live_ask_inside_a_summary_carrier_counts_as_a_user_turn(tmp_path):
    """#7681 finding 3b: the SQL blanket-excluded every `_compressed_summary` row.

    The Agent unwraps the carrier and keeps the live ask in front of the merged
    delimiter, so the aggregate must count it too.
    """
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            _compressed_summary INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, session_source, title, model, started_at,"
        " message_count, parent_session_id, ended_at, end_reason)"
        " VALUES ('s1', 'cli', 'cli', 'S', 'm', 10.0, 4, NULL, NULL, NULL)"
    )
    rows = [
        # plain user turn
        ("m0", "user", "first real question", 0),
        # a recovery nudge: must NOT count
        ("m1", "user", "[System: Continue now. Execute the required tool calls and"
                      " only send your final answer after completing the task.]", 0),
        # a standalone summary carrier: must NOT count
        ("m2", "user", "[PRIOR CONTEXT — for reference only; not a new message]"
                       "\n\n[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
                       "\n\n## Summary\nwe discussed X", 1),
        # a carrier WITH a live ask in front of the delimiter: MUST count
        ("m3", "user", "[PRIOR CONTEXT — for reference only; not a new message]"
                       "\n\nplease also fix the login bug"
                       "\n\n[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
                       "\n\n## Summary\nwe discussed X", 1),
    ]
    for mid, role, content, flag in rows:
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp,"
            " _compressed_summary) VALUES (?,?,?,?,?,?)",
            (mid, "s1", role, content, 11.0, flag),
        )
    conn.commit()
    conn.close()

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.agent_sessions import read_importable_agent_session_rows
    finally:
        sys.path.pop(0)

    by_id = {row["id"]: row for row in read_importable_agent_session_rows(db)}
    assert "s1" in by_id, f"row missing: {list(by_id)}"
    assert by_id["s1"]["actual_user_message_count"] == 2, (
        f"expected 2 user turns (the plain ask + the carrier's embedded live ask), "
        f"got {by_id['s1']['actual_user_message_count']} — the recovery nudge and the "
        "standalone summary must stay excluded (#7681 finding 3)"
    )


# ── Re-gate 2026-10-12: SQL carrier classification (finding 3) ──
#
# The 2026-10-12 re-warm review: the production SQLite loader still applied the
# raw display-kind guard and a nonblank payload test, so three shapes came out
# wrong. Each is pinned against the agent's source semantics
# (context_compressor.py:4641-4643 strip-before-header, :4444-4446
# exclude-the-exact-marker):
#   1. a hidden legacy summary carrier with a real ask after its end marker
#      counts (display_kind='hidden' is the legacy compaction wrapper);
#   2. a flagged merged carrier whose pre-delimiter payload is only the exact
#      continuation marker does NOT count;
#   3. leading whitespace followed only by the prior-context header does NOT
#      count.


def _sql_carrier_classification_db(path):
    """One-session state.db whose user rows exercise the three finding-3 SQL shapes."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, session_source TEXT, title TEXT,
            model TEXT, started_at REAL NOT NULL, message_count INTEGER DEFAULT 0,
            parent_session_id TEXT, ended_at REAL, end_reason TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            timestamp REAL, _compressed_summary INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1, display_kind TEXT
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, session_source, title, model, started_at,"
        " message_count, parent_session_id, ended_at, end_reason)"
        " VALUES ('s1', 'cli', 'cli', 'S', 'm', 10.0, 6, NULL, NULL, NULL)"
    )
    rows = [
        # A plain human ask: MUST count.
        ("m0", "user", "first real question", 0, None),
        # Hidden legacy summary carrier with a real ask after its end marker:
        # MUST count (assertion 1 — the raw display-kind guard rejected every
        # 'hidden' row regardless of payload).
        (
            "m1", "user",
            f"## Summary\nwe discussed X\n\n{_END_MARKER}\n\nplease fix the login bug",
            0, "hidden",
        ),
        # Flagged merged carrier whose pre-delimiter payload is ONLY the exact
        # continuation marker: must NOT count (assertion 2).
        (
            "m2", "user",
            f"{_CONTINUATION_MARKER}\n\n{_DELIM}\n\n## Summary\nwe discussed X",
            1, None,
        ),
        # Leading tab/newline followed only by the prior-context header:
        # must NOT count (assertion 3).
        (
            "m3", "user",
            f"\n\t{_PRIOR_HEADER}\n\n{_DELIM}\n\n## Summary\nwe discussed X",
            1, None,
        ),
    ]
    for mid, role, content, flag, display_kind in rows:
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp,"
            " _compressed_summary, active, display_kind) VALUES (?,?,?,?,?,?,?,?)",
            (mid, "s1", role, content, 11.0, flag, 1, display_kind),
        )
    conn.commit()
    conn.close()


def test_sql_carrier_classification_pins_the_three_finding_3_shapes(tmp_path):
    """#7681 finding 3 (re-gate 2026-10-12): the SQLite loader must classify the
    unwrapped carrier payload, not apply a raw display-kind guard or a nonblank
    payload test."""
    db = tmp_path / "state.db"
    _sql_carrier_classification_db(db)

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.agent_sessions import read_importable_agent_session_rows
    finally:
        sys.path.pop(0)

    by_id = {row["id"]: row for row in read_importable_agent_session_rows(db)}
    assert "s1" in by_id, f"row missing: {list(by_id)}"
    assert by_id["s1"]["actual_user_message_count"] == 2, (
        "expected 2: the plain ask + the hidden legacy carrier's embedded ask; "
        "the marker-only carrier and the whitespace+header-only carrier are "
        f"scaffolding, got {by_id['s1']['actual_user_message_count']}"
    )


# ── Finding 4 (re-gate 2026-10-08): inactive-row detection ──
#
# The flag keyed on `_compressed_summary`, so `/undo` (which soft-archives with
# `active=0` and leaves the summary flag at 0) was invisible and the sidebar kept
# rendering a count that included the rewound turns — 2 versus 1 after
# `rewind_to_message`, 9 versus 7 after `archive_and_compact`.


def _inactive_flag_db(path, *, active_values, summary_flags=None):
    """A one-session state.db whose user rows carry the given active/summary pairs."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            active INTEGER NOT NULL DEFAULT 1,
            _compressed_summary INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, session_source, title, model, started_at,"
        " message_count, parent_session_id, ended_at, end_reason)"
        " VALUES ('s1', 'cli', 'cli', 'S', 'm', 10.0, 4, NULL, NULL, NULL)"
    )
    summary_flags = summary_flags or [0] * len(active_values)
    for i, (active, flag) in enumerate(zip(active_values, summary_flags, strict=True)):
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp, active,"
            " _compressed_summary) VALUES (?,?,?,?,?,?,?)",
            (f"m{i}", "s1", "user", f"turn {i}", 11.0 + i, active, flag),
        )
    conn.commit()
    conn.close()


def test_rewound_user_rows_flag_the_session(tmp_path):
    """#7681 finding 4: `/undo` soft-archives with `active=0` and leaves
    `_compressed_summary` at 0, so the old predicate never fired."""
    db = tmp_path / "state.db"
    # turn 0 was rewound away; turn 1 is still live.
    _inactive_flag_db(db, active_values=[0, 1], summary_flags=[0, 0])

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.agent_sessions import read_importable_agent_session_rows
    finally:
        sys.path.pop(0)

    by_id = {row["id"]: row for row in read_importable_agent_session_rows(db)}
    assert by_id["s1"].get("has_inactive_user_rows"), (
        "a session with a rewound (active=0) user row must be flagged so the "
        "client can suppress the inflated count"
    )


def test_compacted_user_rows_still_flag_the_session(tmp_path):
    """Counterpart: an in-place compaction archives with active=0 too."""
    db = tmp_path / "state.db"
    _inactive_flag_db(db, active_values=[0, 1], summary_flags=[1, 0])

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.agent_sessions import read_importable_agent_session_rows
    finally:
        sys.path.pop(0)

    by_id = {row["id"]: row for row in read_importable_agent_session_rows(db)}
    assert by_id["s1"].get("has_inactive_user_rows"), (
        "a compacted session must still be flagged"
    )


def test_all_active_session_is_not_flagged(tmp_path):
    """The flag must not fire for a healthy session, or every count is hidden."""
    db = tmp_path / "state.db"
    _inactive_flag_db(db, active_values=[1, 1, 1])

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.agent_sessions import read_importable_agent_session_rows
    finally:
        sys.path.pop(0)

    by_id = {row["id"]: row for row in read_importable_agent_session_rows(db)}
    assert not by_id["s1"].get("has_inactive_user_rows"), (
        "a session whose user rows are all active must not be flagged"
    )


def test_inactive_row_flag_survives_the_response_projection():
    """#7681 finding 4: the flag was dropped by the model projection and the
    response allowlist, so it never reached the client that is supposed to act
    on it."""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.route_session_list_cache import _SIDEBAR_SESSION_RESPONSE_FIELDS
    finally:
        sys.path.pop(0)

    assert "has_inactive_user_rows" in _SIDEBAR_SESSION_RESPONSE_FIELDS, (
        "the sidebar response allowlist drops has_inactive_user_rows, so the "
        "client's render gate never sees it"
    )
    models_src = (REPO_ROOT / "api" / "models.py").read_text(encoding="utf-8")
    assert "'has_inactive_user_rows': row.get('has_inactive_user_rows')," in models_src, (
        "api/models.py's cli_sessions projection drops has_inactive_user_rows"
    )


def test_missing_content_column_emits_null_not_a_role_based_guess(tmp_path):
    """#7681 finding 3: "emit NULL for unsupported shapes".

    A ``messages`` table without a ``content`` column cannot run any of the
    synthetic-content heuristics, so a role-based count would include the
    continuation marker, the process-wakeup notice, the max-iterations request and
    the recovery nudges. That is the over-report the finding is about, so the
    aggregate must report UNKNOWN rather than a confident wrong number.
    """
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            timestamp REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, session_source, title, model, started_at,"
        " message_count, parent_session_id, ended_at, end_reason)"
        " VALUES ('noc', 'cli', 'cli', 'No Content', 'm', 10.0, 4, NULL, NULL, NULL)"
    )
    for i, role in enumerate(["user", "assistant", "user", "user"]):
        conn.execute(
            "INSERT INTO messages (id, session_id, role, timestamp) VALUES (?,?,?,?)",
            (f"m{i}", "noc", role, 11.0 + i),
        )
    conn.commit()
    conn.close()

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.agent_sessions import is_cli_session_row_visible, read_importable_agent_session_rows
    finally:
        sys.path.pop(0)

    by_id = {row["id"]: row for row in read_importable_agent_session_rows(db)}
    assert "noc" in by_id, f"row missing: {list(by_id)}"
    assert by_id["noc"]["actual_user_message_count"] is None, (
        "a schema without a content column cannot classify synthetic rows, so the "
        f"user-turn count must be NULL, got {by_id['noc']['actual_user_message_count']!r}"
    )
    assert by_id["noc"]["user_message_count_unknown"], (
        "the unknown marker must accompany the NULL count"
    )
    # And the row must not disappear from the sidebar because of it.
    assert is_cli_session_row_visible(by_id["noc"]) is True, (
        "an unknown count must not hide a real session"
    )


# ── Finding 4 (re-gate 2026-10-08): inactive-row detection ──
#
# The Agent soft-archives a row with ``active = 0`` for BOTH compaction and
# ``/undo``; a rewound row is ``active=0, _compressed_summary=0``, so keying the
# flag on the summary flag alone missed every rewind. Chromium showed 9 vs 7
# after ``archive_and_compact`` and 2 vs 1 after ``rewind_to_message``.


def test_inactive_user_rows_are_detected_including_rewinds(tmp_path):
    """#7681 finding 4: the flag must key on ``active = 0``, which covers /undo.

    Uses a REWIND-ONLY database (``active=0, _compressed_summary=0``): the old
    summary-flag predicate sees nothing there, so this shape is what actually
    discriminates the two predicates.
    """
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, session_source TEXT, title TEXT,
            model TEXT, started_at REAL NOT NULL, message_count INTEGER DEFAULT 0,
            parent_session_id TEXT, ended_at REAL, end_reason TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            timestamp REAL, _compressed_summary INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, session_source, title, model, started_at,"
        " message_count, parent_session_id, ended_at, end_reason)"
        " VALUES ('rw', 'cli', 'cli', 'S', 'm', 10.0, 3, NULL, NULL, NULL)"
    )
    # m1 is the /undo rewind: active=0 with NO summary flag.
    for mid, role, content, flag, active in [
        ("m0", "user", "first ask", 0, 1),
        ("m1", "user", "rewound by /undo", 0, 0),
        ("m2", "user", "third ask", 0, 1),
    ]:
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp,"
            " _compressed_summary, active) VALUES (?,?,?,?,?,?,?)",
            (mid, "rw", role, content, 11.0, flag, active),
        )
    conn.commit()
    conn.close()

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.agent_sessions import read_importable_agent_session_rows
    finally:
        sys.path.pop(0)

    by_id = {row["id"]: row for row in read_importable_agent_session_rows(db)}
    assert "rw" in by_id, f"row missing: {list(by_id)}"
    assert by_id["rw"]["has_inactive_user_rows"], (
        "a session holding an archived (active=0) user row must carry the "
        "inactive-row flag so the client can suppress the inflated count"
    )


def test_active_only_session_is_not_flagged(tmp_path):
    """Counterpart: a session whose user rows are all active must NOT be flagged,
    otherwise every count in the sidebar would be suppressed."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, session_source TEXT, title TEXT,
            model TEXT, started_at REAL NOT NULL, message_count INTEGER DEFAULT 0,
            parent_session_id TEXT, ended_at REAL, end_reason TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            timestamp REAL, _compressed_summary INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, session_source, title, model, started_at,"
        " message_count, parent_session_id, ended_at, end_reason)"
        " VALUES ('s2', 'cli', 'cli', 'S', 'm', 10.0, 2, NULL, NULL, NULL)"
    )
    for mid, content in [("n0", "first ask"), ("n1", "second ask")]:
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp,"
            " _compressed_summary, active) VALUES (?,?,?,?,?,?,?)",
            (mid, "s2", "user", content, 11.0, 0, 1),
        )
    conn.commit()
    conn.close()

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.agent_sessions import read_importable_agent_session_rows
    finally:
        sys.path.pop(0)

    by_id = {row["id"]: row for row in read_importable_agent_session_rows(db)}
    assert not by_id["s2"]["has_inactive_user_rows"], (
        "a session with only active user rows must not be flagged"
    )


def test_inactive_row_flag_survives_the_http_row_projection():
    """#7681 finding 4: the flag was dropped by the model projection and the
    response allowlist, so the client never saw it."""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.route_session_list_cache import _SIDEBAR_SESSION_RESPONSE_FIELDS
    finally:
        sys.path.pop(0)

    assert "has_inactive_user_rows" in _SIDEBAR_SESSION_RESPONSE_FIELDS, (
        "the sidebar response allowlist drops has_inactive_user_rows, so the "
        "client cannot suppress the inflated count"
    )
    models_src = (REPO_ROOT / "api" / "models.py").read_text(encoding="utf-8")
    assert "'has_inactive_user_rows': row.get('has_inactive_user_rows')," in models_src, (
        "api/models.py's cli_sessions projection drops has_inactive_user_rows"
    )


# ── Re-gate 2026-10-12: every-loader propagation (finding 4) ──
#
# The 2026-10-12 re-warm review: the additive cron and webhook projections in
# api/models.py dropped `has_inactive_user_rows` while the primary CLI
# projection kept it, so a compacted/rewound background session rendered an
# inflated count. Historical counting stays separate from label suppression.


def test_inactive_row_flag_survives_the_cron_projection(monkeypatch, tmp_path):
    """#7681 finding 4 (re-gate 2026-10-12): the additive cron projection dropped
    has_inactive_user_rows while the primary CLI projection kept it."""
    import api.models as models

    db = tmp_path / "state.db"
    db.write_text("", encoding="utf-8")

    def fake_read_rows(_db_path, **kwargs):
        if kwargs.get("include_sources") == ("cron",):
            return [{
                "id": "cron-1", "source": "cron", "session_source": "cron",
                "title": "Cron run", "model": "m", "started_at": 10.0,
                "last_activity": 11.0, "message_count": 3,
                "actual_message_count": 3, "actual_user_message_count": 2,
                "archived": 0, "has_inactive_user_rows": True,
            }]
        return []

    monkeypatch.setattr(models, "read_importable_agent_session_rows", fake_read_rows)
    monkeypatch.setattr(models, "get_last_workspace", lambda profile=None: tmp_path)
    monkeypatch.setattr(models, "ensure_cron_project", lambda **_kw: "cron-project-id")
    monkeypatch.setattr(models, "ensure_webhook_project", lambda **_kw: "webhook-project-id", raising=False)
    monkeypatch.setattr(models.Session, "load_metadata_only", lambda _sid: None)

    rows = models._load_cli_sessions_uncached(tmp_path, db, _cli_profile=None)
    by_id = {r["session_id"]: r for r in rows}
    assert "cron-1" in by_id, f"cron row missing: {list(by_id)}"
    assert by_id["cron-1"]["has_inactive_user_rows"] is True, (
        "the cron projection drops has_inactive_user_rows, so a compacted cron "
        "session's inflated count cannot be suppressed"
    )


def test_inactive_row_flag_survives_the_webhook_projection(monkeypatch, tmp_path):
    """#7681 finding 4 (re-gate 2026-10-12): the additive webhook projection
    dropped has_inactive_user_rows as well."""
    import api.models as models

    db = tmp_path / "state.db"
    db.write_text("", encoding="utf-8")

    def fake_read_rows(_db_path, **kwargs):
        if kwargs.get("include_sources") == ("webhook",):
            return [{
                "id": "webhook-1", "source": "webhook", "session_source": "webhook",
                "title": "Webhook run", "model": "m", "started_at": 10.0,
                "last_activity": 11.0, "message_count": 3,
                "actual_message_count": 3, "actual_user_message_count": 2,
                "archived": 0, "has_inactive_user_rows": True,
            }]
        return []

    monkeypatch.setattr(models, "read_importable_agent_session_rows", fake_read_rows)
    monkeypatch.setattr(models, "get_last_workspace", lambda profile=None: tmp_path)
    monkeypatch.setattr(models, "ensure_cron_project", lambda **_kw: "cron-project-id")
    monkeypatch.setattr(models, "ensure_webhook_project", lambda **_kw: "webhook-project-id", raising=False)
    monkeypatch.setattr(models.Session, "load_metadata_only", lambda _sid: None)

    rows = models._load_cli_sessions_uncached(tmp_path, db, _cli_profile=None)
    by_id = {r["session_id"]: r for r in rows}
    assert "webhook-1" in by_id, f"webhook row missing: {list(by_id)}"
    assert by_id["webhook-1"]["has_inactive_user_rows"] is True, (
        "the webhook projection drops has_inactive_user_rows, so a compacted "
        "webhook session's inflated count cannot be suppressed"
    )


# ── re-gate 2026-10-29 finding 1: list-encoded (multimodal) summary carriers ──
#
# The Agent persists list content as the sentinel `\0json:` + `json.dumps(...)`
# with ASCII escaping, so the merged-summary delimiter's em dash is stored as
# `\u2014` and the plaintext INSTR predicates can never match that row. The
# Agent's `_merge_summary_into_tail_row` keeps list content, so production
# compaction folds into exactly this shape. The maintainer's oracle counts the
# four shapes below 1/1/0/0; the webui counted 0/0/1/1. Both the state.db SQL
# aggregate and the sidecar predicate must decode sentinel rows before the
# carrier / blank guards run.
_CONTENT_JSON_PREFIX = "\x00json:"


def _sentinel_list_content(parts: list) -> str:
    """The agent's durable spelling of list content (hermes_state SessionDB)."""
    return _CONTENT_JSON_PREFIX + json.dumps(parts)


def _list_summary_carrier(ask_parts: list) -> list:
    """A merged prior-context carrier whose content is a multimodal part list."""
    header = {"type": "text", "text": _PRIOR_HEADER + "\n"}
    suffix = {
        "type": "text",
        "text": "\n\n" + _DELIM + "\n\n## Summary\nwe discussed X\n\n" + _END_MARKER,
    }
    return [header] + ask_parts + [suffix]


def _list_carrier_session_db(tmp_path, rows):
    """Real-schema state.db with one session; returns {id: row} from the
    route's own reader plus the reader's row list (presence == in /api/sessions)."""
    import sys as _sys

    _sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.agent_sessions import read_importable_agent_session_rows
    finally:
        _sys.path.pop(0)
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            _compressed_summary INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, session_source, title, model, started_at,"
        " message_count, parent_session_id, ended_at, end_reason)"
        " VALUES ('s1', 'cli', 'cli', 'S', 'm', 10.0, %d, NULL, NULL, NULL)" % len(rows)
    )
    for mid, role, content, flag in rows:
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp,"
            " _compressed_summary) VALUES (?,?,?,?,?,?)",
            (mid, "s1", role, content, 11.0, flag),
        )
    conn.commit()
    conn.close()
    listed = read_importable_agent_session_rows(db)
    return {r["id"]: r for r in listed}, listed


_IMAGE_ASK = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}]
_TEXT_ASK = [{"type": "text", "text": "please also fix the login bug"}]


def test_list_encoded_text_ask_carrier_counts_and_stays_in_sessions(tmp_path):
    """The maintained shape 1: a merged carrier whose live ask is TEXT, stored
    as sentinel-encoded list content, counts 1 and its session stays in the
    /api/sessions reader output (agent oracle: 1)."""
    rows = [
        ("m0", "user", "first real question", 0),
        ("m1", "user", _sentinel_list_content(_list_summary_carrier(_TEXT_ASK)), 1),
    ]
    by_id, listed = _list_carrier_session_db(tmp_path, rows)
    assert "s1" in by_id, f"carrier session missing from the listing: {[r['id'] for r in listed]}"
    assert by_id["s1"]["actual_user_message_count"] == 2, (
        "expected 2 (the plain ask + the list-encoded carrier's live text ask); "
        "got %s — the sentinel row was not decoded before the carrier guards"
        % by_id["s1"]["actual_user_message_count"]
    )


def test_list_encoded_image_only_ask_carrier_counts_and_stays_in_sessions(tmp_path):
    """Maintainer shape 2: the same carrier whose live ask is media-only
    (image_url part) counts 1 and stays listed (agent oracle: 1)."""
    rows = [
        ("m0", "user", "first real question", 0),
        ("m1", "user", _sentinel_list_content(_list_summary_carrier(_IMAGE_ASK)), 1),
    ]
    by_id, listed = _list_carrier_session_db(tmp_path, rows)
    assert "s1" in by_id, f"image-ask carrier session missing: {[r['id'] for r in listed]}"
    assert by_id["s1"]["actual_user_message_count"] == 2, (
        "expected 2 (plain ask + the image-only live ask); got %s"
        % by_id["s1"]["actual_user_message_count"]
    )


def test_sentinel_empty_list_user_row_is_not_a_user_turn(tmp_path):
    """Maintainer shape 3: an empty-list row counts 0 (agent oracle: 0). The
    raw sentinel string is never blank, so the old aggregate counted it."""
    rows = [
        ("m0", "user", "first real question", 0),
        ("m1", "user", _sentinel_list_content([]), 0),
    ]
    by_id, _ = _list_carrier_session_db(tmp_path, rows)
    assert by_id["s1"]["actual_user_message_count"] == 1, (
        "expected 1 (the empty list is not a turn); got %s"
        % by_id["s1"]["actual_user_message_count"]
    )


def test_sentinel_blank_text_list_user_row_is_not_a_user_turn(tmp_path):
    """Maintainer shape 4: a list whose only part is blank text counts 0
    (agent oracle: 0)."""
    rows = [
        ("m0", "user", "first real question", 0),
        ("m1", "user", _sentinel_list_content([{"type": "text", "text": "  \t "}]), 0),
    ]
    by_id, _ = _list_carrier_session_db(tmp_path, rows)
    assert by_id["s1"]["actual_user_message_count"] == 1, (
        "expected 1 (the blank-text list is not a turn); got %s"
        % by_id["s1"]["actual_user_message_count"]
    )


def test_sidecar_predicate_decodes_sentinel_list_carriers():
    """The sidecar classifier must decode sentinel rows before classifying:
    both carrier shapes are turns (agent oracle: True)."""
    for label, ask in (("text ask", _TEXT_ASK), ("image-only ask", _IMAGE_ASK)):
        row = {
            "role": "user",
            "content": _sentinel_list_content(_list_summary_carrier(ask)),
            "_compressed_summary": True,
        }
        with _force_mirror_mode() as anchor:
            assert anchor.is_user_originated_turn(row) is True, (
                f"sentinel list carrier with {label} is a live user turn — the "
                "agent counts it, the sidecar must decode first"
            )


def test_sidecar_predicate_decodes_sentinel_empty_and_blank_lists():
    """Decoding must also restore the negatives: an empty list and a blank-text
    list are NOT turns (agent oracle: False)."""
    for label, content in (
        ("empty list", _sentinel_list_content([])),
        ("blank-text list", _sentinel_list_content([{"type": "text", "text": "  \t "}])),
    ):
        row = {"role": "user", "content": content}
        with _force_mirror_mode() as anchor:
            assert anchor.is_user_originated_turn(row) is False, (
                f"sentinel {label} is not a human turn — the agent counts 0"
            )
