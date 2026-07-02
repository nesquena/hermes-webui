import importlib
import uuid

import pytest


def _get_streaming_module():
    return importlib.import_module("api.streaming")


def _reset_mirror_state():
    mod = _get_streaming_module()
    mod._reset_runtime_journal_for_test()


def _make_fake_stream_id():
    return uuid.uuid4().hex


def test_default_mode_does_not_require_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
    _reset_mirror_state()
    mod = _get_streaming_module()
    assert not mod._runtime_adapter_enabled()


def test_journal_mirror_active_in_legacy_journal_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    _reset_mirror_state()
    mod = _get_streaming_module()
    assert mod._runtime_adapter_enabled()


def test_legacy_journal_mode_mirrors_run_started(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    _reset_mirror_state()
    mod = _get_streaming_module()
    j = mod._ensure_runtime_journal()
    stream_id = _make_fake_stream_id()
    session_id = "test_session_run_started"
    j.create_run(session_id=session_id, run_id=stream_id)
    from api.runtime_contract import make_event as _make

    mod._RT_MIRROR_SEQ.setdefault(stream_id, [0])
    mod._RT_MIRROR_SEQ[stream_id][0] += 1
    j.append_event(
        _make(
            run_id=stream_id,
            session_id=session_id,
            seq=mod._RT_MIRROR_SEQ[stream_id][0],
            type="run.started",
            terminal=False,
            payload={"model": "test", "workspace": "/tmp", "ephemeral": False},
        )
    )
    events = j.read_events(stream_id)
    assert events is not None
    assert len(events) >= 1
    assert events[0].type == "run.started"


def _create_and_mirror(mod, stream_id, session_id, sse_event, data):
    j = mod._ensure_runtime_journal()
    j.create_run(session_id=session_id, run_id=stream_id)
    mod._mirror_to_runtime_journal(stream_id, session_id, sse_event, data)
    return mod._ensure_runtime_journal()


def test_legacy_journal_mode_mirrors_token_delta(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    _reset_mirror_state()
    mod = _get_streaming_module()
    stream_id = _make_fake_stream_id()
    session_id = "test_session_token"
    j = _create_and_mirror(mod, stream_id, session_id, "token", {"text": "hello world"})
    events = j.read_events(stream_id)
    assert events is not None
    token_events = [e for e in events if e.type == "token.delta"]
    assert len(token_events) >= 1
    assert token_events[0].payload["text"] == "hello world"


def test_legacy_journal_mode_mirrors_tool_progress(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    _reset_mirror_state()
    mod = _get_streaming_module()
    stream_id = _make_fake_stream_id()
    session_id = "test_session_tool"
    j = _create_and_mirror(mod, stream_id, session_id, "tool", {"name": "bash", "preview": "ls"})
    events = j.read_events(stream_id)
    assert events is not None
    progress_events = [e for e in events if e.type == "progress"]
    assert len(progress_events) >= 1


def test_legacy_journal_mode_mirrors_tool_started(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    _reset_mirror_state()
    mod = _get_streaming_module()
    stream_id = _make_fake_stream_id()
    session_id = "test_session_tool_started"
    j = _create_and_mirror(
        mod,
        stream_id,
        session_id,
        "tool",
        {"event_type": "tool.started", "name": "write_file", "args": {"path": "/tmp/x"}},
    )
    events = j.read_events(stream_id)
    assert events is not None
    tool_started = [e for e in events if e.type == "tool.started"]
    assert len(tool_started) >= 1
    assert tool_started[0].payload["name"] == "write_file"


def test_legacy_journal_mode_mirrors_done_terminal(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    _reset_mirror_state()
    mod = _get_streaming_module()
    stream_id = _make_fake_stream_id()
    session_id = "test_session_done"
    j = mod._ensure_runtime_journal()
    j.create_run(session_id=session_id, run_id=stream_id)
    mod._mirror_to_runtime_journal(stream_id, session_id, "token", {"text": "partial"})
    mod._mirror_to_runtime_journal(stream_id, session_id, "done", {"session": {}, "usage": {}})
    events = j.read_events(stream_id)
    assert events is not None
    done_events = [e for e in events if e.type == "done"]
    assert len(done_events) >= 1
    assert done_events[0].terminal is True


def test_legacy_journal_mode_mirrors_error_terminal(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    _reset_mirror_state()
    mod = _get_streaming_module()
    stream_id = _make_fake_stream_id()
    session_id = "test_session_error"
    j = mod._ensure_runtime_journal()
    j.create_run(session_id=session_id, run_id=stream_id)
    mod._mirror_to_runtime_journal(
        stream_id, session_id, "apperror", {"message": "crash", "type": "interrupted"}
    )
    events = j.read_events(stream_id)
    assert events is not None
    error_events = [e for e in events if e.type == "error"]
    assert len(error_events) >= 1
    assert error_events[0].terminal is True


def test_legacy_journal_mode_reads_back_from_disk(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-journal")
    _reset_mirror_state()
    from api.runtime_journal import RuntimeJournal

    j1 = RuntimeJournal(base_dir=tmp_path / "runs")
    stream_id = _make_fake_stream_id()
    session_id = "test_session_disk"
    with monkeypatch.context() as m:
        m.setattr("api.streaming._runtime_journal_instance", j1)
        mod2 = _get_streaming_module()
        mod2._ensure_runtime_journal().create_run(session_id=session_id, run_id=stream_id)
        mod2._mirror_to_runtime_journal(stream_id, session_id, "done", {"session": {}})
    j2 = RuntimeJournal(base_dir=tmp_path / "runs")
    events = j2.read_events(stream_id)
    assert events is not None
    done_events = [e for e in events if e.type == "done"]
    assert len(done_events) >= 1


def test_default_mode_journal_not_required(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
    _reset_mirror_state()
    mod = _get_streaming_module()
    assert not mod._runtime_adapter_enabled()


def test_legacy_journal_direct_skips_mirroring(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
    _reset_mirror_state()
    mod = _get_streaming_module()
    assert not mod._runtime_adapter_enabled()
