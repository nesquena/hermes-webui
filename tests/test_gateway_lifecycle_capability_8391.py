"""Gateway lifecycle capability (#8391).

The Settings Gateway card can monitor a Gateway that WebUI does not run (an
explicit remote health URL), while the Start/Stop/Restart actions and the
health-banner restart launch the LOCAL ``hermes gateway`` CLI. WebUI must only
offer — and only perform — lifecycle actions when it can prove it controls the
Gateway it is monitoring. These tests pin:

* one server-owned capability (``agent_health.gateway_lifecycle_capability``);
* the capability exposed on ``/api/gateway/status`` and ``/api/health/agent``;
* every lifecycle entrypoint (Settings routes, ``/api/health/restart``, the
  shared restart helper, the Agent-update caller) refusing BEFORE any lock or
  subprocess when the capability is false;
* locally managed Gateways keeping today's behaviour;
* the Settings renderer showing a read-only explanation instead of buttons on
  every status branch of an external Gateway;
* a loopback Gateway with no ownership proof yet reporting its own reason
  (``local_gateway_unproven``, still unmanaged everywhere) so the Settings card
  can tell a stopped Gateway of this machine to start once with the exact CLI
  command for the target profile (``start_command``: ``hermes gateway start``,
  or ``hermes --profile <name> gateway start`` for a named profile, the same
  rule the Settings Start button uses) (follow-up D).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import types
from pathlib import Path
from urllib.parse import urlparse

import pytest

from tests.js_source_extract import extract_function

REPO_ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

_REMOTE_ENV_VARS = (
    "GATEWAY_HEALTH_URL",
    "HERMES_GATEWAY_HEALTH_URL",
    "HERMES_API_URL",
    "HERMES_WEBUI_GATEWAY_BASE_URL",
)


@pytest.fixture(autouse=True)
def _clean_gateway_env(monkeypatch, tmp_path):
    from api import agent_health

    for var in _REMOTE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # Each test gets its own persisted-ownership record so a proof recorded by
    # one test can never authorize another.
    ownership_path = tmp_path / "webui-state" / "gateway_lifecycle_ownership.json"
    monkeypatch.setattr(
        agent_health,
        "_gateway_ownership_record_path",
        lambda: ownership_path,
        raising=False,
    )
    yield


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


def _fake_agent(tmp_path):
    agent_dir = tmp_path / "hermes-agent"
    cli_dir = agent_dir / "hermes_cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "main.py").write_text("print('fake hermes cli')\n", encoding="utf-8")
    return agent_dir


def _call_post(monkeypatch, path: str, body: dict | None = None):
    from api import routes

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body or {})
    handler = _FakeHandler()
    routes.handle_post(handler, urlparse(path))
    return handler, handler.get_json()


def _no_local_pid(monkeypatch):
    from api import agent_health

    calls = []
    monkeypatch.setattr(
        agent_health,
        "get_active_profile_gateway_running_pid",
        lambda profile=None: calls.append(profile) or None,
    )
    return calls


def _local_pid(monkeypatch, pid=4242):
    from api import agent_health

    calls = []
    monkeypatch.setattr(
        agent_health,
        "get_active_profile_gateway_running_pid",
        lambda profile=None: calls.append(profile) or pid,
    )
    return calls


# ── capability ────────────────────────────────────────────────────────────


def test_capability_is_managed_for_local_only_setup(monkeypatch):
    from api import agent_health

    cap = agent_health.gateway_lifecycle_capability()

    assert cap["managed"] is True
    assert cap["reason"] == "local_gateway"


@pytest.mark.parametrize("env_var", _REMOTE_ENV_VARS)
def test_capability_is_unmanaged_for_each_remote_gateway_env_var(monkeypatch, env_var):
    from api import agent_health

    _local_pid(monkeypatch)  # even a running local gateway is NOT proof for a remote URL
    monkeypatch.setenv(env_var, "http://gateway.internal:8642")

    cap = agent_health.gateway_lifecycle_capability()

    assert cap["managed"] is False
    assert cap["reason"] == "external_gateway"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8642",
        "http://localhost:8642/health",
        "http://[::1]:8642/",
        "http://127.0.0.2:8642",
    ],
)
def test_capability_is_managed_for_loopback_url_with_running_local_gateway(monkeypatch, url):
    from api import agent_health

    calls = _local_pid(monkeypatch)
    monkeypatch.setenv("HERMES_API_URL", url)

    cap = agent_health.gateway_lifecycle_capability()

    assert cap["managed"] is True
    assert cap["reason"] == "local_gateway"
    assert calls == [None]


def test_capability_is_unmanaged_for_loopback_url_without_local_gateway_process(monkeypatch):
    """A loopback URL alone is not proof (Docker port maps, SSH tunnels). It
    stays unmanaged, with its own reason so the UI can explain the one-time
    ``hermes gateway start`` instead of "managed outside WebUI"."""
    from api import agent_health

    _no_local_pid(monkeypatch)
    monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:8642")

    cap = agent_health.gateway_lifecycle_capability()

    assert cap["managed"] is False
    assert cap["reason"] == "local_gateway_unproven"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1.evil.example:8642",
        "http://localhost.evil.example:8642",
        "127.0.0.1:8642",
        "http://10.0.0.5:8642",
        "http://gateway:8642",
    ],
)
def test_capability_does_not_treat_lookalike_or_lan_hosts_as_loopback(monkeypatch, url):
    from api import agent_health

    _local_pid(monkeypatch)
    monkeypatch.setenv("GATEWAY_HEALTH_URL", url)

    assert agent_health.gateway_lifecycle_capability()["managed"] is False


def test_capability_pins_explicit_profile_for_loopback_proof(monkeypatch):
    from api import agent_health

    calls = _local_pid(monkeypatch)
    monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:8642")

    assert agent_health.gateway_lifecycle_capability(profile="work")["managed"] is True
    assert calls == ["work"]


# ── status payloads expose the capability ─────────────────────────────────


def test_gateway_status_payload_exposes_unmanaged_capability_for_remote(monkeypatch):
    from api import routes

    monkeypatch.setenv("HERMES_API_URL", "https://gateway.example.com")
    monkeypatch.setattr(
        routes,
        "build_agent_health_payload",
        lambda: {"alive": False, "details": {"state": "down", "reason": "remote_gateway_unreachable"}},
    )

    payload = routes._gateway_status_payload()

    assert payload["lifecycle"] == {"managed": False, "reason": "external_gateway"}
    # Status semantics are unchanged: still reported as configured + not running.
    assert payload["configured"] is True
    assert payload["running"] is False


def test_gateway_status_payload_exposes_managed_capability_for_local(monkeypatch):
    from api import routes

    monkeypatch.setattr(
        routes,
        "build_agent_health_payload",
        lambda: {"alive": True, "details": {"state": "alive"}},
    )

    payload = routes._gateway_status_payload()

    assert payload["lifecycle"] == {"managed": True, "reason": "local_gateway"}
    assert payload["running"] is True


def test_agent_health_route_exposes_gateway_lifecycle(monkeypatch):
    from api import routes

    monkeypatch.setenv("HERMES_API_URL", "https://gateway.example.com")
    monkeypatch.setattr(
        routes,
        "build_agent_health_payload",
        lambda: {"alive": False, "checked_at": "x", "details": {"reason": "remote_gateway_unreachable"}},
    )
    monkeypatch.setattr(routes, "gateway_chat_config_status", lambda: {})
    handler = _FakeHandler()

    routes.handle_get(handler, urlparse("/api/health/agent"))
    data = handler.get_json()

    assert handler.status == 200
    assert data["gateway_lifecycle"] == {"managed": False, "reason": "external_gateway"}


# ── Settings lifecycle routes refuse without the capability ───────────────


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
@pytest.mark.parametrize("alive", [True, False])
def test_lifecycle_routes_refuse_remote_gateway_without_subprocess(monkeypatch, tmp_path, action, alive):
    from api import config, routes

    agent_dir = _fake_agent(tmp_path)
    monkeypatch.setattr(config, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(config, "PYTHON_EXE", sys.executable)
    monkeypatch.setenv("GATEWAY_HEALTH_URL", "http://gateway.internal:8642/health")
    monkeypatch.setattr(
        routes,
        "build_agent_health_payload",
        lambda: {"alive": alive, "details": {"reason": "remote_gateway" if alive else "remote_gateway_unreachable"}},
    )
    spawned = []
    monkeypatch.setattr(routes.subprocess, "run", lambda cmd, **kw: spawned.append(cmd))
    monkeypatch.setattr(routes.subprocess, "Popen", lambda *a, **kw: spawned.append(a))

    handler, data = _call_post(monkeypatch, f"/api/gateway/{action}", {})

    assert handler.status == 409
    assert data["ok"] is False
    assert data["action"] == action
    assert data["code"] == "gateway_externally_managed"
    assert "managed outside WebUI" in data["error"]
    assert data["lifecycle"] == {"managed": False, "reason": "external_gateway"}
    assert spawned == []
    # Refusal happens before the single-flight lock is taken.
    assert routes._GATEWAY_ACTION_LOCK.locked() is False


def test_lifecycle_route_refusal_does_not_wait_on_held_lock(monkeypatch):
    """Policy is checked before the lock: an external Gateway gets 409-managed,
    not the 409-in-progress contention response."""
    from api import routes

    monkeypatch.setenv("HERMES_API_URL", "http://gateway.internal:8642")
    assert routes._GATEWAY_ACTION_LOCK.acquire(blocking=False)
    try:
        handler, data = _call_post(monkeypatch, "/api/gateway/restart", {})
    finally:
        routes._GATEWAY_ACTION_LOCK.release()

    assert handler.status == 409
    assert data["code"] == "gateway_externally_managed"


def test_lifecycle_route_runs_local_cli_for_managed_local_gateway(monkeypatch, tmp_path):
    from api import config, profiles, routes

    agent_dir = _fake_agent(tmp_path)
    monkeypatch.setattr(config, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(config, "PYTHON_EXE", sys.executable)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "work")
    monkeypatch.setattr(
        routes,
        "build_agent_health_payload",
        lambda: {"alive": True, "details": {"state": "alive"}},
    )
    calls = []
    monkeypatch.setattr(
        routes.subprocess,
        "run",
        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    handler, data = _call_post(monkeypatch, "/api/gateway/restart", {})

    assert handler.status == 200
    assert data["ok"] is True
    assert calls and calls[0][-4:] == ["--profile", "work", "gateway", "restart"]
    assert data["status"]["lifecycle"]["managed"] is True


def test_lifecycle_route_runs_local_cli_for_loopback_url_with_local_gateway(monkeypatch, tmp_path):
    from api import config, profiles, routes

    agent_dir = _fake_agent(tmp_path)
    monkeypatch.setattr(config, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(config, "PYTHON_EXE", sys.executable)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    _local_pid(monkeypatch)
    monkeypatch.setattr(
        routes,
        "build_agent_health_payload",
        lambda: {"alive": True, "details": {"state": "alive", "reason": "remote_gateway"}},
    )
    calls = []
    monkeypatch.setattr(
        routes.subprocess,
        "run",
        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    handler, data = _call_post(monkeypatch, "/api/gateway/stop", {})

    assert handler.status == 200
    assert calls and calls[0][-2:] == ["gateway", "stop"]


# ── shared restart helper + health banner route ───────────────────────────


def test_shared_restart_helper_refuses_external_gateway_before_lock(monkeypatch):
    import api.gateway_restart as gateway_restart

    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    monkeypatch.setenv("HERMES_API_URL", "http://gateway.internal:8642")

    def fail_popen(*args, **kwargs):
        raise AssertionError("external gateway must not launch a local CLI")

    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fail_popen)

    # Even with the restart lock held by another caller, the policy answer wins.
    assert gateway_restart._GATEWAY_RESTART_LOCK.acquire(blocking=False)
    try:
        busy_result = gateway_restart.restart_active_profile_gateway()
    finally:
        gateway_restart._GATEWAY_RESTART_LOCK.release()
    result = gateway_restart.restart_active_profile_gateway(profile="default")

    for outcome in (busy_result, result):
        assert outcome["status"] == "external"
        assert "managed outside WebUI" in outcome["message"]
        assert outcome["lifecycle"] == {"managed": False, "reason": "external_gateway"}
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_shared_restart_helper_keeps_local_restart(monkeypatch):
    import api.gateway_restart as gateway_restart

    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    launched = []

    class _Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return "ok", ""

    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: "/mock/hermes/home")
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(
        gateway_restart.subprocess,
        "Popen",
        lambda args, **kw: launched.append(args) or _Proc(),
    )

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "completed"
    assert launched and launched[0][-2:] == ["gateway", "restart"]


def test_health_restart_route_maps_external_outcome_to_409(monkeypatch):
    import api.routes as routes

    responses = []
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, **kw: responses.append((payload, kw.get("status", 200))) or True,
    )
    monkeypatch.setattr(
        routes,
        "restart_active_profile_gateway",
        lambda: {
            "status": "external",
            "message": "This Gateway is managed outside WebUI. Restart it where it runs.",
            "lifecycle": {"managed": False, "reason": "external_gateway"},
        },
    )

    assert routes._handle_health_restart(types.SimpleNamespace(headers={})) is True
    assert responses == [
        (
            {
                "ok": False,
                "error": "This Gateway is managed outside WebUI. Restart it where it runs.",
                "code": "gateway_externally_managed",
            },
            409,
        )
    ]


def test_health_restart_route_refuses_real_external_gateway_without_subprocess(monkeypatch):
    import api.gateway_restart as gateway_restart
    import api.routes as routes

    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    monkeypatch.setenv("GATEWAY_HEALTH_URL", "http://gateway.internal:8642")
    monkeypatch.setattr(
        gateway_restart.subprocess,
        "Popen",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    handler = _FakeHandler()

    routes._handle_health_restart(handler)
    data = handler.get_json()

    assert handler.status == 409
    assert data["ok"] is False
    assert data["code"] == "gateway_externally_managed"


# ── Agent-update caller ───────────────────────────────────────────────────


def test_agent_update_does_not_launch_or_claim_restart_for_external_gateway(monkeypatch):
    import api.updates as upd

    calls = []
    sleeps = []

    def fake_restart(*, profile=None):
        calls.append(profile)
        return {
            "status": "external",
            "message": "This Gateway is managed outside WebUI. Restart it where it runs.",
            "lifecycle": {"managed": False, "reason": "external_gateway"},
        }

    monkeypatch.setattr(upd, "restart_active_profile_gateway", fake_restart)
    monkeypatch.setattr(upd.time, "sleep", sleeps.append)
    monkeypatch.setattr(upd, "get_active_profile_gateway_running_pid", lambda *, profile=None: None)

    ok, result = upd._ensure_gateway_restart_for_agent_update()

    # The Agent checkout update itself succeeded; WebUI just doesn't own the
    # monitored Gateway, so it neither retries/launches nor reports a restart.
    assert ok is True
    assert result["status"] == "external"
    assert result["status"] not in {"completed", "in_progress"}
    assert calls == ["default"]
    assert sleeps == []


def test_agent_update_real_helper_makes_zero_subprocess_calls_for_external_gateway(monkeypatch):
    import api.gateway_restart as gateway_restart
    import api.updates as upd

    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    monkeypatch.setenv("HERMES_API_URL", "http://gateway.internal:8642")
    monkeypatch.setattr(
        gateway_restart.subprocess,
        "Popen",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    monkeypatch.setattr(upd.time, "sleep", lambda s: None)
    monkeypatch.setattr(upd, "get_active_profile_gateway_running_pid", lambda *, profile=None: None)

    ok, result = upd._ensure_gateway_restart_for_agent_update()

    assert ok is True
    assert result["status"] == "external"


# ── persisted local ownership: Stop / crash / failed restart keep control ─
#
# Codex r1 (CORE): with a loopback URL, ownership used to be proven only by a
# live PID, so Stop (or a restart whose replacement failed to launch) made the
# capability false and Start / the restart retry returned 409. Once a running
# Gateway for this profile home has been observed behind this exact loopback
# endpoint, that ownership persists (WebUI state dir) through Stop, crashes,
# failed restarts and WebUI restarts, scoped to profile home + endpoint.


class _PidSwitch:
    """Stand-in for get_active_profile_gateway_running_pid with a mutable PID."""

    def __init__(self, pid=None):
        self.pid = pid
        self.calls = []

    def __call__(self, profile=None):
        self.calls.append(profile)
        return self.pid


def _pid_switch(monkeypatch, pid=None):
    from api import agent_health

    switch = _PidSwitch(pid)
    monkeypatch.setattr(agent_health, "get_active_profile_gateway_running_pid", switch)
    return switch


def _pin_profile_homes(monkeypatch, active_home, homes_by_name=None, active_name="default"):
    from api import profiles

    homes_by_name = dict(homes_by_name or {})
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: active_home)
    # The same accessor routes._run_gateway_lifecycle_command reads; it also
    # picks the start_command an unproven result carries.
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: active_name)
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: homes_by_name.get(name, active_home),
    )


def _managed():
    return {"managed": True, "reason": "local_gateway"}


def _external():
    return {"managed": False, "reason": "external_gateway"}


def _unproven(start_command="hermes gateway start"):
    return {"managed": False, "reason": "local_gateway_unproven", "start_command": start_command}


def test_loopback_gateway_stopped_by_webui_can_be_started_again(monkeypatch, tmp_path):
    from api import agent_health, config, profiles, routes

    agent_dir = _fake_agent(tmp_path)
    monkeypatch.setattr(config, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(config, "PYTHON_EXE", sys.executable)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    _pin_profile_homes(monkeypatch, tmp_path / "home")
    # The documented gateway-chat configuration.
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    pid = _pid_switch(monkeypatch, 4242)
    monkeypatch.setattr(
        routes,
        "build_agent_health_payload",
        lambda: {"alive": pid.pid is not None, "details": {}},
    )
    actions = []

    def fake_run(cmd, **kw):
        actions.append(cmd[-1])
        pid.pid = None if cmd[-1] == "stop" else 5151
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(routes.subprocess, "run", fake_run)

    handler, data = _call_post(monkeypatch, "/api/gateway/stop", {})
    assert handler.status == 200, data
    # Stopped now: the refreshed status still offers lifecycle actions.
    assert data["status"]["lifecycle"] == _managed()
    assert agent_health.gateway_lifecycle_capability() == _managed()

    handler, data = _call_post(monkeypatch, "/api/gateway/start", {})
    assert handler.status == 200, data
    assert data["ok"] is True
    assert actions == ["stop", "start"]


def test_loopback_ownership_survives_crash_and_is_persisted_on_disk(monkeypatch, tmp_path):
    from api import agent_health

    home = tmp_path / "home"
    _pin_profile_homes(monkeypatch, home)
    monkeypatch.setenv("HERMES_API_URL", "http://localhost:8642/health")
    pid = _pid_switch(monkeypatch, 4242)

    assert agent_health.gateway_lifecycle_capability() == _managed()
    pid.pid = None  # the Gateway crashed / was stopped outside the Settings card
    assert agent_health.gateway_lifecycle_capability() == _managed()

    # The proof lives on disk (survives a WebUI restart), scoped to the
    # resolved profile home and the exact monitored endpoint.
    record = json.loads(agent_health._gateway_ownership_record_path().read_text(encoding="utf-8"))
    owners = record["owners"]
    assert list(owners) == [str(home.resolve())]
    assert owners[str(home.resolve())]["endpoint"] == "http://localhost:8642"


def test_health_restart_can_be_retried_after_replacement_failed(monkeypatch, tmp_path):
    import api.gateway_restart as gateway_restart

    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    home = tmp_path / "home"
    _pin_profile_homes(monkeypatch, home)
    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(gateway_restart, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    pid = _pid_switch(monkeypatch, 4242)
    launches = []

    class _Proc:
        def __init__(self, returncode):
            self.returncode = returncode

        def communicate(self, timeout=None):
            return "", ("replacement failed to launch" if self.returncode else "")

    def fake_popen(args, **kw):
        launches.append(args)
        if len(launches) == 1:
            pid.pid = None  # old Gateway stopped; its replacement never came up
            return _Proc(1)
        pid.pid = 5151
        return _Proc(0)

    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fake_popen)

    first = gateway_restart.restart_active_profile_gateway()
    retry = gateway_restart.restart_active_profile_gateway()

    assert first["status"] == "failed"
    assert retry["status"] == "completed", retry
    assert len(launches) == 2


def test_agent_update_retry_recovers_loopback_gateway_after_failed_replacement(monkeypatch, tmp_path):
    import api.gateway_restart as gateway_restart
    import api.updates as upd

    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    home = tmp_path / "home"
    _pin_profile_homes(monkeypatch, home, {"default": home})
    monkeypatch.setattr(gateway_restart, "get_hermes_home_for_profile", lambda name: home)
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(upd, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(upd.time, "sleep", lambda s: None)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    pid = _pid_switch(monkeypatch, 4242)
    monkeypatch.setattr(upd, "get_active_profile_gateway_running_pid", pid)
    launches = []

    class _Proc:
        def __init__(self, returncode):
            self.returncode = returncode

        def communicate(self, timeout=None):
            return "", ("replacement failed to launch" if self.returncode else "")

    def fake_popen(args, **kw):
        launches.append(args)
        if len(launches) == 1:
            pid.pid = None
            return _Proc(1)
        pid.pid = 5151
        return _Proc(0)

    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fake_popen)

    ok, result = upd._ensure_gateway_restart_for_agent_update()

    assert ok is True, result
    assert result["status"] == "completed"
    assert result["retry_attempted"] is True
    assert len(launches) == 2


def test_loopback_gateway_never_seen_running_stays_read_only(monkeypatch, tmp_path):
    """No proof yet (stopped, never observed running behind this endpoint):
    a loopback URL alone may be a Docker port map or SSH tunnel -> 409."""
    from api import routes

    _pin_profile_homes(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    _pid_switch(monkeypatch, None)
    spawned = []
    monkeypatch.setattr(routes.subprocess, "run", lambda cmd, **kw: spawned.append(cmd))

    handler, data = _call_post(monkeypatch, "/api/gateway/start", {})

    assert handler.status == 409
    assert data["code"] == "gateway_externally_managed"
    assert data["lifecycle"] == _unproven()
    assert spawned == []


@pytest.mark.parametrize(
    "url",
    [
        "http://gateway.internal:8642",
        "http://10.0.0.5:8642",
        "http://192.168.1.20:8642",
        "http://127.0.0.1.evil.example:8642",
        "http://localhost.evil.example:8642",
        "127.0.0.1:8642",
        "ftp://127.0.0.1:8642",
    ],
)
@pytest.mark.parametrize("pid_alive", [True, False])
def test_persisted_loopback_proof_never_authorizes_remote_lan_or_lookalike(
    monkeypatch, tmp_path, url, pid_alive
):
    from api import agent_health, routes

    _pin_profile_homes(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    pid = _pid_switch(monkeypatch, 4242)
    assert agent_health.gateway_lifecycle_capability() == _managed()  # proof recorded

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", url)
    pid.pid = 4242 if pid_alive else None
    spawned = []
    monkeypatch.setattr(routes.subprocess, "run", lambda cmd, **kw: spawned.append(cmd))

    assert agent_health.gateway_lifecycle_capability() == _external()
    handler, data = _call_post(monkeypatch, "/api/gateway/start", {})
    assert handler.status == 409
    assert data["code"] == "gateway_externally_managed"
    assert spawned == []


def test_persisted_proof_is_scoped_to_the_exact_loopback_endpoint(monkeypatch, tmp_path):
    from api import agent_health

    _pin_profile_homes(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:8642")
    pid = _pid_switch(monkeypatch, 4242)
    assert agent_health.gateway_lifecycle_capability() == _managed()
    pid.pid = None

    # Another loopback port (e.g. a Docker port map or SSH tunnel) is a
    # different target: the proof does not transfer, and the target change
    # invalidates it -- going back requires a fresh proof. Unproven loopback
    # targets stay unmanaged (their own reason only changes the UI copy).
    monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:9642")
    assert agent_health.gateway_lifecycle_capability() == _unproven()
    monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:8642")
    assert agent_health.gateway_lifecycle_capability() == _unproven()


def test_persisted_proof_is_scoped_to_the_profile_home(monkeypatch, tmp_path):
    from api import agent_health

    home_a = tmp_path / "profiles" / "alpha"
    home_b = tmp_path / "profiles" / "beta"
    monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:8642")
    pid = _pid_switch(monkeypatch, 4242)
    _pin_profile_homes(monkeypatch, home_a, {"alpha": home_a, "beta": home_b})
    assert agent_health.gateway_lifecycle_capability() == _managed()
    pid.pid = None

    assert agent_health.gateway_lifecycle_capability() == _managed()
    assert agent_health.gateway_lifecycle_capability(profile="alpha") == _managed()
    # Same endpoint, different profile home: never proven -> unmanaged. The
    # start hint names the profile the capability was computed for.
    assert agent_health.gateway_lifecycle_capability(profile="beta") == _unproven(
        "hermes --profile beta gateway start"
    )
    _pin_profile_homes(monkeypatch, home_b, {"alpha": home_a, "beta": home_b})
    assert agent_health.gateway_lifecycle_capability() == _unproven()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # Unparseable record: the read raises -> fail closed (external).
        ("{not json", "external_gateway"),
        # Parseable but malformed record: reads as empty -> no proof.
        ("[]", "local_gateway_unproven"),
        ('{"owners": []}', "local_gateway_unproven"),
        ('{"owners": {"x": 1}}', "local_gateway_unproven"),
    ],
)
def test_unreadable_ownership_record_fails_closed(monkeypatch, tmp_path, payload, expected):
    from api import agent_health

    _pin_profile_homes(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:8642")
    _pid_switch(monkeypatch, None)
    path = agent_health._gateway_ownership_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")

    expected_result = _unproven() if expected == "local_gateway_unproven" else _external()
    assert agent_health.gateway_lifecycle_capability() == expected_result


def test_ownership_record_never_stores_url_credentials(monkeypatch, tmp_path):
    from api import agent_health

    _pin_profile_homes(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("HERMES_API_URL", "http://alice:s3cret@127.0.0.1:8642?token=abc")
    pid = _pid_switch(monkeypatch, 4242)
    assert agent_health.gateway_lifecycle_capability() == _managed()
    pid.pid = None
    assert agent_health.gateway_lifecycle_capability() == _managed()

    text = agent_health._gateway_ownership_record_path().read_text(encoding="utf-8")
    assert "s3cret" not in text and "alice" not in text and "token" not in text


def test_unwritable_ownership_record_keeps_live_proof_and_fails_closed_after(monkeypatch, tmp_path):
    from api import agent_health

    _pin_profile_homes(monkeypatch, tmp_path / "home")
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        agent_health, "_gateway_ownership_record_path", lambda: blocker / "ownership.json"
    )
    monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:8642")
    pid = _pid_switch(monkeypatch, 4242)

    assert agent_health.gateway_lifecycle_capability() == _managed()
    pid.pid = None
    assert agent_health.gateway_lifecycle_capability() == _external()


def test_local_only_setup_never_reads_or_writes_ownership_record(monkeypatch, tmp_path):
    from api import agent_health

    _pin_profile_homes(monkeypatch, tmp_path / "home")
    _pid_switch(monkeypatch, None)

    assert agent_health.gateway_lifecycle_capability() == _managed()
    assert not agent_health._gateway_ownership_record_path().exists()


# ── frontend ──────────────────────────────────────────────────────────────


def _render_gateway_card(status: dict, *, strings: dict | None = None, esc_src: str | None = None) -> str:
    fns = "\n".join(
        extract_function(PANELS_JS, name)
        for name in (
            "_gatewayStatusReason",
            "_gatewayActionButton",
            "_gatewayLockNote",
            "_gatewayLocalUnprovenNoteHtml",
            "_gatewayActionControls",
            "_renderGatewayStatus",
        )
    )
    harness = f"""
const card = {{ innerHTML: '' }};
const $ = (id) => id === 'gatewayStatusCard' ? card : null;
const _strings = {json.dumps(strings or {})};
const t = (k) => Object.prototype.hasOwnProperty.call(_strings, k) ? _strings[k] : '[' + k + ']';
{esc_src or "const esc = (s) => String(s);"}
const li = (name, size) => '<i data-icon="' + name + '" data-size="' + size + '"></i>';
const jsArg = (s) => JSON.stringify(s);
let _gatewayActionInFlight = false;
{fns}
_renderGatewayStatus({json.dumps(status)});
process.stdout.write(card.innerHTML);
"""
    proc = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


_EXTERNAL = {"managed": False, "reason": "external_gateway"}
_LOCAL = {"managed": True, "reason": "local_gateway"}

_STATUS_BRANCHES = {
    "running": {"running": True, "configured": True, "platforms": [], "session_count": 0, "health": {"reason": "remote_gateway"}},
    "unreachable": {"running": False, "configured": True, "health": {"reason": "remote_gateway_unreachable"}},
    "not_running": {"running": False, "configured": True, "health": {"reason": "gateway_not_running"}},
    "not_configured": {"running": False, "configured": False, "health": {"reason": "gateway_not_configured"}},
}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("branch", sorted(_STATUS_BRANCHES))
def test_renderer_external_gateway_is_read_only_on_every_branch(branch):
    html = _render_gateway_card({**_STATUS_BRANCHES[branch], "lifecycle": _EXTERNAL})

    assert "data-gateway-action" not in html
    assert "_gatewayAction(" not in html
    assert "[gateway_external_managed]" in html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("branch", sorted(_STATUS_BRANCHES))
def test_renderer_missing_capability_fails_closed(branch):
    html = _render_gateway_card(dict(_STATUS_BRANCHES[branch]))

    assert "data-gateway-action" not in html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_renderer_local_managed_gateway_keeps_lifecycle_actions():
    running = _render_gateway_card({**_STATUS_BRANCHES["running"], "lifecycle": _LOCAL})
    stopped = _render_gateway_card({**_STATUS_BRANCHES["not_running"], "lifecycle": _LOCAL})

    assert 'data-gateway-action="stop"' in running
    assert 'data-gateway-action="restart"' in running
    assert 'data-gateway-action="start"' not in running
    assert 'data-gateway-action="start"' in stopped
    assert "[gateway_external_managed]" not in running + stopped


def test_health_banner_hides_restart_button_unless_gateway_is_managed():
    body = extract_function(UI_JS, "_showAgentHealthAlert")
    assert "btnRestartGateway" in body
    assert "gateway_lifecycle" in body
    assert "managed===true" in body.replace(" ", "")


def test_gateway_external_managed_copy_exists_in_every_locale():
    assert I18N_JS.count("gateway_external_managed:") == 15


# ── follow-up D: loopback Gateway with no ownership proof yet ─────────────
#
# An install that points WebUI at its own Gateway through a loopback URL and
# upgrades while that Gateway is stopped has no proof yet. It stays unmanaged
# (no change to who may start/stop/restart), but reports its own reason so
# the Settings card can say "start it once with `hermes gateway start`"
# instead of "managed outside WebUI".


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8642",
        "http://localhost:8642/health",
        "http://[::1]:8642/",
        "https://127.0.0.1:8642",
    ],
)
def test_unproven_loopback_reports_its_own_reason_and_stays_unmanaged(monkeypatch, tmp_path, url):
    from api import agent_health

    _pin_profile_homes(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", url)
    pid = _pid_switch(monkeypatch, None)

    assert agent_health.gateway_lifecycle_capability() == _unproven()
    assert pid.calls == [None]
    # Checking an unproven target never records proof.
    assert not agent_health._gateway_ownership_record_path().exists()


def test_unproven_loopback_becomes_managed_once_its_gateway_is_seen_running(monkeypatch, tmp_path):
    from api import agent_health

    _pin_profile_homes(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    pid = _pid_switch(monkeypatch, None)
    assert agent_health.gateway_lifecycle_capability() == _unproven()

    pid.pid = 4242  # the user ran `hermes gateway start` once
    assert agent_health.gateway_lifecycle_capability() == _managed()
    pid.pid = None  # the recorded proof keeps Start available after a stop
    assert agent_health.gateway_lifecycle_capability() == _managed()


def test_loopback_with_persisted_proof_and_no_pid_is_managed(monkeypatch, tmp_path):
    from api import agent_health

    home = tmp_path / "home"
    _pin_profile_homes(monkeypatch, home)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    _pid_switch(monkeypatch, None)
    path = agent_health._gateway_ownership_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": agent_health._GATEWAY_OWNERSHIP_VERSION,
                "owners": {str(home.resolve()): {"endpoint": "http://127.0.0.1:8642", "proven_at": "x"}},
            }
        ),
        encoding="utf-8",
    )

    assert agent_health.gateway_lifecycle_capability() == _managed()


def test_loopback_with_live_pid_is_managed_not_unproven(monkeypatch, tmp_path):
    from api import agent_health

    _pin_profile_homes(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    _pid_switch(monkeypatch, 4242)

    assert agent_health.gateway_lifecycle_capability() == _managed()


@pytest.mark.parametrize("url", ["http://gateway.internal:8642", "http://10.0.0.5:8642", "127.0.0.1:8642"])
def test_non_loopback_without_proof_stays_external_not_unproven(monkeypatch, tmp_path, url):
    from api import agent_health

    _pin_profile_homes(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", url)
    _pid_switch(monkeypatch, None)

    assert agent_health.gateway_lifecycle_capability() == _external()


@pytest.mark.parametrize(
    "target",
    [
        "get_active_profile_gateway_running_pid",
        "_gateway_ownership_proven",
        "_gateway_profile_home_key",
        "_normalized_gateway_endpoint",
    ],
)
def test_unproven_reason_never_masks_a_failed_check(monkeypatch, tmp_path, target):
    """Any exception while proving ownership still fails closed to external."""
    from api import agent_health

    _pin_profile_homes(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    _pid_switch(monkeypatch, None)

    def boom(*args, **kwargs):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(agent_health, target, boom)

    assert agent_health.gateway_lifecycle_capability() == _external()


@pytest.mark.parametrize(("target", "value"), [("_gateway_profile_home_key", ""), ("_normalized_gateway_endpoint", None)])
def test_unresolvable_endpoint_or_home_key_stays_external(monkeypatch, tmp_path, target, value):
    from api import agent_health

    _pin_profile_homes(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    _pid_switch(monkeypatch, None)
    monkeypatch.setattr(agent_health, target, lambda *args, **kwargs: value)

    assert agent_health.gateway_lifecycle_capability() == _external()


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_lifecycle_routes_refuse_unproven_loopback_without_subprocess(monkeypatch, tmp_path, action):
    from api import config, routes

    agent_dir = _fake_agent(tmp_path)
    monkeypatch.setattr(config, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(config, "PYTHON_EXE", sys.executable)
    _pin_profile_homes(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    _pid_switch(monkeypatch, None)
    spawned = []
    monkeypatch.setattr(routes.subprocess, "run", lambda cmd, **kw: spawned.append(cmd))
    monkeypatch.setattr(routes.subprocess, "Popen", lambda *a, **kw: spawned.append(a))

    handler, data = _call_post(monkeypatch, f"/api/gateway/{action}", {})

    assert handler.status == 409
    assert data["ok"] is False
    assert data["code"] == "gateway_externally_managed"
    assert data["lifecycle"] == _unproven()
    assert spawned == []
    assert routes._GATEWAY_ACTION_LOCK.locked() is False


def test_health_restart_and_agent_update_refuse_unproven_loopback_without_subprocess(monkeypatch, tmp_path):
    import api.gateway_restart as gateway_restart
    import api.routes as routes
    import api.updates as upd
    from api import agent_health

    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    _pin_profile_homes(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    pid = _pid_switch(monkeypatch, None)
    monkeypatch.setattr(
        gateway_restart.subprocess,
        "Popen",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    monkeypatch.setattr(upd.time, "sleep", lambda s: None)
    monkeypatch.setattr(upd, "get_active_profile_gateway_running_pid", pid)

    helper = gateway_restart.restart_active_profile_gateway()
    assert helper["status"] == "external"
    assert helper["lifecycle"] == _unproven()

    handler = _FakeHandler()
    routes._handle_health_restart(handler)
    assert handler.status == 409
    assert handler.get_json()["code"] == "gateway_externally_managed"

    ok, result = upd._ensure_gateway_restart_for_agent_update()
    assert ok is True
    assert result["status"] == "external"
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False
    assert not agent_health._gateway_ownership_record_path().exists()


_UNPROVEN = {"managed": False, "reason": "local_gateway_unproven", "start_command": "hermes gateway start"}
_ESC_SRC = UI_JS[UI_JS.index("const esc=s=>") : UI_JS.index("\n", UI_JS.index("const esc=s=>"))]
_EN_LOCAL_UNPROVEN = (
    "WebUI hasn't confirmed this Gateway runs on this machine. "
    "If it does, start it once with {cmd}; WebUI can manage it after that."
)


def _note_wrapper(html: str) -> str:
    match = re.search(r'<div class="gateway-external-note"[^>]*>', html)
    assert match, html
    return re.sub(r' data-gateway-note="[^"]*"', "", match.group(0))


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("branch", ["not_running", "unreachable", "not_configured"])
def test_renderer_unproven_loopback_not_running_shows_one_time_start_note(branch):
    html = _render_gateway_card({**_STATUS_BRANCHES[branch], "lifecycle": _UNPROVEN})

    assert "[gateway_local_unproven]" in html
    assert "[gateway_external_managed]" not in html
    assert 'data-gateway-note="local_unproven"' in html
    assert "data-gateway-action" not in html
    assert "_gatewayAction(" not in html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_renderer_unproven_loopback_running_keeps_external_note():
    """A running loopback Gateway WebUI cannot prove (e.g. a Docker port map)
    really is managed elsewhere."""
    html = _render_gateway_card({**_STATUS_BRANCHES["running"], "lifecycle": _UNPROVEN})

    assert "[gateway_external_managed]" in html
    assert "[gateway_local_unproven]" not in html
    assert 'data-gateway-note="external"' in html
    assert "data-gateway-action" not in html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_renderer_unproven_note_uses_the_external_note_lock_markup():
    unproven = _render_gateway_card({**_STATUS_BRANCHES["not_running"], "lifecycle": _UNPROVEN})
    external = _render_gateway_card({**_STATUS_BRANCHES["not_running"], "lifecycle": _EXTERNAL})

    assert _note_wrapper(unproven) == _note_wrapper(external)
    assert '<i data-icon="lock" data-size="12"></i>' in unproven
    assert '<i data-icon="lock" data-size="12"></i>' in external


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_renderer_unproven_note_renders_command_as_inline_code_and_stays_escaped():
    html = _render_gateway_card(
        {**_STATUS_BRANCHES["not_running"], "lifecycle": _UNPROVEN},
        strings={"gateway_local_unproven": _EN_LOCAL_UNPROVEN},
        esc_src=_ESC_SRC,
    )
    assert html.count("<code") == 1
    assert ">hermes gateway start</code>" in html
    assert "{cmd}" not in html
    assert "WebUI hasn&#39;t confirmed this Gateway runs on this machine." in html
    assert "start it once with <code" in html

    hostile = '<img src=x onerror=alert(1)> then run {cmd} "now"'
    html = _render_gateway_card(
        {**_STATUS_BRANCHES["not_running"], "lifecycle": _UNPROVEN},
        strings={"gateway_local_unproven": hostile},
        esc_src=_ESC_SRC,
    )
    assert "<img" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "&quot;now&quot;" in html
    assert ">hermes gateway start</code>" in html


def test_renderer_unproven_command_chip_wraps_instead_of_overflowing_the_card():
    # A profile id can be up to 64 characters; a no-wrap chip pushed the whole
    # Settings page sideways at phone width (Fable #8633 r1). The chip must wrap.
    html = _render_gateway_card(
        {**_STATUS_BRANCHES["not_running"], "lifecycle": {
            **_UNPROVEN, "start_command": "hermes --profile " + "p" * 64 + " gateway start"}},
        strings={"gateway_local_unproven": _EN_LOCAL_UNPROVEN},
        esc_src=_ESC_SRC,
    )
    code_open = re.search(r'<code style="([^"]*)">', html)
    assert code_open, html
    style = code_open.group(1).replace(" ", "")
    assert "white-space:nowrap" not in style
    assert "white-space:pre-wrap" in style
    assert "overflow-wrap:anywhere" in style


def test_renderer_unproven_command_keeps_the_profile_flag_whole():
    # The chip may wrap, but never inside `--profile` (a bare `--` at a line end
    # reads as the end-of-options marker). The name after it may still wrap.
    name = "personal-assistant-macos"
    html = _render_gateway_card(
        {**_STATUS_BRANCHES["not_running"], "lifecycle": {
            **_UNPROVEN, "start_command": f"hermes --profile {name} gateway start"}},
        strings={"gateway_local_unproven": _EN_LOCAL_UNPROVEN},
        esc_src=_ESC_SRC,
    )
    assert '<span style="white-space:nowrap">--profile</span>' in html
    assert f"--profile</span> {name} gateway start</code>" in html
    text = re.sub(r"<[^>]+>", "", re.search(r"<code[^>]*>(.*?)</code>", html).group(1))
    assert text == f"hermes --profile {name} gateway start"

    hostile = _render_gateway_card(
        {**_STATUS_BRANCHES["not_running"], "lifecycle": {
            **_UNPROVEN, "start_command": "hermes --<img src=x onerror=1> --a\"b"}},
        strings={"gateway_local_unproven": _EN_LOCAL_UNPROVEN},
        esc_src=_ESC_SRC,
    )
    assert "<img" not in hostile
    assert "--&lt;img src=x onerror=1&gt;" in hostile
    assert '<span style="white-space:nowrap">--a</span>&quot;b' in hostile


def test_renderer_unproven_branch_keys_on_reason_and_running():
    body = extract_function(PANELS_JS, "_gatewayActionControls").replace(" ", "")
    assert "lifecycle.reason==='local_gateway_unproven'&&!(r&&r.running)" in body
    assert "t('gateway_external_managed')" in body


def _locale_blocks() -> dict[str, str]:
    blocks: dict[str, list[str]] = {}
    current = None
    for line in I18N_JS.splitlines():
        match = re.match(r"^  (['\"]?[A-Za-z_-]+['\"]?)\s*:\s*\{\s*$", line)
        if match:
            current = match.group(1).strip("'\"")
            blocks[current] = []
            continue
        if current is not None and re.match(r"^\S", line):
            current = None  # end of the LOCALES object
            continue
        if current is not None:
            blocks[current].append(line)
    return {name: "\n".join(lines) for name, lines in blocks.items()}


def test_gateway_local_unproven_copy_exists_once_per_locale_with_external_note():
    blocks = _locale_blocks()
    with_external = [name for name, body in blocks.items() if "gateway_external_managed:" in body]
    assert len(with_external) == 15

    values = {}
    for name in with_external:
        body = blocks[name]
        assert body.count("gateway_external_managed:") == 1, name
        assert body.count("gateway_local_unproven:") == 1, name
        match = re.search(r"^\s*gateway_local_unproven: '((?:[^'\\]|\\.)*)',$", body, re.M)
        assert match, name
        values[name] = match.group(1).replace("\\'", "'")
        # The renderer puts the server's exact start command (which may carry
        # --profile <name>) at {cmd}; no locale hard-codes the plain command.
        assert values[name].count("{cmd}") == 1, name
        assert "hermes gateway start" not in values[name], name
        assert "`" not in values[name] and "<" not in values[name], name
    assert I18N_JS.count("gateway_local_unproven:") == len(with_external)
    assert values["en"] == _EN_LOCAL_UNPROVEN
    assert all(values[name] != values["en"] for name in with_external if name != "en")


# ── follow-up D: the start hint names the target profile ─────────────────
#
# Ownership proof is per profile home, and WebUI's own Start button runs
# ``hermes --profile <name> gateway start`` for a named active profile. Telling
# a user on profile "work" to run plain ``hermes gateway start`` would start
# the default profile's Gateway and never prove this one, so the unproven
# result carries the exact command and the card renders it.


def _unproven_capability(monkeypatch, tmp_path, *, active_name="default", profile=None):
    from api import agent_health

    _pin_profile_homes(monkeypatch, tmp_path / "home", active_name=active_name)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    _pid_switch(monkeypatch, None)
    return agent_health.gateway_lifecycle_capability(profile=profile)


def test_unproven_start_command_is_plain_for_the_default_profile(monkeypatch, tmp_path):
    cap = _unproven_capability(monkeypatch, tmp_path, active_name="default")

    assert cap == _unproven("hermes gateway start")


def test_unproven_start_command_names_the_active_named_profile(monkeypatch, tmp_path):
    cap = _unproven_capability(monkeypatch, tmp_path, active_name="work")

    assert cap == _unproven("hermes --profile work gateway start")
    assert cap["managed"] is False


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ("work", "hermes --profile work gateway start"),
        ("default", "hermes gateway start"),
    ],
)
def test_unproven_start_command_follows_an_explicit_profile(monkeypatch, tmp_path, profile, expected):
    # The explicit profile wins over the active one, like the home it checks.
    cap = _unproven_capability(monkeypatch, tmp_path, active_name="other", profile=profile)

    assert cap == _unproven(expected)


@pytest.mark.parametrize("active_name", ["default", "work", "team-2", "", "  work  "])
def test_unproven_start_command_matches_the_settings_start_button(monkeypatch, tmp_path, active_name):
    """Same rule as routes._run_gateway_lifecycle_command for every valid name."""
    from api import config, routes

    agent_dir = _fake_agent(tmp_path)
    monkeypatch.setattr(config, "_AGENT_DIR", agent_dir)
    monkeypatch.setattr(config, "PYTHON_EXE", sys.executable)
    cap = _unproven_capability(monkeypatch, tmp_path, active_name=active_name)
    ran = []
    monkeypatch.setattr(
        routes.subprocess,
        "run",
        lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    routes._run_gateway_lifecycle_command("start")

    assert ran and ran[0][:2] == [sys.executable, str(agent_dir.resolve() / "hermes_cli" / "main.py")]
    assert ["hermes", *ran[0][2:]] == cap["start_command"].split(" ")


@pytest.mark.parametrize(
    "hostile",
    [
        "x; rm -rf /",
        "work prod",
        "../etc",
        "Work",
        "-x",
        "--help",
        "<img src=x onerror=1>",
        "work\ngateway stop",
        "a" * 65,
    ],
)
def test_unproven_start_command_never_echoes_an_invalid_profile_name(monkeypatch, tmp_path, hostile):
    cap = _unproven_capability(monkeypatch, tmp_path, active_name=hostile)

    assert cap == _unproven("hermes gateway start")


def test_unproven_start_command_falls_back_when_the_profile_lookup_fails(monkeypatch, tmp_path):
    from api import agent_health, profiles

    _pin_profile_homes(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    _pid_switch(monkeypatch, None)

    def boom():
        raise RuntimeError("profile state unavailable")

    monkeypatch.setattr(profiles, "get_active_profile_name", boom)

    # The hint degrades to the plain command; the reason is unaffected.
    assert agent_health.gateway_lifecycle_capability() == _unproven("hermes gateway start")


def test_managed_and_external_results_carry_no_start_command(monkeypatch, tmp_path):
    from api import agent_health

    _pin_profile_homes(monkeypatch, tmp_path / "home", active_name="work")
    pid = _pid_switch(monkeypatch, None)

    # Local-only setup.
    assert agent_health.gateway_lifecycle_capability() == _managed()
    # Loopback with a live Gateway, then with persisted proof only.
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    pid.pid = 4242
    assert agent_health.gateway_lifecycle_capability() == _managed()
    pid.pid = None
    assert agent_health.gateway_lifecycle_capability() == _managed()
    # Remote Gateway.
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.internal:8642")
    assert agent_health.gateway_lifecycle_capability() == _external()


def test_unproven_409_payload_carries_the_named_profile_start_command(monkeypatch, tmp_path):
    from api import routes

    _pin_profile_homes(monkeypatch, tmp_path / "home", active_name="work")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://127.0.0.1:8642")
    _pid_switch(monkeypatch, None)
    spawned = []
    monkeypatch.setattr(routes.subprocess, "run", lambda cmd, **kw: spawned.append(cmd))

    handler, data = _call_post(monkeypatch, "/api/gateway/start", {})

    assert handler.status == 409
    assert data["code"] == "gateway_externally_managed"
    assert data["lifecycle"] == _unproven("hermes --profile work gateway start")
    assert spawned == []


def _render_unproven_note(lifecycle_extra: dict | None = None, *, strings: dict | None = None) -> str:
    lifecycle = {"managed": False, "reason": "local_gateway_unproven", **(lifecycle_extra or {})}
    return _render_gateway_card(
        {**_STATUS_BRANCHES["not_running"], "lifecycle": lifecycle},
        strings=strings if strings is not None else {"gateway_local_unproven": _EN_LOCAL_UNPROVEN},
        esc_src=_ESC_SRC,
    )


def _code_text(html: str) -> str:
    """Text of the note's single <code> chip, with the nowrap flag spans removed."""
    m = re.search(r"<code[^>]*>(.*?)</code>", html)
    assert m, html
    return re.sub(r"<[^>]+>", "", m.group(1))


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize(
    "command",
    ["hermes --profile work gateway start", "hermes gateway start"],
)
def test_renderer_unproven_note_shows_the_server_start_command(command):
    html = _render_unproven_note({"start_command": command})

    assert html.count("<code") == 1
    assert _code_text(html) == command
    assert "start it once with <code" in html
    assert "{cmd}" not in html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("bad", [None, "", "   ", 123, ["hermes", "--profile", "work"], {"cmd": "x"}])
def test_renderer_unproven_note_falls_back_to_the_plain_command(bad):
    extra = {} if bad is None else {"start_command": bad}
    html = _render_unproven_note(extra)

    assert html.count("<code") == 1
    assert ">hermes gateway start</code>" in html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_renderer_unproven_note_escapes_a_hostile_start_command():
    html = _render_unproven_note({"start_command": '<img src=x onerror=1>"&'})

    assert "<img" not in html
    assert html.count("<code") == 1
    assert ">&lt;img src=x onerror=1&gt;&quot;&amp;</code>" in html


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_renderer_unproven_note_escapes_a_hostile_translation_around_the_command():
    hostile = '<script>alert(1)</script> {cmd} <b onmouseover="x">'
    html = _render_unproven_note(
        {"start_command": "hermes --profile work gateway start"},
        strings={"gateway_local_unproven": hostile},
    )

    assert "<script" not in html and "<b " not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;b onmouseover=&quot;x&quot;&gt;" in html
    assert html.count("<code") == 1
    assert _code_text(html) == "hermes --profile work gateway start"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_renderer_unproven_note_without_placeholder_adds_no_command():
    html = _render_unproven_note(
        {"start_command": "hermes --profile work gateway start"},
        strings={"gateway_local_unproven": "Start <it> once."},
    )

    assert "<code" not in html
    assert "hermes --profile work gateway start" not in html
    assert "Start &lt;it&gt; once." in html


def test_renderer_passes_the_lifecycle_to_the_unproven_note():
    body = extract_function(PANELS_JS, "_gatewayActionControls").replace(" ", "")
    assert "_gatewayLocalUnprovenNoteHtml(lifecycle)" in body
    note = extract_function(PANELS_JS, "_gatewayLocalUnprovenNoteHtml")
    assert "lifecycle.start_command" in note
    assert "esc(command)" in note
