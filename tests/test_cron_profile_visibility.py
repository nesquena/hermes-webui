import os
import sys
import types


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
