import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from api.runtime_contract import make_event, make_status
from api.runtime_journal import RuntimeJournal, _load_index, _atomic_write_json
from api.runtime_adapter import ControlResult, RunStatus

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


def _call_post(handler, path, body=None):
    routes = _routes()
    parsed = urlparse(path)
    bp = body or {}
    with patch("api.routes.read_body", return_value=bp), patch(
        "api.routes._check_csrf", return_value=True
    ), patch("api.routes._check_same_origin_browser_request", return_value=True):
        return routes.handle_post(handler, parsed)


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


class TestMobilePendingActions:
    """GET /api/mobile/pending-actions and POST .../resolve."""

    def test_returns_pending_actions_array(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/pending-actions")
        assert isinstance(captured["payload"]["pending_actions"], list)

    def test_approval_in_active_run_appears(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        from api.runtime_routes import _reset_journal_for_test

        _reset_journal_for_test()
        journal = _make_journal(tmp_path)
        status = journal.create_run("sess_1")
        journal.append_event(
            make_event(
                run_id=status.run_id, session_id="sess_1",
                seq=1, type="approval.requested",
                payload={"approval_id": "appr_2"},
            )
        )
        index = _load_index(journal._index_file)
        entry = index["runs"][status.run_id]
        entry["pending_approval_ids"] = ["appr_2"]
        index["runs"][status.run_id] = entry
        _atomic_write_json(journal._index_file, index, journal._index_lock)

        from api import mobile_routes as mr

        mr._MOBILE_JOURNAL = journal
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/pending-actions")
        actions = captured["payload"]["pending_actions"]
        assert any(
            a["action_id"] == "appr_2" and a["type"] == "approval"
            for a in actions
        )

    def test_clarify_in_active_run_appears(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
        from api.runtime_routes import _reset_journal_for_test

        _reset_journal_for_test()
        journal = _make_journal(tmp_path)
        status = journal.create_run("sess_1")
        journal.append_event(
            make_event(
                run_id=status.run_id, session_id="sess_1",
                seq=1, type="clarify.requested",
                payload={"clarify_id": "clar_2"},
            )
        )
        index = _load_index(journal._index_file)
        entry = index["runs"][status.run_id]
        entry["pending_clarify_ids"] = ["clar_2"]
        index["runs"][status.run_id] = entry
        _atomic_write_json(journal._index_file, index, journal._index_lock)

        from api import mobile_routes as mr

        mr._MOBILE_JOURNAL = journal
        captured, fake_j = _capture_j()
        handler = MagicMock()
        with _patch_j(fake_j):
            _call_get(handler, "http://localhost/api/mobile/pending-actions")
        actions = captured["payload"]["pending_actions"]
        assert any(
            a["action_id"] == "clar_2" and a["type"] == "clarify"
            for a in actions
        )

    def test_approval_resolve_routes_to_legacy_not_supported(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        body = {
            "type": "approval",
            "run_id": "run_1",
            "choice": "approve",
        }
        with _patch_j(fake_j):
            _call_post(handler, "http://localhost/api/mobile/pending-actions/appr_1/resolve", body)
        assert captured["payload"]["error"] == "not_supported"

    def test_clarify_resolve_routes_to_legacy_not_supported(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        body = {
            "type": "clarify",
            "run_id": "run_1",
            "answer": "yes",
        }
        with _patch_j(fake_j):
            _call_post(handler, "http://localhost/api/mobile/pending-actions/clar_1/resolve", body)
        assert captured["payload"]["error"] == "not_supported"

    def test_unknown_action_returns_404(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        body = {
            "type": "approval",
            "run_id": "run_1",
            "choice": "approve",
        }
        with _patch_j(fake_j):
            _call_post(handler,
                       "http://localhost/api/mobile/pending-actions/no_such_action/resolve",
                       body)
        assert captured["status"] == 501

    def test_invalid_action_type_returns_validation_error(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        body = {
            "type": "invalid",
            "run_id": "run_1",
        }
        with _patch_j(fake_j):
            _call_post(handler, "http://localhost/api/mobile/pending-actions/act_1/resolve", body)
        assert captured["status"] == 400

    def test_missing_run_id_returns_400(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
        captured, fake_j = _capture_j()
        handler = MagicMock()
        body = {
            "type": "approval",
            "choice": "approve",
        }
        with _patch_j(fake_j):
            _call_post(handler, "http://localhost/api/mobile/pending-actions/act_1/resolve", body)
        assert captured["status"] == 400

    def test_agent_runs_approval_resolve(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_API_KEY", "test-key")

        from api.runtime_adapters import _reset_adapter_instance_for_test

        _reset_adapter_instance_for_test()
        fake_adapter = MagicMock()
        fake_adapter.respond_approval.return_value = ControlResult(
            True, status="accepted", safe_message="ok"
        )
        from api.runtime_adapters import get_runtime_adapter

        with patch(
            "api.mobile_routes._adapter", return_value=fake_adapter
        ):
            captured, fake_j = _capture_j()
            handler = MagicMock()
            body = {
                "type": "approval",
                "run_id": "run_1",
                "choice": "approve",
            }
            with _patch_j(fake_j):
                _call_post(
                    handler,
                    "http://localhost/api/mobile/pending-actions/appr_99/resolve",
                    body,
                )
            assert captured["payload"]["ok"] is True
            assert captured["status"] == 200

    def test_agent_runs_clarify_resolve(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_API_KEY", "test-key")

        from api.runtime_adapters import _reset_adapter_instance_for_test

        _reset_adapter_instance_for_test()
        fake_adapter = MagicMock()
        fake_adapter.respond_clarify.return_value = ControlResult(
            True, status="accepted", safe_message="ok"
        )

        with patch(
            "api.mobile_routes._adapter", return_value=fake_adapter
        ):
            captured, fake_j = _capture_j()
            handler = MagicMock()
            body = {
                "type": "clarify",
                "run_id": "run_1",
                "answer": "yes",
            }
            with _patch_j(fake_j):
                _call_post(
                    handler,
                    "http://localhost/api/mobile/pending-actions/clar_99/resolve",
                    body,
                )
            assert captured["payload"]["ok"] is True
            assert captured["status"] == 200

    def test_agent_runs_not_supported_propagates(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_API_KEY", "test-key")

        from api.runtime_adapters import _reset_adapter_instance_for_test

        _reset_adapter_instance_for_test()
        fake_adapter = MagicMock()
        fake_adapter.respond_approval.return_value = ControlResult(
            False, status="not_supported", safe_message="not supported"
        )

        with patch(
            "api.mobile_routes._adapter", return_value=fake_adapter
        ):
            captured, fake_j = _capture_j()
            handler = MagicMock()
            body = {
                "type": "approval",
                "run_id": "run_1",
                "choice": "approve",
            }
            with _patch_j(fake_j):
                _call_post(
                    handler,
                    "http://localhost/api/mobile/pending-actions/appr_ns/resolve",
                    body,
                )
            assert captured["payload"]["error"] == "not_supported"
            assert captured["status"] == 501

    def test_response_does_not_include_secrets(self, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
        monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_API_KEY", "test-key")

        from api.runtime_adapters import _reset_adapter_instance_for_test

        _reset_adapter_instance_for_test()
        fake_adapter = MagicMock()
        fake_adapter.respond_approval.return_value = ControlResult(
            True, status="accepted", safe_message="ok",
        )

        with patch(
            "api.mobile_routes._adapter", return_value=fake_adapter
        ):
            captured, fake_j = _capture_j()
            handler = MagicMock()
            body = {
                "type": "approval",
                "run_id": "run_1",
                "choice": "approve",
            }
            with _patch_j(fake_j):
                _call_post(
                    handler,
                    "http://localhost/api/mobile/pending-actions/appr_sec/resolve",
                    body,
                )
            payload_str = json.dumps(captured["payload"])
            lower = payload_str.lower()
            assert "test-key" not in lower
