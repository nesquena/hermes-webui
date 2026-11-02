"""The sidebar reconciliation is cached across polls, keyed by its real revision.

#8368 memoized the sidecar+state.db walk for ONE build. That is the wrong
lifetime: the overlay writes the reconciled ``message_count`` into the response
payload and never into the sidecar, so every grown row is "grown" again on the
next poll and the walk runs again -- measured 98 rows x 0.19 s = 18.8 s of a
20 s ``all_sessions.state_db_overrides`` stage, while the SQL for the same ids
takes 7 ms.

The revision an answer belongs to is (sidecar bytes, state.db message count),
and that is what the cache keys on, so these tests pin:

  * a second poll over an unchanged board does NOT re-parse the sidecar and does
    NOT re-read state.db;
  * a sidecar rewrite invalidates the entry (the visible count is re-derived);
  * a state.db count change invalidates the entry;
  * the answer itself is unchanged -- the cache is transparent.
"""
from __future__ import annotations

import json
import os

import pytest

import api.models as models

SID = "s1"


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    # getattr, not a direct call: the same file must run against a tree without
    # the cache, so the regression proof is the assertion below and not an
    # AttributeError on a missing helper.
    clear = getattr(models, "_clear_sidebar_reconcile_cache", None)
    if clear is not None:
        clear()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    yield sessions
    if clear is not None:
        clear()


def _write_sidecar(sessions, messages, *, title="t"):
    path = sessions / f"{SID}.json"
    path.write_text(
        json.dumps(
            {
                "session_id": SID,
                "title": title,
                "created_at": 1.0,
                "updated_at": 2.0,
                "messages": messages,
            }
        ),
        encoding="utf-8",
    )
    # The cache keys on st_mtime_ns; two writes inside the same nanosecond tick
    # would look identical, and the test would then be proving the clock instead
    # of the cache.
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    return path


def _row(role, text, ts):
    return {"role": role, "content": text, "timestamp": ts}


def _state_rows(n, start=100.0):
    return [_row("user" if i % 2 == 0 else "assistant", f"m{i}", start + i) for i in range(n)]


def _install_state_db(monkeypatch, rows):
    counter = {"calls": 0, "rows": rows}

    def _read(sid, profile=None):
        counter["calls"] += 1
        return list(counter["rows"])

    monkeypatch.setattr(models, "get_state_db_session_messages", _read)
    return counter


def _count_loads(monkeypatch):
    """Count full sidecar parses -- the expensive half the cache must skip."""
    counter = {"loads": 0, "metadata_loads": 0}
    real_load = models.Session.load.__func__
    real_metadata = models.Session.load_metadata_only.__func__

    def _load(cls, sid):
        counter["loads"] += 1
        return real_load(cls, sid)

    def _metadata(cls, sid, **kwargs):
        counter["metadata_loads"] += 1
        return real_metadata(cls, sid, **kwargs)

    monkeypatch.setattr(models.Session, "load", classmethod(_load))
    monkeypatch.setattr(models.Session, "load_metadata_only", classmethod(_metadata))
    return counter


def _walk(sid=SID, *, count):
    return models._reconciled_visible_count_for_sidebar(
        sid, expected_state_count=count
    )


def test_second_poll_over_an_unchanged_board_does_not_rewalk(store, monkeypatch):
    _write_sidecar(store, [_row("user", "hello", 1.0)])
    state = _install_state_db(monkeypatch, _state_rows(3))
    loads = _count_loads(monkeypatch)

    first = _walk(count=3)
    assert first is not None, "the walk must resolve a count for a readable board"
    assert loads["loads"] == 1 and state["calls"] == 1

    second = _walk(count=3)
    assert second == first, "the cached answer must be identical to the walked one"
    assert loads["loads"] == 1, (
        "a second poll re-parsed the sidecar: the memo still dies with the request"
    )
    assert loads["metadata_loads"] == 1, (
        "a cache hit must cost a stat, not even the metadata-prefix read"
    )
    assert state["calls"] == 1, "a second poll re-read state.db"


def test_a_sidecar_rewrite_invalidates_the_entry(store, monkeypatch):
    _write_sidecar(store, [_row("user", "hello", 1.0)])
    _install_state_db(monkeypatch, _state_rows(3))
    loads = _count_loads(monkeypatch)

    before = _walk(count=3)
    assert before is not None
    assert loads["loads"] == 1

    _write_sidecar(store, [_row("user", "hello", 1.0), _row("assistant", "hi", 2.0)])
    after = _walk(count=3)

    assert loads["loads"] == 2, (
        "the sidecar changed but the cached answer was served anyway (stale board)"
    )
    assert after is not None and after.visible > before.visible, (
        f"the rewritten sidecar must be re-derived (before={before}, after={after})"
    )


def test_a_state_db_count_change_invalidates_the_entry(store, monkeypatch):
    _write_sidecar(store, [_row("user", "hello", 1.0)])
    state = _install_state_db(monkeypatch, _state_rows(3))
    loads = _count_loads(monkeypatch)

    assert _walk(count=3) is not None
    assert loads["loads"] == 1

    state["rows"] = _state_rows(5)
    grown = _walk(count=5)

    assert loads["loads"] == 2, (
        "state.db advanced past the cached revision but the stale answer was served"
    )
    assert grown is not None and grown.visible > 3, (
        f"two more state.db rows must raise the visible count (got {grown})"
    )


def test_the_hidden_row_provenance_survives_the_cache(store, monkeypatch):
    # A hidden row is the one signal the caller must not pay twice (#7882); an
    # answer served from cache must carry the same provenance as the walk.
    _write_sidecar(store, [_row("user", "hello", 1.0)])
    _install_state_db(monkeypatch, _state_rows(3))
    _count_loads(monkeypatch)

    first = _walk(count=3)
    second = _walk(count=3)
    assert first is not None and second is not None
    assert second.hidden_excluded == first.hidden_excluded
