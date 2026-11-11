"""#9000: the delayed-write resolver never hands back a session loaded from a copy.

``_resolve_current_session_for_write`` reloads the worker's session by id through
``get_session``. If that session's file was replaced mid-turn with a copy of another
session's file, the reload builds the object from the copy's body: its ``session_id`` (and so
its ``path``) is the OTHER session's, and the #8852 loader marks it with
``_sidecar_names_other_session``. On master the resolver returned that object anyway, so:

- the turn-end cwd mirror targeted the other session's ``state.db`` row;
- an ephemeral cancel (``_finalize_cancelled_turn(..., ephemeral=True)``) unlinked
  ``session.path``, deleting the other session's file.

The resolver now refuses a marked object on the way out (``None``, the fail-closed answer its
callers already handle). An ordinary session still resolves to its own object.
"""

import json

import pytest

from tests.test_issue8852_body_id_write_target import (  # noqa: F401 (fixture)
    VICTIM,
    _files,
    _victim,
    store,
)

OWN = "own-9000"
STREAM = "stream-9000"


def _own_session(directory):
    """Save the worker's session and return the snapshot a running turn would hold."""
    from api import models

    session = models.Session(session_id=OWN, title="Own", profile="default")
    session.messages = [
        {"role": "user", "content": "own q", "timestamp": 200},
        {"role": "assistant", "content": "own a", "timestamp": 201},
    ]
    session.save(touch_updated_at=False)
    models.SESSIONS.clear()
    snapshot = models.get_session(OWN)
    assert snapshot.session_id == OWN
    return snapshot


def _swap_own_file_for_a_copy_of_the_victim(directory):
    """An outside writer (a restored backup, a manual copy) replaces the worker's file mid-turn
    with the victim's file: the body now names the victim."""
    from api import models

    (directory / f"{OWN}.json").write_bytes((directory / f"{VICTIM}.json").read_bytes())
    models.SESSIONS.clear()


@pytest.fixture
def writeback_owner():
    from api.config import clear_session_writeback_owner_if_owned, register_session_writeback_owner

    register_session_writeback_owner(OWN, STREAM)
    try:
        yield STREAM
    finally:
        clear_session_writeback_owner_if_owned(OWN, STREAM)


def test_the_resolver_refuses_a_session_reloaded_from_a_copy(store):
    from api import models
    from api.streaming import _resolve_current_session_for_write

    _victim(store)
    snapshot = _own_session(store)
    _swap_own_file_for_a_copy_of_the_victim(store)

    # The reload by id really does build the victim's object, marked by the #8852 loader.
    reloaded = models.Session.load(OWN)
    assert reloaded.session_id == VICTIM
    assert getattr(reloaded, "_sidecar_names_other_session", None) == OWN
    models.SESSIONS.clear()

    assert _resolve_current_session_for_write(snapshot) is None


def test_an_ephemeral_cancel_never_deletes_the_session_a_copy_names(store, writeback_owner):
    from api.streaming import _finalize_cancelled_turn

    _victim(store)
    snapshot = _own_session(store)
    _swap_own_file_for_a_copy_of_the_victim(store)
    before = _files(store)

    _finalize_cancelled_turn(snapshot, ephemeral=True, stream_id=writeback_owner)

    assert (store / f"{VICTIM}.json").exists(), "the ephemeral cancel deleted another session's file"
    assert _files(store) == before
    victim = json.loads(before[f"{VICTIM}.json"])
    assert (victim["session_id"], len(victim["messages"])) == (VICTIM, 10)


# --- what still resolves and finalizes, as before -------------------------------------------


def test_an_ordinary_session_still_resolves_to_its_own_object(store):
    from api import models
    from api.streaming import _resolve_current_session_for_write

    _victim(store)
    snapshot = _own_session(store)
    models.SESSIONS.clear()

    current = _resolve_current_session_for_write(snapshot)

    assert current is not None
    assert current.session_id == OWN
    assert not getattr(current, "_sidecar_names_other_session", None)
    assert current is models.get_session(OWN), "the resolver must return the cached current object"


def test_an_ephemeral_cancel_still_removes_its_own_file(store, writeback_owner):
    """Positive control for the refusal above: with no swap, the same call reaches the unlink
    and removes the session's own (ephemeral) file, and only that file."""
    from api.streaming import _finalize_cancelled_turn

    _victim(store)
    snapshot = _own_session(store)
    assert (store / f"{OWN}.json").exists()
    victim_before = (store / f"{VICTIM}.json").read_bytes()

    _finalize_cancelled_turn(snapshot, ephemeral=True, stream_id=writeback_owner)

    assert not (store / f"{OWN}.json").exists()
    assert (store / f"{VICTIM}.json").read_bytes() == victim_before
