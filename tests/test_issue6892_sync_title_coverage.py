"""Direct coverage for api.state_sync.sync_session_title (#6892).

Codex round-1 SHOULD-FIX: the PR's only test change mocks sync_session_title to a
no-op, so the new state.db-writing path had no direct test proving it actually
persists a generated title AND preserves a manual rename. These tests exercise
the real function against a temporary hermes-agent state.db, opened through
``state_sync._open_state_db``: the legacy ``SessionDB`` on Agents that still carry
its write verbs, the storage interface (``persistence_assembly``) on newer ones.
Rows are read back straight from the file, independent of either API.
"""

import sqlite3

import pytest


def _make_db(tmp_path):
    from tests._agent_state_store import open_agent_store

    return open_agent_store(tmp_path / "state.db")


def _row(tmp_path, session_id):
    conn = sqlite3.connect(f"file:{tmp_path / 'state.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT source, title, title_source FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()


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

        row = _row(tmp_path, "sess-gen")
        assert row["title"] == "Generated Title"
        # Provenance is the automatic LLM source, not a user rename.
        assert row["title_source"] == "llm"
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
        assert _row(tmp_path, "sess-manual")["title_source"] == "user"

        monkeypatch.setattr(
            state_sync,
            "_get_state_db",
            lambda profile=None: _make_db(tmp_path),
        )

        # A background generation now tries to sync an auto title — must be a no-op.
        state_sync.sync_session_title("sess-manual", "Auto Generated", profile="default")

        row = _row(tmp_path, "sess-manual")
        assert row["title"] == "My Manual Name"
        assert row["title_source"] == "user"
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
        row = _row(tmp_path, "sess-cli")
        assert row["title"] == "Some Title"
        assert row["source"] == "cli"
    finally:
        db.close()
