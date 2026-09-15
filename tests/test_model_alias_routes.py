"""Behavioral coverage for canonical Hermes model aliases in WebUI."""

import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent
NODE = shutil.which("node")


_NODE_ALIAS_DRIVER = r"""
const fs = require('fs');
const cmds = fs.readFileSync(process.argv[2], 'utf8');
const ui = fs.readFileSync(process.argv[3], 'utf8');
function extractFunc(src, name){
  const start=src.search(new RegExp('function\\s+'+name+'\\s*\\('));
  if(start<0) throw new Error(name+' not found');
  let i=src.indexOf('{',start), depth=1; i++;
  while(depth>0&&i<src.length){if(src[i]==='{')depth++;else if(src[i]==='}')depth--;i++;}
  return src.slice(start,i);
}
for(const name of ['_buildModelCandidates','_resolveModelAliasTarget','_looksLikeVersionedModel','_bestModelMatch','_nearestModelSuggestion']) eval('globalThis.'+name+'='+extractFunc(cmds,name));
for(const name of ['_providerFromModelValue','_getOptionProviderId','_modelStateForSelect','_ensureModelOptionInDropdown','_findModelInDropdown']) eval('globalThis.'+name+'='+extractFunc(ui,name));
eval('globalThis.cmdModel=async '+extractFunc(cmds,'cmdModel'));
function makeOption(value,provider='',model=''){return {value,textContent:value,dataset:{provider,model}};}
const result={persisted:null};
const sel={
  id:'modelSelect',
  options:[
    makeOption('openai/gpt-5.6-sol','openrouter','openai/gpt-5.6-sol'),
    makeOption('@openrouter:gpt-4','openrouter','gpt-4'),
    makeOption('@anthropic:gpt-4','anthropic','gpt-4'),
  ],
  value:'openai/gpt-5.6-sol',
  appendChild(opt){this.options.push(opt);},
  onchange:async()=>{const state=_modelStateForSelect(sel,sel.value);result.persisted=state;},
};
function $(id){return id==='modelSelect'?sel:null;} function t(k){return k;} function showToast(){}
function _applyModelToDropdown(){return null;} function _refreshOpenModelDropdown(){} function syncModelChip(){} function getModelLabel(v){return v;}
const S={session:{session_id:'test',model_provider:'openrouter'}};
const window={_activeProvider:'openrouter',_configuredModelBadges:{}};
const document={baseURI:'http://localhost/',createElement(){return makeOption('');}};
const location={href:'http://localhost/'};
const payload=JSON.parse(process.argv[4]);
async function fetch(){return {ok:true,json:async()=>payload};}
(async () => {
  await cmdModel(process.argv[5]);
  console.log(JSON.stringify(result));
})();
"""


def _run_cmd_model_alias_driver(tmp_path, payload, alias):
    driver = tmp_path / "alias_driver.js"
    driver.write_text(_NODE_ALIAS_DRIVER, encoding="utf-8")
    return subprocess.run(
        [
            NODE,
            str(driver),
            str(REPO_ROOT / "static/commands.js"),
            str(REPO_ROOT / "static/ui.js"),
            json.dumps(payload),
            alias,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_model_catalog_exposes_sanitized_canonical_alias_routes(monkeypatch):
    from api import config

    monkeypatch.setattr(config, "cfg", {
        "model_aliases": {
            "sol": {
                "model": "gpt-5.6-sol",
                "provider": "openai-codex",
                "base_url": "https://codex.example.test/v1",
                "api_key": "canonical-secret",
                "key_env": "CANONICAL_KEY",
            },
        },
        "model": {
            "provider": "openrouter",
            "default": "openai/gpt-5.6-sol",
            "aliases": {
                "sol": "openrouter/openai/gpt-5.6-sol",
                "legacy": "anthropic/claude-sonnet-4.6",
            },
        },
    })

    payload = config._annotate_fast_tier_model_groups({"groups": []})
    aliases = payload["model_alias_routes"]

    assert aliases["sol"]["model"] == "gpt-5.6-sol"
    assert aliases["sol"]["provider"] == "openai-codex"
    assert aliases["sol"]["route_provider"].startswith("model-alias-")
    assert aliases["legacy"]["model"] == "claude-sonnet-4.6"
    assert aliases["legacy"]["provider"] == "anthropic"
    serialized = json.dumps(aliases)
    assert "canonical-secret" not in serialized
    assert "CANONICAL_KEY" not in serialized
    assert "codex.example.test" not in serialized


def test_model_catalog_routes_only_explicit_legacy_identities(monkeypatch):
    from api import config

    monkeypatch.setattr(config, "cfg", {
        "model_aliases": {
            "canonical": {"model": "canonical-model", "provider": "openai"},
        },
        "model": {
            "provider": "openrouter",
            "aliases": {
                "plain": "gpt-4",
                "qualified": "anthropic/claude-sonnet-4.6",
                "malformed_qualified": "/gpt-4",
                "implicit_structured": {"model": "gpt-4.1"},
                "provider_structured": {"model": "gpt-4.1", "provider": "openai"},
                "endpoint_structured": {
                    "model": "local-model",
                    "base_url": "http://127.0.0.1:11434/v1",
                },
            },
        },
    })

    payload = config._annotate_fast_tier_model_groups({
        "groups": [],
        "aliases": config._model_aliases_from_config(),
    })

    assert payload["aliases"] == {
        "plain": "gpt-4",
        "qualified": "anthropic/claude-sonnet-4.6",
        "malformed_qualified": "/gpt-4",
    }
    assert set(payload["model_alias_routes"]) == {
        "canonical",
        "qualified",
        "provider_structured",
        "endpoint_structured",
    }
    assert "plain" not in payload["model_alias_routes"]
    assert "implicit_structured" not in payload["model_alias_routes"]


def test_custom_alias_route_resolves_exact_endpoint_and_credential(monkeypatch):
    from api import config

    configured = {
        "model_aliases": {
            "east": {
                "model": "shared-model",
                "provider": "custom",
                "base_url": "https://east.example.test/v1",
                "key_env": "EAST_ALIAS_KEY",
            },
            "west": {
                "model": "shared-model",
                "provider": "custom",
                "base_url": "https://west.example.test/v1",
                "api_key": "west-secret",
            },
        },
    }
    monkeypatch.setattr(config, "cfg", configured)
    monkeypatch.setenv("EAST_ALIAS_KEY", "east-secret")
    aliases = {
        "east": types.SimpleNamespace(model="shared-model", provider="custom", base_url="https://east.example.test/v1"),
        "west": types.SimpleNamespace(model="shared-model", provider="custom", base_url="https://west.example.test/v1"),
    }
    fake_switch = types.SimpleNamespace(
        _load_direct_aliases=lambda: aliases,
        direct_alias_runtime_request=lambda alias: (
            "custom",
            "east-secret" if alias is aliases["east"] else "west-secret",
        ),
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.model_switch", fake_switch)

    public = config._public_model_alias_routes()
    east = config.resolve_model_alias_runtime(public["east"]["route_provider"], "shared-model")
    west = config.resolve_model_alias_runtime(public["west"]["route_provider"], "shared-model")

    assert east == {
        "model": "shared-model",
        "provider": "custom",
        "base_url": "https://east.example.test/v1",
        "api_key": "east-secret",
        "key_env": "",
        "alias": "east",
    }
    assert west["base_url"] == "https://west.example.test/v1"
    assert west["api_key"] == "west-secret"
    assert public["east"]["route_provider"] != public["west"]["route_provider"]


def test_alias_runtime_fallback_supports_older_agent_loader(monkeypatch):
    from api import config

    monkeypatch.setattr(config, "cfg", {
        "model_aliases": {
            "local": {
                "model": "qwen-local",
                "provider": "custom",
                "base_url": "http://127.0.0.1:11434/v1",
                "key_env": "LOCAL_ALIAS_KEY",
            },
        },
    })
    monkeypatch.setenv("LOCAL_ALIAS_KEY", "local-secret")
    monkeypatch.setitem(sys.modules, "hermes_cli.model_switch", types.SimpleNamespace())
    route = config._public_model_alias_routes()["local"]["route_provider"]

    assert config.resolve_model_alias_runtime(route, "qwen-local") == {
        "model": "qwen-local",
        "provider": "custom",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "local-secret",
        "key_env": "LOCAL_ALIAS_KEY",
        "alias": "local",
    }
    assert config.resolve_model_alias_runtime(route, "different-model") is None


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_cmd_model_prefers_canonical_alias_route(tmp_path):
    result = _run_cmd_model_alias_driver(tmp_path, {
        "aliases": {"sol": "openrouter/openai/gpt-5.6-sol"},
        "model_alias_routes": {
            "sol": {
                "model": "gpt-5.6-sol",
                "provider": "openai-codex",
                "route_provider": "model-alias-canonical",
            },
        },
        "groups": [{
            "provider_id": "openrouter",
            "models": [{"id": "openai/gpt-5.6-sol"}],
        }],
    }, "sol")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["persisted"] == {
        "model": "gpt-5.6-sol",
        "model_provider": "model-alias-canonical",
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_cmd_model_falls_back_to_unqualified_scalar_alias(tmp_path):
    result = _run_cmd_model_alias_driver(tmp_path, {
        "aliases": {"fast": "gpt-4"},
        "model_alias_routes": {
            "other": {
                "model": "other-model",
                "provider": "anthropic",
                "route_provider": "model-alias-other",
            },
        },
        "groups": [
            {"provider_id": "openrouter", "models": [{"id": "@openrouter:gpt-4"}]},
            {"provider_id": "anthropic", "models": [{"id": "@anthropic:gpt-4"}]},
        ],
    }, "fast")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["persisted"] == {
        "model": "gpt-4",
        "model_provider": "openrouter",
    }


def _canonical_runtime_route():
    return {
        "alias": "east",
        "model": "shared-model",
        "provider": "custom",
        "base_url": "https://east.example.test/v1",
        "api_key": "east-secret",
        "key_env": "",
    }


def _start_run_kwargs():
    return {
        "msg": "hello",
        "attachments": [],
        "workspace": "/tmp/workspace",
        "model": "shared-model",
        "model_provider": "model-alias-canonical",
        "normalized_model": False,
        "source": "webui",
        "route": "/api/chat/start",
    }


def test_legacy_dispatch_receives_canonical_alias_endpoint_and_credential(monkeypatch):
    from api import routes

    captured = {}
    session = types.SimpleNamespace(session_id="session-1", profile=None)
    monkeypatch.setattr(routes.api_config, "resolve_model_alias_runtime", lambda *_args, **_kwargs: _canonical_runtime_route())
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: False)
    monkeypatch.setattr(
        routes,
        "_start_chat_stream_for_session",
        lambda _session, **kwargs: captured.update(kwargs) or {"stream_id": "legacy-1", "session_id": "session-1"},
    )

    routes._start_run(session, **_start_run_kwargs())

    assert captured["model"] == "shared-model"
    assert captured["model_provider"] == "custom"
    assert captured["runtime_base_url"] == "https://east.example.test/v1"
    assert captured["runtime_api_key"] == "east-secret"


def test_gateway_dispatch_uses_alias_identity_for_gateway_model_route(monkeypatch):
    from api import routes

    captured = {}
    session = types.SimpleNamespace(session_id="session-1", profile=None)
    monkeypatch.setattr(routes.api_config, "resolve_model_alias_runtime", lambda *_args, **_kwargs: _canonical_runtime_route())
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: False)
    monkeypatch.setattr(
        routes,
        "_start_chat_stream_for_session",
        lambda _session, **kwargs: captured.update(kwargs) or {"stream_id": "gateway-1", "session_id": "session-1"},
    )

    routes._start_run(session, gateway_chat_enabled=True, **_start_run_kwargs())

    assert captured["external_runtime_owned"] is True
    assert captured["model"] == "east"
    assert captured["model_provider"] is None
    assert captured["runtime_api_key"] is None
    assert captured["runtime_base_url"] is None


def test_runner_dispatch_uses_alias_identity_in_start_run_contract(monkeypatch):
    from api import routes

    captured = []

    class RunnerClient:
        def start_run(self, request):
            captured.append(request)
            return {"run_id": "run-1", "stream_id": "stream-1", "session_id": request.session_id}

    session = types.SimpleNamespace(session_id="session-1", profile=None)
    monkeypatch.setattr(routes.api_config, "resolve_model_alias_runtime", lambda *_args, **_kwargs: _canonical_runtime_route())
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "runner-local")
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: True)
    monkeypatch.setattr(routes, "_runtime_runner_client_factory", lambda: RunnerClient())

    routes._start_run(session, **_start_run_kwargs())

    assert len(captured) == 1
    assert captured[0].model == "east"
    assert captured[0].provider is None
    assert "east-secret" not in json.dumps(captured[0].metadata)