"""Contract tests for POST /api/session/mark-done (structural `done_at`).

The endpoint is the sanctioned writer for the "this conversation is finished"
flag: the sidebar reads `done_at` off the session row instead of inferring
"done" from a ✅ title prefix plus silence.

Contracts exercised here, against the live test server:
- a successful call stamps `done_at` on the sidebar row (`GET /api/sessions`);
- `done: false` clears it again (un-mark);
- `done_at` can be supplied explicitly (used by the history backfill);
- an unknown session id answers 404, not 500;
- a non-boolean `done` is rejected;
- `done_at` does not touch `archived` — workflow state, not storage.

Sidebar rows are only built for sessions that carry at least one message (an
empty session is pruned from the list by design), so each case seeds a real
sidecar with one message in the test server's session dir.
"""

import json
import pathlib
import sys
import time
import uuid
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from conftest import TEST_BASE, TEST_STATE_DIR  # noqa: E402

SESSION_DIR = TEST_STATE_DIR / "sessions"


def _post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        TEST_BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read()), e.code
        except Exception:
            return {}, e.code


def _get(path):
    with urllib.request.urlopen(TEST_BASE + path, timeout=10) as r:
        return json.loads(r.read())


def _seed_session(title="Mark Done Test"):
    """Write a one-message sidecar plus its `_index.json` entry.

    The sidebar list is built from `_index.json` (a cache rebuilt from the
    sidecar dir), so a sidecar alone is not enough for the row to appear with
    a settled index on disk.
    """
    sid = f"markdone-{uuid.uuid4().hex[:10]}"
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    payload = {
        "session_id": sid,
        "title": title,
        "workspace": str(TEST_STATE_DIR / "test-workspace"),
        "model": "gpt-test",
        "model_provider": "openai",
        "created_at": now,
        "updated_at": now,
        "pinned": False,
        "archived": False,
        "profile": "default",
        "messages": [
            {"role": "user", "content": "hello", "timestamp": now},
            {"role": "assistant", "content": "world", "timestamp": now},
        ],
        "tool_calls": [],
        "message_count": 2,
        "last_message_at": now,
    }
    (SESSION_DIR / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")

    index_path = SESSION_DIR / "_index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(index, list):
            index = []
    except Exception:
        index = []
    index.append(dict(payload))
    index_path.write_text(json.dumps(index), encoding="utf-8")
    return sid


def _sidebar_row(sid):
    payload = _get("/api/sessions")
    rows = payload.get("sessions") or payload.get("items") or []
    for row in rows:
        if row.get("session_id") == sid:
            return row
    raise AssertionError(
        f"session {sid} not in /api/sessions (n_rows={len(rows)}, "
        f"ids={[r.get('session_id') for r in rows[:5]]})"
    )


def test_mark_done_stamps_done_at():
    sid = _seed_session()
    body, status = _post("/api/session/mark-done", {"session_id": sid})
    assert status == 200, body
    assert body["session"]["done_at"] is not None

    row = _sidebar_row(sid)
    assert row.get("done_at") == body["session"]["done_at"]


def test_mark_done_accepts_explicit_timestamp():
    sid = _seed_session()
    body, status = _post("/api/session/mark-done", {"session_id": sid, "done_at": 1234.5})
    assert status == 200, body
    assert body["session"]["done_at"] == 1234.5

    row = _sidebar_row(sid)
    assert row.get("done_at") == 1234.5


def test_unmark_clears_done_at():
    sid = _seed_session()
    _post("/api/session/mark-done", {"session_id": sid})
    body, status = _post("/api/session/mark-done", {"session_id": sid, "done": False})
    assert status == 200, body
    assert body["session"]["done_at"] is None

    row = _sidebar_row(sid)
    assert not row.get("done_at")


def test_mark_done_does_not_archive():
    sid = _seed_session()
    body, status = _post("/api/session/mark-done", {"session_id": sid})
    assert status == 200, body
    assert body["session"]["done_at"] is not None
    assert body["session"]["archived"] is False


def test_mark_done_unknown_session_is_404():
    body, status = _post("/api/session/mark-done", {"session_id": "no-such-session-xyz123"})
    assert status == 404, (status, body)


def test_mark_done_rejects_non_boolean_done():
    sid = _seed_session()
    body, status = _post("/api/session/mark-done", {"session_id": sid, "done": "yes"})
    assert status == 400, (status, body)


def test_mark_done_requires_session_id():
    body, status = _post("/api/session/mark-done", {})
    assert status == 400, (status, body)
