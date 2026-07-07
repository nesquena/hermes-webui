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

import io
import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

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
    # Maintainer follow-up (nesquena-hermes, 2026-06-14): pin that
    # _sanitize_error's output does not contain a `/` segment from the
    # session store root, so a future refactor of _sanitize_error can't
    # silently regress path-stripping.  Use the real SESSION_DIR so the
    # assertion catches both the directory name (`/sessions`) and any
    # parent path the production layout would expose.
    from api.helpers import _sanitize_error  # noqa: E402
    from api.config import SESSION_DIR  # noqa: E402
    sanitized = _sanitize_error(OSError(
        f"[Errno 2] No such file or directory: "
        f"{SESSION_DIR}/sid_xyz.json: bogus"
    ))
    assert f"/{SESSION_DIR.name}/" not in sanitized, (
        f"_sanitize_error leaked the session store root "
        f"({SESSION_DIR!r}): {sanitized!r}"
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
    """Greptile #4911 follow-up: the GET stub must report read_only
    from the synthesized Session, NOT from cli_meta directly.

    Why this changed: the initial refactor + the master-revert
    commit (b298b886) had the GET handler read
    ``bool((cli_meta or {}).get("read_only"))``.  That works for
    sessions where the foreign store explicitly sets read_only=True
    on cli_meta, but it MISSES the source-refused cases — messaging
    / claude_code / external_agent sessions whose refusal comes
    from the source check rather than an explicit flag.  For those,
    cli_meta.get("read_only") returns None, the GET response
    advertises read_only=False, the frontend renders the composer,
    and the user only discovers the block at POST time with a
    confusing 403.

    The fix: read from synth.read_only, which the helper correctly
    sets to True for BOTH the explicit AND the source-refused
    refusal paths."""
    block = re.search(
        r'if parsed\.path == "/api/session":.*?return j\(handler, \{"session": redact_session_data\(sess\)\}\)\)?',
        ROUTES_PY.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert block, "could not locate /api/session GET block"
    text = block.group(0)
    # Must read read_only from the synthesized Session.
    assert "synth.read_only" in text, (
        "GET /api/session must read read_only from the synthesized "
        "Session (synth.read_only), not from cli_meta directly.  The "
        "helper sets synth.read_only=True for BOTH explicit and "
        "source-refused refusals, but cli_meta.get('read_only') only "
        "captures the explicit case (Greptile #4911 follow-up)."
    )
    # And must NOT read it from cli_meta directly.
    assert 'bool((cli_meta or {}).get("read_only"))' not in text, (
        "GET /api/session must not read read_only from cli_meta "
        "directly — that misses source-refused cases (messaging / "
        "claude_code / external_agent) and the frontend renders the "
        "composer for sessions it should be displaying as read-only"
    )
    assert '"read_only": False' not in text, (
        "GET /api/session must not hardcode read_only: False — that "
        "was a side effect of the initial refactor and is a UX shift "
        "from the helper-correct path"
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


class _FakePostHandler:
    def __init__(self, body: dict, *, path: str):
        raw = json.dumps(body).encode("utf-8")
        self.status = None
        self.response_headers = {}
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.command = "POST"
        self.path = path
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


def _response_json(handler: _FakePostHandler) -> dict:
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


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


def test_helper_returns_was_webui_for_durable_deleted_tombstone_without_index(
    routes_module, isolated_state_db
):
    """A full WebUI delete tombstone must keep the 404 self-heal contract even
    after /api/session/delete prunes _index.json before state.db cleanup fails.
    """
    import api.models as _models

    sid = "webui-deleted-db-survives"
    _make_state_db(
        isolated_state_db["db"],
        sid,
        message_count=2,
        title="Deleted WebUI",
        source="webui",
    )
    _models._record_webui_deleted_session_tombstone(sid)

    sess, reason = routes_module._claim_or_synthesize_cli_session(sid)

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
    assert Path(sess.workspace).name == "root"  # from CLI metadata
    assert len(sess.messages) == 3
    assert sess.messages[0]["role"] == "user"
    # Greptile #4911 P1: created_at must be populated from state.db
    # (started_at), not left as epoch (0).  Otherwise the first POST
    # writes "Jan 1 1970" into the permanent sidecar.
    #
    # `created_at` is preserved by Session.save() (it's not in the touch
    # list — see api/models.py:715,735-736, where only `updated_at` is
    # stamped with time.time()), so the mapping is load-bearing for
    # created_at on BOTH the GET read-only stub path AND the POST claim
    # path (where the helper subsequently calls synth.save()).
    #
    # The `updated_at` mapping is load-bearing only for the GET read-only
    # stub path (which never calls save() and so reads back the
    # pre-claim sidebar timestamp directly from the synthesized Session).
    # On the POST claim path, synth.save() defaults to
    # touch_updated_at=True and unconditionally stamps updated_at to
    # wall-clock now, so the post-claim sidecar's `updated_at` will
    # reflect the moment of claim rather than the state.db `ended_at`.
    # That's the desired UX — the session was just claimed/touched —
    # so we don't pass touch_updated_at=False.  This assertion just pins
    # that the helper populated updated_at to *something* non-zero (the
    # mapping ran), not that it survives save().
    assert sess.created_at > 0, (
        f"created_at must be populated from state.db.started_at — got "
        f"{sess.created_at} (epoch), the synthesized Session will be "
        "written into the sidecar with a 1970 timestamp on first save"
    )
    assert sess.updated_at > 0, (
        f"updated_at must be populated from state.db.ended_at or "
        f"started_at — got {sess.updated_at} (epoch); note: on the "
        "POST claim path synth.save() will overwrite this to wall-"
        "clock now, which is the desired UX"
    )
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
    # Greptile #4911 P1: timestamps must come from state.db, not be
    # left as epoch.  This test is the worst-case for the bug — empty
    # cli_meta means nothing else in the path can populate the dates.
    assert sess.created_at > 0, (
        f"empty cli_meta path must still get created_at from "
        f"state.db.started_at — got {sess.created_at} (epoch), the "
        "sidecar would sort this session as 'Jan 1 1970'"
    )
    assert sess.updated_at > 0, (
        f"empty cli_meta path must still get updated_at from "
        f"state.db.ended_at or started_at — got {sess.updated_at} "
        "(epoch)"
    )


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


def test_helper_does_not_mutate_callers_cli_meta(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """No-mutation contract (Greptile #4911 follow-up): the GET path
    passes a pre-computed ``cli_meta`` dict via the ``cli_meta``
    kwarg and expects it to be unchanged after the helper returns.
    The state.db enrichment block must not silently mutate that dict
    in place — future refactors of the GET response builder could
    trip on the implicit mutation.

    The caller_meta here deliberately omits 'title', 'model', and
    'workspace' so the state.db enrichment block would normally
    want to fill them.  After the helper returns, the caller's
    dict must still equal the snapshot taken before the call."""
    SID = "20260610_tui_no_mutation"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=2,
        title="TUI session", source="tui", cwd="/root",
    )
    caller_meta = {
        "session_id": SID,
        "source_tag": "tui",
        "raw_source": "tui",
        "session_source": "other",
        # Intentionally NO title/model/workspace — the state.db
        # enrichment block would normally want to add them.  This
        # is the case the old copy-on-write pattern got wrong.
    }
    snapshot = {k: v for k, v in caller_meta.items()}
    # GET path: caller passes the pre-computed cli_meta dict via
    # the kwarg (no internal _lookup_cli_session_metadata call).
    # The POST path uses the same helper but without the kwarg;
    # test_helper_does_not_mutate_callers_cli_meta_when_empty
    # covers that shape.
    sess, reason = routes_module._claim_or_synthesize_cli_session(
        SID, cli_meta=caller_meta,
    )
    assert reason == "materialized"
    # The session we got back should have the enriched fields.
    assert sess.title == "TUI session", (
        "the synthesized Session should pick up state.db title "
        "into its own session object"
    )
    # But the caller's dict must be byte-for-byte unchanged.
    assert caller_meta == snapshot, (
        f"helper mutated caller's cli_meta in place: "
        f"before={snapshot} after={caller_meta}"
    )


def test_helper_does_not_mutate_callers_cli_meta_when_empty(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """No-mutation contract — TUI/Desktop shape (state.db lookup
    path, POST path's typical invocation).

    This test exists alongside ``test_helper_does_not_mutate_callers_cli_meta``
    to cover the POST-path shape (caller passes nothing, helper
    does its own ``_lookup_cli_session_metadata`` internally).  The
    helper's enrichment block then copies the returned dict and
    overwrites the missing fields from state.db.

    To actually exercise the copy-on-write guard, the caller's
    dict must have SOME content but be MISSING the fields the
    enrichment wants to fill.  An empty dict would not exercise
    the guard — even the pre-fix buggy code (which used
    ``setdefault`` on the caller's dict) would pass an empty-dict
    assertion, because there's nothing to setdefault on.  The
    non-empty shape below would have failed under the buggy code
    because the enrichment would have setdefault'd the missing
    fields, mutating the caller's dict in place."""
    SID = "20260610_tui_no_mutation_empty"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=1,
        title="TUI empty", source="tui", cwd="/root",
    )
    # Non-empty dict, but MISSING the fields state.db enrichment
    # would fill (source_tag, raw_source, title, model, workspace,
    # created_at, updated_at).  With copy-on-write the helper
    # builds a fresh dict and fills it; the caller's dict stays
    # exactly as it was.
    caller_meta = {
        "session_source": "other",
        "profile": "default",
        # Intentionally missing: source_tag, raw_source, title,
        # model, workspace, created_at, updated_at — all of these
        # state.db has and the enrichment will want to fill them.
    }
    snapshot = {k: v for k, v in caller_meta.items()}
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid: caller_meta,
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "materialized"
    # The synthesized Session picks up state.db fields.
    assert sess.source_tag == "tui"
    assert sess.title == "TUI empty"
    # The caller's dict must still be byte-for-byte the same — no
    # new keys added, no values overwritten.
    assert caller_meta == snapshot, (
        f"helper mutated caller's non-empty dict in place: "
        f"before={snapshot} after={caller_meta}"
    )


def test_helper_sets_read_only_for_source_refused_sessions(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """Greptile #4911 follow-up: the synthesized Session must
    carry read_only=True for source-refused sessions (messaging /
    claude_code / external_agent), not just for explicit
    read_only=True cli_meta.  The GET response reads read_only
    from synth.read_only (line ~6043), so the GET wire shape
    inherits this for free.  Pin the helper contract here."""
    SID = "20260610_telegram_refused_session"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=2,
        title="Telegram", source="telegram", cwd="/root",
    )
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid: {"session_id": SID, "source_tag": "telegram",
                      "raw_source": "telegram",
                      "session_source": "messaging"},
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "not_claimable"
    # The contract: source-refused sessions have read_only=True on
    # the synthesized Session, even though cli_meta.read_only is None.
    assert sess.read_only is True, (
        "synth.read_only must be True for source-refused sessions — "
        "the GET response reads it from synth.read_only (line ~6043) "
        "and the frontend renders the read-only banner based on this"
    )


def test_import_cli_reads_read_only_from_persisted_session():
    """Greptile #4911 follow-up: the import_cli refresh path
    (line ~15708) must read read_only from the persisted Session
    (existing.read_only), NOT from cli_meta directly.  The same
    rationale as the GET fix: cli_meta.get("read_only") is only
    populated for explicit cases, so reading it from there gives
    the wrong answer for sessions whose refusal comes from the
    source check.

    This is the same pattern in two different response builders;
    the audit grep should also catch any future sibling paths."""
    block = re.search(
        r'def _handle_session_import_cli.*?(?=\n\ndef |\Z)',
        ROUTES_PY.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert block, "could not locate _handle_session_import_cli"
    text = block.group(0)
    # The refresh path (top branch) is the first 'return j(...)' that
    # mentions existing.compact().  Find the read_only line within
    # that return block and inspect the right-hand side.
    refresh_block = re.search(
        r'existing\.compact\(\)[\s\S]*?\}',
        text, re.DOTALL,
    )
    assert refresh_block, "could not locate existing.compact() spread"
    rb = refresh_block.group(0)
    # Accept either the attribute access (`existing.read_only`) or the
    # safer `getattr(existing, "read_only", False)` form — both
    # correctly derive the value from the persisted Session.
    assert (
        "existing.read_only" in rb
        or 'getattr(existing, "read_only"' in rb
    ), (
        "import_cli refresh path must read read_only from "
        "existing (the persisted Session), not from cli_meta"
    )
    assert 'bool((cli_meta or {}).get("read_only"))' not in rb, (
        "import_cli refresh path must not read read_only from cli_meta "
        "directly — that misses source-refused cases (Greptile #4911 "
        "follow-up)"
    )


# ---------------------------------------------------------------------------
# Residual gap from nesquena-hermes review 2026-06-10 03:58Z:
# platformless gateway fallbacks ("gateway", "unknown") slipped the
# original denylist.  These tests pin the tightened refusal list.
# ---------------------------------------------------------------------------


def test_helper_refuses_bare_gateway_session(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """Bare ``"gateway"`` source (gateway/run.py:3461 — no platform
    set) must be refused.  Platform-tagged gateways (telegram,
    discord, etc.) are caught by the existing messaging check; this
    test covers the residual platformless window."""
    SID = "20260610_bare_gateway_session"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=1,
        title="Bare gateway", source="gateway", cwd="/root",
    )
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid: {"session_id": SID, "source_tag": "gateway",
                      "raw_source": "gateway",
                      "session_source": "other"},
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "not_claimable", (
        "bare 'gateway' sessions must be refused — the conversation "
        "is owned by the gateway process, not WebUI (#4911 residual "
        "review gap)"
    )
    assert sess.read_only is True


def test_helper_refuses_cron_session(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """Cron sessions are owned by the cron runner process — a
    scheduled run should not find its session already claimed by a
    stray WebUI POST.  ``get_cli_sessions()`` surfaces cron sessions
    (see ``CRON_PROJECT_CHIP_LIMIT``) so cli_meta can carry
    ``source_tag='cron'`` even when the session is fully covered by
    the foreign store.  The helper must refuse with the same shape
    as gateway / unknown. (#4911 follow-up Greptile 4/5 review)"""
    SID = "20260610_cron_scheduled_run_001"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=1,
        title="Cron job", source="cron", cwd="/root",
    )
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid: {"session_id": SID, "source_tag": "cron",
                      "raw_source": "cron",
                      "session_source": "other"},
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "not_claimable", (
        "cron sessions must be refused — a WebUI POST should not "
        "materialise a writable sidecar and take write ownership "
        "of a scheduled run (Greptile 4/5 P2)"
    )
    assert sess is not None
    assert sess.read_only is True
    assert sess.source_tag == "cron"


def test_helper_refuses_cron_via_state_db_source(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """Same refusal must fire on the state.db-source branch when
    cli_meta is empty (the typical case for cron runs that haven't
    been re-imported into the foreign store)."""
    SID = "20260610_cron_via_state_db"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=1,
        title="Cron", source="cron", cwd="/root",
    )
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata", lambda _sid: {},
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "not_claimable", (
        "cron sessions must be refused on the state.db-source "
        "branch too — a stray POST should not claim a scheduled "
        "run that's only visible via state.db (Greptile 4/5 P2)"
    )
    assert sess.read_only is True


def test_branch_from_cron_state_db_returns_writable_fork_without_source_sidecar(
    routes_module, monkeypatch, isolated_state_db
):
    SID = "20260610_cron_branch_route"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=2,
        title="Cron job", source="cron", cwd="/root",
    )
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes_module, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes_module, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    handler = _FakePostHandler({"session_id": SID}, path="/api/session/branch")
    routes_module.handle_post(handler, SimpleNamespace(path="/api/session/branch", query=""))
    assert handler.status == 200
    payload = _response_json(handler)
    assert payload["parent_session_id"] == SID
    assert payload["session_id"] != SID
    assert (isolated_state_db["sessions_dir"] / f'{payload["session_id"]}.json').exists()
    assert not (isolated_state_db["sessions_dir"] / f"{SID}.json").exists()


@pytest.mark.parametrize("source", ["gateway", "messaging", "external_agent", "unknown", "claude_code"])
def test_branch_still_refuses_non_cron_not_claimable_sources(
    routes_module, monkeypatch, isolated_state_db, source
):
    SID = f"20260610_{source}_branch_route"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=1,
        title=f"{source} chat", source=source, cwd="/root",
    )
    monkeypatch.setattr(
        routes_module,
        "_lookup_cli_session_metadata",
        lambda _sid: {
            "session_id": SID,
            "source_tag": source,
            "raw_source": source,
            "session_source": "other",
        },
    )
    monkeypatch.setattr(routes_module, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes_module, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    handler = _FakePostHandler({"session_id": SID}, path="/api/session/branch")
    routes_module.handle_post(handler, SimpleNamespace(path="/api/session/branch", query=""))
    assert handler.status == 403
    payload = _response_json(handler)
    assert "read-only" in payload["error"].lower()
    assert not (isolated_state_db["sessions_dir"] / f"{SID}.json").exists()


def test_branch_refuses_cron_prefixed_non_cron_not_claimable_source(
    routes_module, monkeypatch, isolated_state_db
):
    SID = "cron_spoof_messaging"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=1,
        title="Messaging chat", source="messaging", cwd="/root",
    )
    monkeypatch.setattr(
        routes_module,
        "_lookup_cli_session_metadata",
        lambda _sid: {
            "session_id": SID,
            "source_tag": "messaging",
            "raw_source": "messaging",
            "session_source": "other",
        },
    )
    monkeypatch.setattr(routes_module, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes_module, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    handler = _FakePostHandler({"session_id": SID}, path="/api/session/branch")
    routes_module.handle_post(handler, SimpleNamespace(path="/api/session/branch", query=""))
    assert handler.status == 403
    payload = _response_json(handler)
    assert "read-only" in payload["error"].lower()
    assert not (isolated_state_db["sessions_dir"] / f"{SID}.json").exists()


def test_branch_from_claimable_tui_still_creates_fork(
    routes_module, monkeypatch, isolated_state_db
):
    SID = "20260610_tui_branch_route"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=3,
        title="TUI chat", source="tui", cwd="/root",
    )
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes_module, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes_module, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "materialized"
    sess.save()
    handler = _FakePostHandler({"session_id": SID}, path="/api/session/branch")
    routes_module.handle_post(handler, SimpleNamespace(path="/api/session/branch", query=""))
    assert handler.status == 200
    payload = _response_json(handler)
    assert payload["parent_session_id"] == SID
    assert payload["title"] == "TUI chat (fork)"
    assert (isolated_state_db["sessions_dir"] / f"{SID}.json").exists()
    assert (isolated_state_db["sessions_dir"] / f'{payload["session_id"]}.json').exists()


def test_chat_start_still_refuses_cron_state_db_source(
    routes_module, monkeypatch, isolated_state_db
):
    SID = "20260610_cron_chat_start_route"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=1,
        title="Cron job", source="cron", cwd="/root",
    )
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes_module, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes_module, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    handler = _FakePostHandler({"session_id": SID}, path="/api/chat/start")
    routes_module.handle_post(handler, SimpleNamespace(path="/api/chat/start", query=""))
    assert handler.status == 403
    payload = _response_json(handler)
    assert "read-only" in payload["error"].lower()
    assert not (isolated_state_db["sessions_dir"] / f"{SID}.json").exists()


def test_branch_refuses_subagent_view_only_source(
    routes_module, monkeypatch
):
    monkeypatch.setattr(routes_module, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes_module, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes_module, "_session_is_subagent_view_only", lambda _sid: True)
    handler = _FakePostHandler({"session_id": "subagent-1"}, path="/api/session/branch")
    routes_module.handle_post(handler, SimpleNamespace(path="/api/session/branch", query=""))
    assert handler.status == 400
    payload = _response_json(handler)
    assert "view-only" in payload["error"].lower()


def test_helper_reason_distinguishes_cli_meta_vs_state_db_source(
    routes_module,
):
    """The diagnostic reason string returned by
    ``_is_claimable_cli_source`` must use ``cli_meta_source=`` when
    the matched value came from cli_meta and ``state_db_source=``
    when it came from state.db.  The reason is currently discarded
    by the caller, but it's exported in the return tuple and may
    surface in a future log / user-visible diagnostic, so
    mislabelling it would mislead debugging. (Greptile 4/5 P2)

    Both branches are exercised with the same denylisted literal
    (``messaging``) so we can be sure the prefix reflects the
    *source of the value*, not just the value itself.
    """
    # cli_meta has source_tag='messaging' — this used to be
    # mislabelled as state_db_source=messaging in the prior
    # implementation.  session_source='other' prevents the
    # session_source branch from matching first.
    cli_meta_msg = {"source_tag": "messaging", "raw_source": "messaging",
                    "session_source": "other"}
    _, reason_cli = routes_module._is_claimable_cli_source(cli_meta_msg)
    assert reason_cli == "cli_meta_source=messaging", (
        f"expected cli_meta_source=messaging when the denylisted "
        f"value came from cli_meta, got {reason_cli!r}"
    )
    # state.db carries source='messaging' but cli_meta is empty
    # (the typical TUI/Desktop / cron-run case) — the reason must
    # use the state_db_source= prefix.
    _, reason_db = routes_module._is_claimable_cli_source(
        {}, state_db_source="messaging",
    )
    assert reason_db == "state_db_source=messaging", (
        f"expected state_db_source=messaging when the value came "
        f"from state.db, got {reason_db!r}"
    )
    # And the same with cron, which is the new entry on the
    # denylist — both branches must match and use the right prefix.
    cli_meta_cron = {"source_tag": "cron", "raw_source": "cron",
                     "session_source": "other"}
    _, reason_cron_cli = routes_module._is_claimable_cli_source(cli_meta_cron)
    assert reason_cron_cli == "cli_meta_source=cron", (
        f"expected cli_meta_source=cron for cli_meta-sourced cron "
        f"denial, got {reason_cron_cli!r}"
    )
    _, reason_cron_db = routes_module._is_claimable_cli_source(
        {}, state_db_source="cron",
    )
    assert reason_cron_db == "state_db_source=cron", (
        f"expected state_db_source=cron for state.db-sourced cron "
        f"denial, got {reason_cron_db!r}"
    )


def test_helper_refuses_bare_unknown_session(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """Bare ``"unknown"`` source (gateway/slash_commands.py:2454) must
    be refused with the same reasoning as bare-gateway."""
    SID = "20260610_bare_unknown_session"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=1,
        title="Bare unknown", source="unknown", cwd="/root",
    )
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid: {"session_id": SID, "source_tag": "unknown",
                      "raw_source": "unknown",
                      "session_source": "other"},
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "not_claimable"
    assert sess.read_only is True


def test_helper_refuses_gateway_via_state_db_source(
    routes_module, tmp_path, monkeypatch, isolated_state_db
):
    """Same refusal must fire when cli_meta is empty (the typical
    TUI/Desktop case where the foreign store doesn't have the row)
    and state.db has ``source='gateway'``."""
    SID = "20260610_gateway_via_state_db"
    _make_state_db(
        isolated_state_db["db"], SID, message_count=1,
        title="Gateway", source="gateway", cwd="/root",
    )
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata", lambda _sid: {},
    )
    sess, reason = routes_module._claim_or_synthesize_cli_session(SID)
    assert reason == "not_claimable"
    assert sess.read_only is True


def test_helper_denylist_includes_gateway_and_unknown():
    """Direct check on the denylist set — guards against a future
    refactor that splits the denylist and forgets the gateway/
    unknown/cron literals."""
    import re
    # Use the ROUTES_PY constant (defined at module top) instead of
    # hardcoding the path so the test runs on any machine with the
    # project checked out, not just at /opt/hermes-webui/.
    src = ROUTES_PY.read_text(encoding="utf-8")
    # Find the function body
    m = re.search(
        r"def _is_claimable_cli_source.*?(?=\n\ndef |\Z)",
        src, re.DOTALL,
    )
    assert m, "could not locate _is_claimable_cli_source"
    body = m.group(0)
    # Both the cli_meta branch and the state.db-source branch must
    # list gateway, unknown, and cron.
    for literal in ("gateway", "unknown", "cron"):
        assert f'"{literal}"' in body, (
            f"_is_claimable_cli_source must denylist '{literal}' in "
            f"both the cli_meta and state_db branches — cron was "
            f"added in the Greptile 4/5 P2 review to keep scheduled "
            f"runs CLI-owned; gateway/unknown were added in the "
            f"residual #4911 review gap"
        )
