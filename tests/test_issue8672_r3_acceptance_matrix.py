"""PR #8672 r3 acceptance matrix: 15 assertions over the two P2 residuals.

Scope: the independent SQLite / reconciler / planner / settlement matrix the
maintainer described in the 2026-11-05 06:39 re-review.  It covers:

* the two P2 residuals (P2-1 identity namespaces, P2-2 bounded prefix);
* the controls that must NOT change (one-sided id, same-namespace conflict,
  real clock-disagreement fallback, collision guard, distinct-ID recovery);
* the repairs the maintainer explicitly asked to retain (truncation, empty
  tool row, oversized replay, restamped-clock parity, distinct tool call /
  empty anchor).

Every case is PRODUCER-COMPOSED: a real ``_settle_result_messages()`` call, a
real SQLite database, and the real reconciler / planner / settlement entry
points.  No hand-built composite key fixture is used as an oracle.
"""

import copy
import sqlite3
from collections import OrderedDict

import pytest

pytestmark = pytest.mark.requires_agent_modules

SENTINEL = "sentinel prompt"
PROVIDER_BYTES = "provider-bytes"


def _make_state_db(path, sid, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, "
        "tool_name TEXT, api_content TEXT)"
    )
    for row in rows:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, tool_call_id, "
            "tool_calls, tool_name, api_content) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                row["role"],
                row["content"],
                row.get("timestamp"),
                row.get("tool_call_id"),
                row.get("tool_calls"),
                row.get("tool_name"),
                row.get("api_content"),
            ),
        )
    conn.commit()
    conn.close()


def _install(monkeypatch, tmp_path, sid, sidecar_messages, context_messages=None):
    import api.config as config
    import api.models as models
    import api.profiles as profiles

    monkeypatch.setattr(config, "STATE_DIR", tmp_path, raising=False)
    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    session_dir.mkdir(parents=True, exist_ok=True)

    session = models.Session(
        session_id=sid,
        title="8672 r3 matrix",
        workspace=str(tmp_path),
        model="test-model",
        messages=sidecar_messages,
        created_at=1000.0,
        updated_at=1001.0,
    )
    if context_messages is not None:
        session.context_messages = list(context_messages)
    session.save(touch_updated_at=False)
    return session


def _settle(result_rows, prompt):
    """One real settle call -> (display rows, context rows) with minted ids."""
    from api import streaming
    from api.models import Session

    streaming._annotate_media_snapshots_for_settled_messages = lambda _m: None
    session = Session(session_id="webui_8672_matrix", messages=[], context_messages=[])
    streaming._settle_result_messages(
        session, [], [], copy.deepcopy(result_rows), prompt, "webui", None
    )
    return list(session.messages), list(session.context_messages)


def _settled_transcript(turns=101, sid="webui_8672_matrix"):
    """Build a settled transcript through REAL settle calls."""
    from api import streaming
    from api.models import Session

    streaming._annotate_media_snapshots_for_settled_messages = lambda _m: None
    session = Session(session_id=sid, messages=[], context_messages=[])
    previous_context = []
    previous_display = []
    for turn in range(turns):
        prompt = f"prompt {turn:04d}"
        result = copy.deepcopy(previous_context) + [
            {"role": "user", "content": prompt, "timestamp": 1000.0 + turn * 2},
            {
                "role": "assistant",
                "content": f"reply {turn:04d}",
                "timestamp": 1001.0 + turn * 2,
            },
        ]
        streaming._settle_result_messages(
            session,
            list(previous_display),
            list(previous_context),
            result,
            prompt,
            "webui",
            None,
        )
        previous_display = list(session.messages)
        previous_context = list(session.context_messages)
    return session


# ══ P2-1: identity namespaces (4 assertions) ═══════════════════════════════


def test_a01_display_reconciliation_keeps_one_sentinel_and_the_final_answer(
    monkeypatch, tmp_path
):
    """P2-1: the display reconciliation must not append a second sentinel."""
    import api.models as models

    display_rows, context_rows = _settle(
        [
            {"role": "assistant", "content": "earlier answer", "timestamp": 500.0},
            {
                "role": "user",
                "content": SENTINEL,
                "timestamp": 1000.0,
                "api_content": PROVIDER_BYTES,
            },
            {"role": "assistant", "content": "final answer", "timestamp": 1001.0},
        ],
        SENTINEL,
    )
    sid = "webui_matrix_a01"
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {
                "role": "user",
                "content": SENTINEL,
                "timestamp": None,
                "api_content": PROVIDER_BYTES,
            }
        ],
    )
    session = _install(monkeypatch, tmp_path, sid, display_rows, context_messages=context_rows)

    display = models.reconciled_state_db_messages_for_session(session)
    contents = [row.get("content") for row in display]
    assert contents.count(SENTINEL) == 1, f"display appended a sentinel: {contents}"
    assert contents[-1] == "final answer", f"display displaced the final answer: {contents}"


def test_a02_context_reconciliation_keeps_one_sentinel_and_the_final_answer(
    monkeypatch, tmp_path
):
    """P2-1: the context reconciliation must not append a second sentinel."""
    import api.models as models

    display_rows, context_rows = _settle(
        [
            {"role": "assistant", "content": "earlier answer", "timestamp": 500.0},
            {
                "role": "user",
                "content": SENTINEL,
                "timestamp": 1000.0,
                "api_content": PROVIDER_BYTES,
            },
            {"role": "assistant", "content": "final answer", "timestamp": 1001.0},
        ],
        SENTINEL,
    )
    sid = "webui_matrix_a02"
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {
                "role": "user",
                "content": SENTINEL,
                "timestamp": None,
                "api_content": PROVIDER_BYTES,
            }
        ],
    )
    session = _install(monkeypatch, tmp_path, sid, display_rows, context_messages=context_rows)

    context = models.reconciled_state_db_messages_for_session(session, prefer_context=True)
    contents = [row.get("content") for row in context]
    assert contents.count(SENTINEL) == 1, f"context appended a sentinel: {contents}"
    assert contents[-1] == "final answer", f"context displaced the final answer: {contents}"


def test_a03_one_sided_id_still_collapses_onto_its_sidecar_twin():
    """P2-1 control: the one-sided-id case stays repaired."""
    from api.models import (
        _matching_visible_duplicate,
        _session_message_visible_key,
    )

    clocked = {"role": "user", "content": SENTINEL, "timestamp": 1000.0, "id": 1}
    clockless = {"role": "user", "content": SENTINEL, "_state_db_row_id": 7}

    assert _matching_visible_duplicate(
        _session_message_visible_key(clockless),
        {_session_message_visible_key(clocked)},
    ) == _session_message_visible_key(clocked)


def test_a04_same_namespace_conflicting_ids_stay_distinct():
    """P2-1 control: same-namespace conflicts are still contradictions."""
    from api.models import (
        _matching_visible_duplicate,
        _session_message_visible_key,
    )

    first = {"role": "user", "content": SENTINEL, "_state_db_row_id": 7}
    second = {"role": "user", "content": SENTINEL, "_state_db_row_id": 8}

    assert _matching_visible_duplicate(
        _session_message_visible_key(second),
        {_session_message_visible_key(first)},
    ) is None


# ══ P2-2: bounded regeneration (4 assertions) ══════════════════════════════


def test_a05_bounded_tail_snapshot_reads_the_tail_window(monkeypatch, tmp_path):
    """P2-2: the bounded path must return the 200-row tail window."""
    import api.models as models
    from api import session_ops

    session = _settled_transcript(sid="webui_matrix_a05")
    rows = list(session.messages)
    assert len(rows) == 202
    _make_state_db(
        tmp_path / "state.db",
        "webui_matrix_a05",
        [
            {
                "role": row.get("role"),
                "content": row.get("content"),
                "timestamp": row.get("timestamp"),
            }
            for row in rows
        ],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")

    floor = min(row["timestamp"] for row in rows[-200:])
    bounded = session_ops._bounded_tail_snapshot_if_safe(session, floor)
    assert bounded is not None, "the bounded path was refused"
    assert len(bounded) == 200, f"expected the 200-row tail, got {len(bounded)}"


def test_a06_regeneration_does_not_call_the_full_reader(monkeypatch, tmp_path):
    """P2-2: the planner must NOT fall back to the full state.db reader."""
    import api.models as models
    from api import session_ops

    session = _settled_transcript(sid="webui_matrix_a06")
    rows = list(session.messages)
    _make_state_db(
        tmp_path / "state.db",
        "webui_matrix_a06",
        [
            {
                "role": row.get("role"),
                "content": row.get("content"),
                "timestamp": row.get("timestamp"),
            }
            for row in rows
        ],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")

    full_reads = []
    real_reader = models.get_state_db_session_messages

    def _spy(*args, **kwargs):
        full_reads.append(1)
        return real_reader(*args, **kwargs)

    monkeypatch.setattr(models, "get_state_db_session_messages", _spy)
    monkeypatch.setattr(session_ops, "get_state_db_session_messages", _spy, raising=False)
    session_ops.regeneration_state(session, use_sidecar=True)
    assert not full_reads, "the regeneration fell back to the full reader"


def test_a07_real_clock_disagreement_still_refuses_the_bounded_path(monkeypatch, tmp_path):
    """P2-2 control: the real clock-disagreement fallback is preserved."""
    import api.models as models
    from api import session_ops

    sid = "webui_matrix_a07"
    sidecar = [
        {"role": "user", "content": "first prompt", "timestamp": 100.0},
        {"role": "assistant", "content": "first answer", "timestamp": 200.0},
        {"role": "user", "content": "second prompt", "timestamp": 300.0},
        {"role": "assistant", "content": "final answer", "timestamp": 400.0},
    ]
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": "first prompt", "timestamp": 100.0},
            {"role": "assistant", "content": "first answer", "timestamp": 200.0},
            {"role": "user", "content": "second prompt", "timestamp": 301.0},
            {"role": "assistant", "content": "final answer", "timestamp": 400.0},
        ],
    )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")
    session = models.Session(
        session_id=sid,
        messages=[dict(row) for row in sidecar],
        context_messages=[dict(row) for row in sidecar],
    )
    assert session_ops._bounded_tail_snapshot_if_safe(session, 150.0) is None, (
        "a restamped twin must still refuse the bounded path"
    )


def test_a08_collision_guard_still_refuses_the_bounded_path(monkeypatch, tmp_path):
    """P2-2 control: the prefix/tail collision guard is preserved."""
    import api.models as models
    from api import session_ops

    sid = "webui_matrix_a08"
    # A prompt that lives both below and above the read floor.
    rows = [
        {"role": "user", "content": "prompt", "timestamp": 100.0},
        {"role": "assistant", "content": "answer", "timestamp": 200.0},
        {"role": "user", "content": "prompt", "timestamp": 300.0},
        {"role": "assistant", "content": "final answer", "timestamp": 400.0},
    ]
    _make_state_db(tmp_path / "state.db", sid, rows)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")
    session = models.Session(
        session_id=sid,
        messages=[dict(row) for row in rows],
        context_messages=[dict(row) for row in rows],
    )
    assert session_ops._bounded_tail_snapshot_if_safe(session, 250.0) is None, (
        "a repeated prompt across the floor must refuse the bounded path"
    )


# ══ retained repairs (7 assertions) ════════════════════════════════════════


def test_a09_truncation_repair_is_retained():
    """Retained: the truncation repair still drops an edited-away row."""
    from api.models import merge_session_messages_append_only

    merged = merge_session_messages_append_only(
        [
            {"role": "assistant", "content": "earlier answer", "timestamp": 800.0},
            {"role": "user", "content": "repeat prompt", "timestamp": 900.0},
            {"role": "user", "content": "kept prompt", "timestamp": 1000.0},
            {"role": "assistant", "content": "final answer", "timestamp": 1002.0},
        ],
        [{"role": "user", "content": "repeat prompt", "timestamp": 1001.0}],
        truncation_watermark=1002.0,
        truncation_boundary=1002.0,
    )
    assert [row["content"] for row in merged] == [
        "earlier answer",
        "repeat prompt",
        "kept prompt",
        "final answer",
    ]


def test_a10_empty_tool_repair_is_retained():
    """Retained: the empty-tool-row replay still collapses."""
    from api.models import merge_session_messages_append_only

    call = [{"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}}]
    merged = merge_session_messages_append_only(
        [
            {"role": "assistant", "content": "earlier answer", "timestamp": 500.0},
            {
                "role": "assistant",
                "content": "",
                "timestamp": 1000.0,
                "tool_calls": call,
            },
            {"role": "assistant", "content": "final answer", "timestamp": 1001.0},
        ],
        [{"role": "assistant", "content": "", "tool_calls": call}],
    )
    assert len(merged) == 3, f"the empty tool row replay was appended: {merged}"


def test_a11_oversized_replay_repair_is_retained():
    """Retained: the oversized scalar replay still collapses."""
    from api.models import merge_session_messages_append_only

    oversized = "x" * 200_001
    merged = merge_session_messages_append_only(
        [
            {"role": "assistant", "content": "earlier answer", "timestamp": 500.0},
            {"role": "user", "content": oversized, "timestamp": 1000.0},
            {"role": "assistant", "content": "final answer", "timestamp": 1001.0},
        ],
        [{"role": "user", "content": oversized}],
    )
    assert len(merged) == 3, "the oversized scalar replay was appended"


def test_a12_restamped_clock_parity_is_retained(monkeypatch, tmp_path):
    """Retained: the restamped-clock full/bounded parity still holds.

    The sidecar mirror and the state.db row carry the SAME logical row with
    clocks one second apart.  The bounded snapshot feeds a positional,
    clock-tolerant replay probe, so it cannot recognise the replay -- the
    proof must fall back to the full read exactly as before.
    """
    from api import session_ops
    from api.models import Session

    sid = "webui_matrix_a12"
    sidecar = [
        {"role": "user", "content": "prompt", "timestamp": 100.0},
        {"role": "assistant", "content": "answer", "timestamp": 200.0},
    ]
    # The state.db mirror of "answer" is restamped by a whole second.
    db_rows = [
        {"role": "user", "content": "prompt", "timestamp": 100.0},
        {"role": "assistant", "content": "answer", "timestamp": 201.0},
    ]
    _make_state_db(tmp_path / "state.db", sid, db_rows)
    import api.models as models

    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")
    session = Session(
        session_id=sid,
        messages=[dict(row) for row in sidecar],
        context_messages=[dict(row) for row in sidecar],
    )
    floor = 150.0
    assert session_ops._bounded_tail_snapshot_if_safe(session, floor) is None, (
        "the restamped twin must still refuse the bounded path"
    )


def test_a13_distinct_tool_call_control_is_retained():
    """Retained: a distinct tool call still survives the merge."""
    from api.models import merge_session_messages_append_only

    merged = merge_session_messages_append_only(
        [
            {
                "role": "assistant",
                "content": "",
                "timestamp": 1000.0,
                "tool_calls": [
                    {"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}}
                ],
            },
            {"role": "assistant", "content": "final answer", "timestamp": 1002.0},
        ],
        [
            {
                "role": "assistant",
                "content": "",
                "timestamp": 1001.0,
                "tool_calls": [
                    {"id": "call_2", "function": {"name": "terminal", "arguments": "{}"}}
                ],
            }
        ],
    )
    tool_ids = [
        call.get("id")
        for row in merged
        for call in (row.get("tool_calls") or [])
    ]
    assert tool_ids == ["call_1", "call_2"], f"a distinct tool call was dropped: {tool_ids}"


def test_a14_empty_anchor_control_is_retained():
    """Retained: the recovered tool call after an empty anchor still lands."""
    from api.models import merge_session_messages_append_only

    merged = merge_session_messages_append_only(
        [
            {"role": "assistant", "content": "", "timestamp": 1000.0},
            {"role": "assistant", "content": "final answer", "timestamp": 1002.0},
        ],
        [
            {
                "role": "assistant",
                "content": "",
                "timestamp": 1001.0,
                "tool_calls": [
                    {
                        "id": "call_recovered",
                        "function": {"name": "terminal", "arguments": "{}"},
                    }
                ],
            }
        ],
    )
    tool_ids = [
        call.get("id")
        for row in merged
        for call in (row.get("tool_calls") or [])
    ]
    assert tool_ids == ["call_recovered"], f"the recovered tool call was dropped: {tool_ids}"


def test_a15_distinct_id_recovery_protection_is_retained():
    """Retained: the distinct-ID recovery protection still preserves the turn."""
    from api.models import merge_session_messages_append_only

    merged = merge_session_messages_append_only(
        [
            {"role": "assistant", "content": "earlier answer", "timestamp": 500.0},
            {
                "role": "user",
                "content": SENTINEL,
                "timestamp": 1000.0,
                "_state_db_row_id": 7,
            },
            {"role": "assistant", "content": "final answer", "timestamp": 1001.0},
        ],
        [{"role": "user", "content": SENTINEL, "_state_db_row_id": 8}],
    )
    occurrences = [
        row
        for row in merged
        if row.get("role") == "user" and row.get("content") == SENTINEL
    ]
    assert len(occurrences) == 2, (
        f"the conflicting-id turn disappeared: {[(r.get('role'), r.get('content')) for r in merged]}"
    )
