"""Gateway reattach must keep the browser live: stream the run event feed after a restart.

_Await_gateway_run_result used to poll run status every 2s and forward only approvals and the
terminal result — after a WebUI restart the reattached browser received no live events and the
run journal stopped growing until the run finished. Now the reattach path consumes the same
gateway event feed as a normal run (whose buffered events also backfill the restart gap) and
only falls back to status polling when the feed is unavailable or ends mid-run.
"""
import json
import threading
import urllib.error

from api import gateway_chat


class _FakeResp:
    def __init__(self, lines):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sse(event, payload):
    return [f"event: {event}".encode(), f"data: {json.dumps(payload)}".encode(), b""]


def _install_stream_state(monkeypatch, stream_id):
    monkeypatch.setattr(gateway_chat, "STREAM_LIVE_TOOL_CALLS", {stream_id: []})
    monkeypatch.setattr(gateway_chat, "STREAM_REASONING_TEXT", {stream_id: ""})
    monkeypatch.setattr(gateway_chat, "STREAM_PARTIAL_TEXT", {stream_id: ""})
    monkeypatch.setattr(gateway_chat, "update_active_run", lambda *a, **k: None)


def _reattach(stream_id, put, run_id="run-r1"):
    # gateway_chat calls put_gateway_event(name, data); adapt to a collector.
    sink = put if callable(getattr(put, "__code__", None)) and put.__code__.co_argcount >= 2 else (
        lambda name, data=None: put((name, data))
    )
    return gateway_chat._await_gateway_run_result(
        "sess-1", stream_id, run_id, "http://gw.invalid", "key",
        put_gateway_event=sink, cancel_event=threading.Event(),
    )


def test_reattach_streams_events_and_returns_final(monkeypatch):
    stream_id = "st-live"
    _install_stream_state(monkeypatch, stream_id)
    lines = (
        _sse("run.started", {"run_id": "run-r1"})
        + _sse("tool.started", {"tool": "terminal", "args": {"cmd": "ls"}})
        + _sse("tool.completed", {"tool": "terminal", "status": "completed", "label": "ls"})
        + _sse("message.delta", {"delta": "Hallo "})
        + _sse("run.completed", {"output": "Hallo Welt"})
    )
    monkeypatch.setattr(
        gateway_chat.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp(lines),
    )
    events = []
    result = _reattach(stream_id, events.append)

    names = [name for name, _ in events]
    assert "token" in names, f"delta must reach the browser queue: {names}"
    assert any(name == "tool" for name, _ in events)
    tool_rows = gateway_chat.STREAM_LIVE_TOOL_CALLS[stream_id]
    assert tool_rows and tool_rows[0]["name"] == "terminal" and tool_rows[0]["done"] is True
    # Terminal frame settles the turn on the streamed contract (same as a normal run).
    assert result is not None
    assert isinstance(result, tuple)


def test_reattach_falls_back_to_polling_when_feed_unavailable(monkeypatch):
    stream_id = "st-404"
    _install_stream_state(monkeypatch, stream_id)

    def raise_404(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "no stream", {}, None)

    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", raise_404)
    monkeypatch.setattr(
        gateway_chat, "_get_gateway_run_status",
        lambda base_url, api_key, run_id: {"status": "completed", "output": "fertig"},
    )
    events = []
    result = _reattach(stream_id, events.append)
    assert result == ("fertig", {}), "polling fallback must still settle the run"


def test_reattach_feed_end_midrun_falls_back_to_polling(monkeypatch):
    stream_id = "st-midrun"
    _install_stream_state(monkeypatch, stream_id)
    # Feed ends (connection drop) while the run is still live: no terminal frame.
    lines = _sse("tool.started", {"tool": "terminal", "args": {}})
    monkeypatch.setattr(
        gateway_chat.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp(lines),
    )
    statuses = iter([
        {"status": "running"},                     # end-of-feed liveness check
        {"status": "running", "last_event": "tool.started"},
        {"status": "completed", "output": "spaeter fertig"},
    ])
    monkeypatch.setattr(
        gateway_chat, "_get_gateway_run_status",
        lambda base_url, api_key, run_id: next(statuses),
    )
    events = []
    result = _reattach(stream_id, events.append)
    assert result == ("spaeter fertig", {}), "mid-run feed end must poll, never fake a finish"
    assert events, "events seen before the drop must still be forwarded"


def test_reattach_relays_approval_requests(monkeypatch):
    stream_id = "st-approval"
    _install_stream_state(monkeypatch, stream_id)
    payload = {"approval_id": "ap-1", "tool_name": "terminal"}
    lines = (
        _sse("approval.request", payload)
        + _sse("run.completed", {"output": "ok"})
    )
    monkeypatch.setattr(
        gateway_chat.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp(lines),
    )
    relayed = []
    monkeypatch.setattr(
        gateway_chat, "_relay_gateway_run_approval",
        lambda session_id, run_id, approval, base_url, api_key, **kw: relayed.append(approval),
    )
    _reattach(stream_id, lambda name, data=None: None)
    assert relayed == [payload], "approval prompts must surface on a reattached run"
