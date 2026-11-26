"""PR #8672 gate (2026-11-25): matched reconciliation replay regression.

The gate reproduced a durable replay that added a displayed turn on the
candidate head while the matched frozen-base control kept the transcript at
five rows.  The two cases below are that pair, producer-composed:

* the real ``_assign_stable_message_ids()`` / ``_stamp_missing_message_timestamps()``
  helpers settle the sidecar rows (which keep their durable row ids), a real
  SQLite database holds the replayed rows, the real ``get_state_db_session_messages``
  reader projects them with ``_state_db_row_id``, and the real
  ``reconciled_state_db_messages_for_session`` consumes both stores;
* a restamped replay names the SAME durable row id as the saved occurrence it
  mirrors (a legacy-shaped projection whose ``created_at`` was rewritten), so
  the saved occurrence must absorb its own replay instead of the merge
  appending a sixth row;
* a genuinely distinct durable turn carries a different row id and must stay
  separate -- that direction is the #7587 behaviour this PR exists to protect,
  and frozen master loses it (five rows instead of six).
"""

import sqlite3
from collections import OrderedDict

import pytest

pytestmark = pytest.mark.requires_agent_modules

REPEATED_PROMPT = "Q"
SAVED_CONTENTS = ["PROLOGUE", REPEATED_PROMPT, "ALPHA", REPEATED_PROMPT, "OMEGA"]


def _make_state_db(path, sid, rows):
    """Create a real state.db whose rows pin their durable primary key."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, "
        "tool_name TEXT, api_content TEXT)"
    )
    for row in rows:
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp, "
            "tool_call_id, tool_calls, tool_name, api_content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"],
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


def _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages):
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
        title="8672 matched reconciliation",
        workspace=str(tmp_path),
        model="test-model",
        messages=sidecar_messages,
        created_at=1000.0,
        updated_at=1001.0,
    )
    session.save(touch_updated_at=False)
    return session


def _settled_sidecar():
    """Saved transcript with stable ids minted by the real producer.

    The saved rows carry only their stable WebUI id, exactly what the display
    path persists; the durable row id lives on the SQLite side and is projected
    by the real reader because each repeated prompt carries a provider payload
    (``api_content``).  That one-sided identity is what the replay comparison
    has to treat as evidence of the same logical row rather than as a second
    clock.
    """
    from api.streaming import (
        _assign_stable_message_ids,
        _stamp_missing_message_timestamps,
    )

    rows = [
        {"role": "assistant", "content": "PROLOGUE", "timestamp": 500.0},
        {
            "role": "user",
            "content": REPEATED_PROMPT,
            "_ts": 1000,
            "api_content": "provider-Q-1",
        },
        {"role": "assistant", "content": "ALPHA", "_ts": 1001},
        {
            "role": "user",
            "content": REPEATED_PROMPT,
            "_ts": 2000,
            "api_content": "provider-Q-2",
        },
        {"role": "assistant", "content": "OMEGA", "_ts": 2001},
    ]
    _assign_stable_message_ids(rows)
    stamped = _stamp_missing_message_timestamps(rows, now=9_000_000.0)
    assert stamped == 0, "the stamper added a timestamp to a supported _ts row"
    return rows


def _reconcile(monkeypatch, tmp_path, sid, state_rows):
    """Compose the real reader and the real reconciler over one SQLite store."""
    import api.models as models

    _make_state_db(tmp_path / "state.db", sid, state_rows)
    session = _install_test_session(monkeypatch, tmp_path, sid, _settled_sidecar())
    # The default display read, exactly what the product calls: a row with a
    # provider payload projects its durable primary key.
    state_messages = models.get_state_db_session_messages(sid)
    assert state_messages and all(
        row.get("_state_db_row_id") is not None for row in state_messages
    ), "the real SQLite reader must project the durable row id"
    return models.reconciled_state_db_messages_for_session(
        session, state_messages=state_messages
    )


def test_restamped_durable_replay_collapses_onto_its_saved_occurrence(monkeypatch, tmp_path):
    """Gate RED: a restamped replay of row 7 must not add a sixth row.

    Frozen master collapses it (five rows).  The candidate head appended it
    after ``OMEGA`` because the clocks disagree, even though the incoming row
    names durable row 7 -- the exact occurrence the saved prompt owns.
    """
    display = _reconcile(
        monkeypatch,
        tmp_path,
        "webui_8672_matched_single",
        [
            {
                "id": 7,
                "role": "user",
                "content": REPEATED_PROMPT,
                "timestamp": 9000.0,
                "api_content": "provider-Q-1",
            },
        ],
    )
    contents = [row.get("content") for row in display]
    assert contents == SAVED_CONTENTS, (
        "a restamped durable replay was appended as a new displayed turn: "
        f"expected {SAVED_CONTENTS} got {contents}"
    )


def test_both_repeated_occurrences_absorb_their_own_restamped_replays(monkeypatch, tmp_path):
    """Both saved occurrences keep one replay each, never a shared one."""
    display = _reconcile(
        monkeypatch,
        tmp_path,
        "webui_8672_matched_pair",
        [
            {
                "id": 7,
                "role": "user",
                "content": REPEATED_PROMPT,
                "timestamp": 9000.0,
                "api_content": "provider-Q-1",
            },
            {
                "id": 9,
                "role": "user",
                "content": REPEATED_PROMPT,
                "timestamp": 9001.0,
                "api_content": "provider-Q-2",
            },
        ],
    )
    contents = [row.get("content") for row in display]
    assert contents == SAVED_CONTENTS, (
        "repeated restamped replays were appended instead of absorbed: "
        f"expected {SAVED_CONTENTS} got {contents}"
    )


def test_distinct_durable_turn_is_not_absorbed_by_a_similar_row(monkeypatch, tmp_path):
    """Control: durable row 50 is a genuinely different turn and must survive.

    The saved occurrence owns durable row 7, so the incoming row 50 conflicts
    inside the SAME durable namespace.  Frozen master collapses it to five rows
    (the #7587 loss this PR repairs); the candidate head keeps six.  This pins
    the direction the replay fix must not trade away: sharing content is not
    sharing an occurrence.
    """
    import api.models as models

    sid = "webui_8672_matched_distinct"
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {
                "id": 50,
                "role": "user",
                "content": REPEATED_PROMPT,
                "timestamp": 9000.0,
                "api_content": "provider-Q-1",
            },
        ],
    )
    sidecar = _settled_sidecar()
    sidecar[1]["_state_db_row_id"] = 7
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar)
    state_messages = models.get_state_db_session_messages(sid)

    display = models.reconciled_state_db_messages_for_session(
        session, state_messages=state_messages
    )
    contents = [row.get("content") for row in display]
    assert contents == SAVED_CONTENTS + [REPEATED_PROMPT], (
        "a genuinely distinct durable turn was suppressed as a duplicate: "
        f"expected six rows got {contents}"
    )


def test_identity_less_restamped_replay_keeps_the_strict_separation(monkeypatch, tmp_path):
    """Control: without a shared identity, a restamped clock still separates.

    The first replay names durable row 7 and must collapse onto its saved
    occurrence; the second carries NO private identity at all, so it cannot
    prove it is anybody's replay and must be appended.  This pins the boundary
    of the relaxation: only a SHARED identity grants a restamped row its
    predecessor's replay slot -- bare content plus a new clock never does.
    """
    import api.models as models

    sid = "webui_8672_matched_no_identity"
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {
                "id": 7,
                "role": "user",
                "content": REPEATED_PROMPT,
                "timestamp": 9000.0,
                "api_content": "provider-Q-1",
            },
        ],
    )
    session = _install_test_session(monkeypatch, tmp_path, sid, _settled_sidecar())
    state_messages = models.get_state_db_session_messages(sid)
    state_messages.append(
        {"role": "user", "content": REPEATED_PROMPT, "timestamp": 9001.0}
    )

    display = models.reconciled_state_db_messages_for_session(
        session, state_messages=state_messages
    )
    contents = [row.get("content") for row in display]
    assert contents == SAVED_CONTENTS + [REPEATED_PROMPT], (
        "an identity-less restamped replay was absorbed without proof: "
        f"expected six rows got {contents}"
    )
