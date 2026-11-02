"""Re-gate #7777 round 3 (maintainer CHANGES_REQUESTED, 2026-10-06).

Three findings from the review of head ``8a2eba849`` (rebased onto master):

1. MUST-FIX — the /api/models DISK cache persists and rebuilds only the older
   payload fields, so a disk-cache hit returns no ``picker_excludes``. The
   cache fingerprint still includes the policy, so the stale-looking file is
   accepted and the browser resets ``window._pickerExcludes`` to ``{}``: after
   a restart a hidden model is re-injected and selected.
2. SHOULD-FIX — with everything excluded the normal builder returns
   ``groups: []`` with no ``no_eligible_models`` flag (only the minimal/static
   builder sets it), so the browser keeps the stale option; and the empty
   branch cleared the picker even when the running session's model is excluded.
3. SHOULD-FIX — an explicit ``/model <hidden>`` pick hits the newly guarded
   ``_ensureModelOptionInDropdown``, which returns null; the caller then fires
   ``onchange`` on the previous row and reports a switch that did not happen.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO))

import api.config as config
import api.routes as routes

UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


@pytest.fixture(autouse=True)
def _isolate_caches():
    """The TTL cache and the /api/models/live cache would otherwise hand back
    a previous test's catalog and silently pass."""
    for fn in (
        lambda: config.invalidate_models_cache(),
        lambda: routes._clear_live_models_cache(),
    ):
        try:
            fn()
        except Exception:
            pass
    yield
    for fn in (
        lambda: config.invalidate_models_cache(),
        lambda: routes._clear_live_models_cache(),
    ):
        try:
            fn()
        except Exception:
            pass


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Point the disk cache at a tmp path and pin the runtime version so the
    #1633 stamps line up."""
    import api.updates as upd

    cache_path = tmp_path / "models_cache.json"
    monkeypatch.setattr(config, "_models_cache_path", cache_path)
    original = upd.WEBUI_VERSION
    monkeypatch.setattr(upd, "WEBUI_VERSION", "v0.99.999-test")
    yield cache_path
    upd.WEBUI_VERSION = original


def _patch_settings(monkeypatch, excludes: dict) -> None:
    """Point ``load_settings`` at a fixed picker_excludes map."""
    monkeypatch.setattr(
        config,
        "load_settings",
        lambda: {"picker_excludes": excludes, "default_model": "test/x"},
    )


def _shape_cache(**overrides) -> dict:
    cache = {
        "active_provider": "openai",
        "default_model": "gpt-keep",
        "configured_model_badges": {},
        "groups": [
            {
                "provider": "OpenAI",
                "provider_id": "openai",
                "models": [{"id": "gpt-keep", "label": "GPT Keep"}],
            }
        ],
        "aliases": {},
    }
    cache.update(overrides)
    return cache


# ── Finding 1: the disk cache must carry the policy across a restart ─────


class TestFinding1DiskCachePersistsPolicy:
    """A restart forgets the policy today: the disk cache never stores
    ``picker_excludes`` and neither loader restores it."""

    def test_save_writes_picker_excludes(self, isolated_cache, monkeypatch):
        monkeypatch.setattr(
            config,
            "load_settings",
            lambda: {"picker_excludes": {"openai": ["gpt-x"]},
                     "default_model": "test/x"},
        )
        config._save_models_cache_to_disk(_shape_cache())

        on_disk = json.loads(isolated_cache.read_text(encoding="utf-8"))
        assert on_disk.get("picker_excludes") == {"openai": ["gpt-x"]}, (
            "the disk cache dropped picker_excludes — a restart would forget "
            "the policy and resurrect a hidden model (#7777 MUST-FIX 1)"
        )

    def test_strict_load_restores_picker_excludes(self, isolated_cache, monkeypatch):
        monkeypatch.setattr(
            config,
            "load_settings",
            lambda: {"picker_excludes": {"openai": ["gpt-x"]},
                     "default_model": "test/x"},
        )
        config._save_models_cache_to_disk(_shape_cache())

        loaded = config._load_models_cache_from_disk()

        assert loaded is not None
        assert loaded.get("picker_excludes") == {"openai": ["gpt-x"]}, (
            "a strict disk-cache hit must restore picker_excludes"
        )
        # Disk-only metadata still stripped, and the groups come back intact.
        assert "_webui_version" not in loaded
        assert [g["provider_id"] for g in loaded["groups"]] == ["openai"]
        assert [m["id"] for m in loaded["groups"][0]["models"]] == ["gpt-keep"]

    def test_stale_load_restores_picker_excludes(self, isolated_cache, monkeypatch):
        """The timeout fallback serves under a superseded WebUI version, but
        it still reads the same settings store — it must not be the one path
        that forgets the policy."""
        monkeypatch.setattr(
            config,
            "load_settings",
            lambda: {"picker_excludes": {"anthropic": ["claude-x"]},
                     "default_model": "test/x"},
        )
        config._save_models_cache_to_disk(_shape_cache())

        # Bump the runtime version so the STRICT loader rejects the file
        # (that is the stale loader's documented role: timeout fallback).
        monkeypatch.setattr(config, "_current_webui_version", lambda: "v1.0.0")
        assert config._load_models_cache_from_disk() is None

        stale = config._load_stale_models_cache_from_disk()
        assert stale is not None
        assert stale.get("picker_excludes") == {"anthropic": ["claude-x"]}, (
            "the stale loader must also restore picker_excludes"
        )

    def test_legacy_disk_cache_without_policy_still_loads(self, isolated_cache, monkeypatch):
        """A pre-policy cache file must remain loadable with an empty map
        rather than being rejected."""
        legacy = _shape_cache()
        legacy["_schema_version"] = config._MODELS_CACHE_SCHEMA_VERSION
        legacy["_webui_version"] = "v0.99.999-test"
        legacy["_source_fingerprint"] = config._models_cache_source_fingerprint()
        isolated_cache.write_text(json.dumps(legacy), encoding="utf-8")
        monkeypatch.setattr(
            config,
            "_models_cache_source_fingerprint",
            lambda: legacy["_source_fingerprint"],
        )

        loaded = config._load_models_cache_from_disk()

        assert loaded is not None
        assert loaded.get("picker_excludes") == {}, (
            "a legacy cache file must load with an empty policy map"
        )


# ── Finding 2: every builder flags an all-excluded catalog ───────────────


class TestFinding2NormalBuilderFlagsNoEligibleModels:
    def test_normal_builder_sets_flag_when_every_model_is_excluded(self, monkeypatch):
        monkeypatch.setattr(
            config,
            "cfg",
            {
                "model": {"provider": "openai", "default": "gpt-x"},
                "providers": {"openai": {"api_key": "test", "models": ["gpt-x"]}},
            },
            raising=False,
        )
        _patch_settings(monkeypatch, {"openai": ["gpt-x"]})

        result = config.get_available_models(force_refresh=True)

        assert result.get("groups") == []
        assert result.get("no_eligible_models") is True, (
            "the normal builder must set no_eligible_models like the minimal "
            "builder does, or the browser keeps the stale option (#7777 "
            "SHOULD-FIX 2)"
        )

    def test_normal_builder_leaves_flag_absent_when_something_survives(self, monkeypatch):
        monkeypatch.setattr(
            config,
            "cfg",
            {
                "model": {"provider": "openai", "default": "gpt-keep"},
                "providers": {
                    "openai": {"api_key": "test", "models": ["gpt-keep", "gpt-x"]},
                },
            },
            raising=False,
        )
        _patch_settings(monkeypatch, {"openai": ["gpt-x"]})

        result = config.get_available_models(force_refresh=True)

        assert "no_eligible_models" not in result
        ids = [
            m["id"]
            for g in result.get("groups", []) or []
            for m in (g.get("models") or []) + (g.get("extra_models") or [])
        ]
        assert "gpt-keep" in ids

    def test_static_builder_flag_unchanged(self, monkeypatch):
        """The already-closed round-2 behaviour must not regress."""
        monkeypatch.setattr(
            config, "cfg",
            {"model": {"provider": "openai", "default": "gpt-x"}},
            raising=False,
        )
        _patch_settings(monkeypatch, {"openai": ["gpt-x"]})

        result = config._minimal_static_models_catalog()

        assert result["groups"] == []
        assert result.get("no_eligible_models") is True


# ── Finding 3: an explicit /model pick bypasses the picker policy ────────


_CMD_MODEL_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');
const commands = fs.readFileSync(process.argv[3], 'utf8');
const scenario = process.argv[4] || 'bare';

function extractFunc(name, src) {
  const source = src;
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const m = re.exec(source);
  if (!m) throw new Error(name + ' not found');
  let i = source.indexOf('{', m.index);
  let depth = 1; i++;
  while (depth > 0 && i < source.length) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') depth--;
    i++;
  }
  return source.slice(m.index, i);
}

function makeSelect(id) {
  return {
    id: id || 'modelSelect',
    innerHTML: '',
    options: [],
    value: '',
    dataset: {},
    title: '',
    tagName: 'SELECT',
    onchange: null,
    querySelector: () => null,
    querySelectorAll: () => [],
    appendChild(el) {
      this.options.push(el);
      if (this.options.length === 1 && !this.value) this.value = el.value;
      return el;
    },
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {},
  };
}

// Mirrors a live build that already filtered `gpt-x` out of the rendered
// picker, leaving it only in the extras tail (the shape the reviewer hit):
// the option therefore does NOT exist yet and must be synthesized.
const CATALOG = {
  active_provider: 'openai',
  default_model: 'gpt-keep',
  aliases: { hidden: 'openai/gpt-x' },
  model_alias_routes: {},
  groups: [
    {
      provider: 'OpenAI', provider_id: 'openai',
      models: [{ id: 'gpt-keep', label: 'GPT Keep' }],
      extra_models: [{ id: 'gpt-x', label: 'GPT X' }],
    },
  ],
};

let modelSelect;
global.document = {
  createElement: (tag) => {
    const upper = String(tag).toUpperCase();
    if (upper === 'OPTGROUP') {
      return { tagName: 'OPTGROUP', label: '', dataset: {}, children: [], appendChild(o){ this.children.push(o); return o; } };
    }
    return { tagName: upper, value: '', textContent: '', title: '', dataset: {}, children: [], appendChild(o){ this.children.push(o); return o; } };
  },
  getElementById: () => null,
  baseURI: 'http://localhost/app/',
};
global.window = {
  _pickerExcludes: { openai: ['gpt-x'] },
  _activeProvider: 'openai',
  _defaultModel: 'gpt-keep',
  _configuredModelBadges: {},
  _modelCatalogGroups: CATALOG.groups,
};
global.localStorage = { _s: new Map(), getItem(k){ return this._s.has(k) ? this._s.get(k) : null; }, setItem(k,v){ this._s.set(k, String(v)); }, removeItem(k){ this._s.delete(k); } };
global.sessionStorage = { _s: new Map(), getItem(k){ return this._s.has(k) ? this._s.get(k) : null; }, setItem(k,v){ this._s.set(k, String(v)); }, removeItem(k){ this._s.delete(k); } };
global.navigator = { onLine: true };

const S = { session: { id: 's1', session_id: 's1', model: 'gpt-keep', model_provider: 'openai' } };
let _dynamicModelLabels = {};
let _liveModelCache = {};
let _liveModelFetchPending = new Set();
let _modelDropdownRequestSeq = 1;
let _liveModelFetchEpoch = 0;

const __toasts = [];
const showToast = (msg) => { __toasts.push(String(msg)); };
const $ = (id) => (id === 'modelSelect' ? modelSelect : null);
const _providerFromModelValue = (v) => {
  const value = String(v || '').trim();
  if (value.startsWith('@') && value.includes(':')) return value.slice(1, value.lastIndexOf(':'));
  return '';
};
const getModelLabel = (m) => String(m || '');
const syncModelChip = () => {};
const syncSettingsModelChip = () => {};
const syncTopbar = () => {};
const _refreshOpenModelDropdown = () => {};
const _deduplicateModelPickerOptions = () => 0;
const _applyModelToDropdown = (modelId, sel) => {
  const found = Array.from(sel.options || []).find(o => String(o.value || '') === String(modelId));
  if (found) { sel.value = found.value; return found.value; }
  return null;
};
const _modelStateForSelect = (sel, modelId) => {
  const value = String(modelId || '');
  const p = _providerFromModelValue(value);
  return p ? { model: value.slice(value.indexOf(':') + 1), model_provider: p } : { model: value, model_provider: null };
};
const _getOptionProviderId = (opt) => {
  if (!opt) return '';
  if (opt.dataset && opt.dataset.provider) return opt.dataset.provider;
  return _providerFromModelValue(opt.value);
};
const _modelPickerOptionIdentity = (modelId, providerId) => String(modelId || '').split('/').pop().replace(/-/g, '.').toLowerCase();
const _findModelInDropdown = (modelId, sel) => {
  const opts = Array.from(sel.options || []).map(o => o.value);
  return opts.includes(modelId) ? modelId : null;
};
const t = (k) => (k === 'switched_to' ? 'Switched to ' : k);

global.fetch = async (url) => {
  const href = String(url);
  if (href.includes('api/models')) {
    return { ok: true, json: async () => JSON.parse(JSON.stringify(CATALOG)) };
  }
  return { ok: true, json: async () => ({}) };
};

const names = [];
for (const src of [ui, commands]) {
  const re = /(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/g;
  let m;
  while ((m = re.exec(src))) if (!names.includes(m[1])) names.push(m[1]);
}
for (const name of names) {
  if (name === 'cmdModel') continue;
  const src = ui.includes('function ' + name + '(') ? ui : commands;
  try { eval(extractFunc(name, src)); } catch (_e) { /* optional helper */ }
}
eval(extractFunc('cmdModel', commands));

(async () => {
  let applied = null;
  modelSelect = makeSelect('modelSelect');
  modelSelect.options.push({ tagName: 'OPTION', value: 'gpt-keep', textContent: 'GPT Keep', title: '', dataset: { provider: 'openai' } });
  modelSelect.value = 'gpt-keep';
  // Mirrors static/boot.js:2327 — the onchange persists whatever the
  // <select> ends up holding, so it is the observable "did the switch happen".
  modelSelect.onchange = async () => {
    const state = _modelStateForSelect(modelSelect, modelSelect.value);
    applied = { value: modelSelect.value, model: state.model, model_provider: state.model_provider };
  };

  const arg = scenario === 'alias' ? 'hidden' : (scenario === 'unknown' ? 'no-such-model' : 'gpt-x');
  await cmdModel(arg);

  process.stdout.write(JSON.stringify({
    scenario,
    selValue: modelSelect.value,
    applied: applied,
    optionValues: modelSelect.options.map(o => o.value),
    toasts: __toasts,
  }));
})().catch(e => { process.stderr.write(String((e && e.stack) || e)); process.exit(1); });
"""


class TestFinding2BrowserEmptyCatalogBehaviour:
    """Behavioural: when every model is excluded the real
    ``populateModelDropdown`` must clear the stale options instead of leaving
    them selected, while keeping the running session's own model visible."""

    DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const m = re.exec(ui);
  if (!m) throw new Error(name + ' not found');
  let openParen = ui.indexOf('(', m.index);
  let i = openParen + 1;
  let parenDepth = 1;
  while (parenDepth > 0 && i < ui.length) {
    if (ui[i] === '(') parenDepth++;
    else if (ui[i] === ')') parenDepth--;
    i++;
  }
  i = ui.indexOf('{', i);
  let depth = 1; i++;
  while (depth > 0 && i < ui.length) {
    if (ui[i] === '{') depth++;
    else if (ui[i] === '}') depth--;
    i++;
  }
  return ui.slice(m.index, i);
}

let modelSelect = {
  id: 'modelSelect',
  options: [],
  value: 'gpt-x',
  dataset: {},
  innerHTML: '',
  querySelector: () => null,
  querySelectorAll: () => [],
  appendChild(el) { this.options.push(el); if (this.options.length === 1) this.value = el.value; return el; },
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent() {},
};
// Seed a stale (excluded) option exactly like the reviewer's repro: the
// previous rows were rendered before the policy existed.
modelSelect.options.push({ tagName: 'OPTION', value: 'gpt-x', textContent: 'GPT X', dataset: { provider: 'openai' } });

global.document = {
  createElement: (tag) => {
    const upper = String(tag).toUpperCase();
    if (upper === 'OPTGROUP') {
      return { tagName: 'OPTGROUP', label: '', dataset: {}, children: [], appendChild(o){ this.children.push(o); return o; } };
    }
    return { tagName: upper, value: '', textContent: '', title: '', dataset: {}, children: [], appendChild(o){ this.children.push(o); return o; } };
  },
  getElementById: () => null,
  baseURI: 'http://localhost/app/',
};
global.window = {
  _pickerExcludes: { openai: ['gpt-x'] },
  _activeProvider: 'openai',
  _defaultModel: '',
  _configuredModelBadges: {},
  _modelEndpointErrors: {},
  _modelCatalogGroups: [],
};
global.localStorage = { getItem(){return null;}, setItem(){}, removeItem(){} };
global.sessionStorage = { getItem(){return null;}, setItem(){}, removeItem(){} };
global.navigator = { onLine: true };

const S = { session: null };
let _dynamicModelLabels = {};
let _liveModelCache = {};
let _liveModelFetchPending = new Set();
let _liveModelFetchEpoch = 0;

const $ = (id) => (id === 'modelSelect' ? modelSelect : null);
const _providerFromModelValue = (v) => '';
const syncModelChip = () => {};
const _refreshOpenModelDropdown = () => {};
const getModelLabel = (m) => String(m || '');
const _deduplicateModelPickerOptions = () => 0;
const _applyModelToDropdown = () => null;
const _modelStateForSelect = (sel, modelId) => ({ model: String(modelId || ''), model_provider: null });
const _getOptionProviderId = () => '';
const _modelPickerOptionIdentity = (m) => String(m || '');
const _findModelInDropdown = () => null;
const _redirectIfUnauth = () => false;
const _invalidateLiveModelCache = async () => {};
const t = (k, d) => d || k;
const renderModelDropdown = () => {};
const _positionModelDropdown = () => {};

for (const name of [
  '_bumpLiveModelFetchEpoch', '_pickerExcludesForProvider', '_collectKnownPickerProviders',
  '_isKnownPickerProvider', '_bareModelIdForExcludeMatch', '_modelIsPickerExcluded',
  '_ensureModelOptionInDropdown', '_addLiveModelsToSelect', '_fetchLiveModels',
  '_applySessionModelFallback', '_modelStateFromAppliedDropdown', '_captureModelDropdownSelection',
  '_reconcileModelDropdownSelection',
]) { try { eval(extractFunc(name)); } catch (e) {} }
eval(extractFunc('populateModelDropdown'));

const PAYLOAD = process.argv[3] ? JSON.parse(process.argv[3]) : {};
global.fetch = async (url) => ({
  ok: true,
  json: async () => Object.assign({
    active_provider: 'openai',
    default_model: 'gpt-x',
    configured_model_badges: {},
    groups: [],
    no_eligible_models: true,
    picker_excludes: { openai: ['gpt-x'] },
  }, PAYLOAD),
});

(async () => {
  const sessionModel = process.argv[4] || '';
  if (sessionModel) S.session = { model: sessionModel, model_provider: 'openai' };
  await populateModelDropdown({});
  // The empty-catalog path re-arms itself with a `session_visit` freshness
  // retry that is deliberately NOT awaited (fire-and-forget in production).
  // Flush the macrotask queue so the assertion observes the FINAL state.
  await new Promise((r) => setTimeout(r, 50));
  process.stdout.write(JSON.stringify({
    selValue: modelSelect.value,
    optionCount: modelSelect.options.length,
    optionValues: modelSelect.options.map(o => o.value),
  }));
})().catch(e => { process.stderr.write(String((e && e.stack) || e)); process.exit(1); });
"""

    @pytest.mark.skipif(NODE is None, reason="node not on PATH")
    def _run(self, tmp_path, payload=None, session_model=""):
        driver = tmp_path / "empty_driver.js"
        driver.write_text(self.DRIVER, encoding="utf-8")
        result = subprocess.run(
            [NODE, str(driver), str(REPO / "static" / "ui.js"),
             json.dumps(payload or {}), session_model],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def test_all_excluded_clears_the_stale_option(self, tmp_path):
        """The reviewer's repro: with one configured model excluded, the
        browser must NOT keep the old ``gpt-x`` option selected."""
        out = self._run(tmp_path)
        assert out["selValue"] == "", (
            f"an all-excluded picker left {out['selValue']!r} selected — the "
            "next send would use exactly the model the user hid"
        )

    def test_all_excluded_keeps_the_running_session_model(self, tmp_path):
        """The active session's model is the documented exception: it stays
        visible and selected so the picker never disagrees with
        ``_chatPayloadModelState()``."""
        out = self._run(tmp_path, session_model="gpt-x")
        assert out["selValue"] == "@openai:gpt-x", out
        assert out["optionValues"] == ["gpt-x", "@openai:gpt-x"], out

    def test_no_policy_preserves_pre_existing_behavior(self, tmp_path):
        """Control: with no picker policy the empty-catalog branch behaves
        exactly as before (no clearing, no session re-injection)."""
        out = self._run(
            tmp_path,
            payload={"no_eligible_models": False, "picker_excludes": {}},
        )
        assert out["selValue"] == "gpt-x", out
        assert out["optionCount"] > 1, out


class TestFinding3ExplicitModelCommandBypassesPolicy:
    """``/model gpt-x`` with ``gpt-x`` hidden from the picker must still
    switch the session — the policy is display-only and the backend resolves
    an explicitly requested model."""

    def test_wiring_passes_the_opt_out(self):
        """The call site in cmdModel must pass the explicit-pick opt-out,
        otherwise ``_ensureModelOptionInDropdown`` returns null and the
        caller fires ``onchange`` on the previous row."""
        anchor = (
            "if((aliasRoute||!hasOption) && typeof _ensureModelOptionInDropdown==='function'){"
        )
        assert anchor in COMMANDS_JS
        idx = COMMANDS_JS.index(anchor)
        window = COMMANDS_JS[idx: idx + 500]
        assert "allowExcludedForActiveSession" in window, (
            "cmdModel must route _ensureModelOptionInDropdown with the "
            "explicit-pick opt-out (#7777 SHOULD-FIX 3)"
        )

    def test_empty_catalog_branch_clears_and_reconciles(self):
        """The empty branch must clear the selection before returning and
        re-apply the running session's model (the documented exception)."""
        assert "sel.value='';" in UI_JS
        assert "allowExcludedForActiveSession" in UI_JS
        assert "no_eligible_models" in UI_JS, (
            "the browser must honour the server flag (or an active policy) "
            "when the catalog is empty (#7777 SHOULD-FIX 2)"
        )

    @pytest.mark.skipif(NODE is None, reason="node not on PATH")
    def test_cmd_model_switches_to_an_excluded_model(self, tmp_path):
        """Behavioural RED/GREEN: driving the real ``cmdModel`` against a
        mocked DOM must end with the excluded model selected and applied, not
        silently snapped back to the previous row."""
        driver = tmp_path / "driver.js"
        driver.write_text(_CMD_MODEL_DRIVER, encoding="utf-8")
        result = subprocess.run(
            [NODE, str(driver), str(REPO / "static" / "ui.js"),
             str(REPO / "static" / "commands.js"), "bare"],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout)

        assert out["optionValues"] == ["gpt-keep", "@openai:gpt-x"], out
        assert out["selValue"] == "@openai:gpt-x", (
            "the excluded model must be injected and selected"
        )
        assert out["applied"]["model"] == "gpt-x", (
            f"the switch never happened — onchange persisted {out['applied']}"
        )
        assert any("gpt-x" in toast for toast in out["toasts"]), (
            f"the success toast never fired: {out['toasts']}"
        )

    @pytest.mark.skipif(NODE is None, reason="node not on PATH")
    def test_cmd_model_alias_target_is_applied(self, tmp_path):
        """With an alias (``hidden`` → ``openai/gpt-x``) the same thing must
        happen — the reported switch must match what actually happened."""
        driver = tmp_path / "driver.js"
        driver.write_text(_CMD_MODEL_DRIVER, encoding="utf-8")
        result = subprocess.run(
            [NODE, str(driver), str(REPO / "static" / "ui.js"),
             str(REPO / "static" / "commands.js"), "alias"],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout)

        assert out["applied"]["model"] == "gpt-x", (
            f"alias target never applied: {out['applied']}"
        )
        assert any("gpt-x" in toast for toast in out["toasts"]), (
            f"the success toast never fired: {out['toasts']}"
        )

    @pytest.mark.skipif(NODE is None, reason="node not on PATH")
    def test_cmd_model_still_refuses_an_unknown_model(self, tmp_path):
        """Control: the picker policy is not a blanket bypass — a name that
        matches nothing still reports no match and leaves the session alone."""
        driver = tmp_path / "driver.js"
        driver.write_text(_CMD_MODEL_DRIVER, encoding="utf-8")
        result = subprocess.run(
            [NODE, str(driver), str(REPO / "static" / "ui.js"),
             str(REPO / "static" / "commands.js"), "unknown"],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout)

        assert out["applied"] is None, (
            f"an unknown /model argument changed the session: {out['applied']}"
        )
        assert out["optionValues"] == ["gpt-keep"], out
        assert any("no_model_match" in toast or "did you mean" in toast.lower()
                   for toast in out["toasts"]), out
