"""When SSO is on, onboarding does not gate on a Hermes-side password.
Provider-setup and workspace-setup steps are unchanged.
"""
import os
from unittest.mock import patch

from api.onboarding import get_onboarding_status


def test_password_step_marked_complete_when_sso_on():
    with patch.dict(os.environ, {"VENDO_AUTH": "1"}, clear=False):
        status = get_onboarding_status()
        assert status["settings"]["password_enabled"] is True


def test_password_state_unchanged_when_sso_off():
    env = {k: v for k, v in os.environ.items()
           if k not in ("VENDO_AUTH", "HERMES_WEBUI_PASSWORD")}
    with patch.dict(os.environ, env, clear=True):
        status = get_onboarding_status()
        # When VENDO_AUTH is off and no password set, password_enabled should be False
        # (existing behavior - relies on is_auth_enabled() returning False).
        assert status["settings"]["password_enabled"] is False
