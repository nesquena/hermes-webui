"""Regression coverage for WebUI session personality defaults.

``display.personality`` is a legacy/global config key.  New WebUI sessions must
not silently inherit it, because profile response mode already controls whether
the agent uses a built-in style overlay or the baseline SOUL-driven behavior.
"""

from unittest.mock import patch

import pytest


def _new_session_with_config(config_data):
    import api.config as cfg_mod
    import api.models as models

    with patch.object(cfg_mod, "get_config", return_value=config_data), \
         patch.object(models.Session, "save", return_value=None):
        return models.new_session(workspace="/tmp/test-personality")


@pytest.mark.parametrize(
    "personality_value",
    ["kawaii", "taleb", "teacher", "technical", "hype", "concise"],
)
def test_new_session_ignores_legacy_display_personality(personality_value):
    session = _new_session_with_config(
        {
            "display": {"personality": personality_value},
            "agent": {"personality": ""},
        }
    )

    assert session.personality is None


def test_new_session_does_not_read_global_config_for_personality():
    import api.config as cfg_mod
    import api.models as models

    with patch.object(cfg_mod, "get_config", side_effect=AssertionError("unexpected config read")), \
         patch.object(models.Session, "save", return_value=None):
        session = models.new_session(workspace="/tmp/test-personality-no-config-read")

    assert session.personality is None
