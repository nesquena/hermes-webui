"""Gateway mode refuses entry points that still run the agent in-process.

``/btw``, ``/background``, ``/compress``, the synchronous ``/api/chat`` and the
one-shot model calls (update summaries, commit messages) build an in-process
agent. On a gateway-backed deployment they either fail with a
provider-credential error (the WebUI usually has no model key) or silently run a
turn outside the gateway. Until each is routed through the gateway they answer
409, matching the existing MoA refusal, and must do so before creating a hidden
session, job or any other state.

#8971 item 1: the refusal follows the SESSION'S OWNING PROFILE, not the request
thread's active profile, so mixed backends across profiles cannot let one
profile's setting admit or refuse another profile's session. The owner's ENV
decides alongside its config (#9024 review round 2): launcher export first,
then the owner's own .env, then its config.yaml — never the live process env,
which after a profile switch holds the active profile's .env.
"""
import collections
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import gateway_sessions, helpers, profiles, routes  # noqa: E402

_REFUSAL = "is unavailable on gateway-backed sessions"
_GATEWAY_CFG = {"webui_chat_backend": "gateway"}
_ENV_KEY = profiles.CHAT_BACKEND_ENV_KEY


@pytest.fixture
def no_owner_env(monkeypatch):
    """The owner's env slice is empty: config-only backend selection, hermetic.

    Also keeps the real ~/.hermes/.env (or a developer's exported
    HERMES_WEBUI_CHAT_BACKEND) out of these tests.
    """
    monkeypatch.setattr(profiles, "chat_backend_environ_for_profile", lambda _p=None: {})


@pytest.fixture
def captured(monkeypatch):
    """Capture the JSON response written through ``helpers.bad``.

    The request profile's snapshot is LOCAL ({}); the owning profile's config is
    controlled per-test through ``gateway_sessions.config_for_profile``.
    """
    box = {}

    def fake_j(handler, payload, status=200, extra_headers=None, *, pretty=True):
        box["payload"] = payload
        box["status"] = status

    monkeypatch.setattr(helpers, "j", fake_j)
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(
        routes, "get_session_profile_readonly", lambda _sid: None
    )
    return box


@pytest.fixture
def gateway_mode(monkeypatch, no_owner_env):
    """The session's owning profile runs the gateway backend (#8971 item 1).

    Patches ``config_for_profile`` — the seam both the session and profile
    resolvers go through (``config_for_session`` delegates to it) — with an
    empty owner env slice, so the gateway answer comes from config alone.
    """
    monkeypatch.setattr(gateway_sessions, "config_for_profile", lambda _profile=None: dict(_GATEWAY_CFG))


@pytest.fixture
def captured_route_j(monkeypatch):
    """Capture the payload returned through ``routes.j`` (handler responses).

    The refusal helper resolves ``j`` dynamically from ``api.helpers``, so the
    ``captured`` fixture sees it; handler-level responses go through the
    module-bound ``routes.j`` instead.
    """
    box = {}

    def fake_j(handler, payload, status=200, extra_headers=None, *, pretty=True):
        box["payload"] = payload
        box["status"] = status
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    return box


@pytest.mark.parametrize(
    ("handler_name", "body", "route"),
    [
        ("_handle_btw", {"session_id": "s1", "question": "why?"}, "/btw"),
        ("_handle_background", {"session_id": "s1", "prompt": "do it"}, "/background"),
        ("_handle_chat_sync", {"session_id": "s1", "message": "hi"}, "/api/chat"),
        ("_handle_session_compress_start", {"session_id": "s1"}, "/compress"),
        ("_handle_session_compress", {"session_id": "s1"}, "/compress"),
        ("_handle_git_commit_message", {"session_id": "s1"}, "/api/git/commit-message"),
        (
            "_handle_git_commit_message_selected",
            {"session_id": "s1"},
            "/api/git/commit-message-selected",
        ),
    ],
)
def test_in_process_routes_refuse_in_gateway_mode(
    monkeypatch, captured, gateway_mode, handler_name, body, route
):
    """A gateway-backed OWNING profile refuses even when the request profile is local.

    The request snapshot is {} (local) via the ``captured`` fixture, so this
    fails before the owning-profile fix, which used to read the request profile.
    """

    getattr(routes, handler_name)(object(), dict(body))

    assert captured["status"] == 409
    assert captured["payload"] == {
        "error": f"{route} {_REFUSAL}",
        "code": "gateway_mode_unsupported",
        "feature": route,
    }


def test_gateway_request_profile_does_not_refuse_a_local_owned_session(
    monkeypatch, captured_route_j, no_owner_env
):
    """The inverse mixed-backend case: a gateway REQUEST profile must not refuse.

    A local owning profile admits the feature; the next admission gate
    (server_draining, #7293) answers, proving the gateway refusal was skipped.
    Before the fix the request profile's snapshot refused with 409 here.
    """
    monkeypatch.setattr(
        routes, "get_config_snapshot", lambda: dict(_GATEWAY_CFG)
    )
    monkeypatch.setattr(
        routes, "get_session_profile_readonly", lambda _sid: None
    )
    monkeypatch.setattr(gateway_sessions, "config_for_profile", lambda _profile=None: {})
    drain = {"error": "server draining", "code": "server_draining", "_status": 503}
    monkeypatch.setattr(routes, "run_admission_closed_response", lambda: dict(drain))

    routes._handle_btw(object(), {"session_id": "s1", "question": "why?"})

    assert captured_route_j["status"] == 503
    assert captured_route_j["payload"]["code"] == "server_draining"


def test_updates_summary_refuses_in_gateway_mode(monkeypatch, captured):
    """No session context exists here, so the request profile's snapshot rules."""
    monkeypatch.setattr(
        routes, "get_config_snapshot", lambda: dict(_GATEWAY_CFG)
    )
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"updates": {}})
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)

    handled = routes.handle_post(object(), SimpleNamespace(path="/api/updates/summary"))

    assert handled is not False, "the route fell through to 404"
    assert captured["status"] == 409
    assert captured["payload"]["error"] == f"/api/updates/summary {_REFUSAL}"
    assert captured["payload"]["code"] == "gateway_mode_unsupported"
    assert captured["payload"]["feature"] == "/api/updates/summary"


def test_local_backend_is_not_refused(monkeypatch, captured):
    """The refusal is gateway-only: the in-process backend still reaches its handler."""
    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)
    monkeypatch.setattr(
        routes,
        "get_session_profile_readonly",
        lambda _sid: (_ for _ in ()).throw(KeyError("missing")),
    )

    routes._handle_session_compress_start(object(), {"session_id": "s1"})

    assert captured["status"] == 404
    assert "unavailable on gateway-backed sessions" not in captured["payload"]["error"]


def test_refusal_helper_is_false_off_gateway(monkeypatch):
    from api.gateway_chat import refuse_on_gateway_backend

    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)
    assert not refuse_on_gateway_backend(object(), "/btw", {})


def test_refusal_does_not_load_or_rewrite_the_session(tmp_path, monkeypatch, no_owner_env):
    """#9024 review: the refusal learns the owner without the full loader.

    ``get_session()`` runs ``Session.load``, which collapses adjacent
    duplicate ``_partial`` rows and saves the sidecar (with a ``.bak``) — so a
    request answered 409 used to rewrite the conversation it refused. The
    owner must come from ``get_session_profile_readonly()``: the refused
    request leaves the file, the backup set and the session cache untouched.
    """
    import collections
    import json

    import api.models as models

    sid = "legacy-9024"
    directory = tmp_path / "sessions"
    directory.mkdir()
    partial = {"role": "assistant", "content": "interrupted", "_partial": True}
    sidecar = directory / f"{sid}.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": sid,
                "title": "Legacy",
                "created_at": 1.0,
                "updated_at": 2.0,
                "profile": "work",
                "messages": [
                    {"role": "user", "content": "hello"},
                    partial,
                    dict(partial),
                ],
            }
        ),
        encoding="utf-8",
    )
    before = sidecar.read_bytes()

    cache = collections.OrderedDict()
    for module in (models, routes):
        monkeypatch.setattr(module, "SESSION_DIR", directory)
        monkeypatch.setattr(module, "SESSIONS", cache)

    box = {}

    def fake_j(handler, payload, status=200, extra_headers=None, *, pretty=True):
        box["payload"] = payload
        box["status"] = status

    monkeypatch.setattr(helpers, "j", fake_j)
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(
        gateway_sessions, "config_for_profile", lambda _profile=None: dict(_GATEWAY_CFG)
    )

    routes._handle_btw(object(), {"session_id": sid, "question": "why?"})

    assert box["status"] == 409
    assert box["payload"]["code"] == "gateway_mode_unsupported"
    assert sidecar.read_bytes() == before, "the refused request rewrote the sidecar"
    assert list(directory.glob("*.bak")) == [], "the refused request wrote a backup"
    assert sid not in cache, "the refused request loaded (and cached) the session"


def _owner_env_from(monkeypatch, tmp_path, *, dotenv=None):
    """Point the real owner-env resolver at controlled inputs.

    ``dotenv`` seeds a REAL
    ``.env`` in the owner profile's home (the resolver stats the file, so it
    must exist). The live ``os.environ`` / provenance set is configured
    separately per test: a launcher export is a live value NOT in
    ``_loaded_profile_env_keys``; the active profile's .env value IS.
    """
    home = tmp_path / "owner-home"
    home.mkdir()
    if dotenv:
        (home / ".env").write_text(
            "".join(f"{k}={v}\n" for k, v in dotenv.items()), encoding="utf-8"
        )

    def fake_home(_name):
        return home

    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", fake_home)
    profiles._CHAT_BACKEND_ENV_CACHE.clear()


def test_active_profile_env_cannot_unlock_a_gateway_owner(monkeypatch, tmp_path, captured):
    """#9024 review round 2: the active profile's env must not decide the owner.

    The owning profile's config selects gateway, but the live process env says
    legacy AND the provenance set (#8432) says that value came from the ACTIVE
    profile's .env — exactly what a profile switch leaves in os.environ. The
    refusal must stand: only a launcher export, the owner's own .env, or the
    owner's config can change the answer.
    """
    monkeypatch.setattr(
        routes, "get_session_profile_readonly", lambda _sid: "owner"
    )
    monkeypatch.setattr(gateway_sessions, "config_for_profile", lambda _profile=None: dict(_GATEWAY_CFG))
    _owner_env_from(monkeypatch, tmp_path, dotenv={})
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "legacy")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", {_ENV_KEY})

    routes._handle_btw(object(), {"session_id": "s1", "question": "why?"})

    assert captured["status"] == 409
    assert captured["payload"]["code"] == "gateway_mode_unsupported"


def test_owner_env_file_selects_gateway(monkeypatch, tmp_path, captured):
    """The owner's own .env selects gateway even when its config is local."""
    monkeypatch.setattr(
        routes, "get_session_profile_readonly", lambda _sid: "owner"
    )
    monkeypatch.setattr(gateway_sessions, "config_for_profile", lambda _profile=None: {})
    _owner_env_from(monkeypatch, tmp_path, dotenv={"HERMES_WEBUI_CHAT_BACKEND": "gateway"})
    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)

    routes._handle_btw(object(), {"session_id": "s1", "question": "why?"})

    assert captured["status"] == 409
    assert captured["payload"]["code"] == "gateway_mode_unsupported"


def test_launcher_export_overrides_owner_env_file(monkeypatch, tmp_path, captured_route_j):
    """A genuine launcher export (live, not profile-loaded) wins over the owner's .env.

    The operator exported legacy; the owner profile's .env says gateway. The
    launcher value is not in the #8432 provenance set, so it is a genuine
    operator override and the local owner config admits the feature — the
    next admission gate (#7293) answers.
    """
    monkeypatch.setattr(
        routes, "get_session_profile_readonly", lambda _sid: "owner"
    )
    monkeypatch.setattr(
        routes, "get_config_snapshot", lambda: {}
    )
    monkeypatch.setattr(gateway_sessions, "config_for_profile", lambda _profile=None: {})
    _owner_env_from(
        monkeypatch,
        tmp_path,
        dotenv={"HERMES_WEBUI_CHAT_BACKEND": "gateway"},
    )
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "legacy")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set())
    drain = {"error": "server draining", "code": "server_draining", "_status": 503}
    monkeypatch.setattr(routes, "run_admission_closed_response", lambda: dict(drain))

    routes._handle_btw(object(), {"session_id": "s1", "question": "why?"})

    assert captured_route_j["status"] == 503
    assert captured_route_j["payload"]["code"] == "server_draining"


def _profile_homes(monkeypatch, tmp_path, *, owner_env=None, other_env=None):
    """Real profile homes for the owner and a second profile's worker scope.

    ``other_env`` seeds the OTHER profile's .env — the values
    ``profile_env_for_background_worker`` mirrors into os.environ while its
    scope is held open (#9024 review round 3).
    """
    homes = {}
    for name, env_text in (("owner", owner_env), ("other", other_env)):
        home = tmp_path / f"home-{name}"
        home.mkdir()
        if env_text:
            (home / ".env").write_text(env_text, encoding="utf-8")
        homes[name] = home
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name, _h=homes: _h.get(str(name or "owner"), _h["owner"]),
    )
    profiles._CHAT_BACKEND_ENV_CACHE.clear()
    return homes


def test_held_worker_scope_cannot_unlock_a_gateway_owner(monkeypatch, tmp_path, captured):
    """#9024 review round 3: another profile's open worker env is not launcher authority.

    The owner's config selects gateway; a background worker for profile
    "other" holds its .env (HERMES_WEBUI_CHAT_BACKEND=legacy) mirrored into the
    live process env. The mirror must not decide the owner's session: /btw
    refuses with 409 while the worker scope is open.
    """
    monkeypatch.setattr(
        routes, "get_session_profile_readonly", lambda _sid: "owner"
    )
    monkeypatch.setattr(
        gateway_sessions, "config_for_profile", lambda _profile=None: dict(_GATEWAY_CFG)
    )
    _profile_homes(
        monkeypatch,
        tmp_path,
        owner_env=None,
        other_env="HERMES_WEBUI_CHAT_BACKEND=legacy\n",
    )
    with profiles.profile_env_for_background_worker("other", "refusal test"):
        assert profiles.profile_env_writer_owns(_ENV_KEY), "fixture setup: worker mirror not recorded"
        routes._handle_btw(object(), {"session_id": "s1", "question": "why?"})

    assert captured["status"] == 409
    assert captured["payload"]["code"] == "gateway_mode_unsupported"
    assert not profiles.profile_env_writer_owns(_ENV_KEY), "worker restore must drop the mirror"


def test_held_worker_scope_cannot_refuse_a_local_owner(
    monkeypatch, tmp_path, captured_route_j
):
    """The inverse direction: a worker's gateway .env must not refuse a local owner.

    The owner's config is local; the held-open worker for profile "other"
    mirrors HERMES_WEBUI_CHAT_BACKEND=gateway into the live env. The refusal
    must be skipped so the next admission gate (#7293) answers.
    """
    monkeypatch.setattr(
        routes, "get_session_profile_readonly", lambda _sid: "owner"
    )
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(
        gateway_sessions, "config_for_profile", lambda _profile=None: {}
    )
    _profile_homes(
        monkeypatch,
        tmp_path,
        owner_env=None,
        other_env="HERMES_WEBUI_CHAT_BACKEND=gateway\n",
    )
    drain = {"error": "server draining", "code": "server_draining", "_status": 503}
    monkeypatch.setattr(routes, "run_admission_closed_response", lambda: dict(drain))
    with profiles.profile_env_for_background_worker("other", "refusal test"):
        routes._handle_btw(object(), {"session_id": "s1", "question": "why?"})

    assert captured_route_j["status"] == 503
    assert captured_route_j["payload"]["code"] == "server_draining"


def test_gateway_owned_commit_message_never_reaches_the_model_boundary(
    monkeypatch, tmp_path, captured
):
    """A gateway-owned refusal must happen before any in-process model call.

    The owner's config selects gateway; a held-open worker for profile
    "other" mirrors HERMES_WEBUI_CHAT_BACKEND=legacy into the live env. The
    git commit-message handler must answer 409 with ZERO calls to the
    in-process model boundary.
    """
    monkeypatch.setattr(
        routes, "get_session_profile_readonly", lambda _sid: "owner"
    )
    monkeypatch.setattr(
        gateway_sessions, "config_for_profile", lambda _profile=None: dict(_GATEWAY_CFG)
    )
    _profile_homes(
        monkeypatch,
        tmp_path,
        owner_env=None,
        other_env="HERMES_WEBUI_CHAT_BACKEND=legacy\n",
    )
    calls = {"model_boundary": 0}

    def _no_model(*_args, **_kwargs):
        calls["model_boundary"] += 1
        raise AssertionError("gateway-owned refusal reached the in-process model boundary")

    monkeypatch.setattr(routes, "_llm_git_commit_message", _no_model)
    with profiles.profile_env_for_background_worker("other", "refusal test"):
        routes._handle_git_commit_message(object(), {"session_id": "s1"})

    assert captured["status"] == 409
    assert captured["payload"]["code"] == "gateway_mode_unsupported"
    assert calls["model_boundary"] == 0


def _two_writer_homes(monkeypatch, tmp_path, *, first_env, second_env):
    """Homes for two distinct profile writers with different .env values."""
    homes = {}
    for name, env_text in (("first", first_env), ("second", second_env)):
        home = tmp_path / f"home-{name}"
        home.mkdir()
        if env_text:
            (home / ".env").write_text(env_text, encoding="utf-8")
        homes[name] = home
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name, _h=homes: _h[str(name)],
    )
    profiles._CHAT_BACKEND_ENV_CACHE.clear()
    return homes


def test_fifo_overlapping_worker_scopes_never_resurrect_the_exited_value(
    monkeypatch, tmp_path, captured
):
    """#9024 review round 4 (FIFO): an exited writer's value must not come back.

    Scope "first" opens, then scope "second" opens over it; they exit in FIFO
    order. The second scope's snapshot holds the FIRST writer's value, which
    its exit must never restore as a fake launcher export — after both scopes
    close the key is absent, and a gateway owner's in-process feature still
    refuses. Before the pair-level fix the second restore resurrected the
    first writer's value into the live env, where it read as launcher
    authority and unlocked the owner's session.
    """
    monkeypatch.setattr(
        routes, "get_session_profile_readonly", lambda _sid: "owner"
    )
    monkeypatch.setattr(
        gateway_sessions, "config_for_profile", lambda _profile=None: dict(_GATEWAY_CFG)
    )
    _two_writer_homes(
        monkeypatch,
        tmp_path,
        first_env="HERMES_WEBUI_CHAT_BACKEND=legacy\n",
        second_env="HERMES_WEBUI_CHAT_BACKEND=local\n",
    )
    calls = {"model_boundary": 0}

    def _no_model(*_args, **_kwargs):
        calls["model_boundary"] += 1
        raise AssertionError("gateway-owned refusal reached the in-process model boundary")

    monkeypatch.setattr(routes, "_llm_git_commit_message", _no_model)

    # FIFO scope order needs manual enter/exit: nesting two ``with`` blocks is
    # always LIFO. The first-opened scope exits while the second is still
    # open, and the second scope's snapshot holds the FIRST writer's value.
    cm_first = profiles.profile_env_for_background_worker("first", "fifo test")
    cm_second = profiles.profile_env_for_background_worker("second", "fifo test")
    first_open = second_open = False
    try:
        cm_first.__enter__()
        first_open = True
        assert os.environ.get(_ENV_KEY) == "legacy"
        cm_second.__enter__()
        second_open = True
        assert os.environ.get(_ENV_KEY) == "local"
        # FIFO: the first-opened scope exits first. Its own snapshot saw no
        # launcher value, so the key leaves the env while the second scope is
        # still open — restore always returns what the exiting scope saw.
        cm_first.__exit__(None, None, None)
        first_open = False
        assert os.environ.get(_ENV_KEY) is None
        assert (_ENV_KEY, "legacy") not in profiles._PROFILE_ENV_VALUES, (
            "the exited writer's pair is dropped even though the second "
            "scope still owns the KEY"
        )
        cm_second.__exit__(None, None, None)
        second_open = False
    finally:
        for _cm, _opened in ((cm_second, second_open), (cm_first, first_open)):
            if _opened:
                try:
                    _cm.__exit__(None, None, None)
                except Exception:
                    pass

    assert os.environ.get(_ENV_KEY) is None, "exited writer's value was resurrected"
    assert not profiles.profile_env_writer_owns(_ENV_KEY)
    # The same FIFO resurrection in the worker CM's THREAD-LOCAL channel: the
    # second scope's snapshot holds the first writer's thread env, so its
    # unconditional restore used to leave the first writer's binding live (and
    # the process-env fallback blocked) after both scopes closed — which made
    # later thread-local env reads in the same thread resolve through an
    # exited writer's profile.
    from api.config import _thread_ctx

    assert getattr(_thread_ctx, "env", {}) == {}, "exited writer's thread env was resurrected"
    assert not getattr(_thread_ctx, "block_process_env_fallback", False)
    routes._handle_git_commit_message(object(), {"session_id": "s1"})
    assert captured["status"] == 409
    assert captured["payload"]["code"] == "gateway_mode_unsupported"
    assert calls["model_boundary"] == 0


def test_lifo_overlapping_worker_scopes_restore_the_live_writer_value(
    monkeypatch, tmp_path, captured_route_j
):
    """#9024 review round 4 (LIFO): a still-live writer's value IS restored.

    The same overlap exits in LIFO order: the second scope restores the first
    writer's value (still owned by a live writer), then the first scope
    restores the launcher's absence. No value survives as a fake launcher
    export, and the owner stays refused only through genuine authority.
    """
    monkeypatch.setattr(
        routes, "get_session_profile_readonly", lambda _sid: "owner"
    )
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(
        gateway_sessions, "config_for_profile", lambda _profile=None: {}
    )
    _two_writer_homes(
        monkeypatch,
        tmp_path,
        first_env="HERMES_WEBUI_CHAT_BACKEND=local\n",
        second_env="HERMES_WEBUI_CHAT_BACKEND=gateway\n",
    )
    drain = {"error": "server draining", "code": "server_draining", "_status": 503}
    monkeypatch.setattr(routes, "run_admission_closed_response", lambda: dict(drain))

    with profiles.profile_env_for_background_worker("first", "lifo test"):
        assert os.environ.get(_ENV_KEY) == "local"
        with profiles.profile_env_for_background_worker("second", "lifo test"):
            assert os.environ.get(_ENV_KEY) == "gateway"
        # LIFO exit of the second scope restores the FIRST writer's value —
        # its writer is still open, so the value returns and stays ignored by
        # the checker rather than counting as launcher authority.
        assert os.environ.get(_ENV_KEY) == "local"
        assert profiles.profile_env_writer_owns(_ENV_KEY, "local")
        # A local owner is never refused through a writer-owned mirror value.
        routes._handle_btw(object(), {"session_id": "s1", "question": "why?"})
        assert captured_route_j["status"] == 503
        assert captured_route_j["payload"]["code"] == "server_draining"
    assert os.environ.get(_ENV_KEY) is None
    assert not profiles.profile_env_writer_owns(_ENV_KEY)


def test_streaming_turn_scope_cannot_unlock_a_gateway_owner(
    monkeypatch, tmp_path, captured
):
    """#9024 review round 4: the streaming turn's env mirror is writer-owned too.

    A real ``_run_agent_streaming`` turn for profile "other" (whose .env says
    HERMES_WEBUI_CHAT_BACKEND=legacy) holds the streaming env mirror open
    while its agent turn runs. The owner (gateway config) must still refuse
    the in-process commit-message call with ZERO model-boundary calls, and
    the stream's restore must return the env to its pre-turn state without
    resurrecting the mirror as launcher authority.
    """
    import queue
    import threading
    import types
    from unittest import mock

    import api.config as config
    import api.oauth
    import api.streaming as streaming

    monkeypatch.setattr(
        routes, "get_session_profile_readonly", lambda _sid: "owner"
    )
    monkeypatch.setattr(
        gateway_sessions, "config_for_profile", lambda _profile=None: dict(_GATEWAY_CFG)
    )
    _profile_homes(
        monkeypatch,
        tmp_path,
        owner_env=None,
        other_env="HERMES_WEBUI_CHAT_BACKEND=legacy\n",
    )
    monkeypatch.delenv(_ENV_KEY, raising=False)

    with config.SESSION_AGENT_CACHE_LOCK:
        config.SESSION_AGENT_CACHE.clear()

    session_id = "stream-9024"
    stream_id = f"stream-{session_id}"
    turn_done = threading.Event()
    release = threading.Event()

    class FakeSession:
        def __init__(self):
            self.session_id = session_id
            self.title = "Test"
            self.workspace = str(tmp_path)
            self.model = "test-model"
            self.messages = []
            self.personality = None
            self.input_tokens = 0
            self.output_tokens = 0
            self.estimated_cost = None
            self.tool_calls = []
            self.active_stream_id = stream_id
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.pending_user_source = None
            self.profile = "other"

        def save(self, touch_updated_at=True, skip_index=False):
            pass

        def compact(self):
            return {"session_id": self.session_id, "messages": self.messages}

    class BlockingAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id")
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            release.wait(timeout=30)
            return {
                "messages": [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {"role": "assistant", "content": "ok"},
                ]
            }

        def interrupt(self, _message):
            pass

    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = mock.Mock(return_value={})
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = mock.Mock(return_value=object())

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)
    monkeypatch.setattr(
        api.oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda resolver, **kwargs: resolver(**kwargs),
    )
    monkeypatch.setattr(streaming, "get_session", lambda _session_id: FakeSession())
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: BlockingAgent)
    monkeypatch.setattr("api.config.get_config", lambda: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])

    calls = {"model_boundary": 0}

    def _no_model(*_args, **_kwargs):
        calls["model_boundary"] += 1
        raise AssertionError("gateway-owned refusal reached the in-process model boundary")

    monkeypatch.setattr(routes, "_llm_git_commit_message", _no_model)

    q = queue.Queue()
    streaming.STREAMS[stream_id] = q
    worker = threading.Thread(
        target=streaming._run_agent_streaming,
        kwargs=dict(
            session_id=session_id,
            msg_text="hello",
            model="test-model",
            workspace=str(tmp_path),
            stream_id=stream_id,
        ),
        daemon=True,
    )
    try:
        monkeypatch.setattr(
            streaming, "peek_stream", lambda _sid: q, raising=False
        )
        worker.start()
        # Wait until the streaming writer's mirror is live in the process env.
        for _ in range(600):
            if profiles.profile_env_writer_owns(_ENV_KEY):
                break
            if not worker.is_alive():
                raise AssertionError("streaming worker exited before holding the mirror")
            time.sleep(0.05)
        assert os.environ.get(_ENV_KEY) == "legacy"
        assert profiles.profile_env_writer_owns(_ENV_KEY, "legacy")

        routes._handle_git_commit_message(object(), {"session_id": "s1"})
        assert captured["status"] == 409
        assert captured["payload"]["code"] == "gateway_mode_unsupported"
        assert calls["model_boundary"] == 0
    finally:
        release.set()
        worker.join(timeout=60)
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)

    assert not worker.is_alive()
    assert os.environ.get(_ENV_KEY) is None, "streaming restore resurrected the mirror value"
    assert not profiles.profile_env_writer_owns(_ENV_KEY)


def test_unverifiable_owner_config_fails_closed_to_refusal(monkeypatch, captured):
    """#9024 review round 4: an unreadable owner config never authorizes in-process.

    When the owning profile's config cannot be resolved (exception / non-dict),
    the decision must fail closed to the refusal side — never silently run an
    in-process model call on the request profile's local rule.
    """

    def _raise(_profile=None):
        raise RuntimeError("owner config unreadable")

    monkeypatch.setattr(gateway_sessions, "config_for_profile", _raise)
    monkeypatch.setattr(
        routes, "get_session_profile_readonly", lambda _sid: "owner"
    )
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})

    routes._handle_btw(object(), {"session_id": "s1", "question": "why?"})

    assert captured["status"] == 409
    assert captured["payload"]["code"] == "gateway_mode_unsupported"


def test_unverifiable_sidecar_fails_closed_but_a_missing_session_does_not(
    monkeypatch, captured, captured_route_j
):
    """#9024 review round 4: distinguish a missing session from an unverifiable one.

    A MISSING session (KeyError) keeps the request profile's rule so a bogus
    session_id still answers through the later handler path unchanged. An
    UNVERIFIABLE sidecar (ValueError) fails closed: the in-process feature
    refuses with 409 instead of silently authorizing a model call.
    """
    drain = {"error": "server draining", "code": "server_draining", "_status": 503}
    monkeypatch.setattr(routes, "run_admission_closed_response", lambda: dict(drain))

    def _missing(_sid):
        raise KeyError(_sid)

    monkeypatch.setattr(routes, "get_session_profile_readonly", _missing)
    routes._handle_btw(object(), {"session_id": "missing", "question": "why?"})
    assert captured_route_j["status"] == 503, "missing session: request rule must apply"
    assert captured_route_j["payload"]["code"] == "server_draining"

    def _unverifiable(_sid):
        raise ValueError("unverifiable sidecar")

    monkeypatch.setattr(routes, "get_session_profile_readonly", _unverifiable)
    routes._handle_btw(object(), {"session_id": "broken", "question": "why?"})
    assert captured["status"] == 409, "unverifiable sidecar: must fail closed"
    assert captured["payload"]["code"] == "gateway_mode_unsupported"


# ── #9024 review round 5: real checkpoint workers over a genuine launcher ───


def _checkpoint_homes(monkeypatch, tmp_path, *, owner_config, worker_a_env, worker_b_env):
    """Real profile homes: an owner with a REAL config.yaml, two worker profiles
    with REAL .env files, plus the root/default home config_for_profile
    compares against. Nothing is monkeypatched at the resolver level —
    config_for_profile/get_config_for_profile_home read the real files.
    """
    homes = {}
    for name in ("default", "owner", "worker-a", "worker-b"):
        home = tmp_path / f"home-{name}"
        home.mkdir()
        homes[name] = home
    (homes["owner"] / "config.yaml").write_text(owner_config, encoding="utf-8")
    (homes["worker-a"] / ".env").write_text(worker_a_env, encoding="utf-8")
    (homes["worker-b"] / ".env").write_text(worker_b_env, encoding="utf-8")
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name, _h=homes: _h.get(str(name or "default"), _h["default"]),
    )
    profiles._CHAT_BACKEND_ENV_CACHE.clear()
    return homes


def _run_real_checkpoints(monkeypatch, exit_first, sessions):
    """Two real ``_save_streaming_checkpoint()`` workers, released in order.

    Each worker's ``Session.save(skip_index=True)`` runs for REAL and only its
    wrapper's return is gated (a class-level ``Session.save`` patch that
    finishes the real save, then waits on a per-session gate — an instance
    attribute would leak into the persisted payload), so the profile env
    mirror stays open past the persisted checkpoint. ``exit_first`` picks FIFO
    ("a": the first-opened scope exits first) or LIFO ("b") release order;
    both real saves complete before either gate opens.
    """
    import threading

    import api.models as models
    import api.streaming as streaming

    real_save = models.Session.save
    saved_done = {}
    gates = {}
    for sess in sessions:
        saved_done[sess.session_id] = threading.Event()
        gates[sess.session_id] = threading.Event()

    def _gated_save(self, touch_updated_at=True, skip_index=False):
        real_save(self, touch_updated_at=touch_updated_at, skip_index=skip_index)
        saved_done[self.session_id].set()
        assert gates[self.session_id].wait(timeout=30), "checkpoint gate never opened"

    monkeypatch.setattr(models.Session, "save", _gated_save)

    errors = []
    threads = {}

    def _run(sess):
        try:
            streaming._save_streaming_checkpoint(sess)
        except BaseException as exc:  # pragma: no cover - surfaced by the assert
            errors.append(exc)

    for sess in sessions:
        threads[sess.session_id] = threading.Thread(
            target=_run, args=(sess,), daemon=True
        )
        threads[sess.session_id].start()
    # Both real Session.save(skip_index=True) calls must finish before release.
    for sess in sessions:
        assert saved_done[sess.session_id].wait(timeout=30), (
            f"real checkpoint save never finished for {sess.session_id}"
        )
    first, second = (
        (sessions[0], sessions[1]) if exit_first == "a" else (sessions[1], sessions[0])
    )
    for sess in (first, second):
        gates[sess.session_id].set()
        threads[sess.session_id].join(timeout=30)
        assert not threads[sess.session_id].is_alive(), "checkpoint worker never exited"
    assert not errors, f"checkpoint workers failed: {errors!r}"


@pytest.mark.parametrize(
    ("launcher_backend", "owner_config", "expected_env_after"),
    [
        # Direction 1: the launcher override is GATEWAY while the owner's own
        # config says local — launcher authority must win, so the in-process
        # features refuse.
        ("gateway", "webui_chat_backend: local\n", "gateway"),
        # Direction 2: the launcher override is LEGACY while the owner's own
        # config says gateway — the owner must NOT be refused.
        ("legacy", "webui_chat_backend: gateway\n", "legacy"),
    ],
)
@pytest.mark.parametrize(("exit_first",), [("a",), ("b",)])
def test_real_checkpoints_preserve_the_launcher_baseline(
    monkeypatch, tmp_path, captured, captured_route_j, exit_first,
    launcher_backend, owner_config, expected_env_after,
):
    """#9024 review round 5: FIFO/LIFO checkpoint exits keep the launcher value.

    Two REAL ``_save_streaming_checkpoint()`` workers (the supported
    ``scope_skill_modules=False`` path) for two different profiles run over a
    genuine launcher ``HERMES_WEBUI_CHAT_BACKEND`` export and a REAL saved
    owner session; both real ``Session.save(skip_index=True)`` calls complete
    and only the wrapper returns are gated. Whatever the exit order, the final
    process-env value is the ORIGINAL launcher export — the previous restore
    deleted it after a FIFO exit, reversing admission in both mixed-backend
    directions. The actual owner-scoped handlers then decide by the launcher
    value with ZERO in-process model-boundary calls, and the refused request
    leaves the saved owner session untouched.
    """
    import json

    import api.models as models

    _checkpoint_homes(
        monkeypatch,
        tmp_path,
        owner_config=owner_config,
        worker_a_env="HERMES_WEBUI_CHAT_BACKEND=legacy\n",
        worker_b_env="HERMES_WEBUI_CHAT_BACKEND=local\n",
    )
    monkeypatch.delenv(_ENV_KEY, raising=False)
    monkeypatch.setenv(_ENV_KEY, launcher_backend)

    directory = tmp_path / "sessions"
    directory.mkdir()
    owner_sid = "owner-9024"
    sidecar = directory / f"{owner_sid}.json"
    sidecar.write_text(
        json.dumps(
            {
                "session_id": owner_sid,
                "title": "Owner",
                "created_at": 1.0,
                "updated_at": 2.0,
                "profile": "owner",
                "messages": [{"role": "user", "content": "hello"}],
            }
        ),
        encoding="utf-8",
    )
    before = sidecar.read_bytes()
    cache = collections.OrderedDict()
    monkeypatch.setattr(models, "SESSION_DIR", directory)
    monkeypatch.setattr(models, "SESSIONS", cache)
    monkeypatch.setattr(routes, "SESSION_DIR", directory)
    monkeypatch.setattr(routes, "SESSIONS", cache)
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})

    workers = []
    for name in ("worker-a", "worker-b"):
        workers.append(
            models.Session(
                session_id=f"ck-{name}",
                title=f"checkpoint {name}",
                workspace=str(tmp_path),
                messages=[{"role": "user", "content": "hi"}],
                profile=name,
            )
        )

    _run_real_checkpoints(monkeypatch, exit_first, workers)

    assert os.environ.get(_ENV_KEY) == expected_env_after, (
        "checkpoint restore did not return the genuine launcher baseline"
    )
    assert not profiles.profile_env_writer_owns(_ENV_KEY)
    assert _ENV_KEY not in profiles._PROFILE_ENV_BASELINE

    calls = {"model_boundary": 0}

    def _no_model(*_args, **_kwargs):
        calls["model_boundary"] += 1
        raise AssertionError("reached the in-process model boundary")

    monkeypatch.setattr(routes, "_llm_git_commit_message", _no_model)
    routes._handle_git_commit_message(object(), {"session_id": owner_sid})
    assert calls["model_boundary"] == 0

    if launcher_backend == "gateway":
        # Launcher gateway + local owner config: refused before the model,
        # before any session load — the sidecar bytes, backup set and cache
        # membership are all untouched by the refused request.
        assert captured["status"] == 409
        assert captured["payload"]["code"] == "gateway_mode_unsupported"
        assert owner_sid not in cache, "the refused request loaded (and cached) the owner session"
    else:
        # Launcher legacy + gateway owner config: NOT refused — the next
        # admission gate answers (drain control), never a model call. The
        # request is legitimate here, so it may load the session; the saved
        # conversation must still not be rewritten by it.
        drain = {"error": "server draining", "code": "server_draining", "_status": 503}
        monkeypatch.setattr(routes, "run_admission_closed_response", lambda: dict(drain))
        routes._handle_btw(object(), {"session_id": owner_sid, "question": "why?"})
        assert captured_route_j["status"] == 503
        assert captured_route_j["payload"]["code"] == "server_draining"
        assert calls["model_boundary"] == 0

    assert sidecar.read_bytes() == before, "the request rewrote the owner sidecar"
    assert list(directory.glob("*.bak")) == [], "the request wrote a backup"


@pytest.mark.parametrize(
    ("breakage",), [("directory",), ("malformed",)],
)
def test_unreadable_owner_config_fails_closed_to_refusal(
    monkeypatch, tmp_path, captured, captured_route_j, breakage
):
    """#9024 review round 5: an unreadable owner config is not a valid {}.

    The owner's config.yaml EXISTS but cannot be read or parsed — a directory
    at the path, or malformed YAML. The loader used to collapse that to a
    valid local-owner {}, silently authorizing in-process model calls. The
    unreadable marker now fails closed: the in-process feature refuses 409
    with zero model-boundary calls. A home with NO config.yaml stays a valid
    local owner ({}) and is not refused.
    """
    import api.models as models

    homes = {}
    for name in ("default", "owner", "noconfig"):
        home = tmp_path / f"home-{name}"
        home.mkdir()
        homes[name] = home
    config_path = homes["owner"] / "config.yaml"
    if breakage == "directory":
        config_path.mkdir()
    else:
        config_path.write_text("webui_chat_backend: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name, _h=homes: _h.get(str(name or "default"), _h["default"]),
    )
    profiles._CHAT_BACKEND_ENV_CACHE.clear()
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.delenv(_ENV_KEY, raising=False)
    calls = {"model_boundary": 0}

    def _no_model(*_args, **_kwargs):
        calls["model_boundary"] += 1
        raise AssertionError("unreadable owner config reached the model boundary")

    monkeypatch.setattr(routes, "_llm_git_commit_message", _no_model)

    def _owner_profile(_sid):
        return "owner"

    monkeypatch.setattr(routes, "get_session_profile_readonly", _owner_profile)
    routes._handle_git_commit_message(object(), {"session_id": "s1"})
    assert captured["status"] == 409
    assert captured["payload"]["code"] == "gateway_mode_unsupported"
    assert calls["model_boundary"] == 0

    # Control: a profile home with NO config.yaml is a valid local owner —
    # the refusal is skipped and the next admission gate answers.
    monkeypatch.setattr(routes, "get_session_profile_readonly", lambda _sid: "noconfig")
    drain = {"error": "server draining", "code": "server_draining", "_status": 503}
    monkeypatch.setattr(routes, "run_admission_closed_response", lambda: dict(drain))
    routes._handle_btw(object(), {"session_id": "s2", "question": "why?"})
    assert captured_route_j["status"] == 503
    assert captured_route_j["payload"]["code"] == "server_draining"
