"""Route contracts for the global session sidebar index endpoints."""

import json
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from tests._pytest_port import BASE, TEST_STATE_DIR


ROOT = pathlib.Path(__file__).resolve().parents[1]
ROUTES = ROOT / "api" / "routes.py"


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def _session_dir():
    path = TEST_STATE_DIR / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _drop_session_index():
    index_path = _session_dir() / "_index.json"
    try:
        index_path.unlink()
    except FileNotFoundError:
        pass


def _write_session_file(
    *,
    sid=None,
    title="Sidebar route test",
    workspace_group="chats",
    profile="default",
    age_days=0,
    archived=False,
):
    sid = sid or f"sidebar_{uuid.uuid4().hex[:10]}"
    now = time.time()
    activity_at = now - (age_days * 86_400)
    session = {
        "session_id": sid,
        "title": title,
        "workspace": str(TEST_STATE_DIR / "workspace"),
        "workspace_group": workspace_group,
        "model": "openai/gpt-5.4-mini",
        "model_provider": "openai",
        "created_at": activity_at,
        "updated_at": activity_at,
        "pinned": False,
        "archived": archived,
        "project_id": None,
        "profile": profile,
        "messages": [
            {
                "role": "user",
                "content": title,
                "timestamp": activity_at,
            }
        ],
        "tool_calls": [],
    }
    path = _session_dir() / f"{sid}.json"
    path.write_text(json.dumps(session), encoding="utf-8")
    _drop_session_index()
    return sid, path


def _delete_session_file(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    _drop_session_index()


def test_source_contracts_for_sidebar_index_routes():
    src = ROUTES.read_text(encoding="utf-8")

    assert 'parsed.path == "/api/session-index"' in src
    assert 'parsed.path == "/api/session-index/archive"' in src
    assert 'parsed.path == "/api/sessions"' in src
    assert "build_session_sidebar_index" in src
    assert "build_session_archive_page" in src
    assert "profile_aware_dedupe=True" in src


def test_messaging_dedupe_profile_aware_contract(monkeypatch):
    import api.routes as routes

    src = ROUTES.read_text(encoding="utf-8")
    assert "profile_aware: bool = False" in src
    assert 'dedupe_key = f"{profile_key}\\x1f{key}" if profile_aware else key' in src
    assert "profile_key = _safe_first(session.get(\"profile\"))" in src

    monkeypatch.setattr(routes, "_load_gateway_session_identity_map", lambda: {})
    rows = [
        {
            "session_id": "old_default",
            "profile": "default",
            "raw_source": "telegram",
            "user_id": "same-user",
            "updated_at": 10,
        },
        {
            "session_id": "new_haku",
            "profile": "haku",
            "raw_source": "telegram",
            "user_id": "same-user",
            "updated_at": 20,
        },
    ]

    profile_blind = routes._keep_latest_messaging_session_per_source(rows)
    assert [row["session_id"] for row in profile_blind] == ["new_haku"]

    profile_aware = routes._keep_latest_messaging_session_per_source(
        rows,
        profile_aware=True,
    )
    assert {row["session_id"] for row in profile_aware} == {"old_default", "new_haku"}


def test_profile_aware_dedupe_does_not_hide_stale_rows_for_other_profiles(monkeypatch):
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "_load_gateway_session_identity_map",
        lambda: {
            "active_default": {
                "raw_source": "telegram",
            }
        },
    )
    rows = [
        {
            "session_id": "active_default",
            "profile": "default",
            "raw_source": "telegram",
            "user_id": "default-user",
            "updated_at": 30,
        },
        {
            "session_id": "stale_other",
            "profile": "other",
            "raw_source": "telegram",
            "user_id": "other-user",
            "updated_at": 20,
            "end_reason": "session_reset",
        },
    ]

    profile_blind = routes._keep_latest_messaging_session_per_source(rows)
    assert [row["session_id"] for row in profile_blind] == ["active_default"]

    profile_aware = routes._keep_latest_messaging_session_per_source(
        rows,
        profile_aware=True,
    )
    assert {row["session_id"] for row in profile_aware} == {"active_default", "stale_other"}


def test_session_new_accepts_chats_workspace_group_with_runtime_workspace():
    data, status = post("/api/session/new", {"workspace_group": "chats"})
    assert status == 200
    sid = data["session"]["session_id"]
    try:
        session = data["session"]
        assert session["workspace_group"] == "chats"
        assert session["workspace"]
    finally:
        post("/api/session/delete", {"session_id": sid})


def test_session_new_without_workspace_group_preserves_workspace_default():
    data, status = post("/api/session/new", {})
    assert status == 200
    sid = data["session"]["session_id"]
    try:
        session = data["session"]
        assert session["workspace_group"] == "workspace"
        assert session["workspace"]
    finally:
        post("/api/session/delete", {"session_id": sid})


def test_session_index_returns_grouped_payload_without_messages():
    sid, path = _write_session_file(title="Visible chats row", workspace_group="chats")
    try:
        data, status = get(f"/api/session-index?current_session_id={urllib.parse.quote(sid)}")
        assert status == 200, data
        assert isinstance(data.get("groups"), list)
        assert "manual_archived" in data
        assert "archive_after_days" in data
        assert "server_time" in data
        assert isinstance(data.get("projects"), list)

        rows = [
            row
            for group in data["groups"]
            for row in group.get("sessions", [])
            if row.get("session_id") == sid or row.get("id") == sid
        ]
        assert rows, data
        row = rows[0]
        assert "messages" not in row
        assert row.get("profile") == "default"
        assert row.get("session_id") == sid
        assert row.get("id") == sid
    finally:
        _delete_session_file(path)


def test_session_index_archive_requires_group_id():
    data, status = get("/api/session-index/archive")
    assert status == 400
    assert data.get("error") == "group_id is required"


def test_session_index_archive_returns_aged_rows_without_messages():
    sid, path = _write_session_file(
        title="Old chats row",
        workspace_group="chats",
        age_days=10,
    )
    try:
        data, status = get(
            "/api/session-index/archive?group_id=chats&limit=5"
        )
        assert status == 200, data
        assert data.get("group_id") == "chats"
        rows = [row for row in data.get("sessions", []) if row.get("session_id") == sid]
        assert rows, data
        assert "messages" not in rows[0]
        assert rows[0].get("profile") == "default"
    finally:
        _delete_session_file(path)
