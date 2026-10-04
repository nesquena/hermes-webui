"""Regression coverage for the #7170 round-7+1 (post-HEAD) profile-scope findings.

Three live defects pinned by this file against HEAD d3fd6546:

A) [P1 security] ``api/gateway_chat.py`` — the gateway worker resolves the
   captured session API key with a TRUTHINESS check
   (``if isinstance(session_api_key, str) and session_api_key``). An
   intentionally EMPTY captured key is the session profile's legitimate
   "no Authorization header" result (issue #7074 anonymous path), but the
   check treats ``""`` as "not captured" and falls back to
   ``_gateway_api_key()``, which reads the AMBIENT process-env key — on a
   multi-profile instance that sends another profile's credential (or, for a
   session whose profile has no key, adds a bearer token the profile never
   configured). An empty captured key must be used verbatim; only a caller
   that captured NO key (``None`` from a legacy direct caller) may fall back.

F) [P1 security] ``api/gateway_chat.py`` — the key and URL fallbacks are
   INDEPENDENT: when the dispatch-captured ``session_base_url`` is ``""``
   (the helper's defensive "could not resolve the session profile's home")
   but ``session_api_key`` was captured, the worker pairs the session
   profile's key with ``_gateway_base_url(cfg)`` which consults the AMBIENT
   ``os.environ`` first — the session profile's bearer token goes to the
   wrong gateway ("Gateway key crosses endpoints"). The two halves of the
   capture are one atomic pair: once either half is captured, BOTH must come
   from the session-owned profile (URL fallback must stay inside the session
   config snapshot, never ambient env).

G) [P1] ``api/config.py`` ``_main_model_request_overrides`` — the
   service-tier gate resolves a missing provider via
   ``resolve_model_provider(gate_model)`` WITHOUT ``config_obj``, i.e. from
   the module-global AMBIENT ``cfg`` even though the caller already handed
   it the session-owning profile snapshot. The gateway worker and the native
   streaming worker both call this with a profile-scoped ``config_data``, so
   a profile whose snapshot carries no ``model.provider`` resolves the
   service-tier gate against another profile's config.

Also pinned here:

H) the dispatch's snapshot-capture failure cleanup must cover EVERY capture
   helper (not just ``_gateway_session_owner_cfg``) — the try/except around
   the capture wraps ``session_api_key``/``session_base_url`` too, and any
   raise must unregister the stream/run state and reset the session.

I) legacy direct/reattach callers that capture NO key/URL pair keep the
   historical ambient resolution (both halves ambient — genuine
   non-profile-scoped behaviour, not a weakening of the captured path).
"""

from collections import OrderedDict
import os
from pathlib import Path

import pytest
import yaml

import api.config as config
import api.gateway_chat as gateway_chat
import api.models as models
import api.routes as routes
import api.streaming as streaming
from api.config import STREAMS, create_stream_channel

_GATEWAY_MODEL = "claude-opus-4-5"


def _write_profile_cfg(home: Path, *, provider: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    home.joinpath("config.yaml").write_text(
        yaml.safe_dump(
            {"model": {"default": _GATEWAY_MODEL, "provider": provider}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_profile_env(
    home: Path, *, api_key: str | None = None, base_url: str | None = None
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    lines = []
    if api_key is not None:
        lines.append(f'HERMES_WEBUI_GATEWAY_API_KEY="{api_key}"')
    if base_url is not None:
        lines.append(f'HERMES_WEBUI_GATEWAY_BASE_URL="{base_url}"')
    if lines:
        home.joinpath(".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_global_stream_registries():
    STREAMS.clear()
    gateway_chat._STREAM_RUN_IDS.clear()
    gateway_chat._STREAM_RUN_LIFECYCLE.clear()
    gateway_chat._STREAM_ENDPOINTS.clear()
    yield
    STREAMS.clear()
    gateway_chat._STREAM_RUN_IDS.clear()
    gateway_chat._STREAM_RUN_LIFECYCLE.clear()
    gateway_chat._STREAM_ENDPOINTS.clear()


@pytest.fixture
def two_profiles_ambient_a_session_b(tmp_path, monkeypatch):
    """Session profile B owns DIFFERENT URL+key than the ambient process env.

    Process env holds profile A's URL and key (what ``_gateway_base_url()`` /
    ``_gateway_api_key()`` read from ``os.environ``). Session profile B's
    .env holds B's url/key. The worker must use B's pair, never A's.
    """
    profile_a_home = tmp_path / "profiles" / "a"
    profile_b_home = tmp_path / "profiles" / "b"
    _write_profile_cfg(profile_a_home, provider="lmstudio")
    _write_profile_cfg(profile_b_home, provider="anthropic")
    _write_profile_env(
        profile_a_home,
        api_key="KEY_FOR_PROFILE_A",
        base_url="http://profile-a.invalid:8642",
    )
    _write_profile_env(
        profile_b_home,
        api_key="KEY_FOR_PROFILE_B",
        base_url="http://profile-b.invalid:8642",
    )

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(profile_a_home / "config.yaml"))
    config.reload_config()
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://profile-a.invalid:8642")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "KEY_FOR_PROFILE_A")
    monkeypatch.delenv("API_SERVER_KEY", raising=False)

    monkeypatch.setattr(
        models, "_get_profile_home", lambda profile: profile_b_home
    )

    yield profile_a_home, profile_b_home, monkeypatch

    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_API_KEY", raising=False)
    config.reload_config()


@pytest.fixture
def anonymous_profile_b_no_key(tmp_path, monkeypatch):
    """Session profile B has NO gateway key; ambient holds profile A's key."""
    profile_a_home = tmp_path / "profiles" / "a"
    profile_b_home = tmp_path / "profiles" / "b"
    _write_profile_cfg(profile_a_home, provider="lmstudio")
    _write_profile_cfg(profile_b_home, provider="anthropic")
    # Profile A's .env AND the ambient process env carry A's key + URL.
    _write_profile_env(
        profile_a_home,
        api_key="KEY_FOR_PROFILE_A",
        base_url="http://profile-a.invalid:8642",
    )
    # Profile B's .env carries ONLY a URL — NO gateway key at all.
    _write_profile_env(profile_b_home, base_url="http://profile-b.invalid:8642")

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(profile_a_home / "config.yaml"))
    config.reload_config()
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://profile-a.invalid:8642")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "KEY_FOR_PROFILE_A")
    monkeypatch.delenv("API_SERVER_KEY", raising=False)

    monkeypatch.setattr(
        models, "_get_profile_home", lambda profile: profile_b_home
    )
    yield profile_a_home, profile_b_home, monkeypatch

    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_API_KEY", raising=False)
    config.reload_config()


def _session_browser_ready(session_dir, monkeypatch) -> models.Session:
    s = models.new_session(profile="b")
    s.pending_user_message = "hello"
    s.pending_attachments = []
    s.pending_started_at = 1.0
    s.save()
    return s


def _gateway_worker_network(monkeypatch, captured: dict):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"done"}}]}\n\n'
            yield b"data: [DONE]\n\n"

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


# ---------------------------------------------------------------------------
# Finding E: an intentionally empty captured session key is "no Authorization",
# NOT a licence to fall back to the ambient process key.
# ---------------------------------------------------------------------------


def test_dispatch_captures_empty_key_for_anonymous_session_profile(
    anonymous_profile_b_no_key,
):
    """The dispatch-side capture is already correct: a profile with no key
    must be handed to the worker as ``session_api_key == ""`` — the worker's
    job is to keep it empty."""
    _, profile_b_home, _ = anonymous_profile_b_no_key
    s = models.new_session(profile="b")
    s.save()
    captured = gateway_chat._gateway_session_api_key(s)
    assert captured == "", (
        f"_gateway_session_api_key returned {captured!r}; the session profile "
        "has no gateway key, so an empty capture is the correct anonymous "
        "result."
    )
    assert profile_b_home.joinpath(".env").exists()


def test_gateway_worker_empty_captured_key_sends_no_authorization_header(
    anonymous_profile_b_no_key,
):
    """End-to-end: profile B has NO gateway key; the ambient process env holds
    profile A's key. The dispatch captures ``session_api_key == ""`` and the
    worker must send NO ``Authorization`` header — the ambient key
    (KEY_FOR_PROFILE_A) must NEVER be sent on profile B's behalf."""
    _, profile_b_home, monkeypatch = anonymous_profile_b_no_key

    session_dir = profile_b_home.parent / "sessions_anon"
    session_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    captured: dict = {}
    _gateway_worker_network(monkeypatch, captured)

    s = _session_browser_ready(profile_b_home, monkeypatch)
    stream_id = "stream-empty-key"
    s.active_stream_id = stream_id
    STREAMS[stream_id] = create_stream_channel()

    session_cfg = gateway_chat._gateway_session_owner_cfg(s)
    session_api_key = gateway_chat._gateway_session_api_key(s)
    session_base_url = gateway_chat._gateway_session_base_url(s)
    assert session_api_key == ""
    assert session_base_url == "http://profile-b.invalid:8642"

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
        session_base_url=session_base_url,
    )

    headers = captured.get("headers", {})
    assert "Authorization" not in headers, (
        f"Outgoing request carried Authorization={headers.get('Authorization')!r}; "
        "the session profile has no gateway key, so the worker must send NO "
        "Authorization header. The ambient process-env key "
        "(KEY_FOR_PROFILE_A) leaked into the anonymous profile's request."
    )
    url = captured.get("url", "")
    assert url.startswith("http://profile-b.invalid:8642"), (
        f"Outgoing URL is {url!r}; expected the session profile's gateway."
    )
    assert not url.startswith("http://profile-a.invalid:8642")


# ---------------------------------------------------------------------------
# Finding F: URL and key are one atomic pair — a failed URL capture must never
# pair the session key with the ambient process-env URL.
# ---------------------------------------------------------------------------


def test_gateway_worker_failed_url_capture_never_mixes_with_ambient_url(
    two_profiles_ambient_a_session_b,
):
    """The dispatch captured the session key (KEY_FOR_PROFILE_B) but the
    session base-URL helper returned ``""`` (defensive failure: profile home
    could not be determined). The worker must NOT pair B's key with the
    ambient env URL (http://profile-a.invalid:8642) — that is the greptile
    "Gateway key crosses endpoints" leak. The URL must fall back inside the
    session snapshot (default 127.0.0.1:8642), never the ambient env."""
    _, profile_b_home, monkeypatch = two_profiles_ambient_a_session_b

    session_dir = profile_b_home.parent / "sessions_mix"
    session_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    captured: dict = {}
    _gateway_worker_network(monkeypatch, captured)

    s = _session_browser_ready(session_dir, monkeypatch)
    stream_id = "stream-url-mix"
    s.active_stream_id = stream_id
    STREAMS[stream_id] = create_stream_channel()

    session_cfg = gateway_chat._gateway_session_owner_cfg(s)
    session_api_key = gateway_chat._gateway_session_api_key(s)
    # Simulate the helper's defensive failure: session base URL unknown.
    session_base_url = ""

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
        session_base_url=session_base_url,
    )

    url = captured.get("url", "")
    assert not url.startswith("http://profile-a.invalid:8642"), (
        f"Outgoing URL is {url!r}; the AMBIENT process-env URL was paired "
        "with the session profile's captured key — finding F is live. The "
        "session profile's bearer token must never be sent to the ambient "
        "profile's gateway."
    )
    # Same-snapshot fallback: the session cfg snapshot carries no
    # webui_gateway_base_url, so the profile-scoped default applies.
    assert url.startswith("http://127.0.0.1:8642"), (
        f"Outgoing URL is {url!r}; expected the session snapshot's own "
        "default URL when no URL was captured."
    )
    auth = captured.get("headers", {}).get("Authorization", "")
    assert auth == "Bearer KEY_FOR_PROFILE_B"
    assert "KEY_FOR_PROFILE_A" not in str(captured.get("headers", {}))


# ---------------------------------------------------------------------------
# Finding I: legacy direct/reattach callers keep the ambient pair (both
# halves together). The atomic-pair rule only applies once a capture exists.
# ---------------------------------------------------------------------------


def test_gateway_worker_legacy_direct_caller_keeps_ambient_pair(
    two_profiles_ambient_a_session_b,
):
    """A direct worker invocation that captured NO key/URL pair (both None)
    keeps the historical ambient resolution — both halves from the
    environment together, never a capture half."""
    _, profile_b_home, monkeypatch = two_profiles_ambient_a_session_b

    session_dir = profile_b_home.parent / "sessions_legacy"
    session_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    captured: dict = {}
    _gateway_worker_network(monkeypatch, captured)

    s = _session_browser_ready(session_dir, monkeypatch)
    stream_id = "stream-legacy-pair"
    s.active_stream_id = stream_id
    STREAMS[stream_id] = create_stream_channel()

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "hello",
        _GATEWAY_MODEL,
        str(session_dir),
        stream_id,
        [],
        model_provider="anthropic",
        # No capture: legacy direct caller.
        session_cfg=None,
        session_api_key=None,
        session_base_url=None,
    )

    url = captured.get("url", "")
    auth = captured.get("headers", {}).get("Authorization", "")
    assert url.startswith("http://profile-a.invalid:8642"), (
        f"Legacy direct caller sent {url!r}; the historical ambient pair "
        "(env URL + env key) must be preserved when NOTHING was captured."
    )
    assert auth == "Bearer KEY_FOR_PROFILE_A"


# ---------------------------------------------------------------------------
# Finding G: _main_model_request_overrides must scope its provider lookup to
# the profile snapshot it was handed.
# ---------------------------------------------------------------------------


def test_main_model_request_overrides_scopes_provider_lookup(tmp_path, monkeypatch):
    """When ``_main_model_request_overrides`` is handed a profile snapshot
    with no ``model.provider``, the service-tier gate's provider lookup must
    use that snapshot (``config_obj``), never the module-global ambient
    ``cfg``. The gateway/native workers call this with a profile-scoped
    snapshot — an ambient read leaks another profile's routing."""
    ambient_home = tmp_path / "ambient"
    ambient_home.mkdir(parents=True, exist_ok=True)
    ambient_home.joinpath("config.yaml").write_text(
        yaml.safe_dump(
            {"model": {"default": "gpt-5.4", "provider": "lmstudio"}},
            sort_keys=False,
        )
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(ambient_home / "config.yaml"))
    config.reload_config()

    scoped_config = {"model": {"default": "gpt-5.4"}}  # intentionally no provider

    seen: dict = {}
    real_resolve = config.resolve_model_provider

    def spy_resolve(model_id, **kwargs):
        seen["call"] = (model_id, kwargs.get("config_obj"))
        return real_resolve(model_id, **kwargs)

    monkeypatch.setattr(config, "resolve_model_provider", spy_resolve)

    config._main_model_request_overrides(
        scoped_config,
        effective_model="gpt-5.4",
        effective_provider=None,
    )

    assert seen.get("call") is not None, "resolve_model_provider was not called"
    called_model, called_config = seen["call"]
    assert called_model == "gpt-5.4"
    assert called_config is scoped_config, (
        f"resolve_model_provider was called with config_obj={called_config!r}; "
        "expected the profile snapshot passed to _main_model_request_overrides. "
        "With the bug, the lookup uses the ambient module-global cfg "
        "(lmstudio here) instead of the caller's snapshot."
    )


# ---------------------------------------------------------------------------
# Finding H: dispatch cleanup covers EVERY capture helper.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_dispatch_env(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    os.environ["HERMES_WEBUI_CHAT_BACKEND"] = "gateway"
    os.environ["HERMES_WEBUI_GATEWAY_BASE_URL"] = "http://gateway.local"
    os.environ["HERMES_WEBUI_GATEWAY_API_KEY"] = "test-key"
    yield monkeypatch, session_dir
    for k in (
        "HERMES_WEBUI_CHAT_BACKEND",
        "HERMES_WEBUI_GATEWAY_BASE_URL",
        "HERMES_WEBUI_GATEWAY_API_KEY",
    ):
        os.environ.pop(k, None)


def test_dispatch_api_key_capture_failure_unregisters_stream_state(
    isolated_dispatch_env, tmp_path
):
    """A raise from ANY capture helper (here ``_gateway_session_api_key``,
    after the owner-cfg capture succeeded) must re-run the same cleanup:
    stream/run/goal state released, session's active_stream_id reset, and
    the original error surfaced."""
    monkeypatch, session_dir = isolated_dispatch_env

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated api_key capture failure")

    monkeypatch.setattr(gateway_chat, "_gateway_session_api_key", _raise)

    s = models.new_session(profile="default")
    s.pending_user_message = "hello"
    s.pending_attachments = []
    s.pending_started_at = 1.0
    s.save()

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
    assert raised is not None, "Dispatch should have raised"
    assert "simulated api_key capture failure" in str(raised)

    assert len(STREAMS) == 0, f"STREAMS non-empty after failed dispatch: {list(STREAMS)!r}"
    assert len(gateway_chat._STREAM_RUN_LIFECYCLE) == 0, (
        f"_STREAM_RUN_LIFECYCLE non-empty after failed dispatch: "
        f"{list(gateway_chat._STREAM_RUN_LIFECYCLE)!r}"
    )
    s_loaded = models.get_session(s.session_id)
    assert s_loaded is not None
    assert s_loaded.active_stream_id is None, (
        f"active_stream_id {s_loaded.active_stream_id!r} strand stranded the session"
    )