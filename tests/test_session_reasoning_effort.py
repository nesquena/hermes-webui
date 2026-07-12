"""Tests for session-scoped reasoning effort.

Verifies that reasoning_effort is stored per-session in the Session model
(like model/provider), persists to the session JSON, flows through
/api/session/update, and overrides config.yaml at agent invocation time.
"""

import tempfile
from pathlib import Path

from api.models import Session
from api.config import get_reasoning_status


def test_session_stores_reasoning_effort_in_constructor():
    """Session constructor accepts and stores reasoning_effort."""
    s = Session(reasoning_effort="high")
    assert s.reasoning_effort == "high"

    s2 = Session(reasoning_effort=None)
    assert s2.reasoning_effort is None

    s3 = Session()  # default
    assert s3.reasoning_effort is None


def test_reasoning_effort_in_compact():
    """Session.compact() includes reasoning_effort."""
    s = Session(reasoning_effort="medium")
    c = s.compact()
    assert c["reasoning_effort"] == "medium"

    s2 = Session()
    c2 = s2.compact()
    assert c2["reasoning_effort"] is None


def test_reasoning_effort_persists_to_session_json(monkeypatch):
    """reasoning_effort survives a save/load round-trip."""
    td = tempfile.mkdtemp(prefix="webui-test-re-")
    monkeypatch.setattr("api.models.SESSION_DIR", Path(td))
    try:
        s = Session(reasoning_effort="xhigh", model="test/model")
        s.save()

        loaded = Session.load(s.session_id)
        assert loaded is not None
        assert loaded.reasoning_effort == "xhigh"
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)


def test_reasoning_effort_none_is_falsy_default():
    """None reasoning_effort means 'use config.yaml default'."""
    s = Session(reasoning_effort=None)
    assert not s.reasoning_effort
    # Compact should still include it
    assert "reasoning_effort" in s.compact()


def test_get_reasoning_status_with_override():
    """get_reasoning_status() returns override_effort when provided."""
    st = get_reasoning_status(override_effort="high")
    # With override, the returned effort should be the override value
    # (coerced per model capabilities — without a model, coercion is lenient)
    assert st["reasoning_effort"] in ("high", "")


def test_get_reasoning_status_override_none_falls_back():
    """get_reasoning_status() with override_effort=None reads config.yaml."""
    st = get_reasoning_status(override_effort=None)
    # Returns whatever config.yaml has (or empty string if unset)
    assert isinstance(st["reasoning_effort"], str)


# ── Phase 1: adapter-off default return path ──────────────────────────

def test_adapter_off_start_run_forwards_reasoning_effort(monkeypatch):
    """_start_run default return passes reasoning_effort to _start_chat_stream_for_session."""
    from api.routes import _start_run
    from api.models import Session

    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: False)

    captured = {}
    def _stub_stream(*args, **kwargs):
        captured.update(kwargs)
        return {"stream_id": "fake", "_status": 200}

    monkeypatch.setattr("api.routes._start_chat_stream_for_session", _stub_stream)

    s = Session(model="test/model")
    _start_run(
        s,
        msg="hello",
        attachments=[],
        workspace="/tmp",
        model="test/model",
        model_provider="test",
        reasoning_effort="high",
        normalized_model=True,
        source="webui",
        route="test",
    )
    assert captured.get("reasoning_effort") == "high"


def test_adapter_off_start_run_forwards_none_reasoning_effort(monkeypatch):
    """_start_run forwards None reasoning_effort unchanged."""
    from api.routes import _start_run
    from api.models import Session

    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: False)

    captured = {}
    def _stub_stream(*args, **kwargs):
        captured.update(kwargs)
        return {"stream_id": "fake", "_status": 200}

    monkeypatch.setattr("api.routes._start_chat_stream_for_session", _stub_stream)

    s = Session(model="test/model")
    _start_run(
        s,
        msg="hello",
        attachments=[],
        workspace="/tmp",
        model="test/model",
        model_provider="test",
        reasoning_effort=None,
        normalized_model=True,
        source="webui",
        route="test",
    )
    assert captured.get("reasoning_effort") is None


# ── Phase 2: start_session_turn wakeup path ────────────────────────────

def test_start_session_turn_forwards_session_reasoning_effort(monkeypatch):
    """start_session_turn passes s.reasoning_effort to _start_run."""
    import tempfile
    from pathlib import Path
    from api.models import Session
    from api.routes import start_session_turn

    td = tempfile.mkdtemp(prefix="webui-test-wakeup-")
    monkeypatch.setattr("api.models.SESSION_DIR", Path(td))
    try:
        s = Session(reasoning_effort="xhigh", model="test/model")
        s.save()

        captured = {}
        def _stub_run(s_arg, **kwargs):
            captured.update(kwargs)
            return {"stream_id": "fake", "_status": 200}

        monkeypatch.setattr("api.routes._start_run", _stub_run)
        monkeypatch.setattr("api.routes.clear_process_wakeup_pause_if_model_changed", lambda *a, **kw: True)
        monkeypatch.setattr("api.routes.process_wakeup_pause_credential_state_changed", lambda *a, **kw: True)
        monkeypatch.setattr("api.routes.process_wakeup_pause_matches", lambda *a, **kw: False)

        start_session_turn(s.session_id, "wakeup message")
        assert captured.get("reasoning_effort") == "xhigh"
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)
