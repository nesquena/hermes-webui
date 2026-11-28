"""Behavioral regression: a models refresh must not wipe an unsaved provider draft.

Review blocker on #8418: expanding a configurable provider in Settings, typing a
draft API key, then clicking **Refresh Models** used to rebuild the whole card
(``replaceWith``) — the draft vanished and the Save button reset. Frozen master
kept both because it never re-rendered the card at all.

This runs the PRODUCTION ``_refreshProviderModels`` / ``_updateProviderCardModels``
bodies from ``static/panels.js`` in node against a small DOM stub, then asserts
the editable controls survive by identity while the model metadata updates.
"""

import json
import shutil
import subprocess

import pytest

from tests.js_source_extract import extract_function

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None, reason="node is required to execute the provider-card refresh harness"
)


_DRIVER = r"""
const fs = require('fs');
const fnSource = fs.readFileSync(process.argv[2], 'utf8');

class FakeNode {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.className = '';
    this.id = '';
    this.value = '';
    this.disabled = false;
    this.parentNode = null;
    this._text = '';
  }
  get classList() {
    const self = this;
    return { contains: (c) => String(self.className || '').split(/\s+/).includes(c) };
  }
  get textContent() { return this._text; }
  set textContent(v) {
    this._text = String(v);
    if (this._text === '') this.children = [];
  }
  appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
  insertBefore(node, ref) {
    const i = this.children.indexOf(ref);
    if (i < 0) this.children.push(node); else this.children.splice(i, 0, node);
    node.parentNode = this;
    return node;
  }
  remove() {
    if (!this.parentNode) return;
    const i = this.parentNode.children.indexOf(this);
    if (i >= 0) this.parentNode.children.splice(i, 1);
  }
  get lastElementChild() {
    return this.children.length ? this.children[this.children.length - 1] : null;
  }
  _walk(out) {
    for (const c of this.children) { out.push(c); c._walk(out); }
    return out;
  }
  _matches(sel) {
    if (typeof sel !== 'string') return false;
    if (sel.startsWith('.')) return this.classList.contains(sel.slice(1));
    if (sel.startsWith('#')) return this.id === sel.slice(1);
    return this.tagName === sel.toUpperCase();
  }
  querySelectorAll(sel) { return this._walk([]).filter((n) => n._matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

function node(tag, className) {
  const n = new FakeNode(tag);
  if (className) n.className = className;
  return n;
}

// --- the card as _buildProviderCard renders it -----------------------------
const providersList = node('div');
providersList.id = 'providersList';

const card = node('div', 'provider-card');
card.dataset.provider = 'custom:test-gateway';

const meta = node('div', 'provider-card-meta');
meta.textContent = '2 models \u00b7 configured';

const body = node('div', 'provider-card-body');

const keyRow = node('div', 'provider-card-row');
const draftInput = node('input', 'provider-card-input');
draftInput.id = 'apiKeyInput';
draftInput.value = 'unsaved-draft-value';
const saveBtn = node('button', 'provider-card-save');
keyRow.appendChild(draftInput);
keyRow.appendChild(saveBtn);

const modelsSection = node('div', 'provider-card-models');
const modelsLabel = node('div', 'provider-card-label');
modelsLabel.textContent = 'Models';
const tags = node('div', 'provider-card-model-tags');
for (const id of ['model-a', 'model-b']) {
  const tag = node('span', 'provider-card-model-tag');
  tag.textContent = id;
  tags.appendChild(tag);
}
modelsSection.appendChild(modelsLabel);
modelsSection.appendChild(tags);

const refreshRow = node('div', 'provider-card-row');
const refreshBtn = node('button', 'provider-card-refresh');
refreshBtn.innerHTML = 'Refresh models';
refreshRow.appendChild(refreshBtn);

body.appendChild(keyRow);
body.appendChild(modelsSection);
body.appendChild(refreshRow);
card.appendChild(meta);
card.appendChild(body);
providersList.appendChild(card);

// --- production seams ------------------------------------------------------
const $ = (id) => (id === 'providersList' ? providersList : null);
const t = () => undefined;  // fall through to the built-in English strings
const showToast = () => {};
const _refreshModelDropdownsAfterProviderChange = () => {};
const api = async (path) => {
  if (path === '/api/models/refresh') return { ok: true, provider: 'custom:test-gateway' };
  return {
    providers: [{
      id: 'custom:test-gateway',
      models_total: 4,
      models: [{ id: 'model-c' }, { id: 'model-d' }],
    }],
  };
};
const document = { createElement: (tag) => new FakeNode(tag) };

eval(fnSource);

(async () => {
  const result = {};
  try {
    await _refreshProviderModels('custom:test-gateway', refreshBtn);
    result.ok = true;
  } catch (e) {
    result.ok = false;
    result.error = String((e && e.stack) || e);
  }
  result.draft = draftInput.value;
  result.sameInput = providersList.querySelector('.provider-card-input') === draftInput;
  result.saveDisabled = saveBtn.disabled;
  result.cardSame = providersList.querySelector('.provider-card') === card;
  result.meta = meta.textContent;
  result.tags = tags.children.map((c) => c.textContent);
  console.log(JSON.stringify(result));
})();
"""


def _run_refresh(tmp_path):
    js = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    source = "\n".join(
        [
            extract_function(js, "_refreshProviderModels", prefix="async function"),
            extract_function(js, "_updateProviderCardModels"),
        ]
    )
    fn_path = tmp_path / "provider_card_refresh_fns.js"
    fn_path.write_text(source, encoding="utf-8")
    drv_path = tmp_path / "driver.js"
    drv_path.write_text(_DRIVER, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(drv_path), str(fn_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_refresh_keeps_unsaved_draft_and_updates_metadata(tmp_path):
    result = _run_refresh(tmp_path)
    assert result["ok"] is True, result.get("error")
    # The unsaved draft and the editable control itself must survive.
    assert result["draft"] == "unsaved-draft-value"
    assert result["sameInput"] is True
    assert result["saveDisabled"] is False
    # The card element is updated in place, never rebuilt.
    assert result["cardSame"] is True
    # ...and the metadata still reflects the refreshed catalog.
    assert result["meta"].startswith("4 models"), result["meta"]
    assert result["tags"] == ["model-c", "model-d", "+2 more"], result["tags"]


def test_refresh_source_never_rebuilds_the_card(tmp_path):
    """Guard the fix shape: no wholesale card replacement on the refresh path."""
    js = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    refresh = extract_function(js, "_refreshProviderModels", prefix="async function")
    assert "replaceWith" not in refresh
    assert "_updateProviderCardModels" in refresh
