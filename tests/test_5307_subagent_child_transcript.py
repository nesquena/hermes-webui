"""Regression tests for issue #5307 — delegated subagent child transcript load.

A delegated ``delegate_task`` child is recorded in Hermes ``state.db`` with
``source='subagent'`` and usually has **no WebUI JSON sidecar** (it ran
server-side; its transcript lives only in state.db). But its ``session_id`` is
registered in the WebUI ``_index.json`` sharing the parent's lineage, often as
a ``webui``/``fork``/blank-source row.

Before the fix, ``GET /api/session`` -> ``get_session()`` raised ``KeyError``
-> ``_claim_or_synthesize_cli_session()`` saw ``_session_index_marks_was_webui``
True and returned ``"was_webui"`` -> **404**, so the child pane opened empty even
though state.db held messages.

The fix excludes delegated subagent children from the ``was_webui`` 404 gate
(``_is_subagent_child_session_id``) so they recover their state.db transcript,
while genuinely-deleted WebUI sessions keep the #2782 self-heal 404 contract.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTES_PY = ROOT / "api" / "routes.py"
SESSIONS_JS = ROOT / "static" / "sessions.js"


# ---------------------------------------------------------------------------
# Local seed helpers (mirror test_chat_start_claim_cli_session, kept
# self-contained so this file has no cross-test import dependency)
# ---------------------------------------------------------------------------


def _make_state_db(path: Path, sid: str, *, message_count: int = 2,
                   title: str = "tui session", model: str = "MiniMax-M3",
                   source: str = "tui", cwd: str = "/root") -> None:
    """Create a minimal state.db with one session and a few messages.

    Schema mirrors hermes_state.SessionDB closely enough for
    get_state_db_session_messages to return rows.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (version INTEGER);
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            billing_provider TEXT,
            billing_base_url TEXT,
            billing_mode TEXT,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            pricing_version TEXT,
            title TEXT,
            api_call_count INTEGER DEFAULT 0,
            handoff_state TEXT,
            handoff_platform TEXT,
            handoff_error TEXT,
            cwd TEXT,
            rewind_count INTEGER DEFAULT 0,
            archived INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_call_count INTEGER DEFAULT 0
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO sessions (id, source, model, message_count, started_at, title, cwd) "
        "VALUES (?, ?, ?, ?, 1781024055.0, ?, ?)",
        (sid, source, model, message_count, title, cwd),
    )
    for i in range(message_count):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, "user" if i % 2 == 0 else "assistant",
             f"msg {i}", 1781024055.0 + i),
        )
    conn.commit()
    conn.close()


def _write_index(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


@pytest.fixture
def routes_module():
    return pytest.importorskip("api.routes")


@pytest.fixture
def isolated_state_db(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    state_dir = tmp_path / "webui-state"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    index_path = sessions_dir / "_index.json"
    index_path.write_text("[]", encoding="utf-8")
    import api.routes as _routes
    import api.models as _models
    monkeypatch.setattr(_models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(_routes, "SESSION_INDEX_FILE", index_path)
    monkeypatch.setattr(_models, "SESSION_INDEX_FILE", index_path)
    monkeypatch.setattr(_models, "SESSION_DIR", sessions_dir)
    return {"db": db, "state_dir": state_dir, "sessions_dir": sessions_dir,
            "index_path": index_path}


# ---------------------------------------------------------------------------
# Static checks: the fix is present in source
# ---------------------------------------------------------------------------


def test_subagent_child_helpers_defined_in_routes():
    src = ROUTES_PY.read_text(encoding="utf-8")
    assert "def _is_subagent_child_session_id(" in src, (
        "routes.py must define _is_subagent_child_session_id to distinguish "
        "delegated subagent children from deleted WebUI sessions (#5307)"
    )
    assert "def _state_db_session_source(" in src, (
        "routes.py must define _state_db_session_source (cheap state.db source lookup)"
    )


def test_was_webui_gate_excludes_subagent_children():
    """The was_webui 404 gate must be guarded by
    ``not _is_subagent_child_session_id(sid)`` so subagent children fall
    through to state.db transcript recovery instead of 404ing."""
    src = ROUTES_PY.read_text(encoding="utf-8")
    start = src.index("def _claim_or_synthesize_cli_session(")
    m = re.search(r"\n(?:def |class )", src[start + 1:])
    block = src[start:(start + 1 + m.start()) if m else len(src)]
    assert "_session_index_marks_was_webui(sid)" in block
    assert "_session_deleted_tombstone_marks_was_webui(sid)" in block
    guard_start = block.index("if (")
    guard = block[guard_start:block.index("return None, \"was_webui\"", guard_start)]
    assert "_is_subagent_child_session_id(sid)" in guard, (
        "all was_webui predicates must share the subagent-child exclusion (#5307)"
    )


def test_sessions_js_open_handlers_keep_isexternalsession_contract():
    """The fix is server-side + view-only, so sessions.js must be UNCHANGED —
    the #3603 contract that the open/tap/child handlers gate on
    _isExternalSession is preserved (subagent children are recovered read-only
    by the server, not by widening the client import trigger)."""
    js = SESSIONS_JS.read_text(encoding="utf-8")
    # We did NOT add a widened import predicate — recovery is server-side.
    assert "_sessionNeedsServerImportForLoad" not in js, (
        "the #5307 fix is server-side (read-only recovery); the client import "
        "predicate must NOT be widened (preserves #3603's _isExternalSession contract)"
    )


# ---------------------------------------------------------------------------
# Functional: helper recovers subagent children, preserves #2782 404
# ---------------------------------------------------------------------------


def test_subagent_child_indexed_as_webui_recovers_readonly_not_404(
    routes_module, isolated_state_db
):
    """A subagent child (source='subagent' in state.db) registered in the WebUI
    index as a webui-lineage row must NOT return 'was_webui' (404). It must
    recover its state.db transcript VIEW-ONLY: reason='not_claimable', a Session
    is returned, read_only=True, and it is NOT CLI-classified/writable (#5307 +
    Codex hardening — a delegated child must not become a writable imported
    session)."""
    _make_state_db(
        isolated_state_db["db"], "subagent-child-1",
        source="subagent", title="delegate child", message_count=2,
    )
    _write_index(
        isolated_state_db["index_path"],
        [
            {"session_id": "subagent-child-1", "source_tag": "webui",
             "raw_source": "webui", "session_source": "webui",
             "parent_session_id": "parent-abc"},
        ],
    )

    sess, reason = routes_module._claim_or_synthesize_cli_session("subagent-child-1")
    assert reason == "not_claimable", (
        f"subagent child must recover view-only (not_claimable), not 404 or "
        f"become writable; got reason={reason!r}"
    )
    assert sess is not None, "the read-only transcript session must be returned"
    # View-only: must be read_only and must NOT be a writable CLI-classified session.
    assert getattr(sess, "read_only", False) is True, (
        "recovered subagent child must be read_only (not writable)"
    )
    assert getattr(sess, "is_cli_session", False) is not True, (
        "recovered subagent child must NOT be CLI-classified (would widen "
        "poll-skip/active-refresh gating)"
    )
    # And it carries the state.db transcript.
    msgs = getattr(sess, "messages", None)
    assert msgs and len(msgs) >= 2, "the state.db transcript must be recovered"


def test_deleted_webui_session_still_returns_was_webui(
    routes_module, isolated_state_db
):
    """#2782 self-heal contract preserved: a genuinely-deleted WebUI session
    (webui-origin index row, NO state.db row) still returns 'was_webui'."""
    _make_state_db(isolated_state_db["db"], "some-other-sid", source="tui")
    _write_index(
        isolated_state_db["index_path"],
        [
            {"session_id": "webui-orphan", "source_tag": "webui",
             "raw_source": "webui", "session_source": "webui"},
        ],
    )

    sess, reason = routes_module._claim_or_synthesize_cli_session("webui-orphan")
    assert sess is None
    assert reason == "was_webui", (
        "a deleted WebUI session with no state.db row must keep the #2782 404"
    )


def test_state_db_source_helper_reads_subagent(routes_module, isolated_state_db):
    """_state_db_session_source returns the lowercased source; _is_subagent_child
    is True only for source='subagent', and False for a missing row."""
    _make_state_db(
        isolated_state_db["db"], "sa-1", source="subagent", message_count=1,
    )
    assert routes_module._state_db_session_source("sa-1") == "subagent"
    assert routes_module._is_subagent_child_session_id("sa-1") is True
    assert routes_module._is_subagent_child_session_id("does-not-exist") is False


def test_import_cli_endpoint_does_not_materialize_subagent_child():
    """Codex hardening: POST /api/session/import_cli must NOT create a writable
    sidecar for a subagent child — the handler gates source='subagent' into the
    read-only view payload (is_cli_session=False, imported=False) before ever
    calling import_cli_session(). Pinned as a static-source contract check."""
    src = ROUTES_PY.read_text(encoding="utf-8")
    start = src.index("def _handle_session_import_cli(")
    m = re.search(r"\n(?:def |class )", src[start + 1:])
    block = src[start:(start + 1 + m.start()) if m else len(src)]
    assert "_is_subagent_child_session_id(sid)" in block, (
        "import_cli handler must detect subagent children"
    )
    assert "_read_only_view" in block, (
        "import_cli must route subagent children through the read-only view "
        "payload (no writable import_cli_session materialization)"
    )
    # the read-only view branch must return before import_cli_session()
    ro_idx = block.index("_read_only_view")
    import_idx = block.index("import_cli_session(")
    assert ro_idx < import_idx, (
        "the read-only-view gate must precede import_cli_session() so subagent "
        "children never materialize a writable sidecar"
    )


def test_materialize_helper_refuses_subagent_child(routes_module, isolated_state_db):
    """The shared _get_or_materialize_session chokepoint (reached by POST
    /api/chat/start) must raise PermissionError for a subagent child, so no
    entry point can turn a delegated child into a writable sidecar (#5307
    Codex round 3 — the chat-start write path)."""
    _make_state_db(
        isolated_state_db["db"], "sa-mat-1", source="subagent", message_count=2,
    )
    with pytest.raises(PermissionError):
        routes_module._get_or_materialize_session("sa-mat-1")


def test_materialize_helper_still_allows_tui(routes_module, isolated_state_db):
    """Guard scope check: a genuine TUI/CLI session is still materializable
    (the subagent gate must not regress normal CLI/TUI/Desktop claiming)."""
    _make_state_db(
        isolated_state_db["db"], "tui-mat-1", source="tui", message_count=2,
    )
    # Should NOT raise PermissionError for a claimable tui source.
    try:
        routes_module._get_or_materialize_session("tui-mat-1")
    except PermissionError:
        pytest.fail("a genuine TUI session must remain materializable")
    except Exception:
        # Other errors (e.g. workspace/save plumbing under the minimal fixture)
        # are out of scope; the contract here is: NOT a PermissionError.
        pass


def test_materialize_helper_refuses_persisted_writable_subagent_sidecar(
    routes_module, isolated_state_db, monkeypatch
):
    """A subagent sidecar previously persisted with read_only=False (e.g.
    materialized before this fix) must still be refused by
    _get_or_materialize_session on the happy path — chat-start cannot use it as
    a writable session (#5307 Codex round 6)."""

    class _FakeSession:
        session_id = "persisted-sa"
        read_only = False
        source_tag = "subagent"
        raw_source = "subagent"
        is_cli_session = False
        messages = [{"role": "user", "content": "hi"}]

    monkeypatch.setattr(routes_module, "get_session", lambda _sid: _FakeSession())
    monkeypatch.setattr(
        routes_module, "_ensure_full_session_before_mutation",
        lambda _sid, s: s, raising=False,
    )
    with pytest.raises(PermissionError):
        routes_module._get_or_materialize_session("persisted-sa")


def test_subagent_view_only_guard_helper(routes_module, isolated_state_db):
    """_session_is_subagent_view_only is True for a state.db subagent source
    (used by the delete/truncate/clear/pin mutation-route guards, #5307)."""
    _make_state_db(
        isolated_state_db["db"], "sa-guard-1", source="subagent", message_count=1,
    )
    assert routes_module._session_is_subagent_view_only("sa-guard-1") is True
    _make_state_db(
        isolated_state_db["db"], "tui-guard-1", source="tui", message_count=1,
    )
    # a non-subagent id with no matching sidecar is not flagged
    assert routes_module._session_is_subagent_view_only("nope-nope") is False


def test_mutation_routes_guard_subagent_source_in_source():
    """Static contract: the direct session-mutation routes (delete / clear /
    truncate / pin) refuse subagent children via _session_is_subagent_view_only
    before mutating, and the /api/sessions list coerces subagent rows to
    read_only=True / is_cli_session=False (#5307 Codex round 8 — prevents
    delete/swipe from erasing the child's state.db transcript)."""
    src = ROUTES_PY.read_text(encoding="utf-8")
    # each mutation route body must call the guard
    for route in ("/api/session/delete", "/api/session/clear",
                  "/api/session/truncate", "/api/session/pin"):
        idx = src.index(f'parsed.path == "{route}"')
        nxt = src.index('parsed.path == "/api/session', idx + 10)
        block = src[idx:nxt]
        assert "_session_is_subagent_view_only(" in block, (
            f"{route} must guard against subagent children before mutating"
        )
    # list coercion present
    assert "_coerce_subagent_rows" in src, (
        "the /api/sessions list must coerce subagent rows to read_only/non-CLI"
    )
