import threading
from types import SimpleNamespace

import pytest

import api.config as config
import api.routes as routes
from api import session_events


@pytest.fixture(autouse=True)
def _isolated_session_list_cache_state():
    routes._session_list_cache_clear()
    with routes._SESSIONS_CACHE_LOCK:
        routes._SESSIONS_CACHE_INFLIGHT.clear()
    yield
    routes._session_list_cache_clear()
    with routes._SESSIONS_CACHE_LOCK:
        routes._SESSIONS_CACHE_INFLIGHT.clear()


class _StageRecorder:
    def __init__(self):
        self.stages = []

    def stage(self, name):
        self.stages.append(str(name))


def _session_cache_payload(marker: str, *, all_profiles: bool = False) -> dict:
    return {
        "sessions": [{"session_id": marker}],
        "cli_count": 0,
        "all_profiles": all_profiles,
        "active_profile": None,
        "other_profile_count": 0,
    }


def test_session_list_cache_key_separates_profile_and_all_profiles():
    routes._session_list_cache_clear()

    calls = []

    def builder_profile_a():
        calls.append("default")
        return _session_cache_payload("a")

    def builder_profile_a_all():
        calls.append("default_all")
        return _session_cache_payload("a_all", all_profiles=True)

    def builder_profile_b():
        calls.append("other")
        return _session_cache_payload("b")

    key_a = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    key_a_all = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=True,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    key_b = routes._session_list_cache_key(
        active_profile="other",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    assert routes._get_cached_session_list_payload(key=key_a, builder=builder_profile_a) == _session_cache_payload("a")
    assert calls == ["default"]
    assert routes._get_cached_session_list_payload(key=key_a, builder=builder_profile_a) == _session_cache_payload("a")
    assert calls == ["default"]
    assert routes._get_cached_session_list_payload(key=key_b, builder=builder_profile_b) == _session_cache_payload("b")
    assert calls == ["default", "other"]
    assert routes._get_cached_session_list_payload(key=key_a_all, builder=builder_profile_a_all) == _session_cache_payload("a_all", all_profiles=True)
    assert calls == ["default", "other", "default_all"]
    assert routes._get_cached_session_list_payload(key=key_a, builder=builder_profile_a) == _session_cache_payload("a")
    assert calls == ["default", "other", "default_all"]


def test_session_list_cache_singleflight_rebuild_once(monkeypatch):
    routes._session_list_cache_clear()
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))

    started = threading.Event()
    release = threading.Event()
    calls = 0
    lock = threading.Lock()

    def builder():
        nonlocal calls
        with lock:
            calls += 1
        started.set()
        release.wait()
        return _session_cache_payload("singleflight")

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    results = []
    errors = []

    def reader():
        try:
            results.append(routes._get_cached_session_list_payload(key=key, builder=builder))
        except Exception as exc:
            errors.append(exc)

    owner = threading.Thread(target=reader)
    follower = threading.Thread(target=reader)
    owner.start()
    assert started.wait(1.0)
    follower.start()
    release.set()
    owner.join(2)
    follower.join(2)
    assert not errors
    assert len(results) == 2
    assert results[0] == _session_cache_payload("singleflight")
    assert results[1] == _session_cache_payload("singleflight")
    assert calls == 1


def test_session_list_cache_follower_wait_stage_when_rebuild_inflight(monkeypatch):
    routes._session_list_cache_clear()
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))

    started = threading.Event()
    release = threading.Event()

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    def builder():
        started.set()
        release.wait()
        return _session_cache_payload("wait")

    follower_diag = _StageRecorder()
    owner_diag = _StageRecorder()
    wait_seen = threading.Event()
    original_follower_stage = follower_diag.stage

    def follower_stage(name):
        original_follower_stage(name)
        if name == "session_list_cache_wait":
            wait_seen.set()

    follower_diag.stage = follower_stage

    def owner():
        routes._get_cached_session_list_payload(
            key=key,
            builder=builder,
            diag=owner_diag,
        )

    def follower():
        routes._get_cached_session_list_payload(
            key=key,
            builder=builder,
            diag=follower_diag,
        )

    owner_thread = threading.Thread(target=owner)
    follower_thread = threading.Thread(target=follower)
    try:
        owner_thread.start()
        assert started.wait(1.0)
        follower_thread.start()
        assert wait_seen.wait(1.0)
    finally:
        release.set()
        owner_thread.join(2)
        follower_thread.join(2)

    assert "session_list_cache_wait" in follower_diag.stages
    assert "session_list_cache_hit" in owner_diag.stages or "session_list_cache_stored" in owner_diag.stages


def test_session_list_cache_source_changed_owner_rebuilds_while_follower_reuses_stale(monkeypatch):
    routes._session_list_cache_clear()

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    routes._session_list_cache_set(key, _session_cache_payload("stale"))
    with routes._SESSIONS_CACHE_LOCK:
        ts, stamp, payload = routes._SESSIONS_CACHE[key]
        # The "changed" stamp below has no recognisable row content, so the
        # rebuild-rate floor never holds it: the entry only has to be past the TTL.
        routes._SESSIONS_CACHE[key] = (
            ts - routes._SESSIONS_CACHE_TTL_SECONDS - 1.0,
            stamp,
            payload,
        )
    # Simulate state.db/WAL/fingerprint changing after the stale payload was
    # cached. The owner must rebuild synchronously so committed external state is
    # visible immediately, but followers can still use stale while that rebuild
    # is blocked; otherwise sidebar polling can pile up behind a slow rebuild.
    monkeypatch.setattr(
        routes,
        "_session_list_cache_source_stamp",
        lambda _key: ("changed",),
    )

    started = threading.Event()
    release = threading.Event()
    owner_result = {}
    follower_result = {}
    owner_diag = _StageRecorder()
    follower_diag = _StageRecorder()

    def builder():
        started.set()
        release.wait()
        return _session_cache_payload("fresh")

    def owner():
        owner_result["payload"] = routes._get_cached_session_list_payload(
            key=key,
            builder=builder,
            diag=owner_diag,
        )

    def follower():
        follower_result["payload"] = routes._get_cached_session_list_payload(
            key=key,
            builder=builder,
            diag=follower_diag,
        )

    owner_thread = threading.Thread(target=owner)
    follower_thread = threading.Thread(target=follower)
    try:
        owner_thread.start()
        assert started.wait(1.0)
        follower_thread.start()
        follower_thread.join(1.0)
        assert not follower_thread.is_alive()
    finally:
        release.set()
        owner_thread.join(2.0)
        follower_thread.join(2.0)

    assert owner_result["payload"] == _session_cache_payload("fresh")
    assert follower_result["payload"] == _session_cache_payload("stale")
    assert "session_list_cache_rebuild_owner" in owner_diag.stages
    assert "session_list_cache_wait_stale_fallback" in follower_diag.stages


def test_session_list_cache_rate_floor_holds_rebuilds(monkeypatch):
    """The 10 s floor must cap REBUILDS through the real consumer, not just cost.

    `_session_list_cache_get` reports a floor-held payload as "not fresh", and the
    consumer used to treat every non-fresh hit whose reason was not "source" as
    "return stale, start a background rebuild". Singleflight caps CONCURRENT
    rebuilds only, so with a writer touching state.db on every poll the floor
    still allowed one full all_sessions() per poll. This pins the consumer
    contract: no builder inside the floor, a real refresh once it expires, and no
    delay after an explicit rename/delete invalidation.
    """
    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    # A live session writes to state.db right after the entry was stored: the
    # STAT components then differ on every poll while the committed row content
    # does not. Only this shape — an 8-tuple with a stable fingerprint — may be
    # held; see `_session_list_cache_stamp_row_content`.
    stat = {"generation": 0}
    stable_row_content = ("fp-unchanged", None)

    def _changing_stat_stamp(_key):
        stat["generation"] += 1
        return (
            (stat["generation"], 1),
            (stat["generation"], 2),
            (stat["generation"], 3),
            (stat["generation"], 4),
            (stat["generation"], 5),
            stable_row_content[0],
            stable_row_content[1],
            0,
        )

    monkeypatch.setattr(
        routes, "_session_list_cache_source_stamp", _changing_stat_stamp
    )
    routes._session_list_cache_set(key, _session_cache_payload("initial"))

    built = []

    def builder():
        built.append(1)
        return _session_cache_payload(f"fresh-{len(built)}")

    diag = _StageRecorder()
    for _ in range(3):
        payload = routes._get_cached_session_list_payload(key=key, builder=builder, diag=diag)
        assert payload == _session_cache_payload("initial")

    assert built == [], "no rebuild may start inside the rebuild-rate floor"
    assert "session_list_cache_rate_floor_hold" in diag.stages
    assert "session_list_cache_stale_background_rebuild" not in diag.stages

    # Past the floor the same hit becomes an ordinary revalidation.
    with routes._SESSIONS_CACHE_LOCK:
        ts, stamp, cached_payload = routes._SESSIONS_CACHE[key]
        routes._SESSIONS_CACHE[key] = (
            ts - routes._SESSIONS_CACHE_MIN_REFRESH_SECONDS - 1.0,
            stamp,
            cached_payload,
        )
    refreshed = routes._get_cached_session_list_payload(key=key, builder=builder)
    assert refreshed == _session_cache_payload("fresh-1")
    assert len(built) == 1

    # An explicit rename/delete invalidation clears the entry, so the next request
    # rebuilds immediately even though the floor would otherwise hold it.
    session_events.publish_session_list_changed("session_rename", profile="default")
    assert routes._session_list_cache_get(key, allow_stale=True)[0] is None
    renamed = routes._get_cached_session_list_payload(key=key, builder=builder)
    assert renamed == _session_cache_payload("fresh-2")
    assert len(built) == 2


def test_session_list_cache_rate_floor_never_holds_a_committed_message(monkeypatch):
    """A settled row must reconcile on the next request, not after the floor.

    The reconciliation contract (`test_webui_state_db_reconciliation.py`) says an
    external writer — the official Desktop App continuing a WebUI-origin session,
    a CLI turn — is visible immediately. The floor only ever applies while the
    committed row content is unchanged, so a fingerprint move rebuilds at once.
    """
    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    rows = {"fingerprint": "rows-2"}

    def _stamp(_key):
        return (
            (1, 1),
            (1, 2),
            (1, 3),
            (1, 4),
            (1, 5),
            rows["fingerprint"],
            None,
            0,
        )

    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", _stamp)
    routes._session_list_cache_set(key, _session_cache_payload("two"))

    # Two messages settle in state.db a millisecond later; the entry is far inside
    # the floor, so only the row-content check can let it through.
    rows["fingerprint"] = "rows-4"
    built = []

    def builder():
        built.append(1)
        return _session_cache_payload("four")

    diag = _StageRecorder()
    payload = routes._get_cached_session_list_payload(key=key, builder=builder, diag=diag)
    assert payload == _session_cache_payload("four")
    assert built == [1], "committed rows must not wait for the rebuild-rate floor"
    assert "session_list_cache_rate_floor_hold" not in diag.stages


def test_content_fingerprint_marks_a_failed_read_as_unreadable(tmp_path):
    """A locked state.db yields an UNREADABLE fingerprint, not a NULL one.

    ``_sqlite_content_fingerprint`` degrades instead of blocking the hot path,
    and it used to degrade to ``None`` per table. Two of those compare equal, so
    a consumer could not tell "unchanged" from "could not read". The producer now
    marks a failed read explicitly. (#8694)
    """
    import sqlite3

    from api import models

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, title TEXT)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO sessions (title) VALUES ('s1')")
    conn.execute("INSERT INTO messages (body) VALUES ('m1')")
    conn.commit()
    conn.close()

    readable = models._sqlite_content_fingerprint(db_path)
    assert models._content_fingerprint_readable(readable) is True

    writer = sqlite3.connect(db_path, isolation_level=None)
    writer.execute("PRAGMA journal_mode=DELETE")
    writer.execute("BEGIN EXCLUSIVE")
    try:
        locked = models._sqlite_content_fingerprint(db_path)
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    assert locked is not None, "a per-table lock must not collapse into the total-failure None"
    assert models._content_fingerprint_readable(locked) is False


def test_session_list_cache_rate_floor_never_holds_an_unreadable_fingerprint(monkeypatch):
    """An unknown state.db may not license the ten-second hold. (#8694)

    With a writer touching state.db the stat components move on every poll, so
    the floor is the only thing between a poll and a full rebuild. The hold must
    therefore be authorized by a fingerprint that was actually READ: two
    unreadable stamps compare equal, and before this guard they held the entry
    while suppressing the file-stat reconciliation.
    """
    from api import models

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    stat = {"generation": 0}
    fingerprint = {"value": models._CONTENT_FINGERPRINT_UNREADABLE}

    def _changing_stat_stamp(_key):
        stat["generation"] += 1
        generation = stat["generation"]
        return (
            (generation, 1),
            (generation, 2),
            (generation, 3),
            (generation, 4),
            (generation, 5),
            fingerprint["value"],
            None,
            0,
        )

    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", _changing_stat_stamp)

    # Cached while the fingerprint read times out (a writer holds the lock) — the
    # exact state the review replayed.
    routes._session_list_cache_set(key, _session_cache_payload("two"))

    built = []

    def builder():
        built.append(1)
        return _session_cache_payload("four")

    diag = _StageRecorder()
    payload = routes._get_cached_session_list_payload(key=key, builder=builder, diag=diag)
    assert payload == _session_cache_payload("four")
    assert built == [1], "an unreadable fingerprint must never authorize the hold"
    assert "session_list_cache_rate_floor_hold" not in diag.stages


def test_session_list_cache_rate_floor_holds_through_an_all_profiles_fingerprint(monkeypatch):
    """All-profiles keys hold on a fully readable stack, and never on a partial one.

    The all-profiles stamp is a per-profile tuple of (db stat, wal stat, content
    fingerprint). One unreadable profile fingerprint in that set makes the whole
    stamp unusable as proof of "unchanged". (#8694)
    """
    from api import models

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=True,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    stat = {"generation": 0}
    profile_fingerprint: dict[str, object] = {"value": (7, 12)}

    def _changing_stat_stamp(_key):
        stat["generation"] += 1
        generation = stat["generation"]
        profile_stamp = (
            (1, 1),
            (1, 2),
            profile_fingerprint["value"],
        )
        return (
            (generation, 1),
            (generation, 2),
            (generation, 3),
            (generation, 4),
            (generation, 5),
            (40, 91),
            (profile_stamp,),
            0,
        )

    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", _changing_stat_stamp)
    routes._session_list_cache_set(key, _session_cache_payload("a_all", all_profiles=True))

    built = []

    def builder():
        built.append(1)
        return _session_cache_payload(f"fresh-{len(built)}", all_profiles=True)

    # Readable stack: the floor still holds (the feature is not disabled).
    diag = _StageRecorder()
    payload = routes._get_cached_session_list_payload(key=key, builder=builder, diag=diag)
    assert payload == _session_cache_payload("a_all", all_profiles=True)
    assert built == []
    assert "session_list_cache_rate_floor_hold" in diag.stages

    # One profile's fingerprint becomes unreadable: the hold is not authorized.
    profile_fingerprint["value"] = models._CONTENT_FINGERPRINT_UNREADABLE
    rebuilt = routes._get_cached_session_list_payload(key=key, builder=builder)
    assert rebuilt == _session_cache_payload("fresh-1", all_profiles=True)
    assert built == [1]


def test_session_list_cache_rate_floor_declines_an_empty_profile_set(monkeypatch):
    """An all-profiles key with NO profile fingerprint evidence may not hold.

    ``_all_profiles_cli_contexts`` degrades to an empty context list when it
    cannot resolve a single profile home (its ``api.profiles`` import fails) —
    the same "degrade instead of raise" shape as the fingerprint reader. The
    resulting empty all-profiles stamp carries zero per-profile fingerprints, so
    it is UNKNOWN evidence, not proof that no participating profile committed
    rows. Accepting it would let the floor suppress the file-stat reconciliation
    of an external writer for the whole hold, which is the #8694 failure mode
    reached through a different degradation path.
    """
    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=True,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    stat = {"generation": 0}
    profiles: dict[str, tuple] = {"value": (((1, 1), (1, 2), (7, 12)),)}

    def _changing_stat_stamp(_key):
        stat["generation"] += 1
        generation = stat["generation"]
        return (
            (generation, 1),
            (generation, 2),
            (generation, 3),
            (generation, 4),
            (generation, 5),
            (40, 91),
            profiles["value"],
            0,
        )

    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", _changing_stat_stamp)
    routes._session_list_cache_set(key, _session_cache_payload("a_all", all_profiles=True))

    built = []

    def builder():
        built.append(1)
        return _session_cache_payload(f"fresh-{len(built)}", all_profiles=True)

    # Control: one participating profile with a READ fingerprint still holds.
    diag = _StageRecorder()
    payload = routes._get_cached_session_list_payload(key=key, builder=builder, diag=diag)
    assert payload == _session_cache_payload("a_all", all_profiles=True)
    assert built == []
    assert "session_list_cache_rate_floor_hold" in diag.stages

    # Both stamps are written and read inside the enumeration-failure window, so
    # only the unrelated stat components move: an empty profile set on BOTH
    # sides still compares equal as row content.
    profiles["value"] = ()
    routes._session_list_cache_clear()
    routes._session_list_cache_set(key, _session_cache_payload("a_all", all_profiles=True))
    rebuilt = routes._get_cached_session_list_payload(key=key, builder=builder)
    assert rebuilt == _session_cache_payload("fresh-1", all_profiles=True)
    assert built == [1], "an unenumerated profile set must never authorize the hold"


def _make_fingerprint_db(path, rows: int) -> None:
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, title TEXT)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
    for index in range(rows):
        conn.execute("INSERT INTO sessions (title) VALUES (?)", (f"s{index}",))
        conn.execute("INSERT INTO messages (body) VALUES (?)", (f"m{index}",))
    conn.commit()
    conn.close()


def test_session_list_cache_rate_floor_declines_a_locked_participating_profile(
    monkeypatch, tmp_path
):
    """An unreadable PARTICIPATING PROFILE fingerprint must not authorize the hold.

    The all-profiles stamp carries one content fingerprint per participating
    profile, read through the real ``_sqlite_content_fingerprint``. A real
    rollback-journal ``EXCLUSIVE`` lock on a profile's state.db makes that read
    degrade to the unreadable sentinel while the active profile's db stays
    readable, so two stamps can differ only by an unrelated stat component while
    the profile fingerprint is UNKNOWN on both sides: without the readability
    guard the row-content tuples compare equal and the ten-second floor holds the
    entry. (#8694)
    """
    import os
    import sqlite3
    from pathlib import Path

    import api.route_session_list_cache as cache_module
    from api import models

    active_db = tmp_path / "active" / "state.db"
    profile_db = tmp_path / "bravo" / "state.db"
    active_db.parent.mkdir()
    profile_db.parent.mkdir()
    _make_fingerprint_db(active_db, 3)
    _make_fingerprint_db(profile_db, 3)

    monkeypatch.setattr(routes, "_active_state_db_path", lambda: str(active_db))
    monkeypatch.setattr(
        models, "_all_profiles_cli_contexts", lambda: ([("home", profile_db, "bravo")], None)
    )

    real_fingerprint = models._sqlite_content_fingerprint
    schedule = {"locked": False}
    produced_under_lock = []

    def _fingerprint(path):
        """Real producer; a real writer lock is held around the PROFILE read only.

        The lock is a scheduling device — the production value is returned
        unchanged, so readability is judged on what production returned.
        """
        if path is not None and Path(path) == profile_db and schedule["locked"]:
            writer = sqlite3.connect(profile_db, isolation_level=None)
            try:
                writer.execute("PRAGMA journal_mode=DELETE")
                writer.execute("BEGIN EXCLUSIVE")
                value = real_fingerprint(path)
            finally:
                writer.execute("ROLLBACK")
                writer.close()
            produced_under_lock.append(value)
            return value
        return real_fingerprint(path)

    monkeypatch.setattr(models, "_sqlite_content_fingerprint", _fingerprint)

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=True,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    built = []

    def builder():
        built.append(1)
        return _session_cache_payload(f"fresh-{len(built)}", all_profiles=True)

    # Control: with every participating fingerprint READ, moving an active-db
    # stat alone still holds the entry — the floor is active, not disabled.
    routes._session_list_cache_set(key, _session_cache_payload("a_all", all_profiles=True))
    os.utime(active_db, ns=(1_000_000_000, 1_000_000_000))
    diag = _StageRecorder()
    payload = routes._get_cached_session_list_payload(key=key, builder=builder, diag=diag)
    assert payload == _session_cache_payload("a_all", all_profiles=True)
    assert built == []
    assert "session_list_cache_rate_floor_hold" in diag.stages

    # Same shape, but the participating profile's fingerprint is UNKNOWN on both
    # the cached and the current stamp, and the only stamp movement is the
    # unrelated active-db stat.
    routes._session_list_cache_clear()
    schedule["locked"] = True
    routes._session_list_cache_set(key, _session_cache_payload("a_all", all_profiles=True))
    assert produced_under_lock, "the profile fingerprint read must have run under the lock"
    assert all(
        models._content_fingerprint_readable(value) is False
        for value in produced_under_lock
    ), f"a locked profile read must be unreadable, got {produced_under_lock!r}"

    os.utime(active_db, ns=(2_000_000_000, 2_000_000_000))
    current_stamp = cache_module._session_list_cache_resolved_source_stamp(key)
    assert models._content_fingerprint_readable(current_stamp[5]) is True, (
        "the active profile's fingerprint must stay readable, otherwise this "
        "test would pass for the wrong reason"
    )
    assert models._content_fingerprint_readable(current_stamp[6][0][2]) is False

    rebuilt = routes._get_cached_session_list_payload(key=key, builder=builder)
    assert rebuilt == _session_cache_payload("fresh-1", all_profiles=True)
    assert built == [1], "an unreadable participating profile must never authorize the hold"


def test_session_list_cache_owner_returns_stale_and_rebuilds_in_background(monkeypatch):
    routes._session_list_cache_clear()
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    routes._session_list_cache_set(key, _session_cache_payload("stale"))
    with routes._SESSIONS_CACHE_LOCK:
        ts, stamp, payload = routes._SESSIONS_CACHE[key]
        routes._SESSIONS_CACHE[key] = (
            ts - routes._SESSIONS_CACHE_TTL_SECONDS - 1.0,
            stamp,
            payload,
        )
    started = threading.Event()
    release = threading.Event()
    diag = _StageRecorder()

    def builder():
        started.set()
        release.wait()
        return _session_cache_payload("fresh")

    try:
        result = routes._get_cached_session_list_payload(key=key, builder=builder, diag=diag)
        assert result == _session_cache_payload("stale")
        assert started.wait(1.0), "stale owner should kick off a background rebuild"
        assert "session_list_cache_stale_background_rebuild" in diag.stages
        # While background rebuild is still blocked, stale is still returned.
        cached, fresh = routes._session_list_cache_get(key, allow_stale=True)
        assert cached == _session_cache_payload("stale")
        assert fresh is False
    finally:
        release.set()

    # The background owner should eventually populate fresh cache.
    deadline = threading.Event()
    for _ in range(20):
        cached, fresh = routes._session_list_cache_get(key, allow_stale=True)
        if cached == _session_cache_payload("fresh"):
            break
        deadline.wait(0.05)
    assert cached == _session_cache_payload("fresh")


def test_session_list_cache_stale_background_rebuild_failure_releases_owner(monkeypatch):
    routes._session_list_cache_clear()
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda _key: ("stable",))

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    routes._session_list_cache_set(key, _session_cache_payload("stale"))
    with routes._SESSIONS_CACHE_LOCK:
        ts, stamp, payload = routes._SESSIONS_CACHE[key]
        routes._SESSIONS_CACHE[key] = (
            ts - routes._SESSIONS_CACHE_TTL_SECONDS - 1.0,
            stamp,
            payload,
        )
    started = threading.Event()
    logged = []

    def record_exception(message):
        logged.append(message)

    monkeypatch.setattr(routes.logger, "exception", record_exception)

    def failing_builder():
        started.set()
        raise RuntimeError("boom")

    result = routes._get_cached_session_list_payload(key=key, builder=failing_builder)

    assert result == _session_cache_payload("stale")
    assert started.wait(1.0), "background rebuild should have attempted the builder"
    for _ in range(20):
        with routes._SESSIONS_CACHE_LOCK:
            inflight_empty = key not in routes._SESSIONS_CACHE_INFLIGHT
        if inflight_empty:
            break
        threading.Event().wait(0.05)
    assert inflight_empty, "failed background rebuild must release the singleflight owner"
    assert logged == ["session list stale-cache background rebuild failed"]
    cached, fresh = routes._session_list_cache_get(key, allow_stale=True)
    assert cached == _session_cache_payload("stale")
    assert fresh is False

    def recovery_builder():
        return _session_cache_payload("fresh")

    # Once the failed owner releases, a later request can retry instead of being
    # pinned behind the dead background rebuild.
    routes._get_cached_session_list_payload(key=key, builder=recovery_builder)
    for _ in range(20):
        cached, _fresh = routes._session_list_cache_get(key, allow_stale=True)
        if cached == _session_cache_payload("fresh"):
            break
        threading.Event().wait(0.05)
    assert cached == _session_cache_payload("fresh")


def test_session_list_cache_invalidated_on_session_list_publish():
    routes._session_list_cache_clear()

    key_a = routes._session_list_cache_key(
        active_profile="profile-a",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    key_b = routes._session_list_cache_key(
        active_profile="profile-b",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    key_a_all = routes._session_list_cache_key(
        active_profile="profile-a",
        all_profiles=True,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    routes._session_list_cache_set(key_a, _session_cache_payload("a"))
    routes._session_list_cache_set(key_b, _session_cache_payload("b"))
    routes._session_list_cache_set(key_a_all, _session_cache_payload("a_all", all_profiles=True))

    session_events.publish_session_list_changed("session_pin", profile="profile-a")

    assert routes._session_list_cache_get(key_a)[0] is None
    assert routes._session_list_cache_get(key_a_all)[0] is None
    assert routes._session_list_cache_get(key_b)[0] is not None


def test_session_list_cache_rebuild_retries_after_invalidation():
    routes._session_list_cache_clear()

    key = routes._session_list_cache_key(
        active_profile="profile-a",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    calls = []

    def builder():
        calls.append("build")
        if len(calls) == 1:
            routes._session_list_cache_clear("profile-a")
            return _session_cache_payload("stale")
        return _session_cache_payload("fresh")

    payload = routes._get_cached_session_list_payload(key=key, builder=builder)

    assert payload == _session_cache_payload("fresh")
    assert calls == ["build", "build"]


def test_session_list_cache_source_stamp_tracks_state_db_wal(tmp_path, monkeypatch):
    state_db = tmp_path / "state.db"
    state_db.write_text("db", encoding="utf-8")
    state_db_wal = tmp_path / "state.db-wal"
    state_db_wal.write_text("wal-1", encoding="utf-8")
    gateway = tmp_path / "gateway-sessions.json"
    gateway.write_text("{}", encoding="utf-8")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "_index.json").write_text("{}", encoding="utf-8")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(routes, "_active_state_db_path", lambda: str(state_db))
    monkeypatch.setattr(routes, "_gateway_session_metadata_path", lambda: gateway)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SETTINGS_FILE", settings_file)

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    before = routes._session_list_cache_source_stamp(key)
    state_db_wal.write_text("wal-2-more", encoding="utf-8")
    after = routes._session_list_cache_source_stamp(key)

    assert after != before


def test_session_list_cache_source_stamp_tracks_settings_file(tmp_path, monkeypatch):
    state_db = tmp_path / "state.db"
    state_db.write_text("db", encoding="utf-8")
    gateway = tmp_path / "gateway-sessions.json"
    gateway.write_text("{}", encoding="utf-8")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "_index.json").write_text("{}", encoding="utf-8")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"show_cli_sessions": false}', encoding="utf-8")

    monkeypatch.setattr(routes, "_active_state_db_path", lambda: str(state_db))
    monkeypatch.setattr(routes, "_gateway_session_metadata_path", lambda: gateway)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SETTINGS_FILE", settings_file)

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    before = routes._session_list_cache_source_stamp(key)
    settings_file.write_text('{"show_cli_sessions": true}', encoding="utf-8")
    after = routes._session_list_cache_source_stamp(key)

    assert after != before


def test_session_list_cache_source_stamp_tracks_settings_write_version(
    tmp_path,
    monkeypatch,
):
    state_db = tmp_path / "state.db"
    state_db.write_text("db", encoding="utf-8")
    gateway = tmp_path / "gateway-sessions.json"
    gateway.write_text("{}", encoding="utf-8")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "_index.json").write_text("{}", encoding="utf-8")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(routes, "_active_state_db_path", lambda: str(state_db))
    monkeypatch.setattr(routes, "_gateway_session_metadata_path", lambda: gateway)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SETTINGS_FILE", settings_file)

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    before = routes._session_list_cache_source_stamp(key)
    monkeypatch.setattr(config, "_SETTINGS_WRITE_VERSION", config._SETTINGS_WRITE_VERSION + 1)
    after = routes._session_list_cache_source_stamp(key)

    assert after != before


def test_session_list_payload_to_response_overlays_live_stream_runtime(monkeypatch):
    payload = {
        "sessions": [
            {
                "session_id": "streaming-session",
                "title": "Live title",
                "active_stream_id": None,
                "is_streaming": False,
                "has_pending_user_message": False,
            },
            {
                "session_id": "stale-session",
                "title": "Old title",
                "active_stream_id": "stale-stream",
                "is_streaming": True,
                "has_pending_user_message": True,
            },
        ],
        "cli_count": 0,
        "all_profiles": False,
        "active_profile": "default",
        "other_profile_count": 0,
    }

    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {"live-stream"})
    with routes.LOCK:
        original = dict(routes.SESSIONS)
        routes.SESSIONS.clear()
        routes.SESSIONS["streaming-session"] = SimpleNamespace(
            active_stream_id="live-stream",
            pending_user_message="queued prompt",
        )
        routes.SESSIONS["stale-session"] = SimpleNamespace(
            active_stream_id=None,
            pending_user_message=None,
        )
    try:
        response = routes._session_list_payload_to_response(payload)
    finally:
        with routes.LOCK:
            routes.SESSIONS.clear()
            routes.SESSIONS.update(original)

    by_id = {row["session_id"]: row for row in response["sessions"]}
    assert by_id["streaming-session"]["active_stream_id"] == "live-stream"
    assert by_id["streaming-session"]["is_streaming"] is True
    assert by_id["streaming-session"]["has_pending_user_message"] is True
    assert by_id["stale-session"]["active_stream_id"] is None
    assert by_id["stale-session"]["is_streaming"] is False
    assert by_id["stale-session"]["has_pending_user_message"] is False


def _build_stamp_env(tmp_path, monkeypatch):
    """Wire a self-contained source-stamp environment and return its key."""
    state_db = tmp_path / "state.db"
    state_db.write_text("db", encoding="utf-8")
    state_db_wal = tmp_path / "state.db-wal"
    state_db_wal.write_text("wal-1", encoding="utf-8")
    gateway = tmp_path / "gateway-sessions.json"
    gateway.write_text("{}", encoding="utf-8")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "_index.json").write_text("{}", encoding="utf-8")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(routes, "_active_state_db_path", lambda: str(state_db))
    monkeypatch.setattr(routes, "_gateway_session_metadata_path", lambda: gateway)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SETTINGS_FILE", settings_file)
    # Make the content fingerprint deterministic and unaffected by the dummy
    # text-file state.db (a real sqlite connect would just return None here).
    fingerprint = {"value": (1, 1)}
    monkeypatch.setattr(
        routes,
        "_session_list_cache_state_db_fingerprint",
        lambda _p: fingerprint["value"],
    )

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    return key, state_db_wal, settings_file, fingerprint


def test_source_stamp_freezes_during_streaming_message_writes(tmp_path, monkeypatch):
    """#4672: per-token state.db churn must not bust the cache mid-stream.

    With an active stream the volatile state.db-derived components are collapsed
    to a stream-set marker, so advancing the WAL stat AND the content
    fingerprint (the per-message-write signals) leaves the stamp unchanged.
    Before the fix each of these advanced the stamp and forced a rebuild on
    every poll.
    """
    key, state_db_wal, _settings_file, fingerprint = _build_stamp_env(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {"turn-1"})

    before = routes._session_list_cache_source_stamp(key)
    # Simulate the writes an active chat turn makes to state.db: WAL grows and
    # the messages-table fingerprint advances.
    state_db_wal.write_text("wal-2-grew-a-lot", encoding="utf-8")
    fingerprint["value"] = (2, 99)
    after = routes._session_list_cache_source_stamp(key)

    assert after == before


def test_source_stamp_changes_when_stream_set_transitions(tmp_path, monkeypatch):
    """The hold-down marker tracks the active-stream SET, so a turn starting or
    finishing re-validates the cache and the final title/count is picked up."""
    key, _wal, _settings, _fp = _build_stamp_env(tmp_path, monkeypatch)

    streams = {"value": set()}
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set(streams["value"]))

    idle = routes._session_list_cache_source_stamp(key)
    streams["value"] = {"turn-1"}
    streaming = routes._session_list_cache_source_stamp(key)
    streams["value"] = {"turn-1", "turn-2"}
    two_streams = routes._session_list_cache_source_stamp(key)
    streams["value"] = set()
    idle_again = routes._session_list_cache_source_stamp(key)

    assert idle != streaming
    assert streaming != two_streams
    # Returning to idle re-engages the live state.db stamp path.
    assert idle_again != streaming


def test_source_stamp_tracks_settings_even_while_streaming(tmp_path, monkeypatch):
    """Settings-file / sidebar-toggle changes must still invalidate the cache
    during streaming so user-initiated changes are never held stale."""
    key, _wal, settings_file, _fp = _build_stamp_env(tmp_path, monkeypatch)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {"turn-1"})

    before = routes._session_list_cache_source_stamp(key)
    settings_file.write_text('{"show_cli_sessions": true}', encoding="utf-8")
    after = routes._session_list_cache_source_stamp(key)

    assert after != before


def test_source_stamp_still_tracks_wal_when_idle(tmp_path, monkeypatch):
    """Regression guard: with NO active stream the stamp must still advance on a
    state.db/WAL write (the original commit-reliable behavior is preserved)."""
    key, state_db_wal, _settings, _fp = _build_stamp_env(tmp_path, monkeypatch)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    before = routes._session_list_cache_source_stamp(key)
    state_db_wal.write_text("wal-2-more", encoding="utf-8")
    after = routes._session_list_cache_source_stamp(key)

    assert after != before


def _streaming_ttl_key():
    return routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )


def _age_cache_entry(key, seconds):
    """Backdate the cache entry's timestamp by `seconds` (keep stamp+payload)."""
    with routes._SESSIONS_CACHE_LOCK:
        ts, stamp, payload = routes._SESSIONS_CACHE[key]
        routes._SESSIONS_CACHE[key] = (ts - seconds, stamp, payload)


def test_streaming_widens_cache_freshness_window(monkeypatch):
    """#4808: while a turn streams, an entry older than the idle TTL but younger
    than the streaming TTL must still read FRESH, so the fixed streaming poll
    does not force a rebuild every poll."""
    routes._session_list_cache_clear()
    key = _streaming_ttl_key()
    # Keep the source stamp stable across the get() calls (no structural change).
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda k: ("stable",))

    routes._session_list_cache_set(key, _session_cache_payload("live"))
    # Age it past the idle TTL but keep it under the streaming TTL.
    _age_cache_entry(key, routes._SESSIONS_CACHE_TTL_SECONDS + 1.0)

    # Idle (no active stream) → stale → miss.
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    payload_idle, fresh_idle = routes._session_list_cache_get(key)
    assert payload_idle is None and fresh_idle is False

    # Re-seed + re-age, now WITH an active stream → fresh (held).
    routes._session_list_cache_set(key, _session_cache_payload("live"))
    _age_cache_entry(key, routes._SESSIONS_CACHE_TTL_SECONDS + 1.0)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {"turn-1"})
    payload_stream, fresh_stream = routes._session_list_cache_get(key)
    assert fresh_stream is True
    assert payload_stream == _session_cache_payload("live")


def test_streaming_window_still_evicts_past_streaming_ttl(monkeypatch):
    """Even while streaming, an entry older than the streaming TTL is evicted —
    the hold-down is bounded, not indefinite."""
    routes._session_list_cache_clear()
    key = _streaming_ttl_key()
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda k: ("stable",))
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {"turn-1"})

    routes._session_list_cache_set(key, _session_cache_payload("old"))
    _age_cache_entry(key, routes._SESSIONS_CACHE_STREAMING_TTL_SECONDS + 1.0)
    payload, fresh = routes._session_list_cache_get(key)
    assert payload is None and fresh is False


def test_streaming_window_does_not_extend_idle_ttl(monkeypatch):
    """Regression guard: with NO active stream the idle 2.5s TTL is unchanged —
    an entry aged just past it must read stale (byte-for-byte idle behavior)."""
    routes._session_list_cache_clear()
    key = _streaming_ttl_key()
    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", lambda k: ("stable",))
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    routes._session_list_cache_set(key, _session_cache_payload("idle"))
    _age_cache_entry(key, routes._SESSIONS_CACHE_TTL_SECONDS + 0.5)
    payload, fresh = routes._session_list_cache_get(key)
    assert payload is None and fresh is False
