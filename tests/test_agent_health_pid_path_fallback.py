"""Regression coverage for _gateway_root_pid_path() profile-scoped fallback.

Before the fix, _gateway_root_pid_path() unconditionally returned
<hermes_root>/gateway.pid.  Profile-scoped gateways (running via
``gateway run --profile <name>`` or with ``active_profile`` set) write
their PID file under <hermes_root>/profiles/<name>/gateway.pid instead of
the root, so the root-level file never existed.  The WebUI's
build_agent_health_payload() therefore always received a non-existent
pid_path, fell through to the stale root-level gateway_state.json, and
returned alive=None — causing the cron page to display "Gateway not
configured" even though the gateway was running.

Fix: when the root-level gateway.pid is absent, _gateway_root_pid_path()
now falls back to the active profile's directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("hermes_constants", reason="hermes-agent not installed")


def _call(monkeypatch, root: Path, profile_dir: Path | None = None) -> Path | None:
    """Call _gateway_root_pid_path() with mocked filesystem roots."""
    import hermes_constants
    import api.profiles as profiles

    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)
    if profile_dir is not None:
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: str(profile_dir))

    from api.agent_health import _gateway_root_pid_path
    return _gateway_root_pid_path()


# ── core behaviour ────────────────────────────────────────────────────────────

def test_returns_root_pid_when_root_level_file_exists(tmp_path, monkeypatch):
    """Root-level gateway.pid present → return it (original behaviour unchanged)."""
    root = tmp_path / "hermes"
    root.mkdir()
    root_pid = root / "gateway.pid"
    root_pid.write_text("1234")
    profile_dir = root / "profiles" / "other"
    profile_dir.mkdir(parents=True)
    (profile_dir / "gateway.pid").write_text("9999")

    result = _call(monkeypatch, root=root, profile_dir=profile_dir)
    assert result == root_pid


def test_falls_back_to_profile_pid_when_root_absent(tmp_path, monkeypatch):
    """Root gateway.pid absent + profile-level exists → return profile path."""
    root = tmp_path / "hermes"
    root.mkdir()
    # root-level gateway.pid intentionally not created
    profile_dir = root / "profiles" / "safeline"
    profile_dir.mkdir(parents=True)
    profile_pid = profile_dir / "gateway.pid"
    profile_pid.write_text("5678")

    result = _call(monkeypatch, root=root, profile_dir=profile_dir)
    assert result == profile_pid


def test_returns_root_path_when_neither_pid_exists(tmp_path, monkeypatch):
    """Neither root nor profile gateway.pid exists → return root path (graceful)."""
    root = tmp_path / "hermes"
    root.mkdir()
    profile_dir = root / "profiles" / "empty"
    profile_dir.mkdir(parents=True)
    # no gateway.pid created anywhere

    result = _call(monkeypatch, root=root, profile_dir=profile_dir)
    assert result == root / "gateway.pid"


def test_returns_root_path_when_profile_lookup_raises(tmp_path, monkeypatch):
    """get_active_hermes_home() raising must be caught; root path returned."""
    root = tmp_path / "hermes"
    root.mkdir()

    import hermes_constants
    import api.profiles as profiles

    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)

    def _raise():
        raise RuntimeError("profile resolution failed")

    monkeypatch.setattr(profiles, "get_active_hermes_home", _raise)

    from api.agent_health import _gateway_root_pid_path
    result = _gateway_root_pid_path()
    assert result == root / "gateway.pid"


def test_root_takes_priority_over_profile_when_both_exist(tmp_path, monkeypatch):
    """Root gateway.pid present even when profile pid also exists → root wins."""
    root = tmp_path / "hermes"
    root.mkdir()
    root_pid = root / "gateway.pid"
    root_pid.write_text("1111")
    profile_dir = root / "profiles" / "safeline"
    profile_dir.mkdir(parents=True)
    (profile_dir / "gateway.pid").write_text("2222")

    result = _call(monkeypatch, root=root, profile_dir=profile_dir)
    assert result == root_pid
