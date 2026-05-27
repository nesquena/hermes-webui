"""Registry and dispatcher for the Hermes Action Bus.

Threaded, synchronous, no event loop. Idempotency cache is in-memory
only; a process restart loses pending entries. That is acceptable for
the v1 use cases (uptime >> typical TTL) and a durable cache can slot
in behind the same ``dispatch`` signature in a later PR if needed.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from .types import Action, ActionContext, ActionResult


class ActionNotFound(Exception):
    """Raised when an action name is not registered."""


class _IdempotencyCache:
    """Thread-safe ``(action, key) -> (created_at, result)`` cache.

    Entries past ``ttl_seconds`` are pruned lazily on each ``get``.
    """

    def __init__(self, ttl_seconds: int = 300):
        self._cache: dict[tuple[str, str], tuple[float, ActionResult]] = {}
        self._ttl = ttl_seconds
        self._lock = threading.Lock()

    def get(self, action: str, key: str) -> Optional[ActionResult]:
        with self._lock:
            self._prune_locked()
            entry = self._cache.get((action, key))
            return entry[1] if entry else None

    def put(self, action: str, key: str, result: ActionResult) -> None:
        with self._lock:
            self._cache[(action, key)] = (time.time(), result)

    def _prune_locked(self) -> None:
        now = time.time()
        expired = [
            k for k, (created_at, _) in self._cache.items()
            if now - created_at > self._ttl
        ]
        for key in expired:
            self._cache.pop(key, None)


class ActionRegistry:
    """Named-action registry with idempotent dispatch.

    The registry is intentionally a plain object so callers can hold
    their own instance (the recommended path for tests and for entry
    points that want to ship their own allowlist). A module-level
    :data:`default_registry` exists for the common case.
    """

    def __init__(self, idempotency_ttl_seconds: int = 300):
        self._actions: dict[str, Action] = {}
        self._idempotency = _IdempotencyCache(idempotency_ttl_seconds)
        self._register_lock = threading.Lock()

    def register(self, action: Action) -> None:
        with self._register_lock:
            if action.name in self._actions:
                raise ValueError(f"Action {action.name!r} already registered")
            self._actions[action.name] = action

    def known_actions(self) -> list[str]:
        with self._register_lock:
            return sorted(self._actions)

    def dispatch(
        self,
        action: str,
        payload: dict,
        context: ActionContext,
        idempotency_key: Optional[str] = None,
    ) -> ActionResult:
        if idempotency_key:
            cached = self._idempotency.get(action, idempotency_key)
            if cached is not None:
                return cached

        impl = self._actions.get(action)
        if impl is None:
            raise ActionNotFound(action)

        try:
            result = impl.run(payload, context)
        except Exception as exc:
            # Actions are user-defined and may raise anything. We never
            # want a registered action to take down the request handler
            # thread, so wrap unexpected exceptions into a structured
            # error result. ActionNotFound is raised above this guard
            # and continues to bubble for the HTTP adapter to map to 404.
            result = ActionResult(
                ok=False,
                silent=True,
                error=f"{type(exc).__name__}: {exc}",
            )

        if idempotency_key:
            self._idempotency.put(action, idempotency_key, result)

        return result


default_registry = ActionRegistry()
