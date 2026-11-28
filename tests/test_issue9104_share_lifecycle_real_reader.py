"""#9104 re-gate: share lifecycle of profile-less global transcripts.

Maintainer blockers (2026-11-22): the no-listing-owner fallback treated ANY
transcript returned by ``get_cli_session_messages(sid)`` as proof that the
active profile owns the session. For ``claude_code_`` ids that reader opens
the GLOBAL Claude Code transcript directory (``~/.claude/projects``), not a
profile's state.db. ``GET /api/session`` already exempts those profile-less
rows, but the share path did not, so under a named profile the fallback newly
admitted them: ``POST /api/share/create`` published a public link, the detail
load and ``POST /api/share/revoke`` both answered 404 (no WebUI-owned sidecar),
and the public link could never be taken down.

Pinned through the REAL transcript readers, the REAL claim/synthesize helper
and the REAL share store (no transcript/synthesizer doubles):

- named profile: a ``claude_code_`` share create fails closed with 404 and
  publishes nothing, while the profile-agnostic detail load keeps master's
  read-only 200;
- named profile: a state.db-backed CLI session with no listing row keeps the
  full create -> public -> detail -> revoke -> public-404 lifecycle;
- root: a ``claude_code_`` session keeps master's full lifecycle
  create 200 -> public 200 -> detail 200 -> revoke 200 -> public 404;
- ownership is established by reading the REQUEST profile's state.db
  explicitly (asserted on the reader call), not by the global reader.
"""

from __future__ import annotations

import io
import json
import sqlite3
import urllib.parse
from collections import OrderedDict
from pathlib import Path

import pytest


OLD_SID = "20250101_120000_deadbeef"


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for ``routes.handle_get/post``."""

    def __init__(self, raw: bytes = b""):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(raw)
        self.headers = {"Content-Length": str(len(raw))}
        self.request = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, name):
        for key, value in self.sent_headers:
            if key.lower() == name.lower():
                return value
        return None

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _claude_fixture_rows() -> list[dict]:
    return [
        {"summary": "Claude Code share blocker QA"},
        {
            "timestamp": "2026-04-18T12:00:01Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "global transcript question"}],
            },
        },
        {
            "timestamp": "2026-04-18T12:00:02Z",
            "message": {"role": "assistant", "content": "global transcript answer"},
        },
    ]


def _make_state_db(path: Path, sid: str, *, source: str = "cli", title: str = "CLI session") -> None:
    """Create a minimal real state.db with one session and two messages."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
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
            "VALUES (?, ?, ?, 2, 1781024055.0, ?, ?)",
            (sid, source, "test-model", title, "/tmp"),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, 'user', 'cli question', 1781024055.0)",
            (sid,),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, 'assistant', 'cli answer', 1781024056.0)",
            (sid,),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Real profile homes, state.dbs, Claude projects dir, sidecars and shares."""
    import api.models as models
    import api.routes as routes
    import api.shares as shares

    hermes_root = tmp_path / "hermes"
    alpha_home = hermes_root / "profiles" / "alpha"
    hermes_root.mkdir(parents=True)
    alpha_home.mkdir(parents=True)

    homes = {"default": hermes_root, "alpha": alpha_home}
    current = {"profile": "alpha"}

    def active_state_db():
        return homes[current["profile"]] / "state.db"

    monkeypatch.setattr(models, "_active_state_db_path", active_state_db)
    monkeypatch.setattr(
        models,
        "_get_profile_home",
        lambda name: homes.get(name or "default", hermes_root),
    )
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: current["profile"])
    monkeypatch.setattr(routes, "get_last_workspace", lambda: str(tmp_path / "workspace"))

    sessions = OrderedDict()
    session_dir = tmp_path / "webui" / "sessions"
    session_dir.mkdir(parents=True)
    index_path = session_dir / "_index.json"
    index_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_path)
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_path)

    shares_dir = tmp_path / "webui" / "shares"
    monkeypatch.setattr(shares, "SHARES_DIR", shares_dir)

    projects_dir = tmp_path / "claude" / "projects"
    monkeypatch.setenv("HERMES_WEBUI_CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.delenv("HERMES_WEBUI_TEST_STATE_DIR", raising=False)
    models.clear_cli_sessions_cache()

    jsonl = projects_dir / "project-a" / "session.jsonl"
    _write_jsonl(jsonl, _claude_fixture_rows())
    claude_sid = models._claude_code_session_id(jsonl)
    claude_row = {
        "session_id": claude_sid,
        "title": "Claude Code share blocker QA",
        "workspace": "/tmp/claude-workspace",
        "model": "claude-code",
        "created_at": 1776513601.0,
        "updated_at": 1776513602.0,
        "profile": None,
        "source_tag": "claude_code",
        "raw_source": "claude_code",
        "session_source": "external_agent",
        "source_label": "Claude Code",
        "is_cli_session": True,
        "read_only": True,
        "message_count": 2,
    }
    # The listing row is not the ownership decision under test: the global
    # transcript reader and the claim/share path below are the real ones.
    monkeypatch.setattr(
        routes,
        "_lookup_cli_session_metadata",
        lambda session_id, **_kw: dict(claude_row) if session_id == claude_sid else {},
    )

    yield {
        "tmp": tmp_path,
        "root_home": hermes_root,
        "alpha_home": alpha_home,
        "root_db": hermes_root / "state.db",
        "alpha_db": alpha_home / "state.db",
        "current": current,
        "session_dir": session_dir,
        "shares_dir": shares_dir,
        "projects_dir": projects_dir,
        "jsonl": jsonl,
        "claude_sid": claude_sid,
        "claude_row": claude_row,
    }

    models.clear_cli_sessions_cache()


def _post(path: str, body: dict):
    from api.routes import handle_post

    raw = json.dumps(body).encode("utf-8")
    handler = _FakeHandler(raw)
    handle_post(handler, urllib.parse.urlparse(path))
    return handler.status, handler.json_body()


def _get(path: str):
    from api.routes import handle_get

    handler = _FakeHandler()
    handled = handle_get(handler, urllib.parse.urlparse(path))
    body = handler.json_body() if handler.body else None
    return handler.status, body, handled


def _detail(sid: str):
    return _get(f"/api/session?session_id={sid}&messages=0&resolve_model=0")


# ---------------------------------------------------------------------------
# Named profile: global Claude Code transcripts are not profile-owned
# ---------------------------------------------------------------------------


def test_named_profile_claude_code_share_create_fails_closed(sandbox):
    """The real global reader HAS messages, but they prove no profile ownership."""
    from api.models import get_cli_session_messages

    claude_sid = sandbox["claude_sid"]
    assert len(get_cli_session_messages(claude_sid)) == 2, (
        "the real global Claude Code reader must return the transcript — that is "
        "exactly why an unrestricted fallback admitted it"
    )

    status, body = _post("/api/share/create", {"session_id": claude_sid})

    assert status == 404, (
        "a global Claude Code transcript has no profile-owned state.db row, so a "
        "named profile must not publish it as a share"
    )
    assert list(sandbox["shares_dir"].glob("*.json")) == [], "no public snapshot may be written"
    assert not (sandbox["session_dir"] / f"{claude_sid}.json").exists(), "no WebUI sidecar may be created"


def test_named_profile_claude_code_detail_keeps_master_read_only_view(sandbox):
    """The profile-agnostic detail exemption stays: named profiles can still view."""
    claude_sid = sandbox["claude_sid"]

    status, body, _handled = _detail(claude_sid)

    assert status == 200
    assert body["session"]["session_id"] == claude_sid
    assert body["session"]["read_only"] is True
    assert body["session"]["source_tag"] == "claude_code"


def test_named_profile_claude_code_revoke_stays_404(sandbox):
    """Nothing was published, so revoke answers the ordinary not-found."""
    claude_sid = sandbox["claude_sid"]

    status, _body = _post("/api/share/revoke", {"session_id": claude_sid})

    assert status == 404


def _old_claude_sid_without_listing_row(sandbox):
    """A second global transcript that falls outside the listing window."""
    import api.models as models

    old_jsonl = sandbox["projects_dir"] / "project-old" / "old.jsonl"
    _write_jsonl(old_jsonl, _claude_fixture_rows())
    old_sid = models._claude_code_session_id(old_jsonl)
    assert old_sid != sandbox["claude_sid"]
    assert models.get_cli_session_messages(old_sid), "the real global reader still has it"
    return old_sid


def test_claude_code_without_listing_row_cannot_be_shared_under_named_profile(sandbox):
    """Ownership must not depend on the sidecar/listing window existing."""
    old_sid = _old_claude_sid_without_listing_row(sandbox)

    status, _body = _post("/api/share/create", {"session_id": old_sid})

    assert status == 404
    assert list(sandbox["shares_dir"].glob("*.json")) == []


def test_claude_code_without_listing_row_detail_matches_master_404(sandbox):
    """Without a row the detail load is not exempt — master also 404s it."""
    old_sid = _old_claude_sid_without_listing_row(sandbox)

    status, _body, _handled = _detail(old_sid)

    assert status == 404


# ---------------------------------------------------------------------------
# Named profile: the state.db-backed CLI session this issue actually fixes
# ---------------------------------------------------------------------------


def test_named_profile_state_db_cli_session_full_share_lifecycle(sandbox):
    """create -> public read -> detail -> revoke -> public 404, no doubling."""
    _make_state_db(sandbox["alpha_db"], OLD_SID, source="cli", title="older cli")

    status, body = _post("/api/share/create", {"session_id": OLD_SID})
    assert status == 200, body
    token = body["share"]["token"]
    assert token

    status, public_body, _handled = _get(f"/api/share/{token}")
    assert status == 200
    assert public_body["share"]["messages"], "the public snapshot carries the transcript"

    status, detail_body, _handled = _detail(OLD_SID)
    assert status == 200
    assert detail_body["session"]["session_id"] == OLD_SID

    status, _revoke_body = _post("/api/share/revoke", {"session_id": OLD_SID})
    assert status == 200

    status, _public_body, _handled = _get(f"/api/share/{token}")
    assert status == 404, "a revoked share must stay dead"


def test_named_profile_cli_sidecar_is_owned_by_the_named_profile(sandbox):
    """The successful claim persists the named profile as the sidecar owner."""
    _make_state_db(sandbox["alpha_db"], OLD_SID, source="cli")

    status, _body = _post("/api/share/create", {"session_id": OLD_SID})
    assert status == 200

    sidecar = json.loads((sandbox["session_dir"] / f"{OLD_SID}.json").read_text(encoding="utf-8"))
    assert sidecar["profile"] == "alpha", (
        "ownership must come from the request profile whose state.db was read"
    )


# ---------------------------------------------------------------------------
# Root profile: unchanged master behaviour for global Claude Code transcripts
# ---------------------------------------------------------------------------


def test_root_claude_code_share_lifecycle_is_unchanged(sandbox):
    sandbox["current"]["profile"] = "default"
    claude_sid = sandbox["claude_sid"]

    status, body = _post("/api/share/create", {"session_id": claude_sid})
    assert status == 200, body
    token = body["share"]["token"]

    status, public_body, _handled = _get(f"/api/share/{token}")
    assert status == 200
    assert public_body["share"]["messages"]

    status, detail_body, _handled = _detail(claude_sid)
    assert status == 200
    assert detail_body["session"]["session_id"] == claude_sid

    status, _revoke_body = _post("/api/share/revoke", {"session_id": claude_sid})
    assert status == 200

    status, _public_body, _handled = _get(f"/api/share/{token}")
    assert status == 404


def test_root_cli_session_lifecycle_is_unchanged(sandbox):
    sandbox["current"]["profile"] = "default"
    _make_state_db(sandbox["root_db"], OLD_SID, source="cli", title="root cli")

    status, body = _post("/api/share/create", {"session_id": OLD_SID})
    assert status == 200, body
    token = body["share"]["token"]

    status, public_body, _handled = _get(f"/api/share/{token}")
    assert status == 200
    assert public_body["share"]["messages"]

    status, detail_body, _handled = _detail(OLD_SID)
    assert status == 200

    status, _revoke_body = _post("/api/share/revoke", {"session_id": OLD_SID})
    assert status == 200

    status, _public_body, _handled = _get(f"/api/share/{token}")
    assert status == 404


# ---------------------------------------------------------------------------
# The ownership read must name the request profile's database explicitly
# ---------------------------------------------------------------------------


def test_ownership_fallback_reads_the_request_profile_state_db(monkeypatch, sandbox):
    """The fallback must call the state.db reader with profile='alpha'."""
    import api.models as models
    import api.routes as routes

    _make_state_db(sandbox["alpha_db"], OLD_SID, source="cli")
    seen = []
    real_reader = models.get_state_db_session_messages

    def spy(sid, **kwargs):
        seen.append((sid, dict(kwargs)))
        return real_reader(sid, **kwargs)

    monkeypatch.setattr(routes, "get_state_db_session_messages", spy)

    visible, messages = routes._session_visible_with_cli_owner_fallback(
        OLD_SID, None, object()
    )

    assert visible is True
    assert messages, "the fallback returns the profile-owned transcript for synthesis"
    assert seen, "ownership must be established by reading a profile state.db"
    assert seen[0][1].get("profile") == "alpha", (
        "the request profile's database must be named explicitly, not resolved "
        "implicitly through process/thread state"
    )


def test_ownership_fallback_never_reads_global_transcripts_for_claude_code(monkeypatch, sandbox):
    """A claude_code_ id must fail closed without touching the global reader."""
    import api.routes as routes

    calls = []

    def exploding_global_reader(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("the ownership fallback must not read a global transcript")

    monkeypatch.setattr(routes, "get_cli_session_messages", exploding_global_reader)

    visible, messages = routes._session_visible_with_cli_owner_fallback(
        sandbox["claude_sid"], None, object()
    )

    assert visible is False
    assert messages == []
    assert calls == []
