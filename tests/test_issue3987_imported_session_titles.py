"""Regression coverage for imported-session title generation after CLI import (#3987)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import api.routes as routes


ROOT = Path(__file__).resolve().parents[1]
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_import_cli_handler_queues_default_titles_after_persisting_import():
    handler_idx = ROUTES_PY.index("def _handle_session_import_cli")
    next_handler_idx = ROUTES_PY.index("def _handle_session_import(", handler_idx)
    block = ROUTES_PY[handler_idx:next_handler_idx]
    queue_idx = block.index("_queue_generated_title_for_imported_session(")
    publish_idx = block.index('publish_session_list_changed(\n        "session_import_cli",')
    response_idx = block.index("return j(", queue_idx)
    assert publish_idx < queue_idx < response_idx
    queue_window = block[queue_idx:queue_idx + 400]
    assert '"title": cli_title' in queue_window
    assert '"read_only": cli_read_only' in queue_window


def test_import_cli_queue_helper_is_guarded_and_runs_in_background():
    helper_idx = ROUTES_PY.index("def _queue_generated_title_for_imported_session")
    next_helper_idx = ROUTES_PY.index("def _gateway_sse_probe_payload", helper_idx)
    block = ROUTES_PY[helper_idx:next_helper_idx]
    assert "cli_meta.get(\"read_only\")" in block
    assert "not _looks_like_default_cli_title(cli_meta)" in block
    assert "if not _looks_like_default_cli_title(current_meta):" in block
    assert "generate_session_title_for_session(current)" in block
    assert "_persist_generated_session_title(current, normalized_next, event_reason=\"session_title_regenerate\")" in block
    assert "threading.Thread(target=_run, daemon=True" in block


def test_import_cli_queue_helper_generates_title_once_for_placeholder_session(monkeypatch):
    persisted = []
    generated = []

    class FakeSession:
        def __init__(self, title):
            self.session_id = "cli_queued_title"
            self.title = title
            self.source_tag = "cli"
            self.raw_source = "cli"
            self.session_source = "external_agent"
            self.source_label = "CLI"
            self.read_only = False

    current = FakeSession("CLI Session")

    class InlineThread:
        def __init__(self, *, target, daemon, name):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(routes.threading, "Thread", InlineThread)
    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, sid: current if sid == current.session_id else None))
    monkeypatch.setattr(routes, "_ensure_full_session_before_mutation", lambda sid, session: session)
    monkeypatch.setattr(routes, "generate_session_title_for_session", lambda session: (generated.append(session.session_id) or "Better imported title", "llm", "raw"))
    monkeypatch.setattr(routes, "_persist_generated_session_title", lambda session, title, *, event_reason: persisted.append((session.session_id, title, event_reason)))

    routes._queue_generated_title_for_imported_session(current, {"title": "CLI Session", "source_tag": "cli"})

    assert generated == [current.session_id]
    assert persisted == [(current.session_id, "Better imported title", "session_title_regenerate")]


def test_import_cli_queue_helper_skips_sessions_that_already_have_real_titles(monkeypatch):
    generated = []
    persisted = []

    class FakeSession:
        def __init__(self):
            self.session_id = "cli_real_title"
            self.title = "Useful imported title"
            self.source_tag = "cli"
            self.raw_source = "cli"
            self.session_source = "external_agent"
            self.source_label = "CLI"
            self.read_only = False

    current = FakeSession()

    class InlineThread:
        def __init__(self, *, target, daemon, name):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(routes.threading, "Thread", InlineThread)
    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, sid: current if sid == current.session_id else None))
    monkeypatch.setattr(routes, "_ensure_full_session_before_mutation", lambda sid, session: session)
    monkeypatch.setattr(
        routes,
        "generate_session_title_for_session",
        lambda session: generated.append(session.session_id) or ("Unexpected generated title", "llm", "raw"),
    )
    monkeypatch.setattr(routes, "_persist_generated_session_title", lambda session, title, *, event_reason: persisted.append((session.session_id, title, event_reason)))

    routes._queue_generated_title_for_imported_session(
        current,
        {"title": "CLI Session", "source_tag": "cli"},
    )

    assert generated == []
    assert persisted == []


def test_regenerate_endpoint_only_blocks_read_only_imported_sessions():
    endpoint_idx = ROUTES_PY.index('"/api/session/title/regenerate"')
    next_endpoint_idx = ROUTES_PY.index('"/api/personality/set"', endpoint_idx)
    block = ROUTES_PY[endpoint_idx:next_endpoint_idx]
    assert 'if getattr(s, "read_only", False):' in block
    assert 'getattr(s, "is_imported", False)' not in block


def test_sessions_ui_keeps_regenerate_action_for_writable_imports():
    regen_idx = SESSIONS_JS.index("api('/api/session/title/regenerate'")
    window = SESSIONS_JS[regen_idx - 500:regen_idx]
    assert "session.is_imported" not in window
    assert "_isReadOnlySession(session)" in SESSIONS_JS
