"""PR #8672 gate round 4: the three regressions of the 2026-11-09 RED certificate.

Maintainer head ``4fbe08831612d758ae55959d12254041595e7b55`` was certified RED
with three independently reproduced regressions.  Each case below is
PRODUCER-COMPOSED, exactly like the reviewer probes:

* C67-F1 -- the real ``_assign_stable_message_ids()`` producer mints the stable
  ids, the real ``_stamp_missing_message_timestamps()`` stamper preserves the
  legacy ``_ts`` clock rows, a real SQLite database holds the replayed suffix,
  and the real reconciler consumes both stores.  The four-row replay must
  collapse onto the five saved occurrences one-to-one instead of appending the
  second repeated prompt after the tail.
* C67-F2 -- the exact-index duplicate path must not let ONE group member's
  durable id veto a COMPATIBLE sibling: the incoming durable row 8 conflicts
  with the earlier saved row 7 but not with the later unbound occurrence, which
  has to consume its own replay.
* C67-F3 -- the bounded display-prefix proof must compare both ordered prefix
  lists through the shared projection without private occurrence identity, so
  settled rows (stable id on the sidecar, row id in SQLite) still take the
  bounded read instead of falling back to the full read.

No hand-built composite key is used as an oracle anywhere in this file.
"""

import copy
import sqlite3
from collections import OrderedDict

import pytest

pytestmark = pytest.mark.requires_agent_modules

REPEATED_PROMPT = "Q"
PROVIDER_BYTES = "provider-bytes"
# The C67-F2 discriminator is a VALID scalar payload over the exact-only
# cutoff, so the merge reaches the exact-index probe instead of the fuzzy loop.
OVERSIZED_PROMPT = "x" * 200_001


def _make_state_db(path, sid, rows):
    """Create a real state.db whose rows may pin their durable primary key."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, "
        "tool_name TEXT, api_content TEXT)"
    )
    for row in rows:
        explicit_id = row.get("id")
        if explicit_id is None:
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
        else:
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp, tool_call_id, "
                "tool_calls, tool_name, api_content) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    explicit_id,
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


def _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages, context_messages=None):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes

    monkeypatch.setattr(config, "STATE_DIR", tmp_path, raising=False)
    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    session_dir.mkdir(parents=True, exist_ok=True)

    session = models.Session(
        session_id=sid,
        title="8672 c67 gate",
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


def _settled_sidecar(rows):
    """Run the REAL producers over saved transcript rows.

    ``_assign_stable_message_ids()`` is the WebUI stable-id producer;
    ``_stamp_missing_message_timestamps()`` is the real missing-timestamp
    stamper, which preserves the supported legacy ``_ts`` clock rows without
    adding a ``timestamp`` column of its own.
    """
    from api.streaming import (
        _assign_stable_message_ids,
        _stamp_missing_message_timestamps,
    )

    rows = [dict(row) for row in rows]
    _assign_stable_message_ids(rows)
    stamped = _stamp_missing_message_timestamps(rows, now=9_000_000.0)
    return rows, stamped


# ══ C67-F1: replay capacity is consumed one occurrence at a time ═══════════


def test_repeated_turn_replay_consumes_each_saved_occurrence_once(monkeypatch, tmp_path):
    """C67-F1: the replayed repeated prompt must not be appended after the tail.

    The sidecar holds a leading assistant ``PROLOGUE`` and then
    ``Q / ALPHA / Q / OMEGA`` on legacy ``_ts`` clocks 1000/1001/2000/2001, with
    stable ids 1-5 minted by the real producer.  state.db replays the four-row
    suffix with the corresponding ordinary ``timestamp`` values.  Every replay
    has to collapse onto its own saved occurrence, so the transcript stays five
    rows; the pre-fix head appended the replayed ``Q`` after ``OMEGA``.
    """
    import api.models as models

    sidecar, stamped = _settled_sidecar(
        [
            {"role": "assistant", "content": "PROLOGUE", "timestamp": 500.0},
            {"role": "user", "content": REPEATED_PROMPT, "_ts": 1000},
            {"role": "assistant", "content": "ALPHA", "_ts": 1001},
            {"role": "user", "content": REPEATED_PROMPT, "_ts": 2000},
            {"role": "assistant", "content": "OMEGA", "_ts": 2001},
        ]
    )
    # The real stamper must have recognised the legacy `_ts` clocks as
    # supported, so no synthetic `timestamp` was folded into the saved rows.
    assert stamped == 0, "the stamper added a timestamp to a supported _ts row"
    assert [row.get("id") for row in sidecar] == [1, 2, 3, 4, 5]
    assert all("timestamp" not in row for row in sidecar[1:])

    sid = "webui_8672_c67_f1"
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": REPEATED_PROMPT, "timestamp": 1000.0},
            {"role": "assistant", "content": "ALPHA", "timestamp": 1001.0},
            {"role": "user", "content": REPEATED_PROMPT, "timestamp": 2000.0},
            {"role": "assistant", "content": "OMEGA", "timestamp": 2001.0},
        ],
    )
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar)

    display = models.reconciled_state_db_messages_for_session(session)
    contents = [row.get("content") for row in display]
    assert contents == [
        "PROLOGUE",
        REPEATED_PROMPT,
        "ALPHA",
        REPEATED_PROMPT,
        "OMEGA",
    ], f"the repeated-turn replay was appended: {contents}"


def test_clockless_replay_consumes_the_second_saved_occurrence(monkeypatch, tmp_path):
    """C67-F1 (capacity half): an exhausted representative must not block a
    compatible sibling even when neither side carries a usable clock.

    The saved rows are replayed from state.db WITHOUT timestamps, so the clock
    cannot pair them one-to-one: the second replay has to fall through to the
    saved occurrence whose capacity is still available instead of being
    appended as a new row.
    """
    import api.models as models

    sidecar, _stamped = _settled_sidecar(
        [
            {"role": "assistant", "content": "PROLOGUE", "timestamp": 500.0},
            {"role": "user", "content": REPEATED_PROMPT, "_ts": 1000},
            {"role": "assistant", "content": "ALPHA", "_ts": 1001},
            {"role": "user", "content": REPEATED_PROMPT, "_ts": 2000},
            {"role": "assistant", "content": "OMEGA", "_ts": 2001},
        ]
    )
    sid = "webui_8672_c67_f1_capacity"
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": REPEATED_PROMPT, "timestamp": None},
            {"role": "assistant", "content": "ALPHA", "timestamp": None},
            {"role": "user", "content": REPEATED_PROMPT, "timestamp": None},
            {"role": "assistant", "content": "OMEGA", "timestamp": None},
        ],
    )
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar)

    display = models.reconciled_state_db_messages_for_session(session)
    contents = [row.get("content") for row in display]
    assert contents == [
        "PROLOGUE",
        REPEATED_PROMPT,
        "ALPHA",
        REPEATED_PROMPT,
        "OMEGA",
    ], f"an exhausted representative blocked a compatible occurrence: {contents}"


def test_third_occurrence_of_a_repeated_prompt_is_still_appended(monkeypatch, tmp_path):
    """C67-F1 control: #7587's distinct-occurrence protection is retained.

    Two saved occurrences can absorb at most two replays; a genuinely third
    occurrence of the same prompt must still land as its own row.
    """
    import api.models as models

    sidecar, _stamped = _settled_sidecar(
        [
            {"role": "assistant", "content": "PROLOGUE", "timestamp": 500.0},
            {"role": "user", "content": REPEATED_PROMPT, "_ts": 1000},
            {"role": "assistant", "content": "ALPHA", "_ts": 1001},
            {"role": "user", "content": REPEATED_PROMPT, "_ts": 2000},
            {"role": "assistant", "content": "OMEGA", "_ts": 2001},
        ]
    )
    sid = "webui_8672_c67_f1_third"
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": REPEATED_PROMPT, "timestamp": 1000.0},
            {"role": "assistant", "content": "ALPHA", "timestamp": 1001.0},
            {"role": "user", "content": REPEATED_PROMPT, "timestamp": 2000.0},
            {"role": "assistant", "content": "OMEGA", "timestamp": 2001.0},
            {"role": "user", "content": REPEATED_PROMPT, "timestamp": 3000.0},
        ],
    )
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar)

    display = models.reconciled_state_db_messages_for_session(session)
    occurrences = [row for row in display if row.get("content") == REPEATED_PROMPT]
    assert len(occurrences) == 3, (
        f"the third occurrence of the repeated prompt was dropped: "
        f"{[row.get('content') for row in display]}"
    )


# ══ C67-F2: a durable conflict applies to the candidate, not the group ═════


def test_compatible_sibling_occurrence_consumes_its_own_replay(monkeypatch, tmp_path):
    """C67-F2: durable row 8 must collapse onto the unbound later occurrence.

    The earlier saved user occurrence owns durable row 7 at timestamp 1000; the
    later saved occurrence is unbound (legacy ``_ts`` clock, no durable
    provenance).  An incoming replay of the later occurrence carries durable row
    8, which conflicts with row 7 but NOT with the unbound sibling, so the
    five-row transcript must survive instead of gaining a sixth row.
    """
    import api.models as models

    sidecar, _stamped = _settled_sidecar(
        [
            {"role": "assistant", "content": "PROLOGUE", "timestamp": 500.0},
            {
                "role": "user",
                "content": OVERSIZED_PROMPT,
                "timestamp": 1000.0,
                "api_content": PROVIDER_BYTES,
                "_state_db_row_id": 7,
            },
            {"role": "assistant", "content": "ALPHA", "timestamp": 1001.0},
            {
                "role": "user",
                "content": OVERSIZED_PROMPT,
                "_ts": 2000,
                "api_content": PROVIDER_BYTES,
            },
            {"role": "assistant", "content": "OMEGA", "_ts": 2001},
        ]
    )
    # The producer really minted stable local ids that stay a SEPARATE
    # namespace from the durable provenance the earlier row carries.
    assert [row.get("id") for row in sidecar] == [1, 2, 3, 4, 5]
    assert sidecar[1].get("_state_db_row_id") == 7
    assert "_state_db_row_id" not in sidecar[3]

    sid = "webui_8672_c67_f2"
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {
                "id": 8,
                "role": "user",
                "content": OVERSIZED_PROMPT,
                "timestamp": 2000.0,
                "api_content": PROVIDER_BYTES,
            }
        ],
    )
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar)

    projected = models.get_state_db_session_messages(sid)
    assert projected and projected[0].get("_state_db_row_id") == 8, (
        "the real SQLite reader must project durable row id 8, got "
        f"{[row.get('_state_db_row_id') for row in projected]}"
    )

    display = models.reconciled_state_db_messages_for_session(session)
    contents = [row.get("content") for row in display]
    assert len(display) == 5, (
        f"the compatible sibling refused its own replay: {len(display)} rows"
    )
    assert contents.count(OVERSIZED_PROMPT) == 2, (
        f"the replayed occurrence was appended instead of merged: {len(contents)} rows"
    )


def test_conflicting_durable_pair_without_a_compatible_sibling_stays_distinct():
    """C67-F2 control: a genuinely conflicting pair is still refused.

    The same shape with NO compatible sibling in the group must keep both
    rows: durable row 8 is a distinct turn from the saved durable row 7.
    """
    from api.models import (
        _matching_visible_duplicate,
        _session_message_visible_key,
    )

    saved = {
        "role": "user",
        "content": OVERSIZED_PROMPT,
        "timestamp": 1000.0,
        "api_content": PROVIDER_BYTES,
        "_state_db_row_id": 7,
    }
    conflicting = {
        "role": "user",
        "content": OVERSIZED_PROMPT,
        "timestamp": 2000.0,
        "api_content": PROVIDER_BYTES,
        "_state_db_row_id": 8,
    }
    assert (
        _matching_visible_duplicate(
            _session_message_visible_key(conflicting),
            {_session_message_visible_key(saved)},
        )
        is None
    ), "a conflicting durable row collapsed onto an unrelated durable row"


# ══ C67-F3: the bounded display-prefix proof keeps working ═════════════════


def test_bounded_display_prefix_proof_survives_settled_stable_ids(monkeypatch, tmp_path):
    """C67-F3: settled rows must not disable the bounded display read.

    A real SQLite database holds 350 distinct timestamped rows; the sidecar
    carries the same rows with stable ids minted by the real producer.  The
    prefix reader proves the 50 rows below the floor, so a display limit of 10
    must still yield the bounded floor (1050) instead of falling back to the
    full read.
    """
    import api.models as models
    import api.routes as routes

    sid = "webui_8672_c67_f3"
    rows = [
        {
            "role": "user" if idx % 2 == 0 else "assistant",
            "content": f"row {idx:04d}",
            "timestamp": 1000.0 + idx,
        }
        for idx in range(350)
    ]
    _make_state_db(tmp_path / "state.db", sid, [dict(row) for row in rows])
    sidecar, _stamped = _settled_sidecar(rows)
    assert [row.get("id") for row in sidecar] == list(range(1, 351))
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar)

    floor, returned_sidecar = routes._state_db_since_timestamp_for_limited_display(
        session,
        10,
    )
    assert floor == 1050.0, (
        "the bounded display read was refused for settled rows, so the display "
        f"path fell back to the full read (floor={floor!r})"
    )
    assert returned_sidecar == sidecar


def test_bounded_display_prefix_still_refuses_a_genuine_prefix_mismatch(
    monkeypatch, tmp_path
):
    """C67-F3 control: the other conservative prefix guards are retained.

    A state.db row whose visible identity differs from the sidecar's must
    still refuse the bounded read and select the full-read fallback.
    """
    import api.models as models
    import api.routes as routes

    sid = "webui_8672_c67_f3_mismatch"
    rows = [
        {
            "role": "user" if idx % 2 == 0 else "assistant",
            "content": f"row {idx:04d}",
            "timestamp": 1000.0 + idx,
        }
        for idx in range(350)
    ]
    state_rows = [dict(row) for row in rows]
    state_rows[10]["content"] = "edited state content"
    _make_state_db(tmp_path / "state.db", sid, state_rows)
    sidecar, _stamped = _settled_sidecar(rows)
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar)

    floor, returned_sidecar = routes._state_db_since_timestamp_for_limited_display(
        session,
        10,
    )
    assert floor is None, "a real prefix mismatch must still refuse the bounded read"
    assert returned_sidecar == sidecar


def test_bounded_display_prefix_still_refuses_a_null_clock(monkeypatch, tmp_path):
    """C67-F3 control: null clocks still refuse the bounded display read."""
    import api.models as models
    import api.routes as routes

    sid = "webui_8672_c67_f3_null"
    rows = [
        {
            "role": "user" if idx % 2 == 0 else "assistant",
            "content": f"row {idx:04d}",
            "timestamp": 1000.0 + idx,
        }
        for idx in range(350)
    ]
    state_rows = [dict(row) for row in rows]
    state_rows[10]["timestamp"] = None
    _make_state_db(tmp_path / "state.db", sid, state_rows)
    sidecar, _stamped = _settled_sidecar(rows)
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar)

    floor, _returned_sidecar = routes._state_db_since_timestamp_for_limited_display(
        session,
        10,
    )
    assert floor is None, "a null clock in the skipped prefix must refuse the bounded read"
