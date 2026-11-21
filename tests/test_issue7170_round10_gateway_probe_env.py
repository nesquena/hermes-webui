"""#7170 round-10 P1: gateway worker scoped probes must see the session profile's env.

The detached gateway worker thread has NO thread-local owner env
(``api.config._thread_ctx.env`` is empty there), so the LM Studio reasoning
probe — which reads ``LM_API_KEY`` through that thread-local env on scoped
lookups — went KEYLESS in the worker. The probe 401'd, the capability ladder
came back empty, and ``xhigh`` was preserved instead of being demoted to
``high`` (greptile P1).

Fix: the dispatch captures the session-owning profile's env
(``_gateway_session_env`` — the same construction as the ``session_api_key``
/ ``session_base_url`` captures: ambient loaded-profile keys stripped, the
session profile's filtered runtime env overlaid) and hands it to the worker
as ``session_env``; the worker pins it around its reasoning-effort
resolution (save/restore pattern like ``_gateway_endpoint_for_profile``).
Legacy direct callers fall back to deriving it from the session profile
home.

Profile isolation is unchanged: a session profile with no key of its own
stays keyless — the ambient (process-active) profile's credential is never
borrowed (fail-closed, round-2 finding 2).
"""

import json
from collections import OrderedDict
from pathlib import Path
import threading

import pytest
import yaml

import api.config as config
import api.gateway_chat as gateway_chat
import api.models as models
import api.routes as routes
import api.streaming as streaming
from api.config import STREAMS, create_stream_channel


_MODEL = "b-model"
_LM_BASE_URL = "http://b-lmstudio:1234/v1"
_KEY_B = "KEY_FOR_PROFILE_B"
_KEY_A = "KEY_FOR_PROFILE_A"


class _ProbeRecorder:
    """Stand-in for ``_lmstudio_model_reasoning_options`` recording calls."""

    def __init__(self, options):
        self._options = list(options)
        self.calls: list[dict] = []

    def __call__(self, model, base_url, *, api_key=None, timeout=5.0):
        self.calls.append(
            {
                "model": model,
                "base_url": base_url,
                "api_key": api_key,
            }
        )
        return list(self._options)


class _KeyGatedProbe(_ProbeRecorder):
    """Record calls AND simulate the real probe's failure mode: a keyless
    request 401s and returns no options (unknown ladder), a keyed request
    returns the endpoint's ladder."""

    def __call__(self, model, base_url, *, api_key=None, timeout=5.0):
        super().__call__(model, base_url, api_key=api_key, timeout=timeout)
        if not api_key:
            return []
        return list(self._options)


def _write_profile_cfg(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    home.joinpath("config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": _MODEL,
                    "provider": "lmstudio",
                    "base_url": _LM_BASE_URL,
                },
                "providers": {"lmstudio": {"base_url": _LM_BASE_URL}},
                # Stored xhigh must coerce down to high once the probe can
                # see the session profile's key (the greptile P1 symptom).
                "agent": {"reasoning_effort": "xhigh"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_profile_env(home: Path, *, lm_key: str | None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    if lm_key is None:
        return
    home.joinpath(".env").write_text(
        f'LM_API_KEY="{lm_key}"\n', encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def _clear_global_stream_registries():
    """Reset module-global stream/run state between tests."""
    STREAMS.clear()
    gateway_chat._STREAM_RUN_IDS.clear()
    gateway_chat._STREAM_RUN_LIFECYCLE.clear()
    yield
    STREAMS.clear()
    gateway_chat._STREAM_RUN_IDS.clear()
    gateway_chat._STREAM_RUN_LIFECYCLE.clear()


@pytest.fixture
def two_profiles_lmstudio(tmp_path, monkeypatch):
    """Ambient profile A's key in the process env; session profile B's own key.

    A = the process-active profile whose ``LM_API_KEY`` the ambient
    ``os.environ`` carries (and whose mirrored env keys land in
    ``api.profiles._loaded_profile_env_keys``).
    B = the profile owning the session under test, with its own key in its
    own ``.env``.
    """
    import api.profiles as profiles_mod

    profile_a_home = tmp_path / "profiles" / "a"
    profile_b_home = tmp_path / "profiles" / "b"
    _write_profile_cfg(profile_a_home)
    _write_profile_cfg(profile_b_home)
    _write_profile_env(profile_a_home, lm_key=None)
    _write_profile_env(profile_b_home, lm_key=_KEY_B)

    # Pin the ambient resolver to profile A.
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(profile_a_home / "config.yaml"))
    config.reload_config()
    # The process env carries profile A's mirrored key, and A's key is
    # registered as a loaded-profile key — exactly the multi-profile shape
    # that must never leak into B's request.
    monkeypatch.setenv("LM_API_KEY", _KEY_A)
    monkeypatch.setenv("LMSTUDIO_API_KEY", _KEY_A)
    monkeypatch.setattr(
        profiles_mod, "_loaded_profile_env_keys", {"LM_API_KEY", "LMSTUDIO_API_KEY"}
    )

    # Pin session profile -> home resolution to profile B.
    import api.models as _models_mod

    monkeypatch.setattr(
        _models_mod, "_get_profile_home", lambda profile: profile_b_home
    )

    yield profile_b_home

    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.delenv("LM_API_KEY", raising=False)
    monkeypatch.delenv("LMSTUDIO_API_KEY", raising=False)
    config.reload_config()


def _prepare_session(tmp_path, monkeypatch, *, profile="b"):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    s = models.new_session(profile=profile)
    s.pending_user_message = "Say hello"
    s.pending_attachments = []
    s.pending_started_at = 123
    s.save()
    return s


def _install_fake_transport(monkeypatch, captured, *, probe):
    monkeypatch.setattr(config, "_lmstudio_model_reasoning_options", probe)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"done"}}]}\n\n'
            yield b"data: [DONE]\n\n"

    def fake_urlopen(req, timeout=0):
        captured["body"] = req.data.decode("utf-8")
        return FakeResponse()

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "secret-token")
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        streaming, "_load_webui_prefill_context", lambda c: {"messages": []}
    )
    monkeypatch.setattr(
        streaming, "_prefill_messages_with_webui_context", lambda ctx, c: []
    )


def _run_worker_with_cleared_thread_env(s, stream_id, workspace, probe):
    """Run the worker as it would on a detached thread: no thread-local env.

    The detached worker thread starts with an EMPTY ``_thread_ctx`` (thread
    locals do not travel), so clearing it here is faithful — and the worker
    must supply the session env itself (dispatch-captured ``session_env`` or
    its own derivation), never inherit anything from the caller.
    """
    from api.config import _thread_ctx

    prev_env = getattr(_thread_ctx, "env", {})
    prev_block = bool(getattr(_thread_ctx, "block_process_env_fallback", False))
    _thread_ctx.env = {}
    _thread_ctx.block_process_env_fallback = False
    try:
        gateway_chat._run_gateway_chat_streaming(
            s.session_id,
            "Say hello",
            _MODEL,
            workspace,
            stream_id,
            [],
            model_provider="lmstudio",
        )
    finally:
        _thread_ctx.env = prev_env
        _thread_ctx.block_process_env_fallback = prev_block


# ---------------------------------------------------------------------------
# Fix: the worker's scoped LM Studio probe must see the session profile's key
# ---------------------------------------------------------------------------


def test_gateway_session_env_resolves_session_profile_lm_key(
    two_profiles_lmstudio, tmp_path, monkeypatch
):
    """The dispatch-side capture reads B's key, never the ambient A key."""
    _ = two_profiles_lmstudio  # fixture setup (two-profile env + key registration)
    s = _prepare_session(tmp_path, monkeypatch)

    env = gateway_chat._gateway_session_env(s)

    assert env.get("LM_API_KEY") == _KEY_B, (
        f"_gateway_session_env captured {env.get('LM_API_KEY')!r}; expected "
        f"the session profile's {_KEY_B}, never the ambient {_KEY_A}."
    )


def test_gateway_worker_probe_uses_session_profile_lm_key(
    two_profiles_lmstudio, tmp_path, monkeypatch
):
    """End-to-end: the worker's LM Studio probe carries the SESSION profile's
    key and xhigh coerce down to high (the greptile P1 symptom)."""
    _ = two_profiles_lmstudio
    captured: dict = {}
    probe = _ProbeRecorder(["low", "high"])  # B's ladder tops out at high
    _install_fake_transport(monkeypatch, captured, probe=probe)

    s = _prepare_session(tmp_path, monkeypatch)
    stream_id = "stream-probe-session-key"
    s.active_stream_id = stream_id
    STREAMS[stream_id] = create_stream_channel()

    # Detached-thread shape: empty thread-local env, no session_cfg /
    # session_api_key / session_base_url / session_env (legacy direct caller).
    _run_worker_with_cleared_thread_env(s, stream_id, str(tmp_path), probe)

    assert probe.calls, "the LM Studio probe was never invoked"
    last = probe.calls[-1]
    assert last["api_key"] == _KEY_B, (
        f"worker probe carried api_key={last['api_key']!r}; expected the "
        f"session profile's {_KEY_B}. Without the env capture the probe went "
        "keyless (401) and xhigh was not demoted."
    )
    payload = json.loads(captured["body"])
    assert payload["reasoning_effort"] == "high", (
        f"worker sent reasoning_effort={payload['reasoning_effort']!r}; with "
        "the profile key the probe ladder is known, so xhigh must degrade "
        "to high."
    )


def test_gateway_worker_probe_stays_keyless_for_anonymous_session_profile(
    two_profiles_lmstudio, tmp_path, monkeypatch
):
    """Isolation: a session profile with NO key of its own probes keyless —
    the ambient A key is never borrowed (fail-closed, round-2 finding 2)."""
    import api.profiles as profiles_mod

    profile_b_home = two_profiles_lmstudio
    # Drop B's own key: the session profile now configures no credential.
    b_env = profile_b_home / ".env"
    if b_env.exists():
        b_env.unlink()

    captured: dict = {}
    probe = _KeyGatedProbe(["low", "high"])
    _install_fake_transport(monkeypatch, captured, probe=probe)

    s = _prepare_session(tmp_path, monkeypatch)
    stream_id = "stream-probe-anonymous"
    s.active_stream_id = stream_id
    STREAMS[stream_id] = create_stream_channel()

    _run_worker_with_cleared_thread_env(s, stream_id, str(tmp_path), probe)

    assert probe.calls, "the LM Studio probe was never invoked"
    last = probe.calls[-1]
    assert last["api_key"] is None, (
        f"anonymous session probe carried api_key={last['api_key']!r} — the "
        "ambient profile A's LM_API_KEY leaked into profile B's endpoint. "
        "A keyless probe is the fail-closed contract."
    )
    payload = json.loads(captured["body"])
    # Keyless probe -> unknown ladder -> the configured effort is preserved.
    assert payload["reasoning_effort"] == "xhigh", (
        f"anonymous session sent reasoning_effort={payload['reasoning_effort']!r}; "
        "without a key the ladder is unknown, so xhigh stays as configured."
    )
    assert profiles_mod._loaded_profile_env_keys, "fixture precondition lost"


def test_gateway_worker_detached_thread_probe_uses_session_profile_key(
    two_profiles_lmstudio, tmp_path, monkeypatch
):
    """The real detached-thread shape: run the worker on a NEW thread whose
    thread-local env is empty by definition."""
    _ = two_profiles_lmstudio
    captured: dict = {}
    probe = _ProbeRecorder(["low", "high"])
    _install_fake_transport(monkeypatch, captured, probe=probe)

    s = _prepare_session(tmp_path, monkeypatch)
    stream_id = "stream-probe-real-thread"
    s.active_stream_id = stream_id
    STREAMS[stream_id] = create_stream_channel()

    result: dict = {}

    def run():
        try:
            gateway_chat._run_gateway_chat_streaming(
                s.session_id,
                "Say hello",
                _MODEL,
                str(tmp_path),
                stream_id,
                [],
                model_provider="lmstudio",
            )
            result["ok"] = True
        except Exception as exc:  # pragma: no cover - surfaces via assertion
            result["error"] = exc

    worker_thread = threading.Thread(target=run, daemon=True)
    worker_thread.start()
    worker_thread.join(timeout=60)
    assert not worker_thread.is_alive(), "worker thread did not finish"
    assert result.get("ok"), f"worker failed on detached thread: {result.get('error')!r}"

    assert probe.calls, "the LM Studio probe was never invoked"
    assert probe.calls[-1]["api_key"] == _KEY_B, (
        f"detached-thread probe carried api_key={probe.calls[-1]['api_key']!r}; "
        "the fresh thread has no thread-local env, so the worker's own "
        "capture must supply the session profile's key."
    )
    payload = json.loads(captured["body"])
    assert payload["reasoning_effort"] == "high"


def test_dispatch_captures_session_env_for_gateway_worker(
    two_profiles_lmstudio, tmp_path, monkeypatch
):
    """The /api/chat/start dispatch hands the worker ``session_env`` — the
    session profile's env captured on the request thread."""
    _ = two_profiles_lmstudio

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
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
    s.pending_user_message = "Say hello"
    s.pending_attachments = []
    s.pending_started_at = 123
    s.title = "Profile B"
    s.messages = [{"role": "user", "content": "Say hello"}]
    s.save()

    response = routes._start_chat_stream_for_session(
        s,
        msg="Say hello",
        attachments=[],
        workspace=str(tmp_path),
        model=_MODEL,
        model_provider="lmstudio",
        external_runtime_owned=True,
    )
    assert response and "stream_id" in response

    thread_payload = captured_thread.get("kwargs") or {}
    worker_kwargs = thread_payload.get("kwargs") or {}
    session_env = worker_kwargs.get("session_env")
    assert isinstance(session_env, dict), (
        "the gateway worker must receive the dispatch-captured session env"
    )
    assert session_env.get("LM_API_KEY") == _KEY_B, (
        f"dispatch captured LM_API_KEY={session_env.get('LM_API_KEY')!r}; "
        f"expected the session profile's {_KEY_B}, never ambient {_KEY_A}."
    )
