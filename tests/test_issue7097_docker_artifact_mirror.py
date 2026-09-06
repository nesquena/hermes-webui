"""Regression coverage for Docker-sandbox artifact browsing (#7097)."""

from types import SimpleNamespace
from urllib.parse import urlencode, urlparse

import pytest

from api import routes


def _capture_json(monkeypatch):
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: (payload, status),
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400, **_kwargs: ({"error": message}, status),
    )


def _configure_docker_mirror(
    monkeypatch,
    profile_home,
    *,
    backend="docker",
    persistent=True,
):
    monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)
    monkeypatch.setattr(routes, "get_active_hermes_home", lambda: profile_home)
    monkeypatch.setattr(
        routes,
        "get_config",
        lambda: {
            "terminal": {
                "backend": backend,
                "container_persistent": persistent,
            }
        },
    )


def _parsed(route_path, **query):
    return urlparse(f"{route_path}?{urlencode(query)}")


@pytest.mark.parametrize("request_path", ["root/subdir", "/root/subdir"])
def test_list_dir_falls_back_to_default_profile_docker_home_mirror(
    tmp_path,
    monkeypatch,
    request_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_home = tmp_path / "hermes"
    mirror_dir = profile_home / "sandboxes" / "docker" / "default" / "home" / "subdir"
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "artifact.py").write_text("print('ok')", encoding="utf-8")
    session = SimpleNamespace(workspace=str(workspace), profile="default")

    _capture_json(monkeypatch)
    _configure_docker_mirror(monkeypatch, profile_home)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda _sid: session)

    payload, status = routes._handle_list_dir(
        object(),
        _parsed("/api/list", session_id="session-1", path=request_path),
    )

    assert status == 200
    assert [entry["name"] for entry in payload["entries"]] == ["artifact.py"]
    assert payload["entries"][0]["path"] == "root/subdir/artifact.py"
    assert payload["path"] == request_path

    opened, opened_status = routes._handle_file_read(
        object(),
        _parsed(
            "/api/file",
            session_id="session-1",
            path=payload["entries"][0]["path"],
        ),
    )
    assert opened_status == 200
    assert opened["content"] == "print('ok')"


def test_file_read_uses_named_profile_docker_workspace_mirror(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_home = tmp_path / "hermes" / "profiles" / "work"
    mirror_dir = profile_home / "sandboxes" / "docker" / "default" / "workspace"
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "report.md").write_bytes(b"# Docker artifact\n")
    session = SimpleNamespace(workspace=str(workspace), profile="work")

    _capture_json(monkeypatch)
    _configure_docker_mirror(monkeypatch, profile_home)
    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda _sid: session)

    payload, status = routes._handle_file_read(
        object(),
        _parsed("/api/file", session_id="session-2", path="workspace/report.md"),
    )

    assert status == 200
    assert payload["content"] == "# Docker artifact\n"
    assert payload["path"] == "report.md"


def test_file_raw_anchors_docker_artifact_to_mirror_root(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_home = tmp_path / "hermes"
    mirror_root = profile_home / "sandboxes" / "docker" / "default" / "home"
    target = mirror_root / "image.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"png")
    session = SimpleNamespace(workspace=str(workspace), profile="default")

    _capture_json(monkeypatch)
    _configure_docker_mirror(monkeypatch, profile_home)
    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda _sid: session)
    monkeypatch.setattr(
        routes,
        "_serve_file_bytes",
        lambda _handler, file_path, *_args, anchor_root=None, **_kwargs: {
            "target": file_path,
            "anchor_root": anchor_root,
        },
    )

    result = routes._handle_file_raw(
        object(),
        _parsed("/api/file/raw", session_id="session-3", path="root/image.png"),
    )

    assert result["target"] == target.resolve()
    assert result["anchor_root"] == mirror_root


def test_workspace_path_keeps_precedence_over_docker_mirror(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    primary_dir = workspace / "root" / "subdir"
    primary_dir.mkdir(parents=True)
    (primary_dir / "primary.txt").write_text("primary", encoding="utf-8")
    profile_home = tmp_path / "hermes"
    mirror_dir = profile_home / "sandboxes" / "docker" / "default" / "home" / "subdir"
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "mirror.txt").write_text("mirror", encoding="utf-8")
    session = SimpleNamespace(workspace=str(workspace), profile="default")

    _capture_json(monkeypatch)
    _configure_docker_mirror(monkeypatch, profile_home)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)

    payload, status = routes._handle_list_dir(
        object(),
        _parsed("/api/list", session_id="session-4", path="root/subdir"),
    )

    assert status == 200
    assert [entry["name"] for entry in payload["entries"]] == ["primary.txt"]


@pytest.mark.parametrize(
    ("backend", "persistent"),
    [("local", True), ("docker", False)],
)
def test_docker_mirror_fallback_requires_persistent_docker_backend(
    tmp_path,
    monkeypatch,
    backend,
    persistent,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_home = tmp_path / "hermes"
    mirror_dir = profile_home / "sandboxes" / "docker" / "default" / "home"
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "secret.txt").write_text("not reachable", encoding="utf-8")
    session = SimpleNamespace(workspace=str(workspace), profile="default")

    _capture_json(monkeypatch)
    _configure_docker_mirror(
        monkeypatch,
        profile_home,
        backend=backend,
        persistent=persistent,
    )
    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda _sid: session)

    payload, status = routes._handle_file_read(
        object(),
        _parsed("/api/file", session_id="session-5", path="root/secret.txt"),
    )

    assert status == 404
    assert "error" in payload


def test_docker_mirror_symlink_escape_stays_blocked(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_home = tmp_path / "hermes"
    mirror_root = profile_home / "sandboxes" / "docker" / "default" / "home"
    mirror_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = mirror_root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    session = SimpleNamespace(workspace=str(workspace), profile="default")

    _capture_json(monkeypatch)
    _configure_docker_mirror(monkeypatch, profile_home)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)

    payload, status = routes._handle_list_dir(
        object(),
        _parsed("/api/list", session_id="session-6", path="root/escape"),
    )

    assert status == 404
    assert "content" not in payload


@pytest.mark.parametrize(
    "terminal_cfg",
    [
        {"backend": "docker"},
        {"env_type": "docker", "container_persistent": "true"},
    ],
)
def test_docker_mirror_accepts_agent_default_and_legacy_backend_key(
    tmp_path,
    monkeypatch,
    terminal_cfg,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_home = tmp_path / "hermes"
    mirror_dir = profile_home / "sandboxes" / "docker" / "default" / "home"
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "artifact.txt").write_bytes(b"artifact")
    session = SimpleNamespace(workspace=str(workspace), profile="default")

    _capture_json(monkeypatch)
    monkeypatch.setattr(routes, "get_active_hermes_home", lambda: profile_home)
    monkeypatch.setattr(routes, "get_config", lambda: {"terminal": terminal_cfg})
    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda _sid: session)

    payload, status = routes._handle_file_read(
        object(),
        _parsed("/api/file", session_id="session-7", path="root/artifact.txt"),
    )

    assert status == 200
    assert payload["content"] == "artifact"


def test_docker_mirror_respects_configured_sandbox_dir(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_home = tmp_path / "hermes"
    sandbox_dir = tmp_path / "custom-sandboxes"
    mirror_dir = sandbox_dir / "docker" / "default" / "home"
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "artifact.txt").write_bytes(b"custom")
    session = SimpleNamespace(workspace=str(workspace), profile="default")

    _capture_json(monkeypatch)
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(tmp_path / "wrong-sandboxes"))
    monkeypatch.setattr(routes, "get_active_hermes_home", lambda: profile_home)
    monkeypatch.setattr(
        routes,
        "get_config",
        lambda: {
            "terminal": {
                "backend": "docker",
                "container_persistent": True,
                "sandbox_dir": str(sandbox_dir),
            }
        },
    )
    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda _sid: session)

    payload, status = routes._handle_file_read(
        object(),
        _parsed("/api/file", session_id="session-8", path="root/artifact.txt"),
    )

    assert status == 200
    assert payload["content"] == "custom"


def test_docker_mirror_respects_environment_sandbox_dir(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_home = tmp_path / "hermes"
    sandbox_dir = tmp_path / "environment-sandboxes"
    mirror_dir = sandbox_dir / "docker" / "default" / "home"
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "artifact.txt").write_bytes(b"environment")
    session = SimpleNamespace(workspace=str(workspace), profile="default")

    _capture_json(monkeypatch)
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(sandbox_dir))
    monkeypatch.setattr(routes, "get_active_hermes_home", lambda: profile_home)
    monkeypatch.setattr(
        routes,
        "get_config",
        lambda: {"terminal": {"backend": "docker", "container_persistent": True}},
    )
    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda _sid: session)

    payload, status = routes._handle_file_read(
        object(),
        _parsed("/api/file", session_id="session-8b", path="root/artifact.txt"),
    )

    assert status == 200
    assert payload["content"] == "environment"


@pytest.mark.parametrize(
    "request_path",
    [
        "root/../workspace/secret.txt",
        "root/subdir\\secret.txt",
        "other/secret.txt",
    ],
)
def test_docker_mirror_rejects_ambiguous_or_traversal_paths(
    tmp_path,
    monkeypatch,
    request_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_home = tmp_path / "hermes"
    mirror_dir = profile_home / "sandboxes" / "docker" / "default" / "workspace"
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "secret.txt").write_bytes(b"secret")
    session = SimpleNamespace(workspace=str(workspace), profile="default")

    _capture_json(monkeypatch)
    _configure_docker_mirror(monkeypatch, profile_home)
    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda _sid: session)

    payload, status = routes._handle_file_read(
        object(),
        _parsed("/api/file", session_id="session-9", path=request_path),
    )

    assert status == 404
    assert "content" not in payload
