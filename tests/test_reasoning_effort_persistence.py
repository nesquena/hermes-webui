"""Regression coverage for per-chat reasoning_effort persistence (#7381).

Walter Gaalswyk's review of #7381 found the blocker: the PR added
``reasoning_effort`` to ``Session.__init__``'s signature and to
``METADATA_FIELDS``, but never assigned it in the init body — so save()
persisted it to the sidecar while load() never restored it, silently
reverting per-chat effort to the global default on reload/cache eviction.
"""
import json

import pytest

import api.config as config
import api.models as models
from api.models import Session


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()
    yield session_dir
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()


def test_reasoning_effort_survives_save_and_load_roundtrip(tmp_path):
    """The #7381 blocker: set -> save -> load must restore the override."""
    s = Session(session_id="rt1", title="t")
    s.reasoning_effort = "low"
    s.save(touch_updated_at=False)

    # The value is on disk...
    sidecar = json.loads((tmp_path / "sessions" / "rt1.json").read_text())
    assert sidecar.get("reasoning_effort") == "low"

    # ...and a fresh load must restore it (this was the AttributeError).
    s2 = Session.load("rt1")
    assert s2 is not None
    assert getattr(s2, "reasoning_effort", None) == "low"


def test_reasoning_effort_accepted_via_constructor_kwargs():
    """A sidecar dict passed through cls(**data) lands on the instance."""
    s = Session(session_id="rt2", title="t", reasoning_effort="high")
    assert s.reasoning_effort == "high"


def test_reasoning_effort_defaults_to_none_when_absent():
    """Old sidecars without the field must not break or invent a value."""
    s = Session(session_id="rt3", title="t")
    assert s.reasoning_effort is None


def test_reasoning_effort_none_persists_and_loads_as_none(tmp_path):
    """Explicit Default (None) round-trips as None, not a stale value."""
    s = Session(session_id="rt4", title="t", reasoning_effort=None)
    s.save(touch_updated_at=False)
    s2 = Session.load("rt4")
    assert getattr(s2, "reasoning_effort", "MISSING") is None
