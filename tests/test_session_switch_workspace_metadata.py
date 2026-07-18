from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import api.routes as routes


def _capture(monkeypatch):
    captured = {}

    def fake_j(_handler, data, status=200, **_kwargs):
        captured["data"] = data
        captured["status"] = status
        return data

    def fake_bad(_handler, message, status=400):
        captured["data"] = {"error": message}
        captured["status"] = status
        return captured["data"]

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "bad", fake_bad)
    return captured


def test_list_endpoint_uses_profile_safe_metadata_loader(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = SimpleNamespace(session_id="switch-list", workspace=str(workspace), profile="default")
    calls = []

    def metadata_loader(sid):
        calls.append(sid)
        return session

    monkeypatch.setattr(routes, "get_session_for_file_ops", metadata_loader)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("/api/list must not call the full session loader")
        ),
    )
    monkeypatch.setattr(
        routes,
        "list_dir",
        lambda root, rel: [
            {"name": "notes.txt", "path": "notes.txt", "type": "file", "size": 7}
        ],
    )
    monkeypatch.setattr(routes, "dir_signature", lambda root, rel, entries: "synthetic-signature")
    captured = _capture(monkeypatch)

    routes._handle_list_dir(
        SimpleNamespace(),
        urlparse("/api/list?session_id=switch-list&path=."),
    )

    assert calls == ["switch-list"]
    assert captured == {
        "status": 200,
        "data": {
            "entries": [
                {"name": "notes.txt", "path": "notes.txt", "type": "file", "size": 7}
            ],
            "signature": "synthetic-signature",
            "path": ".",
        },
    }


def test_git_info_endpoint_uses_profile_safe_metadata_loader(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = SimpleNamespace(session_id="switch-git", workspace=str(workspace), profile="default")
    calls = []

    def metadata_loader(sid):
        calls.append(sid)
        return session

    monkeypatch.setattr(routes, "get_session_for_file_ops", metadata_loader)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("/api/git-info must not call the full session loader")
        ),
    )
    captured = _capture(monkeypatch)
    with patch(
        "api.workspace_git.git_status",
        return_value={
            "is_git": True,
            "branch": "fix/synthetic",
            "totals": {"changed": 3, "staged": 1, "unstaged": 1, "untracked": 1},
            "ahead": 2,
            "behind": 0,
        },
    ):
        routes.handle_get(
            SimpleNamespace(_safe_webui_print=lambda _text: None),
            urlparse("/api/git-info?session_id=switch-git"),
        )

    assert calls == ["switch-git"]
    assert captured == {
        "status": 200,
        "data": {
            "git": {
                "branch": "fix/synthetic",
                "dirty": 3,
                "modified": 2,
                "untracked": 1,
                "ahead": 2,
                "behind": 0,
                "is_git": True,
            }
        },
    }


def test_workspace_only_endpoints_keep_foreign_sessions_hidden(monkeypatch):
    monkeypatch.setattr(
        routes,
        "get_session_for_file_ops",
        lambda _sid: (_ for _ in ()).throw(KeyError("foreign")),
    )
    monkeypatch.setattr(routes, "get_cli_sessions", lambda: [])
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("foreign profile must not reach the full loader")
        ),
    )

    list_capture = _capture(monkeypatch)
    routes._handle_list_dir(
        SimpleNamespace(),
        urlparse("/api/list?session_id=foreign&path=."),
    )
    assert list_capture == {"status": 404, "data": {"error": "Session not found"}}

    git_capture = _capture(monkeypatch)
    routes.handle_get(
        SimpleNamespace(_safe_webui_print=lambda _text: None),
        urlparse("/api/git-info?session_id=foreign"),
    )
    assert git_capture == {"status": 404, "data": {"error": "Session not found"}}


def test_session_status_reuses_metadata_without_full_load(monkeypatch):
    session = SimpleNamespace(
        session_id="switch-status",
        title="Synthetic status",
        model="test/model",
        profile="default",
        workspace="/synthetic/workspace",
        personality=None,
        messages=[],
        _metadata_message_count=42,
        created_at=1.0,
        updated_at=2.0,
        active_stream_id=None,
        input_tokens=3,
        output_tokens=4,
        estimated_cost=0.25,
    )
    route_loads = []

    def route_metadata_loader(sid, metadata_only=False):
        route_loads.append((sid, metadata_only))
        assert metadata_only is True
        return session

    monkeypatch.setattr(routes, "get_session", route_metadata_loader)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda _session: False)
    monkeypatch.setattr(
        "api.session_ops.get_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("/api/session/status must not re-enter the full loader")
        ),
    )
    monkeypatch.setattr(
        "api.profiles.get_hermes_home_for_profile",
        lambda _profile: "/synthetic/hermes-home",
    )
    captured = _capture(monkeypatch)

    routes.handle_get(
        SimpleNamespace(_safe_webui_print=lambda _text: None),
        urlparse("/api/session/status?session_id=switch-status"),
    )

    assert route_loads == [("switch-status", True)]
    assert captured["status"] == 200
    assert captured["data"] == {
        "session_id": "switch-status",
        "title": "Synthetic status",
        "model": "test/model",
        "profile": "default",
        "hermes_home": "/synthetic/hermes-home",
        "workspace": "/synthetic/workspace",
        "personality": None,
        "message_count": 42,
        "created_at": 1.0,
        "updated_at": 2.0,
        "agent_running": False,
        "active_stream_id": None,
        "input_tokens": 3,
        "output_tokens": 4,
        "total_tokens": 7,
        "estimated_cost": 0.25,
    }
