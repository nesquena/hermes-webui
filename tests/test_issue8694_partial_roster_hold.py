"""#8694 maintainer finish: a PARTIAL profile roster must never certify a sidebar.

``_all_profiles_cli_contexts`` is best-effort: when the named-profile directory
enumeration fails, or one profile's home fails to resolve, it returns the
contexts it managed to collect. The all-profiles cache stamp is built from those
contexts, so a profile the roster could not see can commit (a Desktop row
deleted from its state.db) without moving anything the stamp compares. The
roster producer therefore marks an incomplete roster in its cache key and in
the all-profiles stamp.

The first test is the Gate's production-composed schedule (real SQLite files,
the real roster producer, the strict owning-profile query, the Desktop orphan
pruner and the cache consumer). It failed on #8694's head `63d3b5e5bbbe`
(the deleted row survived three reads, one build).

#8986 simplify: the server-side rebuild-rate floor ("hold") is gone, so every
source-stamp move rebuilds on the next request as on master; the tests that only
pinned the hold's gates were removed and the rest assert the rebuild.
"""

import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.models as models

_REAL_CONTEXTS = models._all_profiles_cli_contexts


@pytest.fixture
def store(tmp_path, monkeypatch):
    import api.routes as r
    import api.models as m

    db = tmp_path / "state.db"
    with sqlite3.connect(db) as c:
        c.executescript(
            "CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT);"
            "CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT);"
            "INSERT INTO sessions VALUES('a','original'),('z','last');"
            "INSERT INTO messages VALUES(1,'a'),(2,'z');"
        )
    folder = tmp_path / "sessions"
    folder.mkdir()
    index = folder / "_index.json"
    settings = tmp_path / "settings.json"
    gateway = tmp_path / "gateway.json"
    for p in [index, settings, gateway]:
        p.write_text("{}")
    monkeypatch.setattr(r, "_active_state_db_path", lambda: str(db))
    monkeypatch.setattr(r, "_gateway_session_metadata_path", lambda: gateway)
    monkeypatch.setattr(r, "SESSION_DIR", folder)
    monkeypatch.setattr(r, "SETTINGS_FILE", settings)
    monkeypatch.setattr(r, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(m, "_all_profiles_cli_contexts", lambda: ([(tmp_path, db, None)], ()))
    r._session_list_cache_clear()
    yield SimpleNamespace(r=r, db=db, gateway=gateway)
    r._session_list_cache_clear()


@pytest.fixture
def two_profiles(store, monkeypatch):
    """A real default + ``bravo`` roster under the store's home, via the real producer."""
    import api.profiles as profiles

    home = store.db.parent
    roster = home / "profiles"
    bravo = roster / "bravo"
    bravo.mkdir(parents=True)
    foreign = bravo / "state.db"
    shutil.copyfile(store.db, foreign)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(profiles, "_root_profile_name_cache", set())
    monkeypatch.setattr(profiles, "_root_profile_name_cache_loaded", False)
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [
        {"name": "default", "is_default": True}, {"name": "bravo", "is_default": False}])
    monkeypatch.setattr(models, "_all_profiles_cli_contexts", _REAL_CONTEXTS)
    contexts, _key = models._all_profiles_cli_contexts()
    assert {ctx[2] for ctx in contexts} == {"default", "bravo"}
    return SimpleNamespace(home=home, roster=roster, bravo=bravo, foreign=foreign, profiles=profiles)


def _fail_roster_enumeration(monkeypatch, roster):
    original_iterdir = Path.iterdir
    failures = []

    def iterdir(path):
        if path == roster:
            failures.append(str(path))
            raise PermissionError("controlled named-profile directory enumeration failure")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    return failures


def _fail_bravo_home_resolution(monkeypatch, profiles):
    """Fail bravo's home resolution INSIDE the roster producer only.

    The strict owner query resolves homes through the same resolver, and it must
    keep working: the point is that bravo is fine and only the roster missed it.
    """
    original = profiles.get_hermes_home_for_profile
    inside_roster = []

    def resolve(name):
        if name == "bravo" and inside_roster:
            raise OSError("controlled profile-home resolution failure")
        return original(name)

    def roster():
        inside_roster.append(1)
        try:
            return _REAL_CONTEXTS()
        finally:
            inside_roster.pop()

    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", resolve)
    monkeypatch.setattr(models, "_all_profiles_cli_contexts", roster)


@pytest.mark.parametrize("roster_failure", [None, "enumeration", "home_resolution"])
def test_partial_roster_must_not_certify_a_foreign_desktop_row(store, two_profiles, monkeypatch, roster_failure):
    r = store.r
    if roster_failure == "enumeration":
        assert _fail_roster_enumeration(monkeypatch, two_profiles.roster) == []
    elif roster_failure == "home_resolution":
        _fail_bravo_home_resolution(monkeypatch, two_profiles.profiles)
    # The behaviour under test only: the roster producer's completeness signal
    # is pinned separately below, so this schedule discriminates on the
    # contributor head too (where that signal does not exist yet).
    contexts, _roster_key = models._all_profiles_cli_contexts()
    assert {ctx[2] for ctx in contexts} == ({"default", "bravo"} if roster_failure is None else {"default"})
    # Bravo's own database is still perfectly readable: only the roster missed it.
    assert r.agent_session_rows_existing_strict(["a"], profile="bravo") == frozenset({"a"})
    row = {"session_id": "a", "title": "Opened Desktop conversation", "source": "desktop",
           "session_source": "desktop", "profile": "bravo", "message_count": 1}
    assert r._is_desktop_sidecar_row(row) and not r._session_source_is_webui(row)
    key = r._session_list_cache_key(active_profile="default", all_profiles=True,
        show_cli_sessions=True, show_previous_messaging_sessions=False, show_cron_sessions=False)
    builds = []

    def build():
        builds.append(1)
        kept = r._prune_orphaned_desktop_sidecar_sessions([dict(row)])
        return {"sessions": kept, "cli_count": 0, "active_profile": "default",
                "all_profiles": True, "other_profile_count": len(kept)}

    def get():
        return r._get_cached_session_list_payload(key=key, builder=build)

    assert [item["session_id"] for item in get()["sessions"]] == ["a"]
    assert [item["session_id"] for item in get()["sessions"]] == ["a"] and len(builds) == 1, (
        "prime must produce a real cache hit"
    )
    before = r._session_list_cache_source_stamp(key)
    foreign_before = models._sqlite_content_fingerprint(two_profiles.foreign)
    with sqlite3.connect(two_profiles.foreign) as c:
        c.execute("DELETE FROM messages WHERE session_id='a'")
        c.execute("DELETE FROM sessions WHERE id='a'")
    # Not the maximum rowid: the append fingerprint cannot see this delete.
    assert models._sqlite_content_fingerprint(two_profiles.foreign) == foreign_before
    assert r.agent_session_rows_existing_strict(["a"], profile="bravo") == frozenset()
    # The same ID survives in default, so an accidental fallback to the active
    # profile cannot satisfy the oracle below.
    assert r.agent_session_rows_existing_strict(["a"], profile="default") == frozenset({"a"})
    store.gateway.write_text(json.dumps({"external_metadata_tick": 1}))
    after = r._session_list_cache_source_stamp(key)
    assert before != after and before[:2] == after[:2] and before[5] == after[5]
    # With a partial roster the all-profiles stamp cannot see bravo's commit.
    assert (before[6] == after[6]) is (roster_failure is not None)
    observed = [[item["session_id"] for item in get()["sessions"]] for _ in range(3)]
    assert observed == [[], [], []], "a deleted foreign Desktop row must revalidate despite a partial roster"


def _metadata_only_churn_builds(store, two_profiles, monkeypatch, *, roster_failure):
    """Prime an all-profiles entry, rewrite ONLY gateway metadata, read again."""
    r = store.r
    if roster_failure == "enumeration":
        _fail_roster_enumeration(monkeypatch, two_profiles.roster)
    key = r._session_list_cache_key(active_profile="default", all_profiles=True,
        show_cli_sessions=True, show_previous_messaging_sessions=False, show_cron_sessions=False)
    builds = []

    def build():
        builds.append(1)
        return {"sessions": [{"session_id": "a"}], "cli_count": 0, "active_profile": "default",
                "all_profiles": True, "other_profile_count": 0}

    r._get_cached_session_list_payload(key=key, builder=build)
    assert len(builds) == 1
    store.gateway.write_text(json.dumps({"external_metadata_tick": 2}))
    probe_payload, status = r._session_list_cache_probe(key, allow_stale=True)
    r._get_cached_session_list_payload(key=key, builder=build)
    return status, len(builds)


@pytest.mark.parametrize("roster_failure", [None, "enumeration"])
def test_metadata_only_churn_rebuilds_on_the_next_read(store, two_profiles, monkeypatch, roster_failure):
    """Gateway-metadata churn alone moves the stamp, so the next read rebuilds (master's behaviour)."""
    status, builds = _metadata_only_churn_builds(store, two_profiles, monkeypatch, roster_failure=roster_failure)
    assert status == store.r._SESSION_LIST_CACHE_STALE_SOURCE
    assert builds == 2


def test_explicit_invalidation_still_clears_an_all_profiles_entry(store, two_profiles):
    r = store.r
    key = r._session_list_cache_key(active_profile="default", all_profiles=True,
        show_cli_sessions=True, show_previous_messaging_sessions=False, show_cron_sessions=False)
    builds = []

    def build():
        builds.append(1)
        return {"sessions": [], "cli_count": 0, "active_profile": "default",
                "all_profiles": True, "other_profile_count": 0}

    r._get_cached_session_list_payload(key=key, builder=build)
    r._session_list_cache_clear()
    r._get_cached_session_list_payload(key=key, builder=build)
    assert len(builds) == 2


# --- the roster producer's completeness signal -------------------------------


def test_roster_key_marks_a_failed_directory_enumeration(store, two_profiles, monkeypatch):
    _fail_roster_enumeration(monkeypatch, two_profiles.roster)
    contexts, key = models._all_profiles_cli_contexts()
    assert [ctx[2] for ctx in contexts] == ["default"]
    assert models.all_profiles_roster_incomplete(key)


def test_roster_key_marks_a_failed_home_resolution(store, two_profiles, monkeypatch):
    _fail_bravo_home_resolution(monkeypatch, two_profiles.profiles)
    contexts, key = models._all_profiles_cli_contexts()
    assert [ctx[2] for ctx in contexts] == ["default"]
    assert models.all_profiles_roster_incomplete(key)


def test_roster_key_marks_a_failed_active_profile_lookup(store, two_profiles, monkeypatch):
    def boom():
        raise RuntimeError("controlled active-profile lookup failure")

    monkeypatch.setattr(two_profiles.profiles, "get_active_profile_name", boom)
    contexts, key = models._all_profiles_cli_contexts()
    assert {ctx[2] for ctx in contexts} == {"default", "bravo"}
    assert models.all_profiles_roster_incomplete(key)


def test_a_containment_refusal_is_an_exclusion_not_lost_evidence(store, two_profiles, monkeypatch):
    original = two_profiles.profiles.get_hermes_home_for_profile

    def resolve(name):
        if name == "bravo":
            raise ValueError("resolves outside the profiles root")
        return original(name)

    monkeypatch.setattr(two_profiles.profiles, "get_hermes_home_for_profile", resolve)
    contexts, key = models._all_profiles_cli_contexts()
    assert [ctx[2] for ctx in contexts] == ["default"]
    assert not models.all_profiles_roster_incomplete(key)


def test_a_missing_profiles_root_is_a_complete_roster(store, two_profiles):
    shutil.rmtree(two_profiles.roster)
    contexts, key = models._all_profiles_cli_contexts()
    assert [ctx[2] for ctx in contexts] == ["default"]
    assert not models.all_profiles_roster_incomplete(key)


def test_an_unimportable_profiles_module_is_an_incomplete_roster(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "api.profiles":
            raise ImportError("controlled")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    contexts, key = _REAL_CONTEXTS()
    assert contexts == []
    assert models.all_profiles_roster_incomplete(key)


def test_partial_and_complete_roster_keys_differ_for_the_cli_cache(store, two_profiles, monkeypatch):
    # A failed active-profile lookup leaves the context list itself unchanged,
    # so only the marker can tell the two keys apart (senior review finding 6).
    complete_contexts, complete_key = models._all_profiles_cli_contexts()

    def boom():
        raise RuntimeError("controlled active-profile lookup failure")

    monkeypatch.setattr(two_profiles.profiles, "get_active_profile_name", boom)
    partial_contexts, partial_key = models._all_profiles_cli_contexts()
    assert partial_contexts == complete_contexts
    assert complete_key != partial_key
    assert (
        models._all_profiles_cli_cache_key(complete_contexts, complete_key, None, True)
        != models._all_profiles_cli_cache_key(partial_contexts, partial_key, None, True)
    )


def test_a_partial_roster_read_by_the_builder_is_rebuilt_on_the_next_source_move(store, two_profiles, monkeypatch):
    """The stamp's roster read was complete, but the BUILDER's own read was not.

    Senior review finding 2: the payload then misses bravo's rows while the stamp
    says every profile was seen. The next source move must rebuild it.
    """
    r = store.r
    key = r._session_list_cache_key(active_profile="default", all_profiles=True,
        show_cli_sessions=True, show_previous_messaging_sessions=False, show_cron_sessions=False)
    original_iterdir = Path.iterdir
    fail_next = []

    def iterdir(path):
        if path == two_profiles.roster and fail_next:
            fail_next.pop()
            raise PermissionError("controlled one-time enumeration failure inside the builder")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    builds = []

    def build():
        builds.append(1)
        if len(builds) == 1:
            fail_next.append(1)
        contexts, _ = models._all_profiles_cli_contexts()
        return {"sessions": [{"session_id": ctx[2]} for ctx in contexts], "cli_count": 0,
                "active_profile": "default", "all_profiles": True, "other_profile_count": 0}

    first = r._get_cached_session_list_payload(key=key, builder=build)
    assert [row["session_id"] for row in first["sessions"]] == ["default"], "the builder's read was partial"
    store.gateway.write_text(json.dumps({"external_metadata_tick": 3}))
    _payload, status = r._session_list_cache_probe(key, allow_stale=True)
    assert status == r._SESSION_LIST_CACHE_STALE_SOURCE
    second = r._get_cached_session_list_payload(key=key, builder=build)
    assert sorted(row["session_id"] for row in second["sessions"]) == ["bravo", "default"]
    assert len(builds) == 2


def test_creating_a_profiles_state_db_revalidates(store, two_profiles):
    r = store.r
    two_profiles.foreign.unlink()
    key = r._session_list_cache_key(active_profile="default", all_profiles=True,
        show_cli_sessions=True, show_previous_messaging_sessions=False, show_cron_sessions=False)
    builds = []

    def build():
        builds.append(1)
        return {"sessions": [], "cli_count": 0, "active_profile": "default",
                "all_profiles": True, "other_profile_count": 0}

    r._get_cached_session_list_payload(key=key, builder=build)
    shutil.copyfile(store.db, two_profiles.foreign)
    store.gateway.write_text(json.dumps({"external_metadata_tick": 4}))
    _payload, status = r._session_list_cache_probe(key, allow_stale=True)
    assert status == r._SESSION_LIST_CACHE_STALE_SOURCE


def test_a_partial_roster_stamp_never_equals_the_complete_one(store, two_profiles, monkeypatch):
    """A failed active-profile lookup leaves the context list unchanged; only the marker moves the stamp."""
    r = store.r
    key = r._session_list_cache_key(active_profile="default", all_profiles=True,
        show_cli_sessions=True, show_previous_messaging_sessions=False, show_cron_sessions=False)
    complete = r._session_list_cache_source_stamp(key)

    def boom():
        raise RuntimeError("controlled active-profile lookup failure")

    monkeypatch.setattr(two_profiles.profiles, "get_active_profile_name", boom)
    partial = r._session_list_cache_source_stamp(key)
    assert partial[6][-1] == models.ALL_PROFILES_ROSTER_INCOMPLETE
    assert partial[6][:-1] == complete[6], "the same profiles were seen"
    assert partial != complete


def test_a_failed_row_load_inside_the_builder_is_rebuilt_on_the_next_source_move(store, monkeypatch):
    """Senior review r2 finding 2: the same problem as the builder's roster read, for ROWS.

    The stamp's fingerprint read is fine; then the builder's own state.db read
    fails ("database is locked") and ``get_cli_sessions`` serves last-known-good
    rows that predate a Desktop commit. The next source move (gateway-metadata
    churn) must rebuild and show the committed row (master's behaviour).
    """
    r = store.r
    key = r._session_list_cache_key(active_profile="default", all_profiles=False,
        show_cli_sessions=True, show_previous_messaging_sessions=False, show_cron_sessions=False)
    monkeypatch.setattr(models, "_cli_sessions_streaming_freeze_marker", lambda: None)

    def resolve(source_filter=None, include_claude_code=True, **_kw):
        cache_key = (str(store.db.parent), "", str(store.db), str(source_filter or ""),
                     models._sqlite_file_stat_cache_key(store.db), bool(include_claude_code), "", "", "")
        return store.db.parent, store.db, None, cache_key

    monkeypatch.setattr(models, "_resolve_cli_sessions_context", resolve)
    fail = []

    def load(home, db, profile, **_kw):
        if fail:
            fail.pop()
            raise sqlite3.OperationalError("database is locked")
        with sqlite3.connect(db) as c:
            return [{"session_id": row[0]} for row in c.execute("SELECT id FROM sessions ORDER BY id")]

    monkeypatch.setattr(models, "_load_cli_sessions_uncached", load)
    if hasattr(models, "clear_cli_sessions_cache"):
        models.clear_cli_sessions_cache()
    builds = []

    def build():
        builds.append(1)
        rows = models.get_cli_sessions()
        return {"sessions": [{"session_id": s["session_id"]} for s in rows], "cli_count": len(rows),
                "active_profile": "default", "all_profiles": False, "other_profile_count": 0}

    def ids(payload):
        return sorted(row["session_id"] for row in payload["sessions"])

    assert ids(r._get_cached_session_list_payload(key=key, builder=build)) == ["a", "z"]
    with sqlite3.connect(store.db) as c:
        c.execute("INSERT INTO sessions VALUES('n','committed by Desktop')")
        c.execute("INSERT INTO messages VALUES(3,'n')")
    fail.append(1)  # only the builder's read hits the lock
    stale = r._get_cached_session_list_payload(key=key, builder=build)
    assert ids(stale) == ["a", "z"], "the builder served last-known-good rows"
    store.gateway.write_text(json.dumps({"external_metadata_tick": 51}))
    _payload, status = r._session_list_cache_probe(key, allow_stale=True)
    assert status == r._SESSION_LIST_CACHE_STALE_SOURCE
    assert ids(r._get_cached_session_list_payload(key=key, builder=build)) == ["a", "n", "z"]
