"""Shared scaffolding for the auxiliary title-generation test suites.

``test_title_aux_routing.py`` and ``test_2235_initial_aux_title.py`` both need
``agent.auxiliary_client`` importable so ``api.streaming`` can be patched
against it. Both used to install synthetic modules at import time with
``sys.modules.setdefault`` and never remove them, so whichever ran first left
the stub in place for the rest of the process and the next file to import
``agent.context_compressor`` got the stub instead of the real module (#6630).

Scope the install to the test that needs it and let ``mock.patch.dict`` own the
restore: it snapshots the mapping and puts it back on exit, so a key that was
absent is deleted, a key that was present is restored by identity, and a key
holding ``None`` stays ``None``. ``tests/test_4413_seed_provider_models.py``
already stubs ``sys.modules`` this way, including the ``None`` case.

One constraint that comes with it: ``patch.dict`` restores by clearing the
mapping and replacing it wholesale, so any module first imported inside the
scope is evicted on exit and re-imported later as a new object. That is
harmless for these suites, which import everything they touch at collection
time, but it is the thing to check before reusing this helper around code
that imports lazily.
"""
from __future__ import annotations

import sys
import types
from unittest import mock


def auxiliary_client_modules():
    """Context manager installing stub ``agent`` / ``agent.auxiliary_client``.

    The stubs are fresh objects owned by this helper, so the ``auxiliary_client``
    attribute is set on our own module and no pre-existing ``agent`` module is
    mutated. Restoration is therefore entirely ``patch.dict``'s job.

    ``agent.portal_tags`` is stubbed with a real ``ContextVar`` behind the same
    set/reset/get surface as the Agent runtime (tokens are truthy objects, and
    reset restores the previous value), so title-generation tests can assert
    conversation-context publication/restoration without a real Agent runtime
    (#7470).
    """
    import contextvars as _contextvars

    agent_stub = types.ModuleType("agent")
    auxiliary_client_stub = types.ModuleType("agent.auxiliary_client")
    portal_tags_stub = types.ModuleType("agent.portal_tags")
    agent_stub.auxiliary_client = auxiliary_client_stub
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
    agent_stub.portal_tags = portal_tags_stub
    return mock.patch.dict(
        sys.modules,
        {
            "agent": agent_stub,
            "agent.auxiliary_client": auxiliary_client_stub,
            "agent.portal_tags": portal_tags_stub,
        },
    )


def patch_tg_config(config_dict):
    """Make ``_get_auxiliary_task_config`` return ``config_dict``."""
    return mock.patch(
        "agent.auxiliary_client._get_auxiliary_task_config",
        return_value=config_dict,
        create=True,
    )
