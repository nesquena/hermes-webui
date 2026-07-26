"""Runtime regression coverage for dedicated composer draft sidecars."""

import sqlite3
from types import SimpleNamespace

import pytest


def _build_draft_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BASE_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_WEBUI_DEFAULT_WORKSPACE", str(tmp_path / "workspace"))

    import api.models as models
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    return models, routes, session_dir


def _post_json(monkeypatch, routes, path, body):
    captured = {}
    handler = SimpleNamespace(
        command="POST",
        wfile=None,
        send_response=lambda status: None,
        send_header=lambda key, value: None,
        end_headers=lambda: None,
    )

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )

    parsed = SimpleNamespace(path=path)
    assert routes.handle_post(handler, parsed) is True
    return captured["status"], captured["payload"]


def _cleanup_route(monkeypatch, routes, *, zero_only=False):
    captured = {}
    handler = SimpleNamespace(
        wfile=None,
        send_response=lambda status: None,
        send_header=lambda key, value: None,
        end_headers=lambda: None,
    )

    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )

    assert routes._handle_sessions_cleanup(handler, {}, zero_only=zero_only) is True
    return captured["payload"]


def _write_cli_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                title TEXT,
                parent_session_id TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT
            );
            INSERT INTO sessions (id, source, title, parent_session_id)
            VALUES ('sid-model-delete', 'cli', 'model deleted', NULL);
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_dedicated_draft_overlays_full_and_metadata_loads(tmp_path, monkeypatch):
    models, _, session_dir = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-overlay"
    models.Session(
        session_id=sid,
        title="Session",
        composer_draft={"text": "legacy", "files": []},
    ).save(touch_updated_at=False, skip_index=True)
    models._save_session_draft(sid, {"text": "dedicated", "files": []})

    loaded = models.Session.load(sid)
    loaded_meta = models.Session.load_metadata_only(sid)
    assert loaded.composer_draft == {"text": "dedicated", "files": []}
    assert loaded_meta.composer_draft == {"text": "dedicated", "files": []}

    models._delete_session_draft(sid)
    fallback = models.Session.load(sid)
    fallback_meta = models.Session.load_metadata_only(sid)
    assert fallback.composer_draft == {"text": "legacy", "files": []}
    assert fallback_meta.composer_draft == {"text": "legacy", "files": []}
    assert (session_dir / ".drafts" / f"{sid}.json").exists() is False


def test_corrupted_draft_file_falls_back_to_legacy_with_warning(tmp_path, monkeypatch, caplog):
    models, _, _ = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-corrupt"
    models.Session(
        session_id=sid,
        title="Session",
        composer_draft={"text": "from-sidecar"},
    ).save(touch_updated_at=False, skip_index=True)

    draft_dir = models._session_draft_dir()
    draft_dir.mkdir(exist_ok=True)
    (draft_dir / f"{sid}.json").write_text("{malformed", encoding="utf-8")

    with caplog.at_level("WARNING"):
        loaded = models.Session.load(sid)

    assert loaded.composer_draft == {"text": "from-sidecar"}
    assert any("Ignoring malformed draft sidecar" in record.getMessage() for record in caplog.records)


def test_drafts_directory_is_ignored_by_session_file_scanners(tmp_path, monkeypatch):
    models, _, session_dir = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-scan"
    models.Session(session_id=sid, title="Session").save(touch_updated_at=False, skip_index=True)
    models._save_session_draft(sid, {"text": "scan draft", "files": []})

    top_level = {p.name for p in session_dir.glob("*.json")}
    session_ids = models._persisted_session_ids_snapshot()

    assert sid in session_ids
    assert f"{sid}.json" in top_level
    assert all(p.parent == session_dir for p in session_dir.glob("*.json"))


def test_draft_post_uses_dedicated_sidecar_without_main_sidecar_rewrite(tmp_path, monkeypatch):
    models, routes, session_dir = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-large"
    models.Session(
        session_id=sid,
        title="Large",
        messages=[{"role": "assistant", "content": "x" * 300_000}],
    ).save(skip_index=True)
    main_sidecar = session_dir / f"{sid}.json"
    before_payload = main_sidecar.read_bytes()
    before_mtime_ns = main_sidecar.stat().st_mtime_ns

    save_calls = []
    real_save = models.Session.save

    def save_spy(*args, **kwargs):
        save_calls.append((args, kwargs))
        return real_save(*args, **kwargs)

    monkeypatch.setattr(models.Session, "save", save_spy)

    status, payload = _post_json(
        monkeypatch,
        routes,
        "/api/session/draft",
        {"session_id": sid, "text": "drafted payload", "files": []},
    )

    assert status == 200
    assert payload["draft"] == {"text": "drafted payload", "files": []}
    assert not save_calls
    assert (session_dir / ".drafts" / f"{sid}.json").exists()
    assert main_sidecar.stat().st_mtime_ns == before_mtime_ns
    assert main_sidecar.read_bytes() == before_payload


def test_draft_explicit_empty_stays_authoritative_after_reload(tmp_path, monkeypatch):
    models, routes, _ = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-empty"
    models.Session(session_id=sid, title="Session").save(
        touch_updated_at=False,
        skip_index=True,
    )

    status, payload = _post_json(
        monkeypatch,
        routes,
        "/api/session/draft",
        {"session_id": sid, "text": "", "files": []},
    )
    assert status == 200
    assert payload["draft"] == {"text": "", "files": []}

    loaded = models.Session.load(sid)
    loaded.title = "touch"
    loaded.save(touch_updated_at=False, skip_index=True)
    assert models.Session.load(sid).composer_draft == {"text": "", "files": []}


def test_session_delete_removes_dedicated_draft_file(tmp_path, monkeypatch):
    import api.routes as routes
    models, _, session_dir = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-delete"
    models.Session(session_id=sid, title="Session").save(
        touch_updated_at=False,
        skip_index=True,
    )
    routes._save_session_draft(sid, {"text": "in-memory draft", "files": []})
    assert (session_dir / ".drafts" / f"{sid}.json").exists()

    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: False)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda value: False)

    _, payload = _post_json(
        monkeypatch,
        routes,
        "/api/session/delete",
        {"session_id": sid},
    )

    assert payload["ok"] is True
    assert not (session_dir / f"{sid}.json").exists()
    assert not (session_dir / ".drafts" / f"{sid}.json").exists()


def test_sessions_cleanup_phase1_removes_dedicated_draft_file(tmp_path, monkeypatch):
    models, routes, session_dir = _build_draft_env(tmp_path, monkeypatch)

    sid = "sid-orphan"
    models.Session(session_id=sid, title="Untitled").save(
        touch_updated_at=False,
        skip_index=True,
    )
    routes._save_session_draft(sid, {"text": "draft", "files": []})

    result = _cleanup_route(monkeypatch, routes)

    assert result["cleaned"] == 1
    assert not (session_dir / f"{sid}.json").exists()
    assert not (session_dir / ".drafts" / f"{sid}.json").exists()


def test_model_cleanup_removes_draft_artifact(tmp_path, monkeypatch):
    import api.profiles as profiles

    models, routes, session_dir = _build_draft_env(tmp_path, monkeypatch)
    sid = "sid-model-delete"

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(tmp_path))

    _write_cli_db(tmp_path / "state.db")

    (session_dir / f"{sid}.json").write_text('{"session_id": "sid-model-delete"}', encoding="utf-8")
    draft_path = session_dir / ".drafts" / f"{sid}.json"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text('{"text": "x", "files": []}', encoding="utf-8")

    assert models.delete_cli_session(sid) is True
    assert not (session_dir / f"{sid}.json").exists()
    assert not draft_path.exists()


def test_draft_path_error_shows_original_input(tmp_path, monkeypatch):
    models, _, _ = _build_draft_env(tmp_path, monkeypatch)

    with pytest.raises(ValueError) as exc:
        models._session_draft_path("../unsafe")
    assert "Unsafe session_id '../unsafe'" in str(exc.value)
