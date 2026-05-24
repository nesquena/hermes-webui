"""Tests for profile-scoped gateway control endpoint.

Plan reference: Phase 1C. The endpoint must:
  * validate action in {start, restart, stop}
  * call a profile-scoped runner with the action and profile name
  * return a structured unavailable response when the backend is missing
  * never leak secret-looking strings from runner output
"""

import importlib
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _reload_profiles_module(base_home: Path):
    os.environ["HERMES_BASE_HOME"] = str(base_home)
    os.environ["HERMES_HOME"] = str(base_home)

    _saved = {name: sys.modules[name] for name in ["api.config", "api.profiles"]
              if name in sys.modules}
    for name in ["api.config", "api.profiles"]:
        if name in sys.modules:
            del sys.modules[name]

    profiles = importlib.import_module("api.profiles")

    # Restore original modules and package attributes so this temporary import
    # does not leave api.profiles pointing at a module that is no longer present
    # in sys.modules. Later tests call importlib.reload(api.profiles), which
    # requires those references to remain consistent.
    sys.modules.update(_saved)
    api_pkg = sys.modules.get("api")
    if api_pkg is not None:
        for name, module in _saved.items():
            setattr(api_pkg, name.rsplit(".", 1)[-1], module)
    return profiles


def _seed_named_profile(base: Path, name: str) -> Path:
    profile_dir = base / "profiles" / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def _serve_once(host: str, handler_cls):
    server = ThreadingHTTPServer((host, 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_invalid_action_rejected():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        with pytest.raises(ValueError):
            profiles.profile_gateway_control_api("coder", "burninate")


def test_unknown_profile_404():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        with pytest.raises(FileNotFoundError):
            profiles.profile_gateway_control_api("ghost", "start")


def test_hook_receives_action_and_profile():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        calls: list[tuple[str, str]] = []

        def fake_hook(name, action):
            calls.append((name, action))
            return {"ok": True, "running": action != "stop"}

        profiles._set_gateway_control_hook(fake_hook)
        try:
            result = profiles.profile_gateway_control_api("coder", "start")
            assert result["ok"] is True
            assert result["profile"] == "coder"
            assert result["action"] == "start"
            assert calls == [("coder", "start")]
        finally:
            profiles._set_gateway_control_hook(None)


def test_hook_failure_is_sanitized_not_raised():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)

        def boom(name, action):
            raise RuntimeError("gateway exploded with api_key=SHHH-DONT-TELL")

        profiles._set_gateway_control_hook(boom)
        try:
            result = profiles.profile_gateway_control_api("coder", "stop")
        finally:
            profiles._set_gateway_control_hook(None)
        assert result["ok"] is False
        # Sanitized: secret token should not appear verbatim.
        assert "SHHH-DONT-TELL" not in result["message"]
        assert "api_key=[redacted]" in result["message"]


def test_unavailable_when_no_backend_and_no_hook():
    """Without a hook and without hermes_cli.gateway, the endpoint must degrade
    honestly instead of pretending success.

    // Updated for T5: legacy unavailable:True key removed when default backend
    // dispatch replaced the import-fallback. The new code raises ImportError
    // through _default_gateway_control which is then caught and sanitized, so
    // the response is ok:False with a clean message — no unavailable key required.
    """
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        # Ensure no hook installed by other tests bleeds through.
        profiles._set_gateway_control_hook(None)
        # If hermes_cli.gateway happens to be importable in this env, skip the
        # honest-degradation assertion — the test is then tautological.
        try:
            import hermes_cli.gateway  # type: ignore  # noqa: F401
        except ImportError:
            result = profiles.profile_gateway_control_api("coder", "start")
            assert result["ok"] is False
            # Legacy assertion removed: result.get("unavailable") is True
            # New assertion: just a clean ok:False with a message (no key req'd)
            assert "message" in result


# ── State write on successful start (rework Task 6) ───────────────────────────


def test_gateway_start_writes_last_run_at_state():
    """On a successful start the profile's .gateway-state.json must contain
    a last_run_at ISO-8601 UTC timestamp. The activity line reads this to show
    "gateway last ran ..." even after the gateway is stopped."""
    import json as _json

    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile_dir = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)

        def fake_hook(name, action):
            return {"ok": True, "running": action != "stop"}

        profiles._set_gateway_control_hook(fake_hook)
        try:
            result = profiles.profile_gateway_control_api("coder", "start")
            assert result["ok"] is True
        finally:
            profiles._set_gateway_control_hook(None)

        state_path = profile_dir / ".gateway-state.json"
        assert state_path.exists()
        state = _json.loads(state_path.read_text(encoding="utf-8"))
        assert isinstance(state.get("last_run_at"), str)
        # ISO-8601 UTC: ends with Z (we normalize +00:00 -> Z for the wire).
        assert state["last_run_at"].endswith("Z")


def test_gateway_stop_does_not_overwrite_last_run_at():
    """Only start/restart should bump last_run_at; a stop must preserve it."""
    import json as _json

    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile_dir = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)

        # Pre-existing state from a prior successful start.
        state_path = profile_dir / ".gateway-state.json"
        state_path.write_text(
            _json.dumps({"last_run_at": "2026-05-01T12:00:00Z"}),
            encoding="utf-8",
        )

        def fake_hook(name, action):
            return {"ok": True, "running": False}

        profiles._set_gateway_control_hook(fake_hook)
        try:
            profiles.profile_gateway_control_api("coder", "stop")
        finally:
            profiles._set_gateway_control_hook(None)

        state = _json.loads(state_path.read_text(encoding="utf-8"))
        assert state["last_run_at"] == "2026-05-01T12:00:00Z"


def test_gateway_failed_start_does_not_write_last_run_at():
    """If the hook raises, the start "failed" — last_run_at must NOT be
    written. The state file itself now exists (phase='failed' is recorded
    by the pre-action phase write + failure handler in Task 3), but the
    successful-run timestamp must remain absent so the activity line
    doesn't mislead readers into thinking the gateway started."""
    import json as _json
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile_dir = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)

        def boom(name, action):
            raise RuntimeError("nope")

        profiles._set_gateway_control_hook(boom)
        try:
            result = profiles.profile_gateway_control_api("coder", "start")
        finally:
            profiles._set_gateway_control_hook(None)

        assert result["ok"] is False
        # State file exists (phase='failed') but last_run_at must be absent.
        state_path = profile_dir / ".gateway-state.json"
        assert state_path.exists()
        state = _json.loads(state_path.read_text(encoding="utf-8"))
        assert state.get("phase") == "failed"
        assert "last_run_at" not in state


# ── T5: default backend dispatch (replaces import-based fallback) ─────────────


def test_default_backend_dispatches_start(monkeypatch):
    """When no test-hook is installed, profile_gateway_control_api must
    dispatch through the in-process default backend instead of the now-
    deleted import-based fallback."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        profiles._set_gateway_control_hook(None)
        calls: list[tuple[str, str]] = []

        def fake_default(name, action):
            calls.append((name, action))
            return {"ok": True, "running": action != "stop"}

        monkeypatch.setattr(profiles, "_default_gateway_control", fake_default)
        result = profiles.profile_gateway_control_api("coder", "start")
        assert result["ok"] is True
        assert result["running"] is True
        assert calls == [("coder", "start")]


def test_default_backend_dispatches_stop(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        profiles._set_gateway_control_hook(None)
        calls: list[tuple[str, str]] = []

        def fake_default(name, action):
            calls.append((name, action))
            return {"ok": True, "running": False}

        monkeypatch.setattr(profiles, "_default_gateway_control", fake_default)
        result = profiles.profile_gateway_control_api("coder", "stop")
        assert result["ok"] is True
        assert result["running"] is False
        assert calls == [("coder", "stop")]


def test_default_backend_failure_is_sanitized(monkeypatch):
    """Errors from the default backend must be sanitized (no api_key=*** leakage)."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        profiles._set_gateway_control_hook(None)

        def bad_default(name, action):
            raise RuntimeError("gateway exploded with token=SHHH-DONT-TELL")

        monkeypatch.setattr(profiles, "_default_gateway_control", bad_default)
        result = profiles.profile_gateway_control_api("coder", "start")
        assert result["ok"] is False
        assert "SHHH-DONT-TELL" not in result["message"]


def test_default_backend_missing_agent_returns_clean_failure(monkeypatch):
    """When hermes_cli.gateway is not importable, the default backend
    raises ImportError -> caller sanitizes and returns ok:False with
    a non-leaky message."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        profiles._set_gateway_control_hook(None)

        def import_failing_default(name, action):
            raise ImportError("No module named 'hermes_cli'")

        monkeypatch.setattr(profiles, "_default_gateway_control", import_failing_default)
        result = profiles.profile_gateway_control_api("coder", "start")
        assert result["ok"] is False
        # No specific 'unavailable' key required anymore — just a clean failure.
        assert "ImportError" not in result.get("message", "")  # exc class names sanitized out is optional; key check is ok:False


# ── T6: subprocess-spawn fix (containers sys.exit() on gateway_command) ──────


def test_default_backend_start_spawns_subprocess(monkeypatch):
    """Start must invoke `hermes gateway run` as a detached subprocess,
    NOT call gateway_command (which sys.exit()s inside containers)."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        profiles._set_gateway_control_hook(None)

        spawned: list[tuple] = []

        class FakePopen:
            def __init__(self, args, **kwargs):
                spawned.append((tuple(args), kwargs))

        # Stub the optional hermes_cli.gateway with a minimal namespace.
        import types as _types
        fake_gw = _types.SimpleNamespace(
            stop_profile_gateway=lambda: False,
            # We must NOT reach gateway_command in the new code path; if a
            # test of this stub gets called the assertion below will fail.
            gateway_command=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("gateway_command should not be called")
            ),
        )
        monkeypatch.setitem(
            __import__('sys').modules, 'hermes_cli', _types.SimpleNamespace(gateway=fake_gw)
        )
        monkeypatch.setitem(
            __import__('sys').modules, 'hermes_cli.gateway', fake_gw
        )
        monkeypatch.setattr('subprocess.Popen', FakePopen)

        result = profiles._default_gateway_control("coder", "start")
        assert result == {'ok': True, 'running': True}
        assert len(spawned) == 1
        args, kwargs = spawned[0]
        # argv[0] must be a resolved binary (absolute path or shutil.which result
        # ending with 'hermes'/'hermes.exe'), NOT the bare string 'hermes'.
        # The exact value depends on the runtime env; assert the tail is right
        # and that the subcommands are unchanged. --replace clears stale PIDs
        # so subsequent toggle clicks don't trip "Gateway already running".
        assert args[-3:] == ("gateway", "run", "--replace")
        assert 'hermes' in args[0].lower()
        # Detached on at least one of the two flavors.
        if __import__('sys').platform == "win32":
            assert "creationflags" in kwargs
        else:
            assert kwargs.get("start_new_session") is True


def test_default_backend_rejects_restart_action(monkeypatch):
    """Task 3 dropped 'restart' from the default backend. The dispatch must
    raise ValueError without invoking stop or spawn — clients should issue
    stop-then-start instead (toggle off+on at the UI layer)."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        profiles._set_gateway_control_hook(None)

        events: list[str] = []
        class FakePopen:
            def __init__(self, args, **kwargs):
                events.append("spawn")

        import types as _types
        fake_gw = _types.SimpleNamespace(
            stop_profile_gateway=lambda: (events.append("stop") or True),
            gateway_command=lambda *a, **k: (_ for _ in ()).throw(AssertionError()),
        )
        monkeypatch.setitem(
            __import__('sys').modules, 'hermes_cli', _types.SimpleNamespace(gateway=fake_gw)
        )
        monkeypatch.setitem(
            __import__('sys').modules, 'hermes_cli.gateway', fake_gw
        )
        monkeypatch.setattr('subprocess.Popen', FakePopen)

        with pytest.raises(ValueError):
            profiles._default_gateway_control("coder", "restart")
        # Neither stop nor spawn should have been invoked.
        assert events == []


def test_default_backend_shields_systemexit(monkeypatch):
    """A sys.exit() inside the dispatch path must NOT propagate — it would
    kill the WebUI process. Verify the SystemExit shield converts it to a
    normal RuntimeError that the caller can sanitize."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        profiles._set_gateway_control_hook(None)

        def raise_systemexit(*a, **k):
            raise SystemExit(0)

        import types as _types
        fake_gw = _types.SimpleNamespace(
            stop_profile_gateway=raise_systemexit,
            gateway_command=raise_systemexit,
        )
        monkeypatch.setitem(
            __import__('sys').modules, 'hermes_cli', _types.SimpleNamespace(gateway=fake_gw)
        )
        monkeypatch.setitem(
            __import__('sys').modules, 'hermes_cli.gateway', fake_gw
        )

        with pytest.raises(RuntimeError, match="gateway subsystem aborted"):
            profiles._default_gateway_control("coder", "stop")


def test_default_backend_start_uses_resolvable_hermes_binary(monkeypatch):
    """The subprocess argv[0] must come from _resolve_hermes_bin(), not the
    bare string 'hermes'. The container PATH does not include /app/venv/bin
    where the entry script lives, so a bare 'hermes' argv[0] would fail
    silently (FileNotFoundError swallowed by stderr=DEVNULL).

    This test monkeypatches _resolve_hermes_bin to return a deterministic
    absolute path, then verifies that _spawn_gateway uses that path as
    argv[0] — confirming the integration between the resolver and the spawner."""
    import os as _os
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        profiles._set_gateway_control_hook(None)

        captured_args: list = []

        class FakePopen:
            def __init__(self, args, **kwargs):
                captured_args.append(list(args))

        import types as _types
        fake_gw = _types.SimpleNamespace(
            stop_profile_gateway=lambda: False,
            gateway_command=lambda *a, **k: (_ for _ in ()).throw(AssertionError()),
        )
        monkeypatch.setitem(sys.modules, 'hermes_cli', _types.SimpleNamespace(gateway=fake_gw))
        monkeypatch.setitem(sys.modules, 'hermes_cli.gateway', fake_gw)
        monkeypatch.setattr('subprocess.Popen', FakePopen)

        # Inject a fake absolute path so the test is env-independent.
        fake_abs_hermes = _os.path.join(_os.path.dirname(sys.executable), 'hermes-fake')
        monkeypatch.setattr(profiles, '_resolve_hermes_bin', lambda: fake_abs_hermes)

        profiles._default_gateway_control("coder", "start")
        assert len(captured_args) == 1
        args = captured_args[0]
        # Must use the resolved path returned by _resolve_hermes_bin.
        assert args[0] == fake_abs_hermes, (
            f"argv[0] should be the resolved binary path, got: {args[0]!r}"
        )
        # Must be absolute (confirming _resolve_hermes_bin returned an abs path).
        assert _os.path.isabs(args[0]), (
            f"argv[0] should be an absolute path, got: {args[0]!r}"
        )
        # The trailing args remain unchanged.
        assert args[-3:] == ['gateway', 'run', '--replace']


def test_default_backend_start_passes_replace_flag(monkeypatch):
    """The gateway spawn must include `--replace` so a stale gateway.pid
    file from a previous run (gateway exited but didn't clean up its PID,
    or PID got recycled) doesn't block subsequent starts with
    `❌ Gateway already running (PID X)`. Without this flag, every start
    after the first exits with that error and the user is stuck at
    'Starting Failed' on every toggle click."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        profiles._set_gateway_control_hook(None)

        spawned: list[list] = []

        class FakePopen:
            def __init__(self, args, **kwargs):
                spawned.append(list(args))

        import types as _types
        fake_gw = _types.SimpleNamespace(
            stop_profile_gateway=lambda: False,
            gateway_command=lambda *a, **k: (_ for _ in ()).throw(AssertionError()),
        )
        monkeypatch.setitem(sys.modules, 'hermes_cli', _types.SimpleNamespace(gateway=fake_gw))
        monkeypatch.setitem(sys.modules, 'hermes_cli.gateway', fake_gw)
        monkeypatch.setattr('subprocess.Popen', FakePopen)

        profiles._default_gateway_control("coder", "start")
        assert len(spawned) == 1
        args = spawned[0]
        assert '--replace' in args, (
            f"spawn argv must include --replace to clear stale gateway.pid, got: {args!r}"
        )
        # Order matters: --replace must follow `gateway run` (it's a subcommand flag).
        assert args[-3:] == ['gateway', 'run', '--replace']


def test_control_rejects_restart_action():
    """The 'restart' action is no longer accepted — clients should
    issue stop then start (the toggle UX does this implicitly)."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        with pytest.raises(ValueError):
            profiles.profile_gateway_control_api("coder", "restart")


def test_start_action_writes_starting_phase_before_returning():
    import json
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)

        def fake_hook(name, action):
            # By the time the hook is called, phase must already be 'starting'.
            data = json.loads((profile / ".gateway-state.json").read_text())
            assert data["phase"] == "starting"
            assert isinstance(data["phase_started_at"], str)
            return {"ok": True, "running": True}

        profiles._set_gateway_control_hook(fake_hook)
        try:
            result = profiles.profile_gateway_control_api("coder", "start")
            assert result["ok"] is True
            assert result.get("phase") == "starting"
        finally:
            profiles._set_gateway_control_hook(None)


def test_stop_action_writes_stopping_phase_before_returning():
    """The in-flight 'stopping' stamp is visible to a status poll that races
    the kill; on success the handler reconciles to 'stopped' before returning
    so the response is authoritative and the UI cannot flicker back to
    'stopping' afterwards.
    """
    import json
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)

        def fake_hook(name, action):
            data = json.loads((profile / ".gateway-state.json").read_text())
            assert data["phase"] == "stopping"
            return {"ok": True, "running": False}

        profiles._set_gateway_control_hook(fake_hook)
        try:
            result = profiles.profile_gateway_control_api("coder", "stop")
            assert result["ok"] is True
            # On successful stop, the response now reports the settled phase
            # so a racing status poll cannot read back a stale 'stopping'
            # stamp and flip the UI from Off → Stopping.
            assert result.get("phase") == "stopped"
            data = json.loads((profile / ".gateway-state.json").read_text())
            assert data["phase"] is None
            assert data["phase_started_at"] is None
        finally:
            profiles._set_gateway_control_hook(None)


def test_start_failure_writes_failed_phase_with_error():
    import json
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)

        def fake_hook(name, action):
            raise RuntimeError("simulated spawn failure: token=secretvalue")

        profiles._set_gateway_control_hook(fake_hook)
        try:
            result = profiles.profile_gateway_control_api("coder", "start")
            assert result["ok"] is False
            assert result.get("phase") == "failed"
            assert "secretvalue" not in (result.get("message") or "")
            data = json.loads((profile / ".gateway-state.json").read_text())
            assert data["phase"] == "failed"
        finally:
            profiles._set_gateway_control_hook(None)


def test_stop_failure_writes_failed_phase_with_error():
    """A stop action that raises must also write phase='failed' so the
    next status poll surfaces the failure (symmetric with start failure)."""
    import json
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)

        def fake_hook(name, action):
            raise RuntimeError("simulated kill failure")

        profiles._set_gateway_control_hook(fake_hook)
        try:
            result = profiles.profile_gateway_control_api("coder", "stop")
            assert result["ok"] is False
            assert result.get("phase") == "failed"
            data = json.loads((profile / ".gateway-state.json").read_text())
            assert data["phase"] == "failed"
            assert data.get("last_error")
        finally:
            profiles._set_gateway_control_hook(None)


def test_duplicate_telegram_token_failure_surfaces_copyable_profile_conflict_detail():
    """Shared Telegram bot tokens are profile conflicts, not generic failures."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)

        def fake_hook(name, action):
            raise RuntimeError(
                "telegram-bot-token_lock is already held; duplicate Telegram polling "
                "for bot token 123456789:SHOULD_NOT_LEAK_THIS_FAKE_TELEGRAM_TOKEN"
            )

        profiles._set_gateway_control_hook(fake_hook)
        try:
            result = profiles.profile_gateway_control_api("coder", "start")
            assert result["ok"] is False
            assert result["phase"] == "failed"
            detail = result["message"]
            assert "Profile 'coder'" in detail
            assert "Telegram bot token" in detail
            assert "another Gateway/profile" in detail
            assert "Stop the other profile" in detail
            assert "TELEGRAM_BOT_TOKEN" in detail
            assert "SHOULD_NOT_LEAK" not in detail
            assert "123456789:" not in detail

            status = profiles.profile_gateway_status_api("coder")
            assert status["phase"] == "failed"
            assert status["detail"] == detail
            assert status["last_error"] == detail
            assert "another Gateway/profile" in status["detail"]

            persisted = json.loads((profile / ".gateway-state.json").read_text())
            assert persisted["phase"] == "failed"
            assert persisted["last_error"] == detail
        finally:
            profiles._set_gateway_control_hook(None)


# ── Docker/WSL Gateway adapter contract ─────────────────────────────────────


def test_gateway_control_adapter_defaults_to_local(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        monkeypatch.delenv("WEBUI_GATEWAY_CONTROL_MODE", raising=False)

        adapter = profiles._select_gateway_control_adapter("coder", base / "profiles" / "coder")

        assert adapter.name == "local"


def test_gateway_control_adapter_uses_docker_only_when_explicit(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        monkeypatch.setenv("WEBUI_GATEWAY_CONTROL_MODE", "docker_exec")
        monkeypatch.setenv("WEBUI_GATEWAY_DOCKER_CONTAINER", "hermes-agent")

        adapter = profiles._select_gateway_control_adapter("coder", profile)

        assert adapter.name == "docker_exec"


def test_docker_adapter_builds_profile_scoped_commands_without_executing(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        adapter = profiles._DockerGatewayControlAdapter("hermes-agent")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            class Result:
                returncode = 0
                stderr = ""
            return Result()

        monkeypatch.setattr("subprocess.run", fake_run)

        start = adapter.start(profile, "coder")
        stop = adapter.stop(profile, "coder")

        assert start["ok"] is True
        assert stop["ok"] is True
        assert len(calls) == 2
        assert calls[0][0] == [
            "docker", "exec", "-e", f"HERMES_HOME={profile}", "hermes-agent",
            "hermes", "gateway", "run", "--replace",
        ]
        assert calls[1][0] == [
            "docker", "exec", "-e", f"HERMES_HOME={profile}", "hermes-agent",
            "hermes", "gateway", "stop",
        ]
        assert all(call[1]["timeout"] <= 10 for call in calls)


def test_docker_cli_unavailable_status_is_unavailable_and_sanitized(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        monkeypatch.setenv("WEBUI_GATEWAY_CONTROL_MODE", "docker_exec")
        monkeypatch.setenv("WEBUI_GATEWAY_DOCKER_CONTAINER", "hermes-agent")
        monkeypatch.setattr(profiles, "_read_gateway_pid", lambda home: None)
        monkeypatch.setattr("shutil.which", lambda name: None if name == "docker" else "/usr/bin/other")

        result = profiles.profile_gateway_status_api("coder")

        assert result["phase"] == "unavailable"
        assert result["control_available"] is False
        assert "docker" in result["detail"].lower()
        assert str(profile) not in result["detail"]
        assert "token=" not in result["detail"].lower()


def test_remote_health_probe_rejects_unsafe_urls_and_times_out_quickly(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append((req.full_url, timeout))
            raise AssertionError("unsafe URL should not be requested")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        unsafe = profiles._probe_gateway_remote_health("http://169.254.169.254/latest/meta-data")

        assert unsafe["phase"] == "unavailable"
        assert unsafe["control_available"] is False
        assert calls == []

        class TimeoutOpener:
            def open(self, req, timeout=None):
                calls.append((req.full_url, timeout))
                raise TimeoutError("too slow token=SECRET")

        monkeypatch.setattr("urllib.request.build_opener", lambda *handlers: TimeoutOpener())
        timed_out = profiles._probe_gateway_remote_health("http://127.0.0.1:8642")

        assert timed_out["phase"] == "unavailable"
        assert timed_out["control_available"] is False
        assert calls and calls[-1][1] <= 1.0
        assert "SECRET" not in timed_out["detail"]


def test_remote_health_probe_does_not_follow_redirect_to_disallowed_host():
    target_hits = 0

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal target_hits
            target_hits += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, format, *args):  # pragma: no cover - keep test output quiet
            return

    target = None
    try:
        target = _serve_once("127.0.0.2", TargetHandler)
    except OSError as exc:
        pytest.skip(f"loopback alias 127.0.0.2 unavailable: {exc}")
    assert target is not None
    target_port = target.server_port

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.2:{target_port}/health")
            self.end_headers()

        def log_message(self, format, *args):  # pragma: no cover - keep test output quiet
            return

    redirector = _serve_once("127.0.0.1", RedirectHandler)
    try:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / ".hermes"
            (base / "profiles").mkdir(parents=True)
            _seed_named_profile(base, "coder")
            profiles = _reload_profiles_module(base)

            result = profiles._probe_gateway_remote_health(f"http://127.0.0.1:{redirector.server_port}")
    finally:
        redirector.shutdown()
        redirector.server_close()
        target.shutdown()
        target.server_close()

    assert result["phase"] == "unavailable"
    assert result["control_available"] is False
    assert target_hits == 0


def test_gateway_message_sanitizer_redacts_headers_and_cli_credentials():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)

        message = (
            "Authorization: Bearer DUMMYBEARER123\n"
            "curl -H 'authorization: token DUMMYTOKEN456'\n"
            "hermes gateway run --password DUMMYPASS123 --api-key DUMMYKEY789 "
            "--token=DUMMYTOKEN789 --secret DUMMYSECRET123"
        )

        sanitized = profiles._sanitize_gateway_message(message)

    for leaked in (
        "DUMMYBEARER123",
        "DUMMYTOKEN456",
        "DUMMYPASS123",
        "DUMMYKEY789",
        "DUMMYTOKEN789",
        "DUMMYSECRET123",
    ):
        assert leaked not in sanitized
    assert "Authorization: [redacted]" in sanitized
    assert "--password [redacted]" in sanitized
    assert "--api-key [redacted]" in sanitized


def test_gateway_status_failed_phase_redacts_cli_style_last_error():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        (profile / ".gateway-state.json").write_text(
            json.dumps({
                "phase": "failed",
                "last_error": "gateway failed --password DUMMYPASS123 Authorization: Bearer DUMMYBEARER123",
            }),
            encoding="utf-8",
        )

        result = profiles.profile_gateway_status_api("coder")

    assert result["phase"] == "failed"
    assert result["last_error"] == result["detail"]
    assert "DUMMYPASS123" not in result["detail"]
    assert "DUMMYBEARER123" not in result["detail"]
