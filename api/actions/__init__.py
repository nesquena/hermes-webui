"""Hermes Action Bus -- typed backend action dispatcher.

The bus provides one shared primitive:

    entry point  ->  dispatch_action(name, payload, context)
                 ->  registered backend action
                 ->  ActionResult

See ``docs/rfcs/action-bus.md`` (added in this PR) for the full design
and the ``register_builtins`` helper at the bottom of this module for
the v1 builtin registration surface.
"""

import threading

from .registry import ActionNotFound, ActionRegistry, default_registry
from .types import (
    Action,
    ActionContext,
    ActionResult,
    SILENT_SENTINEL,
)

__all__ = [
    "Action",
    "ActionContext",
    "ActionNotFound",
    "ActionRegistry",
    "ActionResult",
    "SILENT_SENTINEL",
    "default_registry",
    "register_builtins",
]


# Module-level idempotency guard for the process-global default_registry.
# Per-test or per-caller registries (passed explicitly) always register
# fresh so test isolation is preserved; only the shared singleton path is
# locked behind this flag. Earlier drafts of the route hook used a name
# sentinel ("echo.test" in known_actions()) which silently broke whenever
# a new builtin was added without also updating the sentinel -- the new
# builtin would never be picked up because the sentinel check still
# passed. The flag survives the addition of new builtins because it
# tracks the registration call itself, not any individual action name.
_BUILTINS_LOCK = threading.Lock()
_BUILTINS_REGISTERED = False


def register_builtins(registry: ActionRegistry = default_registry) -> None:
    """Register the v1 builtin actions against the given registry.

    v1 ships only :class:`~api.actions.builtin.echo_test.EchoTestAction`
    so the dispatch path is testable end-to-end without touching the
    session database or the agent. Follow-up PRs add real builtins
    (``session.nudge``, ``session.refresh``) and the helpers they need.

    Idempotency contract:

    - When called with the module-level :data:`default_registry` (the
      common production path), this function is idempotent. The first
      successful call registers every v1 builtin; subsequent calls
      return immediately under a module-level lock. Request handlers
      can therefore call ``register_builtins(default_registry)`` on
      every request without paying re-registration cost or risking a
      ``ValueError`` on duplicate names. New builtins added in follow-up
      PRs are picked up automatically the first time the process starts
      after the upgrade.
    - When called with an explicitly-passed registry (tests, allowlist
      entry points), no flag is set. The registry is registered fresh
      every call, which is what test isolation needs.
    """
    if registry is default_registry:
        global _BUILTINS_REGISTERED
        with _BUILTINS_LOCK:
            if _BUILTINS_REGISTERED:
                return
            _register_all_builtins(registry)
            _BUILTINS_REGISTERED = True
    else:
        _register_all_builtins(registry)


def _register_all_builtins(registry: ActionRegistry) -> None:
    """Register every v1 builtin against ``registry``.

    Kept separate from :func:`register_builtins` so the locking and
    idempotency policy stays a clean wrapper around the actual
    registration list. Follow-up PRs should add their builtins here.
    """
    from .builtin.echo_test import EchoTestAction

    registry.register(EchoTestAction())
