from __future__ import annotations

import io
import os
import signal
from pathlib import Path
import json
import subprocess
import threading
import time
from urllib.parse import urlparse

import pytest

from api import routes


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self
        self.headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8"))

    def get_json(self):
        return json.loads(self.body.decode("utf-8"))

    def header(self, name):
        for k, v in self.sent_headers:
            if k.lower() == name.lower():
                return v
        return None


def _call_get(monkeypatch, path: str):
    from api import routes

    handler = _FakeHandler()
    routes.handle_get(handler, urlparse(path))
    return handler


def _call_post(monkeypatch, path: str, body: dict | None = None):
    from api import routes

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body or {})
    handler = _FakeHandler()
    routes.handle_post(handler, urlparse(path))
    return handler, handler.get_json()


class _FakeStdout(io.StringIO):
    """A readline-able, closeable fake process stdout stream."""


def _make_fake_popen(lines: list[str], returncode: int = 0, on_spawn=None):
    """Build a fake ``subprocess.Popen`` replacement returning canned output."""

    class _FakeProc:
        def __init__(self, cmd, **kwargs):
            self.args = cmd
            self.kwargs = kwargs
            self.stdout = _FakeStdout("".join(lines))
            self.returncode = returncode
            if on_spawn:
                on_spawn(cmd, kwargs)

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    return _FakeProc


@pytest.fixture(autouse=True)
def _reset_ops_actions_state():
    """Ops actions keep module-level lock/state; reset it around every test
    so tests can't leak a held lock or a stale status into the next one."""
    from api import ops_actions

    def _reset():
        with ops_actions._REGISTRY_LOCK:
            ops_actions._PROFILES.clear()
        # threading.Lock.release() doesn't track ownership, so this is safe to
        # call even if a previous test's background thread left it held.
        if ops_actions._ACTION_LOCK.locked():
            try:
                ops_actions._ACTION_LOCK.release()
            except RuntimeError:
                pass

    _reset()
    yield
    _reset()


def _wait_until_not_running(timeout=5.0):
    from api import ops_actions

    deadline = time.time() + timeout
    while time.time() < deadline:
        status = ops_actions.get_status("default")
        if status["status"] != "running":
            return status
        time.sleep(0.02)
    raise AssertionError("ops action did not finish in time")


# ── Gate (fail-closed) ───────────────────────────────────────────────────────


def test_ops_status_reports_gate_closed_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_ALLOW_OPS_ACTIONS", raising=False)
    handler = _call_get(monkeypatch, "/api/ops/status")
    data = handler.get_json()
    assert handler.status == 200
    assert data["allowed"] is False
    assert data["status"] == "idle"


def test_ops_post_rejected_with_403_when_gate_closed(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_ALLOW_OPS_ACTIONS", raising=False)
    for path in ("/api/ops/doctor", "/api/ops/security-audit", "/api/ops/backup"):
        handler, data = _call_post(monkeypatch, path)
        assert handler.status == 403, path
        assert data["allowed"] is False
        assert "HERMES_WEBUI_ALLOW_OPS_ACTIONS" in data["error"]


def test_ops_backup_download_rejected_when_gate_closed(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_ALLOW_OPS_ACTIONS", raising=False)
    handler = _call_get(monkeypatch, "/api/ops/backup/download")
    assert handler.status == 403
    assert "HERMES_WEBUI_ALLOW_OPS_ACTIONS" in handler.get_json()["error"]


# ── Gate open: action lifecycle ──────────────────────────────────────────────


def test_ops_doctor_action_runs_and_completes(monkeypatch, tmp_path):
    from api import ops_actions, profiles

    monkeypatch.setattr(routes, "_OPS_ACTIONS_GATE_STARTUP_SNAPSHOT", True)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    spawned = []
    fake_proc = _make_fake_popen(
        ["diagnosing...\n", "all good\n"],
        returncode=0,
        on_spawn=lambda cmd, kw: spawned.append((cmd, kw)),
    )
    monkeypatch.setattr(ops_actions.subprocess, "Popen", fake_proc)

    handler, data = _call_post(monkeypatch, "/api/ops/doctor")
    assert handler.status == 200
    assert data["allowed"] is True
    assert data["action"] == "doctor"
    assert data["status"] in ("running", "completed")

    final = _wait_until_not_running()
    assert final["status"] == "completed"
    assert final["returncode"] == 0
    assert "diagnosing" in final["log"]

    cmd, kwargs = spawned[0]
    assert cmd[-1] == "doctor"
    assert kwargs["env"]["HERMES_HOME"] == str(tmp_path)


def test_ops_security_audit_uses_expected_subcommand(monkeypatch, tmp_path):
    from api import ops_actions, profiles

    monkeypatch.setattr(routes, "_OPS_ACTIONS_GATE_STARTUP_SNAPSHOT", True)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    spawned = []
    fake_proc = _make_fake_popen(
        ['{"findings": []}\n'],
        returncode=0,
        on_spawn=lambda cmd, kw: spawned.append((cmd, kw)),
    )
    monkeypatch.setattr(ops_actions.subprocess, "Popen", fake_proc)

    handler, data = _call_post(monkeypatch, "/api/ops/security-audit")
    assert handler.status == 200
    _wait_until_not_running()

    cmd, _ = spawned[0]
    assert cmd[-3:] == ["security", "audit", "--json"]


def test_ops_backup_action_writes_file_and_is_downloadable(monkeypatch, tmp_path):
    from api import config, ops_actions, profiles

    monkeypatch.setattr(routes, "_OPS_ACTIONS_GATE_STARTUP_SNAPSHOT", True)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    state_dir = tmp_path / "webui_state"
    monkeypatch.setattr(config, "STATE_DIR", state_dir)

    def _write_zip(cmd, kwargs):
        out_idx = cmd.index("--output") + 1
        out_path = cmd[out_idx]
        # Backup file must land under the WebUI state root, never the repo.
        assert str(state_dir) in out_path
        import zipfile as _zf

        with _zf.ZipFile(out_path, "w") as zf:
            zf.writestr("config.yaml", "fake: backup")

    fake_proc = _make_fake_popen(["backing up...\n"], returncode=0, on_spawn=_write_zip)
    monkeypatch.setattr(ops_actions.subprocess, "Popen", fake_proc)

    handler, data = _call_post(monkeypatch, "/api/ops/backup")
    assert handler.status == 200
    final = _wait_until_not_running()
    assert final["status"] == "completed"
    assert final["backup_path"]

    dl_handler = _call_get(monkeypatch, "/api/ops/backup/download")
    assert dl_handler.status == 200
    assert bytes(dl_handler.body)[:2] == b"PK"  # served the real archive
    assert dl_handler.header("Content-Type") == "application/zip"
    disposition = dl_handler.header("Content-Disposition") or ""
    assert "attachment" in disposition


def test_ops_backup_download_404_before_any_backup(monkeypatch):
    monkeypatch.setattr(routes, "_OPS_ACTIONS_GATE_STARTUP_SNAPSHOT", True)
    handler = _call_get(monkeypatch, "/api/ops/backup/download")
    assert handler.status == 404


def test_ops_action_failure_is_reported(monkeypatch, tmp_path):
    from api import ops_actions, profiles

    monkeypatch.setattr(routes, "_OPS_ACTIONS_GATE_STARTUP_SNAPSHOT", True)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    fake_proc = _make_fake_popen(["oops\n"], returncode=1)
    monkeypatch.setattr(ops_actions.subprocess, "Popen", fake_proc)

    _call_post(monkeypatch, "/api/ops/doctor")
    final = _wait_until_not_running()
    assert final["status"] == "failed"
    assert final["returncode"] == 1
    assert "1" in final["error"]


# ── Single-flight (409) ──────────────────────────────────────────────────────


def test_ops_action_contention_returns_409_without_spawning(monkeypatch):
    from api import ops_actions

    monkeypatch.setattr(routes, "_OPS_ACTIONS_GATE_STARTUP_SNAPSHOT", True)
    spawned = []
    monkeypatch.setattr(
        ops_actions.subprocess,
        "Popen",
        _make_fake_popen(["x\n"], on_spawn=lambda cmd, kw: spawned.append(cmd)),
    )

    acquired = ops_actions._ACTION_LOCK.acquire(blocking=False)
    assert acquired, "lock should be free at test start"
    try:
        handler, data = _call_post(monkeypatch, "/api/ops/doctor")
    finally:
        ops_actions._ACTION_LOCK.release()

    assert handler.status == 409
    assert data["allowed"] is True
    assert spawned == [], "no subprocess should be spawned while another action holds the lock"


# ── Regression: audit findings (lock leak, zombie reap, status redaction) ───


def test_ops_action_lock_is_released_when_profile_home_resolution_raises(monkeypatch, tmp_path):
    """Audit finding (HOCH): get_active_hermes_home()/_backups_dir() ran outside
    the try/except that releases _ACTION_LOCK on failure -- a raise there left
    the lock held forever, 409-ing every future action until a process
    restart. Both calls must now be inside that try/except."""
    from api import ops_actions, profiles

    monkeypatch.setattr(
        profiles, "get_active_hermes_home", lambda: (_ for _ in ()).throw(RuntimeError("profile resolution failed"))
    )

    with pytest.raises(RuntimeError):
        ops_actions.start_action("doctor", "default")

    assert not ops_actions._ACTION_LOCK.locked(), "lock leaked after get_active_hermes_home() raised"

    # Prove the leak doesn't linger: a normal call right after must still work.
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(ops_actions.subprocess, "Popen", _make_fake_popen(["ok\n"], returncode=0))
    started, _status = ops_actions.start_action("doctor", "default")
    assert started is True
    _wait_until_not_running()


def test_ops_action_lock_is_released_when_backups_dir_raises(monkeypatch, tmp_path):
    """Same finding, the other call inside the vulnerable window: _backups_dir()
    (which mkdir()s) raising (disk full, permissions) must also not leak the
    lock."""
    from api import ops_actions, profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(ops_actions, "_backups_dir", lambda profile: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError):
        ops_actions.start_action("backup", "default")

    assert not ops_actions._ACTION_LOCK.locked(), "lock leaked after _backups_dir() raised"


def test_ops_action_timeout_reaps_zombie_via_background_thread(monkeypatch, tmp_path):
    """Audit finding (MEDIUM): after proc.kill(), if the 5s proc.wait(timeout=5)
    also times out, the code assumed returncode=-9 without ever reaping the
    process, leaving a zombie. A background thread must now call a blocking
    proc.wait() to actually reclaim it."""
    from api import ops_actions, profiles

    monkeypatch.setattr(routes, "_OPS_ACTIONS_GATE_STARTUP_SNAPSHOT", True)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    wait_calls = []
    reaped = threading.Event()

    class _StuckProc:
        def __init__(self, cmd, **kwargs):
            self.args = cmd
            self.stdout = io.StringIO("")
            self.returncode = None

        def wait(self, timeout=None):
            wait_calls.append(timeout)
            if timeout is None:
                # The background reaper's blocking wait -- simulate the
                # stuck process finally dying now.
                self.returncode = -9
                reaped.set()
                return self.returncode
            # Every timed wait (the initial action timeout, then the 5s
            # post-kill wait) times out: the process is stuck.
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout)

        def kill(self):
            pass

    monkeypatch.setattr(ops_actions.subprocess, "Popen", _StuckProc)

    handler, data = _call_post(monkeypatch, "/api/ops/doctor")
    assert handler.status == 200
    final = _wait_until_not_running()
    assert final["status"] == "timeout"
    assert final["returncode"] == -9

    assert reaped.wait(timeout=2), "background reap thread never issued a blocking proc.wait()"
    assert None in wait_calls, "expected at least one no-timeout (blocking) wait() call to reap the process"


def test_ops_status_redacts_log_error_backup_path_when_gate_closed(monkeypatch, tmp_path):
    """Audit finding (MEDIUM): GET /api/ops/status stays readable when the gate
    is off (so the frontend can show the disabled state), but it must not leak
    a prior run's log tail, error, or backup_path through that always-open
    route -- only the allowed:false flag and non-sensitive fields."""
    from api import ops_actions, profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(routes, "_OPS_ACTIONS_GATE_STARTUP_SNAPSHOT", True)
    fake_proc = _make_fake_popen(["some possibly sensitive log line\n"], returncode=1)
    monkeypatch.setattr(ops_actions.subprocess, "Popen", fake_proc)

    _call_post(monkeypatch, "/api/ops/doctor")
    _wait_until_not_running()

    # Gate now decided against the immutable startup snapshot -- flip the
    # snapshot itself to model an operator-closed deployment.
    monkeypatch.setattr(routes, "_OPS_ACTIONS_GATE_STARTUP_SNAPSHOT", False)
    handler = _call_get(monkeypatch, "/api/ops/status")
    data = handler.get_json()
    assert data["allowed"] is False
    assert data["log"] == ""
    assert data["error"] is None
    assert data["backup_path"] is None
    # Non-sensitive fields stay visible so the UI can still show *something*.
    assert data["status"] == "failed"
    assert data["action"] == "doctor"

    monkeypatch.setattr(routes, "_OPS_ACTIONS_GATE_STARTUP_SNAPSHOT", True)
    handler2 = _call_get(monkeypatch, "/api/ops/status")
    data2 = handler2.get_json()
    assert data2["allowed"] is True
    assert "sensitive log line" in data2["log"]


# ── Frontend / i18n presence ─────────────────────────────────────────────────


def test_ops_actions_frontend_wiring_present():
    from pathlib import Path

    panels = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(encoding="utf-8")
    assert "/api/ops/status" in panels
    assert "/api/ops/doctor" in panels
    assert "/api/ops/security-audit" in panels
    assert "/api/ops/backup" in panels


_OPS_I18N_KEYS = (
    "ops_maintenance_title",
    "ops_maintenance_desc",
    "ops_action_doctor",
    "ops_action_security_audit",
    "ops_action_backup",
    "ops_status_idle",
    "ops_status_running",
    "ops_status_completed",
    "ops_status_failed",
    "ops_status_timeout",
    "ops_view_log",
    "ops_download_backup",
    "ops_action_failed",
    "ops_status_load_failed",
    "ops_gate_disabled",
    "ops_confirm_security_audit",
    "ops_confirm_backup",
)

# Locales this repo enforces exact key-parity with `en` for — see the
# dedicated tests/test_*_locale.py coverage tests. it/de/fr/pt have no such
# test and may legitimately fall back to English.
_OPS_I18N_ENFORCED_LOCALES = {
    "cs": "\n  cs: {",
    "ja": "\n  ja: {",
    "ko": "\n  ko: {",
    "pl": "\n  pl: {",
    "ru": "\n  ru: {",
    "es": "\n  es: {",
    "tr": "\n  tr: {",
    "vi": "\n  vi: {",
    "zh": "\n  zh: {",
    "zh-Hant": "\n  'zh-Hant': {",
}


def _extract_locale_block(src: str, marker: str) -> str:
    """Return the `{ ... }` body of one locale object, brace-matched (a plain
    substring search would stop at the first `}` inside a translated string)."""
    start = src.index(marker)
    open_brace = src.index("{", start)
    pos = open_brace + 1
    depth = 1
    in_single = in_double = False
    i = pos
    while depth > 0:
        ch = src[i]
        if in_single:
            if ch == "\\":
                i += 1
            elif ch == "'":
                in_single = False
        elif in_double:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_double = False
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        i += 1
    return src[open_brace:i]


def test_ops_actions_i18n_keys_exist():
    from pathlib import Path

    i18n = (Path(__file__).resolve().parents[1] / "static" / "i18n.js").read_text(encoding="utf-8")
    for key in _OPS_I18N_KEYS:
        assert f"{key}:" in i18n


def test_ops_actions_i18n_keys_translated_in_enforced_locales():
    """Regression: an earlier version of this test only checked that ops_*
    keys existed *somewhere* in i18n.js, which is trivially true once they're
    added to `en` — it never caught them being missing from the ten locales
    this repo enforces full key-parity for (a maintenance action button
    silently rendering in English for those users). Verify each ops_ key is
    present in every enforced locale's own block, not just `en`."""
    from pathlib import Path

    i18n = (Path(__file__).resolve().parents[1] / "static" / "i18n.js").read_text(encoding="utf-8")

    for locale, marker in _OPS_I18N_ENFORCED_LOCALES.items():
        block = _extract_locale_block(i18n, marker)
        missing = [key for key in _OPS_I18N_KEYS if f"{key}:" not in block]
        assert not missing, f"ops_ keys missing from {locale!r} locale: {missing}"


# ── Gate follow-ups: profile ownership, run identity, process tree, artifacts ─


def test_cross_profile_cannot_see_running_state_or_backup(monkeypatch, tmp_path):
    """A run started under profile A must be invisible to profile B: status,
    log, error, backup path — and B's 409 must be a minimal busy envelope."""
    from api import config, ops_actions, profiles

    monkeypatch.setattr(routes, "_OPS_ACTIONS_GATE_STARTUP_SNAPSHOT", True)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "webui_state")

    release = threading.Event()

    class _HeldProc:
        def __init__(self, cmd, **kwargs):
            self.args = cmd
            self.stdout = io.StringIO("secret-log-line\n")
            self.returncode = 0

        def wait(self, timeout=None):
            release.wait(timeout=5)
            return 0

        def kill(self):
            release.set()

    monkeypatch.setattr(ops_actions.subprocess, "Popen", _HeldProc)
    started, own = ops_actions.start_action("doctor", "profile-a")
    assert started and own["status"] == "running"

    try:
        # B's own status: idle, no trace of A's run.
        b_status = ops_actions.get_status("profile-b")
        assert b_status["status"] == "idle"
        assert b_status["log"] == ""
        assert b_status["backup_path"] is None

        # B's start attempt: refused with a minimal busy envelope.
        b_started, b_snap = ops_actions.start_action("doctor", "profile-b")
        assert b_started is False
        assert b_snap.get("busy") is True
        for leaky in ("log", "error", "backup_path", "action", "run_id"):
            assert not b_snap.get(leaky), f"busy envelope leaked {leaky!r}"

        # A's OWN 409 keeps A's full state (same profile retries).
        a_started, a_snap = ops_actions.start_action("doctor", "profile-a")
        assert a_started is False
        assert a_snap["status"] == "running"
    finally:
        release.set()
        deadline = time.time() + 5
        while ops_actions._ACTION_LOCK.locked() and time.time() < deadline:
            time.sleep(0.02)

    # B never gains a download path from A's world.
    assert ops_actions.latest_backup_path("profile-b") is None


def test_late_reader_from_previous_run_cannot_write_into_new_run(tmp_path):
    """Run-identity contract: an append carrying a stale run id is dropped."""
    from api import ops_actions

    runs = ops_actions._profile_runs("default")
    with runs.state_lock:
        runs.run_seq += 1
        runs.state = dict(ops_actions._IDLE_STATE)
        runs.state.update({"run_id": "current-run", "status": "running", "log": "new "})

    ops_actions._append_log(runs, "stale-run", "POISON")
    ops_actions._append_log(runs, "current-run", "ok")
    with runs.state_lock:
        assert "POISON" not in runs.state["log"]
        assert runs.state["log"].endswith("ok")


def test_timeout_kills_the_whole_process_group(monkeypatch, tmp_path):
    """A timed-out action must not leave CLI-spawned children alive: the
    runner owns the process group and the grandchild dies with it."""
    from api import config, ops_actions, profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "webui_state")
    monkeypatch.setattr(ops_actions, "_ACTION_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(ops_actions, "_TERM_GRACE_SECONDS", 1)
    pid_file = tmp_path / "grandchild.pid"
    monkeypatch.setattr(
        ops_actions,
        "_command_for_action",
        lambda action, backup_dir: [
            "bash",
            "-c",
            f"sleep 300 & echo $! > {pid_file}; wait",
        ],
    )

    started, _ = ops_actions.start_action("doctor", "default")
    assert started
    final = _wait_until_not_running(timeout=15)
    assert final["status"] == "timeout"

    deadline = time.time() + 5
    grandchild = None
    while time.time() < deadline:
        try:
            grandchild = int(pid_file.read_text().strip())
            break
        except (OSError, ValueError):
            time.sleep(0.05)
    assert grandchild, "grandchild pid was never written"
    # ESRCH proves the whole group is gone (kill 0 = existence probe).
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(grandchild, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            break
    else:
        os.kill(grandchild, signal.SIGKILL)
        raise AssertionError("grandchild survived the group kill")


def test_slot_stays_closed_until_stuck_process_is_reaped(monkeypatch, tmp_path):
    """Single-flight invariant: after the SIGKILL escalation, no new action
    may start while the first process is still unreaped."""
    from api import ops_actions, profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(ops_actions, "_ACTION_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(ops_actions, "_TERM_GRACE_SECONDS", 0)

    reap_gate = threading.Event()
    reaped = threading.Event()

    class _StuckUntilReleased:
        def __init__(self, cmd, **kwargs):
            self.args = cmd
            self.stdout = io.StringIO("")
            self.returncode = None

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout)
            reap_gate.wait(timeout=10)
            self.returncode = -9
            reaped.set()
            return -9

        def send_signal(self, sig):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(ops_actions.subprocess, "Popen", _StuckUntilReleased)
    started, _ = ops_actions.start_action("doctor", "default")
    assert started

    time.sleep(0.2)  # the runner is now inside the blocking reap wait
    second, snap = ops_actions.start_action("doctor", "default")
    assert second is False, "second action started before the first was reaped"

    reap_gate.set()
    assert reaped.wait(timeout=5)
    final = _wait_until_not_running(timeout=5)
    assert final["status"] == "timeout"


def _completed_backup(monkeypatch, tmp_path, profile="default"):
    """Drive one successful, validated backup run; returns the archive path."""
    from api import config, ops_actions, profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "webui_state")

    def _write_zip(cmd, kwargs):
        out = cmd[cmd.index("--output") + 1]
        import zipfile as _zf

        with _zf.ZipFile(out, "w") as zf:
            zf.writestr("config.yaml", "x: 1")

    monkeypatch.setattr(
        ops_actions.subprocess,
        "Popen",
        _make_fake_popen(["ok\n"], returncode=0, on_spawn=_write_zip),
    )
    started, _ = ops_actions.start_action("backup", profile)
    assert started
    final = _wait_until_not_running() if profile == "default" else None
    if profile != "default":
        deadline = time.time() + 5
        while time.time() < deadline:
            st = ops_actions.get_status(profile)
            if st["status"] != "running":
                final = st
                break
            time.sleep(0.02)
    assert final and final["status"] == "completed", final
    return Path(final["backup_path"])


def test_backup_artifacts_are_permission_hardened(monkeypatch, tmp_path):
    from api import ops_actions

    archive = _completed_backup(monkeypatch, tmp_path)
    assert archive.exists()
    assert (archive.stat().st_mode & 0o777) == 0o600
    assert (archive.parent.stat().st_mode & 0o777) == 0o700
    assert (archive.parent.parent.stat().st_mode & 0o777) == 0o700
    assert ops_actions.latest_backup_path("default") == archive


def test_backup_retention_keeps_only_newest(monkeypatch, tmp_path):
    from api import ops_actions

    archive = _completed_backup(monkeypatch, tmp_path)
    root = archive.parent
    import zipfile as _zf

    for i in range(ops_actions._BACKUP_RETENTION_COUNT + 3):
        p = root / f"hermes-backup-{1000 + i}-deadbeef.zip"
        with _zf.ZipFile(p, "w") as zf:
            zf.writestr("x", "y")
        os.utime(p, (1000 + i, 1000 + i))
    ops_actions._apply_backup_retention("default")
    remaining = list(root.glob("hermes-backup-*.zip"))
    assert len(remaining) == ops_actions._BACKUP_RETENTION_COUNT


def test_download_rejects_symlink_and_outside_paths(monkeypatch, tmp_path):
    from api import config, ops_actions

    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "webui_state")
    root = ops_actions._backups_dir("default")
    outside = tmp_path / "outside-secret.zip"
    import zipfile as _zf

    with _zf.ZipFile(outside, "w") as zf:
        zf.writestr("secret", "x")
    link = root / "hermes-backup-999-symlink.zip"
    link.symlink_to(outside)

    runs = ops_actions._profile_runs("default")
    with runs.state_lock:
        runs.last_successful_backup = str(link)
    # The recorded path is a symlink escaping the root: must not be served,
    # and the fallback scan must not pick it either.
    assert ops_actions.latest_backup_path("default") is None


def test_failed_backup_deletes_partial_archive(monkeypatch, tmp_path):
    from api import config, ops_actions, profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "webui_state")
    partials = []

    def _write_partial(cmd, kwargs):
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_bytes(b"PK\x03\x04truncated")
        partials.append(out)

    monkeypatch.setattr(
        ops_actions.subprocess,
        "Popen",
        _make_fake_popen(["boom\n"], returncode=1, on_spawn=_write_partial),
    )
    started, _ = ops_actions.start_action("backup", "default")
    assert started
    final = _wait_until_not_running()
    assert final["status"] == "failed"
    assert partials and not partials[0].exists(), "partial archive not cleaned up"


def test_last_successful_backup_survives_later_runs_and_restart(monkeypatch, tmp_path):
    """The Download affordance must not vanish because a doctor run started —
    and the archive must be rediscovered after a WebUI restart."""
    from api import ops_actions

    archive = _completed_backup(monkeypatch, tmp_path)

    # A later doctor run resets the RUN state but not the backup record.
    monkeypatch.setattr(
        ops_actions.subprocess, "Popen", _make_fake_popen(["ok\n"], returncode=0)
    )
    started, _ = ops_actions.start_action("doctor", "default")
    assert started
    final = _wait_until_not_running()
    assert final["status"] == "completed"
    assert final["backup_path"] is None  # run-scoped field
    assert final["last_successful_backup"] == str(archive)
    assert final["backup_available"] is True
    assert ops_actions.latest_backup_path("default") == archive

    # Simulated restart: in-memory registry gone, artifact rediscovered.
    with ops_actions._REGISTRY_LOCK:
        ops_actions._PROFILES.clear()
    assert ops_actions.latest_backup_path("default") == archive


def test_profile_env_cannot_open_the_ops_gate(monkeypatch):
    """TARS gate review P0: the gate decision is an immutable startup
    snapshot — a profile .env writing the flag into the LIVE os.environ
    after startup must not open status details, download, or start."""
    monkeypatch.setattr(routes, "_OPS_ACTIONS_GATE_STARTUP_SNAPSHOT", False)
    # Simulates api.profiles._reload_dotenv() importing a profile .env:
    monkeypatch.setenv("HERMES_WEBUI_ALLOW_OPS_ACTIONS", "1")

    assert routes._ops_actions_allowed() is False

    handler, data = _call_post(monkeypatch, "/api/ops/doctor")
    assert handler.status == 403

    status_handler = _call_get(monkeypatch, "/api/ops/status")
    body = status_handler.get_json()
    assert body["allowed"] is False
    assert body["log"] == "" and body["backup_path"] is None

    dl = _call_get(monkeypatch, "/api/ops/backup/download")
    assert dl.status == 403
