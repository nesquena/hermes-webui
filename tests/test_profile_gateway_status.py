"""Tests for profile_gateway_status_api — phase promotion logic.

The status endpoint is the only point that promotes 'starting' to
'running' or 'failed' based on PID liveness + grace window.
"""

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _reload_profiles_module(base_home: Path):
    os.environ["HERMES_BASE_HOME"] = str(base_home)
    os.environ["HERMES_HOME"] = str(base_home)
    _saved = {n: sys.modules[n] for n in ("api.config", "api.profiles") if n in sys.modules}
    for n in ("api.config", "api.profiles"):
        if n in sys.modules:
            del sys.modules[n]
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


def _write_state(profile_home: Path, **fields):
    (profile_home / ".gateway-state.json").write_text(json.dumps(fields), encoding="utf-8")


def _install_fake_pid_alive(profiles, *, alive_pids: set[int]):
    """Monkey-patch _is_pid_alive on the module so tests can simulate liveness."""
    profiles._is_pid_alive = lambda pid: pid in alive_pids


def _past_iso(seconds_ago: float) -> str:
    import datetime as _dt
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds_ago)
    return t.isoformat().replace("+00:00", "Z")


def test_status_invalid_name_raises_value_error():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        with pytest.raises(ValueError):
            profiles.profile_gateway_status_api("BAD NAME!")


def test_status_unknown_profile_raises_filenotfound():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        with pytest.raises(FileNotFoundError):
            profiles.profile_gateway_status_api("ghost")


def test_status_stopped_when_no_pid_and_no_phase():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "stopped"
        assert result["pid"] is None
        assert result["last_error"] is None


def test_status_starting_within_grace_window():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        _write_state(profile, phase="starting", phase_started_at=_past_iso(2))
        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "starting"


def test_status_starting_with_stale_runtime_stays_starting_in_grace_window():
    """After a WebUI restart, a fresh start (state.phase='starting') is
    expected to find a stale gateway_state.json — the brand-new gateway
    hasn't ticked yet. The status endpoint must stay at 'starting' during
    the grace window rather than surface a 'Gateway runtime file is stale;
    liveness is unknown' message that scares the user during a normal
    start. Only the post-grace failure path may escalate.
    """
    import datetime as _dt

    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        # Stale runtime file from the prior (dead) gateway process.
        stale = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=600)
        ).isoformat()
        (profile / "gateway_state.json").write_text(
            json.dumps({"gateway_state": "running", "updated_at": stale}),
            encoding="utf-8",
        )
        # Just clicked Start — well within the grace window.
        _write_state(profile, phase="starting", phase_started_at=_past_iso(2))

        result = profiles.profile_gateway_status_api("coder")

        assert result["phase"] == "starting", (
            "Stale runtime during fresh start must not promote to 'unknown'; "
            f"got phase={result['phase']!r}"
        )
        detail = (result.get("detail") or "")
        assert "runtime file is stale" not in detail.lower(), (
            "The 'runtime file is stale' message belongs to a steady-state "
            "diagnosis, not the brand-new-start window."
        )


def test_status_promotes_starting_to_running_when_pid_alive():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids={9999})
        (profile / "gateway.pid").write_text("9999", encoding="utf-8")
        _write_state(profile, phase="starting", phase_started_at=_past_iso(1))
        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "running"
        assert result["pid"] == 9999
        # Promotion is persisted.
        persisted = json.loads((profile / ".gateway-state.json").read_text())
        assert persisted["phase"] == "running"


def test_status_promotes_starting_to_failed_after_grace_with_dead_pid():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        (profile / "gateway.pid").write_text("9999", encoding="utf-8")
        (profile / ".gateway-stderr.log").write_text(
            "telegram: connect refused\ntoken invalid\n", encoding="utf-8"
        )
        _write_state(profile, phase="starting", phase_started_at=_past_iso(10))
        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "failed"
        assert result["last_error"]
        assert "connect refused" in result["last_error"] or "token invalid" in result["last_error"]


def test_status_promotes_starting_to_failed_when_no_pid_file_after_grace():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        _write_state(profile, phase="starting", phase_started_at=_past_iso(10))
        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "failed"


def test_status_running_when_phase_running_and_pid_alive():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids={1234})
        (profile / "gateway.pid").write_text("1234", encoding="utf-8")
        _write_state(profile, phase="running", phase_started_at=_past_iso(60))
        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "running"
        assert result["pid"] == 1234


def test_status_running_drops_to_stopped_when_pid_dies():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        (profile / "gateway.pid").write_text("1234", encoding="utf-8")
        _write_state(profile, phase="running", phase_started_at=_past_iso(60))
        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "stopped"
        persisted = json.loads((profile / ".gateway-state.json").read_text())
        assert persisted.get("phase") is None


def test_status_running_reconciles_to_stopped_when_runtime_is_stale_unknown():
    """After webui stop+start, the gateway subprocess is gone but state file
    still says phase='running' and the gateway_state.json heartbeat is stale.

    Previously the status endpoint returned phase='unknown' and *did not*
    rewrite the state file, leaving the profile stuck on "Check Status"
    forever. The recorded WebUI belief (phase=running) is only valid as long
    as there is a positive liveness signal (live PID or fresh runtime file);
    without either, reconcile to 'stopped' and persist.
    """
    import datetime as _dt

    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        (profile / "gateway.pid").write_text("1234", encoding="utf-8")
        stale = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=600)
        ).isoformat()
        (profile / "gateway_state.json").write_text(
            json.dumps({"gateway_state": "running", "updated_at": stale}),
            encoding="utf-8",
        )
        _write_state(profile, phase="running", phase_started_at=_past_iso(120))

        result = profiles.profile_gateway_status_api("coder")

        assert result["phase"] == "stopped"
        assert result["pid"] is None
        assert result["desired_enabled"] is False
        # State file must be rewritten so the next status query is not stuck.
        persisted = json.loads((profile / ".gateway-state.json").read_text())
        assert persisted.get("phase") is None or persisted.get("phase") == "stopped"
        # A second call must remain stopped (i.e. the fix is sticky in the
        # correct direction, not flapping back to 'unknown').
        result2 = profiles.profile_gateway_status_api("coder")
        assert result2["phase"] == "stopped"


def test_status_starting_reconciles_to_failed_when_runtime_is_stale_unknown():
    """Same shape, starting phase: a stale runtime file with state.phase=starting
    must not pin the profile in 'unknown' indefinitely. After the start grace
    window elapses without a positive liveness signal, mark failed."""
    import datetime as _dt

    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        (profile / "gateway.pid").write_text("1234", encoding="utf-8")
        stale = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=600)
        ).isoformat()
        (profile / "gateway_state.json").write_text(
            json.dumps({"gateway_state": "running", "updated_at": stale}),
            encoding="utf-8",
        )
        # phase_started_at older than the start grace window
        _write_state(
            profile,
            phase="starting",
            phase_started_at=_past_iso(profiles.GATEWAY_START_GRACE_SECONDS + 60),
        )

        result = profiles.profile_gateway_status_api("coder")

        assert result["phase"] in ("failed", "stopped"), (
            "stale runtime + expired starting grace + no live PID must reconcile, "
            f"got phase={result['phase']!r}"
        )
        # And must persist — second call must not return 'unknown'.
        result2 = profiles.profile_gateway_status_api("coder")
        assert result2["phase"] != "unknown"


def test_status_stopping_while_pid_alive():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids={1234})
        (profile / "gateway.pid").write_text("1234", encoding="utf-8")
        _write_state(profile, phase="stopping", phase_started_at=_past_iso(2))
        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "stopping"


def test_status_stale_stopping_reports_running_when_pid_still_alive():
    """A stop transition must not disable the UI forever.

    If the stop action was stamped long ago but an alive gateway PID is still
    visible, the truthful status is running so the toggle can be used again.
    """
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids={1234})
        (profile / "gateway.pid").write_text("1234", encoding="utf-8")
        _write_state(profile, phase="stopping", phase_started_at=_past_iso(600))

        result = profiles.profile_gateway_status_api("coder")

        assert result["phase"] == "running"
        assert result["pid"] == 1234
        assert result["status_source"] == "pid"
        assert result["control_available"] is True
        persisted = json.loads((profile / ".gateway-state.json").read_text())
        assert persisted["phase"] == "running"


def test_status_stale_stopping_reports_running_from_fresh_runtime_file():
    """Same stuck-stop recovery when WebUI cannot see the gateway PID.

    Split container/WSL setups can have no visible PID but a fresh
    gateway_state.json heartbeat; that is also an alive signal and should
    unlock the toggle after the stop grace window.
    """
    import datetime as _dt

    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        fresh = _dt.datetime.now(_dt.timezone.utc).isoformat()
        (profile / "gateway_state.json").write_text(
            json.dumps({"gateway_state": "running", "updated_at": fresh}),
            encoding="utf-8",
        )
        _write_state(profile, phase="stopping", phase_started_at=_past_iso(600))

        result = profiles.profile_gateway_status_api("coder")

        assert result["phase"] == "running"
        assert result["pid"] is None
        assert result["status_source"] == "runtime_file"
        assert result["control_available"] is True
        persisted = json.loads((profile / ".gateway-state.json").read_text())
        assert persisted["phase"] == "running"


def test_status_stopping_promotes_to_stopped_when_pid_gone():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        _write_state(profile, phase="stopping", phase_started_at=_past_iso(1))
        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "stopped"


def test_status_stale_stopping_reports_stopped_when_pid_and_runtime_are_dead():
    """A stale stop intent with no live signal must reconcile to stopped.

    This is the mirror of stale-stop-with-live-PID recovery: once the stop
    grace window has elapsed, a dead/missing PID plus absent runtime heartbeat
    is a terminal stopped state, not an indefinite disabled toggle.
    """
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        (profile / "gateway.pid").write_text("1234", encoding="utf-8")
        _write_state(profile, phase="stopping", phase_started_at=_past_iso(600))

        result = profiles.profile_gateway_status_api("coder")

        assert result["phase"] == "stopped"
        assert result["pid"] is None
        assert result["health"]["alive"] is False
        persisted = json.loads((profile / ".gateway-state.json").read_text())
        assert persisted.get("phase") is None
        assert persisted.get("desired_enabled") is False


def test_status_stale_stopping_reports_stopped_when_no_live_signal_remains():
    """A stale stop intent with no PID/runtime heartbeat must unlock as stopped."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        (profile / "gateway.pid").write_text("1234", encoding="utf-8")
        (profile / "gateway_state.json").write_text(
            json.dumps({"gateway_state": "stopped", "updated_at": _past_iso(600)}),
            encoding="utf-8",
        )
        _write_state(profile, phase="stopping", phase_started_at=_past_iso(600))

        result = profiles.profile_gateway_status_api("coder")

        assert result["phase"] == "stopped"
        assert result["pid"] is None
        assert result["status_source"] == "runtime_file"
        assert result["control_available"] is True
        persisted = json.loads((profile / ".gateway-state.json").read_text())
        assert persisted["phase"] is None
        assert persisted["desired_enabled"] is False


def test_status_failed_is_sticky():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        _write_state(
            profile,
            phase="failed",
            phase_started_at=_past_iso(30),
            last_error="bad token",
        )
        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "failed"
        assert result["last_error"] == "bad token"


def test_status_redacts_secrets_in_last_error():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        (profile / ".gateway-stderr.log").write_text(
            "TELEGRAM_BOT_TOKEN=abc123secret\nfailed\n", encoding="utf-8"
        )
        _write_state(profile, phase="starting", phase_started_at=_past_iso(10))
        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "failed"
        assert "abc123secret" not in (result["last_error"] or "")
        assert "[redacted]" in (result["last_error"] or "").lower() or "redacted" in (result["last_error"] or "")


def test_status_last_error_captures_tail_not_head_of_stderr():
    """When stderr log has stale noise at the front (e.g., a previous
    failed run's box-drawing) and the actual failure cause at the end
    (e.g., 'No messaging platforms enabled'), last_error must reflect
    the tail so the UI tooltip shows the meaningful diagnostic — not
    the box-drawing prefix that happened to land at offset 0 of the
    last-5KB read."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        head_noise = "X" * 1200  # simulate stale prior-run output
        tail_signal = "WARNING gateway.run: No messaging platforms enabled.\n"
        (profile / ".gateway-stderr.log").write_text(
            head_noise + "\n" + tail_signal, encoding="utf-8"
        )
        _write_state(profile, phase="starting", phase_started_at=_past_iso(10))
        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "failed"
        assert "No messaging platforms enabled" in (result["last_error"] or "")
        # The head noise should be truncated away by the tail slice.
        assert "X" * 1000 not in (result["last_error"] or "")


def test_status_synthesizes_running_when_no_phase_but_pid_alive():
    """Orphaned-process recovery: PID file exists, process alive, but state
    file is empty. The status API should synthesize a 'running' state."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids={4242})
        (profile / "gateway.pid").write_text("4242", encoding="utf-8")
        # No .gateway-state.json — clean recovery scenario.
        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "running"
        assert result["pid"] == 4242
        # Synthesized phase is persisted.
        persisted = json.loads((profile / ".gateway-state.json").read_text())
        assert persisted["phase"] == "running"
        assert isinstance(persisted["phase_started_at"], str)


def test_status_promotion_preserves_phase_started_at_across_polls():
    """After 'starting' -> 'running' promotion, the original start
    timestamp must survive a second poll (regression: do not stamp a
    fresh timestamp on every read)."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids={5555})
        (profile / "gateway.pid").write_text("5555", encoding="utf-8")
        original_started = _past_iso(3)
        _write_state(profile, phase="starting", phase_started_at=original_started)

        first = profiles.profile_gateway_status_api("coder")
        assert first["phase"] == "running"
        assert first["phase_started_at"] == original_started

        # Second poll must read the same (preserved) timestamp.
        second = profiles.profile_gateway_status_api("coder")
        assert second["phase"] == "running"
        assert second["phase_started_at"] == original_started


def test_status_reports_running_from_json_gateway_pid(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        (profile / "gateway.pid").write_text(json.dumps({"pid": 2468, "argv": ["hermes", "gateway"]}), encoding="utf-8")
        profiles = _reload_profiles_module(base)

        calls = []

        class FakeGatewayStatus:
            @staticmethod
            def get_running_pid(pid_path, cleanup_stale=False):
                calls.append((Path(pid_path), cleanup_stale))
                return 2468

        monkeypatch.setattr(profiles, "_gateway_status_module", lambda: FakeGatewayStatus)
        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "running"
        assert result["pid"] == 2468
        assert result["status_source"] == "pid"
        assert result["health"] == {"alive": True, "state": "alive", "reason": "pid_alive"}
        assert calls == [(profile / "gateway.pid", False)]


def test_status_reports_running_from_legacy_integer_gateway_pid():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids={1357})
        (profile / "gateway.pid").write_text("1357", encoding="utf-8")

        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "running"
        assert result["pid"] == 1357
        assert result["status_source"] == "pid"
        assert result["health"] == {"alive": True, "state": "alive", "reason": "pid_alive"}


def test_status_reports_running_from_fresh_runtime_file_when_pid_not_visible():
    import datetime as _dt

    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        fresh = _dt.datetime.now(_dt.timezone.utc).isoformat()
        (profile / "gateway_state.json").write_text(
            json.dumps({"gateway_state": "running", "updated_at": fresh, "argv": ["hermes", "gateway", "--token", "secret"]}),
            encoding="utf-8",
        )

        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "running"
        assert result["pid"] is None
        assert result["status_source"] == "runtime_file"
        assert result["health"]["alive"] is True
        assert result["health"]["reason"] == "cross_container_freshness"
        assert "token" not in json.dumps(result).lower()
        assert "secret" not in json.dumps(result).lower()


def test_status_reports_stopped_from_stale_running_runtime_file():
    """Stale gateway_state.json + no WebUI phase + no live PID = stopped.

    Older behavior reported 'unknown' here, which trapped the profile UI on
    "Check Status" forever (no second signal could ever unstick it once the
    heartbeat went silent). Reporting 'stopped' is both more accurate (a
    >120s-silent gateway is not functioning) and actionable. If the gateway
    is genuinely alive cross-container, its next fresh heartbeat will flip
    the status back to 'running' via the runtime_phase=='running' branch.
    """
    import datetime as _dt

    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        _install_fake_pid_alive(profiles, alive_pids=set())
        stale = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=600)).isoformat()
        (profile / "gateway_state.json").write_text(
            json.dumps({"gateway_state": "running", "updated_at": stale}),
            encoding="utf-8",
        )

        result = profiles.profile_gateway_status_api("coder")
        assert result["phase"] == "stopped"
        assert result["status_source"] == "runtime_file"
        assert result["health"]["alive"] is False
        assert "stale" in (result.get("detail") or "").lower()


def test_status_uses_selected_profile_name_not_active_profile():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        active = _seed_named_profile(base, "active")
        selected = _seed_named_profile(base, "selected")
        profiles = _reload_profiles_module(base)
        profiles._active_profile = "active"
        _install_fake_pid_alive(profiles, alive_pids={9002})
        (active / "gateway.pid").write_text("9001", encoding="utf-8")
        (selected / "gateway.pid").write_text("9002", encoding="utf-8")

        result = profiles.profile_gateway_status_api("selected")
        assert result["profile"] == "selected"
        assert result["phase"] == "running"
        assert result["pid"] == 9002


def test_status_only_config_with_stopped_state_disables_controls(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        (profile / "config.yaml").write_text(
            "webui:\n  gateway:\n    control:\n      mode: status_only\n",
            encoding="utf-8",
        )
        _write_state(profile, phase="stopped", desired_enabled=False)
        profiles = _reload_profiles_module(base)
        monkeypatch.delenv("WEBUI_GATEWAY_CONTROL_MODE", raising=False)
        monkeypatch.delenv("WEBUI_GATEWAY_REMOTE_HEALTH_URL", raising=False)
        _install_fake_pid_alive(profiles, alive_pids=set())

        result = profiles.profile_gateway_status_api("coder")

        assert result["phase"] == "unavailable"
        assert result["status_source"] == "adapter"
        assert result["control_available"] is False
        assert result["detail"]


def test_unavailable_config_with_failed_state_disables_controls(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        (profile / "config.yaml").write_text(
            "webui:\n  gateway:\n    control:\n      mode: unavailable\n",
            encoding="utf-8",
        )
        _write_state(profile, phase="failed", desired_enabled=False, last_error="old failure")
        profiles = _reload_profiles_module(base)
        monkeypatch.delenv("WEBUI_GATEWAY_CONTROL_MODE", raising=False)
        monkeypatch.delenv("WEBUI_GATEWAY_REMOTE_HEALTH_URL", raising=False)
        _install_fake_pid_alive(profiles, alive_pids=set())

        result = profiles.profile_gateway_status_api("coder")

        assert result["phase"] == "unavailable"
        assert result["status_source"] == "adapter"
        assert result["control_available"] is False
        assert result["detail"]


def test_docker_exec_config_with_stopped_state_disables_controls_when_cli_missing(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        (profile / "config.yaml").write_text(
            "webui:\n  gateway:\n    control:\n      mode: docker_exec\n      container: hermes-agent\n",
            encoding="utf-8",
        )
        _write_state(profile, phase="stopped", desired_enabled=False)
        profiles = _reload_profiles_module(base)
        monkeypatch.delenv("WEBUI_GATEWAY_CONTROL_MODE", raising=False)
        monkeypatch.delenv("WEBUI_GATEWAY_DOCKER_CONTAINER", raising=False)
        monkeypatch.delenv("WEBUI_GATEWAY_REMOTE_HEALTH_URL", raising=False)
        monkeypatch.setattr("shutil.which", lambda name: None if name == "docker" else "/usr/bin/other")
        _install_fake_pid_alive(profiles, alive_pids=set())

        result = profiles.profile_gateway_status_api("coder")

        assert result["phase"] == "unavailable"
        assert result["status_source"] == "adapter"
        assert result["control_available"] is False
        assert "docker" in result["detail"].lower()


def test_status_only_config_preserves_pid_running_but_disables_controls(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profile = _seed_named_profile(base, "coder")
        (profile / "config.yaml").write_text(
            "webui:\n  gateway:\n    control:\n      mode: status_only\n",
            encoding="utf-8",
        )
        (profile / "gateway.pid").write_text("4242", encoding="utf-8")
        _write_state(profile, phase="stopped", desired_enabled=False)
        profiles = _reload_profiles_module(base)
        monkeypatch.delenv("WEBUI_GATEWAY_CONTROL_MODE", raising=False)
        monkeypatch.delenv("WEBUI_GATEWAY_REMOTE_HEALTH_URL", raising=False)
        _install_fake_pid_alive(profiles, alive_pids={4242})

        result = profiles.profile_gateway_status_api("coder")

        assert result["phase"] == "running"
        assert result["status_source"] == "pid"
        assert result["control_available"] is False
        assert result["detail"]
