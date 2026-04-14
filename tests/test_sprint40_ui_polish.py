"""
Tests for UI polish fixes - Issue #443
Gateway session null model should return None not 'unknown'.
"""
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestGatewaySessionNullModel(unittest.TestCase):
    """Verify that api/models.py and api/gateway_watcher.py do not
    fall back to the string 'unknown' for missing model values."""

    def test_gateway_session_null_model_returns_none_not_unknown(self):
        """api/models.py must not use `or 'unknown'` for the model field
        so that a NULL model in state.db is returned as None (falsy) to
        the frontend rather than the truthy string 'unknown'."""
        models_src = (REPO_ROOT / "api" / "models.py").read_text()
        # Ensure the old fallback pattern is gone
        self.assertNotIn(
            "'model': row['model'] or 'unknown'",
            models_src,
            "api/models.py must not use `or 'unknown'` for the model field "
            "(fixes #443: gateway sessions showed 'telegram · unknown')",
        )

    def test_gateway_watcher_null_model_returns_none_not_unknown(self):
        """api/gateway_watcher.py must not use `or 'unknown'` for the model
        field so that a NULL model in state.db is returned as None (falsy)."""
        gw_src = (REPO_ROOT / "api" / "gateway_watcher.py").read_text()
        self.assertNotIn(
            "'model': row['model'] or 'unknown'",
            gw_src,
            "api/gateway_watcher.py must not use `or 'unknown'` for the model "
            "field (fixes #443: gateway sessions showed 'telegram · unknown')",
        )

    def test_gateway_session_model_uses_none_fallback(self):
        """Both source files must use `row['model'] or None` (explicit None
        fallback) for the model field assignment."""
        models_src = (REPO_ROOT / "api" / "models.py").read_text()
        gw_src = (REPO_ROOT / "api" / "gateway_watcher.py").read_text()
        self.assertIn(
            "'model': row['model'] or None,",
            models_src,
            "api/models.py should assign `row['model'] or None` for the model field",
        )
        self.assertIn(
            "'model': row['model'] or None,",
            gw_src,
            "api/gateway_watcher.py should assign `row['model'] or None` for the model field",
        )


if __name__ == "__main__":
    unittest.main()
