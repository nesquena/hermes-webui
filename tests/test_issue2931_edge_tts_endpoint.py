"""Validation + security-path coverage for the TTS endpoint (#2931).

These exercise the guard rails of _handle_tts (method, input cap,
rate limiting, agent delegation) in-process via a fake handler —
no network and no real TTS synthesis required, since rejections
happen before the agent's text_to_speech_tool is called.
"""
import io
import json
import sys
from types import SimpleNamespace

import pytest

import api.routes as routes


class _FakeHandler:
    def __init__(self, body: bytes, command: str = "POST", headers=None, client="1.2.3.4"):
        self.command = command
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = headers or {}
        self.headers.setdefault("Content-Length", str(len(body)))
        self.client_address = (client, 12345)
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    def payload(self):
        try:
            return json.loads(self.wfile.getvalue().decode("utf-8"))
        except Exception:
            return None


def _post(body_dict, **kw):
    body = json.dumps(body_dict).encode()
    return _FakeHandler(body, **kw)


def _reset_limiter():
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter


def _mock_text_to_speech_tool(monkeypatch, *, side_effect=None, return_value=None):
    """Thin wrapper around the shared TTS mock helper for backwards compat."""
    from tests._tts_helpers import mock_text_to_speech_tool
    return mock_text_to_speech_tool(
        monkeypatch, side_effect=side_effect, return_value=return_value
    )


@pytest.fixture(autouse=True)
def _fresh_tts_limiter(monkeypatch):
    """Reset the rate limiter singleton and disable auth for every test."""
    import api.auth as _auth
    monkeypatch.setattr(_auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "is_auth_enabled", lambda: False, raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    _reset_limiter()
    yield
    _reset_limiter()


def test_tts_requires_post():
    h = _post({"text": "hello"}, command="GET")
    routes._handle_tts(h, None)
    assert h.status == 405


def test_tts_requires_text():
    h = _post({"text": "   "})
    routes._handle_tts(h, None)
    assert h.status == 400
    assert "text is required" in (h.payload() or {}).get("error", "")


def test_tts_rejects_overlong_text():
    h = _post({"text": "x" * 5001}, client="10.0.0.1")
    routes._handle_tts(h, None)
    assert h.status == 400
    assert "text too long" in (h.payload() or {}).get("error", "")


def test_tts_requires_auth_when_enabled(monkeypatch):
    """When auth is enabled, unauthenticated requests are rejected."""
    import api.auth as _auth
    monkeypatch.setattr(_auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(routes, "is_auth_enabled", lambda: True, raising=False)
    monkeypatch.setattr(_auth, "parse_cookie", lambda h: None, raising=False)
    monkeypatch.setattr(_auth, "verify_session", lambda c: False, raising=False)
    h = _post({"text": "hello"}, client="10.0.0.99")
    routes._handle_tts(h, None)
    assert h.status == 401
    assert "unauthorized" in (h.payload() or {}).get("error", "")


def test_tts_delegates_to_agent(monkeypatch):
    """Happy path: TTS calls text_to_speech_tool and returns base64 data URL."""
    captured = {}

    def fake_tool(text, output_path):
        captured["text"] = text
        captured["output_path"] = output_path
        # Write some fake audio so the file is found
        with open(output_path, "wb") as f:
            f.write(b"\xff\xfb\x90" * 10)
        return json.dumps({
            "success": True,
            "file_path": output_path,
            "provider": "edge",
        })

    _mock_text_to_speech_tool(monkeypatch, side_effect=fake_tool)

    h = _post({"text": "hello world"}, client="10.0.0.10")
    routes._handle_tts(h, None)
    assert h.status == 200
    payload = h.payload()
    assert payload is not None
    assert payload["audio"].startswith("data:audio/mpeg;base64,")
    assert captured["text"] == "hello world"


def test_tts_agent_failure_returns_500(monkeypatch):
    """When text_to_speech_tool returns success=False, TTS returns 500."""
    _mock_text_to_speech_tool(monkeypatch, return_value=json.dumps({
        "success": False,
        "error": "TTS engine unavailable",
    }))
    h = _post({"text": "hello"}, client="10.0.0.11")
    routes._handle_tts(h, None)
    assert h.status == 500
    assert "TTS engine unavailable" in (h.payload() or {}).get("error", "")


def test_tts_agent_non_json_response_returns_500(monkeypatch):
    """When text_to_speech_tool returns non-JSON, TTS returns 500."""
    _mock_text_to_speech_tool(monkeypatch, return_value="not json at all")
    h = _post({"text": "hello"}, client="10.0.0.12")
    routes._handle_tts(h, None)
    assert h.status == 500
    assert "non-JSON" in (h.payload() or {}).get("error", "")


def test_tts_agent_missing_file_returns_500(monkeypatch):
    """When text_to_speech_tool reports success but file doesn't exist."""
    _mock_text_to_speech_tool(monkeypatch, return_value=json.dumps({
        "success": True,
        "file_path": "/tmp/does_not_exist_12345.mp3",
    }))
    h = _post({"text": "hello"}, client="10.0.0.13")
    routes._handle_tts(h, None)
    assert h.status == 500
    assert "no audio file" in (h.payload() or {}).get("error", "")


def test_tts_agent_missing_module_returns_503(monkeypatch):
    """When text_to_speech_tool can't be imported, TTS returns 503."""
    # Remove the module from sys.modules so the import fails
    monkeypatch.delitem(sys.modules, "tools.tts_tool", raising=False)
    # Make import raise
    import builtins
    real_import = builtins.__import__

    def _mock_import(name, *args, **kwargs):
        if name == "tools.tts_tool":
            raise ImportError("no agent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _mock_import)

    h = _post({"text": "hello"}, client="10.0.0.14")
    routes._handle_tts(h, None)
    assert h.status == 503
    assert "hermes-agent" in (h.payload() or {}).get("error", "")


def test_tts_rate_limits_second_immediate_request(monkeypatch):
    """Rate limiter records the client after the first request, blocks the second."""
    _mock_text_to_speech_tool(monkeypatch)
    h1 = _post({"text": "hello"}, client="10.0.0.3")
    routes._handle_tts(h1, None)
    assert h1.status == 200  # first request succeeds
    h2 = _post({"text": "hello"}, client="10.0.0.3")
    routes._handle_tts(h2, None)
    assert h2.status == 429


def test_tts_rate_limit_ignores_spoofed_forwarded_for_by_default(monkeypatch):
    """Without HERMES_WEBUI_TRUST_FORWARDED_FOR, X-Forwarded-For is ignored."""
    _mock_text_to_speech_tool(monkeypatch)
    h1 = _post(
        {"text": "hello"},
        headers={"X-Forwarded-For": "203.0.113.10"},
        client="10.0.0.4",
    )
    routes._handle_tts(h1, None)
    assert h1.status == 200

    h2 = _post(
        {"text": "hello"},
        headers={"X-Forwarded-For": "203.0.113.11"},  # different forwarded-for
        client="10.0.0.4",  # same actual client
    )
    routes._handle_tts(h2, None)
    assert h2.status == 429  # same actual client, throttled


def test_tts_rate_limit_can_trust_forwarded_for_when_opted_in(monkeypatch):
    """With HERMES_WEBUI_TRUST_FORWARDED_FOR=1, different forwarded-for = different clients."""
    monkeypatch.setenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "1")
    _mock_text_to_speech_tool(monkeypatch)
    h1 = _post(
        {"text": "hello"},
        headers={"X-Forwarded-For": "203.0.113.12"},
        client="10.0.0.5",
    )
    routes._handle_tts(h1, None)
    assert h1.status == 200

    h2 = _post(
        {"text": "hello"},
        headers={"X-Forwarded-For": "203.0.113.13"},  # different forwarded-for
        client="10.0.0.5",
    )
    routes._handle_tts(h2, None)
    assert h2.status == 200  # different forwarded-for, not throttled


def test_tts_includes_exception_details_on_failure(monkeypatch):
    """When text_to_speech_tool raises, the error message includes exception type and details.

    Provider libraries can fail at import time (missing CUDA, espeak data
    paths, broken dependencies). The error must surface what actually broke
    so the user can diagnose the problem.
    """
    def fake_tts_raises(text, output_path=None, **_):
        raise OSError("libcudart.so.13: cannot open shared object file")

    _mock_text_to_speech_tool(monkeypatch, side_effect=fake_tts_raises)

    h = _post({"text": "hello"}, client="10.0.0.15")
    routes._handle_tts(h, None)

    assert h.status == 500
    error = (h.payload() or {}).get("error", "")
    assert "OSError" in error, f"Error must include exception type, got: {error}"
    assert "libcudart" in error, f"Error must include exception details, got: {error}"
