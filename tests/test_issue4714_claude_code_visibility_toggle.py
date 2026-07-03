"""Regression coverage for Issue #4714: hide Claude Code imports independently.

The route must keep `show_cli_sessions` as the parent gate while allowing
`show_claude_code_sessions` to filter only Claude Code rows.
"""

import io
import json
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import api.routes as routes
import api.models as models
import api.profiles as profiles
import pytest

ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = ROOT / "static" / "panels.js"
INDEX_HTML = ROOT / "static" / "index.html"


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _handle_sessions(url):
    handler = _FakeHandler()
    routes.handle_get(handler, urlparse(url))
    return handler


@pytest.fixture(autouse=True)
def _clear_cache():
    routes._session_list_cache_clear()
    models.clear_cli_sessions_cache()
    yield
    routes._session_list_cache_clear()
    models.clear_cli_sessions_cache()


def _common_monkeypatches(monkeypatch, rows, cli_rows):
    monkeypatch.setattr(routes, "load_settings", lambda: {
        "show_cli_sessions": True,
        "show_previous_messaging_sessions": False,
        "show_cron_sessions": False,
    })
    monkeypatch.setattr(routes, "all_sessions", lambda diag=None: list(rows))
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda rows: None)
    monkeypatch.setattr(routes, "agent_session_rows_existing", lambda ids, profile=None: {row["session_id"] for row in rows})
    monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False, include_claude_code=True: cli_rows)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")


def _extract_between(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def _rows_webui():
    return [
        {
            "session_id": "webui-1",
            "title": "WebUI",
            "profile": "default",
            "archived": False,
            "message_count": 5,
            "updated_at": 1000,
            "last_message_at": 1000,
            "source": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "source_tag": "webui",
            "is_cli_session": False,
        }
    ]


def _row(session_id, source, raw_source, source_tag="cli", source_label=None, is_cli_session=True):
    return {
        "session_id": session_id,
        "title": f"{session_id} title",
        "profile": "default",
        "archived": False,
        "message_count": 2,
        "updated_at": 2000,
        "last_message_at": 2000,
        "source": source,
        "raw_source": raw_source,
        "session_source": raw_source,
        "source_tag": source_tag,
        "source_label": source_label or source,
        "is_cli_session": is_cli_session,
    }


def test_show_cli_sessions_false_hides_all_imported_rows(monkeypatch):
    """When the parent toggle is off, no imported rows appear."""
    rows = _rows_webui()
    cli_rows = [_row("external-cli", "cli", "cli"), _row("external-claude", "cli", "claude_code")]
    _common_monkeypatches(monkeypatch, rows, cli_rows)
    monkeypatch.setattr(routes, "load_settings", lambda: {
        "show_cli_sessions": False,
        "show_claude_code_sessions": False,
        "show_previous_messaging_sessions": False,
        "show_cron_sessions": False,
    })

    handler = _handle_sessions("http://example.com/api/sessions")
    body = handler.json_body()

    assert handler.status == 200
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["session_id"] == "webui-1"
    assert body["cli_count"] == 0


def test_show_claude_code_sessions_false_hides_only_claude_code_rows(monkeypatch):
    """Turning the Claude-specific toggle off should hide Claude Code while keeping
    other imported rows."""
    rows = _rows_webui()
    all_cli_rows = [
        _row("external-cli", "cli", "cli", source_tag="cli"),
        _row("external-claude", "cli", "claude_code", source_tag="cli"),
    ]
    include_claude = True

    def fake_get_cli_sessions(source_filter=None, all_profiles=False, include_claude_code=True):
        nonlocal include_claude
        include_claude = include_claude_code
        if include_claude_code:
            return list(all_cli_rows)
        return [row for row in all_cli_rows if row["raw_source"] != "claude_code"]

    _common_monkeypatches(monkeypatch, rows, [])
    monkeypatch.setattr(routes, "get_cli_sessions", fake_get_cli_sessions)
    monkeypatch.setattr(routes, "load_settings", lambda: {
        "show_cli_sessions": True,
        "show_claude_code_sessions": False,
        "show_previous_messaging_sessions": False,
        "show_cron_sessions": False,
    })

    handler = _handle_sessions("http://example.com/api/sessions")
    body = handler.json_body()

    assert handler.status == 200
    assert include_claude is False
    assert {row["session_id"] for row in body["sessions"]} == {"webui-1", "external-cli"}


def test_show_claude_code_sessions_true_keeps_claude_code_rows_visible(monkeypatch):
    """With both toggles enabled, Claude Code rows are visible."""
    rows = _rows_webui()
    all_cli_rows = [
        _row("external-cli", "cli", "cli"),
        _row("external-claude", "cli", "claude_code"),
    ]
    _common_monkeypatches(monkeypatch, rows, all_cli_rows)
    monkeypatch.setattr(routes, "load_settings", lambda: {
        "show_cli_sessions": True,
        "show_claude_code_sessions": True,
        "show_previous_messaging_sessions": False,
        "show_cron_sessions": False,
    })

    handler = _handle_sessions("http://example.com/api/sessions")
    body = handler.json_body()

    assert handler.status == 200
    assert {row["session_id"] for row in body["sessions"]} == {
        "webui-1",
        "external-cli",
        "external-claude",
    }


def test_session_list_cache_key_changes_with_claude_code_toggle():
    """Cache keys must encode the Claude Code toggle."""
    key_false = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_claude_code_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    key_true = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_claude_code_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    assert key_false != key_true


def test_session_list_cache_key_default_keeps_claude_code_enabled():
    """Helper callers that omit the flag should match the config default."""
    key_default = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    key_true = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_claude_code_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    assert key_default == key_true


def test_cli_sessions_cache_key_varies_with_claude_code_toggle(monkeypatch):
    """The lower CLI-session cache must also key on the Claude Code toggle."""
    calls = []

    def fake_resolve(source_filter=None):
        return Path("D:/tmp/hermes"), Path("D:/tmp/hermes/state.db"), "default", ("ctx", source_filter or "")

    def fake_load(_home, _db_path, _profile, **kwargs):
        include = kwargs["include_claude_code"]
        calls.append(include)
        return [{"session_id": "claude" if include else "plain"}]

    monkeypatch.setattr(models, "_resolve_cli_sessions_context", fake_resolve)
    monkeypatch.setattr(models, "_load_cli_sessions_uncached", fake_load)
    monkeypatch.setattr(models, "_cli_sessions_cache_ttl_seconds", lambda: 60.0)
    monkeypatch.setattr(models, "_cli_sessions_streaming_freeze_marker", lambda: None)

    visible = models.get_cli_sessions(include_claude_code=True)
    hidden = models.get_cli_sessions(include_claude_code=False)

    assert calls == [True, False]
    assert visible == [{"session_id": "claude"}]
    assert hidden == [{"session_id": "plain"}]


def test_sessions_route_supports_historical_get_cli_sessions_signature(monkeypatch):
    """Route compatibility should not rely on swallowing internal TypeErrors."""
    rows = _rows_webui()
    _common_monkeypatches(monkeypatch, rows, [])
    calls = []

    def historical_get_cli_sessions(source_filter=None, all_profiles=False):
        calls.append((source_filter, all_profiles))
        return [_row("external-cli", "cli", "cli")]

    monkeypatch.setattr(routes, "get_cli_sessions", historical_get_cli_sessions)

    handler = _handle_sessions("http://example.com/api/sessions")
    body = handler.json_body()

    assert handler.status == 200
    assert calls == [(None, False)]
    assert {row["session_id"] for row in body["sessions"]} == {"webui-1", "external-cli"}


def test_all_profiles_scans_claude_code_only_once(monkeypatch):
    """All-profiles mode should keep the old single global Claude Code scan."""
    calls = []

    def fake_contexts():
        return [
            (Path("D:/tmp/profile-a"), Path("D:/tmp/profile-a/state.db"), "profile-a"),
            (Path("D:/tmp/profile-b"), Path("D:/tmp/profile-b/state.db"), "profile-b"),
        ], ("profile-a", "profile-b")

    def fake_load(_home, _db_path, profile, **kwargs):
        include = kwargs["include_claude_code"]
        calls.append((profile, include))
        rows = [{"session_id": f"{profile}-cli"}]
        if include:
            rows.append({"session_id": "claude-global"})
        return rows

    monkeypatch.setattr(models, "_all_profiles_cli_contexts", fake_contexts)
    monkeypatch.setattr(models, "_load_cli_sessions_uncached", fake_load)
    monkeypatch.setattr(models, "_cli_sessions_cache_ttl_seconds", lambda: 0.0)

    rows = models.get_cli_sessions(all_profiles=True, include_claude_code=True)

    assert calls == [("profile-a", True), ("profile-b", False)]
    assert [row["session_id"] for row in rows] == [
        "profile-a-cli",
        "claude-global",
        "profile-b-cli",
    ]


def test_preferences_autosave_preserves_claude_code_opt_out_default():
    """Autosave must not stomp the opt-out child when the parent is off."""
    autosave_block = _extract_between(
        PANELS_JS.read_text(encoding="utf-8"),
        "  const showCliCb=$('settingsShowCliSessions');",
        "  const syncCb=$('settingsSyncInsights');",
    )
    script = f"""
const block = {json.dumps(autosave_block)};
const elements = {{
  settingsShowCliSessions: {{ checked: false }},
  settingsShowClaudeCodeSessions: {{ checked: true }},
  settingsShowCronSessions: {{ checked: true }},
  settingsShowPreviousMessagingSessions: {{ checked: false }},
}};
function $(id) {{
  return elements[id] || null;
}}
const payload = {{}};
eval(block);
console.log(JSON.stringify(payload));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)

    assert payload["show_cli_sessions"] is False
    assert payload["show_claude_code_sessions"] is True


def test_claude_code_checkbox_is_parent_gated_in_ui():
    """The child checkbox should disable live when the parent turns off."""
    settings_block = _extract_between(
        PANELS_JS.read_text(encoding="utf-8"),
        "    const showCliCb=$('settingsShowCliSessions');",
        "    const showPreviousMessagingCb=$('settingsShowPreviousMessagingSessions');",
    )
    script = f"""
const block = {json.dumps(settings_block)};
function makeCheckbox(checked) {{
  return {{
    checked,
    disabled: false,
    listeners: {{}},
    addEventListener(name, fn) {{
      this.listeners[name] = fn;
    }},
  }};
}}
const elements = {{
  settingsShowCliSessions: makeCheckbox(true),
  settingsShowClaudeCodeSessions: makeCheckbox(true),
  settingsShowCronSessions: makeCheckbox(true),
}};
const settings = {{
  show_cli_sessions: true,
  show_claude_code_sessions: true,
  show_cron_sessions: true,
}};
let autosaveCalls = 0;
function _schedulePreferencesAutosave() {{
  autosaveCalls += 1;
}}
function $(id) {{
  return elements[id] || null;
}}
eval(block);
const initial = {{
  claudeDisabled: elements.settingsShowClaudeCodeSessions.disabled,
  cronDisabled: elements.settingsShowCronSessions.disabled,
}};
elements.settingsShowCliSessions.checked = false;
elements.settingsShowCliSessions.listeners.change();
console.log(JSON.stringify({{
  initial,
  after: {{
    claudeDisabled: elements.settingsShowClaudeCodeSessions.disabled,
    cronDisabled: elements.settingsShowCronSessions.disabled,
    autosaveCalls,
  }},
}}));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)

    assert payload["initial"] == {
        "claudeDisabled": False,
        "cronDisabled": False,
    }
    assert payload["after"] == {
        "claudeDisabled": True,
        "cronDisabled": True,
        "autosaveCalls": 1,
    }


def test_claude_code_checkbox_parent_listener_does_not_depend_on_cron_checkbox():
    """The Claude child checkbox must still follow the parent without the cron node."""
    settings_block = _extract_between(
        PANELS_JS.read_text(encoding="utf-8"),
        "    const showCliCb=$('settingsShowCliSessions');",
        "    const showPreviousMessagingCb=$('settingsShowPreviousMessagingSessions');",
    )
    script = f"""
const block = {json.dumps(settings_block)};
function makeCheckbox(checked) {{
  return {{
    checked,
    disabled: false,
    listeners: {{}},
    addEventListener(name, fn) {{
      this.listeners[name] = fn;
    }},
  }};
}}
const elements = {{
  settingsShowCliSessions: makeCheckbox(true),
  settingsShowClaudeCodeSessions: makeCheckbox(true),
}};
const settings = {{
  show_cli_sessions: true,
  show_claude_code_sessions: true,
}};
let autosaveCalls = 0;
function _schedulePreferencesAutosave() {{
  autosaveCalls += 1;
}}
function $(id) {{
  return elements[id] || null;
}}
eval(block);
elements.settingsShowCliSessions.checked = false;
elements.settingsShowCliSessions.listeners.change();
console.log(JSON.stringify({{
  claudeDisabled: elements.settingsShowClaudeCodeSessions.disabled,
  autosaveCalls,
}}));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)

    assert payload == {
        "claudeDisabled": True,
        "autosaveCalls": 1,
    }


def test_save_settings_preserves_claude_code_opt_out_default():
    """Explicit save must not persist the opt-out child as false via the parent gate."""
    save_block = _extract_between(
        PANELS_JS.read_text(encoding="utf-8"),
        "  body.show_cli_sessions=showCliSessions;",
        "  body.pinned_sessions_limit=pinnedSessionsLimit;",
    )
    script = f"""
const block = {json.dumps(save_block)};
const showCliSessions = false;
const showClaudeCodeSessions = true;
const showCronSessions = true;
const showPreviousMessagingSessions = false;
const body = {{}};
eval(block);
console.log(JSON.stringify(body));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    body = json.loads(result.stdout)

    assert body["show_cli_sessions"] is False
    assert body["show_claude_code_sessions"] is True


def test_locale_keys_exist_in_every_locale_block():
    """Every locale block should carry the Claude Code label and description keys."""
    i18n = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

    assert i18n.count("settings_label_claude_code_sessions:") == i18n.count("settings_label_api_redact:")
    assert i18n.count("settings_desc_claude_code_sessions:") == i18n.count("settings_desc_previous_messaging_sessions:")
