"""Regression coverage for session-owned composer reasoning effort."""

from __future__ import annotations

import io
import json
import tempfile
import urllib.error
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import patch

import pytest

import api.config as config
import api.gateway_chat as gateway_chat
import api.models as models
from api import profiles
from api.config import resolve_session_reasoning_effort
from api.gateway_chat import _gateway_reasoning_effort_for_request
from api.models import Session
from api.routes import handle_get, handle_post


REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reload_config_after_test():
    # Set up before (and torn down after) monkeypatch, so the cache is rebuilt
    # from the restored environment instead of a deleted temp config.
    yield
    config.reload_config()


class _DummyHandler:
    client_address = ("127.0.0.1", 12345)

    def __init__(self, body: dict | None = None, *, command: str = "GET"):
        raw = json.dumps(body or {}).encode("utf-8")
        self.command = command
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = tempfile.SpooledTemporaryFile()
        self.rfile.write(raw)
        self.rfile.seek(0)
        self.wfile = tempfile.SpooledTemporaryFile()
        self.status = None

    def send_response(self, code: int):
        self.status = code

    def send_header(self, _key: str, _value: str):
        pass

    def end_headers(self):
        pass

    def payload(self) -> dict:
        self.wfile.seek(0)
        return json.loads(self.wfile.read().decode("utf-8"))


def test_session_projection_carries_reasoning_effort():
    session = Session(model="gpt-5", model_provider="openai", reasoning_effort="high")
    assert session.compact()["reasoning_effort"] == "high"


def test_session_reasoning_effort_survives_save_reload(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")

    session = Session(
        session_id="reasoning-session",
        model="gpt-5",
        model_provider="openai",
        reasoning_effort="xhigh",
    )
    session.save(touch_updated_at=False)

    assert Session.load("reasoning-session").reasoning_effort == "xhigh"


def test_new_session_snapshots_profile_reasoning_effort(tmp_path):
    with patch("api.models._profile_default_reasoning_effort", return_value="high"):
        session = models.new_session(workspace=str(tmp_path), profile="work")

    try:
        assert session.reasoning_effort == "high"
    finally:
        models.SESSIONS.pop(session.session_id, None)


def test_session_effort_is_authoritative_and_legacy_sessions_use_profile_default():
    cfg = {"agent": {"reasoning_effort": "high"}}
    assert resolve_session_reasoning_effort(cfg, session_effort="low") == "low"
    assert resolve_session_reasoning_effort(cfg, session_effort="") == ""
    assert resolve_session_reasoning_effort(cfg, session_effort=None) == "high"
    assert _gateway_reasoning_effort_for_request(cfg, session_effort="low") == "low"


def test_reasoning_get_uses_session_override():
    session = SimpleNamespace(reasoning_effort="low")
    captured = {}

    def status(**kwargs):
        captured.update(kwargs)
        return {"reasoning_effort": kwargs.get("effort_override", "high")}

    handler = _DummyHandler()
    with (
        patch("api.routes._session_id_visible_to_request_profile", return_value=True),
        patch("api.routes.get_session", return_value=session),
        patch("api.routes.get_reasoning_status", side_effect=status),
    ):
        handle_get(
            handler,
            urlparse("/api/reasoning?model=gpt-5&provider=openai&session_id=session-b"),
        )

    assert handler.status == 200
    assert handler.payload()["reasoning_effort"] == "low"
    assert captured["effort_override"] == "low"


def test_reasoning_get_rejects_session_outside_active_profile():
    handler = _DummyHandler()

    def reject(other_handler, _session_id):
        other_handler.send_response(409)
        other_handler.end_headers()
        other_handler.wfile.write(
            json.dumps({"error": "Session belongs to a different profile"}).encode()
        )
        return False

    with (
        patch("api.routes._session_id_visible_to_request_profile", side_effect=reject),
        patch("api.routes.get_reasoning_status") as status,
    ):
        handle_get(handler, urlparse("/api/reasoning?session_id=other-profile"))

    assert handler.status == 409
    assert handler.payload()["error"] == "Session belongs to a different profile"
    status.assert_not_called()


def _post_reasoning(body, session, *, save_error=None, visible=True, config_error=None):
    events = []

    class _MutationLock:
        def __enter__(self):
            events.append("lock-enter")

        def __exit__(self, *_exc):
            events.append("lock-exit")

    def save():
        events.append("save")
        if save_error is not None:
            raise save_error

    def reject(other_handler, _session_id):
        other_handler.send_response(409)
        other_handler.end_headers()
        other_handler.wfile.write(
            json.dumps({"error": "Session belongs to a different profile"}).encode()
        )
        return False

    session.save = lambda touch_updated_at=True: save()
    handler = _DummyHandler(body, command="POST")
    with (
        patch(
            "api.routes._session_id_visible_to_request_profile",
            side_effect=(lambda *_a, **_k: True) if visible else reject,
        ),
        patch("api.routes._get_or_materialize_session", return_value=session),
        patch("api.routes._get_session_agent_lock", return_value=_MutationLock()),
        patch(
            "api.routes.write_reasoning_effort",
            side_effect=lambda effort, *_a: (
                events.append("config"),
                (_ for _ in ()).throw(config_error) if config_error else None,
                effort,
            )[2],
        ),
        patch(
            "api.routes.get_reasoning_status",
            side_effect=lambda **_kw: (
                events.append("status"),
                {"reasoning_effort": "low", "supported_efforts": ["low", "high"]},
            )[1],
        ),
        patch("api.config._evict_session_agent") as evict,
    ):
        try:
            handle_post(handler, urlparse("/api/reasoning"))
        except Exception as exc:  # surfaced as a 500 by the real server
            events.append(type(exc).__name__)
    return handler, events, evict


def test_reasoning_post_saves_session_before_profile_default_without_eviction():
    session = SimpleNamespace(reasoning_effort="high")
    handler, events, evict = _post_reasoning(
        {"effort": "LOW", "model": "gpt-5", "provider": "openai", "session_id": "session-b"},
        session,
    )

    assert handler.status == 200
    assert handler.payload()["reasoning_effort"] == "low"
    assert session.reasoning_effort == "low"
    # Session and profile writes form one serialized step per session; the
    # capability lookup (possible network I/O) runs after the lock is released.
    assert events == ["lock-enter", "save", "config", "lock-exit", "status"]
    # reasoning_config is part of the agent cache signature, so the next turn
    # rebuilds the agent without a synchronous lifecycle commit here.
    evict.assert_not_called()


def test_reasoning_post_failed_session_save_leaves_profile_default_untouched():
    session = SimpleNamespace(reasoning_effort="high")
    _handler, events, _evict = _post_reasoning(
        {"effort": "low", "session_id": "session-b"}, session, save_error=OSError("disk full"),
    )
    assert "config" not in events
    assert events[-1] == "OSError"
    # The live session is rolled back so it still matches its sidecar.
    assert session.reasoning_effort == "high"


def test_reasoning_post_failed_real_save_keeps_live_equal_to_reloaded(
    isolated_reasoning_profiles, tmp_path
):
    session = Session(
        session_id="atomic-save", profile="default", model="gpt-5",
        model_provider="openai", workspace=str(tmp_path), reasoning_effort="high",
        messages=[{"role": "user", "content": "hi"}],
    )
    session.save(touch_updated_at=False)
    models.SESSIONS[session.session_id] = session
    handler = _DummyHandler({"effort": "low", "session_id": "atomic-save"}, command="POST")
    with (
        patch("api.models._safe_replace", side_effect=OSError("disk full")),
        patch("api.routes.write_reasoning_effort") as set_effort,
        patch("api.routes._session_id_visible_to_request_profile", return_value=True),
    ):
        try:
            handle_post(handler, urlparse("/api/reasoning"))
        except OSError:
            pass
    set_effort.assert_not_called()
    assert session.reasoning_effort == "high"
    assert Session.load("atomic-save").reasoning_effort == "high"


def test_reasoning_post_failed_profile_write_restores_session():
    session = SimpleNamespace(reasoning_effort="high")
    _handler, events, _evict = _post_reasoning(
        {"effort": "low", "session_id": "session-b"}, session,
        config_error=OSError("read-only config"),
    )
    assert events == ["lock-enter", "save", "config", "save", "lock-exit", "OSError"]
    assert session.reasoning_effort == "high"


def test_reasoning_post_read_only_session_keeps_master_profile_save():
    events = []
    handler = _DummyHandler({"effort": "low", "session_id": "readonly"}, command="POST")
    with (
        patch("api.routes._session_id_visible_to_request_profile", return_value=True),
        patch(
            "api.routes._get_or_materialize_session",
            side_effect=PermissionError("read-only imported session"),
        ),
        patch(
            "api.routes.set_reasoning_effort",
            side_effect=lambda e, **_kw: events.append(e) or {"reasoning_effort": e},
        ),
    ):
        handle_post(handler, urlparse("/api/reasoning"))
    assert handler.status == 200
    assert events == ["low"]


def test_reasoning_post_reports_this_sessions_effort_not_shared_default():
    session = SimpleNamespace(reasoning_effort="high", save=lambda **_kw: None)
    captured = {}

    def status(**kwargs):
        captured.update(kwargs)
        # Another chat rewrote the shared profile default after our lock.
        return {"reasoning_effort": kwargs.get("effort_override", "xhigh")}

    handler = _DummyHandler({"effort": "low", "session_id": "session-a"}, command="POST")
    with (
        patch("api.routes._session_id_visible_to_request_profile", return_value=True),
        patch("api.routes._get_or_materialize_session", return_value=session),
        patch("api.routes.write_reasoning_effort"),
        patch("api.routes.get_reasoning_status", side_effect=status),
    ):
        handle_post(handler, urlparse("/api/reasoning"))
    assert handler.payload()["reasoning_effort"] == "low"
    assert captured["effort_override"] == "low"


def test_reasoning_post_failed_rollback_save_surfaces_profile_error():
    session = SimpleNamespace(reasoning_effort="high")
    saves = []

    def save(touch_updated_at=True):
        saves.append(touch_updated_at)
        if len(saves) == 2:
            raise OSError("rollback failed")

    session.save = save
    handler = _DummyHandler({"effort": "low", "session_id": "session-b"}, command="POST")
    raised = None
    with (
        patch("api.routes._session_id_visible_to_request_profile", return_value=True),
        patch("api.routes._get_or_materialize_session", return_value=session),
        patch("api.routes.write_reasoning_effort", side_effect=PermissionError("config ro")),
    ):
        try:
            handle_post(handler, urlparse("/api/reasoning"))
        except Exception as exc:
            raised = exc
    # The original profile-write error wins; memory matches the sidecar ("low").
    assert isinstance(raised, PermissionError)
    assert session.reasoning_effort == "low"
    assert saves == [False, False]


def test_reasoning_post_rejects_session_outside_active_profile():
    # Enforced by the generic body session_id guard ahead of routing.
    session = SimpleNamespace(reasoning_effort="high")
    handler, events, _evict = _post_reasoning(
        {"effort": "low", "session_id": "other-profile"}, session, visible=False,
    )
    assert handler.status == 409
    assert events == []
    assert session.reasoning_effort == "high"


def test_reasoning_post_rejects_invalid_effort_before_session_write():
    session = SimpleNamespace(reasoning_effort="high")
    handler, events, _evict = _post_reasoning(
        {"effort": "turbo", "session_id": "session-b"}, session,
    )
    assert handler.status == 400
    assert events == []
    assert session.reasoning_effort == "high"


def test_reasoning_post_does_not_mutate_profile_for_unknown_session():
    handler = _DummyHandler(
        {"effort": "low", "session_id": "missing"}, command="POST"
    )
    with (
        patch("api.routes._get_or_materialize_session", side_effect=KeyError),
        patch("api.routes.write_reasoning_effort") as set_effort,
    ):
        handle_post(handler, urlparse("/api/reasoning"))

    assert handler.status == 404
    assert handler.payload()["error"] == "Session not found"
    set_effort.assert_not_called()


def test_runtime_paths_prefer_session_reasoning_effort():
    streaming = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
    gateway = (REPO / "api" / "gateway_chat.py").read_text(encoding="utf-8")
    assert "resolve_session_reasoning_effort(" in streaming
    # _session_meta is pre-bound so a missing sidecar cannot NameError into
    # dropping the profile effort; legacy sessions read the isolated config.
    assert "            _session_meta = None\n" in streaming
    # The live session is the source, matching the Gateway worker.
    assert "_session_effort = getattr(s, 'reasoning_effort', None)" in streaming
    assert "getattr(_session_meta, 'reasoning_effort'" not in streaming
    assert "effective_session_reasoning_effort(\n                        _session_effort, _profile_home," in streaming
    assert "resolve_session_reasoning_effort(" in gateway
    assert 'session_effort=getattr(s, "reasoning_effort", None)' in gateway


def test_reasoning_slash_command_posts_active_session_context():
    commands = (REPO / "static" / "commands.js").read_text(encoding="utf-8")
    start = commands.index("function cmdReasoning")
    end = commands.index("function cmdVoice", start)
    body = commands[start:end]
    assert "_saveReasoningEffort(arg)" in body
    ui = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    helper = ui[ui.index("function _saveReasoningEffort("):]
    helper = helper[:helper.index("\n}\n")]
    assert "_reasoningEffortContext()" in helper


@pytest.fixture
def isolated_reasoning_profiles(tmp_path, monkeypatch):
    root = tmp_path / "root"
    work = root / "profiles" / "work"
    work.mkdir(parents=True)
    (root / "config.yaml").write_text(
        "agent:\n  reasoning_effort: low\nwebui_gateway_base_url: http://root-gateway:8650\n"
    )
    (work / "config.yaml").write_text(
        "agent:\n  reasoning_effort: high\nwebui_gateway_base_url: http://work-gateway:8650\n"
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(root / "config.yaml"))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles._tls, "profile", None, raising=False)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    config.reload_config()
    return root, work


def test_new_session_defaults_honor_external_config_path(
    isolated_reasoning_profiles, tmp_path, monkeypatch
):
    external = tmp_path / "mounted" / "config.yaml"
    external.parent.mkdir()
    external.write_text(
        "model:\n  default: gpt-5\n  provider: openai\n"
        "agent:\n  reasoning_effort: high\n"
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(external))
    config.reload_config()

    session = models.new_session(workspace=str(tmp_path), profile="default")

    assert session.reasoning_effort == "high"
    assert session.model == "gpt-5"
    assert session.model_provider == "openai"
    # An external root config must not replace a different profile's defaults.
    other = models.new_session(workspace=str(tmp_path), model="gpt-5", profile="work")
    assert other.reasoning_effort == "high"
    external.write_text("agent:\n  reasoning_effort: low\n")
    config.reload_config()
    assert models.new_session(
        workspace=str(tmp_path), model="gpt-5", profile="work"
    ).reasoning_effort == "high"


@pytest.mark.parametrize("config_location", ["root", "external", "work", "work-nested"])
@pytest.mark.parametrize("supplied_model", [None, "gpt-5"])
def test_named_request_new_session_defaults_stay_in_profile(
    isolated_reasoning_profiles, tmp_path, monkeypatch, config_location, supplied_model
):
    root, work = isolated_reasoning_profiles
    (root / "config.yaml").write_text(
        "model:\n  default: gpt-5.5\n  provider: openai\nagent:\n  reasoning_effort: low\n"
    )
    (work / "config.yaml").write_text(
        "model:\n  default: gpt-5\n  provider: openai\nagent:\n  reasoning_effort: high\n"
    )
    override = {"root": root / "config.yaml", "external": tmp_path / "external.yaml",
                "work": work / "override.yaml",
                "work-nested": work / "mounted" / "override.yaml"}[config_location]
    override.parent.mkdir(parents=True, exist_ok=True)
    if config_location != "root":
        override.write_text(
            "model:\n  default: gpt-5\n  provider: openai\nagent:\n  reasoning_effort: xhigh\n"
            if config_location.startswith("work") else (root / "config.yaml").read_text()
        )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(override))
    config.reload_config()
    profiles.set_request_profile("work")
    try:
        assert profiles.get_active_hermes_home() == work
        session = models.new_session(workspace=str(tmp_path), profile="work", model=supplied_model)
    finally:
        profiles.clear_request_profile()
    assert session.model == "gpt-5"
    if supplied_model is None:
        assert session.model_provider == "openai"
    expected = "xhigh" if config_location.startswith("work") else "high"
    assert session.reasoning_effort == expected
    # Detached workers must resolve the same default without request-local TLS.
    assert config.get_config_for_profile_home(work, isolate_config_override=True)["agent"]["reasoning_effort"] == expected


def _run_gateway_turn(session, stream_id, tmp_path, monkeypatch, *, runs_api, http_error=False):
    """Run the real Gateway worker with an intercepted transport.

    Returns ``(captured_posts, emitted_events)`` where each captured POST is
    ``(url, authorization, retained_endpoint, payload)``.
    """
    models.SESSIONS[session.session_id] = session
    session.active_stream_id = stream_id
    session.pending_user_message = "hello"
    channel = config.create_stream_channel()
    events = channel.subscribe()
    monkeypatch.setitem(config.STREAMS, stream_id, channel)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_USE_RUNS_API", "1" if runs_api else "0")
    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *a: True)
    monkeypatch.setattr(gateway_chat, "gateway_approval_unavailable_reason", lambda *a: None)
    # Avoid external platform discovery in system-prompt preparation.
    monkeypatch.setattr("api.streaming._load_webui_prefill_context", lambda cfg: {})
    monkeypatch.setattr("api.streaming._webui_ephemeral_system_prompt", lambda *a, **k: "test")
    captured = []

    def urlopen(request, timeout=None):
        if request.get_method() == "POST":
            captured.append((request.full_url, request.get_header("Authorization"),
                             gateway_chat._STREAM_ENDPOINTS[stream_id], json.loads(request.data)))
            if http_error:
                raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
            if runs_api:
                return io.BytesIO(b'{"run_id":"test-run"}')
            return io.BytesIO(
                b'data: {"choices":[{"delta":{"content":"done"}}]}\n'
                b'data: [DONE]\n'
            )
        return io.BytesIO(
            b'data: {"event":"run.completed","output":"done"}\n'
            b'data: [DONE]\n'
        )

    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", urlopen)
    gateway_chat._run_gateway_chat_streaming(
        session.session_id, "hello", session.model, str(tmp_path), stream_id,
        model_provider=session.model_provider,
    )
    assert captured, "worker did not construct a Gateway request"
    assert stream_id not in gateway_chat._STREAM_ENDPOINTS
    return captured, [events.get_nowait() for _ in range(events.qsize())]


@pytest.mark.parametrize("http_error", [False, True], ids=["success", "auth-error"])
@pytest.mark.parametrize("work_key", ["work-test-key", ""], ids=["profile-key", "no-profile-key"])
@pytest.mark.parametrize("session_effort, expected", [(None, "high"), ("xhigh", "xhigh")])
@pytest.mark.parametrize("runs_api", [False, True], ids=["legacy-api", "runs-api"])
def test_gateway_worker_resolves_reasoning_from_session_profile(
    isolated_reasoning_profiles, tmp_path, monkeypatch, session_effort, expected, runs_api, work_key, http_error
):
    # Simulate a detached worker with root ambient config and a named-profile
    # legacy session. Exercise real config resolution and outbound JSON encoding.
    assert config.get_config()["agent"]["reasoning_effort"] == "low"
    session = Session(
        session_id="gateway-profile-effort", profile="work", model="gpt-5",
        model_provider="openai", workspace=str(tmp_path), reasoning_effort=session_effort,
    )
    stream_id = "gateway-profile-effort-stream"
    root, work = isolated_reasoning_profiles
    for name, home in (("root", root), ("work", work)):
        (home / ".env").write_text(
            f"HERMES_WEBUI_GATEWAY_BASE_URL=http://{name}-gateway:8650\n"
            f"HERMES_WEBUI_GATEWAY_API_KEY={work_key if name == 'work' else 'root-test-key'}\n"
        )
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_BASE_URL", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "root-test-key")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", {
        "HERMES_WEBUI_GATEWAY_API_KEY",
    })
    captured, emitted = _run_gateway_turn(
        session, stream_id, tmp_path, monkeypatch, runs_api=runs_api, http_error=http_error,
    )

    url, authorization, endpoint, payload = captured[0]
    assert url.startswith("http://work-gateway:8650/")
    assert authorization == (f"Bearer {work_key}" if work_key else None)
    assert endpoint == ("http://work-gateway:8650", work_key)
    assert payload["reasoning_effort"] == expected
    if http_error:
        error = next(data for event, data, *_ in emitted if event == "apperror")
        if runs_api:
            assert error["type"] == "auth_mismatch"
        else:
            assert error["type"] == "gateway_auth_error"
            assert error["hint"].startswith("Check that" if work_key else "Set ")


@pytest.fixture
def external_root_with_named_active(isolated_reasoning_profiles, tmp_path, monkeypatch):
    """External root HERMES_CONFIG_PATH while the sticky active profile is ``work``."""
    root, work = isolated_reasoning_profiles
    external = tmp_path / "mounted" / "config.yaml"
    external.parent.mkdir()
    external.write_text(
        "agent:\n  reasoning_effort: xhigh\n"
        "webui_gateway_base_url: http://external-root:8650\n"
    )
    (root / "config.yaml").write_text(
        "agent:\n  reasoning_effort: low\nwebui_gateway_base_url: http://root-file:8650\n"
    )
    (root / ".env").write_text("HERMES_WEBUI_GATEWAY_API_KEY=root-test-key\n")
    (work / ".env").write_text("HERMES_WEBUI_GATEWAY_API_KEY=work-test-key\n")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(external))
    monkeypatch.setattr(profiles, "_active_profile", "work")
    # The active named profile's key is loaded into the process environment.
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "work-test-key")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", {"HERMES_WEBUI_GATEWAY_API_KEY"})
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_GATEWAY_BASE_URL", raising=False)
    config.reload_config()
    assert profiles.get_active_hermes_home() == work
    return root, work, external


@pytest.mark.parametrize("root_config_exists", [True, False], ids=["root-file", "no-root-file"])
@pytest.mark.parametrize("session_kind", ["legacy", "new"])
@pytest.mark.parametrize("runs_api", [False, True], ids=["legacy-api", "runs-api"])
def test_root_gateway_turn_honors_external_config_while_named_profile_active(
    external_root_with_named_active, tmp_path, monkeypatch, runs_api, session_kind, root_config_exists
):
    root, _work, _external = external_root_with_named_active
    if not root_config_exists:
        (root / "config.yaml").unlink()
        config.reload_config()
    if session_kind == "new":
        session = models.new_session(workspace=str(tmp_path), profile="default", model="gpt-5")
        session.model_provider = "openai"
        assert session.reasoning_effort == "xhigh"
    else:
        session = Session(
            session_id="gateway-root-external", profile="default", model="gpt-5",
            model_provider="openai", workspace=str(tmp_path), reasoning_effort=None,
        )
    captured, _ = _run_gateway_turn(
        session, f"{session.session_id}-stream", tmp_path, monkeypatch, runs_api=runs_api,
    )

    url, authorization, endpoint, payload = captured[0]
    assert url.startswith("http://external-root:8650/")
    assert authorization == "Bearer root-test-key"
    assert endpoint == ("http://external-root:8650", "root-test-key")
    assert payload["reasoning_effort"] == "xhigh"


@pytest.mark.parametrize("active", ["work", "default"])
def test_root_override_under_named_profile_is_not_root_config(
    external_root_with_named_active, monkeypatch, active
):
    root, work, _external = external_root_with_named_active
    monkeypatch.setattr(profiles, "_active_profile", active)
    nested = work / "mounted" / "config.yaml"
    nested.parent.mkdir()
    nested.write_text("agent:\n  reasoning_effort: high\n")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(nested))
    config.reload_config()
    root_cfg = config.get_config_for_profile_home(root, isolate_config_override=True)
    assert root_cfg["agent"]["reasoning_effort"] == "low"


@pytest.mark.parametrize("config_location", ["root", "external", "unset"])
def test_reasoning_get_legacy_session_uses_session_profile_config(
    isolated_reasoning_profiles, tmp_path, monkeypatch, config_location
):
    root, _work = isolated_reasoning_profiles
    if config_location == "external":
        external = tmp_path / "external.yaml"
        external.write_text("agent:\n  reasoning_effort: low\n")
        monkeypatch.setenv("HERMES_CONFIG_PATH", str(external))
    elif config_location == "unset":
        monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    config.reload_config()
    session = Session(
        session_id="legacy-work-chip", profile="work", model="gpt-5",
        model_provider="openai", workspace=str(tmp_path), reasoning_effort=None,
    )
    models.SESSIONS[session.session_id] = session

    handler = _DummyHandler()
    with patch("api.routes._session_id_visible_to_request_profile", return_value=True):
        handle_get(
            handler,
            urlparse("/api/reasoning?model=gpt-5&provider=openai&session_id=legacy-work-chip"),
        )

    assert handler.status == 200
    # Same source as both backends: the session profile's config, not ambient.
    assert handler.payload()["reasoning_effort"] == "high"
    assert _gateway_reasoning_effort_for_request(
        config.get_config_for_profile_home(
            profiles.get_hermes_home_for_profile("work"), isolate_config_override=True,
        ),
        model="gpt-5", model_provider="openai",
    ) == "high"


def test_reasoning_get_root_legacy_session_honors_external_config_while_named_active(
    external_root_with_named_active, tmp_path
):
    session = Session(
        session_id="legacy-root-chip", profile="default", model="gpt-5",
        model_provider="openai", workspace=str(tmp_path), reasoning_effort=None,
    )
    models.SESSIONS[session.session_id] = session
    handler = _DummyHandler()
    with patch("api.routes._session_id_visible_to_request_profile", return_value=True):
        handle_get(
            handler,
            urlparse("/api/reasoning?model=gpt-5&provider=openai&session_id=legacy-root-chip"),
        )
    assert handler.status == 200
    assert handler.payload()["reasoning_effort"] == "xhigh"


def _yaml_effort(path):
    return config._config_reasoning_effort(config._load_yaml_config_file(path))


@pytest.mark.parametrize("active", ["work", "default"])
@pytest.mark.parametrize("config_location", ["root", "external", "unset"])
@pytest.mark.parametrize("session_profile", ["work", "default", None], ids=["work", "root", "no-session"])
def test_reasoning_pick_writes_the_profile_its_readers_use(
    isolated_reasoning_profiles, tmp_path, monkeypatch, active, config_location, session_profile
):
    root, work = isolated_reasoning_profiles
    external = tmp_path / "external.yaml"
    if config_location == "external":
        external.write_text("agent:\n  reasoning_effort: medium\n")
        monkeypatch.setenv("HERMES_CONFIG_PATH", str(external))
    elif config_location == "unset":
        monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(profiles, "_active_profile", active)
    config.reload_config()
    root_file = external if config_location == "external" else root / "config.yaml"
    files = {"default": root_file, "work": work / "config.yaml"}
    before = {name: _yaml_effort(path) for name, path in files.items()}

    body = {"effort": "xhigh", "model": "gpt-5", "provider": "openai"}
    if session_profile is not None:
        session = Session(
            session_id="pick-session", profile=session_profile, model="gpt-5",
            model_provider="openai", workspace=str(tmp_path), reasoning_effort="low",
        )
        models.SESSIONS[session.session_id] = session
        body["session_id"] = session.session_id
    handler = _DummyHandler(body, command="POST")
    with patch("api.routes._session_id_visible_to_request_profile", return_value=True):
        handle_post(handler, urlparse("/api/reasoning"))
    assert handler.status == 200
    assert handler.payload()["reasoning_effort"] == "xhigh"

    # The pick lands in its own profile's file and no other.
    owner = session_profile or active
    for name, path in files.items():
        assert _yaml_effort(path) == ("xhigh" if name == owner else before[name]), name
    # The owner's next chat and every reader of its default now agree.
    owner_home = profiles.get_hermes_home_for_profile(owner)
    assert config.effective_session_reasoning_effort(None, owner_home) == "xhigh"
    assert models.new_session(
        workspace=str(tmp_path), model="gpt-5", profile=owner
    ).reasoning_effort == "xhigh"
    # The no-session chip reads the active profile's default from the same file.
    handler = _DummyHandler()
    handle_get(handler, urlparse("/api/reasoning?model=gpt-5&provider=openai"))
    expected_active = "xhigh" if owner == active else before[active]
    assert handler.payload()["reasoning_effort"] == expected_active
    assert expected_active == config.effective_session_reasoning_effort(
        None, profiles.get_hermes_home_for_profile(active)
    )


@pytest.mark.parametrize("missing", ["session", "active"])
def test_reasoning_pick_for_missing_profile_keeps_chat_and_writes_no_other_profile(
    isolated_reasoning_profiles, tmp_path, monkeypatch, missing
):
    root, work = isolated_reasoning_profiles
    external = tmp_path / "external.yaml"
    external.write_text("agent:\n  reasoning_effort: medium\n")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(external))
    if missing == "active":
        monkeypatch.setattr(profiles, "_active_profile", "gone")
    config.reload_config()
    body = {"effort": "xhigh", "model": "gpt-5", "provider": "openai"}
    if missing == "session":
        session = Session(
            session_id="gone-chat", profile="gone", model="gpt-5",
            model_provider="openai", workspace=str(tmp_path), reasoning_effort="low",
        )
        models.SESSIONS[session.session_id] = session
        body["session_id"] = session.session_id
    handler = _DummyHandler(body, command="POST")
    with patch("api.routes._session_id_visible_to_request_profile", return_value=True):
        handle_post(handler, urlparse("/api/reasoning"))

    if missing == "session":
        # The chat keeps its own pick even though its profile has no config left.
        assert handler.status == 200
        assert session.reasoning_effort == "xhigh"
        assert handler.payload()["reasoning_effort"] == "xhigh"
    else:
        # Nothing could store the pick, so it is refused rather than shown.
        assert handler.status == 400
        handler = _DummyHandler()
        handle_get(handler, urlparse("/api/reasoning?model=gpt-5&provider=openai"))
        # The no-session chip reports what the missing profile's readers use.
        assert handler.payload()["reasoning_effort"] == ""
    # Never redirected into another profile's file.
    assert _yaml_effort(external) == "medium"
    assert _yaml_effort(root / "config.yaml") == "low"
    assert _yaml_effort(work / "config.yaml") == "high"


def test_reasoning_get_unloadable_session_keeps_profile_answer():
    captured = {}

    def status(**kwargs):
        captured.update(kwargs)
        return {"reasoning_effort": "high", "supported_efforts": ["low", "high"]}

    handler = _DummyHandler()
    with (
        patch("api.routes._session_id_visible_to_request_profile", return_value=True),
        patch("api.routes.get_session", side_effect=KeyError("gone")),
        patch("api.routes.get_reasoning_status", side_effect=status),
    ):
        handle_get(handler, urlparse("/api/reasoning?model=gpt-5&session_id=gone"))

    # Master's answer, not a 404 that hides the chip and re-fires every sync.
    assert handler.status == 200
    assert "effort_override" not in captured


@pytest.mark.parametrize("session_effort", ["low", "", None])
def test_effective_session_effort_shares_one_legacy_source(
    isolated_reasoning_profiles, session_effort
):
    _root, work = isolated_reasoning_profiles
    expected = "high" if session_effort is None else session_effort
    assert config.effective_session_reasoning_effort(session_effort, work) == expected
    if session_effort is None:
        assert models._profile_default_reasoning_effort("work") == "high"


@pytest.mark.parametrize("parent_effort", ["high", "none", "", None])
@pytest.mark.parametrize("route", ["btw", "background"])
def test_btw_and_background_children_inherit_parent_effort(
    isolated_reasoning_profiles, tmp_path, route, parent_effort
):
    parent = Session(
        session_id=f"parent-{route}", profile="default", model="gpt-5",
        model_provider="openai", workspace=str(tmp_path), reasoning_effort=parent_effort,
    )
    models.SESSIONS[parent.session_id] = parent
    created = []
    real_new_session = models.new_session

    def capture(*args, **kwargs):
        child = real_new_session(*args, **kwargs)
        created.append(child)
        return child

    body = {"session_id": parent.session_id}
    body["question" if route == "btw" else "prompt"] = "hi"
    handler = _DummyHandler(body, command="POST")
    with (
        patch("api.models.new_session", side_effect=capture),
        patch("api.models.Session.save"),
        patch("threading.Thread"),
    ):
        handle_post(handler, urlparse(f"/api/{route}"))

    assert created, handler.status
    # Profile default is "low"; the child keeps the parent's raw value,
    # including None (legacy) and "" (provider default).
    assert created[0].reasoning_effort == parent_effort


@pytest.mark.parametrize("config_location", ["root", "external", "unset"])
def test_local_worker_legacy_session_uses_session_profile_config(
    isolated_reasoning_profiles, tmp_path, monkeypatch, config_location
):
    """Local-transport twin of the legacy chip test, sticky active profile ``work``."""
    _root, work = isolated_reasoning_profiles
    if config_location == "external":
        external = tmp_path / "external.yaml"
        external.write_text("agent:\n  reasoning_effort: low\n")
        monkeypatch.setenv("HERMES_CONFIG_PATH", str(external))
    elif config_location == "unset":
        monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(profiles, "_active_profile", "work")
    config.reload_config()
    # The streaming worker resolves a legacy (None) session through this helper
    # with the session's profile home; it must match the chip GET.
    profile_home = str(profiles.get_hermes_home_for_profile("work"))
    effort = config.effective_session_reasoning_effort(None, profile_home)
    assert effort == "high"
    assert resolve_session_reasoning_effort(
        config.get_config_for_profile_home(profile_home),
        session_effort=effort, model_id="gpt-5", provider_id="openai",
    ) == "high"


def test_root_new_session_model_ignores_override_under_named_profile(
    isolated_reasoning_profiles, monkeypatch
):
    root, work = isolated_reasoning_profiles
    (root / "config.yaml").write_text("model:\n  default: gpt-5.5\nagent:\n  reasoning_effort: low\n")
    (work / "config.yaml").write_text("model:\n  default: gpt-5\nagent:\n  reasoning_effort: high\n")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(work / "config.yaml"))
    config.reload_config()
    model, _provider = models._profile_default_model_state("default")
    assert model == "gpt-5.5"
    assert models._profile_default_reasoning_effort("default") == "low"


def test_overlapping_reasoning_posts_for_one_session_do_not_interleave(
    isolated_reasoning_profiles, tmp_path
):
    """`low` pauses after its session save; `high` must wait for the whole step."""
    import threading

    session = Session(
        session_id="overlap", profile="default", model="gpt-5",
        model_provider="openai", workspace=str(tmp_path), reasoning_effort="medium",
        messages=[{"role": "user", "content": "hi"}],
    )
    session.save(touch_updated_at=False)
    models.SESSIONS[session.session_id] = session
    profile_value = {}
    low_in_config = threading.Event()
    release_low = threading.Event()
    order = []

    def fake_profile_write(effort, *_a):
        if effort == "low":
            low_in_config.set()
            assert release_low.wait(5)
        order.append(effort)
        profile_value["effort"] = effort
        return effort

    def post(effort):
        handler = _DummyHandler({"effort": effort, "session_id": "overlap"}, command="POST")
        handle_post(handler, urlparse("/api/reasoning"))
        assert handler.status == 200

    with (
        patch("api.routes.write_reasoning_effort", side_effect=fake_profile_write),
        patch("api.routes.get_reasoning_status", return_value={"reasoning_effort": ""}),
        patch("api.routes._session_id_visible_to_request_profile", return_value=True),
    ):
        low = threading.Thread(target=post, args=("low",))
        low.start()
        assert low_in_config.wait(5)
        high = threading.Thread(target=post, args=("high",))
        high.start()
        high.join(0.3)
        # `high` is blocked on the session lock, not writing between `low`'s steps.
        assert high.is_alive()
        assert session.reasoning_effort == "low"
        release_low.set()
        low.join(5)
        high.join(5)

    assert order == ["low", "high"]
    assert profile_value["effort"] == "high"
    assert session.reasoning_effort == "high"
    assert Session.load("overlap").reasoning_effort == "high"


def test_root_ignores_override_under_symlinked_profiles_dir(tmp_path, monkeypatch):
    real_profiles = tmp_path / "data" / "profiles"
    work = real_profiles / "work"
    work.mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    (root / "profiles").symlink_to(real_profiles, target_is_directory=True)
    (root / "config.yaml").write_text("agent:\n  reasoning_effort: low\n")
    (work / "config.yaml").write_text("agent:\n  reasoning_effort: high\n")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles._tls, "profile", None, raising=False)
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(root / "profiles" / "work" / "config.yaml"))
    config.reload_config()
    assert config.effective_session_reasoning_effort(None, root) == "low"


def test_non_isolated_symlinked_config_file_still_matches_its_home(tmp_path, monkeypatch):
    """`work/config.yaml` is a dotfiles symlink named by HERMES_CONFIG_PATH; root active."""
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    (dotfiles / "hermes.yaml").write_text("agent:\n  reasoning_effort: high\n")
    root = tmp_path / "root"
    work = root / "profiles" / "work"
    work.mkdir(parents=True)
    (work / "config.yaml").symlink_to(dotfiles / "hermes.yaml")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles._tls, "profile", None, raising=False)
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(work / "config.yaml"))
    config.reload_config()
    sentinel = {"agent": {"reasoning_effort": "from-get-config"}}
    # Master: the config's parent directory is `work`, so the ambient
    # get_config() (with its in-memory overrides) is used.
    with patch("api.config.get_config", return_value=sentinel):
        assert config.get_config_for_profile_home(work) is sentinel


def test_root_ignores_symlinked_override_file_under_named_profile(tmp_path, monkeypatch):
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    (dotfiles / "work.yaml").write_text("agent:\n  reasoning_effort: high\n")
    root = tmp_path / "root"
    work = root / "profiles" / "work"
    work.mkdir(parents=True)
    (root / "config.yaml").write_text("agent:\n  reasoning_effort: low\n")
    (work / "config.yaml").symlink_to(dotfiles / "work.yaml")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles._tls, "profile", None, raising=False)
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(work / "config.yaml"))
    config.reload_config()
    # The override is work's file even though it links outside <root>/profiles.
    assert config.effective_session_reasoning_effort(None, root) == "low"
    assert config.effective_session_reasoning_effort(None, work) == "high"


@pytest.mark.parametrize(
    "imported, expected",
    [("ultra", None), ({"effort": "high"}, None), ("High", "high"), ("", ""), (None, None)],
)
def test_session_import_keeps_transcript_with_unknown_effort(imported, expected):
    # An export from a CLI with a level this WebUI doesn't know must still import.
    import api.routes as routes

    captured = {}

    class _FakeSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.session_id = "imported"
            self.messages = kwargs["messages"]

        def save(self):
            pass

        def compact(self):
            return {"session_id": self.session_id}

    handler = _DummyHandler(command="POST")
    body = {"messages": [], "workspace": "/tmp", "reasoning_effort": imported}
    with (
        patch("api.routes.Session", _FakeSession),
        patch("api.routes.resolve_trusted_workspace", side_effect=lambda w: w),
        patch("api.routes.SESSIONS", OrderedDict()),
        patch("api.routes._evict_sessions_over_cap"),
        patch("api.routes.publish_session_list_changed"),
        patch("api.routes.public_session_projection", side_effect=lambda d: d),
    ):
        routes._handle_session_import(handler, body)

    assert handler.status == 200
    assert captured["reasoning_effort"] == expected
