"""Tests for the profile activity aggregator — sessions-this-week + gateway state.

The reworked profile screen has an "Activity line" beneath the hero dossier
that shows last-used, sessions-this-week, optional spend, and gateway last
run. The aggregation is pure-data work over the session index, kept in
api.profiles as `_compute_profile_activity` so it can be unit-tested without
spinning up the HTTP server or a session store.
"""

import importlib
import json
import os
import sys
import tempfile
import time
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
    sys.modules.update(_saved)
    api_pkg = sys.modules.get("api")
    if api_pkg is not None:
        for name, module in _saved.items():
            setattr(api_pkg, name.rsplit(".", 1)[-1], module)
    return profiles


def _seed_profile(base: Path, name: str) -> Path:
    pdir = base / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    return pdir


def _now() -> float:
    return time.time()


def _row(profile: str, *, age_days: float = 0.0) -> dict:
    """Build a minimal session-index row resembling Session.compact()."""
    ts = _now() - age_days * 86400.0
    return {
        "session_id": f"sess-{profile}-{age_days}",
        "profile": profile,
        "updated_at": ts,
        "last_message_at": ts,
        "created_at": ts,
        "input_tokens": 100,
        "output_tokens": 50,
        "estimated_cost": 0.01,
    }


# ── _compute_profile_activity (pure aggregator) ──────────────────────────────


def test_compute_activity_empty_rows():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        result = profiles._compute_profile_activity([], "default", now=_now())
        assert result == {
            "sessions_week": 0,
            "last_used_at": None,
            "spend_week_usd": None,
        }


def test_compute_activity_counts_only_own_profile_in_last_week():
    rows = [
        _row("coder", age_days=1),
        _row("coder", age_days=3),
        _row("coder", age_days=10),     # outside the 7-day window
        _row("writer", age_days=2),     # different profile
    ]
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        result = profiles._compute_profile_activity(rows, "coder", now=_now())
        assert result["sessions_week"] == 2
        assert result["last_used_at"] is not None


def test_compute_activity_last_used_is_most_recent_session_timestamp():
    now = _now()
    rows = [
        _row("coder", age_days=5),
        _row("coder", age_days=0.5),    # most recent
        _row("coder", age_days=3),
    ]
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        result = profiles._compute_profile_activity(rows, "coder", now=now)
        assert result["last_used_at"] is not None
        # Must equal the most-recent row's ISO-formatted updated_at (UTC, Z suffix).
        most_recent_ts = max(r["updated_at"] for r in rows)
        import datetime as dt
        expected = dt.datetime.fromtimestamp(most_recent_ts, tz=dt.timezone.utc) \
            .isoformat().replace("+00:00", "Z")
        assert result["last_used_at"] == expected


def test_compute_activity_treats_missing_profile_field_as_default():
    """Legacy sessions written before per-session profile tagging."""
    rows = [
        {"session_id": "old", "updated_at": _now() - 86400, "input_tokens": 0,
         "output_tokens": 0, "estimated_cost": 0.0},
    ]
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        result = profiles._compute_profile_activity(rows, "default", now=_now())
        assert result["sessions_week"] == 1


def test_compute_activity_last_used_is_unbounded_by_week_window():
    """Regression for validator F#15: a profile last touched outside the 7-day
    window must still report a non-null last_used_at — only sessions_week is
    scoped to the cutoff, last_used_at is the most-recent timestamp ever."""
    rows = [
        _row("coder", age_days=30),     # well outside the weekly window
        _row("coder", age_days=14),     # also outside
    ]
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        result = profiles._compute_profile_activity(rows, "coder", now=_now())
        assert result["sessions_week"] == 0
        # Last used is the most recent session — 14 days ago, not null.
        assert result["last_used_at"] is not None


def test_compute_activity_spend_is_none_in_v1():
    """V1 ships with no spend display until cost-tracking lands."""
    rows = [_row("coder", age_days=1)]
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        result = profiles._compute_profile_activity(rows, "coder", now=_now())
        assert result["spend_week_usd"] is None


# ── read_profile_activity_api (helper + gateway state) ───────────────────────


def test_activity_missing_profile_raises_file_not_found():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        with pytest.raises(FileNotFoundError):
            profiles.read_profile_activity_api("ghost")


def test_activity_invalid_name_raises_value_error():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        profiles = _reload_profiles_module(base)
        with pytest.raises(ValueError):
            profiles.read_profile_activity_api("../bad")


def test_activity_fresh_profile_is_empty():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        _seed_profile(base, "coder")
        profiles = _reload_profiles_module(base)
        result = profiles.read_profile_activity_api("coder")
        assert result["name"] == "coder"
        assert result["sessions_week"] == 0
        assert result["last_used_at"] is None
        assert result["ever_started_gateway"] is False
        assert result["gateway_last_run_at"] is None
        assert result["spend_week_usd"] is None


def test_activity_reads_gateway_state_when_present():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        pdir = _seed_profile(base, "coder")
        (pdir / ".gateway-state.json").write_text(
            json.dumps({"last_run_at": "2026-05-13T18:22:04Z"}),
            encoding="utf-8",
        )
        profiles = _reload_profiles_module(base)
        result = profiles.read_profile_activity_api("coder")
        assert result["ever_started_gateway"] is True
        assert result["gateway_last_run_at"] == "2026-05-13T18:22:04Z"


def test_activity_handles_malformed_gateway_state():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        (base / "profiles").mkdir(parents=True)
        pdir = _seed_profile(base, "coder")
        (pdir / ".gateway-state.json").write_text("not json", encoding="utf-8")
        profiles = _reload_profiles_module(base)
        result = profiles.read_profile_activity_api("coder")
        # Malformed JSON is treated as "no state" rather than crashing.
        assert result["ever_started_gateway"] is False
        assert result["gateway_last_run_at"] is None


def test_activity_supports_default_profile():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / ".hermes"
        base.mkdir(parents=True)
        (base / "profiles").mkdir(exist_ok=True)
        # Default profile lives at HERMES_HOME root; its gateway-state file
        # lives there too.
        (base / ".gateway-state.json").write_text(
            json.dumps({"last_run_at": "2026-05-13T08:00:00Z"}),
            encoding="utf-8",
        )
        profiles = _reload_profiles_module(base)
        result = profiles.read_profile_activity_api("default")
        assert result["name"] == "default"
        assert result["ever_started_gateway"] is True
        assert result["gateway_last_run_at"] == "2026-05-13T08:00:00Z"
