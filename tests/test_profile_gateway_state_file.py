"""Tests for .gateway-state.json phase persistence helpers."""

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
    sys.modules.update(_saved)
    api_pkg = sys.modules.get("api")
    if api_pkg is not None:
        for name, module in _saved.items():
            setattr(api_pkg, name.rsplit(".", 1)[-1], module)
    return profiles


def test_write_gateway_phase_creates_file_with_phase_and_timestamp():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        profiles = _reload_profiles_module(home)
        profiles._write_gateway_phase(home, "starting")
        data = json.loads((home / ".gateway-state.json").read_text(encoding="utf-8"))
        assert data["phase"] == "starting"
        assert isinstance(data["phase_started_at"], str)
        assert data["phase_started_at"].endswith("Z")
        assert data.get("last_error") is None


def test_write_gateway_phase_preserves_last_run_at():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        profiles = _reload_profiles_module(home)
        (home / ".gateway-state.json").write_text(
            json.dumps({"last_run_at": "2026-05-15T10:00:00Z"}), encoding="utf-8"
        )
        profiles._write_gateway_phase(home, "starting")
        data = json.loads((home / ".gateway-state.json").read_text(encoding="utf-8"))
        assert data["last_run_at"] == "2026-05-15T10:00:00Z"
        assert data["phase"] == "starting"


def test_write_gateway_phase_stopped_clears_phase_fields():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        profiles = _reload_profiles_module(home)
        profiles._write_gateway_phase(home, "starting")
        profiles._write_gateway_phase(home, "stopped")
        data = json.loads((home / ".gateway-state.json").read_text(encoding="utf-8"))
        assert data.get("phase") is None
        # phase_started_at and last_error should be cleared on stopped
        assert data.get("phase_started_at") is None
        assert data.get("last_error") is None


def test_write_gateway_phase_failed_sets_last_error():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        profiles = _reload_profiles_module(home)
        profiles._write_gateway_phase(home, "failed", last_error="connect refused")
        data = json.loads((home / ".gateway-state.json").read_text(encoding="utf-8"))
        assert data["phase"] == "failed"
        assert data["last_error"] == "connect refused"


def test_read_gateway_state_returns_empty_dict_on_missing():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        profiles = _reload_profiles_module(home)
        assert profiles._read_gateway_state(home) == {}


def test_write_gateway_phase_unknown_raises():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        profiles = _reload_profiles_module(home)
        with pytest.raises(ValueError, match="unknown gateway phase"):
            profiles._write_gateway_phase(home, "bogus")
