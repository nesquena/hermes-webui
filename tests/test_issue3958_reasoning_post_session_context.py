from pathlib import Path
import json
import re
import shutil
import subprocess

import api.config as cfg
import api.models as models
import api.profiles as profiles
import pytest
import yaml


def read(path):
    return Path(path).read_text(encoding="utf-8")


def _run_reasoning_context(session, transition=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    assert node is not None
    driver = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
function extractFunc(name) {
  const start = src.search(new RegExp('function\\s+' + name + '\\s*\\('));
  let i = src.indexOf('{', start), depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
const input = JSON.parse(process.argv[2]);
const sandbox = {
  S: {session: input.session},
  $: () => null,
  _profileTransitionReasoningContext: input.transition,
};
vm.createContext(sandbox);
vm.runInContext(extractFunc('_reasoningEffortContext'), sandbox);
const body = Object.assign({effort: 'high'}, sandbox._reasoningEffortContext());
process.stdout.write(JSON.stringify(body));
"""
    result = subprocess.run(
        [
            node,
            "-e",
            driver,
            "static/ui.js",
            json.dumps({"session": session, "transition": transition}),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


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


def test_reasoning_post_body_includes_normal_session_base_url():
    body = _run_reasoning_context(
        {
            "model": "local-model",
            "model_provider": "lmstudio",
            "base_url": "http://127.0.0.1:1234/v1",
        }
    )

    assert body == {
        "effort": "high",
        "model": "local-model",
        "provider": "lmstudio",
        "base_url": "http://127.0.0.1:1234/v1",
    }


def test_reasoning_post_body_includes_profile_transition_base_url():
    body = _run_reasoning_context(
        {
            "model": "stale-model",
            "model_provider": "openai",
            "profile": "old-profile",
        },
        {
            "profile": "new-profile",
            "model": "transition-model",
            "provider": "lmstudio",
            "base_url": "http://127.0.0.1:4321/v1",
        },
    )

    assert body == {
        "effort": "high",
        "model": "transition-model",
        "provider": "lmstudio",
        "base_url": "http://127.0.0.1:4321/v1",
    }


def test_new_profile_session_exposes_configured_model_base_url(tmp_path, monkeypatch):
    profile_home = tmp_path / "profile-home"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": "local-model",
                    "provider": "lmstudio",
                    "base_url": "http://127.0.0.1:1234/v1",
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "api.profiles.get_hermes_home_for_profile", lambda _profile: profile_home
    )
    monkeypatch.setattr(models, "get_last_workspace", lambda: tmp_path)

    session = models.new_session(profile="local-profile")
    try:
        assert session.compact()["base_url"] == "http://127.0.0.1:1234/v1"
    finally:
        with models.LOCK:
            models.SESSIONS.pop(session.session_id, None)


def test_profile_switch_response_exposes_configured_model_base_url(
    tmp_path, monkeypatch
):
    profile_home = tmp_path / "profile-home"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": "local-model",
                    "provider": "lmstudio",
                    "base_url": "http://127.0.0.1:4321/v1",
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(profiles, "_resolve_named_profile_home", lambda _name: profile_home)
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [])

    result = profiles.switch_profile("local-profile", process_wide=False)

    assert result["default_model_base_url"] == "http://127.0.0.1:4321/v1"


def test_profile_switch_callers_forward_model_base_url_to_reasoning_context():
    for path in ("static/panels.js", "static/sessions.js"):
        src = read(path)
        assert (
            "refreshProfileTransitionReasoningChip("
            "data.default_model,data.default_model_provider,"
            "data.default_model_base_url)"
        ) in src
    assert "S.session.base_url=data.default_model_base_url||null;" in read(
        "static/panels.js"
    )


def test_reasoning_slash_command_posts_active_model_context():
    src = read("static/commands.js")
    block = src.split("function cmdReasoning(args){", 1)[1].split(
        "async function cmdBug", 1
    )[0]
    assert "_reasoningEffortContext()" in block
    assert "Object.assign({effort:arg},_reasoningEffortContext())" in block


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
