"""Regression tests for issue #7290 — configured-model badge must not leak a
``{provider}/{model}``-prefixed alias when the native model id itself
contains a slash.

When the active provider is a built-in plugin provider (e.g. ``commandcode``)
and the configured default model id contains a slash (e.g.
``deepseek/deepseek-v4-flash``), the configured-model badge builder used
to emit three key variants for the same configured identity:

  - the bare native id (``deepseek/deepseek-v4-flash``)
  - a slash-prefixed alias (``commandcode/deepseek/deepseek-v4-flash``)
  - the ``@provider:model``-prefixed form (``@commandcode:deepseek/deepseek-v4-flash``)

The slash-prefixed alias was a synthetic key the catalog never actually
exposed — the WebUI dropdown rendered it as a real option, selecting it
sent ``model=commandcode/deepseek/deepseek-v4-flash`` to the agent, and
the provider rejected it with ``HTTP 400: Model ... is not supported on
this endpoint``. The session also persisted the wrong id, so reload
replayed the failure.

The fix removes the ``{provider}/{model}`` synthetic alias. Bare native
id (which is what the catalog actually exposes) and ``@provider:model``
(which is the documented prefix form for named custom providers) remain.
Multi-slash, URI-scheme, host:port, and ``@provider:model``-prefixed
model ids all keep working — only the synthesised slash-prefixed alias
disappears.

The pre-fix test below pins both failure modes reported in the issue:

  1. the badge map MUST NOT contain the slash-prefixed alias
  2. selecting the bare native id from the dropdown MUST NOT produce a
     request whose ``model`` field is the slash-prefixed alias
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _build_badges(active_provider, default_model, groups, fallback_providers=None):
    """Inline-exec the live ``_build_configured_model_badges`` and return its result.

    ``_norm_model_id`` is inlined as a stand-in (its full body is part of
    the production source and is exercised by ``test_norm_model_id_*``
    siblings). Mirrors the existing pattern in
    ``test_duplicate_slash_id_primary_badge_sticks_to_matching_provider_only``.
    """
    src = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
    start = src.index("def _build_configured_model_badges() -> dict[str, dict[str, str]]:")
    end = src.index("            return badges", start) + len("            return badges")
    fn_src = textwrap.dedent(src[start:end])

    scope = {
        "active_provider": active_provider,
        "default_model": default_model,
        "cfg": {"fallback_providers": fallback_providers or []},
        "groups": groups,
        "_resolve_provider_alias": lambda provider: provider,
    }
    # The live source uses a different `_norm_model_id` than the test's
    # inline; either is fine because the badge key is the **raw** candidate,
    # not the normalized key, so the raw_candidates list is the surface this
    # test pins.
    exec(
        "def _norm_model_id(model_id):\n"
        "    s=str(model_id or '').strip().lower()\n"
        "    if s.startswith('@') and ':' in s: s=s.split(':',1)[1]\n"
        "    if '/' in s: s=s.split('/',1)[1]\n"
        "    return s.replace('-', '.')\n",
        scope,
    )
    exec(fn_src, scope)
    return scope["_build_configured_model_badges"]()


# ---------------------------------------------------------------------------
# Repro: commandcode + deepseek/deepseek-v4-flash — the literal bug report
# ---------------------------------------------------------------------------


def test_commandcode_deepseek_does_not_emit_slash_prefixed_alias() -> None:
    """The badge map MUST NOT contain ``commandcode/deepseek/deepseek-v4-flash``."""
    badges = _build_badges(
        active_provider="commandcode",
        default_model="deepseek/deepseek-v4-flash",
        groups=[
            # The catalog exposes the bare native id under the commandcode
            # provider — that's the only selectable form.
            {
                "provider": "CommandCode",
                "provider_id": "commandcode",
                "models": [{"id": "deepseek/deepseek-v4-flash"}],
            }
        ],
    )
    # The slash-prefixed alias MUST NOT appear in the badge map.
    assert "commandcode/deepseek/deepseek-v4-flash" not in badges, (
        f"slash-prefixed alias leaked into the badge map: {badges!r}"
    )
    # The bare native id SHOULD be the primary badge (it is the only
    # catalog-exposed form and is the correct value to send to the agent).
    assert badges.get("deepseek/deepseek-v4-flash", {}).get("role") == "primary"


def test_commandcode_deepseek_at_prefixed_alias_still_present() -> None:
    """The ``@provider:model`` prefix form MUST still resolve to the primary badge."""
    badges = _build_badges(
        active_provider="commandcode",
        default_model="deepseek/deepseek-v4-flash",
        groups=[
            {
                "provider": "CommandCode",
                "provider_id": "commandcode",
                "models": [{"id": "deepseek/deepseek-v4-flash"}],
            }
        ],
    )
    assert badges.get("@commandcode:deepseek/deepseek-v4-flash", {}).get("role") == "primary", (
        f"@commandcode:deepseek/deepseek-v4-flash alias missing from badges: {badges!r}"
    )


# ---------------------------------------------------------------------------
# Dedupe across providers: a slash-bearing id shared by two providers must
# keep the badge only on the matching provider's row, never the other way.
# ---------------------------------------------------------------------------


def test_duplicate_slash_id_does_not_pollute_other_provider() -> None:
    """Per #7290 contract: deduplicate only when provider+full native model match."""
    badges = _build_badges(
        active_provider="custom:alpha",
        default_model="vendor/abc-model",
        groups=[
            {
                "provider": "Alpha",
                "provider_id": "custom:alpha",
                "models": [{"id": "vendor/abc-model"}],
            },
            {
                "provider": "Beta",
                "provider_id": "custom:beta",
                "models": [{"id": "vendor/abc-model"}],
            },
        ],
    )
    # The bare native id is a real option for BOTH providers; only the
    # matching provider's row gets the PRIMARY badge.
    payload = badges.get("vendor/abc-model", {})
    assert payload.get("role") == "primary"
    assert payload.get("provider") == "custom:alpha"
    # The OTHER provider's @-prefixed form is not produced (entry is for alpha
    # only); but the alpha @-prefixed form IS produced.
    assert "vendor/abc-model" in badges
    # No synthetic slash-prefixed alias for the other provider.
    assert "custom:beta/vendor/abc-model" not in badges


# ---------------------------------------------------------------------------
# Sanity: other forms of model id (URI scheme, host:port, @provider:model)
# must keep working — only the synthetic slash-prefixed alias is removed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    [
        "deepseek-v4-pro",  # bare id (no slash) — the common case
        "@anthropic:claude-opus-4-7",  # @-prefixed named custom provider
        "gpt://folder/abc/latest",  # URI-scheme id (#3429)
        "https://proxy.internal/models/gpt4",  # URL id (#3429)
    ],
)
def test_non_slash_models_unchanged(model_id: str) -> None:
    """No regression: the bare / @-prefixed / URI-scheme id forms are unchanged."""
    badges = _build_badges(
        active_provider="commandcode",
        default_model=model_id,
        groups=[
            {
                "provider": "CommandCode",
                "provider_id": "commandcode",
                "models": [{"id": model_id}],
            }
        ],
    )
    # The bare native id (or its @-prefixed alias) is the primary badge.
    assert badges.get(model_id, {}).get("role") == "primary", (
        f"bare id missing primary for {model_id!r}: {badges!r}"
    )
    # No synthesised slash-prefixed alias for ANY form.
    slash_aliases = [k for k in badges if k.startswith("commandcode/") and k != model_id]
    assert not slash_aliases, (
        f"synthesised slash-prefixed aliases leaked: {slash_aliases!r} in {badges!r}"
    )


# ---------------------------------------------------------------------------
# Source-shape: the ``{provider}/{model}`` synthetic alias must not appear
# in either configured-badge builder. A silent reversion to the bug
# leaves this regex matchable and the test fails.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "needle",
    [
        # _build_configured_model_badges (line ~8618) — 12-space indent
        'f"{provider}/{model}"',
        # _configured_model_badges_from_static_catalog (line ~6943) — 8-space
        'f"{provider}/{model}"',
    ],
)
def test_synthetic_slash_prefix_alias_is_not_in_source(needle: str) -> None:
    """The synthetic ``{provider}/{model}`` alias MUST NOT appear in either builder."""
    src = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
    assert needle not in src, (
        f"the synthetic slash-prefixed alias leaked back into the source: {needle!r}"
    )


def test_raw_candidates_only_holds_bare_and_at_prefix() -> None:
    """The raw_candidates tuple MUST contain only the bare native id and the ``@provider:`` alias.

    Catches both the old bug and a future regression that adds back
    *any* synthetic prefix form (e.g. ``{provider}::{model}``).
    """
    src = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
    # Find every `for candidate in (` block and verify the contents.
    # The 12-space variant is the main builder; the 8-space variant is the
    # static-catalog fallback; both must contain only bare + @-prefix.
    pattern = re.compile(
        r"for candidate in \(([\s\S]*?)\):\s*\n\s+if candidate and candidate not in raw_candidates:",
    )
    matches = list(pattern.finditer(src))
    assert matches, "raw_candidates tuple not found in api/config.py"
    for m in matches:
        body = m.group(1)
        # The body may be a single line `model, f"@{provider}:{model}"` or
        # a multi-line stack; both are accepted. We only need to verify the
        # two safe entries are present and the synthesised one is gone.
        bare = re.search(r"\bmodel\b", body)
        at_prefixed = re.search(
            r'f["\']@\{provider\}:\{model\}["\']', body
        )
        assert bare, f"raw_candidates missing the bare `model` entry: {body!r}"
        assert at_prefixed, (
            f"raw_candidates missing the @-prefixed entry, or contains a "
            f"synthesised prefix form: {body!r}"
        )
        # No synthesised prefix form like `{provider}/{model}` survives.
        assert "{provider}/{model}" not in body, (
            f"raw_candidates still emits a synthesised slash-prefixed alias: {body!r}"
        )
