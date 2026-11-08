"""Regression tests for #3546 — POST /api/models/refresh must exist and
invalidate the per-provider model cache so the "Refresh Models" button
on Settings > Providers works instead of returning 404.

The bug
-------
``static/panels.js`` sends ``POST /api/models/refresh`` from
``_refreshProviderModels``, but no route handled that path in
``api/routes.py``. Every click showed "Error: Not found" because the
server returned 404, which ``workspace.js``'s ``api()`` helper surfaced
as an error toast.

The fix
-------
``api/routes.py`` adds a ``POST /api/models/refresh`` branch that calls
``invalidate_provider_models_cache(provider_id)`` and returns
``{"ok": true, "provider": provider_id}``. The frontend success path
now also calls ``_refreshModelDropdownsAfterProviderChange()`` so the
model picker rebuilds immediately.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def _read_static(name: str) -> str:
    return (REPO / "static" / name).read_text(encoding="utf-8")


def _extract_function_body(src: str, signature: str) -> str:
    """Return the source of a top-level function declaration via brace-balance."""
    idx = src.find(signature)
    if idx == -1:
        raise AssertionError(f"signature {signature!r} not found in source")
    open_idx = src.find("{", idx)
    if open_idx == -1:
        raise AssertionError(f"could not find opening brace after {signature!r}")
    depth = 0
    for i in range(open_idx, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[idx : i + 1]
    raise AssertionError(f"unbalanced braces in {signature!r}")


class TestRefreshRouteExists:
    """The POST /api/models/refresh route must exist in routes.py and follow
    the same input validation pattern as /api/providers/delete."""

    def test_route_branch_present(self):
        src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
        assert '"/api/models/refresh"' in src, (
            "POST /api/models/refresh route missing from api/routes.py. "
            "Without it, the Refresh Models button 404s (#3546)."
        )

    def test_route_validates_provider_param(self):
        src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
        idx = src.find('"/api/models/refresh"')
        block = src[idx : idx + 500]
        assert "provider" in block and "bad(handler" in block, (
            "/api/models/refresh must validate that 'provider' is present "
            "and return 400 via bad() when missing."
        )

    def test_route_calls_invalidate_provider_models_cache(self):
        src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
        idx = src.find('"/api/models/refresh"')
        block = src[idx : idx + 500]
        assert "invalidate_provider_models_cache" in block, (
            "/api/models/refresh must call invalidate_provider_models_cache "
            "to bust the per-provider TTL cache."
        )

    def test_route_returns_ok_with_provider(self):
        src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
        idx = src.find('"/api/models/refresh"')
        # Window widened past 500 for the #6919 branch comments added above the
        # return; the assertions target the return line itself.
        block = src[idx : idx + 800]
        assert '"ok"' in block and '"provider"' in block, (
            "/api/models/refresh must return {ok: true, provider: provider_id} "
            "so the frontend can confirm the operation succeeded."
        )


class TestFrontendRefreshPath:
    """The frontend success path must update the model picker immediately
    after a cache bust, not wait for the next /api/models poll."""

    def test_refresh_calls_dropdown_updater(self):
        src = _read_static("panels.js")
        body = _extract_function_body(src, "async function _refreshProviderModels(")
        assert "_refreshModelDropdownsAfterProviderChange()" in body, (
            "_refreshProviderModels must call _refreshModelDropdownsAfterProviderChange() "
            "on success so the model picker rebuilds immediately after a cache "
            "bust instead of waiting for the next /api/models call (#3546)."
        )

    def test_refresh_shows_friendly_404(self):
        src = _read_static("panels.js")
        body = _extract_function_body(src, "async function _refreshProviderModels(")
        assert "e.status===404" in body or "e.status === 404" in body, (
            "_refreshProviderModels catch block must check e.status===404 to "
            "show a friendly message when the route is missing on older backends."
        )


class TestRefreshInvalidatesLiveModelCache:
    """#6919: a Refresh Models click must also bust the browser-side
    _liveModelCache, or the success toast is followed by the same stale list."""

    def test_ui_exposes_invalidate_live_model_cache(self):
        src = _read_static("ui.js")
        assert "function invalidateLiveModelCache(" in src, (
            "ui.js must expose invalidateLiveModelCache(providerId) — the "
            "browser-side eviction for the per-page _liveModelCache (#6919)."
        )
        assert "delete _liveModelCache[" in src, (
            "invalidateLiveModelCache must delete the provider entry from "
            "_liveModelCache (#6919)."
        )

    def test_dropdown_rebuild_calls_invalidate_live_model_cache(self):
        panels = _read_static("panels.js")
        body = _extract_function_body(panels, "function _refreshModelDropdownsAfterProviderChange(")
        assert "invalidateLiveModelCache" in body, (
            "_refreshModelDropdownsAfterProviderChange must call "
            "invalidateLiveModelCache() before rebuilding the dropdowns so the "
            "stale per-page list cannot survive a Refresh Models click (#6919)."
        )


class TestLiveModelInvalidationEpoch:
    """#6919 review follow-up: invalidateLiveModelCache() must also revoke
    in-flight _fetchLiveModels() work. The Settings caller passes no
    requestSeq, so a pending no-sequence fetch can otherwise assign
    _liveModelCache[provider] after the invalidation deleted it, and
    re-enrich the dropdown with pre-invalidation models."""

    def test_invalidation_epoch_is_independent_of_request_sequencing(self):
        src = _read_static("ui.js")
        assert re.search(r"let _liveModelInvalidationEpoch\s*=\s*0\s*;", src), (
            "ui.js must declare a dedicated _liveModelInvalidationEpoch counter "
            "— the composer's _modelDropdownRequestSeq only orders concurrent "
            "populateModelDropdown() calls and is not passed by every caller "
            "(#6919)."
        )

    def test_invalidate_live_model_cache_bumps_the_epoch(self):
        src = _read_static("ui.js")
        body = _extract_function_body(src, "function invalidateLiveModelCache(")
        assert "_liveModelInvalidationEpoch++" in body or "_liveModelInvalidationEpoch +=" in body, (
            "invalidateLiveModelCache must advance _liveModelInvalidationEpoch on "
            "every invalidation — provider-scoped AND global — so pending "
            "responses are fenced even when no completed cache entry exists "
            "(#6919)."
        )

    def test_fetch_captures_epoch_before_dispatch(self):
        src = _read_static("ui.js")
        body = _extract_function_body(src, "async function _fetchLiveModels(")
        capture = body.find("const invalidationEpoch=_liveModelInvalidationEpoch")
        dispatch = body.find("await fetch(")
        assert capture != -1, (
            "_fetchLiveModels must capture the invalidation epoch before it "
            "dispatches (#6919)."
        )
        assert dispatch != -1 and capture < dispatch, (
            "the epoch must be captured BEFORE the fetch is dispatched (#6919)."
        )

    def test_fetch_rejects_obsolete_response_before_publication_and_dom(self):
        src = _read_static("ui.js")
        body = _extract_function_body(src, "async function _fetchLiveModels(")
        publish = body.find("_liveModelCache[provider]=data.models")
        dom = body.find("_addLiveModelsToSelect(provider,data.models,sel")
        assert publish != -1 and dom != -1, (
            "_fetchLiveModels must still publish the cache and enrich the DOM "
            "for fresh responses (#6919)."
        )
        last_check = body.rfind("invalidationEpoch!==_liveModelInvalidationEpoch", 0, publish)
        assert last_check != -1, (
            "#6919: an obsolete (post-invalidation) response must be rejected "
            "by an epoch comparison BEFORE the cache publication"
        )
        assert last_check < dom, (
            "#6919: the epoch rejection must also precede DOM enrichment"
        )
        # The rejection has to sit after an await (a pending response only
        # becomes obsolete while it is in flight), not only at entry.
        await_at = body.find("await fetch(")
        assert await_at != -1 and await_at < last_check, (
            "#6919: the epoch rejection must run after the awaited response, "
            "not merely before dispatch"
        )


def _run_node(script: str) -> dict:
    completed = subprocess.run(
        [NODE, "-e", script],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


# The sandbox replays the ACTUAL production functions from static/ui.js; only
# the fetch/DOM boundaries are stubbed. State declarations mirror ui.js so the
# replay exercises the real control flow.
_LIVE_MODEL_REPLAY_PREAMBLE = """
const _liveModelCache={};
const _liveModelFetchPending=new Set();
const _liveModelFetchOwners=new Map();
let _liveModelInvalidationEpoch=0;
let _modelDropdownRequestSeq=0;
const addedCalls=[];
function _addLiveModelsToSelect(provider,models,sel){
  addedCalls.push({provider:provider,ids:models.map(m=>m.id)});
  return models.length;
}
function _redirectIfUnauth(){return false;}
function syncModelChip(){}
const document={baseURI:'http://localhost/',getElementById(id){return id==='settingsModel'?_settingsSel:null;}};
const window={};
const location={href:'http://localhost/'};
const fetchUrls=[];
// #6919: each distinct deferred serves exactly one fetch() call, so a
// replacement dispatch (the Settings re-arm) gets its own resolvable promise
// while the existing single-fetch tests keep resolving the one they planted.
const fetchDeferreds=[];
const _usedFetchDeferreds=new WeakSet();
let _fetchDeferred=null;
function fetch(url){
  fetchUrls.push(url);
  if(_fetchDeferred&&!_usedFetchDeferreds.has(_fetchDeferred)){
    _usedFetchDeferreds.add(_fetchDeferred);
    fetchDeferreds.push(_fetchDeferred);
    return _fetchDeferred.promise;
  }
  const d=_deferred();fetchDeferreds.push(d);return d.promise;
}
function _deferred(){let resolve;const promise=new Promise(r=>{resolve=r});return {promise,resolve};}
// Mirrors the Settings model picker that loadSettingsPanel() hydrates. Only
// present when a Settings receiver test plants it.
let _settingsSel=null;
const _tick=()=>new Promise(r=>setTimeout(r,0));
// Production console.debug() writes to stdout in node; keep the replay's
// JSON channel clean.
const console={debug(){},log(){},warn(){},error(){}};
"""


def _replay_script(scenario: str) -> str:
    ui = _read_static("ui.js")
    invalidate = _extract_function_body(ui, "function invalidateLiveModelCache(")
    fetch_live = _extract_function_body(ui, "async function _fetchLiveModels(")
    return f"""
{_LIVE_MODEL_REPLAY_PREAMBLE}
{invalidate}
{fetch_live}
const stale={{provider:'openai',models:[{{id:'openai/model-1',label:'Model 1'}}],count:1}};
const fresh={{provider:'openai',models:[{{id:'openai/model-2',label:'Model 2'}}],count:1}};
(async()=>{{
{scenario}
}})().catch(e=>{{process.stderr.write(String(e&&e.stack||e));process.exit(1);}});
"""


def _replay(scenario: str) -> dict:
    return _run_node(_replay_script(scenario))


_STALE_MODELS = [{"id": "openai/model-1", "label": "Model 1"}]
_FRESH_MODELS = [{"id": "openai/model-2", "label": "Model 2"}]


_REPLAY_SNAPSHOT = """
process.stdout.write(JSON.stringify({
  cache:_liveModelCache,
  added:addedCalls,
  pending:[..._liveModelFetchPending],
  ownerCount:_liveModelFetchOwners.size,
  fetchCount:fetchUrls.length,
  epoch:_liveModelInvalidationEpoch,
}));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_invalidation_revokes_pending_no_sequence_fetch():
    """#6919: the real Settings caller passes no requestSeq. A fetch that is
    still in flight when invalidateLiveModelCache() runs must not publish
    its stale response afterwards."""
    result = _replay(f"""
  _fetchDeferred=_deferred();
  const pending=_fetchLiveModels('openai',{{}});
  await _tick();
  invalidateLiveModelCache();
  _fetchDeferred.resolve({{json:async()=>stale}});
  await pending;
  await _tick();
{_REPLAY_SNAPSHOT}
""")
    assert result["cache"] == {}, (
        "#6919: the pending no-sequence fetch repopulated _liveModelCache "
        "after invalidateLiveModelCache() deleted it"
    )
    assert result["added"] == [], (
        "#6919: the obsolete response enriched the dropdown after invalidation"
    )
    assert result["pending"] == [], "pending marker must be cleaned up"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_response_settled_before_invalidation_still_populates():
    """Positive control: a response that settles BEFORE the invalidation is
    legitimate and must still populate the cache and the dropdown."""
    result = _replay("""
  _fetchDeferred=_deferred();
  const pending=_fetchLiveModels('openai',{});
  await _tick();
  _fetchDeferred.resolve({json:async()=>stale});
  await pending;
  await _tick();
  const settled={cache:Object.assign({},_liveModelCache),added:addedCalls.slice()};
  invalidateLiveModelCache();
  process.stdout.write(JSON.stringify({
    settled:settled,
    cacheAfter:_liveModelCache,
    pending:[..._liveModelFetchPending],
  }));
""")
    assert result["settled"]["cache"] == {"openai": _STALE_MODELS}, (
        "control failed: a response settled before the invalidation must "
        "populate the cache"
    )
    assert result["settled"]["added"] == [{"provider": "openai", "ids": ["openai/model-1"]}], (
        "control failed: a response settled before the invalidation must "
        "enrich the dropdown"
    )
    assert result["cacheAfter"] == {}, "invalidation must still evict settled entries"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_global_invalidation_revokes_pending_without_completed_entry():
    """#6919: a global invalidation must revoke pending work even when no
    completed cache entry exists for the provider."""
    result = _replay(f"""
  _fetchDeferred=_deferred();
  const pending=_fetchLiveModels('openai',{{}});
  await _tick();
  invalidateLiveModelCache();
  _fetchDeferred.resolve({{json:async()=>stale}});
  await pending;
  await _tick();
{_REPLAY_SNAPSHOT}
""")
    assert result["cache"] == {} and result["added"] == [], (
        "#6919: global invalidation did not revoke the pending fetch"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_provider_scoped_invalidation_also_revokes_pending():
    """#6919: provider-scoped invalidation fences the same pending work."""
    result = _replay(f"""
  _fetchDeferred=_deferred();
  const pending=_fetchLiveModels('openai',{{}});
  await _tick();
  invalidateLiveModelCache('openai');
  _fetchDeferred.resolve({{json:async()=>stale}});
  await pending;
  await _tick();
{_REPLAY_SNAPSHOT}
""")
    assert result["cache"] == {} and result["added"] == [], (
        "#6919: provider-scoped invalidation did not revoke the pending fetch"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_composer_request_sequencing_still_fences_stale_fetches():
    """#6919: the epoch is additive — the existing composer requestSeq fence
    must keep rejecting superseded sequenced fetches, and a current one must
    still be accepted."""
    result = _replay("""
  _modelDropdownRequestSeq=5;
  _fetchDeferred=_deferred();
  const staleSeq=_fetchLiveModels('openai',{},5);
  await _tick();
  _modelDropdownRequestSeq=6;
  _fetchDeferred.resolve({json:async()=>stale});
  await staleSeq;
  await _tick();
  const afterStale={cache:Object.assign({},_liveModelCache),added:addedCalls.slice()};
  _modelDropdownRequestSeq=7;
  _fetchDeferred=_deferred();
  const freshSeq=_fetchLiveModels('openai',{},7);
  await _tick();
  _fetchDeferred.resolve({json:async()=>fresh});
  await freshSeq;
  await _tick();
  process.stdout.write(JSON.stringify({
    afterStale:afterStale,
    cache:_liveModelCache,
    added:addedCalls,
    pending:[..._liveModelFetchPending],
  }));
""")
    assert result["afterStale"]["cache"] == {} and result["afterStale"]["added"] == [], (
        "#6919: a superseded requestSeq fetch must still be rejected"
    )
    assert result["cache"] == {"openai": _FRESH_MODELS}, (
        "a current requestSeq fetch must still populate the cache"
    )
    assert result["added"] == [{"provider": "openai", "ids": ["openai/model-2"]}], (
        "a current requestSeq fetch must still enrich the dropdown"
    )
    assert result["pending"] == [], "pending marker must be cleaned up"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_pending_cleanup_is_successor_aware():
    """#6919/#1169: when an obsolete fetch and a fresh successor overlap for
    the same provider, the obsolete fetch's cleanup must not clear the
    successor's pending marker (syncTopbar defers model correction on it)."""
    result = _replay("""
  const deferredOld=_deferred();
  _fetchDeferred=deferredOld;
  const obsolete=_fetchLiveModels('openai',{});
  await _tick();
  invalidateLiveModelCache();
  const deferredNew=_deferred();
  _fetchDeferred=deferredNew;
  const successor=_fetchLiveModels('openai',{});
  await _tick();
  deferredOld.resolve({json:async()=>stale});
  await obsolete;
  await _tick();
  const afterObsolete={pending:[..._liveModelFetchPending],ownerCount:_liveModelFetchOwners.size,cache:Object.assign({},_liveModelCache)};
  deferredNew.resolve({json:async()=>fresh});
  await successor;
  await _tick();
  process.stdout.write(JSON.stringify({
    afterObsolete:afterObsolete,
    cache:_liveModelCache,
    added:addedCalls,
    pending:[..._liveModelFetchPending],
    ownerCount:_liveModelFetchOwners.size,
  }));
""")
    assert result["afterObsolete"]["pending"] == ["openai"], (
        "#6919/#1169: the obsolete fetch cleared the successor's pending marker"
    )
    assert result["afterObsolete"]["ownerCount"] == 1, (
        "the successor must still own the pending marker"
    )
    assert result["afterObsolete"]["cache"] == {}, (
        "the obsolete fetch must not publish"
    )
    assert result["cache"] == {"openai": _FRESH_MODELS}, (
        "the fresh successor must publish"
    )
    assert result["added"] == [{"provider": "openai", "ids": ["openai/model-2"]}], (
        "only the fresh successor may enrich the dropdown"
    )
    assert result["pending"] == [] and result["ownerCount"] == 0, (
        "the successor's cleanup must run once it completes"
    )


_DROP_STALE_PREAMBLE = """
function _el(tag,attrs){
  const e={tagName:tag,dataset:{},children:[],title:'',value:'',textContent:'',
    appendChild(c){this.children.push(c);c.parentNode=this;return c;},
    removeChild(c){const i=this.children.indexOf(c);if(i>=0)this.children.splice(i,1);c.parentNode=null;return c;}};
  if(attrs){for(const k in attrs){if(k!=='dataset') e[k]=attrs[k];}}
  if(attrs&&attrs.dataset) e.dataset=attrs.dataset;
  return e;
}
function _makeSelect(groups,selectedValue){
  const sel={selectedIndex:0,querySelectorAll(t){return t==='optgroup'?groups:[];}};
  sel.options=[];
  for(const g of groups){for(const o of g.children) sel.options.push(o);}
  // null means "no option selected" (selectedIndex -1), which is what an
  // untouched picker looks like; a string means that option is selected.
  if(selectedValue===null){sel.selectedIndex=-1;return sel;}
  const idx=sel.options.findIndex(o=>o.value===selectedValue);
  sel.selectedIndex=idx>=0?idx:0;
  return sel;
}
const _LIVE_TITLE='Live model — fetched from provider';
function _mk(value,title,dataset){return _el('option',{value:value,title:title||'',dataset:dataset||{}});}
"""


def _replay_drop_stale(scenario: str) -> dict:
    ui = _read_static("ui.js")
    fn = _extract_function_body(ui, "function _dropStaleLiveModelOptions(")
    return _run_node(f"""
{_DROP_STALE_PREAMBLE}
{fn}
(async()=>{{
{scenario}
}})().catch(e=>{{process.stderr.write(String(e&&e.stack||e));process.exit(1);}});
""")


_ADD_LIVE_PREAMBLE = """
function _el(tag,attrs){
  const e={tagName:String(tag).toUpperCase(),dataset:{},children:[],title:'',value:'',textContent:'',
    appendChild(c){this.children.push(c);c.parentNode=this;c.parentElement=this;return c;},
    insertBefore(node,ref){const i=this.children.indexOf(ref);if(i<0){this.children.push(node);}else{this.children.splice(i,0,node);}node.parentNode=this;node.parentElement=this;return node;},
    removeChild(c){const i=this.children.indexOf(c);if(i>=0)this.children.splice(i,1);c.parentNode=null;c.parentElement=null;return c;}};
  if(attrs){for(const k in attrs){if(k!=='dataset') e[k]=attrs[k];}}
  if(attrs&&attrs.dataset) e.dataset=attrs.dataset;
  return e;
}
function _mk(value,title,dataset){return _el('option',{value:value,title:title||'',dataset:dataset||{}});}
function _makeSelect(groups,selectedValue){
  const sel={selectedIndex:-1,querySelectorAll(t){return t==='optgroup'?groups:[];}};
  Object.defineProperty(sel,'options',{get(){const out=[];for(const g of groups){for(const o of g.children) out.push(o);}return out;}});
  sel.value=selectedValue===null?'':String(selectedValue||'');
  return sel;
}
let _dynamicModelLabels={};
const S={session:{}};
const window={_activeProvider:'custom:cpa'};
const document={createElement(tag){return _el(tag);}};
function _sortModelPickerOptions(){}
function _modelStateForSelect(){return null;}
function _applyModelToDropdown(){return true;}
function syncModelChip(){}
function _getOptionProviderId(opt){
  if(!opt) return '';
  if(opt.dataset&&opt.dataset.provider) return opt.dataset.provider;
  const g=opt.parentElement;
  if(g&&g.dataset&&g.dataset.provider) return g.dataset.provider;
  return '';
}
"""


def _replay_add_live(scenario: str) -> dict:
    ui = _read_static("ui.js")
    parts = [
        _extract_function_body(ui, "function _modelPickerOptionIdentity("),
        _extract_function_body(ui, "function _deduplicateModelPickerOptions("),
        _extract_function_body(ui, "function _dropStaleLiveModelOptions("),
        _extract_function_body(ui, "function _addLiveModelsToSelect("),
    ]
    return _run_node(f"""
{_ADD_LIVE_PREAMBLE}
{"".join(parts)}
(async()=>{{
{scenario}
}})().catch(e=>{{process.stderr.write(String(e&&e.stack||e));process.exit(1);}});
""")


# ---------------------------------------------------------------------------
# #6919 (empty-membership remainder): the COMPLETE fetch → invalidate → add →
# prune chain, replayed against the real production functions. Only the fetch
# transport and the DOM are stubs.
#
# The maintainer's schedule:
#   1. Hydrated Settings picker: a selected live model, a static catalog
#      choice, and another provider's option.
#   2. A completed live fetch inserts `@custom:cpa:gone` and the catalog's
#      routed twin.
#   3. invalidateLiveModelCache() dispatches its replacement.
#   4. The replacement resolves with an explicitly empty membership → the
#      unselected `gone` option must go, while the selection, the
#      catalog-origin option and the other provider's option survive.
# ---------------------------------------------------------------------------

# A DOM faithful enough for the real pruner: ``selectedIndex`` is DERIVED from
# ``sel.value`` (the production pruner resolves the protected selection via
# sel.options[sel.selectedIndex], and the sort helper clears it with -1), so a
# mutation can never silently desync the two and fake a protection.
_EMPTY_MEMBERSHIP_PREAMBLE = """
function _el(tag,attrs){
  const e={tagName:String(tag).toUpperCase(),dataset:{},children:[],title:'',value:'',textContent:'',
    appendChild(c){this.children.push(c);c.parentNode=this;c.parentElement=this;return c;},
    insertBefore(node,ref){const i=this.children.indexOf(ref);if(i<0){this.children.push(node);}else{this.children.splice(i,0,node);}node.parentNode=this;node.parentElement=this;return node;},
    removeChild(c){const i=this.children.indexOf(c);if(i>=0)this.children.splice(i,1);c.parentNode=null;c.parentElement=null;return c;}};
  if(attrs){for(const k in attrs){if(k!=='dataset') e[k]=attrs[k];}}
  if(attrs&&attrs.dataset) e.dataset=attrs.dataset;
  return e;
}
function _mk(value,title,dataset){return _el('option',{value:value,title:title||'',dataset:dataset||{}});}
function _makeSelect(groups,selectedValue){
  const sel={querySelectorAll(t){return t==='optgroup'?groups:[];}};
  Object.defineProperty(sel,'options',{get(){const out=[];for(const g of groups){for(const o of g.children) out.push(o);}return out;}});
  sel.value=selectedValue===null?'':String(selectedValue||'');
  Object.defineProperty(sel,'selectedIndex',{
    get(){const opts=sel.options;if(!sel.value)return -1;const i=opts.findIndex(o=>o.value===sel.value);return i>=0?i:-1;},
    set(v){if(v===null||v<0){sel.value='';return;}const opts=sel.options;sel.value=(opts[v]&&opts[v].value)||'';},
    configurable:true,
  });
  return sel;
}
const _LIVE_TITLE='Live model — fetched from provider';
let _dynamicModelLabels={};
const S={session:{}};
const window={_activeProvider:'custom:cpa'};
const document={createElement(tag){return _el(tag);},baseURI:'http://localhost/',getElementById(id){return id==='settingsModel'?_settingsSel:null;}};
const location={href:'http://localhost/'};
function _sortModelPickerOptions(){}
function _modelStateForSelect(){return null;}
function _applyModelToDropdown(){return true;}
function syncModelChip(){}
function _getOptionProviderId(opt){
  if(!opt) return '';
  if(opt.dataset&&opt.dataset.provider) return opt.dataset.provider;
  const g=opt.parentElement;
  if(g&&g.dataset&&g.dataset.provider) return g.dataset.provider;
  return '';
}
const _liveModelCache={};
const _liveModelFetchPending=new Set();
const _liveModelFetchOwners=new Map();
let _liveModelInvalidationEpoch=0;
let _modelDropdownRequestSeq=0;
function _redirectIfUnauth(){return false;}
const fetchUrls=[];
const fetchDeferreds=[];
function _deferred(){let resolve;const promise=new Promise(r=>{resolve=r});return {promise,resolve};}
function fetch(url){fetchUrls.push(url);const d=_deferred();fetchDeferreds.push(d);return d.promise;}
const _tick=()=>new Promise(r=>setTimeout(r,0));
let _settingsSel=null;
const console={debug(){},log(){},warn(){},error(){}};
"""


def _replay_empty_membership(scenario: str) -> dict:
    """Replay the complete fetch → invalidate → add → prune chain with the
    REAL production functions from static/ui.js."""
    ui = _read_static("ui.js")
    parts = [
        _extract_function_body(ui, "function _modelPickerOptionIdentity("),
        _extract_function_body(ui, "function _deduplicateModelPickerOptions("),
        _extract_function_body(ui, "function _dropStaleLiveModelOptions("),
        _extract_function_body(ui, "function _addLiveModelsToSelect("),
        _extract_function_body(ui, "function invalidateLiveModelCache("),
        _extract_function_body(ui, "async function _fetchLiveModels("),
    ]
    return _run_node(f"""
{_EMPTY_MEMBERSHIP_PREAMBLE}
{"".join(parts)}
(async()=>{{
{scenario}
}})().catch(e=>{{process.stderr.write(String(e&&e.stack||e));process.exit(1);}});
""")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_reconcile_preserves_a_static_choice_replaced_by_a_live_twin():
    """Greptile follow-up: a routed live replacement stands in for a bare static
    catalog row and dedup removes the bare one. If a later refresh omits that
    model (a capped live list can change order), reconciliation must not delete
    the replacement — the static catalog still offers the model."""
    result = _replay_add_live("""
  const group=_el('optgroup',{dataset:{provider:'custom:cpa'}});
  group.appendChild(_mk('model-x',''));
  const sel=_makeSelect([group],null);
  // First refresh: live advertises model-x -> routed replacement replaces the
  // bare catalog row, and dedup drops the bare one.
  _addLiveModelsToSelect('custom:cpa',[{id:'model-x',label:'Model X'}],sel,{reconcile:true});
  const afterFirst=group.children.map(o=>o.value);
  // Second refresh: the live list no longer advertises model-x, but the static
  // catalog still does. The picker must keep offering it.
  _addLiveModelsToSelect('custom:cpa',[{id:'model-y',label:'Model Y'}],sel,{reconcile:true});
  const afterSecond=group.children.map(o=>o.value);
  process.stdout.write(JSON.stringify({afterFirst:afterFirst,afterSecond:afterSecond}));
""")
    assert result["afterFirst"] == ["@custom:cpa:model-x"], (
        "the routed replacement must take the bare catalog row's place — "
        f"got {result['afterFirst']}"
    )
    assert result["afterSecond"] == ["@custom:cpa:model-x", "@custom:cpa:model-y"], (
        "a model the static catalog still offers must survive a refresh that "
        f"omits it from the live list — got {result['afterSecond']}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_reconcile_still_drops_a_live_only_option():
    """Control for the fix above: a live-only option (no static twin) must still
    be removed when the provider stops advertising it."""
    result = _replay_add_live("""
  const group=_el('optgroup',{dataset:{provider:'custom:cpa'}});
  const sel=_makeSelect([group],null);
  _addLiveModelsToSelect('custom:cpa',[{id:'live-only',label:'Live Only'}],sel,{reconcile:true});
  const afterFirst=group.children.map(o=>o.value);
  _addLiveModelsToSelect('custom:cpa',[{id:'other',label:'Other'}],sel,{reconcile:true});
  const afterSecond=group.children.map(o=>o.value);
  process.stdout.write(JSON.stringify({afterFirst:afterFirst,afterSecond:afterSecond}));
""")
    assert result["afterFirst"] == ["@custom:cpa:live-only"], (
        f"unexpected first-refresh shape: {result['afterFirst']}"
    )
    assert result["afterSecond"] == ["@custom:cpa:other"], (
        "a live-only option the provider no longer advertises must be dropped — "
        f"got {result['afterSecond']}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_removed_live_models_are_dropped_from_the_settings_picker():
    """#6919 (Greptile follow-up): Refresh Models must also remove live options
    the provider has since dropped, otherwise they stay selectable forever and
    the refresh looks successful while the picker still offers dead entries."""
    result = _replay_drop_stale("""
  const group=_el('optgroup',{dataset:{provider:'openai'}});
  group.appendChild(_mk('static-model',''));
  group.appendChild(_mk('openai/live-a',_LIVE_TITLE,{provider:'openai',live:'1'}));
  group.appendChild(_mk('openai/gone',_LIVE_TITLE,{provider:'openai',live:'1'}));
  const other=_el('optgroup',{dataset:{provider:'zai'}});
  other.appendChild(_mk('zai/gone',_LIVE_TITLE,{provider:'zai',live:'1'}));
  const sel=_makeSelect([group,other],'static-model');
  const removed=_dropStaleLiveModelOptions('openai',new Set(['openai/live-a']),sel);
  process.stdout.write(JSON.stringify({
    removed:removed,
    openai:group.children.map(o=>o.value),
    zai:other.children.map(o=>o.value),
  }));
""")
    assert result["removed"] == 1, f"expected exactly one stale drop, got {result}"
    assert result["openai"] == ["static-model", "openai/live-a"], (
        "the dropped live model must go and the still-advertised one must stay; "
        f"static catalog rows must never be touched — got {result['openai']}"
    )
    assert result["zai"] == ["zai/gone"], (
        "reconciliation is provider-scoped; another provider's live options must survive"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_stale_live_model_that_is_selected_survives_reconciliation():
    """#6919: removing the user's current selection would silently change what
    the next save submits. Keep it selectable; only stop advertising the others."""
    result = _replay_drop_stale("""
  const group=_el('optgroup',{dataset:{provider:'openai'}});
  group.appendChild(_mk('openai/live-a',_LIVE_TITLE,{provider:'openai',live:'1'}));
  group.appendChild(_mk('openai/gone',_LIVE_TITLE,{provider:'openai',live:'1'}));
  const sel=_makeSelect([group],'openai/gone');
  const removed=_dropStaleLiveModelOptions('openai',new Set(['openai/live-a']),sel);
  process.stdout.write(JSON.stringify({removed:removed,values:group.children.map(o=>o.value)}));
""")
    assert result["removed"] == 0, "the selected option must never be dropped"
    assert result["values"] == ["openai/live-a", "openai/gone"], (
        f"the current selection must survive reconciliation — got {result['values']}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_legacy_live_options_without_the_data_marker_are_still_reconciled():
    """#6919: options created before the data-live marker existed are identified
    by their title, so a refresh in an already-open page still prunes them."""
    result = _replay_drop_stale("""
  const group=_el('optgroup',{dataset:{provider:'openai'}});
  group.appendChild(_mk('openai/legacy-gone',_LIVE_TITLE,{provider:'openai'}));
  const sel=_makeSelect([group],null);
  const removed=_dropStaleLiveModelOptions('openai',new Set([]),sel);
  process.stdout.write(JSON.stringify({removed:removed,values:group.children.map(o=>o.value)}));
""")
    assert result["removed"] == 1 and result["values"] == [], (
        f"a live option without data-live must still be pruned — got {result}"
    )


def test_settings_reconcile_is_wired_and_additive():
    """#6919 (Greptile follow-up): the Settings receiver must reconcile against
    the fresh payload, and the append path must tag its options so reconcile can
    tell live rows from static catalog rows."""
    ui = _read_static("ui.js")
    add = _extract_function_body(ui, "function _addLiveModelsToSelect(")
    assert "opts.reconcile" in add, "the append path must accept the reconcile mode"
    assert "_dropStaleLiveModelOptions(provider,refreshed,sel)" in add, (
        "the append path must reconcile after adding the fresh payload"
    )
    assert "opt.dataset.live='1'" in add, (
        "live options must be tagged so static catalog rows are never pruned"
    )
    assert "opt.dataset.catalogOrigin='1'" in add, (
        "a routed replacement for a static catalog twin must record its catalog "
        "origin, or a refresh that omits it deletes a still-valid catalog choice"
    )
    drop = _extract_function_body(ui, "function _dropStaleLiveModelOptions(")
    assert "catalogOrigin==='1'" in drop, (
        "reconciliation must skip options whose model the static catalog still offers"
    )
    invalidate = _extract_function_body(ui, "function invalidateLiveModelCache(")
    assert "{reconcile:true}" in invalidate, (
        "the Settings re-arm must ask for reconciliation, not a plain append"
    )
    fetch_live = _extract_function_body(ui, "async function _fetchLiveModels(")
    assert "_addLiveModelsToSelect(provider,data.models,sel,opts)" in fetch_live, (
        "the fresh-response path must forward the reconcile mode"
    )
    assert "_addLiveModelsToSelect(provider,_liveModelCache[provider],sel,opts)" in fetch_live, (
        "the cached-response path must forward the reconcile mode too"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_invalidation_rearms_settings_receiver_when_it_revokes_a_pending_fetch():
    """#6919 (gate follow-up): Settings → Conversation keeps its own live-model
    receiver. A Refresh Models round revokes its in-flight fetch through the
    epoch fence, so the same invalidation must dispatch a replacement for the
    still-active provider instead of leaving the picker unenriched until the
    whole Settings panel is reopened."""
    result = _replay("""
  _settingsSel={value:'openai/model-0',options:[1]};
  window._activeProvider='openai';
  _fetchDeferred=_deferred();
  const settingsFetch=_fetchLiveModels('openai',_settingsSel);
  await _tick();
  // Refresh Models: epoch advance revokes the Settings fetch above.
  invalidateLiveModelCache();
  await _tick();
  const dispatched=fetchUrls.length;
  // The revoked response lands first: it must neither publish nor enrich.
  fetchDeferreds[0].resolve({json:async()=>({models:[{id:'openai/stale',label:'Stale'}]})});
  await settingsFetch;
  await _tick();
  const afterRevoked={cache:Object.assign({},_liveModelCache),added:addedCalls.slice()};
  // The replacement (same still-valid identifier, gate's shape) lands: it must
  // enrich the Settings receiver.
  fetchDeferreds[1].resolve({json:async()=>({models:[{id:'openai/live-only',label:'Live Only'}]})});
  await _tick();
  process.stdout.write(JSON.stringify({
    dispatched:dispatched,
    afterRevoked:afterRevoked,
    cache:_liveModelCache,
    added:addedCalls,
    pending:[..._liveModelFetchPending],
    ownerCount:_liveModelFetchOwners.size,
  }));
""")
    assert result["dispatched"] == 2, (
        "#6919: invalidating a pending Settings fetch must dispatch a replacement "
        f"for the active provider, got {result['dispatched']} fetch(es)"
    )
    assert result["afterRevoked"]["cache"] == {} and result["afterRevoked"]["added"] == [], (
        "the revoked Settings response must not publish or enrich"
    )
    assert result["cache"] == {"openai": [{"id": "openai/live-only", "label": "Live Only"}]}, (
        "the replacement response must publish to the live-model cache"
    )
    assert result["added"] == [{"provider": "openai", "ids": ["openai/live-only"]}], (
        "the replacement response must enrich the Settings receiver"
    )
    assert result["pending"] == [] and result["ownerCount"] == 0, (
        "the replacement fetch's cleanup must leave no pending marker"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_invalidation_rearms_settings_receiver_after_completed_enrichment():
    """#6919 (gate follow-up, second completion order): when the Settings fetch
    already published before Refresh Models ran, the eviction still has to be
    followed by a replacement — the picker must end up enriched from the fresh
    response, not merely lose the entry the eviction deleted."""
    result = _replay("""
  _settingsSel={value:'openai/live-only',options:[1]};
  window._activeProvider='openai';
  _fetchDeferred=_deferred();
  const settingsFetch=_fetchLiveModels('openai',_settingsSel);
  await _tick();
  fetchDeferreds[0].resolve({json:async()=>({models:[{id:'openai/live-only',label:'Live Only'}]})});
  await settingsFetch;
  await _tick();
  const beforeInvalidate={cache:Object.assign({},_liveModelCache),added:addedCalls.slice()};
  invalidateLiveModelCache();
  await _tick();
  const afterInvalidate={cache:Object.assign({},_liveModelCache),dispatched:fetchUrls.length};
  // Same still-valid identifier: the replacement is accepted on scheduling,
  // not on the payload looking different.
  fetchDeferreds[1].resolve({json:async()=>({models:[{id:'openai/live-only',label:'Live Only'}]})});
  await _tick();
  process.stdout.write(JSON.stringify({
    beforeInvalidate:beforeInvalidate,
    afterInvalidate:afterInvalidate,
    cache:_liveModelCache,
    added:addedCalls,
    pending:[..._liveModelFetchPending],
  }));
""")
    assert result["beforeInvalidate"]["added"] == [
        {"provider": "openai", "ids": ["openai/live-only"]}
    ], "the initial Settings fetch must enrich before the invalidation"
    assert result["afterInvalidate"]["cache"] == {}, (
        "the invalidation must evict the completed Settings entry"
    )
    assert result["afterInvalidate"]["dispatched"] == 2, (
        "an invalidation after the response settled must still dispatch a "
        f"replacement, got {result['afterInvalidate']['dispatched']} fetch(es)"
    )
    assert result["cache"] == {"openai": [{"id": "openai/live-only", "label": "Live Only"}]}, (
        "the replacement must republish the live-only model for the Settings receiver"
    )
    assert len(result["added"]) == 2 and result["added"][1]["ids"] == ["openai/live-only"], (
        "the Settings receiver must be enriched again from the replacement response"
    )
    assert result["pending"] == [], "no pending marker may leak from the replacement"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settings_rearm_skips_unhydrated_picker():
    """#6919: a Settings pane that never ran loadSettingsPanel() has no static
    catalog to enrich — opening it repaints from /api/models anyway. The re-arm
    must not fire a stray live fetch for it (control for the replacement)."""
    result = _replay("""
  window._activeProvider='openai';
  _settingsSel={value:'',options:[]};
  invalidateLiveModelCache();
  await _tick();
  const emptyPickerFetchCount=fetchUrls.length;
  _settingsSel=null;
  invalidateLiveModelCache();
  await _tick();
  process.stdout.write(JSON.stringify({
    emptyPickerFetchCount:emptyPickerFetchCount,
    noPickerFetchCount:fetchUrls.length,
  }));
""")
    assert result["emptyPickerFetchCount"] == 0, (
        "an unhydrated Settings picker (no options) must not trigger a live fetch"
    )
    assert result["noPickerFetchCount"] == 0, (
        "a page without the Settings picker must not trigger a live fetch"
    )


def test_settings_rearm_never_reloads_the_settings_form():
    """#6919: the replacement must be additive. Re-running the Settings loader
    would paint /api/settings over unsaved edits and reset the selection, so the
    invalidation may only dispatch _fetchLiveModels() for the Settings receiver
    and must do so after eviction."""
    ui = _read_static("ui.js")
    invalidate = _extract_function_body(ui, "function invalidateLiveModelCache(")
    assert "_fetchLiveModels" in invalidate, (
        "the invalidation revokes the Settings fetch, so it must also dispatch "
        "the replacement for that receiver"
    )
    assert "'settingsModel'" in invalidate, (
        "the replacement must target the Settings model picker, not only the "
        "composer dropdown"
    )
    for forbidden in ("loadSettingsPanel()", "innerHTML", "replaceChildren"):
        assert forbidden not in invalidate, (
            f"the replacement must not reset the Settings form (found {forbidden!r})"
        )
    assert invalidate.index("delete _liveModelCache") < invalidate.index(
        "_fetchLiveModels(provider,sel,null,{reconcile:true})"
    ), (
        "the replacement must be dispatched AFTER eviction; before it, "
        "_fetchLiveModels() would hit the surviving cache entry and never re-fetch"
    )


# ---------------------------------------------------------------------------
# #6919 (empty-membership remainder): an explicitly successful EMPTY live-model
# response must reconcile the picker to an empty set. Before the fix the
# empty array returned early in two places (the fetch path and the append
# path), so the pruner was never reached and a removed live-only option
# stayed selectable.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_explicitly_empty_membership_reconciles_the_picker_to_empty():
    """The maintainer's schedule, end to end: hydrated Settings picker →
    live fetch inserts `gone` + the catalog's routed twin → invalidate →
    the replacement resolves with an explicitly empty membership.

    The unselected, removed live-only option must disappear; the current
    selection, the catalog-origin option and the other provider's option must
    all survive.
    """
    result = _replay_empty_membership("""
  // 1. Hydrated Settings picker: a selected live model, a static catalog
  //    choice, and another provider's option.
  const group=_el('optgroup',{dataset:{provider:'custom:cpa'}});
  group.appendChild(_mk('model-x',''));                       // static catalog
  group.appendChild(_mk('@custom:cpa:selected',_LIVE_TITLE,{provider:'custom:cpa',live:'1'}));
  const otherGroup=_el('optgroup',{dataset:{provider:'other'}});
  otherGroup.appendChild(_mk('other/model',_LIVE_TITLE,{provider:'other',live:'1'}));
  const sel=_makeSelect([group,otherGroup],'@custom:cpa:selected');
  _settingsSel=sel;

  // 2. A completed live fetch inserts `@custom:cpa:gone` and the catalog's
  //    routed twin.
  const first=_fetchLiveModels('custom:cpa',sel,null,{reconcile:true});
  await _tick();
  fetchDeferreds[0].resolve({json:async()=>({models:[
    {id:'model-x',label:'Model X'},
    {id:'gone',label:'Gone'},
  ]})});
  await first;
  await _tick();
  const afterFetch=group.children.map(o=>o.value);
  const selectedBefore=sel.value;

  // 3. invalidateLiveModelCache() correctly dispatches its replacement.
  invalidateLiveModelCache();
  await _tick();
  const dispatched=fetchUrls.length;

  // 4. The replacement resolves with an explicitly empty membership.
  fetchDeferreds[1].resolve({json:async()=>({models:[]})});
  await _tick();
  await _tick();

  process.stdout.write(JSON.stringify({
    afterFetch:afterFetch,
    selectedBefore:selectedBefore,
    dispatched:dispatched,
    afterEmpty:group.children.map(o=>o.value),
    otherAfter:otherGroup.children.map(o=>o.value),
    selectedAfter:sel.value,
  }));
""")
    assert result["afterFetch"] == [
        "@custom:cpa:model-x",
        "@custom:cpa:selected",
        "@custom:cpa:gone",
    ], f"unexpected pre-reconcile picker shape: {result['afterFetch']}"
    assert result["dispatched"] == 2, (
        "#6919: the invalidation must dispatch exactly one replacement fetch, "
        f"got {result['dispatched']}"
    )
    assert "@custom:cpa:gone" not in result["afterEmpty"], (
        "#6919: an explicitly empty live-model response must drop the "
        "unselected, removed live-only option — it is still selectable: "
        f"{result['afterEmpty']}"
    )
    assert result["selectedAfter"] == "@custom:cpa:selected", (
        "#6919: reconciliation must never remove the current selection — "
        f"got {result['selectedAfter']}"
    )
    assert "@custom:cpa:selected" in result["afterEmpty"], (
        "#6919: the selected option must stay selectable — "
        f"got {result['afterEmpty']}"
    )
    assert "@custom:cpa:model-x" in result["afterEmpty"], (
        "#6919: the catalog-origin option (a routed stand-in for a static "
        f"catalog row) must survive reconciliation — got {result['afterEmpty']}"
    )
    assert result["otherAfter"] == ["other/model"], (
        "#6919: reconciliation is provider-scoped; another provider's options "
        f"must survive — got {result['otherAfter']}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_discovery_failure_does_not_reconcile_the_picker_to_empty():
    """Negative control for the empty-membership fix: a discovery FAILURE is
    not authoritative membership. The endpoint publishes its failure shape
    with an ``error`` marker, and that must never prune the picker — a
    provider we could not reach says nothing about what it offers.

    The option under assertion is deliberately UNSELECTED: the pruner exempts
    the current selection, so a selected option would survive every prune and
    the assertion could not discriminate. Here a DIFFERENT live option holds
    the selection, so the option being asserted on is a genuine prune
    candidate.
    """
    result = _replay_empty_membership("""
  const group=_el('optgroup',{dataset:{provider:'custom:cpa'}});
  // The current selection is a DIFFERENT live option, so `gone` below is an
  // unselected live-only choice — the shape the pruner would remove.
  group.appendChild(_mk('@custom:cpa:selected',_LIVE_TITLE,{provider:'custom:cpa',live:'1'}));
  group.appendChild(_mk('@custom:cpa:gone',_LIVE_TITLE,{provider:'custom:cpa',live:'1'}));
  const sel=_makeSelect([group],'@custom:cpa:selected');
  _settingsSel=sel;

  const first=_fetchLiveModels('custom:cpa',sel,null,{reconcile:true});
  await _tick();
  fetchDeferreds[0].resolve({json:async()=>({models:[
    {id:'selected',label:'Selected'},
    {id:'gone',label:'Gone'},
  ]})});
  await first;
  await _tick();
  const beforeFailure=group.children.map(o=>o.value);

  invalidateLiveModelCache();
  await _tick();
  // The replacement FAILS to discover anything: the endpoint's failure shape.
  fetchDeferreds[1].resolve({json:async()=>({models:[],error:'no_models_discovered'})});
  await _tick();
  await _tick();

  process.stdout.write(JSON.stringify({
    beforeFailure:beforeFailure,
    afterFailure:group.children.map(o=>o.value),
    selectedAfter:sel.value,
  }));
""")
    assert "@custom:cpa:gone" in result["beforeFailure"], (
        "the fixture must place an unselected live-only option in the picker — "
        f"got {result['beforeFailure']}"
    )
    assert result["afterFailure"] == [
        "@custom:cpa:selected",
        "@custom:cpa:gone",
    ], (
        "#6919: a discovery failure must NOT be read as authoritative empty "
        "membership — the UNSELECTED live-only option must survive: "
        f"{result['afterFailure']}"
    )
    assert result["selectedAfter"] == "@custom:cpa:selected", (
        "#6919: a discovery failure must not disturb the selection"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_malformed_live_model_response_does_not_reconcile_the_picker_to_empty():
    """Negative control: a MISSING or MALFORMED payload says nothing about the
    provider's membership either. It must leave the picker untouched.

    As in the failure control above, the option under assertion is
    UNSELECTED — a different live option holds the selection, so the
    assertion genuinely discriminates instead of riding on the pruner's
    selected-option exemption.
    """
    for label, payload in (
        ("missing-models", "{}"),
        ("models-not-a-list", '{"models":"nope"}'),
        ("models-null", '{"models":null}'),
        ("not-an-object", "[]"),
    ):
        result = _replay_empty_membership(f"""
  const group=_el('optgroup',{{dataset:{{provider:'custom:cpa'}}}});
  group.appendChild(_mk('@custom:cpa:selected',_LIVE_TITLE,{{provider:'custom:cpa',live:'1'}}));
  group.appendChild(_mk('@custom:cpa:gone',_LIVE_TITLE,{{provider:'custom:cpa',live:'1'}}));
  const sel=_makeSelect([group],'@custom:cpa:selected');
  _settingsSel=sel;

  const first=_fetchLiveModels('custom:cpa',sel,null,{{reconcile:true}});
  await _tick();
  fetchDeferreds[0].resolve({{json:async()=>({{models:[
    {{id:'selected',label:'Selected'}},
    {{id:'gone',label:'Gone'}},
  ]}})}});
  await first;
  await _tick();

  invalidateLiveModelCache();
  await _tick();
  fetchDeferreds[1].resolve({{json:async()=>({payload})}});
  await _tick();
  await _tick();

  process.stdout.write(JSON.stringify({{afterMalformed:group.children.map(o=>o.value)}}));
""")
        assert result["afterMalformed"] == [
            "@custom:cpa:selected",
            "@custom:cpa:gone",
        ], (
            f"#6919: a {label} response must NOT prune the picker — the "
            "UNSELECTED live-only option must survive: "
            f"{result['afterMalformed']}"
        )


def test_empty_membership_reconcile_is_wired_to_the_pruner():
    """Structural pin: the empty-membership path must actually reach the
    pruner, and the empty array must only be short-circuited on the plain
    append path.

    The behaviour tests above go red when the wiring is removed, but a
    direct call to the pruner stays green even if the fetch path never
    calls it — so the reachability is pinned separately.
    """
    ui = _read_static("ui.js")
    fetch_live = _extract_function_body(ui, "async function _fetchLiveModels(")
    assert "_authoritativeEmpty" in fetch_live, (
        "#6919: the fetch path must distinguish an authoritative empty "
        "membership from a discovery failure"
    )
    assert "!data.error" in fetch_live, (
        "#6919: the endpoint's failure shape carries an `error` marker — the "
        "fetch path must treat it as a non-authoritative empty result"
    )
    add = _extract_function_body(ui, "function _addLiveModelsToSelect(")
    # The reconcile runs against the refreshed set, which is EMPTY for an
    # authoritative empty membership — that is what prunes the live-only
    # choices.
    assert "if(reconcile&&refreshed) _dropStaleLiveModelOptions(provider,refreshed,sel);" in add, (
        "#6919: the append path must reconcile after adding the fresh payload"
    )
    assert "const refreshed=reconcile?new Set():null;" in add, (
        "#6919: the reconcile set must start EMPTY, so an authoritative empty "
        "membership reconciles the picker to an empty set"
    )
    # The append block (and with it the optgroup creation) must be skipped
    # for an empty payload, so an empty membership never manufactures a
    # live group.
    assert "if(models.length){" in add, (
        "#6919: the append block must be skipped for an empty membership"
    )
    guard = add.index("if(models.length){")
    group_create = add.index("providerGroup=document.createElement('optgroup')")
    append_loop = add.index("for(const m of models){")
    prune_call = add.index("_dropStaleLiveModelOptions(provider,refreshed,sel)")
    assert guard < group_create < append_loop, (
        "#6919: the optgroup creation and the append loop must sit inside the "
        "non-empty guard"
    )
    assert prune_call > append_loop, (
        "#6919: the reconcile must run AFTER the append, so the fresh "
        "payload's own options cannot be dropped as stale"
    )
    # The plain append path keeps its empty-array no-op: without the
    # reconcile request, an empty payload must not touch the picker.
    assert "const reconcile=!!(opts&&opts.reconcile);" in add, (
        "#6919: only the reconcile path may reconcile an empty membership — "
        "the plain append path keeps its empty-array no-op"
    )


# ---------------------------------------------------------------------------
# #6919 (gate C48-F3): deduplication and pruning must share ONE identity.
# The append path recognizes a NORMALIZED identity (vendor/Model ===
# vendor/model, dashes as dots, case-insensitive) and skips the fresh
# spelling; the prune path compared RAW values and deleted the existing
# spelling — Settings ended up with neither option after a Refresh Models
# click that reported HTTP 200.
#
# Fix specification: maintain one Settings option per successful fresh
# provider/model identity; reconcile dedup and prune consistently while
# preserving canonical fresh outgoing IDs and selected/static/sibling
# choices; cover case-variant and same-spelling behaviour.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_case_variant_live_option_survives_reconcile_prune():
    """C48-F3 discriminator: an UNSELECTED live-only option whose spelling
    differs only in case from the fresh member must survive reconciliation.
    Dedup skips the fresh spelling (same normalized identity), so pruning the
    old one removes both. The selected option, the static catalog row, the
    sibling provider's option and the canonical routed outgoing ID must all
    stay untouched, and exactly one option may remain for the identity."""
    result = _replay_add_live("""
  const group=_el('optgroup',{dataset:{provider:'custom:cpa'}});
  group.appendChild(_mk('@custom:cpa:static-pick',''));   // static catalog row
  group.appendChild(_mk('@custom:cpa:selected','Live model — fetched from provider',{provider:'custom:cpa',live:'1'}));
  group.appendChild(_mk('@custom:cpa:vendor/Model','Live model — fetched from provider',{provider:'custom:cpa',live:'1'}));
  const other=_el('optgroup',{dataset:{provider:'other'}});
  other.appendChild(_mk('other/model','Live model — fetched from provider',{provider:'other',live:'1'}));
  const sel=_makeSelect([group,other],'@custom:cpa:selected');
  _addLiveModelsToSelect('custom:cpa',[
    {id:'selected',label:'Selected'},
    {id:'vendor/Model',label:'Vendor Model'},
    {id:'static-pick',label:'Static Pick'},
  ],sel,{reconcile:true});
  const afterFirst=group.children.map(o=>o.value);
  // Fresh payload advertises the LOWERCASE spelling of the same identity.
  _addLiveModelsToSelect('custom:cpa',[
    {id:'selected',label:'Selected'},
    {id:'vendor/model',label:'Vendor Model'},
    {id:'static-pick',label:'Static Pick'},
  ],sel,{reconcile:true});
  const afterSecond=group.children.map(o=>o.value);
  const vendorCount=afterSecond.filter(v=>String(v).toLowerCase().includes('vendor/')).length;
  process.stdout.write(JSON.stringify({
    afterFirst:afterFirst,
    afterSecond:afterSecond,
    vendorCount:vendorCount,
    selectedAfter:sel.value,
    otherAfter:other.children.map(o=>o.value),
  }));
""")
    assert result["afterFirst"] == [
        "@custom:cpa:static-pick",
        "@custom:cpa:selected",
        "@custom:cpa:vendor/Model",
    ], f"unexpected hydrated picker shape: {result['afterFirst']}"
    assert result["vendorCount"] == 1, (
        "C48-F3: the case-variant identity must survive with exactly ONE "
        f"Settings option — got {result['afterSecond']}"
    )
    assert "@custom:cpa:vendor/Model" in result["afterSecond"], (
        "C48-F3: the existing vendor/Model option must NOT be pruned by a "
        f"fresh vendor/model — got {result['afterSecond']}"
    )
    assert result["selectedAfter"] == "@custom:cpa:selected", (
        "the current selection must survive reconciliation — "
        f"got {result['selectedAfter']}"
    )
    assert "@custom:cpa:selected" in result["afterSecond"], (
        "the selected option must stay selectable — "
        f"got {result['afterSecond']}"
    )
    assert "@custom:cpa:static-pick" in result["afterSecond"], (
        "the static catalog row must never be pruned — "
        f"got {result['afterSecond']}"
    )
    assert result["otherAfter"] == ["other/model"], (
        "reconciliation is provider-scoped; the sibling provider's option "
        f"must survive — got {result['otherAfter']}"
    )
    # The canonical routed outgoing ID of the fresh member must be the
    # option's value — no rewrite, no duplicate.
    assert sorted(result["afterSecond"]) == sorted([
        "@custom:cpa:static-pick",
        "@custom:cpa:selected",
        "@custom:cpa:vendor/Model",
    ]), f"unexpected post-reconcile picker shape: {result['afterSecond']}"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_same_spelling_live_option_survives_reconcile_prune():
    """Same-spelling control for the C48-F3 fix: an option whose spelling
    EXACTLY matches a fresh member survives (this must stay green before and
    after the identity fix), while an ordinary stale live-only member is
    still pruned on the next refresh."""
    result = _replay_add_live("""
  const group=_el('optgroup',{dataset:{provider:'custom:cpa'}});
  group.appendChild(_mk('@custom:cpa:vendor/model','Live model — fetched from provider',{provider:'custom:cpa',live:'1'}));
  group.appendChild(_mk('@custom:cpa:gone','Live model — fetched from provider',{provider:'custom:cpa',live:'1'}));
  const sel=_makeSelect([group],null);
  _addLiveModelsToSelect('custom:cpa',[
    {id:'vendor/model',label:'Vendor Model'},
    {id:'gone',label:'Gone'},
  ],sel,{reconcile:true});
  const afterFirst=group.children.map(o=>o.value);
  _addLiveModelsToSelect('custom:cpa',[{id:'vendor/model',label:'Vendor Model'}],sel,{reconcile:true});
  const afterSecond=group.children.map(o=>o.value);
  process.stdout.write(JSON.stringify({afterFirst:afterFirst,afterSecond:afterSecond}));
""")
    assert result["afterFirst"] == ["@custom:cpa:vendor/model", "@custom:cpa:gone"], (
        f"unexpected hydrated picker shape: {result['afterFirst']}"
    )
    assert result["afterSecond"] == ["@custom:cpa:vendor/model"], (
        "the same-spelling live option must survive and the stale live-only "
        f"option must still be pruned — got {result['afterSecond']}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_case_variant_live_only_option_survives_the_refresh_chain():
    """C48-F3, end to end — the maintainer's browser schedule: Settings holds
    an UNSELECTED live-only OpenRouter option `vendor/Model`; Refresh Models
    posts 200 and the fresh /api/models/live DATA advertises `vendor/model`.
    At least one Settings option for the identity must survive, and the
    selection, static catalog row and sibling-provider option stay intact."""
    result = _replay_empty_membership("""
  window._activeProvider='openrouter';
  const group=_el('optgroup',{dataset:{provider:'openrouter'}});
  group.appendChild(_mk('openrouter/static-pick',''));   // static catalog row
  group.appendChild(_mk('openrouter/selected','Live model — fetched from provider',{provider:'openrouter',live:'1'}));
  group.appendChild(_mk('vendor/Model','Live model — fetched from provider',{provider:'openrouter',live:'1'}));
  const other=_el('optgroup',{dataset:{provider:'zai'}});
  other.appendChild(_mk('zai/keep','Live model — fetched from provider',{provider:'zai',live:'1'}));
  const sel=_makeSelect([group,other],'openrouter/selected');
  _settingsSel=sel;

  // A completed live fetch hydrates the picker with the same spellings.
  const first=_fetchLiveModels('openrouter',sel,null,{reconcile:true});
  await _tick();
  fetchDeferreds[0].resolve({json:async()=>({models:[
    {id:'openrouter/selected',label:'Selected'},
    {id:'vendor/Model',label:'Vendor Model'},
    {id:'openrouter/static-pick',label:'Static Pick'},
  ]})});
  await first;
  await _tick();
  const afterFetch=group.children.map(o=>o.value);

  // Refresh Models: the epoch invalidation dispatches the replacement fetch.
  invalidateLiveModelCache();
  await _tick();
  const dispatched=fetchUrls.length;

  // The fresh live DATA advertises the case-variant spelling.
  fetchDeferreds[1].resolve({json:async()=>({models:[
    {id:'openrouter/selected',label:'Selected'},
    {id:'vendor/model',label:'Vendor Model'},
    {id:'openrouter/static-pick',label:'Static Pick'},
  ]})});
  await _tick();
  await _tick();
  const afterRefresh=group.children.map(o=>o.value);
  const vendorCount=afterRefresh.filter(v=>String(v).toLowerCase().includes('vendor/')).length;
  process.stdout.write(JSON.stringify({
    afterFetch:afterFetch,
    dispatched:dispatched,
    afterRefresh:afterRefresh,
    vendorCount:vendorCount,
    selectedAfter:sel.value,
    otherAfter:other.children.map(o=>o.value),
  }));
""")
    assert result["afterFetch"] == [
        "openrouter/static-pick",
        "openrouter/selected",
        "vendor/Model",
    ], f"unexpected hydrated picker shape: {result['afterFetch']}"
    assert result["dispatched"] == 2, (
        "the invalidation must dispatch exactly one replacement fetch, "
        f"got {result['dispatched']}"
    )
    assert result["vendorCount"] == 1, (
        "C48-F3: after a refresh whose fresh DATA advertises vendor/model, "
        "the Settings picker must still offer the vendor/Model identity — "
        f"got {result['afterRefresh']}"
    )
    assert "vendor/Model" in result["afterRefresh"], (
        "C48-F3: the existing unselected live-only option must survive the "
        f"refresh chain — got {result['afterRefresh']}"
    )
    assert result["selectedAfter"] == "openrouter/selected", (
        "the refresh chain must never move the selection — "
        f"got {result['selectedAfter']}"
    )
    assert "openrouter/selected" in result["afterRefresh"] and "openrouter/static-pick" in result["afterRefresh"], (
        f"the selected and static choices must survive: {result['afterRefresh']}"
    )
    assert result["otherAfter"] == ["zai/keep"], (
        "the sibling provider's option must survive the refresh chain — "
        f"got {result['otherAfter']}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_same_spelling_live_only_option_survives_the_refresh_chain():
    """Same-spelling control for the refresh chain: the fresh DATA advertises
    the exact existing spelling — the option survives, no duplicate appears,
    and a genuinely stale live-only option is still pruned. Must stay green
    before and after the identity fix."""
    result = _replay_empty_membership("""
  window._activeProvider='openrouter';
  const group=_el('optgroup',{dataset:{provider:'openrouter'}});
  group.appendChild(_mk('openrouter/selected','Live model — fetched from provider',{provider:'openrouter',live:'1'}));
  group.appendChild(_mk('vendor/model','Live model — fetched from provider',{provider:'openrouter',live:'1'}));
  group.appendChild(_mk('vendor/gone','Live model — fetched from provider',{provider:'openrouter',live:'1'}));
  const sel=_makeSelect([group],'openrouter/selected');
  _settingsSel=sel;

  const first=_fetchLiveModels('openrouter',sel,null,{reconcile:true});
  await _tick();
  fetchDeferreds[0].resolve({json:async()=>({models:[
    {id:'openrouter/selected',label:'Selected'},
    {id:'vendor/model',label:'Vendor Model'},
    {id:'vendor/gone',label:'Gone'},
  ]})});
  await first;
  await _tick();

  invalidateLiveModelCache();
  await _tick();
  // Same spelling, stale member removed from the fresh membership.
  fetchDeferreds[1].resolve({json:async()=>({models:[
    {id:'openrouter/selected',label:'Selected'},
    {id:'vendor/model',label:'Vendor Model'},
  ]})});
  await _tick();
  await _tick();
  const afterRefresh=group.children.map(o=>o.value);
  const vendorCount=afterRefresh.filter(v=>String(v).toLowerCase().includes('vendor/')).length;
  process.stdout.write(JSON.stringify({
    afterRefresh:afterRefresh,
    vendorCount:vendorCount,
    selectedAfter:sel.value,
  }));
""")
    assert result["vendorCount"] == 1, (
        "the same-spelling option must survive exactly once — "
        f"got {result['afterRefresh']}"
    )
    assert result["afterRefresh"] == [
        "openrouter/selected",
        "vendor/model",
    ], (
        "the same-spelling option must survive while the stale live-only "
        f"option is pruned — got {result['afterRefresh']}"
    )
    assert result["selectedAfter"] == "openrouter/selected", (
        "the refresh chain must never move the selection — "
        f"got {result['selectedAfter']}"
    )


def test_prune_uses_the_same_identity_as_dedup():
    """Structural pin for C48-F3: the reconcile set must carry the NORMALIZED
    identity of every fresh member, and the pruner must resolve each live
    option's NORMALIZED identity (guarded for standalone extraction). Raw
    value and identity then agree, so vendor/Model -> vendor/model cannot
    remove both."""
    ui = _read_static("ui.js")
    add = _extract_function_body(ui, "function _addLiveModelsToSelect(")
    drop = _extract_function_body(ui, "function _dropStaleLiveModelOptions(")
    assert "optionIdentity(mid,provider)" in add, (
        "C48-F3: the reconcile set must carry each fresh member's NORMALIZED "
        "identity — the pruner compares identities, not raw spellings"
    )
    assert "_modelPickerOptionIdentity" in drop, (
        "C48-F3: the pruner must resolve the NORMALIZED identity of each live "
        "option — a fresh vendor/model must match an existing vendor/Model"
    )
