"""Direct coverage for api.state_sync.sync_session_title (#6892).

Codex round-1 SHOULD-FIX: the PR's only test change mocks sync_session_title to a
no-op, so the new state.db-writing path had no direct test proving it actually
persists a generated title AND preserves a manual rename. These tests exercise
the real function against a temporary hermes-agent SessionDB.
"""

import pytest


def _make_db(tmp_path):
    hermes_state = pytest.importorskip("hermes_state")
    SessionDB = hermes_state.SessionDB
    return SessionDB(db_path=tmp_path / "state.db")


@pytest.mark.requires_agent_modules
def test_sync_session_title_persists_generated_title(tmp_path, monkeypatch):
    """A generated title is written to state.db so `hermes sessions list` isn't blank."""
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

        state_sync.sync_session_title("sess-gen", "Generated Title", profile="default")

        assert db.get_session_title("sess-gen") == "Generated Title"
        # Provenance is the automatic LLM source, not a user rename.
        assert db.get_session_title_source("sess-gen") == db.TITLE_SOURCE_LLM
    finally:
        db.close()


@pytest.mark.requires_agent_modules
def test_sync_session_title_does_not_overwrite_manual_rename(tmp_path, monkeypatch):
    """A user rename (source=user) is never clobbered by a later title sync."""
    from api import state_sync

    db = _make_db(tmp_path)
    try:
        # Seed a manual rename first — this records `user` provenance.
        db.ensure_session(session_id="sess-manual", source="cli")
        db.set_session_title("sess-manual", "My Manual Name")
        assert db.get_session_title_source("sess-manual") == db.TITLE_SOURCE_USER

        monkeypatch.setattr(
            state_sync,
            "_get_state_db",
            lambda profile=None: _make_db(tmp_path),
        )

        # A background generation now tries to sync an auto title — must be a no-op.
        state_sync.sync_session_title("sess-manual", "Auto Generated", profile="default")

        assert db.get_session_title("sess-manual") == "My Manual Name"
        assert db.get_session_title_source("sess-manual") == db.TITLE_SOURCE_USER
    finally:
        db.close()


@pytest.mark.requires_agent_modules
def test_sync_session_title_preserves_existing_source_on_ensure(tmp_path, monkeypatch):
    """ensure_session(source='webui') must not clobber an existing cli/gateway row's source."""
    from api import state_sync

    db = _make_db(tmp_path)
    try:
        db.ensure_session(session_id="sess-cli", source="cli")

        monkeypatch.setattr(
            state_sync,
            "_get_state_db",
            lambda profile=None: _make_db(tmp_path),
        )

        # Sync (which calls ensure_session(source='webui')) on a pre-existing cli row.
        state_sync.sync_session_title("sess-cli", "Some Title", profile="default")

        # INSERT OR IGNORE semantics: the original source is preserved, the title lands.
        assert db.get_session_title("sess-cli") == "Some Title"
        with db._lock:
            row = db._conn.execute(
                "SELECT source FROM sessions WHERE id = ?", ("sess-cli",)
            ).fetchone()
        assert row["source"] == "cli"
    finally:
        db.close()
