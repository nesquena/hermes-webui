from pathlib import Path
from types import SimpleNamespace
import io


ROOT = Path(__file__).resolve().parents[1]
ROUTES_SRC = (ROOT / "api" / "routes.py").read_text()


def test_stream_status_exposes_replay_summary():
    status_pos = ROUTES_SRC.index('parsed.path == "/api/chat/stream/status"')
    block = ROUTES_SRC[status_pos : status_pos + 900]

    assert "find_run_summary(stream_id)" in block
    assert '"replay_available"' in block
    assert '"journal"' in block
    assert "_run_journal_status_payload" in block


def test_dead_stream_sse_replays_journal_before_404_fallback():
    handler_pos = ROUTES_SRC.index("def _handle_sse_stream")
    block = ROUTES_SRC[handler_pos : handler_pos + 1800]

    assert "find_run_summary(stream_id)" in block
    assert "stream not found" in block
    assert "_replay_run_journal" in block
    assert "_parse_run_journal_after_seq" in block
    assert 'Content-Type", "text/event-stream; charset=utf-8"' in block


def test_replay_emits_event_ids_and_stale_restart_diagnostic():
    replay_pos = ROUTES_SRC.index("def _replay_run_journal")
    block = ROUTES_SRC[replay_pos : replay_pos + 1200]

    assert "read_run_events" in block
    assert "_sse_with_id" in block
    assert "stale_interrupted_event" in block


def test_session_payload_exposes_runtime_journal_for_stale_streams():
    assert "original_stream_id = getattr(s, \"active_stream_id\", None)" in ROUTES_SRC
    assert '"runtime_journal"' in ROUTES_SRC
    assert 'terminal_state = "stale-from-restart"' in ROUTES_SRC


def test_status_payload_marks_non_terminal_dead_journal_as_stale():
    import api.routes as routes

    payload = routes._run_journal_status_payload(
        {
            "session_id": "session_1",
            "run_id": "run_1",
            "last_seq": 3,
            "last_event_id": "run_1:3",
            "last_event": "token",
            "terminal": False,
            "terminal_state": "running",
        },
        active=False,
    )

    assert payload["terminal"] is False
    assert payload["terminal_state"] == "stale-from-restart"
    assert payload["last_event_id"] == "run_1:3"


def test_status_payload_preserves_terminal_error_state():
    import api.routes as routes

    payload = routes._run_journal_status_payload(
        {
            "session_id": "session_1",
            "run_id": "run_1",
            "terminal": True,
            "terminal_state": "interrupted-by-crash",
            "last_event": "apperror",
        },
        active=False,
    )

    assert payload["terminal"] is True
    assert payload["terminal_state"] == "interrupted-by-crash"


def test_replay_run_journal_writes_replayed_events_and_synthetic_terminal(monkeypatch):
    import api.routes as routes

    handler = SimpleNamespace(wfile=io.BytesIO())
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "terminal": False,
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id, after_seq=None: {
            "events": [
                {
                    "event": "token",
                    "payload": {"text": "hello"},
                    "event_id": f"{run_id}:1",
                }
            ]
        },
    )
    monkeypatch.setattr(
        routes,
        "stale_interrupted_event",
        lambda session_id, run_id, after_seq=None: {
            "event": "apperror",
            "payload": {"type": "interrupted"},
            "event_id": f"{run_id}:2",
        },
    )

    assert routes._replay_run_journal(handler, "run_1", 0) is True
    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_1:1\n" in body
    assert "event: token\n" in body
    assert "id: run_1:2\n" in body
    assert "event: apperror\n" in body


def test_replay_run_journal_honors_after_seq_cursor(monkeypatch):
    import api.routes as routes

    captured = {}
    handler = SimpleNamespace(wfile=io.BytesIO())
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda stream_id: {
            "session_id": "session_1",
            "run_id": stream_id,
            "terminal": True,
        },
    )

    def fake_read_run_events(session_id, run_id, after_seq=None):
        captured["after_seq"] = after_seq
        return {
            "events": [
                {
                    "event": "done",
                    "payload": {"session": {"session_id": session_id}},
                    "event_id": f"{run_id}:4",
                }
            ]
        }

    monkeypatch.setattr(routes, "read_run_events", fake_read_run_events)

    assert routes._replay_run_journal(handler, "run_1", 3) is True
    assert captured["after_seq"] == 3
    body = handler.wfile.getvalue().decode("utf-8")
    assert "id: run_1:4\n" in body
    assert "event: done\n" in body
