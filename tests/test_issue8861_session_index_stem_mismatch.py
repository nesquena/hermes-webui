"""#8861: a sidecar whose body names another session must not steal its sidebar row.

A session file whose body names a *different* session (a manual copy, or a backup
restored under another name: ``copy-8861.json`` with ``"session_id": "victim-8861"``)
used to wipe the owner's row from the sidebar whenever ``_index.json`` existed:

- ``all_sessions()``'s missing-file recovery enumerates persisted *file stems*
  (``_persisted_session_ids_snapshot()``) and treats every stem that is not an
  indexed id as a row to recover. ``copy-8861`` is never an indexed id, so on
  every listing it looked "missing".
- The recovery loader (``Session.load_metadata_only(stem)``) reads the file's
  body, whose ``session_id`` is ``victim-8861``; the recovered row was then
  keyed by that body id and handed to ``_write_session_index()``, whose fast
  path replaces in-place any row whose ``session_id`` matches. The owner's row
  (10 messages, profile ``work``) was overwritten by the copy's metadata
  (2 messages, profile ``default``) — on every listing, until a full rebuild.

The same re-keying reached the index through the ``last_message_at`` backfill
path, which replaces ``index[i]`` with the loaded object's ``compact()``.

Fix shape (fail closed): recovery/backfill and targeted index updates reject
foreign-body stubs. Full rebuilds check the canonical or supported legacy file
name before deduplication, so a larger copy cannot claim the owner's row.
These guards do not repair, rename, or overwrite any sidecar.

Not a data-loss path: the owner's file is never touched (#8859 covers writes).
"""

import json
from collections import OrderedDict
from pathlib import Path

import pytest

VICTIM = "victim-8861"
COPY = "copy-8861"
KEEP = "keep-8861"


@pytest.fixture
def store(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes

    sessions = OrderedDict()
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", directory)
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(routes, "SESSIONS", sessions)
    index = directory / "_index.json"
    for module in (config, models, routes):
        if hasattr(module, "SESSION_INDEX_FILE"):
            monkeypatch.setattr(module, "SESSION_INDEX_FILE", index)
    routes._session_list_cache_clear()
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    yield directory
    routes._session_list_cache_clear()


def _turns(session, turns, base=100):
    for i in range(turns):
        session.messages += [
            {"role": "user", "content": f"q{i}", "timestamp": base + 2 * i},
            {"role": "assistant", "content": f"a{i}", "timestamp": base + 2 * i + 1},
        ]


def _victim(directory):
    """A real 10-message session owned by `work`."""
    from api import models

    session = models.Session(session_id=VICTIM, title="Victim", profile="work")
    _turns(session, 5)
    session.save(touch_updated_at=False)
    models.SESSIONS.clear()
    return directory / f"{VICTIM}.json"


def _keep(directory):
    """A self-consistent 6-message session that must never be collaterally damage."""
    from api import models

    session = models.Session(session_id=KEEP, title="Keep", profile="work")
    _turns(session, 3, base=300)
    session.save(touch_updated_at=False)
    models.SESSIONS.clear()
    return directory / f"{KEEP}.json"


def _copy(directory, *, message_count=None):
    """A sidecar named `copy-8861` whose body names the owner.

    Default (``message_count=None``) reproduces the original 2-message #8861
    shape byte-for-byte. Pass an int to synthesize that many messages — e.g.
    20, more than the owner's 10 — which drives the full-rebuild dedup that
    prefers the entry with MORE messages and used to let the copy steal the
    owner's row (review 6009588960).
    """
    raw = json.loads((directory / f"{VICTIM}.json").read_text(encoding="utf-8"))
    raw.update(title="Copy", profile="default", session_id=VICTIM)
    if message_count is None:
        raw["messages"] = [
            {"role": "user", "content": "copy q", "timestamp": 500},
            {"role": "assistant", "content": "copy a", "timestamp": 501},
        ]
        raw["message_count"] = 2
    else:
        raw["messages"] = [
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"copy {i}", "timestamp": 500 + i}
            for i in range(message_count)
        ]
        raw["message_count"] = message_count
    path = directory / f"{COPY}.json"
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return path


def _rows_by_id(rows):
    return {r.get("session_id"): r for r in rows}


def _index_rows(store):
    return _rows_by_id(json.loads((store / "_index.json").read_bytes()))


def _visible_to(rows, profile):
    import api.routes as routes

    return [r for r in rows if routes._profiles_match(r.get("profile"), profile)]


# --- the owner's row survives the copy ----------------------------------------------


def test_owner_row_survives_a_foreign_body_copy_on_listing(store):
    from api import models

    _victim(store)
    _copy(store)
    rows = models.all_sessions()
    owner = _rows_by_id(rows).get(VICTIM)
    assert owner is not None, "the owner's row disappeared from the sidebar listing"
    assert owner.get("message_count") == 10, "the copy's metadata replaced the owner's row"
    assert owner.get("profile") == "work", "the owner's row was re-scoped to the copy's profile"
    assert owner.get("title") == "Victim"
    # the owner's own profile must still see its conversation
    assert any(r.get("session_id") == VICTIM for r in _visible_to(rows, "work"))
    # no row may carry the copy's metadata under the owner's id
    assert not (owner.get("title") == "Copy" and owner.get("profile") == "default")
    # the persisted index keeps the owner's row intact
    idx = _index_rows(store)
    assert idx[VICTIM]["message_count"] == 10
    assert idx[VICTIM]["profile"] == "work"
    assert idx[VICTIM]["title"] == "Victim"


def test_listing_never_rewrites_the_owners_sidecar(store):
    from api import models

    _victim(store)
    before = (store / f"{VICTIM}.json").read_bytes()
    _copy(store)
    models.all_sessions()
    assert (store / f"{VICTIM}.json").read_bytes() == before, (
        "the owner's conversation file was modified by the listing"
    )


def test_listing_is_stable_across_polls_with_a_foreign_body_copy(store):
    from api import models

    _victim(store)
    _copy(store)
    first = _rows_by_id(models.all_sessions())
    assert first[VICTIM]["message_count"] == 10
    for _ in range(2):
        again = _rows_by_id(models.all_sessions())
        assert again[VICTIM]["message_count"] == 10, "the owner's row degraded on a later poll"
        assert again[VICTIM]["profile"] == "work"
    idx_before = (store / "_index.json").read_bytes()
    models.all_sessions()
    assert (store / "_index.json").read_bytes() == idx_before, (
        "the listing rewrote the index on every poll"
    )


# --- other rows are not collaterally damaged -----------------------------------------


def test_self_consistent_rows_survive_alongside_the_copy(store):
    from api import models

    _victim(store)
    _keep(store)
    _copy(store)
    rows = _rows_by_id(models.all_sessions())
    assert rows[VICTIM]["message_count"] == 10
    assert rows[VICTIM]["profile"] == "work"
    assert rows[KEEP]["message_count"] == 6, "an unrelated session's row was damaged"
    assert rows[KEEP]["title"] == "Keep"


def test_index_row_for_the_copy_is_not_rekeyed_onto_the_owner(store):
    """The backfill path replaced index[i] with the foreign body's compact()."""
    from api import models

    _victim(store)
    _copy(store)
    entries = json.loads((store / "_index.json").read_bytes())
    entries.append({
        "session_id": COPY,
        "title": "Copy",
        "profile": "default",
        "message_count": 2,
        "updated_at": 9000.0,
        "created_at": 9000.0,
    })  # no last_message_at -> the backfill path loads this row's sidecar
    (store / "_index.json").write_text(json.dumps(entries, indent=2), encoding="utf-8")

    rows = _rows_by_id(models.all_sessions())
    assert rows[VICTIM]["message_count"] == 10, "the copy's metadata replaced the owner's row"
    assert rows[VICTIM]["profile"] == "work"
    assert COPY in rows, "the copy's own indexed row was dropped or re-keyed"
    idx = _index_rows(store)
    assert idx[VICTIM]["message_count"] == 10
    assert idx[VICTIM]["profile"] == "work"
    assert idx[COPY]["session_id"] == COPY


# --- the rebuild path stays correct ---------------------------------------------------


def test_full_rebuild_still_prefers_the_owner_row(store):
    from api import models

    _victim(store)
    _copy(store)
    (store / "_index.json").unlink()
    models.all_sessions()
    thread = models._SESSION_INDEX_REBUILD_THREAD
    if thread is not None:
        thread.join(timeout=10)
    rows = _rows_by_id(models.all_sessions())
    assert rows[VICTIM]["message_count"] == 10
    assert rows[VICTIM]["profile"] == "work"
    idx = _index_rows(store)
    assert idx[VICTIM]["message_count"] == 10
    assert idx[VICTIM]["profile"] == "work"


# --- defense in depth: the index writer itself refuses foreign bodies -----------------


def test_write_session_index_refuses_a_foreign_body_stub(store):
    from api import models

    _victim(store)
    _copy(store)
    stub = models.Session.load_metadata_only(COPY)
    assert stub is not None
    assert getattr(stub, "_sidecar_names_other_session", None) == COPY
    models._write_session_index(updates=[stub])
    idx = _index_rows(store)
    assert idx[VICTIM]["message_count"] == 10, "the index writer let a foreign stub overwrite the owner"
    assert idx[VICTIM]["profile"] == "work"
    assert idx[VICTIM]["title"] == "Victim"


# --- larger-copy rebuild regressions (review 6009588960) -----------------------


def _assert_rebuild_keeps_owner(store, snapshots):
    """Owner and unrelated row stay intact across rebuild and repeated listing."""
    from api import models

    idx = _index_rows(store)
    assert idx[VICTIM]["message_count"] == 10, "persisted owner row was overwritten by the copy"
    assert idx[VICTIM]["profile"] == "work", "persisted owner row was re-scoped to the copy"
    assert idx[VICTIM]["title"] == "Victim"
    assert set(idx) == {VICTIM, KEEP}, "the rebuild admitted a foreign row or dropped an unrelated row"
    assert (idx[KEEP]["title"], idx[KEEP]["profile"], idx[KEEP]["message_count"]) == ("Keep", "work", 6)

    index_bytes = (store / "_index.json").read_bytes()
    for _ in range(3):
        returned = _rows_by_id(models.all_sessions())
        assert set(returned) == {VICTIM, KEEP}
        assert returned[VICTIM]["message_count"] == 10, "returned owner row was overwritten by the copy"
        assert returned[VICTIM]["profile"] == "work", "returned owner row was re-scoped to the copy"
        assert returned[VICTIM]["title"] == "Victim"
        assert (returned[KEEP]["title"], returned[KEEP]["profile"], returned[KEEP]["message_count"]) == (
            "Keep", "work", 6,
        )

    assert (store / "_index.json").read_bytes() == index_bytes, "listing rewrote the rebuilt index"
    for path, raw in snapshots.items():
        assert path.read_bytes() == raw, f"{path.name} bytes changed across the rebuild"


@pytest.mark.parametrize("entry", ["explicit", "missing", "corrupt", "wrong_shape"])
@pytest.mark.parametrize("copy_first", [False, True], ids=["owner-first", "copy-first"])
def test_rebuild_never_lets_a_larger_foreign_copy_steal_the_owner(store, monkeypatch, entry, copy_first):
    from api import models

    _victim(store)
    keep_path = _keep(store)
    copy_path = _copy(store, message_count=20)  # 20 > the owner's 10
    owner_path = store / f"{VICTIM}.json"
    snapshots = {
        owner_path: owner_path.read_bytes(),
        copy_path: copy_path.read_bytes(),
        keep_path: keep_path.read_bytes(),
    }
    index = store / "_index.json"

    real_glob = Path.glob

    def ordered_glob(path, pattern):
        paths = list(real_glob(path, pattern))
        if path == store and pattern == "*.json":
            return sorted(paths, key=lambda p: ((p.stem != COPY) if copy_first else (p.stem == COPY), p.name))
        return paths

    monkeypatch.setattr(Path, "glob", ordered_glob)

    if entry == "explicit":
        models._write_session_index(updates=None)
    else:
        stub = models.Session.load_metadata_only(COPY)
        assert getattr(stub, "_sidecar_names_other_session", None) == COPY
        if entry == "missing":
            index.unlink()
        elif entry == "corrupt":
            index.write_text("{broken", encoding="utf-8")
        else:  # wrong_shape: valid JSON, but not the expected list
            index.write_text(json.dumps({"session_id": "not-a-list"}), encoding="utf-8")
        models._write_session_index(updates=[stub])

    _assert_rebuild_keeps_owner(store, snapshots)


@pytest.mark.parametrize("poison", ["missing", "corrupt", "wrong_shape"])
def test_full_rebuild_still_recovers_a_self_consistent_session(store, poison):
    """A legitimate targeted update recovers self-consistent files after fallback."""
    from api import models

    paths = (_victim(store), _keep(store))
    snapshots = {p: p.read_bytes() for p in paths}
    stub = models.Session.load_metadata_only(KEEP)
    assert stub is not None
    assert not getattr(stub, "_sidecar_names_other_session", None)
    index = store / "_index.json"
    if poison == "missing":
        index.unlink()
    elif poison == "corrupt":
        index.write_text("{broken", encoding="utf-8")
    else:
        index.write_text("{}", encoding="utf-8")
    models._write_session_index(updates=[stub])
    _assert_rebuild_keeps_owner(store, snapshots)


@pytest.mark.parametrize("legacy_only", [False, True], ids=["larger-legacy", "legacy-only"])
def test_full_rebuild_still_supports_legacy_session_prefixed_files(store, legacy_only):
    """Legacy admission is observable, not hidden by an identical canonical file."""
    from api import models

    owner_path = _victim(store)
    raw = json.loads(owner_path.read_text(encoding="utf-8"))
    raw.update(title="Legacy", message_count=20)
    raw["messages"] = [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": f"legacy {i}", "timestamp": 600 + i}
        for i in range(20)
    ]
    legacy_path = store / f"session_{VICTIM}.json"
    legacy_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    if legacy_only:
        owner_path.unlink()
    snapshots = {p: p.read_bytes() for p in (owner_path, legacy_path) if p.exists()}
    (store / "_index.json").unlink()

    models._write_session_index(updates=None)

    idx = _index_rows(store)
    assert set(idx) == {VICTIM}, "the legacy-named file did not collapse onto the owner"
    assert (idx[VICTIM]["title"], idx[VICTIM]["profile"], idx[VICTIM]["message_count"]) == ("Legacy", "work", 20)
    for path, contents in snapshots.items():
        assert path.read_bytes() == contents, "the rebuild modified a legacy or canonical sidecar"


def test_rebuild_does_not_admit_a_foreign_file_with_a_legacy_style_name(store):
    """``session_copy-8861.json`` whose body names ``victim-8861`` is NOT the
    legacy ``session_<id>.json`` layout — that requires the body id to match the
    ``session_`` prefix. A legacy-styled name on a foreign body must be skipped
    like any other copy, not waved through as legacy."""
    from api import models

    owner_path = _victim(store)
    keep_path = _keep(store)
    raw = json.loads(owner_path.read_text(encoding="utf-8"))
    raw.update(title="Sneaky", profile="default", session_id=VICTIM, message_count=30)
    raw["messages"] = [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": f"sneaky {i}", "timestamp": 600 + i}
        for i in range(30)
    ]
    sneaky_path = store / "session_copy-8861.json"  # session_ prefix, body names victim
    sneaky_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    snapshots = {p: p.read_bytes() for p in (owner_path, keep_path, sneaky_path)}
    (store / "_index.json").unlink()

    models._write_session_index(updates=None)

    _assert_rebuild_keeps_owner(store, snapshots)
