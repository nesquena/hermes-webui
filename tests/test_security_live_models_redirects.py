"""Regression tests for credentialed live-model probes refusing redirects."""

from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace


class _Headers(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class _Handler:
    def __init__(self, path: str):
        self.headers = _Headers()
        self.rfile = io.BytesIO()
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []
        self.client_address = ("127.0.0.1", 12345)
        self.path = path

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass


class _RedirectPair:
    def __init__(self):
        self.captured: list[dict[str, str | None]] = []
        self.target = HTTPServer(("127.0.0.1", 0), self._target_handler())
        self.target_thread = threading.Thread(target=self.target.serve_forever, daemon=True)
        self.target_thread.start()
        self.redirector = HTTPServer(("127.0.0.1", 0), self._redirector_handler())
        self.redirector_thread = threading.Thread(target=self.redirector.serve_forever, daemon=True)
        self.redirector_thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.redirector.server_address[1]}"

    @property
    def base_v1(self) -> str:
        return f"{self.base_url}/v1"

    def close(self) -> None:
        self.redirector.shutdown()
        self.redirector.server_close()
        self.redirector_thread.join(timeout=5)
        self.target.shutdown()
        self.target.server_close()
        self.target_thread.join(timeout=5)

    def _target_handler(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parent.captured.append(
                    {"host": "target", "path": self.path, "authorization": self.headers.get("Authorization")}
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"data": [{"id": "leaked-path"}]}).encode())

            def log_message(self, format, *args):
                return None

        return Handler

    def _redirector_handler(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parent.captured.append(
                    {"host": "redirector", "path": self.path, "authorization": self.headers.get("Authorization")}
                )
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{parent.target.server_address[1]}/steal",
                )
                self.end_headers()

            def log_message(self, format, *args):
                return None

        return Handler


def _json_response_body(handler: _Handler) -> dict:
    raw = handler.wfile.getvalue().split(b"\r\n\r\n", 1)[-1]
    if not raw.strip().startswith(b"{"):
        raw = handler.wfile.getvalue()
    return json.loads(raw.decode("utf-8"))


def test_credentialed_json_helper_refuses_redirects():
    from api import routes

    servers = _RedirectPair()
    try:
        result = routes._credentialed_json_get_no_redirect(
            f"{servers.base_v1}/models",
            {"Authorization": "Bearer CANARY_SECRET"},
            timeout=3,
        )
    except Exception:
        result = None
    finally:
        servers.close()

    assert result is None
    assert servers.captured == [
        {
            "host": "redirector",
            "path": "/v1/models",
            "authorization": "Bearer CANARY_SECRET",
        }
    ]


def test_config_credentialed_json_helper_refuses_redirects():
    from api import config

    servers = _RedirectPair()
    try:
        result = config._credentialed_json_get_no_redirect(
            f"{servers.base_v1}/models",
            {"Authorization": "Bearer CANARY_SECRET"},
            timeout=3,
        )
    except Exception:
        result = None
    finally:
        servers.close()

    assert result is None
    assert servers.captured == [
        {
            "host": "redirector",
            "path": "/v1/models",
            "authorization": "Bearer CANARY_SECRET",
        }
    ]


def test_custom_live_models_does_not_forward_key_to_redirect_target(monkeypatch):
    from api import routes

    servers = _RedirectPair()
    routes._clear_live_models_cache()
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "redirect-test-custom")
    monkeypatch.setattr(
        "api.config.get_config",
        lambda: {
            "custom_providers": [
                {
                    "name": "Leak Me",
                    "base_url": servers.base_url,
                    "api_key": "CANARY_SECRET",
                    "models": [],
                }
            ],
            "model": {"provider": "custom:leak-me"},
        },
    )
    monkeypatch.setitem(__import__("sys").modules, "hermes_cli.models", SimpleNamespace(provider_model_ids=lambda provider: []))

    handler = _Handler("/api/models/live?provider=custom:leak-me")
    try:
        routes._handle_live_models(
            handler,
            SimpleNamespace(path="/api/models/live", query="provider=custom:leak-me"),
        )
    finally:
        servers.close()
        routes._clear_live_models_cache()

    assert handler.status == 200
    assert _json_response_body(handler)["models"] == []
    assert {item["host"] for item in servers.captured} == {"redirector"}
    assert servers.captured[0]["authorization"] == "Bearer CANARY_SECRET"


def test_openai_compat_live_models_does_not_forward_key_to_redirect_target(monkeypatch):
    from api import routes

    servers = _RedirectPair()
    routes._clear_live_models_cache()
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "redirect-test-compat")
    monkeypatch.setitem(routes._OPENAI_COMPAT_ENDPOINTS, "deepseek", servers.base_v1)
    monkeypatch.setattr(
        "api.config.get_config",
        lambda: {
            "providers": {"deepseek": {"api_key": "CANARY_SECRET"}},
            "model": {"provider": "deepseek"},
        },
    )
    def provider_model_ids(provider):
        raise AssertionError("credentialed agent live probe should be skipped")

    monkeypatch.setitem(
        __import__("sys").modules,
        "hermes_cli.models",
        SimpleNamespace(provider_model_ids=provider_model_ids),
    )

    handler = _Handler("/api/models/live?provider=deepseek")
    try:
        routes._handle_live_models(
            handler,
            SimpleNamespace(path="/api/models/live", query="provider=deepseek"),
        )
    finally:
        servers.close()
        routes._clear_live_models_cache()

    assert handler.status == 200
    assert {item["host"] for item in servers.captured} == {"redirector"}
    assert servers.captured[0]["authorization"] == "Bearer CANARY_SECRET"


def test_openai_env_key_skips_agent_live_probe(monkeypatch):
    from api import routes

    routes._clear_live_models_cache()
    monkeypatch.setenv("OPENAI_API_KEY", "CANARY_SECRET")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "redirect-test-openai-env")
    monkeypatch.setattr(
        "api.config.get_config",
        lambda: {"model": {"provider": "openai"}},
    )

    def provider_model_ids(provider):
        raise AssertionError("credentialed OpenAI agent live probe should be skipped")

    monkeypatch.setitem(
        __import__("sys").modules,
        "hermes_cli.models",
        SimpleNamespace(provider_model_ids=provider_model_ids),
    )

    handler = _Handler("/api/models/live?provider=openai")
    try:
        routes._handle_live_models(
            handler,
            SimpleNamespace(path="/api/models/live", query="provider=openai"),
        )
    finally:
        routes._clear_live_models_cache()

    assert handler.status == 200
    assert _json_response_body(handler)["provider"] == "openai"


def test_openai_api_env_key_skips_config_agent_live_probe(monkeypatch):
    from api import config

    monkeypatch.setenv("OPENAI_API_KEY", "CANARY_SECRET")

    def provider_model_ids(provider):
        raise AssertionError("credentialed OpenAI API agent live probe should be skipped")

    monkeypatch.setitem(
        __import__("sys").modules,
        "hermes_cli.models",
        SimpleNamespace(provider_model_ids=provider_model_ids),
    )

    assert config._read_live_provider_model_ids("openai-api") == []


def test_copilot_acp_skips_agent_live_probe(monkeypatch):
    from api import routes

    routes._clear_live_models_cache()
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "redirect-test-copilot-acp")
    monkeypatch.setattr(
        "api.config.get_config",
        lambda: {"model": {"provider": "copilot-acp"}},
    )

    def provider_model_ids(provider):
        raise AssertionError("credentialed Copilot ACP agent live probe should be skipped")

    monkeypatch.setitem(
        __import__("sys").modules,
        "hermes_cli.models",
        SimpleNamespace(provider_model_ids=provider_model_ids),
    )

    handler = _Handler("/api/models/live?provider=copilot-acp")
    try:
        routes._handle_live_models(
            handler,
            SimpleNamespace(path="/api/models/live", query="provider=copilot-acp"),
        )
    finally:
        routes._clear_live_models_cache()

    assert handler.status == 200
    assert _json_response_body(handler)["provider"] == "copilot-acp"
