"""`--source tool` sessions must not fill the interactive sidebar window.

Hermes Agent documents `source=tool` as the tag for third-party integration
sessions "that should not appear in user session lists", and its own
`hermes sessions list` hides them. A script that drives `hermes chat --oneshot
--source tool` per batch can create well over a thousand of them, which pushed
Desktop/TUI/CLI conversations out of the 20-row window the same way one-shot
runs did in #8265. They follow the one-shot treatment: out of the window and
its unassigned refill, still reachable from a project chip and by id.
"""

import time

import api.models as models
import tests.test_project_assigned_cli_session_limit as _assigned
from tests.test_pr7555_cli_session_availability import (
    _configure_real_single_profile,
    _insert_session,
    _make_state_db,
)


def _build_crowded_db(tmp_path):
    db = tmp_path / "state.db"
    _make_state_db(db, sessions=6, messages_per_session=1, source="desktop", session_source="desktop")
    now = time.time()
    for i in range(models.CLI_VISIBLE_SESSION_LIMIT * 2):
        _insert_session(db, f"tool-{i:03d}", source="tool", started_at=now + i)
    return db


def test_tool_sessions_do_not_push_desktop_sessions_out_of_the_window(tmp_path, monkeypatch):
    db = _build_crowded_db(tmp_path)
    _configure_real_single_profile(monkeypatch, tmp_path, db)

    rows = models.get_cli_sessions()
    ids = {row["session_id"] for row in rows}

    assert {f"cli_perf_{i:04d}" for i in range(6)} <= ids
    assert not any(sid.startswith("tool-") for sid in ids)


def test_explicit_tool_filter_still_lists_tool_sessions(tmp_path, monkeypatch):
    db = _build_crowded_db(tmp_path)
    _configure_real_single_profile(monkeypatch, tmp_path, db)

    ids = {row["session_id"] for row in models.get_cli_sessions(source_filter="tool")}

    assert ids
    assert all(sid.startswith("tool-") for sid in ids)


def test_tool_is_kept_out_of_the_window_but_keeps_its_project_chip():
    assert "tool" in models.INTERACTIVE_WINDOW_EXCLUDED_CLI_SOURCES
    assert "tool" not in models.BACKGROUND_CLI_SOURCES


fake_hermes_home = _assigned.fake_hermes_home
BASE_TS = _assigned.BASE_TS
_register_projects = _assigned._register_projects
_session = _assigned._session
_write_state_db = _assigned._write_state_db


def _tool_rows(count, *, oldest_project_id=None):
    rows = [_session(f"desk-{i}", BASE_TS + i, source="desktop") for i in range(3)]
    rows.extend(
        _session(
            f"tool-{i:03d}",
            BASE_TS + 100 + i,
            source="tool",
            project_id=oldest_project_id if i == 0 else None,
        )
        for i in range(count)
    )
    return rows


def test_project_assigned_tool_session_is_recovered(fake_hermes_home, tmp_path):
    # A user who filed tool sessions under a project by hand keeps seeing them.
    _register_projects(tmp_path, "project-1")
    count = models.CLI_VISIBLE_SESSION_LIMIT + 1
    _write_state_db(fake_hermes_home / "state.db", _tool_rows(count, oldest_project_id="project-1"))

    by_id = {row["session_id"]: row for row in models.get_cli_sessions()}
    assert by_id["tool-000"]["project_id"] == "project-1"
    assert not any(sid.startswith("tool-") and sid != "tool-000" for sid in by_id)
    assert {"desk-0", "desk-1", "desk-2"} <= set(by_id)


def test_opened_tool_session_resolves_by_id(fake_hermes_home):
    count = models.CLI_VISIBLE_SESSION_LIMIT + 5
    _write_state_db(fake_hermes_home / "state.db", _tool_rows(count))
    from api import routes

    shown = {row["session_id"] for row in models.get_cli_sessions(source_filter="tool")}
    assert "tool-000" not in shown

    assert routes._lookup_cli_session_metadata("tool-000").get("session_id") == "tool-000"
    assert models.get_cli_session_by_id("desk-0", "tool") == {}
