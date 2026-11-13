"""One-shot runs (`hermes -z`) must not fill the interactive sidebar window (#8265).

The reported shape: agents that delegate through `hermes -z` create many more
one-shot rows than Desktop conversations, so the bounded 20-row window listed
mostly one-shot rows and dropped most Desktop sessions.
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


def test_oneshot_is_kept_out_of_the_window_but_keeps_its_project_chip():
    assert "oneshot" in models.INTERACTIVE_WINDOW_EXCLUDED_CLI_SOURCES
    assert "oneshot" not in models.BACKGROUND_CLI_SOURCES


def _configure_named_profiles(monkeypatch, tmp_path, homes):
    _configure_real_single_profile(monkeypatch, tmp_path, homes[0] / "state.db")
    monkeypatch.setattr(
        models,
        "_all_profiles_cli_contexts",
        lambda: (
            [(home, home / "state.db", name) for home, name in zip(homes, ("default", "work"), strict=True)],
            tuple(
                (str(home), name, models._sqlite_file_stat_cache_key(home / "state.db"))
                for home, name in zip(homes, ("default", "work"), strict=True)
            ),
        ),
    )
    models.clear_cli_sessions_cache()


def test_opened_oneshot_session_can_still_be_found_pinned_and_renamed(tmp_path, monkeypatch):
    # #8349 review: keeping oneshot rows out of the sidebar window must not make
    # a oneshot session the user opened unreachable for pin/rename/workspace.
    from api import routes

    import api.profiles as profiles

    db = _build_crowded_db(tmp_path)
    _configure_real_single_profile(monkeypatch, tmp_path, db)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(models, "_get_profile_home", lambda profile=None: tmp_path)
    sid = f"oneshot-{models.CLI_VISIBLE_SESSION_LIMIT * 2 - 1:03d}"

    meta = routes._lookup_cli_session_metadata(sid)
    assert meta.get("session_id") == sid

    session = routes._get_or_materialize_session(sid)
    session.pinned = True
    session.title = "Renamed oneshot"
    session.save()

    stored = models.get_session(sid)
    assert stored.pinned is True
    assert stored.title == "Renamed oneshot"


def test_oneshot_session_resolves_under_a_named_profile(tmp_path, monkeypatch):
    from api import routes

    homes = [tmp_path / "default-home", tmp_path / "work-home"]
    for home in homes:
        home.mkdir()
    _make_state_db(homes[0] / "state.db", sessions=1, messages_per_session=1)
    _make_state_db(homes[1] / "state.db", sessions=1, messages_per_session=1)
    _insert_session(homes[1] / "state.db", "oneshot-work", source="oneshot")
    _configure_named_profiles(monkeypatch, tmp_path, homes)

    meta = routes._resolve_cli_import_metadata(
        "oneshot-work", requested_profile="work", allow_all_profiles=True,
    )
    assert meta.get("session_id") == "oneshot-work"
    assert meta.get("profile") == "work"


# --- #8349 second review: ids outside every window, and the Files pane ------

# Reuse the temporary HERMES_HOME + projects.json fixture and row helpers.
fake_hermes_home = _assigned.fake_hermes_home
BASE_TS = _assigned.BASE_TS
_register_projects = _assigned._register_projects
_session = _assigned._session
_write_state_db = _assigned._write_state_db


def _oneshot_rows(count, *, oldest_project_id=None):
    rows = [_session(f"desk-{i}", BASE_TS + i, source="desktop") for i in range(3)]
    rows.extend(
        _session(
            f"oneshot-{i:03d}",
            BASE_TS + 100 + i,
            source="oneshot",
            project_id=oldest_project_id if i == 0 else None,
        )
        for i in range(count)
    )
    return rows


def test_project_assigned_oneshot_older_than_every_window_is_recovered(fake_hermes_home, tmp_path):
    # Seed more one-shot runs than the one-shot view shows and file the oldest
    # under a project: master's assigned recovery pass reached it, so must this.
    _register_projects(tmp_path, "project-1")
    count = models.CLI_VISIBLE_SESSION_LIMIT + 1
    _write_state_db(fake_hermes_home / "state.db", _oneshot_rows(count, oldest_project_id="project-1"))

    by_id = {row["session_id"]: row for row in models.get_cli_sessions()}
    assert by_id["oneshot-000"]["project_id"] == "project-1"
    assert not any(
        sid.startswith("oneshot-") and sid != "oneshot-000" for sid in by_id
    ), "unassigned one-shot runs must stay out of the interactive window"
    assert {"desk-0", "desk-1", "desk-2"} <= set(by_id)

    from api import routes

    assert routes._lookup_cli_session_metadata("oneshot-000").get("project_id") == "project-1"


def test_unassigned_oneshot_older_than_the_oneshot_view_resolves_by_id(fake_hermes_home):
    count = models.CLI_VISIBLE_SESSION_LIMIT + 5
    _write_state_db(fake_hermes_home / "state.db", _oneshot_rows(count))
    from api import routes

    shown = {row["session_id"] for row in models.get_cli_sessions(source_filter="oneshot")}
    assert "oneshot-000" not in shown

    meta = routes._lookup_cli_session_metadata("oneshot-000")
    assert meta.get("session_id") == "oneshot-000"
    assert routes._lookup_cli_session_metadata("no-such-session") == {}


def test_explicit_lookup_reads_only_the_requested_source(fake_hermes_home):
    _write_state_db(fake_hermes_home / "state.db", _oneshot_rows(2))

    assert models.get_cli_session_by_id("desk-0", "oneshot") == {}
    assert models.get_cli_session_by_id("oneshot-001", "oneshot")["session_id"] == "oneshot-001"
    assert models.get_cli_session_by_id("", "oneshot") == {}


def _list_dir_payload(monkeypatch, tmp_path, sid):
    from api import routes

    listed = {}

    def _get_session(_sid, **_kwargs):
        raise KeyError(_sid)

    def _list(workspace_path, _rel):
        listed["workspace"] = workspace_path
        return []

    monkeypatch.setattr(routes, "get_session", _get_session)
    monkeypatch.setattr(routes, "list_dir", _list)
    monkeypatch.setattr(routes, "dir_signature", lambda *_args: "sig")
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    monkeypatch.setattr(
        routes, "bad", lambda _handler, message, status=400: {"error": message, "status": status},
    )
    from urllib.parse import urlparse

    return routes._handle_list_dir(None, urlparse(f"/api/list?session_id={sid}&path=.")), listed


def test_files_pane_lists_an_opened_oneshot_before_any_sidecar_exists(fake_hermes_home, tmp_path, monkeypatch):
    from api import routes

    count = models.CLI_VISIBLE_SESSION_LIMIT + 5
    _write_state_db(fake_hermes_home / "state.db", _oneshot_rows(count))
    seen = {}

    def _trusted(workspace, profile=None):
        seen["profile"] = profile
        return tmp_path

    monkeypatch.setattr(routes, "resolve_trusted_workspace", _trusted)

    payload, listed = _list_dir_payload(monkeypatch, tmp_path, "oneshot-000")
    assert payload.get("status") != 404, payload
    assert listed["workspace"] == tmp_path
    assert seen["profile"] is None

    payload, _ = _list_dir_payload(monkeypatch, tmp_path, "no-such-session")
    assert payload == {"error": "Session not found", "status": 404}


def test_files_pane_checks_another_profiles_oneshot_against_that_profile(tmp_path, monkeypatch):
    from api import routes

    homes = [tmp_path / "default-home", tmp_path / "work-home"]
    for home in homes:
        home.mkdir()
    _make_state_db(homes[0] / "state.db", sessions=1, messages_per_session=1)
    _make_state_db(homes[1] / "state.db", sessions=1, messages_per_session=1)
    _insert_session(homes[1] / "state.db", "oneshot-work", source="oneshot")
    _configure_named_profiles(monkeypatch, tmp_path, homes)
    seen = {}

    def _trusted(workspace, profile=None):
        seen["profile"] = profile
        return tmp_path

    monkeypatch.setattr(routes, "resolve_trusted_workspace", _trusted)

    payload, listed = _list_dir_payload(monkeypatch, tmp_path, "oneshot-work")
    assert payload.get("status") != 404, payload
    assert seen["profile"] == "work"
