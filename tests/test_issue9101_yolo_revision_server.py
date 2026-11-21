"""#9101 follow-up: the server's per-session YOLO revision.

GET/POST /api/session/yolo answer {yolo_enabled, yolo_revision, yolo_epoch}.
A POST must carry a boolean `enabled`; `expected_revision` makes it
conditional, so a stale /yolo status read cannot undo a later change. An
explicit enable still answers every parked approval "once". Nothing is pushed
on the session streams: no client reads YOLO frames yet, so `initial` stays
master's `{session_id}` and a change emits no frame (#9101 follow-up).
"""

import json
import threading
import urllib.parse
from types import SimpleNamespace

import pytest

import api.background_process as background_process
import api.routes as routes
from api import route_approvals

try:
    from tools.approval import disable_session_yolo, is_session_yolo_enabled

    APPROVAL_AVAILABLE = True
except ImportError:
    APPROVAL_AVAILABLE = False

pytestmark = pytest.mark.skipif(not APPROVAL_AVAILABLE, reason="tools.approval unavailable")


@pytest.fixture
def harness(monkeypatch):
    """Drive the real GET/POST routes; capture responses and any stream frame."""
    frames = []
    sent = {}
    sids = []

    def fake_j(_handler, payload, status=200, extra_headers=None, **_kw):
        sent.update(payload=payload, status=status)
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "bad", lambda _h, msg, status=400: fake_j(_h, {"error": msg}, status))
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)
    monkeypatch.setattr(
        background_process,
        "_emit_to_session_streams",
        lambda sid, event, data: frames.append((sid, event, dict(data))) or 1,
    )

    def post(body):
        sids.append(body.get("session_id"))
        monkeypatch.setattr(routes, "read_body", lambda _handler: body)
        sent.clear()
        routes.handle_post(object(), urllib.parse.urlparse("/api/session/yolo"))
        return sent["status"], sent["payload"]

    def get(sid):
        sids.append(sid)
        sent.clear()
        routes.handle_get(object(), urllib.parse.urlparse("/api/session/yolo?session_id=" + sid))
        return sent["status"], sent["payload"]

    yield SimpleNamespace(post=post, get=get, frames=frames)
    for sid in set(filter(None, sids)):
        disable_session_yolo(sid)
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)


def _versioned(payload):
    return (
        isinstance(payload.get("yolo_enabled"), bool)
        and isinstance(payload.get("yolo_revision"), int)
        and isinstance(payload.get("yolo_epoch"), str)
        and payload["yolo_epoch"]
    )


def test_revision_on_get_and_post(harness):
    """Item 1: answers carry a revision that moves with each change."""
    sid = "webui-9101-rev-basic"
    status, before = harness.get(sid)
    assert status == 200 and _versioned(before), before
    status, on = harness.post({"session_id": sid, "enabled": True})
    assert status == 200 and on["yolo_enabled"] is True and _versioned(on), on
    assert on["yolo_revision"] > before["yolo_revision"]
    assert harness.get(sid)[1] == {k: on[k] for k in ("yolo_enabled", "yolo_revision", "yolo_epoch")}
    status, off = harness.post({"session_id": sid, "enabled": False})
    assert status == 200 and off["yolo_enabled"] is False and off["yolo_revision"] > on["yolo_revision"]
    # Repeating the same state moves nothing.
    assert harness.post({"session_id": sid, "enabled": False})[1]["yolo_revision"] == off["yolo_revision"]


def test_yolo_changes_push_no_session_stream_frames(monkeypatch, harness):
    """No client reads YOLO frames yet, so the server sends none: a change (POST
    enable, POST disable, a change outside WebUI seen by a read) emits nothing on
    the session's streams, and the session stream's `initial` frame is master's
    `{session_id}` even while YOLO is on."""
    from tools.approval import enable_session_yolo

    sid = "webui-9101-rev-no-frames"
    _, on = harness.post({"session_id": sid, "enabled": True})
    _, off = harness.post({"session_id": sid, "enabled": False})
    enable_session_yolo(sid)  # outside WebUI's writers (Agent/gateway side)
    _, seen = harness.get(sid)
    assert on["yolo_revision"] < off["yolo_revision"] < seen["yolo_revision"] and seen["yolo_enabled"] is True
    assert [(e, d) for s, e, d in harness.frames if s == sid] == []

    # A session stream (re)connecting while YOLO is on gets master's `initial`.
    monkeypatch.setattr(routes, "_SSE_SUBSCRIBER_LEASE_SECONDS", 0.04, raising=False)
    monkeypatch.setattr(routes, "_SSE_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda handler: None)
    monkeypatch.setattr(background_process, "active_stream_id_for_session", lambda _sid: None)
    monkeypatch.setattr(background_process, "persisted_message_count_for_session", lambda _sid: None)
    monkeypatch.setattr(background_process, "should_emit_session_updated", lambda known, current: False)

    class Writer:
        def __init__(self):
            self.body = bytearray()

        def write(self, data):
            self.body.extend(data)
            return len(data)

        def flush(self):
            pass

    handler = SimpleNamespace(
        headers={}, wfile=Writer(), close_connection=False,
        send_response=lambda status: None, send_header=lambda k, v: None, end_headers=lambda: None,
    )
    done = threading.Event()
    threading.Thread(
        target=lambda: (routes._handle_session_sse_stream(
            handler, urllib.parse.urlparse("/api/session/stream?session_id=" + sid)), done.set()),
        daemon=True,
    ).start()
    assert done.wait(2.0), "session stream did not end within its lease"
    text = handler.wfile.body.decode("utf-8")
    block = text[text.index("event: initial"):].split("\n\n", 1)[0]
    data = json.loads(next(line[6:] for line in block.splitlines() if line.startswith("data: ")))
    assert route_approvals.session_yolo_state(sid)["yolo_enabled"] is True
    assert data == {"session_id": sid}
    assert [(e, d) for s, e, d in harness.frames if s == sid] == []


@pytest.mark.parametrize("body", [{}, {"enabled": None}, {"enabled": "false"}, {"enabled": 1}])
def test_post_requires_boolean_enabled(harness, body):
    """Item 2: a POST without a boolean `enabled` is refused and changes nothing."""
    sid = "webui-9101-rev-missing-enabled"
    status, payload = harness.post({"session_id": sid, **body})
    assert status == 400, payload
    assert "enabled" in payload["error"]
    assert is_session_yolo_enabled(sid) is False
    assert not [1 for s, *_ in harness.frames if s == sid]


def test_explicit_enable_still_answers_parked_approvals(harness):
    """Item 3: an explicit /yolo enable (conditional on its read) still
    answers every parked approval "once", and reports the new revision."""
    from tools.approval import _ApprovalEntry

    sid = "webui-9101-rev-parked"
    approvals = [{"approval_id": "ap-1", "command": "first"}, {"approval_id": "ap-2", "command": "second"}]
    entries = [_ApprovalEntry(dict(a)) for a in approvals]
    with routes._lock:
        routes._pending[sid] = [dict(a) for a in approvals]
        routes._gateway_queues[sid] = list(entries)
    _, read = harness.get(sid)
    status, payload = harness.post({
        "session_id": sid, "enabled": True,
        "expected_revision": read["yolo_revision"], "expected_epoch": read["yolo_epoch"],
    })
    assert status == 200 and payload["ok"] is True and payload["yolo_enabled"] is True, payload
    assert payload["yolo_revision"] > read["yolo_revision"]
    assert [e.result for e in entries] == ["once", "once"]
    assert all(e.event.is_set() for e in entries)


@pytest.mark.parametrize("enable", [True, False])
def test_stale_expected_revision_is_refused(harness, enable):
    """Item 7: a /yolo toggle computed from a read that a later change (the
    pill, another tab) made stale changes nothing and returns the state."""
    sid = f"webui-9101-rev-stale-{enable}"
    _, read = harness.get(sid)
    harness.post({"session_id": sid, "enabled": True})
    harness.post({"session_id": sid, "enabled": False})  # the later pill disable
    if not enable:
        harness.post({"session_id": sid, "enabled": True})
    _, current = harness.get(sid)
    status, payload = harness.post({
        "session_id": sid, "enabled": enable,
        "expected_revision": read["yolo_revision"], "expected_epoch": read["yolo_epoch"],
    })
    assert status == 409, payload
    assert payload["code"] == "yolo_changed" and payload["ok"] is False
    assert {k: payload[k] for k in current} == current
    assert is_session_yolo_enabled(sid) is current["yolo_enabled"]
    # A different epoch (the server restarted since the read) is stale too.
    status, payload = harness.post({
        "session_id": sid, "enabled": enable,
        "expected_revision": current["yolo_revision"], "expected_epoch": "another-boot",
    })
    assert status == 409 and payload["code"] == "yolo_changed"
    # The current revision is accepted.
    status, payload = harness.post({
        "session_id": sid, "enabled": enable,
        "expected_revision": current["yolo_revision"], "expected_epoch": current["yolo_epoch"],
    })
    assert status == 200 and payload["yolo_enabled"] is enable


def test_writes_take_their_revision_under_the_write_lock(harness):
    """Item 7: each writer stamps the revision inside the lock it writes under,
    so two writes that land before any answer reads the flag (ON, then OFF:
    back to the value a /yolo read saw) still make that read stale."""
    sid = "webui-9101-rev-writer-stamps"
    _, read = harness.get(sid)
    route_approvals.set_session_yolo_enabled(sid, True)
    route_approvals.set_session_yolo_enabled(sid, False)
    expected = {"expected_revision": read["yolo_revision"], "expected_epoch": read["yolo_epoch"]}
    status, payload = harness.post({"session_id": sid, "enabled": True, **expected})
    assert status == 409 and payload["code"] == "yolo_changed", payload
    # The same through the approval relay's transition (enable on success).
    _, read = harness.get(sid)
    token = route_approvals.begin_session_yolo_transition(sid)
    route_approvals.finish_session_yolo_transition(sid, token, succeeded=True)
    route_approvals.set_session_yolo_enabled(sid, False)
    expected = {"expected_revision": read["yolo_revision"], "expected_epoch": read["yolo_epoch"]}
    status, payload = harness.post({"session_id": sid, "enabled": True, **expected})
    assert status == 409 and payload["code"] == "yolo_changed", payload
    assert is_session_yolo_enabled(sid) is False
