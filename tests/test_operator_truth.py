import importlib
import io
import json
import sys
import types
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch


def _chip(payload, chip_id):
    for chip in payload.get("chips", []):
        if chip.get("id") == chip_id:
            return chip
    raise AssertionError(f"missing chip {chip_id!r}: {payload.get('chips')!r}")


def _install_fake_kanban(
    monkeypatch,
    tmp_path,
    *,
    current="hermes-operator",
    existing_boards=None,
    default_workdir=None,
):
    existing = set(existing_boards if existing_boards is not None else {current, "default"})
    hermes_home = tmp_path / "hermes-home"
    kanban_root = hermes_home / "kanban"
    current_file = kanban_root / "current"
    current_file.parent.mkdir(parents=True, exist_ok=True)
    if current is not None:
        current_file.write_text(current + "\n", encoding="utf-8")

    fake = types.ModuleType("hermes_cli.kanban_db")
    calls = []

    def _board_dir(slug):
        return kanban_root / "boards" / (slug or "default")

    def current_board_path():
        return current_file

    def board_exists(slug=None):
        return (slug or "default") in existing

    def board_metadata_path(slug=None):
        return _board_dir(slug) / "board.json"

    def kanban_db_path(slug=None):
        return _board_dir(slug) / "kanban.db"

    def workspaces_root(slug=None):
        return kanban_root / "workspaces" / (slug or "default")

    def read_board_metadata(slug=None):
        board = slug or "default"
        return {
            "slug": board,
            "name": board,
            "default_workdir": str(default_workdir) if default_workdir is not None else str(workspaces_root(board)),
        }

    for board in existing:
        metadata_path = board_metadata_path(board)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text('{"slug":"%s"}' % board, encoding="utf-8")
        kanban_db_path(board).touch()

    def forbidden_write(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("truth endpoint must be read-only")

    setattr(fake, "current_board_path", current_board_path)
    setattr(fake, "board_exists", board_exists)
    setattr(fake, "board_metadata_path", board_metadata_path)
    setattr(fake, "kanban_db_path", kanban_db_path)
    setattr(fake, "workspaces_root", workspaces_root)
    setattr(fake, "read_board_metadata", read_board_metadata)
    setattr(fake, "clear_current_board", forbidden_write)
    setattr(fake, "set_current_board", forbidden_write)
    setattr(fake, "init_db", forbidden_write)
    setattr(fake, "_write_calls", calls)

    fake_pkg = types.ModuleType("hermes_cli")
    setattr(fake_pkg, "kanban_db", fake)
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", fake)
    return fake


def _patch_common_sources(monkeypatch, tmp_path, workspace_path):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.workspace as workspace

    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    state_dir = tmp_path / "webui-state"
    session_dir = state_dir / "sessions"
    state_dir.mkdir(parents=True, exist_ok=True)
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / "abc123.json"
    session_file.write_text("{}", encoding="utf-8")
    workspaces_file = state_dir / "workspaces.json"
    workspaces_file.write_text("[]", encoding="utf-8")
    last_workspace_file = state_dir / "last_workspace.txt"
    last_workspace_file.write_text(str(workspace_path), encoding="utf-8")

    monkeypatch.setattr(config, "STATE_DIR", state_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "DEFAULT_WORKSPACE", workspace_path, raising=False)
    monkeypatch.setattr(workspace, "_workspaces_file", lambda: workspaces_file, raising=False)
    monkeypatch.setattr(workspace, "_last_workspace_file", lambda: last_workspace_file, raising=False)
    monkeypatch.setattr(workspace, "get_last_workspace", lambda: str(workspace_path), raising=False)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default", raising=False)
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: hermes_home, raising=False)
    monkeypatch.setattr(
        models,
        "get_session",
        lambda sid, metadata_only=False: types.SimpleNamespace(
            session_id=sid,
            workspace=str(workspace_path),
            path=session_file,
        ),
        raising=False,
    )
    return state_dir


def test_operator_truth_payload_has_version_timestamp_status_and_chips(monkeypatch, tmp_path):
    truth = importlib.import_module("api.operator_truth")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _patch_common_sources(monkeypatch, tmp_path, workspace)
    _install_fake_kanban(monkeypatch, tmp_path)

    payload = truth.build_operator_truth_payload(session_id="abc123", now=1234.5)

    assert payload["version"] == 1
    assert payload["verified_at"] == 1234.5
    assert payload["status"] == "live"
    assert payload["ttl_seconds"] == 30
    assert payload["summary"] == "Truth live"
    assert {chip["id"] for chip in payload["chips"]} >= {
        "workspace",
        "profile",
        "webui_state",
        "kanban_board",
        "scratch_safety",
        "source_truth_files",
    }
    assert isinstance(payload["sources"], list)
    assert all(chip.get("checked_at") == 1234.5 for chip in payload["chips"])


def test_operator_truth_workspace_uses_session_workspace_when_session_id_present(monkeypatch, tmp_path):
    truth = importlib.import_module("api.operator_truth")
    workspace = tmp_path / "project"
    workspace.mkdir()
    _patch_common_sources(monkeypatch, tmp_path, workspace)
    _install_fake_kanban(monkeypatch, tmp_path)

    payload = truth.build_operator_truth_payload(session_id="abc123", now=1.0)
    workspace_chip = _chip(payload, "workspace")

    assert workspace_chip["state"] == "live"
    assert workspace_chip["source"]["kind"] == "session"
    assert workspace_chip["value"] == "project"
    assert workspace_chip["issues"] == []


def test_operator_truth_workspace_missing_is_stale_not_live(monkeypatch, tmp_path):
    truth = importlib.import_module("api.operator_truth")
    missing_workspace = tmp_path / "missing-project"
    _patch_common_sources(monkeypatch, tmp_path, missing_workspace)
    _install_fake_kanban(monkeypatch, tmp_path)

    payload = truth.build_operator_truth_payload(session_id="abc123", now=1.0)
    workspace_chip = _chip(payload, "workspace")

    assert workspace_chip["state"] == "stale"
    assert payload["status"] == "stale"
    assert any("missing" in issue.lower() or "inaccessible" in issue.lower() for issue in workspace_chip["issues"])


def test_operator_truth_profile_and_state_paths_are_safely_displayed(monkeypatch, tmp_path):
    truth = importlib.import_module("api.operator_truth")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _patch_common_sources(monkeypatch, tmp_path, workspace)
    _install_fake_kanban(monkeypatch, tmp_path)

    payload = truth.build_operator_truth_payload(session_id="abc123", now=1.0)
    profile_chip = _chip(payload, "profile")
    state_chip = _chip(payload, "webui_state")
    serialized = json.dumps(payload)

    assert profile_chip["display_path"]
    assert state_chip["display_path"]
    assert ".env" not in serialized
    assert "auth_token" not in serialized
    assert "password" not in serialized.lower()
    assert "secret" not in serialized.lower()


def test_operator_truth_profile_home_missing_is_unknown_not_live(monkeypatch, tmp_path):
    truth = importlib.import_module("api.operator_truth")
    import api.profiles as profiles

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _patch_common_sources(monkeypatch, tmp_path, workspace)
    _install_fake_kanban(monkeypatch, tmp_path)
    missing_home = tmp_path / "missing-hermes-home"
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: missing_home, raising=False)

    payload = truth.build_operator_truth_payload(session_id="abc123", now=1.0)
    profile_chip = _chip(payload, "profile")
    source_chip = _chip(payload, "source_truth_files")

    assert profile_chip["state"] == "unknown"
    assert payload["status"] == "unknown"
    assert source_chip["state"] == "unknown"
    assert any("missing" in issue.lower() for issue in profile_chip["issues"])


def test_operator_truth_kanban_current_board_read_only_no_repair(monkeypatch, tmp_path):
    truth = importlib.import_module("api.operator_truth")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _patch_common_sources(monkeypatch, tmp_path, workspace)
    fake_kanban = _install_fake_kanban(monkeypatch, tmp_path, current="ghost-board", existing_boards={"default"})

    payload = truth.build_operator_truth_payload(session_id="abc123", now=1.0)
    board_chip = _chip(payload, "kanban_board")

    assert board_chip["state"] == "stale"
    assert board_chip["value"] == "ghost-board"
    assert getattr(fake_kanban, "_write_calls") == []


def test_operator_truth_kanban_env_override_is_named_in_source(monkeypatch, tmp_path):
    truth = importlib.import_module("api.operator_truth")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _patch_common_sources(monkeypatch, tmp_path, workspace)
    _install_fake_kanban(monkeypatch, tmp_path, current="hermes-operator", existing_boards={"default", "env-board"})
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "env-board")

    payload = truth.build_operator_truth_payload(session_id="abc123", now=1.0)
    board_chip = _chip(payload, "kanban_board")

    assert board_chip["state"] == "live"
    assert board_chip["value"] == "env-board"
    assert board_chip["source"]["kind"] == "env:HERMES_KANBAN_BOARD"


def test_operator_truth_board_pointer_drift_is_stale(monkeypatch, tmp_path):
    truth = importlib.import_module("api.operator_truth")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _patch_common_sources(monkeypatch, tmp_path, workspace)
    _install_fake_kanban(monkeypatch, tmp_path, current="deleted-board", existing_boards={"default"})

    payload = truth.build_operator_truth_payload(session_id="abc123", now=1.0)
    board_chip = _chip(payload, "kanban_board")

    assert board_chip["state"] == "stale"
    assert payload["status"] == "stale"
    assert any("missing" in issue.lower() or "does not exist" in issue.lower() for issue in board_chip["issues"])


def test_operator_truth_scratch_default_workdir_under_active_workspace_is_stale(monkeypatch, tmp_path):
    truth = importlib.import_module("api.operator_truth")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _patch_common_sources(monkeypatch, tmp_path, workspace)
    _install_fake_kanban(monkeypatch, tmp_path, default_workdir=workspace)

    payload = truth.build_operator_truth_payload(session_id="abc123", now=1.0)
    scratch_chip = _chip(payload, "scratch_safety")

    assert scratch_chip["state"] == "stale"
    assert scratch_chip["value"] == "risky"
    assert any("active workspace" in issue.lower() for issue in scratch_chip["issues"])


def test_operator_truth_scratch_under_hermes_kanban_storage_is_live(monkeypatch, tmp_path):
    truth = importlib.import_module("api.operator_truth")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _patch_common_sources(monkeypatch, tmp_path, workspace)
    safe_root = tmp_path / "hermes-home" / "kanban" / "workspaces" / "hermes-operator"
    _install_fake_kanban(monkeypatch, tmp_path, default_workdir=safe_root)

    payload = truth.build_operator_truth_payload(session_id="abc123", now=1.0)
    scratch_chip = _chip(payload, "scratch_safety")

    assert scratch_chip["state"] == "live"
    assert scratch_chip["value"] == "safe"
    assert scratch_chip["issues"] == []


def test_operator_truth_source_failure_returns_unknown_chip(monkeypatch, tmp_path):
    truth = importlib.import_module("api.operator_truth")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _patch_common_sources(monkeypatch, tmp_path, workspace)
    real_import_module = importlib.import_module

    def fake_import_module(name, package=None):
        if name == "hermes_cli.kanban_db":
            raise ImportError("kanban unavailable")
        return real_import_module(name, package=package)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)

    payload = truth.build_operator_truth_payload(session_id="abc123", now=1.0)
    board_chip = _chip(payload, "kanban_board")
    scratch_chip = _chip(payload, "scratch_safety")

    assert payload["status"] == "unknown"
    assert board_chip["state"] == "unknown"
    assert scratch_chip["state"] == "unknown"
    assert any("kanban unavailable" in issue for issue in board_chip["issues"])


def test_operator_truth_route_returns_json(monkeypatch):
    import api.routes as routes

    expected = {"version": 1, "status": "unknown", "chips": [], "sources": []}
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload
        return True

    with patch("api.operator_truth.build_operator_truth_payload", return_value=expected) as build_payload, patch(
        "api.routes.j", side_effect=fake_j
    ):
        handled = routes.handle_get(types.SimpleNamespace(wfile=io.BytesIO()), urlparse("/api/operator/truth?session_id=abc123&ui_board=ui-board"))

    assert handled is True
    assert captured["status"] == 200
    assert captured["payload"] == expected
    build_payload.assert_called_once_with(session_id="abc123", ui_board_hint="ui-board")
