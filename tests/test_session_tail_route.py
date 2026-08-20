from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse


def test_initial_bounded_session_load_uses_metadata_and_tail_reader():
    import api.routes as routes
    from tests.test_session_tail_payload import _FakeSession

    session = _FakeSession([
        {"role": "assistant", "content": "tail-a", "timestamp": 10.0},
        {"role": "assistant", "content": "tail-b", "timestamp": 20.0},
    ])
    session.__dict__["_metadata_message_count"] = 1000
    captured = {}
    get_session_calls = []
    state_db_calls = []

    state_db_newer = [
        {"role": "assistant", "content": "tail-a", "timestamp": 10.0},
        {"role": "assistant", "content": "tail-b", "timestamp": 20.0},
        {"role": "assistant", "content": "newer", "timestamp": 30.0},
    ]

    def fake_get_session(sid, metadata_only=False):
        get_session_calls.append((sid, metadata_only))
        return session

    def fake_j(_handler, data, status=200, extra_headers=None):
        captured["session"] = data["session"]
        captured["status"] = status
        return data

    def fake_state_db_reader(sid, **kwargs):
        state_db_calls.append((sid, kwargs))
        return state_db_newer

    with patch("api.routes.get_session", side_effect=fake_get_session), \
         patch("api.routes.read_session_message_tail", return_value=(session.messages, 998)), \
         patch("api.routes._clear_stale_stream_state", return_value=False), \
         patch("api.routes._lookup_cli_session_metadata", return_value={}), \
         patch("api.routes._sidecar_file_exceeds_threshold", return_value=True), \
         patch("api.routes.get_state_db_session_messages", side_effect=fake_state_db_reader), \
         patch("api.routes.redact_session_data", side_effect=lambda raw: raw), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_get(
            SimpleNamespace(),
            urlparse(
                "/api/session?session_id=tail_payload_001&messages=1&resolve_model=0&msg_limit=2"
            ),
        )

    assert get_session_calls == [("tail_payload_001", True)]
    assert [message["content"] for message in captured["session"]["messages"]] == [
        "tail-b",
        "newer",
    ]
    assert captured["session"]["message_count"] == 1001
    assert captured["session"]["_messages_offset"] == 999
    assert state_db_calls == [
        (
            "tail_payload_001",
            {
                "profile": None,
                "since_timestamp": 10.0,
                "limit": routes._STATE_DB_DISPLAY_ROW_BACKSTOP,
            },
        )
    ]
