from pathlib import Path


ROOT = Path(__file__).parent.parent


def test_cancel_request_includes_explicit_reason():
    boot = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")

    assert "reason=${encodeURIComponent(_reason)}" in boot


def test_cancel_route_logs_sanitized_reason():
    routes = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    start = routes.index('if parsed.path == "/api/chat/cancel":')
    end = routes.index('if parsed.path == "/api/chat/stream":', start)
    cancel_route = routes[start:end]

    assert 'get("reason"' in cancel_route
    assert "cancel_reason" in cancel_route
    assert "Cancel requested" in cancel_route
