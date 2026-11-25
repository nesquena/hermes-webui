"""#7170 round-12 (greptile P1, 2026-11-24): native streaming must honour a
qualified (``@provider:model``) override key.

The finding
-----------
With global ``high`` and override ``{"@openrouter:gpt-5.4-mini": "low"}``, a
legacy chat (no saved effort) resolved ``high`` on the NATIVE streaming path
while the reasoning-status endpoint reported ``low``.

Why
---
Native streaming resolves the model BEFORE the effort lookup, so the lookup
received the BARE id (``gpt-5.4-mini``) -- the ``@`` route hint was already
stripped by ``resolve_model_provider()``. The resolver only consults an
``@``-form override key when the incoming model still carries that hint, so the
``@openrouter:gpt-5.4-mini`` key was invisible and the global won.

The status endpoint reads the model from the query string (the user's original
spelling, hint intact), which is why the two paths disagreed.

The fix
-------
Override SELECTION needs the caller's ORIGINAL spelling; capability CHECKS
need the RESOLVED model. ``configured_reasoning_effort_for_model()`` now takes
the requested spelling in a separate argument so both paths can be satisfied
at once, and the native streaming worker passes the model it was handed.

Test strategy (why there is a source-level assertion here)
-----------------------------------------------------------
The native streaming worker resolves the model deep inside a detached thread
(``_run_agent_streaming``), so its effort lookup cannot be called directly
from a unit test. A test that only re-implements the call in the test body
proves NOTHING about the production call site -- mutating that call site
leaves such a test green. So this suite pins BOTH halves:

* behaviour of the resolver (what the two spellings must select), and
* the production call site in ``api/streaming.py`` (that it really passes the
  original spelling), asserted against the source.

The source assertion is deliberately narrow: it requires the argument on
BOTH effort lookups in the streaming worker, and requires that the value
passed is the ORIGINAL ``model`` and never the RESOLVED ``resolved_model``.

Sibling audit
-------------
The Gateway bridge (``api/gateway_chat.py``) already receives the request's
original spelling and resolves correctly -- this suite pins that too, so the
pair cannot drift apart again.
"""

import inspect
import re
from pathlib import Path

from api import config as cfg
from api import gateway_chat


_OVERRIDES = {"@openrouter:gpt-5.4-mini": "low"}
_GLOBAL_HIGH = {
    "agent": {
        "reasoning_effort": "high",
        "reasoning_overrides": _OVERRIDES,
    }
}

_STREAMING_PY = Path(__file__).resolve().parents[1] / "api" / "streaming.py"


def _effort_for_requested(cfg_data, requested_model, resolved_model, provider_id):
    """Resolve the effort the way the NATIVE streaming worker does.

    Mirrors ``api/streaming.py``: the worker knows the REQUESTED spelling and
    the RESOLVED model, and hands both to the resolver.
    """
    return cfg.configured_reasoning_effort_for_model(
        cfg_data,
        model_id=resolved_model,
        provider_id=provider_id,
        base_url=None,
        requested_model_id=requested_model,
    )


# ───────────────── Behaviour: the resolver's two spellings ─────────────────


def test_native_streaming_honors_qualified_override_key():
    """The requested ``@provider:model`` spelling selects the override.

    RED on the pre-fix head: the resolver only sees the resolved model, so the
    ``@``-form key is invisible and the global ``high`` wins.
    """
    effort = _effort_for_requested(
        _GLOBAL_HIGH,
        requested_model="@openrouter:gpt-5.4-mini",
        resolved_model="gpt-5.4-mini",
        provider_id="openrouter",
    )

    assert effort == "low", (
        "native streaming sent %r; the @provider:model override key "
        "(@openrouter:gpt-5.4-mini: low) was ignored" % (effort,)
    )


def test_status_path_and_native_path_agree():
    """Status and native streaming must not disagree about the same model.

    The status endpoint looks the model up by the user's original spelling;
    the native worker used to look it up by the resolved one. Both must land
    on the same effort.
    """
    status_effort = cfg.configured_reasoning_effort_for_model(
        _GLOBAL_HIGH,
        model_id="@openrouter:gpt-5.4-mini",
    )
    native_effort = _effort_for_requested(
        _GLOBAL_HIGH,
        requested_model="@openrouter:gpt-5.4-mini",
        resolved_model="gpt-5.4-mini",
        provider_id="openrouter",
    )

    assert status_effort == native_effort, (
        "status reported %r while native streaming sent %r for the same model"
        % (status_effort, native_effort)
    )


def test_gateway_bridge_path_honors_qualified_override_key():
    """The Gateway bridge keeps working (sibling path, already correct).

    The bridge receives the request's original spelling, so the qualified
    override key applies there today. Pinning it stops the pair from drifting.
    """
    effort = gateway_chat._gateway_reasoning_effort_for_request(
        _GLOBAL_HIGH,
        model="@openrouter:gpt-5.4-mini",
        model_provider="openrouter",
    )

    assert effort == "low", (
        "the Gateway bridge sent %r; the @provider:model override key "
        "was ignored" % (effort,)
    )


def test_bare_model_keeps_the_global_effort():
    """Control: a model with no matching override still gets the global.

    A bare model id with a bare override key must keep resolving through the
    normal precedence chain -- the new argument must not change that.
    """
    data = {
        "agent": {
            "reasoning_effort": "high",
            "reasoning_overrides": {"gpt-5.4-mini": "low"},
        }
    }

    effort = _effort_for_requested(
        data,
        requested_model="gpt-5.4-mini",
        resolved_model="gpt-5.4-mini",
        provider_id="openrouter",
    )

    assert effort == "low", "a bare override key must still win over the global"


def test_unmatched_model_keeps_the_global_effort():
    """Control: an unrelated model is unaffected by the requested spelling."""
    effort = _effort_for_requested(
        _GLOBAL_HIGH,
        requested_model="@openrouter:some-other-model",
        resolved_model="some-other-model",
        provider_id="openrouter",
    )

    assert effort == "high", (
        "a model with no override must keep the global effort, got %r" % (effort,)
    )


def test_requested_spelling_does_not_override_the_bare_key_for_a_bare_request():
    """Control: the requested spelling must not hijack an unrelated model.

    When the caller requested a BARE model, no ``@`` hint may be invented, so
    a qualified override key for a different model stays out of the way.
    """
    effort = _effort_for_requested(
        _GLOBAL_HIGH,
        requested_model="gpt-5.4-mini",
        resolved_model="gpt-5.4-mini",
        provider_id="openrouter",
    )

    assert effort == "high", (
        "a bare request must not pick up another model's qualified override, "
        "got %r" % (effort,)
    )


def test_requested_spelling_alone_still_resolves_when_no_resolved_model():
    """A caller with only the requested spelling still resolves.

    The resolver must derive the bare id and the provider from the
    requested spelling when no resolved model was supplied.
    """
    effort = cfg.configured_reasoning_effort_for_model(
        _GLOBAL_HIGH,
        model_id=None,
        requested_model_id="@openrouter:gpt-5.4-mini",
    )

    assert effort == "low", (
        "the requested spelling alone must still select the override, got %r"
        % (effort,)
    )


# ───────────────── The production call site (api/streaming.py) ─────────────────
#
# The streaming worker's effort lookup runs inside a detached thread, so it
# cannot be called from a unit test. These assertions pin the call site
# itself: without them, mutating the production call would leave every
# behavioural test in this file green.


def _streaming_effort_lookup_calls():
    """The effort-lookup call sites in the streaming worker.

    Returns the source of every call that passes ``requested_model_id`` to one
    of the effort resolvers.
    """
    source = _STREAMING_PY.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(?:configured_reasoning_effort_for_model|effective_session_reasoning_effort)\s*\("
        r"(?:[^()]|\([^()]*\))*\)",
        re.DOTALL,
    )
    return [m.group(0) for m in pattern.finditer(source)]


def test_streaming_worker_passes_the_original_model_spelling_to_effort_lookup():
    """Both streaming effort lookups must receive the ORIGINAL model.

    The worker holds the request's original spelling in ``model`` and the
    resolved (hint-stripped) model in ``resolved_model``. Override selection
    needs the former; capability checks need the latter. A lookup that passes
    ``resolved_model`` as the requested spelling silently drops every
    qualified override key.
    """
    calls = _streaming_effort_lookup_calls()
    assert calls, "no effort-lookup call site found in api/streaming.py"

    passing = [c for c in calls if "requested_model_id" in c]
    assert len(passing) >= 2, (
        "expected BOTH streaming effort lookups to carry the requested spelling, "
        "found %d of %d" % (len(passing), len(calls))
    )

    for call in passing:
        assert "requested_model_id=model" in call.replace(" ", ""), (
            "a streaming effort lookup passes the wrong value as the requested "
            "spelling:\n%s" % (call,)
        )
        assert "requested_model_id=resolved_model" not in call.replace(" ", ""), (
            "a streaming effort lookup passes the RESOLVED model as the requested "
            "spelling, which is exactly the defect:\n%s" % (call,)
        )


def test_resolver_accepts_the_requested_spelling_argument():
    """The resolver's signature must carry the requested spelling.

    A caller that cannot pass it is back to the pre-fix behaviour.
    """
    signature = inspect.signature(cfg.configured_reasoning_effort_for_model)
    assert "requested_model_id" in signature.parameters, (
        "configured_reasoning_effort_for_model() has no requested_model_id "
        "parameter: %s" % (signature,)
    )
