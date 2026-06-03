"""Tests for #1624: _repair_stale_pending must not fire on fresh turns.

Bug shape: _repair_stale_pending() fires whenever pending_user_message is set
and the active_stream_id is not in the live STREAMS registry. There's a
narrow race between the streaming thread clearing pending_user_message and
STREAMS.pop(stream_id), so any fast turn (e.g. command approval) that exits
the thread before the on-disk pending clear flushes gets misdiagnosed as a
crashed turn — producing a spurious "Previous turn did not complete." marker.

Fix: add a grace-period guard. A turn whose pending_started_at is younger
than _REPAIR_STALE_PENDING_GRACE_SECONDS is treated as "the streaming thread
may still be in its post-loop cleanup window" and the repair bails. Missing
or falsy pending_started_at (legacy sidecars that pre-date the field) is
treated as "old enough" to preserve current legacy-data recovery semantics.
"""

import time
import threading
from unittest.mock import patch

import pytest


# ── _repair_stale_pending grace guard ───────────────────────────────────


class _FakeSession:
    """Minimal stand-in for api.models.Session — only the fields _repair_stale_pending reads."""
    def __init__(self, sid="abcdef123456", pending="hi", stream_id="stream_xyz",
                 pending_started_at=None, profile="default"):
        self.session_id = sid
        self.pending_user_message = pending
        self.active_stream_id = stream_id
        self.pending_started_at = pending_started_at
        self.profile = profile
        self.messages = []


def _setup_repair_environment(monkeypatch, tmp_path):
    """Stub out the costly side-channels in _repair_stale_pending so the
    tests exercise the guard logic alone, not the full lock+sidecar pipeline."""
    import api.models as models

    # No live streams — the predicate's "stream not in registry" branch fires.
    monkeypatch.setattr(models, "_active_stream_ids", lambda: set())

    # Profile home -> tmp dir; sessions/<sid>.json doesn't need to exist
    # because we'll stub _apply_core_sync_or_error_marker.
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: tmp_path)
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)

    # Track whether the heavy-lift function was called so we can assert.
    calls = {"applied": 0}

    def fake_apply(session, core_path, **kw):
        calls["applied"] += 1
        return True
    monkeypatch.setattr(models, "_apply_core_sync_or_error_marker", fake_apply)

    return calls


def test_repair_skips_fresh_turn(tmp_path, monkeypatch):
    """A turn that started 5 seconds ago is too fresh — repair must bail."""
    import api.models as models
    calls = _setup_repair_environment(monkeypatch, tmp_path)

    s = _FakeSession(pending_started_at=time.time() - 5.0)
    result = models._repair_stale_pending(s)
    assert result is False, "Repair must skip a 5s-old turn"
    assert calls["applied"] == 0, "Heavy-lift _apply_core_sync_or_error_marker must not be called"


def test_repair_skips_almost_grace_window(tmp_path, monkeypatch):
    """A turn 1 second younger than the grace threshold must still bail."""
    import api.models as models
    calls = _setup_repair_environment(monkeypatch, tmp_path)
    grace = models._REPAIR_STALE_PENDING_GRACE_SECONDS

    s = _FakeSession(pending_started_at=time.time() - (grace - 1.0))
    result = models._repair_stale_pending(s)
    assert result is False, f"Repair must skip a turn {grace - 1}s old"
    assert calls["applied"] == 0


def test_repair_fires_after_grace_window(tmp_path, monkeypatch):
    """A turn older than the grace window should trigger repair as before."""
    import api.models as models
    calls = _setup_repair_environment(monkeypatch, tmp_path)
    grace = models._REPAIR_STALE_PENDING_GRACE_SECONDS

    s = _FakeSession(pending_started_at=time.time() - (grace + 30.0))
    result = models._repair_stale_pending(s)
    assert result is True, f"Repair must fire on a turn older than {grace}s"
    assert calls["applied"] == 1, "Heavy-lift _apply_core_sync_or_error_marker should be called"


def test_repair_fires_when_pending_started_at_missing(tmp_path, monkeypatch):
    """Legacy sidecars predate `pending_started_at`; missing/falsy must NOT
    block repair — preserves current behavior for legacy data."""
    import api.models as models
    calls = _setup_repair_environment(monkeypatch, tmp_path)

    s = _FakeSession(pending_started_at=None)
    result = models._repair_stale_pending(s)
    assert result is True, "Missing pending_started_at must not block legitimate repair"
    assert calls["applied"] == 1


def test_repair_fires_when_pending_started_at_zero(tmp_path, monkeypatch):
    """Falsy 0 must also be treated as 'old enough' (defense against accidental zeroing)."""
    import api.models as models
    calls = _setup_repair_environment(monkeypatch, tmp_path)

    s = _FakeSession(pending_started_at=0)
    result = models._repair_stale_pending(s)
    assert result is True, "pending_started_at=0 must not block legitimate repair"


def test_repair_fires_when_pending_started_at_garbage(tmp_path, monkeypatch):
    """Garbage values (string, dict, etc.) shouldn't crash and shouldn't block repair."""
    import api.models as models
    calls = _setup_repair_environment(monkeypatch, tmp_path)

    s = _FakeSession(pending_started_at="not-a-number")
    result = models._repair_stale_pending(s)
    assert result is True, "Garbage pending_started_at should be treated as 'old enough'"


def test_repair_skips_when_no_pending_message(tmp_path, monkeypatch):
    """Without pending_user_message, repair must always bail (existing contract)."""
    import api.models as models
    calls = _setup_repair_environment(monkeypatch, tmp_path)

    s = _FakeSession(pending="", pending_started_at=time.time() - 60)
    result = models._repair_stale_pending(s)
    assert result is False
    assert calls["applied"] == 0


def test_repair_skips_when_stream_still_alive(tmp_path, monkeypatch):
    """If the stream is still in the registry, repair must bail even past grace."""
    import api.models as models
    monkeypatch.setattr(models, "_active_stream_ids", lambda: {"stream_xyz"})
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: tmp_path)

    s = _FakeSession(pending_started_at=time.time() - 600)
    result = models._repair_stale_pending(s)
    assert result is False, "Stream-alive bail predates the grace guard"


def test_grace_constant_exists_and_is_sane():
    """The grace constant is exposed and sized in a sane range (10s..120s)."""
    import api.models as models
    grace = models._REPAIR_STALE_PENDING_GRACE_SECONDS
    assert isinstance(grace, (int, float))
    assert 10 <= grace <= 120, (
        f"Grace window {grace}s should be 10s-120s — too small re-introduces "
        "the false-positive race; too large delays legitimate recovery."
    )
