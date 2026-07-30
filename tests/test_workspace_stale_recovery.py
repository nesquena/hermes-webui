from collections import OrderedDict
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from api import config as api_config
from api import models, routes, workspace


def _write_session_sidecar(monkeypatch, tmp_path, session):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "_write_session_index", lambda **_kwargs: None)
    sidecar = session_dir / f"{session.session_id}.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": session.session_id,
                "workspace": session.workspace,
                "messages": [{"role": "user", "content": "preserve me"}],
            }
        ),
        encoding="utf-8",
    )
    return sidecar


def test_profile_default_workspace_uses_live_config_default(monkeypatch, tmp_path):
    live_default = tmp_path / "live-default"
    live_default.mkdir()

    monkeypatch.setattr(api_config, "DEFAULT_WORKSPACE", live_default)
    monkeypatch.setattr(api_config, "get_config", lambda: {})

    assert workspace._profile_default_workspace() == str(live_default.resolve())


def test_implicit_workspace_recovery_keeps_fallback_lazy(monkeypatch, tmp_path):
    valid = tmp_path / "valid"
    valid.mkdir()
    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)

    resolved, recovered = workspace.resolve_implicit_workspace_with_recovery(
        valid,
        lambda: (_ for _ in ()).throw(AssertionError("fallback should stay lazy")),
    )

    assert resolved == valid.resolve()
    assert recovered is False


def test_resolve_chat_workspace_with_recovery_repairs_missing_implicit_workspace(monkeypatch, tmp_path):
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    stale = tmp_path / "deleted-workspace"

    session = SimpleNamespace(session_id="sess-1", workspace=str(stale))
    sidecar = _write_session_sidecar(monkeypatch, tmp_path, session)

    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)
    monkeypatch.setattr(workspace, "load_workspaces", lambda: [])
    monkeypatch.setattr(routes, "get_last_workspace", lambda: str(fallback))

    resolved = routes._resolve_chat_workspace_with_recovery(session, None)

    assert resolved == str(fallback.resolve())
    assert session.workspace == str(fallback.resolve())
    assert str(fallback.resolve()) in sidecar.read_text(encoding="utf-8")


def test_chat_recovery_persistence_failure_fails_closed(monkeypatch, tmp_path):
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    stale = tmp_path / "deleted-workspace"
    session = SimpleNamespace(
        session_id="sess-chat-save-fails",
        workspace=str(stale),
        save=lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)
    monkeypatch.setattr(workspace, "load_workspaces", lambda: [])
    monkeypatch.setattr(routes, "get_last_workspace", lambda: str(fallback))

    with pytest.raises(routes.WorkspaceBindingPersistenceError):
        routes._resolve_chat_workspace_with_recovery(session, None)

    assert session.workspace == str(stale)


def test_resolve_chat_workspace_with_recovery_preserves_explicit_errors(monkeypatch, tmp_path):
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    stale = tmp_path / "deleted-workspace"

    def fake_resolve(value):
        if value == str(stale):
            raise ValueError(f"Path does not exist: {stale}")
        return Path(value).resolve()

    saved = {"count": 0}

    def fake_save():
        saved["count"] += 1

    session = SimpleNamespace(session_id="sess-2", workspace=str(fallback), save=fake_save)

    monkeypatch.setattr(routes, "resolve_trusted_workspace", fake_resolve)
    monkeypatch.setattr(routes, "get_last_workspace", lambda: str(fallback))

    with pytest.raises(ValueError, match="Path does not exist"):
        routes._resolve_chat_workspace_with_recovery(session, str(stale))

    assert session.workspace == str(fallback)
    assert saved["count"] == 0


def test_chat_recovery_preserves_existing_implicit_trust_error(monkeypatch, tmp_path):
    home = tmp_path / "home"
    fallback = home / "fallback"
    fallback.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    saved = {"count": 0}
    session = SimpleNamespace(
        session_id="sess-untrusted",
        workspace=str(outside),
        save=lambda: saved.__setitem__("count", saved["count"] + 1),
    )

    monkeypatch.setattr(workspace, "_home_path", lambda: home)
    monkeypatch.setattr(workspace, "load_workspaces", lambda: [])
    monkeypatch.setattr(workspace, "_BOOT_DEFAULT_WORKSPACE", fallback)
    monkeypatch.setattr(routes, "get_last_workspace", lambda: str(fallback))

    with pytest.raises(ValueError, match="outside the user home directory"):
        routes._resolve_chat_workspace_with_recovery(session, None)

    assert session.workspace == str(outside)
    assert saved["count"] == 0


@pytest.mark.parametrize(
    "terminal_cfg",
    [
        pytest.param(
            {"backend": "ssh", "cwd": "/Users/joeyshiue"},
            id="cwd-absolute",
        ),
        pytest.param({"backend": "ssh"}, id="cwd-omitted"),
        pytest.param({"backend": "ssh", "cwd": ""}, id="cwd-empty"),
        pytest.param({"backend": "ssh", "cwd": "."}, id="cwd-dot"),
    ],
)
def test_chat_recovery_preserves_remote_workspace_rejection(
    monkeypatch, tmp_path, terminal_cfg
):
    candidate = "/Users/other/projects/demo"
    fallback_path = tmp_path / "fallback"
    fallback_path.mkdir()
    fallback_calls = {"count": 0}
    saved = {"count": 0}
    session = SimpleNamespace(
        session_id="sess-remote-untrusted",
        workspace=candidate,
        save=lambda: saved.__setitem__("count", saved["count"] + 1),
    )

    monkeypatch.setattr(
        api_config,
        "get_config",
        lambda: {"terminal": terminal_cfg},
    )
    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)

    def fallback():
        fallback_calls["count"] += 1
        return fallback_path

    monkeypatch.setattr(routes, "get_last_workspace", fallback)

    with pytest.raises(ValueError, match="Path does not exist"):
        routes._resolve_chat_workspace_with_recovery(session, None)

    assert fallback_calls["count"] == 0
    assert session.workspace == candidate
    assert saved["count"] == 0


def test_list_dir_recovers_missing_implicit_session_workspace(monkeypatch, tmp_path):
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    stale = tmp_path / "deleted-workspace"
    session = SimpleNamespace(
        session_id="sess-list",
        workspace=str(stale),
    )
    sidecar = _write_session_sidecar(monkeypatch, tmp_path, session)
    captured = {}

    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)
    monkeypatch.setattr(workspace, "load_workspaces", lambda: [])
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "get_last_workspace", lambda: str(fallback))

    def fake_list_dir(workspace_path, rel_path):
        captured["workspace"] = workspace_path
        captured["rel_path"] = rel_path
        return []

    monkeypatch.setattr(routes, "list_dir", fake_list_dir)
    monkeypatch.setattr(routes, "dir_signature", lambda *_args: "sig")
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)

    payload = routes._handle_list_dir(
        object(), urlparse("/api/list?session_id=sess-list&path=.")
    )

    assert captured == {"workspace": fallback.resolve(), "rel_path": "."}
    assert session.workspace == str(fallback.resolve())
    assert str(fallback.resolve()) in sidecar.read_text(encoding="utf-8")
    assert payload == {
        "entries": [],
        "signature": "sig",
        "path": ".",
        "workspace": str(fallback.resolve()),
        "workspace_recovered": True,
    }


def test_list_recovery_stays_bound_when_global_fallback_changes(
    monkeypatch, tmp_path
):
    """A later mutation must use the root that the Files pane displayed."""
    fallback_a = tmp_path / "fallback-a"
    fallback_b = tmp_path / "fallback-b"
    fallback_a.mkdir()
    fallback_b.mkdir()
    stale = tmp_path / "deleted-workspace"
    selected = {"workspace": str(fallback_a)}
    session = SimpleNamespace(
        session_id="sess-authority",
        workspace=str(stale),
        profile=None,
    )
    sidecar = _write_session_sidecar(monkeypatch, tmp_path, session)
    captured = {}

    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)
    monkeypatch.setattr(workspace, "load_workspaces", lambda: [])
    monkeypatch.setattr(routes, "get_session", lambda _sid, **_kwargs: session)
    monkeypatch.setattr(
        routes, "get_last_workspace", lambda: selected["workspace"]
    )
    def capture_list(workspace_path, _rel):
        captured["listed"] = workspace_path
        return []

    monkeypatch.setattr(routes, "list_dir", capture_list)
    monkeypatch.setattr(routes, "dir_signature", lambda *_args: "sig")
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)

    payload = routes._handle_list_dir(
        object(), urlparse("/api/list?session_id=sess-authority&path=.")
    )
    selected["workspace"] = str(fallback_b)

    assert payload["workspace"] == str(fallback_a.resolve())
    assert session.workspace == str(fallback_a.resolve())
    assert captured["listed"] == fallback_a.resolve()
    assert str(fallback_a.resolve()) in sidecar.read_text(encoding="utf-8")


def test_persisted_list_recovery_anchors_later_create_dir_to_fallback_a(
    monkeypatch, tmp_path
):
    """Reproduce the Maintainer's A→B sequence across fresh sidecar loads."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    fallback_a = tmp_path / "fallback-a"
    fallback_b = tmp_path / "fallback-b"
    fallback_a.mkdir()
    fallback_b.mkdir()
    stale = tmp_path / "deleted-workspace"
    selected = {"workspace": str(fallback_a)}
    sid = "sess-http-authority"

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(models, "_write_session_index", lambda **_kwargs: None)
    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)
    monkeypatch.setattr(workspace, "load_workspaces", lambda: [])
    monkeypatch.setattr(routes, "get_last_workspace", lambda: selected["workspace"])
    monkeypatch.setattr(models, "get_last_workspace", lambda: selected["workspace"])
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: {"error": message, "status": status},
    )

    session = models.Session(
        session_id=sid,
        workspace=str(stale),
        messages=[{"role": "user", "content": "preserve transcript"}],
    )
    session.save(skip_index=True)
    models.SESSIONS.clear()

    listed = routes._handle_list_dir(
        object(), urlparse(f"/api/list?session_id={sid}&path=.")
    )
    persisted = models.Session.load(sid)
    assert listed["workspace"] == str(fallback_a.resolve())
    assert listed["workspace_recovered"] is True
    assert persisted is not None
    assert persisted.workspace == str(fallback_a.resolve())
    assert persisted.messages == [{"role": "user", "content": "preserve transcript"}]

    models.SESSIONS.clear()
    selected["workspace"] = str(fallback_b)
    created = routes._handle_create_dir(
        object(), {"session_id": sid, "path": "gate-wrong-root"}
    )

    assert created == {"ok": True, "path": "gate-wrong-root"}
    assert (fallback_a / "gate-wrong-root").is_dir()
    assert not (fallback_b / "gate-wrong-root").exists()


def test_list_recovery_persistence_failure_fails_closed(monkeypatch, tmp_path):
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    stale = tmp_path / "deleted-workspace"
    calls = {"list_dir": 0}
    session = SimpleNamespace(
        session_id="sess-save-fails",
        workspace=str(stale),
        save=lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)
    monkeypatch.setattr(workspace, "load_workspaces", lambda: [])
    monkeypatch.setattr(routes, "get_session", lambda _sid, **_kwargs: session)
    monkeypatch.setattr(routes, "get_last_workspace", lambda: str(fallback))
    monkeypatch.setattr(
        routes,
        "list_dir",
        lambda *_args: calls.__setitem__("list_dir", calls["list_dir"] + 1),
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: {"error": message, "status": status},
    )

    payload = routes._handle_list_dir(
        object(), urlparse("/api/list?session_id=sess-save-fails&path=.")
    )

    assert payload["status"] >= 400
    assert "persist" in payload["error"].lower()
    assert calls["list_dir"] == 0
    assert session.workspace == str(stale)


def test_list_dir_does_not_recover_unpersistable_cli_workspace(
    monkeypatch, tmp_path
):
    stale = tmp_path / "deleted-cli-workspace"
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    calls = {"fallback": 0, "list_dir": 0}

    monkeypatch.setattr(routes, "get_session", lambda _sid: (_ for _ in ()).throw(KeyError(_sid)))
    monkeypatch.setattr(
        routes,
        "get_cli_sessions",
        lambda: [{"session_id": "cli-stale", "workspace": str(stale)}],
    )

    def get_fallback():
        calls["fallback"] += 1
        return str(fallback)

    monkeypatch.setattr(routes, "get_last_workspace", get_fallback)
    monkeypatch.setattr(
        routes,
        "list_dir",
        lambda *_args: calls.__setitem__("list_dir", calls["list_dir"] + 1),
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: {"error": message, "status": status},
    )

    payload = routes._handle_list_dir(
        object(), urlparse("/api/list?session_id=cli-stale&path=.")
    )

    assert payload["status"] == 404
    assert calls == {"fallback": 0, "list_dir": 0}


@pytest.mark.parametrize(
    "terminal_cfg",
    [
        pytest.param(
            {"backend": "ssh", "cwd": "/Users/joeyshiue"},
            id="cwd-absolute",
        ),
        pytest.param({"backend": "ssh"}, id="cwd-omitted"),
        pytest.param({"backend": "ssh", "cwd": ""}, id="cwd-empty"),
        pytest.param({"backend": "ssh", "cwd": "."}, id="cwd-dot"),
    ],
)
def test_list_dir_preserves_remote_workspace_rejection(
    monkeypatch, tmp_path, terminal_cfg
):
    candidate = "/Users/other/projects/demo"
    fallback_path = tmp_path / "fallback"
    fallback_path.mkdir()
    session = SimpleNamespace(session_id="sess-list-remote", workspace=candidate)
    calls = {"fallback": 0, "list_dir": 0}

    monkeypatch.setattr(
        api_config,
        "get_config",
        lambda: {"terminal": terminal_cfg},
    )
    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)

    def fallback():
        calls["fallback"] += 1
        return fallback_path

    def fake_list_dir(*_args):
        calls["list_dir"] += 1
        return []

    monkeypatch.setattr(routes, "get_last_workspace", fallback)
    monkeypatch.setattr(routes, "list_dir", fake_list_dir)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: {"error": message, "status": status},
    )

    payload = routes._handle_list_dir(
        object(), urlparse("/api/list?session_id=sess-list-remote&path=.")
    )

    assert isinstance(payload, dict)
    assert payload["status"] == 404
    assert "Path does not exist" in payload["error"]
    assert calls == {"fallback": 0, "list_dir": 0}
