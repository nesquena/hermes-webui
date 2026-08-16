"""Behavioural proof: the mid-turn emitter actually runs and guards correctly.

The companion module pins structure; this one imports the real helpers and
exercises them, including every fail-closed branch.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _streaming():
    return importlib.import_module("api.streaming")


# ── the matcher ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "kind,message,expected",
    [
        ("compacted", "✓ Context compaction complete — continuing turn...", True),
        ("compacted", "", True),
        ("compacted", "Context compaction aborted", False),
        ("compacted", "compaction failed", False),
        ("lifecycle", "🗜️ Compacting context — summarizing...", False),
        ("", "", False),
        (None, None, False),
    ],
)
def test_done_matcher_behaviour(kind, message, expected):
    st = _streaming()
    assert st._is_agent_compression_done_status(kind, message) is expected


def test_start_and_done_matchers_are_disjoint():
    """A start status must never be read as a completion (and vice versa)."""
    st = _streaming()
    start_msg = "🗜️ Compacting context — summarizing earlier conversation..."
    assert st._is_agent_compression_start_status("lifecycle", start_msg) is True
    assert st._is_agent_compression_done_status("lifecycle", start_msg) is False


# ── the emitter ─────────────────────────────────────────────────────────────

class _Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, name, payload=None):
        self.events.append((name, payload))


def _patch_durability(monkeypatch, value):
    st = _streaming()
    monkeypatch.setattr(st, "_session_transcript_is_durable", lambda *a, **k: value)


def test_emitter_publishes_on_real_rotation(monkeypatch):
    st = _streaming()
    _patch_durability(monkeypatch, True)
    put = _Recorder()
    ok = st._emit_midturn_compaction_event(
        put,
        origin_session_id="sess_A",
        continuation_session_id="sess_B",
        settings={"auto_squash_after_compression": True},
    )
    assert ok is True
    assert len(put.events) == 1
    name, payload = put.events[0]
    assert name == "midturn_compacted"
    assert payload["old_session_id"] == "sess_A"
    assert payload["continuation_session_id"] == "sess_B"


def test_emitter_noop_without_rotation(monkeypatch):
    """Same id in and out: nothing was archived, so nothing may be hidden."""
    st = _streaming()
    _patch_durability(monkeypatch, True)
    put = _Recorder()
    ok = st._emit_midturn_compaction_event(
        put,
        origin_session_id="sess_A",
        continuation_session_id="sess_A",
        settings={"auto_squash_after_compression": True},
    )
    assert ok is False
    assert put.events == []


def test_emitter_noop_when_setting_disabled(monkeypatch):
    st = _streaming()
    _patch_durability(monkeypatch, True)
    put = _Recorder()
    ok = st._emit_midturn_compaction_event(
        put,
        origin_session_id="sess_A",
        continuation_session_id="sess_B",
        settings={"auto_squash_after_compression": False},
    )
    assert ok is False
    assert put.events == []


def test_emitter_fails_closed_when_history_not_durable(monkeypatch):
    """No proof the parent transcript is on disk -> never hide it."""
    st = _streaming()
    _patch_durability(monkeypatch, False)
    put = _Recorder()
    ok = st._emit_midturn_compaction_event(
        put,
        origin_session_id="sess_A",
        continuation_session_id="sess_B",
        settings={"auto_squash_after_compression": True},
    )
    assert ok is False
    assert put.events == []


def test_emitter_survives_a_failing_put(monkeypatch):
    """A broken SSE queue must never break the live turn."""
    st = _streaming()
    _patch_durability(monkeypatch, True)

    def _boom(name, payload=None):
        raise RuntimeError("queue closed")

    ok = st._emit_midturn_compaction_event(
        _boom,
        origin_session_id="sess_A",
        continuation_session_id="sess_B",
        settings={"auto_squash_after_compression": True},
    )
    assert ok is False


def test_emitter_handles_missing_continuation(monkeypatch):
    st = _streaming()
    _patch_durability(monkeypatch, True)
    put = _Recorder()
    for bad in (None, "", "   "):
        ok = st._emit_midturn_compaction_event(
            put,
            origin_session_id="sess_A",
            continuation_session_id=bad,
            settings={"auto_squash_after_compression": True},
        )
        assert ok is False
    assert put.events == []


# ── the durability probe ────────────────────────────────────────────────────

def test_durability_probe_fails_closed_on_lookup_error(monkeypatch):
    st = _streaming()
    fake = types.ModuleType("api.models")

    def _raise(*a, **k):
        raise RuntimeError("db down")

    fake.get_session = _raise
    monkeypatch.setitem(sys.modules, "api.models", fake)
    assert st._session_transcript_is_durable("sess_A") is False


def test_durability_probe_requires_messages(monkeypatch):
    st = _streaming()
    fake = types.ModuleType("api.models")

    class _Empty:
        messages = []

    fake.get_session = lambda *a, **k: _Empty()
    monkeypatch.setitem(sys.modules, "api.models", fake)
    assert st._session_transcript_is_durable("sess_A") is False


def test_durability_probe_accepts_real_transcript(monkeypatch):
    st = _streaming()
    fake = types.ModuleType("api.models")

    class _Full:
        messages = [{"role": "user", "content": "hi"}] * 12

    fake.get_session = lambda *a, **k: _Full()
    monkeypatch.setitem(sys.modules, "api.models", fake)
    assert st._session_transcript_is_durable("sess_A") is True


def test_durability_probe_rejects_missing_session(monkeypatch):
    st = _streaming()
    fake = types.ModuleType("api.models")
    fake.get_session = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "api.models", fake)
    assert st._session_transcript_is_durable("sess_A") is False
