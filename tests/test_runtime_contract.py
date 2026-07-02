import importlib

import pytest


_RUNTIME_CONTRACT = None


def _contract():
    global _RUNTIME_CONTRACT
    if _RUNTIME_CONTRACT is None:
        _RUNTIME_CONTRACT = importlib.import_module("api.runtime_contract")
    return _RUNTIME_CONTRACT


def test_runtime_event_serializes_expected_shape():
    ct = _contract()
    ev = ct.make_event(
        run_id="run-1",
        session_id="sess-1",
        seq=1,
        type="run.started",
    )
    d = ev.to_dict()
    assert d["event_id"] == "run-1:1"
    assert d["seq"] == 1
    assert d["run_id"] == "run-1"
    assert d["session_id"] == "sess-1"
    assert d["type"] == "run.started"
    assert isinstance(d["created_at"], float)
    assert d["terminal"] is False
    assert isinstance(d["payload"], dict)


def test_make_event_creates_stable_event_id():
    ct = _contract()
    ev = ct.make_event(run_id="r", session_id="s", seq=42, type="run.started")
    assert ev.event_id == "r:42"
    d = ev.to_dict()
    assert d["event_id"] == "r:42"


def test_make_event_preserves_run_id_session_id_seq_type_payload():
    ct = _contract()
    payload = {"key": "value", "nested": {"a": 1}}
    ev = ct.make_event(
        run_id="r99",
        session_id="s99",
        seq=3,
        type="token.delta",
        payload=payload,
    )
    d = ev.to_dict()
    assert d["run_id"] == "r99"
    assert d["session_id"] == "s99"
    assert d["seq"] == 3
    assert d["type"] == "token.delta"
    assert d["payload"]["key"] == "value"
    assert d["payload"]["nested"] == {"a": 1}


def test_terminal_events_serialize_terminal_true():
    ct = _contract()
    ev = ct.make_event(
        run_id="r",
        session_id="s",
        seq=1,
        type="done",
        terminal=True,
    )
    assert ev.terminal is True
    assert ev.to_dict()["terminal"] is True


def test_runtime_status_serializes_mobile_reconnect_fields():
    ct = _contract()
    st = ct.make_status(
        run_id="r",
        session_id="s",
        status="running",
        last_event_id="r:7",
        last_seq=7,
    )
    d = st.to_dict()
    assert d["run_id"] == "r"
    assert d["session_id"] == "s"
    assert d["status"] == "running"
    assert d["last_event_id"] == "r:7"
    assert d["last_seq"] == 7
    assert d["terminal"] is False


def test_pending_approval_ids_serialize_correctly():
    ct = _contract()
    st = ct.make_status(
        run_id="r",
        session_id="s",
        status="awaiting_approval",
        pending_approval_ids=["appr-1", "appr-2"],
    )
    d = st.to_dict()
    assert d["status"] == "awaiting_approval"
    assert d["pending_approval_ids"] == ["appr-1", "appr-2"]


def test_pending_clarify_ids_serialize_correctly():
    ct = _contract()
    st = ct.make_status(
        run_id="r",
        session_id="s",
        status="awaiting_clarify",
        pending_clarify_ids=["clar-1"],
    )
    d = st.to_dict()
    assert d["status"] == "awaiting_clarify"
    assert d["pending_clarify_ids"] == ["clar-1"]


def test_controls_serialize_correctly():
    ct = _contract()
    st = ct.make_status(
        run_id="r",
        session_id="s",
        status="running",
        controls=["cancel", "queue"],
    )
    d = st.to_dict()
    assert d["controls"] == ["cancel", "queue"]


def test_event_type_validation():
    ct = _contract()
    assert ct.is_valid_event_type("run.started") is True
    assert ct.is_valid_event_type("token.delta") is True
    assert ct.is_valid_event_type("done") is True
    assert ct.is_valid_event_type("not.a.valid.type") is False
    assert ct.is_valid_event_type("") is False
    assert ct.is_valid_event_type("  ") is False


def test_status_validation():
    ct = _contract()
    assert ct.is_valid_status("running") is True
    assert ct.is_valid_status("completed") is True
    assert ct.is_valid_status("cancelled") is True
    assert ct.is_valid_status("invalid-status") is False
    assert ct.is_valid_status("") is False


def test_runtime_contract_imports_cleanly_without_api_streaming():
    runtime = importlib.import_module("api.runtime_contract")
    assert not hasattr(runtime, "streaming")
    mod_names = {name for name in dir(runtime) if not name.startswith("_")}
    assert "streaming" not in mod_names


def test_redaction_removes_secrets_from_payload():
    ct = _contract()
    ev = ct.make_event(
        run_id="r",
        session_id="s",
        seq=1,
        type="run.started",
        payload={
            "model": "claude-sonnet-4.6",
            "api_key": "sk-abc123",
            "token": "bearer-secret",
            "password": "p@ssw0rd",
            "nested": {"oauth_token": "xyz", "name": "keep"},
            "items": [{"secret": "shh"}, {"name": "keep"}],
        },
    )
    d = ev.to_dict()
    assert d["payload"]["api_key"] == "[REDACTED]"
    assert d["payload"]["token"] == "[REDACTED]"
    assert d["payload"]["password"] == "[REDACTED]"
    assert d["payload"]["model"] == "claude-sonnet-4.6"
    assert d["payload"]["nested"]["oauth_token"] == "[REDACTED]"
    assert d["payload"]["nested"]["name"] == "keep"
    assert d["payload"]["items"][0]["secret"] == "[REDACTED]"
    assert d["payload"]["items"][1]["name"] == "keep"


def test_status_error_redaction():
    ct = _contract()
    st = ct.make_status(
        run_id="r",
        session_id="s",
        error="Connection failed: api_key=sk-abc123",
    )
    d = st.to_dict()
    assert d["error"] == "[REDACTED]"


def test_status_error_not_redacted_for_safe_error():
    ct = _contract()
    st = ct.make_status(
        run_id="r",
        session_id="s",
        error="Agent thread died unexpectedly",
    )
    d = st.to_dict()
    assert d["error"] == "Agent thread died unexpectedly"


def test_make_event_uses_current_timestamp_by_default():
    import time

    ct = _contract()
    before = time.time()
    ev = ct.make_event(run_id="r", session_id="s", seq=1, type="run.started")
    after = time.time()
    assert before <= ev.created_at <= after


def test_make_event_accepts_explicit_timestamp():
    ct = _contract()
    ts = 1778750000.0
    ev = ct.make_event(run_id="r", session_id="s", seq=1, type="run.started", created_at=ts)
    assert ev.created_at == 1778750000.0
