"""Regression coverage for WebUI-local organization of imported Matrix rows."""

import json
import io
from types import SimpleNamespace
from collections import OrderedDict
from pathlib import Path

import pytest


def _matrix_meta(session_id="matrix-room-1", profile="default"):
    return {
        "session_id": session_id,
        "title": "Matrix room",
        "model": "external",
        "created_at": 10,
        "updated_at": 20,
        "message_count": 4,
        "profile": profile,
        "source_tag": "matrix",
        "raw_source": "matrix",
        "session_source": "messaging",
        "source_label": "Matrix",
        "read_only": True,
    }


def _isolate_sidecar_store(monkeypatch, tmp_path):
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "session-index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    return models


class _PostHandler:
    def __init__(self):
        self.headers = {}
        self.status = None
        self.response_headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_matrix_source_predicate_is_narrow():
    from api.routes import _is_matrix_session_record

    assert _is_matrix_session_record(_matrix_meta()) is True
    assert _is_matrix_session_record({**_matrix_meta(), "source_tag": "telegram", "raw_source": "telegram"}) is False
    assert _is_matrix_session_record({"source_tag": "matrix", "session_source": "cli"}) is True


def test_matrix_organization_materializes_read_only_metadata_sidecar(monkeypatch, tmp_path):
    import api.routes as routes

    models = _isolate_sidecar_store(monkeypatch, tmp_path)
    db = tmp_path / "state.db"
    db.write_bytes(b"external-agent-state-db-before")
    meta = _matrix_meta()
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: dict(meta))
    monkeypatch.setattr(routes, "get_last_workspace", lambda: tmp_path)

    session = routes._materialize_matrix_organization_metadata(meta["session_id"])

    assert session.read_only is True
    assert session.messages == []
    assert session.profile == "default"
    assert session.source_tag == "matrix"
    assert session.project_id is None
    assert json.loads((models.SESSION_DIR / "matrix-room-1.json").read_text())[
        "read_only"
    ] is True
    assert db.read_bytes() == b"external-agent-state-db-before"


def test_matrix_organization_rejects_unknown_or_non_matrix_rows(monkeypatch, tmp_path):
    import api.routes as routes

    _isolate_sidecar_store(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {})

    with pytest.raises(KeyError):
        routes._materialize_matrix_organization_metadata("missing-matrix")

    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {
        **_matrix_meta("telegram-1"),
        "source_tag": "telegram",
        "raw_source": "telegram",
    })
    with pytest.raises(PermissionError):
        routes._materialize_matrix_organization_metadata("telegram-1")


def test_matrix_sidecar_project_and_archive_metadata_overlay_external_projection(monkeypatch, tmp_path):
    import api.models as models
    import api.routes as routes

    _isolate_sidecar_store(monkeypatch, tmp_path)
    meta = _matrix_meta()
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: dict(meta))
    monkeypatch.setattr(routes, "get_last_workspace", lambda: tmp_path)
    session = routes._materialize_matrix_organization_metadata(meta["session_id"])
    session.project_id = "project-1"
    session.archived = True
    session.save(touch_updated_at=False)

    overlay = models._state_projection_sidecar_metadata(meta["session_id"])

    assert overlay["project_id"] == "project-1"
    assert overlay["archived"] is True


def test_matrix_move_route_writes_only_the_webui_sidecar(monkeypatch, tmp_path):
    import api.routes as routes

    models = _isolate_sidecar_store(monkeypatch, tmp_path)
    db = tmp_path / "state.db"
    db.write_bytes(b"agent-state-db-before-move")
    meta = _matrix_meta()
    project = {"project_id": "project-1", "profile": "default"}
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: dict(meta))
    monkeypatch.setattr(routes, "get_session", lambda _sid, **_kwargs: (_ for _ in ()).throw(KeyError(_sid)))
    monkeypatch.setattr(routes, "get_last_workspace", lambda: tmp_path)
    monkeypatch.setattr(routes, "load_projects", lambda: [project])
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {
        "session_id": "matrix-room-1",
        "project_id": "project-1",
    })

    handler = _PostHandler()
    routes.handle_post(handler, SimpleNamespace(path="/api/session/move", query=""))

    assert handler.status == 200
    assert handler.json_body()["session"]["project_id"] == "project-1"
    assert json.loads((models.SESSION_DIR / "matrix-room-1.json").read_text())["read_only"] is True
    assert db.read_bytes() == b"agent-state-db-before-move"


def test_matrix_archive_route_preserves_read_only_sidecar_contract(monkeypatch, tmp_path):
    import api.routes as routes

    models = _isolate_sidecar_store(monkeypatch, tmp_path)
    meta = _matrix_meta()
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: dict(meta))
    monkeypatch.setattr(routes, "get_last_workspace", lambda: tmp_path)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {
        "session_id": "matrix-room-1",
        "archived": True,
    })

    handler = _PostHandler()
    routes.handle_post(handler, SimpleNamespace(path="/api/session/archive", query=""))

    assert handler.status == 200
    body = handler.json_body()
    assert body["session"]["archived"] is True
    assert body["session"]["read_only"] is True
    assert json.loads((models.SESSION_DIR / "matrix-room-1.json").read_text())["read_only"] is True


def test_matrix_move_rejects_cross_profile_project_before_sidecar_creation(monkeypatch, tmp_path):
    import api.routes as routes

    models = _isolate_sidecar_store(monkeypatch, tmp_path)
    meta = _matrix_meta()
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: dict(meta))
    monkeypatch.setattr(routes, "load_projects", lambda: [{"project_id": "other", "profile": "work"}])
    monkeypatch.setattr(routes, "read_body", lambda _handler: {
        "session_id": "matrix-room-1",
        "project_id": "other",
    })

    handler = _PostHandler()
    routes.handle_post(handler, SimpleNamespace(path="/api/session/move", query=""))

    assert handler.status == 404
    assert not (models.SESSION_DIR / "matrix-room-1.json").exists()


def test_non_matrix_read_only_move_remains_forbidden(monkeypatch, tmp_path):
    import api.routes as routes

    models = _isolate_sidecar_store(monkeypatch, tmp_path)
    meta = {
        **_matrix_meta("telegram-1"),
        "source_tag": "telegram",
        "raw_source": "telegram",
    }
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: dict(meta))
    monkeypatch.setattr(routes, "read_body", lambda _handler: {
        "session_id": "telegram-1",
        "project_id": None,
    })

    handler = _PostHandler()
    routes.handle_post(handler, SimpleNamespace(path="/api/session/move", query=""))

    assert handler.status == 403
    assert not (models.SESSION_DIR / "telegram-1.json").exists()


def test_frontend_exposes_only_matrix_archive_and_move_for_read_only_rows():
    source = (Path(__file__).resolve().parents[1] / "static" / "sessions.js").read_text(encoding="utf-8")

    assert "function _isMatrixSession(session)" in source
    assert "function _isOrganizableReadOnlySession(session)" in source
    assert "if(_isReadOnlySession(session) && !_isOrganizableReadOnlySession(session))" in source
    assert "const canOrganizeReadOnly = _isOrganizableReadOnlySession(session);" in source
