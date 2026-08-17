import json
import sqlite3
import subprocess
import textwrap
from urllib.parse import urlparse
from pathlib import Path

import pytest

from tests.test_webui_state_db_reconciliation import (
    _GetHandler,
    _append_state_db_rows,
    _install_test_session,
    _make_state_db,
)


pytestmark = pytest.mark.requires_agent_modules


def _foreign_fixture(monkeypatch, tmp_path, *, profile=None, truncation_watermark=None, truncation_boundary=None):
    import api.routes as routes

    routes._clear_foreign_display_coordinate_summary_cache()
    sid = "foreign_metadata_count_7050"
    tool_call = [{"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]
    sidecar_messages = [
        {"role": "user", "content": "hello", "timestamp": 1000.0},
        {"role": "assistant", "content": "", "timestamp": 1001.0, "tool_calls": tool_call},
        {"role": "tool", "content": "result", "timestamp": 1002.0, "tool_call_id": "call-1"},
    ]
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    session.is_cli_session = True
    session.source_tag = "tui"
    session.raw_source = "tui"
    session.session_source = "tui"
    session.profile = profile
    session.truncation_watermark = truncation_watermark
    session.truncation_boundary = truncation_boundary
    session.save(touch_updated_at=False)
    monkeypatch.setattr(
        routes,
        "_lookup_cli_session_metadata",
        lambda _sid: {
            "source_tag": "tui",
            "raw_source": "tui",
            "session_source": "tui",
            "is_cli_session": True,
        },
    )
    state_sidecar_messages = [
        {**message, "tool_calls": json.dumps(message["tool_calls"])}
        if message.get("tool_calls")
        else message
        for message in sidecar_messages
    ]
    _make_state_db(
        tmp_path / "state.db",
        sid,
        state_sidecar_messages
        + [
            dict(sidecar_messages[0]),
            {"role": "assistant", "content": "done", "timestamp": 1003.0},
        ],
    )
    return sid


def _session_payload(routes, path):
    handler = _GetHandler(path)
    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200
    return handler.response_json["session"]


def test_foreign_metadata_count_matches_display_coordinate(monkeypatch, tmp_path):
    import api.routes as routes

    sid = _foreign_fixture(monkeypatch, tmp_path)
    metadata = _session_payload(
        routes,
        f"/api/session?session_id={sid}&messages=0&resolve_model=0",
    )
    limited = _session_payload(
        routes,
        f"/api/session?session_id={sid}&messages=1&resolve_model=0&msg_limit=30",
    )
    unbounded = _session_payload(
        routes,
        f"/api/session?session_id={sid}&messages=1&resolve_model=0",
    )

    assert metadata["message_count"] == limited["message_count"] == unbounded["message_count"]
    assert metadata["last_message_at"] == limited["last_message_at"] == unbounded["last_message_at"]
    assert metadata["message_count"] == 4
    assert metadata["message_count"] != 5


def test_foreign_metadata_uses_session_markers_when_lookup_is_empty(monkeypatch, tmp_path):
    import api.routes as routes

    sid = _foreign_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {})
    metadata = _session_payload(
        routes,
        f"/api/session?session_id={sid}&messages=0&resolve_model=0",
    )
    assert metadata["message_count"] == 4
    assert metadata["last_message_at"] == 1003.0


def test_foreign_metadata_failure_falls_back_to_legacy_summary(monkeypatch, tmp_path):
    import api.routes as routes

    sid = _foreign_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        routes,
        "get_state_db_session_messages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("state db unavailable")),
    )
    payload = _session_payload(
        routes,
        f"/api/session?session_id={sid}&messages=0&resolve_model=0",
    )
    assert payload["message_count"] == 5
    assert payload["last_message_at"] == 1003.0


def test_foreign_metadata_empty_read_falls_back_to_legacy_summary(monkeypatch, tmp_path):
    import api.routes as routes

    sid = _foreign_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "get_state_db_session_messages", lambda *_args, **_kwargs: [])
    payload = _session_payload(
        routes,
        f"/api/session?session_id={sid}&messages=0&resolve_model=0",
    )
    assert payload["message_count"] == 5
    assert payload["last_message_at"] == 1003.0


def test_external_refresh_equal_and_mismatch_use_real_poll_consumer():
    sessions_js = (Path(__file__).resolve().parents[1] / "static" / "sessions.js").read_text()
    start = sessions_js.index("async function refreshActiveSessionIfExternallyUpdated")
    end = sessions_js.index("function ensureActiveSessionExternalRefreshPoll", start)
    function = sessions_js[start:end]
    script = textwrap.dedent(
        f"""
        const outcomes = [];
        const responses = [{{message_count: 4, last_message_at: 1003}}, {{message_count: 5, last_message_at: 1004}}];
        let responseIndex = 0;
        global.S = {{session: {{session_id: 'foreign', message_count: 4, last_message_at: 1003}}, messages: [1,2,3,4], busy: false, activeStreamId: null}};
        global.document = {{hidden: false}};
        global.window = {{}};
        global._activeSessionExternalRefreshInFlight = false;
        global._loadingSessionId = null;
        global._isMessageReaderUnpinned = () => false;
        global._deferActiveSessionExternalRefresh = () => {{}};
        global._isExternalSession = () => true;
        global.api = async () => ({{session: responses[responseIndex++]}});
        global.loadSession = async () => outcomes.push('reloaded');
        global._drainSessionUpdatedPendingCount = () => {{}};
        {function}
        (async () => {{
          outcomes.push(await refreshActiveSessionIfExternallyUpdated('poll'));
          outcomes.push(await refreshActiveSessionIfExternallyUpdated('poll'));
          process.stdout.write(JSON.stringify(outcomes));
        }})().catch((error) => {{ process.stderr.write(String(error.stack || error)); process.exit(1); }});
        """
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["unchanged", "reloaded", "reloaded"]


def test_foreign_metadata_poll_uses_profile_and_safe_backstop(monkeypatch, tmp_path):
    import api.routes as routes

    sid = _foreign_fixture(monkeypatch, tmp_path, profile="profile-7050")
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args: True)
    calls = []
    reader = routes.get_state_db_session_messages
    monkeypatch.setattr(
        routes,
        "get_state_db_session_messages",
        lambda session_id, **kwargs: (calls.append((session_id, kwargs)) or reader(session_id, **kwargs)),
    )
    _session_payload(routes, f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    assert calls[-1][1]["profile"] == "profile-7050"
    assert calls[-1][1]["limit"] == 50000


def test_foreign_metadata_poll_keeps_boundary_read_uncapped(monkeypatch, tmp_path):
    import api.routes as routes

    sid = _foreign_fixture(monkeypatch, tmp_path, truncation_boundary=1001.0)
    calls = []
    reader = routes.get_state_db_session_messages
    monkeypatch.setattr(
        routes,
        "get_state_db_session_messages",
        lambda session_id, **kwargs: (calls.append(kwargs) or reader(session_id, **kwargs)),
    )
    _session_payload(routes, f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    assert "limit" not in calls[-1]


def test_foreign_paged_load_matches_non_snapshot_parent_lineage(monkeypatch, tmp_path):
    import api.models as models
    import api.routes as routes

    routes._clear_foreign_display_coordinate_summary_cache()
    parent_id = "foreign_metadata_parent_7050"
    sid = "foreign_metadata_child_7050"
    child_messages = [
        {"role": "user", "content": f"child {idx}", "timestamp": 1000.0 + idx}
        for idx in range(35)
    ]
    child = _install_test_session(monkeypatch, tmp_path, sid, child_messages)
    parent_messages = [{"role": "user", "content": "parent", "timestamp": 900.0}]
    parent = models.Session(
        session_id=parent_id,
        title="Parent",
        workspace=str(tmp_path),
        model="test-model",
        messages=parent_messages,
        created_at=899.0,
        updated_at=900.0,
    )
    parent.save(touch_updated_at=False)
    child.parent_session_id = parent_id
    child.is_cli_session = True
    child.source_tag = "tui"
    child.raw_source = "tui"
    child.session_source = "tui"
    child.save(touch_updated_at=False)
    monkeypatch.setattr(
        routes,
        "_lookup_cli_session_metadata",
        lambda _sid: {"source_tag": "tui", "session_source": "tui", "is_cli_session": True},
    )
    _make_state_db(tmp_path / "state.db", sid, child_messages)

    metadata = _session_payload(routes, f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    paged = _session_payload(
        routes,
        f"/api/session?session_id={sid}&messages=1&resolve_model=0&msg_limit=30",
    )
    unbounded = _session_payload(routes, f"/api/session?session_id={sid}&messages=1&resolve_model=0")

    assert metadata["message_count"] == paged["message_count"] == unbounded["message_count"] == 36
    assert metadata["last_message_at"] == paged["last_message_at"] == unbounded["last_message_at"] == 1034.0
    assert len(paged["messages"]) == 30
    assert paged["_messages_offset"] == 6


def test_foreign_boundary_summary_cache_skips_unchanged_uncapped_read(monkeypatch, tmp_path):
    import api.routes as routes

    sid = _foreign_fixture(monkeypatch, tmp_path, truncation_boundary=1001.0)
    summary_calls = []
    reader_calls = []
    real_summary = routes.get_state_db_session_summary
    real_reader = routes.get_state_db_session_messages
    monkeypatch.setattr(
        routes,
        "get_state_db_session_summary",
        lambda session_id, **kwargs: (
            summary_calls.append((session_id, kwargs))
            or real_summary(session_id, **kwargs)
        ),
    )
    monkeypatch.setattr(
        routes,
        "get_state_db_session_messages",
        lambda session_id, **kwargs: (
            reader_calls.append((session_id, kwargs))
            or real_reader(session_id, **kwargs)
        ),
    )

    first = _session_payload(routes, f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    second = _session_payload(routes, f"/api/session?session_id={sid}&messages=0&resolve_model=0")

    assert first["message_count"] == second["message_count"]
    assert first["last_message_at"] == second["last_message_at"]
    assert len(summary_calls) == 2
    assert len(reader_calls) == 1
    assert "limit" not in reader_calls[0][1]

    _append_state_db_rows(
        tmp_path / "state.db",
        sid,
        [{"role": "assistant", "content": "new", "timestamp": 1004.0}],
    )
    _session_payload(routes, f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    assert len(reader_calls) == 2


def test_foreign_summary_cache_is_profile_scoped(monkeypatch, tmp_path):
    import api.routes as routes

    sid = _foreign_fixture(monkeypatch, tmp_path, truncation_boundary=1001.0)
    real_summary = routes.get_state_db_session_summary
    real_reader = routes.get_state_db_session_messages
    reader_profiles = []
    monkeypatch.setattr(
        routes,
        "get_state_db_session_summary",
        lambda session_id, **kwargs: real_summary(session_id, **kwargs),
    )
    monkeypatch.setattr(
        routes,
        "get_state_db_session_messages",
        lambda session_id, **kwargs: (
            reader_profiles.append(kwargs.get("profile"))
            or real_reader(session_id, **kwargs)
        ),
    )

    routes._foreign_display_coordinate_summary(sid, profile="profile-a")
    routes._foreign_display_coordinate_summary(sid, profile="profile-b")

    assert reader_profiles == ["profile-a", "profile-b"]


def test_foreign_summary_cache_reloads_changed_sidecar(monkeypatch, tmp_path):
    import api.models as models
    import api.routes as routes

    sid = _foreign_fixture(monkeypatch, tmp_path, truncation_boundary=1001.0)
    reader_calls = []
    real_reader = routes.get_state_db_session_messages
    monkeypatch.setattr(
        routes,
        "get_state_db_session_messages",
        lambda session_id, **kwargs: (
            reader_calls.append((session_id, kwargs))
            or real_reader(session_id, **kwargs)
        ),
    )

    _session_payload(routes, f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    session = models.Session.load(sid)
    session.messages[-1]["content"] = "changed without changing the count"
    session.save(touch_updated_at=False)
    _session_payload(routes, f"/api/session?session_id={sid}&messages=0&resolve_model=0")

    assert len(reader_calls) == 2


def test_foreign_summary_cache_tracks_active_state_changes(monkeypatch, tmp_path):
    import api.routes as routes

    sid = _foreign_fixture(monkeypatch, tmp_path, truncation_boundary=1001.0)
    with sqlite3.connect(tmp_path / "state.db") as conn:
        conn.execute("ALTER TABLE messages ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        conn.commit()
    _append_state_db_rows(
        tmp_path / "state.db",
        sid,
        [{"role": "assistant", "content": "extra", "timestamp": 1002.5}],
    )
    reader_calls = []
    real_reader = routes.get_state_db_session_messages
    monkeypatch.setattr(
        routes,
        "get_state_db_session_messages",
        lambda session_id, **kwargs: (
            reader_calls.append((session_id, kwargs))
            or real_reader(session_id, **kwargs)
        ),
    )

    first = _session_payload(routes, f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    with sqlite3.connect(tmp_path / "state.db") as conn:
        conn.execute("UPDATE messages SET active = 0 WHERE content = 'extra'")
        conn.commit()
    second = _session_payload(routes, f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    unbounded = _session_payload(routes, f"/api/session?session_id={sid}&messages=1&resolve_model=0")

    assert first["message_count"] == 5
    assert second["message_count"] == unbounded["message_count"] == 4
    assert len(reader_calls) == 3


def test_foreign_summary_cache_tracks_same_high_water_rewrite(monkeypatch, tmp_path):
    import api.routes as routes

    sid = _foreign_fixture(monkeypatch, tmp_path, truncation_boundary=1001.0)
    reader_calls = []
    real_reader = routes.get_state_db_session_messages
    monkeypatch.setattr(
        routes,
        "get_state_db_session_messages",
        lambda session_id, **kwargs: (
            reader_calls.append((session_id, kwargs))
            or real_reader(session_id, **kwargs)
        ),
    )

    _session_payload(routes, f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    with sqlite3.connect(tmp_path / "state.db") as conn:
        conn.execute("UPDATE messages SET content = 'rewritten' WHERE content = 'done'")
        conn.commit()
    _session_payload(routes, f"/api/session?session_id={sid}&messages=0&resolve_model=0")

    assert len(reader_calls) == 2


def test_foreign_metadata_poll_respects_truncation_watermark(monkeypatch, tmp_path):
    import api.routes as routes

    sid = _foreign_fixture(monkeypatch, tmp_path, truncation_watermark=1001.5)
    metadata = _session_payload(routes, f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    unbounded = _session_payload(routes, f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    assert metadata["message_count"] == unbounded["message_count"]
    assert metadata["last_message_at"] == unbounded["last_message_at"]


def test_webui_metadata_control_stays_on_cheap_summary(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_metadata_control_7050"
    sidecar = [{"role": "user", "content": "hello", "timestamp": 1000.0}]
    _install_test_session(monkeypatch, tmp_path, sid, sidecar)
    _make_state_db(tmp_path / "state.db", sid, sidecar + [{"role": "assistant", "content": "new", "timestamp": 1001.0}])
    monkeypatch.setattr(routes, "_foreign_display_coordinate_summary", lambda *args, **kwargs: pytest.fail("WebUI control used foreign summary"))
    payload = _session_payload(routes, f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    assert payload["message_count"] == 1


def test_messaging_metadata_control_stays_on_messaging_path(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "messaging_metadata_control_7050"
    sidecar = [{"role": "user", "content": "hello", "timestamp": 1000.0}]
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar)
    session.session_source = "messaging"
    session.source_tag = "slack"
    session.raw_source = "slack"
    session.save(touch_updated_at=False)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {"source_tag": "slack", "session_source": "messaging"})
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda _sid: sidecar)
    monkeypatch.setattr(routes, "_foreign_display_coordinate_summary", lambda *args, **kwargs: pytest.fail("messaging path used foreign summary"))
    payload = _session_payload(routes, f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    assert payload["message_count"] == 1
