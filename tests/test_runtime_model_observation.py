"""Observed Agent runtime, durable replay and HTTP reconnect projection."""
import json
import queue
import sys
import types
from unittest import mock

import pytest

from api.run_journal import RunJournalWriter, latest_run_summary, read_run_events


def observation(model="backup", *, session_id="session-a", stream_id="run-a"):
    return {"session_id": session_id, "stream_id": stream_id, "model": model,
            "provider": "custom", "fallback_active": True, "phase": "observed_output"}


def test_journal_runtime_is_owned_and_invalidated(tmp_path):
    writer = RunJournalWriter("session-a", "run-a", session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "answer"})
    first = writer.append_sse_event("runtime_model", observation())
    assert read_run_events("session-a", "run-a", session_dir=tmp_path)["events"][-1] == first
    summary = latest_run_summary("session-a", "run-a", session_dir=tmp_path)
    assert summary["runtime_model"] == observation()
    summary["runtime_model"]["model"] = "corrupted"
    assert latest_run_summary("session-a", "run-a", session_dir=tmp_path)["runtime_model"] == observation()
    writer.append_sse_event("warning", {"type": "fallback", "message": "Trying another route"})
    assert latest_run_summary("session-a", "run-a", session_dir=tmp_path)["runtime_model"] is None
    writer.append_sse_event("runtime_model", {**observation(), "stream_id": "foreign"})
    assert latest_run_summary("session-a", "run-a", session_dir=tmp_path)["runtime_model"] is None
    writer.append_sse_event("runtime_model", observation("recovered"))
    assert latest_run_summary("session-a", "run-a", session_dir=tmp_path)["runtime_model"]["model"] == "recovered"
    writer.append_sse_event("runtime_model", "malformed observation")
    assert latest_run_summary("session-a", "run-a", session_dir=tmp_path)["runtime_model"] is None
    writer.append_sse_event("runtime_model", observation("recovered-again"))
    assert latest_run_summary("session-a", "run-a", session_dir=tmp_path)["runtime_model"]["model"] == "recovered-again"
    assert latest_run_summary("session-a", "another-run", session_dir=tmp_path)["runtime_model"] is None


@pytest.mark.parametrize("malformed", ["bad observation", ["bad observation"], None])
def test_non_object_observation_invalidates_summary_and_http_snapshot(tmp_path, monkeypatch, malformed):
    from api import routes, run_journal
    writer = RunJournalWriter("session-a", "run-a", session_dir=tmp_path)
    writer.append_sse_event("runtime_model", observation("first"))
    writer.append_sse_event("token", malformed)
    assert latest_run_summary("session-a", "run-a", session_dir=tmp_path)["runtime_model"]["model"] == "first"
    writer.append_sse_event("runtime_model", malformed if malformed is not None else {"placeholder": True})
    if malformed is None:
        # The writer normalizes None to {}; exercise a legacy/raw null journal row.
        path = run_journal._run_path("session-a", "run-a", session_dir=tmp_path)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[-1]["payload"] = None
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    assert latest_run_summary("session-a", "run-a", session_dir=tmp_path)["runtime_model"] is None
    monkeypatch.setattr(routes, "find_run_summary", lambda rid: latest_run_summary("session-a", rid, session_dir=tmp_path))
    monkeypatch.setattr(routes, "read_run_events", lambda sid, rid: read_run_events(sid, rid, session_dir=tmp_path))
    snapshot = routes._run_journal_live_snapshot("run-a")
    assert snapshot["runtime_model"] is None
    assert routes._runtime_journal_snapshot_for_session_payload(snapshot)["runtime_model"] is None


@pytest.mark.parametrize("suffix,expected", [("", "backup"), ("warning", None), ("foreign", None)])
def test_http_snapshot_projects_exact_journal_window(tmp_path, monkeypatch, suffix, expected):
    import api.routes as routes
    writer = RunJournalWriter("session-a", "run-a", session_dir=tmp_path)
    writer.append_sse_event("runtime_model", observation())
    writer.append_sse_event("token", {"text": "answer"})
    if suffix == "warning":
        writer.append_sse_event("warning", {"type": "fallback"})
    elif suffix == "foreign":
        writer.append_sse_event("runtime_model", {**observation(), "session_id": "foreign"})
    monkeypatch.setattr(routes, "find_run_summary", lambda rid: latest_run_summary("session-a", rid, session_dir=tmp_path))
    monkeypatch.setattr(routes, "read_run_events", lambda sid, rid: read_run_events(sid, rid, session_dir=tmp_path))
    snapshot = routes._run_journal_live_snapshot("run-a")
    assert snapshot["last_assistant_text"] == "answer"
    value = snapshot["runtime_model"]
    assert (value["model"] if value else None) == expected
    assert routes._runtime_journal_snapshot_for_session_payload(snapshot)["runtime_model"] == value


def run_worker(steps, *, fail=False, ephemeral=False, tmp_path=None, monkeypatch=None):
    """Drive the production local worker; fake only external Agent/provider calls."""
    from api import streaming, run_journal
    from api.config import SESSION_AGENT_CACHE
    SESSION_AGENT_CACHE.clear()
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: tmp_path)
    class Session:
        session_id = "session-a"
        title = "Session"
        workspace = "/tmp"
        model = "primary"
        model_provider = "anthropic"
        profile = None
        personality = None
        messages = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "old answer"}]
        context_messages = messages
        input_tokens = output_tokens = cache_read_tokens = cache_write_tokens = 0
        estimated_cost = 0.0
        tool_calls = []
        gateway_routing = None
        gateway_routing_history = []
        active_stream_id = "run-a"
        pending_user_message = None
        pending_attachments = []
        pending_started_at = None
        context_length = threshold_tokens = last_prompt_tokens = 0
        llm_title_generated = True
        def save(self, *args, **kwargs): pass
        def compact(self): return {"session_id": self.session_id}

    class Agent:
        def __init__(self, model=None, provider=None, session_id=None,
                     stream_delta_callback=None, reasoning_callback=None,
                     status_callback=None, **kwargs):
            self.model, self.provider, self.session_id = model, provider, session_id
            self.stream_delta_callback = stream_delta_callback
            self.reasoning_callback = reasoning_callback
            self.status_callback = status_callback
            self._provider_fallback_active = False
            self.context_compressor = None
            self.session_prompt_tokens = self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = self.session_cache_write_tokens = 0
            self.reasoning_config = self.ephemeral_system_prompt = self._last_error = None
        def run_conversation(self, **kwargs):
            for model, provider, fallback, channel, text in steps:
                self.model, self.provider, self._provider_fallback_active = model, provider, fallback
                if channel == "token": self.stream_delta_callback(text)
                elif channel == "reasoning": self.reasoning_callback(text)
                elif channel == "status": self.status_callback("lifecycle", text)
            if fail: return {"error": "provider unavailable", "messages": []}
            return {"messages": kwargs.get("conversation_history", []) + [
                {"role": "user", "content": kwargs["persist_user_message"]},
                {"role": "assistant", "content": "served answer"}]}
        def interrupt(self, message): pass

    runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    runtime_module.resolve_runtime_provider = mock.Mock(return_value={
        "provider": "anthropic", "base_url": None, "api_key": "sk-test",
        "api_mode": "chat_completions", "command": None, "args": [], "credential_pool": None})
    cli_module = types.ModuleType("hermes_cli")
    cli_module.runtime_provider = runtime_module
    state_module = types.ModuleType("hermes_state")
    state_module.SessionDB = mock.Mock(return_value=None)
    injected = {"hermes_cli": cli_module, "hermes_cli.runtime_provider": runtime_module,
                "hermes_state": state_module}
    sentinel = object()
    saved = {name: sys.modules.get(name, sentinel) for name in injected}
    sys.modules.update(injected)
    q = queue.Queue()
    s = Session()
    try:
        with mock.patch.object(streaming, "get_session", return_value=s), \
             mock.patch.object(streaming, "_get_ai_agent", return_value=Agent), \
             mock.patch.object(streaming, "resolve_model_provider", return_value=("primary", "anthropic", None)), \
             mock.patch("api.config.get_config", return_value={}), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
            streaming.STREAMS["run-a"] = q
            streaming._run_agent_streaming("session-a", "new turn", "primary", "/tmp", "run-a", ephemeral=ephemeral)
    finally:
        for name, original in saved.items():
            if original is sentinel: sys.modules.pop(name, None)
            else: sys.modules[name] = original
        streaming.STREAMS.pop("run-a", None)
        SESSION_AGENT_CACHE.clear()
    return s, list(q.queue)


def test_worker_observes_serving_runtime_not_requested(tmp_path, monkeypatch):
    s, events = run_worker([
        ("primary", "anthropic", False, "reasoning", "thinking"),
        ("backup", "custom", True, "token", "answer"),
        ("backup", "custom", True, "status", "Switched to fallback: backup (custom)"),
    ], tmp_path=tmp_path, monkeypatch=monkeypatch)
    names = [name for name, _ in events]
    assert names.count("runtime_model") == 3  # primary, backup, post-warning correction
    observed = [value for name, value in events if name == "runtime_model"]
    assert [value["model"] for value in observed] == ["primary", "backup", "backup"]
    assert observed[-1] == observation()
    assert names.index("runtime_model") < names.index("reasoning")
    assert names.index("warning") < len(names) - 1 - names[::-1].index("runtime_model")
    assert latest_run_summary("session-a", "run-a", session_dir=tmp_path)["runtime_model"] == observation()
    assert s.model == "primary"  # selection is not overwritten


def test_worker_does_not_observe_attempt_or_missing_model(tmp_path, monkeypatch):
    _, events = run_worker([
        ("backup", "custom", True, "status", "Trying fallback backup"),
        (None, "custom", True, "token", "partial"),
    ], fail=True, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert not any(name == "runtime_model" for name, _ in events)


def test_missing_agent_model_does_not_stamp_requested_as_served(tmp_path, monkeypatch):
    s, events = run_worker([(None, "custom", True, "token", "partial")],
                           tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert not any(name == "runtime_model" for name, _ in events)
    assert "_usedModel" not in s.messages[-1]
    usage = next(value["usage"] for name, value in events if name == "done")
    assert not usage.get("used_model")


def test_worker_nonstreaming_success_observes_fallback(tmp_path, monkeypatch):
    _, events = run_worker([("backup", "custom", True, "route", "")],
                           tmp_path=tmp_path, monkeypatch=monkeypatch, ephemeral=True)
    assert [value["model"] for name, value in events if name == "runtime_model"] == ["backup"]
