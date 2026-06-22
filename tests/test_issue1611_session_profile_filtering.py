"""Tests for issue #1611: /api/sessions must be scoped to the active profile.

Reporter (@stefanpieter) saw multi-profile installs where querying
/api/sessions with `Cookie: hermes_profile=haku` still returned sessions
tagged to other profiles. Two bugs combined to produce this:
  1. Server-side `/api/sessions` had no profile filter — it merged
     WebUI sidecar sessions and CLI/imported sessions and returned the lot.
  2. Frontend `static/sessions.js` filter let every CLI session bypass the
     active-profile filter via `s.is_cli_session || s.profile === active`.

This test file pins the server-side filter shape via api.routes._profiles_match
(the helper used by the /api/sessions and /api/projects handlers) and the
all_profiles=1 opt-in path. End-to-end HTTP-level tests live separately under
tests/test_sessions_endpoint.py if/when added.
"""

import json
import os
import sqlite3
import time
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import patch
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import pytest

from tests._pytest_port import BASE


# ── _profiles_match helper ─────────────────────────────────────────────────


def test_profiles_match_exact():
    """Same name on both sides matches."""
    from api.routes import _profiles_match
    assert _profiles_match('haku', 'haku') is True
    assert _profiles_match('default', 'default') is True


def test_profiles_match_distinct_named_profiles():
    """Different named profiles do not cross-match."""
    from api.routes import _profiles_match
    assert _profiles_match('haku', 'kinni') is False
    assert _profiles_match('noblepro', 'haku') is False


def test_profiles_match_default_alias_treated_as_root(monkeypatch):
    """A row tagged 'default' matches when the active profile is the renamed
    root (e.g. 'kinni') and vice versa — both resolve to the same ~/.hermes
    home, so they're the same profile from a user perspective."""
    import api.profiles as p
    from api.routes import _profiles_match

    monkeypatch.setattr(p, 'list_profiles_api', lambda: [
        {'name': 'kinni', 'is_default': True, 'path': str(p._DEFAULT_HERMES_HOME)},
    ])
    p._invalidate_root_profile_cache()

    assert _profiles_match('default', 'kinni') is True
    assert _profiles_match('kinni', 'default') is True
    # And neither matches a true named profile
    assert _profiles_match('default', 'haku') is False
    assert _profiles_match('kinni', 'haku') is False


def test_profiles_match_empty_row_treated_as_root():
    """A row with no profile tag (None or empty string) is treated as root.

    Backward compat with legacy sessions/projects that pre-date the profile
    field. The all_sessions() backfill at api/models.py also sets profile
    to 'default' for such rows.
    """
    from api.routes import _profiles_match
    assert _profiles_match(None, 'default') is True
    assert _profiles_match('', 'default') is True
    assert _profiles_match(None, 'haku') is False


def test_profiles_match_active_none_treated_as_default():
    """If active profile resolves to None/empty (boot edge case), treat as 'default'."""
    from api.routes import _profiles_match
    assert _profiles_match('default', None) is True
    assert _profiles_match('default', '') is True


# ── _all_profiles_query_flag ───────────────────────────────────────────────


def test_all_profiles_query_flag_true_values():
    """1, true, yes, on (case-insensitive) all enable aggregate mode."""
    from api.routes import _all_profiles_query_flag
    for v in ('1', 'true', 'TRUE', 'yes', 'YES', 'on'):
        u = urlparse(f'/api/sessions?all_profiles={v}')
        assert _all_profiles_query_flag(u) is True, f"value {v!r} should be true"


def test_all_profiles_query_flag_false_values():
    """0, empty, garbage, missing — all default to scoped mode (False)."""
    from api.routes import _all_profiles_query_flag
    for path in ('/api/sessions', '/api/sessions?all_profiles=0',
                 '/api/sessions?all_profiles=', '/api/sessions?all_profiles=lol'):
        u = urlparse(path)
        assert _all_profiles_query_flag(u) is False, f"path {path!r} should be false"


def test_all_profiles_enabled_in_normal_mode(monkeypatch):
    """The aggregate toggle still works outside isolated-profile mode."""
    import api.routes as routes

    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: False)
    assert routes._all_profiles_enabled(urlparse('/api/sessions?all_profiles=1')) is True


def test_all_profiles_disabled_in_isolated_mode(monkeypatch):
    """An isolated deployment must ignore all_profiles=1 aggregate requests."""
    import api.routes as routes

    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: True)
    assert routes._all_profiles_enabled(urlparse('/api/sessions?all_profiles=1')) is False


# ── No client-side CLI bypass ──────────────────────────────────────────────


def test_static_sessions_js_no_cli_session_bypass():
    """static/sessions.js must NOT filter via `s.is_cli_session || s.profile ===`.

    The original bypass let every CLI-imported session leak into the active-profile
    sidebar regardless of which profile owned it. After #1611 + the Opus pre-release
    SHOULD-FIX, the client trusts the server's scoped wire data and does not
    re-filter by profile at all (a strict-equality client filter would reject
    the server's renamed-root cross-aliased rows).
    """
    from pathlib import Path

    repo_root = Path(__file__).parent.parent
    src = (repo_root / 'static' / 'sessions.js').read_text(encoding='utf-8')

    assert "s.is_cli_session||s.profile===S.activeProfile" not in src, (
        "Old CLI-session bypass must be removed (#1611)"
    )
    assert "s.is_cli_session || s.profile === S.activeProfile" not in src, (
        "Old CLI-session bypass must be removed (#1611)"
    )


def test_static_sessions_js_uses_all_profiles_query_when_toggle_on():
    """Frontend must request /api/sessions?all_profiles=1 when _showAllProfiles is true.

    Without this, flipping the toggle just re-renders client-cached rows that
    may not contain cross-profile data (since the server scoped on first fetch).
    """
    from pathlib import Path

    repo_root = Path(__file__).parent.parent
    src = (repo_root / 'static' / 'sessions.js').read_text(encoding='utf-8')

    assert "if(_showAllProfiles) qs.set('all_profiles','1');" in src, (
        "Expected session-list fetch query to flip on the all-profiles toggle state"
    )
    assert "const projectQS = _showAllProfiles ? '?all_profiles=1' : '';" in src, (
        "Expected project fetch path to flip on the all-profiles toggle state"
    )
    assert "api('/api/sessions' + sessionListQS" in src, (
        "Expected /api/sessions fetch to use the variant query"
    )
    assert "api('/api/projects' + projectQS" in src, (
        "Expected /api/projects fetch to use the variant query"
    )


def test_static_sessions_js_marks_all_profiles_imports_with_profile():
    """All-profiles row opens must opt into cross-profile import explicitly."""
    from pathlib import Path

    repo_root = Path(__file__).parent.parent
    src = (repo_root / 'static' / 'sessions.js').read_text(encoding='utf-8')

    assert "function _externalImportPayload(session)" in src
    assert "payload.all_profiles = true;" in src
    assert "payload.profile = session.profile;" in src
    assert "JSON.stringify(_externalImportPayload(s))" in src


# ── SHOULD-FIX #2: profile filter must run BEFORE messaging-source dedupe ──
# Bug shape (Opus pre-release advisor): _messaging_source_key is profile-blind,
# so if profiles A and B both have a session for the same Slack identity, a
# profile-blind dedupe runs first and discards the older profile's row, then
# the profile filter scopes — leaving the losing profile with zero rows for
# that source.


def test_keep_latest_messaging_runs_after_profile_filter():
    """Source-string check: api/routes.py /api/sessions handler must call
    _keep_latest_messaging_session_per_source AFTER the profile filter."""
    from pathlib import Path

    repo_root = Path(__file__).parent.parent
    src = (repo_root / 'api' / 'routes.py').read_text(encoding='utf-8')

    handler_idx = src.find('parsed.path == "/api/sessions":')
    assert handler_idx > 0
    next_handler = src.find('parsed.path == "/api/projects":', handler_idx)
    block = src[handler_idx:next_handler]

    filter_idx = block.find('_profiles_match(s.get("profile"), active_profile)')
    # The dedupe call can be either single-line `(scoped)` or multi-line
    # `(\n    scoped,\n    show_previous_messaging_sessions=…,\n)`; match the
    # function name + the first arg position rather than coupling to the call
    # shape. (#2294 added the keyword-arg form.)
    dedupe_idx = block.find('_keep_latest_messaging_session_per_source(')
    assert filter_idx > 0, "Profile filter not found in /api/sessions handler"
    assert dedupe_idx > 0, "Messaging dedupe must run on the scoped list"
    assert filter_idx < dedupe_idx, (
        "Profile filter must run BEFORE messaging-source dedupe — running it "
        "after lets the dedupe discard the active profile's row when both "
        "profiles share a messaging identity (Opus pre-release SHOULD-FIX #2)"
    )


# ── SHOULD-FIX #1: client filter must NOT strict-equality-reject server cross-aliased rows ──


def test_static_sessions_js_trusts_server_profile_scoping():
    """After SHOULD-FIX #1, the client should NOT re-filter via strict equality.

    Bug shape: server returns rows tagged 'default' to an active 'kinni' user
    (when kinni is the renamed root) via _profiles_match cross-alias. A
    naïve `(s.profile||'default')===(S.activeProfile||'default')` client filter
    rejects them — user loses every legacy 'default'-tagged session.

    Fix: drop the redundant client filter; trust the server."""
    from pathlib import Path

    repo_root = Path(__file__).parent.parent
    src = (repo_root / 'static' / 'sessions.js').read_text(encoding='utf-8')

    # The fragile client-side strict-equality filter must be gone.
    forbidden = "withMessages.filter(s=>(s.profile||'default')===(S.activeProfile||'default'))"
    assert forbidden not in src, (
        "Client must not re-filter rows the server already cross-aliased "
        "(Opus pre-release SHOULD-FIX #1)"
    )

    # And the count fallback that ran the same broken comparison must be gone too.
    forbidden_count = "withMessages.filter(s=>(s.profile||'default')!==(S.activeProfile||'default')).length"
    assert forbidden_count not in src, (
        "Client otherProfileCount must come from server, not strict-equality fallback"
    )


# ── Direct session access must also honor active profile ───────────────────


class _ProfileScopedSession:
    def __init__(self, session_id="foreign_001", profile="other"):
        self.session_id = session_id
        self.profile = profile
        self.active_stream_id = None
        self.messages = [{"role": "user", "content": "foreign profile secret"}]
        self.tool_calls = []
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.context_length = 0
        self.threshold_tokens = 0
        self.last_prompt_tokens = 0

    def compact(self, *args, **kwargs):
        return {
            "session_id": self.session_id,
            "title": "Foreign session",
            "profile": self.profile,
            "workspace": "/tmp/foreign",
            "model": "gpt-test",
            "message_count": len(self.messages),
        }


def test_get_session_rejects_session_from_inactive_profile():
    """A known session_id from another profile must not bypass /api/sessions scoping.

    /api/sessions already filters rows by active profile.  The detail endpoint
    must apply the same check after loading the sidecar; otherwise a stale URL or
    guessed id can disclose another profile's transcript.
    """
    import api.routes as routes

    captured = {}

    def fake_bad(_handler, message, status=400, **_kwargs):
        captured["bad"] = {"message": message, "status": status}
        return captured["bad"]

    def fake_j(_handler, data, status=200, **_kwargs):
        captured["json"] = {"data": data, "status": status}
        return captured["json"]

    parsed = urlparse("/api/session?session_id=foreign_001&messages=1&resolve_model=0")
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes.get_session", return_value=_ProfileScopedSession()), \
         patch("api.routes._clear_stale_stream_state", return_value=False), \
         patch("api.routes._lookup_cli_session_metadata", return_value={}), \
         patch("api.routes.get_state_db_session_messages", return_value=[]), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_get(SimpleNamespace(headers={"Cookie": "hermes_profile=default"}), parsed)

    assert captured.get("bad", {}).get("status") == 404
    assert "json" not in captured, "foreign-profile transcript must not be returned"


def test_get_session_rejects_metadata_only_session_from_inactive_profile():
    """Metadata-only loads must not bypass the active-profile boundary."""
    import api.routes as routes

    captured = {}

    def fake_bad(_handler, message, status=400, **_kwargs):
        captured["bad"] = {"message": message, "status": status}
        return captured["bad"]

    def fake_j(_handler, data, status=200, **_kwargs):
        captured["json"] = {"data": data, "status": status}
        return captured["json"]

    parsed = urlparse("/api/session?session_id=foreign_001&messages=0&resolve_model=0")
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes.get_session", return_value=_ProfileScopedSession()), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_get(SimpleNamespace(headers={"Cookie": "hermes_profile=default"}), parsed)

    assert captured.get("bad", {}).get("status") == 404
    assert "json" not in captured, "foreign-profile metadata must not be returned"


def test_get_session_rejects_cookieless_session_from_inactive_profile():
    """Cookieless requests must still enforce the active-profile boundary."""
    import api.routes as routes

    captured = {}

    def fake_bad(_handler, message, status=400, **_kwargs):
        captured["bad"] = {"message": message, "status": status}
        return captured["bad"]

    def fake_j(_handler, data, status=200, **_kwargs):
        captured["json"] = {"data": data, "status": status}
        return captured["json"]

    parsed = urlparse("/api/session?session_id=foreign_001&messages=0&resolve_model=0")
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes.get_session", return_value=_ProfileScopedSession()), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_get(SimpleNamespace(headers={}), parsed)

    assert captured.get("bad", {}).get("status") == 404
    assert "json" not in captured, "cookieless foreign-profile metadata must not be returned"


def test_get_session_rejects_cli_session_from_inactive_profile():
    """CLI fallback responses must use the same active-profile boundary."""
    import api.routes as routes

    captured = {}

    def fake_bad(_handler, message, status=400, **_kwargs):
        captured["bad"] = {"message": message, "status": status}
        return captured["bad"]

    def fake_j(_handler, data, status=200, **_kwargs):
        captured["json"] = {"data": data, "status": status}
        return captured["json"]

    parsed = urlparse("/api/session?session_id=cli_foreign&messages=1&resolve_model=0")
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes.get_session", side_effect=KeyError), \
         patch("api.routes.SESSION_INDEX_FILE", SimpleNamespace(exists=lambda: False)), \
         patch("api.routes._lookup_cli_session_metadata", return_value={"profile": "other"}), \
         patch("api.routes.get_cli_session_messages", return_value=[{"role": "user", "content": "foreign profile secret"}]), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_get(SimpleNamespace(headers={"Cookie": "hermes_profile=default"}), parsed)

    assert captured.get("bad", {}).get("status") == 404
    assert "json" not in captured, "foreign-profile CLI transcript must not be returned"


# ── Direct session export must also honor active profile ─────────────────


class _ExportCaptureHandler:
    def __init__(self):
        self.headers = {}
        self.status = None
        self.sent_headers = []
        self.ended = False
        self.wfile = SimpleNamespace(write=self._write)
        self.body = b""

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        self.ended = True

    def _write(self, data):
        self.body += data


def test_session_export_rejects_session_from_inactive_profile():
    """A known session_id from another profile must not bypass /api/sessions scoping.

    /api/sessions hides foreign-profile rows by default, but the export endpoint
    loaded directly by id and serialized the sidecar. It must apply the same
    active-profile check before writing the JSON attachment.
    """
    import api.routes as routes

    captured = {}

    def fake_bad(_handler, message, status=400, **_kwargs):
        captured["bad"] = {"message": message, "status": status}
        return captured["bad"]

    foreign = SimpleNamespace(
        session_id="foreign_export_001",
        profile="other",
        messages=[{"role": "user", "content": "foreign profile secret"}],
    )

    handler = _ExportCaptureHandler()
    parsed = urlparse("/api/session/export?session_id=foreign_export_001")
    with patch("api.routes.get_session", return_value=foreign), \
         patch("api.routes.get_active_profile_name", return_value="default"), \
         patch("api.routes.bad", side_effect=fake_bad):
        routes._handle_session_export(handler, parsed)

    assert captured.get("bad", {}).get("status") == 404
    assert handler.status is None
    assert handler.body == b""


def test_session_export_allows_session_from_active_profile():
    """Same-profile exports still stream the redacted JSON attachment."""
    import api.routes as routes

    active = SimpleNamespace(
        session_id="active_export_001",
        profile="default",
        messages=[{"role": "user", "content": "same profile content"}],
    )

    handler = _ExportCaptureHandler()
    parsed = urlparse("/api/session/export?session_id=active_export_001")
    with patch("api.routes.get_session", return_value=active), \
         patch("api.routes.get_active_profile_name", return_value="default"), \
         patch("api.routes.redact_session_data", side_effect=lambda data: data):
        routes._handle_session_export(handler, parsed)

    assert handler.status == 200
    assert handler.ended is True
    assert b"same profile content" in handler.body
    assert ("Cache-Control", "no-store") in handler.sent_headers


# ── Imported sessions must be stamped with the active profile ───────────────


class _ImportedSessionStub:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.session_id = kwargs.get("session_id") or "imported_profile_001"
        self.profile = kwargs.get("profile")
        self.messages = kwargs.get("messages") or []
        self.pinned = False

    def save(self):
        self.saved = True

    def compact(self):
        return {
            "session_id": self.session_id,
            "profile": self.profile,
            "workspace": getattr(self, "workspace", None),
            "message_count": len(self.messages),
        }


def test_session_import_stamps_active_profile():
    """JSON imports must not create root/default-owned rows from named profiles.

    The import route validates the workspace under the request's active profile.
    If the new Session is then saved with profile=None, default/root requests can
    later export the transcript or use the session id to read files from that
    named-profile workspace. Pin the import-time ownership stamp directly.
    """
    import api.routes as routes

    captured = {}
    body = {
        "title": "Named profile import",
        "workspace": "/tmp/named-profile-workspace",
        "model": "test-model",
        "messages": [{"role": "user", "content": "named profile secret"}],
    }

    def fake_j(_handler, data, status=200, **_kwargs):
        captured["json"] = {"data": data, "status": status}
        return captured["json"]

    sessions = OrderedDict()
    with patch("api.routes.get_active_profile_name", return_value="poc"), \
         patch("api.routes.resolve_trusted_workspace", return_value=Path("/tmp/named-profile-workspace")), \
         patch("api.routes.Session", side_effect=lambda **kwargs: _ImportedSessionStub(**kwargs)), \
         patch.object(routes, "SESSIONS", sessions), \
         patch("api.routes.publish_session_list_changed"), \
         patch("api.routes.j", side_effect=fake_j):
        routes._handle_session_import(SimpleNamespace(headers={}), body)

    session = captured["json"]["data"]["session"]
    assert captured["json"]["status"] == 200
    assert session["profile"] == "poc"
    assert sessions["imported_profile_001"].profile == "poc"


def test_session_import_default_profile_remains_default_owned():
    """Root/default imports keep the legacy default ownership semantics."""
    import api.routes as routes

    captured = {}
    body = {
        "messages": [{"role": "user", "content": "default profile content"}],
    }

    def fake_j(_handler, data, status=200, **_kwargs):
        captured["json"] = {"data": data, "status": status}
        return captured["json"]

    sessions = OrderedDict()
    with patch("api.routes.get_active_profile_name", return_value="default"), \
         patch("api.routes.resolve_trusted_workspace", return_value=Path("/tmp/default-workspace")), \
         patch("api.routes.Session", side_effect=lambda **kwargs: _ImportedSessionStub(**kwargs)), \
         patch.object(routes, "SESSIONS", sessions), \
         patch("api.routes.publish_session_list_changed"), \
         patch("api.routes.j", side_effect=fake_j):
        routes._handle_session_import(SimpleNamespace(headers={}), body)

    session = captured["json"]["data"]["session"]
    assert session["profile"] == "default"
    assert sessions["imported_profile_001"].profile == "default"


def _profile_state_db_path(profile: str | None = None) -> Path:
    root = Path(os.environ["HERMES_WEBUI_TEST_STATE_DIR"])
    if profile:
        return root / "profiles" / profile / "state.db"
    return root / "state.db"


def _ensure_agent_state_db(profile: str | None = None) -> sqlite3.Connection:
    db_path = _profile_state_db_path(profile)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            title TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL
        );
    """)
    conn.commit()
    return conn


def _insert_agent_session(conn: sqlite3.Connection, session_id: str, *, source: str, title: str) -> None:
    started_at = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO sessions (id, source, title, model, started_at, message_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, source, title, "openai/gpt-5", started_at, 2),
    )
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
        (session_id, "Hello from other profile", started_at),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'assistant', ?, ?)",
        (session_id, "Reply from other profile", started_at + 1),
    )
    conn.commit()


def _delete_agent_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()


def _get_json(path: str) -> tuple[dict, int]:
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read()), resp.status


def _post_json(path: str, body: dict) -> tuple[dict, int]:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read()), resp.status


def test_all_profiles_query_includes_named_profile_cli_sessions():
    """all_profiles=1 should aggregate agent sessions from non-active named profiles."""
    conn = _ensure_agent_state_db("issue1611-named")
    sid = "issue1611_named_profile_cli_001"
    try:
        _insert_agent_session(
            conn,
            sid,
            source="telegram",
            title="Named Profile Telegram Session",
        )
        _post_json("/api/settings", {"show_cli_sessions": True})

        scoped, scoped_status = _get_json("/api/sessions")
        assert scoped_status == 200
        assert sid not in {row.get("session_id") for row in scoped.get("sessions", [])}

        aggregate, aggregate_status = _get_json("/api/sessions?all_profiles=1")
        assert aggregate_status == 200
        session = next(
            row for row in aggregate.get("sessions", [])
            if row.get("session_id") == sid
        )
        assert session.get("profile") == "issue1611-named"
        assert aggregate.get("all_profiles") is True
    finally:
        try:
            _post_json("/api/settings", {"show_cli_sessions": False})
        finally:
            _delete_agent_session(conn, sid)
            conn.close()

# ── Cleanup ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _invalidate_profile_cache():
    import api.profiles as p
    p._invalidate_root_profile_cache()
    yield
    p._invalidate_root_profile_cache()
