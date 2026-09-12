"""#6946 re-gate: a real option from provider A must never retire a synthetic,
routable selection owned by provider B (cross-provider orphan dedup).

Two production shapes (per maintainer review #4):
(a) Provider A has a real catalog `shared-model`; a restored provider B
    selection is represented by the synthetic `@provider-b:shared-model` row
    because provider B's catalog entry is not hydrated. Dedup must preserve B.
(b) A provider B real twin is added to the catalog. Only the B
    synthetic/real pair collapses, the B real row becomes the reverse-lookup
    target, and the provider-A row remains.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function(src: str, name: str) -> str:
    import re

    match = re.search(rf"function\s+{re.escape(name)}\s*\(", src)
    assert match, f"{name} not found"
    start = match.start()
    i = src.index("{", match.end())
    depth = 1
    i += 1
    while depth > 0 and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[start:i]


def _run_harness(tmp_path, case: str, with_twin_b: bool) -> dict:
    src = UI_JS_PATH.read_text(encoding="utf-8")
    parts = [
        _function(src, "_getOptionProviderId"),
        _function(src, "_modelPickerOptionIdentity"),
        _function(src, "_modelPickerCanonicalIdentity"),
        _function(src, "_providerQualifiedPresetRest"),
        _function(src, "_deduplicateModelPickerOptions"),
        _function(src, "_findModelInDropdown"),
    ]
    driver = tmp_path / f"driver_{case}.js"
    driver.write_text(
        "\n".join(parts)
        + r"""
class Node {
  constructor(tag) { this.tagName=tag.toUpperCase(); this.children=[]; this.dataset={}; this.parentElement=null; this.value=''; this.textContent=''; }
  appendChild(child) { child.parentElement=this; this.children.push(child); return child; }
  removeChild(child) { this.children=this.children.filter(item=>item!==child); child.parentElement=null; }
  querySelectorAll(selector) {
    if(selector==='optgroup') return this.children.filter(child=>child.tagName==='OPTGROUP');
    return [];
  }
  get options() {
    return this.tagName==='SELECT'
      ? this.children.flatMap(child=>child.tagName==='OPTGROUP'?child.children:[child])
      : undefined;
  }
}
globalThis.window={_activeProvider:null};
globalThis.document={createElement:tag=>new Node(tag)};
globalThis._dynamicModelLabels={};
globalThis._modelStateForSelect=()=>({model:'',model_provider:null});
globalThis._applyModelToDropdown=()=>null;
globalThis.S={session:null};
function makeSelect(withTwinB) {
  const select=new Node('select');
  const groupA=new Node('optgroup'); groupA.dataset.provider='provider-a'; select.appendChild(groupA);
  const realA=new Node('option'); realA.value='shared-model'; realA.textContent='shared-model'; groupA.appendChild(realA);
  if(withTwinB){
    const groupB=new Node('optgroup'); groupB.dataset.provider='provider-b'; select.appendChild(groupB);
    const realB=new Node('option'); realB.value='shared-model'; realB.textContent='shared-model'; groupB.appendChild(realB);
  }
  // Synthetic orphan shaped exactly like _ensureModelOptionInDropdown creates
  // it: dataset.custom='1' AND dataset.provider set (ui.js:3464-3471).
  const orphan=new Node('option'); orphan.value='@provider-b:shared-model'; orphan.textContent='@provider-b:shared-model';
  orphan.dataset.custom='1'; orphan.dataset.provider='provider-b';
  select.appendChild(orphan);
  return select;
}
const WITH_TWIN_B = __WITH_TWIN_B__;
const select=makeSelect(WITH_TWIN_B);
const removed=_deduplicateModelPickerOptions(select,select.value);
const groups=select.querySelectorAll('optgroup').map(item=>item.children.map(option=>option.value));
const orphans=select.children.filter(child=>child.tagName==='OPTION').map(option=>option.value);
const found=_findModelInDropdown('@provider-b:shared-model',select,'provider-b');
// The reverse-lookup result must resolve to a row owned by provider B (the
// provider-aware canonical match filters the provider-A row out).
const foundHasProviderBRow=select.options.some(o=>o.value===found&&(_getOptionProviderId(o)||'')==='provider-b');
process.stdout.write(JSON.stringify({removed,groups,orphans,found,foundHasProviderBRow}));
""".replace(
            "__WITH_TWIN_B__", "true" if with_twin_b else "false"
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(driver)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_cross_provider_orphan_not_retired_by_other_provider_twin(tmp_path):
    """Provider A's real `shared-model` option must NOT retire provider B's
    synthetic `@provider-b:shared-model` orphan while provider B has no
    catalog twin (#6946 re-gate)."""
    out = _run_harness(tmp_path, "cross", with_twin_b=False)
    assert out["removed"] == 0, str(out)
    assert out["orphans"] == ["@provider-b:shared-model"], (
        "orphan owned by provider B must survive dedup (provider mismatch): "
        + str(out)
    )
    assert out["groups"] == [["shared-model"]], str(out)


def test_cross_provider_orphan_collapses_onto_real_twin(tmp_path):
    """Once a provider B real twin exists, only the B synthetic/real pair
    collapses; the B real row becomes the reverse-lookup target and the
    provider-A row remains (#6946 re-gate)."""
    out = _run_harness(tmp_path, "twin", with_twin_b=True)
    assert out["removed"] == 1, (
        "only the provider-B orphan must collapse onto its real twin: " + str(out)
    )
    assert out["orphans"] == [], (
        "provider-B synthetic row must be removed once the real twin exists: "
        + str(out)
    )
    assert out["groups"] == [["shared-model"], ["shared-model"]], (
        "provider-A row and provider-B real row must both remain: " + str(out)
    )
    assert out["found"] == "shared-model", str(out)
    assert out["foundHasProviderBRow"] is True, (
        "reverse lookup of @provider-b:shared-model must resolve to the "
        "provider-B real row (provider equality): " + str(out)
    )
