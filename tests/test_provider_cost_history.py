"""Regression coverage for OpenRouter cost-history endpoint (#692).

Tests cover:
  - Happy-path snapshot append and delta computation
  - Missing credentials (no_key)
  - Unsupported provider (non-openrouter)
  - Upstream failure (graceful degradation with stale data)
  - Malformed / corrupt snapshot file on disk
  - Idempotent same-day updates
  - No real network calls or private credential leakage
"""

from __future__ import annotations

import json
import os
import sys
import types
import urllib.error
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import api.config as config
import api.profiles as profiles

ROOT = Path(__file__).resolve().parents[1]


class _FakeResponse:
    """Minimal stand-in for urllib.request.urlopen context manager."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _with_config(model=None, providers=None):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = model or {}
    if providers is not None:
        config.cfg["providers"] = providers
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    return old_cfg, old_mtime


def _restore_config(old_cfg, old_mtime):
    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime


# ── Happy path: snapshot append + delta response ──────────────────────────────


def test_openrouter_cost_history_happy_path(monkeypatch, tmp_path):
    """On-demand snapshot append returns deltas from cumulative usage."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-or-key\n", encoding="utf-8")
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    call_count = {"n": 0}

    def fake_urlopen(req, timeout):
        call_count["n"] += 1
        # Simulate cumulative usage of 5.0 credits used out of 20 limit
        payload = {
            "data": {
                "limit_remaining": 15.0,
                "usage": 5.0,
                "limit": 20,
                "label": "Test Label",
                "key": "must-not-leak",
            }
        }
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)

    # Freeze "today" so the test is deterministic
    fake_today = "2030-04-15"
    monkeypatch.setattr(providers, "datetime", type("DT", (), {
        "now": staticmethod(lambda tz=None: datetime(2030, 4, 15, 12, 0, 0, tzinfo=tz or timezone.utc)),
        "strftime": datetime.strftime,
    }))

    try:
        result = providers.get_provider_cost_history("openrouter", days=7)
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is True
    assert result["provider"] == "openrouter"
    assert result["supported"] is True
    assert result["status"] == "available"
    assert result["window_days"] == 7
    assert result["limit"] == 20
    assert result["label"] == "Test Label"
    assert result["message"] == "OpenRouter cost history loaded."
    # One snapshot for today
    assert len(result["snapshots"]) == 1
    snap = result["snapshots"][0]
    assert snap["date"] == fake_today
    assert snap["used"] == 5.0
    assert snap["delta"] is None  # first entry has no previous baseline
    # Verify the snapshot file was persisted
    snap_file = tmp_path / "cost-snapshots" / "openrouter.json"
    assert snap_file.exists()
    persisted = json.loads(snap_file.read_text(encoding="utf-8"))
    assert len(persisted["snapshots"]) == 1
    # No credential leakage
    assert "test-or-key" not in repr(result)
    assert "must-not-leak" not in repr(result)


def test_openrouter_cost_history_deltas_from_cumulative(monkeypatch, tmp_path):
    """Deltas are computed as differences between consecutive cumulative values."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-or-key\n", encoding="utf-8")
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    # Pre-seed two historical snapshots
    snap_dir = tmp_path / "cost-snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    historical = {
        "provider": "openrouter",
        "snapshots": [
            {"date": "2030-04-13", "used": 3.0, "limit": 20},
            {"date": "2030-04-14", "used": 4.5, "limit": 20},
        ],
    }
    (snap_dir / "openrouter.json").write_text(json.dumps(historical), encoding="utf-8")

    call_count = {"n": 0}

    def fake_urlopen(req, timeout):
        call_count["n"] += 1
        # Current cumulative usage is 7.0
        payload = {"data": {"usage": 7.0, "limit": 20, "label": "Credits"}}
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)

    # Freeze "today"
    monkeypatch.setattr(providers, "datetime", type("DT", (), {
        "now": staticmethod(lambda tz=None: datetime(2030, 4, 15, 12, 0, 0, tzinfo=tz or timezone.utc)),
        "strftime": datetime.strftime,
    }))

    try:
        result = providers.get_provider_cost_history("openrouter", days=7)
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is True
    snaps = result["snapshots"]
    assert len(snaps) == 3
    # Day 1: no delta (baseline)
    assert snaps[0]["date"] == "2030-04-13"
    assert snaps[0]["used"] == 3.0
    assert snaps[0]["delta"] is None
    # Day 2: delta = 4.5 - 3.0 = 1.5
    assert snaps[1]["date"] == "2030-04-14"
    assert snaps[1]["used"] == 4.5
    assert snaps[1]["delta"] == 1.5
    # Day 3 (today): delta = 7.0 - 4.5 = 2.5
    assert snaps[2]["date"] == "2030-04-15"
    assert snaps[2]["used"] == 7.0
    assert snaps[2]["delta"] == 2.5


def test_openrouter_cost_history_reset_uses_fresh_series_delta(monkeypatch, tmp_path):
    """A lower cumulative value starts a fresh series instead of a negative bar."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-or-key\n", encoding="utf-8")
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    snap_dir = tmp_path / "cost-snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    historical = {
        "provider": "openrouter",
        "snapshots": [
            {"date": "2030-04-13", "used": 9.0, "limit": 20},
            {"date": "2030-04-14", "used": 12.0, "limit": 20},
        ],
    }
    (snap_dir / "openrouter.json").write_text(json.dumps(historical), encoding="utf-8")

    def fake_urlopen(req, timeout):
        # Simulate key rotation or provider reset: cumulative usage dropped.
        payload = {"data": {"usage": 1.25, "limit": 20, "label": "Credits"}}
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(providers, "datetime", type("DT", (), {
        "now": staticmethod(lambda tz=None: datetime(2030, 4, 15, 12, 0, 0, tzinfo=tz or timezone.utc)),
        "strftime": datetime.strftime,
    }))

    try:
        result = providers.get_provider_cost_history("openrouter", days=7)
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is True
    assert result["snapshots"][-1]["date"] == "2030-04-15"
    assert result["snapshots"][-1]["used"] == 1.25
    assert result["snapshots"][-1]["delta"] == 1.25
    assert all(snap["delta"] is None or snap["delta"] >= 0 for snap in result["snapshots"])


def test_cost_snapshot_append_uses_lock(monkeypatch, tmp_path):
    """Snapshot append serializes the read-modify-write critical section."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    import api.providers as providers

    entered = {"count": 0}

    class RecordingLock:
        def __enter__(self):
            entered["count"] += 1
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(providers, "_COST_SNAPSHOT_LOCK", RecordingLock())
    monkeypatch.setattr(providers, "datetime", type("DT", (), {
        "now": staticmethod(lambda tz=None: datetime(2030, 4, 15, 12, 0, 0, tzinfo=tz or timezone.utc)),
        "strftime": datetime.strftime,
    }))

    snapshots = providers._append_cost_snapshot("openrouter", 4.0, 20.0)

    assert entered["count"] == 1
    assert snapshots == [{"date": "2030-04-15", "used": 4.0, "limit": 20.0}]


# ── Missing credentials ───────────────────────────────────────────────────────


def test_openrouter_cost_history_no_key(monkeypatch, tmp_path):
    """No API key → safe no_key response without network call."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    def explode(*_a, **_kw):
        raise AssertionError("should not call network without a key")

    monkeypatch.setattr(providers.urllib.request, "urlopen", explode)

    try:
        result = providers.get_provider_cost_history("openrouter", days=7)
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is False
    assert result["provider"] == "openrouter"
    assert result["supported"] is True
    assert result["status"] == "no_key"
    assert "OPENROUTER_API_KEY" in result["message"]


# ── Unsupported provider ──────────────────────────────────────────────────────


def test_cost_history_unsupported_provider(monkeypatch, tmp_path):
    """Non-openrouter providers return a clear unsupported response."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={"provider": "anthropic"})

    import api.providers as providers

    try:
        result = providers.get_provider_cost_history("anthropic", days=7)
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is False
    assert result["provider"] == "anthropic"
    assert result["supported"] is False
    assert result["status"] == "unsupported"
    assert "openrouter" in result["message"].lower()


def test_cost_history_missing_provider_param(monkeypatch, tmp_path):
    """Empty provider parameter returns a clear error."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    import api.providers as providers

    result = providers.get_provider_cost_history("", days=7)
    assert result["ok"] is False
    assert result["status"] == "missing_provider"

    result2 = providers.get_provider_cost_history(None, days=7)
    assert result2["ok"] is False
    assert result2["status"] == "missing_provider"


# ── Upstream failure / graceful degradation ────────────────────────────────────


def test_openrouter_cost_history_upstream_failure_degrades_gracefully(monkeypatch, tmp_path):
    """When OpenRouter API fails, previously persisted snapshots are still returned."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-or-key\n", encoding="utf-8")
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    # Pre-seed a snapshot
    snap_dir = tmp_path / "cost-snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    historical = {
        "provider": "openrouter",
        "snapshots": [
            {"date": "2030-04-13", "used": 3.0, "limit": 20},
        ],
    }
    (snap_dir / "openrouter.json").write_text(json.dumps(historical), encoding="utf-8")

    req = providers.urllib.request.Request("https://openrouter.ai/api/v1/key")
    def fake_urlopen(_req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, BytesIO(b"error"))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)

    try:
        result = providers.get_provider_cost_history("openrouter", days=7)
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is False
    assert result["status"] == "unavailable"
    # Still returns previously persisted data
    assert len(result["snapshots"]) == 1
    assert result["snapshots"][0]["date"] == "2030-04-13"
    assert "temporarily unavailable" in result["message"].lower()


def test_openrouter_cost_history_timeout_is_safe(monkeypatch, tmp_path):
    """Timeout from OpenRouter does not produce a traceback or leak secrets."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-or-key\n", encoding="utf-8")
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    def fake_urlopen(_req, timeout=None):
        raise TimeoutError("slow secret")

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)

    try:
        result = providers.get_provider_cost_history("openrouter", days=7)
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is False
    assert result["status"] == "unavailable"
    assert "test-or-key" not in repr(result)
    assert "secret" not in repr(result).lower()


# ── Malformed / corrupt snapshot file ─────────────────────────────────────────


def test_openrouter_cost_history_corrupt_snapshot_file(monkeypatch, tmp_path):
    """A corrupt snapshot file on disk is handled gracefully (treated as empty)."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-or-key\n", encoding="utf-8")
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    # Write a corrupt file
    snap_dir = tmp_path / "cost-snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "openrouter.json").write_text("NOT VALID JSON{{{{", encoding="utf-8")

    def fake_urlopen(req, timeout):
        payload = {"data": {"usage": 2.0, "limit": 10, "label": "Credits"}}
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)

    # Freeze "today"
    monkeypatch.setattr(providers, "datetime", type("DT", (), {
        "now": staticmethod(lambda tz=None: datetime(2030, 4, 15, 12, 0, 0, tzinfo=tz or timezone.utc)),
        "strftime": datetime.strftime,
    }))

    try:
        result = providers.get_provider_cost_history("openrouter", days=7)
    finally:
        _restore_config(old_cfg, old_mtime)

    # Corrupt file is ignored; fresh snapshot is created
    assert result["ok"] is True
    assert len(result["snapshots"]) == 1
    assert result["snapshots"][0]["date"] == "2030-04-15"


# ── Idempotent same-day updates ───────────────────────────────────────────────


def test_openrouter_cost_history_same_day_idempotent(monkeypatch, tmp_path):
    """Repeated calls on the same day update the snapshot in-place."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-or-key\n", encoding="utf-8")
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    call_count = {"n": 0}

    def fake_urlopen(req, timeout):
        call_count["n"] += 1
        usage = 5.0 + call_count["n"]  # usage grows each call
        payload = {"data": {"usage": usage, "limit": 20, "label": "Credits"}}
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)

    # Freeze "today"
    monkeypatch.setattr(providers, "datetime", type("DT", (), {
        "now": staticmethod(lambda tz=None: datetime(2030, 4, 15, 12, 0, 0, tzinfo=tz or timezone.utc)),
        "strftime": datetime.strftime,
    }))

    try:
        r1 = providers.get_provider_cost_history("openrouter", days=7)
        r2 = providers.get_provider_cost_history("openrouter", days=7)
    finally:
        _restore_config(old_cfg, old_mtime)

    # Both calls succeed; only one snapshot date (today)
    assert r1["ok"] is True
    assert r2["ok"] is True
    assert len(r1["snapshots"]) == 1
    assert len(r2["snapshots"]) == 1
    # Second call updated the same day's used value
    assert r2["snapshots"][0]["used"] == 7.0  # 5.0 + 2 (second call)
    # Verify persisted file has only one entry for today
    snap_file = tmp_path / "cost-snapshots" / "openrouter.json"
    persisted = json.loads(snap_file.read_text(encoding="utf-8"))
    assert len(persisted["snapshots"]) == 1


# ── Window days parameter ─────────────────────────────────────────────────────


def test_openrouter_cost_history_window_days_truncation(monkeypatch, tmp_path):
    """The window_days parameter limits how many snapshots are returned."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-or-key\n", encoding="utf-8")
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    # Pre-seed 5 historical snapshots
    snap_dir = tmp_path / "cost-snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    historical = {
        "provider": "openrouter",
        "snapshots": [
            {"date": f"2030-04-{d:02d}", "used": float(d), "limit": 20}
            for d in range(10, 15)
        ],
    }
    (snap_dir / "openrouter.json").write_text(json.dumps(historical), encoding="utf-8")

    def fake_urlopen(req, timeout):
        payload = {"data": {"usage": 15.0, "limit": 20, "label": "Credits"}}
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)

    # Freeze "today"
    monkeypatch.setattr(providers, "datetime", type("DT", (), {
        "now": staticmethod(lambda tz=None: datetime(2030, 4, 15, 12, 0, 0, tzinfo=tz or timezone.utc)),
        "strftime": datetime.strftime,
    }))

    try:
        result = providers.get_provider_cost_history("openrouter", days=3)
    finally:
        _restore_config(old_cfg, old_mtime)

    # 5 historical + 1 today = 6 total, but window_days=3 returns last 3
    assert result["ok"] is True
    assert result["window_days"] == 3
    assert len(result["snapshots"]) == 3
    # The returned snapshots are the most recent 3
    assert result["snapshots"][0]["date"] == "2030-04-13"
    assert result["snapshots"][1]["date"] == "2030-04-14"
    assert result["snapshots"][2]["date"] == "2030-04-15"


# ── No real network calls ─────────────────────────────────────────────────────


def test_cost_history_uses_no_real_network(monkeypatch, tmp_path):
    """Every test path must monkeypatch urlopen; verify no real calls escape."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    # Without a key, no network call is made at all
    def explode(*_a, **_kw):
        raise AssertionError("real network call detected")

    monkeypatch.setattr(providers.urllib.request, "urlopen", explode)

    try:
        result = providers.get_provider_cost_history("openrouter", days=7)
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["status"] == "no_key"