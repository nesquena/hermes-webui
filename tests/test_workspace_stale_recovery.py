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
    assert session.workspace == str(stale)
    assert str(fallback.resolve()) in sidecar.read_text(encoding="utf-8")


def test_chat_start_adopts_reloaded_owner_after_implicit_workspace_recovery(
    monkeypatch, tmp_path
):
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    stale_workspace = tmp_path / "deleted-workspace"
    sid = "chat-recovery-owner"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "_write_session_index", lambda **_kwargs: None)
    seed = models.Session(
        session_id=sid,
        workspace=str(stale_workspace),
        model="test-model",
        model_provider="test-provider",
        profile="default",
        messages=[{"role": "user", "content": "preserve me"}],
    )
    seed.save(skip_index=True)
    stale_alias = models.Session.load(sid)
    concurrent_owner = models.Session.load(sid)
    assert stale_alias is not None and concurrent_owner is not None
    concurrent_owner.title = "concurrent mutation"
    concurrent_owner.save(skip_index=True)
    with models.LOCK:
        models.SESSIONS[sid] = stale_alias

    captured = {}

    def start_run(session, **kwargs):
        captured["session"] = session
        session.workspace = kwargs["workspace"]
        session.save(skip_index=True)
        return {"stream_id": "recovered-owner-stream"}

    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)
    monkeypatch.setattr(workspace, "load_workspaces", lambda: [])
    monkeypatch.setattr(routes, "get_last_workspace", lambda: str(fallback))
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda *_a, **_k: stale_alias)
    monkeypatch.setattr(
        routes,
        "_read_profile_model_config",
        lambda *_a, **_k: (None, None, {}),
    )
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_a, **_k: ("test-model", "test-provider", "test-model"),
    )
    monkeypatch.setattr(
        routes,
        "_repair_foreign_session_model_provider",
        lambda _session, **_kwargs: "test-provider",
    )
    monkeypatch.setattr(routes, "_start_run", start_run)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200: payload)

    response = routes._handle_chat_start(
        None,
        {"session_id": sid, "message": "continue"},
    )

    assert response["stream_id"] == "recovered-owner-stream"
    assert captured["session"] is not stale_alias
    assert captured["session"].workspace == str(fallback.resolve())


def _patch_chat_start_after_workspace_adoption(
    monkeypatch,
    tmp_path,
    stale_alias,
    recovered_owner,
    start_run,
):
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(
        routes,
        "_get_or_materialize_session",
        lambda *_args, **_kwargs: stale_alias,
    )
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        routes,
        "_resolve_chat_workspace_with_recovery",
        lambda *_args, **_kwargs: routes._ResolvedChatWorkspace(
            tmp_path,
            recovered_owner,
        ),
    )
    monkeypatch.setattr(
        routes,
        "_read_profile_model_config",
        lambda *_args, **_kwargs: (None, None, {}),
    )
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", "test-provider", "test-model"),
    )
    monkeypatch.setattr(
        routes,
        "_repair_foreign_session_model_provider",
        lambda _session, **_kwargs: "test-provider",
    )
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _config: False)
    monkeypatch.setattr(routes, "_start_run", start_run)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: {
            "error": message,
            "_status": status,
        },
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200: {**payload, "_status": status},
    )


def test_chat_start_rechecks_profile_after_workspace_owner_adoption(
    monkeypatch, tmp_path
):
    stale_alias = SimpleNamespace(
        session_id="profile-owner-race",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        profile="default",
        messages=[{"role": "user", "content": "default profile"}],
        context_messages=[],
        pending_user_message=None,
        compression_recovery={},
        recommended_recovery_action=None,
    )
    recovered_owner = SimpleNamespace(
        session_id=stale_alias.session_id,
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        profile="other",
        messages=[{"role": "user", "content": "other profile"}],
        context_messages=[],
        pending_user_message=None,
        compression_recovery={},
        recommended_recovery_action=None,
    )
    started = {"value": False}

    def start_run(_session, **_kwargs):
        started["value"] = True
        return {"stream_id": "must-not-start"}

    _patch_chat_start_after_workspace_adoption(
        monkeypatch,
        tmp_path,
        stale_alias,
        recovered_owner,
        start_run,
    )

    response = routes._handle_chat_start(
        object(),
        {"session_id": stale_alias.session_id, "message": "continue safely"},
    )

    assert response["_status"] == 404
    assert response["error"] == "Session not found"
    assert started["value"] is False


def test_chat_start_recomputes_recovery_after_workspace_owner_adoption(
    monkeypatch, tmp_path
):
    stale_recovery = {
        "terminal_state": "compression_exhausted",
        "recommended_action": "start_focused_continuation",
    }
    stale_alias = SimpleNamespace(
        session_id="recovery-owner-race",
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        profile="default",
        messages=[{"role": "user", "content": "stale recovery"}],
        context_messages=[],
        pending_user_message=None,
        compression_recovery=stale_recovery,
        recommended_recovery_action="start_focused_continuation",
    )
    saves = {"count": 0}
    recovered_owner = SimpleNamespace(
        session_id=stale_alias.session_id,
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        profile="default",
        messages=[{"role": "user", "content": "recovery already cleared"}],
        context_messages=[],
        pending_user_message=None,
        compression_recovery={},
        recommended_recovery_action=None,
        save=lambda: saves.__setitem__("count", saves["count"] + 1),
    )

    _patch_chat_start_after_workspace_adoption(
        monkeypatch,
        tmp_path,
        stale_alias,
        recovered_owner,
        lambda _session, **_kwargs: {"error": "busy", "_status": 409},
    )
    adoptions = {"count": 0}

    def resolve_workspace(_session, _requested):
        adoptions["count"] += 1
        return routes._ResolvedChatWorkspace(tmp_path, recovered_owner)

    monkeypatch.setattr(
        routes,
        "_resolve_chat_workspace_with_recovery",
        resolve_workspace,
    )

    response = routes._handle_chat_start(
        object(),
        {
            "session_id": stale_alias.session_id,
            "message": "continue",
        },
    )

    assert response["_status"] == 409
    assert response["error"] == "busy"
    assert adoptions["count"] == 1
    assert recovered_owner.compression_recovery == {}
    assert recovered_owner.recommended_recovery_action is None
    assert saves["count"] == 0


def test_server_turn_adopts_recovered_owner_before_model_resolution(
    monkeypatch, tmp_path
):
    stale_alias = SimpleNamespace(
        session_id="server-turn-owner",
        model="stale-model",
        model_provider="stale-provider",
        profile="default",
    )
    recovered_owner = SimpleNamespace(
        session_id=stale_alias.session_id,
        model="owner-model",
        model_provider="owner-provider",
        profile="default",
    )
    sessions = iter((stale_alias, recovered_owner))
    captured = {}

    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_k: None)
    monkeypatch.setattr(routes, "get_session", lambda _sid: next(sessions))
    monkeypatch.setattr(
        routes,
        "_resolve_chat_workspace_with_recovery",
        lambda _session, _requested: routes._ResolvedChatWorkspace(
            tmp_path,
            recovered_owner,
        ),
    )

    def read_profile(session, provider):
        captured["profile_session"] = session
        captured["profile_provider"] = provider
        return None, None, {}

    monkeypatch.setattr(routes, "_read_profile_model_config", read_profile)
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **_kwargs: (model, provider, model),
    )
    monkeypatch.setattr(
        routes,
        "clear_process_wakeup_pause_if_model_changed",
        lambda *_a, **_k: False,
    )

    def start_run(session, **kwargs):
        captured["run_session"] = session
        captured["run_model"] = kwargs["model"]
        captured["run_workspace"] = kwargs["workspace"]
        return {"_status": 200}

    monkeypatch.setattr(routes, "_start_run", start_run)

    response = routes.start_session_turn(
        stale_alias.session_id,
        "wake up",
        source="manual",
    )

    assert response["_status"] == 200
    assert captured["profile_session"] is recovered_owner
    assert captured["profile_provider"] == "owner-provider"
    assert captured["run_session"] is recovered_owner
    assert captured["run_model"] == "owner-model"
    assert captured["run_workspace"] == str(tmp_path)


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


def _post_new_session_with_workspace(
    monkeypatch,
    body,
    previous_session,
    fallback,
    *,
    previous_visible=True,
):
    import api.session_lifecycle as session_lifecycle

    captured = {"new": [], "fallback": 0}

    class _NewSession:
        session_id = "new-session"
        profile = "default"
        messages = []

        def __init__(self, workspace):
            self.workspace = workspace

        def compact(self):
            return {
                "session_id": self.session_id,
                "profile": self.profile,
                "workspace": self.workspace,
            }

    def get_fallback():
        captured["fallback"] += 1
        return str(fallback)

    def create_session(**kwargs):
        captured["new"].append(kwargs)
        return _NewSession(kwargs["workspace"])

    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_csrf_exempt_path", lambda _path: False)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
    monkeypatch.setattr(
        routes,
        "_session_id_visible_to_request_profile",
        lambda *_a, **_k: previous_visible,
    )
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: previous_session
        if sid == body.get("prev_session_id")
        else (_ for _ in ()).throw(KeyError(sid)),
    )
    monkeypatch.setattr(routes, "get_last_workspace", get_fallback)
    monkeypatch.setattr(routes, "_session_model_state_from_request", lambda *_a: (None, None))
    monkeypatch.setattr(routes, "new_session", create_session)
    monkeypatch.setattr(
        session_lifecycle,
        "_register_background_commit_thread",
        lambda _thread: False,
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: captured.setdefault(
            "response", (status, payload)
        ),
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400, **_kwargs: captured.setdefault(
            "error", (status, message)
        ),
    )

    handler = SimpleNamespace(command="POST", headers={})
    routes.handle_post(handler, urlparse("/api/session/new"))
    return captured


def test_session_new_recovers_deleted_workspace_inherited_from_visible_previous_session(
    monkeypatch, tmp_path
):
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    stale = tmp_path / "deleted-workspace"
    previous = SimpleNamespace(
        session_id="previous-session",
        profile="default",
        workspace=str(stale),
    )
    body = {
        "workspace": str(stale),
        "workspace_inherited_from_prev_session": True,
        "prev_session_id": previous.session_id,
        "profile": "default",
        "worktree": False,
    }

    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)
    monkeypatch.setattr(workspace, "load_workspaces", lambda: [])
    captured = _post_new_session_with_workspace(
        monkeypatch,
        body,
        previous,
        fallback,
    )

    assert "error" not in captured
    assert captured["response"][0] == 200
    assert captured["response"][1]["session"]["workspace"] == str(fallback.resolve())
    assert captured["new"][0]["workspace"] == str(fallback.resolve())
    assert captured["fallback"] == 1


@pytest.mark.parametrize(
    ("mark_inherited", "previous_visible", "stored_workspace"),
    [
        pytest.param(False, True, "requested", id="explicit-request"),
        pytest.param(True, False, "requested", id="foreign-previous-session"),
        pytest.param(True, True, "different", id="stored-workspace-mismatch"),
    ],
)
def test_session_new_keeps_deleted_unverified_workspace_strict(
    monkeypatch,
    tmp_path,
    mark_inherited,
    previous_visible,
    stored_workspace,
):
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    stale = tmp_path / "deleted-workspace"
    previous = SimpleNamespace(
        session_id="previous-session",
        profile="default",
        workspace=(str(stale) if stored_workspace == "requested" else str(tmp_path / "other")),
    )
    body = {
        "workspace": str(stale),
        "prev_session_id": previous.session_id,
        "profile": "default",
        "worktree": False,
    }
    if mark_inherited:
        body["workspace_inherited_from_prev_session"] = True

    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)
    monkeypatch.setattr(workspace, "load_workspaces", lambda: [])
    captured = _post_new_session_with_workspace(
        monkeypatch,
        body,
        previous,
        fallback,
        previous_visible=previous_visible,
    )

    assert captured["error"][0] == 400
    assert "does not exist" in captured["error"][1].lower()
    assert captured["new"] == []
    assert captured["fallback"] == 0


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
    assert session.workspace == str(stale)
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
    assert session.workspace == str(stale)
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
