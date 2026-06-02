"""Small route-dispatch registry used to migrate routes out of long if-chains.

The WebUI remains stdlib-only and handler functions keep the existing
``handler, parsed`` call shape. Exact routes are preferred over prefix routes.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

RouteHandler = Callable[[Any, Any], bool]

GET_ROUTES: dict[str, RouteHandler] = {}
POST_ROUTES: dict[str, RouteHandler] = {}
GET_PREFIX_ROUTES: list[tuple[str, RouteHandler]] = []
POST_PREFIX_ROUTES: list[tuple[str, RouteHandler]] = []


def _table(method: str) -> tuple[dict[str, RouteHandler], list[tuple[str, RouteHandler]]]:
    method = str(method or "").upper()
    if method == "GET":
        return GET_ROUTES, GET_PREFIX_ROUTES
    if method == "POST":
        return POST_ROUTES, POST_PREFIX_ROUTES
    raise ValueError(f"unsupported route method: {method!r}")


def clear_routes() -> None:
    """Clear registered routes. Intended for tests only."""
    GET_ROUTES.clear()
    POST_ROUTES.clear()
    GET_PREFIX_ROUTES.clear()
    POST_PREFIX_ROUTES.clear()


def register_route(method: str, path: str, handler: RouteHandler, *, prefix: bool = False) -> RouteHandler:
    """Register *handler* for an exact or prefix route and return it."""
    exact, prefixes = _table(method)
    if prefix:
        pair = (str(path), handler)
        if pair not in prefixes:
            prefixes.append(pair)
            prefixes.sort(key=lambda item: len(item[0]), reverse=True)
    else:
        exact[str(path)] = handler
    return handler


def register_get(path: str, *, prefix: bool = False):
    def decorator(handler: RouteHandler) -> RouteHandler:
        return register_route("GET", path, handler, prefix=prefix)
    return decorator


def register_post(path: str, *, prefix: bool = False):
    def decorator(handler: RouteHandler) -> RouteHandler:
        return register_route("POST", path, handler, prefix=prefix)
    return decorator


def get_route(method: str, path: str) -> RouteHandler | None:
    exact, prefixes = _table(method)
    path = str(path or "")
    handler = exact.get(path)
    if handler is not None:
        return handler
    for prefix, candidate in prefixes:
        if path.startswith(prefix):
            return candidate
    return None
