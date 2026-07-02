"""Regression tests for session-profile visibility on request-scoped session_id uses.

The security contract is now enforced with a generic preflight for request-supplied
session IDs. These tests cover the most sensitive paths that previously loaded
foreign-profile sessions directly: duplicate, file reads, and chat/start.
"""

from __future__ import annotations

import io
import time
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
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda sid, **_kwargs: persisted_foreign)
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
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda sid, **_kwargs: empty_visible)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda *_args, **_kwargs: "/workspace")
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_args, **_kwargs: (None, None, None))
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


def test_chat_stream_status_blocks_foreign_active_stream(monkeypatch):
    from api import config

    handler = _FakeHandler()
    foreign = _SimpleSession("foreign_session", profile="other")

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: foreign if sid == "foreign_session" else (_ for _ in ()).throw(KeyError("Session not found")))
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    with config.ACTIVE_RUNS_LOCK:
        previous = dict(config.ACTIVE_RUNS)
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS["stream-foreign"] = {
            "session_id": "foreign_session",
            "started_at": time.time(),
            "phase": "running",
        }

    try:
        cap = _capture(monkeypatch)
        routes.handle_get(handler, urlparse("/api/chat/stream/status?stream_id=stream-foreign"))
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
            config.ACTIVE_RUNS.update(previous)

    assert cap["bad"] == ("Session not found", 404)


def test_chat_stream_status_blocks_foreign_registered_stream_before_worker_start(monkeypatch):
    from api import config

    handler = _FakeHandler()
    foreign = _SimpleSession("foreign_session", profile="other")

    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: foreign
        if sid == "foreign_session"
        else (_ for _ in ()).throw(KeyError("Session not found")),
    )
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda _stream_id: (_ for _ in ()).throw(AssertionError("journal fallback should not run")),
    )
    with config.ACTIVE_RUNS_LOCK:
        previous_runs = dict(config.ACTIVE_RUNS)
        config.ACTIVE_RUNS.clear()
    with config.STREAM_SESSION_OWNERS_LOCK:
        previous_owners = dict(config.STREAM_SESSION_OWNERS)
        config.STREAM_SESSION_OWNERS.clear()
    config.register_stream_owner("stream-registered", "foreign_session")

    try:
        cap = _capture(monkeypatch)
        routes.handle_get(handler, urlparse("/api/chat/stream/status?stream_id=stream-registered"))
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
            config.ACTIVE_RUNS.update(previous_runs)
        with config.STREAM_SESSION_OWNERS_LOCK:
            config.STREAM_SESSION_OWNERS.clear()
            config.STREAM_SESSION_OWNERS.update(previous_owners)

    assert cap["bad"] == ("Session not found", 404)


def test_chat_stream_status_keeps_same_profile_stream_visible(monkeypatch):
    from api import config

    handler = _FakeHandler()
    visible = _SimpleSession("visible_session", profile="default")

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: visible if sid == "visible_session" else (_ for _ in ()).throw(KeyError("Session not found")))
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    with config.ACTIVE_RUNS_LOCK:
        previous = dict(config.ACTIVE_RUNS)
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS["stream-visible"] = {
            "session_id": "visible_session",
            "started_at": time.time(),
            "phase": "running",
        }

    try:
        cap = _capture(monkeypatch)
        routes.handle_get(handler, urlparse("/api/chat/stream/status?stream_id=stream-visible"))
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
            config.ACTIVE_RUNS.update(previous)

    assert cap["ok"]["stream_id"] == "stream-visible"
    assert cap["ok"]["active"] is False


def test_chat_cancel_blocks_foreign_owned_stream_before_cancel_call(monkeypatch):
    from api import runtime_adapter
    from api import config
    handler = _FakeHandler()
    foreign = _SimpleSession("foreign_session", profile="other")
    calls = {"cancel": 0}

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: foreign if sid == "foreign_session" else (_ for _ in ()).throw(KeyError("Session not found")))
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    with config.ACTIVE_RUNS_LOCK:
        previous = dict(config.ACTIVE_RUNS)
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS["stream-foreign"] = {
            "session_id": "foreign_session",
            "started_at": time.time(),
            "phase": "running",
        }
    monkeypatch.setattr(runtime_adapter, "runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: calls.__setitem__("cancel", calls["cancel"] + 1) or True)

    cap = _capture(monkeypatch)
    try:
        routes.handle_get(
            handler,
            urlparse("/api/chat/cancel?stream_id=stream-foreign"),
        )
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
            config.ACTIVE_RUNS.update(previous)

    assert calls["cancel"] == 0
    assert cap["bad"] == ("Session not found", 404)


def test_chat_cancel_same_profile_stream_still_passes_through(monkeypatch):
    from api import runtime_adapter
    handler = _FakeHandler()
    visible = _SimpleSession("visible_session", profile="default")
    calls = {"cancel": 0}

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: visible if sid == "visible_session" else (_ for _ in ()).throw(KeyError("Session not found")))
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(runtime_adapter, "runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr(routes, "cancel_stream", lambda _stream_id: calls.__setitem__("cancel", calls["cancel"] + 1) or True)

    cap = _capture(monkeypatch)

    with routes.ACTIVE_RUNS_LOCK:
        previous = dict(routes.ACTIVE_RUNS)
        routes.ACTIVE_RUNS.clear()
        routes.ACTIVE_RUNS["stream-visible"] = {
            "session_id": "visible_session",
            "started_at": time.time(),
            "phase": "running",
        }

    try:
        routes.handle_get(handler, urlparse("/api/chat/cancel?stream_id=stream-visible"))
    finally:
        with routes.ACTIVE_RUNS_LOCK:
            routes.ACTIVE_RUNS.clear()
            routes.ACTIVE_RUNS.update(previous)

    assert calls["cancel"] == 1
    assert cap["ok"]["cancelled"] is True


def test_chat_stream_blocks_foreign_owned_dead_stream_before_replay(monkeypatch):
    from api import runtime_adapter
    handler = _FakeHandler()
    foreign = _SimpleSession("foreign_session", profile="other")

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: foreign if sid == "foreign_session" else (_ for _ in ()).throw(KeyError("Session not found")))
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(runtime_adapter, "runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr(routes, "_stream_runner_run_events", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(routes, "find_run_summary", lambda _stream_id: {"session_id": "foreign_session", "terminal": False})
    monkeypatch.setattr(routes, "_replay_run_journal", lambda *_, **__: (_ for _ in ()).throw(AssertionError("replay should not run")))

    cap = _capture(monkeypatch)
    routes.handle_get(handler, urlparse("/api/chat/stream?stream_id=stream-dead-foreign"))

    assert cap["bad"] == ("Session not found", 404)


def test_chat_stream_allows_unknown_dead_stream_fallback_replay_path(monkeypatch):
    handler = _FakeHandler()
    calls = {"replay": 0}

    monkeypatch.setattr(routes, "get_session", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("session lookup should be avoided")))
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_stream_runner_run_events", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(routes, "find_run_summary", lambda _stream_id: None)
    monkeypatch.setattr(routes, "_replay_run_journal", lambda *_args, **_kwargs: calls.__setitem__("replay", calls["replay"] + 1))

    cap = _capture(monkeypatch)
    routes.handle_get(handler, urlparse("/api/chat/stream?stream_id=does-not-exist"))

    assert cap["ok"] == {"error": "stream not found"}
    assert calls["replay"] == 0


def test_session_new_skips_prev_session_commit_from_other_profile(monkeypatch):
    """Cross-profile prev_session_id after a profile switch must not 404 (#5420)."""
    import api.session_lifecycle as session_lifecycle
    handler = _FakeHandler()
    foreign = _SimpleSession("foreign_session", profile="other")
    calls = {"commit": 0, "new": 0}

    class _NewSession:
        def __init__(self):
            self.session_id = "new_session"
            self.messages = []

        def compact(self):
            return {"session_id": self.session_id}

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"prev_session_id": "foreign_session"})
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: foreign if sid == "foreign_session" else (_ for _ in ()).throw(KeyError("Session not found")))
    monkeypatch.setattr(session_lifecycle, "commit_session_memory", lambda sid, agent=None: calls.__setitem__("commit", calls["commit"] + 1))
    monkeypatch.setattr(
        routes,
        "new_session",
        lambda **_kwargs: calls.__setitem__("new", calls["new"] + 1) or _NewSession(),
    )

    cap = _capture(monkeypatch)
    routes.handle_post(handler, urlparse("/api/session/new"))

    assert calls["commit"] == 0
    assert calls["new"] == 1
    assert "bad" not in cap
    assert cap["ok"]["session"]["session_id"] == "new_session"


def test_session_new_keeps_prev_session_commit_for_same_profile(monkeypatch):
    import api.session_lifecycle as session_lifecycle
    handler = _FakeHandler()
    visible = _SimpleSession("visible_session", profile="default")
    calls = {"commit": 0, "new": 0}

    class _NewSession:
        def __init__(self):
            self.session_id = "new_session"
            self.messages = []

        def compact(self):
            return {"session_id": self.session_id}

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"prev_session_id": "visible_session"})
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: visible if sid == "visible_session" else (_ for _ in ()).throw(KeyError("Session not found")))
    monkeypatch.setattr(session_lifecycle, "commit_session_memory", lambda sid, agent=None: calls.__setitem__("commit", calls["commit"] + 1))
    monkeypatch.setattr(routes, "new_session", lambda **_kwargs: calls.__setitem__("new", calls["new"] + 1) or _NewSession())

    cap = _capture(monkeypatch)
    routes.handle_post(handler, urlparse("/api/session/new"))

    assert calls["commit"] == 1
    assert calls["new"] == 1
    assert cap["ok"]["session"]["session_id"] == "new_session"


def test_stream_owner_unregistered_on_worker_early_return(monkeypatch):
    """A stream cancelled before the worker starts (q is None) must not leak its owner entry.

    The route layer registers the stream owner synchronously before launching the
    worker. If the stream is cancelled before the worker reaches `q = STREAMS.get(...)`,
    the worker early-returns before register_active_run / the teardown finally — so the
    owner entry must be released on that early-return path or STREAM_SESSION_OWNERS leaks.
    """
    from api import config
    import api.streaming as streaming

    with config.STREAM_SESSION_OWNERS_LOCK:
        previous = dict(config.STREAM_SESSION_OWNERS)
        config.STREAM_SESSION_OWNERS.clear()
    # Owner registered by the route layer, but the stream was already cancelled
    # (never placed in STREAMS), so the worker will hit `q is None`.
    config.register_stream_owner("leak-stream", "some_session")
    try:
        with config.STREAMS_LOCK:
            config.STREAMS.pop("leak-stream", None)
        streaming._run_agent_streaming(
            "some_session", "hi", "m", "/tmp", "leak-stream",
        )
        with config.STREAM_SESSION_OWNERS_LOCK:
            assert "leak-stream" not in config.STREAM_SESSION_OWNERS
    finally:
        with config.STREAM_SESSION_OWNERS_LOCK:
            config.STREAM_SESSION_OWNERS.clear()
            config.STREAM_SESSION_OWNERS.update(previous)
