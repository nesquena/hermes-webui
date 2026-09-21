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
    must skip the same synthetic cards."""
    src = _read_models_py()
    assert (
        "role == 'user' and not is_context_compression_marker(m)" in src
    ), (
        "Session._compute_user_message_count must exclude synthetic compression "
        "markers via api.compression_anchor.is_context_compression_marker() — "
        "see #7681 finding 3"
    )


def test_count_user_turns_helper_excludes_compression_markers():
    """#7681 finding 3: the visibility helper's row->messages fallback must
    apply the same classification."""
    src = _read_agent_sessions_py()
    assert (
        "and not is_context_compression_marker(msg)" in src
    ), (
        "_count_user_turns() must not count synthetic compression cards when "
        "falling back to row['messages'] — see #7681 finding 3"
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


# ── Finding 1: compressed lineage total ─────────────────────────────────────


def test_collapse_exposes_deduplicated_lineage_user_total():
    """#7681 finding 1: a collapsed lineage row must expose the whole-lineage
    user-turn total, not just the chosen tip segment's count."""
    js = _read_sessions_js() + """
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
if (row._lineage_user_message_count !== 9) {
  throw new Error('expected the deduplicated lineage total 9 (7 + 2), got '
    + row._lineage_user_message_count);
}
process.stdout.write('ok');
"""
    assert _run_node(js).strip() == "ok", (
        "a collapsed lineage must expose the deduplicated total across all "
        "retained segments — see #7681 finding 1"
    )


def test_render_prefers_lineage_total_over_tip_count():
    """#7681 finding 1: the meta row must render the lineage total when the
    row carries one, falling back to the row's own count otherwise."""
    src = _read_sessions_js()
    assert "_lineage_user_message_count" in src, (
        "sessions.js must consume the collapsed lineage total when rendering "
        "the user-turn label — see #7681 finding 1"
    )
    render_idx = src.index("session_meta_user_turns")
    lookup = src[render_idx - 400 : render_idx]
    assert "_lineage_user_message_count" in lookup, (
        "the render site must read _lineage_user_message_count to prefer the "
        "lineage total over the tip segment's own count"
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
