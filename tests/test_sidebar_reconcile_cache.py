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
import sqlite3

import pytest

import api.models as models

SID = "s1"

# The autouse fixture below stubs the store path; the real-store tests at the
# end of this file put the production resolver back.
_REAL_AGENT_STATE_DB_PATH = models._agent_state_db_path
_REAL_STATE_DB_READER = models.get_state_db_session_messages


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


def _make_unreadable(monkeypatch, target):
    """Make exactly *target* refuse to stat, the way a defended file does.

    chmod is not enough (the test owns the file) and a whole-module stub would
    hide which file the code actually consults, so the failure is injected on
    the one path that must be probed.
    """
    real_stat = os.stat
    wanted = os.fspath(target)

    def _stat(path, *args, **kwargs):
        if os.fspath(path) == wanted:
            raise PermissionError(13, "Permission denied", wanted)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(models.os, "stat", _stat)


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
    #
    # The file is deliberately NOT restored afterwards. Putting the old bytes
    # back does not restore st_ctime, so the pre-write signature can never be
    # reproduced: the entry then misses for a reason unrelated to the defect and
    # the assertion holds even against code that publishes the stale answer.
    # Reading the newer durable result back is the discriminator.
    _write_sidecar(store, [_row("user", "hello", 1.0)])
    _install_state_db(monkeypatch, _state_rows(3))
    loads = _count_loads(monkeypatch)

    raced = {"done": False}
    real_load = models.Session.load.__func__
    saved_rows = [
        _row("user", "hello", 1.0),
        _row("assistant", "hi", 2.0),
        _row("user", "again", 3.0),
    ]

    def _load_and_save_mid_walk(cls, sid):
        session = real_load(cls, sid)
        if not raced["done"]:
            raced["done"] = True
            # A real save lands while this walk is still reconciling.
            _write_sidecar(store, saved_rows)
        return session

    monkeypatch.setattr(models.Session, "load", classmethod(_load_and_save_mid_walk))

    stale = _walk(count=3)
    assert stale is not None, "the in-flight walk still returns its own snapshot"
    assert raced["done"] is True
    assert loads["loads"] == 1

    # Control: the save really landed, so the newer durable result is reachable
    # and the assertion below cannot pass on a save that never happened.
    on_disk = json.loads((store / f"{SID}.json").read_text(encoding="utf-8"))
    assert len(on_disk["messages"]) == 3

    fresh = _walk(count=3)
    assert loads["loads"] == 2, (
        "the walk published an answer derived from a snapshot that changed under "
        "it under the newer signature, so the next poll served a stale count"
    )
    assert fresh is not None and fresh.visible == 6, (
        "a fresh poll must read the saved rows back: 3 sidecar rows plus the 3 "
        f"state rows (got {fresh.visible if fresh else None}, in-flight snapshot "
        f"was {stale.visible})"
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


@pytest.mark.parametrize("companion", ["", "-wal", "-journal"])
def test_an_unreadable_state_file_makes_the_generation_unprovable(
    store, monkeypatch, companion
):
    # A generation is only provable if EVERY file that can carry a commit is
    # either provably absent or provably stat'd. An unreadable companion may
    # have been written to, so the whole generation is rejected: accepting it
    # would cache an answer that can silently be older than the store, which is
    # exactly the durable-stale defect the generation exists to prevent.
    _write_sidecar(store, [_row("user", "hello", 1.0)])
    _install_state_db(monkeypatch, _state_rows(3))
    loads = _count_loads(monkeypatch)

    db = _state_db(store)
    target = db if companion == "" else db.with_name(db.name + companion)
    if companion:
        target.write_bytes(b"companion-segment")
    _make_unreadable(monkeypatch, target)

    assert models._sidebar_reconcile_store_generation() is None, (
        f"an unreadable state file ({target.name}) still produced a provable "
        "generation"
    )

    first = _walk(count=3)
    second = _walk(count=3)
    assert first is not None and second is not None
    assert loads["loads"] == 2, (
        "an unprovable generation was cached anyway: the next poll cannot prove "
        "the store did not move under it"
    )


def test_absent_companions_are_still_a_legitimate_generation(store, monkeypatch):
    # The mirror of the test above, so the fix cannot be satisfied by rejecting
    # everything: a store with no journal/WAL companion is normal and must cache.
    _write_sidecar(store, [_row("user", "hello", 1.0)])
    _install_state_db(monkeypatch, _state_rows(3))
    loads = _count_loads(monkeypatch)

    db = _state_db(store)
    assert not db.with_name(db.name + "-wal").exists()
    assert not db.with_name(db.name + "-journal").exists()

    generation = models._sidebar_reconcile_store_generation()
    assert generation is not None, "an absent companion is not an unprovable one"
    assert generation[2] is None and generation[3] is None

    assert _walk(count=3) is not None
    assert _walk(count=3) is not None
    assert loads["loads"] == 1, "a provable store must still be answered from cache"


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


def test_a_row_without_a_caller_count_is_cached_too(store, monkeypatch):
    # Production shape: the overlay caps its count/timestamp tier at the top-N
    # paint-priority rows, so every row past the cap arrives with
    # ``expected_state_count=None``. Gating the lookup on that count left those
    # rows re-walking on every poll -- the 5-20 s sidebar stall the cache exists
    # to remove, still there with the cache "working" for the capped rows only.
    # The revision is provable from the two sources alone, so no count is
    # needed to key it.
    _write_sidecar(store, [_row("user", "hello", 1.0)])
    _install_state_db(monkeypatch, _state_rows(3))
    loads = _count_loads(monkeypatch)

    first = models._reconciled_visible_count_for_sidebar(SID)
    assert first is not None, "an uncapped walk must still resolve a count"
    assert loads["loads"] == 1

    second = models._reconciled_visible_count_for_sidebar(SID)
    assert second == first, "the cached answer must match the walked one"
    assert loads["loads"] == 1, (
        "a row outside the paint cap re-walked on the second poll: the lookup "
        "is still gated on a caller-supplied count"
    )


# --- The store generation fingerprints the file the reader opens ------------
#
# The production reader (``get_state_db_session_messages`` ->
# ``open_state_db_readonly``) connects to ``state.db`` with any symlink
# resolved, and SQLite keeps ``-wal``/``-journal`` beside that resolved file.
# These tests use a REAL SQLite store in WAL mode and the REAL reader (wrapped
# only to count calls), with a writer connection that stays open like the
# Agent's, so a same-count commit lands where a native writer puts it: in the
# target's WAL, with neither the message count nor the main file moving. The
# layouts are an ordinary file, a directory symlink and an operator-made
# ``state.db -> other/store.sqlite3`` file symlink; all three must stay fresh.

_LAYOUTS = ("plain", "dir_symlink", "file_symlink")


def _real_store(tmp_path, monkeypatch, layout):
    """Build a WAL-mode store for *layout*; return (alias, target, writer)."""
    root = tmp_path / "layout"
    root.mkdir()
    if layout == "plain":
        home = root / "home"
        home.mkdir()
        target = alias = home / "state.db"
    elif layout == "dir_symlink":
        real_home = root / "real-home"
        real_home.mkdir()
        (root / "home").symlink_to(real_home, target_is_directory=True)
        target = real_home / "state.db"
        alias = root / "home" / "state.db"
    elif layout == "file_symlink":
        home = root / "home"
        home.mkdir()
        other = root / "other"
        other.mkdir()
        target = other / "store.sqlite3"
        alias = home / "state.db"
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(layout)
    setup = sqlite3.connect(str(target))
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT, role TEXT, content TEXT, timestamp REAL, "
        "active INTEGER DEFAULT 1, api_content TEXT)"
    )
    setup.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        [(SID, r["role"], r["content"], r["timestamp"]) for r in _state_rows(3)],
    )
    setup.commit()
    setup.close()
    if layout == "file_symlink":
        alias.symlink_to(target)
    # The writer opens the path the store is configured under, as the Agent
    # does, and stays open for the whole test: closing the last connection
    # would checkpoint the WAL into the main file and hide the schedule.
    writer = sqlite3.connect(str(alias))
    writer.execute("SELECT COUNT(*) FROM messages").fetchone()
    monkeypatch.setattr(models, "_agent_state_db_path", _REAL_AGENT_STATE_DB_PATH)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: alias)
    return alias, target, writer


def _count_real_reads(monkeypatch):
    counter = {"calls": 0}

    def _read(*args, **kwargs):
        counter["calls"] += 1
        return _REAL_STATE_DB_READER(*args, **kwargs)

    monkeypatch.setattr(models, "get_state_db_session_messages", _read)
    return counter


def _main_file_stat(path):
    st = os.stat(path)
    return (st.st_ino, st.st_size, st.st_mtime_ns)


@pytest.mark.parametrize("count", [3, None], ids=["caller_count", "no_caller_count"])
@pytest.mark.parametrize("layout", _LAYOUTS)
def test_a_same_count_wal_commit_reads_fresh_through_every_store_layout(
    store, monkeypatch, tmp_path, layout, count
):
    _write_sidecar(store, [_row("user", "hello", 1.0)])
    alias, target, writer = _real_store(tmp_path, monkeypatch, layout)
    try:
        reads = _count_real_reads(monkeypatch)
        loads = _count_loads(monkeypatch)
        walk = lambda: models._reconciled_visible_count_for_sidebar(  # noqa: E731
            SID, expected_state_count=count
        )

        primed = walk()
        assert primed is not None
        assert (loads["loads"], reads["calls"]) == (1, 1)

        # A proved hit before the write: the second poll neither parses the
        # sidecar nor reads the store again.
        assert walk() == primed
        assert (loads["loads"], reads["calls"]) == (1, 1), (
            f"{layout}: an unchanged store must be answered from the cache"
        )

        # A row-addressed rewrite: same row count, same active count. Only the
        # WAL of the file SQLite resolved moves.
        main_before = _main_file_stat(target)
        writer.execute("UPDATE messages SET content = ? WHERE id = 1", ("hello",))
        writer.commit()
        assert writer.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
        assert _main_file_stat(target) == main_before, (
            "precondition: the commit stayed in the WAL"
        )
        assert os.path.getsize(f"{target}-wal") > 0, (
            "precondition: SQLite put the WAL beside the resolved file"
        )

        refreshed = walk()

        # The authority: the same question with no cache at all.
        models._clear_sidebar_reconcile_cache()
        authority = walk()
        assert authority is not None and authority.visible != primed.visible, (
            "precondition: the rewrite changes the reconciled answer "
            f"(before={primed}, after={authority})"
        )
        assert refreshed == authority, (
            f"{layout}: a same-count commit left the cached answer in place "
            f"(served={refreshed}, fresh={authority})"
        )
    finally:
        writer.close()


def test_the_generation_names_the_resolved_store(store, monkeypatch, tmp_path):
    alias, target, writer = _real_store(tmp_path, monkeypatch, "file_symlink")
    try:
        generation = models._sidebar_reconcile_store_generation()
        assert generation is not None
        assert generation[0] == str(target.resolve()), (
            "the generation must fingerprint the file the reader opens, "
            f"not the alias {alias}"
        )
        # Retargeting the alias at a different store is a different generation.
        other = tmp_path / "layout" / "other" / "second.sqlite3"
        second = sqlite3.connect(str(other))
        second.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
        second.commit()
        second.close()
        alias.unlink()
        alias.symlink_to(other)
        moved = models._sidebar_reconcile_store_generation()
        assert moved is not None and moved != generation
        assert moved[0] == str(other.resolve())
    finally:
        writer.close()


def test_an_unresolvable_store_path_caches_nothing(store, monkeypatch, tmp_path):
    # A self-referencing symlink: depending on the Python version resolve()
    # raises or hands the loop back (and stat then fails). Either way the
    # generation is unproved, so nothing may be served or published.
    loop = tmp_path / "loop.db"
    loop.symlink_to(loop)
    monkeypatch.setattr(models, "_agent_state_db_path", lambda profile=None: loop)
    assert models._sidebar_reconcile_store_generation() is None


@pytest.mark.parametrize("layout", ["plain", "file_symlink"])
def test_the_native_prologue_backfill_reads_fresh_through_a_store_symlink(
    store, monkeypatch, tmp_path, layout
):
    # The Agent's own writer: the turn prologue's row-addressed
    # ``set_message_api_content`` backfill, on a store the Agent opened by the
    # path it is configured with. The sidecar already carries the provider
    # bytes, so the backfill makes the store row its duplicate: one row fewer,
    # with no count moving.
    hermes_state = pytest.importorskip("hermes_state")
    if not hasattr(hermes_state.SessionDB, "set_message_api_content"):
        pytest.skip("this Agent has no row-addressed api_content writer")
    root = tmp_path / "native"
    home = root / "home"
    home.mkdir(parents=True)
    alias = home / "state.db"
    if layout == "file_symlink":
        (root / "other").mkdir()
        target = root / "other" / "store.sqlite3"
        builder = hermes_state.SessionDB(db_path=target)
        builder.close()
        alias.symlink_to(target)
    else:
        target = alias
    db = hermes_state.SessionDB(db_path=alias)
    try:
        db.create_session(SID, "webui")
        row_id = db.append_message(SID, "user", "hello", timestamp=100.0)
        db.append_message(SID, "assistant", "a1", timestamp=101.0)
        db.append_message(SID, "user", "q2", timestamp=102.0)
        if layout == "file_symlink":
            assert alias.is_symlink(), "precondition: the alias is still a symlink"
        _write_sidecar(store, [dict(_row("user", "hello", 100.0), api_content="P")])
        monkeypatch.setattr(models, "_agent_state_db_path", _REAL_AGENT_STATE_DB_PATH)
        monkeypatch.setattr(models, "_active_state_db_path", lambda: alias)
        models._clear_sidebar_reconcile_cache()
        reads = _count_real_reads(monkeypatch)
        loads = _count_loads(monkeypatch)
        walk = lambda: models._reconciled_visible_count_for_sidebar(  # noqa: E731
            SID, expected_state_count=3
        )

        primed = walk()
        assert primed is not None
        assert walk() == primed
        assert (loads["loads"], reads["calls"]) == (1, 1), (
            f"{layout}: an unchanged store must be answered from the cache"
        )

        main_before = _main_file_stat(target)
        assert db.set_message_api_content(SID, row_id, "hello", "P") == 1
        assert _main_file_stat(target) == main_before, (
            "precondition: the backfill stayed in the WAL"
        )

        refreshed = walk()
        models._clear_sidebar_reconcile_cache()
        authority = walk()
        assert authority is not None and authority.visible != primed.visible, (
            "precondition: the backfill changes the reconciled answer "
            f"(before={primed}, after={authority})"
        )
        assert refreshed == authority, (
            f"{layout}: the native backfill left the cached answer in place "
            f"(served={refreshed}, fresh={authority})"
        )
    finally:
        db.close()
