"""Authenticated Agent TTS HTTP route contract."""

from __future__ import annotations

import io
import json
import socket
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

import api.auth as auth
import api.routes as routes
from api.agent_tts import AgentTtsAudio, AgentTtsError


class Handler:
    def __init__(self, body=None, *, command="POST", headers=None, client="127.0.0.1"):
        encoded = b"" if body is None else json.dumps(body).encode("utf-8")
        self.command = command
        self.path = "/api/tts"
        self.rfile = io.BytesIO(encoded)
        self.wfile = io.BytesIO()
        self.headers = dict(headers or {})
        self.headers.setdefault("Content-Length", str(len(encoded)))
        self.client_address = (client, 12345)
        self.status = None
        self.sent_headers = {}
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    def json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


@pytest.fixture(autouse=True)
def reset_route_state(monkeypatch):
    monkeypatch.setattr(routes, "_tts_synthesis_limiter", routes._TtsRateLimiter(0))
    monkeypatch.setattr(routes, "_tts_provider_limiter", routes._TtsRateLimiter(0))
    routes._tts_migration_cache.clear()
    routes._tts_profile_write_locks.clear()


@pytest.mark.parametrize("route", ["capability", "provider", "synthesis"])
def test_passwordless_remote_tts_routes_fail_before_worker(monkeypatch, route):
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    monkeypatch.delenv("HERMES_WEBUI_ONBOARDING_OPEN", raising=False)
    monkeypatch.setattr(
        routes,
        "run_agent_tts_operation",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("worker called")),
    )
    monkeypatch.setattr(
        routes,
        "synthesize_agent_tts",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("worker called")),
    )
    headers = {"X-Forwarded-For": "127.0.0.1"}
    if route == "capability":
        handler = Handler(command="GET", headers=headers, client="8.8.8.8")
        routes._handle_agent_tts_capability(handler)
    elif route == "provider":
        handler = Handler({}, headers=headers, client="8.8.8.8")
        routes._handle_agent_tts_provider(handler)
    else:
        handler = Handler(
            {"engine": "agent", "text": "hello"}, headers=headers, client="8.8.8.8"
        )
        routes._handle_tts(handler, None)

    assert handler.status == 403
    assert handler.json()["code"] == "local_origin_required"


def test_capability_route_returns_sanitized_worker_projection(monkeypatch):
    payload = {
        "ok": True,
        "state": "ready",
        "active_provider": "Microsoft Edge TTS",
        "config_fingerprint": "sha256:abc",
        "providers": [],
    }
    monkeypatch.setattr(routes, "run_agent_tts_operation", lambda *a, **k: payload)
    handler = Handler(command="GET")

    routes.handle_get(handler, urlparse("/api/tts/capability"))
    assert handler.status == 200
    assert handler.json() == payload
    assert handler.sent_headers["Cache-Control"] == "no-store"
    assert "api_key" not in handler.wfile.getvalue().decode("utf-8")


def test_capability_maps_sanitized_agent_error(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AgentTtsError("agent_timeout", 504, "Agent TTS operation timed out.")

    monkeypatch.setattr(routes, "run_agent_tts_operation", fail)
    handler = Handler(command="GET")

    routes._handle_agent_tts_capability(handler)
    assert handler.status == 504
    assert handler.json() == {
        "ok": False,
        "code": "agent_timeout",
        "error": "Agent TTS operation timed out.",
    }


@pytest.mark.parametrize("engine", ["edge", "elevenlabs", "openai", "server"])
def test_legacy_engine_is_stable_409_without_outbound_or_worker(monkeypatch, engine):
    monkeypatch.setattr(
        routes,
        "_tts_open",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("outbound HTTP called")),
        raising=False,
    )
    monkeypatch.setattr(
        routes,
        "synthesize_agent_tts",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("worker called")),
    )
    handler = Handler({"engine": engine, "text": "hello"})

    routes._handle_tts(handler, None)

    assert handler.status == 409
    assert handler.json()["code"] == "legacy_tts_migration_required"


@pytest.mark.parametrize(
    "extra",
    [
        {"provider": "openai"},
        {"voice": "alloy"},
        {"rate": "+10%"},
        {"pitch": "+5Hz"},
        {"base_url": "https://example.test"},
        {"api_key": "secret"},
        {"format": "mp3"},
        {"output_path": "/tmp/x"},
        {"environment": {"TOKEN": "secret"}},
    ],
)
def test_agent_synthesis_rejects_every_override_before_worker(monkeypatch, extra):
    monkeypatch.setattr(
        routes,
        "synthesize_agent_tts",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("worker called")),
    )
    handler = Handler({"engine": "agent", "text": "hello", **extra})

    routes._handle_tts(handler, None)
    assert handler.status == 400
    assert handler.json()["code"] == "invalid_request"


def test_agent_synthesis_returns_exact_validated_audio_headers(monkeypatch):
    monkeypatch.setattr(
        routes,
        "synthesize_agent_tts",
        lambda *_a, **_k: AgentTtsAudio(b"RIFF\x08\0\0\0WAVEdata", "audio/wav", "edge"),
    )
    handler = Handler({"engine": "agent", "text": "hello"})

    routes._handle_tts(handler, None)

    assert handler.status == 200
    assert handler.wfile.getvalue() == b"RIFF\x08\0\0\0WAVEdata"
    assert handler.sent_headers["Content-Type"] == "audio/wav"
    assert handler.sent_headers["Content-Length"] == str(len(handler.wfile.getvalue()))
    assert handler.sent_headers["Cache-Control"] == "no-store"
    assert handler.sent_headers["X-Content-Type-Options"] == "nosniff"
    assert handler.sent_headers["X-Hermes-TTS-Provider"] == "edge"


def test_client_disconnect_probe_distinguishes_open_and_closed_socket():
    server, client = socket.socketpair()
    try:
        handler = SimpleNamespace(connection=server)
        assert routes._tts_client_disconnected(handler) is False
        client.close()
        assert routes._tts_client_disconnected(handler) is True
    finally:
        server.close()
        client.close()


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("invalid_request", 400),
        ("config_conflict", 409),
        ("agent_busy", 429),
        ("provider_mismatch", 502),
        ("agent_contract_unavailable", 503),
        ("agent_timeout", 504),
    ],
)
def test_synthesis_maps_only_sanitized_agent_errors(monkeypatch, code, status):
    def fail(*_args, **_kwargs):
        raise AgentTtsError(code, status, f"safe {code}")

    monkeypatch.setattr(routes, "synthesize_agent_tts", fail)
    handler = Handler({"engine": "agent", "text": "do not log me"})

    routes._handle_tts(handler, None)
    assert handler.status == status
    assert handler.json() == {"ok": False, "code": code, "error": f"safe {code}"}


def test_tts_body_has_dedicated_64k_cap_before_json_parse(monkeypatch):
    monkeypatch.setattr(
        routes,
        "synthesize_agent_tts",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("worker called")),
    )
    handler = Handler(None)
    handler.headers["Content-Length"] = str(64 * 1024 + 1)

    routes._handle_tts(handler, None)
    assert handler.status == 413
    assert handler.close_connection is True


def test_provider_route_requires_exact_schema_and_delegates(monkeypatch):
    calls = []

    def run(op, payload, **kwargs):
        calls.append((op, payload, kwargs))
        return {
            "ok": True,
            "active_provider": "edge",
            "active_provider_name": "Microsoft Edge TTS",
            "active_provider_available": True,
            "resolved_provider": "neutts",
            "configured": True,
            "synthesis_supported": True,
            "provider_max_text_length": 4000,
            "request_max_text_length": 900,
            "limit_source": "agent_contract",
            "config_fingerprint": "sha256:new",
        }

    monkeypatch.setattr(routes, "run_agent_tts_operation", run)
    handler = Handler(
        {
            "provider": "Microsoft Edge TTS",
            "expected_config_fingerprint": "sha256:" + "0" * 64,
        }
    )

    routes._handle_agent_tts_provider(handler)

    assert handler.status == 200
    assert calls[0][0] == "select_provider"
    assert calls[0][1] == {
        "provider_name": "Microsoft Edge TTS",
        "expected_fingerprint": "sha256:" + "0" * 64,
    }
    assert handler.json() == {
        "ok": True,
        "active_provider": "edge",
        "active_provider_name": "Microsoft Edge TTS",
        "active_provider_available": True,
        "resolved_provider": "neutts",
        "configured": True,
        "synthesis_supported": True,
        "provider_max_text_length": 4000,
        "request_max_text_length": 900,
        "limit_source": "agent_contract",
        "config_fingerprint": "sha256:new",
    }


def test_provider_route_rejects_browser_config_fields(monkeypatch):
    monkeypatch.setattr(
        routes,
        "run_agent_tts_operation",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("worker called")),
    )
    handler = Handler(
        {
            "provider": "Microsoft Edge TTS",
            "expected_config_fingerprint": "sha256:" + "0" * 64,
            "api_key": "must-not-cross-boundary",
        }
    )

    routes._handle_agent_tts_provider(handler)
    assert handler.status == 400
    assert "must-not-cross-boundary" not in handler.wfile.getvalue().decode("utf-8")


def _migration_body(operation_id="12345678-1234-5678-9234-567812345678"):
    return {
        "provider": "Microsoft Edge TTS",
        "expected_config_fingerprint": "sha256:" + "0" * 64,
        "migration": {
            "operation_id": operation_id,
            "legacy_engine": "edge",
            "legacy_engine_was_persisted": True,
            "legacy_edge_voice": "en-US-AriaNeural",
            "legacy_voice_was_persisted": True,
            "expected_settings_revision": 7,
        },
    }


def _migration_scope(monkeypatch):
    scope = SimpleNamespace(name="named", home=None)
    monkeypatch.setattr(routes, "capture_agent_tts_profile_scope", lambda: scope)
    monkeypatch.setattr(routes, "_tts_request_owner", lambda *_a: "owner")
    monkeypatch.setattr(
        routes.api_config,
        "speech_settings_snapshot",
        lambda: {
            "revision": 7,
            "values": {
                "tts_engine": "edge",
                "tts_voice": "en-US-AriaNeural",
            },
            "present_keys": ["tts_engine", "tts_voice"],
        },
    )
    return scope


def test_migration_commits_sparse_settings_and_is_idempotent(monkeypatch):
    _migration_scope(monkeypatch)
    calls = []

    def run(operation, payload, **_kwargs):
        calls.append(operation)
        assert operation == "select_provider"
        assert payload["legacy_edge_voice"] == "en-US-AriaNeural"
        return {
            "active_provider": "edge",
            "active_provider_name": "Microsoft Edge TTS",
            "active_provider_available": True,
            "config_fingerprint": "sha256:" + "1" * 64,
            "previous_tts_present": False,
            "previous_tts": None,
        }

    monkeypatch.setattr(routes, "run_agent_tts_operation", run)
    monkeypatch.setattr(
        routes.api_config,
        "set_tts_engine_revisioned",
        lambda revision, engine: {
            "revision": 8,
            "values": {"tts_engine": "agent", "tts_voice": "en-US-AriaNeural"},
            "present_keys": ["tts_engine", "tts_voice"],
        },
    )

    first = Handler(_migration_body())
    routes._handle_agent_tts_provider(first)
    second = Handler(_migration_body())
    routes._handle_agent_tts_provider(second)

    assert first.status == second.status == 200
    assert first.json() == second.json()
    assert calls == ["select_provider"]
    assert "previous_tts" not in first.json()
    assert first.json()["speech_settings"]["values"]["tts_voice"] == "en-US-AriaNeural"


def test_migration_stale_settings_fails_before_agent_write(monkeypatch):
    _migration_scope(monkeypatch)
    monkeypatch.setattr(
        routes.api_config,
        "speech_settings_snapshot",
        lambda: {"revision": 8, "values": {"tts_engine": "edge"}},
    )
    monkeypatch.setattr(
        routes,
        "run_agent_tts_operation",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("worker called")),
    )
    handler = Handler(_migration_body())

    routes._handle_agent_tts_provider(handler)

    assert handler.status == 409
    assert handler.json()["code"] == "settings_conflict"


def test_migration_settings_failure_compensates_agent(monkeypatch):
    _migration_scope(monkeypatch)
    calls = []

    def run(operation, payload, **_kwargs):
        calls.append((operation, payload))
        if operation == "select_provider":
            return {
                "active_provider": "edge",
                "active_provider_name": "Microsoft Edge TTS",
                "active_provider_available": True,
                "config_fingerprint": "sha256:" + "1" * 64,
                "previous_tts_present": True,
                "previous_tts": {"provider": "openai"},
            }
        assert operation == "restore_tts"
        return {"config_fingerprint": "sha256:" + "2" * 64}

    monkeypatch.setattr(routes, "run_agent_tts_operation", run)
    monkeypatch.setattr(
        routes.api_config,
        "set_tts_engine_revisioned",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full secret")),
    )
    handler = Handler(_migration_body())

    routes._handle_agent_tts_provider(handler)

    assert handler.status == 500
    assert handler.json()["rolled_back"] is True
    assert [operation for operation, _payload in calls] == [
        "select_provider",
        "restore_tts",
    ]
    assert calls[1][1]["previous_tts"] == {"provider": "openai"}
    assert "disk full secret" not in handler.wfile.getvalue().decode("utf-8")


def test_migration_failed_compensation_reports_inconsistent(monkeypatch):
    _migration_scope(monkeypatch)

    def run(operation, _payload, **_kwargs):
        if operation == "select_provider":
            return {
                "active_provider": "edge",
                "config_fingerprint": "sha256:" + "1" * 64,
                "previous_tts_present": False,
                "previous_tts": None,
            }
        raise AgentTtsError("config_conflict", 409, "safe conflict")

    monkeypatch.setattr(routes, "run_agent_tts_operation", run)
    monkeypatch.setattr(
        routes.api_config,
        "set_tts_engine_revisioned",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("write failed")),
    )
    handler = Handler(_migration_body())

    routes._handle_agent_tts_provider(handler)

    assert handler.status == 409
    assert handler.json()["code"] == "migration_inconsistent"
