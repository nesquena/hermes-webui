import importlib
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from api.runtime_contract import make_event, make_status
from api.runtime_journal import RuntimeJournal

# Module-level to allow import isolation tests
_ROUTES_MOD = None


def _routes():
    global _ROUTES_MOD
    if _ROUTES_MOD is None:
        import api.routes as mod

        _ROUTES_MOD = mod
    return _ROUTES_MOD


def _make_journal(tmp_path):
    return RuntimeJournal(base_dir=tmp_path / "runs")


def _call_get(handler, path):
    routes = _routes()
    return routes.handle_get(handler, urlparse(path))


def _call_post(handler, path, body=None):
    routes = _routes()
    parsed = urlparse(path)
    bp = body or {}
    with patch("api.routes.read_body", return_value=bp), patch(
        "api.routes._check_csrf", return_value=True
    ), patch("api.routes._check_same_origin_browser_request", return_value=True):
        return routes.handle_post(handler, parsed)


def _capture_j():
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None, pretty=None):
        captured["payload"] = payload
        captured["status"] = status
        return True

    return captured, fake_j


def _patch_j(fake_j):
    return patch("api.helpers.j", side_effect=fake_j)


# ── Capabilities endpoint ────────────────────────────────────────────────


def test_capabilities_reports_api_version(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
    captured, fake_j = _capture_j()
    handler = MagicMock()
    with _patch_j(fake_j):
        _call_get(handler, "http://localhost/api/runtime/capabilities")
    assert captured["payload"]["api_version"] == "2026-07-02"


def test_capabilities_reports_runtime_adapter(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
    captured, fake_j = _capture_j()
    handler = MagicMock()
    with _patch_j(fake_j):
        _call_get(handler, "http://localhost/api/runtime/capabilities")
    assert captured["payload"]["runtime_adapter"] == "legacy-direct"


def test_capabilities_reports_resumable_events_false_in_direct_mode(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
    captured, fake_j = _capture_j()
    handler = MagicMock()
    with _patch_j(fake_j):
        _call_get(handler, "http://localhost/api/runtime/capabilities")
    assert captured["payload"]["supports"]["resumable_events"] is False


def test_capabilities_reports_resumable_events_true_in_journal_mode(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    captured, fake_j = _capture_j()
    handler = MagicMock()
    with _patch_j(fake_j):
        _call_get(handler, "http://localhost/api/runtime/capabilities")
    assert captured["payload"]["supports"]["resumable_events"] is True


# ── Active-run endpoint ──────────────────────────────────────────────────


def test_active_run_returns_active_false_for_idle_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
    captured, fake_j = _capture_j()
    handler = MagicMock()
    handler.headers = MagicMock()
    handler.headers.get = lambda k, d=None: "application/json"
    with _patch_j(fake_j):
        _call_get(handler, "http://localhost/api/sessions/no_such_session/active-run")
    assert captured["payload"]["active"] is False
    assert captured["payload"]["run"] is None


def test_active_run_returns_active_run_in_journal_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    from api.runtime_routes import _reset_journal_for_test

    _reset_journal_for_test()
    journal = _make_journal(tmp_path)
    with patch("api.runtime_routes._RUNTIME_JOURNAL", journal):
        status = journal.create_run("session_1")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        handler.headers = MagicMock()
        handler.headers.get = lambda k, d=None: "application/json"
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/sessions/session_1/active-run")
        assert captured["payload"]["active"] is True
        assert captured["payload"]["run"]["run_id"] == status.run_id
        assert captured["payload"]["run"]["status"] == "queued"


# ── Run status endpoint ──────────────────────────────────────────────────


def test_run_status_returns_runtime_status(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    with patch("api.runtime_routes._RUNTIME_JOURNAL", journal):
        captured, fake_j = _capture_j()
        handler = MagicMock()
        handler.headers = MagicMock()
        handler.headers.get = lambda k, d=None: "application/json"
        with _patch_j(fake_j):
            _call_get(handler, f"http://localhost/api/runs/{status.run_id}")
        assert captured["payload"]["run_id"] == status.run_id
        assert captured["payload"]["status"] == "queued"


def test_run_status_returns_404_for_unknown_run():
    captured, fake_j = _capture_j()
    handler = MagicMock()
    handler.headers = MagicMock()
    handler.headers.get = lambda k, d=None: "application/json"
    with _patch_j(fake_j):
        _call_get(handler, "http://localhost/api/runs/nonexistent")
    assert captured["status"] == 404


# ── Events endpoint ──────────────────────────────────────────────────────


def test_events_replays_all_events(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    for seq, typ in enumerate(["token.delta", "tool.started"], start=1):
        journal.append_event(
            make_event(run_id=status.run_id, session_id="session_1", seq=seq, type=typ)
        )
    with patch("api.runtime_routes._RUNTIME_JOURNAL", journal):
        captured, fake_j = _capture_j()
        handler = MagicMock()
        handler.headers = MagicMock()
        handler.headers.get = lambda k, d=None: "application/json"
        with _patch_j(fake_j):
            _call_get(handler, f"http://localhost/api/runs/{status.run_id}/events")
        assert captured["payload"]["run_id"] == status.run_id
        assert len(captured["payload"]["events"]) == 2


def test_events_respects_after_seq(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    for seq in range(1, 4):
        journal.append_event(
            make_event(run_id=status.run_id, session_id="session_1", seq=seq, type="token.delta")
        )
    with patch("api.runtime_routes._RUNTIME_JOURNAL", journal):
        captured, fake_j = _capture_j()
        handler = MagicMock()
        handler.headers = MagicMock()
        handler.headers.get = lambda k, d=None: "application/json"
        with _patch_j(fake_j):
            _call_get(handler, f"http://localhost/api/runs/{status.run_id}/events?after_seq=1")
        assert len(captured["payload"]["events"]) == 2
        assert captured["payload"]["events"][0]["seq"] == 2


def test_events_respects_limit(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    for seq in range(1, 6):
        journal.append_event(
            make_event(run_id=status.run_id, session_id="session_1", seq=seq, type="token.delta")
        )
    with patch("api.runtime_routes._RUNTIME_JOURNAL", journal):
        captured, fake_j = _capture_j()
        handler = MagicMock()
        handler.headers = MagicMock()
        handler.headers.get = lambda k, d=None: "application/json"
        with _patch_j(fake_j):
            _call_get(handler, f"http://localhost/api/runs/{status.run_id}/events?limit=2")
        assert len(captured["payload"]["events"]) == 2


def test_events_sse_includes_id_event_data(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    journal.append_event(
        make_event(run_id=status.run_id, session_id="session_1", seq=1, type="token.delta")
    )
    with patch("api.runtime_routes._RUNTIME_JOURNAL", journal):
        handler = MagicMock()
        handler.headers = MagicMock()
        handler.headers.get = lambda k, d=None: "text/event-stream"
        handler.wfile = io.BytesIO()
        handler.send_response = lambda code: None
        handler.send_header = lambda k, v: None
        handler.end_headers = lambda: None
        handler.log_message = lambda fmt, *args: None
        with patch("api.routes.j", side_effect=lambda *a, **kw: True):
            _call_get(handler, f"http://localhost/api/runs/{status.run_id}/events")
        output = handler.wfile.getvalue().decode("utf-8")
        assert f"id: {status.run_id}:1" in output
        assert "event: token.delta" in output
        assert "data:" in output


def test_done_event_is_terminal(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    done = make_event(
        run_id=status.run_id, session_id="session_1", seq=1, type="done", terminal=True
    )
    journal.append_event(done)
    with patch("api.runtime_routes._RUNTIME_JOURNAL", journal):
        captured, fake_j = _capture_j()
        handler = MagicMock()
        handler.headers = MagicMock()
        handler.headers.get = lambda k, d=None: "application/json"
        with _patch_j(fake_j):
            _call_get(handler, f"http://localhost/api/runs/{status.run_id}/events")
        ev = captured["payload"]["events"][0]
        assert ev["terminal"] is True


def test_error_event_is_terminal(tmp_path):
    journal = _make_journal(tmp_path)
    status = journal.create_run("session_1")
    err = make_event(
        run_id=status.run_id, session_id="session_1", seq=1, type="error", terminal=True
    )
    journal.append_event(err)
    with patch("api.runtime_routes._RUNTIME_JOURNAL", journal):
        captured, fake_j = _capture_j()
        handler = MagicMock()
        handler.headers = MagicMock()
        handler.headers.get = lambda k, d=None: "application/json"
        with _patch_j(fake_j):
            _call_get(handler, f"http://localhost/api/runs/{status.run_id}/events")
        ev = captured["payload"]["events"][0]
        assert ev["terminal"] is True


# ── Cancel endpoint ──────────────────────────────────────────────────────


def test_cancel_returns_not_supported_in_direct_mode(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
    captured, fake_j = _capture_j()
    handler = MagicMock()
    with _patch_j(fake_j):
        _call_post(handler, "http://localhost/api/runs/test_run/cancel", {"run_id": "test_run"})
    assert captured["payload"]["error"] == "not_supported"
    assert captured["status"] == 501


def test_approval_returns_not_supported(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    captured, fake_j = _capture_j()
    handler = MagicMock()
    with _patch_j(fake_j):
        _call_post(handler, "http://localhost/api/runs/test_run/approval", {"run_id": "test_run"})
    assert captured["payload"]["error"] == "not_supported"


def test_clarify_returns_not_supported(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    captured, fake_j = _capture_j()
    handler = MagicMock()
    with _patch_j(fake_j):
        _call_post(handler, "http://localhost/api/runs/test_run/clarify", {"run_id": "test_run"})
    assert captured["payload"]["error"] == "not_supported"


# ── Default mode compatibility ───────────────────────────────────────────


def test_default_mode_does_not_require_journal(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
    captured, fake_j = _capture_j()
    handler = MagicMock()
    handler.headers = MagicMock()
    handler.headers.get = lambda k, d=None: "application/json"
    with _patch_j(fake_j):
        _call_get(handler, "http://localhost/api/runtime/capabilities")
    assert captured["payload"]["runtime_adapter"] == "legacy-direct"
    assert captured["status"] == 200


# ── Route health ─────────────────────────────────────────────────────────


def test_route_modules_import_cleanly():
    mod = importlib.import_module("api.runtime_routes")
    assert hasattr(mod, "handle_runtime_capabilities")
    assert hasattr(mod, "handle_active_run")
    assert hasattr(mod, "handle_run_status")
    assert hasattr(mod, "handle_run_events")
    assert hasattr(mod, "handle_run_cancel")
    assert hasattr(mod, "handle_run_approval")
    assert hasattr(mod, "handle_run_clarify")


def test_runtime_routes_does_not_import_streaming_on_import():
    import sys

    mod = importlib.import_module("api.runtime_routes")
    assert "api.streaming" not in sys.modules or not hasattr(
        importlib.import_module("api.runtime_routes"), "streaming"
    )
