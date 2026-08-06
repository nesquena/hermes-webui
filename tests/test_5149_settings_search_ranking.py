"""Browserless behavioral coverage for settings search ranking."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PANELS_JS = ROOT / "static" / "panels.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="node is required to execute browserless settings-search ranking harness",
)


_DRIVER = r"""
const fs = require('fs');

const panelsPath = process.argv[2];
const scenario = JSON.parse(process.argv[3] || '{}');
const src = fs.readFileSync(panelsPath, 'utf8');

function extractSearchFunctions(source) {
  const start = source.indexOf('function _normalizeSettingsSearchText(value)');
  if (start < 0) throw new Error('missing settings search helpers');
  const endMarker = 'function _navigateToSettingsField(entry)';
  const end = source.indexOf(endMarker, start);
  if (end < 0) throw new Error('missing end marker before _navigateToSettingsField');
  return source.slice(start, end);
}

function normalizeText(value) {
  return String(value || '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
}

function parseDataAttributeName(name) {
  return String(name || '')
    .split('-')
    .map((part, index) => index === 0 ? part : part.charAt(0).toUpperCase() + part.slice(1))
    .join('');
}

function createClassList(owner) {
  const tokens = new Set();
  const update = (value) => {
    owner._className = Array.from(tokens).join(' ');
  };
  return {
    contains(name) {
      return tokens.has(name);
    },
    add(name) {
      if (!name) return;
      const token = String(name).trim();
      if (!token) return;
      tokens.add(token);
      update();
    },
    remove(name) {
      tokens.delete(String(name));
      update();
    },
    toggle(name, force) {
      const token = String(name);
      const enabled = force !== undefined ? force : !tokens.has(token);
      if (enabled) tokens.add(token);
      else tokens.delete(token);
      update();
      return enabled;
    },
    _set(value) {
      tokens.clear();
      for (const token of String(value || '').split(/\s+/)) {
        if (token) tokens.add(token);
      }
      update();
    },
  };
}

class FakeElement {
  constructor(tagName = 'div') {
    this.tagName = String(tagName || '').toLowerCase();
    this.children = [];
    this.parentElement = null;
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.classList = createClassList(this);
    this.classList._set('');
    this._text = '';
    this._innerHTML = '';
    this._className = '';
    this.value = '';
  }

  set id(value) {
    this._id = String(value || '');
  }

  get id() {
    return this._id || '';
  }

  set className(value) {
    this._className = String(value || '');
    this.classList._set(this._className);
  }

  get className() {
    return this._className;
  }

  get textContent() {
    const childText = this.children.map((child) => child.textContent).filter(Boolean).join(' ');
    return [this._text, childText].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
  }

  set textContent(value) {
    this._text = String(value || '');
  }

  set innerHTML(value) {
    this._innerHTML = String(value || '');
  }

  get innerHTML() {
    return this._innerHTML;
  }

  setAttribute(name, value) {
    const key = String(name || '');
    const v = String(value || '');
    this.attributes[key] = v;
    if (key.startsWith('data-')) {
      this.dataset[parseDataAttributeName(key.slice(5))] = v;
    }
    if (key === 'class') this.className = v;
    if (key === 'id') this.id = v;
  }

  getAttribute(name) {
    if (name === 'class') return this.className;
    if (name === 'id') return this.id;
    return this.attributes[String(name)] || null;
  }

  appendChild(child) {
    if (!child) return child;
    this.children.push(child);
    child.parentElement = this;
    child.parentNode = this;
    return child;
  }

  contains(node) {
    let current = node;
    while (current) {
      if (current === this) return true;
      current = current.parentElement;
    }
    return false;
  }

  closest(selector) {
    let current = this;
    while (current) {
      if (matchesSelector(current, selector)) return current;
      current = current.parentElement;
    }
    return null;
  }

  querySelectorAll(selector) {
    return querySelectorAll(this, selector);
  }

  querySelector(selector) {
    const found = this.querySelectorAll(selector);
    return found[0] || null;
  }

  addEventListener() {}
}

function allNodes(root) {
  const nodes = [root];
  for (const child of root.children || []) {
    nodes.push(...allNodes(child));
  }
  return nodes;
}

function isNodeMatch(node, selector) {
  if (!node || !selector) return false;
  const tagOnly = selector.match(/^([a-z0-9-]+)$/i);
  if (selector === '*') return true;
  if (selector === '[data-i18n]') {
    return !!(node.dataset && node.dataset.i18n);
  }
  if (selector.startsWith('[') && selector.endsWith(']')) {
    const body = selector.slice(1, -1);
    const [key, expected] = body.split('=').map((it) => it.trim());
    const actual = key.startsWith('data-')
      ? (node.dataset ? node.dataset[parseDataAttributeName(key.slice(5))] : null)
      : (node.getAttribute ? node.getAttribute(key) : null);
    if (expected === undefined) return !!actual;
    const expectedValue = expected.replace(/^["']|["']$/g, '');
    return actual === expectedValue;
  }
  if (selector.startsWith('.')) {
    return node.classList.contains(selector.slice(1));
  }
  const withAttr = selector.match(/^([a-z0-9-]+)\[([^\]]+)\]$/i);
  if (withAttr) {
    const tag = withAttr[1].toLowerCase();
    const attr = withAttr[2];
    if (node.tagName !== tag) return false;
    if (attr === 'data-i18n') return !!(node.dataset && node.dataset.i18n);
    return !!node.getAttribute(attr);
  }
  return node.tagName === selector.toLowerCase();
}

function matchesSelector(node, selector) {
  const value = selector.trim();
  if (!value.includes(' ')) return isNodeMatch(node, value);
  const [ancestorSelector, targetSelector] = value.split(/\s+/);
  if (!ancestorSelector || !targetSelector) return false;
  const ancestors = allNodes(node);
  for (const candidate of ancestors) {
    if (!isNodeMatch(candidate, ancestorSelector)) continue;
    for (const child of allNodes(candidate).slice(1)) {
      if (isNodeMatch(child, targetSelector)) return true;
    }
  }
  return false;
}

function querySelectorAll(node, selector) {
  const parts = selector.split(',').map((part) => part.trim()).filter(Boolean);
  const found = [];
  const seen = new Set();
  for (const part of parts) {
    if (part.includes(' ')) {
      const [ancestorSelector, targetSelector] = part.split(/\s+/);
      for (const candidate of allNodes(node)) {
        if (!isNodeMatch(candidate, ancestorSelector)) continue;
        for (const child of allNodes(candidate).slice(1)) {
          if (!isNodeMatch(child, targetSelector)) continue;
          if (!seen.has(child)) {
            seen.add(child);
            found.push(child);
          }
        }
      }
      continue;
    }
    for (const candidate of allNodes(node)) {
      if (!isNodeMatch(candidate, part)) continue;
      if (!seen.has(candidate)) {
        seen.add(candidate);
        found.push(candidate);
      }
    }
  }
  return found;
}

const registry = new Map();

function register(node) {
  if (node && node.id) registry.set(node.id, node);
  return node;
}

function $(id) {
  return registry.get(id) || null;
}

function createElement(tagName) {
  return new FakeElement(tagName);
}

function makePane(id) {
  return register(Object.assign(createElement('div'), { id }));
}

function makeLabel(text) {
  const label = createElement('label');
  label.textContent = text;
  return label;
}

function makeSettingsField({
  labelText,
  descriptionText = '',
  descriptionI18nKey = '',
  settingsSearch = '',
  options = [],
}) {
  const field = createElement('div');
  field.className = 'settings-field';
  if (settingsSearch) field.dataset.settingsSearch = settingsSearch;
  const label = makeLabel(labelText);
  field.appendChild(label);
  if (options.length) {
    const select = createElement('select');
    for (const optionText of options) {
      const option = createElement('option');
      option.textContent = optionText;
      select.appendChild(option);
    }
    field.appendChild(select);
  }
  if (descriptionText || descriptionI18nKey) {
    const description = createElement('div');
    if (descriptionI18nKey) description.dataset.i18n = descriptionI18nKey;
    description.textContent = descriptionText || '';
    field.appendChild(description);
  }
  return field;
}

function makeProviderCard(name) {
  const card = createElement('div');
  card.className = 'provider-card';
  const cardName = createElement('div');
  cardName.className = 'provider-card-name';
  cardName.textContent = name;
  card.appendChild(cardName);
  return card;
}

function makeProviderField(cardName, fieldLabel, valueText) {
  const field = createElement('div');
  field.className = 'provider-card-field';
  const label = createElement('div');
  label.className = 'provider-card-label';
  label.textContent = fieldLabel;
  field.appendChild(label);
  if (valueText) {
    const input = createElement('input');
    input.value = valueText;
    field.appendChild(input);
  }
  return field;
}

function makePluginCard(name) {
  const card = createElement('div');
  card.className = 'provider-card plugin-card';
  const cardName = createElement('div');
  cardName.className = 'provider-card-name';
  cardName.textContent = name;
  card.appendChild(cardName);
  return card;
}

function setupDom(mode) {
  const conversation = makePane('settingsPaneConversation');
  const providers = makePane('settingsPaneProviders');
  const plugins = makePane('settingsPanePlugins');
  register(createElement('div')).id = 'settingsPaneAppearance';
  register(createElement('div')).id = 'settingsPanePreferences';
  register(createElement('div')).id = 'settingsPaneExtensions';
  register(createElement('div')).id = 'settingsPaneSystem';
  register(createElement('div')).id = 'settingsPaneHelp';
  const settingsSearch = createElement('input');
  settingsSearch.id = 'settingsSearch';
  register(settingsSearch);
  const results = createElement('div');
  results.id = 'settingsSearchResults';
  register(results);

  if (mode === 'title-vs-description') {
    conversation.appendChild(makeSettingsField({
      labelText: 'Blob field',
      descriptionText: 'priority appears only in this plain-text fallback',
    }));
    conversation.appendChild(makeSettingsField({
      labelText: 'Descriptor-only field',
      descriptionI18nKey: 'settings_desc_priority_only',
    }));
    conversation.appendChild(makeSettingsField({
      labelText: 'Priority title field',
      descriptionI18nKey: 'settings_desc_irrelevant',
    }));
  } else if (mode === 'value-vs-description') {
    conversation.appendChild(makeSettingsField({
      labelText: 'Blob value field',
      descriptionText: 'rank only lives in this plain-text fallback text',
    }));
    conversation.appendChild(makeSettingsField({
      labelText: 'Value-later field',
      descriptionI18nKey: 'settings_desc_rank_only',
    }));
    conversation.appendChild(makeSettingsField({
      labelText: 'Value field',
      options: ['rank-value option'],
    }));
  } else if (mode === 'same-tier-order') {
    conversation.appendChild(makeSettingsField({
      labelText: 'Description-later-contains',
      descriptionI18nKey: 'settings_desc_needle_contains_late',
    }));
    conversation.appendChild(makeSettingsField({
      labelText: 'Description-prefix',
      descriptionI18nKey: 'settings_desc_needle_prefix',
    }));
    conversation.appendChild(makeSettingsField({
      labelText: 'Description-earlier-contains',
      descriptionI18nKey: 'settings_desc_needle_contains_early',
    }));
  } else if (mode === 'label-rendering') {
    conversation.appendChild(makeSettingsField({
      labelText: 'Visible Label',
      options: ['token-label-option'],
      descriptionText: 'description with token-label-option',
    }));
  } else if (mode === 'supplemental-term') {
    conversation.appendChild(makeSettingsField({
      labelText: 'Supplemental alias field',
      descriptionText: 'description without the alias query',
      settingsSearch: 'steer alias query',
    }));
  } else if (mode === 'twelve-result-cap') {
    for (let i = 0; i < 13; i++) {
      conversation.appendChild(makeSettingsField({
        labelText: `Entry ${String(i + 1).padStart(2, '0')}`,
        descriptionText: `shared token for cap ${i}`,
      }));
    }
  } else if (mode === 'provider-plugin') {
    conversation.appendChild(makeSettingsField({
      labelText: 'Conversation field',
      descriptionText: 'provider plugin terms plugin',
    }));
    const providerCard = makeProviderCard('Provider Alpha');
    providerCard.appendChild(makeProviderField('provider', 'API Key', 'sk-test'));
    providers.appendChild(providerCard);
    plugins.appendChild(makePluginCard('Plugin Sample'));
  }

  return {
    conversation,
    providers,
    plugins,
  };
}

function runScenario(command) {
  setupDom(command);
  return new Promise(async (resolve, reject) => {
    try {
      const block = extractSearchFunctions(src);
      eval(block);
      globalThis.$ = $;
      globalThis.document = {
        createElement: createElement,
      };
      globalThis.t = (key) => {
        const translations = {
          settings_tab_conversation: 'Conversation',
          settings_tab_appearance: 'Appearance',
          settings_tab_preferences: 'Preferences',
          providers_tab_title: 'Providers',
          settings_tab_plugins: 'Plugins',
          settings_tab_extensions: 'Extensions',
          settings_tab_system: 'System',
          settings_tab_help: 'Help',
          settings_desc_priority_only: 'this contains priority in the descriptor',
          settings_desc_irrelevant: 'nothing about query there',
          settings_desc_rank_only: 'contains rank token in descriptor only',
          settings_desc_needle_prefix: 'needle appears at the start of this description',
          settings_desc_needle_contains_early: 'this field has needle near the front',
          settings_desc_needle_contains_late: 'this field waits a while before it reaches needle',
        };
        return translations[key] || String(key || '');
      };
      globalThis.esc = (value) => String(value || '');
      globalThis.loadProvidersPanel = async () => undefined;
      globalThis.loadPluginsPanel = async () => undefined;
      globalThis.loadExtensionsPanel = async () => undefined;
      globalThis._settingsIndex = null;
      globalThis._settingsIndexPromise = null;
      globalThis._settingsSearchSeq = 0;

      let query = '';
      if (command === 'title-vs-description') query = 'priority';
      else if (command === 'value-vs-description') query = 'rank';
      else if (command === 'same-tier-order') query = 'needle';
      else if (command === 'label-rendering') query = 'token-label-option';
      else if (command === 'supplemental-term') query = 'steer alias';
      else if (command === 'twelve-result-cap') query = 'shared';
      else if (command === 'provider-plugin') query = scenario.query || 'plugin';
      await filterSettings(query);

      const results = $('settingsSearchResults');
      const labels = [];
      const html = results.innerHTML || '';
      for (const child of results.children || []) {
        if (!child || typeof child.innerHTML !== 'string') continue;
        const re = /<span class=\"settings-search-label\">([^<]*)<\/span>/g;
        let match;
        while ((match = re.exec(child.innerHTML)) !== null) labels.push(match[1]);
      }
      resolve({
        query,
        labels,
        html,
        resultCount: (results.children || []).length,
        noResults: html.includes('settings-search-empty'),
      });
    } catch (err) {
      reject(err);
    }
  });
}

(async () => {
  const payload = await runScenario(scenario.command);
  process.stdout.write(JSON.stringify(payload));
})();
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    return str(tmp_path_factory.mktemp("settings-search-ranking-driver") / "driver.js")


@pytest.fixture
def driver_file(driver_path):
    Path(driver_path).write_text(_DRIVER, encoding="utf-8")
    return driver_path


def _run_driver(driver_file, command):
    process = subprocess.run(
        [
            NODE,
            driver_file,
            str(PANELS_JS),
            json.dumps(command if isinstance(command, dict) else {"command": command}),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip())
    return json.loads(process.stdout)


def test_title_matches_outrank_descriptor_only(driver_file):
    payload = _run_driver(driver_file, "title-vs-description")
    assert payload["labels"][0] == "Priority title field"
    assert payload["labels"][1] == "Descriptor-only field"
    assert payload["labels"][2] == "Blob field"
    assert not payload["noResults"]


def test_value_or_option_match_outranks_descriptor_match(driver_file):
    payload = _run_driver(driver_file, "value-vs-description")
    assert payload["labels"][0] == "Value field"
    assert payload["labels"][1] == "Value-later field"
    assert payload["labels"][2] == "Blob value field"
    assert not payload["noResults"]


def test_same_tier_order_uses_prefix_and_earlier_index(driver_file):
    payload = _run_driver(driver_file, "same-tier-order")
    assert payload["labels"][0] == "Description-prefix"
    assert payload["labels"][1] == "Description-earlier-contains"
    assert payload["labels"][2] == "Description-later-contains"


def test_rendered_label_is_visible_field_title(driver_file):
    payload = _run_driver(driver_file, "label-rendering")
    assert payload["labels"][0] == "Visible Label"


def test_supplemental_search_terms_remain_behaviorally_searchable(driver_file):
    payload = _run_driver(driver_file, "supplemental-term")
    assert payload["labels"] == ["Supplemental alias field"]


def test_results_cap_still_applies_to_ranked_matches(driver_file):
    payload = _run_driver(driver_file, "twelve-result-cap")
    assert payload["resultCount"] == 12
    assert not payload["noResults"]


def test_provider_and_plugin_cards_remain_searchable(driver_file):
    provider_payload = _run_driver(
        driver_file,
        {"command": "provider-plugin", "query": "alpha"},
    )
    field_payload = _run_driver(
        driver_file,
        {"command": "provider-plugin", "query": "key"},
    )
    plugin_payload = _run_driver(
        driver_file,
        {"command": "provider-plugin", "query": "plugin"},
    )
    assert "Provider Alpha" in provider_payload["labels"]
    assert "Provider Alpha API Key" in field_payload["labels"]
    assert "Plugin Sample" in plugin_payload["labels"]
