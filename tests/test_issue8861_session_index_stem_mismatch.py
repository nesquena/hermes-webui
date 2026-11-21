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

Fix shape (fail closed): a row recovered or backfilled from a file whose body
names another session is never written back. The listing keeps the owner's row
untouched and skips the foreign stub entirely; ``_write_session_index()`` also
refuses foreign-body updates so no other caller can re-open the hole. The
owner's sidecar is never read or written by these paths.

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


def _copy(directory):
    """A sidecar named `copy-8861` whose body names the owner (2 messages, default)."""
    raw = json.loads((directory / f"{VICTIM}.json").read_text(encoding="utf-8"))
    raw.update(title="Copy", profile="default", session_id=VICTIM)
    raw["messages"] = [
        {"role": "user", "content": "copy q", "timestamp": 500},
        {"role": "assistant", "content": "copy a", "timestamp": 501},
    ]
    raw["message_count"] = 2
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
