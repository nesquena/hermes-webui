"""Regression tests for the GET-vs-POST asymmetry on foreign-origin sessions.

The WebUI sidebar happily shows TUI/Desktop/CLI sessions (synthesized from
state.db) via GET /api/session, but POST /api/chat/start was 404-ing for the
same session_id because get_session() only reads WebUI JSON sidecars. The
typed message was then wiped by the messages.js 404 handler, leaving the user
on an empty "new session" screen with their text gone.

The fix routes both endpoints through a shared helper,
``_claim_or_synthesize_cli_session(sid)``, that materialises a WebUI-owned
Session on first write. This file pins the contract with static checks
(handler no longer just 404s) and functional tests (helper resolves each
reason branch correctly with monkey-patched state.db / SESSION_INDEX_FILE).
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ROUTES_PY = ROOT / "api" / "routes.py"


# ---------------------------------------------------------------------------
# Static checks: the fix is in the source
# ---------------------------------------------------------------------------


def _route_handler_block(src: str, handler: str) -> str:
    """Return the body of ``def _handle_chat_start(...)`` up to the next
    top-level def or class."""
    start = src.index(f"def {handler}(")
    m = re.search(r"\n(?:def |class )", src[start + 1:])
    end = (start + 1 + m.start()) if m else len(src)
    return src[start:end]


def test_helper_is_defined():
    src = ROUTES_PY.read_text(encoding="utf-8")
    assert "def _claim_or_synthesize_cli_session(" in src, (
        "shared foreign-session synthesiser must be defined; this helper "
        "closes the GET/POST asymmetry for CLI/TUI/Desktop sessions"
    )


def test_helper_accepts_pass_through_cli_meta():
    """GET path pre-computes _lookup_cli_session_metadata(sid) once; the
    helper must accept it via the cli_meta kwarg to avoid a redundant
    lookup.  Regression for Greptile review note 2026-06-09."""
    import inspect
    import api.routes as _routes
    sig = inspect.signature(_routes._claim_or_synthesize_cli_session)
    assert "cli_meta" in sig.parameters, (
        "_claim_or_synthesize_cli_session must accept a pass-through "
        "cli_meta kwarg so the GET path can avoid a second "
        "_lookup_cli_session_metadata call"
    )
    assert sig.parameters["cli_meta"].default is None, (
        "cli_meta must default to None so existing callers (POST path, "
        "tests) keep working without a keyword argument"
    )


def test_chat_start_sanitises_500_error():
    """Regression for Greptile review note 2026-06-09: the 500 returned
    when synth.save() fails must NOT leak the sidecar filesystem path to
    the client.  _sanitize_error replaces absolute paths with ``<path>``."""
    body = _route_handler_block(
        ROUTES_PY.read_text(encoding="utf-8"), "_handle_chat_start"
    )
    # Locate the save-failure arm and assert the response uses the
    # sanitiser, not the raw exception.
    m = re.search(
        r"except Exception as _save_err:(.*?)(?=\n\s*s = synth)",
        body, re.DOTALL,
    )
    assert m, "could not find the save-failure arm of _handle_chat_start"
    arm = m.group(1)
    assert "_sanitize_error(_save_err)" in arm, (
        "save-failure 500 must pipe the exception through _sanitize_error "
        "so filesystem paths from OSError don't leak to the client"
    )
    assert "logger.exception(" in arm, (
        "save-failure 500 must also log the full exception server-side "
        "so the operator can debug — sanitisation is only for the response"
    )


def test_classifier_helper_is_defined():
    src = ROUTES_PY.read_text(encoding="utf-8")
    assert "def _session_index_marks_was_webui(" in src, (
        "WebUI-vs-foreign classifier must be extracted so GET and POST can "
        "share the #2782 deleted-WebUI-session 404 contract"
    )


def test_chat_start_no_longer_bare_404_on_keyerror():
    """The exact bug: POST /api/chat/start 404'd on missing sidecar."""
    src = ROUTES_PY.read_text(encoding="utf-8")
    body = _route_handler_block(src, "_handle_chat_start")
    # Locate the KeyError arm specifically (the original 3-line bug).
    m = re.search(
        r"except\s+KeyError:\s*\n(.*?)(?=\n\s*diag\.stage\(\"validate_profile\"\))",
        body,
        re.DOTALL,
    )
    assert m, "could not find the KeyError arm of _handle_chat_start"
    arm = m.group(1)
    # Must NOT be the old one-liner anymore.
    assert 'return bad(handler, "Session not found", 404)' not in arm.split(
        "_claim_or_synthesize_cli_session"
    )[0], (
        "the bare 404-on-KeyError branch is still in place before the new "
        "synthesiser is consulted — a TUI/Desktop session would still 404"
    )
    # Must call the new helper.
    assert "_claim_or_synthesize_cli_session" in arm, (
        "_handle_chat_start must delegate to _claim_or_synthesize_cli_session "
        "on KeyError so a foreign session can be claimed writeable"
    )
    # Must persist the sidecar so subsequent GETs find it.
    assert "synth.save()" in arm, (
        "materialised session must be persisted to disk via save() so the "
        "next request (and the next server restart) sees a WebUI sidecar"
    )


def test_get_session_route_uses_shared_synthesiser():
    """The GET KeyError path must also delegate to the same helper."""
    src = ROUTES_PY.read_text(encoding="utf-8")
    # Find the /api/session GET block (not /api/sessions).
    block = re.search(
        r'if parsed\.path == "/api/session":.*?return j\(handler, \{"session": redact_session_data\(sess\)\}\)',
        src,
        re.DOTALL,
    )
    assert block, "could not locate /api/session GET block"
    text = block.group(0)
    assert "_claim_or_synthesize_cli_session" in text, (
        "GET /api/session must also delegate to the shared synthesiser so "
        "the two endpoints cannot drift on foreign-session semantics"
    )


def test_get_session_preserves_cli_read_only_flag():
    """Regression for nesquena-hermes review 2026-06-09: the GET stub
    must report ``read_only`` from cli_meta (matching master) rather
    than hardcoding False, so a Telegram/external-agent session that
    the foreign store flagged read-only is still gated as read-only on
    the WebUI side until the user explicitly POSTs to claim it."""
    block = re.search(
        r'if parsed\.path == "/api/session":.*?return j\(handler, \{"session": redact_session_data\(sess\)\}\)',
        ROUTES_PY.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert block, "could not locate /api/session GET block"
    text = block.group(0)
    # Master did: bool((cli_meta or {}).get("read_only"))
    assert 'bool((cli_meta or {}).get("read_only"))' in text, (
        "GET /api/session must derive read_only from cli_meta (not hardcode "
        "False) so the foreign store's read-only flag survives to the wire. "
        "The POST path claims the session writeable when it materialises the "
        "sidecar; the GET path stays read-only-faithful until then."
    )
    assert '"read_only": False' not in text, (
        "GET /api/session must not hardcode read_only: False — that was a "
        "side effect of the initial refactor and is a UX shift from master "
        "for sessions where the foreign store explicitly sets read_only"
    )


# ---------------------------------------------------------------------------
# Functional tests: the helper resolves each reason branch correctly
# ---------------------------------------------------------------------------


pytestmark_models = pytest.mark.requires_agent_modules


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
        "INSERT INTO sessions (id, source, model, message_count, started_at, title, cwd) "
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
    """Wire up the helper's two external paths to tmp_path:

      * state.db lives in tmp_path/state.db
      * SESSION_INDEX_FILE lives in tmp_path/webui-state/sessions/_index.json
      * SESSION_DIR lives in tmp_path/webui-state/sessions (for any save())
      * get_last_workspace defaults to tmp_path (no prior session)

    All three (routes, models, agent_sessions) read these globals directly,
    so the fixture must patch every module the helper's call chain touches.
    """
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


def test_helper_rejects_unsafe_sid(routes_module, monkeypatch):
    """is_safe_session_id guard fires first; result reason='invalid_sid'."""
    captured = []

    def fake_safe(_sid):
        captured.append(_sid)
        return False

    monkeypatch.setattr(routes_module, "is_safe_session_id", fake_safe)
    sess, reason = routes_module._claim_or_synthesize_cli_session("../etc/passwd")
    assert captured == ["../etc/passwd"]
    assert sess is None
    assert reason == "invalid_sid"


def test_helper_returns_no_foreign_state_for_unknown_sid(routes_module, tmp_path,
                                                          monkeypatch, isolated_state_db):
    """No state.db row + no index entry → reason='no_foreign_state'."""
    _make_state_db(isolated_state_db["db"], "real-sid-xxx")

    sess, reason = routes_module._claim_or_synthesize_cli_session("ghost-sid-yyy")
    assert sess is None
    assert reason == "no_foreign_state"


def test_helper_returns_was_webui_for_deleted_webui_session(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """A webui-origin entry in the index, sidecar missing → 'was_webui'."""
    _make_state_db(isolated_state_db["db"], "real-sid-xxx")
    _write_index(
        isolated_state_db["index_path"],
        [
            {"session_id": "webui-orphan", "source_tag": "webui",
             "raw_source": "webui", "session_source": "webui"},
        ],
    )

    sess, reason = routes_module._claim_or_synthesize_cli_session("webui-orphan")
    assert sess is None
    assert reason == "was_webui"


def test_helper_keeps_cli_orphan_with_blank_source(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """Legacy CLI/imported rows with blank source fields must NOT be treated
    as deleted WebUI sessions — they keep the existing CLI stub path."""
    _make_state_db(isolated_state_db["db"], "real-sid-xxx")
    _write_index(
        isolated_state_db["index_path"],
        [
            {"session_id": "legacy-cli",  # all source fields blank, is_cli_session True
             "is_cli_session": True, "read_only": True},
        ],
    )

    # 'legacy-cli' has no state.db row, so it falls through to no_foreign_state,
    # but the important assertion is that it does NOT 404 with 'was_webui'.
    sess, reason = routes_module._claim_or_synthesize_cli_session("legacy-cli")
    assert sess is None
    assert reason == "no_foreign_state"


def test_helper_materialises_state_db_only_session(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """The bug-trigger case: state.db row exists, no WebUI sidecar →
    'materialized' with a populated Session that the caller can save()."""
    SID = "20260609_tui_xyz123"
    _make_state_db(isolated_state_db["db"], SID, message_count=3,
                    title="Codex honcho integration",
                    source="tui", cwd="/root")
    # Inject a CLI metadata record so the helper picks up title/workspace
    # from the same lookup the live GET path uses.
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid: {
            "session_id": SID,
            "title": "Codex honcho integration",
            "workspace": "/root",
            "model": "MiniMax-M3",
            "source_tag": "tui",
            "raw_source": "tui",
            "source_label": "Tui",
            "session_source": "other",
        },
    )

    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "materialized"
    assert sess is not None
    # Session has the right shape for the caller to save() and _start_run().
    assert sess.session_id == SID
    assert sess.title == "Codex honcho integration"
    assert sess.model == "MiniMax-M3"
    assert sess.workspace == "/root"  # from CLI metadata
    assert len(sess.messages) == 3
    assert sess.messages[0]["role"] == "user"
    # Source-tag metadata is preserved so the sidebar still shows the badge.
    assert sess.is_cli_session is True
    assert sess.source_tag == "tui"
    assert sess.raw_source == "tui"
    # WebUI is now the owner; read_only cleared so the next turn persists.
    assert sess.read_only is False


def test_helper_uses_get_last_workspace_when_cwd_missing(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """Falls back to get_last_workspace() when neither state.db cwd nor
    CLI metadata carries one — keeps _start_run from tripping on a missing
    workspace."""
    SID = "noworkspace_sid"
    _make_state_db(isolated_state_db["db"], SID, message_count=1, cwd="")
    # No CLI metadata; state.db cwd is empty; fall through to the helper's
    # last-resort workspace lookup.
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                        lambda _sid: {})
    fallback_workspace = tmp_path / "fallback-ws"
    fallback_workspace.mkdir()
    # The helper does ``from api.workspace import get_last_workspace`` inside
    # the function body, so the local name is re-resolved at every call.
    # Patch the source-of-truth attribute on api.workspace.
    import api.workspace as _workspace_mod
    monkeypatch.setattr(_workspace_mod, "get_last_workspace",
                        lambda: str(fallback_workspace))

    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "materialized"
    assert sess is not None
    assert Path(sess.workspace).resolve() == fallback_workspace.resolve()


# ---------------------------------------------------------------------------
# Refusal tests — the #4911 security gate
# ---------------------------------------------------------------------------
# A WebUI POST must NOT be able to claim a session owned by another process
# (messaging channel, Claude Code, external_agent) and turn it into a
# writable WebUI sidecar.  The helper returns the Session anyway (so the
# GET stub still renders the read-only banner), but the reason is
# 'not_claimable' and the Session is built with read_only=True.  The
# POST handler maps that reason to 403.  The four refusal families
# below correspond to the maintainer's required-before-merge list.


def test_helper_refuses_claude_code_session(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """Claude Code sessions are owned by the Claude Code app.  A WebUI
    POST must not be able to materialise a writable sidecar from them.

    Note: ``get_cli_session_messages`` short-circuits to a JSONL-on-disk
    reader for sids that start with the ``claude_code_`` prefix, so the
    test sid deliberately doesn't use that prefix and goes through the
    regular state.db path."""
    SID = "20260610_claude_code_xyz"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=2,
        title="Claude Code chat", source="claude_code", cwd="/root",
    )
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid: {"session_id": SID, "source_tag": "claude_code",
                      "raw_source": "claude_code",
                      "session_source": "external_agent"},
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "not_claimable", (
        "Claude Code sessions must surface as 'not_claimable' — "
        "claim would convert a Claude-Code-owned session into a "
        "writable WebUI sidecar (#4911 review)"
    )
    assert sess is not None, "GET stub must still return a read-only view"
    assert sess.read_only is True, (
        "refused sessions must keep read_only=True so the GET stub's "
        "read-only banner stays accurate"
    )
    assert sess.is_cli_session is True
    assert sess.source_tag == "claude_code"


def test_helper_refuses_messaging_session(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """A Telegram/Discord/etc session is owned by the gateway, not
    WebUI.  A WebUI POST must not be able to claim it."""
    SID = "20260610_telegram_chat_42"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=2,
        title="Telegram chat", source="telegram", cwd="/root",
    )
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid: {"session_id": SID, "source_tag": "telegram",
                      "raw_source": "telegram",
                      "session_source": "messaging"},
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "not_claimable"
    assert sess is not None
    assert sess.read_only is True
    assert sess.session_source == "messaging"


def test_helper_refuses_external_agent_session_source(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """session_source='external_agent' is a hard refusal regardless of
    which raw source tag the foreign store used."""
    SID = "20260610_external_agent_99"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=1,
        title="External agent", source="unknown", cwd="/root",
    )
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid: {"session_id": SID, "source_tag": "agent_x",
                      "raw_source": "agent_x",
                      "session_source": "external_agent"},
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "not_claimable"
    assert sess.read_only is True


def test_helper_refuses_explicit_readonly_flag(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """If the foreign store marks a session read_only=True, the WebUI
    must respect that.  Mirrors /api/session/import_cli policy at
    routes.py:~15626."""
    SID = "20260610_explicit_ro_session"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=1,
        title="Read-only", source="cli", cwd="/root",
    )
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid: {"session_id": SID, "source_tag": "cli",
                      "raw_source": "cli",
                      "session_source": "other", "read_only": True},
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "not_claimable"
    assert sess.read_only is True, (
        "explicit read_only=True from cli_meta must be preserved "
        "even when the source looks claimable"
    )


def test_helper_uses_state_db_source_when_cli_meta_empty(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """TUI/Desktop sessions often have empty cli_meta (they don't
    appear in get_cli_sessions() because of the cap).  The helper
    must fall back to state.db's source column to make the
    claim-eligibility check robust."""
    SID = "20260610_tui_no_cli_meta"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=2,
        title="TUI session", source="tui", cwd="/root",
    )
    # Empty cli_meta (the typical case for TUI)
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata", lambda _sid: {},
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "materialized", (
        "TUI sessions with empty cli_meta must still be claimable — "
        "the helper must read state.db.source as the fallback"
    )
    assert sess.read_only is False
    assert sess.source_tag == "tui"


def test_helper_refuses_claude_code_via_state_db_source(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """Same as test_helper_refuses_claude_code_session but with empty
    cli_meta — the state.db source column is the fallback."""
    SID = "20260610_claude_via_state_db"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=1,
        title="Claude Code", source="claude_code", cwd="/root",
    )
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata", lambda _sid: {},
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "not_claimable"
    assert sess.read_only is True


def test_post_chat_start_returns_403_for_not_claimable(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """Static check: the POST _handle_chat_start KeyError arm must map
    the 'not_claimable' reason to a 403 (not 404).  404 would trigger
    the frontend's empty-state self-heal which is the wrong UX for a
    legitimately-listed read-only session."""
    src = ROUTES_PY.read_text(encoding="utf-8")
    # The new arm sits between the bare-404 collapse and the synth.save()
    # call.  Locate it via the "not_claimable" string and the 403 marker.
    m = re.search(
        r'if reason == "not_claimable":(.*?)(?=\n\s*try:\s*\n\s*synth\.save)',
        src, re.DOTALL,
    )
    assert m, "could not find the 'not_claimable' arm in _handle_chat_start"
    arm = m.group(1)
    assert "403" in arm, (
        "'not_claimable' must return 403, not 404, so the frontend "
        "keeps the user's URL and shows a refusal bubble instead of "
        "triggering the empty-state self-heal"
    )
    assert "read-only" in arm.lower(), (
        "403 response body should mention read-only so the user "
        "understands why the claim was refused"
    )
    # And critically: the 'not_claimable' arm must NOT call synth.save()
    assert "synth.save()" not in arm, (
        "'not_claimable' must skip synth.save() — claiming a "
        "read-only / foreign-owned session into a writable sidecar "
        "is the ownership-boundary violation #4911 review called out"
    )


def test_tui_session_still_claimable_regression(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """Regression for the original bug (#4911): TUI sessions must
    still be claimable after the security gate lands.  This is the
    happy path — if it breaks, the whole PR regresses.  The
    state.db source-column fallback is exercised by
    ``test_helper_uses_state_db_source_when_cli_meta_empty``."""
    SID = "20260610_tui_happy_path"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=3,
        title="TUI chat", source="tui", cwd="/root",
    )
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid: {"session_id": SID, "source_tag": "tui",
                      "raw_source": "tui",
                      "session_source": "other"},
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "materialized"
    assert sess.read_only is False
    assert sess.is_cli_session is True
    assert sess.source_tag == "tui"
