"""Shared qualified-ID grammar for @custom: model labels (deep-review 2026-08-13).

PR #6657 defect 2: `getModelLabel()` used an unconditional colon split, which
misparsed a supported host-port provider ID such as `@custom:localhost:1234:qwen3`
into `1234:qwen3`. The fallback now mirrors api/config.py's
`_parse_provider_qualified_model_id` grammar (rsplit + host-port-aware), while
the operator-supplied catalog label always wins when present.
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


_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');
function extractFunc(name) {
  // Top-level functions in ui.js start at column 0 and close with a bare '}'
  // at column 0. The brace-counter extractor used by other tests is unsafe
  // here: getModelLabel's body contains '{' inside strings, which makes the
  // counter swallow the neighbouring functions.
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  const lineStart = ui.lastIndexOf('\n', start) + 1;
  const lines = ui.slice(lineStart).split('\n');
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    out.push(lines[i]);
    if (i > 0 && lines[i] === '}') break;
  }
  return out.join('\n');
}
let _dynamicModelLabels = JSON.parse(process.argv[3]);
eval(extractFunc('_customSlugLooksLikeHostPort'));
eval(extractFunc('_customModelFromQualifiedId'));
eval(extractFunc('getModelLabel'));
const cases = JSON.parse(process.argv[4]);
const result = cases.map(c => getModelLabel(c));
process.stdout.write(JSON.stringify(result));
"""


def _labels(tmp_path, dynamic_labels, cases):
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    assert NODE is not None
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS_PATH), json.dumps(dynamic_labels), json.dumps(cases)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_qualified_id_grammar_without_dynamic_labels(tmp_path):
    """Dynamic labels absent: the fallback mirrors the backend grammar."""
    cases = [
        "@custom:my-key:some-model:free",                       # named provider + colon tag
        "@custom:localhost:1234:qwen3",                         # host-port endpoint slug
        "@custom:10.8.71.41:8080:Qwen3",                        # IPv4 endpoint slug
        "@custom:claude-code:us.anthropic.claude-opus-4-5-1-v1:0",  # namespaced id + :0 tail
        "@custom:ai_gateway/Qwen3.6-35B-A3B",                   # legacy slash form
        "@custom:qwen397b-64k",                                 # bare model, no separator
    ]
    expected = [
        "some-model:free",
        "qwen3",
        "Qwen3",
        "us.anthropic.claude-opus-4-5-1-v1:0",
        "Qwen3.6-35B-A3B",
        "qwen397b-64k",
    ]
    assert _labels(tmp_path, {}, cases) == expected


def test_dynamic_catalog_label_wins_when_present(tmp_path):
    """Dynamic labels present: the operator-supplied catalog label is returned
    verbatim, no id parsing involved."""
    labels = {"@custom:my-key:some-model:free": "Meu Modelo Free"}
    cases = ["@custom:my-key:some-model:free", "@custom:localhost:1234:qwen3"]
    assert _labels(tmp_path, labels, cases) == ["Meu Modelo Free", "qwen3"]
