"""Regression tests for the synthetic-provider MoA picker fix.

Bug: picking a MoA preset (e.g. ``ai-council-lite``) from the WebUI model
dropdown sent the literal id ``moa:ai-council-lite`` as the model id.
``_split_provider_qualified_model`` only recognised ``@provider:model``,
so the literal string ``moa:ai-council-lite`` was forwarded downstream
unchanged and surfaced as ``Error: 'moa:ai-council-lite'``.

Fix: extend ``_split_provider_qualified_model`` to also recognise a
qualifying set of synthetic / virtual provider ids (``moa``, ``mix``,
``hybrid``) when the id is ``<virtual>:<name>`` without a leading ``@``.
Real provider/model ids never use these prefixes, so the false-positive
risk is contained.

Tests cover:
  - The new ``moa:preset-name`` shape
  - The existing ``@provider:model`` shape (no regression)
  - Empty / bare / slash-prefixed / unknown-virtual cases
  - Prefixes that LOOK virtual but aren't (``moaa:fake``)
  - Edge case: a MoA preset name containing a colon is split only once
    (left-side split limit-by-1).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Light up just enough of api.routes to import the function under test.
# The real module pulls in profiles.py -> import yaml etc.; for this
# isolated function we don't need any of it.
_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))


def _load_split_function():
    """Import ``_split_provider_qualified_model`` without importing the
    entire ``api.routes`` module (which has heavy deps)."""
    import importlib.util

    src_path = _HERE / "api" / "routes.py"
    src = src_path.read_text(encoding="utf-8")
    # Pull out the module-level constant + the function definition only.
    import re

    virt_match = re.search(
        r"^_VIRTUAL_PICKER_PROVIDERS\s*=\s*frozenset\([^)]+\)\s*$",
        src,
        re.MULTILINE,
    )
    assert virt_match, "_VIRTUAL_PICKER_PROVIDERS not found in api/routes.py"
    fn_match = re.search(
        r"^def _split_provider_qualified_model\(model: str\).*?(?=^def |\Z)",
        src,
        re.MULTILINE | re.DOTALL,
    )
    assert fn_match, "_split_provider_qualified_model not found in api/routes.py"

    # Build a tiny module with stub dependencies.
    code = (
        "from __future__ import annotations\n"
        + virt_match.group()
        + "\n\n"
        # Stub: this helper normally lives earlier in routes.py but we
        # don't need it for the unqualified-id branches the bug touches.
        + "def _clean_session_model_provider(s):\n"
        + "    s = (s or '').strip()\n"
        + "    if s.startswith('@'):\n"
        + "        s = s[1:]\n"
        + "    return s or None\n"
        + "\n"
        + fn_match.group()
    )
    ns: dict = {}
    exec(code, ns)
    return ns["_split_provider_qualified_model"]


split = _load_split_function()


def test_moa_picker_synthetic_provider_splits_to_preset_name():
    """The bug fix: ``moa:<preset>`` rounds-trips to ``(<preset>, 'moa')``."""
    assert split("moa:ai-council-lite") == ("ai-council-lite", "moa")


def test_at_provider_colon_model_unchanged():
    """Standard shape still works (no regression)."""
    assert split("@anthropic:claude-opus-4.8") == ("claude-opus-4.8", "anthropic")


def test_other_virtual_providers_in_the_set_also_split():
    """``mix`` and ``hybrid`` are listed — they should split the same way."""
    assert split("mix:hybrid-preset") == ("hybrid-preset", "mix")


def test_empty_string_returns_empty_pair():
    """Defensive: an empty input yields empty strings, not a crash."""
    model, provider = split("")
    assert model == ""
    assert provider is None


def test_bare_model_id_returns_provider_none():
    """A model without any prefix has no provider hint."""
    assert split("gpt-5") == ("gpt-5", None)


def test_slash_prefixed_openrouter_format_passes_through():
    """OpenRouter-style ``provider/model`` ids must not be split on the slash."""
    # The slash-form is handled by OTHER call sites; this splitter should
    # NOT degrade such an id into ``(provider, model)`` because there is
    # no colon. Returning the original string + None is the correct
    # pass-through behaviour.
    assert split("anthropic/claude-3.5-sonnet") == ("anthropic/claude-3.5-sonnet", None)


def test_unknown_virtual_prefix_does_not_split():
    """Defensive: an id starting with a non-virtual prefix must NOT be
    split. ``moaa:fake`` looks MoA-ish but isn't in the allowlist —
    we want to surface it as one literal model id, not two halves.
    """
    assert split("moaa:fake") == ("moaa:fake", None)


def test_colon_does_not_split_when_both_halves_non_empty_for_non_virtual():
    """Same as above for an arbitrary: ``foo:bar`` where ``foo`` is not a
    known virtual provider. We must NOT split this into ``('bar', 'foo')``
    because ``foo`` is a real model namespace (e.g. ``minimax:MiniMax-M2.7``).
    """
    # Important: any real-provider ``<name>:<model>`` form SHOULD split
    # once we add real providers to the allowlist. Until then, treat
    # unknown prefixes as a single id to avoid chewing real model names.
    assert split("unknown-prefix:some-model") == ("unknown-prefix:some-model", None)


def test_moa_preset_name_with_inner_colon_splits_once():
    """Edge case: if a MoA preset name itself contains a colon (unlikely
    but possible), the splitter must only consume the FIRST colon so we
    don't silently swallow parts of the preset name.
    """
    # ``moa:preset:v2`` -> ``preset:v2`` as the bare model id with
    # provider ``moa``. Left-side split limitation by 1.
    assert split("moa:preset:v2") == ("preset:v2", "moa")
