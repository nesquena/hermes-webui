"""Regression tests for session-profile visibility on request-scoped session_id uses.

The security contract is now enforced with a generic preflight for request-supplied
session IDs. These tests cover the most sensitive paths that previously loaded
foreign-profile sessions directly: duplicate, file reads, and chat/start.
"""

from __future__ import annotations

import io
from urllib.parse import urlparse

import api.routes as routes
import api.upload as upload


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {"Content-Type": "multipart/form-data; boundary=test", "Content-Length": "1"}
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.command = "GET"
        self.path = "/"
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


def _capture(monkeypatch):
    cap = {}

    def _j(_h, obj, *_, **__):
        cap["ok"] = obj
        return True

    def _bad(_h, msg, code=400):
        cap["bad"] = (msg, code)
        return True

    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "bad", _bad)
    return cap


def _capture_upload(monkeypatch):
    cap = {}

    def _j(_h, obj, *_, **kwargs):
        cap["ok"] = obj
        cap["status"] = kwargs.get("status", 200)
        return True

    monkeypatch.setattr(upload, "j", _j)
    return cap


class _SimpleSession:
    def __init__(self, sid, profile="default", workspace="/workspace", messages=None, context_messages=None, pending_user_message=None):
        self.session_id = sid
        self.profile = profile
        self.workspace = workspace
        self.model = "test-model"
        self.model_provider = None
        self.title = "Test"
        self.messages = messages or []
        self.tool_calls = []
        self.project_id = None
        self.context_messages = context_messages
        self.pending_user_message = pending_user_message
        self.personality = None
        self.enabled_toolsets = None
        self.context_length = None
        self.threshold_tokens = None
        self.truncation_watermark = None
        self.truncation_boundary = None
        self.gateway_routing = None
        self.gateway_routing_history = []
        self.llm_title_generated = False
        self.manual_title = False
        self.composer_draft = {}
        self.context_engine = None
        self.context_engine_state = {}
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost = 0.0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0


def test_session_duplicate_foreign_profile_session_blocked_by_visibility_guard(monkeypatch):
    handler = _FakeHandler()
    foreign = _SimpleSession("foreign_duplicate", profile="other")
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: foreign)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": "foreign_duplicate"})
    monkeypatch.setattr(routes.Session, "load", staticmethod(lambda sid: (_ for _ in ()).throw(AssertionError("duplicate should not materialize foreign session"))))

    cap = _capture(monkeypatch)
    routes.handle_post(handler, urlparse("/api/session/duplicate"))

    assert cap["bad"] == ("Session not found", 404)


def test_session_duplicate_same_profile_still_duplicates(monkeypatch):
    handler = _FakeHandler()
    source = _SimpleSession("session_visible", profile="default")
    calls = {"load": 0, "save": 0}

    def _load(_sid):
        calls["load"] += 1
        return source

    monkeypatch.setattr(routes.Session, "load", staticmethod(_load))
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: source)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": "session_visible"})

    def _session_save(_self, *_, **__):
        calls["save"] += 1

    monkeypatch.setattr(routes.Session, "save", _session_save)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_, **__: None)

    cap = _capture(monkeypatch)
    routes.handle_post(handler, urlparse("/api/session/duplicate"))

    assert calls["load"] == 1
    assert calls["save"] == 1
    assert "bad" not in cap
    assert cap["ok"]["session"]["session_id"] != "session_visible"


def test_file_read_foreign_profile_session_returns_404_before_file_ops(monkeypatch):
    handler = _FakeHandler()
    foreign = _SimpleSession("foreign_file", profile="other", workspace="/workspace")
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: foreign)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: (_ for _ in ()).throw(AssertionError("file ops should not run")))
    cap = _capture(monkeypatch)

    routes.handle_get(handler, urlparse("/api/file?session_id=foreign_file&path=notes.txt"))

    assert cap["bad"] == ("Session not found", 404)


def test_chat_start_foreign_persisted_session_returns_404_before_start_run(monkeypatch):
    handler = _FakeHandler()
    persisted_foreign = _SimpleSession(
        "chat_foreign",
        profile="other",
        messages=[{"role": "user", "content": "first"}],
    )

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": "chat_foreign", "message": "hello"})
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda sid: persisted_foreign)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_start_run", lambda *_, **__: (_ for _ in ()).throw(AssertionError("_start_run should not run")))

    cap = _capture(monkeypatch)
    routes.handle_post(handler, urlparse("/api/chat/start"))

    assert cap["bad"] == ("Session not found", 404)


def test_chat_start_body_profile_cannot_retag_visible_empty_session_without_active_profile(monkeypatch):
    handler = _FakeHandler()
    empty_visible = _SimpleSession("chat_empty", profile="default", messages=[])
    captured = {}

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {"session_id": "chat_empty", "message": "hello", "profile": "other"},
    )
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda sid: empty_visible)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda *_args, **_kwargs: "/workspace")
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_args, **_kwargs: (None, None))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("test-model", None, "test-model"),
    )

    def _start_run(session, **_kwargs):
        captured["profile"] = session.profile
        return {"ok": True, "stream_id": "stream-test", "session_id": session.session_id}

    monkeypatch.setattr(routes, "_start_run", _start_run)

    cap = _capture(monkeypatch)
    routes.handle_post(handler, urlparse("/api/chat/start"))

    assert "bad" not in cap
    assert captured["profile"] == "default"


def test_attachment_upload_foreign_profile_session_returns_404_before_write(monkeypatch):
    handler = _FakeHandler()
    foreign = _SimpleSession("upload_foreign", profile="other")
    monkeypatch.setattr(upload, "parse_multipart", lambda *_args: ({"session_id": "upload_foreign"}, {"file": ("note.txt", b"x")}))
    monkeypatch.setattr(upload, "get_session", lambda sid: foreign)
    monkeypatch.setattr(upload, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        upload,
        "_upload_destination",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("upload destination should not be resolved")),
    )

    cap = _capture_upload(monkeypatch)
    upload.handle_upload(handler)

    assert cap == {"ok": {"error": "Session not found"}, "status": 404}


def test_archive_upload_extract_foreign_profile_session_returns_404_before_extract(monkeypatch):
    handler = _FakeHandler()
    foreign = _SimpleSession("extract_foreign", profile="other")
    monkeypatch.setattr(upload, "parse_multipart", lambda *_args: ({"session_id": "extract_foreign"}, {"file": ("archive.zip", b"zip")}))
    monkeypatch.setattr(upload, "get_session", lambda sid: foreign)
    monkeypatch.setattr(upload, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        upload,
        "extract_archive",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("archive extraction should not run")),
    )

    cap = _capture_upload(monkeypatch)
    upload.handle_upload_extract(handler)

    assert cap == {"ok": {"error": "Session not found"}, "status": 404}


def test_workspace_upload_foreign_profile_session_returns_404_before_workspace_resolution(monkeypatch):
    handler = _FakeHandler()
    foreign = _SimpleSession("workspace_foreign", profile="other", workspace="/workspace/foreign")
    monkeypatch.setattr(upload, "parse_multipart", lambda *_args: ({"session_id": "workspace_foreign"}, {"file": ("note.txt", b"x")}))
    monkeypatch.setattr(upload, "get_session", lambda sid: foreign)
    monkeypatch.setattr(upload, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        upload,
        "resolve_trusted_workspace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("workspace should not be resolved")),
    )

    cap = _capture_upload(monkeypatch)
    upload.handle_workspace_upload(handler)

    assert cap == {"ok": {"error": "Session not found"}, "status": 404}
