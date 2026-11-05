"""#8756: one Configured identity for the badge lookup, the configured-row
dedup and the Configured section key.

#8730 (fixing #8725) moved ``_getConfiguredModelBadge`` to a provider-aware
identity while ``_isEquivalentConfiguredModelEntry`` and the Configured semantic
key kept the legacy normalization. The two disagreed:

* A: a live ``@openai:openai/gpt-6.1-sol`` row lost the Primary badge and the
  synthesized canonical row was suppressed, leaving Configured empty;
* B: a typed ``openrouter/anthropic/claude-sonnet-4.6`` alias and the catalog
  row both showed in Configured with "Fallback 1";
* C: a providerless legacy badge no longer matched ``openai/gpt-6.1-sol``.

These tests drive the real ``renderModelDropdown`` (plus the real live-model
merge and custom-option paths) under node, and keep #8725's sibling unbadged.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_RENDER_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = ui.indexOf('(', start) + 1;
  let parenDepth = 1;
  while (parenDepth > 0 && i < ui.length) {
    if (ui[i] === '(') parenDepth++;
    else if (ui[i] === ')') parenDepth--;
    i++;
  }
  i = ui.indexOf('{', i) + 1;
  let depth = 1;
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
    add(c) { set.add(c); },
    remove(c) { set.delete(c); },
    contains(c) { return set.has(c); },
    toggle(c, force) {
      const on = force === undefined ? !set.has(c) : !!force;
      if (on) set.add(c); else set.delete(c);
      return on;
    },
  };
}

function detach(node) {
  const parent = node.parentElement;
  if (!parent) return;
  parent.children = parent.children.filter(c => c !== node);
  const owner = parent.tagName === 'SELECT' ? parent : parent._ownerSelect;
  if (owner) owner.options = owner.options.filter(o => o !== node);
  node.parentElement = null;
}

function attach(parent, child, index) {
  detach(child);
  child.parentElement = parent;
  if (index >= 0) parent.children.splice(index, 0, child);
  else parent.children.push(child);
  const owner = parent.tagName === 'SELECT' ? parent : parent._ownerSelect;
  if (owner && child.tagName === 'OPTION') owner.options = owner.querySelectorAll('option');
  if (child.tagName === 'OPTGROUP') child._ownerSelect = parent;
  return child;
}

function makeNode(tag) {
  const node = {
    tagName: String(tag || '').toUpperCase(),
    children: [], dataset: {}, style: {}, parentElement: null,
    textContent: '', value: '', tabIndex: 0, onclick: null,
    _listeners: {}, _innerHTML: '', _qs: {},
    appendChild(child) { return attach(this, child, -1); },
    insertBefore(child, ref) { return attach(this, child, ref ? this.children.indexOf(ref) : -1); },
    removeChild(child) { detach(child); return child; },
    addEventListener(type, fn) { this._listeners[type] = fn; },
    querySelector(sel) { return this._qs[sel] || null; },
    querySelectorAll(sel) {
      const out = [];
      const walk = n => {
        for (const c of n.children || []) {
          if (c.tagName === String(sel).toUpperCase()) out.push(c);
          walk(c);
        }
      };
      walk(this);
      return out;
    },
    setAttribute(k, v) { this[k] = v; },
    hasAttribute(k) { return this[k] !== undefined && this[k] !== null; },
    focus() {},
  };
  node.classList = makeClassList();
  Object.defineProperty(node, 'className', {
    get() { return [...node.classList._set].join(' '); },
    set(v) { node.classList = makeClassList(String(v || '').split(/\s+/).filter(Boolean)); },
  });
  Object.defineProperty(node, 'innerHTML', {
    get() { return this._innerHTML; },
    set(v) {
      this._innerHTML = String(v || '');
      this.children = [];
      this._qs = {};
      for (const cls of ['model-search-input', 'model-search-clear', 'model-custom-input', 'model-custom-btn']) {
        if (this._innerHTML.includes(cls)) {
          const child = makeNode('input');
          child.className = cls;
          this._qs['.' + cls] = child;
        }
      }
    },
  });
  return node;
}

const payload = JSON.parse(process.argv[3]);
const modelSelect = makeNode('select');
modelSelect.id = 'modelSelect';
modelSelect.options = [];
Object.defineProperty(modelSelect, 'value', {
  get() { return this._selected ? this._selected.value : ''; },
  set(v) { this._selected = this.options.find(o => o.value === String(v || '')) || null; },
});
Object.defineProperty(modelSelect, 'selectedOptions', {
  get() { return this._selected ? [this._selected] : []; },
});
Object.defineProperty(modelSelect, 'selectedIndex', {
  get() { return this.options.indexOf(this._selected); },
});
for (const group of payload.groups || []) {
  const og = makeNode('optgroup');
  og.label = group.provider || '';
  og.dataset.provider = group.provider_id || '';
  if (group.extra_models) og.dataset.extraModels = JSON.stringify(group.extra_models);
  modelSelect.appendChild(og);
  for (const model of group.models || []) {
    const opt = makeNode('option');
    opt.value = model.id;
    opt.textContent = model.label || model.id;
    og.appendChild(opt);
  }
}
modelSelect.value = payload.selectedValue || '';

const dropdown = makeNode('div');
dropdown.classList.add('open');
function $(id) {
  if (id === 'composerModelDropdown') return dropdown;
  if (id === 'modelSelect') return modelSelect;
  return null;
}
const window = {
  _configuredModelBadges: payload.configuredBadges || {},
  _activeProvider: payload.activeProvider || '',
};
const document = { createElement(tag) { return makeNode(tag); } };
const S = { session: null };
const _dynamicModelLabels = {};
function esc(v) { return String(v || ''); }
function t(key) { return key; }
function li() { return ''; }
function getModelLabel(v) { return String(v || ''); }
function closeModelDropdown() {}
function syncModelChip() {}
function _refreshOpenModelDropdown() {}

// The real row click goes through selectModelFromDropdown(), which resolves
// the option with _ensureModelOptionInDropdown() and then re-renders.
function selectModelFromDropdown(value, provider) {
  _ensureModelOptionInDropdown(value, modelSelect, provider || '');
}

for (const name of [
  '_normalizeConfiguredModelKey',
  '_configuredModelIdentity',
  '_getConfiguredModelBadge',
  '_isEquivalentConfiguredModelEntry',
  '_getOptionProviderId',
  '_providerFromModelValue',
  '_modelPickerOptionIdentity',
  '_deduplicateModelPickerOptions',
  '_modelPickerContractRuns',
  '_modelPickerCompareRuns',
  '_modelPickerCompareContract',
  '_modelPickerSortableId',
  '_compareModelPickerEntries',
  '_sortModelPickerEntries',
  '_sortModelPickerOptions',
  '_readModelOverflowData',
  '_appendOverflowOptionsToGroup',
  '_modelStateForSelect',
  '_findModelInDropdown',
  '_applyModelToDropdown',
  '_ensureModelOptionInDropdown',
  '_addLiveModelsToSelect',
  'renderModelDropdown',
]) {
  eval(extractFunc(name));
}

// Real live /v1/models merge (catalog-to-live replacement) and real
// custom-model-ID option, in the order the page applies them.
if (payload.live) _addLiveModelsToSelect(payload.live.provider, payload.live.models, modelSelect);
if (payload.requested) _ensureModelOptionInDropdown(payload.requested, modelSelect, payload.requestedProvider || '');
renderModelDropdown();

// Rows directly under the "Configured" heading are the Configured section;
// every other row (group bodies, ungrouped tail) is reported separately.
function collect() {
  const configured = [];
  const configuredNodes = [];
  const rows = [];
  const more = [];
  let section = '';
  const visit = (node) => {
    for (const child of node.children || []) {
      const cls = String(child.className || '').split(/\s+/);
      if (node === dropdown && cls.includes('model-group')) {
        section = child.textContent === 'model_group_configured' ? 'configured' : 'groups';
        continue;
      }
      const html = String(child._innerHTML || '');
      if (cls.includes('model-opt-more')) more.push(section);
      const id = (html.match(/class="model-opt-id">([^<]*)</) || [])[1];
      if (id !== undefined && cls.includes('model-opt')) {
        const badge = (html.match(/model-opt-badge--(?!selected)([a-z]+)">([^<]*)</) || []).slice(1);
        const row = { id, role: badge[0] || '', badge: badge[1] || '', active: cls.includes('active') };
        if (node === dropdown && section === 'configured') {
          configured.push(row);
          configuredNodes.push(child);
        } else rows.push(row);
      }
      if (cls.includes('model-group-body')) visit(child);
    }
  };
  visit(dropdown);
  return {
    configured,
    configuredNodes,
    rows,
    more,
    options: modelSelect.options.map(o => o.value),
    selected: modelSelect.value,
  };
}

const first = collect();
const out = { ...first };
delete out.configuredNodes;
// Click the given Configured row `clicks` times, re-rendering after each click
// as the page does, and report the state after the last one.
if (payload.click !== undefined) {
  let state = first;
  for (let n = 0; n < (payload.clicks || 1); n++) {
    state.configuredNodes[n === 0 ? payload.click : 0].onclick();
    renderModelDropdown();
    state = collect();
  }
  out.afterClick = { ...state };
  delete out.afterClick.configuredNodes;
}
process.stdout.write(JSON.stringify(out));
"""


def _render(tmp_path, payload):
    driver = tmp_path / "render_driver.js"
    driver.write_text(_RENDER_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS_PATH), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _badge(provider, role="fallback", label="Fallback 1"):
    badge = {"role": role, "label": label}
    if provider is not None:
        badge["provider"] = provider
    return badge


def _row(data, model_id):
    matches = [row for row in data["rows"] if row["id"] == model_id]
    assert len(matches) == 1, (model_id, data["rows"])
    return matches[0]


_PRIMARY_OPENAI = _badge("openai", "primary", "Primary")


@pytest.mark.parametrize(
    "badges",
    [
        # What _build_configured_model_badges emits for default `gpt-6.1-sol`
        # matched to the catalog row `openai/gpt-6.1-sol`.
        {
            "gpt-6.1-sol": _PRIMARY_OPENAI,
            "@openai:gpt-6.1-sol": _PRIMARY_OPENAI,
            "openai/gpt-6.1-sol": _PRIMARY_OPENAI,
        },
        # The minimal map from the issue's case table.
        {"gpt-6.1-sol": _PRIMARY_OPENAI},
    ],
    ids=["backend-map", "issue-table-map"],
)
def test_live_replacement_keeps_exactly_one_primary_in_configured(tmp_path, badges):
    """Case A: the live `openai/gpt-6.1-sol` replaces the catalog row as
    `@openai:openai/gpt-6.1-sol`; Configured must still show one Primary."""
    data = _render(
        tmp_path,
        {
            "groups": [
                {
                    "provider": "OpenAI",
                    "provider_id": "openai",
                    "models": [{"id": "gpt-other"}, {"id": "openai/gpt-6.1-sol"}],
                }
            ],
            "selectedValue": "gpt-other",
            "activeProvider": "openai",
            "configuredBadges": badges,
            "live": {"provider": "openai", "models": [{"id": "openai/gpt-6.1-sol"}]},
        },
    )

    # The real live merge did replace the catalog twin.
    assert "@openai:openai/gpt-6.1-sol" in data["options"]
    assert "openai/gpt-6.1-sol" not in data["options"]
    # The live row carries the badge and is the one Configured row, as on .615.
    assert [(row["id"], row["role"]) for row in data["configured"]] == [
        ("@openai:openai/gpt-6.1-sol", "primary")
    ]


@pytest.mark.parametrize("requested_provider", ["", "openrouter"], ids=["typed", "typed-with-provider"])
def test_typed_openrouter_alias_yields_one_configured_fallback(tmp_path, requested_provider):
    """Case B: a typed `openrouter/<vendor>/<model>` alias and the catalog row
    are one configured model, so Configured lists one Fallback 1."""
    fallback = _badge("openrouter")
    data = _render(
        tmp_path,
        {
            "groups": [
                {
                    "provider": "OpenRouter",
                    "provider_id": "openrouter",
                    "models": [{"id": "openai/gpt-other"}, {"id": "anthropic/claude-sonnet-4.6"}],
                }
            ],
            "selectedValue": "openai/gpt-other",
            "configuredBadges": {
                "anthropic/claude-sonnet-4.6": fallback,
                "@openrouter:anthropic/claude-sonnet-4.6": fallback,
            },
            "requested": "openrouter/anthropic/claude-sonnet-4.6",
            "requestedProvider": requested_provider,
        },
    )

    # The typed alias is a real picker option next to the catalog row.
    assert any(value.endswith("openrouter/anthropic/claude-sonnet-4.6") for value in data["options"])
    assert "anthropic/claude-sonnet-4.6" in data["options"]
    assert [(row["id"], row["role"]) for row in data["configured"]] == [
        ("anthropic/claude-sonnet-4.6", "fallback")
    ]


@pytest.mark.parametrize("requested_provider", ["", "openrouter"], ids=["typed", "typed-with-provider"])
def test_typed_alias_never_replaces_the_native_configured_id(tmp_path, requested_provider):
    """Case B without the catalog row. The typed `openrouter/<vendor>/<model>`
    alias keeps its extra `openrouter/` segment on the wire (the resolver strips
    only `@openrouter:`), so it must not stand in for the configured
    `anthropic/claude-sonnet-4.6`: Configured still offers the native id, once."""
    fallback = _badge("openrouter")
    data = _render(
        tmp_path,
        {
            "groups": [
                {
                    "provider": "OpenRouter",
                    "provider_id": "openrouter",
                    "models": [{"id": "openai/gpt-other"}],
                }
            ],
            "selectedValue": "openai/gpt-other",
            "configuredBadges": {
                "anthropic/claude-sonnet-4.6": fallback,
                "@openrouter:anthropic/claude-sonnet-4.6": fallback,
            },
            "requested": "openrouter/anthropic/claude-sonnet-4.6",
            "requestedProvider": requested_provider,
        },
    )

    assert any(value.endswith("openrouter/anthropic/claude-sonnet-4.6") for value in data["options"])
    assert [(row["id"], row["role"]) for row in data["configured"]] == [
        ("anthropic/claude-sonnet-4.6", "fallback")
    ]


def test_typed_alias_on_a_hyphenated_provider_keeps_the_native_id(tmp_path):
    """The typed-alias rule compares normalized ids, so a provider whose name
    has a dash (`custom:foo-bar`) must be normalized the same way."""
    primary = _badge("custom:foo-bar", "primary", "Primary")
    data = _render(
        tmp_path,
        {
            "groups": [
                {
                    "provider": "Foo Bar",
                    "provider_id": "custom:foo-bar",
                    "models": [{"id": "acme/other-model"}],
                }
            ],
            "selectedValue": "acme/other-model",
            "configuredBadges": {"acme/example-model": primary},
            "requested": "@custom:foo-bar:custom:foo-bar/acme/example-model",
            "requestedProvider": "custom:foo-bar",
        },
    )

    assert [(row["id"], row["role"]) for row in data["configured"]] == [
        ("acme/example-model", "primary")
    ]


_FOO_BAR_ROW = "@custom:foo-bar:custom:foo-bar/acme/example-model"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "groups": [
                {
                    "provider": "Foo Bar",
                    "provider_id": "custom:foo-bar",
                    "models": [{"id": "acme/other-model"}, {"id": _FOO_BAR_ROW}],
                }
            ],
        },
        {
            "groups": [
                {
                    "provider": "Foo Bar",
                    "provider_id": "custom:foo-bar",
                    "models": [{"id": "acme/other-model"}],
                    "extra_models": [{"id": _FOO_BAR_ROW}],
                }
            ],
        },
        {
            "groups": [
                {"provider": "Foo Bar", "provider_id": "custom:foo-bar", "models": [{"id": "acme/other-model"}]}
            ],
            "activeProvider": "custom:foo-bar",
            "live": {"provider": "custom:foo-bar", "models": [{"id": "custom:foo-bar/acme/example-model"}]},
        },
    ],
    ids=["catalog", "overflow", "live"],
)
def test_a_provider_prefixed_row_never_replaces_the_configured_id(tmp_path, payload):
    """Gate r1103d: a catalog, overflow or live row composed as
    `@p:p/<model>` is sent as `p/<model>`, so Configured must still select the
    configured `<model>` (master does), in every section, not only typed rows."""
    data = _render(
        tmp_path,
        dict(
            payload,
            selectedValue="acme/other-model",
            configuredBadges={"acme/example-model": _badge("custom:foo-bar")},
        ),
    )

    assert [(row["id"], row["role"]) for row in data["configured"]] == [
        ("acme/example-model", "fallback")
    ]


def test_providerless_legacy_badge_still_matches_vendor_spelling(tmp_path):
    """Case C: with no provider on the row or the badge, the legacy key still
    matches `openai/gpt-6.1-sol` to a `gpt-6.1-sol` badge."""
    data = _render(
        tmp_path,
        {
            "groups": [
                {
                    "provider": "Legacy",
                    "provider_id": "",
                    "models": [{"id": "other"}, {"id": "openai/gpt-6.1-sol"}],
                }
            ],
            "selectedValue": "other",
            "configuredBadges": {"gpt-6.1-sol": _badge(None, "primary", "Primary")},
        },
    )

    assert [(row["id"], row["role"]) for row in data["configured"]] == [
        ("openai/gpt-6.1-sol", "primary")
    ]


@pytest.mark.parametrize(
    "groups,badge_provider",
    [
        (
            [
                {"provider": "Any", "provider_id": "any", "models": [{"id": "other"}, {"id": "any/gpt-6.1-sol"}]},
                {"provider": "Codex", "provider_id": "codex", "models": [{"id": "codex/gpt-6.1-sol"}]},
            ],
            "codex",
        ),
        (
            [
                {
                    "provider": "Magpie",
                    "provider_id": "magpie",
                    "models": [
                        {"id": "other"},
                        {"id": "any/gpt-6.1-sol"},
                        {"id": "auto/gpt-6.1-sol"},
                        {"id": "codex/gpt-6.1-sol"},
                    ],
                }
            ],
            "magpie",
        ),
    ],
    ids=["other-provider", "same-provider"],
)
def test_issue8725_vendor_sibling_stays_unbadged(tmp_path, groups, badge_provider):
    fallback = _badge(badge_provider)
    data = _render(
        tmp_path,
        {
            "groups": groups,
            "selectedValue": "other",
            "configuredBadges": {
                "codex/gpt-6.1-sol": fallback,
                f"@{badge_provider}:codex/gpt-6.1-sol": fallback,
            },
        },
    )

    assert [(row["id"], row["role"]) for row in data["configured"]] == [
        ("codex/gpt-6.1-sol", "fallback")
    ]
    assert _row(data, "any/gpt-6.1-sol")["badge"] == ""
    if badge_provider == "magpie":
        assert _row(data, "auto/gpt-6.1-sol")["badge"] == ""


_PRIMARY_NOUS = _badge("nous", "primary", "Primary")
_PRIMARY_FOO = _badge("custom:foo", "primary", "Primary")
_NOUS_BADGES = {
    "hermes-4-405b": _PRIMARY_NOUS,
    "@nous:hermes-4-405b": _PRIMARY_NOUS,
    "nousresearch/hermes-4-405b": _PRIMARY_NOUS,
}
_FOO_BADGES = {"qwen3": _PRIMARY_FOO, "@custom:foo:qwen3": _PRIMARY_FOO, "Qwen/qwen3": _PRIMARY_FOO}


@pytest.mark.parametrize(
    "payload",
    [
        {
            "groups": [{"provider": "Foo", "provider_id": "custom:foo", "models": [{"id": "other"}, {"id": "Qwen/qwen3"}]}],
            "configuredBadges": _FOO_BADGES,
        },
        {
            "groups": [{"provider": "Foo", "provider_id": "custom:foo", "models": [{"id": "other"}, {"id": "Qwen/qwen3"}]}],
            "configuredBadges": _FOO_BADGES,
            "requested": "qwen3",
            "requestedProvider": "custom:foo",
        },
        {
            "groups": [
                {
                    "provider": "Nous",
                    "provider_id": "nous",
                    "models": [{"id": "other"}, {"id": "nousresearch/hermes-4-405b"}],
                    "extra_models": [{"id": "hermes-4-405b"}],
                }
            ],
            "configuredBadges": _NOUS_BADGES,
        },
        {
            "groups": [
                {"provider": "Nous", "provider_id": "nous", "models": [{"id": "other"}, {"id": "nousresearch/hermes-4-405b"}]}
            ],
            "activeProvider": "nous",
            "configuredBadges": _NOUS_BADGES,
            "live": {"provider": "nous", "models": [{"id": "hermes-4-405b"}]},
        },
    ],
    ids=["synthesized", "custom-option", "overflow", "live"],
)
def test_backend_vendor_linked_spellings_stay_one_configured_row(tmp_path, payload):
    """The backend links a bare configured model to a vendor-qualified catalog
    row (`qwen3` -> `Qwen/qwen3`) and badges both spellings. The identity keeps
    vendor namespaces apart, so Configured must still list the entry once."""
    data = _render(tmp_path, dict(payload, selectedValue="other"))

    assert [row["role"] for row in data["configured"]] == ["primary"]


def _lookup_source() -> str:
    ui = UI_JS_PATH.read_text(encoding="utf-8")
    return "\n".join(
        extract_function(ui, name)
        for name in (
            "_normalizeConfiguredModelKey",
            "_configuredModelIdentity",
            "_getConfiguredModelBadge",
            "_isEquivalentConfiguredModelEntry",
        )
    )


def _run_node(assertions: str) -> None:
    result = subprocess.run(
        [NODE, "-e", "const assert=require('assert');var window={};\n" + _lookup_source() + assertions],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_issue_case_table_through_the_lookup():
    _run_node(
        """
const primaryOpenai={role:'primary',label:'Primary',provider:'openai'};
const fallbackOr={role:'fallback',label:'Fallback 1',provider:'openrouter'};
const legacy={role:'primary',label:'Primary'};
const fallbackCodex={role:'fallback',label:'Fallback 1',provider:'codex'};
assert.strictEqual(_getConfiguredModelBadge('@openai:openai/gpt-6.1-sol',{'gpt-6.1-sol':primaryOpenai},'openai'),primaryOpenai);
assert.strictEqual(
  _getConfiguredModelBadge('openrouter/anthropic/claude-sonnet-4.6',{'anthropic/claude-sonnet-4.6':fallbackOr},'openrouter'),
  fallbackOr
);
assert.strictEqual(_getConfiguredModelBadge('openai/gpt-6.1-sol',{'gpt-6.1-sol':legacy},''),legacy);
assert.strictEqual(_getConfiguredModelBadge('any/gpt-6.1-sol',{'codex/gpt-6.1-sol':fallbackCodex},'any'),null);
// Vendor namespaces under the same provider stay distinct identities.
assert.strictEqual(_configuredModelIdentity('@openrouter:openrouter/anthropic/x','openrouter'),'anthropic/x');
assert.notStrictEqual(_configuredModelIdentity('any/x','magpie'),_configuredModelIdentity('codex/x','magpie'));
"""
    )


@pytest.mark.parametrize(
    "extra, more",
    [
        # Sent as `custom:foo-bar/acme/example-model`: a different model id from
        # the Configured row, so "Show all" must still reach it (as on master).
        (_FOO_BAR_ROW, ["groups"]),
        # The configured id itself: Configured already shows it.
        ("acme/example-model", []),
    ],
    ids=["provider-prefixed-twin", "configured-id"],
)
def test_show_all_counts_only_hidden_rows_configured_does_not_send(tmp_path, extra, more):
    """Gate r1103e: a hidden overflow row stays out of the "Show all" count only
    when a Configured row sends its model id."""
    data = _render(
        tmp_path,
        {
            "groups": [
                {
                    "provider": "Foo Bar",
                    "provider_id": "custom:foo-bar",
                    "models": [{"id": "acme/other-model"}],
                    "extra_models": [{"id": extra}],
                }
            ],
            "selectedValue": "acme/other-model",
            "configuredBadges": {"acme/example-model": _badge("custom:foo-bar")},
        },
    )

    assert len(data["configured"]) == 1
    assert data["more"] == more


def test_show_all_still_counts_another_providers_hidden_twin(tmp_path):
    """A hidden overflow row that only shares its bare id with a Configured row
    of another provider sends that id to a different provider, so it is not the
    configured route and "Show all" must still count it (as on master)."""
    data = _render(
        tmp_path,
        {
            "groups": [
                {"provider": "OpenAI", "provider_id": "openai", "models": [{"id": "gpt-other"}, {"id": "gpt-5.5"}]},
                {
                    "provider": "Codex",
                    "provider_id": "openai-codex",
                    "models": [{"id": "gpt-c-other"}],
                    "extra_models": [{"id": "gpt-5.5"}],
                },
            ],
            "selectedValue": "gpt-other",
            "configuredBadges": {
                "gpt-5.5": _badge("openai", "primary", "Primary"),
                "@openai:gpt-5.5": _badge("openai", "primary", "Primary"),
            },
        },
    )

    assert [(row["id"], row["role"]) for row in data["configured"]] == [("gpt-5.5", "primary")]
    assert data["more"] == ["groups"]


@pytest.mark.parametrize("clicks", [1, 2], ids=["first-click", "second-click"])
@pytest.mark.parametrize(
    "payload, configured_id",
    [
        (
            {
                "groups": [
                    {
                        "provider": "Anthropic",
                        "provider_id": "anthropic",
                        "models": [{"id": "acme/other-model"}, {"id": "anthropic/acme/example-model"}],
                    }
                ],
                "selectedValue": "acme/other-model",
                "configuredBadges": {"acme/example-model": _badge("anthropic")},
            },
            "acme/example-model",
        ),
        (
            {
                "groups": [{"provider": "OpenRouter", "provider_id": "openrouter", "models": [{"id": "x/other"}]}],
                "selectedValue": "x/other",
                "configuredBadges": {"anthropic/claude-sonnet-4.6": _badge("openrouter")},
                "requested": "openrouter/anthropic/claude-sonnet-4.6",
            },
            "anthropic/claude-sonnet-4.6",
        ),
    ],
    ids=["catalog-alias", "typed-alias"],
)
def test_the_clicked_configured_row_stays_the_selected_configured_row(tmp_path, payload, configured_id, clicks):
    """Gate r1103e: clicking the native Configured row selects the configured id;
    on re-render the Configured row must be that selected row, not the
    `<provider>/<model>` alias, so a second click sends the same id."""
    data = _render(tmp_path, dict(payload, click=0, clicks=clicks))

    assert [row["id"] for row in data["configured"]] == [configured_id]
    after = data["afterClick"]
    assert len(after["configured"]) == 1
    assert after["configured"][0]["active"] is True
    assert after["selected"] == after["configured"][0]["id"]
    assert after["selected"].endswith(":" + configured_id)


@pytest.mark.parametrize("clicks", [0, 1, 2], ids=["render", "first-click", "second-click"])
def test_a_providerless_entry_is_one_configured_row(tmp_path, clicks):
    """A providerless badge shows on a provider-wrapped row too (the identity
    peels that row's `@p:p/` wrappers), and the native id is still synthesized.
    Both rows show the same configured entry, so Configured has one row for it,
    and clicking it keeps that row selected (master shows one row here)."""
    payload = {
        "groups": [{"provider": "OpenAI", "provider_id": "openai", "models": [{"id": "x/other-model"}]}],
        "selectedValue": "x/other-model",
        "configuredBadges": {"acme/example-model": _badge(None)},
        "requested": "openai/acme/example-model",
        "requestedProvider": "openai",
    }
    if clicks:
        payload.update(click=0, clicks=clicks)
    data = _render(tmp_path, payload)

    assert len(data["configured"]) == 1
    if clicks:
        after = data["afterClick"]
        assert len(after["configured"]) == 1
        assert after["configured"][0]["active"] is True
        assert after["selected"] == after["configured"][0]["id"]

