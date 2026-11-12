"""#7882 re-gate (2026-10-20): the two ingress/refresh provenance shapes.

Both scenes are the maintainer's: a capped-out target is served by metadata-only
ingress (missing index / metadata stub) and by the index refresh, and in both the
eager-checkpoint fact has to survive at the CURRENT turn and only at it.

1. A metadata-only reconstruction must keep the eagerly saved fact.  The loader
   substitutes ``messages=[]`` on purpose, so an empty transcript is not proof
   the checkpointed row disappeared.
2. An older eager checkpoint must not vouch for a different deferred turn.  The
   index row's stale fact is replaced by the sidecar's current authority.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

import api.models as models

CAP = 2
PINNED_FILLERS = 4
TARGET = "CHECKPOINTED"


def _install_store(monkeypatch, tmp_path):
    import api.config as config
    import api.profiles as profiles
    import api.routes as routes
    from collections import OrderedDict

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    index_file = session_dir / "_index.json"
    for module in (config, models):
        monkeypatch.setattr(module, "STATE_DIR", tmp_path, raising=False)
        monkeypatch.setattr(module, "SESSION_DIR", session_dir, raising=False)
        monkeypatch.setattr(module, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": False})
    return session_dir


def _state_db(tmp_path):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, "
        "model TEXT, started_at REAL, message_count INTEGER)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, "
        "tool_name TEXT)"
    )
    conn.commit()
    conn.close()
    return path


def _append_state_db_rows(tmp_path, sid, rows):
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    try:
        for row in rows:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (sid, row["role"], row.get("content", ""), float(row["timestamp"])),
            )
        conn.execute(
            "INSERT OR REPLACE INTO sessions (id, source, title, model, started_at, message_count) "
            "VALUES (?, 'webui', 'Session', 'test-model', 1000.0, "
            "(SELECT COUNT(*) FROM messages WHERE session_id = ?))",
            (sid, sid),
        )
        conn.commit()
    finally:
        conn.close()


def _new_session(sid, *, messages, created_at, updated_at, pinned=False):
    return models.Session(
        session_id=sid,
        title=f"Session {sid}",
        workspace="/tmp",
        model="test-model",
        messages=messages,
        created_at=created_at,
        updated_at=updated_at,
        pinned=pinned,
    )


def _seed_fillers(monkeypatch, tmp_path, *, now):
    """Pinned fillers own the reconcile window so the target is capped out."""
    _install_store(monkeypatch, tmp_path)
    _state_db(tmp_path)
    for index in range(PINNED_FILLERS):
        sid = f"fmt{index}"
        rows = [
            {"role": "user", "content": f"filler {index} q", "timestamp": now - 900 - index},
            {"role": "assistant", "content": f"filler {index} a", "timestamp": now - 899 - index},
        ]
        _new_session(
            sid, messages=rows, created_at=now - 900 - index,
            updated_at=now - 899 - index, pinned=True,
        ).save(touch_updated_at=False)
        _append_state_db_rows(tmp_path, sid, rows)


def _eager_checkpoint(session, *, text, started_at):
    import api.routes as routes

    session.pending_user_message = text
    session.pending_user_source = "delegation_wakeup"
    session.pending_started_at = started_at
    session.active_stream_id = f"stream-{session.session_id}"
    routes._checkpoint_user_message_for_eager_session_save(
        session, text, None, started_at, source="delegation_wakeup",
    )


def _sidebar_row_for(sid, *, cap=CAP):
    """The row the capped-out sidebar serves, without paying the public handler.

    ``include_lineage_metadata=False`` mirrors the real ``/api/sessions``
    builder (``routes._all_sessions_for_sidebar``): the lineage path calls the
    overlay UNCAPPED, so it would never exercise the bounded fallback.
    """
    rows = models.all_sessions(
        sidebar_metadata_only=True, include_lineage_metadata=False
    )
    index_rows = [row for row in rows if row.get("session_id") == sid]
    assert index_rows, f"{sid} missing from the sidebar payload"
    return index_rows[0]


def _cap_the_target_out(monkeypatch):
    """Put the target OUTSIDE the reconcile walk, exactly as the maintainer does.

    ``TOP_N`` bounds the count tier and ``RECONCILE_TOP_N`` the transcript walk
    (default 60). Leaving the walk at its default would let the target take the
    authoritative walk and never exercise the capped-out fallback at all.
    """
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", str(CAP))
    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_RECONCILE_TOP_N", str(CAP))


def _prepare_continued_target(monkeypatch, tmp_path, *, now):
    """Eager checkpoint + a continued conversation in state.db (visible 3)."""
    _seed_fillers(monkeypatch, tmp_path, now=now)
    base = [
        {"role": "user", "content": "hi", "timestamp": now - 60},
        {"role": "assistant", "content": "hello", "timestamp": now - 59},
    ]
    session = _new_session(TARGET, messages=base, created_at=now - 60, updated_at=now - 59)
    session.save(touch_updated_at=False)
    _append_state_db_rows(tmp_path, TARGET, base)
    _eager_checkpoint(session, text="handoff", started_at=now)
    session.save()
    _append_state_db_rows(tmp_path, TARGET, [
        {"role": "user", "content": "handoff", "timestamp": now + 1},
        {"role": "assistant", "content": "done", "timestamp": now + 2},
    ])
    return session


def test_metadata_only_ingress_keeps_the_eager_fact(monkeypatch, tmp_path):
    """``messages=[]`` is not evidence the checkpointed row disappeared."""
    now = time.time()
    _prepare_continued_target(monkeypatch, tmp_path, now=now)
    _cap_the_target_out(monkeypatch)

    stub = models.Session.load_metadata_only(TARGET)
    assert stub is not None
    assert stub._pending_hidden_wakeup_at == pytest.approx(now, rel=0, abs=1e-6), (
        "a metadata-only load must not read its own substituted empty transcript "
        "as proof that the eagerly checkpointed row vanished"
    )
    assert stub.compact(include_runtime=True).get("_pending_hidden_wakeup_at") == (
        pytest.approx(now, rel=0, abs=1e-6)
    )

    # The full loader still validates against the real transcript.
    full = models.Session.load(TARGET)
    assert full._pending_hidden_wakeup_at == pytest.approx(now, rel=0, abs=1e-6)

    row = _sidebar_row_for(TARGET)
    assert row["visible_message_count"] == 3, (
        "the hidden row the sidecar already excluded must not be discounted a "
        f"second time (got {row['visible_message_count']})"
    )


def test_metadata_only_still_drops_the_fact_once_the_row_is_gone(monkeypatch, tmp_path):
    """Keeping the fact on a stub must not resurrect a settled row's excuse."""
    now = time.time()
    _seed_fillers(monkeypatch, tmp_path, now=now)
    session = _new_session(
        TARGET,
        messages=[{"role": "user", "content": "hi", "timestamp": now - 60}],
        created_at=now - 60,
        updated_at=now - 59,
    )
    _eager_checkpoint(session, text="handoff", started_at=now)
    session.messages.pop()  # the checkpointed row disappears before the save
    session.save()

    stub = models.Session.load_metadata_only(TARGET)
    assert stub._pending_hidden_wakeup_at is None, (
        "the writer already dropped the fact, so the stub must not invent one"
    )


def test_stale_index_fact_does_not_vouch_for_a_deferred_turn(monkeypatch, tmp_path):
    """An older eager checkpoint must not excuse a newer deferred turn.

    The unit under test is the refresh: given an index row carrying a fact from
    a PREVIOUS eager checkpoint and a sidecar whose pending turn is a newer
    deferred one, the refreshed row must not keep the stale fact.
    """
    now = time.time()
    _seed_fillers(monkeypatch, tmp_path, now=now)

    # The sidecar: a deferred hidden turn (no materialised row, so no fact).
    deferred = _new_session(
        TARGET,
        messages=[
            {"role": "user", "content": "hi", "timestamp": now - 60},
            {"role": "assistant", "content": "hello", "timestamp": now - 59},
        ],
        created_at=now - 60,
        updated_at=now - 59,
    )
    deferred.pending_user_message = "handoff"
    deferred.pending_user_source = "delegation_wakeup"
    deferred.pending_started_at = now + 120
    deferred.active_stream_id = f"stream-{TARGET}"
    deferred.save()
    assert deferred._pending_hidden_wakeup_at is None

    stale_started_at = now - 3600
    stale_index_row = {
        "session_id": TARGET,
        "message_count": 2,
        "pending_user_source": "delegation_wakeup",
        "pending_started_at": stale_started_at,
        # The fact from the PREVIOUS eager checkpoint, still in the index.
        "_pending_hidden_wakeup_at": stale_started_at,
        "visible_message_count": 2,
    }
    assert models._pending_hidden_row_materialized(stale_index_row) is True, (
        "precondition: the stale pair looks materialised on its own"
    )

    refreshed = models._refresh_index_rows_from_sidecar_metadata(
        [dict(stale_index_row)],
        index_message_counts={TARGET: 2},
    )[0]

    assert refreshed.get("_pending_hidden_wakeup_at") != pytest.approx(
        stale_started_at
    ), "the refresh must not carry the previous turn's fact forward"
    assert models._pending_hidden_row_materialized(refreshed) is False, (
        "the stale fact from the previous turn must not vouch for the new "
        "deferred turn"
    )


def _read_index_rows():
    import json

    if not models.SESSION_INDEX_FILE.exists():
        return []
    return json.loads(models.SESSION_INDEX_FILE.read_bytes())


def _write_index_rows(rows):
    import json

    models.SESSION_INDEX_FILE.write_bytes(json.dumps(rows).encode())


def test_older_hidden_checkpoint_does_not_discount_a_current_ordinary_turn(
    monkeypatch, tmp_path
):
    """A stale ``delegation_wakeup`` source must not survive the refresh.

    The gate's scene: an older eager hidden checkpoint, its index row retained,
    the turn settled, then a current ORDINARY pending turn saved and its user
    row plus a visible reply appended through state.db. ``compact()`` omits the
    default ``webui`` source, so copying pending fields only when non-None left
    the older ``delegation_wakeup`` source on the refreshed row; the capped
    fallback then discounts an ordinary user row as a hidden wakeup (the walk
    no longer pays that discount, the fallback does).
    """
    now = time.time()
    _seed_fillers(monkeypatch, tmp_path, now=now)

    base = [
        {"role": "user", "content": "hi", "timestamp": now - 60},
        {"role": "assistant", "content": "hello", "timestamp": now - 59},
    ]
    session = _new_session(TARGET, messages=base, created_at=now - 60, updated_at=now - 59)
    session.save(touch_updated_at=False)
    _append_state_db_rows(tmp_path, TARGET, base)

    _eager_checkpoint(session, text="handoff", started_at=now - 30)
    session.save()
    stale_index_row = next(
        row for row in _read_index_rows() if row.get("session_id") == TARGET
    )
    assert stale_index_row.get("pending_user_source") == "delegation_wakeup"
    assert stale_index_row.get("_pending_hidden_wakeup_at") == pytest.approx(
        now - 30, rel=0, abs=1e-6
    )

    # The hidden turn settles: the sidecar no longer names a pending turn.
    session.pending_user_message = None
    session.pending_user_source = ""
    session.pending_started_at = None
    session.active_stream_id = None
    session.save()
    _append_state_db_rows(
        tmp_path, TARGET,
        [{"role": "assistant", "content": "done", "timestamp": now - 29}],
    )

    # A current ORDINARY pending turn on the same session.
    session.pending_user_message = "next"
    session.pending_user_source = "webui"
    session.pending_started_at = now + 100
    session.active_stream_id = "stream-ordinary"
    session.save()
    _append_state_db_rows(
        tmp_path, TARGET,
        [
            {"role": "user", "content": "next", "timestamp": now + 101},
            {"role": "assistant", "content": "sure", "timestamp": now + 102},
        ],
    )

    # The sidebar still serves the stale index row for this session.
    _write_index_rows([
        stale_index_row if row.get("session_id") == TARGET else row
        for row in _read_index_rows()
    ])

    _cap_the_target_out(monkeypatch)
    capped = _sidebar_row_for(TARGET)

    monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "0")
    uncapped = _sidebar_row_for(TARGET)
    assert capped["visible_message_count"] == uncapped["visible_message_count"], (
        "the capped poll must match the reconciled count "
        f"(capped {capped['visible_message_count']}, uncapped {uncapped['visible_message_count']})"
    )
    assert str(capped.get("pending_user_source") or "").strip().lower() != "delegation_wakeup", (
        "an ordinary current turn must not inherit the settled checkpoint's source"
    )
