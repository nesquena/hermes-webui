"""The WebUI in-process runtime forwards the profile's provider_routing block
to AIAgent — parity with the gateway's TurnRunner._build_fresh_agent.

Before this fix, the streaming chat path (POST /api/chat/start, browser turns)
assembled ``_agent_kwargs`` in api/streaming.py without any of the six routing
kwargs (provider_sort / providers_allowed / providers_ignored /
providers_order / provider_require_parameters / provider_data_collection), so
an OpenRouter ``provider_routing`` block in config.yaml (sort: price, etc.)
was silently dropped for browser chat turns while Discord/Telegram/TUI turns
respected it. The fallback sync endpoint (POST /api/chat,
routes._handle_chat_sync) had the same gap.

These tests:
  * unit-test the pure mapping helper ``_provider_routing_kwargs_for_agent``,
  * structurally assert (AST) that the streaming construction site feeds the
    helper's result into ``_agent_kwargs`` before the
    ``_AIAgent(**_agent_kwargs)`` construction,
  * structurally assert (AST) that ``_handle_chat_sync``'s AIAgent call carries
    all six routing kwargs.

Pre-fix state: ``api.streaming`` has no ``_provider_routing_kwargs_for_agent``
and no wiring call, so the helper import raises AttributeError and the AST
assertions fail; post-fix everything passes.
"""
from __future__ import annotations

import ast
import inspect

ROUTING_KWARGS = {
    "provider_sort",
    "providers_allowed",
    "providers_ignored",
    "providers_order",
    "provider_require_parameters",
    "provider_data_collection",
}


# ── Helper unit tests ──────────────────────────────────────────────────────

def _helper():
    from api import streaming as streaming_mod
    return streaming_mod._provider_routing_kwargs_for_agent


def test_helper_forwards_full_block():
    h = _helper()
    cfg = {"provider_routing": {
        "sort": "price",
        "ignore": ["open-inference"],
        "only": ["openrouter"],
        "order": ["a", "b"],
        "require_parameters": True,
        "data_collection": "extra",
    }}
    out = h(cfg, ROUTING_KWARGS)
    assert out == {
        "provider_sort": "price",
        "providers_ignored": ["open-inference"],
        "providers_allowed": ["openrouter"],
        "providers_order": ["a", "b"],
        "provider_require_parameters": True,
        "provider_data_collection": "extra",
    }


def test_helper_filters_to_supported_params():
    h = _helper()
    out = h({"provider_routing": {"sort": "price"}}, {"provider_sort"})
    assert out == {"provider_sort": "price"}
    # Empty param set (older agent build) → nothing forwarded.
    assert h({"provider_routing": {"sort": "price"}}, set()) == {}


def test_helper_absent_or_invalid_block_forwards_constructor_defaults():
    h = _helper()
    # Gateway parity (TurnRunner._build_fresh_agent): an absent block forwards
    # the AIAgent constructor defaults (None / False), which are no-ops.
    defaults = {
        "provider_sort": None,
        "providers_allowed": None,
        "providers_ignored": None,
        "providers_order": None,
        "provider_require_parameters": False,
        "provider_data_collection": None,
    }
    assert h({}, ROUTING_KWARGS) == defaults
    assert h(None, ROUTING_KWARGS) == defaults
    assert h({"provider_routing": None}, ROUTING_KWARGS) == defaults
    assert h({"provider_routing": "not-a-dict"}, ROUTING_KWARGS) == defaults
    assert h({"provider_routing": ["list"]}, ROUTING_KWARGS) == defaults


def test_helper_require_parameters_defaults_false():
    h = _helper()
    out = h({"provider_routing": {}}, {"provider_require_parameters"})
    assert out == {"provider_require_parameters": False}


# ── Streaming construction-site wiring (AST) ───────────────────────────────

def _is_streaming_construction(call: ast.Call) -> bool:
    """True for ``_AIAgent(**_agent_kwargs)``."""
    if not (isinstance(call.func, ast.Name) and call.func.id == "_AIAgent"):
        return False
    return (
        len(call.args) == 0
        and len(call.keywords) == 1
        and call.keywords[0].arg is None
        and isinstance(call.keywords[0].value, ast.Name)
        and call.keywords[0].value.id == "_agent_kwargs"
    )


def _is_routing_wiring(call: ast.Call) -> bool:
    """True for ``_agent_kwargs.update(_provider_routing_kwargs_for_agent(...))``."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "update"):
        return False
    if not (isinstance(call.func.value, ast.Name)
            and call.func.value.id == "_agent_kwargs"):
        return False
    return any(
        isinstance(a, ast.Call)
        and isinstance(a.func, ast.Name)
        and a.func.id == "_provider_routing_kwargs_for_agent"
        for a in call.args
    )


def _functions_with(tree: ast.AST, predicate):
    """Yield (func_node, [matching sub-calls]) for every function whose subtree
    contains at least one call matching ``predicate``."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        matches = [
            sub for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and predicate(sub)
        ]
        if matches:
            yield node, matches


def test_streaming_construction_sites_forward_provider_routing():
    from api import streaming as streaming_mod
    tree = ast.parse(inspect.getsource(streaming_mod))

    construction_sites = list(_functions_with(tree, _is_streaming_construction))
    assert construction_sites, (
        "expected at least one _AIAgent(**_agent_kwargs) construction in "
        "api/streaming.py — did the chat path move?"
    )

    for fn, constructions in construction_sites:
        wiring = [
            sub for sub in ast.walk(fn)
            if isinstance(sub, ast.Call) and _is_routing_wiring(sub)
        ]
        assert wiring, (
            f"{fn.name} constructs _AIAgent(**_agent_kwargs) but never forwards "
            "provider_routing into _agent_kwargs — OpenRouter sort/ignore/only "
            "would be silently dropped for browser chat turns"
        )
        # The wiring must precede the first construction in source order.
        assert wiring[0].lineno < min(c.lineno for c in constructions), (
            f"provider_routing wiring in {fn.name} must run before the "
            "_AIAgent(**_agent_kwargs) construction"
        )


# ── Fallback sync endpoint wiring (AST) ────────────────────────────────────

def test_chat_sync_agent_carries_all_routing_kwargs():
    from api import routes as routes_mod
    tree = ast.parse(inspect.getsource(routes_mod))
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_handle_chat_sync"),
        None,
    )
    assert fn is not None, "_handle_chat_sync not found in api/routes.py"

    agent_calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "AIAgent"
    ]
    assert agent_calls, "expected an AIAgent(...) construction in _handle_chat_sync"

    for call in agent_calls:
        kwargs = {k.arg for k in call.keywords if k.arg is not None}
        missing = ROUTING_KWARGS - kwargs
        assert not missing, (
            f"_handle_chat_sync AIAgent(...) is missing routing kwargs: "
            f"{sorted(missing)} — provider_routing would be dropped for "
            "POST /api/chat turns"
        )
