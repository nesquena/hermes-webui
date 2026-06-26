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
    assert payload["foreground"]["strict_no_tools_enforced"] is False
    assert payload["background"]["durable_task_store"] is True
    assert payload["background"]["parent_transcript_return"] is False
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


def test_fast_health_route_is_registered():
    from pathlib import Path

    routes = Path("api/routes.py").read_text(encoding="utf-8")
    assert 'parsed.path == "/api/fast/health"' in routes
    assert "health_payload" in routes
