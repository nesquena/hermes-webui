"""Regression tests for lightweight composer-draft persistence."""

import io
import json
from types import SimpleNamespace
from urllib.parse import urlparse

import api.models as models
import api.routes as routes
from api import draft_store


def test_draft_save_uses_small_sidecar_without_rewriting_transcript(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)

    sid = "large_session_001"
    session_path = session_dir / f"{sid}.json"
    transcript = {
        "session_id": sid,
        "messages": [
            {"role": "user", "content": f"message {i}"}
            for i in range(1000)
        ],
        "composer_draft": {"text": "legacy draft", "files": []},
    }
    session_path.write_text(json.dumps(transcript, indent=2), encoding="utf-8")
    before = session_path.read_bytes()

    saved = draft_store.save_draft(sid, {"text": "new draft", "files": []})

    assert saved == {"text": "new draft", "files": []}
    assert session_path.read_bytes() == before
    assert draft_store.load_draft(sid) == saved
    assert json.loads(
        (session_dir / "_drafts" / f"{sid}.json").read_text(encoding="utf-8")
    ) == {"text": "new draft", "files": []}


def test_draft_load_falls_back_to_embedded_legacy_value(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)

    sid = "legacy_session_001"
    assert draft_store.load_draft(sid, fallback={"text": "legacy", "files": []}) == {
        "text": "legacy",
        "files": [],
    }


def test_session_load_prefers_draft_sidecar(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)

    sid = "overlay_session_001"
    session_path = session_dir / f"{sid}.json"
    session_path.write_text(
        json.dumps(
            {
                "session_id": sid,
                "messages": [{"role": "user", "content": "history"}],
                "composer_draft": {"text": "legacy", "files": []},
            }
        ),
        encoding="utf-8",
    )
    draft_store.save_draft(sid, {"text": "sidecar", "files": []})

    loaded = models.Session.load(sid)

    assert loaded is not None
    assert loaded.composer_draft == {"text": "sidecar", "files": []}


def test_draft_route_does_not_call_full_session_save(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda sid: False)

    sid = "route_session_001"
    session = SimpleNamespace(
        session_id=sid,
        composer_draft={"text": "", "files": []},
    )
    session_path = session_dir / f"{sid}.json"
    session_path.write_text(
        json.dumps(
            {
                "session_id": sid,
                "messages": [{"role": "user", "content": "history"}],
                "composer_draft": {"text": "", "files": []},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: session)

    def fail_save(*args, **kwargs):
        raise AssertionError("draft persistence must not call Session.save")

    monkeypatch.setattr(models.Session, "save", fail_save)

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)

    body = {"session_id": sid, "text": "typed", "files": []}
    encoded_body = json.dumps(body).encode("utf-8")
    handler = SimpleNamespace(
        command="POST",
        headers={"Content-Length": str(len(encoded_body))},
        rfile=io.BytesIO(encoded_body),
        _safe_webui_print=lambda *_args, **_kwargs: None,
    )
    parsed = urlparse("/api/session/draft")
    responses = []

    def fake_json(_handler, payload, *args, **kwargs):
        responses.append(payload)
        return True

    monkeypatch.setattr(routes, "j", fake_json)
    assert routes.handle_post(handler, parsed) is True

    assert responses[-1]["draft"] == {"text": "typed", "files": []}
    assert draft_store.load_draft(sid) == {"text": "typed", "files": []}
    assert json.loads(session_path.read_text(encoding="utf-8"))["messages"] == [
        {"role": "user", "content": "history"}
    ]
