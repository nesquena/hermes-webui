"""Independent actual SQLite prefix admission and compaction/truncation boundaries."""

import copy
import pytest
from api import models
from tests.test_cancel_restart_journal_recovery import _isolated_state  # noqa: F401 - autouse isolation
from tests.test_cancelled_journal_owner_occurrences import _recover
from tests.test_webui_state_db_reconciliation import _make_state_db


@pytest.mark.parametrize(
    "view",
    [
        "after-cached",
        "between-cached",
        "before-first",
        "empty-prefix",
        "compressed-anchored",
        "compressed-unanchored",
        "truncated",
    ],
)
def test_actual_sqlite_prefix_respects_selected_anchor_and_cutoff(
    tmp_path, monkeypatch, view
):
    sid = "r21-order-" + view
    db = tmp_path / "state.db"
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    prior = [
        {"role": "user", "content": "PREFIX_Q", "timestamp": 1},
        {"role": "assistant", "content": "PREFIX_A", "timestamp": 2},
    ]
    gap = [
        {"role": "user", "content": "GATEWAY_GAP_Q", "timestamp": 3},
        {"role": "assistant", "content": "GATEWAY_GAP_A", "timestamp": 4},
    ]
    later = [
        {"role": "user", "content": "LATER_CACHED_Q", "timestamp": 5},
        {"role": "assistant", "content": "LATER_CACHED_A", "timestamp": 6},
    ]
    local = (
        prior + later
        if view == "between-cached"
        else later
        if view == "before-first"
        else []
        if view == "empty-prefix"
        else prior
    )
    s, owner = _recover(sid, local)
    _make_state_db(
        db,
        sid,
        [
            *prior,
            *gap,
            *later,
            owner,
            {"role": "assistant", "content": "CANCELLED_RAW_REPLAY", "timestamp": 11},
        ],
    )
    if view.startswith("compressed-"):
        tail = [
            copy.deepcopy(r)
            for r in s.context_messages
            if r.get("content") not in {"PREFIX_Q", "PREFIX_A"}
        ]
        s.context_messages = [
            {
                "role": "assistant",
                "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
                "timestamp": 2,
            },
            *tail,
        ]
        if view == "compressed-anchored":
            s.compression_anchor_message_key = {
                "role": "assistant",
                "ts": 2,
                "text": "PREFIX_A",
                "attachments": 0,
            }
    if view == "truncated":
        s.truncation_watermark = 2
        s.truncation_boundary = 2
    before = copy.deepcopy(s.context_messages)
    rows = models.reconciled_state_db_messages_for_session(s, prefer_context=True)
    assert s.context_messages == before
    text = [r.get("content") for r in rows]
    assert "CANCELLED_RAW_REPLAY" not in text
    if view in {"after-cached", "between-cached", "compressed-anchored"}:
        assert text.count("GATEWAY_GAP_Q") == text.count("GATEWAY_GAP_A") == 1, text
        assert text.index("GATEWAY_GAP_Q") < text.index("GATEWAY_GAP_A"), text
        if view == "between-cached":
            assert (
                text.index("PREFIX_A")
                < text.index("GATEWAY_GAP_Q")
                < text.index("GATEWAY_GAP_A")
                < text.index("LATER_CACHED_Q")
            ), text
    else:
        assert "GATEWAY_GAP_Q" not in text and "GATEWAY_GAP_A" not in text, text
    if view.startswith("compressed-"):
        assert "PREFIX_Q" not in text and "PREFIX_A" not in text
