"""Gateway runs-API turns keep their tool calls/results in the replayed model context.

``/v1/runs`` reports only the final text, so the WebUI used to store just the user and
assistant rows for a gateway turn and every later turn replayed a transcript with the tool
calls cut out. The WebUI stays authoritative for history (edit/retry/fork/rewind, #4362) --
it does not omit it as #7930 opts into -- so it carries the tool rows itself: after a turn it
reads them back from the gateway's session store and replays them with the next request.
"""

import http.server
import json
import threading
from collections import OrderedDict
from unittest.mock import MagicMock, patch

import pytest

import api.gateway_chat as gateway_chat
import api.models as models
from api.config import STREAMS, STREAMS_LOCK
from api.models import new_session

CALL = {"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": "{\"command\": \"ls\"}"}}


class _Run:
    """Stops ``_run_gateway_runs_api_streaming`` right after the request body is built."""

    def __init__(self):
        self.body = None

    def on_run_id(self, run_id, request=None):
        self.body = request
        raise RuntimeError("stop before network")


def _history_sent(context_messages):
    run = _Run()
    session = MagicMock(context_messages=context_messages)
    with pytest.raises(RuntimeError):
        gateway_chat._run_gateway_runs_api_streaming(
            "sid", "next question", "m", "/w", "stream", "http://127.0.0.1:1", "k", [], {},
            put_gateway_event=lambda *a, **k: None, cancel_event=threading.Event(),
            session=session, on_run_id=run.on_run_id,
        )
    return run.body["conversation_history"]


def test_history_builder_replays_tool_rows():
    sent = _history_sent([
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": None, "tool_calls": [CALL]},
        {"role": "tool", "content": "a.txt", "tool_call_id": "call_1", "tool_name": "terminal"},
        {"role": "assistant", "content": "a.txt is there"},
    ])

    assert sent == [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": "", "tool_calls": [CALL]},
        {"role": "tool", "content": "a.txt", "tool_call_id": "call_1", "tool_name": "terminal"},
        {"role": "assistant", "content": "a.txt is there"},
    ]


def test_history_builder_drops_unpaired_tool_rows():
    sent = _history_sent([
        {"role": "user", "content": "hi"},
        {"role": "tool", "content": "orphan", "tool_call_id": "zzz"},
        {"role": "assistant", "content": "", "tool_calls": [CALL, {**CALL, "id": "call_2"}]},
        {"role": "tool", "content": "files", "tool_call_id": "call_1"},
        {"role": "assistant", "content": "done"},
        {"role": "assistant", "content": "", "tool_calls": [{**CALL, "id": "call_9"}]},
        {"role": "user", "content": "next"},
    ])

    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "assistant", "user"]
    assert [c["id"] for c in sent[1]["tool_calls"]] == ["call_1"]


def test_history_builder_plain_history_is_unchanged():
    sent = _history_sent([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])

    assert sent == [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]


class _SessionMessages(http.server.BaseHTTPRequestHandler):
    payload: dict = {}
    seen: tuple = ()

    def do_GET(self):
        type(self).seen = (self.path, self.headers.get("Authorization"))
        body = json.dumps(type(self).payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def gateway_messages():
    """A gateway whose ``/api/sessions/{id}/messages`` answers with a configurable payload."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _SessionMessages)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, _SessionMessages
    server.shutdown()


def test_fetch_keeps_only_the_finished_turns_tool_rows(gateway_messages):
    server, handler = gateway_messages
    handler.payload = {"data": [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old reply"},
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": None, "tool_calls": [CALL]},
        {"role": "tool", "content": "a.txt", "tool_call_id": "call_1", "tool_name": "terminal"},
        {"role": "assistant", "content": "a.txt is there"},
    ]}

    rows = gateway_chat._fetch_gateway_turn_tool_rows(f"http://127.0.0.1:{server.server_port}", "secret", "a b/c")

    assert [m["role"] for m in rows] == ["assistant", "tool"]
    assert rows[0]["content"] == "" and rows[1]["tool_name"] == "terminal"
    assert handler.seen == ("/api/sessions/a%20b%2Fc/messages?order=latest&limit=200", "Bearer secret")


def test_fetch_is_best_effort(gateway_messages):
    server, handler = gateway_messages
    assert gateway_chat._fetch_gateway_turn_tool_rows("http://127.0.0.1:1", "k", "s") == []
    handler.payload = {"data": [{"role": "assistant", "content": "x", "tool_calls": [CALL]}]}  # no user row

    assert gateway_chat._fetch_gateway_turn_tool_rows(f"http://127.0.0.1:{server.server_port}", "k", "s") == []


def _runs_api_turn(session, stream_id, text, tool_rows, bodies, tmp_path):
    """Drive one runs-API turn end to end against a fake gateway."""
    def fake_urlopen(req, *, timeout=None):
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        url = req.full_url
        if url.endswith("/v1/runs"):
            bodies.append(json.loads(req.data.decode("utf-8")))
            resp.read = lambda sz=65536: b'{"run_id":"run_1"}'
        elif "/api/sessions/" in url:
            resp.read = lambda sz=-1: json.dumps({"data": tool_rows}).encode()
        else:
            resp.__iter__ = lambda s: iter([
                b'data: {"event":"run.completed","output":"answer"}\n',
            ])
        return resp

    session.active_stream_id = stream_id
    session.pending_user_message = text
    session.pending_attachments = []
    session.save()
    queue = MagicMock()
    with STREAMS_LOCK:
        STREAMS[stream_id] = queue
    try:
        with patch.dict("os.environ", {
            "HERMES_WEBUI_CHAT_BACKEND": "gateway",
            "HERMES_WEBUI_GATEWAY_USE_RUNS_API": "1",
            "HERMES_WEBUI_GATEWAY_BASE_URL": "http://gateway.local",
        }, clear=True), \
             patch("api.gateway_chat.gateway_supports_approval", return_value=True), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            gateway_chat._run_gateway_chat_streaming(
                session_id=session.session_id, msg_text=text, model="test", workspace=str(tmp_path),
                stream_id=stream_id,
            )
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
    return models.get_session(session.session_id)


def test_tool_rows_reach_the_next_request_but_not_the_visible_transcript(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    bodies: list = []
    session = new_session()

    first_turn_rows = [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": None, "tool_calls": [CALL]},
        {"role": "tool", "content": "a.txt", "tool_call_id": "call_1", "tool_name": "terminal"},
        {"role": "assistant", "content": "answer"},
    ]
    saved = _runs_api_turn(session, "stream-tool-1", "list files", first_turn_rows, bodies, tmp_path)
    saved = _runs_api_turn(saved, "stream-tool-2", "what was in it?", [], bodies, tmp_path)

    replayed = bodies[1]["conversation_history"]
    assert [m["role"] for m in replayed] == ["user", "assistant", "tool", "assistant"]
    assert replayed[1]["tool_calls"][0]["id"] == "call_1"
    assert replayed[2]["tool_call_id"] == "call_1"
    assert [m["role"] for m in saved.messages] == ["user", "assistant", "user", "assistant"]
