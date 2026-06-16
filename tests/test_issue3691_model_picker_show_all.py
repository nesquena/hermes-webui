"""Regression tests for #3691: provider-agnostic model-picker overflow groups."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import types
import urllib.request
from pathlib import Path

import pytest

import api.config as config


REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


class _FakeResponse:
    def __init__(self, payload: dict):
        self._buf = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self._buf


@pytest.fixture(autouse=True)
def _clear_models_cache():
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    yield
    try:
        config.invalidate_models_cache()
    except Exception:
        pass


def _openrouter_group() -> dict:
    return next(g for g in config.get_available_models()["groups"] if g["provider_id"] == "openrouter")


def test_populate_model_dropdown_persists_extra_models_for_picker_runtime():
    assert "dataset.extraModels=JSON.stringify(g.extra_models)" in UI_JS, (
        "populateModelDropdown() must persist extra_models onto the optgroup so "
        "renderModelDropdown() can search hidden overflow models before expansion."
    )


def test_native_model_selectors_include_overflow_extra_models():
    """The non-picker native <select> model selectors (Settings / Cron / Profile /
    Auxiliary) must include g.extra_models, not just g.models — otherwise the
    server-side overflow split (#3691) silently hides every model beyond the first
    15 for any large provider in those selectors."""
    assert PANELS_JS.count("extra_models") >= 4, (
        "Settings/Cron/Profile/Auxiliary model selectors must each include g.extra_models "
        "so large-provider catalogs aren't truncated to the visible-15 picker cap."
    )
    assert "[...(g.models||[]),...(g.extra_models||[])]" in PANELS_JS, (
        "The composer-mirroring native selector must concat models + extra_models."
    )


def test_show_all_row_uses_i18n_key():
    assert "t('model_show_all_models',hiddenCount)" in UI_JS, (
        "The synthetic overflow row must use an i18n key instead of hardcoded English."
    )
    assert I18N_JS.count("model_show_all_models:") >= 10, (
        "model_show_all_models should be defined across the shipped locale blocks."
    )
    assert "Mostrar todos los {0} modelos" in I18N_JS
    assert "Afficher tous les {0} modèles" in I18N_JS


def test_openrouter_overflow_preserves_hidden_tail(monkeypatch):
    monkeypatch.setattr(
        config,
        "cfg",
        {
            "model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"},
            "providers": {"openrouter": {"api_key": "sk-or-test-key"}},
        },
        raising=False,
    )
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.fetch_openrouter_models = lambda: [
        ("anthropic/claude-sonnet-4.6", ""),
        ("openai/gpt-4o", ""),
    ]
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)

    payload = {
        "data": [
            {
                "id": f"vendor{i}/overflow-{i}:free",
                "name": f"Overflow {i}",
                "supported_parameters": [],
                "pricing": {"prompt": "0", "completion": "0"},
            }
            for i in range(40)
        ]
    }
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(payload))

    group = _openrouter_group()
    total = len(group["models"]) + len(group.get("extra_models", []))
    capped_total = 2 + config._OPENROUTER_FREE_TIER_AUGMENT_CAP

    assert len(group["models"]) == config._MODEL_PICKER_VISIBLE_TARGET
    assert total == capped_total, "OpenRouter overflow models must move into extra_models within the capped augmentation budget."
    assert any(m["id"] == "vendor29/overflow-29:free" for m in group.get("extra_models", [])), (
        "The last capped free-tier model should land in extra_models once the visible picker cap is reached."
    )
    assert all(m["id"] != "vendor30/overflow-30:free" for bucket in ("models", "extra_models") for m in group.get(bucket, [])), (
        "Free-tier augmentation must stop at the configured cap instead of continuing through the whole live payload."
    )


def test_deduplicate_model_ids_includes_extra_models():
    groups = [
        {
            "provider": "Alpha",
            "provider_id": "alpha",
            "models": [{"id": "shared/model", "label": "Shared Model"}],
            "extra_models": [{"id": "alpha/only-extra", "label": "Alpha Extra"}],
        },
        {
            "provider": "Beta",
            "provider_id": "beta",
            "models": [{"id": "beta/visible", "label": "Beta Visible"}],
            "extra_models": [{"id": "shared/model", "label": "Shared Model"}],
        },
    ]

    config._deduplicate_model_ids(groups)

    assert groups[0]["models"][0]["id"] == "shared/model"
    assert groups[1]["extra_models"][0]["id"] == "@beta:shared/model"
    assert groups[1]["extra_models"][0]["label"] == "Shared Model (Beta)"


def test_openrouter_free_tier_selection_stays_visible_when_selected_id_is_bare():
    ordered = [
        {"id": f"@openrouter:vendor/model-{idx}", "label": f"Model {idx}"}
        for idx in range(config._MODEL_PICKER_VISIBLE_TARGET)
    ]
    ordered.append({"id": "@openrouter:vendor/selected-model:free", "label": "Selected Free"})

    visible, extra = config._split_picker_overflow_models(
        ordered,
        selected_model_id="vendor/selected-model:free",
        provider_id="openrouter",
        threshold=config._MODEL_PICKER_OVERFLOW_THRESHOLD,
        target=config._MODEL_PICKER_VISIBLE_TARGET,
    )

    assert any(m["id"] == "@openrouter:vendor/selected-model:free" for m in visible), (
        "A bare OpenRouter :free selection must stay visible when the selected model is in overflow."
    )
    assert all(m["id"] != "@openrouter:vendor/selected-model:free" for m in extra)


_DROPDOWN_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let openParen = ui.indexOf('(', start);
  let i = openParen + 1;
  let parenDepth = 1;
  while (parenDepth > 0 && i < ui.length) {
    if (ui[i] === '(') parenDepth++;
    else if (ui[i] === ')') parenDepth--;
    i++;
  }
  i = ui.indexOf('{', i);
  let depth = 1;
  i++;
  while (depth > 0 && i < ui.length) {
    if (ui[i] === '{') depth++;
    else if (ui[i] === '}') depth--;
    i++;
  }
  return ui.slice(start, i);
}

function makeClassList(initial) {
  const set = new Set(initial || []);
  return {
    _set: set,
    add(cls) { set.add(cls); },
    remove(cls) { set.delete(cls); },
    contains(cls) { return set.has(cls); },
    toggle(cls, force) {
      if (force === true) { set.add(cls); return true; }
      if (force === false) { set.delete(cls); return false; }
      if (set.has(cls)) { set.delete(cls); return false; }
      set.add(cls);
      return true;
    },
  };
}

function defineClassName(node) {
  Object.defineProperty(node, 'className', {
    get() { return [...node.classList._set].join(' '); },
    set(v) { node.classList = makeClassList(String(v || '').split(/\s+/).filter(Boolean)); },
  });
}

function makeNode(tag) {
  const node = {
    tagName: String(tag || '').toUpperCase(),
    children: [],
    dataset: {},
    style: {},
    parentElement: null,
    textContent: '',
    value: '',
    tabIndex: 0,
    onclick: null,
    _listeners: {},
    _innerHTML: '',
    appendChild(child) {
      child.parentElement = this;
      this.children.push(child);
      if (this.tagName === 'OPTGROUP' && this._ownerSelect && child.tagName === 'OPTION') {
        this._ownerSelect.options.push(child);
      }
      return child;
    },
    addEventListener(type, handler) { this._listeners[type] = handler; },
    querySelector(selector) { return this._qs ? this._qs[selector] || null : null; },
    setAttribute(name, value) { this[name] = value; },
    focus() { this._focused = true; },
  };
  node.classList = makeClassList();
  defineClassName(node);
  Object.defineProperty(node, 'innerHTML', {
    get() { return this._innerHTML; },
    set(v) {
      this._innerHTML = String(v || '');
      this.children = [];
      this._qs = {};
      if (this.tagName === 'DIV' && this._innerHTML.includes('model-search-input')) {
        const input = makeNode('input');
        input.className = 'model-search-input';
        const clear = makeNode('button');
        clear.className = 'model-search-clear';
        this._qs['.model-search-input'] = input;
        this._qs['.model-search-clear'] = clear;
      } else if (this.tagName === 'DIV' && this._innerHTML.includes('model-custom-input')) {
        const input = makeNode('input');
        input.className = 'model-custom-input';
        const btn = makeNode('button');
        btn.className = 'model-custom-btn';
        this._qs['.model-custom-input'] = input;
        this._qs['.model-custom-btn'] = btn;
      }
    },
  });
  return node;
}

function makeOption(value, label, parent) {
  const opt = makeNode('option');
  opt.value = value;
  opt.textContent = label || value;
  opt.parentElement = parent || null;
  return opt;
}

function makeSelect(groups, selectedValue) {
  const sel = { id: 'modelSelect', children: [], options: [], value: selectedValue || '' };
  for (const group of groups || []) {
    const og = makeNode('optgroup');
    og.label = group.provider || '';
    og.dataset.provider = group.provider_id || '';
    og._ownerSelect = sel;
    if (group.extra_models) og.dataset.extraModels = JSON.stringify(group.extra_models);
    for (const model of group.models || []) {
      og.appendChild(makeOption(model.id, model.label || model.id, og));
    }
    sel.children.push(og);
    sel.options.push(...og.children);
  }
  return sel;
}

function snapshot(dd) {
  return dd.children.map(node => ({
    className: node.className,
    textContent: node.textContent,
    html: node._innerHTML || '',
  }));
}

const payload = JSON.parse(process.argv[3]);
const dropdown = makeNode('div');
dropdown.classList.add('open');
const modelSelect = makeSelect(payload.groups, payload.selectedValue || payload.groups[0].models[0].id);

function $(id) {
  if (id === 'composerModelDropdown') return dropdown;
  if (id === 'modelSelect') return modelSelect;
  return null;
}
const window = { _configuredModelBadges: payload.configuredBadges || {} };
const document = { createElement(tag) { return makeNode(tag); } };
function esc(v) { return String(v || ''); }
function t(key, ...args) {
  if (key === 'model_show_all_models') return `Show all ${args[0]} models`;
  return key;
}
function li() { return 'x'; }
function getModelLabel(v) { return String(v || ''); }
function _providerFromModelValue(v) {
  const value = String(v || '');
  if (value.startsWith('@') && value.includes(':')) return value.slice(1, value.lastIndexOf(':'));
  return '';
}
function _normalizeConfiguredModelKey(v) { return String(v || '').toLowerCase(); }
function _getConfiguredModelBadge(value, badgeMap) { return badgeMap[value] || null; }
function closeModelDropdown() {}
function selectModelFromDropdown() {}

for (const name of [
  '_readModelOverflowData',
  '_appendOverflowOptionsToGroup',
  'renderModelDropdown',
]) {
  eval(extractFunc(name));
}

renderModelDropdown();
const initial = snapshot(dropdown);
const initialShowAllRow = dropdown.children.find(node => String(node._innerHTML || '').includes('Show all'));
const searchInput = dropdown.children[1].querySelector('.model-search-input');
searchInput.value = payload.searchTerm;
searchInput._listeners.input();
const searched = snapshot(dropdown);
initialShowAllRow.onclick({ stopPropagation() {} });
const searchInputAfterExpand = dropdown.children[1].querySelector('.model-search-input');
searchInputAfterExpand.value = '';
searchInputAfterExpand._listeners.input();
const expanded = snapshot(dropdown);

process.stdout.write(JSON.stringify({
  initial,
  searched,
  expanded,
  optionCountAfterExpand: modelSelect.children[0].children.length,
  hiddenDatasetAfterExpand: modelSelect.children[0].dataset.extraModels || '',
}));
"""


@pytest.fixture(scope="module")
def _dropdown_driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("issue3691_dropdown_driver") / "driver.js"
    path.write_text(_DROPDOWN_DRIVER, encoding="utf-8")
    return str(path)


def _run_dropdown_driver(driver_path: str, payload: dict | None = None) -> dict:
    if payload is None:
        payload = {
            "groups": [
                {
                    "provider": "OpenRouter",
                    "provider_id": "openrouter",
                    "models": [
                        {"id": "openrouter/visible-one", "label": "Visible One"},
                        {"id": "openrouter/visible-two", "label": "Visible Two"},
                    ],
                    "extra_models": [
                        {"id": "openrouter/overflow-one", "label": "Overflow One"},
                        {"id": "openrouter/overflow-two", "label": "Overflow Two"},
                    ],
                }
            ],
            "searchTerm": "overflow-two",
        }
    result = subprocess.run(
        [NODE, driver_path, str(REPO / "static" / "ui.js"), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_runtime_picker_shows_generic_expander_and_searches_hidden_overflow(_dropdown_driver_path):
    out = _run_dropdown_driver(_dropdown_driver_path)

    initial_html = "\n".join(item["html"] for item in out["initial"])
    searched_html = "\n".join(item["html"] for item in out["searched"])
    expanded_html = "\n".join(item["html"] for item in out["expanded"])

    assert "Show all 2 models" in initial_html, (
        "The picker should render a synthetic show-all row when extra_models are present."
    )
    assert "openrouter/overflow-two" in searched_html, (
        "Filtering must already match hidden overflow models before the group is expanded."
    )
    assert "Show all 2 models" not in expanded_html, (
        "Once expanded, the synthetic row should disappear and the live options should take over."
    )
    assert out["optionCountAfterExpand"] == 4, (
        "Expanding a capped group must append the hidden models into the live optgroup."
    )
    assert out["hiddenDatasetAfterExpand"] == "[]", (
        "After expansion the optgroup should no longer advertise a hidden overflow tail."
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_runtime_picker_preserves_backend_decorated_nous_heading_without_double_count(
    _dropdown_driver_path,
):
    payload = {
        "groups": [
            {
                "provider": "Nous (2 of 4)",
                "provider_id": "nous",
                "models": [
                    {"id": "@nous:visible-one", "label": "Visible One"},
                    {"id": "@nous:visible-two", "label": "Visible Two"},
                ],
                "extra_models": [
                    {"id": "@nous:hidden-one", "label": "Hidden One"},
                    {"id": "@nous:hidden-two", "label": "Hidden Two"},
                ],
            }
        ],
        "searchTerm": "",
    }
    out = _run_dropdown_driver(_dropdown_driver_path, payload)

    # The decorated overflow count ("Nous (2 of 4)") belongs on the GROUP HEADING
    # (rendered via textContent), NOT stamped onto every per-row provider chip.
    heading_text = "\n".join(item["textContent"] for item in out["initial"])
    row_html = "\n".join(item["html"] for item in out["initial"])

    assert "Nous (2 of 4) (4)" not in heading_text, (
        "Backend-decorated Nous headings must not get a second frontend count suffix."
    )
    assert "Nous (2 of 4)" in heading_text, (
        "The picker should preserve the backend-crafted Nous heading verbatim when overflow exists."
    )
    # Regression (#3691 row-chip leak): the per-row provider chip must show the PLAIN
    # provider label, never the "(N of M)" overflow count — otherwise the count is
    # repeated on every row and reads as nonsense after "Show all" expands the group.
    assert "(2 of 4)" not in row_html, (
        "The per-row provider chip must not carry the overflow count; it belongs on the heading only."
    )
    assert 'class="model-opt-provider">Nous<' in row_html, (
        "Each row's provider chip should render the plain provider label (e.g. 'Nous')."
    )
    # After expand, the heading must not double-count ("Nous (2 of 4) (4)") — it should
    # strip the backend decoration and show the plain rendered-row count. (#3691)
    expanded_heading_text = "\n".join(item["textContent"] for item in out["expanded"])
    assert "(2 of 4) (4)" not in expanded_heading_text, (
        "Expanded heading must not append a second count onto the decorated overflow label."
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_runtime_picker_excludes_configured_hidden_models_from_show_all_count(
    _dropdown_driver_path,
):
    payload = {
        "groups": [
            {
                "provider": "OpenRouter",
                "provider_id": "openrouter",
                "models": [
                    {"id": "openrouter/visible-one", "label": "Visible One"},
                ],
                "extra_models": [
                    {"id": "openrouter/overflow-one", "label": "Overflow One"},
                    {"id": "openrouter/overflow-two", "label": "Overflow Two"},
                ],
            }
        ],
        "configuredBadges": {
            "openrouter/overflow-two": {
                "label": "Primary",
                "role": "primary",
                "provider": "openrouter",
            }
        },
        "searchTerm": "",
    }
    out = _run_dropdown_driver(_dropdown_driver_path, payload)

    initial_html = "\n".join(item["html"] for item in out["initial"])
    assert "Show all 1 models" in initial_html, (
        "Configured overflow models should be excluded from the provider-group "
        "show-all count once they are lifted into the Configured section."
    )
    assert "Show all 2 models" not in initial_html, (
        "The show-all label must not over-report configured hidden overflow models."
    )
