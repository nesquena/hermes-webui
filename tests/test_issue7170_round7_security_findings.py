"""Regression coverage for the #7170 round-7 security findings.

Three live findings from the 2026-09-25 / 2026-09-23 re-gate that this file
pins:

A) [P1 security, 2026-09-25] ``api/gateway_chat.py:1322`` — the gateway worker
   can pair the session-owning profile's Gateway URL with the PROCESS-ENV API
   key (which holds the active profile's .env values), so a request may fail
   auth or send profile A's credential to profile B's endpoint. The Gateway
   key must come from the session-owning profile, like the config snapshot.

B) [P1, 2026-09-23 / 2026-09-25] ``api/config.py:5749`` — when a profile-scoped
   reasoning lookup has no ``provider_id``, ``_resolve_model_reasoning_efforts_impl``
   calls ``resolve_model_provider(model)`` which reads the module-global
   ``cfg``. So a profile-scoped ``/api/reasoning?model=…`` can probe the
   ambient profile's provider endpoint and report an effort clamped against
   the wrong model capabilities.

C) [P1, 2026-09-25] ``api/routes.py:24056`` — if ``_gateway_session_owner_cfg``
   raises during Gateway chat dispatch, the stream and pending session state
   have already been registered, but the worker has not started. The cleanup
   handler only covers thread-start failures, so this leaves a registered
   stream with no worker and a pending Gateway-run entry. Subsequent chat
   starts for the session are blocked as though a stream were active.
"""

from collections import OrderedDict
import os
from pathlib import Path

import pytest
import yaml

import api.config as cfg
import api.gateway_chat as gateway_chat
import api.models as models
import api.routes as routes
import api.streaming as streaming
from api.config import STREAMS, create_stream_channel


# ---------------------------------------------------------------------------
# Finding A: Gateway key must come from the session-owning profile
# ---------------------------------------------------------------------------


_GATEWAY_MODEL = "claude-opus-4-5"


def _write_profile_cfg(home: Path, *, provider: str, base_url: str | None = None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "model": {"default": _GATEWAY_MODEL, "provider": provider},
    }
    if base_url is not None:
        payload["model"]["base_url"] = base_url
    home.joinpath("config.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def _write_profile_env(home: Path, *, api_key: str | None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    if api_key is None:
        return
    home.joinpath(".env").write_text(
        f'HERMES_WEBUI_GATEWAY_API_KEY="{api_key}"\n', encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def _clear_global_stream_registries():
    """Reset module-global stream/run state between tests so a stranded
    stream from a prior test never leaks into the next test's assertions.
    """
    STREAMS.clear()
    gateway_chat._STREAM_RUN_IDS.clear()
    gateway_chat._STREAM_RUN_LIFECYCLE.clear()
    yield
    STREAMS.clear()
    gateway_chat._STREAM_RUN_IDS.clear()
    gateway_chat._STREAM_RUN_LIFECYCLE.clear()


@pytest.fixture
def two_profiles_with_keys(tmp_path, monkeypatch):
    """Two profiles with DIFFERENT ``HERMES_WEBUI_GATEWAY_API_KEY`` values.

    Ambient process profile A's key is what ``_gateway_api_key()`` returns
    from ``os.environ``. Session profile B's key is the one the worker
    SHOULD send.
    """
    profile_a_home = tmp_path / "profiles" / "a"
    profile_b_home = tmp_path / "profiles" / "b"
    _write_profile_cfg(profile_a_home, provider="lmstudio", base_url="http://a-gateway:1234")
    _write_profile_cfg(profile_b_home, provider="anthropic", base_url="http://b-gateway:5678")
    _write_profile_env(profile_a_home, api_key="KEY_FOR_PROFILE_A")
    _write_profile_env(profile_b_home, api_key="KEY_FOR_PROFILE_B")

    # Pin ambient resolver to profile A.
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(profile_a_home / "config.yaml"))
    cfg.reload_config()
    # Pin process env to profile A's key (the wrong key for profile B).
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "KEY_FOR_PROFILE_A")
    monkeypatch.delenv("API_SERVER_KEY", raising=False)

    # Pin session profile -> home resolution to profile B.
    import api.models as _models_mod
    monkeypatch.setattr(
        _models_mod, "_get_profile_home", lambda profile: profile_b_home
    )

    yield cfg, profile_a_home, profile_b_home, monkeypatch

    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_API_KEY", raising=False)
    cfg.reload_config()


def test_session_profile_api_key_resolves_from_session_home_env(
    two_profiles_with_keys,
):
    """The new helper must read the session profile's
    ``HERMES_WEBUI_GATEWAY_API_KEY``, not the process env. Without the fix,
    the helper would only see ``KEY_FOR_PROFILE_A`` (the process env value).
    """
    _, _, profile_b_home, _ = two_profiles_with_keys
    s = models.new_session(profile="b")
    s.save()
    key = gateway_chat._gateway_session_api_key(s)
    assert key == "KEY_FOR_PROFILE_B", (
        f"_gateway_session_api_key returned {key!r}; expected KEY_FOR_PROFILE_B. "
        "The session profile's .env must win over the process env."
    )


def test_dispatch_captures_session_profile_api_key_for_gateway_worker(
    two_profiles_with_keys,
):
    """``/api/chat/start`` must capture the session profile's gateway api
    key on the request thread (where the session profile's env is in scope)
    and hand it to the detached worker as ``session_api_key`` — the worker
    must NOT fall back to ``_gateway_api_key()`` (which reads process env
    and would return profile A's key for a profile-B request).
    """
    cfg_mod, _, profile_b_home, monkeypatch = two_profiles_with_keys

    session_dir = profile_b_home.parent / "sessions"
    session_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    captured_thread: dict = {}

    class ImmediateThread:
        def __init__(self, *args, **kwargs):
            captured_thread["kwargs"] = kwargs
            self.args = args

        def start(self):
            return None

    monkeypatch.setattr(routes, "set_last_workspace", lambda workspace, **_kw: None)
    monkeypatch.setattr(
        routes, "create_stream_channel", lambda: create_stream_channel()
    )
    monkeypatch.setattr(routes.threading, "Thread", ImmediateThread)

    s = models.new_session(profile="b")
    s.pending_user_message = "hello"
    s.pending_attachments = []
    s.pending_started_at = 1.0
    s.title = "Profile B"
    s.messages = [{"role": "user", "content": "hello"}]
    s.save()

    os.environ["HERMES_WEBUI_CHAT_BACKEND"] = "gateway"

    try:
        response = routes._start_chat_stream_for_session(
            s,
            msg="hello",
            attachments=[],
            workspace=str(session_dir),
            model=_GATEWAY_MODEL,
            model_provider="anthropic",
            external_runtime_owned=True,
        )
    finally:
        del os.environ["HERMES_WEBUI_CHAT_BACKEND"]
    assert response and "stream_id" in response

    thread_payload = captured_thread.get("kwargs") or {}
    worker_kwargs = thread_payload.get("kwargs") or {}
    session_api_key = worker_kwargs.get("session_api_key")
    assert session_api_key == "KEY_FOR_PROFILE_B", (
        f"Dispatch handed worker session_api_key={session_api_key!r}; "
        "expected KEY_FOR_PROFILE_B. The worker must NOT fall back to the "
        "process env (which holds KEY_FOR_PROFILE_A)."
    )


def test_gateway_worker_sends_session_profile_api_key_not_process_env(
    two_profiles_with_keys,
):
    """End-to-end: with dispatch captured, the worker's outgoing
    ``Authorization: Bearer …`` header must carry the session profile's
    key (KEY_FOR_PROFILE_B), NOT the process-env key (KEY_FOR_PROFILE_A).
    """
    _, _, profile_b_home, monkeypatch = two_profiles_with_keys

    session_dir = profile_b_home.parent / "sessions_e2e"
    session_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    captured: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"done"}}]}\n\n'
            yield b'data: [DONE]\n\n'

    def fake_urlopen(req, timeout=0):
        captured["headers"] = dict(req.headers or {})
        captured["url"] = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        return FakeResponse()

    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        streaming, "_load_webui_prefill_context", lambda c: {"messages": []}
    )
    monkeypatch.setattr(
        streaming, "_prefill_messages_with_webui_context", lambda ctx, c: []
    )

    s = models.new_session(profile="b")
    s.pending_user_message = "hello"
    s.pending_attachments = []
    s.pending_started_at = 1.0
    s.save()
    stream_id = "stream-key-isolation"
    s.active_stream_id = stream_id
    channel = create_stream_channel()
    STREAMS[stream_id] = channel

    # Resolve the session-owner cfg AND api key the same way the dispatch would.
    session_cfg = gateway_chat._gateway_session_owner_cfg(s)
    session_api_key = gateway_chat._gateway_session_api_key(s)

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "hello",
        _GATEWAY_MODEL,
        str(session_dir),
        stream_id,
        [],
        model_provider="anthropic",
        session_cfg=session_cfg,
        session_api_key=session_api_key,
    )

    auth = captured.get("headers", {}).get("Authorization", "")
    assert auth == "Bearer KEY_FOR_PROFILE_B", (
        f"Outgoing Authorization header is {auth!r}; expected 'Bearer KEY_FOR_PROFILE_B'. "
        "The worker read the process-env key (KEY_FOR_PROFILE_A) instead of the "
        "session profile's key — finding A is live."
    )


# ---------------------------------------------------------------------------
# Finding B.2 / C.1: provider lookup uses ambient config
# ---------------------------------------------------------------------------


def test_provider_resolution_uses_scoped_config_not_ambient(tmp_path, monkeypatch):
    """``_resolve_model_reasoning_efforts_impl`` must resolve the provider
    from the SCOPED config_data, not the module-global ``cfg``.

    We hook ``resolve_model_provider`` to record what config_obj it was
    called with. With the bug, it's called with no config_obj (or the
    module global); with the fix, it's called with the scoped config_data.
    """
    from api.config import _resolve_model_reasoning_efforts_impl

    ambient_home = tmp_path / "ambient"
    ambient_home.mkdir(parents=True, exist_ok=True)
    ambient_home.joinpath("config.yaml").write_text(yaml.safe_dump({
        "model": {"default": "local-thinker", "provider": "lmstudio"},
    }))

    scoped_config = {
        "model": {"default": "local-thinker", "provider": "anthropic"},
    }

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(ambient_home / "config.yaml"))
    cfg.reload_config()

    # Hook resolve_model_provider to record the call signature.
    seen: dict = {}
    import api.config as _cfg_mod
    real_resolve = _cfg_mod.resolve_model_provider

    def spy_resolve(model_id, **kwargs):
        seen["call"] = (model_id, kwargs.get("config_obj"))
        return real_resolve(model_id, **kwargs)

    monkeypatch.setattr(_cfg_mod, "resolve_model_provider", spy_resolve)

    # Now call the function with the scoped config. The spied call should
    # receive the scoped config_obj (not None / not the module global).
    _resolve_model_reasoning_efforts_impl(
        model_id="local-thinker",
        config_data=scoped_config,
    )

    assert seen.get("call") is not None, "resolve_model_provider was not called"
    called_model, called_config = seen["call"]
    assert called_model == "local-thinker"
    assert called_config is scoped_config, (
        f"resolve_model_provider was called with config_obj={called_config!r}; "
        "expected the scoped config_data. With the bug, the scoped config "
        "is dropped and the ambient module-global cfg is used."
    )


# ---------------------------------------------------------------------------
# Finding C.2: snapshot failure strands stream
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_dispatch_env(tmp_path, monkeypatch):
    """Set up the minimum state for a /api/chat/start dispatch.

    Yields a tuple (monkeypatch, session_dir). The test is responsible for
    creating the session and calling the dispatch.
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    os.environ["HERMES_WEBUI_CHAT_BACKEND"] = "gateway"
    os.environ["HERMES_WEBUI_GATEWAY_BASE_URL"] = "http://gateway.local"
    os.environ["HERMES_WEBUI_GATEWAY_API_KEY"] = "test-key"

    yield monkeypatch, session_dir

    for k in ("HERMES_WEBUI_CHAT_BACKEND", "HERMES_WEBUI_GATEWAY_BASE_URL", "HERMES_WEBUI_GATEWAY_API_KEY"):
        os.environ.pop(k, None)


def test_dispatch_snapshot_failure_cleans_up_stream_state(
    isolated_dispatch_env, tmp_path,
):
    """If ``_gateway_session_owner_cfg`` raises during Gateway chat
    dispatch, the stream/run state must be released — otherwise the
    session is blocked as though a stream were active for subsequent
    chat starts.

    The fix wraps line 24056 in a try/except that runs the same cleanup
    that ``thr.start()`` failure runs.
    """
    monkeypatch, session_dir = isolated_dispatch_env

    # Make _gateway_session_owner_cfg raise.
    def _raise(*args, **kwargs):
        raise RuntimeError("simulated snapshot failure")

    monkeypatch.setattr(gateway_chat, "_gateway_session_owner_cfg", _raise)

    s = models.new_session(profile="default")
    s.pending_user_message = "hello"
    s.pending_attachments = []
    s.pending_started_at = 1.0
    s.save()

    # The dispatch will raise; capture it.
    raised = None
    try:
        routes._start_chat_stream_for_session(
            s,
            msg="hello",
            attachments=[],
            workspace=str(tmp_path),
            model="gpt-4o",
            model_provider="openai",
            external_runtime_owned=True,
        )
    except Exception as e:
        raised = e

    # The dispatch raised, so we expect the simulated exception.
    assert raised is not None, "Dispatch should have raised RuntimeError"
    assert "simulated snapshot failure" in str(raised)

    # THE BUG: STREAMS and _STREAM_RUN_LIFECYCLE are non-empty because the
    # cleanup handler only covered thread-start failures. With the fix, the
    # new try/except around the snapshot capture releases them.
    assert len(STREAMS) == 0, (
        f"STREAMS registry is non-empty ({list(STREAMS)!r}) after a "
        "snapshot-failed dispatch; subsequent chats may collide."
    )
    assert len(gateway_chat._STREAM_RUN_LIFECYCLE) == 0, (
        f"_STREAM_RUN_LIFECYCLE is non-empty "
        f"({list(gateway_chat._STREAM_RUN_LIFECYCLE)!r}) after a "
        "snapshot-failed dispatch; pending Gateway-run entry is stranded."
    )


def test_dispatch_snapshot_failure_then_subsequent_dispatch_on_same_session_succeeds(
    isolated_dispatch_env, tmp_path, monkeypatch
):
    """End-to-end: a snapshot-failed dispatch must reset the session's
    active_stream_id so a subsequent chat start for the SAME session
    does not see it as already-active.

    This is the user-visible symptom: after a transient snapshot
    failure, the session is dead to subsequent chats.
    """
    _, session_dir = isolated_dispatch_env

    # First dispatch: make the snapshot capture fail.
    def _raise(*args, **kwargs):
        raise RuntimeError("simulated snapshot failure")

    monkeypatch.setattr(gateway_chat, "_gateway_session_owner_cfg", _raise)

    s = models.new_session(profile="default")
    s.pending_user_message = "first"
    s.pending_attachments = []
    s.pending_started_at = 1.0
    s.save()

    first_raised = None
    try:
        routes._start_chat_stream_for_session(
            s,
            msg="first",
            attachments=[],
            workspace=str(tmp_path),
            model="gpt-4o",
            model_provider="openai",
            external_runtime_owned=True,
        )
    except Exception as e:
        first_raised = e
    assert first_raised is not None
    assert "simulated snapshot failure" in str(first_raised)

    # The fix must release STREAMS / _STREAM_RUN_LIFECYCLE and clear the
    # session's active_stream_id so the session isn't permanently blocked.
    # Re-read the session from disk (the in-memory `s` has the stranded
    # value; the fix is supposed to have called .save() with active_stream_id=None).
    s_loaded = models.get_session(s.session_id)
    assert s_loaded is not None, "Session must still be loadable after a failed dispatch"
    assert s_loaded.active_stream_id is None, (
        f"Session {s.session_id!r} active_stream_id is {s_loaded.active_stream_id!r} "
        "after a snapshot-failed dispatch; the session is permanently blocked "
        "for new chats."
    )

    for k in ("HERMES_WEBUI_CHAT_BACKEND", "HERMES_WEBUI_GATEWAY_BASE_URL", "HERMES_WEBUI_GATEWAY_API_KEY"):
        os.environ.pop(k, None)
