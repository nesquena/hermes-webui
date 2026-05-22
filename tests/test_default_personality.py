"""Test that new_session() reads display.personality from config and uses it as default.

Regression test for the feature that makes /personality taleb sticky across
new sessions — when display.personality is set in config.yaml, every new
session should inherit it without requiring an explicit /personality command.
"""

import pytest
from unittest.mock import patch


# ---------------------------------------------------------------------------
# R1: new_session() inherits display.personality from config
# ---------------------------------------------------------------------------

def test_new_session_reads_default_personality_from_config():
    """When display.personality is set to 'taleb', new_session() should
    create a Session with personality='taleb'."""
    import api.models as m
    import api.config as cfg_mod

    _cfg = {
        "display": {"personality": "taleb"},
        "agent": {"personalities": {"taleb": {"system_prompt": "Be like Taleb", "tone": "blunt"}}},
    }

    with patch.object(cfg_mod, "get_config", return_value=_cfg), \
         patch.object(m.Session, "save", return_value=None):
        s = m.new_session(workspace="/tmp/test-personality")

    assert s.personality == "taleb", (
        f"Expected personality='taleb', got {s.personality!r}"
    )


# ---------------------------------------------------------------------------
# R2: 'none', 'default', 'neutral' are treated as no personality
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("personality_value", ["none", "default", "neutral", ""])
def test_new_session_ignores_neutral_personality_values(personality_value):
    """Values like 'none', 'default', 'neutral', and '' should NOT be set as
    the session personality — they mean 'no personality overlay'."""

    import api.models as m
    import api.config as cfg_mod

    _cfg = {
        "display": {"personality": personality_value},
        "agent": {"personalities": {}},
    }

    with patch.object(cfg_mod, "get_config", return_value=_cfg), \
         patch.object(m.Session, "save", return_value=None):
        s = m.new_session(workspace="/tmp/test-personality-neutral")

    assert s.personality is None, (
        f"Expected None for display.personality={personality_value!r}, "
        f"got {s.personality!r}"
    )


# ---------------------------------------------------------------------------
# R3: Missing display.personality → personality=None
# ---------------------------------------------------------------------------

def test_new_session_no_personality_when_config_missing():
    """When config has no display.personality (or display section is absent),
    new_session() should set personality=None."""

    import api.models as m
    import api.config as cfg_mod

    _cfg = {"agent": {"personalities": {}}}  # No display section at all

    with patch.object(cfg_mod, "get_config", return_value=_cfg), \
         patch.object(m.Session, "save", return_value=None):
        s = m.new_session(workspace="/tmp/test-personality-missing")

    assert s.personality is None


# ---------------------------------------------------------------------------
# R4: Config exception is handled gracefully → personality=None
# ---------------------------------------------------------------------------

def test_new_session_handles_config_exception_gracefully():
    """If get_config() raises, we should still get a valid session with
    personality=None (the try/except should swallow the error)."""

    import api.models as m
    import api.config as cfg_mod

    def _boom():
        raise RuntimeError("config exploded")

    with patch.object(cfg_mod, "get_config", side_effect=_boom), \
         patch.object(m.Session, "save", return_value=None):
        s = m.new_session(workspace="/tmp/test-personality-boom")

    assert s.personality is None


# ---------------------------------------------------------------------------
# R5: display.personality is case-insensitive
# ---------------------------------------------------------------------------

def test_new_session_personality_is_case_insensitive():
    """display.personality='Taleb' should be normalized to 'taleb'."""

    import api.models as m
    import api.config as cfg_mod

    _cfg = {
        "display": {"personality": "Taleb"},
        "agent": {"personalities": {"taleb": {"system_prompt": "Be like Taleb"}}},
    }

    with patch.object(cfg_mod, "get_config", return_value=_cfg), \
         patch.object(m.Session, "save", return_value=None):
        s = m.new_session(workspace="/tmp/test-personality-case")

    assert s.personality == "taleb"