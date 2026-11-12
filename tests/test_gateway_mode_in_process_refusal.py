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
import sys
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
