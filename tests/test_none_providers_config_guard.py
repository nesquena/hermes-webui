"""Regression guard: a config with an explicit null ``providers:`` key must not
break the provider-config read paths in onboarding / providers / routes, and
must behave exactly as if ``providers`` were an empty mapping.

Background (salvage of grab-bag PR #3967, thanks @lidi1011)
----------------------------------------------------------
``cfg.get("providers", {})`` only returns the ``{}`` default when the key is
*absent*. When ``config.yaml`` contains an explicit empty/null key::

    providers:

PyYAML parses that as ``None``, so ``cfg.get("providers", {})`` yields ``None``
(not ``{}``). master already wraps each of these reads in an
``isinstance(providers_cfg, dict)`` guard, so the None value silently disables
the provider lookup -- but the value handed to that guard is still a footgun:
any future maintainer who chains ``cfg.get("providers", {}).get(...)`` (the
natural-looking pattern, already used in api/config.py before its own #3967-style
fix) would crash with ``AttributeError: 'NoneType' object has no attribute
'get'``.

The hardening replaces every such read with ``cfg.get("providers") or {}`` so an
explicit null degrades to an empty dict at the *source*, matching the existing
api/config.py convention.

This module pins the fix two ways:

1. **Source-form pins** (``test_<file>_hardens_providers_key_read``): assert that
   onboarding / providers / routes no longer contain the unguarded
   ``cfg.get("providers", {})`` form and *do* contain the hardened
   ``cfg.get("providers") or {}`` form. These FAIL on master (where the
   unguarded form is present) and would FAIL again if anyone reverts the guard
   in any one of the three files -- so the test is non-vacuous per file.

2. **Behavioural checks**: drive the real functions with ``providers: None`` and
   assert they neither raise nor diverge from the ``providers: {}`` / absent-key
   behaviour. This guards the runtime contract end to end.
"""

import ast
import pathlib

import pytest

import api.config as config
import api.onboarding as onboarding
import api.providers as providers
import api.routes as routes

_REPO = pathlib.Path(__file__).resolve().parents[1]

_UNGUARDED = 'cfg.get("providers", {})'
_HARDENED = 'cfg.get("providers") or {}'


def _src(name: str) -> str:
    return (_REPO / "api" / name).read_text(encoding="utf-8")


# --- source-form pins (fail on master / on any per-file revert) -----------


@pytest.mark.parametrize("filename", ["onboarding.py", "providers.py", "routes.py"])
def test_file_hardens_providers_key_read_against_none(filename):
    """Each target file must read the providers key as ``... or {}``.

    Non-vacuous: on master the unguarded ``cfg.get("providers", {})`` form is
    present in every one of these files, so this assertion fails there. Reverting
    the guard in any single file re-fails it -- the guard is pinned per file.
    """
    source = _src(filename)
    # The hardened form must appear at least once.
    assert _HARDENED in source, (
        f"api/{filename} should read the providers key as `{_HARDENED}` so an "
        f"explicit null `providers:` degrades to an empty mapping (salvage #3967)."
    )
    # The unguarded form must not appear as live code. It may legitimately appear
    # inside an explanatory comment, so strip comment-only occurrences before
    # asserting absence.
    code_lines = []
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Drop trailing inline comments crudely (good enough: the token we look
        # for contains no '#').
        code_lines.append(line.split("#", 1)[0])
    code = "\n".join(code_lines)
    assert _UNGUARDED not in code, (
        f"api/{filename} still reads the providers key as the unguarded "
        f"`{_UNGUARDED}`; an explicit `providers:` (None) in config.yaml would "
        f"yield None instead of an empty mapping (salvage #3967)."
    )


@pytest.mark.parametrize("filename", ["onboarding.py", "providers.py", "routes.py"])
def test_target_files_parse(filename):
    """Sanity: the hardened files are valid Python (guards real edits)."""
    ast.parse(_src(filename), filename=filename)


# --- behavioural: onboarding ----------------------------------------------


def _onboarding_cfg(providers_value):
    return {
        "model": {"provider": "custom", "api_key": "", "base_url": "http://localhost:1"},
        "providers": providers_value,
        "custom_providers": [],
    }


def test_onboarding_provider_api_key_present_handles_none_providers():
    """``_provider_api_key_present`` must treat ``providers: None`` like ``{}``."""
    none_cfg = _onboarding_cfg(None)
    empty_cfg = _onboarding_cfg({})

    for provider in ("custom", "openai", "anthropic"):
        got_none = onboarding._provider_api_key_present(provider, none_cfg, {})
        got_empty = onboarding._provider_api_key_present(provider, empty_cfg, {})
        assert got_none == got_empty
        assert got_none is False  # no key configured anywhere


def test_onboarding_none_providers_does_not_mask_configured_key():
    """A provider key set under model.api_key is still found with providers: None."""
    cfg = _onboarding_cfg(None)
    cfg["model"] = {"provider": "custom", "api_key": "sk-xyz", "base_url": "http://x"}
    assert onboarding._provider_api_key_present("custom", cfg, {}) is True


# --- behavioural: providers (catalog + key lookup) ------------------------


def _patch_get_config(monkeypatch, cfg):
    """Force api.providers.get_config()/api.config.get_config() to return ``cfg``."""
    monkeypatch.setattr(providers, "get_config", lambda: cfg)
    # Some helpers import get_config from api.config at call time.
    monkeypatch.setattr(config, "get_config", lambda: cfg, raising=False)


def _providers_cfg(providers_value):
    return {
        "model": {"provider": "custom", "api_key": "", "base_url": "http://localhost:1"},
        "providers": providers_value,
        "custom_providers": [],
    }


def test_providers_has_key_handles_none_providers(monkeypatch):
    """``_provider_has_key`` must not crash and must match the empty-dict result."""
    monkeypatch.setattr(providers, "_provider_env_var_for", lambda pid: None)
    monkeypatch.setattr(
        providers, "_load_env_file", lambda path: {}, raising=False
    )

    for providers_value, label in ((None, "none"), ({}, "empty")):
        _patch_get_config(monkeypatch, _providers_cfg(providers_value))
        # Should return False (no key) without raising on the None config.
        assert providers._provider_has_key("custom") is False, label
        assert providers._get_provider_api_key("custom") is None, label


# --- behavioural: routes (context-length helper + key resolution) ----------


def test_routes_context_length_helper_handles_none_providers():
    """``_context_length_lookup_inputs_for_model`` must accept providers: None."""
    cfg_none = {
        "model": {"provider": "custom", "base_url": "http://localhost:1"},
        "providers": None,
        "custom_providers": [],
    }
    cfg_empty = dict(cfg_none, providers={})

    # Pass cfg explicitly so the helper does not hit real config/disk.
    out_none = routes._context_length_lookup_inputs_for_model(
        "@custom:my-model", "custom", cfg=cfg_none
    )
    out_empty = routes._context_length_lookup_inputs_for_model(
        "@custom:my-model", "custom", cfg=cfg_empty
    )
    # No crash, and the None config produces the same lookup inputs as the
    # empty-mapping config (no provider override available in either case).
    # ``_ContextLengthLookupInputs`` is a __slots__ class without __eq__, so
    # compare the materialised fields rather than object identity.
    fields = ("config_context_length", "custom_providers", "base_url", "provider", "api_key")
    snapshot_none = {f: getattr(out_none, f) for f in fields}
    snapshot_empty = {f: getattr(out_empty, f) for f in fields}
    assert snapshot_none == snapshot_empty
