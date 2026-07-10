from pathlib import Path
import re

import api.config as cfg
import yaml


def read(path):
    return Path(path).read_text(encoding="utf-8")


def test_set_reasoning_effort_returns_status_for_explicit_model(tmp_path, monkeypatch):
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(
        yaml.safe_dump(
            {
                "model": {"default": "gpt-4o", "provider": "openai"},
                "agent": {"reasoning_effort": ""},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg, "_get_config_path", lambda: cfgfile)
    monkeypatch.setattr(cfg, "reload_config", lambda: None)

    seen = {}

    def fake_resolve(model_id, provider_id=None, base_url=None):
        seen["args"] = (model_id, provider_id, base_url)
        if model_id == "claude-opus-4-7":
            return ["minimal", "low", "medium", "high", "xhigh", "max"]
        return []

    monkeypatch.setattr(cfg, "resolve_model_reasoning_efforts", fake_resolve)

    status = cfg.set_reasoning_effort(
        "high",
        model_id="claude-opus-4-7",
        provider_id="anthropic",
    )

    assert seen["args"] == ("claude-opus-4-7", "anthropic", None)
    assert status["reasoning_effort"] == "high"
    assert status["supported_efforts"] == [
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]


def test_ui_posts_reasoning_context_with_effort():
    src = read("static/ui.js")
    assert "function _reasoningEffortContext()" in src
    assert "new URLSearchParams(_reasoningEffortContext())" in src
    assert "Object.assign({effort:effort},_reasoningEffortContext())" in src


def test_slash_reasoning_posts_active_session_context():
    src = read("static/commands.js")
    match = re.search(r"function cmdReasoning\(.*?\n\}", src, re.DOTALL)
    assert match
    command = match.group(0)

    assert "_reasoningEffortContext()" in command
    assert "Object.assign({effort:arg},context)" in command


def test_reasoning_post_route_threads_model_context():
    src = read("api/routes.py")
    match = re.search(
        r"if parsed\.path == \"/api/reasoning\":(.*?)return bad\(handler, \"reasoning: must supply 'display' or 'effort'\"\)",
        src,
        re.DOTALL,
    )
    assert match, "The /api/reasoning POST route block must exist"
    body = match.group(1)
    assert 'body.get("model")' in body
    assert 'body.get("provider")' in body
    assert 'body.get("base_url")' in body
    assert 'set_reasoning_effort(' in body
    assert "model_id=model_id" in body
    assert "provider_id=provider_id" in body
    assert "base_url=base_url" in body


def test_named_custom_provider_base_url_is_resolvable(monkeypatch):
    monkeypatch.setattr(
        cfg,
        "cfg",
        {
            "custom_providers": [
                {
                    "name": "Local Lab",
                    "base_url": "http://127.0.0.1:1234/v1/",
                    "models": {"lab-model": {}},
                }
            ]
        },
    )

    assert cfg._get_provider_base_url("custom:local-lab") == "http://127.0.0.1:1234/v1"


def test_reasoning_resolves_provider_base_url_server_side(monkeypatch):
    monkeypatch.setattr(
        cfg,
        "cfg",
        {
            "providers": {
                "lmstudio": {"base_url": "http://127.0.0.1:1234/v1"}
            }
        },
    )
    seen = {}

    def fake_options(model, base_url, **kwargs):
        seen["args"] = (model, base_url)
        return ["low", "high"]

    monkeypatch.setattr(cfg, "_lmstudio_model_reasoning_options", fake_options)

    assert cfg.resolve_model_reasoning_efforts(
        "lab-model", provider_id="lmstudio"
    ) == ["low", "high"]
    assert seen["args"] == ("lab-model", "http://127.0.0.1:1234/v1")
