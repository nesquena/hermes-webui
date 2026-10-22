import time
import pytest
from api import models, routes
from tests.test_pr7555_cli_session_availability import (
    _configure_real_single_profile,
    _insert_session,
    _make_state_db,
)


def test_aged_out_desktop_session_lookup_succeeds(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    # Create 25 desktop sessions
    _make_state_db(db, sessions=25, messages_per_session=1, source="desktop", session_source="desktop")
    _configure_real_single_profile(monkeypatch, tmp_path, db)
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 20)

    # 1. get_cli_sessions returns only the most recent 20
    recent = models.get_cli_sessions()
    recent_sids = {r["session_id"] for r in recent}
    assert len(recent) <= 20
    assert "cli_perf_0000" not in recent_sids
    assert "cli_perf_0024" in recent_sids

    # 2. _lookup_cli_session_metadata finds the aged-out session outside the window (#8061)
    aged_meta = routes._lookup_cli_session_metadata("cli_perf_0000")
    assert aged_meta.get("session_id") == "cli_perf_0000"
    assert aged_meta.get("source_tag") == "desktop"

    # 3. Nonexistent session still returns {}
    assert routes._lookup_cli_session_metadata("nonexistent-sid") == {}

    # 4. get_cli_session_by_id source filtering
    assert models.get_cli_session_by_id("cli_perf_0000")["session_id"] == "cli_perf_0000"
    assert models.get_cli_session_by_id("cli_perf_0000", "desktop")["session_id"] == "cli_perf_0000"
    assert models.get_cli_session_by_id("cli_perf_0000", "oneshot") == {}
