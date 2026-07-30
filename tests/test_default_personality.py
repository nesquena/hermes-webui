"""Regression coverage for display.personality not becoming durable session state.

Issue #2845: display.personality is a display/default hint, but new_session()
previously copied it into Session.personality. That made cosmetic config durable
per-session state and could override profile-scoped behavior later. Only an
explicit /api/personality/set call should persist Session.personality.
"""

from unittest.mock import patch


def test_new_session_does_not_inherit_display_personality_from_config():
    """display.personality='taleb' must not stamp Session.personality."""
    import api.models as m
    import api.config as cfg_mod

    cfg = {
        "display": {"personality": "taleb"},
        "agent": {"personalities": {"taleb": {"system_prompt": "Be like Taleb", "tone": "blunt"}}},
    }

    with patch.object(cfg_mod, "get_config", return_value=cfg), \
         patch.object(m.Session, "save", return_value=None):
        s = m.new_session(workspace="/tmp/test-personality")

    try:
        assert s.personality is None
    finally:
        with m.LOCK:
            m.SESSIONS.pop(s.session_id, None)


def test_new_session_still_defaults_to_no_personality_when_config_missing():
    """Missing display.personality continues to produce personality=None."""
    import api.models as m
    import api.config as cfg_mod

    cfg = {"agent": {"personalities": {}}}

    with patch.object(cfg_mod, "get_config", return_value=cfg), \
         patch.object(m.Session, "save", return_value=None):
        s = m.new_session(workspace="/tmp/test-personality-missing")

    try:
        assert s.personality is None
    finally:
        with m.LOCK:
            m.SESSIONS.pop(s.session_id, None)
