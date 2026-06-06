"""Regression test for #3718 -- /api/models/live skips probe for custom providers.

When a custom provider (``custom_providers`` entry in config.yaml) has a
``model:`` field but no ``models:`` allowlist, the live endpoint previously
populated ``ids`` from the config entry and then skipped the live ``/v1/models``
probe (guarded by ``if not ids``).  The fix collects config-specified model IDs
in a separate ``_config_ids`` list so the live fetch always runs, and merges
config entries as a fallback after the fetch.
"""
import pathlib

ROUTES_PY = (pathlib.Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")


class TestLiveModelsCustomProviderProbe:
    """Live endpoint must probe the upstream /v1/models even when config has entries."""

    def test_config_ids_collected_separately(self):
        """Config-specified model IDs must go into ``_config_ids``, not ``ids``."""
        assert "_config_ids" in ROUTES_PY, (
            "routes.py must collect config model IDs in _config_ids, not ids (#3718)"
        )

    def test_live_fetch_not_guarded_by_ids(self):
        """The live fetch must not be guarded by ``if not ids`` for custom providers."""
        # The old code had: if not ids and (provider == "custom" ...
        # The fix changes it to: if provider == "custom" ...
        # Verify the guard ``not ids`` is NOT present before the custom-provider block.
        live_fetch_section = ROUTES_PY[ROUTES_PY.find("Always try live fetch for custom providers"):]
        assert "if not ids and" not in live_fetch_section.split("OpenAI-compat live fetch fallback")[0], (
            "Live fetch for custom providers must not be guarded by 'if not ids' (#3718)"
        )

    def test_merge_logic_after_live_fetch(self):
        """After the live fetch, config entries must be merged (not replaced)."""
        merge_section = ROUTES_PY[ROUTES_PY.find("live fetch succeeded, merge"):]
        assert "_live_set = set(ids)" in merge_section, (
            "Live fetch results must take priority over config entries (#3718)"
        )
        assert "ids.append(_cid)" in merge_section, (
            "Config entries not already in live results must be appended (#3718)"
        )
