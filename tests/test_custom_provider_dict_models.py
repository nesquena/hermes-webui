"""Regression tests for custom_providers models dict shape in get_providers().

The ``models`` field in a ``custom_providers`` config entry can be a list
or a dict (the standard shape produced by ``hermes config set``):

    custom_providers:
      - name: litellm
        model: Coding               # default / sticky metadata
        models:                     # ← dict: {model_id: {context_length: ...}}
          Best: {context_length: 128000}
          Coding: {context_length: 1000000}
          Fast: {}

get_providers() only handled the list shape, so the Providers settings card
showed just the single ``model`` field instead of the full catalog.
"""
import sys
import types


def _install_fake_hermes_cli(monkeypatch):
    """Stub hermes_cli modules so tests are deterministic and offline."""
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda pid: []

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)


def _setup_providers_module(monkeypatch):
    """Patch api.providers for offline unit testing of get_providers()."""
    _install_fake_hermes_cli(monkeypatch)

    from api import providers as prov

    monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {})
    monkeypatch.setattr(prov, "_PROVIDER_MODELS", {})
    monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
    monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
    monkeypatch.setattr(prov, "_provider_has_key", lambda _pid: False)

    def _invalidate():
        if hasattr(prov, "invalidate_providers_cache"):
            prov.invalidate_providers_cache()

    return prov, _invalidate


# ── Tests ─────────────────────────────────────────────────────────────────


class TestCustomProviderModelsDict:
    """get_providers() must expand dict-shaped models in custom_providers."""

    def test_dict_shaped_models_all_appear(self, monkeypatch):
        """Every key in a dict-shaped models field must become a model entry."""
        prov, _invalidate = _setup_providers_module(monkeypatch)
        monkeypatch.setattr(
            prov,
            "get_config",
            lambda: {
                "model": {"provider": "custom:litellm"},
                "custom_providers": [
                    {
                        "name": "litellm",
                        "model": "Coding",
                        "api_key": "sk-test",
                        "models": {
                            "Best": {"context_length": 128000},
                            "Coding": {"context_length": 1000000},
                            "Fast": {},
                            "minimax-m3": {},
                        },
                    },
                ],
            },
        )
        try:
            result = prov.get_providers()
            cp = next(p for p in result["providers"] if p.get("is_custom"))
            model_ids = [m["id"] for m in cp["models"]]
            assert set(model_ids) == {"Best", "Coding", "Fast", "minimax-m3"}
            assert cp["models_total"] == 4
        finally:
            _invalidate()

    def test_list_shaped_models_still_work(self, monkeypatch):
        """Regression guard: the existing list shape must keep working."""
        prov, _invalidate = _setup_providers_module(monkeypatch)
        monkeypatch.setattr(
            prov,
            "get_config",
            lambda: {
                "model": {"provider": "custom:litellm"},
                "custom_providers": [
                    {
                        "name": "litellm",
                        "model": "Coding",
                        "api_key": "sk-test",
                        "models": ["Coding", "Best", "Fast"],
                    },
                ],
            },
        )
        try:
            result = prov.get_providers()
            cp = next(p for p in result["providers"] if p.get("is_custom"))
            model_ids = [m["id"] for m in cp["models"]]
            assert set(model_ids) == {"Coding", "Best", "Fast"}
        finally:
            _invalidate()

    def test_singular_model_appears_first_before_dict_models(self, monkeypatch):
        """The singular ``model`` field appears first, then dict catalog models.

        This matches the sticky-before-plural ordering used by the model picker
        (api/config.py:7308-7314), so the Providers card stays consistent.
        The sticky model appears even when it is not a key in the models dict.
        """
        prov, _invalidate = _setup_providers_module(monkeypatch)
        monkeypatch.setattr(
            prov,
            "get_config",
            lambda: {
                "model": {"provider": "custom:litellm"},
                "custom_providers": [
                    {
                        "name": "litellm",
                        "model": "glm-5.2",
                        "api_key": "sk-test",
                        "models": {
                            "Best": {},
                            "Fast": {},
                        },
                    },
                ],
            },
        )
        try:
            result = prov.get_providers()
            cp = next(p for p in result["providers"] if p.get("is_custom"))
            model_ids = [m["id"] for m in cp["models"]]
            # Sticky model first, then dict keys.
            assert model_ids == ["glm-5.2", "Best", "Fast"]
        finally:
            _invalidate()

    def test_dict_models_no_duplicate_when_model_is_also_key(self, monkeypatch):
        """No duplicate entry when the singular ``model`` is also a dict key.

        Regression for the dedup contract: when ``model: Best`` and
        ``models: {Best: {}, ...}``, "Best" must appear exactly once.
        """
        prov, _invalidate = _setup_providers_module(monkeypatch)
        monkeypatch.setattr(
            prov,
            "get_config",
            lambda: {
                "model": {"provider": "custom:litellm"},
                "custom_providers": [
                    {
                        "name": "litellm",
                        "model": "Best",  # also a dict key
                        "api_key": "sk-test",
                        "models": {
                            "Best": {"context_length": 128000},
                            "Coding": {"context_length": 1000000},
                            "Fast": {},
                        },
                    },
                ],
            },
        )
        try:
            result = prov.get_providers()
            cp = next(p for p in result["providers"] if p.get("is_custom"))
            model_ids = [m["id"] for m in cp["models"]]
            # No duplicates, sticky-first then dict insertion order.
            assert model_ids == ["Best", "Coding", "Fast"]
            assert model_ids.count("Best") == 1
        finally:
            _invalidate()

    def test_empty_dict_falls_through_to_model_field(self, monkeypatch):
        """An empty dict falls through to the singular ``model`` field.

        This keeps the Providers card consistent with the model picker
        (get_available_models), which always surfaces the configured
        default model regardless of the ``models`` catalog shape.
        """
        prov, _invalidate = _setup_providers_module(monkeypatch)
        monkeypatch.setattr(
            prov,
            "get_config",
            lambda: {
                "model": {"provider": "custom:litellm"},
                "custom_providers": [
                    {
                        "name": "litellm",
                        "model": "Coding",
                        "api_key": "sk-test",
                        "models": {},
                    },
                ],
            },
        )
        try:
            result = prov.get_providers()
            cp = next(p for p in result["providers"] if p.get("is_custom"))
            # Empty dict falls through to the model field, consistent
            # with the model picker which always shows the default model.
            model_ids = [m["id"] for m in cp["models"]]
            assert model_ids == ["Coding"]
        finally:
            _invalidate()

    def test_whitespace_and_duplicate_keys_normalized(self, monkeypatch):
        """Whitespace-padded and duplicate dict keys collapse to clean entries.

        Matches _configured_model_ids behaviour: strip, drop empties, dedup.
        The singular model field (Coding) is prepended as sticky-first.
        """
        prov, _invalidate = _setup_providers_module(monkeypatch)
        monkeypatch.setattr(
            prov,
            "get_config",
            lambda: {
                "model": {"provider": "custom:litellm"},
                "custom_providers": [
                    {
                        "name": "litellm",
                        "model": "Coding",
                        "api_key": "sk-test",
                        "models": {
                            "Best": {},
                            " Best ": {},      # whitespace variant → deduped
                            "   ": {},          # whitespace-only → dropped
                        },
                    },
                ],
            },
        )
        try:
            result = prov.get_providers()
            cp = next(p for p in result["providers"] if p.get("is_custom"))
            model_ids = [m["id"] for m in cp["models"]]
            # Sticky-first, then normalized dict keys (no dupes, no empties).
            assert model_ids == ["Coding", "Best"]
            assert cp["models_total"] == 2
        finally:
            _invalidate()

    def test_whitespace_only_model_field_produces_no_entries(self, monkeypatch):
        """A whitespace-only singular model field emits no empty pills."""
        prov, _invalidate = _setup_providers_module(monkeypatch)
        monkeypatch.setattr(
            prov,
            "get_config",
            lambda: {
                "model": {"provider": "custom:litellm"},
                "custom_providers": [
                    {
                        "name": "litellm",
                        "model": "   ",  # whitespace-only → no fallback
                        "api_key": "sk-test",
                        "models": {},
                    },
                ],
            },
        )
        try:
            result = prov.get_providers()
            cp = next(p for p in result["providers"] if p.get("is_custom"))
            model_ids = [m["id"] for m in cp["models"]]
            assert model_ids == []
        finally:
            _invalidate()
