"""One-shot runs (`hermes -z`) must not fill the interactive sidebar window (#8265).

The reported shape: agents that delegate through `hermes -z` create many more
one-shot rows than Desktop conversations, so the bounded 20-row window listed
mostly one-shot rows and dropped most Desktop sessions.
"""

import time

import api.models as models
from tests.test_pr7555_cli_session_availability import (
    _configure_real_single_profile,
    _insert_session,
    _make_state_db,
)


def _build_crowded_db(tmp_path):
    db = tmp_path / "state.db"
    # Six older Desktop conversations, then many newer one-shot runs, the way
    # a delegating agent produces them (241 one-shot vs 82 desktop in the report).
    _make_state_db(db, sessions=6, messages_per_session=1, source="desktop", session_source="desktop")
    now = time.time()
    for i in range(models.CLI_VISIBLE_SESSION_LIMIT * 2):
        _insert_session(db, f"oneshot-{i:03d}", source="oneshot", started_at=now + i)
    return db


def test_oneshot_runs_do_not_push_desktop_sessions_out_of_the_window(tmp_path, monkeypatch):
    db = _build_crowded_db(tmp_path)
    _configure_real_single_profile(monkeypatch, tmp_path, db)

    rows = models.get_cli_sessions()
    ids = {row["session_id"] for row in rows}

    assert {f"cli_perf_{i:04d}" for i in range(6)} <= ids
    assert not any(sid.startswith("oneshot-") for sid in ids)


def test_explicit_oneshot_filter_still_lists_oneshot_runs(tmp_path, monkeypatch):
    db = _build_crowded_db(tmp_path)
    _configure_real_single_profile(monkeypatch, tmp_path, db)

    rows = models.get_cli_sessions(source_filter="oneshot")
    ids = {row["session_id"] for row in rows}

    assert ids
    assert all(sid.startswith("oneshot-") for sid in ids)


def test_oneshot_is_a_background_source():
    assert "oneshot" in models.BACKGROUND_CLI_SOURCES
