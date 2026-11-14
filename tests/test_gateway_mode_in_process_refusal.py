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


def test_open_worker_scope_keeps_the_launcher_rule(
    monkeypatch, tmp_path, captured, captured_route_j
):
    """#9024 review round 10: a held-open worker never hides the launcher's rule.

    While a profile-owned writer owns HERMES_WEBUI_CHAT_BACKEND, the live value
    belongs to that writer — but the value the OPERATOR exported before the
    first writer displaced it is still the launcher's rule (the mirror keeps it
    as the launcher baseline). Both directions were wrong when the reader fell
    straight through to the owner's .env: a genuine launcher "gateway" stopped
    refusing a local owner while another profile's worker mirrored "legacy",
    and a genuine launcher "legacy" wrongly refused a gateway owner while
    another profile's worker mirrored "gateway".
    """
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(routes, "get_session_profile_readonly", lambda _sid: "owner")
    drain = {"error": "server draining", "code": "server_draining", "_status": 503}
    monkeypatch.setattr(routes, "run_admission_closed_response", lambda: dict(drain))
    calls = {"model_boundary": 0}

    def _no_model(*_args, **_kwargs):
        calls["model_boundary"] += 1
        raise AssertionError("launcher rule hidden by a worker scope")

    monkeypatch.setattr(routes, "_llm_git_commit_message", _no_model)

    # A) launcher "gateway", LOCAL owner, another profile's worker mirrors
    # "legacy". The launcher value still decides: refuse.
    monkeypatch.setattr(gateway_sessions, "config_for_profile", lambda _profile=None: {})
    first = tmp_path / "first"
    first.mkdir()
    _profile_homes(
        monkeypatch,
        first,
        owner_env=None,
        other_env="HERMES_WEBUI_CHAT_BACKEND=legacy\n",
    )
    monkeypatch.setenv(_ENV_KEY, "gateway")
    with profiles.profile_env_for_background_worker("other", "launcher test"):
        assert profiles.profile_env_writer_owns(_ENV_KEY)
        assert os.environ.get(_ENV_KEY) == "legacy", "fixture setup: worker mirror not live"
        routes._handle_git_commit_message(object(), {"session_id": "s1"})
    assert captured["status"] == 409
    assert captured["payload"]["code"] == "gateway_mode_unsupported"
    assert calls["model_boundary"] == 0
    # The worker's exit restored the launcher value, which keeps deciding.
    assert os.environ.get(_ENV_KEY) == "gateway", "worker exit must restore the launcher value"
    routes._handle_git_commit_message(object(), {"session_id": "s1"})
    assert captured["status"] == 409
    assert calls["model_boundary"] == 0

    # B) launcher "legacy", GATEWAY owner, another profile's worker mirrors
    # "gateway". The launcher value still decides: do NOT refuse.
    monkeypatch.setattr(
        gateway_sessions, "config_for_profile", lambda _profile=None: dict(_GATEWAY_CFG)
    )
    second = tmp_path / "second"
    second.mkdir()
    _profile_homes(
        monkeypatch,
        second,
        owner_env=None,
        other_env="HERMES_WEBUI_CHAT_BACKEND=gateway\n",
    )
    monkeypatch.setenv(_ENV_KEY, "legacy")
    with profiles.profile_env_for_background_worker("other", "launcher test"):
        assert os.environ.get(_ENV_KEY) == "gateway", "fixture setup: worker mirror not live"
        routes._handle_btw(object(), {"session_id": "s1", "question": "why?"})
    assert captured_route_j["status"] == 503
    assert captured_route_j["payload"]["code"] == "server_draining"


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


_SCRUB_KEY = "OPENAI_API_KEY"


def _run_scrub_checkpoints(monkeypatch, exit_first, session_a, session_b, scrub_value):
    """Real ``_save_streaming_checkpoint()`` workers, opened in order, scrub over.

    Worker A's profile .env SUPPLIES the credential; worker B's home has none,
    so B's worker scope SCRUBS it from the process env while A is still open.
    The open order is enforced (A mirrored, then B scrubbed over it) by polling
    the live env between thread starts; both real ``Session.save`` calls run to
    completion before either gate opens, so each scope is still open past its
    persisted checkpoint. ``exit_first`` picks FIFO ("a") or LIFO ("b").
    """
    import threading

    import api.models as models
    import api.streaming as streaming

    real_save = models.Session.save
    saved_done = {}
    gates = {}
    for sess in (session_a, session_b):
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

    def _wait_for(fn, what):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if fn():
                return
            time.sleep(0.01)
        raise AssertionError(f"setup never happened: {what}")

    threads[session_a.session_id] = threading.Thread(
        target=_run, args=(session_a,), daemon=True
    )
    threads[session_a.session_id].start()
    # A's worker scope is open and its credential mirrored into the live env.
    _wait_for(
        lambda: os.environ.get(_SCRUB_KEY) == scrub_value,
        "worker A never mirrored its credential",
    )
    threads[session_b.session_id] = threading.Thread(
        target=_run, args=(session_b,), daemon=True
    )
    threads[session_b.session_id].start()
    # B's worker scope opened OVER A and scrubbed the credential.
    _wait_for(
        lambda: os.environ.get(_SCRUB_KEY) is None,
        "worker B never scrubbed the live credential",
    )
    for sess in (session_a, session_b):
        assert saved_done[sess.session_id].wait(timeout=30), (
            f"real checkpoint save never finished for {sess.session_id}"
        )
    first, second = (
        (session_a, session_b) if exit_first == "a" else (session_b, session_a)
    )
    for sess in (first, second):
        gates[sess.session_id].set()
        threads[sess.session_id].join(timeout=30)
        assert not threads[sess.session_id].is_alive(), "checkpoint worker never exited"
        yield sess
    assert not errors, f"checkpoint workers failed: {errors!r}"


@pytest.mark.parametrize(("exit_first",), [("a",), ("b",)])
def test_real_checkpoints_scrub_only_credential_never_resurrects(
    monkeypatch, tmp_path, exit_first
):
    """#9024 review round 8: a scrub-only scope cannot resurrect the credential.

    Two REAL ``_save_streaming_checkpoint()`` workers: A's profile supplies a
    provider credential in its .env, B's profile has none, so B's worker scope
    SCRUBS the mirrored credential (the worker CM pops a live secret its own
    env does not supply). Both real saves complete and only the wrapper
    returns are gated, so each scope is held open past its persisted
    checkpoint. FIFO — the SUPPLYING worker exits first: its exit must not
    reinstall the credential the live scrubber removed, and after both exit
    the credential is absent with no provenance (never fake launcher
    authority). LIFO control — the scrubber exits first: the still-live
    supplier's value is correctly back until its own exit.
    """
    import api.models as models

    scrub_value = "sk-scrubbed-9024"
    _checkpoint_homes(
        monkeypatch,
        tmp_path,
        owner_config="webui_chat_backend: local\n",
        worker_a_env=f"{_SCRUB_KEY}={scrub_value}\n",
        worker_b_env="HERMES_WEBUI_CHAT_BACKEND=local\n",
    )
    monkeypatch.delenv(_SCRUB_KEY, raising=False)

    directory = tmp_path / "sessions"
    directory.mkdir()
    cache = collections.OrderedDict()
    monkeypatch.setattr(models, "SESSION_DIR", directory)
    monkeypatch.setattr(models, "SESSIONS", cache)
    monkeypatch.setattr(routes, "SESSION_DIR", directory)
    monkeypatch.setattr(routes, "SESSIONS", cache)

    workers = []
    for name in ("worker-a", "worker-b"):
        workers.append(
            models.Session(
                session_id=f"scrub-{name}",
                title=f"scrub {name}",
                workspace=str(tmp_path),
                messages=[{"role": "user", "content": "hi"}],
                profile=name,
            )
        )

    for join_index, sess in enumerate(
        _run_scrub_checkpoints(
            monkeypatch, exit_first, workers[0], workers[1], scrub_value
        )
    ):
        if join_index != 0:
            continue
        if exit_first == "a":
            # FIFO: A (the supplier) left while B's scrub still holds the key.
            assert os.environ.get(_SCRUB_KEY) is None, (
                "the exited supplier resurrected the scrubbed credential "
                "into the live scrubber's scope"
            )
        else:
            # LIFO: B (the scrubber) left while A still supplies the key live.
            assert os.environ.get(_SCRUB_KEY) == scrub_value, (
                "the live supplier's credential was lost before its own exit"
            )

    assert os.environ.get(_SCRUB_KEY) is None, (
        "an exited writer's credential survived both scopes as fake launcher authority"
    )
    assert not profiles.profile_env_writer_owns(_SCRUB_KEY)
    assert _SCRUB_KEY not in profiles._PROFILE_ENV_BASELINE
    assert _SCRUB_KEY not in profiles._WORKER_OWNED_ENV_KEYS
    assert _SCRUB_KEY not in profiles._PROFILE_ENV_SCRUBBED_KEYS
    assert not [pair for pair in profiles._PROFILE_ENV_VALUES if pair[0] == _SCRUB_KEY]


@pytest.mark.parametrize(
    ("breakage", "control_name"),
    [
        ("nondict-config", "empty-config"),
        ("symlink-loop-config", "dangling-symlink-config"),
        ("unreadable-owner-env", "absent-owner-env"),
    ],
)
def test_unverifiable_owner_inputs_fail_closed_to_refusal(
    monkeypatch, tmp_path, captured, captured_route_j, breakage, control_name
):
    """#9024 review round 8: unverifiable owner inputs are not valid answers.

    Three inputs the loaders used to collapse to a valid local-owner answer
    now fail closed with zero in-process model-boundary calls:

    - a config.yaml that parses to a NON-DICT (list/scalar) — only a genuinely
      empty file (``None``) is the valid {} "no config";
    - a SELF-REFERENTIAL config.yaml symlink (stat OSError/ELOOP — the file
      exists but cannot be stated, unlike a dangling symlink = genuine absence);
    - an owner ``.env`` that exists but cannot be read (a directory at .env).

    Each case's CONTROL is the genuinely valid counterpart, which must keep
    answering through the next admission gate (503 drain), never 409.
    """
    homes = {}
    for name in ("default", "owner", "control"):
        home = tmp_path / f"home-{name}"
        home.mkdir()
        homes[name] = home
    owner_home = homes["owner"]
    control_home = homes["control"]
    config_path = owner_home / "config.yaml"
    env_path = owner_home / ".env"
    if breakage == "nondict-config":
        config_path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        # Control: a genuinely EMPTY config.yaml file is the valid {} answer.
        (control_home / "config.yaml").write_text("", encoding="utf-8")
    elif breakage == "symlink-loop-config":
        os.symlink(config_path, config_path)
        # Control: a DANGLING symlink is genuine absence — valid {} answer.
        os.symlink(
            control_home / "config-missing.yaml", control_home / "config.yaml"
        )
    else:  # unreadable-owner-env: valid LOCAL config, unreadable .env
        config_path.write_text("webui_chat_backend: local\n", encoding="utf-8")
        env_path.mkdir()
        # Control: a valid local config with NO .env at all.
        (control_home / "config.yaml").write_text(
            "webui_chat_backend: local\n", encoding="utf-8"
        )

    def fake_home(name, _h=homes):
        return _h.get(str(name or "owner"), _h["owner"])

    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", fake_home)
    profiles._CHAT_BACKEND_ENV_CACHE.clear()
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.delenv(_ENV_KEY, raising=False)
    calls = {"model_boundary": 0}

    def _no_model(*_args, **_kwargs):
        calls["model_boundary"] += 1
        raise AssertionError("unverifiable owner input reached the model boundary")

    monkeypatch.setattr(routes, "_llm_git_commit_message", _no_model)
    monkeypatch.setattr(
        routes, "get_session_profile_readonly", lambda _sid: "owner"
    )
    routes._handle_git_commit_message(object(), {"session_id": "s1"})
    assert captured["status"] == 409
    assert captured["payload"]["code"] == "gateway_mode_unsupported"
    assert calls["model_boundary"] == 0

    # The valid counterpart control: the same request against the genuinely
    # valid input is NOT refused — the next admission gate answers.
    monkeypatch.setattr(
        routes, "get_session_profile_readonly", lambda _sid: "control"
    )
    drain = {"error": "server draining", "code": "server_draining", "_status": 503}
    monkeypatch.setattr(routes, "run_admission_closed_response", lambda: dict(drain))
    routes._handle_btw(object(), {"session_id": "s1", "question": "why?"})
    assert captured_route_j["status"] == 503
    assert captured_route_j["payload"]["code"] == "server_draining"
    assert calls["model_boundary"] == 0


def test_ambient_unreadable_config_fails_closed_through_the_cached_path(
    monkeypatch, tmp_path, captured, captured_route_j
):
    """#9024 review round 8: the ambient/cached resolution keeps the marker.

    ``get_config()``/``get_config_snapshot()`` used to discard an unreadable
    active config.yaml in ``reload_config`` and serve the defaults-filled
    cache as if the owner had a valid local config — the marker only failed
    closed on the direct per-profile read. A session with no owning profile
    (KeyError) resolves through the REQUEST profile's ambient snapshot: with
    an unreadable ACTIVE config.yaml the in-process feature must refuse with
    zero model-boundary calls, and repairing the file recovers without a
    restart (the marker is never cached).
    """
    import api.config as config

    home = tmp_path / "ambient-home"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text("webui_chat_backend: [unclosed\n", encoding="utf-8")
    real_get_config_path = config._get_config_path
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)

    def _missing(_sid):
        raise KeyError(_sid)

    monkeypatch.setattr(routes, "get_session_profile_readonly", _missing)
    # Re-point routes.get_config_snapshot at the REAL snapshot: the ambient
    # cache is the code under test (the captured fixture had patched it).
    monkeypatch.setattr(routes, "get_config_snapshot", config.get_config_snapshot)
    monkeypatch.delenv(_ENV_KEY, raising=False)
    calls = {"model_boundary": 0}

    def _no_model(*_args, **_kwargs):
        calls["model_boundary"] += 1
        raise AssertionError("ambient unreadable config reached the model boundary")

    monkeypatch.setattr(routes, "_llm_git_commit_message", _no_model)

    saved = (
        dict(config._cfg_cache),
        config._cfg_mtime,
        config._cfg_path,
        config._cfg_unreadable,
    )
    try:
        config._cfg_cache.clear()
        config._cfg_mtime = 0.0
        config._cfg_path = None
        config._cfg_unreadable = False
        routes._handle_git_commit_message(object(), {"session_id": "s1"})
        assert captured["status"] == 409
        assert captured["payload"]["code"] == "gateway_mode_unsupported"
        assert calls["model_boundary"] == 0
        assert isinstance(routes.get_config_snapshot(), config.UnreadableProfileConfig)

        # Repair the file: the marker is never cached, so the same process
        # recovers and the valid config answers through the next gate. Bump the
        # mtime deterministically — a same-tick rewrite would look unchanged to
        # the mtime-staleness check.
        config_path.write_text("webui_chat_backend: local\n", encoding="utf-8")
        _st = config_path.stat()
        os.utime(
            config_path,
            ns=(_st.st_atime_ns, _st.st_mtime_ns + 1_000_000_000),
        )
        drain = {"error": "server draining", "code": "server_draining", "_status": 503}
        monkeypatch.setattr(
            routes, "run_admission_closed_response", lambda: dict(drain)
        )
        routes._handle_btw(object(), {"session_id": "s1", "question": "why?"})
        assert captured_route_j["status"] == 503
        assert captured_route_j["payload"]["code"] == "server_draining"
        assert calls["model_boundary"] == 0
    finally:
        config._cfg_cache.clear()
        config._cfg_cache.update(saved[0])
        config._cfg_mtime = saved[1]
        config._cfg_path = saved[2]
        config._cfg_unreadable = saved[3]
        monkeypatch.setattr(config, "_get_config_path", real_get_config_path)


def test_repaired_owner_env_recovers_without_an_mtime_change(
    monkeypatch, tmp_path, captured, captured_route_j
):
    """#9024 review round 9: a failed owner ``.env`` read is retried, not cached.

    The unreadable marker used to be cached behind the file's mtime, so a repair
    that leaves the mtime unchanged (restored read permission, a fixed symlink,
    a file restored with its original timestamp) kept the profile blocked until
    the file was edited or WebUI restarted. The repaired file here carries the
    EXACT mtime the unreadable ``.env`` had, so only a retried read can notice.
    """
    owner_home = tmp_path / "home-owner"
    owner_home.mkdir()
    (owner_home / "config.yaml").write_text(
        "webui_chat_backend: local\n", encoding="utf-8"
    )
    env_path = owner_home / ".env"
    env_path.mkdir()  # exists but cannot be read as a file
    broken_ns = env_path.stat().st_mtime_ns

    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _n: owner_home)
    profiles._CHAT_BACKEND_ENV_CACHE.clear()
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(routes, "get_session_profile_readonly", lambda _sid: "owner")
    monkeypatch.delenv(_ENV_KEY, raising=False)
    calls = {"model_boundary": 0}

    def _no_model(*_args, **_kwargs):
        calls["model_boundary"] += 1
        raise AssertionError("unreadable owner .env reached the model boundary")

    monkeypatch.setattr(routes, "_llm_git_commit_message", _no_model)
    routes._handle_git_commit_message(object(), {"session_id": "s1"})
    assert captured["status"] == 409
    assert calls["model_boundary"] == 0

    # Repair: a readable (empty-rule) .env carrying the SAME mtime.
    env_path.rmdir()
    env_path.write_text("UNRELATED=1\n", encoding="utf-8")
    os.utime(env_path, ns=(broken_ns, broken_ns))
    assert env_path.stat().st_mtime_ns == broken_ns

    drain = {"error": "server draining", "code": "server_draining", "_status": 503}
    monkeypatch.setattr(routes, "run_admission_closed_response", lambda: dict(drain))
    routes._handle_btw(object(), {"session_id": "s1", "question": "why?"})
    assert captured_route_j["status"] == 503
    assert captured_route_j["payload"]["code"] == "server_draining"
    assert calls["model_boundary"] == 0


def test_repaired_ambient_config_recovers_without_an_mtime_change(
    monkeypatch, tmp_path, captured, captured_route_j
):
    """#9024 review round 9: the ambient ``_cfg_unreadable`` flag is retried too.

    The flag was only cleared by a refresh, and a refresh only ran when the
    config.yaml mtime moved — so a repair that preserves the mtime left the
    ambient resolution failing closed forever. While the flag is set, every
    read re-attempts the load; the repaired file here has the EXACT mtime the
    broken one had.
    """
    import api.config as config

    home = tmp_path / "ambient-home"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text("webui_chat_backend: [unclosed\n", encoding="utf-8")
    broken_ns = config_path.stat().st_mtime_ns
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(routes, "get_config_snapshot", config.get_config_snapshot)
    monkeypatch.delenv(_ENV_KEY, raising=False)

    saved = (
        dict(config._cfg_cache),
        config._cfg_mtime,
        config._cfg_path,
        config._cfg_unreadable,
    )
    try:
        config._cfg_cache.clear()
        config._cfg_mtime = 0.0
        config._cfg_path = None
        config._cfg_unreadable = False
        assert isinstance(config.get_config(), config.UnreadableProfileConfig)
        assert isinstance(config.get_config_snapshot(), config.UnreadableProfileConfig)

        config_path.write_text("webui_chat_backend: local\n", encoding="utf-8")
        os.utime(config_path, ns=(broken_ns, broken_ns))
        assert config_path.stat().st_mtime_ns == broken_ns

        assert not isinstance(config.get_config(), config.UnreadableProfileConfig)
        assert not isinstance(
            config.get_config_snapshot(), config.UnreadableProfileConfig
        )
        assert config.get_config_snapshot().get("webui_chat_backend") == "local"
    finally:
        config._cfg_cache.clear()
        config._cfg_cache.update(saved[0])
        config._cfg_mtime = saved[1]
        config._cfg_path = saved[2]
        config._cfg_unreadable = saved[3]


def test_admission_reads_coherently_with_worker_cleanup(
    monkeypatch, tmp_path, captured
):
    """#9024 review round 8: admission never reads the drop-before-restore window.

    A background worker for profile "other" mirrors
    HERMES_WEBUI_CHAT_BACKEND=legacy into the live env. Its cleanup drops the
    mirror provenance BEFORE restoring the env (drop -> restore under
    _ENV_LOCK). Pausing the worker right after the actual drop used to let a
    gateway-owned commit-message request read the dead value with no
    provenance — launcher authority — and return 200 with one in-process
    model-boundary call. The backend value and its provenance are now read
    coherently under the same writer lock: the admission blocks until the
    restore finishes, then sees the true (absent) value and refuses.
    """
    import threading

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
    calls = {"model_boundary": 0}

    def _no_model(*_args, **_kwargs):
        calls["model_boundary"] += 1
        raise AssertionError("cleanup-window admission reached the model boundary")

    monkeypatch.setattr(routes, "_llm_git_commit_message", _no_model)

    real_restore = profiles.restore_profile_env_snapshot
    in_window = threading.Event()
    exit_body = threading.Event()

    def _paused_restore(old_values):
        # Runs INSIDE the worker CM's finally, under _ENV_LOCK: ownership has
        # been dropped, the env still holds the dead value. Hold the window
        # open for a fixed interval while the main thread makes its admission
        # call — an event handshake would deadlock against the coherent read
        # (the admission blocks on _ENV_LOCK until this returns).
        in_window.set()
        time.sleep(1.0)
        return real_restore(old_values)

    monkeypatch.setattr(profiles, "restore_profile_env_snapshot", _paused_restore)

    errors = []
    worker_open = threading.Event()

    def _worker():
        try:
            cm = profiles.profile_env_for_background_worker("other", "cleanup window")
            cm.__enter__()
            worker_open.set()
            exit_body.wait(timeout=30)
            cm.__exit__(None, None, None)
        except BaseException as exc:  # pragma: no cover - surfaced by the assert
            errors.append(exc)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    try:
        assert worker_open.wait(timeout=30), "worker scope never opened"
        assert os.environ.get(_ENV_KEY) == "legacy"
        # Let the worker leave its body; the paused restore holds the
        # drop-before-restore window open for a fixed interval.
        exit_body.set()
        assert in_window.wait(timeout=30), "cleanup window never opened"
        assert not profiles.profile_env_writer_owns(_ENV_KEY), (
            "fixture setup: the drop must have released ownership by now"
        )
        # The admission read coherently: with the fix this call BLOCKS on the
        # writer lock until the paused restore finishes; without it the dead
        # value read as launcher authority and reached the model boundary.
        routes._handle_git_commit_message(object(), {"session_id": "s1"})
    finally:
        exit_body.set()
        thread.join(timeout=30)
        assert not thread.is_alive(), "cleanup-window worker never exited"
    assert not errors, f"cleanup-window worker failed: {errors!r}"

    assert captured["status"] == 409
    assert captured["payload"]["code"] == "gateway_mode_unsupported"
    assert calls["model_boundary"] == 0
    assert os.environ.get(_ENV_KEY) is None
    assert not profiles.profile_env_writer_owns(_ENV_KEY)


def _prime_handoff_summary(monkeypatch):
    """Stub the state reads the handoff handler performs before the model call."""
    from api import models

    msgs = [
        {"role": "user", "content": "Please fix the failing build", "timestamp": 1},
        {"role": "assistant", "content": "Build fixed; tests pass", "timestamp": 2},
    ]
    monkeypatch.setattr(models, "get_cli_session_messages", lambda _sid: msgs)
    monkeypatch.setattr(
        models, "count_conversation_rounds", lambda _sid, since=None: 10
    )
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "_persist_handoff_summary", lambda *a, **k: {})


def _stub_model_resolution(monkeypatch):
    """Make execution reach in-process agent construction deterministically.

    The handler imports the agent runtime lazily and wraps the whole model
    path in ``except Exception`` → fallback, so without these stubs a missing
    module would make both backend modes return the fallback and the
    gateway/no-agent assertion below would pass for the wrong reason.
    """
    import types

    import api.config as api_config
    import api.oauth as api_oauth

    fake_runtime_provider = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_provider.resolve_runtime_provider = lambda *a, **k: {}
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_provider
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(
        sys.modules, "hermes_cli.runtime_provider", fake_runtime_provider
    )
    monkeypatch.setattr(routes, "ensure_agent_runtime_current", lambda: None)
    monkeypatch.setattr(routes, "require_ai_agent_class", lambda: object)
    monkeypatch.setattr(
        routes,
        "_resolve_agent_connection_bundle",
        lambda provider, api_key, base_url, _rt: {
            "provider": provider,
            "api_key": api_key,
            "base_url": base_url,
        },
    )
    monkeypatch.setattr(routes, "_agent_bundle_kwargs", lambda _cls, _bundle: {})
    monkeypatch.setattr(
        api_config, "model_with_provider_context", lambda model, provider: model
    )
    monkeypatch.setattr(
        api_config, "resolve_model_provider", lambda _model: ("m", "openai", None)
    )
    monkeypatch.setattr(
        api_oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda _fn, requested=None: {"api_key": "test-key", "provider": "openai"},
    )


def test_handoff_summary_never_builds_an_agent_in_gateway_mode(
    monkeypatch, captured_route_j
):
    """#8971 item 2: gateway mode returns the deterministic fallback.

    The handler exists for gateway sessions, so the fix is the local fallback,
    not a 409 — but it must never construct the in-process helper agent that
    calls the model outside the gateway and the session's profile.

    This handler reads the request profile's ambient rule, so the mode comes
    from the process env (this file's ``gateway_mode`` fixture encodes the
    session-owner rule instead, #9024).
    """
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    _prime_handoff_summary(monkeypatch)
    _stub_model_resolution(monkeypatch)

    built = []

    def record_agent_construction(*args, **kwargs):
        built.append((args, kwargs))
        raise AssertionError("unreachable fake agent")

    monkeypatch.setattr(
        routes, "construct_memoryless_helper_agent", record_agent_construction
    )

    routes._handle_handoff_summary(object(), {"session_id": "s1"})

    # If the agent were built, its construction would be recorded even though
    # the handler's own ``except Exception`` turns the failure into a fallback.
    assert built == []
    assert captured_route_j["status"] == 200
    assert captured_route_j["payload"]["ok"] is True
    assert captured_route_j["payload"]["fallback"] is True
    assert captured_route_j["payload"]["summary"]
    assert captured_route_j["payload"]["rounds"] == 10


def test_handoff_summary_reaches_the_model_path_off_gateway(
    monkeypatch, captured_route_j
):
    """The gateway gate is the only thing that skips agent construction."""
    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)
    _prime_handoff_summary(monkeypatch)
    _stub_model_resolution(monkeypatch)

    class FakeAgent:
        reasoning_config = None

    built = []

    def record_agent_construction(*args, **kwargs):
        built.append((args, kwargs))
        return FakeAgent()

    monkeypatch.setattr(
        routes, "construct_memoryless_helper_agent", record_agent_construction
    )

    routes._handle_handoff_summary(object(), {"session_id": "s1"})

    # Off gateway, the handler proceeds into the model path; the fake agent
    # then fails inside it and the handler degrades to the fallback, which is
    # fine — the assertion is that construction was attempted at all.
    assert built, "gateway gate skipped agent construction off gateway"
