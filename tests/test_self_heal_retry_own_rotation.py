"""The retry's own-rotation test must not read a rebuilt agent's constructor id.

A first attempt that compressed leaves the session at `continuation-one` while
the turn started at `original`. The 401 self-heal rebuilds the agent on the
current canonical id, so the rebuilt agent carries `continuation-one` in its
constructor — the same as the session's owner. It has NOT rotated on its own, so
the post-retry migration must not replay `continuation-one -> original` (which
raises and discards the retry's real answer).
"""
import inspect

import api.streaming as streaming


def _source() -> str:
    return inspect.getsource(streaming._run_agent_streaming)


def test_rebuilt_agent_is_constructed_on_the_current_identity():
    src = _source()
    marker = src.split("_agent_kwargs_session_id = _agent_kwargs.get('session_id')", 1)
    assert len(marker) == 2, "the retry agent's constructed id is not captured"
    block = marker[1].split("_replaced_agent = agent", 1)[0]
    assert "_agent_kwargs['session_id']" in block
    assert "getattr(s, 'session_id', None) or session_id" in block


def test_own_rotation_exempts_the_constructed_id():
    src = _source()
    decision = src.split("_retry_rotated_on_its_own = bool(", 1)
    assert len(decision) == 2, "the retry-rotation decision is gone"
    block = decision[1].split("if _retry_rotated_on_its_own:", 1)[0]
    assert "_agent_kwargs_session_id" in block, (
        "a rebuilt agent's constructor id is read as a rotation of its own"
    )
    assert "str(_retry_agent_sid)" in block
    assert "str(_agent_kwargs_session_id)" in block
