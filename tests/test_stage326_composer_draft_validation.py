"""Stage-326 hardening tests for #1956 composer-draft input validation.

Opus advisor flagged that POST /api/session/draft accepted text/files of
arbitrary size and type. A misbehaving or malicious client could persist
multi-MB strings into the session JSON on every keystroke via the 400ms
debounced auto-save. The hardening:

- text: must be str; clamped to 50 KB
- files: must be list; clamped to 50 entries
"""
import json
from types import SimpleNamespace


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
    return models, routes


def _draft_post(monkeypatch, routes, body):
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

    assert routes.handle_post(handler, SimpleNamespace(path="/api/session/draft")) is True
    return captured


def test_draft_text_clamped_to_50kb(monkeypatch, tmp_path):
    models, routes = _build_draft_env(tmp_path, monkeypatch)

    sid = "draft-save-text-clamp"
    models.Session(
        session_id=sid,
        title="Draft validation",
        messages=[{"role": "assistant", "content": "x" * 10}],
    ).save(touch_updated_at=False, skip_index=True)

    captured = _draft_post(
        monkeypatch,
        routes,
        {
            "session_id": sid,
            "text": "x" * 60_000,
            "files": [],
        },
    )

    draft_path = models.SESSION_DIR / ".drafts" / f"{sid}.json"
    draft_payload = json.loads(draft_path.read_text(encoding="utf-8"))
    assert len(draft_payload["text"]) == 50_000
    assert captured["payload"]["draft"]["text"] == "x" * 50_000


def test_draft_files_clamped_to_50_entries(monkeypatch, tmp_path):
    models, routes = _build_draft_env(tmp_path, monkeypatch)

    sid = "draft-save-files-clamp"
    models.Session(
        session_id=sid,
        title="Draft validation",
        messages=[{"role": "assistant", "content": "x" * 10}],
    ).save(touch_updated_at=False, skip_index=True)

    captured = _draft_post(
        monkeypatch,
        routes,
        {
            "session_id": sid,
            "text": "",
            "files": list(range(60)),
        },
    )

    draft_path = models.SESSION_DIR / ".drafts" / f"{sid}.json"
    draft_payload = json.loads(draft_path.read_text(encoding="utf-8"))
    assert len(draft_payload["files"]) == 50
    assert len(captured["payload"]["draft"]["files"]) == 50


def test_draft_text_type_coerced_to_string(monkeypatch, tmp_path):
    models, routes = _build_draft_env(tmp_path, monkeypatch)

    sid = "draft-save-text-nonstring"
    models.Session(
        session_id=sid,
        title="Draft validation",
        messages=[{"role": "assistant", "content": "x" * 10}],
    ).save(touch_updated_at=False, skip_index=True)

    observed = {}
    original_save = routes._save_session_draft

    def save_draft_spy(sid_value, draft_payload):
        observed["payload"] = draft_payload
        return original_save(sid_value, draft_payload)

    monkeypatch.setattr(routes, "_save_session_draft", save_draft_spy)
    captured = _draft_post(
        monkeypatch,
        routes,
        {
            "session_id": sid,
            "text": 1234,
            "files": [],
        },
    )

    assert captured["payload"]["draft"]["text"] == ""
    assert observed["payload"]["text"] == ""


def test_draft_files_type_coerced_to_list(monkeypatch, tmp_path):
    models, routes = _build_draft_env(tmp_path, monkeypatch)

    sid = "draft-save-files-nonlist"
    models.Session(
        session_id=sid,
        title="Draft validation",
        messages=[{"role": "assistant", "content": "x" * 10}],
    ).save(touch_updated_at=False, skip_index=True)

    observed = {}
    original_save = routes._save_session_draft

    def save_draft_spy(sid_value, draft_payload):
        observed["payload"] = draft_payload
        return original_save(sid_value, draft_payload)

    monkeypatch.setattr(routes, "_save_session_draft", save_draft_spy)
    captured = _draft_post(
        monkeypatch,
        routes,
        {
            "session_id": sid,
            "text": "text",
            "files": {"not": "a-list"},
        },
    )

    assert captured["payload"]["draft"]["files"] == []
    assert observed["payload"]["files"] == []


def test_draft_validation_appears_before_persist(monkeypatch, tmp_path):
    models, routes = _build_draft_env(tmp_path, monkeypatch)

    sid = "draft-validation-order"
    models.Session(
        session_id=sid,
        title="Draft validation",
        messages=[{"role": "assistant", "content": "x" * 10}],
    ).save(touch_updated_at=False, skip_index=True)

    entered_lock = []

    class LockProbe:
        def __enter__(self):
            entered_lock.append("entered")
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: LockProbe())

    original_save = routes._save_session_draft

    def save_draft_spy(sid_value, draft_payload):
        assert entered_lock
        assert draft_payload["text"] == ""
        assert draft_payload["files"] == []
        return original_save(sid_value, draft_payload)

    monkeypatch.setattr(routes, "_save_session_draft", save_draft_spy)
    _draft_post(
        monkeypatch,
        routes,
        {
            "session_id": sid,
            "text": 999,
            "files": {"not": "a-list"},
        },
    )


def test_draft_save_does_not_touch_session_updated_at(monkeypatch, tmp_path):
    models, routes = _build_draft_env(tmp_path, monkeypatch)

    sid = "draft-save-activity"
    models.Session(
        session_id=sid,
        title="Draft validation",
        messages=[{"role": "assistant", "content": "x" * 200_000}],
        updated_at=1234.0,
    ).save(touch_updated_at=False, skip_index=True)

    session_sidecar = models.SESSION_DIR / f"{sid}.json"
    before = json.loads(session_sidecar.read_text(encoding="utf-8"))

    captured = _draft_post(
        monkeypatch,
        routes,
        {
            "session_id": sid,
            "text": "new draft text",
            "files": [],
        },
    )

    after = json.loads(session_sidecar.read_text(encoding="utf-8"))
    assert captured["payload"]["draft"] == {"text": "new draft text", "files": []}
    assert after["updated_at"] == before["updated_at"]
    assert after["composer_draft"] == before["composer_draft"]


def test_draft_save_skips_unchanged_payload_before_persist(monkeypatch, tmp_path):
    models, routes = _build_draft_env(tmp_path, monkeypatch)

    sid = "draft-save-noop"
    models.Session(
        session_id=sid,
        title="Draft validation noop",
        messages=[{"role": "user", "content": "hello"}],
    ).save(touch_updated_at=False, skip_index=True)

    draft_calls = []
    original_save_draft = routes._save_session_draft

    def save_draft_spy(sid_value, draft_payload):
        draft_calls.append((sid_value, draft_payload))
        return original_save_draft(sid_value, draft_payload)

    monkeypatch.setattr(routes, "_save_session_draft", save_draft_spy)

    first = _draft_post(
        monkeypatch,
        routes,
        {
            "session_id": sid,
            "text": "persistent draft",
            "files": [],
        },
    )
    second = _draft_post(
        monkeypatch,
        routes,
        {
            "session_id": sid,
            "text": "persistent draft",
            "files": [],
        },
    )

    assert second["payload"].get("unchanged") is True
    assert len(draft_calls) == 1
    assert first["payload"]["draft"] == {"text": "persistent draft", "files": []}
