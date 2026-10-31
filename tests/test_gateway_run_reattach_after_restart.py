"""A gateway-owned run must survive a WebUI restart instead of being marked interrupted."""
from collections import OrderedDict
import io
import json
import os
import threading
import urllib.error
from email.message import Message

import pytest

import api.gateway_chat as gateway_chat
import api.models as models
import api.streaming as streaming
from api.config import ACTIVE_RUNS, STREAMS, STREAMS_LOCK, create_stream_channel
from api import profiles
from api.models import new_session

_REAL_OPEN_EVENTS = gateway_chat._open_gateway_run_events


@pytest.fixture
def isolated_sessions(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_USE_RUNS_API", "1")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setattr(gateway_chat, "GATEWAY_REATTACH_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(gateway_chat, "_gateway_reasoning_effort_for_request", lambda *a, **k: None)
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {
        "status": "not_configured", "source": "none", "label": "", "message_count": 0, "messages": [],
    })
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])
    # Default: a gateway without event replay, so reattach falls back to status polling.
    monkeypatch.setattr(gateway_chat, "_open_gateway_run_events", _events_http_error(404))
    return session_dir


def _events_http_error(code):
    def fail(base_url, headers, run_id, last_seq=-1):
        raise urllib.error.HTTPError(f"{base_url}/v1/runs/{run_id}/events", code, "no", Message(), io.BytesIO(b""))
    return fail


def _orphaned_gateway_turn(run_id="run_survivor", stream_id="stream-before-restart"):
    """Persist the sidecar exactly as a WebUI process leaves it when killed mid-run."""
    s = new_session()
    s.messages = [
        {"role": "user", "content": "earlier", "timestamp": 0.5},
        {"role": "assistant", "content": "earlier reply", "timestamp": 0.6},
    ]
    s.active_stream_id = stream_id
    s.pending_user_message = "long task"
    s.pending_attachments = []
    s.pending_started_at = 1.0
    s.pending_user_source = "webui"
    s.gateway_run = {"run_id": run_id, "stream_id": stream_id, "regeneration": False, "goal_related": False}
    s.save()
    # Simulate the new process: nothing of the old run is in memory.
    models.SESSIONS.clear()
    with STREAMS_LOCK:
        STREAMS.pop(stream_id, None)
    ACTIVE_RUNS.pop(stream_id, None)
    return s.session_id, stream_id


def _saved(session_id):
    return json.loads((models.SESSION_DIR / f"{session_id}.json").read_text())


def _wait_for_reattach_threads(timeout=10.0):
    for thread in threading.enumerate():
        if thread.name.startswith("gateway-reattach-"):
            thread.join(timeout)
            assert not thread.is_alive(), "reattach worker did not settle"


def test_runs_api_start_sends_idempotency_key_and_persists_run_id(isolated_sessions, monkeypatch):
    s = new_session()
    stream_id = "stream-live"
    s.active_stream_id = stream_id
    s.pending_user_message = "hi"
    s.pending_attachments = []
    s.pending_started_at = 123.0
    s.save()
    captured = {}

    def fake_urlopen(req, timeout=None):
        if req.get_method() == "POST":
            captured["post_headers"] = dict(req.header_items())
            return io.BytesIO(b'{"run_id":"run_live"}')
        # The run id is durable before the first event is relayed.
        captured["persisted_at_events"] = _saved(s.session_id).get("gateway_run")
        return io.BytesIO(
            b'data: {"event":"message.delta","delta":"done"}\n'
            b'data: {"event":"run.completed","output":"done"}\n'
            b"data: [DONE]\n"
        )

    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *a, **k: True)
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gateway_chat, "_open_gateway_run_events", _REAL_OPEN_EVENTS)
    with STREAMS_LOCK:
        STREAMS[stream_id] = create_stream_channel()

    gateway_chat._run_gateway_chat_streaming(s.session_id, "hi", "test-model", "/tmp", stream_id, [])

    assert captured["post_headers"]["Idempotency-key"] == f"webui-{stream_id}"
    # Only ids and flags are persisted, never a credential.
    assert captured["persisted_at_events"] == {
        "run_id": "run_live", "stream_id": stream_id, "regeneration": False, "goal_related": False,
        "endpoint_routing": "profile-v1", "base_url": "http://gateway.local",
    }
    saved = _saved(s.session_id)
    assert saved["gateway_run"] is None
    assert saved["active_stream_id"] is None
    assert saved["messages"][-1]["content"] == "done"


def test_live_send_keeps_relaying_after_initial_replay_truncated(isolated_sessions, monkeypatch):
    s = new_session()
    stream_id = "stream-live-trunc"
    s.active_stream_id = stream_id
    s.pending_user_message = "hi"
    s.pending_attachments = []
    s.pending_started_at = 123.0
    s.save()

    def fake_urlopen(req, timeout=None):
        if req.get_method() == "POST":
            return io.BytesIO(b'{"run_id":"run_live_trunc"}')
        return io.BytesIO(
            b'data: {"event":"replay.truncated","oldest_retained_seq":1200}\n\n'
            b'data: {"event":"message.delta","delta":"kept answer","seq":1200}\n\n'
            b'data: {"event":"run.completed","output":"kept answer","seq":1201}\n\n'
            b"data: [DONE]\n\n"
        )

    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *a, **k: True)
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gateway_chat, "_open_gateway_run_events", _REAL_OPEN_EVENTS)
    monkeypatch.setattr(
        gateway_chat, "_get_gateway_run_status",
        lambda *a: pytest.fail("live send must not fall back to status polling"),
    )
    with STREAMS_LOCK:
        STREAMS[stream_id] = create_stream_channel()

    gateway_chat._run_gateway_chat_streaming(s.session_id, "hi", "test-model", "/tmp", stream_id, [])

    saved = _saved(s.session_id)
    assert saved["active_stream_id"] is None
    assert saved["messages"][-1]["role"] == "assistant"
    assert saved["messages"][-1]["content"] == "kept answer"


def test_restart_reattaches_and_writes_back_real_answer(isolated_sessions, monkeypatch):
    sid, stream_id = _orphaned_gateway_turn()
    release = threading.Event()
    polls = []

    def fake_status(base_url, api_key, run_id):
        polls.append(run_id)
        if not release.is_set():
            return {"run_id": run_id, "status": "running"}
        return {
            "run_id": run_id,
            "status": "completed",
            "output": "finished after the restart",
            "usage": {"input_tokens": 7, "output_tokens": 3},
        }

    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", fake_status)

    assert gateway_chat.resume_gateway_runs_after_restart() == [sid]
    # While the run is still going, the turn is live: no stale-pending repair, reconnect works.
    assert stream_id in STREAMS
    assert gateway_chat.wait_for_gateway_run_id(stream_id, 5.0) == (True, "run_survivor")
    live = models.get_session(sid)
    assert live.active_stream_id == stream_id
    assert not any(m.get("_error") for m in live.messages)

    release.set()
    _wait_for_reattach_threads()

    saved = _saved(sid)
    assert [m["role"] for m in saved["messages"]] == ["user", "assistant", "user", "assistant"]
    assert saved["messages"][2]["content"] == "long task"
    assert saved["messages"][3]["content"] == "finished after the restart"
    assert not any(m.get("_error") for m in saved["messages"])
    assert saved["active_stream_id"] is None
    assert saved["pending_user_message"] is None
    assert saved["gateway_run"] is None
    assert set(polls) == {"run_survivor"}
    assert stream_id not in STREAMS


def test_reattach_surfaces_pending_approval_once(isolated_sessions, monkeypatch):
    sid, stream_id = _orphaned_gateway_turn(run_id="run_parked")
    relayed = []
    calls = {"n": 0}

    def fake_status(base_url, api_key, run_id):
        calls["n"] += 1
        if calls["n"] < 4:
            return {"run_id": run_id, "status": "waiting_for_approval", "approval": {
                "event": "approval.request", "approval_id": "appr-1", "command": "rm -rf build", "description": "delete",
            }}
        return {"run_id": run_id, "status": "completed", "output": "approved and done"}

    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", fake_status)
    monkeypatch.setattr(
        gateway_chat, "_relay_gateway_run_approval",
        lambda session_id, run_id, payload, *a, **k: relayed.append((session_id, run_id, payload["approval_id"])),
    )
    gateway_chat.resume_gateway_runs_after_restart()
    _wait_for_reattach_threads()

    assert relayed == [(sid, "run_parked", "appr-1")]
    assert _saved(sid)["messages"][-1]["content"] == "approved and done"


def test_nothing_to_reattach_is_left_to_stale_pending_repair(isolated_sessions, monkeypatch):
    sid, stream_id = _orphaned_gateway_turn()
    s = models.Session.load(sid)
    s.gateway_run = None
    s.save()
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", lambda *a, **k: pytest.fail("must not poll"))
    assert gateway_chat.resume_gateway_runs_after_restart() == []
    assert stream_id not in STREAMS


def test_cancel_still_stops_a_reattached_run(isolated_sessions, monkeypatch):
    sid, stream_id = _orphaned_gateway_turn(run_id="run_to_stop")
    stopped = []
    monkeypatch.setattr(
        gateway_chat, "_get_gateway_run_status",
        lambda base_url, api_key, run_id: {"run_id": run_id, "status": "running"},
    )
    monkeypatch.setattr(gateway_chat, "stop_gateway_run", lambda run_id: stopped.append(run_id) or True)
    gateway_chat.resume_gateway_runs_after_restart()

    # Same two steps /api/chat/cancel performs for a gateway-backed stream.
    _structured, run_id = gateway_chat.wait_for_gateway_run_id(stream_id, 5.0)
    assert gateway_chat.stop_gateway_run(run_id)
    for _ in range(500):
        if stream_id in gateway_chat.CANCEL_FLAGS:
            break
        threading.Event().wait(0.01)
    assert streaming.cancel_stream(stream_id)
    _wait_for_reattach_threads()

    assert stopped == ["run_to_stop"]
    saved = _saved(sid)
    assert saved["active_stream_id"] is None
    assert saved["gateway_run"] is None
    assert not any(m.get("content") == "long task" and m.get("role") == "assistant" for m in saved["messages"])


def _poll_until_completed(monkeypatch):
    seen = []

    def fake_status(base_url, api_key, run_id):
        seen.append((base_url, api_key, run_id))
        return {"run_id": run_id, "status": "completed", "output": "answer"}

    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", fake_status)
    return seen


@pytest.mark.parametrize("index_state", ["missing", "stale", "corrupt"])
def test_reattach_does_not_depend_on_the_session_index(isolated_sessions, monkeypatch, index_state):
    sid, _stream_id = _orphaned_gateway_turn()
    index = isolated_sessions / "_index.json"
    if index_state == "missing":
        index.unlink(missing_ok=True)
    elif index_state == "stale":
        # Sidecar saved, index update lost: the row still shows no active stream.
        index.write_text(json.dumps([{"session_id": sid, "active_stream_id": None}]))
    else:
        index.write_text("{not json")
    _poll_until_completed(monkeypatch)

    assert gateway_chat.resume_gateway_runs_after_restart() == [sid]
    _wait_for_reattach_threads()

    saved = _saved(sid)
    assert saved["messages"][-1]["content"] == "answer"
    assert saved["active_stream_id"] is None


def test_reattach_skips_idle_sidecars_without_parsing_them(isolated_sessions, monkeypatch):
    idle = new_session()
    idle.messages = [{"role": "user", "content": "x", "timestamp": 1.0}]
    idle.save()
    sid, _ = _orphaned_gateway_turn()
    loaded = []
    real_load = models.Session.load_metadata_only
    monkeypatch.setattr(
        models.Session, "load_metadata_only",
        classmethod(lambda cls, s, **kw: loaded.append(s) or real_load.__func__(cls, s, **kw)),
    )
    _poll_until_completed(monkeypatch)

    assert gateway_chat.resume_gateway_runs_after_restart() == [sid]
    _wait_for_reattach_threads()
    assert idle.session_id not in loaded
    assert sid in loaded


@pytest.mark.parametrize("default_backend", ["gateway", "local"])
@pytest.mark.parametrize("session_profile, process_profile", [("default", "work"), ("work", "default")])
def test_reattach_resolves_gateway_from_the_session_profile(
    isolated_sessions, tmp_path, monkeypatch, default_backend, session_profile, process_profile,
):
    """Poll the session profile's own gateway, root included, whatever profile the process restarted under."""
    root = tmp_path / "hermes"
    for name, home in (("default", root), ("work", root / "profiles" / "work")):
        home.mkdir(parents=True, exist_ok=True)
        (home / ".env").write_text(
            f"HERMES_WEBUI_GATEWAY_BASE_URL=http://{name}-gateway:8642\nHERMES_WEBUI_GATEWAY_API_KEY={name}-key\n"
        )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_active_profile", process_profile)
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set())
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_BASE_URL")
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_API_KEY", raising=False)
    profiles._reload_dotenv(root if process_profile == "default" else root / "profiles" / "work")
    if default_backend == "local":
        monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND")
        monkeypatch.delenv("HERMES_WEBUI_GATEWAY_USE_RUNS_API")
    sid, stream_id = _orphaned_gateway_turn()
    s = models.Session.load(sid)
    s.profile = session_profile
    s.save(touch_updated_at=False)
    models.SESSIONS.clear()
    seen = _poll_until_completed(monkeypatch)

    assert os.environ["HERMES_WEBUI_GATEWAY_API_KEY"] == f"{process_profile}-key"
    assert gateway_chat.resume_gateway_runs_after_restart() == [sid]
    _wait_for_reattach_threads()

    assert {(b, k, r) for b, k, r in seen} == {
        (f"http://{session_profile}-gateway:8642", f"{session_profile}-key", "run_survivor"),
    }
    saved = _saved(sid)
    assert saved["messages"][-1]["content"] == "answer"
    assert saved["active_stream_id"] is None and saved["gateway_run"] is None


def test_crash_after_admission_before_run_id_saved_replays_the_same_admission(isolated_sessions, monkeypatch):
    s = new_session()
    stream_id = "stream-admitted"
    s.active_stream_id = stream_id
    s.pending_user_message = "long task"
    s.pending_attachments = []
    s.pending_started_at = 1.0
    s.save()
    posts = []

    class Crash(BaseException):
        pass

    def fake_urlopen(req, timeout=None):
        assert req.get_method() == "POST"
        posts.append((req.get_header("Idempotency-key"), req.data))
        return io.BytesIO(b'{"run_id":"run_original","replayed":%s}' % (b"true" if len(posts) > 1 else b"false"))

    real_record = gateway_chat._record_gateway_run

    def record_then_die(session_id, stream_id, run_id, **kw):
        if run_id:
            raise Crash  # process killed after the POST response, before the id is saved
        real_record(session_id, stream_id, run_id, **kw)

    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *a, **k: True)
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gateway_chat, "_record_gateway_run", record_then_die)
    with STREAMS_LOCK:
        STREAMS[stream_id] = create_stream_channel()
    with pytest.raises(Crash):
        gateway_chat._run_gateway_runs_api_streaming(
            s.session_id, "long task", "test-model", "/tmp", stream_id, "http://gateway.local", "", [], {},
            put_gateway_event=lambda *a: None, cancel_event=threading.Event(), session=s,
            on_run_id=lambda run_id, **kw: gateway_chat._record_gateway_run(s.session_id, stream_id, run_id, **kw),
        )
    monkeypatch.setattr(gateway_chat, "_record_gateway_run", real_record)
    models.SESSIONS.clear()
    with STREAMS_LOCK:
        STREAMS.pop(stream_id, None)
    ACTIVE_RUNS.pop(stream_id, None)
    assert _saved(s.session_id)["gateway_run"]["run_id"] == ""
    seen = _poll_until_completed(monkeypatch)

    assert gateway_chat.resume_gateway_runs_after_restart() == [s.session_id]
    _wait_for_reattach_threads()

    # Replayed with the same key and byte-identical body, so the gateway returns the original run.
    assert len(posts) == 2 and posts[0] == posts[1] == (f"webui-{stream_id}", posts[0][1])
    assert {r for _b, _k, r in seen} == {"run_original"}
    saved = _saved(s.session_id)
    assert saved["messages"][-1]["content"] == "answer"
    assert not any(m.get("_error") for m in saved["messages"])
    assert saved["active_stream_id"] is None and saved["gateway_run"] is None


@pytest.mark.parametrize("code, message", [
    (404, "could not be recovered after the WebUI restart"),
    (401, "HTTP 401"),
    (403, "HTTP 403"),
])
def test_reattach_http_rejection_settles_the_turn_with_an_error(isolated_sessions, monkeypatch, code, message):
    sid, _stream_id = _orphaned_gateway_turn()
    calls = []

    def rejected(base_url, api_key, run_id):
        calls.append(run_id)
        raise urllib.error.HTTPError("http://gateway.local/v1/runs/x", code, "denied", Message(), io.BytesIO(b""))

    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", rejected)
    assert gateway_chat.resume_gateway_runs_after_restart() == [sid]
    _wait_for_reattach_threads()

    assert len(calls) == 1
    saved = _saved(sid)
    assert saved["messages"][-2]["content"] == "long task"
    assert saved["messages"][-1]["_error"] is True
    assert message in saved["messages"][-1]["content"]
    assert saved["active_stream_id"] is None and saved["gateway_run"] is None


def test_failed_recovery_checkpoint_never_admits_the_run(isolated_sessions, monkeypatch):
    s = new_session()
    stream_id = "stream-unsaved"
    s.active_stream_id = stream_id
    s.pending_user_message = "hi"
    s.pending_attachments = []
    s.pending_started_at = 1.0
    s.save()
    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *a, **k: True)
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", lambda *a, **k: pytest.fail("admitted without a checkpoint"))
    monkeypatch.setattr(models.Session, "save", lambda self, **kw: (_ for _ in ()).throw(OSError("disk full")))
    with STREAMS_LOCK:
        STREAMS[stream_id] = create_stream_channel()

    gateway_chat._run_gateway_chat_streaming(s.session_id, "hi", "test-model", "/tmp", stream_id, [])
    assert stream_id not in STREAMS


@pytest.mark.parametrize("stopped_by", ["user", "gateway"])
def test_cancelled_replayed_admission_keeps_the_prompt_and_is_not_reattached(isolated_sessions, monkeypatch, stopped_by):
    sid, stream_id = _orphaned_gateway_turn(run_id="")
    s = models.Session.load(sid)
    s.gateway_run["request"] = {"input": "long task"}
    s.save(touch_updated_at=False)
    models.SESSIONS.clear()
    monkeypatch.setattr(gateway_chat, "_admit_gateway_run", lambda *a, **k: "run_replayed")
    state = "running" if stopped_by == "user" else "cancelled"
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", lambda b, k, r: {"run_id": r, "status": state})
    monkeypatch.setattr(gateway_chat, "stop_gateway_run", lambda run_id: True)

    assert gateway_chat.resume_gateway_runs_after_restart() == [sid]
    if stopped_by == "user":
        assert gateway_chat.wait_for_gateway_run_id(stream_id, 5.0) == (True, "run_replayed")
        assert streaming.cancel_stream(stream_id)
    _wait_for_reattach_threads()

    saved = _saved(sid)
    assert [(m["role"], m["content"]) for m in saved["messages"][:3]] == [
        ("user", "earlier"), ("assistant", "earlier reply"), ("user", "long task"),
    ]
    assert len(saved["messages"]) == 4 and saved["messages"][3]["_error"] is True
    assert "Task cancelled" in saved["messages"][3]["content"]
    assert saved["active_stream_id"] is None and saved["gateway_run"] is None
    assert saved["pending_user_message"] is None
    models.SESSIONS.clear()
    assert gateway_chat.resume_gateway_runs_after_restart() == []


def _two_profile_gateways(tmp_path, monkeypatch, process_profile):
    root = tmp_path / "hermes"
    for name, home in (("default", root), ("work", root / "profiles" / "work")):
        home.mkdir(parents=True, exist_ok=True)
        (home / ".env").write_text(
            f"HERMES_WEBUI_GATEWAY_BASE_URL=http://{name}-gateway:8642\nHERMES_WEBUI_GATEWAY_API_KEY={name}-key\n"
        )
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_active_profile", process_profile)
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set())
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_BASE_URL")
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_API_KEY", raising=False)
    profiles._reload_dotenv(root if process_profile == "default" else root / "profiles" / "work")


def _reattach_under_profile(session_profile, run_id):
    sid, stream_id = _orphaned_gateway_turn(run_id=run_id)
    s = models.Session.load(sid)
    s.profile = session_profile
    s.save(touch_updated_at=False)
    models.SESSIONS.clear()
    assert gateway_chat.resume_gateway_runs_after_restart() == [sid]
    assert gateway_chat.wait_for_gateway_run_id(stream_id, 5.0) == (True, run_id)
    return sid, stream_id


@pytest.mark.parametrize("session_profile, process_profile", [("default", "work"), ("work", "default")])
def test_stop_reaches_the_gateway_that_owns_a_reattached_run(
    isolated_sessions, tmp_path, monkeypatch, session_profile, process_profile,
):
    _two_profile_gateways(tmp_path, monkeypatch, process_profile)
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", lambda b, k, r: {"run_id": r, "status": "running"})
    sent = []

    class _Resp(io.BytesIO):
        status = 200

    class _Opener:
        def open(self, req, timeout=None):
            sent.append((req.full_url, req.get_header("Authorization")))
            resp = _Resp(b"{}")
            resp.geturl = lambda: req.full_url
            return resp

    monkeypatch.setattr(gateway_chat.urllib.request, "build_opener", lambda *a: _Opener())
    _sid, stream_id = _reattach_under_profile(session_profile, "run_to_stop")

    assert gateway_chat.stop_gateway_run("run_to_stop")
    assert streaming.cancel_stream(stream_id)
    _wait_for_reattach_threads()

    assert sent == [
        (f"http://{session_profile}-gateway:8642/v1/runs/run_to_stop/stop", f"Bearer {session_profile}-key"),
    ]
    assert stream_id not in gateway_chat._STREAM_ENDPOINTS


@pytest.mark.parametrize("session_profile, process_profile", [("default", "work"), ("work", "default")])
def test_approval_reply_reaches_the_gateway_that_owns_a_reattached_run(
    isolated_sessions, tmp_path, monkeypatch, session_profile, process_profile,
):
    from unittest.mock import MagicMock

    import api.route_approvals as approvals
    import api.runner_client as runner_client
    from api import config, routes

    _two_profile_gateways(tmp_path, monkeypatch, process_profile)
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", lambda b, k, r: {
        "run_id": r, "status": "waiting_for_approval",
        "approval": {"event": "approval.request", "approval_id": "appr-1", "command": "rm -rf build", "description": "d"},
    })
    monkeypatch.setattr(config, "gateway_supports_approval_identity_v1", lambda *a, **k: True)
    replies = []

    def fake_respond(self, run_id, approval_id, choice):
        replies.append((self.base_url, self.api_key, run_id, approval_id, choice))
        return {"ok": True}

    monkeypatch.setattr(runner_client.HttpRunnerClient, "respond_approval", fake_respond)
    sid, stream_id = _reattach_under_profile(session_profile, "run_parked")
    try:
        for _ in range(500):
            if approvals.gateway_pending_mirror(sid, approval_id="appr-1", run_id="run_parked"):
                break
            threading.Event().wait(0.01)
        handler = MagicMock()
        handler.wfile = io.BytesIO()
        routes._handle_approval_respond(handler, {"session_id": sid, "choice": "once", "approval_id": "appr-1"})

        handler.send_response.assert_called_with(200)
        assert replies == [(f"http://{session_profile}-gateway:8642", f"{session_profile}-key", "run_parked", "appr-1", "once")]
    finally:
        streaming.cancel_stream(stream_id)
        _wait_for_reattach_threads()
        approvals._pending.pop(sid, None)


def _sse(*payloads):
    frames = []
    for seq, payload in payloads:
        frames.append(f"id: {seq}\n".encode() if seq is not None else b"")
        body = dict(payload, **({"seq": seq} if seq is not None else {}))
        frames.append(f"data: {json.dumps(body)}\n\n".encode())
    return io.BytesIO(b"".join(frames))


def _journal(sid, stream_id):
    from api.run_journal import read_run_events
    return read_run_events(sid, stream_id)["events"]


def _relayed_before_restart(sid, stream_id, rows):
    """Journal rows exactly as the pre-restart worker relayed them."""
    from api.run_journal import append_run_event
    for event, payload in rows:
        append_run_event(sid, stream_id, event, payload)


def test_reattach_streams_from_the_journal_cursor_and_keeps_reasoning_and_tools(isolated_sessions, monkeypatch):
    sid, stream_id = _orphaned_gateway_turn(run_id="run_live_after_restart")
    _relayed_before_restart(sid, stream_id, [
        ("context_status", {"session_id": sid}),
        ("reasoning", {"text": "plan. ", "gateway_seq": 0}),
        ("tool", {"name": "terminal", "args": {"command": "ls"}, "tid": "t1", "gateway_seq": 1}),
        ("tool_complete", {"name": "terminal", "tid": "t1", "is_error": False, "gateway_seq": 2}),
        ("token", {"text": "Before ", "gateway_seq": 3}),
    ])
    opened = []

    def open_events(base_url, headers, run_id, last_seq=-1):
        opened.append((run_id, last_seq))
        return _sse(
            (4, {"event": "reasoning.available", "text": "then check."}),
            (5, {"event": "tool.started", "tool": "read_file", "toolCallId": "t2", "args": {"path": "a"}}),
            (6, {"event": "tool.completed", "tool": "read_file", "toolCallId": "t2"}),
            (7, {"event": "message.delta", "delta": "after."}),
            (8, {"event": "run.completed", "output": "Before after.", "usage": {"input_tokens": 5}}),
        )

    monkeypatch.setattr(gateway_chat, "_open_gateway_run_events", open_events)
    probes = []
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", lambda b, k, r: probes.append(r) or {"run_id": r, "status": "running"})

    assert gateway_chat.resume_gateway_runs_after_restart() == [sid]
    _wait_for_reattach_threads()

    assert opened == [("run_live_after_restart", 3)]
    assert probes == ["run_live_after_restart"]  # one parked-approval probe, then no polling
    relayed = [(e["event"], e["payload"].get("gateway_seq")) for e in _journal(sid, stream_id)]
    # Reopened tabs replay these from the WebUI journal: live text, tool cards and reasoning after the restart.
    assert [r for r in relayed if r[1] is not None and r[1] >= 4] == [
        ("reasoning", 4), ("tool", 5), ("tool_complete", 6), ("token", 7),
    ]
    assert relayed[-2:] == [("done", None), ("stream_end", None)]
    saved = _saved(sid)
    final = saved["messages"][-1]
    assert final["content"] == "Before after."
    assert final["reasoning"] == "plan. then check."
    assert saved["active_stream_id"] is None and saved["gateway_run"] is None


def test_reattach_reconnects_after_the_event_stream_drops(isolated_sessions, monkeypatch):
    sid, stream_id = _orphaned_gateway_turn(run_id="run_flaky")
    opened = []
    streams = iter([
        _sse((0, {"event": "message.delta", "delta": "one "})),  # EOF before the run ends
        _sse((1, {"event": "message.delta", "delta": "two"}), (2, {"event": "run.completed", "output": "one two"})),
    ])

    def open_events(base_url, headers, run_id, last_seq=-1):
        opened.append(last_seq)
        return next(streams)

    monkeypatch.setattr(gateway_chat, "_open_gateway_run_events", open_events)
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", lambda b, k, r: {"run_id": r, "status": "running"})
    gateway_chat.resume_gateway_runs_after_restart()
    _wait_for_reattach_threads()

    assert opened == [-1, 0]
    assert _saved(sid)["messages"][-1]["content"] == "one two"


@pytest.mark.parametrize("why", ["truncated", "unaligned_journal"])
def test_reattach_falls_back_to_status_polling(isolated_sessions, monkeypatch, why):
    sid, stream_id = _orphaned_gateway_turn(run_id="run_fallback")
    opened = []
    if why == "unaligned_journal":
        # Relayed by a WebUI that did not record gateway seqs: no safe cursor exists.
        _relayed_before_restart(sid, stream_id, [("token", {"text": "partial"})])

    def open_events(base_url, headers, run_id, last_seq=-1):
        opened.append(last_seq)
        return _sse((None, {"event": "replay.truncated", "oldest_retained_seq": 40}), (40, {"event": "message.delta", "delta": "x"}))

    monkeypatch.setattr(gateway_chat, "_open_gateway_run_events", open_events)
    statuses = iter(["running"])  # the pre-stream probe sees the run still going
    monkeypatch.setattr(
        gateway_chat, "_get_gateway_run_status",
        lambda b, k, r: {"run_id": r, "status": next(statuses, "completed"), "output": "full answer from status"},
    )
    gateway_chat.resume_gateway_runs_after_restart()
    _wait_for_reattach_threads()

    assert opened == ([-1] if why == "truncated" else [])
    assert _saved(sid)["messages"][-1]["content"] == "full answer from status"
    assert not any(e["event"] == "token" and e["payload"].get("text") == "x" for e in _journal(sid, stream_id))


def test_reattach_polls_without_replay_when_the_journal_read_fails(isolated_sessions, monkeypatch):
    from api import run_journal
    sid, stream_id = _orphaned_gateway_turn(run_id="run_journal_unreadable")
    _relayed_before_restart(sid, stream_id, [("token", {"text": "A", "gateway_seq": 0})])
    real_read, failed = run_journal.read_run_events, []

    def read_once_broken(*args, **kwargs):
        if not failed:
            failed.append(True)
            raise OSError("journal unreadable")
        return real_read(*args, **kwargs)

    monkeypatch.setattr(run_journal, "read_run_events", read_once_broken)
    opened = []

    def open_events(base_url, headers, run_id, last_seq=-1):
        opened.append(last_seq)
        return _sse((0, {"event": "message.delta", "delta": "A"}), (1, {"event": "message.delta", "delta": "B"}))

    monkeypatch.setattr(gateway_chat, "_open_gateway_run_events", open_events)
    statuses = iter(["running"])  # the pre-stream probe sees the run still going
    monkeypatch.setattr(
        gateway_chat, "_get_gateway_run_status",
        lambda b, k, r: {"run_id": r, "status": next(statuses, "completed"), "output": "AB"},
    )
    gateway_chat.resume_gateway_runs_after_restart()
    _wait_for_reattach_threads()

    assert failed and opened == []  # an unread journal proves no cursor: poll only
    tokens = [(e["payload"].get("gateway_seq"), e["payload"].get("text")) for e in _journal(sid, stream_id) if e["event"] == "token"]
    assert tokens == [(0, "A")]
    saved = _saved(sid)
    assert saved["messages"][-1]["content"] == "AB"
    assert saved["active_stream_id"] is None and saved["gateway_run"] is None


def test_reattach_resurfaces_an_approval_relayed_before_the_restart(isolated_sessions, monkeypatch):
    sid, stream_id = _orphaned_gateway_turn(run_id="run_parked_stream")
    _relayed_before_restart(sid, stream_id, [("token", {"text": "checking ", "gateway_seq": 0})])
    parked = {"event": "approval.request", "approval_id": "appr-9", "command": "rm x", "description": "d"}
    relayed = []
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", lambda b, k, r: {
        "run_id": r, "status": "waiting_for_approval", "approval": parked,
    })
    monkeypatch.setattr(
        gateway_chat, "_relay_gateway_run_approval",
        lambda session_id, run_id, payload, *a, **k: relayed.append(payload["approval_id"]),
    )
    monkeypatch.setattr(gateway_chat, "_open_gateway_run_events", lambda b, h, r, last_seq=-1: _sse(
        (2, {"event": "message.delta", "delta": "done"}), (3, {"event": "run.completed"}),
    ))
    gateway_chat.resume_gateway_runs_after_restart()
    _wait_for_reattach_threads()

    assert relayed == ["appr-9"]
    assert _saved(sid)["messages"][-1]["content"] == "checking done"


def test_reattach_cursor_includes_a_journaled_approval_so_it_is_not_relayed_twice(isolated_sessions, monkeypatch):
    sid, stream_id = _orphaned_gateway_turn(run_id="run_parked_journaled")
    parked = {"event": "approval.request", "approval_id": "appr-7", "command": "rm y", "description": "d"}
    _relayed_before_restart(sid, stream_id, [
        ("token", {"text": "checking ", "gateway_seq": 0}),
        ("approval", {"approval_id": "appr-7", "command": "rm y", "pending_count": 1, "gateway_seq": 1}),
    ])
    relayed, opened = [], []
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", lambda b, k, r: {
        "run_id": r, "status": "waiting_for_approval", "approval": parked,
    })
    monkeypatch.setattr(
        gateway_chat, "_relay_gateway_run_approval",
        lambda session_id, run_id, payload, *a, **k: relayed.append(payload["approval_id"]),
    )
    history = [(1, parked), (2, {"event": "message.delta", "delta": "done"}), (3, {"event": "run.completed"})]

    def open_events(base_url, headers, run_id, last_seq=-1):
        opened.append(last_seq)  # sent as Last-Event-ID; the gateway replays only seq > last_seq
        return _sse(*[(s, p) for s, p in history if s > last_seq])

    monkeypatch.setattr(gateway_chat, "_open_gateway_run_events", open_events)
    gateway_chat.resume_gateway_runs_after_restart()
    _wait_for_reattach_threads()

    assert opened == [1]
    assert relayed == ["appr-7"]
    assert _saved(sid)["messages"][-1]["content"] == "checking done"


class _ResetAfter(io.BytesIO):
    """An event stream whose connection resets once its bytes are consumed."""

    def __next__(self):
        line = self.readline()
        if not line:
            raise ConnectionResetError("reset by peer")
        return line


def test_reattach_cursor_commits_an_event_as_soon_as_it_is_relayed(isolated_sessions, monkeypatch):
    sid, stream_id = _orphaned_gateway_turn(run_id="run_reset_mid_frame")
    _relayed_before_restart(sid, stream_id, [("token", {"text": "", "gateway_seq": 0})])
    history = [(1, {"event": "message.delta", "delta": "A"}), (2, {"event": "run.completed", "output": "A"})]
    opened = []

    def open_events(base_url, headers, run_id, last_seq=-1):
        opened.append(last_seq)
        if len(opened) == 1:
            # Token A arrives, then the connection resets before the blank frame separator.
            return _ResetAfter(b"id: 1\n" + f"data: {json.dumps(dict(history[0][1], seq=1))}\n".encode())
        return _sse(*[(s, p) for s, p in history if s > last_seq])

    monkeypatch.setattr(gateway_chat, "_open_gateway_run_events", open_events)
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", lambda b, k, r: {"run_id": r, "status": "running"})
    gateway_chat.resume_gateway_runs_after_restart()
    _wait_for_reattach_threads()

    assert opened == [0, 1]
    assert [e["payload"]["text"] for e in _journal(sid, stream_id) if e["event"] == "token" and e["payload"].get("text")] == ["A"]
    assert _saved(sid)["messages"][-1]["content"] == "A"


@pytest.mark.parametrize("journaled", [True, False], ids=["cursor", "empty_journal"])
def test_reattach_probe_and_replay_surface_one_approval_once(isolated_sessions, monkeypatch, journaled):
    sid, stream_id = _orphaned_gateway_turn(run_id="run_parked_replayed")
    if journaled:
        _relayed_before_restart(sid, stream_id, [("token", {"text": "checking ", "gateway_seq": 0})])
    parked = {"event": "approval.request", "approval_id": "appr-3", "command": "rm z", "description": "d"}
    order = []
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", lambda b, k, r: order.append("probe") or {
        "run_id": r, "status": "waiting_for_approval", "approval": parked,
    })
    monkeypatch.setattr(
        gateway_chat, "_relay_gateway_run_approval",
        lambda session_id, run_id, payload, *a, **k: order.append(("approval", payload["approval_id"])),
    )

    def open_events(base_url, headers, run_id, last_seq=-1):
        order.append("events")
        # The gateway replays the still-pending approval after the cursor.
        return _sse((1, parked), (2, {"event": "message.delta", "delta": "done"}), (3, {"event": "run.completed"}))

    monkeypatch.setattr(gateway_chat, "_open_gateway_run_events", open_events)
    gateway_chat.resume_gateway_runs_after_restart()
    _wait_for_reattach_threads()

    # The status probe runs before the first blocking /events read, whatever the cursor.
    assert order == ["probe", ("approval", "appr-3"), "events"]
    assert _saved(sid)["messages"][-1]["content"].endswith("done")


def test_reattach_saves_the_run_output_over_the_restored_partial(isolated_sessions, monkeypatch):
    sid, stream_id = _orphaned_gateway_turn(run_id="run_transformed")
    _relayed_before_restart(sid, stream_id, [("token", {"text": "raw", "gateway_seq": 0})])
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", lambda b, k, r: {"run_id": r, "status": "running"})
    monkeypatch.setattr(gateway_chat, "_open_gateway_run_events", lambda b, h, r, last_seq=-1: _sse(
        (1, {"event": "run.completed", "output": "transformed"}),
    ))
    gateway_chat.resume_gateway_runs_after_restart()
    _wait_for_reattach_threads()

    # The Agent can rewrite its answer after streaming; run.completed.output is the final answer.
    assert _saved(sid)["messages"][-1]["content"] == "transformed"


def test_reattach_drops_a_replayed_approval_the_gateway_already_settled(isolated_sessions, monkeypatch):
    sid, stream_id = _orphaned_gateway_turn(run_id="run_auto_approved")
    _relayed_before_restart(sid, stream_id, [("token", {"text": "checking ", "gateway_seq": 0})])
    stale = {"event": "approval.request", "approval_id": "appr-auto", "command": "ls", "description": "d"}
    relayed, opened = [], []
    # Auto-approved before the restart (no journal row): the run is running again, nothing parked.
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", lambda b, k, r: {"run_id": r, "status": "running"})
    monkeypatch.setattr(
        gateway_chat, "_relay_gateway_run_approval",
        lambda session_id, run_id, payload, *a, **k: relayed.append(payload["approval_id"]),
    )
    history = [(1, stale), (2, {"event": "message.delta", "delta": "done"}), (3, {"event": "run.completed"})]

    def open_events(base_url, headers, run_id, last_seq=-1):
        opened.append(last_seq)
        return _sse(*[(s, p) for s, p in history if s > last_seq])

    monkeypatch.setattr(gateway_chat, "_open_gateway_run_events", open_events)
    gateway_chat.resume_gateway_runs_after_restart()
    _wait_for_reattach_threads()

    assert relayed == []
    assert opened == [0]
    assert _saved(sid)["messages"][-1]["content"] == "checking done"
