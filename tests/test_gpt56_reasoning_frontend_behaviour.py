"""Behavioral coverage for model-filtered GPT-5.6 reasoning controls."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = REPO_ROOT / "static" / "commands.js"
UI_JS = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

GPT56_ULTRA_EFFORTS = ["none", "low", "medium", "high", "xhigh", "max", "ultra"]
GPT56_LUNA_EFFORTS = ["none", "low", "medium", "high", "xhigh", "max"]


_SLASH_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const supported = JSON.parse(process.argv[3]);
const ctx = {
  console,
  localStorage: {getItem(){return null;}, setItem(){}, removeItem(){}},
  t: key => key,
  api: async path => {throw new Error('unexpected API call: ' + path);},
  _currentReasoningEffortsSupported: supported,
};
vm.createContext(ctx);
vm.runInContext(src, ctx);
vm.runInContext("getSlashAutocompleteMatches('/reasoning ')", ctx)
  .then(items => process.stdout.write(JSON.stringify(items.map(item => item.value))))
  .catch(error => {console.error(error.stack || error); process.exit(1);});
"""


_OPTIONS_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const supported = JSON.parse(process.argv[3]);
const efforts = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
const options = efforts.map(effort => ({dataset:{effort}, style:{display:'unset'}}));
const dropdown = {querySelectorAll(){return options;}};
const ctx = {$: id => id === 'composerReasoningDropdown' ? dropdown : null};
vm.createContext(ctx);

function extractFunc(name) {
  const start = src.search(new RegExp('function\\s+' + name + '\\s*\\('));
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start) + 1;
  let depth = 1;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

vm.runInContext(extractFunc('_applyReasoningOptions'), ctx);
ctx._applyReasoningOptions(supported);
process.stdout.write(JSON.stringify(Object.fromEntries(
  options.map(option => [option.dataset.effort, option.style.display])
)));
"""


def _run_driver(tmp_path: Path, name: str, source: str, target: Path, payload):
    driver = tmp_path / name
    driver.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(target), json.dumps(payload)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("supported", "expected"),
    [
        (
            GPT56_ULTRA_EFFORTS,
            ["show", "hide", "none", "low", "medium", "high", "xhigh", "max", "ultra"],
        ),
        (
            GPT56_LUNA_EFFORTS,
            ["show", "hide", "none", "low", "medium", "high", "xhigh", "max"],
        ),
        ([], ["show", "hide", "none"]),
    ],
    ids=["sol-terra-ultra", "luna-max-only", "unsupported-or-unknown"],
)
def test_reasoning_slash_autocomplete_uses_active_model_capabilities(
    tmp_path, supported, expected
):
    assert _run_driver(
        tmp_path, "slash-driver.js", _SLASH_DRIVER, COMMANDS_JS, supported
    ) == expected


@pytest.mark.parametrize(
    ("supported", "visible", "hidden"),
    [
        (GPT56_ULTRA_EFFORTS, {"max", "ultra"}, {"minimal"}),
        (GPT56_LUNA_EFFORTS, {"max"}, {"minimal", "ultra"}),
        ([], {"none"}, {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}),
    ],
    ids=["sol-terra-ultra", "luna-max-only", "unsupported-or-unknown"],
)
def test_reasoning_dropdown_uses_same_model_capability_filter(
    tmp_path, supported, visible, hidden
):
    display = _run_driver(
        tmp_path, "options-driver.js", _OPTIONS_DRIVER, UI_JS, supported
    )
    assert all(display[effort] == "" for effort in visible)
    assert all(display[effort] == "none" for effort in hidden)
