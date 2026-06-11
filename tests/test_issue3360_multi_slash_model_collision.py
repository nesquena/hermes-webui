"""
Regression tests for #3360 — multi-slash model ID collisions in the
model picker.

Two bugs:

1. ``_findModelInDropdown`` provider-aware match (L1074) runs BEFORE the
   exact match (L1078). When multiple options from the same proxy provider
   normalize identically (e.g. ``vendor_a/deepseek/deepseek-v4-pro`` and
   ``vendor_b/deepseek/deepseek-v4-pro`` both → ``deepseek/deepseek.v4.pro``),
   ``options.find()`` returns whichever appears first in DOM order. Fix:
   move the exact match to the top of the function.

2. ``_normalizeConfiguredModelKey`` uses ``split('/').pop()`` which takes
   only the last segment, collapsing multi-slash IDs to the same key as
   single-slash configured models. Fix: strip only the first segment via
   ``replace(/^[^/]+\\//, '')``.  Backend ``_norm_model_id`` mirrors the
   same fix.

Tests run the live JS functions via Node and the live Python function via
exec, so drift between the test and the real code is caught immediately.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
CONFIG_PY = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


# ── JS driver for _findModelInDropdown ──────────────────────────────────────

_FIND_MODEL_DRIVER = r"""
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
function _getOptionProviderId(opt) {
  if (!opt) return '';
  if (opt.dataset && opt.dataset.provider) return opt.dataset.provider;
  const group = opt.parentElement;
  if (group && group.tagName === 'OPTGROUP' && group.dataset && group.dataset.provider) return group.dataset.provider;
  const value = String(opt.value || '');
  if (value.startsWith('@') && value.includes(':')) return value.slice(1, value.lastIndexOf(':'));
  return '';
}
eval(extractFunc('_findModelInDropdown'));
const args = JSON.parse(process.argv[3]);
const sel = {
  options: args.options.map(v => {
    const opt = {value: v.value || v, dataset: {}};
    if (v.provider) opt.dataset.provider = v.provider;
    // Simulate optgroup parent for provider detection
    if (v.provider) {
      opt.parentElement = {tagName: 'OPTGROUP', dataset: {provider: v.provider}};
    }
    return opt;
  })
};
const got = _findModelInDropdown(args.modelId, sel, args.preferredProvider || undefined);
process.stdout.write(JSON.stringify(got));
"""


# ── JS driver for _normalizeConfiguredModelKey ──────────────────────────────

_NORM_KEY_DRIVER = r"""
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
eval(extractFunc('_normalizeConfiguredModelKey'));
const ids = JSON.parse(process.argv[3]);
const result = {};
for (const id of ids) { result[id] = _normalizeConfiguredModelKey(id); }
process.stdout.write(JSON.stringify(result));
"""


@pytest.fixture(scope="module")
def find_driver(tmp_path_factory):
    p = tmp_path_factory.mktemp("find_driver") / "driver.js"
    p.write_text(_FIND_MODEL_DRIVER, encoding="utf-8")
    return str(p)


@pytest.fixture(scope="module")
def norm_driver(tmp_path_factory):
    p = tmp_path_factory.mktemp("norm_driver") / "driver.js"
    p.write_text(_NORM_KEY_DRIVER, encoding="utf-8")
    return str(p)


def _find(driver_path, model_id, options, preferred=None):
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH),
         json.dumps({"modelId": model_id, "options": options, "preferredProvider": preferred})],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


def _norm_keys(driver_path, ids):
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH), json.dumps(ids)],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


def _backend_norm():
    """Extract and exec the backend _norm_model_id function."""
    start_marker = "def _norm_model_id(model_id: str) -> str:"
    end_marker = "def _build_configured_model_badges"
    s = CONFIG_PY.find(start_marker)
    e = CONFIG_PY.find(end_marker, s)
    assert s != -1 and e != -1
    body = CONFIG_PY[s:e]
    lines = body.splitlines()
    indent = None
    for ln in lines:
        if ln.strip():
            indent = len(ln) - len(ln.lstrip())
            break
    dedented = "\n".join(ln[indent:] if len(ln) >= indent else ln for ln in lines)
    ns = {}
    exec(dedented, ns)
    return ns["_norm_model_id"]


# ═══════════════════════════════════════════════════════════════════════════
# Fix 1: _findModelInDropdown — exact match must beat provider-aware match
# ═══════════════════════════════════════════════════════════════════════════


class TestFindModelExactMatchPriority:
    """When the exact model ID exists as an option value, return it
    regardless of normalization collisions with other options."""

    def test_exact_match_beats_normalized_collision_same_provider(self, find_driver):
        """Core #3360 regression: two multi-slash IDs from the same proxy
        provider normalize identically. The clicked value must be returned."""
        options = [
            {"value": "nanogpt/deepseek/deepseek-v4-pro", "provider": "llm-proxy"},
            {"value": "command/deepseek/deepseek-v4-pro", "provider": "llm-proxy"},
        ]
        got = _find(find_driver, "command/deepseek/deepseek-v4-pro", options, "llm-proxy")
        assert got == "command/deepseek/deepseek-v4-pro", (
            f"Expected exact match for command/deepseek/deepseek-v4-pro, got {got!r}"
        )

    def test_exact_match_beats_dom_order(self, find_driver):
        """Even when the clicked option is NOT first in DOM order, the
        exact match must still win."""
        options = [
            {"value": "alpha/deepseek/deepseek-v4-pro", "provider": "proxy"},
            {"value": "beta/deepseek/deepseek-v4-pro", "provider": "proxy"},
            {"value": "gamma/deepseek/deepseek-v4-pro", "provider": "proxy"},
        ]
        # Click the last one
        got = _find(find_driver, "gamma/deepseek/deepseek-v4-pro", options, "proxy")
        assert got == "gamma/deepseek/deepseek-v4-pro"

    def test_exact_match_single_slash_still_works(self, find_driver):
        """Single-slash IDs that exist as options must still resolve."""
        options = [
            {"value": "openai/gpt-5.5", "provider": "openai"},
            {"value": "openai/gpt-5.4-mini", "provider": "openai"},
        ]
        got = _find(find_driver, "openai/gpt-5.5", options, "openai")
        assert got == "openai/gpt-5.5"


# ═══════════════════════════════════════════════════════════════════════════
# Fix 2: _normalizeConfiguredModelKey — multi-slash IDs must not collide
# ═══════════════════════════════════════════════════════════════════════════


class TestNormalizeConfiguredModelKeyMultiSlash:
    """After the fix, multi-slash IDs preserve vendor hierarchy and do
    not collide with single-slash or bare IDs."""

    def test_multi_slash_preserves_vendor_segment(self, norm_driver):
        keys = _norm_keys(norm_driver, [
            "vendor_a/deepseek-v4-pro",
            "vendor_b/deepseek/deepseek-v4-pro",
        ])
        assert keys["vendor_a/deepseek-v4-pro"] == "deepseek.v4.pro"
        assert keys["vendor_b/deepseek/deepseek-v4-pro"] == "deepseek/deepseek.v4.pro"
        assert keys["vendor_a/deepseek-v4-pro"] != keys["vendor_b/deepseek/deepseek-v4-pro"], (
            "Single-slash and multi-slash IDs must not collide"
        )

    def test_single_slash_behavior_unchanged(self, norm_driver):
        keys = _norm_keys(norm_driver, [
            "openai/gpt-5.5",
            "anthropic/claude-opus-4.6",
        ])
        assert keys["openai/gpt-5.5"] == "gpt.5.5"
        assert keys["anthropic/claude-opus-4.6"] == "claude.opus.4.6"

    def test_bare_model_unchanged(self, norm_driver):
        keys = _norm_keys(norm_driver, ["deepseek-v4-pro"])
        assert keys["deepseek-v4-pro"] == "deepseek.v4.pro"

    def test_at_provider_prefix_still_stripped(self, norm_driver):
        keys = _norm_keys(norm_driver, ["@custom:jingdong:GLM-5"])
        assert keys["@custom:jingdong:GLM-5"] == "glm.5"

    def test_trailing_slash_fallback(self, norm_driver):
        """A trailing slash (malformed) must not collapse to empty."""
        keys = _norm_keys(norm_driver, ["provider/"])
        assert keys["provider/"] != "", "Trailing slash collapsed to empty string"


# ═══════════════════════════════════════════════════════════════════════════
# Backend / frontend parity
# ═══════════════════════════════════════════════════════════════════════════


class TestBackendFrontendNormParity:
    """The Python _norm_model_id must produce the same output as the
    JS _normalizeConfiguredModelKey for identical inputs."""

    def test_parity_multi_slash(self, norm_driver):
        ids = [
            "deepseek-v4-pro",
            "vendor_a/deepseek-v4-pro",
            "vendor_b/deepseek/deepseek-v4-pro",
            "@custom:jingdong:GLM-5",
        ]
        js_keys = _norm_keys(norm_driver, ids)
        py_norm = _backend_norm()
        for model_id in ids:
            py_result = py_norm(model_id)
            js_result = js_keys[model_id]
            assert py_result == js_result, (
                f"Parity mismatch for {model_id!r}: "
                f"Python={py_result!r}, JS={js_result!r}"
            )
