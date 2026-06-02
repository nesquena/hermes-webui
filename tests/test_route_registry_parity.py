from urllib.parse import urlsplit

from api.route_registry import clear_routes, get_route, register_get, register_route


def _restore_migrated_routes():
    from api import routes

    clear_routes()
    routes._ROUTE_REGISTRY_READY = False
    routes._init_route_registry()


def test_route_registry_exact_prefix_and_method_are_isolated():
    clear_routes()
    try:
        def exact(handler, parsed):
            return "exact"

        def prefixed(handler, parsed):
            return "prefix"

        register_route("GET", "/health", exact)
        register_route("GET", "/static/", prefixed, prefix=True)

        assert get_route("GET", "/health") is exact
        assert get_route("GET", "/static/app.js") is prefixed
        assert get_route("POST", "/health") is None
        assert get_route("GET", "/missing") is None
    finally:
        _restore_migrated_routes()


def test_register_get_decorator_keeps_handler_identity():
    clear_routes()
    try:
        @register_get("/api/example")
        def handler(handler, parsed):
            return True

        assert get_route("GET", "/api/example") is handler
    finally:
        _restore_migrated_routes()


def test_migrated_health_routes_are_registered_with_real_handlers():
    from api import routes

    clear_routes()
    routes._ROUTE_REGISTRY_READY = False
    try:
        routes._init_route_registry()

        assert get_route("GET", "/health") is routes._handle_health
        assert get_route("GET", "/api/health/agent") is routes._handle_agent_health_registry
        assert get_route("GET", "/api/system/health") is routes._handle_system_health_registry
        assert get_route("GET", "/static/app.js") is routes._serve_static
    finally:
        _restore_migrated_routes()


def test_registered_none_returning_route_is_terminal():
    from api import routes

    calls = []

    def none_returning_handler(handler, parsed):
        calls.append(parsed.path)
        return None

    clear_routes()
    routes._ROUTE_REGISTRY_READY = False
    try:
        routes._init_route_registry()
        register_get("/api/none-return")(none_returning_handler)

        assert routes._dispatch_registered_get_route(object(), urlsplit("/api/none-return")) is True
        assert calls == ["/api/none-return"]
    finally:
        _restore_migrated_routes()


def test_agent_health_registry_route_preserves_gateway_chat_payload(monkeypatch):
    from api import routes

    captured = {}

    monkeypatch.setattr(routes, "build_agent_health_payload", lambda: {"status": "ok"})
    monkeypatch.setattr(routes, "gateway_chat_config_status", lambda: {"enabled": True, "reason": "test"})

    def fake_j(handler, payload, *args, **kwargs):
        captured["payload"] = payload
        captured["args"] = args
        captured["kwargs"] = kwargs
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    clear_routes()
    routes._ROUTE_REGISTRY_READY = False
    try:
        routes._init_route_registry()
        handler = get_route("GET", "/api/health/agent")
        assert handler is routes._handle_agent_health_registry
        assert routes._dispatch_registered_get_route(object(), urlsplit("/api/health/agent")) is True
        assert captured["payload"] == {
            "status": "ok",
            "gateway_chat": {"enabled": True, "reason": "test"},
        }
    finally:
        _restore_migrated_routes()


def test_route_registry_self_heals_after_test_clear():
    from api import routes

    clear_routes()
    routes._ROUTE_REGISTRY_READY = True
    try:
        routes._init_route_registry()

        assert get_route("GET", "/health") is routes._handle_health
        assert get_route("GET", "/api/health/agent") is routes._handle_agent_health_registry
        assert get_route("GET", "/api/system/health") is routes._handle_system_health_registry
        assert get_route("GET", "/static/app.js") is routes._serve_static
    finally:
        _restore_migrated_routes()
