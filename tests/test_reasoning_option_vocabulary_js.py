"""Behavioral coverage for server-driven reasoning effort options."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const options = [];

function makeOption(effort, label) {
  return {
    className: 'reasoning-option',
    dataset: {effort},
    style: {},
    textContent: label,
  };
}

const dropdown = {
  querySelectorAll(selector) {
    if (selector !== '.reasoning-option') return [];
    return options.filter(opt => opt.className === 'reasoning-option');
  },
  appendChild(opt) { options.push(opt); },
};

options.push(makeOption('none', 'None'));
options.push(makeOption('low', 'Low'));
options.push(makeOption('max', 'Max'));

global.document = {
  createElement() {
    return {className: '', dataset: {}, style: {}, textContent: ''};
  },
};
global.$ = id => id === 'composerReasoningDropdown' ? dropdown : null;

function extractFunc(name) {
  const start = src.search(new RegExp('function\\s+' + name + '\\s*\\('));
  if (start < 0) throw new Error(name + ' not found');
  let cursor = src.indexOf('{', start) + 1;
  let depth = 1;
  while (depth > 0 && cursor < src.length) {
    if (src[cursor] === '{') depth++;
    else if (src[cursor] === '}') depth--;
    cursor++;
  }
  return src.slice(start, cursor);
}

eval(extractFunc('_normalizeReasoningEffort'));
eval(extractFunc('_formatReasoningEffortLabel'));
eval(extractFunc('_applyReasoningOptions'));

_applyReasoningOptions([' LOW ', 'ultra', 'future-tier', 'future-tier']);
const first = options.map(opt => ({
  effort: opt.dataset.effort,
  label: opt.textContent,
  display: opt.style.display,
}));

_applyReasoningOptions(['medium']);
const second = options.map(opt => ({
  effort: opt.dataset.effort,
  display: opt.style.display,
}));

process.stdout.write(JSON.stringify({first, second}));
"""


def test_server_vocabulary_materializes_and_reuses_dropdown_options(tmp_path):
    driver = tmp_path / "reasoning-options.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS_PATH)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)

    first = {entry["effort"]: entry for entry in output["first"]}
    assert len(output["first"]) == 5
    assert first["none"]["display"] == ""
    assert first["low"]["display"] == ""
    assert first["max"]["display"] == "none"
    assert first["ultra"] == {"effort": "ultra", "label": "Ultra", "display": ""}
    assert first["future-tier"] == {
        "effort": "future-tier",
        "label": "Future-tier",
        "display": "",
    }

    second = {entry["effort"]: entry for entry in output["second"]}
    assert len(output["second"]) == 6
    assert second["medium"]["display"] == ""
    assert second["ultra"]["display"] == "none"
    assert second["future-tier"]["display"] == "none"
