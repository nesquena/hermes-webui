"""Shared scaffolding for the auxiliary title-generation test suites.

``test_title_aux_routing.py`` and ``test_2235_initial_aux_title.py`` need
``agent.auxiliary_client`` available while they patch provider calls. Keep that
temporary import scoped to each test and restore the complete ``agent.*``
module graph, including parent-package child bindings, when the test ends.
"""
from __future__ import annotations

from contextlib import contextmanager
import importlib
import sys
import types
from unittest import mock


def _snapshot_agent_modules():
    return {
        name: module
        for name, module in sys.modules.items()
        if name == "agent" or name.startswith("agent.")
    }


def _clear_agent_modules():
    for name in list(sys.modules):
        if name == "agent" or name.startswith("agent."):
            sys.modules.pop(name, None)


def _restore_agent_modules(snapshot):
    _clear_agent_modules()
    sys.modules.update(snapshot)

    for name, module in snapshot.items():
        if "." not in name:
            continue
        parent_name, child_name = name.rsplit(".", 1)
        parent = sys.modules.get(parent_name)
        if isinstance(parent, types.ModuleType):
            setattr(parent, child_name, module)


@contextmanager
def isolated_agent_modules():
    """Restore the full ``agent.*`` module graph after a temporary import."""
    snapshot = _snapshot_agent_modules()
    _clear_agent_modules()
    try:
        yield
    finally:
        _restore_agent_modules(snapshot)


@contextmanager
def auxiliary_client_modules():
    """Provide auxiliary-client modules in a temporary, restorable Agent graph.

    Reuse the real Agent package and auxiliary client when they are importable.
    The whole ``agent.*`` module graph is restored afterward, including direct
    child bindings on the parent package.

    ``agent.portal_tags`` is stubbed with a real ``ContextVar`` behind the same
    set/reset/get surface as the Agent runtime (tokens are truthy objects, and
    reset restores the previous value), so title-generation tests can assert
    conversation-context publication/restoration without a real Agent runtime
    (#7470).
    """
    import contextvars as _contextvars

    with isolated_agent_modules():
        try:
            agent_pkg = importlib.import_module("agent")
        except ModuleNotFoundError as exc:
            if exc.name != "agent":
                raise
            agent_pkg = types.ModuleType("agent")
            agent_pkg.__path__ = []
            sys.modules["agent"] = agent_pkg

        try:
            auxiliary_client = importlib.import_module("agent.auxiliary_client")
        except ModuleNotFoundError:
            # The Agent source may be visible without its optional runtime
            # dependencies in this test interpreter. Those tests only need the
            # import seam, so use a stub while restoring the original graph.
            auxiliary_client = types.ModuleType("agent.auxiliary_client")
            if not hasattr(agent_pkg, "__path__"):
                agent_pkg.__path__ = []
            sys.modules["agent.auxiliary_client"] = auxiliary_client
            agent_pkg.auxiliary_client = auxiliary_client

        portal_tags_stub = types.ModuleType("agent.portal_tags")
        portal_state = {"set_calls": [], "reset_calls": []}
        _conversation_cv = _contextvars.ContextVar("webui_test_conversation_id", default=None)

        def _set_conversation_context(conversation_id):
            portal_state["set_calls"].append(conversation_id)
            return _conversation_cv.set(conversation_id or None)

        def _reset_conversation_context(token):
            portal_state["reset_calls"].append(token)
            _conversation_cv.reset(token)

        def _get_conversation_context():
            return _conversation_cv.get()

        portal_tags_stub.set_conversation_context = _set_conversation_context
        portal_tags_stub.reset_conversation_context = _reset_conversation_context
        portal_tags_stub.get_conversation_context = _get_conversation_context
        portal_tags_stub._portal_state = portal_state
        sys.modules["agent.portal_tags"] = portal_tags_stub
        agent_pkg.portal_tags = portal_tags_stub

        yield


def patch_tg_config(config_dict):
    """Make ``_get_auxiliary_task_config`` return ``config_dict``."""
    return mock.patch(
        "agent.auxiliary_client._get_auxiliary_task_config",
        return_value=config_dict,
        create=True,
    )
