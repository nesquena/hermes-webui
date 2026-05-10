"""Tests for issue #1614: /api/projects must be scoped to the active profile.

Same shape as #1611 but for projects:
  - Global PROJECTS_FILE returned to every profile.
  - Project rows had no `profile` field.
  - Mutation endpoints didn't validate profile ownership.
  - ensure_cron_project() returned the same global Cron Jobs project across profiles.

Fix:
  - New `profile` field on project dicts (defaulted at create-time).
  - /api/projects filters by active profile by default; ?all_profiles=1 opts in.
  - Create/rename/delete/move endpoints reject ops on cross-profile projects.
  - ensure_cron_project() keys lookup by (name, profile).
  - One-time migration: untagged projects inherit profile from sessions, fall back to 'default'.
"""

import json
import threading
from pathlib import Path

import pytest


# ── ensure_cron_project: per-profile ─────────────────────────────────────


def test_ensure_cron_project_creates_per_profile(tmp_path, monkeypatch):
    """Each distinct profile gets its own 'Cron Jobs' project_id."""
    import api.config as cfg
    import api.models as models
    import api.profiles as profiles

    projects_file = tmp_path / "projects.json"
    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(models, "_CRON_PROJECT_LOCK", threading.Lock())
    profiles._invalidate_root_profile_cache()
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [])

    monkeypatch.setattr(profiles, "_active_profile", "haku")
    pid_haku = models.ensure_cron_project()
    monkeypatch.setattr(profiles, "_active_profile", "kinni")
    pid_kinni = models.ensure_cron_project()

    assert pid_haku != pid_kinni, "Per-profile cron projects must have distinct ids"

    # Verify on disk
    saved = json.loads(projects_file.read_text())
    cron_rows = [p for p in saved if p["name"] == "Cron Jobs"]
    assert len(cron_rows) == 2
    assert {r["profile"] for r in cron_rows} == {"haku", "kinni"}


def test_ensure_cron_project_idempotent_per_profile(tmp_path, monkeypatch):
    """Repeated calls within the same profile return the same id."""
    import api.config as cfg
    import api.models as models
    import api.profiles as profiles

    projects_file = tmp_path / "projects.json"
    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(models, "_CRON_PROJECT_LOCK", threading.Lock())
    profiles._invalidate_root_profile_cache()
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [])
    monkeypatch.setattr(profiles, "_active_profile", "haku")

    pid1 = models.ensure_cron_project()
    pid2 = models.ensure_cron_project()
    assert pid1 == pid2


def test_ensure_cron_project_back_tags_legacy_untagged(tmp_path, monkeypatch):
    """A pre-existing 'Cron Jobs' project with no `profile` field is back-tagged
    to whichever profile first calls ensure_cron_project(), then reused going forward."""
    import api.config as cfg
    import api.models as models
    import api.profiles as profiles

    projects_file = tmp_path / "projects.json"
    legacy_pid = "legacy123abc"
    projects_file.write_text(
        json.dumps(
            [
                {
                    "project_id": legacy_pid,
                    "name": "Cron Jobs",
                    "color": "#6366f1",
                    "created_at": 1.0,
                }
            ]
        )
    )
    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(
        models, "_projects_migrated", True
    )  # skip the load_projects auto-migration
    monkeypatch.setattr(models, "_CRON_PROJECT_LOCK", threading.Lock())
    profiles._invalidate_root_profile_cache()
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [])
    monkeypatch.setattr(profiles, "_active_profile", "haku")

    returned = models.ensure_cron_project()
    assert returned == legacy_pid

    saved = json.loads(projects_file.read_text())
    assert saved[0]["profile"] == "haku", (
        "Legacy untagged cron project must be back-tagged"
    )


def test_ensure_cron_project_renamed_root_matches_default(tmp_path, monkeypatch):
    """When the root profile has been renamed (e.g. 'kinni'), an existing cron
    project tagged 'default' is reused — they're the same profile from the
    user's perspective."""
    import api.config as cfg
    import api.models as models
    import api.profiles as profiles

    projects_file = tmp_path / "projects.json"
    pid = "crondefault1"
    projects_file.write_text(
        json.dumps(
            [
                {
                    "project_id": pid,
                    "name": "Cron Jobs",
                    "color": "#6366f1",
                    "profile": "default",
                    "created_at": 1.0,
                }
            ]
        )
    )
    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(models, "_CRON_PROJECT_LOCK", threading.Lock())

    monkeypatch.setattr(
        profiles,
        "list_profiles_api",
        lambda: [
            {"name": "kinni", "is_default": True, "path": str(tmp_path)},
        ],
    )
    profiles._invalidate_root_profile_cache()
    monkeypatch.setattr(profiles, "_active_profile", "kinni")

    returned = models.ensure_cron_project()
    assert returned == pid, "Renamed root must reuse the 'default'-tagged cron project"


# ── load_projects migration ────────────────────────────────────────────────


def test_load_projects_backfills_from_session_index(tmp_path, monkeypatch):
    """Untagged projects pick up their profile from any session that uses them."""
    import api.config as cfg
    import api.models as models

    projects_file = tmp_path / "projects.json"
    index_file = tmp_path / "_index.json"

    projects_file.write_text(
        json.dumps(
            [
                {"project_id": "abc111", "name": "My Project", "created_at": 1.0},
                {"project_id": "def222", "name": "Other", "created_at": 2.0},
                {
                    "project_id": "tagged3",
                    "name": "Already Tagged",
                    "profile": "haku",
                    "created_at": 3.0,
                },
            ]
        )
    )
    index_file.write_text(
        json.dumps(
            [
                {
                    "session_id": "s1",
                    "project_id": "abc111",
                    "profile": "haku",
                    "message_count": 1,
                },
                {
                    "session_id": "s2",
                    "project_id": "def222",
                    "profile": "kinni",
                    "message_count": 2,
                },
                {
                    "session_id": "s3",
                    "project_id": "tagged3",
                    "profile": "haku",
                    "message_count": 0,
                },
            ]
        )
    )

    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(cfg, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "_projects_migrated", False)
    monkeypatch.setattr(models, "_PROJECTS_MIGRATION_LOCK", threading.Lock())

    out = models.load_projects()
    by_id = {p["project_id"]: p for p in out}
    assert by_id["abc111"]["profile"] == "haku", "abc111 had a haku session"
    assert by_id["def222"]["profile"] == "kinni", "def222 had a kinni session"
    assert by_id["tagged3"]["profile"] == "haku", "Already-tagged unchanged"

    # Persisted to disk
    saved = json.loads(projects_file.read_text())
    saved_by_id = {p["project_id"]: p for p in saved}
    assert saved_by_id["abc111"]["profile"] == "haku"
    assert saved_by_id["def222"]["profile"] == "kinni"


def test_load_projects_backfills_to_default_when_no_sessions(tmp_path, monkeypatch):
    """Untagged project with no session attribution falls back to 'default'."""
    import api.config as cfg
    import api.models as models

    projects_file = tmp_path / "projects.json"
    projects_file.write_text(
        json.dumps(
            [
                {"project_id": "orphan1", "name": "Orphan", "created_at": 1.0},
            ]
        )
    )

    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects_file)
    # Index doesn't exist
    monkeypatch.setattr(cfg, "SESSION_INDEX_FILE", tmp_path / "no-index.json")
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "no-index.json")
    monkeypatch.setattr(models, "_projects_migrated", False)
    monkeypatch.setattr(models, "_PROJECTS_MIGRATION_LOCK", threading.Lock())

    out = models.load_projects()
    assert out[0]["profile"] == "default"


def test_load_projects_idempotent_after_first_migrate(tmp_path, monkeypatch):
    """Once everything is tagged, subsequent calls don't re-write the file."""
    import api.config as cfg
    import api.models as models

    projects_file = tmp_path / "projects.json"
    projects_file.write_text(
        json.dumps(
            [
                {
                    "project_id": "abc111",
                    "name": "My Project",
                    "profile": "haku",
                    "created_at": 1.0,
                },
            ]
        )
    )
    monkeypatch.setattr(cfg, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", False)
    monkeypatch.setattr(models, "_PROJECTS_MIGRATION_LOCK", threading.Lock())

    mtime_before = projects_file.stat().st_mtime_ns
    models.load_projects()
    models.load_projects()
    mtime_after = projects_file.stat().st_mtime_ns
    assert mtime_before == mtime_after, "No-op when everything already tagged"


# ── _profiles_match shape used by /api/projects ───────────────────────────


def test_profile_field_on_project_dict_default_create(monkeypatch):
    """A new project dict shape must include `profile` after create.

    We can't full-stack-test the HTTP path without spinning up a server, so
    instead we pin the file-level invariant: the create handler now stamps
    `profile` on the created dict.
    """
    src = (Path(__file__).parent.parent / "api" / "routes.py").read_text(
        encoding="utf-8"
    )

    # The create handler must now include get_active_profile_name() for the new dict
    create_idx = src.find('"/api/projects/create"')
    assert create_idx > 0
    next_handler_idx = src.find('"/api/projects/rename"', create_idx)
    create_block = src[create_idx:next_handler_idx]
    assert "\"profile\": get_active_profile_name() or 'default'" in create_block, (
        "Project create must stamp the active profile (#1614)"
    )


def test_project_rename_rejects_cross_profile():
    """Source-string check that rename's active-profile guard is in place."""
    src = (Path(__file__).parent.parent / "api" / "routes.py").read_text(
        encoding="utf-8"
    )

    rename_idx = src.find('"/api/projects/rename"')
    assert rename_idx > 0
    next_idx = src.find('"/api/projects/delete"', rename_idx)
    rename_block = src[rename_idx:next_idx]
    assert '_profiles_match(proj.get("profile"), active_profile)' in rename_block, (
        "Rename must check active-profile ownership"
    )


def test_project_delete_rejects_cross_profile():
    src = (Path(__file__).parent.parent / "api" / "routes.py").read_text(
        encoding="utf-8"
    )

    delete_idx = src.find('"/api/projects/delete"')
    assert delete_idx > 0
    delete_block = src[delete_idx : delete_idx + 1500]
    assert '_profiles_match(proj.get("profile"), active_profile)' in delete_block, (
        "Delete must check active-profile ownership"
    )


def test_session_move_rejects_cross_profile_project():
    """/api/session/move must refuse moves into a project from another profile."""
    src = (Path(__file__).parent.parent / "api" / "routes.py").read_text(
        encoding="utf-8"
    )

    move_idx = src.find('"/api/session/move"')
    assert move_idx > 0
    move_block = src[move_idx : move_idx + 2000]
    assert '_profiles_match(target.get("profile"), active_profile)' in move_block, (
        "session/move must check target project's active-profile ownership"
    )


# ── Cleanup ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_profile_state():
    import api.profiles as profiles
    import api.models as models

    profiles._invalidate_root_profile_cache()
    # Reset migration flag so each test starts fresh
    models._projects_migrated = False
    yield
    profiles._invalidate_root_profile_cache()
    models._projects_migrated = False
