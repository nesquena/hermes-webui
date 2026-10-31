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
from api import profiles
from api.config import STREAMS, STREAMS_LOCK, create_stream_channel
from api.models import new_session

SHARED = "http://127.0.0.1:8642"
WORK_KEY = "work-key-0123456789"
ROOT_KEY = "root-key-0123456789"


@pytest.fixture
def work_profile(tmp_path, monkeypatch):
    """Root profile plus a named ``work`` profile that carries its own Gateway key."""
    root = tmp_path / "hermes"
    work = root / "profiles" / "work"
    work.mkdir(parents=True)
    (root / ".env").write_text(f"API_SERVER_KEY={ROOT_KEY}\n")
    (work / ".env").write_text(f"API_SERVER_KEY={WORK_KEY}\n")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set())
    monkeypatch.delenv("HERMES_WEBUI_ISOLATED_PROFILE", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", SHARED)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", ROOT_KEY)
    return work


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    """An isolated session store and the gateway-backend env a WebUI process starts with."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_USE_RUNS_API", "1")
    monkeypatch.setattr(gateway_chat, "GATEWAY_REATTACH_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(gateway_chat, "_gateway_reasoning_effort_for_request", lambda *a, **k: None)
    return session_dir


@pytest.mark.parametrize("profile_env,expected_key", [
    (f"API_SERVER_KEY={WORK_KEY}", WORK_KEY),
    ("", ""),
    (f"HERMES_WEBUI_GATEWAY_API_KEY={ROOT_KEY}\nAPI_SERVER_KEY={WORK_KEY}", WORK_KEY),
], ids=["api_server_key", "keyless_never_borrows_the_root_key", "webui_alias_is_not_a_route_key"])
def test_named_profile_without_own_url_routes_to_its_prefix_with_its_own_key(work_profile, profile_env, expected_key):
    (work_profile / ".env").write_text(profile_env + "\n")

    assert gateway_chat._gateway_endpoint_for_profile("work") == (f"{SHARED}/p/work", expected_key)


@pytest.mark.parametrize("profile_env,profile_config,expected_key", [
    (f"HERMES_WEBUI_GATEWAY_BASE_URL=http://work-gw:9000/\nHERMES_WEBUI_GATEWAY_API_KEY={WORK_KEY}", "", WORK_KEY),
    (f"HERMES_WEBUI_GATEWAY_API_KEY={WORK_KEY}", "webui_gateway_base_url: http://work-gw:9000/\n", WORK_KEY),
    (f"HERMES_WEBUI_GATEWAY_BASE_URL=http://work-gw:9000\nAPI_SERVER_KEY={WORK_KEY}", "", WORK_KEY),
    ("HERMES_WEBUI_GATEWAY_BASE_URL=http://work-gw:9000\n", "", ""),
], ids=["url_in_env", "url_in_config", "api_server_key_wins", "no_key_gets_none"])
def test_a_profile_owned_gateway_url_is_used_verbatim_with_only_its_own_key(
        work_profile, profile_env, profile_config, expected_key):
    (work_profile / ".env").write_text(profile_env + "\n")
    (work_profile / "config.yaml").write_text(profile_config)

    assert gateway_chat._gateway_endpoint_for_profile("work") == ("http://work-gw:9000", expected_key)


def test_an_open_worker_scope_of_another_profile_does_not_pick_the_destination_for_a_profiles_key(work_profile):
    """profile_env_for_background_worker() mirrors alpha's .env into os.environ for the worker body; resolving
    ``work`` meanwhile must still route to the launcher's shared URL, never alpha's Gateway, with work's key."""
    alpha = work_profile.parent / "alpha"
    alpha.mkdir()
    (alpha / ".env").write_text("HERMES_WEBUI_GATEWAY_BASE_URL=http://alpha-gw:9000\nAPI_SERVER_KEY=alpha-key\n")

    with profiles.profile_env_for_background_worker("alpha"):
        assert gateway_chat.os.environ["HERMES_WEBUI_GATEWAY_BASE_URL"] == "http://alpha-gw:9000"  # the mirror is open
        assert gateway_chat._gateway_endpoint_for_profile("work") == (f"{SHARED}/p/work", WORK_KEY)
        assert gateway_chat._gateway_endpoint_for_profile("alpha") == ("http://alpha-gw:9000", "alpha-key")

    assert gateway_chat.os.environ["HERMES_WEBUI_GATEWAY_BASE_URL"] == SHARED
    assert profiles._WORKER_ENV_BASELINE == {}


def test_nested_worker_scopes_restore_the_process_environ_view(work_profile):
    alpha = work_profile.parent / "alpha"
    alpha.mkdir()
    (alpha / ".env").write_text("HERMES_WEBUI_GATEWAY_BASE_URL=http://alpha-gw:9000\n")

    with profiles.profile_env_for_background_worker("alpha"):
        with profiles.profile_env_for_background_worker("alpha"):
            assert profiles.process_environ_without_worker_overlays()["HERMES_WEBUI_GATEWAY_BASE_URL"] == SHARED
        assert profiles.process_environ_without_worker_overlays()["HERMES_WEBUI_GATEWAY_BASE_URL"] == SHARED
    assert profiles._WORKER_ENV_BASELINE == {}


def test_a_profile_with_only_a_webui_alias_is_not_routed_and_keeps_the_unprefixed_listener(work_profile):
    """Pinned (#7877): the alias is the shared URL's own key, so /p/<name> would be a 401; the profile's own
    credential goes to the trusted shared listener, exactly as before routing."""
    (work_profile / ".env").write_text(f"HERMES_WEBUI_GATEWAY_API_KEY={WORK_KEY}\n")

    assert gateway_chat._gateway_endpoint_for_profile("work") == (SHARED, WORK_KEY)


def test_a_shared_url_already_naming_a_profile_route_is_refused_not_doubled(work_profile, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", f"{SHARED}/p/alpha")

    with pytest.raises(RuntimeError, match="already names a profile route"):
        gateway_chat._gateway_endpoint_for_profile("work")


def test_shared_url_loaded_from_root_env_reaches_named_profile(work_profile, monkeypatch):
    root = work_profile.parents[1]
    (root / ".env").write_text(f"HERMES_WEBUI_GATEWAY_BASE_URL=http://shared-gw:7000\nAPI_SERVER_KEY={ROOT_KEY}\n")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://shared-gw:7000")
    # Root-home values are shared, never profile-sourced (#8432): model startup, where the launch home is the root
    # home and its .env is the snapshot a launcher preloaded.
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(root))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME_DOTENV", {"HERMES_WEBUI_GATEWAY_BASE_URL": "http://shared-gw:7000"})

    assert gateway_chat._gateway_endpoint_for_profile("work") == ("http://shared-gw:7000/p/work", WORK_KEY)


def test_process_shared_url_beats_a_stale_root_env_url(work_profile):
    (work_profile.parents[1] / ".env").write_text(
        f"HERMES_WEBUI_GATEWAY_BASE_URL=http://stale-gw:7000\nAPI_SERVER_KEY={ROOT_KEY}\n")

    assert gateway_chat._gateway_endpoint_for_profile("work") == (f"{SHARED}/p/work", WORK_KEY)


@pytest.mark.parametrize("profile_url,expected_key", [(SHARED, "root-secret"), ("http://work-gw:9000", "")])
def test_profile_owned_url_without_key_uses_root_key_only_for_the_root_listener(
        work_profile, monkeypatch, profile_url, expected_key):
    root = work_profile.parents[1]
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_BASE_URL")
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_API_KEY")
    (root / ".env").write_text(f"HERMES_WEBUI_GATEWAY_BASE_URL={SHARED}\nHERMES_WEBUI_GATEWAY_API_KEY=root-secret\n")
    profiles._reload_dotenv(root)
    (work_profile / ".env").write_text("")
    (work_profile / "config.yaml").write_text(f"webui_gateway_base_url: {profile_url}\n")

    assert gateway_chat._gateway_api_key() == "root-secret"
    assert gateway_chat._gateway_endpoint_for_profile("work") == (profile_url, expected_key)


@pytest.mark.parametrize("name", [None, "", "default"])
def test_root_profile_stays_unprefixed(work_profile, name):
    assert gateway_chat._gateway_endpoint_for_profile(name) == (SHARED, ROOT_KEY)


def test_isolated_profile_deployment_stays_unprefixed(work_profile, monkeypatch):
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: True)

    assert gateway_chat._gateway_endpoint_for_profile("work")[0] == SHARED


def test_live_turn_of_named_profile_session_goes_to_its_profile(work_profile, sessions, monkeypatch):
    s = new_session()
    s.profile = "work"
    s.active_stream_id = "stream-work"
    s.pending_user_message = "hi"
    s.pending_attachments = []
    s.pending_started_at = 1.0
    s.save()
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append((req.full_url, req.get_header("Authorization")))
        if req.get_method() == "POST":
            return io.BytesIO(b'{"run_id":"run_work"}')
        return io.BytesIO(b'data: {"event":"run.completed","output":"done"}\n' b"data: [DONE]\n")

    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *a, **k: True)
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)
    with STREAMS_LOCK:
        STREAMS["stream-work"] = create_stream_channel()

    gateway_chat._run_gateway_chat_streaming(s.session_id, "hi", "test-model", "/tmp", "stream-work", [])

    assert seen and all(url.startswith(f"{SHARED}/p/work/v1/runs") for url, _ in seen), seen
    assert {auth for _, auth in seen} == {f"Bearer {WORK_KEY}"}


def _orphaned_named_turn(gateway_run):
    """A ``work``-profile sidecar as a killed WebUI process leaves it."""
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


def _reattach(monkeypatch, gateway_run, status=None):
    """Orphan ``gateway_run``, stub the runs API with ``status``, and return (session id, polls, admits)."""
    sid = _orphaned_named_turn(gateway_run)
    polls, admits = [], []

    def poll(base_url, api_key, run_id):
        polls.append((base_url, api_key, run_id))
        return status(run_id) if status else {}

    monkeypatch.setattr(gateway_chat, "_get_gateway_run_status", poll)
    monkeypatch.setattr(gateway_chat, "_admit_gateway_run",
                        lambda url, headers, *a: admits.append((url, headers.get("Authorization"))) or "run_old")
    assert gateway_chat.resume_gateway_runs_after_restart() == [sid]
    _join_reattach()
    return sid, polls, admits


@pytest.mark.parametrize("admitted", [True, False])
def test_pre_upgrade_named_run_reattaches_on_the_unprefixed_root_endpoint(work_profile, sessions, monkeypatch, admitted):
    run = {"run_id": "run_old" if admitted else "", "stream_id": "stream-old", "regeneration": False,
           "goal_related": False}
    if not admitted:
        run["request"] = {"input": "long task"}

    sid, polls, admits = _reattach(monkeypatch, run, lambda r: {
        "run_id": r, "status": "completed", "output": "answer from before the upgrade"})

    assert polls and set(polls) == {(SHARED, ROOT_KEY, "run_old")}
    assert admits == ([] if admitted else [(f"{SHARED}/v1/runs", f"Bearer {ROOT_KEY}")])
    saved = json.loads((models.SESSION_DIR / f"{sid}.json").read_text())
    assert saved["messages"][-1]["content"] == "answer from before the upgrade"
    assert saved["gateway_run"] is None and saved["active_stream_id"] is None


def test_marked_named_run_reattaches_on_its_recorded_profile_endpoint(work_profile, sessions, monkeypatch):
    _, polls, _ = _reattach(monkeypatch, {
        "run_id": "run_new", "stream_id": "stream-new", "regeneration": False, "goal_related": False,
        "endpoint_routing": "profile-v1", "base_url": f"{SHARED}/p/work",
    }, lambda r: {"run_id": r, "status": "completed", "output": "ok"})

    assert set(polls) == {(f"{SHARED}/p/work", WORK_KEY, "run_new")}


def test_moved_profile_endpoint_fails_reattach_without_sending_the_new_key(work_profile, sessions, monkeypatch):
    sid, polls, _ = _reattach(monkeypatch, {
        "run_id": "run_old", "stream_id": "stream-moved", "regeneration": False, "goal_related": False,
        "endpoint_routing": "profile-v1", "base_url": "http://old-gw:7000",
    })

    assert polls == []
    saved = json.loads((models.SESSION_DIR / f"{sid}.json").read_text())
    assert saved["active_stream_id"] is None and "Gateway URL changed" in json.dumps(saved["messages"])


@pytest.mark.parametrize("saved,expected_key", [(f"{SHARED}/p/work", WORK_KEY), ("http://old-gw:7000", None)])
def test_reattach_key_is_bound_to_the_url_it_was_resolved_for(work_profile, saved, expected_key):
    run = {"endpoint_routing": "profile-v1", "base_url": saved}

    assert gateway_chat._gateway_reattach_endpoint(type("S", (), {"profile": "work"})(), run) == (saved, expected_key)


@pytest.mark.parametrize("code", [401, 404])
def test_named_profile_refusal_names_the_routing_fix(code):
    exc = urllib.error.HTTPError(f"{SHARED}/p/work/v1/runs", code, "x", hdrs=Message(), fp=None)
    hint = gateway_chat._gateway_profile_route_hint(f"{SHARED}/p/work", code)
    event = gateway_chat._gateway_http_error_event(exc, "", api_key_configured=False, route_hint=hint)

    assert ("API_SERVER_KEY" if code == 401 else "gateway.multiplex_profiles") in event["hint"]
    assert "HERMES_WEBUI_GATEWAY_BASE_URL" in event["hint"] and "profile's .env" in event["hint"]
    assert gateway_chat._gateway_profile_route_hint(SHARED, code) == ""
    assert gateway_chat._gateway_profile_route_hint("http://gw/p/work/x", code) == ""


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
def test_runs_api_routing_refusal_is_not_classified_as_a_provider_error(work_profile, sessions, monkeypatch, code, err_type):
    s = new_session(workspace=str(sessions))
    s.profile = "work"
    s.active_stream_id = "stream-route"
    s.save()
    with STREAMS_LOCK:
        STREAMS["stream-route"] = create_stream_channel()
    events = []

    def refuse(*a, **k):
        raise urllib.error.HTTPError(f"{SHARED}/p/work/v1/runs", code, "x", hdrs=Message(), fp=None)

    monkeypatch.setattr(gateway_chat, "_gateway_use_runs_api_enabled", lambda *a, **k: True)
    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *a, **k: True)
    monkeypatch.setattr(gateway_chat, "_run_gateway_runs_api_streaming", refuse)
    monkeypatch.setattr(gateway_chat, "_settle_gateway_terminal_error",
                        lambda *a, **k: events.append(k.get("route_classification")) or None)

    gateway_chat._run_gateway_chat_streaming(s.session_id, "hi", "m", str(sessions), "stream-route", [])
    with STREAMS_LOCK:
        STREAMS.pop("stream-route", None)

    assert events and events[0]["type"] == err_type
    assert events[0]["hint"] == gateway_chat._gateway_profile_route_hint(f"{SHARED}/p/work", code)
