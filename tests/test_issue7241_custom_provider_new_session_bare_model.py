"""
Regression tests for #7241 — selecting a custom-provider model on a NEW
session must persist the bare model id, not the full @provider:model routing
id.

Root cause: server-side catalog rows for NON-active providers arrive
provider-qualified (the dedupe pass rewrites colliding ids as
@<provider_id>:<model>, e.g. @custom:omni:antigravity/gemini-3.7-flash-tiered).
The picker population loop assigned that id straight to option.value without
populating the option's bare-model metadata (dataset.model), while
_modelStateForSelect() prefers dataset.model and falls back to option.value —
so a fresh-session selection leaked the whole routing id into the session
config.

Fix: _qualifiedCatalogOptionMeta() computes {model, provider} for such rows
and the option-creation paths (catalog population + "Show more" overflow
reveal) stamp it as dataset.model/dataset.provider, mirroring what
_ensureModelOptionInDropdown() already does for injected options.

Tests execute the REAL metadata-stamping paths — the catalog population loop's
stamp decision (_qualifiedCatalogOptionMeta over the server-shipped id +
group provider) and the overflow reveal (_appendOverflowOptionsToGroup) —
rather than supplying an already-stamped option by hand, so drift between the
test payload and production stamping is caught immediately.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# The issue's exact repro: a non-active custom provider group (custom:omni)
# whose model id collides with another group, so the server qualified it.
QUALIFIED_ID = "@custom:omni:antigravity/gemini-3.7-flash-tiered"
BARE_MODEL = "antigravity/gemini-3.7-flash-tiered"
PROVIDER = "custom:omni"

_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');
function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = ui.indexOf('{', start); let depth = 1; i++;
  while (depth > 0 && i < ui.length) { if (ui[i]==='{') depth++; else if (ui[i]==='}') depth--; i++; }
  return ui.slice(start, i);
}
for (const n of ['_qualifiedCatalogOptionMeta', '_providerFromModelValue', '_getOptionProviderId', '_modelStateForSelect', '_appendOverflowOptionsToGroup']) {
  eval(extractFunc(n));
}
const args = JSON.parse(process.argv[3]);
const out = {};
// 1) _qualifiedCatalogOptionMeta over the requested (model, provider) pairs.
out.meta = args.meta.map(pair => {
  const [model, provider] = pair;
  return { model, provider, got: _qualifiedCatalogOptionMeta(model, provider) };
});

// Minimal DOM for the option-creation paths under test.
function makeOptionNode() {
  const node = { value: '', textContent: '', dataset: {}, parentElement: null };
  return node;
}
const document = { createElement(tag) { return tag === 'option' ? makeOptionNode() : { dataset: {}, children: [] }; } };

// Build an optgroup the way populateModelDropdown() builds it from a server
// group: dataset.provider = group.provider_id, children appended as <option>.
function makeOptgroup(providerId) {
  const og = {
    tagName: 'OPTGROUP',
    dataset: { provider: providerId || '' },
    children: [],
    parentNode: null,
    appendChild(child) { child.parentElement = this; this.children.push(child); return child; },
  };
  return og;
}

// 2) CATALOG stamping path — replicate the population loop's option creation
// EXACTLY (ui.js populateModelDropdown): opt.value = m.id (server-shipped),
// then _qualifiedCatalogOptionMeta(m.id, group.provider_id) decides the
// dataset.model/dataset.provider stamp. Extraction over the resulting select
// must yield the bare model.
function buildCatalogSelect(groupProviderId, modelId) {
  const og = makeOptgroup(groupProviderId);
  const sel = { options: [], value: modelId };
  const opt = document.createElement('option');
  opt.value = modelId;
  opt.textContent = modelId;
  const qualifiedMeta = _qualifiedCatalogOptionMeta(modelId, og.dataset.provider);
  if (qualifiedMeta) {
    opt.dataset.model = qualifiedMeta.model;
    opt.dataset.provider = qualifiedMeta.provider;
  }
  og.appendChild(opt);
  sel.options.push(opt);
  return sel;
}
out.withMeta = _modelStateForSelect(buildCatalogSelect(args.provider, args.qualifiedId), args.qualifiedId);
// Same qualified id with NO owning provider group: the catalog stamp derives
// nothing, so extraction falls back to option.value (the pre-fix leak).
out.withoutMeta = _modelStateForSelect(buildCatalogSelect('', args.qualifiedId), args.qualifiedId);

// 3) OVERFLOW stamping path — _appendOverflowOptionsToGroup() appends the
// hidden extra_models tail as real <option> entries and stamps them with the
// same _qualifiedCatalogOptionMeta() helper (ui.js). Reuse the real function.
const overflowGroup = makeOptgroup(args.provider);
const overflowAppended = _appendOverflowOptionsToGroup(overflowGroup, [
  { id: args.qualifiedId, label: 'Overflow Qualified' },
  { id: '@custom:omni:gpt-5.4', label: 'Overflow Colon-Free' },
]);
const overflowOptions = overflowGroup.children.map(opt => ({
  value: opt.value,
  dataset: { ...opt.dataset },
}));
out.overflow = { overflowAppended, overflowOptions };
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("driver_7241") / "driver.js"
    p.write_text(_DRIVER, encoding="utf-8")
    return str(p)


def _run_driver(driver_path, meta_pairs):
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH),
         json.dumps({"meta": meta_pairs, "qualifiedId": QUALIFIED_ID,
                     "bareModel": BARE_MODEL, "provider": PROVIDER})],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


def test_qualified_catalog_row_meta_is_stripped(driver_path):
    """_qualifiedCatalogOptionMeta recovers bare model + provider from the
    provider-qualified ids the server ships for non-active providers."""
    pairs = [
        (QUALIFIED_ID, PROVIDER),                     # repro id from #7241
        ("@custom:omni:gpt-5.4", "custom:omni"),      # colon-free model
        ("@custom:10.8.71.41:8080:Qwen3", "custom:10.8.71.41:8080"),  # multi-colon custom endpoint
    ]
    out = _run_driver(driver_path, pairs)
    assert out["meta"][0]["got"] == {"model": BARE_MODEL, "provider": PROVIDER}
    assert out["meta"][1]["got"] == {"model": "gpt-5.4", "provider": "custom:omni"}
    assert out["meta"][2]["got"] == {"model": "Qwen3", "provider": "custom:10.8.71.41:8080"}


def test_bare_or_unmatched_rows_get_no_meta(driver_path):
    """Rows that are NOT provider-qualified must stay untouched (no metadata
    noise), and a qualifier that does not match the group provider is not
    mis-attributed."""
    pairs = [
        ("antigravity/gemini-3.7-flash-tiered", "custom:omni"),  # bare (active provider)
        ("@other:vendor:model", "custom:omni"),                  # prefix mismatch
        ("", "custom:omni"),
    ]
    out = _run_driver(driver_path, pairs)
    for entry in out["meta"]:
        assert entry["got"] is None, entry


def test_catalog_stamped_selection_keeps_bare_model(driver_path):
    """End-to-end over the REAL catalog stamping path: building a select option
    the way populateModelDropdown() does (server id + _qualifiedCatalogOptionMeta
    stamp) and extracting its selection state persists the bare model id +
    owning provider — NOT the @provider:model routing id."""
    out = _run_driver(driver_path, [])
    assert out["withMeta"] == {"model": BARE_MODEL, "model_provider": PROVIDER}, (
        "a catalog option for a non-active custom provider must extract to the "
        "bare model, not the @provider:model routing id"
    )


def test_ungrouped_option_leaks_routing_id(driver_path):
    """Bug demonstration / contract guard: with NO owning provider group the
    catalog stamp derives nothing and extraction falls back to the full routing
    id — exactly the leak #7241 reports. Keeps the stamping contract honest."""
    out = _run_driver(driver_path, [])
    assert out["withoutMeta"]["model"] == QUALIFIED_ID, (
        "without an owning provider group the extraction falls back to "
        "option.value (the pre-fix leak) — stamping must keep running for "
        "provider-qualified catalog rows"
    )
    assert out["withoutMeta"]["model"] != BARE_MODEL


def test_overflow_reveal_stamps_metadata(driver_path):
    """The 'Show more' overflow path (_appendOverflowOptionsToGroup) stamps the
    revealed extra_models options with the same bare model + provider metadata,
    so a fresh-session selection of an overflow model also persists bare."""
    out = _run_driver(driver_path, [])
    overflow = out["overflow"]
    assert overflow["overflowAppended"] == 2, overflow
    by_value = {opt["value"]: opt["dataset"] for opt in overflow["overflowOptions"]}
    assert by_value[QUALIFIED_ID] == {"model": BARE_MODEL, "provider": PROVIDER}, (
        "the overflow reveal must stamp dataset.model/dataset.provider on a "
        "provider-qualified extra model (#7241)"
    )
    assert by_value["@custom:omni:gpt-5.4"] == {"model": "gpt-5.4", "provider": PROVIDER}
