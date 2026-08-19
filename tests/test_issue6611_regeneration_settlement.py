import copy
import pytest

from api.models import Session
from api.session_ops import RegenerationUnavailable, apply_regeneration_plan, plan_regeneration, regeneration_revision


def _session():
    return Session(
        session_id="settlement6611",
        messages=[
            {"role": "user", "content": "old", "id": "u0", "_source": "webui"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "latest", "id": "u1", "_source": "webui"},
            {"role": "assistant", "content": "error"},
        ],
        context_messages=[
            {"role": "user", "content": "old", "id": "u0"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "latest", "id": "u1"},
            {"role": "assistant", "content": "error"},
        ],
    )


def test_recovered_prefix_survives_regeneration_truncation():
    session = _session()
    plan = plan_regeneration(session)
    assert apply_regeneration_plan(session, plan)
    assert [row["content"] for row in session.messages] == ["old", "old answer", "latest"]


def test_stale_revision_rejects_without_mutation():
    session = _session()
    before = copy.deepcopy(session.__dict__)
    with pytest.raises(RegenerationUnavailable):
        plan_regeneration(session, expected_revision="stale-revision")
    assert session.__dict__ == before


def test_double_fire_revision_changes_after_winner():
    session = _session()
    plan = plan_regeneration(session)
    assert apply_regeneration_plan(session, plan)
    assert regeneration_revision(session) != plan.revision


def test_trailing_tool_state_is_not_regenerable_as_an_older_exchange():
    session = Session(
        session_id="settlement6611-tool-tail",
        messages=[
            {"role": "user", "content": "prompt", "_source": "webui"},
            {"role": "assistant", "content": "visible answer"},
            {"role": "tool", "content": "unfinished tool result"},
        ],
        context_messages=[
            {"role": "user", "content": "prompt"},
            {"role": "assistant", "content": "visible answer"},
            {"role": "tool", "content": "unfinished tool result"},
        ],
    )
    try:
        plan_regeneration(session)
    except RegenerationUnavailable as exc:
        assert exc.code == "no_regenerable_turn"
    else:
        raise AssertionError("trailing tool state was treated as a completed exchange")
