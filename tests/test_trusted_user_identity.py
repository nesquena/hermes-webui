"""The WebUI passes its trusted-auth user into the agent per turn.

The identity is what lets a group agent tell which member is speaking. The
helper is intentionally pure so these tests do not need a running agent.
"""
from api.streaming import _apply_turn_user_identity


class _Agent:
    pass


def test_fresh_agent_kwargs_get_identity():
    kwargs = {}
    _apply_turn_user_identity(kwargs, {"user_id", "user_name"}, None, "alice")
    assert kwargs == {"user_id": "alice", "user_name": "alice"}


def test_older_agent_without_identity_params_is_unchanged():
    kwargs = {}
    _apply_turn_user_identity(kwargs, {"model"}, None, "alice")
    assert kwargs == {}


def test_cached_agent_is_restamped_per_turn():
    agent = _Agent()
    agent._user_id = "alice"
    agent._user_name = "alice"
    _apply_turn_user_identity(None, {"user_id"}, agent, "bob")
    assert agent._user_id == "bob"
    assert agent._user_name == "bob"


def test_cached_agent_without_identity_attr_is_unchanged():
    agent = _Agent()
    _apply_turn_user_identity(None, {"user_id"}, agent, "alice")
    assert not hasattr(agent, "_user_id")


def test_anonymous_turn_leaves_everything_alone():
    kwargs = {}
    agent = _Agent()
    _apply_turn_user_identity(kwargs, {"user_id"}, agent, None)
    assert kwargs == {}
    assert not hasattr(agent, "_user_id")
