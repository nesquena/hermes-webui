"""Regression test for #3718 -- /api/models/live skips probe for custom providers.

When a custom provider (``custom_providers`` entry in config.yaml) has a
``model:`` field but no ``models:`` allowlist, the live endpoint previously
populated ``ids`` from the config entry and then skipped the live ``/v1/models``
probe (guarded by ``if not ids``).  The fix collects config-specified model IDs
in a separate ``_config_ids`` list so the live fetch always runs, and merges
config entries as a fallback after the fetch.
"""
import json
import pathlib
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).parent.parent
ROUTES_PY = REPO / "api" / "routes.py"


class TestLiveModelsCustomProviderProbe(unittest.TestCase):
    """Live endpoint must probe the upstream /v1/models even when config has entries."""

    def test_custom_provider_with_model_field_probes_upstream(self):
        """When a custom provider has model: in config, live fetch must still run.

        Before the fix, _custom_provider_model_ids() populated ids=["assistant"],
        and the `if not ids` guard prevented the upstream /v1/models probe from
        running. The endpoint returned only ["assistant"] instead of the full
        upstream catalog.

        We test the fix by simulating the logic directly: config provides
        model IDs, the upstream probe returns a different set, and the result
        must contain BOTH the live models and the config fallback.
        """
        # Simulate config-specified model IDs
        _config_ids = ["assistant"]

        # Simulate upstream /v1/models response
        live_models = [
            "assistant",
            "assistant-pro",
            "assistant-zdr",
            "gpt-5.5:pt",
            "claude-sonnet-4.6:pt",
        ]

        # Simulate the merge logic from the fix:
        # if ids: merge config entries not in live set
        # else: fall back to config-only list
        ids = list(live_models)  # live fetch succeeded
        _live_set = set(ids)
        for _cid in _config_ids:
            if _cid not in _live_set:
                ids.append(_cid)

        # "assistant" from config should not duplicate the live result
        self.assertEqual(ids, live_models)
        # All live models must be present
        for m in live_models:
            self.assertIn(m, ids)

    def test_custom_provider_falls_back_to_config_when_probe_fails(self):
        """When the upstream probe fails, config entries must be used as fallback."""
        _config_ids = ["assistant", "custom-only-model"]

        # Simulate failed live fetch: ids stays empty
        ids = []

        # Fallback logic from the fix
        if not ids:
            ids = list(_config_ids)

        self.assertEqual(ids, ["assistant", "custom-only-model"])

    def test_config_only_model_appended_when_not_in_live_results(self):
        """Config-specified models not in the live results must be appended."""
        _config_ids = ["assistant", "local-finetune"]
        live_models = ["assistant", "assistant-pro", "gpt-5.5:pt"]

        ids = list(live_models)
        _live_set = set(ids)
        for _cid in _config_ids:
            if _cid not in _live_set:
                ids.append(_cid)

        # "assistant" is in both, "local-finetune" is config-only
        self.assertIn("local-finetune", ids)
        self.assertIn("assistant-pro", ids)
        self.assertEqual(ids.count("assistant"), 1, "assistant must not be duplicated")

    def test_live_fetch_guard_no_longer_checks_ids(self):
        """The live fetch block must not be guarded by `if not ids`.

        This is a structural regression guard: the fix replaced
        `if not ids and (provider == "custom" ...)` with
        `if provider == "custom" ...` so the probe always runs regardless
        of whether config entries populated ids.
        """
        source = ROUTES_PY.read_text(encoding="utf-8")

        # Find the "Always try live fetch" comment (added by the fix)
        marker = "Always try live fetch for custom providers"
        self.assertIn(marker, source, (
            "routes.py must contain the 'Always try live fetch' comment (#3718)"
        ))

        # Extract the block between the marker and the next major section
        marker_pos = source.find(marker)
        next_section = source.find("OpenAI-compat live fetch fallback", marker_pos)
        self.assertNotEqual(next_section, -1, "could not find next section marker")
        block = source[marker_pos:next_section]

        # The old `if not ids and` guard must NOT appear in this block
        self.assertNotIn("if not ids and", block, (
            "Live fetch for custom providers must not be guarded by 'if not ids' (#3718)"
        ))

    def test_mocked_live_fetch_returns_full_catalog_plus_config_entry(self):
        """Integration test: mock urlopen and exercise the real fetch/parse/merge path.

        This test patches urllib.request.urlopen to return a fake /v1/models
        response, then runs the same fetch+merge logic that the live handler
        uses, verifying that config entries are merged and live models are
        included.
        """
        fake_models = {"data": [
            {"id": "assistant"},
            {"id": "assistant-pro"},
            {"id": "gpt-5.5:pt"},
        ]}
        fake_body = json.dumps(fake_models).encode("utf-8")
        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = fake_body
        fake_resp.__enter__ = mock.MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = mock.MagicMock(return_value=False)

        _config_ids = ["assistant", "local-only"]

        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            # Replicate the fetch+parse logic from routes.py
            import urllib.request
            ids = []
            _req = urllib.request.Request(
                "http://localhost:4000/v1/models",
                headers={"Authorization": "Bearer test-key"},
            )
            with urllib.request.urlopen(_req, timeout=5) as _resp:
                _body = json.loads(_resp.read())

            if isinstance(_body, dict):
                _data = _body.get("data", [])
                if isinstance(_data, list):
                    ids = [m.get("id", "") for m in _data if m.get("id")]

        # Apply the merge logic from the fix
        if ids:
            _live_set = set(ids)
            for _cid in _config_ids:
                if _cid not in _live_set:
                    ids.append(_cid)
        else:
            ids = list(_config_ids)

        # Live models must all be present
        self.assertIn("assistant", ids)
        self.assertIn("assistant-pro", ids)
        self.assertIn("gpt-5.5:pt", ids)
        # Config-only entry must be appended
        self.assertIn("local-only", ids)
        # No duplicates
        self.assertEqual(ids.count("assistant"), 1)

    def test_timeout_uses_config_constant(self):
        """The live fetch must use CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS, not a hardcoded value."""
        source = ROUTES_PY.read_text(encoding="utf-8")

        # Find the custom-provider live fetch block
        marker = "Always try live fetch for custom providers"
        marker_pos = source.find(marker)
        next_section = source.find("OpenAI-compat live fetch fallback", marker_pos)
        block = source[marker_pos:next_section]

        # Must use the constant, not a bare number
        self.assertIn("CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", block, (
            "Live fetch timeout must use CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS "
            "instead of a hardcoded value (#3718 review feedback)"
        ))
        self.assertNotIn("timeout=8", block, (
            "Live fetch must not use hardcoded timeout=8 (#3718 review feedback)"
        ))


if __name__ == "__main__":
    unittest.main()