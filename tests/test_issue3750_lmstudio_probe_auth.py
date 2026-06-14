"""Regression tests for #3750 LM Studio reasoning probes dropping auth."""

from __future__ import annotations

import json
import sys
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


def test_reasoning_probe_falls_back_without_hermes_cli(
    tmp_path,
    monkeypatch,
    lmstudio_probe_server,
):
    blocked_modules = [
        name
        for name in list(sys.modules)
        if name == "hermes_cli" or name.startswith("hermes_cli.")
    ]
    for name in blocked_modules:
        monkeypatch.delitem(sys.modules, name, raising=False)

    class _Blocker:
        def find_module(self, name, path=None):
            if name == "hermes_cli" or name.startswith("hermes_cli."):
                return self
            return None

        def load_module(self, name):
            raise ImportError(f"hermes_cli blocked for test: {name}")

    blocker = _Blocker()
    sys.meta_path.insert(0, blocker)
    try:
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

        status = config.get_reasoning_status()

        assert status["supports_reasoning_effort"] is True
        assert status["supported_efforts"] == ["low", "medium", "high"]
        assert lmstudio_probe_server.requests[0] == {
            "path": "/api/v1/models",
            "authorization": "Bearer config-token",
        }
    finally:
        try:
            sys.meta_path.remove(blocker)
        except ValueError:
            pass


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


def test_credentialed_probe_never_calls_hermes_cli(
    tmp_path,
    monkeypatch,
    lmstudio_probe_server,
):
    """When a credential is configured, the probe must use the built-in
    no-redirect fallback and NEVER the bundled hermes_cli probe (which follows
    redirects and could forward the key). (#3837 security review)
    """
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

    cli_calls: list[tuple] = []

    class _CliModule:
        @staticmethod
        def lmstudio_model_reasoning_options(model, base_url, api_key=None, timeout=5.0):
            cli_calls.append((model, base_url, api_key))
            return ["low", "medium", "high"]

    monkeypatch.setitem(sys.modules, "hermes_cli", type("HermesCli", (), {})())
    monkeypatch.setitem(sys.modules, "hermes_cli.models", _CliModule)

    status = config.get_reasoning_status()

    assert status["supports_reasoning_effort"] is True
    assert status["supported_efforts"] == ["low", "medium", "high"]
    # The credential reached the configured endpoint via the built-in probe …
    assert lmstudio_probe_server.requests[0] == {
        "path": "/api/v1/models",
        "authorization": "Bearer config-token",
    }
    # … and the redirect-following hermes_cli probe was never invoked.
    assert cli_calls == [], "credentialed probe must bypass hermes_cli"


def test_keyless_probe_logs_signature_mismatch_before_fallback(
    tmp_path,
    monkeypatch,
    lmstudio_probe_server,
):
    """Keyless probes still prefer hermes_cli; an incompatible CLI signature
    logs a warning and degrades to the built-in probe. (#3750)
    """
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

    captured: list[tuple[str, bool]] = []

    class _CliModule:
        @staticmethod
        def lmstudio_model_reasoning_options(model, base_url):
            raise TypeError("unexpected keyword argument: api_key")

    monkeypatch.setitem(sys.modules, "hermes_cli", type("HermesCli", (), {})())
    monkeypatch.setitem(sys.modules, "hermes_cli.models", _CliModule)
    monkeypatch.setattr(
        config.logger,
        "warning",
        lambda msg, *args, **kwargs: captured.append((str(msg), bool(kwargs.get("exc_info")))),
    )

    # No key configured -> CLI path is attempted, raises TypeError, warning is
    # logged, then the built-in (keyless) probe runs against the auth-requiring
    # test server, which 401s -> []. The point is the warning + degrade path.
    status = config.get_reasoning_status()

    assert status["supports_reasoning_effort"] is False
    assert status["supported_efforts"] == []
    assert lmstudio_probe_server.requests[-1]["authorization"] is None
    assert captured, "TypeError fallback must log a warning before probing directly"
    assert "unexpected signature" in captured[0][0]
    assert captured[0][1] is True


def test_reasoning_probe_drops_key_for_caller_supplied_base_url(
    tmp_path,
    monkeypatch,
    lmstudio_probe_server,
):
    """A caller-supplied base_url that is NOT the configured LM Studio endpoint
    must be probed WITHOUT the stored credential. /api/reasoning takes a
    base_url query param, so attaching the key to an arbitrary host would leak
    it. (#3837 security review)
    """
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

    # Point the probe at the SAME server but via an unconfigured base_url. The
    # configured base_url is the only one allowed to carry the key. The probe
    # server returns 401 without auth, so reasoning is reported unsupported and,
    # crucially, no Bearer token reaches the caller-supplied URL.
    efforts = config.resolve_model_reasoning_efforts(
        "auth-model",
        provider_id="lmstudio",
        base_url=lmstudio_probe_server.base_v1.replace("127.0.0.1", "localhost"),
    )

    assert efforts == []
    assert lmstudio_probe_server.requests, "probe should still be attempted keyless"
    assert lmstudio_probe_server.requests[0]["authorization"] is None


def test_reasoning_probe_does_not_follow_redirects_with_credential(
    tmp_path,
    monkeypatch,
):
    """A 3xx from the configured probe URL must NOT forward the Authorization
    header to the redirect target. urllib re-sends request headers across
    redirects, so the probe uses a no-redirect opener. (#3837 security review)
    """
    captured: list[dict[str, str | None]] = []

    class _Target(BaseHTTPRequestHandler):
        def do_GET(self):
            captured.append(
                {"host": "target", "authorization": self.headers.get("Authorization")}
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"models": []}).encode())

        def log_message(self, fmt, *args):
            return None

    class _Redirector(BaseHTTPRequestHandler):
        def do_GET(self):
            captured.append(
                {"host": "redirector", "authorization": self.headers.get("Authorization")}
            )
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{target.server_address[1]}/api/v1/models"
            )
            self.end_headers()

        def log_message(self, fmt, *args):
            return None

    target = HTTPServer(("127.0.0.1", 0), _Target)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    target_thread.start()
    redir = HTTPServer(("127.0.0.1", 0), _Redirector)
    redir_thread = threading.Thread(target=redir.serve_forever, daemon=True)
    redir_thread.start()
    try:
        result = config._lmstudio_reasoning_probe_options_fallback(
            "auth-model",
            f"http://127.0.0.1:{redir.server_address[1]}/v1",
            api_key="secret-token",
        )
    finally:
        redir.shutdown()
        redir.server_close()
        redir_thread.join(timeout=5)
        target.shutdown()
        target.server_close()
        target_thread.join(timeout=5)

    # The probe stops at the redirector (no-redirect opener) and never reaches
    # the redirect target, so the credential is never forwarded onward.
    assert result == []
    hosts_hit = {c["host"] for c in captured}
    assert "target" not in hosts_hit, "redirect must not be followed"
    assert all(c["host"] == "redirector" for c in captured)


def test_credentialed_probe_does_not_follow_redirects_end_to_end(
    tmp_path,
    monkeypatch,
):
    """End-to-end through resolve_model_reasoning_efforts() with the REAL
    hermes_cli importable: when the CONFIGURED LM Studio endpoint returns a 302,
    the credentialed probe must not forward the key to the redirect target.
    This is the production path (hermes_cli present) — credentialed probes route
    through the built-in no-redirect probe, so the redirect is refused.
    (#3837 security review)
    """
    captured: list[dict[str, str | None]] = []

    class _Target(BaseHTTPRequestHandler):
        def do_GET(self):
            captured.append(
                {"host": "target", "authorization": self.headers.get("Authorization")}
            )
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
                                    "reasoning": {"allowed_options": ["low", "high"]}
                                },
                            }
                        ]
                    }
                ).encode()
            )

        def log_message(self, fmt, *args):
            return None

    class _Redirector(BaseHTTPRequestHandler):
        def do_GET(self):
            captured.append(
                {"host": "redirector", "authorization": self.headers.get("Authorization")}
            )
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{target.server_address[1]}/api/v1/models"
            )
            self.end_headers()

        def log_message(self, fmt, *args):
            return None

    target = HTTPServer(("127.0.0.1", 0), _Target)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    target_thread.start()
    redir = HTTPServer(("127.0.0.1", 0), _Redirector)
    redir_thread = threading.Thread(target=redir.serve_forever, daemon=True)
    redir_thread.start()

    # Sanity: this test only proves the redirect guard if hermes_cli is
    # importable (the production path). If it isn't, the keyless/credentialed
    # split still routes credentialed probes through the no-redirect fallback,
    # so the assertion holds either way.
    redir_base = f"http://127.0.0.1:{redir.server_address[1]}/v1"
    _write_config(
        tmp_path,
        monkeypatch,
        f"""
model:
  provider: lmstudio
  default: auth-model
  base_url: {redir_base}
providers:
  lmstudio:
    api_key: secret-token
agent:
  reasoning_effort: medium
display:
  show_reasoning: true
""",
    )
    try:
        efforts = config.resolve_model_reasoning_efforts(
            "auth-model",
            provider_id="lmstudio",
            base_url=redir_base,
        )
    finally:
        redir.shutdown()
        redir.server_close()
        redir_thread.join(timeout=5)
        target.shutdown()
        target.server_close()
        target_thread.join(timeout=5)

    assert efforts == []
    hosts_hit = {c["host"] for c in captured}
    assert "target" not in hosts_hit, "credentialed redirect must not be followed"
    # The credential only ever touched the configured (redirector) endpoint.
    assert all(c["host"] == "redirector" for c in captured)
