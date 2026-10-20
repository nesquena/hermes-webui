"""#6753: in-chat ``cronjob(action="run")`` must not hold ``_cron_env_lock`` for the run.

The naive streaming wrapper executed the model-facing cron tool inside
``cron_profile_context_for_home()``, whose process-wide ``_cron_env_lock`` was
therefore held for the *entire* (minutes-long) job body. Every other cron /
profile request in the parent WebUI process then blocked on
``api/profiles.py::_cron_env_lock.acquire()`` and the UI froze.

The contract these tests lock in (maintainer guidance on the issue):

* the long-running ``run``/``run_now``/``trigger`` actions execute behind a
  profile-pinned child boundary, and the parent holds no lock while it waits;
* the parent stays responsive: unrelated callers can take ``_cron_env_lock``
  while the job is still running;
* profile-home isolation survives the process boundary;
* the tool's immediate result (claimed / skipped / error JSON) is unchanged;
* metadata actions stay on the short serialized in-process context;
* a child that errors or wedges is cleaned up and reported, never left hanging.

Note on where the fake handlers live: the spawn child re-imports the handler by
module reference, and the hermes-agent checkout that the test env prepends to
``sys.path`` ships its own ``tests`` package which shadows this repo's ``tests``
package in the child. The handler functions therefore live in a uniquely-named
module written to ``tmp_path`` (see the ``fakes`` fixture), never under
``tests.*``.
"""

import contextvars
import importlib
import json
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from api import profiles as profiles_mod
from api import streaming as st

_MARKER_ENV = "HERMES_WEBUI_TEST_6753_MARKER"
_BLOCK_ENV = "HERMES_WEBUI_TEST_6753_BLOCK_SECONDS"
_DEFAULT_BLOCK_SECONDS = "4.0"

_FAKES_MODULE = "wcron6753_fakes"
_FAKES_SOURCE = '''
"""Fake cron tool handlers for the #6753 tests (importable in the spawn child)."""
import json
import os
import time
from pathlib import Path

INPROCESS_CALLS = []


def child_observation(marker_path):
    payload = {"pid": os.getpid(), "hermes_home": os.environ.get("HERMES_HOME")}
    try:
        import cron.jobs as _cj

        payload["jobs_file"] = str(_cj.JOBS_FILE)
    except Exception:
        payload["jobs_file"] = None
    if marker_path:
        Path(marker_path).write_text(json.dumps(payload), encoding="utf-8")
    return payload


def blocking_cron_run_handler(args, **kwargs):
    payload = child_observation(os.environ.get("HERMES_WEBUI_TEST_6753_MARKER", ""))
    time.sleep(float(os.environ.get("HERMES_WEBUI_TEST_6753_BLOCK_SECONDS", "4.0")))
    return json.dumps(
        {
            "success": True,
            "executed": True,
            "job": {"id": (args or {}).get("job_id"), "ran_pid": payload["pid"]},
        }
    )


def raising_cron_run_handler(args, **kwargs):
    child_observation(os.environ.get("HERMES_WEBUI_TEST_6753_MARKER", ""))
    raise RuntimeError("boom from the cron child")


def recording_cron_handler(args, **kwargs):
    INPROCESS_CALLS.append(
        (os.getpid(), os.environ.get("HERMES_HOME"), (args or {}).get("action"))
    )
    return json.dumps({"success": True, "job": {"id": (args or {}).get("job_id")}})
'''


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    """Handlers in an unshadowable module so the spawn child can unpickle them."""
    fakes_dir = tmp_path / "fakes6753"
    fakes_dir.mkdir()
    (fakes_dir / f"{_FAKES_MODULE}.py").write_text(_FAKES_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(fakes_dir))
    return importlib.import_module(_FAKES_MODULE)


def _install(original_handler, profile_home):
    """Build the wrapper exactly as ``_wrap_streaming_cronjob_tool_entries`` does."""
    handler = st._streaming_cronjob_profile_handler(original_handler)
    token = st._STREAMING_CRON_PROFILE_HOME.set(str(profile_home))
    return handler, token


def test_in_chat_cron_run_does_not_hold_parent_env_lock(fakes, tmp_path, monkeypatch):
    """While the job runs, unrelated callers must be able to take ``_cron_env_lock``."""
    profile_home = tmp_path / "profile_run"
    profile_home.mkdir()
    other_home = tmp_path / "profile_other"
    other_home.mkdir()
    marker = tmp_path / "child.json"
    monkeypatch.setenv(_MARKER_ENV, str(marker))

    handler, token = _install(fakes.blocking_cron_run_handler, profile_home)
    result = {}

    def _call():
        result["value"] = handler({"action": "run", "job_id": "job6753"}, task_id=None)

    try:
        # Mirror the agent tool executor's ``propagate_context_to_thread``: a raw
        # thread starts with an empty context, so the profile contextvar would be
        # invisible and the wrapper would skip the cron profile context entirely.
        worker_ctx = contextvars.copy_context()
        worker = threading.Thread(target=worker_ctx.run, args=(_call,), name="in-chat-cron-run")
        worker.start()

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.05)
        assert marker.exists(), "cron job body never started"

        # Unrelated cron/profile work: must acquire the parent lock immediately.
        started = time.monotonic()
        with profiles_mod.cron_profile_context_for_home(other_home):
            pass
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, (
            f"parent _cron_env_lock was held for {elapsed:.1f}s while the in-chat "
            f"cron job ran — unrelated requests starve (#6753)"
        )

        worker.join(timeout=60)
        assert not worker.is_alive(), "in-chat cron tool call never returned"
        assert json.loads(result["value"])["success"] is True
    finally:
        st._STREAMING_CRON_PROFILE_HOME.reset(token)

    child = json.loads(marker.read_text(encoding="utf-8"))
    assert child["pid"] != os.getpid(), "action=run must execute in a child process"
    assert child["hermes_home"] == str(profile_home)


def test_in_chat_cron_run_executes_under_selected_profile_home(fakes, tmp_path, monkeypatch):
    """The child must inherit the selected profile home for env + cron module paths."""
    profile_home = tmp_path / "profile_home"
    profile_home.mkdir()
    monkeypatch.setenv(_MARKER_ENV, str(tmp_path / "child.json"))
    monkeypatch.setenv(_BLOCK_ENV, "0.2")

    handler, token = _install(fakes.blocking_cron_run_handler, profile_home)
    try:
        value = handler({"action": "run", "job_id": "job6753-home"}, task_id=None)
    finally:
        st._STREAMING_CRON_PROFILE_HOME.reset(token)

    assert json.loads(value)["success"] is True
    child = json.loads((tmp_path / "child.json").read_text(encoding="utf-8"))
    assert child["pid"] != os.getpid()
    assert child["hermes_home"] == str(profile_home)
    if child["jobs_file"] is not None:
        assert child["jobs_file"].startswith(str(profile_home))


def test_in_chat_cron_metadata_action_stays_in_process(fakes, tmp_path):
    """Non-run actions are short metadata work: no child, still profile-pinned."""
    profile_home = tmp_path / "profile_meta"
    profile_home.mkdir()
    fakes.INPROCESS_CALLS.clear()

    handler, token = _install(fakes.recording_cron_handler, profile_home)
    try:
        value = handler({"action": "list"}, task_id=None)
    finally:
        st._STREAMING_CRON_PROFILE_HOME.reset(token)

    assert json.loads(value)["success"] is True
    assert fakes.INPROCESS_CALLS == [(os.getpid(), str(profile_home), "list")]


def test_in_chat_cron_run_child_error_is_reported_not_raised(fakes, tmp_path, monkeypatch):
    """A crashing child surfaces as a tool error instead of hanging the turn."""
    profile_home = tmp_path / "profile_err"
    profile_home.mkdir()
    monkeypatch.setenv(_MARKER_ENV, str(tmp_path / "child.json"))

    handler, token = _install(fakes.raising_cron_run_handler, profile_home)
    started = time.monotonic()
    try:
        value = handler({"action": "run", "job_id": "job6753-err"}, task_id=None)
    finally:
        st._STREAMING_CRON_PROFILE_HOME.reset(token)

    assert time.monotonic() - started < 30.0, "child failure must not hang the tool call"
    payload = json.loads(value)
    assert payload["success"] is False
    assert "boom from the cron child" in payload["error"]


def test_in_chat_cron_run_wedged_child_is_terminated(fakes, tmp_path, monkeypatch):
    """A child that never returns is terminated at the wait timeout and reported."""
    profile_home = tmp_path / "profile_wedge"
    profile_home.mkdir()
    marker = tmp_path / "child.json"
    monkeypatch.setenv(_MARKER_ENV, str(marker))
    monkeypatch.setenv(_BLOCK_ENV, "120")
    monkeypatch.setattr(st, "_cron_tool_subprocess_result_timeout_seconds", lambda: 2.0)

    handler, token = _install(fakes.blocking_cron_run_handler, profile_home)
    started = time.monotonic()
    try:
        value = handler({"action": "run", "job_id": "job6753-wedge"}, task_id=None)
    finally:
        st._STREAMING_CRON_PROFILE_HOME.reset(token)

    elapsed = time.monotonic() - started
    assert elapsed < 30.0, f"wedged child was not terminated (waited {elapsed:.1f}s)"
    payload = json.loads(value)
    assert payload["success"] is False

    # The child process must be gone (cleanup), not orphaned and still sleeping.
    child_pid = json.loads(marker.read_text(encoding="utf-8"))["pid"]
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except (ProcessLookupError, PermissionError):
            break
        time.sleep(0.1)
    else:
        pytest.fail(f"wedged child {child_pid} still alive after timeout cleanup")


def test_in_chat_cron_run_falls_back_inline_for_unpicklable_handler(tmp_path):
    """An unpicklable handler degrades to the legacy in-process path, never crashes."""
    profile_home = tmp_path / "profile_fallback"
    profile_home.mkdir()
    seen = []

    def closure_handler(args, **kwargs):
        seen.append(os.getpid())
        return json.dumps({"success": True, "note": "inline fallback"})

    handler, token = _install(closure_handler, profile_home)
    try:
        value = handler({"action": "run", "job_id": "job6753-fb"}, task_id=None)
    finally:
        st._STREAMING_CRON_PROFILE_HOME.reset(token)

    assert json.loads(value)["note"] == "inline fallback"
    assert seen == [os.getpid()]


def test_in_chat_cron_run_keeps_live_gateway_delivery_in_parent(fakes, tmp_path, monkeypatch):
    """Handle-backed runs (live gateway adapters) stay on the parent path (#7328 residual)."""
    profile_home = tmp_path / "profile_handles"
    profile_home.mkdir()
    fakes.INPROCESS_CALLS.clear()

    import sys
    import types

    fake_module = types.ModuleType("gateway.run")
    runner = types.SimpleNamespace(adapters={"telegram": object()})
    fake_module._gateway_runner_ref = lambda: runner
    monkeypatch.setitem(sys.modules, "gateway.run", fake_module)

    handler, token = _install(fakes.recording_cron_handler, profile_home)
    try:
        value = handler({"action": "run", "job_id": "job6753-handles"}, task_id=None)
    finally:
        st._STREAMING_CRON_PROFILE_HOME.reset(token)

    assert json.loads(value)["success"] is True
    assert fakes.INPROCESS_CALLS == [(os.getpid(), str(profile_home), "run")]


def test_child_start_uses_spawn_context():
    """The child must use spawn: fork from a threaded parent is unsafe."""
    source = Path(st.__file__).read_text(encoding="utf-8")
    start = source.find("def _run_cron_tool_in_profile_subprocess")
    assert start != -1, "_run_cron_tool_in_profile_subprocess not found"
    body = source[start:source.find("\ndef ", start + 10)]
    assert 'get_context("spawn")' in body
    assert "multiprocessing" in body


def test_child_target_runs_handler_under_profile_context():
    """Module-level spawn target exists and pins the profile context itself."""
    # The child is spawned through an explicit spawn context (see
    # test_child_start_uses_spawn_context), so the process-global default
    # start method is irrelevant to this contract. Asserting on it made the
    # test order-dependent: get_start_method(allow_none=True) is None until
    # some *other* test calls set_start_method(), so this test passed or
    # failed depending on which tests ran earlier in the same CI shard.
    assert multiprocessing.get_context("spawn").get_start_method() == "spawn"
    # Spawn ships the target to the child by reference, so it must be a
    # module-level function reachable by qualified name (not a closure).
    assert hasattr(st, "_cron_tool_subprocess_main")
    assert st._cron_tool_subprocess_main.__qualname__ == "_cron_tool_subprocess_main"
    assert st._cron_tool_subprocess_main.__module__ == st.__name__
    source = Path(st.__file__).read_text(encoding="utf-8")
    start = source.find("def _cron_tool_subprocess_main")
    assert start != -1
    body = source[start:source.find("\ndef ", start + 10)]
    assert "cron_profile_context_for_home" in body
    assert "_webui_original_handler" in body  # never double-wrap in the child
