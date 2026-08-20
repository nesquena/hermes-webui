"""Focused regressions for Refresh Models cache ownership and fencing."""

from pathlib import Path
import json
import subprocess
import sys
import types
from urllib.parse import urlparse


REPO = Path(__file__).resolve().parent.parent


def _extract_js_function(source, marker):
    start = source.index(marker)
    close = source.index(")", start)
    brace = source.index("{", close)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {marker}")


def _run_node(script):
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _install_provider_model_ids(monkeypatch, fn):
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    models = types.ModuleType("hermes_cli.models")
    models.provider_model_ids = fn
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models)


def _prepare(monkeypatch, routes, profile="default"):
    import api.config as config
    import api.profiles as profiles

    routes._clear_live_models_cache()
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr(config, "get_config", lambda: {"model": {"provider": "openai"}})
    monkeypatch.setattr(config, "_resolve_provider_alias", lambda provider: provider)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: profile)


def test_refresh_replaces_warmed_catalog_without_reload(monkeypatch):
    import api.routes as routes

    calls = []

    def provider_model_ids(provider, *, force_refresh=False):
        calls.append(force_refresh)
        return ["provider/model-before-refresh" if not force_refresh else "provider/model-after-refresh"]

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _prepare(monkeypatch, routes)
    parsed = urlparse("/api/models/live?provider=provider")
    before = routes._handle_live_models(object(), parsed)
    if hasattr(routes, "_invalidate_live_models_for_provider"):
        routes._invalidate_live_models_for_provider("provider")
    else:
        import api.config as config
        config.invalidate_provider_models_cache("provider")
    after = routes._handle_live_models(object(), parsed)
    assert before["models"][0]["id"] == "provider/model-before-refresh"
    assert after["models"][0]["id"] == "provider/model-after-refresh"
    assert calls == [False, True]


def test_refresh_post_invalidates_live_cache_and_marks_required_generation(monkeypatch):
    import api.routes as routes
    import api.config as config

    _prepare(monkeypatch, routes)
    invalidated = []
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"provider": "provider-alias"})
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **kwargs: payload)
    monkeypatch.setattr(config, "invalidate_provider_models_cache", lambda provider: invalidated.append(f"catalog:{provider}"))
    monkeypatch.setattr(config, "_resolve_provider_alias", lambda provider: "provider")

    response = routes.handle_post(object(), urlparse("/api/models/refresh"))
    assert response == {"ok": True, "provider": "provider"}
    assert invalidated == ["catalog:provider-alias"]
    key = routes._live_models_cache_key("provider")
    assert routes._get_cached_live_models(key) is None
    assert key in routes._LIVE_MODELS_REFRESH_REQUIRED


def test_provider_mutations_invalidate_server_live_cache(monkeypatch):
    import api.onboarding as onboarding
    import api.routes as routes

    _prepare(monkeypatch, routes)
    bodies = [
        {"provider": "gpt", "api_key": "long-enough-key"},
        {"provider": "gpt"},
        {"provider": "ollama", "base_url": "http://localhost:11434", "model": "llama"},
    ]
    invalidated = []
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: bodies.pop(0))
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **kwargs: payload)
    monkeypatch.setattr(routes, "set_provider_key", lambda provider, api_key: {"ok": True, "provider": provider})
    monkeypatch.setattr(routes, "remove_provider_key", lambda provider: {"ok": True, "provider": provider})
    monkeypatch.setattr(
        onboarding,
        "apply_self_hosted_provider_setup",
        lambda body: {"ok": True, "provider": body["provider"]},
    )
    monkeypatch.setattr(routes, "_invalidate_live_models_for_provider", invalidated.append)

    assert routes.handle_post(object(), urlparse("/api/providers"))["ok"]
    assert routes.handle_post(object(), urlparse("/api/providers/delete"))["ok"]
    assert routes.handle_post(object(), urlparse("/api/providers/self-hosted"))["ok"]
    assert invalidated == ["gpt", "gpt", "ollama"]


def test_refresh_evicts_only_canonical_profile_provider_key(monkeypatch):
    import api.routes as routes
    import api.profiles as profiles

    active = ["default"]
    _install_provider_model_ids(monkeypatch, lambda provider: [f"{provider}/{active[0]}"])
    _prepare(monkeypatch, routes)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: active[0])
    parsed = urlparse("/api/models/live?provider=provider")
    default = routes._handle_live_models(object(), parsed)
    active[0] = "adjacent"
    adjacent = routes._handle_live_models(object(), parsed)
    active[0] = "default"
    routes._invalidate_live_models_for_provider("provider")
    refreshed = routes._handle_live_models(object(), parsed)
    assert default["models"] == [{"id": "provider/default", "label": "Default"}]
    assert adjacent["models"] == [{"id": "provider/adjacent", "label": "Adjacent"}]
    assert refreshed["models"] == default["models"]
    assert ("adjacent", "provider") in routes._LIVE_MODELS_CACHE


def test_pre_refresh_completion_cannot_repopulate_server_cache(monkeypatch):
    import api.routes as routes

    _install_provider_model_ids(monkeypatch, lambda provider: ["provider/model-before-refresh"])
    _prepare(monkeypatch, routes)
    key = routes._live_models_cache_key("provider")
    generation = routes._live_models_generation(key)
    routes._invalidate_live_models_for_provider("provider")
    assert routes._set_cached_live_models(key, {"models": [{"id": "stale"}]}, generation) is False
    assert routes._get_cached_live_models(key) is None


def test_ordinary_cache_clear_fences_unknown_in_flight_key(monkeypatch):
    import api.routes as routes

    _prepare(monkeypatch, routes)
    key = routes._live_models_cache_key("provider")
    generation = routes._live_models_generation(key)
    routes._clear_live_models_cache()
    assert routes._set_cached_live_models(key, {"models": [{"id": "stale"}]}, generation) is False
    assert routes._get_cached_live_models(key) is None


def test_forced_empty_lookup_stays_uncached_after_failure(monkeypatch):
    import api.routes as routes

    _install_provider_model_ids(monkeypatch, lambda provider, *, force_refresh=False: [])
    _prepare(monkeypatch, routes)
    routes._invalidate_live_models_for_provider("provider")
    result = routes._handle_live_models(object(), urlparse("/api/models/live?provider=provider"))
    key = routes._live_models_cache_key("provider")
    assert result["models"] == []
    assert result["error"] == "live_models_unavailable"
    assert key not in routes._LIVE_MODELS_REFRESH_REQUIRED
    assert routes._get_cached_live_models(key) is None


def test_failed_refresh_does_not_poison_ordinary_lookup(monkeypatch):
    import api.routes as routes

    force_flags = []

    def provider_model_ids(provider, *, force_refresh=False):
        force_flags.append(force_refresh)
        if force_refresh:
            raise RuntimeError("provider unavailable")
        return ["provider/static-after-failure"]

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _prepare(monkeypatch, routes)
    routes._invalidate_live_models_for_provider("provider")
    result = routes._handle_live_models(object(), urlparse("/api/models/live?provider=provider"))
    key = routes._live_models_cache_key("provider")
    assert result["models"] == []
    assert result["error"] == "live_models_unavailable"
    assert key not in routes._LIVE_MODELS_REFRESH_REQUIRED
    recovered = routes._handle_live_models(object(), urlparse("/api/models/live?provider=provider"))
    assert recovered["models"] == [
        {"id": "provider/static-after-failure", "label": "Static After Failure"}
    ]
    assert force_flags == [True, False]


def test_forced_lookup_exception_uses_custom_live_fallback(monkeypatch):
    import urllib.request

    import api.config as config
    import api.routes as routes

    def provider_model_ids(provider, *, force_refresh=False):
        raise RuntimeError("provider unavailable")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "upstream-model"}]}).encode()

    requested_urls = []

    def fake_urlopen(request, timeout):
        requested_urls.append(request.full_url)
        return FakeResponse()

    _install_provider_model_ids(monkeypatch, provider_model_ids)
    _prepare(monkeypatch, routes)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "model": {"provider": "openai"},
            "custom_providers": [
                {
                    "name": "custom:relay",
                    "api_key": "relay-key",
                    "base_url": "https://relay.example/v1",
                }
            ],
        },
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    routes._invalidate_live_models_for_provider("custom:relay")
    key = routes._live_models_cache_key("custom:relay")

    publication_states = []
    original_set_cached = routes._set_cached_live_models

    def recording_set_cached(cache_key, payload, expected_generation=None):
        publication_states.append((bool(payload.get("models")), cache_key in routes._LIVE_MODELS_REFRESH_REQUIRED))
        result = original_set_cached(cache_key, payload, expected_generation)
        publication_states.append((bool(payload.get("models")), cache_key in routes._LIVE_MODELS_REFRESH_REQUIRED))
        return result

    monkeypatch.setattr(routes, "_set_cached_live_models", recording_set_cached)
    result = routes._handle_live_models(
        object(), urlparse("/api/models/live?provider=custom%3Arelay")
    )

    assert requested_urls == ["https://relay.example/v1/models"]
    assert result["models"] == [{"id": "upstream-model", "label": "Upstream Model"}]
    assert "error" not in result
    assert publication_states == [(True, True), (True, False)]
    assert key not in routes._LIVE_MODELS_REFRESH_REQUIRED


def test_forced_custom_lookup_does_not_use_configured_static_model(monkeypatch):
    import urllib.request

    import api.config as config
    import api.routes as routes

    _install_provider_model_ids(monkeypatch, lambda provider, *, force_refresh=False: [])
    _prepare(monkeypatch, routes)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "model": {"provider": "openai"},
            "custom_providers": [
                {
                    "name": "custom:relay",
                    "api_key": "relay-key",
                    "base_url": "https://relay.example/v1",
                    "model": "configured-only",
                }
            ],
        },
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: (_ for _ in ()).throw(OSError("provider unavailable")),
    )
    routes._invalidate_live_models_for_provider("custom:relay")
    result = routes._handle_live_models(
        object(), urlparse("/api/models/live?provider=custom%3Arelay")
    )
    key = routes._live_models_cache_key("custom:relay")
    assert result == {"error": "live_models_unavailable", "models": []}
    assert key not in routes._LIVE_MODELS_REFRESH_REQUIRED
    assert routes._get_cached_live_models(key) is None


def test_post_refresh_lookup_forces_agent_until_current_generation_publishes(monkeypatch):
    import api.routes as routes

    force_flags = []
    _install_provider_model_ids(monkeypatch, lambda provider, *, force_refresh=False: force_flags.append(force_refresh) or ["provider/fresh"])
    _prepare(monkeypatch, routes)
    parsed = urlparse("/api/models/live?provider=provider")
    routes._handle_live_models(object(), parsed)
    routes._invalidate_live_models_for_provider("provider")
    routes._handle_live_models(object(), parsed)
    routes._handle_live_models(object(), parsed)
    assert force_flags == [False, True]


def test_refresh_button_awaits_live_fetch_before_success():
    source = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    invalidate = _extract_js_function(source, "function _invalidateLiveModelCacheForProvider(")
    rebuild = _extract_js_function(source, "function _refreshModelDropdownsAfterProviderChange(")
    refresh = _extract_js_function(source, "async function _refreshProviderModels(")
    bundle = invalidate + "\n" + rebuild + "\n" + refresh + "\nglobalThis.runRefresh=_refreshProviderModels;"
    script = f"""
const events = [];
globalThis.window = {{
  _invalidateLiveModelCache: p => events.push(['invalidate', p]),
  _invalidateSlashModelCache: () => events.push(['slash']),
  _ensureModelDropdownReady: async () => {{ events.push(['rebuild-start']); await Promise.resolve(); events.push(['rebuild-end']); }}
}};
globalThis.api = async () => {{ events.push(['post']); return {{ok:true, provider:'provider'}}; }};
globalThis._fetchLiveModels = async () => {{ events.push(['live']); return [{{id:'provider/model'}}]; }};
globalThis.t = () => 'refreshed';
globalThis.showToast = value => events.push(['toast', value]);
globalThis.eval({json.dumps(bundle)});
(async () => {{ await globalThis.runRefresh('provider', {{disabled:false, innerHTML:''}}); console.log(JSON.stringify(events)); }})();
"""
    events = _run_node(script)
    kinds = [event[0] for event in events]
    assert kinds.index("live") < kinds.index("rebuild-start") < kinds.index("rebuild-end") < kinds.index("toast")


def test_required_live_model_request_failure_uses_localized_translation_key():
    source = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    cache_key = _extract_js_function(source, "function _liveModelCacheKey(")
    fetch_live = _extract_js_function(source, "async function _fetchLiveModels(")
    bundle = (
        "globalThis._liveModelCache={}; globalThis._liveModelCacheGen={}; "
        "globalThis._liveModelFetchPending=new Set(); globalThis._modelDropdownRequestSeq=0; "
        "globalThis.S={activeProfile:'default'}; "
        "globalThis.document={baseURI:'http://localhost/'}; globalThis._redirectIfUnauth=()=>false; "
        "globalThis._addLiveModelsToSelect=()=>0; globalThis.syncModelChip=()=>{}; "
        + cache_key
        + "\n"
        + fetch_live
        + "\nglobalThis.runFetch=_fetchLiveModels;"
    )
    script = f"""
const requested = [];
globalThis.t = key => {{ requested.push(key); return `localized:${{key}}`; }};
globalThis.fetch = async () => ({{ok:false, json:async () => ({{}})}});
globalThis.eval({json.dumps(bundle)});
(async () => {{
  try {{ await globalThis.runFetch('provider', null, null, {{required:true}}); }}
  catch (error) {{ console.log(JSON.stringify({{requested, message:error.message}})); }}
}})();
"""
    result = _run_node(script)
    assert result == {
        "requested": ["providers_live_models_request_failed"],
        "message": "localized:providers_live_models_request_failed",
    }


def test_required_empty_live_models_uses_localized_translation_key():
    source = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    cache_key = _extract_js_function(source, "function _liveModelCacheKey(")
    fetch_live = _extract_js_function(source, "async function _fetchLiveModels(")
    bundle = (
        "globalThis._liveModelCache={}; globalThis._liveModelCacheGen={}; "
        "globalThis._liveModelFetchPending=new Set(); globalThis._modelDropdownRequestSeq=0; "
        "globalThis.S={activeProfile:'default'}; "
        "globalThis.document={baseURI:'http://localhost/'}; globalThis._redirectIfUnauth=()=>false; "
        "globalThis._addLiveModelsToSelect=()=>0; globalThis.syncModelChip=()=>{}; "
        + cache_key
        + "\n"
        + fetch_live
        + "\nglobalThis.runFetch=_fetchLiveModels;"
    )
    script = f"""
const requested = [];
globalThis.t = key => {{ requested.push(key); return `localized:${{key}}`; }};
globalThis.fetch = async () => ({{ok:true, json:async () => ({{models:[]}})}});
globalThis.eval({json.dumps(bundle)});
(async () => {{
  try {{ await globalThis.runFetch('provider', null, null, {{required:true}}); }}
  catch (error) {{ console.log(JSON.stringify({{requested, message:error.message}})); }}
}})();
"""
    result = _run_node(script)
    assert result == {
        "requested": ["providers_live_models_empty"],
        "message": "localized:providers_live_models_empty",
    }


def test_partial_locale_falls_back_to_english_provider_refresh_messages():
    source_path = str(REPO / "static" / "i18n.js")
    script = f"""
const source = require('fs').readFileSync({json.dumps(source_path)}, 'utf8');
const localeCheck = source + "\\nglobalThis.runLocaleCheck=()=>{{setLocale('es'); return {{request:t('providers_live_models_request_failed'), empty:t('providers_live_models_empty'), english:LOCALES.en}};}};";
globalThis.localStorage = {{getItem: () => null, setItem: () => {{}}}};
globalThis.document = {{documentElement: {{lang: ''}}, querySelectorAll: () => []}};
globalThis.eval(localeCheck);
console.log(JSON.stringify(globalThis.runLocaleCheck()));
"""
    result = _run_node(script)
    assert result["request"] == result["english"]["providers_live_models_request_failed"]
    assert result["empty"] == result["english"]["providers_live_models_empty"]


def test_browser_refresh_fences_stale_live_lookup():
    source = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    cache_key = _extract_js_function(source, "function _liveModelCacheKey(")
    invalidate = _extract_js_function(source, "function _invalidateLiveModelCache(")
    fetch_live = _extract_js_function(source, "async function _fetchLiveModels(")
    bundle = (
        "globalThis._liveModelCache={}; globalThis._liveModelCacheGen={}; "
        "globalThis._liveModelFetchPending=new Set(); globalThis._modelDropdownRequestSeq=0; "
        "globalThis.S={activeProfile:'default'}; "
        "globalThis.document={baseURI:'http://localhost/'}; globalThis._redirectIfUnauth=()=>false; "
        "globalThis._addLiveModelsToSelect=()=>0; globalThis.syncModelChip=()=>{}; "
        + cache_key
        + "\n"
        + invalidate
        + "\n"
        + fetch_live
        + "\nglobalThis.runFetch=_fetchLiveModels; globalThis.runInvalidate=_invalidateLiveModelCache;"
    )
    script = f"""
let resolveFetch;
globalThis.fetch = () => new Promise(resolve => {{ resolveFetch=resolve; }});
globalThis.eval({json.dumps(bundle)});
(async () => {{
  const pending = globalThis.runFetch('provider', null, null, {{required:true}});
  globalThis.runInvalidate('provider');
  resolveFetch({{ok:true, json:async () => ({{models:[{{id:'stale'}}]}})}});
  const result = await pending;
  console.log(JSON.stringify({{result:result===undefined?null:result, cached:globalThis._liveModelCache['default::provider']||null, pending:globalThis._liveModelFetchPending.size}}));
}})();
"""
    result = _run_node(script)
    assert result == {"result": None, "cached": None, "pending": 0}


def test_browser_pending_state_tracks_concurrent_fetches():
    source = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    cache_key = _extract_js_function(source, "function _liveModelCacheKey(")
    fetch_live = _extract_js_function(source, "async function _fetchLiveModels(")
    bundle = (
        "globalThis._liveModelCache={}; globalThis._liveModelCacheGen={}; "
        "globalThis._liveModelFetchPending=new Set(); globalThis._modelDropdownRequestSeq=0; "
        "globalThis.S={activeProfile:'default'}; "
        "globalThis.document={baseURI:'http://localhost/'}; globalThis._redirectIfUnauth=()=>false; "
        "globalThis._addLiveModelsToSelect=()=>0; globalThis.syncModelChip=()=>{}; "
        + cache_key
        + "\n"
        + fetch_live
        + "\nglobalThis.runFetch=_fetchLiveModels;"
    )
    script = """
const resolvers=[];
globalThis.fetch = () => new Promise(resolve => resolvers.push(resolve));
globalThis.eval(""" + json.dumps(bundle) + """);
(async () => {
  const first=globalThis.runFetch('provider',null,null,{required:true});
  const second=globalThis.runFetch('provider',null,null,{required:true});
  resolvers[0]({ok:true,json:async () => ({models:[{id:'first'}]})});
  await first;
  const pendingAfterFirst=globalThis._liveModelFetchPending.size;
  resolvers[1]({ok:true,json:async () => ({models:[{id:'second'}]})});
  await second;
  console.log(JSON.stringify({pendingAfterFirst,pendingAfterSecond:globalThis._liveModelFetchPending.size}));
})();
"""
    result = _run_node(script)
    assert result == {"pendingAfterFirst": 1, "pendingAfterSecond": 0}


def test_browser_live_model_cache_is_profile_scoped():
    source = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    cache_key = _extract_js_function(source, "function _liveModelCacheKey(")
    fetch_live = _extract_js_function(source, "async function _fetchLiveModels(")
    bundle = (
        "globalThis._liveModelCache={}; globalThis._liveModelCacheGen={}; "
        "globalThis._liveModelFetchPending=new Set(); globalThis._modelDropdownRequestSeq=0; "
        "globalThis.S={activeProfile:'profile-a'}; "
        "globalThis.document={baseURI:'http://localhost/'}; globalThis._redirectIfUnauth=()=>false; "
        "globalThis._addLiveModelsToSelect=()=>0; globalThis.syncModelChip=()=>{}; "
        + cache_key
        + "\n"
        + fetch_live
        + "\nglobalThis.runFetch=_fetchLiveModels;"
    )
    script = f"""
let calls = 0;
globalThis.fetch = async () => {{
  calls += 1;
  return {{ok:true, json:async () => ({{models:[{{id:globalThis.S.activeProfile}}]}})}};
}};
globalThis.eval({json.dumps(bundle)});
(async () => {{
  await globalThis.runFetch('provider', {{}}, null, {{}});
  globalThis.S.activeProfile='profile-b';
  await globalThis.runFetch('provider', {{}}, null, {{}});
  console.log(JSON.stringify({{calls, keys:Object.keys(globalThis._liveModelCache).sort()}}));
}})();
"""
    result = _run_node(script)
    assert result == {"calls": 2, "keys": ["profile-a::provider", "profile-b::provider"]}


def test_refresh_failure_has_no_success_path():
    source = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    invalidate = _extract_js_function(source, "function _invalidateLiveModelCacheForProvider(")
    rebuild = _extract_js_function(source, "function _refreshModelDropdownsAfterProviderChange(")
    refresh = _extract_js_function(source, "async function _refreshProviderModels(")
    bundle = invalidate + "\n" + rebuild + "\n" + refresh + "\nglobalThis.runRefresh=_refreshProviderModels;"
    script = f"""
const events = [];
globalThis.window = {{_invalidateLiveModelCache: () => {{}}, _invalidateSlashModelCache: () => {{}}, _ensureModelDropdownReady: async () => {{}}}};
globalThis.api = async () => ({{ok:true, provider:'provider'}});
globalThis._fetchLiveModels = async () => {{ throw new Error('provider unavailable'); }};
globalThis.t = () => 'refreshed';
globalThis.showToast = value => events.push(value);
globalThis.eval({json.dumps(bundle)});
(async () => {{ await globalThis.runRefresh('provider', {{disabled:false, innerHTML:''}}); console.log(JSON.stringify(events)); }})();
"""
    messages = _run_node(script)
    assert messages
    assert not any(message == "refreshed" for message in messages)


def test_refresh_discards_profile_switched_continuation():
    source = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    invalidate = _extract_js_function(source, "function _invalidateLiveModelCacheForProvider(")
    rebuild = _extract_js_function(source, "function _refreshModelDropdownsAfterProviderChange(")
    refresh = _extract_js_function(source, "async function _refreshProviderModels(")
    bundle = invalidate + "\n" + rebuild + "\n" + refresh
    script = """
const events=[];
let resolveLive;
globalThis.S={activeProfile:'profile-a'};
globalThis.window={
  _invalidateLiveModelCache: provider => events.push(['invalidate',provider]),
  _invalidateSlashModelCache: () => events.push(['slash']),
  _ensureModelDropdownReady: async () => events.push(['rebuild'])
};
globalThis.api=async () => ({ok:true,provider:'provider'});
globalThis._fetchLiveModels=() => new Promise(resolve => {
  resolveLive=() => { globalThis.S.activeProfile='profile-b'; resolve([{id:'provider/model'}]); };
});
globalThis.t=() => 'refreshed';
globalThis.showToast=value => events.push(['toast',value]);
globalThis.eval(""" + json.dumps(bundle) + """);
(async () => {
  const task=globalThis._refreshProviderModels('provider',{disabled:false,innerHTML:''});
  await new Promise(resolve => setTimeout(resolve,0));
  resolveLive();
  await task;
  console.log(JSON.stringify(events));
})();
"""
    events = _run_node(script)
    assert events == [["invalidate", "provider"]]


def test_provider_key_save_and_remove_invalidate_requested_and_canonical_live_caches():
    source = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    invalidate = _extract_js_function(source, "function _invalidateLiveModelCacheForProvider(")
    save = _extract_js_function(source, "async function _saveProviderKey(")
    remove = _extract_js_function(source, "async function _removeProviderKey(")
    bundle = (
        invalidate
        + "\n"
        + save
        + "\n"
        + remove
        + "\nglobalThis.runSave=_saveProviderKey; globalThis.runRemove=_removeProviderKey;"
    )
    script = f"""
const events = [];
const card = {{input: {{value: 'long-enough-key'}}, saveBtn: {{disabled:false, textContent:''}}}};
globalThis._providerCardEls = new Map([['provider-alias', card]]);
    globalThis.window = {{_invalidateLiveModelCache: (provider, profile) => events.push([provider, profile])}};
    globalThis.S = {{activeProfile:'profile-a'}};
    globalThis.api = async path => {{
      globalThis.S.activeProfile='profile-b';
      return {{ok:true, provider:'provider', action:path.includes('delete')?'removed':'updated'}};
    }};
globalThis.showToast = () => {{}};
globalThis.t = () => 'text';
globalThis._refreshModelDropdownsAfterProviderChange = () => {{}};
globalThis.loadProvidersPanel = async () => {{}};
globalThis.eval({json.dumps(bundle)});
(async () => {{
  await globalThis.runSave('provider-alias');
  await globalThis.runRemove('provider-alias');
  console.log(JSON.stringify(events));
}})();
"""
    events = _run_node(script)
    assert events == [
        ["provider-alias", "profile-a"],
        ["provider", "profile-a"],
        ["provider-alias", "profile-b"],
        ["provider", "profile-b"],
    ]
