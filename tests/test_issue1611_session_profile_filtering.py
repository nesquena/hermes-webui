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

from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import pytest


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


# ── Direct session export must also honor active profile ───────────────────


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


# ── Sibling session read/mutate paths must also honor active profile ───────
#
# /api/session and /api/session/export are not the only routes that load a
# session by id and return (or mutate) its transcript. duplicate / update /
# truncate / import_cli all loaded by id without the active-profile boundary,
# so a foreign-profile transcript could leak or be mutated by a caller scoped
# to a different profile. These guards close that class.


def test_session_duplicate_rejects_session_from_inactive_profile():
    """POST /api/session/duplicate must not copy/return a foreign-profile transcript."""
    import api.routes as routes

    captured = {}

    def fake_bad(_handler, message, status=400, **_kwargs):
        captured["bad"] = {"message": message, "status": status}
        return captured["bad"]

    def fake_j(_handler, data, status=200, **_kwargs):
        captured["json"] = {"data": data, "status": status}
        return captured["json"]

    foreign = SimpleNamespace(
        session_id="foreign_dup_001",
        profile="other",
        title="Foreign",
        messages=[{"role": "user", "content": "foreign profile secret"}],
    )
    parsed = urlparse("/api/session/duplicate")
    body = {"session_id": "foreign_dup_001"}
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes._check_csrf", return_value=True), \
         patch("api.routes.read_body", return_value=body), \
         patch("api.routes.Session") as MockSession, \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        MockSession.load.return_value = foreign
        routes.handle_post(SimpleNamespace(headers={}), parsed)

    assert captured.get("bad", {}).get("status") == 404
    assert "json" not in captured, "foreign-profile transcript must not be duplicated/returned"


def test_session_update_rejects_session_from_inactive_profile():
    """POST /api/session/update must not read/mutate a foreign-profile session."""
    import api.routes as routes

    captured = {}

    def fake_bad(_handler, message, status=400, **_kwargs):
        captured["bad"] = {"message": message, "status": status}
        return captured["bad"]

    def fake_j(_handler, data, status=200, **_kwargs):
        captured["json"] = {"data": data, "status": status}
        return captured["json"]

    foreign = SimpleNamespace(
        session_id="foreign_upd_001",
        profile="other",
        workspace="/tmp/foreign",
        model="gpt-test",
        model_provider="openai",
        messages=[{"role": "user", "content": "foreign profile secret"}],
    )
    parsed = urlparse("/api/session/update")
    body = {"session_id": "foreign_upd_001", "workspace": "/tmp/x"}
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes._check_csrf", return_value=True), \
         patch("api.routes.read_body", return_value=body), \
         patch("api.routes.get_session", return_value=foreign), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_post(SimpleNamespace(headers={}), parsed)

    assert captured.get("bad", {}).get("status") == 404
    assert "json" not in captured, "foreign-profile session must not be updated/returned"


def test_session_truncate_rejects_session_from_inactive_profile():
    """POST /api/session/truncate must not read/mutate a foreign-profile session."""
    import api.routes as routes

    captured = {}

    def fake_bad(_handler, message, status=400, **_kwargs):
        captured["bad"] = {"message": message, "status": status}
        return captured["bad"]

    def fake_j(_handler, data, status=200, **_kwargs):
        captured["json"] = {"data": data, "status": status}
        return captured["json"]

    foreign = SimpleNamespace(
        session_id="foreign_trunc_001",
        profile="other",
        messages=[{"role": "user", "content": "foreign profile secret"}],
    )
    parsed = urlparse("/api/session/truncate")
    body = {"session_id": "foreign_trunc_001", "keep_count": 1}
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes._check_csrf", return_value=True), \
         patch("api.routes.read_body", return_value=body), \
         patch("api.routes.get_session", return_value=foreign), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_post(SimpleNamespace(headers={}), parsed)

    assert captured.get("bad", {}).get("status") == 404
    assert "json" not in captured, "foreign-profile session must not be truncated/returned"


def test_session_import_cli_rejects_existing_sidecar_from_inactive_profile():
    """_handle_session_import_cli must not refresh/return a foreign-profile sidecar."""
    import api.routes as routes

    captured = {}

    def fake_bad(_handler, message, status=400, **_kwargs):
        captured["bad"] = {"message": message, "status": status}
        return captured["bad"]

    def fake_j(_handler, data, status=200, **_kwargs):
        captured["json"] = {"data": data, "status": status}
        return captured["json"]

    foreign = SimpleNamespace(
        session_id="foreign_cli_001",
        profile="other",
        messages=[{"role": "user", "content": "foreign profile secret"}],
    )
    body = {"session_id": "foreign_cli_001"}
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes.Session") as MockSession, \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        MockSession.load.return_value = foreign
        routes._handle_session_import_cli(SimpleNamespace(headers={}), body)

    assert captured.get("bad", {}).get("status") == 404
    assert "json" not in captured, "foreign-profile CLI sidecar must not be refreshed/returned"


# ── Destructive + derive paths must also honor active profile (LX class sweep) ──
#
# delete / clear / branch / retry / undo / compress / handoff-summary / fresh
# import_cli all loaded a session by id without the active-profile boundary, so a
# caller scoped to one profile could DESTROY (delete/clear/truncate/retry/undo) or
# derive-from (branch/compress/handoff/import) another profile's transcript by id.


def _post_capture():
    captured = {}

    def fake_bad(_handler, message, status=400, **_kwargs):
        captured["bad"] = {"message": message, "status": status}
        return captured["bad"]

    def fake_j(_handler, data, status=200, **_kwargs):
        captured["json"] = {"data": data, "status": status}
        return captured["json"]

    return captured, fake_bad, fake_j


def test_session_delete_rejects_session_from_inactive_profile():
    """POST /api/session/delete must not delete a foreign-profile session."""
    import api.routes as routes

    captured, fake_bad, fake_j = _post_capture()
    foreign = SimpleNamespace(profile="other")
    parsed = urlparse("/api/session/delete")
    body = {"session_id": "foreign_del_001"}
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes._check_csrf", return_value=True), \
         patch("api.routes.read_body", return_value=body), \
         patch("api.routes.is_safe_session_id", return_value=True), \
         patch("api.routes._lookup_cli_session_metadata", return_value={}), \
         patch("api.routes._is_messaging_session_id", return_value=False), \
         patch("api.routes._worktree_retained_payload_for_session_id", return_value={}), \
         patch("api.routes.get_session", return_value=foreign), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_post(SimpleNamespace(headers={}), parsed)

    assert captured.get("bad", {}).get("status") == 404
    assert "json" not in captured, "foreign-profile session must not be deleted"


def test_session_clear_rejects_session_from_inactive_profile():
    """POST /api/session/clear must not wipe a foreign-profile transcript."""
    import api.routes as routes

    captured, fake_bad, fake_j = _post_capture()
    foreign = SimpleNamespace(profile="other", messages=[{"role": "user", "content": "x"}], tool_calls=[])
    parsed = urlparse("/api/session/clear")
    body = {"session_id": "foreign_clr_001"}
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes._check_csrf", return_value=True), \
         patch("api.routes.read_body", return_value=body), \
         patch("api.routes.get_session", return_value=foreign), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_post(SimpleNamespace(headers={}), parsed)

    assert captured.get("bad", {}).get("status") == 404
    assert foreign.messages == [{"role": "user", "content": "x"}], "transcript must not be wiped"
    assert "json" not in captured


def test_session_branch_rejects_session_from_inactive_profile():
    """POST /api/session/branch must not fork a foreign-profile transcript."""
    import api.routes as routes

    captured, fake_bad, fake_j = _post_capture()
    foreign = SimpleNamespace(profile="other", messages=[{"role": "user", "content": "x"}], title="F")
    parsed = urlparse("/api/session/branch")
    body = {"session_id": "foreign_br_001"}
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes._check_csrf", return_value=True), \
         patch("api.routes.read_body", return_value=body), \
         patch("api.routes.get_session", return_value=foreign), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_post(SimpleNamespace(headers={}), parsed)

    assert captured.get("bad", {}).get("status") == 404
    assert "json" not in captured, "foreign-profile session must not be forked/returned"


def test_session_retry_rejects_session_from_inactive_profile():
    """POST /api/session/retry must not truncate/return a foreign-profile transcript."""
    import api.routes as routes
    import api.session_ops as session_ops

    captured, fake_bad, fake_j = _post_capture()
    meta = SimpleNamespace(profile="other")
    parsed = urlparse("/api/session/retry")
    body = {"session_id": "foreign_rt_001"}
    called = {"retry": False}

    def _retry(_sid):
        called["retry"] = True
        return {}

    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes._check_csrf", return_value=True), \
         patch("api.routes.read_body", return_value=body), \
         patch("api.routes.get_session", return_value=meta), \
         patch.object(session_ops, "retry_last", side_effect=_retry), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_post(SimpleNamespace(headers={}), parsed)

    assert captured.get("bad", {}).get("status") == 404
    assert called["retry"] is False, "retry_last must not run for a foreign-profile session"


def test_session_undo_rejects_session_from_inactive_profile():
    """POST /api/session/undo must not truncate/return a foreign-profile transcript."""
    import api.routes as routes
    import api.session_ops as session_ops

    captured, fake_bad, fake_j = _post_capture()
    meta = SimpleNamespace(profile="other")
    parsed = urlparse("/api/session/undo")
    body = {"session_id": "foreign_un_001"}
    called = {"undo": False}

    def _undo(_sid):
        called["undo"] = True
        return {}

    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes._check_csrf", return_value=True), \
         patch("api.routes.read_body", return_value=body), \
         patch("api.routes.get_session", return_value=meta), \
         patch.object(session_ops, "undo_last", side_effect=_undo), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_post(SimpleNamespace(headers={}), parsed)

    assert captured.get("bad", {}).get("status") == 404
    assert called["undo"] is False, "undo_last must not run for a foreign-profile session"


def test_session_compress_rejects_session_from_inactive_profile():
    """_handle_session_compress must not compress/return a foreign-profile transcript."""
    import api.routes as routes

    captured, fake_bad, fake_j = _post_capture()
    foreign = SimpleNamespace(
        profile="other", active_stream_id=None,
        messages=[{"role": "user", "content": "x"}],
    )
    body = {"session_id": "foreign_cmp_001"}
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes.get_session", return_value=foreign), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes._handle_session_compress(SimpleNamespace(headers={}), body)

    assert captured.get("bad", {}).get("status") == 404
    assert "json" not in captured, "foreign-profile transcript must not be compressed/returned"


def test_session_handoff_summary_rejects_session_from_inactive_profile():
    """_handle_handoff_summary must not summarize a foreign-profile CLI transcript."""
    import api.routes as routes

    captured, fake_bad, fake_j = _post_capture()
    body = {"session_id": "foreign_ho_001"}
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes._lookup_cli_session_metadata", return_value={"profile": "other"}), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes._handle_handoff_summary(SimpleNamespace(headers={}), body)

    assert captured.get("bad", {}).get("status") == 404
    assert "json" not in captured, "foreign-profile CLI transcript must not be summarized"


def test_session_import_cli_fresh_rejects_session_from_inactive_profile():
    """_handle_session_import_cli fresh (no-sidecar) branch must reject foreign profiles."""
    import api.routes as routes

    captured, fake_bad, fake_j = _post_capture()
    body = {"session_id": "foreign_fresh_001"}
    leaked = {"read": False}

    def _get_cli_msgs(_sid):
        leaked["read"] = True
        return [{"role": "user", "content": "foreign profile secret"}]

    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes.Session") as MockSession, \
         patch("api.routes._lookup_cli_session_metadata", return_value={"profile": "other"}), \
         patch("api.routes.get_cli_session_messages", side_effect=_get_cli_msgs), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        MockSession.load.return_value = None  # no existing sidecar -> fresh branch
        routes._handle_session_import_cli(SimpleNamespace(headers={}), body)

    assert captured.get("bad", {}).get("status") == 404
    assert leaked["read"] is False, "foreign-profile CLI transcript must not be read before the guard"
    assert "json" not in captured


# ── Turn-start / derive paths must also honor active profile (LX class sweep r2) ──
#
# chat/start, chat (sync fallback), btw, archive, draft (GET+POST), and
# title/regenerate all loaded a session by id and read/appended/derived from its
# transcript without the active-profile boundary.


def test_chat_start_rejects_session_from_inactive_profile():
    """POST /api/chat/start must not start a turn on a foreign-profile session."""
    import api.routes as routes

    captured, fake_bad, fake_j = _post_capture()
    started = {"run": False}

    def _start_run(*a, **k):
        started["run"] = True
        return {}

    foreign = SimpleNamespace(
        profile="other", messages=[{"role": "user", "content": "x"}],
        context_messages=[], pending_user_message=None,
    )
    body = {"session_id": "foreign_cs_001", "message": "hi"}
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes.get_session", return_value=foreign), \
         patch("api.routes._start_run", side_effect=_start_run), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes._handle_chat_start(SimpleNamespace(headers={}), body)

    assert captured.get("bad", {}).get("status") == 404
    assert started["run"] is False, "a turn must not start on a foreign-profile session"


def test_chat_start_allows_empty_placeholder_retagged_to_active_profile():
    """An empty placeholder session retagged to the active profile must NOT be rejected."""
    import api.routes as routes

    captured, fake_bad, fake_j = _post_capture()
    started = {"run": False}

    def _start_run(*a, **k):
        started["run"] = True
        return {"ok": True}

    # Empty placeholder (no persisted turns) created under a stale profile.
    placeholder = SimpleNamespace(
        profile="stale", messages=[], context_messages=[], pending_user_message=None,
        session_id="ph_001", model="m", model_provider="p", workspace="/tmp/ws",
    )
    body = {"session_id": "ph_001", "message": "hi", "profile": "default"}
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes.get_session", return_value=placeholder), \
         patch("api.routes._normalize_chat_attachments", return_value=[]), \
         patch("api.routes._resolve_chat_workspace_with_recovery", return_value="/tmp/ws"), \
         patch("api.routes._read_profile_model_config", return_value=(None, None)), \
         patch("api.routes._resolve_compatible_session_model_state", return_value=("m", "p", "m")), \
         patch("api.routes._start_run", side_effect=_start_run), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes._handle_chat_start(SimpleNamespace(headers={}), body)

    # Retag set placeholder.profile to "default" (== active) so the guard passes.
    assert placeholder.profile == "default"
    assert started["run"] is True, "placeholder retagged to active profile must start normally"
    assert "bad" not in captured or captured.get("bad", {}).get("status") != 404


def test_chat_sync_rejects_session_from_inactive_profile():
    """POST /api/chat (sync fallback) must not read/return a foreign-profile transcript."""
    import api.routes as routes

    captured, fake_bad, fake_j = _post_capture()
    foreign = SimpleNamespace(profile="other", messages=[{"role": "user", "content": "x"}], workspace="/tmp")
    body = {"session_id": "foreign_csync_001", "message": "hi"}
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes.get_session", return_value=foreign), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes._handle_chat_sync(SimpleNamespace(headers={}), body)

    assert captured.get("bad", {}).get("status") == 404


def test_btw_rejects_session_from_inactive_profile():
    """POST /api/btw must not derive an answer from a foreign-profile transcript."""
    import api.routes as routes

    captured, fake_bad, fake_j = _post_capture()
    foreign = SimpleNamespace(profile="other", messages=[{"role": "user", "content": "x"}], active_stream_id=None)
    body = {"session_id": "foreign_btw_001", "question": "what?"}
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes.get_session", return_value=foreign), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes._handle_btw(SimpleNamespace(headers={}), body)

    assert captured.get("bad", {}).get("status") == 404


def test_session_archive_rejects_session_from_inactive_profile():
    """POST /api/session/archive must not archive a foreign-profile session."""
    import api.routes as routes

    captured, fake_bad, fake_j = _post_capture()
    foreign = SimpleNamespace(profile="other", _loaded_metadata_only=False, archived=False)
    parsed = urlparse("/api/session/archive")
    body = {"session_id": "foreign_arch_001"}
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes._check_csrf", return_value=True), \
         patch("api.routes.read_body", return_value=body), \
         patch("api.routes.get_session", return_value=foreign), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_post(SimpleNamespace(headers={}), parsed)

    assert captured.get("bad", {}).get("status") == 404


def test_session_draft_get_rejects_session_from_inactive_profile():
    """GET /api/session/draft must not return a foreign-profile draft."""
    import api.routes as routes

    captured, fake_bad, fake_j = _post_capture()
    foreign = SimpleNamespace(profile="other", composer_draft={"text": "secret draft"})
    parsed = urlparse("/api/session/draft?session_id=foreign_dr_001")
    handler = SimpleNamespace(headers={}, command="GET")
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes._check_csrf", return_value=True), \
         patch("api.routes.read_body", return_value={}), \
         patch("api.routes.get_session", return_value=foreign), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_post(handler, parsed)

    assert captured.get("bad", {}).get("status") == 404
    assert "json" not in captured, "foreign-profile draft must not be returned"


def test_session_title_regenerate_rejects_session_from_inactive_profile():
    """POST /api/session/title/regenerate must not derive a title from a foreign transcript."""
    import api.routes as routes

    captured, fake_bad, fake_j = _post_capture()
    foreign = SimpleNamespace(profile="other", read_only=False, is_imported=False,
                              messages=[{"role": "user", "content": "x"}])
    parsed = urlparse("/api/session/title/regenerate")
    body = {"session_id": "foreign_tr_001"}
    with patch("api.routes._get_active_profile_name", return_value="default"), \
         patch("api.routes._check_csrf", return_value=True), \
         patch("api.routes.read_body", return_value=body), \
         patch("api.routes.get_session", return_value=foreign), \
         patch("api.routes._ensure_full_session_before_mutation", return_value=foreign), \
         patch("api.routes.generate_session_title_for_session", return_value=("LEAKED", "ok", "raw")), \
         patch("api.routes.bad", side_effect=fake_bad), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_post(SimpleNamespace(headers={}), parsed)

    assert captured.get("bad", {}).get("status") == 404
    assert "json" not in captured, "foreign-profile title must not be generated/returned"


# ── Cleanup ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _invalidate_profile_cache():
    import api.profiles as p
    p._invalidate_root_profile_cache()
    yield
    p._invalidate_root_profile_cache()
