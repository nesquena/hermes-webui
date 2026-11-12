"""#7295 / #6753: /api/crons* must not wait on the global cron env lock.

A long in-process cron job (scheduled fire-claim run via the WebUI
``run_job`` isolation wrapper, #7295, or the in-chat ``cronjob action=run``
handler, #6753) holds ``api.profiles._cron_env_lock`` and keeps the process
globals (``os.environ['HERMES_HOME']``, ``cron.jobs`` path constants,
``cron.scheduler._hermes_home``) pointed at its own profile for the whole run.

These tests hold that context in a real background thread and drive the real
route dispatchers for a DIFFERENT profile and for the SAME profile.  Every
converted endpoint must answer promptly with its own profile's data and must
not disturb the running job's globals.  ``/api/crons/create`` deliberately
stays on the lock (its selected-profile snapshot repair mutates process env),
so it is pinned as still waiting.  Older Agents without the per-thread helpers
fall back to the lock and stay correct.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

_collected_cron_jobs = pytest.importorskip("cron.jobs")  # needs the installed Agent

import api.profiles as profiles  # noqa: E402
import api.routes as routes  # noqa: E402


def _cron_jobs():
    """The live ``cron.jobs`` module, resolved at use time like the product does.

    Product code imports ``cron.jobs`` inside each call, so patching or reading
    the object this file bound at collection time would target a stale module
    if anything re-imported ``cron`` in between.
    """
    return importlib.import_module("cron.jobs")

JOB_ID = "a1b2c3d4e5f6"  # identical id in both profiles: wrong-store reads are visible
RUN_FILE = "2026-10-01_00-00-00.md"
PROMPT_DEADLINE_S = 3.0
HOLD_MAX_S = 60.0

_HAS_PER_THREAD_HELPERS = callable(getattr(_collected_cron_jobs, "use_cron_store", None)) and callable(
    getattr(__import__("hermes_constants"), "set_hermes_home_override", None)
)
requires_per_thread_helpers = pytest.mark.skipif(
    not _HAS_PER_THREAD_HELPERS,
    reason="installed Agent predates cron.jobs.use_cron_store/set_hermes_home_override",
)


def _write_profile(home: Path, label: str) -> None:
    cron_dir = home / "cron"
    (cron_dir / "output" / JOB_ID).mkdir(parents=True, exist_ok=True)
    job = {
        "id": JOB_ID,
        "name": f"{label}-job",
        "prompt": f"{label} prompt",
        "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"},
        "schedule_display": "every 60m",
        "enabled": True,
        "state": "scheduled",
        "repeat": {"times": None, "completed": 1},
        "next_run_at": "2099-01-01T00:00:00+00:00",
        "last_run_at": "2026-10-01T00:00:00+00:00",
        "last_status": "ok",
        "deliver": "local",
        "profile": label if label != "default" else None,
    }
    (cron_dir / "jobs.json").write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    (cron_dir / "output" / JOB_ID / RUN_FILE).write_text(
        f"# Cron Job\n\n**Model:** {label}-model\n\n## Response\nOUTPUT-FROM-{label}\n",
        encoding="utf-8",
    )


def _jobs_on_disk(home: Path) -> list[dict]:
    return json.loads((home / "cron" / "jobs.json").read_text(encoding="utf-8"))["jobs"]


class _Handler:
    def __init__(self, body=None, path="/"):
        raw = json.dumps(body or {}).encode("utf-8")
        self.status = None
        self.response_headers = {}
        self.headers = {"Content-Length": str(len(raw)), "Content-Type": "application/json"}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.command = "POST" if body is not None else "GET"
        self.path = path
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def payload(self):
        raw = self.wfile.getvalue()
        return json.loads(raw.decode("utf-8")) if raw else {}


@pytest.fixture
def world(tmp_path, monkeypatch):
    base = (tmp_path / "base").resolve()
    alpha = base / "profiles" / "alpha"
    beta = base / "profiles" / "beta"
    for home, label in ((base, "default"), (alpha, "alpha"), (beta, "beta")):
        _write_profile(home, label)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: False)
    rows = [{"name": "default", "visible": True}, {"name": "alpha", "visible": True}, {"name": "beta", "visible": True}]
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [dict(r) for r in rows])
    monkeypatch.setattr(routes, "list_profiles_api", lambda: [dict(r) for r in rows])
    monkeypatch.setattr(profiles, "publish_session_list_changed", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_latest_cron_session_info_for_jobs", lambda *a, **k: {})
    # The process default points at the server-default profile, as in production.
    monkeypatch.setenv("HERMES_HOME", str(base))
    cron_jobs = _cron_jobs()
    for name, value in (
        ("HERMES_DIR", base),
        ("CRON_DIR", base / "cron"),
        ("JOBS_FILE", base / "cron" / "jobs.json"),
        ("OUTPUT_DIR", base / "cron" / "output"),
    ):
        monkeypatch.setattr(cron_jobs, name, value)
    try:
        import cron.scheduler as cron_scheduler
    except Exception:  # pragma: no cover - Agent without scheduler
        cron_scheduler = None
    if cron_scheduler is not None:
        monkeypatch.setattr(cron_scheduler, "_hermes_home", base, raising=False)
    assert not profiles._cron_env_lock.locked(), "cron env lock leaked from an earlier test"
    return {"base": base, "alpha": alpha, "beta": beta, "scheduler": cron_scheduler}


class _Holder:
    """Run a real long cron job body in the background holding the cron context."""

    def __init__(self, start_fn):
        self.started = threading.Event()
        self.release = threading.Event()
        self.observed = {}
        self.error = None
        self._start_fn = start_fn
        self.thread = threading.Thread(target=self._run, daemon=True)

    def body(self, *args, **kwargs):
        self.observed["env_home"] = os.environ.get("HERMES_HOME")
        self.observed["jobs_file"] = _cron_jobs().JOBS_FILE
        self.started.set()
        self.release.wait(HOLD_MAX_S)
        # Globals must still be this job's after concurrent readers ran.
        self.observed["env_home_after"] = os.environ.get("HERMES_HOME")
        self.observed["jobs_file_after"] = _cron_jobs().JOBS_FILE
        return "done"

    def _run(self):
        try:
            self._start_fn(self)
        except BaseException as exc:  # pragma: no cover - surfaced in asserts
            self.error = exc

    def __enter__(self):
        self.thread.start()
        assert self.started.wait(10), "slow job never entered its cron context"
        assert profiles._cron_env_lock.locked(), "slow job is not holding the cron env lock"
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(15)
        assert not self.thread.is_alive()
        return False


def _scheduled_fire_holder(world, monkeypatch, profile_home):
    """#7295: scheduled fire-claim path through the WebUI run_job isolation wrapper."""
    cron_scheduler = world["scheduler"]
    if cron_scheduler is None:
        pytest.skip("cron.scheduler unavailable")

    def start(holder):
        monkeypatch.setattr(cron_scheduler, "run_job", holder.body)
        profiles.install_cron_scheduler_profile_isolation()
        wrapped = cron_scheduler.run_job
        assert getattr(wrapped, "_webui_profile_isolated", False)
        wrapped({"id": JOB_ID, "profile": profile_home.name})

    return _Holder(start)


def _in_chat_run_holder(world, monkeypatch, profile_home):
    """#6753: in-chat cronjob(action='run') through the streaming profile wrapper."""
    import api.streaming as streaming

    def start(holder):
        wrapper = streaming._streaming_cronjob_profile_handler(lambda args, **kw: holder.body())
        token = streaming._STREAMING_CRON_PROFILE_HOME.set(str(profile_home))
        try:
            wrapper({"action": "run", "job_id": JOB_ID})
        finally:
            streaming._STREAMING_CRON_PROFILE_HOME.reset(token)

    return _Holder(start)


HOLDERS = {"scheduled_fire_7295": _scheduled_fire_holder, "in_chat_run_6753": _in_chat_run_holder}


def _request(method, path, profile, body=None, deadline=PROMPT_DEADLINE_S):
    """Dispatch one request on a fresh server-like thread; return (elapsed, status, payload)."""
    out = {}

    def run():
        profiles.set_request_profile(profile)
        try:
            handler = _Handler(body=body if method == "POST" else None, path=path)
            parsed = urlparse(path)
            start = time.monotonic()
            if method == "GET":
                routes.handle_get(handler, parsed)
            else:
                routes.handle_post(handler, parsed)
            out["elapsed"] = time.monotonic() - start
            out["status"] = handler.status
            out["payload"] = handler.payload()
        except BaseException as exc:  # pragma: no cover - surfaced below
            out["error"] = exc
        finally:
            profiles.clear_request_profile()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(deadline)
    return t, out


def _finish(t, out):
    t.join(HOLD_MAX_S)
    if "error" in out:
        raise out["error"]
    return out


def _assert_job_untouched(world, profile):
    jobs = _jobs_on_disk(world[profile])
    assert [j["name"] for j in jobs] == [f"{profile}-job"], (profile, jobs)
    assert jobs[0].get("state") == "scheduled", (profile, jobs)


GET_READERS = [
    ("/api/crons", lambda body, me: [j["name"] for j in body["jobs"]] == [f"{me}-job"]),
    ("/api/crons?all_profiles=1", lambda body, me: sorted((j["owner_profile"], j["name"]) for j in body["jobs"])
     == sorted([("default", "default-job"), ("alpha", "alpha-job"), ("beta", "beta-job")])),
    ("/api/crons/recent?since=0", lambda body, me: [(c["job_id"], c["name"]) for c in body["completions"]]
     == [(JOB_ID, f"{me}-job")]),
    (f"/api/crons/output?job_id={JOB_ID}", lambda body, me: len(body["outputs"]) == 1
     and f"OUTPUT-FROM-{me}" in body["outputs"][0]["content"]),
    (f"/api/crons/history?job_id={JOB_ID}", lambda body, me: [r["filename"] for r in body["runs"]] == [RUN_FILE]
     and body["runs"][0]["usage"].get("model") == f"{me}-model"),
    (f"/api/crons/run?job_id={JOB_ID}&filename={RUN_FILE}", lambda body, me: f"OUTPUT-FROM-{me}" in body["content"]),
    ("/api/crons/status", lambda body, me: "running" in body),
    ("/api/crons/delivery-options", lambda body, me: any(p["value"] == "local" for p in body["platforms"])),
]


@requires_per_thread_helpers
@pytest.mark.parametrize("holder_kind", sorted(HOLDERS))
@pytest.mark.parametrize("reader_profile", ["beta", "alpha"])
def test_cron_readers_answer_promptly_with_own_profile_while_job_holds_lock(
    world, monkeypatch, holder_kind, reader_profile
):
    results = []
    with HOLDERS[holder_kind](world, monkeypatch, world["alpha"]) as holder:
        for path, check in GET_READERS:
            t, out = _request("GET", path, reader_profile)
            results.append((path, t.is_alive(), t, out, check))
        # Globals still belong to the running job while readers ran.
        assert os.environ.get("HERMES_HOME") == str(world["alpha"])
        assert _cron_jobs().JOBS_FILE == world["alpha"] / "cron" / "jobs.json"
        blocked = [path for path, alive, *_ in results if alive]
    for path, _alive, t, out, check in results:
        _finish(t, out)
    assert blocked == [], f"readers waited on the running cron job's lock: {blocked}"
    assert holder.error is None
    assert holder.observed["env_home"] == holder.observed["env_home_after"] == str(world["alpha"])
    assert holder.observed["jobs_file"] == holder.observed["jobs_file_after"]
    for path, _alive, _t, out, check in results:
        assert out["status"] == 200, (path, out)
        assert check(out["payload"], reader_profile), (path, out["payload"])
    _assert_job_untouched(world, "alpha")
    _assert_job_untouched(world, "beta")


POST_WRITERS = [
    ("/api/crons/update", {"job_id": JOB_ID, "name": "beta-renamed"},
     lambda jobs: jobs[0]["name"] == "beta-renamed"),
    ("/api/crons/pause", {"job_id": JOB_ID, "reason": "t7295"},
     lambda jobs: jobs[0]["state"] == "paused"),
    ("/api/crons/resume", {"job_id": JOB_ID},
     lambda jobs: jobs[0]["state"] == "scheduled"),
    ("/api/crons/run", {"job_id": JOB_ID}, lambda jobs: jobs[0]["name"] == "beta-renamed"),
    ("/api/crons/delete", {"job_id": JOB_ID}, lambda jobs: jobs == []),
]


@requires_per_thread_helpers
@pytest.mark.parametrize("holder_kind", sorted(HOLDERS))
def test_cron_store_writers_do_not_wait_and_only_touch_own_profile(world, monkeypatch, holder_kind):
    launched = []
    monkeypatch.setattr(routes, "_RUNNING_CRON_JOBS", {})
    monkeypatch.setattr(routes, "_run_cron_tracked", lambda *a, **k: launched.append(a))
    with HOLDERS[holder_kind](world, monkeypatch, world["alpha"]) as holder:
        for path, body, check in POST_WRITERS:
            t, out = _request("POST", path, "beta", body=body)
            alive = t.is_alive()
            _finish(t, out)
            assert not alive, f"{path} waited on the running cron job's lock"
            assert out["status"] == 200, (path, out)
            assert out["payload"].get("ok") is True, (path, out)
            assert check(_jobs_on_disk(world["beta"])), (path, _jobs_on_disk(world["beta"]))
            _assert_job_untouched(world, "alpha")
        assert os.environ.get("HERMES_HOME") == str(world["alpha"])
    assert holder.error is None
    assert holder.observed["env_home_after"] == str(world["alpha"])
    assert len(launched) == 1
    job, owner_home, execution_home, _event_profile = launched[0]
    assert job["name"] == "beta-renamed"
    assert Path(owner_home) == world["beta"] and Path(execution_home) == world["beta"]
    _assert_job_untouched(world, "alpha")


@pytest.mark.parametrize("holder_kind", sorted(HOLDERS))
def test_cron_create_still_serializes_on_the_lock(world, monkeypatch, holder_kind):
    """Create keeps the lock: its selected-profile snapshot repair mutates os.environ."""
    with HOLDERS[holder_kind](world, monkeypatch, world["alpha"]):
        t, out = _request(
            "POST", "/api/crons/create", "beta",
            body={"prompt": "new beta job", "schedule": "every 2h", "name": "beta-created"},
            deadline=0.5,
        )
        assert t.is_alive(), "create unexpectedly bypassed the cron env lock"
    _finish(t, out)
    assert out["status"] == 200, out
    assert sorted(j["name"] for j in _jobs_on_disk(world["beta"])) == ["beta-created", "beta-job"]
    _assert_job_untouched(world, "alpha")


@requires_per_thread_helpers
def test_reader_scope_resets_per_thread_overrides_on_reused_threads(world):
    """Tokens are reset in finally: a reused server thread never inherits a prior profile."""
    import hermes_constants
    from concurrent.futures import ThreadPoolExecutor

    store_var = getattr(_cron_jobs(), "_cron_store_override", None)

    def serve(profile, path):
        profiles.set_request_profile(profile)
        try:
            handler = _Handler(path=path)
            routes.handle_get(handler, urlparse(path))
            return handler.status, handler.payload(), hermes_constants.get_hermes_home_override(), (
                store_var.get() if store_var is not None else None)
        finally:
            profiles.clear_request_profile()

    def boom(profile):
        profiles.set_request_profile(profile)
        try:
            with profiles.cron_store_scope():
                raise RuntimeError("handler failure")
        except RuntimeError:
            return hermes_constants.get_hermes_home_override(), (
                store_var.get() if store_var is not None else None)
        finally:
            profiles.clear_request_profile()

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(serve, "alpha", f"/api/crons/output?job_id={JOB_ID}").result()
        failed = pool.submit(boom, "alpha").result()
        second = pool.submit(serve, "beta", f"/api/crons/output?job_id={JOB_ID}").result()
        third = pool.submit(serve, "default", "/api/crons").result()
    assert "OUTPUT-FROM-alpha" in first[1]["outputs"][0]["content"]
    assert "OUTPUT-FROM-beta" in second[1]["outputs"][0]["content"]
    assert [j["name"] for j in third[1]["jobs"]] == ["default-job"]
    for leftover in (first[2:], failed, second[2:], third[2:]):
        assert leftover == (None, None), leftover


def _hide_per_thread_helpers(monkeypatch, which):
    if which == "use_cron_store":
        monkeypatch.delattr(_cron_jobs(), "use_cron_store", raising=False)
    else:
        import hermes_constants

        monkeypatch.delattr(hermes_constants, "set_hermes_home_override", raising=False)
        monkeypatch.setattr(profiles, "_hermes_home_override_available", None, raising=False)


@pytest.mark.parametrize("missing", ["use_cron_store", "set_hermes_home_override"])
def test_older_agent_without_helpers_falls_back_to_lock_and_stays_correct(world, monkeypatch, missing):
    _hide_per_thread_helpers(monkeypatch, missing)
    # No holder: lock-based fallback still reads the requesting profile.
    t, out = _request("GET", f"/api/crons/output?job_id={JOB_ID}", "beta")
    _finish(t, out)
    assert "OUTPUT-FROM-beta" in out["payload"]["outputs"][0]["content"]
    t, out = _request("GET", "/api/crons", "alpha")
    _finish(t, out)
    assert [j["name"] for j in out["payload"]["jobs"]] == ["alpha-job"]
    # With a running job holding the lock the fallback waits (master behavior)
    # and then answers with the requesting profile's data, not the job's.
    with _scheduled_fire_holder(world, monkeypatch, world["alpha"]):
        t, out = _request("GET", f"/api/crons/history?job_id={JOB_ID}", "beta", deadline=0.5)
        assert t.is_alive(), "older-Agent fallback must keep serializing on the cron env lock"
    _finish(t, out)
    assert out["payload"]["runs"][0]["usage"]["model"] == "beta-model"
    assert not profiles._cron_env_lock.locked()
    assert os.environ.get("HERMES_HOME") == str(world["base"])
    assert _cron_jobs().JOBS_FILE == world["base"] / "cron" / "jobs.json"


def test_concurrent_manual_runs_admit_exactly_one_worker(world, monkeypatch):
    """Without the outer env lock, Run admission itself must be atomic.

    Two simultaneous Run requests for one job used to both pass the separate
    "already running?" check before either marked the job, launching two
    workers. Any separate mark step is held until both requests reach it, which
    reproduces that interleaving deterministically.
    """
    launched = []
    monkeypatch.setattr(routes, "_RUNNING_CRON_JOBS", {})
    monkeypatch.setattr(routes, "_run_cron_tracked", lambda *a, **k: launched.append(a))
    both_checked = threading.Barrier(2)
    real_mark = routes._mark_cron_running

    def mark_after_both_checked(job_id):
        try:
            both_checked.wait(2.0)
        except threading.BrokenBarrierError:
            pass
        real_mark(job_id)

    monkeypatch.setattr(routes, "_mark_cron_running", mark_after_both_checked)
    requests = [
        _request("POST", "/api/crons/run", "beta", body={"job_id": JOB_ID}, deadline=0)
        for _ in range(2)
    ]
    outs = [_finish(t, out) for t, out in requests]
    assert [o["status"] for o in outs] == [200, 200], outs
    assert sorted(o["payload"].get("status") for o in outs) == ["already_running", "running"], outs
    assert len(launched) == 1, launched


def test_resume_racing_edit_keeps_next_run_on_the_saved_schedule(world, monkeypatch):
    """Resume reads the schedule and computes next_run_at before writing.

    An Edit landing in that window must not leave the new schedule paired with
    the old next_run_at (a one-shot moved from Jan to Feb would fire in Jan).
    Resume is paused right after its computation; the Edit is issued then.
    """
    jan, feb = "2099-01-01T00:00:00+00:00", "2099-02-01T00:00:00+00:00"
    path = world["beta"] / "cron" / "jobs.json"
    job = _jobs_on_disk(world["beta"])[0]
    job.update({
        "schedule": {"kind": "once", "run_at": jan, "display": "once at 2099-01-01 00:00"},
        "schedule_display": "once at 2099-01-01 00:00",
        "enabled": False,
        "state": "paused",
        "paused_at": "2026-10-01T00:00:00+00:00",
        "next_run_at": jan,
        "repeat": {"times": 1, "completed": 0},
    })
    path.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")

    live = _cron_jobs()
    real_compute = live.compute_next_run
    computed, release = threading.Event(), threading.Event()
    calls = []

    def compute_then_pause_first_caller(*args, **kwargs):
        value = real_compute(*args, **kwargs)
        calls.append(value)
        if len(calls) == 1:
            computed.set()
            release.wait(HOLD_MAX_S)
        return value

    monkeypatch.setattr(live, "compute_next_run", compute_then_pause_first_caller)
    resume_t, resume_out = _request("POST", "/api/crons/resume", "beta", body={"job_id": JOB_ID}, deadline=0)
    assert computed.wait(10), "resume never computed its next run"
    edit_t, edit_out = _request(
        "POST", "/api/crons/update", "beta", body={"job_id": JOB_ID, "schedule": feb}, deadline=1.0
    )
    release.set()
    _finish(resume_t, resume_out)
    _finish(edit_t, edit_out)
    assert resume_out["status"] == 200 and edit_out["status"] == 200, (resume_out, edit_out)

    from datetime import datetime

    saved = _jobs_on_disk(world["beta"])[0]
    assert datetime.fromisoformat(saved["schedule"]["run_at"]) == datetime.fromisoformat(feb), saved
    assert saved["state"] == "scheduled" and saved["enabled"] is True, saved
    assert datetime.fromisoformat(saved["next_run_at"]) == datetime.fromisoformat(feb), saved
    _assert_job_untouched(world, "alpha")


def test_lock_free_cron_store_scope_requires_the_agent_jobs_lock(world, monkeypatch):
    """Without the Agent's jobs.json lock nothing would serialize Resume against
    Edit, so the per-thread scope must fall back to the locked cron context."""
    assert profiles._cron_store_scope_helpers() is not None or not _HAS_PER_THREAD_HELPERS
    monkeypatch.setattr(_cron_jobs(), "_jobs_lock", None)
    assert profiles._cron_store_scope_helpers() is None


@pytest.mark.parametrize("holder_kind", [None, "scheduled_fire_7295"])
def test_model_pin_resolves_config_against_the_requesting_profile(world, monkeypatch, holder_kind):
    """pinned=True without a model reads the profile's config.yaml.

    Config ``${VAR}`` expansion reads the process environment, so the pin
    update must run with the process env pointed at the requesting profile
    (the locked cron context), never at the default or a running job's profile.
    """
    (world["beta"] / "config.yaml").write_text("model:\n  default: ${HERMES_HOME}/beta-model\n", encoding="utf-8")
    body = {"job_id": JOB_ID, "pinned": True}
    if holder_kind is None:
        t, out = _request("POST", "/api/crons/update", "beta", body=body)
        serialized = True
    else:
        with HOLDERS[holder_kind](world, monkeypatch, world["alpha"]):
            t, out = _request("POST", "/api/crons/update", "beta", body=body, deadline=0.5)
            serialized = t.is_alive()
    _finish(t, out)
    assert out["status"] == 200, out
    saved = _jobs_on_disk(world["beta"])[0]
    assert saved.get("model") == f"{world['beta']}/beta-model", saved
    assert serialized, "pin-resolving update must wait for the running job's cron env lock"
    assert os.environ.get("HERMES_HOME") == str(world["base"])
    _assert_job_untouched(world, "alpha")


def _create_body(**extra):
    body = {"prompt": "fixture-only", "schedule": "every 1h", "name": "create-race", "profile": "alpha"}
    body.update(extra)
    return body


@pytest.fixture
def no_dispatch(monkeypatch):
    """Record Run dispatches instead of starting workers; keep Create offline."""
    launched = []
    monkeypatch.setattr(routes, "_RUNNING_CRON_JOBS", {})
    monkeypatch.setattr(routes, "_run_cron_tracked", lambda *a, **k: launched.append(a))
    # Selected-profile provider/model snapshot repair needs a real provider config.
    monkeypatch.setattr(routes, "_selected_profile_snapshot_updates", lambda *a, **k: {})
    return launched


@requires_per_thread_helpers
def test_create_two_step_write_is_never_visible_to_list_or_run(world, monkeypatch, no_dispatch):
    """Codex r2 CORE: Create persists a job in two writes.

    ``create_job()`` has no ``profile`` parameter, so the selected execution
    profile lands in a follow-up ``update_job()``. List and Run no longer wait
    on Create's cron env lock; if they read between the two writes they see
    ``profile: null`` and Run dispatches the job to the requesting profile
    instead of the selected one. Create is paused right after its first write.
    """
    live = _cron_jobs()
    real_create = live.create_job
    between, release = threading.Event(), threading.Event()
    created = {}

    def create_then_pause(*args, **kwargs):
        job = real_create(*args, **kwargs)
        created["id"] = job["id"]
        between.set()
        release.wait(HOLD_MAX_S)
        return job

    monkeypatch.setattr(live, "create_job", create_then_pause)
    create_t, create_out = _request("POST", "/api/crons/create", "beta", body=_create_body(), deadline=0)
    try:
        assert between.wait(10), "create never reached its first write"
        # The intermediate record really is on disk: this is the state readers must not see.
        partial = [j for j in _jobs_on_disk(world["beta"]) if j["id"] == created["id"]]
        assert partial and partial[0].get("profile") is None, partial
        list_t, list_out = _request("GET", "/api/crons", "beta", deadline=0.3)
        run_t, run_out = _request("POST", "/api/crons/run", "beta", body={"job_id": created["id"]}, deadline=0.3)
    finally:
        release.set()
    _finish(create_t, create_out)
    _finish(list_t, list_out)
    _finish(run_t, run_out)
    assert create_out["status"] == 200 and create_out["payload"]["job"]["profile"] == "alpha", create_out
    listed = [j for j in list_out["payload"]["jobs"] if j["id"] == created["id"]]
    assert [j.get("profile") for j in listed] == ["alpha"], listed
    assert run_out["status"] == 200 and run_out["payload"].get("status") == "running", run_out
    assert len(no_dispatch) == 1, no_dispatch
    dispatched, owner_home, execution_home, _event_profile = no_dispatch[0]
    assert dispatched.get("profile") == "alpha", dispatched
    assert Path(owner_home) == world["beta"], owner_home
    assert Path(execution_home) == world["alpha"], execution_home
    _assert_job_untouched(world, "alpha")


@requires_per_thread_helpers
def test_create_waiting_on_a_running_job_never_holds_the_jobs_lock(world, monkeypatch, no_dispatch):
    """Lock order is cron env lock -> Agent jobs lock, never the reverse.

    A running in-process job holds the cron env lock and takes the Agent jobs
    lock for its own bookkeeping. A Create queued behind it must not already
    hold the jobs lock, or list/Run would stall for the whole run and the
    job's bookkeeping would deadlock against the waiting Create.
    """
    entered, bookkeep, bookkept, release = (threading.Event() for _ in range(4))
    errors = []

    def running_job():
        try:
            with profiles.cron_profile_context_for_home(world["alpha"]):
                entered.set()
                bookkeep.wait(HOLD_MAX_S)
                with _cron_jobs()._jobs_lock():  # e.g. mark_job_run() at the end of the run
                    bookkept.set()
                release.wait(HOLD_MAX_S)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    holder = threading.Thread(target=running_job, daemon=True)
    holder.start()
    assert entered.wait(10) and profiles._cron_env_lock.locked()
    try:
        create_t, create_out = _request("POST", "/api/crons/create", "beta", body=_create_body(), deadline=0.5)
        create_waited = create_t.is_alive()
        list_t, list_out = _request("GET", "/api/crons", "beta")
        run_t, run_out = _request("POST", "/api/crons/run", "beta", body={"job_id": JOB_ID})
        blocked = [name for name, t in (("list", list_t), ("run", run_t)) if t.is_alive()]
        bookkeep.set()
        bookkeeping_ok = bookkept.wait(PROMPT_DEADLINE_S)
    finally:
        bookkeep.set()
        release.set()
    holder.join(15)
    assert not holder.is_alive() and errors == [], errors
    _finish(create_t, create_out)
    _finish(list_t, list_out)
    _finish(run_t, run_out)
    assert create_waited, "create must still wait for the running job's cron env lock"
    assert blocked == [], f"waited behind a Create parked on the cron env lock: {blocked}"
    assert bookkeeping_ok, "running job's jobs.json bookkeeping blocked by a waiting Create"
    assert [j["name"] for j in list_out["payload"]["jobs"]] == ["beta-job"], list_out
    assert run_out["payload"].get("status") == "running", run_out
    assert create_out["status"] == 200 and create_out["payload"]["job"]["profile"] == "alpha", create_out
    assert not profiles._cron_env_lock.locked()


def test_agent_jobs_lock_helper_is_a_noop_on_older_agents(monkeypatch):
    """Without the Agent's _jobs_lock, Create/list/Run stay on the legacy env lock."""
    live = _cron_jobs()
    if callable(getattr(live, "_jobs_lock", None)):
        with routes._cron_agent_jobs_lock():
            pass
    monkeypatch.delattr(live, "_jobs_lock", raising=False)
    with routes._cron_agent_jobs_lock():
        pass
    assert profiles._cron_store_scope_helpers() is None


OLDER_AGENT_JOB_ID = "01de4a6e1701"


def _install_plain_jobs_lock_agent(monkeypatch, *, keep_use_cron_store):
    """Model an Agent whose ``_jobs_lock`` is a NON-reentrant ``threading.Lock``.

    Agent e5b4cf7bea2 (Codex r3 CORE) has exactly that and no
    ``use_cron_store``; its ``create_job``/``update_job``/``resume_job`` take
    the lock internally. ``keep_use_cron_store=True`` models a hypothetical
    Agent that has the per-thread store helpers but still a plain lock. The
    real lock blocks forever on re-entry; this one times out and records it so
    a regression fails fast instead of hanging the suite while holding
    ``_cron_env_lock``.
    """
    live = _cron_jobs()
    plain = threading.Lock()
    reentered = []

    @contextlib.contextmanager
    def plain_jobs_lock():
        if not plain.acquire(timeout=2.0):
            reentered.append(threading.current_thread().name)
            raise RuntimeError("re-entered the non-reentrant Agent jobs lock")
        try:
            yield
        finally:
            plain.release()

    stored = {}

    def create_job(**kwargs):
        with plain_jobs_lock():
            stored.update({"id": OLDER_AGENT_JOB_ID, "name": kwargs.get("name"), "prompt": kwargs.get("prompt"),
                           "enabled": True, "state": "scheduled"})
            return dict(stored)

    def update_job(job_id, updates):
        with plain_jobs_lock():
            stored.update(updates)
            return dict(stored)

    def resume_job(job_id):
        with plain_jobs_lock():
            return {"id": job_id, "enabled": True, "state": "scheduled"}

    if not keep_use_cron_store:
        monkeypatch.delattr(live, "use_cron_store", raising=False)
    monkeypatch.setattr(live, "_jobs_lock", plain_jobs_lock)
    monkeypatch.setattr(live, "_jobs_file_lock", plain, raising=False)
    monkeypatch.setattr(live, "create_job", create_job)
    monkeypatch.setattr(live, "update_job", update_job)
    monkeypatch.setattr(live, "resume_job", resume_job)
    return reentered


@pytest.mark.parametrize("keep_use_cron_store", [False, True], ids=["agent_e5b4cf7bea2", "store_helpers_plain_lock"])
def test_non_reentrant_agent_jobs_lock_never_deadlocks_create_or_resume(
    world, monkeypatch, no_dispatch, keep_use_cron_store
):
    """Codex r3 CORE: the outer Agent jobs lock must only be taken when the
    Agent's lock is re-entrant and the lock-free store scope is in use;
    otherwise Create/Resume re-acquire it inside the Agent and hang while
    holding ``_cron_env_lock`` (master returns 200)."""
    reentered = _install_plain_jobs_lock_agent(monkeypatch, keep_use_cron_store=keep_use_cron_store)
    create_t, create_out = _request("POST", "/api/crons/create", "beta", body=_create_body(), deadline=10)
    assert not create_t.is_alive(), "Create hung on the Agent jobs lock"
    _finish(create_t, create_out)
    resume_t, resume_out = _request("POST", "/api/crons/resume", "beta", body={"job_id": OLDER_AGENT_JOB_ID}, deadline=10)
    assert not resume_t.is_alive(), "Resume hung on the Agent jobs lock"
    _finish(resume_t, resume_out)
    assert reentered == [], reentered
    assert create_out["status"] == 200 and create_out["payload"]["job"]["profile"] == "alpha", create_out
    assert resume_out["status"] == 200 and resume_out["payload"]["job"]["state"] == "scheduled", resume_out
    assert profiles._cron_store_scope_helpers() is None, "non-reentrant Agent lock must use the locked fallback"
    with routes._cron_agent_jobs_lock():  # no-op: must not take the plain lock
        with _cron_jobs()._jobs_lock():
            pass
    assert not profiles._cron_env_lock.locked()


@requires_per_thread_helpers
def test_installed_agent_jobs_lock_is_reentrant_when_the_lock_free_scope_is_enabled(world):
    """Pins the assumption behind the outer jobs lock: on any Agent where the
    per-thread store scope is enabled, the thread holding ``_jobs_lock()`` can
    re-enter it (Create/Resume/List/Run call Agent code that takes it again)."""
    assert profiles._cron_store_scope_helpers() is not None
    done = threading.Event()

    def nested():
        with profiles.cron_store_scope_for_home(world["beta"]), routes._cron_agent_jobs_lock():
            with _cron_jobs()._jobs_lock():
                done.set()

    t = threading.Thread(target=nested, daemon=True)
    t.start()
    t.join(10)
    assert done.is_set() and not t.is_alive(), "installed Agent _jobs_lock() is not re-entrant"


SNAPSHOT_TRIGGER_BODIES = {
    # Codex r3 SILENT: clearing a model pin recomputes model_snapshot from config.
    "model_cleared": ({"model": None}, {"model": "pinned-model"}),
    "provider": ({"provider": "custom"}, {}),
    "base_url": ({"base_url": "http://127.0.0.1:9/v1"}, {}),
    "no_agent": ({"no_agent": False}, {}),
}


def _install_snapshot_agent(monkeypatch):
    """Model released Agent v2026.9.14's update_job snapshot recomputation.

    There, ``update_job`` calls ``_compute_provider_model_snapshots`` whenever an
    update touches provider/model/base_url/no_agent; for an unpinned model it
    resolves ``model.default`` from ``get_hermes_home()/config.yaml`` with
    ``${VAR}`` expansion from the PROCESS environment (and resolves the
    provider from process env too).
    """
    live = _cron_jobs()
    from hermes_cli.config_effective import load_user_config_effective

    def compute(*, provider, model, base_url, no_agent):
        if bool(no_agent):
            return None, None
        model_snapshot = None
        if not model:
            cfg_path = live.get_hermes_home() / "config.yaml"
            cfg = load_user_config_effective(cfg_path) if cfg_path.exists() else {}
            model_cfg = cfg.get("model") or {}
            if isinstance(model_cfg, dict):
                model_cfg = model_cfg.get("default") or model_cfg.get("model")
            model_snapshot = model_cfg.strip() or None if isinstance(model_cfg, str) else None
        return None, model_snapshot

    real_update = live.update_job

    def update_job(job_id, updates):
        updates = dict(updates)
        if {"provider", "model", "base_url", "no_agent"}.intersection(updates):
            merged = {**(live.get_job(job_id) or {}), **updates}
            updates["provider_snapshot"], updates["model_snapshot"] = compute(
                provider=merged.get("provider"), model=merged.get("model"),
                base_url=merged.get("base_url"), no_agent=merged.get("no_agent"))
        return real_update(job_id, updates)

    monkeypatch.setattr(live, "_compute_provider_model_snapshots", compute, raising=False)
    monkeypatch.setattr(live, "update_job", update_job)


@pytest.mark.parametrize("holder_kind", [None, "scheduled_fire_7295"])
@pytest.mark.parametrize("trigger", sorted(SNAPSHOT_TRIGGER_BODIES))
def test_snapshot_agent_inference_edits_resolve_config_against_the_requesting_profile(
    world, monkeypatch, holder_kind, trigger
):
    """Codex r3 SILENT: on Agents exposing ``_compute_provider_model_snapshots``
    an inference-field edit re-resolves the model from config.yaml, whose
    ``${HERMES_HOME}`` expansion reads the process env. Lock-free, B's job
    saved the default's or the running job's (A's) path; it must save B's."""
    body_extra, job_extra = SNAPSHOT_TRIGGER_BODIES[trigger]
    (world["beta"] / "config.yaml").write_text("model:\n  default: ${HERMES_HOME}/configured-model\n", encoding="utf-8")
    path = world["beta"] / "cron" / "jobs.json"
    job = _jobs_on_disk(world["beta"])[0]
    job.update(job_extra)
    path.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    _install_snapshot_agent(monkeypatch)
    body = {"job_id": JOB_ID, **body_extra}
    if holder_kind is None:
        t, out = _request("POST", "/api/crons/update", "beta", body=body)
        serialized = True
    else:
        with HOLDERS[holder_kind](world, monkeypatch, world["alpha"]):
            t, out = _request("POST", "/api/crons/update", "beta", body=body, deadline=0.5)
            serialized = t.is_alive()
    _finish(t, out)
    assert out["status"] == 200, out
    saved = _jobs_on_disk(world["beta"])[0]
    assert saved.get("model_snapshot") == f"{world['beta']}/configured-model", saved
    assert serialized, "snapshot-recomputing update must wait for the running job's cron env lock"
    assert os.environ.get("HERMES_HOME") == str(world["base"])
    _assert_job_untouched(world, "alpha")


@pytest.mark.parametrize("snapshot_agent", [False, True], ids=["current_agent", "snapshot_agent"])
def test_cron_update_profile_context_selector(monkeypatch, snapshot_agent):
    """Which /api/crons/update bodies need the process env pointed at the profile."""
    live = _cron_jobs()
    if snapshot_agent:
        monkeypatch.setattr(live, "_compute_provider_model_snapshots", lambda **kw: (None, None), raising=False)
    else:
        monkeypatch.delattr(live, "_compute_provider_model_snapshots", raising=False)
    needs = routes._cron_update_needs_profile_context
    # Current Agents: only a pin that resolves the main model reads config.
    assert needs({"job_id": JOB_ID, "pinned": True}) is True
    assert needs({"job_id": JOB_ID, "pinned": True, "model": "m"}) is snapshot_agent
    assert needs({"job_id": JOB_ID, "name": "x", "schedule": "every 2h", "prompt": "p"}) is False
    assert needs({"job_id": JOB_ID, "enabled": False, "deliver": "local", "skills": []}) is False
    for key, value in (("model", None), ("model", "m"), ("provider", None), ("provider", "p"),
                       ("base_url", None), ("base_url", "http://x"), ("no_agent", False), ("no_agent", True),
                       ("pinned", False)):
        assert needs({"job_id": JOB_ID, key: value}) is snapshot_agent, (key, value)
    assert needs(None) is False and needs([]) is False
