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
  every status branch of an external Gateway.
"""

from __future__ import annotations

import json
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
    """A loopback URL alone is not proof (Docker port maps, SSH tunnels)."""
    from api import agent_health

    _no_local_pid(monkeypatch)
    monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:8642")

    cap = agent_health.gateway_lifecycle_capability()

    assert cap["managed"] is False
    assert cap["reason"] == "external_gateway"


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


def _pin_profile_homes(monkeypatch, active_home, homes_by_name=None):
    from api import profiles

    homes_by_name = dict(homes_by_name or {})
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: active_home)
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: homes_by_name.get(name, active_home),
    )


def _managed():
    return {"managed": True, "reason": "local_gateway"}


def _external():
    return {"managed": False, "reason": "external_gateway"}


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
    # invalidates it -- going back requires a fresh proof.
    monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:9642")
    assert agent_health.gateway_lifecycle_capability() == _external()
    monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:8642")
    assert agent_health.gateway_lifecycle_capability() == _external()


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
    # Same endpoint, different profile home: never proven -> external.
    assert agent_health.gateway_lifecycle_capability(profile="beta") == _external()
    _pin_profile_homes(monkeypatch, home_b, {"alpha": home_a, "beta": home_b})
    assert agent_health.gateway_lifecycle_capability() == _external()


@pytest.mark.parametrize("payload", ["{not json", "[]", '{"owners": []}', '{"owners": {"x": 1}}'])
def test_unreadable_ownership_record_fails_closed(monkeypatch, tmp_path, payload):
    from api import agent_health

    _pin_profile_homes(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("HERMES_API_URL", "http://127.0.0.1:8642")
    _pid_switch(monkeypatch, None)
    path = agent_health._gateway_ownership_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")

    assert agent_health.gateway_lifecycle_capability() == _external()


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


def _render_gateway_card(status: dict) -> str:
    fns = "\n".join(
        extract_function(PANELS_JS, name)
        for name in (
            "_gatewayStatusReason",
            "_gatewayActionButton",
            "_gatewayActionControls",
            "_renderGatewayStatus",
        )
    )
    harness = f"""
const card = {{ innerHTML: '' }};
const $ = (id) => id === 'gatewayStatusCard' ? card : null;
const t = (k) => '[' + k + ']';
const esc = (s) => String(s);
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
