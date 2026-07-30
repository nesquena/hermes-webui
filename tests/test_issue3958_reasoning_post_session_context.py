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
    assert 'set_reasoning_effort(' in body
    assert "model_id=model_id" in body
    assert "provider_id=provider_id" in body
