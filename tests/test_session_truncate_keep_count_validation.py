"""Regression: POST /api/session/truncate must validate keep_count before it
reaches the destructive ``s.messages[:keep]`` slice.

keep_count was parsed with a bare ``int()`` and used directly to slice messages,
then persisted via ``s.save()``:

  * A non-numeric keep_count (e.g. "abc") raised ValueError -> generic HTTP 500.
  * A NEGATIVE keep_count sliced as ``messages[:-N]``, silently DELETING the most
    recent N messages instead of keeping N. keep_count=-5 on a 3-message session
    wiped the entire transcript — and the wipe was saved to disk.

The sibling /api/session/branch handler already guards its own keep_count
(reject non-int -> 400, reject negative -> 400). truncate is *destructive*, so
the missing guard is worse there. This test reproduces both failure modes
through the real handle_post route, mirroring tests/test_issue2914_*.
"""

from __future__ import annotations

import io
import json
from io import BytesIO
from types import SimpleNamespace


class _JSONHandler:
    """Captures a JSON response through the real send_response/wfile path so
    both j() (success) and bad() (error, via helpers.j) are observable."""

    def __init__(self, body_bytes: bytes):
        self.status = None
        self.response_headers = []
        self.rfile = BytesIO(body_bytes)
        self.headers = {"Content-Length": str(len(body_bytes))}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    def payload(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _msg(role: str, content: str, ts: float, mid: str) -> dict:
    return {"id": mid, "role": role, "content": content, "timestamp": ts}


def _make_session(monkeypatch, tmp_path, sid):
    import api.models as models
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    msgs = [
        _msg("user", "first", 1.0, "u1"),
        _msg("assistant", "reply first", 2.0, "a1"),
        _msg("user", "second", 3.0, "u2"),
    ]
    session = Session(
        session_id=sid,
        messages=list(msgs),
        context_messages=list(msgs),
    )
    session.save()
    return Session


def _post_truncate(monkeypatch, sid, keep_count):
    import api.routes as routes

    body_bytes = json.dumps({"session_id": sid, "keep_count": keep_count}).encode()
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)

    handler = _JSONHandler(body_bytes)
    routes.handle_post(handler, SimpleNamespace(path="/api/session/truncate"))
    return {"status": handler.status, "payload": handler.payload()}


def test_truncate_negative_keep_count_rejected_and_not_destructive(monkeypatch, tmp_path):
    Session = _make_session(monkeypatch, tmp_path, "trunc_neg")

    # keep_count=-5 on a 3-message session: an unclamped messages[:-5] slice
    # wipes the whole transcript and persists []. Must be rejected with 400.
    captured = _post_truncate(monkeypatch, "trunc_neg", -5)

    assert captured["status"] == 400
    assert "non-negative" in captured["payload"]["error"]

    # The on-disk transcript must be untouched (no data loss).
    loaded = Session.load("trunc_neg")
    assert loaded is not None
    assert [m["content"] for m in loaded.messages] == ["first", "reply first", "second"]


def test_truncate_non_numeric_keep_count_does_not_500(monkeypatch, tmp_path):
    Session = _make_session(monkeypatch, tmp_path, "trunc_nan")

    # Before the fix this raised ValueError -> 500.
    captured = _post_truncate(monkeypatch, "trunc_nan", "abc")

    assert captured["status"] == 400
    assert "integer" in captured["payload"]["error"]

    loaded = Session.load("trunc_nan")
    assert loaded is not None
    assert [m["content"] for m in loaded.messages] == ["first", "reply first", "second"]


def test_truncate_valid_keep_count_still_truncates(monkeypatch, tmp_path):
    Session = _make_session(monkeypatch, tmp_path, "trunc_ok")

    captured = _post_truncate(monkeypatch, "trunc_ok", 2)

    assert captured["status"] == 200
    assert captured["payload"].get("ok") is True

    loaded = Session.load("trunc_ok")
    assert loaded is not None
    assert [m["content"] for m in loaded.messages] == ["first", "reply first"]
    assert [m["content"] for m in loaded.context_messages] == ["first", "reply first"]


def test_truncate_zero_keep_count_clears_transcript(monkeypatch, tmp_path):
    # keep_count=0 is a legitimate "clear all messages" request and must still
    # work (this is how Edit-from-first-message is implemented).
    Session = _make_session(monkeypatch, tmp_path, "trunc_zero")

    captured = _post_truncate(monkeypatch, "trunc_zero", 0)

    assert captured["status"] == 200
    loaded = Session.load("trunc_zero")
    assert loaded is not None
    assert loaded.messages == []
