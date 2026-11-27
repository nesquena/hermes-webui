"""Cross-check: the server's owner rule and the browser's owner rule agree.

The server (``api/config.py::_picker_policy_owner_key``) and the browser
(``static/ui.js``) must resolve a stored policy key to the SAME owner, or the
two sides filter different groups.

The browser exposes no "resolve the owner" API, so the parity is asserted
through OBSERVABLE behaviour: for every key in the built-in alias table (plus
the custom/punctuation variants), the browser must hide a model from exactly
the group the server attributes the key to — and from no other group.

This file drives the real JS in node and compares its answer with the
server's.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from api import config

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const m = re.exec(src);
  if (!m) throw new Error(name + ' not found');
  const start = m.index;
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

global.window = global;
global.document = {
  createElement: () => ({ dataset: {}, style: {}, classList: { add(){}, remove(){} } }),
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
};

eval(extractFunc('_pickerExcludesForProvider'));
eval(extractFunc('_collectKnownPickerProviders'));
eval(extractFunc('_isKnownPickerProvider'));
eval(extractFunc('_bareModelIdForExcludeMatch'));
eval(extractFunc('_modelIsPickerExcluded'));

// For each (storedKey, candidateGroup) pair: does the browser hide "m" for
// that group under a policy stored under storedKey?
const pairs = JSON.parse(process.argv[3]);
const out = [];
for (const [storedKey, group] of pairs) {
  const policy = {};
  policy[storedKey] = ['m'];
  window._pickerExcludes = policy;
  window._modelCatalogGroups = [{ provider_id: group }];
  window._activeProvider = group;
  window._configuredModelBadges = {};
  out.push(_modelIsPickerExcluded('m', group));
}
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("js") / "owner_driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return path


def _browser_hides(driver_path: Path, pairs: list[tuple[str, str]]) -> list[bool]:
    """For each (storedKey, group), does the browser hide the model?"""
    node = NODE or shutil.which("node")
    assert node, "node not on PATH"
    result = subprocess.run(
        [node, str(driver_path), str(UI_JS_PATH), json.dumps(pairs)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"node driver failed: {result.stderr.strip()[:800]}")
    return json.loads(result.stdout)


def _server_hides(stored_key: str, group: str) -> bool:
    """The server's own answer: does the policy stored under stored_key apply
    to the given provider group?"""
    owner = config._picker_policy_owner_key(stored_key)
    group_owner = config._picker_policy_owner_key(group)
    return owner == group_owner


def _assert_parity(driver_path, pairs):
    browser = _browser_hides(driver_path, pairs)
    mismatches = {}
    for (stored_key, group), browser_says in zip(pairs, browser):
        server_says = _server_hides(stored_key, group)
        if browser_says != server_says:
            mismatches[(stored_key, group)] = (server_says, browser_says)
    assert not mismatches, (
        "the server and the browser resolve a policy key to different owners: "
        f"{mismatches}"
    )


def test_the_browser_owner_rule_matches_the_server_for_every_alias(driver_path):
    """Every built-in alias key resolves to the same owner on both sides."""
    alias_keys = sorted(config._PROVIDER_ALIASES.keys())
    assert alias_keys, "the alias table is empty — the probe would be vacuous"

    canonical_groups = sorted(set(config._PROVIDER_ALIASES.values()))
    pairs = [(key, group) for key in alias_keys for group in canonical_groups]

    _assert_parity(driver_path, pairs)


def test_the_browser_owner_rule_matches_the_server_for_custom_keys(driver_path):
    """A ``custom:``-shaped key owns itself on BOTH sides."""
    keys = [
        "custom:glm",
        "custom:openai",
        "custom:ab",
        "custom:a-b",
        "custom:alpha:model-a",
    ]
    groups = ["custom:glm", "custom:openai", "custom:ab", "custom:a-b", "zai"]
    pairs = [(k, g) for k in keys for g in groups]

    _assert_parity(driver_path, pairs)


def test_the_browser_owner_rule_matches_the_server_for_plain_keys(driver_path):
    """A plain provider key owns itself on BOTH sides."""
    keys = ["openai", "zai", "anthropic", "openrouter", "some-new-provider"]
    groups = ["openai", "zai", "anthropic", "openrouter", "some-new-provider"]
    pairs = [(k, g) for k in keys for g in groups]

    _assert_parity(driver_path, pairs)
