import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from api.runtime_contract import make_event
from api.runtime_journal import RuntimeJournal


def _capture_json():
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None, pretty=None):
        captured["payload"] = payload
        captured["status"] = status
        return True

    return captured, fake_j


def _capture_bad():
    captured = {}

    def fake_bad(handler, message, status=400):
        captured["message"] = message
        captured["status"] = status
        return True

    return captured, fake_bad


def test_runtime_journal_terminal_state_survives_later_event(tmp_path):
    journal = RuntimeJournal(base_dir=tmp_path / "runs")
    status = journal.create_run("session_1")
    journal.append_event(
        make_event(
            run_id=status.run_id,
            session_id="session_1",
            seq=1,
            type="done",
            terminal=True,
        )
    )
    journal.append_event(
        make_event(
            run_id=status.run_id,
            session_id="session_1",
            seq=2,
            type="token.delta",
        )
    )

    fetched = journal.get_status(status.run_id)
    assert fetched is not None
    assert fetched.terminal is True
    assert journal.get_active_run_for_session("session_1") is None


def test_run_events_reads_journal_once_with_filters(monkeypatch):
    import api.runtime_routes as routes

    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    calls = []

    class FakeJournal:
        def read_events(self, run_id, after_seq=None, limit=None):
            calls.append((run_id, after_seq, limit))
            return []

    handler = MagicMock()
    handler.headers.get = lambda key, default=None: "application/json"
    captured, fake_j = _capture_json()

    with patch("api.runtime_routes._journal", return_value=FakeJournal()), patch(
        "api.helpers.j", side_effect=fake_j
    ):
        routes.handle_run_events(
            handler,
            SimpleNamespace(path="/api/runs/run_1/events", query="after_seq=2&limit=5"),
        )

    assert calls == [("run_1", 2, 5)]
    assert captured["status"] == 200


def test_run_status_action_subpath_is_not_treated_as_run_id():
    import api.runtime_routes as routes

    handler = MagicMock()
    captured, fake_bad = _capture_bad()

    with patch("api.helpers.bad", side_effect=fake_bad):
        routes.handle_run_status(
            handler,
            SimpleNamespace(path="/api/runs/run_1/cancel", query=""),
        )

    assert captured == {"message": "invalid route", "status": 404}


def test_agent_runs_adapter_singleton_initialization_is_locked(monkeypatch):
    import api.runtime_adapters as adapters
    from api.runtime_adapters.agent_runs import AgentRunsAdapter

    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "agent-runs")
    monkeypatch.setenv("HERMES_WEBUI_AGENT_RUNS_BASE_URL", "http://127.0.0.1:8642")
    adapters._reset_adapter_instance_for_test()

    created = []
    returned = []

    class FakeAdapter:
        pass

    def fake_from_env(environ=None):
        time.sleep(0.01)
        adapter = FakeAdapter()
        created.append(adapter)
        return adapter

    def worker():
        returned.append(adapters.get_runtime_adapter())

    with patch.object(AgentRunsAdapter, "from_env", side_effect=fake_from_env):
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert len(created) == 1
    assert len(returned) == 8
    assert all(adapter is returned[0] for adapter in returned)
