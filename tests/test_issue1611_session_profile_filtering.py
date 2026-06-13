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

    assert "_showAllProfiles ? '?all_profiles=1' : ''" in src, (
        "Expected fetch path to flip on the toggle state"
    )
    assert "api('/api/sessions' + allProfilesQS,{timeoutToast:false})" in src, (
        "Expected /api/sessions fetch to use the variant query"
    )
    assert "api('/api/projects' + allProfilesQS,{timeoutToast:false})" in src, (
        "Expected /api/projects fetch to use the variant query"
    )


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
