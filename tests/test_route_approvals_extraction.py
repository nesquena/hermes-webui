"""Import-resolution and identity tests for the route_approvals extraction.

Verifies that api.route_approvals is self-contained and that api.routes
re-exports the same live objects (not copies) so existing callers are
unaffected by the move.
"""

def test_route_approvals_imports():
    from api.route_approvals import submit_pending, _approval_sse_subscribers
    assert callable(submit_pending)
    assert isinstance(_approval_sse_subscribers, dict)


def test_backward_compat_imports():
    from api.routes import submit_pending
    assert callable(submit_pending)


def test_identity():
    from api.route_approvals import _approval_sse_subscribers as a
    from api.routes import _approval_sse_subscribers as b
    assert a is b, "routes.py must re-export the same object, not a copy"


def test_pending_identity():
    """_pending dict must be the same object in both modules."""
    from api.route_approvals import _pending as a
    from api.routes import _pending as b
    assert a is b, "routes.py must re-export the same _pending object"


def test_lock_identity():
    """_lock must be the same object in both modules."""
    from api.route_approvals import _lock as a
    from api.routes import _lock as b
    assert a is b, "routes.py must re-export the same _lock object"


def test_sse_helpers_importable_from_route_approvals():
    """All SSE helpers must be importable directly from route_approvals."""
    from api.route_approvals import (
        _approval_sse_subscribe,
        _approval_sse_unsubscribe,
        _approval_sse_notify_locked,
        _approval_sse_notify,
    )
    assert callable(_approval_sse_subscribe)
    assert callable(_approval_sse_unsubscribe)
    assert callable(_approval_sse_notify_locked)
    assert callable(_approval_sse_notify)


def test_sse_helpers_backward_compat():
    """SSE helpers must still be importable from routes for backward compat."""
    from api.routes import (
        _approval_sse_subscribe,
        _approval_sse_unsubscribe,
        _approval_sse_notify_locked,
        _approval_sse_notify,
    )
    assert callable(_approval_sse_subscribe)
    assert callable(_approval_sse_unsubscribe)
    assert callable(_approval_sse_notify_locked)
    assert callable(_approval_sse_notify)


def test_sse_helper_identity():
    """SSE helpers imported from routes must be the same callables from route_approvals."""
    import api.route_approvals as ra
    import api.routes as r
    assert ra._approval_sse_subscribe is r._approval_sse_subscribe
    assert ra._approval_sse_unsubscribe is r._approval_sse_unsubscribe
    assert ra._approval_sse_notify_locked is r._approval_sse_notify_locked
    assert ra._approval_sse_notify is r._approval_sse_notify
    assert ra.submit_pending is r.submit_pending


def test_no_circular_import():
    """route_approvals must not import from api.routes (no circular dep)."""
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / "api" / "route_approvals.py").read_text()
    assert "from api.routes" not in src, "route_approvals.py must not import from api.routes"
    assert "import api.routes" not in src, "route_approvals.py must not import api.routes"
