"""
Sprint 39 Tests: Skip-onboarding env var + onboarding key reload fix (PR A of issue #329).

Covers:
- HERMES_WEBUI_SKIP_ONBOARDING=1 bypasses the wizard unconditionally (chat_ready not required)
- HERMES_WEBUI_SKIP_ONBOARDING unset leaves default behaviour unchanged
- apply_onboarding_setup sets os.environ synchronously when an API key is saved
- apply_onboarding_setup refuses to write config/env files when SKIP_ONBOARDING is set
"""
import os
import unittest
import unittest.mock
from unittest.mock import patch

import api.onboarding as mod


_READY_RUNTIME = {
    "chat_ready": True,
    "provider_configured": True,
    "provider_ready": True,
    "setup_state": "ready",
    "provider_note": "Ready",
    "current_provider": "openai",
    "current_model": "gpt-4o",
    "current_base_url": None,
    "env_path": "/tmp/test.env",
}

_NOT_READY_RUNTIME = {
    "chat_ready": False,
    "provider_configured": False,
    "provider_ready": False,
    "setup_state": "needs_provider",
    "provider_note": "Needs setup",
    "current_provider": None,
    "current_model": None,
    "current_base_url": None,
    "env_path": "/tmp/test.env",
}

_COMMON_PATCHES = [
    ("api.onboarding.load_settings",        lambda: {}),
    ("api.onboarding.get_config",           lambda: {}),
    ("api.onboarding.verify_hermes_imports",lambda: (True, [], [])),
    ("api.onboarding.load_workspaces",      lambda: []),
    ("api.onboarding.get_last_workspace",   lambda: "/tmp"),
    ("api.onboarding.get_available_models", lambda: []),
    ("api.onboarding.is_auth_enabled",      lambda: False),
    ("api.onboarding._build_setup_catalog", lambda cfg: {}),
    ("api.onboarding._get_config_path",     lambda: __import__("pathlib").Path("/tmp/fake.yaml")),
]


def _apply_patches(extra_patches=()):
    patches = []
    for target, side_effect in _COMMON_PATCHES:
        p = patch(target, side_effect=side_effect)
        patches.append(p)
    for target, side_effect in extra_patches:
        p = patch(target, side_effect=side_effect)
        patches.append(p)
    return patches


class TestSkipOnboardingEnvVar(unittest.TestCase):

    def _run_status(self, runtime, env_override):
        runtime_patches = [("api.onboarding._status_from_runtime", lambda cfg, ok: runtime)]
        all_patches = _apply_patches(runtime_patches)
        with patch.dict(os.environ, env_override, clear=False):
            for p in all_patches:
                p.start()
            try:
                return mod.get_onboarding_status()
            finally:
                for p in all_patches:
                    p.stop()

    def test_skip_env_1_and_chat_ready_marks_completed(self):
        """HERMES_WEBUI_SKIP_ONBOARDING=1 + chat_ready=True → completed=True."""
        status = self._run_status(_READY_RUNTIME, {"HERMES_WEBUI_SKIP_ONBOARDING": "1"})
        self.assertTrue(status["completed"],
                        "completed must be True when skip env var is 1 and chat_ready")

    def test_skip_env_true_and_chat_ready_marks_completed(self):
        """HERMES_WEBUI_SKIP_ONBOARDING=true also accepted."""
        status = self._run_status(_READY_RUNTIME, {"HERMES_WEBUI_SKIP_ONBOARDING": "true"})
        self.assertTrue(status["completed"])

    def test_skip_env_yes_and_chat_ready_marks_completed(self):
        """HERMES_WEBUI_SKIP_ONBOARDING=yes also accepted."""
        status = self._run_status(_READY_RUNTIME, {"HERMES_WEBUI_SKIP_ONBOARDING": "yes"})
        self.assertTrue(status["completed"])

    def test_skip_env_1_works_even_when_not_chat_ready(self):
        """HERMES_WEBUI_SKIP_ONBOARDING=1 skips unconditionally — chat_ready is NOT required."""
        status = self._run_status(_NOT_READY_RUNTIME, {"HERMES_WEBUI_SKIP_ONBOARDING": "1"})
        self.assertTrue(status["completed"],
                        "completed must be True when skip env var is set, regardless of chat_ready")

    def test_skip_env_unset_leaves_default_false(self):
        """Without the env var, completed is False when settings are empty."""
        env = {k: v for k, v in os.environ.items() if k != "HERMES_WEBUI_SKIP_ONBOARDING"}
        with patch.dict(os.environ, env, clear=True):
            status = self._run_status(_READY_RUNTIME, {})
        self.assertFalse(status["completed"],
                         "completed must be False when env var absent and settings empty")

    def test_settings_completed_still_works_without_env_var(self):
        """onboarding_completed in settings → completed=True regardless of env var."""
        runtime_patches = [("api.onboarding._status_from_runtime", lambda cfg, ok: _READY_RUNTIME)]
        settings_patch = [("api.onboarding.load_settings", lambda: {"onboarding_completed": True})]
        all_patches = _apply_patches(runtime_patches + settings_patch)
        env = {k: v for k, v in os.environ.items() if k != "HERMES_WEBUI_SKIP_ONBOARDING"}
        with patch.dict(os.environ, env, clear=True):
            for p in all_patches:
                p.start()
            try:
                status = mod.get_onboarding_status()
            finally:
                for p in all_patches:
                    p.stop()
        self.assertTrue(status["completed"])


class TestApplyOnboardingKeySync(unittest.TestCase):
    """Verify that apply_onboarding_setup sets os.environ synchronously."""

    def test_api_key_set_in_os_environ_after_apply(self):
        """After apply_onboarding_setup with a key, os.environ must have the key."""
        import pathlib

        os.environ.pop("OPENAI_API_KEY", None)

        mock_cfg = {"model": {"provider": "openai", "default": "gpt-4o"}}

        with patch("api.onboarding._load_yaml_config", return_value=mock_cfg), \
             patch("api.onboarding._save_yaml_config"), \
             patch("api.onboarding._write_env_file"), \
             patch("api.onboarding.reload_config"), \
             patch("api.onboarding.get_onboarding_status", return_value={"completed": True}), \
             patch("api.onboarding._get_config_path", return_value=pathlib.Path("/tmp/fake.yaml")), \
             patch("api.onboarding._load_env_file", return_value={}), \
             patch("api.onboarding._provider_api_key_present", return_value=False), \
             patch("api.onboarding._get_active_hermes_home", return_value=pathlib.Path("/tmp")):

            mod.apply_onboarding_setup({
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-test-key-123",
            })

        self.assertEqual(os.environ.get("OPENAI_API_KEY"), "sk-test-key-123",
                         "OPENAI_API_KEY must be set directly on os.environ after apply")
        os.environ.pop("OPENAI_API_KEY", None)

    def test_no_key_provided_does_not_set_environ(self):
        """If no api_key is given (key already present), os.environ is not clobbered."""
        import pathlib

        os.environ["OPENAI_API_KEY"] = "sk-existing-key"

        mock_cfg = {"model": {"provider": "openai", "default": "gpt-4o"}}

        with patch("api.onboarding._load_yaml_config", return_value=mock_cfg), \
             patch("api.onboarding._save_yaml_config"), \
             patch("api.onboarding._write_env_file"), \
             patch("api.onboarding.reload_config"), \
             patch("api.onboarding.get_onboarding_status", return_value={"completed": True}), \
             patch("api.onboarding._get_config_path", return_value=pathlib.Path("/tmp/fake.yaml")), \
             patch("api.onboarding._load_env_file", return_value={"OPENAI_API_KEY": "sk-existing-key"}), \
             patch("api.onboarding._provider_api_key_present", return_value=True), \
             patch("api.onboarding._get_active_hermes_home", return_value=pathlib.Path("/tmp")):

            mod.apply_onboarding_setup({
                "provider": "openai",
                "model": "gpt-4o",
            })

        # Key must be unchanged
        self.assertEqual(os.environ.get("OPENAI_API_KEY"), "sk-existing-key")
        os.environ.pop("OPENAI_API_KEY", None)


class TestApplyOnboardingSkipGuard(unittest.TestCase):
    """apply_onboarding_setup must not write config/env when SKIP_ONBOARDING is set."""

    def test_apply_setup_blocked_when_skip_env_set(self):
        """SKIP_ONBOARDING=1 → apply_onboarding_setup never touches disk."""
        save_yaml_mock = unittest.mock.MagicMock()
        write_env_mock = unittest.mock.MagicMock()

        with patch.dict(os.environ, {"HERMES_WEBUI_SKIP_ONBOARDING": "1"}, clear=False), \
             patch("api.onboarding._save_yaml_config", save_yaml_mock), \
             patch("api.onboarding._write_env_file", write_env_mock), \
             patch("api.onboarding.save_settings"), \
             patch("api.onboarding.get_onboarding_status", return_value={"completed": True}):
            mod.apply_onboarding_setup({
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "should-not-be-saved",
            })

        save_yaml_mock.assert_not_called()
        write_env_mock.assert_not_called()

    def test_apply_setup_proceeds_normally_without_skip_env(self):
        """Without SKIP_ONBOARDING, apply_onboarding_setup writes config as usual."""
        import pathlib
        save_yaml_mock = unittest.mock.MagicMock()

        mock_cfg = {"model": {"provider": "openai", "default": "gpt-4o"}}
        env = {k: v for k, v in os.environ.items() if k != "HERMES_WEBUI_SKIP_ONBOARDING"}

        with patch.dict(os.environ, env, clear=True), \
             patch("api.onboarding._load_yaml_config", return_value=mock_cfg), \
             patch("api.onboarding._save_yaml_config", save_yaml_mock), \
             patch("api.onboarding._write_env_file"), \
             patch("api.onboarding.reload_config"), \
             patch("api.onboarding.get_onboarding_status", return_value={"completed": True}), \
             patch("api.onboarding._get_config_path", return_value=pathlib.Path("/tmp/fake.yaml")), \
             patch("api.onboarding._load_env_file", return_value={"OPENAI_API_KEY": "existing"}), \
             patch("api.onboarding._provider_api_key_present", return_value=True), \
             patch("api.onboarding._get_active_hermes_home", return_value=pathlib.Path("/tmp")):
            mod.apply_onboarding_setup({
                "provider": "openai",
                "model": "gpt-4o",
            })

        save_yaml_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
