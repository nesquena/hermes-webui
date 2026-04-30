import os
from pathlib import Path
import sys
import types

REPO_ROOT = Path(__file__).parent.parent.resolve()


def _read_static(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _extract_js_function(src: str, name: str) -> str:
    marker = f"function {name}("
    idx = src.find(marker)
    assert idx != -1, f"{name} not found"
    depth = 0
    for i, ch in enumerate(src[idx:], idx):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[idx:i + 1]
    raise AssertionError(f"Could not extract {name}")


def test_cron_listing_reads_each_profile_without_mutating_env(tmp_path, monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    default_home = tmp_path
    cronbot_home = tmp_path / "profiles" / "cronbot"
    cronbot_home.mkdir(parents=True)

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", default_home)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles._tls, "profile", None, raising=False)

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.HERMES_DIR = default_home
    cron_jobs.CRON_DIR = default_home / "cron"
    cron_jobs.JOBS_FILE = default_home / "cron" / "jobs.json"
    cron_jobs.OUTPUT_DIR = default_home / "cron" / "output"

    def list_jobs(include_disabled=False):
        return [{
            "id": cron_jobs.HERMES_DIR.name or "default",
            "name": f"job in {cron_jobs.HERMES_DIR}",
            "prompt": "hello",
            "enabled": True,
            "schedule_display": "every 1h",
        }]

    cron_jobs.list_jobs = list_jobs
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)
    monkeypatch.delitem(sys.modules, "cron.scheduler", raising=False)
    monkeypatch.setenv("HERMES_HOME", "/should/not/change")

    jobs = routes._list_cron_jobs_for_profiles(["default", "cronbot"])

    assert [job["profile"] for job in jobs] == ["default", "cronbot"]
    assert str(default_home) in jobs[0]["name"]
    assert str(cronbot_home) in jobs[1]["name"]
    assert os.environ["HERMES_HOME"] == "/should/not/change"
    assert cron_jobs.HERMES_DIR == default_home


def test_cron_profile_context_scopes_agent_cron_paths_without_mutating_env(tmp_path, monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    default_home = tmp_path
    foo_home = tmp_path / "profiles" / "foo"
    foo_home.mkdir(parents=True)

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", default_home)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setenv("HERMES_HOME", "/should/not/change")

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.HERMES_DIR = default_home
    cron_jobs.CRON_DIR = default_home / "cron"
    cron_jobs.JOBS_FILE = default_home / "cron" / "jobs.json"
    cron_jobs.OUTPUT_DIR = default_home / "cron" / "output"
    cron_scheduler = types.ModuleType("cron.scheduler")
    cron_scheduler._hermes_home = default_home
    cron_scheduler._LOCK_DIR = default_home / "cron"
    cron_scheduler._LOCK_FILE = default_home / "cron" / ".tick.lock"
    hermes_state = types.ModuleType("hermes_state")
    hermes_state.DEFAULT_DB_PATH = default_home / "state.db"

    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)
    monkeypatch.setitem(sys.modules, "cron.scheduler", cron_scheduler)
    monkeypatch.setitem(sys.modules, "hermes_state", hermes_state)

    with routes._cron_profile_context("foo", include_scheduler=True):
        assert cron_jobs.HERMES_DIR == foo_home
        assert cron_jobs.OUTPUT_DIR == foo_home / "cron" / "output"
        assert cron_scheduler._hermes_home == foo_home
        assert hermes_state.DEFAULT_DB_PATH == foo_home / "state.db"
        assert os.environ["HERMES_HOME"] == "/should/not/change"

    assert cron_jobs.HERMES_DIR == default_home
    assert cron_scheduler._hermes_home == default_home
    assert hermes_state.DEFAULT_DB_PATH == default_home / "state.db"
    assert os.environ["HERMES_HOME"] == "/should/not/change"


def test_cron_query_supports_profile_and_profiles_params():
    from urllib.parse import urlparse
    import api.routes as routes

    single = routes._cron_profiles_from_query(urlparse("/api/crons?profile=cronbot"))
    multi = routes._cron_profiles_from_query(urlparse("/api/crons?profiles=default,cronbot,default"))
    repeated = routes._cron_profiles_from_query(urlparse("/api/crons?profile=default&profile=cronbot"))
    combined = routes._cron_profiles_from_query(urlparse("/api/crons?profiles=default&profile=cronbot"))

    assert single == ["cronbot"]
    assert multi == ["default", "cronbot"]
    assert repeated == ["default", "cronbot"]
    assert combined == ["default", "cronbot"]


def test_cron_profile_jobs_and_cron_project_sessions_share_profile_scope(tmp_path, monkeypatch):
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes

    default_home = tmp_path
    foo_home = tmp_path / "profiles" / "foo"
    foo_home.mkdir(parents=True)
    (foo_home / "state.db").write_text("", encoding="utf-8")
    cron_dir = foo_home / "cron"
    cron_dir.mkdir()
    (cron_dir / "jobs.json").write_text(
        '{"jobs":[{"id":"foo-job","name":"Foo Cron","enabled":true}]}',
        encoding="utf-8",
    )

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", default_home)
    monkeypatch.setattr(profiles, "_active_profile", "foo")
    monkeypatch.setattr(profiles._tls, "profile", None, raising=False)
    monkeypatch.setattr(models, "PROJECTS_FILE", tmp_path / "projects.json")
    models.save_projects([])

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.HERMES_DIR = default_home
    cron_jobs.CRON_DIR = default_home / "cron"
    cron_jobs.JOBS_FILE = default_home / "cron" / "jobs.json"
    cron_jobs.OUTPUT_DIR = default_home / "cron" / "output"
    cron_jobs.list_jobs = lambda include_disabled=False: [
        {
            "id": "foo-job",
            "name": "Foo Cron",
            "enabled": True,
            "schedule_display": "every 1h",
        }
    ]
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)

    panel_jobs = routes._list_cron_jobs_for_profiles(["foo"])
    assert panel_jobs == [
        {
            "id": "foo-job",
            "name": "Foo Cron",
            "enabled": True,
            "schedule_display": "every 1h",
            "profile": "foo",
        }
    ]

    monkeypatch.setattr(
        models,
        "read_importable_agent_session_rows",
        lambda db_path, **kwargs: [
            {
                "id": "cron_foo-job_123",
                "last_activity": 20,
                "started_at": 10,
                "source": "cron",
                "title": None,
                "model": "test-model",
                "message_count": 2,
                "actual_message_count": 2,
                "raw_source": "cron",
                "session_source": "cron",
                "source_label": "Cron",
            }
        ],
    )

    sidebar_sessions = models.get_cli_sessions()
    assert len(sidebar_sessions) == 1
    assert sidebar_sessions[0]["title"] == "Foo Cron"
    assert sidebar_sessions[0]["profile"] == "foo"
    assert sidebar_sessions[0]["project_id"] == models.ensure_cron_project()


def test_visible_cron_profiles_fall_back_to_active_profile():
    fn = _extract_js_function(_read_static("static/panels.js"), "_cronVisibleProfiles")

    assert "const saved = _readCronVisibleProfiles()" in fn
    assert "if (saved.length) return saved;" in fn
    assert "const active = S.activeProfile || 'default';" in fn
    assert "return [active];" in fn
    assert "return ['default'];" in fn


def test_chat_actions_still_use_active_profile():
    commands = _read_static("static/commands.js")

    assert commands.count("profile:S.activeProfile||'default'") >= 3
