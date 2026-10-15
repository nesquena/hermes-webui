"""A named-profile session must reach its own profile on a shared Gateway listener, never the root's."""
from collections import OrderedDict
from email.message import Message
import io
import json
import threading
import urllib.error

import pytest

import api.gateway_chat as gateway_chat
import api.models as models
import api.streaming as streaming
from api import profiles
from api.config import STREAMS, STREAMS_LOCK, create_stream_channel
from api.models import new_session

SHARED = "http://127.0.0.1:8642"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    work = root / "profiles" / "work"
    work.mkdir(parents=True)
    (root / ".env").write_text("API_SERVER_KEY=root-key-0123456789\n")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set())
    monkeypatch.delenv("HERMES_WEBUI_ISOLATED_PROFILE", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", SHARED)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "root-key-0123456789")
    return root, work


def test_named_profile_without_own_url_uses_its_prefix_and_key(homes):
    _, work = homes
    (work / ".env").write_text("API_SERVER_KEY=work-key-0123456789\n")

    assert gateway_chat._gateway_endpoint_for_profile("work") == (f"{SHARED}/p/work", "work-key-0123456789")


def test_named_profile_without_key_never_borrows_the_root_key(homes):
    assert gateway_chat._gateway_endpoint_for_profile("work") == (f"{SHARED}/p/work", "")


def test_shared_url_loaded_from_root_env_reaches_named_profile(homes, monkeypatch):
    root, work = homes
    (root / ".env").write_text("HERMES_WEBUI_GATEWAY_BASE_URL=http://shared-gw:7000\nAPI_SERVER_KEY=root-key-0123456789\n")
    (work / ".env").write_text("API_SERVER_KEY=work-key-0123456789\n")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://shared-gw:7000")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", {"HERMES_WEBUI_GATEWAY_BASE_URL", "API_SERVER_KEY"})

    assert gateway_chat._gateway_endpoint_for_profile("work") == ("http://shared-gw:7000/p/work", "work-key-0123456789")


@pytest.mark.parametrize("source", ["env", "config"])
def test_named_profile_with_own_url_is_used_verbatim(homes, source):
    _, work = homes
    if source == "env":
        (work / ".env").write_text("HERMES_WEBUI_GATEWAY_BASE_URL=http://work-gw:9000/\nHERMES_WEBUI_GATEWAY_API_KEY=work-key-0123456789\n")
    else:
        (work / "config.yaml").write_text("webui_gateway_base_url: http://work-gw:9000/\n")
        (work / ".env").write_text("HERMES_WEBUI_GATEWAY_API_KEY=work-key-0123456789\n")

    assert gateway_chat._gateway_endpoint_for_profile("work") == ("http://work-gw:9000", "work-key-0123456789")


def test_profile_owned_gateway_uses_its_api_server_key_over_the_process_key(homes):
    _, work = homes
    (work / ".env").write_text("HERMES_WEBUI_GATEWAY_BASE_URL=http://work-gw:9000\nAPI_SERVER_KEY=work-key-0123456789\n")

    assert gateway_chat._gateway_endpoint_for_profile("work") == ("http://work-gw:9000", "work-key-0123456789")


def test_profile_owned_gateway_without_key_falls_back_to_the_process_key(homes):
    _, work = homes
    (work / ".env").write_text("HERMES_WEBUI_GATEWAY_BASE_URL=http://work-gw:9000\n")

    assert gateway_chat._gateway_endpoint_for_profile("work") == ("http://work-gw:9000", "root-key-0123456789")


@pytest.mark.parametrize("profile_url,expected_key", [(SHARED, "root-secret"), ("http://work-gw:9000", "")])
def test_profile_owned_url_without_key_uses_root_key_only_for_the_root_listener(homes, monkeypatch, profile_url, expected_key):
    root, work = homes
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_BASE_URL")
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_API_KEY")
    (root / ".env").write_text(f"HERMES_WEBUI_GATEWAY_BASE_URL={SHARED}\nHERMES_WEBUI_GATEWAY_API_KEY=root-secret\n")
    profiles._reload_dotenv(root)
    (work / "config.yaml").write_text(f"webui_gateway_base_url: {profile_url}\n")

    assert gateway_chat._gateway_api_key() == "root-secret"
    assert gateway_chat._gateway_endpoint_for_profile("work") == (profile_url, expected_key)


def test_process_shared_url_beats_a_stale_root_env_url(homes):
    root, work = homes
    (root / ".env").write_text("HERMES_WEBUI_GATEWAY_BASE_URL=http://stale-gw:7000\nAPI_SERVER_KEY=root-key-0123456789\n")
    (work / ".env").write_text("API_SERVER_KEY=work-key-0123456789\n")

    assert gateway_chat._gateway_endpoint_for_profile("work") == (f"{SHARED}/p/work", "work-key-0123456789")


@pytest.mark.parametrize("name", [None, "", "default"])
def test_root_profile_stays_unprefixed(homes, name):
    assert gateway_chat._gateway_endpoint_for_profile(name) == (SHARED, "root-key-0123456789")


def test_isolated_profile_deployment_stays_unprefixed(homes, monkeypatch):
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: True)

    base_url, _ = gateway_chat._gateway_endpoint_for_profile("work")

    assert base_url == SHARED


def test_live_turn_of_named_profile_session_goes_to_its_profile(homes, tmp_path, monkeypatch):
    _, work = homes
    (work / ".env").write_text("API_SERVER_KEY=work-key-0123456789\n")
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_USE_RUNS_API", "1")
    monkeypatch.setattr(gateway_chat, "_gateway_reasoning_effort_for_request", lambda *a, **k: None)
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {
        "status": "not_configured", "source": "none", "label": "", "message_count": 0, "messages": [],
    })
    monkeypatch.setattr(streaming, "_prefill_messages_with_webui_context", lambda ctx, cfg: [])
    s = new_session()
    s.profile = "work"
    stream_id = "stream-work"
    s.active_stream_id = stream_id
    s.pending_user_message = "hi"
    s.pending_attachments = []
    s.pending_started_at = 1.0
    s.save()
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append((req.get_method(), req.full_url, req.get_header("Authorization")))
        if req.get_method() == "POST":
            return io.BytesIO(b'{"run_id":"run_work"}')
        return io.BytesIO(
            b'data: {"event":"run.completed","output":"done"}\n'
            b"data: [DONE]\n"
        )

    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *a, **k: True)
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)
    with STREAMS_LOCK:
        STREAMS[stream_id] = create_stream_channel()

    gateway_chat._run_gateway_chat_streaming(s.session_id, "hi", "test-model", "/tmp", stream_id, [])

    assert seen and all(url.startswith(f"{SHARED}/p/work/v1/runs") for _, url, _ in seen), seen
    assert {auth for _, _, auth in seen} == {"Bearer work-key-0123456789"}


def _orphaned_named_turn(tmp_path, monkeypatch, gateway_run):
    """A work-profile sidecar as a killed WebUI process leaves it."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_USE_RUNS_API", "1")
    monkeypatch.setattr(gateway_chat, "GATEWAY_REATTACH_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(gateway_chat, "_gateway_reasoning_effort_for_request", lambda *a, **k: None)
    s = new_session()
    s.profile = "work"
    s.active_stream_id = gateway_run["stream_id"]
    s.pending_user_message = "long task"
    s.pending_attachments = []
    s.pending_started_at = 1.0
    s.gateway_run = gateway_run
    s.save()
    models.SESSIONS.clear()
    with STREAMS_LOCK:
        STREAMS.pop(gateway_run["stream_id"], None)
    return s.session_id


def _join_reattach():
    for thread in threading.enumerate():
        if thread.name.startswith("gateway-reattach-"):
            thread.join(10)
            assert not thread.is_alive()


@pytest.mark.parametrize("admitted", [True, False])
def test_pre_upgrade_named_run_reattaches_on_the_unprefixed_root_endpoint(homes, tmp_path, monkeypatch, admitted):
    _, work = homes
    (work / ".env").write_text("API_SERVER_KEY=work-key-0123456789\n")
    run = {"run_id": "run_old" if admitted else "", "stream_id": "stream-old", "regeneration": False, "goal_related": False}
    if not admitted:
        run["request"] = {"input": "long task"}
    sid = _orphaned_named_turn(tmp_path, monkeypatch, run)
    admits, polls = [], []
    monkeypatch.setattr(gateway_chat, "_admit_gateway_run", lambda url, headers, *a: admits.append(
        (url, headers.get("Authorization"))) or "run_old")
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", lambda b, k, r: polls.append((b, k, r)) or {
        "run_id": r, "status": "completed", "output": "answer from before the upgrade"})

    assert gateway_chat.resume_gateway_runs_after_restart() == [sid]
    _join_reattach()

    assert polls and set(polls) == {(SHARED, "root-key-0123456789", "run_old")}
    assert admits == ([] if admitted else [(f"{SHARED}/v1/runs", "Bearer root-key-0123456789")])
    saved = json.loads((models.SESSION_DIR / f"{sid}.json").read_text())
    assert saved["messages"][-1]["content"] == "answer from before the upgrade"
    assert saved["gateway_run"] is None and saved["active_stream_id"] is None


def test_marked_named_run_reattaches_on_its_recorded_profile_endpoint(homes, tmp_path, monkeypatch):
    _, work = homes
    (work / ".env").write_text("API_SERVER_KEY=work-key-0123456789\n")
    sid = _orphaned_named_turn(tmp_path, monkeypatch, {
        "run_id": "run_new", "stream_id": "stream-new", "regeneration": False, "goal_related": False,
        "endpoint_routing": "profile-v1", "base_url": f"{SHARED}/p/work",
    })
    polls = []
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", lambda b, k, r: polls.append((b, k)) or {
        "run_id": r, "status": "completed", "output": "ok"})

    assert gateway_chat.resume_gateway_runs_after_restart() == [sid]
    _join_reattach()

    assert set(polls) == {(f"{SHARED}/p/work", "work-key-0123456789")}


@pytest.mark.parametrize("code", [401, 404])
def test_named_profile_refusal_names_the_routing_fix(code):
    exc = urllib.error.HTTPError(f"{SHARED}/p/work/v1/runs", code, "x", hdrs=Message(), fp=None)

    event = gateway_chat._gateway_http_error_event(
        exc, "", api_key_configured=False,
        route_hint=gateway_chat._gateway_profile_route_hint(f"{SHARED}/p/work", code),
    )

    assert ("API_SERVER_KEY" if code == 401 else "gateway.multiplex_profiles") in event["hint"]
    assert "HERMES_WEBUI_GATEWAY_BASE_URL" in event["hint"] and "profile's .env" in event["hint"]
    assert gateway_chat._gateway_profile_route_hint(SHARED, code) == ""
    assert gateway_chat._gateway_profile_route_hint("http://gw/p/work/x", code) == ""


def test_profile_route_sends_api_server_key_not_a_cloned_webui_key(homes):
    _, work = homes
    (work / ".env").write_text("HERMES_WEBUI_GATEWAY_API_KEY=root-key-0123456789\nAPI_SERVER_KEY=work-key-0123456789\n")

    assert gateway_chat._gateway_endpoint_for_profile("work") == (f"{SHARED}/p/work", "work-key-0123456789")


@pytest.mark.parametrize("saved,expected_key", [(f"{SHARED}/p/work", "work-key-0123456789"), ("http://old-gw:7000", None)])
def test_reattach_key_is_bound_to_the_url_it_was_resolved_for(homes, saved, expected_key):
    _, work = homes
    (work / ".env").write_text("API_SERVER_KEY=work-key-0123456789\n")
    session = type("S", (), {"profile": "work"})()
    run = {"endpoint_routing": "profile-v1", "base_url": saved}

    assert gateway_chat._gateway_reattach_endpoint(session, run) == (saved, expected_key)


def test_moved_profile_endpoint_fails_reattach_without_sending_the_new_key(homes, tmp_path, monkeypatch):
    _, work = homes
    (work / ".env").write_text("API_SERVER_KEY=work-key-0123456789\n")
    sid = _orphaned_named_turn(tmp_path, monkeypatch, {
        "run_id": "run_old", "stream_id": "stream-moved", "regeneration": False, "goal_related": False,
        "endpoint_routing": "profile-v1", "base_url": "http://old-gw:7000",
    })
    polls = []
    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", lambda b, k, r: polls.append((b, k)) or {})

    assert gateway_chat.resume_gateway_runs_after_restart() == [sid]
    _join_reattach()

    assert polls == []
    saved = json.loads((models.SESSION_DIR / f"{sid}.json").read_text())
    assert saved["active_stream_id"] is None
    assert "Gateway URL changed" in json.dumps(saved["messages"])


def test_keyless_profile_owned_url_401_names_the_key_fix():
    hint = gateway_chat._gateway_profile_route_hint("http://localhost:8642", 401, keyless_profile=True)

    assert "API_SERVER_KEY" in hint and "profile's .env" in hint
    assert gateway_chat._gateway_profile_route_hint("http://localhost:8642", 401) == ""
    assert gateway_chat._gateway_profile_route_hint("http://localhost:8642", 404, keyless_profile=True) == ""


def test_profile_route_401_points_at_the_profile_key_not_multiplexing():
    hint401 = gateway_chat._gateway_profile_route_hint(f"{SHARED}/p/work", 401)
    hint404 = gateway_chat._gateway_profile_route_hint(f"{SHARED}/p/work", 404)

    assert "API_SERVER_KEY" in hint401 and "multiplex_profiles" not in hint401
    assert "multiplex_profiles" in hint404


@pytest.mark.parametrize("code,err_type", [(401, "gateway_auth_error"), (404, "gateway_http_error")])
def test_runs_api_routing_refusal_is_not_classified_as_a_provider_error(homes, tmp_path, monkeypatch, code, err_type):
    _, work = homes
    (work / ".env").write_text("API_SERVER_KEY=work-key-0123456789\n")
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    s = new_session(workspace=str(tmp_path))
    s.profile = "work"
    s.save()
    stream_id = "stream-route"
    s.active_stream_id = stream_id
    s.save()
    with STREAMS_LOCK:
        STREAMS[stream_id] = create_stream_channel()
    events = []

    def refuse(*a, **k):
        raise urllib.error.HTTPError(f"{SHARED}/p/work/v1/runs", code, "x", hdrs=Message(), fp=None)

    monkeypatch.setattr(gateway_chat, "_gateway_use_runs_api_enabled", lambda *a, **k: True)
    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *a, **k: True)
    monkeypatch.setattr(gateway_chat, "_run_gateway_runs_api_streaming", refuse)
    monkeypatch.setattr(gateway_chat, "_settle_gateway_terminal_error",
                        lambda *a, **k: events.append(k.get("route_classification")) or None)

    gateway_chat._run_gateway_chat_streaming(s.session_id, "hi", "m", str(tmp_path), stream_id, [])
    with STREAMS_LOCK:
        STREAMS.pop(stream_id, None)

    assert events and events[0]["type"] == err_type
    assert events[0]["hint"] == gateway_chat._gateway_profile_route_hint(f"{SHARED}/p/work", code)
