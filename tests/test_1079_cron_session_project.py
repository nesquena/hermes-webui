"""Tests for #1079: Auto-assign cron job sessions to dedicated 'Cron Jobs' project."""

import json
import pathlib
import shutil
import tempfile

import pytest

from tests.conftest import TEST_STATE_DIR, _post, TEST_BASE

pytestmark = pytest.mark.usefixtures("test_server")


def _get_projects(base_url):
    """Fetch the project list from the API."""
    import urllib.request
    with urllib.request.urlopen(base_url + "/api/projects", timeout=5) as r:
        return json.loads(r.read())


def test_ensure_cron_project_creates_project():
    """ensure_cron_project() should create a 'Cron Jobs' project if none exists."""
    from api.models import ensure_cron_project, load_projects, save_projects

    # Remove any existing Cron Jobs project to test creation
    projects = load_projects()
    original = [p for p in projects if p.get('name') != 'Cron Jobs']
    save_projects(original)

    pid = ensure_cron_project()

    # Should now exist
    projects = load_projects()
    cron_projects = [p for p in projects if p.get('name') == 'Cron Jobs']
    assert len(cron_projects) == 1
    assert cron_projects[0]['project_id'] == pid
    assert cron_projects[0]['color'] == '#6366f1'
    assert len(pid) == 12

    # Restore
    save_projects(projects)


def test_ensure_cron_project_idempotent():
    """Calling ensure_cron_project() twice should return the same ID."""
    from api.models import ensure_cron_project, load_projects, save_projects

    projects = load_projects()
    save_projects([p for p in projects if p.get('name') != 'Cron Jobs'])

    pid1 = ensure_cron_project()
    pid2 = ensure_cron_project()
    assert pid1 == pid2


def test_is_cron_session():
    """is_cron_session should detect cron sessions by source_tag or ID prefix."""
    from api.models import is_cron_session

    # By source_tag
    assert is_cron_session("any_id", source_tag="cron") is True
    assert is_cron_session("any_id", source_tag="cli") is False

    # By session ID prefix
    assert is_cron_session("cron_abc123") is True
    assert is_cron_session("cron_") is True
    assert is_cron_session("regular_session") is False
    assert is_cron_session(None) is False
    assert is_cron_session("") is False


def test_cron_jobs_project_i18n_key_exists():
    """Every shipped locale must have the cron_jobs_project i18n key.

    Test was originally written for 8 locales; v0.50.264 added Japanese,
    bringing the count to 9. Use >= so future locale additions don't
    require touching this test.
    """
    i18n_path = pathlib.Path(__file__).resolve().parent.parent / "static" / "i18n.js"
    content = i18n_path.read_text(encoding="utf-8")

    # Count occurrences of cron_jobs_project
    count = content.count("cron_jobs_project:")
    assert count >= 9, f"Expected >= 9 locale entries for cron_jobs_project, found {count}"


def test_cron_session_gets_project_id_in_cli_list():
    """get_cli_sessions() should assign project_id for cron sessions."""
    from api.models import get_cli_sessions
    # Just verify the function is callable and returns a list
    # The actual project assignment is tested indirectly via integration
    sessions = get_cli_sessions()
    assert isinstance(sessions, list)
