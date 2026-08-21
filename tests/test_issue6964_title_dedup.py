"""Regression tests for state.db title de-dup on collision (#6964).

Follow-up to #6892 (sync auto-generated session titles to state.db).
When two WebUI sessions generate the SAME auto-title, hermes-agent's
state.db uniqueness rule makes the second ``set_auto_title_if_empty``
raise ValueError, which ``sync_session_title`` used to swallow at debug
level -- leaving the second row blank in ``hermes sessions list``.
These tests prove the collision path now de-duplicates (suffix variant)
instead of leaving the row untitled, and that unique titles are unchanged.
"""

import pytest


def _make_db(tmp_path):
    hermes_state = pytest.importorskip("hermes_state")
    SessionDB = hermes_state.SessionDB
    return SessionDB(db_path=tmp_path / "state.db")


@pytest.mark.requires_agent_modules
def test_sync_session_title_dedups_second_session_on_collision(tmp_path, monkeypatch):
    """Two sessions with the same auto-title: the 2nd gets a de-duplicated
    variant instead of being left blank in state.db."""
    from api import state_sync

    db = _make_db(tmp_path)
    try:
        # Route sync_session_title at the real temp DB (a fresh handle each call,
        # mirroring production, but pointed at our tmp state.db).
        monkeypatch.setattr(
            state_sync,
            "_get_state_db",
            lambda profile=None: _make_db(tmp_path),
        )

        state_sync.sync_session_title("sess-1", "Same Title", profile="default")
        state_sync.sync_session_title("sess-2", "Same Title", profile="default")

        t1 = db.get_session_title("sess-1")
        t2 = db.get_session_title("sess-2")
        assert t1 == "Same Title"
        # De-duplicated with a lineage suffix -- never left blank.
        assert t2 == "Same Title #2"
        assert t1 != t2
    finally:
        db.close()


@pytest.mark.requires_agent_modules
def test_sync_session_title_dedup_handles_numbered_base(tmp_path, monkeypatch):
    """A colliding title that already carries a '#N' suffix keeps incrementing
    deterministically (e.g. 'Same Title #2' -> 'Same Title #3')."""
    from api import state_sync

    db = _make_db(tmp_path)
    try:
        monkeypatch.setattr(
            state_sync,
            "_get_state_db",
            lambda profile=None: _make_db(tmp_path),
        )

        state_sync.sync_session_title("sess-1", "Same Title #2", profile="default")
        state_sync.sync_session_title("sess-2", "Same Title #2", profile="default")

        t1 = db.get_session_title("sess-1")
        t2 = db.get_session_title("sess-2")
        assert t1 == "Same Title #2"
        assert t2 == "Same Title #3"
        assert t1 != t2
    finally:
        db.close()


@pytest.mark.requires_agent_modules
def test_sync_session_title_unique_titles_unchanged(tmp_path, monkeypatch):
    """Non-colliding titles keep their exact value (no suffix added)."""
    from api import state_sync

    db = _make_db(tmp_path)
    try:
        monkeypatch.setattr(
            state_sync,
            "_get_state_db",
            lambda profile=None: _make_db(tmp_path),
        )

        state_sync.sync_session_title("sess-a", "Alpha Title", profile="default")
        state_sync.sync_session_title("sess-b", "Beta Title", profile="default")

        assert db.get_session_title("sess-a") == "Alpha Title"
        assert db.get_session_title("sess-b") == "Beta Title"
    finally:
        db.close()


@pytest.mark.requires_agent_modules
def test_sync_session_title_dedup_retry_never_clobbers_manual_rename(tmp_path, monkeypatch):
    """The de-dup retry still respects title provenance: if the second session
    was manually renamed (``user`` source) before the colliding auto-title sync
    runs, the retry with the ``#2`` variant must NOT overwrite the user's name.

    Codex #6966 SHOULD-FIX: covers the manual-rename-during-retry interleaving
    (the existing tests cover manual-rename-before-sync only indirectly)."""
    from api import state_sync

    db = _make_db(tmp_path)
    try:
        monkeypatch.setattr(
            state_sync,
            "_get_state_db",
            lambda profile=None: _make_db(tmp_path),
        )

        # sess-1 takes "Same Title" via the normal auto-title sync.
        state_sync.sync_session_title("sess-1", "Same Title", profile="default")

        # sess-2 is manually renamed by the user (higher-ranked `user` source)
        # BEFORE the colliding auto-title sync fires for it.
        db.ensure_session(session_id="sess-2", source="webui")
        db.set_session_title("sess-2", "My Own Name")

        # Now the colliding auto-title sync runs for sess-2. It collides on
        # "Same Title", derives "Same Title #2", and retries -- but the retry
        # goes through the same provenance rank guard, so the user's manual
        # name must survive.
        state_sync.sync_session_title("sess-2", "Same Title", profile="default")

        assert db.get_session_title("sess-1") == "Same Title"
        # The manual (user-source) rename is preserved -- never clobbered by
        # either the initial auto-title or the de-dup retry variant.
        assert db.get_session_title("sess-2") == "My Own Name"
    finally:
        db.close()
