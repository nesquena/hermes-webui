import json
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from api.runtime_contract import make_event, make_status
from api.runtime_journal import RuntimeJournal, _load_index, _atomic_write_json

_ROUTES_MOD = None


def _routes():
    global _ROUTES_MOD
    if _ROUTES_MOD is None:
        import api.routes as mod

        _ROUTES_MOD = mod
    return _ROUTES_MOD


def _call_get(handler, path):
    routes = _routes()
    return routes.handle_get(handler, urlparse(path))


def _make_journal(tmp_path):
    return RuntimeJournal(base_dir=tmp_path / "runs")


def _capture_j():
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None, pretty=None):
        captured["payload"] = payload
        captured["status"] = status
        return True

    return captured, fake_j


def _patch_j(fake_j):
    return patch("api.helpers.j", side_effect=fake_j)


class TestMobileRunDashboard:
    """GET /api/mobile/runs returns active_runs and pending_actions."""

    def test_returns_active_runs_and_pending_actions_arrays(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/runs")
        assert isinstance(captured["payload"]["active_runs"], list)
        assert isinstance(captured["payload"]["pending_actions"], list)

    def test_idle_state_returns_empty_arrays(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        from api.runtime_routes import _reset_journal_for_test
        from api import mobile_routes as mr

        _reset_journal_for_test()
        mr._reset_journal_for_test()
        journal = _make_journal(tmp_path)
        mr._MOBILE_JOURNAL = journal
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/runs")
        assert captured["payload"]["active_runs"] == []
        assert captured["payload"]["pending_actions"] == []

    def test_active_legacy_journal_run_appears(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        from api.runtime_routes import _reset_journal_for_test

        _reset_journal_for_test()
        journal = _make_journal(tmp_path)
        status = journal.create_run("sess_1")
        journal.append_event(
            make_event(
                run_id=status.run_id, session_id="sess_1", seq=1, type="run.started"
            )
        )

        from api import mobile_routes as mr

        mr._MOBILE_JOURNAL = journal
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/runs")
        active = captured["payload"]["active_runs"]
        assert len(active) >= 1
        assert any(r["run_id"] == status.run_id for r in active)

    def test_active_run_has_required_fields(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        from api.runtime_routes import _reset_journal_for_test

        _reset_journal_for_test()
        journal = _make_journal(tmp_path)
        status = journal.create_run("sess_1")
        journal.append_event(
            make_event(
                run_id=status.run_id, session_id="sess_1", seq=1, type="run.started"
            )
        )

        from api import mobile_routes as mr

        mr._MOBILE_JOURNAL = journal
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/runs")
        run = captured["payload"]["active_runs"][0]
        required = {"run_id", "session_id", "status", "last_event_id", "last_seq", "controls"}
        for field in required:
            assert field in run, f"missing field: {field}"
        assert "title" in run
        assert "current_activity" in run
        assert "model" in run
        assert "profile" in run
        assert "workspace" in run
        assert "elapsed_seconds" in run

    def test_terminal_run_does_not_appear(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        from api.runtime_routes import _reset_journal_for_test

        _reset_journal_for_test()
        journal = _make_journal(tmp_path)
        status = journal.create_run("sess_1")
        journal.append_event(
            make_event(
                run_id=status.run_id, session_id="sess_1",
                seq=2, type="done", terminal=True,
            )
        )

        from api import mobile_routes as mr

        mr._MOBILE_JOURNAL = journal
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/runs")
        active = captured["payload"]["active_runs"]
        run_ids = [r["run_id"] for r in active]
        assert status.run_id not in run_ids

    def test_pending_approval_appears_in_pending_actions(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        from api.runtime_routes import _reset_journal_for_test

        _reset_journal_for_test()
        journal = _make_journal(tmp_path)
        status = journal.create_run("sess_1")
        journal.append_event(
            make_event(
                run_id=status.run_id, session_id="sess_1",
                seq=1, type="approval.requested",
                payload={"approval_id": "appr_1"},
            )
        )
        index = _load_index(journal._index_file)
        entry = index["runs"][status.run_id]
        entry["pending_approval_ids"] = ["appr_1"]
        index["runs"][status.run_id] = entry
        _atomic_write_json(journal._index_file, index, journal._index_lock)

        from api import mobile_routes as mr

        mr._MOBILE_JOURNAL = journal
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/runs")
        pending = captured["payload"]["pending_actions"]
        assert any(
            a["action_id"] == "appr_1" and a["type"] == "approval"
            for a in pending
        )

    def test_pending_clarify_appears_in_pending_actions(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        from api.runtime_routes import _reset_journal_for_test

        _reset_journal_for_test()
        journal = _make_journal(tmp_path)
        status = journal.create_run("sess_1")
        journal.append_event(
            make_event(
                run_id=status.run_id, session_id="sess_1",
                seq=1, type="clarify.requested",
                payload={"clarify_id": "clar_1"},
            )
        )
        index = _load_index(journal._index_file)
        entry = index["runs"][status.run_id]
        entry["pending_clarify_ids"] = ["clar_1"]
        index["runs"][status.run_id] = entry
        _atomic_write_json(journal._index_file, index, journal._index_lock)

        from api import mobile_routes as mr

        mr._MOBILE_JOURNAL = journal
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/runs")
        pending = captured["payload"]["pending_actions"]
        assert any(
            a["action_id"] == "clar_1" and a["type"] == "clarify"
            for a in pending
        )

    def test_secret_like_values_are_redacted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        from api.runtime_routes import _reset_journal_for_test

        _reset_journal_for_test()
        journal = _make_journal(tmp_path)
        status = journal.create_run("sess_1")
        journal.append_event(
            make_event(
                run_id=status.run_id, session_id="sess_1",
                seq=1, type="run.started",
                payload={"api_key": "sk-12345"},
            )
        )

        from api import mobile_routes as mr

        mr._MOBILE_JOURNAL = journal
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/runs")
        payload_str = json.dumps(captured["payload"])
        assert "sk-12345" not in payload_str

    def test_unavailable_metadata_fields_are_null(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        from api.runtime_routes import _reset_journal_for_test

        _reset_journal_for_test()
        journal = _make_journal(tmp_path)
        status = journal.create_run("sess_1")
        journal.append_event(
            make_event(
                run_id=status.run_id, session_id="sess_1", seq=1, type="run.started"
            )
        )

        from api import mobile_routes as mr

        mr._MOBILE_JOURNAL = journal
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/runs")
        run = captured["payload"]["active_runs"][0]
        assert run["title"] is None
        assert run["model"] is None
        assert run["profile"] is None
        assert run["workspace"] is None
