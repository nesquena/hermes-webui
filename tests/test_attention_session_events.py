"""Attention badges must refresh the session sidebar immediately.

Approval/clarify queues already notify the per-session card SSE stream.  The
sidebar, however, listens to the global session-events stream and then reloads
``/api/sessions``.  Without publishing on queue transitions, the badge and
attention sound only appear after the user opens the affected thread.
"""


def test_clarify_pending_and_resolved_publish_session_list_changed(monkeypatch):
    import api.clarify as clarify

    events = []
    monkeypatch.setattr(
        clarify,
        "publish_session_list_changed",
        lambda reason: events.append(reason),
    )
    sid = "test-attention-clarify"

    with clarify._lock:
        clarify._pending.pop(sid, None)
        clarify._gateway_queues.pop(sid, None)
        clarify._gateway_notify_cbs.pop(sid, None)

    try:
        entry = clarify.submit_pending(sid, {"question": "Need input?"})
        assert events == ["attention_pending"]
        pending = clarify.get_pending(sid)
        assert pending is not None
        assert pending["clarify_id"] == entry.clarify_id

        assert clarify.resolve_clarify_by_id(sid, entry.clarify_id, "yes") is True
        assert events == ["attention_pending", "attention_resolved"]
        assert clarify.get_pending(sid) is None
    finally:
        with clarify._lock:
            clarify._pending.pop(sid, None)
            clarify._gateway_queues.pop(sid, None)
            clarify._gateway_notify_cbs.pop(sid, None)


def test_approval_pending_and_resolved_publish_session_list_changed(monkeypatch):
    import api.routes as routes
    import api.route_approvals as route_approvals

    events = []
    capture = lambda reason: events.append(reason)
    monkeypatch.setattr(route_approvals, "publish_session_list_changed", capture)
    monkeypatch.setattr(routes, "publish_session_list_changed", capture)
    sid = "test-attention-approval"

    with routes._lock:
        routes._pending.pop(sid, None)
        routes._gateway_queues.pop(sid, None)

    try:
        routes.submit_pending(sid, {"command": "echo ok", "pattern_key": "test-key"})
        assert events == ["attention_pending"]

        ok = routes._resolve_approval_legacy(sid, "", "deny")
        assert ok is True
        assert events == ["attention_pending", "attention_resolved"]
    finally:
        with routes._lock:
            routes._pending.pop(sid, None)
            routes._gateway_queues.pop(sid, None)
