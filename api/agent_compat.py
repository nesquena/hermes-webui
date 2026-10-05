"""Resolve Hermes Agent names that moved to a sibling module.

Hermes Agent's September 2026 decomposition moved many names out of their
original modules (``tools.approval``, ``tools.mcp_tool``,
``hermes_cli.kanban_db``, ...) into ``<stem>_<topic>`` siblings. The old paths
kept resolving for a while only through PEP 562 ``__getattr__`` pointers that
emit ``HermesPluginCompatWarning`` and are removed on schedule, so WebUI code
must not rely on them. Importing only the new module would instead break Agent
installs that predate the split.

``agent_attr`` is compatibility-only and resolves a moved name in this order:

1. the original module's own namespace -- pre-split Agents, and tests that stub
   the original module in ``sys.modules`` or patch the name onto it;
2. the new home module -- split Agents, with or without the old-path pointers;
3. plain attribute access on the original object -- non-module test doubles,
   or an Agent whose new home is not importable.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

_MISSING = object()


def agent_attr(owner: Any, name: str, home: str, default: Any = _MISSING) -> Any:
    """Return ``owner.name`` without going through a removed/deprecated pointer.

    ``owner`` is the original module (or its dotted name); ``home`` is the
    dotted name of the module the Agent moved ``name`` to. Raises
    ``ImportError``/``AttributeError`` like the plain import it replaces, unless
    ``default`` is given.
    """
    if isinstance(owner, str):
        try:
            owner = importlib.import_module(owner)
        except ImportError:
            if default is _MISSING:
                return getattr(importlib.import_module(home), name)
            try:
                return getattr(importlib.import_module(home), name, default)
            except ImportError:
                return default
    if isinstance(owner, ModuleType) and name not in vars(owner):
        try:
            return getattr(importlib.import_module(home), name)
        except (ImportError, AttributeError):
            pass
    if default is _MISSING:
        return getattr(owner, name)
    return getattr(owner, name, default)
