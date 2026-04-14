"""Tests for fix: onboarding wizard must not fire when Hermes is already configured.

Issue #420 — existing Hermes users (config.yaml present + chat_ready) were
shown the first-run wizard because the only gate was settings.onboarding_completed.

Covers:
  (a) config.yaml present + chat_ready=True  →  completed=True (no wizard)
  (b) no config.yaml                         →  completed=False (wizard fires)
  (c) apply_onboarding_setup refuses to overwrite an existing config without
      confirm_overwrite=True
"""
from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.request
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Unit tests — no live server needed, test logic directly via imports
# ---------------------------------------------------------------------------


def _make_status(*, config_exists: bool, chat_ready: bool, onboarding_done: bool = False):
    """Call get_onboarding_status() with a controlled filesystem + settings."""
    import importlib

    # Import fresh copies each call so module-level state doesn't bleed across
    import api.onboarding as mod

    fake_config_path = pathlib.Path("/tmp/_test_config.yaml")

    settings = {"onboarding_completed": onboarding_done}

    # Build a minimal runtime dict that get_onboarding_status() would produce
    # from _status_from_runtime.  We only need the keys the gate checks.
    runtime = {
        "chat_ready": chat_ready,
        "provider_configured": chat_ready,
        "provider_ready": chat_ready,
        "setup_state": "ready" if chat_ready else "needs_provider",
        "provider_note": "test note",
        "current_provider": "openrouter" if chat_ready else None,
        "current_model": "anthropic/claude-sonnet-4.6" if chat_ready else None,
        "current_base_url": None,
        "env_path": "/tmp/.hermes_test/.env",
    }

    with (
        mock.patch.object(mod, "load_settings", return_value=settings),
        mock.patch.object(mod, "get_config", return_value={}),
        mock.patch.object(
            mod,
            "verify_hermes_imports",
            return_value=(chat_ready, [], {}),
        ),
        mock.patch.object(mod, "_status_from_runtime", return_value=runtime),
        mock.patch.object(mod, "load_workspaces", return_value=[]),
        mock.patch.object(mod, "get_last_workspace", return_value=None),
        mock.patch.object(mod, "get_available_models", return_value=[]),
        mock.patch.object(mod, "_get_config_path", return_value=fake_config_path),
        mock.patch.object(pathlib.Path, "exists") as mock_exists,
    ):
        # Make Path(_get_config_path()).exists() return config_exists
        mock_exists.return_value = config_exists
        result = mod.get_onboarding_status()

    return result


class TestOnboardingGate:
    def test_config_exists_and_chat_ready_returns_completed_true(self):
        """Primary fix: existing valid config → wizard must NOT fire."""
        result = _make_status(config_exists=True, chat_ready=True)
        assert result["completed"] is True, (
            "Wizard fired for existing Hermes user! "
            "config.yaml + chat_ready must auto-complete onboarding."
        )

    def test_no_config_returns_completed_false(self):
        """Fresh install with no config → wizard should fire."""
        result = _make_status(config_exists=False, chat_ready=False)
        assert result["completed"] is False, (
            "Fresh install must show the wizard (completed should be False)."
        )

    def test_config_exists_but_not_chat_ready_still_shows_wizard(self):
        """Broken/incomplete config (config.yaml exists but chat_ready=False) →
        still show wizard so the user can fix it."""
        result = _make_status(config_exists=True, chat_ready=False)
        # Should NOT be auto-completed — config is present but broken
        assert result["completed"] is False, (
            "Broken config (chat_ready=False) must still show the wizard."
        )

    def test_onboarding_done_flag_always_respected(self):
        """If user already completed onboarding in settings, never show wizard."""
        result = _make_status(config_exists=False, chat_ready=False, onboarding_done=True)
        assert result["completed"] is True

    def test_config_exists_always_exposed_in_system(self):
        """config_exists must still appear in the response system block."""
        result = _make_status(config_exists=True, chat_ready=True)
        assert "config_exists" in result["system"]
        assert result["system"]["config_exists"] is True


class TestApplyOnboardingSetupGuard:
    """Fix #2: apply_onboarding_setup must not silently overwrite config.yaml."""

    def _call_setup(self, body: dict, config_yaml_exists: bool):
        import api.onboarding as mod

        fake_config_path = pathlib.Path("/tmp/_test_config.yaml")

        with (
            mock.patch.object(mod, "_get_config_path", return_value=fake_config_path),
            mock.patch.object(pathlib.Path, "exists", return_value=config_yaml_exists),
        ):
            return mod.apply_onboarding_setup(body)

    def test_setup_blocked_when_config_exists_without_confirm(self):
        """Must return an error dict (not raise) if config.yaml exists and no confirm_overwrite."""
        result = self._call_setup(
            {
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.6",
                "api_key": "test-key",
            },
            config_yaml_exists=True,
        )
        assert isinstance(result, dict), "Expected a dict response, not an exception"
        assert result.get("error") == "config_exists", (
            f"Expected error='config_exists', got: {result}"
        )
        assert result.get("requires_confirm") is True

    def test_setup_allowed_with_confirm_overwrite(self):
        """With confirm_overwrite=True, setup may proceed (will hit real logic)."""
        import api.onboarding as mod

        fake_config_path = pathlib.Path("/tmp/_test_config_confirm.yaml")
        fake_config_path.unlink(missing_ok=True)  # start clean
        try:
            # Without patching Path.exists, use a non-existent path so it won't block
            result = mod.apply_onboarding_setup(
                {
                    "provider": "openrouter",
                    "model": "anthropic/claude-sonnet-4.6",
                    "api_key": "test-key-confirm",
                    "confirm_overwrite": True,
                }
            )
            # Should NOT return config_exists error
            if isinstance(result, dict):
                assert result.get("error") != "config_exists", (
                    "confirm_overwrite=True should bypass the config-exists guard."
                )
        finally:
            fake_config_path.unlink(missing_ok=True)

    def test_setup_allowed_when_no_config_exists(self):
        """Fresh install: no config.yaml → setup proceeds normally (no blocking error)."""
        import api.onboarding as mod

        fake_config_path = pathlib.Path("/tmp/_test_config_fresh.yaml")
        fake_config_path.unlink(missing_ok=True)
        try:
            with mock.patch.object(mod, "_get_config_path", return_value=fake_config_path):
                result = mod.apply_onboarding_setup(
                    {
                        "provider": "openrouter",
                        "model": "anthropic/claude-sonnet-4.6",
                        "api_key": "test-key-fresh",
                    }
                )
            if isinstance(result, dict):
                assert result.get("error") != "config_exists"
        finally:
            fake_config_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Integration tests — require the live test server on port 8788
# ---------------------------------------------------------------------------

BASE = "http://127.0.0.1:8788"


def _http_get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def _http_post(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def _server_hermes_home() -> pathlib.Path:
    data, _ = _http_get("/api/onboarding/status")
    env_path = data.get("system", {}).get("env_path", "")
    if env_path:
        return pathlib.Path(env_path).parent
    return pathlib.Path.home() / ".hermes" / "webui-mvp-test"


def _server_reachable() -> bool:
    try:
        _http_get("/health")
        return True
    except Exception:
        return False


# Mark integration tests to only run when test server is up
requires_server = pytest.mark.skipif(
    not _server_reachable(),
    reason="Test server on :8788 not reachable",
)


try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

_needs_yaml = pytest.mark.skipif(
    not _HAS_YAML, reason="PyYAML not installed"
)


@requires_server
class TestOnboardingGateIntegration:
    """Live-server integration tests for the onboarding gate fix."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        hermes_home = _server_hermes_home()
        for rel in ("config.yaml", ".env"):
            (hermes_home / rel).unlink(missing_ok=True)
        yield
        for rel in ("config.yaml", ".env"):
            (hermes_home / rel).unlink(missing_ok=True)

    def test_no_config_wizard_fires(self):
        """No config.yaml → completed=False."""
        data, status = _http_get("/api/onboarding/status")
        assert status == 200
        assert data["completed"] is False

    @_needs_yaml
    def test_existing_config_and_chat_ready_skips_wizard(self):
        """Write a valid config.yaml + .env → completed must be True."""
        import yaml

        hermes_home = _server_hermes_home()
        # Write a real config.yaml
        cfg = {"model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"}}
        (hermes_home / "config.yaml").write_text(
            yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8"
        )
        # Write a fake API key so provider_ready (and thus chat_ready) fires
        # — but only when hermes_cli imports are available
        data, _ = _http_get("/api/onboarding/status")
        if data["system"]["hermes_found"] and data["system"]["imports_ok"]:
            (hermes_home / ".env").write_text(
                "OPENROUTER_API_KEY=test-existing-key\n", encoding="utf-8"
            )
            data, status = _http_get("/api/onboarding/status")
            assert status == 200
            assert data["completed"] is True, (
                "Existing config + chat_ready must auto-complete onboarding."
            )
        else:
            # Agent not installed: chat_ready is always False, so wizard still
            # fires — that is the correct behaviour (can't verify readiness).
            assert data["completed"] is False

    @_needs_yaml
    def test_setup_blocked_for_existing_config(self):
        """POST /api/onboarding/setup must return config_exists error if config.yaml exists."""
        import yaml

        hermes_home = _server_hermes_home()
        cfg = {"model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"}}
        (hermes_home / "config.yaml").write_text(
            yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8"
        )

        data, status = _http_post(
            "/api/onboarding/setup",
            {
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.6",
                "api_key": "test-key",
            },
        )
        assert status == 200
        assert data.get("error") == "config_exists", (
            f"Expected config_exists guard. Got: {data}"
        )
        assert data.get("requires_confirm") is True

    @_needs_yaml
    def test_setup_allowed_with_confirm_overwrite(self):
        """POST /api/onboarding/setup with confirm_overwrite=True succeeds."""
        import yaml

        hermes_home = _server_hermes_home()
        cfg = {"model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"}}
        (hermes_home / "config.yaml").write_text(
            yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8"
        )

        data, status = _http_post(
            "/api/onboarding/setup",
            {
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.6",
                "api_key": "test-key",
                "confirm_overwrite": True,
            },
        )
        assert status == 200
        assert data.get("error") != "config_exists", (
            "confirm_overwrite=True must bypass the guard."
        )
