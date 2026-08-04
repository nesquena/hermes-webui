"""Regression coverage for duplicate configured model entries in the picker."""

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


_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');
function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = ui.indexOf('{', start); let depth = 1; i++;
  while (depth > 0 && i < ui.length) {
    if (ui[i] === '{') depth++;
    else if (ui[i] === '}') depth--;
    i++;
  }
  return ui.slice(start, i);
}
eval(extractFunc('_normalizeConfiguredModelKey'));
eval(extractFunc('_isEquivalentConfiguredModelEntry'));
const cases = JSON.parse(process.argv[3]);
const result = cases.map(c => _isEquivalentConfiguredModelEntry(c.modelId, c.badge, c.entries));
process.stdout.write(JSON.stringify(result));
"""


def _equivalent_cases(tmp_path, cases):
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    assert NODE is not None
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS_PATH), json.dumps(cases)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_picker_rows_preserve_provider_id_for_equivalence_check():
    """The synthesis loop must compare badge routes against real row providers."""
    ui = UI_JS_PATH.read_text(encoding="utf-8")

    assert "const providerId=child.dataset&&child.dataset.provider?child.dataset.provider:'';" in ui
    assert "providerId,modelsEndpointError,badge:_getConfiguredModelBadge" in ui
    assert "providerId,\n          modelsEndpointError," in ui
    assert "if(_isEquivalentConfiguredModelEntry(modelId,badge,_modelData)) continue;" in ui
    assert "_existingConfiguredKeys" not in ui


def test_named_custom_provider_routing_id_does_not_duplicate_picker_row(tmp_path):
    entries = [{"value": "model-a", "providerId": "custom:example"}]
    results = _equivalent_cases(
        tmp_path,
        [
            {
                "modelId": "@custom:example:model-a",
                "badge": {"provider": "custom:example"},
                "entries": entries,
            },
            {
                "modelId": "model-a",
                "badge": {"provider": "custom:example"},
                "entries": entries,
            },
        ],
    )

    assert results == [True, True]


def test_same_model_id_from_another_provider_remains_distinct(tmp_path):
    entries = [{"value": "model-a", "providerId": "custom:primary"}]
    results = _equivalent_cases(
        tmp_path,
        [
            {
                "modelId": "@custom:backup:model-a",
                "badge": {"provider": "custom:backup"},
                "entries": entries,
            },
            {
                "modelId": "model-a",
                "badge": {"provider": "custom:backup"},
                "entries": entries,
            },
        ],
    )

    assert results == [False, False]
