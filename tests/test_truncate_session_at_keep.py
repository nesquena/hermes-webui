"""truncate_session_at_keep aligns context when lengths differ (#5096 C)."""

from api.models import Session
from api.session_ops import truncate_session_at_keep


def test_truncate_session_at_keep_compaction_prefix():
    s = Session(
        session_id="t1",
        messages=[
            {"role": "user", "content": "u1", "timestamp": 1},
            {"role": "assistant", "content": "a1", "timestamp": 2},
            {"role": "user", "content": "gone", "timestamp": 3},
        ],
        context_messages=[
            {"role": "user", "content": "cref", "timestamp": 0.5},
            {"role": "user", "content": "u1", "timestamp": 1},
            {"role": "assistant", "content": "a1", "timestamp": 2},
            {"role": "user", "content": "gone", "timestamp": 3},
        ],
    )
    truncate_session_at_keep(s, 2)
    assert len(s.messages) == 2
    assert len(s.context_messages) == 3
    assert "gone" not in [m["content"] for m in s.context_messages]