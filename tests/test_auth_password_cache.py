"""
Tests for the password hash cache invalidation hook.

Verifies that changing the password via save_settings() takes effect
immediately in the running process — without a restart.

Regression: before the invalidation hook was added to save_settings(),
_AUTH_HASH_COMPUTED stayed True and get_password_hash() returned the
stale hash from before the UI password change.
"""
import os
import pathlib
import tempfile
import unittest

_TEST_STATE = pathlib.Path(tempfile.mkdtemp())
os.environ["HERMES_WEBUI_STATE_DIR"] = str(_TEST_STATE)

import sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import importlib

auth = importlib.import_module("api.auth")
config = importlib.import_module("api.config")


def _reset_cache():
    auth._invalidate_password_hash_cache()


class TestPasswordCacheInvalidation(unittest.TestCase):

    def setUp(self):
        _reset_cache()
        # Ensure no env-var password interferes
        os.environ.pop("HERMES_WEBUI_PASSWORD", None)

    def tearDown(self):
        _reset_cache()
        os.environ.pop("HERMES_WEBUI_PASSWORD", None)

    def test_set_password_takes_effect_without_restart(self):
        config.save_settings({"_set_password": "first"})
        self.assertTrue(auth.verify_password("first"))

        config.save_settings({"_set_password": "second"})
        # Cache must be invalidated; old password must no longer verify
        self.assertFalse(auth.verify_password("first"),
                         "stale hash still accepted after password change — cache not invalidated")
        self.assertTrue(auth.verify_password("second"))

    def test_clear_password_takes_effect_without_restart(self):
        config.save_settings({"_set_password": "secret"})
        self.assertTrue(auth.is_auth_enabled())

        config.save_settings({"_clear_password": True})
        # Cache must be invalidated; auth must be disabled immediately
        self.assertFalse(auth.is_auth_enabled(),
                         "auth still enabled after clear — cache not invalidated")
        self.assertFalse(auth.verify_password("secret"))

    def test_cache_repopulates_after_invalidation(self):
        config.save_settings({"_set_password": "pw"})
        # Warm the cache
        auth.get_password_hash()
        # Invalidate and warm again — must reflect current settings.json
        _reset_cache()
        self.assertTrue(auth.verify_password("pw"))


if __name__ == "__main__":
    unittest.main()
