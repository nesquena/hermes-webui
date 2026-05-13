"""
P0 regression test for the metadata-only save-wipe (#1558).

Before this fix, `_clear_stale_stream_state()` could be called on a session
loaded with `metadata_only=True` (which means messages=[]). That handler called
`session.save()` to persist the cleared stream flags — but `save()` writes
`self.messages` to disk verbatim, atomically overwriting the on-disk session
JSON with an empty messages array.

Affected callsites in api/routes.py:
  * line 1695 — `/api/session?session_id=…` GET handler (metadata mode)
  * line 1837 — `/api/session/status?session_id=…` GET handler

The route the user hits in steady state is `/api/session/status`, which the
SSE reconnect loop polls. So a routine "Reconnecting…" cycle after a server
restart could wipe a 1000-message conversation in a single round-trip.

This test reproduces the data loss path against the on-disk session file.
"""
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def temp_session_dir(tmp_path, monkeypatch):
    """Point the api.models SESSION_DIR at a temp dir so we don't touch real state."""
    sd = tmp_path / "sessions"
    sd.mkdir()
    # api.models reads SESSION_DIR at import time; patch the module-level binding.
    import api.models as _m
    from collections import OrderedDict
    monkeypatch.setattr(_m, "SESSION_DIR", sd)
    monkeypatch.setattr(_m, "SESSIONS", OrderedDict())
    yield sd


def _make_session_on_disk(session_dir, sid="s_test_1557", n_msgs=1000, with_active_stream=True):
    """Write a realistic session JSON with N messages and a stale active_stream_id."""
    from api.models import Session
    s = Session(
        session_id=sid,
        title="A long conversation",
        workspace="",
        model="MiniMax-M2.7",
        model_provider="ollama-cloud",
        created_at=1.0,
        updated_at=2.0,
        active_stream_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" if with_active_stream else None,
        pending_user_message="What is the meaning of life?" if with_active_stream else None,
        messages=[
            {"role": "user", "content": f"prompt {i}"} if i % 2 == 0
            else {"role": "assistant", "content": f"reply {i}"}
            for i in range(n_msgs)
        ],
    )
    # Session.path is a property derived from SESSION_DIR + session_id, which
    # the temp_session_dir fixture patches. No manual path assignment needed.
    s.save(skip_index=True)
    return sid


def test_metadata_only_save_raises_to_prevent_wipe(temp_session_dir):
    """Direct test of the #1558 guard: save() must refuse to wipe on-disk messages."""
    from api.models import get_session
    sid = _make_session_on_disk(temp_session_dir, n_msgs=1000)

    # Pre-state: on-disk file has 1000 messages.
    raw_before = json.loads((temp_session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert len(raw_before["messages"]) == 1000

    # Load metadata-only — synthesizes a stub with messages=[].
    s = get_session(sid, metadata_only=True)
    assert len(s.messages) == 0, "metadata-only load synthesizes empty messages — that's its job"
    assert getattr(s, "_loaded_metadata_only", False) is True, (
        "load_metadata_only() must set the _loaded_metadata_only flag so save() "
        "knows to refuse this save and prevent #1558 data-loss."
    )

    # Mutate as the buggy code path did, then attempt to save.
    s.active_stream_id = None
    s.pending_user_message = None
    with pytest.raises(RuntimeError, match="metadata-only"):
        s.save()

    # On-disk file MUST still have 1000 messages — the guard prevented the wipe.
    raw_after = json.loads((temp_session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert len(raw_after["messages"]) == 1000, (
        "save() raised but the file still got mutated — the guard must run BEFORE "
        "any disk write happens."
    )


def test_clear_stale_stream_state_preserves_messages(temp_session_dir):
    """High-level: the production trigger from #1558 must NOT wipe messages."""
    from api.models import get_session
    sid = _make_session_on_disk(temp_session_dir, n_msgs=1000, with_active_stream=True)

    # Simulate a server restart: STREAMS is empty, but the session has a stale
    # active_stream_id on disk. This is exactly the production trigger.
    from api.config import STREAMS, STREAMS_LOCK
    with STREAMS_LOCK:
        STREAMS.clear()

    # The SSE reconnect path calls /api/session/status, which loads metadata-only.
    s = get_session(sid, metadata_only=True)

    from api.routes import _clear_stale_stream_state
    # We don't care about the return value — the post-fix path may return False
    # because _repair_stale_pending clears the stream during the metadata=False
    # reload. What we care about is the messages array surviving.
    _clear_stale_stream_state(s)

    # The on-disk file MUST still have its 1000 messages (or more — the full-load
    # path in _repair_stale_pending may inject a stale-pending error marker pair
    # for transparency, growing the array slightly. Growth is acceptable; what
    # matters is that the existing conversation is not wiped).
    raw = json.loads((temp_session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert len(raw["messages"]) >= 1000, (
        f"_clear_stale_stream_state() shrank messages to {len(raw['messages'])} — "
        "see #1558. It must clear the stream flags WITHOUT losing existing messages."
    )
    # And the stream flag must actually be cleared (whether by _repair_stale_pending
    # during the reload or by the explicit clear afterwards).
    assert raw["active_stream_id"] is None, (
        "_clear_stale_stream_state() must clear the stale active_stream_id, "
        "either directly or via the full-load _repair_stale_pending path."
    )


def test_save_writes_bak_when_messages_shrink(temp_session_dir):
    """The backup safeguard: a save that shrinks messages must leave a .bak."""
    from api.models import Session
    sid = _make_session_on_disk(temp_session_dir, n_msgs=1000, with_active_stream=False)

    # Build a fresh in-memory Session with a smaller messages array, then save —
    # this models the precise failure shape of #1558 (a caller mutates messages
    # downward and saves). We construct the Session directly rather than going
    # through get_session() so we don't trigger _repair_stale_pending side-effects.
    s = Session(
        session_id=sid,
        title="t",
        workspace="",
        model="m",
        messages=[{"role": "user", "content": f"m{i}"} for i in range(500)],
    )
    s.save()

    bak_path = temp_session_dir / f"{sid}.json.bak"
    assert bak_path.exists(), (
        "save() that shrinks messages must leave a .bak — #1558 backup safeguard."
    )
    bak_data = json.loads(bak_path.read_text(encoding="utf-8"))
    assert len(bak_data["messages"]) == 1000, (
        "The .bak must contain the pre-shrink state (1000 messages), not the new state."
    )
    live_data = json.loads((temp_session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert len(live_data["messages"]) == 500


def test_save_does_not_write_bak_when_messages_grow(temp_session_dir):
    """No backup overhead on the normal grow-the-conversation path."""
    from api.models import Session
    sid = _make_session_on_disk(temp_session_dir, n_msgs=1000, with_active_stream=False)

    # Build a session with MORE messages than on disk — the normal grow path.
    s = Session(
        session_id=sid,
        title="t",
        workspace="",
        model="m",
        messages=[{"role": "user", "content": f"m{i}"} for i in range(1001)],
    )
    s.save()

    bak_path = temp_session_dir / f"{sid}.json.bak"
    assert not bak_path.exists(), (
        "save() that grows messages must NOT produce a .bak — would balloon disk usage."
    )


def test_recover_all_sessions_on_startup_restores_shrunken_session(temp_session_dir):
    """Startup self-heal: a session whose .bak has more messages must be restored."""
    sid = _make_session_on_disk(temp_session_dir, n_msgs=1000)

    # Manually plant a "shrunken live + intact bak" state, simulating what
    # the buggy v0.50.279 code path used to leave behind.
    live_path = temp_session_dir / f"{sid}.json"
    bak_path = temp_session_dir / f"{sid}.json.bak"
    bak_path.write_text(live_path.read_text(encoding="utf-8"), encoding="utf-8")
    # Now corrupt the live file — empty messages.
    live = json.loads(live_path.read_text(encoding="utf-8"))
    live["messages"] = []
    live_path.write_text(json.dumps(live), encoding="utf-8")

    from api.session_recovery import recover_all_sessions_on_startup
    result = recover_all_sessions_on_startup(temp_session_dir)
    assert result["restored"] == 1
    assert result["scanned"] >= 1

    restored = json.loads(live_path.read_text(encoding="utf-8"))
    assert len(restored["messages"]) == 1000


def test_recover_all_sessions_on_startup_restores_orphan_bak(temp_session_dir):
    """Startup self-heal: if only <sid>.json.bak survived, recreate <sid>.json."""
    sid = _make_session_on_disk(temp_session_dir, n_msgs=293)
    live_path = temp_session_dir / f"{sid}.json"
    bak_path = temp_session_dir / f"{sid}.json.bak"
    bak_path.write_text(live_path.read_text(encoding="utf-8"), encoding="utf-8")
    live_path.unlink()

    from api.session_recovery import recover_all_sessions_on_startup
    result = recover_all_sessions_on_startup(temp_session_dir)

    assert result["restored"] == 1
    assert result["scanned"] == 1
    assert result.get("orphaned_backups") == 1
    restored = json.loads(live_path.read_text(encoding="utf-8"))
    assert len(restored["messages"]) == 293


def test_recover_all_sessions_on_startup_rebuilds_index_after_orphan_restore(temp_session_dir, monkeypatch):
    """A restored orphan must be visible through the WebUI session index immediately."""
    import api.models as _m

    sid = _make_session_on_disk(temp_session_dir, n_msgs=42)
    live_path = temp_session_dir / f"{sid}.json"
    bak_path = temp_session_dir / f"{sid}.json.bak"
    bak_path.write_text(live_path.read_text(encoding="utf-8"), encoding="utf-8")
    live_path.unlink()

    stale_index = temp_session_dir / "_index.json"
    stale_index.write_text(json.dumps([]), encoding="utf-8")
    monkeypatch.setattr(_m, "SESSION_INDEX_FILE", stale_index)

    from api.session_recovery import recover_all_sessions_on_startup
    result = recover_all_sessions_on_startup(temp_session_dir, rebuild_index=True)

    assert result["restored"] == 1
    index = json.loads(stale_index.read_text(encoding="utf-8"))
    assert [entry["session_id"] for entry in index] == [sid]
    assert index[0]["message_count"] == 42


def test_orphan_bak_recovery_skips_sessions_absent_from_state_db(temp_session_dir):
    """Do not resurrect an explicitly deleted session when state.db lacks the row."""
    import sqlite3

    sid = _make_session_on_disk(temp_session_dir, n_msgs=12)
    live_path = temp_session_dir / f"{sid}.json"
    bak_path = temp_session_dir / f"{sid}.json.bak"
    bak_path.write_text(live_path.read_text(encoding="utf-8"), encoding="utf-8")
    live_path.unlink()

    state_db = temp_session_dir / "state.db"
    with sqlite3.connect(state_db) as conn:
        conn.execute("create table sessions (id text primary key)")
        conn.execute("insert into sessions (id) values (?)", ("different_session",))

    from api.session_recovery import recover_all_sessions_on_startup
    result = recover_all_sessions_on_startup(temp_session_dir, state_db_path=state_db)

    assert result["restored"] == 0
    assert result["scanned"] == 0
    assert result["orphaned_backups"] == 0
    assert not live_path.exists()


def test_recover_all_sessions_on_startup_is_idempotent_no_op_on_clean_state(temp_session_dir):
    """A clean install (no .bak files) must not modify anything."""
    sid = _make_session_on_disk(temp_session_dir, n_msgs=1000)
    live_before = (temp_session_dir / f"{sid}.json").read_text(encoding="utf-8")

    from api.session_recovery import recover_all_sessions_on_startup
    result = recover_all_sessions_on_startup(temp_session_dir)
    assert result["restored"] == 0

    live_after = (temp_session_dir / f"{sid}.json").read_text(encoding="utf-8")
    assert live_before == live_after


def test_recover_all_sessions_on_startup_skips_non_session_index_json(temp_session_dir):
    """Regression for v0.50.284 startup: ``_index.json`` is a top-level list
    (not a dict), and the recovery scanner globs ``*.json``. Without the
    underscore-prefix skip + ``isinstance(data, dict)`` guard in ``_msg_count``,
    the very first iteration crashed with ``AttributeError: 'list' object has
    no attribute 'get'`` and the broad ``except Exception`` in server.py
    swallowed the error, so recovery silently no-op'd in production.
    """
    # Simulate the production session dir: 1 valid session + _index.json
    sid = _make_session_on_disk(temp_session_dir, n_msgs=1000)
    # _index.json is the index file shape — a top-level list of metadata dicts
    index_path = temp_session_dir / "_index.json"
    index_path.write_text(
        json.dumps([
            {"session_id": sid, "title": "Test", "updated_at": 1.0},
            {"session_id": "other", "title": "Other", "updated_at": 2.0},
        ]),
        encoding="utf-8",
    )

    from api.session_recovery import recover_all_sessions_on_startup
    # Before the fix, this raised AttributeError; the broad except in server.py
    # swallowed it and printed [recovery] startup recovery failed: 'list'
    # object has no attribute 'get'. Now the scanner skips _index.json
    # entirely (underscore-prefix convention) and continues scanning real
    # session files.
    result = recover_all_sessions_on_startup(temp_session_dir)
    assert result["restored"] == 0
    # The 1 valid session was scanned; _index.json was skipped (not counted)
    assert result["scanned"] == 1, (
        f"_index.json must be skipped, scanned should be 1, got {result['scanned']}"
    )


def test_msg_count_returns_neg1_for_non_dict_top_level(temp_session_dir):
    """``_msg_count`` must not raise on a JSON file whose top-level is a list."""
    from api.session_recovery import _msg_count
    list_shaped = temp_session_dir / "_index.json"
    list_shaped.write_text(json.dumps([{"session_id": "x"}]), encoding="utf-8")
    # Pre-fix: AttributeError. Post-fix: -1.
    assert _msg_count(list_shaped) == -1

