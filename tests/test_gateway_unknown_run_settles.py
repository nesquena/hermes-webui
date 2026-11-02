"""A Gateway run whose owning worker epoch died reports status "unknown". The Gateway keeps the session paused and
may still recover a captured answer (``resolve-unknown``), so the WebUI must keep polling, not settle it as lost,
and must not report a Stop the Gateway refused (409 ``unknown_execution``) as successful."""
import io
import json
import urllib.error
from email.message import Message
from unittest.mock import patch

import pytest

import api.gateway_chat as gateway_chat
from tests.test_gateway_run_reattach_after_restart import (  # noqa: F401  (fixtures and helpers)
    _orphaned_gateway_turn,
    _saved,
    _wait_for_reattach_threads,
    isolated_sessions,
)


@pytest.mark.usefixtures("isolated_sessions")
def test_reattach_keeps_polling_an_unknown_run_until_the_gateway_recovers_it(monkeypatch):
    sid, _stream_id = _orphaned_gateway_turn()
    statuses = iter([{"status": "unknown"}, {"status": "unknown"}, {"status": "completed", "output": "recovered"}])
    polls = []

    def poll(base_url, api_key, run_id):
        polls.append(run_id)
        return {"run_id": run_id, **next(statuses)}

    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", poll)
    assert gateway_chat.resume_gateway_runs_after_restart() == [sid]
    _wait_for_reattach_threads()

    assert len(polls) == 3, "unknown is nonterminal: the run is polled until the Gateway settles it"
    saved = _saved(sid)
    assert saved["messages"][-1]["content"] == "recovered"
    assert not saved["messages"][-1].get("_error")
    assert saved["active_stream_id"] is None and saved["gateway_run"] is None


def test_unknown_is_not_a_terminal_gateway_run_status():
    assert "unknown" not in gateway_chat._GATEWAY_RUN_TERMINAL_STATUSES


@pytest.mark.parametrize("code,error_code", [(409, "unknown_execution"), (409, "session_busy"), (500, "boom")])
def test_stop_is_not_acknowledged_when_the_gateway_refuses_it(code, error_code):
    class _Opener:
        def open(self, req, *, timeout=None):
            body = json.dumps({"error": {"code": error_code}}).encode()
            raise urllib.error.HTTPError(req.full_url, code, "conflict", Message(), io.BytesIO(body))

    with patch("urllib.request.build_opener", return_value=_Opener()), \
         patch("api.gateway_chat.gateway_run_endpoint", return_value=("http://gw:8642", "k")):
        assert gateway_chat.stop_gateway_run("run-dead") is False
