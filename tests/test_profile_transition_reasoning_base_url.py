"""Runtime regression for profile-transition reasoning context."""

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
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1;
  i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

const sandbox = {
  requestedUrl: null,
  _profileTransitionReasoningContext: null,
  _currentReasoningEffort: null,
  _currentReasoningEffortsSupported: null,
  _currentReasoningToggleSupported: undefined,
  _lastReasoningFetchKey: null,
  _reasoningFetchSeq: 0,
  S: {activeProfile: 'work'},
  URLSearchParams,
  _applyReasoningChip() {},
};
sandbox.api = function(url) {
  sandbox.requestedUrl = url;
  return {
    then() { return {catch() {}}; },
    catch() {},
  };
};
vm.createContext(sandbox);
vm.runInContext(extractFunc('fetchReasoningChip'), sandbox);
vm.runInContext(extractFunc('refreshProfileTransitionReasoningChip'), sandbox);
sandbox.refreshProfileTransitionReasoningChip(
  'glm-5.3',
  'zai',
  'https://zai.example.test/v1?tenant=alpha'
);
process.stdout.write(JSON.stringify({
  requestedUrl: sandbox.requestedUrl,
  context: sandbox._profileTransitionReasoningContext,
}));
"""


def test_profile_transition_initial_reasoning_get_includes_base_url(tmp_path):
    driver = tmp_path / "profile-transition-reasoning.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [str(NODE), str(driver), str(UI_JS_PATH)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["context"]["base_url"] == "https://zai.example.test/v1?tenant=alpha"
    assert payload["requestedUrl"] == (
        "/api/reasoning?model=glm-5.3&provider=zai&"
        "base_url=https%3A%2F%2Fzai.example.test%2Fv1%3Ftenant%3Dalpha"
    )
