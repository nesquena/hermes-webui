"""Behavioral coverage for canonical Hermes model aliases in WebUI."""

import contextlib
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
        "base_url_explicit": True,
        "credential_explicit": True,
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
        "base_url_explicit": True,
        "credential_explicit": True,
    }
    assert config.resolve_model_alias_runtime(route, "different-model") is None


def _named_custom_provider_config():
    return {
        "custom_providers": [{
            "name": "West",
            "base_url": "https://provider-west.example.test/v1",
            "api_key": "provider-west-secret",
        }],
    }


def test_alias_endpoint_and_credential_override_named_custom_provider(monkeypatch):
    from api import config

    configured = _named_custom_provider_config()
    monkeypatch.setattr(config, "cfg", configured)
    monkeypatch.setattr(config, "get_config", lambda: configured)
    alias_route = {
        "alias": "west-special",
        "model": "shared-model",
        "provider": "custom:west",
        "base_url": "https://alias-west.example.test/v1",
        "api_key": "alias-west-secret",
        "base_url_explicit": True,
        "credential_explicit": True,
    }
    runtime = {
        "provider": "openrouter",
        "base_url": "https://unrelated.example.test/v1",
        "api_key": "unrelated-secret",
        "credential_pool": object(),
        "api_mode": "responses",
    }

    bundle = config.merge_model_alias_runtime_bundle(alias_route, runtime)

    assert bundle["provider"] == "custom"
    assert bundle["base_url"] == "https://alias-west.example.test/v1"
    assert bundle["api_key"] == "alias-west-secret"
    assert bundle["credential_pool"] is None
    assert bundle["api_mode"] is None
    assert "provider-west-secret" not in str(bundle)
    assert "unrelated-secret" not in str(bundle)


def test_alias_endpoint_without_credential_never_borrows_custom_provider_key(monkeypatch):
    from api import config

    configured = _named_custom_provider_config()
    monkeypatch.setattr(config, "cfg", configured)
    monkeypatch.setattr(config, "get_config", lambda: configured)
    alias_route = {
        "alias": "west-keyless",
        "model": "shared-model",
        "provider": "custom:west",
        "base_url": "https://alias-keyless.example.test/v1",
        "api_key": "",
        "base_url_explicit": True,
        "credential_explicit": False,
    }

    bundle = config.merge_model_alias_runtime_bundle(alias_route, {
        "provider": "custom:west",
        "base_url": "https://provider-west.example.test/v1",
        "api_key": "provider-west-secret",
    })

    assert bundle["base_url"] == "https://alias-keyless.example.test/v1"
    assert bundle["api_key"] == config.KEYLESS_CUSTOM_API_KEY
    assert "provider-west-secret" not in str(bundle)


def test_provider_only_alias_uses_named_custom_provider_connection(monkeypatch):
    from api import config

    configured = _named_custom_provider_config()
    monkeypatch.setattr(config, "cfg", configured)
    monkeypatch.setattr(config, "get_config", lambda: configured)
    alias_route = {
        "alias": "west-default",
        "model": "shared-model",
        "provider": "custom:west",
        "base_url": "",
        "api_key": "",
        "base_url_explicit": False,
        "credential_explicit": False,
    }

    bundle = config.merge_model_alias_runtime_bundle(alias_route, {
        "provider": "openrouter",
        "base_url": "https://unrelated.example.test/v1",
        "api_key": "unrelated-secret",
    })

    assert bundle["provider"] == "custom"
    assert bundle["base_url"] == "https://provider-west.example.test/v1"
    assert bundle["api_key"] == "provider-west-secret"
    assert "unrelated-secret" not in str(bundle)


def test_credential_only_alias_overrides_named_custom_provider_key(monkeypatch):
    from api import config

    configured = _named_custom_provider_config()
    monkeypatch.setattr(config, "cfg", configured)
    monkeypatch.setattr(config, "get_config", lambda: configured)
    alias_route = {
        "alias": "west-account-two",
        "model": "shared-model",
        "provider": "custom:west",
        "base_url": "",
        "api_key": "alias-account-secret",
        "base_url_explicit": False,
        "credential_explicit": True,
    }

    bundle = config.merge_model_alias_runtime_bundle(alias_route, {
        "provider": "custom:west",
        "base_url": "https://provider-west.example.test/v1",
        "api_key": "provider-west-secret",
        "credential_pool": object(),
    })

    assert bundle["base_url"] == "https://provider-west.example.test/v1"
    assert bundle["api_key"] == "alias-account-secret"
    assert bundle["credential_pool"] is None
    assert "provider-west-secret" not in str(bundle)


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
        "base_url_explicit": True,
        "credential_explicit": True,
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


def test_legacy_dispatch_keeps_opaque_alias_lane_until_profile_scoped_resolution(monkeypatch):
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
    assert captured["model_provider"] == "model-alias-canonical"
    assert "runtime_base_url" not in captured
    assert "runtime_api_key" not in captured
    assert "east-secret" not in json.dumps(captured)


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
    assert "runtime_api_key" not in captured
    assert "runtime_base_url" not in captured


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


# ─────────────────────────────────────────────────────────────────────────────
# Server-initiated turns (start_session_turn) must reach the same alias routing
# as a human /api/chat/start turn.
#
# /api/chat/start computes gateway ownership from its request-scoped config
# snapshot and passes it explicitly; the wakeup path left the argument unset and
# `_start_chat_stream_for_session` only discovered gateway mode AFTER the
# alias-lane conversion had been skipped — so a gateway-backed wakeup handed the
# external runtime the opaque lane instead of the alias name.
# ─────────────────────────────────────────────────────────────────────────────


def _stub_start_session_turn(
    monkeypatch,
    *,
    profile=None,
    provider="model-alias-canonical",
    real_config=False,
):
    from api import routes as routes_mod

    session = types.SimpleNamespace(
        session_id="sess-alias-wake",
        model="shared-model",
        model_provider=provider,
        profile=profile,
        workspace="/tmp/ws-test",
    )
    monkeypatch.setattr(routes_mod, "get_session", lambda _sid: session)
    monkeypatch.setattr(
        routes_mod, "_resolve_chat_workspace_with_recovery", lambda _s, _req: "/tmp/ws-test"
    )
    monkeypatch.setattr(
        routes_mod,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **kwargs: (model, provider, False),
    )
    if not real_config:
        # Pin the config snapshot so gateway ownership is decided by the test's
        # env, not by the suite's own state-dir config. Tests that need the real
        # profile config read leave this unset.
        monkeypatch.setattr(
            routes_mod,
            "get_config_snapshot",
            lambda: {},
        )
    import api.background_process as bp_mod

    monkeypatch.setattr(bp_mod, "get_session_channel", lambda _sid: None)
    return routes_mod


def _capture_legacy_dispatch(monkeypatch, routes_mod):
    captured = {}
    monkeypatch.setattr(
        routes_mod,
        "_start_chat_stream_for_session",
        lambda _session, **kwargs: captured.update(kwargs)
        or {"_status": 200, "stream_id": "stream-wake", "session_id": "sess-alias-wake"},
    )
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: False)
    return captured


def test_start_session_turn_gateway_wakeup_converts_alias_lane(monkeypatch):
    """A gateway-backed wakeup routes the alias NAME, not the opaque lane."""
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    routes_mod = _stub_start_session_turn(monkeypatch)
    captured = _capture_legacy_dispatch(monkeypatch, routes_mod)
    monkeypatch.setattr(
        routes_mod.api_config,
        "resolve_model_alias_runtime",
        lambda *_args, **_kwargs: _canonical_runtime_route(),
    )

    resp = routes_mod.start_session_turn("sess-alias-wake", "wakeup")

    assert resp["_status"] == 200
    assert captured["model"] == "east", (
        "the external runtime's request contract is the alias route, not the "
        "session's stored model"
    )
    assert captured["model_provider"] is None
    assert captured["external_runtime_owned"] is True
    assert "east-secret" not in json.dumps(captured)


def test_start_session_turn_legacy_wakeup_keeps_opaque_alias_lane(monkeypatch):
    """Without gateway ownership the legacy worker still resolves the lane itself."""
    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)
    routes_mod = _stub_start_session_turn(monkeypatch)
    captured = _capture_legacy_dispatch(monkeypatch, routes_mod)
    monkeypatch.setattr(
        routes_mod.api_config,
        "resolve_model_alias_runtime",
        lambda *_args, **_kwargs: _canonical_runtime_route(),
    )

    resp = routes_mod.start_session_turn("sess-alias-wake", "wakeup")

    assert resp["_status"] == 200
    assert captured["model"] == "shared-model"
    assert captured["model_provider"] == "model-alias-canonical"
    assert captured["external_runtime_owned"] is False


def test_start_session_turn_gateway_detection_uses_session_profile_scope(monkeypatch):
    """A named profile's backend setting is read inside that profile's scope."""
    import contextlib

    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    routes_mod = _stub_start_session_turn(monkeypatch, profile="named-profile")
    captured = _capture_legacy_dispatch(monkeypatch, routes_mod)
    monkeypatch.setattr(
        routes_mod.api_config,
        "resolve_model_alias_runtime",
        lambda *_args, **_kwargs: _canonical_runtime_route(),
    )
    entered = []

    @contextlib.contextmanager
    def _fake_scope(profile_name, purpose="detached worker", logger_override=None):
        entered.append(profile_name)
        yield

    monkeypatch.setattr(routes_mod, "profile_scope_for_detached_worker", _fake_scope)

    routes_mod.start_session_turn("sess-alias-wake", "wakeup")

    assert entered == ["named-profile"], (
        "gateway ownership and the alias lane must be resolved under the owning "
        "session's profile scope on a thread without request profile TLS"
    )
    assert captured["model"] == "east"


def test_start_run_explicit_gateway_flag_skips_ownership_detection(monkeypatch):
    """An explicit False from the HTTP path is honored, not re-derived."""
    from api import routes

    captured = {}
    session = types.SimpleNamespace(session_id="session-1", profile=None)
    monkeypatch.setattr(
        routes.api_config,
        "resolve_model_alias_runtime",
        lambda *_args, **_kwargs: _canonical_runtime_route(),
    )
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: False)
    monkeypatch.setattr(
        routes,
        "_start_chat_stream_for_session",
        lambda _session, **kwargs: captured.update(kwargs)
        or {"stream_id": "legacy-1", "session_id": "session-1"},
    )

    routes._start_run(session, gateway_chat_enabled=False, **_start_run_kwargs())

    assert captured["external_runtime_owned"] is False
    assert captured["model"] == "shared-model"
    assert captured["model_provider"] == "model-alias-canonical"


# ─────────────────────────────────────────────────────────────────────────────
# An opaque alias lane that no longer resolves is terminal.
#
# The lane is a WebUI-minted digest, not a provider id. Letting it reach generic
# provider resolution hands the session's prompt to whatever the ambient or
# fallback chain resolves, for a route the session no longer owns.
# ─────────────────────────────────────────────────────────────────────────────

_ALIAS_WORKER_CFG = {
    "model": {"default": "active/model", "provider": "openrouter"},
    "model_aliases": {
        "east": {
            "model": "shared-model",
            "provider": "custom",
            "base_url": "https://east.example.test/v1",
            "api_key": "east-secret",
        },
    },
}

_ALIAS_WORKER_RUNTIME = {
    "provider": "openrouter",
    "base_url": "https://ambient.example.test/v1",
    "api_key": "ambient-secret",
}


def _setup_alias_worker(monkeypatch, cfg_dict, session_id="session-alias-1"):
    """Compose the production streaming send path around a capturing agent."""
    import queue
    from unittest import mock

    import api.oauth
    import api.streaming as streaming
    from api import config as worker_config

    with worker_config.SESSION_AGENT_CACHE_LOCK:
        worker_config.SESSION_AGENT_CACHE.clear()

    old_cfg = dict(worker_config.cfg)
    worker_config.cfg.clear()
    worker_config.cfg.update(cfg_dict)

    class FakeSession:
        def __init__(self):
            self.session_id = session_id
            self.title = "Test"
            self.workspace = "/tmp"
            self.model = "shared-model"
            self.messages = []
            self.personality = None
            self.input_tokens = 0
            self.output_tokens = 0
            self.estimated_cost = None
            self.tool_calls = []
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.pending_user_source = None
            self.profile = None

        def save(self, touch_updated_at=True, skip_index=False):
            self._saved = touch_updated_at

        def compact(self):
            return {"session_id": self.session_id, "messages": self.messages}

    captured = {}

    class CapturingAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key=None,
            api_mode=None,
            acp_command=None,
            acp_args=None,
            credential_pool=None,
            **kwargs,
        ):
            captured["init_kwargs"] = {
                "model": model,
                "provider": provider,
                "base_url": base_url,
                "api_key": api_key,
            }
            captured.setdefault("init_kwargs_history", []).append(dict(captured["init_kwargs"]))
            self.session_id = kwargs.get("session_id")
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            captured["run_calls"] = captured.get("run_calls", 0) + 1
            return {"messages": [{"role": "assistant", "content": "ok"}]}

        def interrupt(self, _message):
            captured["interrupted"] = _message

    fake_session = FakeSession()
    fake_stream_id = f"stream-{session_id}"
    fake_session.active_stream_id = fake_stream_id
    fake_queue = queue.Queue()

    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = mock.Mock(
        return_value=dict(_ALIAS_WORKER_RUNTIME)
    )
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = mock.Mock(return_value=object())

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)
    monkeypatch.setattr(
        api.oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda resolver, **kwargs: resolver(**kwargs),
    )
    monkeypatch.setattr(streaming, "get_session", lambda _session_id: fake_session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: CapturingAgent)
    monkeypatch.setattr("api.config.get_config", lambda: dict(worker_config.cfg))
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])

    def restore():
        with worker_config.SESSION_AGENT_CACHE_LOCK:
            worker_config.SESSION_AGENT_CACHE.clear()
        worker_config.cfg.clear()
        worker_config.cfg.update(old_cfg)
        worker_config.invalidate_models_cache()

    return streaming, fake_stream_id, fake_queue, captured, restore


def _drive_alias_worker_send(monkeypatch, cfg_dict, *, model, lane, session_id):
    import queue as _queue

    streaming, stream_id, q, captured, restore = _setup_alias_worker(
        monkeypatch, cfg_dict, session_id=session_id
    )
    try:
        streaming.STREAMS[stream_id] = q
        streaming._run_agent_streaming(
            session_id=session_id,
            msg_text="hello",
            model=model,
            model_provider=lane,
            workspace="/tmp",
            stream_id=stream_id,
        )
        apperrors = []
        while True:
            try:
                item = q.get_nowait()
            except _queue.Empty:
                break
            if item and item[0] == "apperror":
                apperrors.append(item[1])
        cache_empty = True
        from api import config as worker_config

        with worker_config.SESSION_AGENT_CACHE_LOCK:
            cache_empty = not worker_config.SESSION_AGENT_CACHE
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        restore()
    return captured, apperrors, cache_empty


def _assert_alias_lane_refused(captured, apperrors, cache_empty, label):
    assert not captured.get("init_kwargs_history"), f"{label}: an agent was constructed"
    assert not captured.get("run_calls"), f"{label}: the turn was actually sent"
    assert cache_empty, f"{label}: the agent cache was poisoned"
    assert apperrors, f"{label}: no controlled failure was emitted"
    payload = apperrors[-1]
    assert payload["type"] == "provider_unroutable", (
        f"{label}: emitted {payload['type']!r} instead of a provider-route failure"
    )
    assert payload.get("reason") == "model_alias_route_unresolved"
    assert payload.get("hint"), f"{label}: the failure named no fix"
    assert "ambient-secret" not in json.dumps(payload)
    return payload


def test_deleted_alias_lane_is_terminal(monkeypatch):
    """The alias no longer exists in the active profile's config."""
    from api import config

    lane = config._model_alias_route_provider("ghost")
    captured, apperrors, cache_empty = _drive_alias_worker_send(
        monkeypatch,
        {"model": {"default": "active/model", "provider": "openrouter"}},
        model="shared-model",
        lane=lane,
        session_id="session-alias-deleted",
    )

    payload = _assert_alias_lane_refused(captured, apperrors, cache_empty, "deleted alias")
    assert "alias" in payload["message"].lower()


def test_alias_lane_unknown_to_active_profile_is_terminal(monkeypatch):
    """A lane minted under another profile does not resolve here."""
    from api import config

    lane = config._model_alias_route_provider("west")
    captured, apperrors, cache_empty = _drive_alias_worker_send(
        monkeypatch,
        _ALIAS_WORKER_CFG,
        model="shared-model",
        lane=lane,
        session_id="session-alias-other-profile",
    )

    assert lane != config._model_alias_route_provider("east")
    _assert_alias_lane_refused(captured, apperrors, cache_empty, "unknown lane")


def test_alias_lane_model_mismatch_is_terminal(monkeypatch):
    """The alias resolves, but not to the model the session stored."""
    from api import config

    lane = config._model_alias_route_provider("east")
    captured, apperrors, cache_empty = _drive_alias_worker_send(
        monkeypatch,
        _ALIAS_WORKER_CFG,
        model="other-model",
        lane=lane,
        session_id="session-alias-mismatch",
    )

    _assert_alias_lane_refused(captured, apperrors, cache_empty, "model mismatch")


def test_hostile_alias_lane_is_terminal(monkeypatch):
    """A crafted lane that never belonged to any configured alias."""
    hostile = "model-alias-" + "f" * 64
    captured, apperrors, cache_empty = _drive_alias_worker_send(
        monkeypatch,
        _ALIAS_WORKER_CFG,
        model="shared-model",
        lane=hostile,
        session_id="session-alias-hostile",
    )

    payload = _assert_alias_lane_refused(captured, apperrors, cache_empty, "hostile lane")
    assert hostile not in json.dumps(payload)


def test_resolved_alias_lane_still_reaches_its_own_endpoint(monkeypatch):
    """Positive control: a live lane keeps routing to the alias endpoint/key."""
    from api import config

    lane = config._model_alias_route_provider("east")
    captured, apperrors, cache_empty = _drive_alias_worker_send(
        monkeypatch,
        _ALIAS_WORKER_CFG,
        model="shared-model",
        lane=lane,
        session_id="session-alias-live",
    )

    assert not apperrors
    init_kwargs = captured["init_kwargs"]
    assert init_kwargs["base_url"] == "https://east.example.test/v1"
    assert init_kwargs["api_key"] == "east-secret"
    assert init_kwargs["provider"] == "custom"
    assert captured.get("run_calls") == 1


# ─────────────────────────────────────────────────────────────────────────────
# An unresolved alias lane is refused before ANY backend dispatch.
#
# `_start_run` is the dispatcher for three backends — the runner adapter, the
# gateway, and the legacy in-process worker. Only the legacy worker resolves the
# lane itself (and fails closed inside `_run_agent_streaming`); the gateway and
# the runner receive the request verbatim, so the minted digest would reach them
# as a provider id and land the turn on an endpoint the user never picked.
# ─────────────────────────────────────────────────────────────────────────────


def _alias_cfg_with_east():
    """A config where `east` is live and every other alias name is dead."""
    return {
        "model": {"default": "shared-model", "provider": "custom"},
        "model_aliases": {
            "east": {
                "model": "shared-model",
                "provider": "custom",
                "base_url": "https://east.example.test/v1",
                "api_key": "east-secret",
            },
        },
    }


def _dead_lane():
    """A well-formed lane for an alias the active config does not define."""
    from api import config

    return config._model_alias_route_provider("ghost")


def _live_lane():
    from api import config

    return config._model_alias_route_provider("east")


def _install_alias_cfg(monkeypatch, cfg_dict=None):
    """Pin the module config the lane resolver reads, without touching disk."""
    from api import config

    monkeypatch.setattr(
        config, "cfg", dict(_alias_cfg_with_east() if cfg_dict is None else cfg_dict)
    )


def _start_run_kwargs_for_lane(lane):
    kwargs = _start_run_kwargs()
    kwargs["model_provider"] = lane
    return kwargs


def _assert_alias_lane_refusal(resp, lane, label, *, status=None):
    actual_status = resp["_status"] if status is None else status
    assert actual_status == 400, f"{label}: expected a controlled refusal, got {resp!r}"
    assert resp["type"] == "provider_unroutable", (
        f"{label}: emitted {resp.get('type')!r} instead of a provider-route failure"
    )
    assert resp["reason"] == "model_alias_route_unresolved"
    assert resp["hint"], f"{label}: the refusal named no fix"
    assert resp["error"], f"{label}: the refusal carried no message"
    assert lane not in json.dumps(resp), f"{label}: the refusal echoed the opaque lane"
    assert "secret" not in json.dumps(resp), f"{label}: the refusal leaked a credential"
    return resp


def test_gateway_dispatch_refuses_unresolved_alias_lane(monkeypatch):
    """The gateway backend is never handed a lane it cannot route."""
    from api import routes

    _install_alias_cfg(monkeypatch)
    lane = _dead_lane()
    invoked = []
    session = types.SimpleNamespace(session_id="session-1", profile=None)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: False)
    monkeypatch.setattr(
        routes,
        "_start_chat_stream_for_session",
        lambda _session, **kwargs: invoked.append(kwargs)
        or {"stream_id": "gateway-1", "session_id": "session-1"},
    )

    resp = routes._start_run(
        session, gateway_chat_enabled=True, **_start_run_kwargs_for_lane(lane)
    )

    assert invoked == [], "the gateway backend was invoked with an unroutable alias lane"
    _assert_alias_lane_refusal(resp, lane, "gateway dispatch")


def test_runner_dispatch_refuses_unresolved_alias_lane(monkeypatch):
    """Neither the runner client nor its factory is reached for a dead lane."""
    from api import routes

    _install_alias_cfg(monkeypatch)
    lane = _dead_lane()
    started = []
    factories = []

    class RunnerClient:
        def start_run(self, request):
            started.append(request)
            return {"run_id": "run-1", "stream_id": "stream-1", "session_id": request.session_id}

    session = types.SimpleNamespace(session_id="session-1", profile=None)
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "runner-local")
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: True)

    def _factory():
        factories.append(True)
        return RunnerClient()

    monkeypatch.setattr(routes, "_runtime_runner_client_factory", _factory)

    resp = routes._start_run(session, **_start_run_kwargs_for_lane(lane))

    assert started == [], "the runner client was invoked with an unroutable alias lane"
    assert factories == [], "no runner client should be built for a refused lane"
    _assert_alias_lane_refusal(resp, lane, "runner dispatch")


def test_legacy_dispatch_refuses_unresolved_alias_lane(monkeypatch):
    """The legacy worker is refused at the dispatcher, not left to fail later."""
    from api import routes

    _install_alias_cfg(monkeypatch)
    lane = _dead_lane()
    invoked = []
    session = types.SimpleNamespace(session_id="session-1", profile=None)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: False)
    monkeypatch.setattr(
        routes,
        "_start_chat_stream_for_session",
        lambda _session, **kwargs: invoked.append(kwargs)
        or {"stream_id": "legacy-1", "session_id": "session-1"},
    )

    resp = routes._start_run(
        session, gateway_chat_enabled=False, **_start_run_kwargs_for_lane(lane)
    )

    assert invoked == [], "the legacy backend was dispatched a turn it can only refuse"
    _assert_alias_lane_refusal(resp, lane, "legacy dispatch")


def test_live_alias_lane_still_dispatches_after_the_refusal_check(monkeypatch):
    """Positive control: a resolvable lane is not caught by the new refusal."""
    from api import routes

    _install_alias_cfg(monkeypatch)
    captured = {}
    session = types.SimpleNamespace(session_id="session-1", profile=None)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: False)
    monkeypatch.setattr(
        routes,
        "_start_chat_stream_for_session",
        lambda _session, **kwargs: captured.update(kwargs)
        or {"stream_id": "gateway-live", "session_id": "session-1"},
    )

    resp = routes._start_run(
        session, gateway_chat_enabled=True, **_start_run_kwargs_for_lane(_live_lane())
    )

    assert resp["stream_id"] == "gateway-live"
    assert captured["model"] == "east"
    assert captured["model_provider"] is None
    assert "east-secret" not in json.dumps(captured)


def test_start_session_turn_wakeup_refuses_unresolved_alias_lane(monkeypatch):
    """A server-initiated turn gets the same refusal as a browser turn."""
    _install_alias_cfg(monkeypatch)
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "gateway")
    lane = _dead_lane()
    routes_mod = _stub_start_session_turn(monkeypatch, provider=lane)
    invoked = []
    monkeypatch.setattr(
        routes_mod,
        "_start_chat_stream_for_session",
        lambda _session, **kwargs: invoked.append(kwargs)
        or {"_status": 200, "stream_id": "stream-wake", "session_id": "sess-alias-wake"},
    )
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: False)

    resp = routes_mod.start_session_turn("sess-alias-wake", "wakeup")

    assert invoked == [], "the wakeup backend was dispatched an unroutable alias lane"
    _assert_alias_lane_refusal(resp, lane, "wakeup dispatch")


# ─────────────────────────────────────────────────────────────────────────────
# Profile scope, proven by outcome rather than by entering a mocked scope.
#
# The session profile's OWN config must decide both `webui_chat_backend` and the
# alias table. The pair below holds the lane and the file text constant and
# varies only WHICH profile owns the alias table, so a resolver that read the
# default profile (what the drain thread does without the scope) would answer
# the wrong one of the two.
# ─────────────────────────────────────────────────────────────────────────────

_ALIAS_PROFILE_CONFIG = (
    "webui_chat_backend: gateway\n"
    "model:\n"
    "  default: shared-model\n"
    "  provider: custom\n"
    "model_aliases:\n"
    "  east:\n"
    "    model: shared-model\n"
    "    provider: custom\n"
    "    base_url: https://east.example.test/v1\n"
    "    api_key: east-secret\n"
)


def _install_profile_home(monkeypatch, tmp_path, profile_name, config_yaml):
    """Give ``profile_name`` its own profile home with the given config.yaml."""
    from api import profiles as profiles_mod

    base = tmp_path / ".hermes"
    profile_home = base / "profiles" / profile_name
    profile_home.mkdir(parents=True)
    if config_yaml is not None:
        (profile_home / "config.yaml").write_text(config_yaml, encoding="utf-8")
    monkeypatch.setattr(profiles_mod, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.delenv("HERMES_WEBUI_CHAT_BACKEND", raising=False)
    # conftest pins HERMES_CONFIG_PATH at the suite's state dir; the profile
    # config path only wins once that override is out of the way.
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    return base


@contextlib.contextmanager
def _restore_config_cache():
    """Keep a tmp profile's config out of the process-wide config cache."""
    from api import config

    with config._cfg_lock:
        saved = (
            dict(config._cfg_cache),
            config._cfg_path,
            config._cfg_mtime,
            config._cfg_fingerprint,
        )
    try:
        yield
    finally:
        with config._cfg_lock:
            config._cfg_cache.clear()
            config._cfg_cache.update(saved[0])
            config._cfg_path, config._cfg_mtime, config._cfg_fingerprint = saved[1:]
            config.cfg = config._cfg_cache


def test_wakeup_alias_routing_follows_the_session_profile_config(monkeypatch, tmp_path):
    """A named profile's own config supplies both gateway ownership and the lane."""
    _install_profile_home(monkeypatch, tmp_path, "work", _ALIAS_PROFILE_CONFIG)
    routes_mod = _stub_start_session_turn(
        monkeypatch, profile="work", provider=_live_lane(), real_config=True
    )
    captured = _capture_legacy_dispatch(monkeypatch, routes_mod)

    with _restore_config_cache():
        resp = routes_mod.start_session_turn("sess-alias-wake", "wakeup")

    assert resp["_status"] == 200
    assert captured["external_runtime_owned"] is True, (
        "the named profile's own webui_chat_backend did not select the gateway"
    )
    assert captured["model"] == "east", (
        "the named profile's own alias table did not resolve the lane"
    )
    assert captured["model_provider"] is None
    assert "east-secret" not in json.dumps(captured)


def test_wakeup_alias_lane_dead_in_the_session_profile_is_refused(monkeypatch, tmp_path):
    """The default profile's alias table does not answer for a named profile."""
    base = _install_profile_home(monkeypatch, tmp_path, "work", "webui_chat_backend: gateway\n")
    # The SAME alias exists for the default profile only.
    (base / "config.yaml").write_text(_ALIAS_PROFILE_CONFIG, encoding="utf-8")
    lane = _live_lane()
    routes_mod = _stub_start_session_turn(
        monkeypatch, profile="work", provider=lane, real_config=True
    )
    invoked = []
    monkeypatch.setattr(
        routes_mod,
        "_start_chat_stream_for_session",
        lambda _session, **kwargs: invoked.append(kwargs)
        or {"_status": 200, "stream_id": "stream-wake", "session_id": "sess-alias-wake"},
    )
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_runner_enabled", lambda: False)

    with _restore_config_cache():
        resp = routes_mod.start_session_turn("sess-alias-wake", "wakeup")

    assert invoked == [], "a lane owned by another profile reached the backend"
    _assert_alias_lane_refusal(resp, lane, "cross-profile lane")


# ─────────────────────────────────────────────────────────────────────────────
# The /goal kickoff is the one turn start that reaches the backends directly
# instead of through `_start_run`, so it makes the same two alias decisions.
# ─────────────────────────────────────────────────────────────────────────────


def _stub_goal_kickoff(monkeypatch, *, provider, gateway_owned, model="shared-model"):
    import api.goals as webui_goals

    from api import routes

    session = types.SimpleNamespace(
        session_id="sid-goal-alias",
        profile=None,
        workspace="/tmp/ws-test",
        model=model,
        model_provider=provider,
        messages=[],
        context_messages=[],
        pending_user_message=None,
        active_stream_id=None,
    )
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(
        routes, "resolve_trusted_workspace", lambda workspace, **_kw: "/tmp/ws-test"
    )
    monkeypatch.setattr(routes, "get_config", lambda: {})
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: gateway_owned)
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **_: (model, provider, False),
    )
    monkeypatch.setattr(
        webui_goals,
        "goal_command_payload",
        lambda *args, **kwargs: {"ok": True, "action": "set", "kickoff_prompt": "ship it"},
    )
    monkeypatch.setattr(
        webui_goals, "goal_state_snapshot", lambda *args, **kwargs: {"goal": "ship it"}
    )
    restored = []
    monkeypatch.setattr(
        webui_goals,
        "restore_goal_state",
        lambda sid, state, **kwargs: restored.append(state),
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, **kwargs: {"status": status, "payload": payload},
    )
    monkeypatch.setattr("api.runtime_adapter.runtime_adapter_enabled", lambda: False)
    return routes, restored


def _run_goal_kickoff(monkeypatch, routes, started):
    monkeypatch.setattr(
        routes,
        "_start_chat_stream_for_session",
        lambda _session, **kwargs: started.append(kwargs)
        or {"stream_id": "goal-stream", "session_id": "sid-goal-alias"},
    )
    return routes._handle_goal_command(
        object(), {"session_id": "sid-goal-alias", "args": "ship it"}
    )


def test_goal_kickoff_refuses_unresolved_alias_lane(monkeypatch):
    """The kickoff stream never dispatches a dead lane to any backend."""
    _install_alias_cfg(monkeypatch)
    lane = _dead_lane()
    routes, restored = _stub_goal_kickoff(monkeypatch, provider=lane, gateway_owned=True)
    started = []

    result = _run_goal_kickoff(monkeypatch, routes, started)

    assert started == [], "the goal kickoff dispatched an unroutable alias lane"
    assert result["status"] == 400
    assert result["payload"]["ok"] is False
    _assert_alias_lane_refusal(
        result["payload"], lane, "goal kickoff", status=result["status"]
    )
    assert restored, "a refused kickoff must roll the just-set goal back"


def test_goal_kickoff_routes_alias_name_to_gateway(monkeypatch):
    """A live lane reaches a gateway-owned kickoff as the alias NAME."""
    _install_alias_cfg(monkeypatch)
    routes, restored = _stub_goal_kickoff(
        monkeypatch, provider=_live_lane(), gateway_owned=True
    )
    started = []

    result = _run_goal_kickoff(monkeypatch, routes, started)

    assert result["status"] == 200
    assert len(started) == 1
    assert started[0]["model"] == "east"
    assert started[0]["model_provider"] is None
    assert started[0]["external_runtime_owned"] is True
    assert "east-secret" not in json.dumps(started)
    assert restored == []


def test_goal_kickoff_legacy_keeps_opaque_alias_lane(monkeypatch):
    """Without gateway ownership the legacy worker still resolves the lane."""
    _install_alias_cfg(monkeypatch)
    lane = _live_lane()
    routes, _restored = _stub_goal_kickoff(
        monkeypatch, provider=lane, gateway_owned=False
    )
    started = []

    result = _run_goal_kickoff(monkeypatch, routes, started)

    assert result["status"] == 200
    assert started[0]["model"] == "shared-model"
    assert started[0]["model_provider"] == lane
    assert started[0]["external_runtime_owned"] is False
