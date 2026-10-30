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
        dom = body.find("_addLiveModelsToSelect(provider,data.models,sel)")
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
const document={baseURI:'http://localhost/'};
const location={href:'http://localhost/'};
const fetchUrls=[];
let _fetchDeferred=null;
function fetch(url){fetchUrls.push(url);return _fetchDeferred.promise;}
function _deferred(){let resolve;const promise=new Promise(r=>{resolve=r});return {promise,resolve};}
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
