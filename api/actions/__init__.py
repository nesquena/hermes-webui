"""Hermes Action Bus -- typed backend action dispatcher.

The bus provides one shared primitive:

    entry point  ->  dispatch_action(name, payload, context)
                 ->  registered backend action
                 ->  ActionResult

See ``docs/rfcs/action-bus.md`` (added in this PR) for the full design
and the ``register_builtins`` helper at the bottom of this module for
the v1 builtin registration surface.
"""

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


def register_builtins(registry: ActionRegistry = default_registry) -> None:
    """Register the v1 builtin actions against the given registry.

    v1 ships only :class:`~api.actions.builtin.echo_test.EchoTestAction`
    so the dispatch path is testable end-to-end without touching the
    session database or the agent. Follow-up PRs add real builtins
    (``session.nudge``, ``session.refresh``) and the helpers they need.
    """
    from .builtin.echo_test import EchoTestAction

    registry.register(EchoTestAction())
