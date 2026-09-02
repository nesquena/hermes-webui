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

Tests run the live JS functions via Node, so drift between the test and the
real code is caught immediately.
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
for (const n of ['_qualifiedCatalogOptionMeta', '_providerFromModelValue', '_getOptionProviderId', '_modelStateForSelect']) {
  eval(extractFunc(n));
}
const args = JSON.parse(process.argv[3]);
const out = {};
// _qualifiedCatalogOptionMeta over the requested (model, provider) pairs.
out.meta = args.meta.map(pair => {
  const [model, provider] = pair;
  return { model, provider, got: _qualifiedCatalogOptionMeta(model, provider) };
});
// Extraction over a select whose chosen option carries the metadata exactly
// as the fixed population loop stamps it.
function makeSel(value, metaModel, metaProvider) {
  const opt = { value: value, dataset: {}, textContent: value };
  if (metaModel) opt.dataset.model = metaModel;
  if (metaProvider) {
    opt.dataset.provider = metaProvider;
    opt.parentElement = { tagName: 'OPTGROUP', dataset: { provider: metaProvider } };
  }
  return { options: [opt], value: value };
}
out.withMeta = _modelStateForSelect(makeSel(args.qualifiedId, args.bareModel, args.provider), args.qualifiedId);
out.withoutMeta = _modelStateForSelect(makeSel(args.qualifiedId, null, null), args.qualifiedId);
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


def test_fresh_session_selection_keeps_bare_model(driver_path):
    """End-to-end: extracting the selection state of a custom-provider model
    on a new session (option stamped with the fix's metadata) persists the
    bare model id + owning provider — NOT the @provider:model routing id."""
    out = _run_driver(driver_path, [])
    assert out["withMeta"] == {"model": BARE_MODEL, "model_provider": PROVIDER}, (
        "a catalog option for a non-active custom provider must extract to the "
        "bare model, not the @provider:model routing id"
    )


def test_pre_fix_option_leaks_routing_id(driver_path):
    """Bug demonstration / contract guard: an option WITHOUT the metadata the
    population paths now stamp falls back to the full routing id — which is
    exactly the leak #7241 reports. Keeps the metadata contract honest."""
    out = _run_driver(driver_path, [])
    assert out["withoutMeta"]["model"] == QUALIFIED_ID, (
        "without option metadata the extraction falls back to option.value "
        "(the pre-fix leak) — metadata must keep being populated"
    )
    assert out["withoutMeta"]["model"] != BARE_MODEL
