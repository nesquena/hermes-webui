"""The sidebar reconciliation is cached across polls, keyed by its real revision.

#8368 memoized the sidecar+state.db walk for ONE build. That is the wrong
lifetime: the overlay writes the reconciled ``message_count`` into the response
payload and never into the sidecar, so every grown row is "grown" again on the
next poll and the walk runs again -- measured 98 rows x 0.19 s = 18.8 s of a
20 s ``all_sessions.state_db_overrides`` stage, while the SQL for the same ids
takes 7 ms.

The revision an answer belongs to is the sidecar bytes AND the state store's
generation (its message count is not a revision: a row-addressed update rewrites
a payload without moving either count). The tests below pin:

  * a second poll over an unchanged board does NOT re-parse the sidecar and does
    NOT re-read state.db (the unchanged-board counters);
  * a sidecar rewrite invalidates the entry (the visible count is re-derived);
  * a state.db count change invalidates the entry (append schedule);
  * a same-count store write invalidates the entry (native update schedule);
  * a write that lands DURING the walk is not published under the newer
    generation -- the next poll re-derives instead of serving a stale answer;
  * an unprovable store generation caches nothing at all;
  * the answer itself, including its hidden-row provenance, is unchanged.
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
    # The cache keys on the resolved store's stat signature. Point it at a real
    # (harmless) file under tmp_path: the walk's state.db read is stubbed below,
    # so only its generation -- the file's stat -- is under test.
    db = tmp_path / "state.db"
    db.write_bytes(b"state-store-v1")
    monkeypatch.setattr(models, "_agent_state_db_path", lambda profile=None: db)
    yield sessions
    if clear is not None:
        clear()


def _state_db(store):
    """The (fake) state.db file the fixture resolved for this test."""
    return store.parent / "state.db"


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


def _commit_to_store(db, *, payload=b"state-store-v2"):
    """Simulate a commit to the store without touching the message count.

    A row-addressed write (``set_message_api_content``) rewrites a row's payload
    and nothing else: no append, no count change, no watermark. The store file
    is what moves.
    """
    with open(db, "wb") as handle:
        handle.write(payload)
    st = db.stat()
    os.utime(db, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


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
    # Sequential control: prime -> save -> poll must answer from the new bytes,
    # on the same code path the cache serves.
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
    _commit_to_store(_state_db(store), payload=b"state-store-appended")
    grown = _walk(count=5)

    assert loads["loads"] == 2, (
        "state.db advanced past the cached revision but the stale answer was served"
    )
    assert grown is not None and grown.visible > 3, (
        f"two more state.db rows must raise the visible count (got {grown})"
    )


def test_a_same_count_store_write_invalidates_the_entry(store, monkeypatch):
    # The native path (``set_message_api_content`` from the turn prologue)
    # rewrites a row's payload and moves neither the total nor the active count.
    # That payload is part of the merge identity, so the count based key alone
    # would keep serving the previous answer forever.
    _write_sidecar(store, [_row("user", "hello", 1.0)])
    state = _install_state_db(monkeypatch, _state_rows(3))
    loads = _count_loads(monkeypatch)

    primed = _walk(count=3)
    assert primed is not None
    assert loads["loads"] == 1

    # Control: without a store write, the same poll is answered from cache.
    assert _walk(count=3) == primed
    assert loads["loads"] == 1, "an unchanged store must still hit the cache"

    # Same row count, new payload: only the store file moves. The restamped row
    # now duplicates the sidecar's own text, so the merge identity changes even
    # though neither count does -- the answer must move with it.
    state["rows"] = [
        _row("user", "hello", 100.0),
        _row("assistant", "m1", 101.0),
        _row("user", "m2", 102.0),
    ]
    _commit_to_store(_state_db(store))

    refreshed = _walk(count=3)
    assert loads["loads"] == 2, (
        "a same-count store write left the cached answer in place: the count is "
        "not a transcript revision"
    )
    assert refreshed is not None and refreshed.visible != primed.visible, (
        "the walk must re-derive the transcript after the write "
        f"(before={primed}, after={refreshed})"
    )


def test_a_write_during_the_walk_is_not_published(store, monkeypatch):
    # A publication must carry the generation the walk READ, never the one the
    # store has at write time: attaching an old answer to a newer signature is
    # a durable stale entry, unlike an in-flight request choosing an older
    # snapshot.
    _write_sidecar(store, [_row("user", "hello", 1.0)])
    _install_state_db(monkeypatch, _state_rows(3))
    loads = _count_loads(monkeypatch)

    original = (store / f"{SID}.json").read_bytes()
    original_st = (store / f"{SID}.json").stat()
    raced = {"done": False}
    real_load = models.Session.load.__func__

    def _load_and_save_mid_walk(cls, sid):
        session = real_load(cls, sid)
        if not raced["done"]:
            raced["done"] = True
            # A real save lands while this walk is still reconciling.
            _write_sidecar(
                store,
                [
                    _row("user", "hello", 1.0),
                    _row("assistant", "hi", 2.0),
                    _row("user", "again", 3.0),
                ],
            )
        return session

    monkeypatch.setattr(models.Session, "load", classmethod(_load_and_save_mid_walk))

    stale = _walk(count=3)
    assert stale is not None, "the in-flight walk still returns its own snapshot"
    assert raced["done"] is True
    assert loads["loads"] == 1

    # Put the sidecar back exactly as this poll saw it: the revision is then
    # byte- and nanosecond-identical, so a second poll can only miss if nothing
    # was published.
    path = store / f"{SID}.json"
    path.write_bytes(original)
    os.utime(path, ns=(original_st.st_atime_ns, original_st.st_mtime_ns))

    second = _walk(count=3)
    assert loads["loads"] == 2, (
        "the walk published an answer derived from a snapshot that changed under "
        "it, so the next poll served a stale count"
    )
    assert second is not None and second.visible == stale.visible, (
        "the re-derived answer must describe the same revision this poll reads"
    )


def test_an_unprovable_store_generation_caches_nothing(store, monkeypatch):
    # Three states, not two: an unresolvable store is UNKNOWN, and an unknown
    # revision must be re-read, never answered from cache.
    _write_sidecar(store, [_row("user", "hello", 1.0)])
    _install_state_db(monkeypatch, _state_rows(3))
    loads = _count_loads(monkeypatch)
    monkeypatch.setattr(models, "_agent_state_db_path", lambda profile=None: None)

    assert _walk(count=3) is not None
    assert _walk(count=3) is not None
    assert loads["loads"] == 2, (
        "a walk with no provable store generation was cached anyway"
    )


def test_the_hidden_row_provenance_survives_the_cache(store, monkeypatch):
    # A hidden row is the one signal the caller must not pay twice (#7882); an
    # answer served from cache must carry the same provenance as the walk. The
    # transcript here really contains a hidden row -- a hidden-negative fixture
    # would pass without the flag ever being set.
    _write_sidecar(store, [_row("user", "hello", 1.0)])
    rows = _state_rows(2) + [
        {
            "role": "user",
            "content": "wakeup",
            "timestamp": 102.0,
            "_source": "delegation_wakeup",
        }
    ]
    _install_state_db(monkeypatch, rows)
    loads = _count_loads(monkeypatch)

    first = _walk(count=3)
    second = _walk(count=3)
    assert first is not None and second is not None
    assert first.hidden_excluded is True, (
        "the fixture must actually contain a hidden row, or this proves nothing"
    )
    # The sidecar row plus three state rows, one of them hidden and dropped.
    assert first.visible == 3, (
        f"the hidden row must not be counted (got {first.visible})"
    )
    assert loads["loads"] == 1, "the second poll must be the cache hit under test"
    assert second.hidden_excluded == first.hidden_excluded
    assert second.visible == first.visible
