"""Health route and shared gateway restart helper checks."""

import io
import json
import subprocess
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import api.gateway_restart as gateway_restart
import api.routes as routes


class MockPopen:
    def __init__(
        self,
        args,
        *,
        stdout_text="",
        stderr_text="",
        returncode=0,
        communicate_timeout=False,
        wait_timeout=False,
        env=None,
        pid=None,
    ):
        self.args = args
        self.env = env or {}
        self.returncode = returncode
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self.communicate_timeout = communicate_timeout
        self.wait_timeout = wait_timeout
        self.terminated = False
        self.killed = False
        self.communicate_timeout_arg = None
        self.wait_timeout_arg = None
        self.pid = pid

    def communicate(self, timeout=None):
        self.communicate_timeout_arg = timeout
        if self.communicate_timeout:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.stdout.getvalue(), self.stderr.getvalue()

    def wait(self, timeout=None):
        self.wait_timeout_arg = timeout
        if self.wait_timeout:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class InlineThread:
    def __init__(self, *, target, args=(), daemon=None):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.target(*self.args)


def _call_health_restart(monkeypatch, helper_result):
    handler = types.SimpleNamespace()
    responses = []
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, **kw: responses.append((payload, kw.get("status", 200))) or True,
    )
    monkeypatch.setattr(routes, "restart_active_profile_gateway", lambda: dict(helper_result))
    return routes._handle_health_restart(handler), responses


def test_restart_active_profile_gateway_success_uses_active_profile_home(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    called = {}

    def fake_popen(args, stdout=None, stderr=None, text=True, env=None):
        called["args"] = args
        called["env"] = env
        return MockPopen(
            args,
            stdout_text="✓ Service restarted",
            returncode=0,
            env=env,
        )

    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: "/mock/hermes/home")
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fake_popen)

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "completed"
    assert result["message"] == "Gateway service restarted successfully"
    assert called["args"] == ["/mock/bin/hermes", "--profile", "default", "gateway", "restart"]
    assert called["env"]["HERMES_HOME"] == "/mock/hermes/home"
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_active_profile_gateway_pins_explicit_default_profile(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    called = {}

    def fake_popen(args, stdout=None, stderr=None, text=True, env=None):
        called["args"] = args
        called["env"] = env
        return MockPopen(args, stdout_text="ok", returncode=0, env=env)

    monkeypatch.setattr(
        gateway_restart,
        "get_hermes_home_for_profile",
        lambda profile: "/mock/hermes/default" if profile == "default" else "/mock/hermes/profiles/work",
    )
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fake_popen)

    result = gateway_restart.restart_active_profile_gateway(profile="default")

    assert result["status"] == "completed"
    assert called["args"] == ["/mock/bin/hermes", "--profile", "default", "gateway", "restart"]
    assert called["env"]["HERMES_HOME"] == "/mock/hermes/default"


def test_restart_active_profile_gateway_omits_profile_for_isolated_default_home(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    called = {}

    def fake_popen(args, stdout=None, stderr=None, text=True, env=None):
        called["args"] = args
        called["env"] = env
        return MockPopen(args, stdout_text="ok", returncode=0, env=env)

    monkeypatch.setattr(
        gateway_restart,
        "get_hermes_home_for_profile",
        lambda profile: "/mock/hermes/profiles/default",
    )
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fake_popen)

    result = gateway_restart.restart_active_profile_gateway(profile="default")

    assert result["status"] == "completed"
    assert called["args"] == ["/mock/bin/hermes", "gateway", "restart"]
    assert called["env"]["HERMES_HOME"] == "/mock/hermes/profiles/default"


def test_restart_active_profile_gateway_rejects_malformed_explicit_profile(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()

    def fail_popen(*args, **kwargs):
        raise AssertionError("malformed explicit profile must not launch subprocess")

    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fail_popen)

    for profile in ("", " default", "default ", "default\n", "../bad", "bad;echo"):
        result = gateway_restart.restart_active_profile_gateway(profile=profile)

        assert result["status"] == "failed"
        assert "Invalid profile for gateway restart" in result["message"]
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_active_profile_gateway_accepts_renamed_root_alias(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    called = {}

    def fake_popen(args, stdout=None, stderr=None, text=True, env=None):
        called["args"] = args
        called["env"] = env
        return MockPopen(args, stdout_text="ok", returncode=0, env=env)

    monkeypatch.setattr(
        gateway_restart,
        "get_hermes_home_for_profile",
        lambda profile: "/mock/hermes/root" if profile == "rootalias" else "/mock/hermes/other",
    )
    monkeypatch.setattr(
        gateway_restart,
        "_is_root_profile",
        lambda profile: profile in {"default", "rootalias"},
    )
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fake_popen)

    result = gateway_restart.restart_active_profile_gateway(profile="rootalias")

    assert result["status"] == "completed"
    assert called["args"] == ["/mock/bin/hermes", "--profile", "default", "gateway", "restart"]
    assert called["env"]["HERMES_HOME"] == "/mock/hermes/root"


def test_restart_active_profile_gateway_failure_preserves_empty_output_contract(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()

    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: "/mock/hermes/home")
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(
        gateway_restart.subprocess,
        "Popen",
        lambda args, stdout=None, stderr=None, text=True, env=None: MockPopen(
            args,
            returncode=7,
            env=env,
        ),
    )

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "failed"
    assert result["message"] == "Restart failed: "
    assert result["returncode"] == 7
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_active_profile_gateway_timeout_releases_lock_after_background_wait(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    proc = MockPopen(
        ["/mock/bin/hermes", "gateway", "restart"],
        communicate_timeout=True,
        env={"HERMES_HOME": "/mock/hermes/home"},
    )

    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: "/mock/hermes/home")
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(gateway_restart.threading, "Thread", InlineThread)

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "in_progress"
    assert proc.communicate_timeout_arg == 2.0
    assert proc.wait_timeout_arg == 240.0
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def _became_gateway_proc(tmp_path, *, pid=4242, wait_timeout=True):
    return MockPopen(
        ["/mock/bin/hermes", "gateway", "restart"],
        communicate_timeout=True,
        wait_timeout=wait_timeout,
        pid=pid,
        env={"HERMES_HOME": str(tmp_path)},
    )


def _write_gateway_state(tmp_path, **fields):
    (tmp_path / "gateway_state.json").write_text(
        json.dumps(fields), encoding="utf-8"
    )


def _future_timestamp() -> str:
    """A timezone-aware ``updated_at`` guaranteed to postdate any subprocess
    spawned during the test (matches what a gateway that became the restart
    child would write after finishing initialization)."""
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def test_restart_timeout_does_not_terminate_when_subprocess_became_gateway(monkeypatch, tmp_path):
    """#6730: single-container `hermes gateway restart` becomes the gateway
    itself (no service manager). The 240s background-wait timeout must NOT
    SIGTERM it — that killed the healthy replacement gateway and dropped every
    active session/SSE stream.  Exemption requires a confirmed ``running``
    record that postdates the restart subprocess and names its PID."""
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    proc = _became_gateway_proc(tmp_path)
    _write_gateway_state(
        tmp_path,
        gateway_state="running",
        pid=proc.pid,
        updated_at=_future_timestamp(),
    )

    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(gateway_restart.threading, "Thread", InlineThread)

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "in_progress"
    # The subprocess IS the gateway: it must be left running, not terminated.
    assert proc.terminated is False
    assert proc.killed is False
    # Lock still released so the next Restart Service click is not blocked.
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_timeout_still_terminates_when_subprocess_is_not_gateway(monkeypatch, tmp_path):
    """Fail closed: without a gateway_state.json naming this subprocess PID,
    the old terminate-on-timeout behaviour must be preserved."""
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    proc = _became_gateway_proc(tmp_path)
    # No gateway_state.json written: unverifiable -> terminate.

    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(gateway_restart.threading, "Thread", InlineThread)

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "in_progress"
    assert proc.terminated is True
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_timeout_still_terminates_for_same_pid_starting_record(monkeypatch, tmp_path):
    """CHANGES_REQUESTED (#6733): the gateway writes ``starting`` BEFORE
    potentially-blocking plugin/MCP/platform initialization.  A restart child
    wedged during startup therefore matches a same-PID ``starting`` record —
    the termination exemption must NOT apply, and the 240s cleanup must still
    terminate it (the old code exempted ``starting`` and leaked the process)."""
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    proc = _became_gateway_proc(tmp_path)
    # Same PID, fresh timestamp — only the ``starting`` state blocks the
    # exemption, isolating the state gate from the generation gate.
    _write_gateway_state(
        tmp_path,
        gateway_state="starting",
        pid=proc.pid,
        updated_at=_future_timestamp(),
    )

    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(gateway_restart.threading, "Thread", InlineThread)

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "in_progress"
    assert proc.terminated is True
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_timeout_still_terminates_for_stale_generation_record(monkeypatch, tmp_path):
    """CHANGES_REQUESTED (#6733): a ``running`` record whose ``updated_at``
    predates the restart subprocess belongs to a previous gateway generation.
    With PID reuse such a stale record can falsely match raw PID equality —
    the record must postdate the subprocess spawn, otherwise the genuinely
    stuck child is terminated instead of exempted."""
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    proc = _became_gateway_proc(tmp_path)
    stale_updated_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    # Same PID, ``running`` state — only the stale generation blocks the
    # exemption, isolating the generation gate from the state gate.
    _write_gateway_state(
        tmp_path,
        gateway_state="running",
        pid=proc.pid,
        updated_at=stale_updated_at,
    )

    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(gateway_restart.threading, "Thread", InlineThread)

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "in_progress"
    assert proc.terminated is True
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_timeout_still_terminates_when_canonical_pid_file_unreadable(monkeypatch, tmp_path):
    """Re-gate (#6733): malformed canonical metadata must fail CLOSED.  The
    old parse let ``UnicodeDecodeError`` escape ``_read_canonical_gateway_pid``,
    so the background thread released ``_GATEWAY_RESTART_LOCK`` in its
    ``finally`` but SKIPPED ``terminate()``/``kill()`` — a genuinely hung
    restart child survived.  State + PID + generation all match here; only the
    non-UTF-8 ``gateway.pid`` blocks the exemption, and the child must still
    be terminated with the lock released."""
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    proc = _became_gateway_proc(tmp_path)
    _write_gateway_state(
        tmp_path,
        gateway_state="running",
        pid=proc.pid,
        updated_at=_future_timestamp(),
    )
    (tmp_path / "gateway.pid").write_bytes(bytes([0xFF, 0xFE, 0x00, 0x01]) + b"binary-pid")

    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(gateway_restart.threading, "Thread", InlineThread)

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "in_progress"
    assert proc.terminated is True
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_timeout_still_terminates_when_identity_verification_raises(monkeypatch, tmp_path):
    """Re-gate (#6733): if ``_subprocess_became_gateway`` itself throws, the
    call site must treat the exception as \"not confirmed\" and still run the
    terminate/kill cleanup — a verifier exception must never skip the
    timed-out child's termination."""
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    proc = _became_gateway_proc(tmp_path)

    def _exploding_verifier(*args, **kwargs):
        raise RuntimeError("verifier boom")

    monkeypatch.setattr(gateway_restart, "_subprocess_became_gateway", _exploding_verifier)
    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(gateway_restart.threading, "Thread", InlineThread)

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "in_progress"
    assert proc.terminated is True
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_subprocess_became_gateway_true_for_matching_running_record(tmp_path):
    _write_gateway_state(tmp_path, gateway_state="running", pid=4242)
    proc = MockPopen(["/mock/bin/hermes", "gateway", "restart"], pid=4242)
    assert gateway_restart._subprocess_became_gateway(proc, tmp_path) is True


def test_subprocess_became_gateway_false_on_pid_mismatch(tmp_path):
    _write_gateway_state(tmp_path, gateway_state="running", pid=999)
    proc = MockPopen(["/mock/bin/hermes", "gateway", "restart"], pid=4242)
    assert gateway_restart._subprocess_became_gateway(proc, tmp_path) is False


def test_subprocess_became_gateway_false_when_canonical_pid_file_mismatches(tmp_path):
    """CHANGES_REQUESTED (#6733): the recorded PID must agree with the
    canonical ``gateway.pid`` file when present.  A mismatch means the record
    is not the current gateway generation — fail closed."""
    _write_gateway_state(
        tmp_path,
        gateway_state="running",
        pid=4242,
        updated_at=_future_timestamp(),
    )
    # Hermes writes gateway.pid as a JSON object, not a plain integer.
    (tmp_path / "gateway.pid").write_text(
        json.dumps({"pid": 7777, "started_at": "2026-08-03T20:00:00+00:00"}),
        encoding="utf-8",
    )
    proc = MockPopen(["/mock/bin/hermes", "gateway", "restart"], pid=4242)
    assert (
        gateway_restart._subprocess_became_gateway(
            proc, tmp_path, proc_started_at=time.time() - 60
        )
        is False
    )


def test_subprocess_became_gateway_true_when_canonical_pid_file_matches(tmp_path):
    """A ``running`` record that postdates the subprocess AND agrees with the
    canonical ``gateway.pid`` file is the current generation — exempt.  Uses
    the real JSON-object shape Hermes writes (``{"pid": 4242, ...}``)."""
    _write_gateway_state(
        tmp_path,
        gateway_state="running",
        pid=4242,
        updated_at=_future_timestamp(),
    )
    (tmp_path / "gateway.pid").write_text(
        json.dumps({"pid": 4242, "started_at": "2026-08-03T20:00:00+00:00"}),
        encoding="utf-8",
    )
    proc = MockPopen(["/mock/bin/hermes", "gateway", "restart"], pid=4242)
    assert (
        gateway_restart._subprocess_became_gateway(
            proc, tmp_path, proc_started_at=time.time() - 60
        )
        is True
    )


def test_subprocess_became_gateway_true_with_legacy_plain_integer_pid_file(tmp_path):
    """A legacy plain-integer ``gateway.pid`` (``"4242"``) is still accepted."""
    _write_gateway_state(
        tmp_path,
        gateway_state="running",
        pid=4242,
        updated_at=_future_timestamp(),
    )
    (tmp_path / "gateway.pid").write_text("4242", encoding="utf-8")
    proc = MockPopen(["/mock/bin/hermes", "gateway", "restart"], pid=4242)
    assert (
        gateway_restart._subprocess_became_gateway(
            proc, tmp_path, proc_started_at=time.time() - 60
        )
        is True
    )


def test_subprocess_became_gateway_false_when_canonical_pid_file_pid_not_integer(tmp_path):
    """CORE (#6733 re-gate): a non-integer ``pid`` in the canonical JSON
    ``gateway.pid`` file (string/float) must fail closed, never exempt."""
    proc = MockPopen(["/mock/bin/hermes", "gateway", "restart"], pid=4242)
    for bad_pid in ("4242", 4242.9, True):
        _write_gateway_state(
            tmp_path,
            gateway_state="running",
            pid=4242,
            updated_at=_future_timestamp(),
        )
        (tmp_path / "gateway.pid").write_text(
            json.dumps({"pid": bad_pid}), encoding="utf-8"
        )
        assert (
            gateway_restart._subprocess_became_gateway(
                proc, tmp_path, proc_started_at=time.time() - 60
            )
            is False
        ), f"canonical pid {bad_pid!r} must fail closed"


def test_subprocess_became_gateway_false_when_canonical_pid_file_invalid_json(tmp_path):
    """CORE (#6733 re-gate): an unreadable/non-JSON/non-integer ``gateway.pid``
    must fail closed (the old ``int()``-on-raw-file parse raised and killed the
    healthy replacement)."""
    _write_gateway_state(
        tmp_path,
        gateway_state="running",
        pid=4242,
        updated_at=_future_timestamp(),
    )
    (tmp_path / "gateway.pid").write_text("not-a-pid", encoding="utf-8")
    proc = MockPopen(["/mock/bin/hermes", "gateway", "restart"], pid=4242)
    assert (
        gateway_restart._subprocess_became_gateway(
            proc, tmp_path, proc_started_at=time.time() - 60
        )
        is False
    )


def test_subprocess_became_gateway_false_when_canonical_pid_file_not_utf8(tmp_path):
    """Re-gate (#6733): non-UTF-8 ``gateway.pid`` content must fail closed
    instead of raising out of the verifier.  An escaping ``UnicodeDecodeError``
    skipped the timed-out child's terminate cleanup (fail-open leak)."""
    _write_gateway_state(
        tmp_path,
        gateway_state="running",
        pid=4242,
        updated_at=_future_timestamp(),
    )
    (tmp_path / "gateway.pid").write_bytes(bytes([0xFF, 0xFE, 0x00, 0x01]) + b"binary-pid")
    proc = MockPopen(["/mock/bin/hermes", "gateway", "restart"], pid=4242)
    assert (
        gateway_restart._subprocess_became_gateway(
            proc, tmp_path, proc_started_at=time.time() - 60
        )
        is False
    )


def test_subprocess_became_gateway_false_when_canonical_pid_file_oversized_integer(tmp_path):
    """Re-gate (#6733): a ``gateway.pid`` whose integer exceeds the
    interpreter's string-conversion digit limit raises ValueError from both
    ``json.loads`` and ``int()``; the read must stay non-throwing and fail
    closed rather than skipping the timed-out child's cleanup."""
    _write_gateway_state(
        tmp_path,
        gateway_state="running",
        pid=4242,
        updated_at=_future_timestamp(),
    )
    (tmp_path / "gateway.pid").write_text("9" * 5000, encoding="utf-8")
    proc = MockPopen(["/mock/bin/hermes", "gateway", "restart"], pid=4242)
    assert (
        gateway_restart._subprocess_became_gateway(
            proc, tmp_path, proc_started_at=time.time() - 60
        )
        is False
    )


def test_subprocess_became_gateway_false_on_non_integer_runtime_pid(tmp_path):
    """CORE (#6733 re-gate): a string/float PID in ``gateway_state.json`` must
    fail closed.  ``int()`` accepted ``"4242"`` and truncated ``4242.9``, so
    with no canonical pid file a genuinely hung child was exempted."""
    proc = MockPopen(["/mock/bin/hermes", "gateway", "restart"], pid=4242)
    for bad_pid in ("4242", 4242.9, True):
        _write_gateway_state(tmp_path, gateway_state="running", pid=bad_pid)
        assert (
            gateway_restart._subprocess_became_gateway(proc, tmp_path) is False
        ), f"runtime pid {bad_pid!r} must fail closed"


def test_restart_timeout_exempts_child_that_wrote_state_during_popen(monkeypatch, tmp_path):
    """CORE (#6733 re-gate): ``proc_started_at`` must be captured BEFORE
    ``Popen``.  A fast-starting child can write its ``running`` record while
    the parent is still inside ``Popen``; a post-Popen capture makes that
    fresh record look older than the spawn moment and the healthy replacement
    is terminated.  The pre-Popen capture keeps the exemption intact."""
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    proc = _became_gateway_proc(tmp_path)

    def _popen_writes_state(*args, **kwargs):
        # The child writes its running record before Popen returns control to
        # the parent (i.e. before any post-Popen proc_started_at capture).
        _write_gateway_state(
            tmp_path,
            gateway_state="running",
            pid=proc.pid,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        return proc

    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", _popen_writes_state)
    monkeypatch.setattr(gateway_restart.threading, "Thread", InlineThread)

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "in_progress"
    assert proc.terminated is False
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_subprocess_became_gateway_false_on_stopped_state(tmp_path):
    _write_gateway_state(tmp_path, gateway_state="stopped", pid=4242)
    proc = MockPopen(["/mock/bin/hermes", "gateway", "restart"], pid=4242)
    assert gateway_restart._subprocess_became_gateway(proc, tmp_path) is False


def test_subprocess_became_gateway_false_on_missing_state_file(tmp_path):
    proc = MockPopen(["/mock/bin/hermes", "gateway", "restart"], pid=4242)
    assert gateway_restart._subprocess_became_gateway(proc, tmp_path) is False


def test_restart_active_profile_gateway_busy_reports_contention(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    assert gateway_restart._GATEWAY_RESTART_LOCK.acquire(blocking=False) is True

    try:
        result = gateway_restart.restart_active_profile_gateway()
    finally:
        gateway_restart._GATEWAY_RESTART_LOCK.release()

    assert result == {
        "status": "busy",
        "message": "Restart already in progress. Please wait a moment and try again.",
    }


def test_handle_health_restart_success(monkeypatch):
    result, responses = _call_health_restart(
        monkeypatch,
        {"status": "completed", "message": "Gateway service restarted successfully"},
    )
    assert result is True
    assert responses == [({"ok": True, "message": "Gateway service restarted successfully"}, 200)]


def test_handle_health_restart_timeout(monkeypatch):
    result, responses = _call_health_restart(
        monkeypatch,
        {"status": "in_progress", "message": "Gateway service restart initiated (in progress)"},
    )
    assert result is True
    assert responses == [({"ok": True, "message": "Gateway service restart initiated (in progress)"}, 200)]


def test_handle_health_restart_failure_preserves_empty_output_message(monkeypatch):
    result, responses = _call_health_restart(
        monkeypatch,
        {"status": "failed", "message": "Restart failed: "},
    )
    assert result is True
    assert responses == [({"ok": False, "error": "Restart failed: "}, 500)]


def test_handle_health_restart_failure(monkeypatch):
    result, responses = _call_health_restart(
        monkeypatch,
        {"status": "failed", "message": "Restart failed: bad thing"},
    )
    assert result is True
    assert responses == [({"ok": False, "error": "Restart failed: bad thing"}, 500)]


def test_handle_health_restart_internal_error(monkeypatch):
    _, responses = _call_health_restart(
        monkeypatch,
        {"status": "failed", "message": "Internal error running restart: OSError: bad spawn"},
    )
    assert responses == [({"ok": False, "error": "Internal error running restart: OSError: bad spawn"}, 500)]


def test_handle_health_restart_concurrency(monkeypatch):
    _, responses = _call_health_restart(
        monkeypatch,
        {"status": "busy", "message": "Restart already in progress. Please wait a moment and try again."},
    )
    assert responses == [
        (
            {"ok": False, "error": "Restart already in progress. Please wait a moment and try again."},
            429,
        )
    ]
