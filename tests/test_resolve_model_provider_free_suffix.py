"""
Regression tests for resolve_model_provider — issue #1744.

When an OpenRouter model ID ends in a colon-suffixed tag like ``:free``,
``:beta``, ``:thinking``, the ``@provider:model`` qualifier produced by
``model_with_provider_context`` collides with the ``rsplit(":", 1)`` grammar
inside ``resolve_model_provider``.  The resolver would incorrectly peel the
suffix into the provider field instead of keeping it attached to the model.

E.g. ``@openrouter:tencent/hy3-preview:free`` was resolved as
``model="free", provider="openrouter:tencent/hy3-preview"`` instead of the
correct ``model="tencent/hy3-preview:free", provider="openrouter"``.

The fix (api/config.py ~line 1370) validates the rsplit result: if the
provider hint is not a known provider and not a custom provider, it falls
back to ``split(":", 1)`` so trailing suffixes stay with the model.
"""

from api.config import resolve_model_provider, model_with_provider_context


# ---------------------------------------------------------------------------
# Helper: simulate a config where provider != openrouter so that
# model_with_provider_context actually qualifies the ID.
# ---------------------------------------------------------------------------
def _set_config_provider(provider: str, default_model: str = "claude-sonnet-4.6"):
    """Temporarily set the model config provider for testing."""
    import api.config as cfg_mod
    old = dict(cfg_mod.cfg.get("model", {}))
    cfg_mod.cfg["model"] = {"provider": provider, "default": default_model}
    return old, cfg_mod


def _restore_config(old, cfg_mod):
    cfg_mod.cfg["model"] = old


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_openrouter_free_suffix_survives_provider_qualification():
    """tencent/hy3-preview:free must resolve correctly when qualified."""
    import api.config as cfg_mod
    old, cfg_mod = _set_config_provider("anthropic")
    try:
        qualified = model_with_provider_context("tencent/hy3-preview:free", "openrouter")
        model, provider, _ = resolve_model_provider(qualified)
        assert provider == "openrouter", f"expected provider='openrouter', got '{provider}'"
        assert model == "tencent/hy3-preview:free", f"expected model='tencent/hy3-preview:free', got '{model}'"
    finally:
        _restore_config(old, cfg_mod)


def test_openrouter_free_suffix_nvidia():
    """nvidia/nemotron-3-super-120b-a12b:free — same bug class."""
    import api.config as cfg_mod
    old, cfg_mod = _set_config_provider("anthropic")
    try:
        qualified = model_with_provider_context("nvidia/nemotron-3-super-120b-a12b:free", "openrouter")
        model, provider, _ = resolve_model_provider(qualified)
        assert provider == "openrouter"
        assert model == "nvidia/nemotron-3-super-120b-a12b:free"
    finally:
        _restore_config(old, cfg_mod)


def test_openrouter_free_suffix_arcee():
    """arcee-ai/trinity-large-preview:free — same bug class."""
    import api.config as cfg_mod
    old, cfg_mod = _set_config_provider("anthropic")
    try:
        qualified = model_with_provider_context("arcee-ai/trinity-large-preview:free", "openrouter")
        model, provider, _ = resolve_model_provider(qualified)
        assert provider == "openrouter"
        assert model == "arcee-ai/trinity-large-preview:free"
    finally:
        _restore_config(old, cfg_mod)


def test_openrouter_thinking_suffix():
    """Models ending in :thinking should also be preserved."""
    import api.config as cfg_mod
    old, cfg_mod = _set_config_provider("anthropic")
    try:
        qualified = model_with_provider_context("some/model:thinking", "openrouter")
        model, provider, _ = resolve_model_provider(qualified)
        assert provider == "openrouter"
        assert model == "some/model:thinking"
    finally:
        _restore_config(old, cfg_mod)


def test_custom_provider_rsplit_still_works():
    """custom:my-key:model must still parse correctly via rsplit."""
    qualified = "@custom:my-key:some-model"
    model, provider, _ = resolve_model_provider(qualified)
    assert provider == "custom:my-key", f"expected provider='custom:my-key', got '{provider}'"
    assert model == "some-model", f"expected model='some-model', got '{model}'"


def test_known_provider_single_colon():
    """@openrouter:simple-model — no suffix, should still work."""
    qualified = "@openrouter:simple-model"
    model, provider, _ = resolve_model_provider(qualified)
    assert provider == "openrouter"
    assert model == "simple-model"


def test_known_provider_anthropic():
    """@anthropic:claude-sonnet-4.6 — standard case."""
    qualified = "@anthropic:claude-sonnet-4.6"
    model, provider, _ = resolve_model_provider(qualified)
    assert provider == "anthropic"
    assert model == "claude-sonnet-4.6"
