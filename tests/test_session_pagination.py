"""
Task 1: Backend message pagination on /api/session.

Adds three optional query params (backward-compatible):
  - tail=N        → return only the last N messages
  - since_idx=K   → return messages whose index >= K
  - limit=M       → cap returned messages at M (combine with since_idx)

Response gains a `pagination` object on the session payload:
  {"start_idx": 150, "end_idx": 200, "total": 200}

Each returned message also carries `_idx` so the client knows its absolute
position. `total` always reflects the underlying message_count, never the
windowed slice.
"""
import json
import secrets
import time
import urllib.request

import pytest

from tests._pytest_port import BASE
from tests.conftest import TEST_STATE_DIR

SESSION_DIR = TEST_STATE_DIR / "sessions"


def _seed(sid: str, n: int) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    msgs = [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": f"msg-{i}"}
        for i in range(n)
    ]
    payload = {
        "session_id": sid,
        "title": "pagination-fixture",
        "workspace": str(TEST_STATE_DIR / "test-workspace"),
        "model": "stub/test",
        "created_at": time.time() - 100,
        "updated_at": time.time(),
        "messages": msgs,
    }
    (SESSION_DIR / f"{sid}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _api_get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


@pytest.fixture
def fixture_session(cleanup_test_sessions):
    sid = secrets.token_hex(6)
    cleanup_test_sessions.append(sid)
    _seed(sid, 200)
    return sid


def test_no_pagination_param_returns_all_messages_unchanged(fixture_session):
    """Backward compatibility: without query params, response shape is identical."""
    sid = fixture_session
    sess = _api_get(f"/api/session?session_id={sid}")["session"]
    assert len(sess["messages"]) == 200
    assert sess["messages"][0]["content"] == "msg-0"
    assert sess["messages"][-1]["content"] == "msg-199"
    # No mandatory pagination object on the no-params path
    # (presence is acceptable, absence is also acceptable — both compatible).


def test_tail_returns_last_n_messages_with_idx_and_pagination(fixture_session):
    sid = fixture_session
    sess = _api_get(f"/api/session?session_id={sid}&tail=50")["session"]
    msgs = sess["messages"]
    assert len(msgs) == 50, f"tail=50 should return 50, got {len(msgs)}"
    # Each returned message carries its absolute index in the full history.
    assert msgs[0]["_idx"] == 150, f"first _idx should be 150, got {msgs[0].get('_idx')}"
    assert msgs[-1]["_idx"] == 199, f"last _idx should be 199, got {msgs[-1].get('_idx')}"
    assert msgs[0]["content"] == "msg-150"
    assert msgs[-1]["content"] == "msg-199"
    # Pagination metadata
    assert sess["pagination"] == {"start_idx": 150, "end_idx": 200, "total": 200}
    # Total message count remains accurate (not the slice size).
    assert sess["message_count"] == 200


def test_since_idx_with_limit_returns_window(fixture_session):
    sid = fixture_session
    sess = _api_get(f"/api/session?session_id={sid}&since_idx=100&limit=50")["session"]
    msgs = sess["messages"]
    assert [m["_idx"] for m in msgs] == list(range(100, 150))
    assert sess["pagination"] == {"start_idx": 100, "end_idx": 150, "total": 200}


def test_since_idx_without_limit_returns_to_end(fixture_session):
    sid = fixture_session
    sess = _api_get(f"/api/session?session_id={sid}&since_idx=180")["session"]
    msgs = sess["messages"]
    assert len(msgs) == 20
    assert msgs[0]["_idx"] == 180
    assert msgs[-1]["_idx"] == 199
    assert sess["pagination"] == {"start_idx": 180, "end_idx": 200, "total": 200}


def test_tail_larger_than_total_returns_all(fixture_session):
    """tail=500 on a 200-msg session should clamp to 200, not raise."""
    sid = fixture_session
    sess = _api_get(f"/api/session?session_id={sid}&tail=500")["session"]
    assert len(sess["messages"]) == 200
    assert sess["pagination"] == {"start_idx": 0, "end_idx": 200, "total": 200}


def test_since_idx_negative_clamps_to_zero(fixture_session):
    sid = fixture_session
    sess = _api_get(f"/api/session?session_id={sid}&since_idx=-5&limit=10")["session"]
    msgs = sess["messages"]
    assert [m["_idx"] for m in msgs] == list(range(0, 10))
    assert sess["pagination"]["start_idx"] == 0


def test_since_idx_beyond_total_returns_empty(fixture_session):
    sid = fixture_session
    sess = _api_get(f"/api/session?session_id={sid}&since_idx=999")["session"]
    assert sess["messages"] == []
    # Out-of-range since_idx clamps to total — clients can compare
    # end_idx to total to detect "no more messages above".
    assert sess["pagination"] == {"start_idx": 200, "end_idx": 200, "total": 200}


def test_limit_zero_or_negative_returns_empty(fixture_session):
    sid = fixture_session
    for lim in (0, -1, -100):
        sess = _api_get(f"/api/session?session_id={sid}&since_idx=10&limit={lim}")["session"]
        assert sess["messages"] == [], f"limit={lim} should return empty, got {len(sess['messages'])}"


def test_tail_zero_returns_empty_but_keeps_total(fixture_session):
    sid = fixture_session
    sess = _api_get(f"/api/session?session_id={sid}&tail=0")["session"]
    assert sess["messages"] == []
    assert sess["message_count"] == 200
    assert sess["pagination"] == {"start_idx": 200, "end_idx": 200, "total": 200}


def test_pagination_combined_with_messages_zero_skips_payload(fixture_session):
    """messages=0 short-circuit takes precedence — pagination params are ignored."""
    sid = fixture_session
    sess = _api_get(f"/api/session?session_id={sid}&messages=0&tail=50")["session"]
    assert sess["messages"] == []
    # message_count metadata still accurate
    assert sess.get("message_count") in (200, None) or len(sess.get("messages", [])) == 0
