from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from tests.js_source_extract import extract_function

REPO_ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
LOAD_PROVIDERS_PANEL_JS = extract_function(PANELS_JS, "loadProvidersPanel", prefix="async function")
NODE = shutil.which("node")


def test_provider_cost_chart_ui_guards_are_present():
    panels_js = PANELS_JS
    style_css = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")

    # function is defined
    assert "async function renderProviderCostChart(card)" in panels_js

    # function is wired up inside loadProvidersPanel (fire-and-forget)
    assert "renderProviderCostChart(quotaCard)" in panels_js

    # fetch target is correct
    assert "/api/provider/cost-history?provider=openrouter" in panels_js

    # CSS container class present in both JS and CSS
    assert "provider-cost-chart-wrap" in panels_js
    assert "provider-cost-chart-wrap" in style_css

    # monthly pace projection annotation
    assert "Monthly pace" in panels_js

    # null delta guard for the oldest snapshot
    assert "s.delta!=null" in panels_js


def _run_load_panel_harness(scenario):
    if NODE is None:  # pragma: no cover
        import pytest

        pytest.skip("node is unavailable in this test environment")

    driver = r"""
const { performance } = require('perf_hooks');

const scenario = JSON.parse(process.argv[1] || '{}');
const loadProvidersPanelSource = process.argv[2] || '';
if (!loadProvidersPanelSource) {
  process.exit(1);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function makeElement(className = '') {
  const element = {
    className,
    children: [],
    style: {},
    dataset: {},
    _parent: null,
    _innerHTML: '',
    classList: {
      contains(name) {
        return element.className.split(' ').includes(name);
      },
    },
    prepend(child) {
      child._parent = element;
      element.children.unshift(child);
    },
    appendChild(child) {
      child._parent = element;
      element.children.push(child);
    },
    replaceWith(newNode) {
      if (!this._parent) return;
      const idx = this._parent.children.indexOf(this);
      if (idx < 0) return;
      this._parent.children[idx] = newNode;
      newNode._parent = this._parent;
      this._parent = null;
    },
    querySelector(selector) {
      if (selector === '.provider-quota-card') {
        return element.children.find((child) => child.className.includes('provider-quota-card')) || null;
      }
      return null;
    },
  };

  Object.defineProperty(element, 'innerHTML', {
    get() {
      return element._innerHTML;
    },
    set(value) {
      element._innerHTML = value;
      if (value === '') {
        element.children = [];
      }
    },
  });

  return element;
}

let providersPaintedAt = null;
let quotaPaintedAt = null;
const events = [];
const t0 = performance.now();
const toMs = () => Number((performance.now() - t0).toFixed(3));
let providersCalls = 0;
let quotaCalls = 0;

const providers = scenario.providersList !== undefined ? scenario.providersList : [
  {
    id: 'provider-a',
    display_name: 'Provider A',
    has_key: false,
    configurable: true,
    is_oauth: false,
    is_custom: false,
    is_plugin_provider: false,
    is_self_hosted: false,
    key_source: 'none',
    models: [],
    models_total: 0,
  },
];

const list = makeElement();
const empty = makeElement();
globalThis.$ = (id) => ({ providersList: list, providersEmpty: empty }[id] || null);
globalThis.esc = (value) => String(value || '');
globalThis.t = (value) => String(value || '');
globalThis._providerCardEls = new Map();
globalThis._providersPanelLoadGeneration = 0;
globalThis._buildProviderCard = () => {
  if (providersPaintedAt === null) providersPaintedAt = toMs();
  return makeElement('provider-card');
};
globalThis._buildProviderQuotaCard = (status) => {
  if (quotaPaintedAt === null) quotaPaintedAt = toMs();
  const node = makeElement('provider-quota-card');
  const marker = status ? (status.quotaMarker || status.status || status.message || '') : '';
  node.textContent = String(marker || '');
  return node;
};
globalThis.renderProviderCostChart = () => {};
globalThis.api = async (url) => {
  if (url !== '/api/providers') return { providers: [] };
  providersCalls += 1;
  const delay = (scenario.providersDelays && scenario.providersDelays[providersCalls]) || scenario.providersDelay || 0;
  await sleep(delay);
  events.push({ type: 'providers-resolve', call: providersCalls, at: toMs() });
  return { providers };
};
globalThis._fetchProviderQuotaStatus = async () => {
  quotaCalls += 1;
  const delay = (scenario.quotaDelays && scenario.quotaDelays[quotaCalls]) || scenario.quotaDelay || 0;
  await sleep(delay);
  if (scenario.quotaRejects && scenario.quotaRejects[quotaCalls]) {
    const message = scenario.quotaRejectMessages && scenario.quotaRejectMessages[quotaCalls]
      ? scenario.quotaRejectMessages[quotaCalls]
      : `quota-reject-${quotaCalls}`;
    events.push({ type: 'quota-reject', call: quotaCalls, marker: message, at: toMs() });
    throw new Error(message);
  }
  const marker = scenario.quotaMarkers && scenario.quotaMarkers[quotaCalls]
    ? scenario.quotaMarkers[quotaCalls]
    : `call-${quotaCalls}`;
  events.push({ type: 'quota-resolve', call: quotaCalls, marker, at: toMs() });
  return { ok: true, status: 'available', quotaMarker: marker };
};

eval(loadProvidersPanelSource);

(async () => {
  if (scenario.doubleLoad) {
    const first = loadProvidersPanel();
    if (scenario.secondLoadOffsetMs) {
      await sleep(scenario.secondLoadOffsetMs);
    }
    await Promise.all([first, loadProvidersPanel()]);
  } else {
    await loadProvidersPanel();
  }
  await sleep(scenario.postDelayMs || 0);
  const quotaCards = list.children.filter((child) => child.className.includes('provider-quota-card'));
  const providerCards = list.children.filter((child) => child.className === 'provider-card');
  console.log(JSON.stringify({
    providersCalls,
    quotaCalls,
    providersPaintedAt,
    quotaPaintedAt,
    providerCards: providerCards.length,
    quotaCards: quotaCards.length,
    quotaMarker: quotaCards.length ? quotaCards[0].textContent : null,
    events,
    listLength: list.children.length,
    listDisplay: list.style.display || '',
    emptyDisplay: empty.style.display || '',
  }));
})();
"""

    proc = subprocess.run(
        [NODE, "-e", driver, json.dumps(scenario), LOAD_PROVIDERS_PANEL_JS],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node harness failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return json.loads(proc.stdout.strip())


def test_load_providers_panel_renders_providers_without_waiting_for_quota():
    """Provider cards should paint before quota status, since quota is now concurrent."""
    result = _run_load_panel_harness({
        "providersDelay": 5,
        "quotaDelay": 50,
        "postDelayMs": 75,
    })
    assert result["providerCards"] > 0
    assert result["providersPaintedAt"] is not None
    assert result["quotaPaintedAt"] is not None
    assert result["providersPaintedAt"] <= result["quotaPaintedAt"]


def test_load_providers_panel_avoids_stale_quota_updates_with_generation_guard():
    """A late quota response from an older load must not duplicate/update a stale card."""
    result = _run_load_panel_harness({
        "providersDelay": 5,
        "quotaDelays": {1: 150, 2: 25},
        "quotaMarkers": {1: "first-load", 2: "second-load"},
        "doubleLoad": True,
        "secondLoadOffsetMs": 5,
        "postDelayMs": 250,
    })
    assert result["quotaCards"] == 1
    assert result["quotaMarker"] == "second-load"
    assert result["providersCalls"] >= 2
    assert result["quotaCalls"] >= 2


def test_load_providers_panel_starts_providers_and_quota_requests_in_parallel():
    """Source-level contract: quota + providers requests are started before the await."""
    panels = PANELS_JS
    assert "providersPromise = api('/api/providers')" in LOAD_PROVIDERS_PANEL_JS
    assert "quotaPromise = _fetchProviderQuotaStatus(false)" in LOAD_PROVIDERS_PANEL_JS
    assert ".then((quota)=>" not in LOAD_PROVIDERS_PANEL_JS
    assert LOAD_PROVIDERS_PANEL_JS.count("}catch(e){") == 1
    assert "await quotaPromise" in LOAD_PROVIDERS_PANEL_JS
    assert "_providersPanelLoadGeneration" in panels
    assert "generation!==_providersPanelLoadGeneration" in LOAD_PROVIDERS_PANEL_JS


def test_load_providers_panel_shows_unavailable_quota_card_when_quota_rejects():
    """Quota fetch rejection should still render provider cards and an unavailable quota card."""
    result = _run_load_panel_harness({
        "providersDelay": 5,
        "quotaDelay": 2,
        "quotaRejects": {1: True},
        "postDelayMs": 75,
    })
    assert result["providerCards"] > 0
    assert result["quotaCards"] == 1
    assert result["quotaMarker"] == "unavailable"
    assert any(event["type"] == "quota-reject" for event in result["events"])


def test_load_providers_panel_builds_provider_cards_before_quota_dom_insertion_even_when_quota_resolves_first():
    """Quota DOM insertion always follows provider-card insertion in code order, even when the quota fetch itself settles first."""
    result = _run_load_panel_harness({
        "providersDelay": 40,
        "quotaDelay": 2,
        "postDelayMs": 75,
    })
    assert result["providerCards"] > 0
    assert result["quotaCards"] == 1
    assert result["providersPaintedAt"] is not None
    assert result["quotaPaintedAt"] is not None
    # Quota's own fetch resolved first, but the quota card is still only ever
    # built after the provider cards, because that is where the code puts it.
    assert result["providersPaintedAt"] <= result["quotaPaintedAt"]


def test_load_providers_panel_awaits_quota_before_returning_on_zero_providers():
    """Awaiting loadProvidersPanel() must include the quota fetch even when the provider list is empty."""
    result = _run_load_panel_harness({
        "providersList": [],
        "providersDelay": 5,
        "quotaDelay": 40,
        "postDelayMs": 0,
    })
    assert result["providerCards"] == 0
    assert result["quotaCards"] == 0
    # By the time `await loadProvidersPanel()` returns (postDelayMs=0), the
    # slower quota fetch must already have settled.
    assert result["quotaCalls"] == 1
    quota_events = [e for e in result["events"] if e["type"] == "quota-resolve"]
    assert len(quota_events) == 1
    # Empty-state visibility must be unchanged by the added await.
    assert result["listDisplay"] == "none"
    assert result["emptyDisplay"] == ""
