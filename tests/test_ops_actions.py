from __future__ import annotations

import io
import json
import subprocess
import threading
import time
from urllib.parse import urlparse

import pytest


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
        with ops_actions._STATE_LOCK:
            ops_actions._STATE.update(
                {
                    "action": None,
                    "status": "idle",
                    "started_at": None,
                    "finished_at": None,
                    "returncode": None,
                    "log": "",
                    "backup_path": None,
                    "error": None,
                }
            )
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
        status = ops_actions.get_status()
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

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_OPS_ACTIONS", "1")
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

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_OPS_ACTIONS", "1")
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

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_OPS_ACTIONS", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    state_dir = tmp_path / "webui_state"
    monkeypatch.setattr(config, "STATE_DIR", state_dir)

    def _write_zip(cmd, kwargs):
        out_idx = cmd.index("--output") + 1
        out_path = cmd[out_idx]
        # Backup file must land under the WebUI state root, never the repo.
        assert str(state_dir) in out_path
        with open(out_path, "wb") as f:
            f.write(b"PK\x03\x04fake-zip-bytes")

    fake_proc = _make_fake_popen(["backing up...\n"], returncode=0, on_spawn=_write_zip)
    monkeypatch.setattr(ops_actions.subprocess, "Popen", fake_proc)

    handler, data = _call_post(monkeypatch, "/api/ops/backup")
    assert handler.status == 200
    final = _wait_until_not_running()
    assert final["status"] == "completed"
    assert final["backup_path"]

    dl_handler = _call_get(monkeypatch, "/api/ops/backup/download")
    assert dl_handler.status == 200
    assert bytes(dl_handler.body) == b"PK\x03\x04fake-zip-bytes"
    assert dl_handler.header("Content-Type") == "application/zip"
    disposition = dl_handler.header("Content-Disposition") or ""
    assert "attachment" in disposition


def test_ops_backup_download_404_before_any_backup(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_ALLOW_OPS_ACTIONS", "1")
    handler = _call_get(monkeypatch, "/api/ops/backup/download")
    assert handler.status == 404


def test_ops_action_failure_is_reported(monkeypatch, tmp_path):
    from api import ops_actions, profiles

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_OPS_ACTIONS", "1")
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

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_OPS_ACTIONS", "1")
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
        ops_actions.start_action("doctor")

    assert not ops_actions._ACTION_LOCK.locked(), "lock leaked after get_active_hermes_home() raised"

    # Prove the leak doesn't linger: a normal call right after must still work.
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(ops_actions.subprocess, "Popen", _make_fake_popen(["ok\n"], returncode=0))
    started, _status = ops_actions.start_action("doctor")
    assert started is True
    _wait_until_not_running()


def test_ops_action_lock_is_released_when_backups_dir_raises(monkeypatch, tmp_path):
    """Same finding, the other call inside the vulnerable window: _backups_dir()
    (which mkdir()s) raising (disk full, permissions) must also not leak the
    lock."""
    from api import ops_actions, profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(ops_actions, "_backups_dir", lambda: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError):
        ops_actions.start_action("backup")

    assert not ops_actions._ACTION_LOCK.locked(), "lock leaked after _backups_dir() raised"


def test_ops_action_timeout_reaps_zombie_via_background_thread(monkeypatch, tmp_path):
    """Audit finding (MEDIUM): after proc.kill(), if the 5s proc.wait(timeout=5)
    also times out, the code assumed returncode=-9 without ever reaping the
    process, leaving a zombie. A background thread must now call a blocking
    proc.wait() to actually reclaim it."""
    from api import ops_actions, profiles

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_OPS_ACTIONS", "1")
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
    monkeypatch.setenv("HERMES_WEBUI_ALLOW_OPS_ACTIONS", "1")
    fake_proc = _make_fake_popen(["some possibly sensitive log line\n"], returncode=1)
    monkeypatch.setattr(ops_actions.subprocess, "Popen", fake_proc)

    _call_post(monkeypatch, "/api/ops/doctor")
    _wait_until_not_running()

    monkeypatch.delenv("HERMES_WEBUI_ALLOW_OPS_ACTIONS", raising=False)
    handler = _call_get(monkeypatch, "/api/ops/status")
    data = handler.get_json()
    assert data["allowed"] is False
    assert data["log"] == ""
    assert data["error"] is None
    assert data["backup_path"] is None
    # Non-sensitive fields stay visible so the UI can still show *something*.
    assert data["status"] == "failed"
    assert data["action"] == "doctor"

    monkeypatch.setenv("HERMES_WEBUI_ALLOW_OPS_ACTIONS", "1")
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
