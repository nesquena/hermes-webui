"""Regression tests for #3750 LM Studio reasoning probes dropping auth."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import api.config as config
import api.onboarding as onboarding
import api.profiles as profiles


_API_KEY_ENV_VARS = (
    "LM_API_KEY",
    "LMSTUDIO_API_KEY",
)


class _LmStudioProbeServer:
    def __init__(self):
        self.requests: list[dict[str, str | None]] = []
        self._server = HTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_v1(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _handler(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                auth = self.headers.get("Authorization")
                parent.requests.append(
                    {
                        "path": self.path,
                        "authorization": auth,
                    }
                )

                if self.path == "/api/v1/models":
                    if auth:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(
                            json.dumps(
                                {
                                    "models": [
                                        {
                                            "key": "auth-model",
                                            "capabilities": {
                                                "reasoning": {
                                                    "allowed_options": ["low", "medium", "high"]
                                                }
                                            },
                                        }
                                    ]
                                }
                            ).encode()
                        )
                        return

                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "unauthorized"}).encode())
                    return

                if self.path == "/v1/models":
                    if auth:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"data": [{"id": "auth-model"}]}).encode())
                        return

                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(
                        json.dumps({"error": {"message": "unauthorized"}}).encode()
                    )
                    return

                self.send_response(404)
                self.end_headers()

            def log_message(self, fmt, *args):
                return None

        return Handler


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_path = config._cfg_path
    old_fp = config._cfg_fingerprint
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("BROWSER", "echo")
    for var in _API_KEY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    config.invalidate_models_cache()
    yield
    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime
    config._cfg_path = old_path
    config._cfg_fingerprint = old_fp
    config.invalidate_models_cache()


@pytest.fixture
def lmstudio_probe_server():
    server = _LmStudioProbeServer()
    try:
        yield server
    finally:
        server.close()


def _write_config(tmp_path, monkeypatch, text: str) -> None:
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(text, encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfgfile)
    config.reload_config()
    config.invalidate_models_cache()


def test_reasoning_probe_uses_config_key_before_env_aliases(
    tmp_path,
    monkeypatch,
    lmstudio_probe_server,
):
    _write_config(
        tmp_path,
        monkeypatch,
        f"""
model:
  provider: lmstudio
  default: auth-model
  base_url: {lmstudio_probe_server.base_v1}
providers:
  lmstudio:
    api_key: config-token
agent:
  reasoning_effort: medium
display:
  show_reasoning: true
""",
    )
    monkeypatch.setenv("LM_API_KEY", "env-token")
    monkeypatch.setenv("LMSTUDIO_API_KEY", "legacy-token")

    status = config.get_reasoning_status()

    assert status["supports_reasoning_effort"] is True
    assert status["supported_efforts"] == ["low", "medium", "high"]
    assert lmstudio_probe_server.requests[0] == {
        "path": "/api/v1/models",
        "authorization": "Bearer config-token",
    }


def test_reasoning_probe_honors_active_model_api_key(
    tmp_path,
    monkeypatch,
    lmstudio_probe_server,
):
    _write_config(
        tmp_path,
        monkeypatch,
        f"""
model:
  provider: lmstudio
  default: auth-model
  base_url: {lmstudio_probe_server.base_v1}
  api_key: model-token
providers:
  lmstudio:
    api_key: provider-token
agent:
  reasoning_effort: medium
display:
  show_reasoning: true
""",
    )

    status = config.get_reasoning_status()

    assert status["supports_reasoning_effort"] is True
    assert status["supported_efforts"] == ["low", "medium", "high"]
    assert lmstudio_probe_server.requests[0] == {
        "path": "/api/v1/models",
        "authorization": "Bearer model-token",
    }


def test_reasoning_probe_stays_keyless_when_no_key_is_configured(
    tmp_path,
    monkeypatch,
    lmstudio_probe_server,
):
    _write_config(
        tmp_path,
        monkeypatch,
        f"""
model:
  provider: lmstudio
  default: auth-model
  base_url: {lmstudio_probe_server.base_v1}
agent:
  reasoning_effort: medium
display:
  show_reasoning: true
""",
    )

    status = config.get_reasoning_status()

    assert status["supports_reasoning_effort"] is False
    assert status["supported_efforts"] == []
    assert lmstudio_probe_server.requests[0] == {
        "path": "/api/v1/models",
        "authorization": None,
    }


def test_onboarding_probe_remains_authorized_control(
    lmstudio_probe_server,
):
    result = onboarding.probe_provider_endpoint(
        "lmstudio",
        lmstudio_probe_server.base_v1,
        api_key="control-token",
    )

    assert result["ok"] is True
    assert lmstudio_probe_server.requests[0] == {
        "path": "/v1/models",
        "authorization": "Bearer control-token",
    }
