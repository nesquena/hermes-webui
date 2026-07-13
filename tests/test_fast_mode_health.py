from __future__ import annotations


def _flatten_strings(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _flatten_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _flatten_strings(v)
    elif isinstance(value, str):
        yield value


def test_fast_health_defaults_are_truthful_and_sanitized(monkeypatch):
    from api.fast_mode import health_payload

    monkeypatch.delenv("HERMES_WEBUI_FAST_MODE", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_FAST_MODE_KIND", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "leaky_secret_value")
    monkeypatch.setenv("HERMES_HOME", "/private/home/user/.hermes")

    payload = health_payload()

    assert payload["ok"] is True
    assert payload["fast_mode"]["enabled"] is False
    assert payload["fast_mode"]["mode"] == "disabled"
    assert payload["foreground"]["normal_composer_fast_toggle"] is True
    assert payload["foreground"]["strict_no_tools_enforced"] is False
    assert payload["background"]["durable_task_store"] is True
    assert payload["background"]["parent_transcript_return"] is True
    assert payload["background"]["parent_transcript_return_mode"] == "durable_parent_cards"
    assert payload["background"]["live_update_event"] == "background_task_updated"
    assert payload["background"]["normal_composer_auto_launch"] is True
    assert payload["acceptance"]["synthetic_provider_counts_as_acceptance"] is False

    rendered = "\n".join(_flatten_strings(payload))
    assert "leaky_secret_value" not in rendered
    assert "/private/home/user" not in rendered
    assert ".hermes" not in rendered


def test_fast_health_enabled_mode_is_enum_limited(monkeypatch):
    from api.fast_mode import health_payload

    monkeypatch.setenv("HERMES_WEBUI_FAST_MODE", "1")
    monkeypatch.setenv("HERMES_WEBUI_FAST_MODE_KIND", "real_model_prototype")
    assert health_payload()["fast_mode"] == {
        "version": 1,
        "enabled": True,
        "mode": "real_model_prototype",
    }

    monkeypatch.setenv("HERMES_WEBUI_FAST_MODE_KIND", "../../secret-mode")
    payload = health_payload()
    assert payload["fast_mode"]["enabled"] is True
    assert payload["fast_mode"]["mode"] == "host_smoke"


def test_fast_request_requires_capability_and_strict_truthy_value(monkeypatch):
    from api.fast_mode import request_enabled

    monkeypatch.delenv("HERMES_WEBUI_FAST_MODE", raising=False)
    assert request_enabled(True) is False
    assert request_enabled("true") is False

    monkeypatch.setenv("HERMES_WEBUI_FAST_MODE", "1")
    for value in (True, 1, "1", "true", "yes", "on", " TRUE "):
        assert request_enabled(value) is True
    for value in (False, 0, None, "", "0", "false", "off", "no", [], {}):
        assert request_enabled(value) is False


def test_fast_health_route_is_registered():
    from pathlib import Path

    routes = Path("api/routes.py").read_text(encoding="utf-8")
    assert 'parsed.path == "/api/fast/health"' in routes
    assert "health_payload" in routes


def test_fast_mode_composer_surface_and_background_launch_are_registered():
    from pathlib import Path

    index = Path("static/index.html").read_text(encoding="utf-8")
    messages = Path("static/messages.js").read_text(encoding="utf-8")

    assert 'id="fastModePill"' in index
    assert 'onclick="toggleFastMode()"' in index
    assert "hermes-webui-fast-mode-enabled" in messages
    assert "_fastModeBackgroundPrompt" in messages
    assert "fast_mode:fastModeActive||undefined" in messages
    assert "void _launchFastModeBackground(activeSid,fastModeOriginalPrompt)" in messages
    assert "api('/api/background'" in messages
    assert "const fastModeActive=!!(msgText&&" in messages
    assert "S.busy || S.activeStreamId || (typeof INFLIGHT !== 'undefined' && INFLIGHT[sid])" in messages
    assert "const data=await api('/api/fast/health')" in messages
    assert "if(!_fastModeCapabilityEnabled)return false" in messages
    assert "btn.disabled=!_fastModeCapabilityEnabled" in messages


def test_fast_mode_foreground_guidance_is_ephemeral_not_user_message():
    from pathlib import Path

    routes = Path("api/routes.py").read_text(encoding="utf-8")
    streaming = Path("api/streaming.py").read_text(encoding="utf-8")
    gateway = Path("api/gateway_chat.py").read_text(encoding="utf-8")
    messages = Path("static/messages.js").read_text(encoding="utf-8")

    assert '"fast_mode": _fast_mode_request_enabled(body.get("fast_mode"))' in routes
    assert "FAST_MODE_FOREGROUND_GUIDANCE" in streaming
    assert "FAST_MODE_FOREGROUND_GUIDANCE" in gateway
    assert "_fastModeForegroundPrompt" not in messages
