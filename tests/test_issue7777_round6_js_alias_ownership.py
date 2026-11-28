"""#7777 round 6 — the browser applies the SAME alias-ownership rule.

Review 5489844033 (2026-10-12), still open at the 2026-11-23 re-gate:

    Provider-less unions and punctuation (``static/ui.js``): ``custom:ab``
    matches ``custom:a-b``, and an OpenAI-only ``shared`` exclusion hides
    ``@anthropic:shared``.

and the 2026-11-23 re-gate's third requirement:

    Make the browser policy payload apply the same ownership rule, so the
    server and the picker filter the same group.

The server (``api/config.py``) resolves a stored policy key to its OWNER: a
built-in alias key (``glm``) owns the canonical provider it resolves to
(``zai``), while a ``custom:<slug>`` key owns itself. The browser must
resolve the same way, or the two sides filter different groups.

This file spawns ``node`` on the real ``static/ui.js``, extracts the functions
under test, and drives them against a mocked DOM — the pattern used by
``tests/test_issue7507_round2_js_behaviour.py``.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3] || '';

// ── extract a top-level function by name (brace-matched) ──────────────────
function extractFunc(name, fromSrc) {
  const source = fromSrc || src;
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const m = re.exec(source);
  if (!m) throw new Error(name + ' not found');
  const start = m.index;
  let i = source.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < source.length) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') depth--;
    i++;
  }
  return source.slice(start, i);
}

// ── mocked DOM ────────────────────────────────────────────────────────────
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

// Probe: is a model excluded for a given provider, under a given policy?
function probe(policy, providerId, modelId) {
  window._pickerExcludes = policy;
  window._modelCatalogGroups = [{ provider_id: providerId }];
  window._activeProvider = providerId;
  window._configuredModelBadges = {};
  return _modelIsPickerExcluded(modelId, providerId);
}

const results = {};
switch (scenario) {
  case 'alias_key_hides_builtin':
    results.value = probe({ glm: ['m'] }, 'zai', 'm');
    break;
  case 'alias_key_does_not_hide_custom':
    results.value = probe({ glm: ['m'] }, 'custom:glm', 'm');
    break;
  case 'custom_key_hides_custom':
    results.value = probe({ 'custom:glm': ['m'] }, 'custom:glm', 'm');
    break;
  case 'custom_key_does_not_hide_builtin':
    results.value = probe({ 'custom:glm': ['m'] }, 'zai', 'm');
    break;
  case 'kimi_alias_hides_kimi_coding':
    results.value = probe({ kimi: ['m'] }, 'kimi-coding', 'm');
    break;
  case 'kimi_alias_does_not_hide_custom':
    results.value = probe({ kimi: ['m'] }, 'custom:kimi', 'm');
    break;
  case 'moonshot_alias_hides_kimi_coding':
    results.value = probe({ moonshot: ['m'] }, 'kimi-coding', 'm');
    break;
  case 'plain_key_hides_its_own_group':
    results.value = probe({ openai: ['m'] }, 'openai', 'm');
    break;
  case 'plain_key_does_not_hide_a_sibling':
    results.value = probe({ openai: ['m'] }, 'anthropic', 'm');
    break;
  case 'punctuation_variant_stays_distinct':
    // ``custom:ab`` must not apply to ``custom:a-b``.
    results.value = probe({ 'custom:ab': ['m'] }, 'custom:a-b', 'm');
    break;
  case 'provider_less_publishes_nothing':
    // A provider-less query must not union every provider's list.
    results.value = probe({ openai: ['shared'] }, null, 'shared');
    break;
  default:
    throw new Error('unknown scenario ' + scenario);
}
process.stdout.write(JSON.stringify(results));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("js") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return path


def _run_scenario(driver_path: Path, scenario: str) -> dict:
    node = NODE or shutil.which("node")
    assert node, "node not on PATH"
    result = subprocess.run(
        [node, str(driver_path), str(UI_JS_PATH), scenario],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"node driver failed for {scenario}: {result.stderr.strip()[:800]}"
        )
    return json.loads(result.stdout)


@pytest.mark.parametrize("alias", ["kimi", "moonshot"])
def test_other_builtin_aliases_own_their_canonical_group(driver_path, alias):
    """The rule covers alias keys generally, not only ``glm``."""
    hides = _run_scenario(driver_path, f"{alias}_alias_hides_kimi_coding")
    assert hides["value"] is True, (
        f"the alias key {alias!r} did not hide a model from its canonical group"
    )


def test_another_alias_key_does_not_hide_a_custom_endpoint(driver_path):
    """``{"kimi": ["m"]}`` must NOT hide ``m`` for ``custom:kimi``."""
    out = _run_scenario(driver_path, "kimi_alias_does_not_hide_custom")
    assert out["value"] is False, out


def test_an_alias_key_hides_a_model_from_the_builtin_group(driver_path):
    """``{"glm": ["m"]}`` must hide ``m`` for the ``zai`` group."""
    out = _run_scenario(driver_path, "alias_key_hides_builtin")
    assert out["value"] is True, (
        "the browser did not apply the alias key to the builtin group, so the "
        "server and the picker filter different groups"
    )


def test_an_alias_key_does_not_hide_a_model_from_a_custom_endpoint(driver_path):
    """``{"glm": ["m"]}`` must NOT hide ``m`` for the ``custom:glm`` group."""
    out = _run_scenario(driver_path, "alias_key_does_not_hide_custom")
    assert out["value"] is False, (
        "a builtin alias exclusion leaked onto a custom endpoint of the same name"
    )


def test_a_custom_key_hides_a_model_from_its_own_group(driver_path):
    out = _run_scenario(driver_path, "custom_key_hides_custom")
    assert out["value"] is True, out


def test_a_custom_key_does_not_hide_a_model_from_the_builtin_group(driver_path):
    out = _run_scenario(driver_path, "custom_key_does_not_hide_builtin")
    assert out["value"] is False, (
        "a custom endpoint's policy reached the builtin behind its alias"
    )


def test_punctuation_variants_stay_distinct(driver_path):
    """``custom:ab`` must not apply to ``custom:a-b``."""
    out = _run_scenario(driver_path, "punctuation_variant_stays_distinct")
    assert out["value"] is False, out


def test_a_provider_less_query_publishes_nothing(driver_path):
    """An OpenAI-only ``shared`` exclusion must not hide ``@anthropic:shared``."""
    out = _run_scenario(driver_path, "provider_less_publishes_nothing")
    assert out["value"] is False, out


def test_a_plain_key_hides_its_own_group(driver_path):
    out = _run_scenario(driver_path, "plain_key_hides_its_own_group")
    assert out["value"] is True, out


def test_a_plain_key_does_not_hide_a_sibling_group(driver_path):
    out = _run_scenario(driver_path, "plain_key_does_not_hide_a_sibling")
    assert out["value"] is False, out
