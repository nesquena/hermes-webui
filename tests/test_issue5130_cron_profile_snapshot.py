"""Regression coverage for issue #5130 cron create-time profile snapshots."""

import io
import json
import sys
import types
from contextlib import contextmanager
from pathlib import Path

import pytest


class _JSONHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.response_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass


def _payload(handler):
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _install_fake_cron_modules(monkeypatch, cron_jobs):
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)


def test_cron_create_recomputes_unpinned_provider_snapshot_under_selected_profile(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    calls = []
    profile_events = []
    created = {
        "id": "job5130",
        "name": "Selected profile job",
        "prompt": "ping",
        "schedule": {"kind": "interval", "minutes": 60},
        "provider_snapshot": "openai-api",
    }

    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.JOBS_FILE = Path("C:/owning/cron/jobs.json")
    cron_jobs.CRON_DIR = Path("C:/owning/cron")
    cron_jobs.OUTPUT_DIR = Path("C:/owning/cron/output")

    def create_job(**kwargs):
        calls.append(("create", kwargs))
        return dict(created)

    def update_job(job_id, updates):
        calls.append(("update", job_id, updates))
        return {**created, **updates}

    def compute_snapshots(*, provider, model, base_url, no_agent):
        calls.append(
            (
                "compute",
                {
                    "provider": provider,
                    "model": model,
                    "base_url": base_url,
                    "no_agent": no_agent,
                },
            )
        )
        return "openai-codex", "gpt-5.4"

    @contextmanager
    def fake_profile_env(profile, purpose, logger_override=None):
        profile_events.append(("enter", profile, purpose, logger_override is routes.logger))
        yield
        profile_events.append(("exit", profile, purpose))

    cron_jobs.create_job = create_job
    cron_jobs.update_job = update_job
    cron_jobs._compute_provider_model_snapshots = compute_snapshots

    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [{"name": "research"}])
    monkeypatch.setattr(profiles, "profile_env_for_background_worker", fake_profile_env)
    _install_fake_cron_modules(monkeypatch, cron_jobs)

    handler = _JSONHandler()
    routes._handle_cron_create(
        handler,
        {
            "name": "Selected profile job",
            "prompt": "ping",
            "schedule": "every 60m",
            "deliver": "local",
            "profile": "research",
            "model": "gpt-5.4",
        },
    )

    body = _payload(handler)
    assert handler.status == 200
    assert body["ok"] is True
    assert calls[0] == (
        "create",
        {
            "prompt": "ping",
            "schedule": "every 60m",
            "name": "Selected profile job",
            "deliver": "local",
            "skills": [],
            "model": "gpt-5.4",
            "provider": None,
        },
    )
    assert calls[1] == (
        "compute",
        {
            "provider": None,
            "model": "gpt-5.4",
            "base_url": None,
            "no_agent": False,
        },
    )
    assert calls[2] == (
        "update",
        "job5130",
        {"profile": "research", "provider_snapshot": "openai-codex"},
    )
    assert profile_events == [
        ("enter", "research", "cron create snapshot", True),
        ("exit", "research", "cron create snapshot"),
    ]


def test_cron_create_with_blank_profile_keeps_ambient_snapshot_semantics(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    calls = []
    profile_events = []
    created = {
        "id": "job5130",
        "name": "Ambient job",
        "prompt": "ping",
        "schedule": {"kind": "interval", "minutes": 60},
        "provider_snapshot": "openai-api",
    }

    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.create_job = lambda **kwargs: calls.append(("create", kwargs)) or dict(created)
    cron_jobs.update_job = lambda *args, **kwargs: pytest.fail("blank profile should not update the job")
    cron_jobs._compute_provider_model_snapshots = (
        lambda **kwargs: pytest.fail("blank profile should not recompute snapshots")
    )

    @contextmanager
    def fake_profile_env(*args, **kwargs):
        profile_events.append(("enter", args, kwargs))
        yield

    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [{"name": "research"}])
    monkeypatch.setattr(profiles, "profile_env_for_background_worker", fake_profile_env)
    _install_fake_cron_modules(monkeypatch, cron_jobs)

    handler = _JSONHandler()
    routes._handle_cron_create(
        handler,
        {
            "name": "Ambient job",
            "prompt": "ping",
            "schedule": "every 60m",
            "deliver": "local",
            "profile": "",
            "model": "gpt-5.4",
        },
    )

    body = _payload(handler)
    assert handler.status == 200
    assert body["ok"] is True
    assert body["job"]["provider_snapshot"] == "openai-api"
    assert calls == [
        (
            "create",
            {
                "prompt": "ping",
                "schedule": "every 60m",
                "name": "Ambient job",
                "deliver": "local",
                "skills": [],
                "model": "gpt-5.4",
                "provider": None,
            },
        )
    ]
    assert profile_events == []


def test_cron_create_with_explicit_provider_and_model_skips_snapshot_override(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    calls = []
    created = {
        "id": "job5130",
        "name": "Pinned job",
        "prompt": "ping",
        "schedule": {"kind": "interval", "minutes": 60},
        "provider_snapshot": "openai-codex",
        "model_snapshot": "gpt-5.4",
    }

    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.create_job = lambda **kwargs: calls.append(("create", kwargs)) or dict(created)
    cron_jobs.update_job = lambda job_id, updates: calls.append(("update", job_id, updates)) or {
        **created,
        **updates,
    }
    cron_jobs._compute_provider_model_snapshots = (
        lambda **kwargs: pytest.fail("explicit provider/model should not recompute snapshots")
    )

    @contextmanager
    def fake_profile_env(*args, **kwargs):
        pytest.fail("explicit provider/model should not enter selected-profile snapshot context")
        yield

    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [{"name": "research"}])
    monkeypatch.setattr(profiles, "profile_env_for_background_worker", fake_profile_env)
    _install_fake_cron_modules(monkeypatch, cron_jobs)

    handler = _JSONHandler()
    routes._handle_cron_create(
        handler,
        {
            "name": "Pinned job",
            "prompt": "ping",
            "schedule": "every 60m",
            "deliver": "local",
            "profile": "research",
            "provider": "openai-codex",
            "model": "gpt-5.4",
        },
    )

    body = _payload(handler)
    assert handler.status == 200
    assert body["ok"] is True
    assert calls == [
        (
            "create",
            {
                "prompt": "ping",
                "schedule": "every 60m",
                "name": "Pinned job",
                "deliver": "local",
                "skills": [],
                "model": "gpt-5.4",
                "provider": "openai-codex",
            },
        ),
        ("update", "job5130", {"profile": "research"}),
    ]


def test_cron_create_with_explicit_provider_recomputes_only_model_snapshot(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    calls = []
    profile_events = []
    created = {
        "id": "job5130",
        "name": "Pinned provider job",
        "prompt": "ping",
        "schedule": {"kind": "interval", "minutes": 60},
        "provider": "openai-codex",
        "provider_snapshot": None,
        "model_snapshot": "gpt-5.4",
    }

    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.create_job = lambda **kwargs: calls.append(("create", kwargs)) or dict(created)
    cron_jobs.update_job = lambda job_id, updates: calls.append(("update", job_id, updates)) or {
        **created,
        **updates,
    }

    def compute_snapshots(*, provider, model, base_url, no_agent):
        calls.append(
            (
                "compute",
                {
                    "provider": provider,
                    "model": model,
                    "base_url": base_url,
                    "no_agent": no_agent,
                },
            )
        )
        return None, "gpt-5.5"

    @contextmanager
    def fake_profile_env(profile, purpose, logger_override=None):
        profile_events.append(("enter", profile, purpose, logger_override is routes.logger))
        yield
        profile_events.append(("exit", profile, purpose))

    cron_jobs._compute_provider_model_snapshots = compute_snapshots

    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [{"name": "research"}])
    monkeypatch.setattr(profiles, "profile_env_for_background_worker", fake_profile_env)
    _install_fake_cron_modules(monkeypatch, cron_jobs)

    handler = _JSONHandler()
    routes._handle_cron_create(
        handler,
        {
            "name": "Pinned provider job",
            "prompt": "ping",
            "schedule": "every 60m",
            "deliver": "local",
            "profile": "research",
            "provider": "openai-codex",
        },
    )

    body = _payload(handler)
    assert handler.status == 200
    assert body["ok"] is True
    assert calls == [
        (
            "create",
            {
                "prompt": "ping",
                "schedule": "every 60m",
                "name": "Pinned provider job",
                "deliver": "local",
                "skills": [],
                "model": None,
                "provider": "openai-codex",
            },
        ),
        (
            "compute",
            {
                "provider": "openai-codex",
                "model": None,
                "base_url": None,
                "no_agent": False,
            },
        ),
        ("update", "job5130", {"profile": "research", "model_snapshot": "gpt-5.5"}),
    ]
    assert profile_events == [
        ("enter", "research", "cron create snapshot", True),
        ("exit", "research", "cron create snapshot"),
    ]


def test_selected_profile_snapshot_helper_never_repoints_cron_store_globals(monkeypatch):
    import api.profiles as profiles
    from api.routes import _selected_profile_snapshot_updates

    profile_events = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.JOBS_FILE = Path("C:/owning/cron/jobs.json")
    cron_jobs.CRON_DIR = Path("C:/owning/cron")
    cron_jobs.OUTPUT_DIR = Path("C:/owning/cron/output")
    original_paths = (
        cron_jobs.JOBS_FILE,
        cron_jobs.CRON_DIR,
        cron_jobs.OUTPUT_DIR,
    )

    def compute_snapshots(*, provider, model, base_url, no_agent):
        profile_events.append(
            (
                "compute",
                cron_jobs.JOBS_FILE,
                cron_jobs.CRON_DIR,
                cron_jobs.OUTPUT_DIR,
                provider,
                model,
                base_url,
                no_agent,
            )
        )
        return "openai-codex", "gpt-5.4"

    @contextmanager
    def fake_profile_env(profile, purpose, logger_override=None):
        profile_events.append(
            ("enter", profile, purpose, cron_jobs.JOBS_FILE, cron_jobs.CRON_DIR, cron_jobs.OUTPUT_DIR)
        )
        yield
        profile_events.append(
            ("exit", profile, purpose, cron_jobs.JOBS_FILE, cron_jobs.CRON_DIR, cron_jobs.OUTPUT_DIR)
        )

    cron_jobs._compute_provider_model_snapshots = compute_snapshots

    monkeypatch.setattr(profiles, "profile_env_for_background_worker", fake_profile_env)
    _install_fake_cron_modules(monkeypatch, cron_jobs)

    updates = _selected_profile_snapshot_updates(
        "research",
        provider=None,
        model=None,
    )

    assert updates == {
        "provider_snapshot": "openai-codex",
        "model_snapshot": "gpt-5.4",
    }
    assert (cron_jobs.JOBS_FILE, cron_jobs.CRON_DIR, cron_jobs.OUTPUT_DIR) == original_paths
    assert profile_events == [
        (
            "enter",
            "research",
            "cron create snapshot",
            original_paths[0],
            original_paths[1],
            original_paths[2],
        ),
        (
            "compute",
            original_paths[0],
            original_paths[1],
            original_paths[2],
            None,
            None,
            None,
            False,
        ),
        (
            "exit",
            "research",
            "cron create snapshot",
            original_paths[0],
            original_paths[1],
            original_paths[2],
        ),
    ]


def test_selected_profile_snapshot_helper_holds_lock_across_profile_env_and_compute(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    events = []
    cron_jobs = types.ModuleType("cron.jobs")

    class RecorderLock:
        def __enter__(self):
            events.append("lock-enter")

        def __exit__(self, exc_type, exc, tb):
            events.append("lock-exit")

    def compute_snapshots(*, provider, model, base_url, no_agent):
        events.append(("compute", provider, model, base_url, no_agent))
        return "openai-codex", "gpt-5.4"

    @contextmanager
    def fake_profile_env(profile, purpose, logger_override=None):
        events.append(("profile-enter", profile, purpose, logger_override is routes.logger))
        yield
        events.append(("profile-exit", profile, purpose))

    cron_jobs._compute_provider_model_snapshots = compute_snapshots

    monkeypatch.setattr(routes, "_CRON_CREATE_SNAPSHOT_LOCK", RecorderLock())
    monkeypatch.setattr(profiles, "profile_env_for_background_worker", fake_profile_env)
    _install_fake_cron_modules(monkeypatch, cron_jobs)

    updates = routes._selected_profile_snapshot_updates(
        "research",
        provider=None,
        model="gpt-5.4",
    )

    assert updates == {"provider_snapshot": "openai-codex"}
    assert events == [
        "lock-enter",
        ("profile-enter", "research", "cron create snapshot", True),
        ("compute", None, "gpt-5.4", None, False),
        ("profile-exit", "research", "cron create snapshot"),
        "lock-exit",
    ]
