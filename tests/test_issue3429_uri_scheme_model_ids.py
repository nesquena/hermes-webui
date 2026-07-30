"""
Regression tests for #3429 — getModelLabel breaks URI-scheme model IDs.

The #3360 fix introduced first-segment slash stripping in getModelLabel,
_normalizeConfiguredModelKey, and _norm_model_id.  This correctly handles
provider-prefixed IDs (e.g. openai/gpt-5.5) but breaks URI-shaped model
IDs like gpt://${YANDEX_FOLDER_ID}/deepseek-v4-flash/latest — the scheme
portion (gpt:) is treated as the provider prefix, and the stripped result
starts with /${YANDEX_FOLDER_ID}/... instead of preserving the full URI.

Fix: detect URI-scheme IDs (matching ^[a-z][a-z0-9+.-]*://) and skip the
first-segment slash stripping entirely for those inputs.

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


# ── JS driver for getModelLabel ─────────────────────────────────────────────

_LABEL_DRIVER = r"""
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
// Stub dependencies
const _dynamicModelLabels = {};
function _fmtOllamaLabel(s) { return s; }
function $(id) { return null; }
eval(extractFunc('getModelLabel'));
const ids = JSON.parse(process.argv[3]);
const result = {};
for (const id of ids) { result[id] = getModelLabel(id); }
process.stdout.write(JSON.stringify(result));
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
def label_driver(tmp_path_factory):
    p = tmp_path_factory.mktemp("label_driver") / "driver.js"
    p.write_text(_LABEL_DRIVER, encoding="utf-8")
    return str(p)


@pytest.fixture(scope="module")
def norm_driver(tmp_path_factory):
    p = tmp_path_factory.mktemp("norm_driver") / "driver.js"
    p.write_text(_NORM_KEY_DRIVER, encoding="utf-8")
    return str(p)


def _labels(driver_path, ids):
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH), json.dumps(ids)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


def _norm_keys(driver_path, ids):
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH), json.dumps(ids)],
        capture_output=True, text=True, timeout=30,
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


def _backend_label(model_id):
    """Extract and exec the backend _get_label_for_model function."""
    start_marker = "def _get_label_for_model(model_id: str, existing_groups: list) -> str:"
    # Find the end by looking for the next top-level def
    s = CONFIG_PY.find(start_marker)
    assert s != -1
    # Find the next unindented def after the start
    rest = CONFIG_PY[s + len(start_marker):]
    lines = rest.splitlines()
    end_offset = 0
    for i, ln in enumerate(lines):
        if i > 0 and ln.startswith("def "):
            end_offset = sum(len(l) + 1 for l in lines[:i])
            break
    body = CONFIG_PY[s:s + len(start_marker) + end_offset]
    ns = {}
    exec(body, ns)
    return ns["_get_label_for_model"](model_id, [])


# ═══════════════════════════════════════════════════════════════════════════
# getModelLabel — URI-scheme model IDs must not be stripped
# ═══════════════════════════════════════════════════════════════════════════


class TestNormalizeConfiguredModelKeyUriScheme:
    def test_uri_scheme_preserved_in_normalization(self, norm_driver):
        """URI IDs must not have their scheme+authority stripped."""
        ids = [
            "gpt://b1g12345/deepseek-v4-flash/latest",
            "https://proxy.internal/models/gpt4",
        ]
        keys = _norm_keys(norm_driver, ids)
        for model_id in ids:
            assert "://" in keys[model_id], (
                f"URI scheme was stripped from normalized key: "
                f"{model_id!r} → {keys[model_id]!r}"
            )

    def test_regular_slash_ids_still_normalize(self, norm_driver):
        """Non-URI slash IDs must still have provider prefix stripped."""
        ids = ["openai/gpt-5.5", "vendor_a/deepseek-v4-pro"]
        keys = _norm_keys(norm_driver, ids)
        assert keys["openai/gpt-5.5"] == "gpt.5.5"
        assert keys["vendor_a/deepseek-v4-pro"] == "deepseek.v4.pro"


# ═══════════════════════════════════════════════════════════════════════════
# Backend / frontend parity for URI-scheme IDs
# ═══════════════════════════════════════════════════════════════════════════


class TestBackendFrontendUriSchemeParity:
    """Python _norm_model_id must match JS _normalizeConfiguredModelKey
    for URI-scheme inputs."""

    def test_parity_uri_scheme_ids(self, norm_driver):
        ids = [
            "gpt://b1g12345/deepseek-v4-flash/latest",
            "https://proxy.internal/v1/gpt4",
            "openai/gpt-5.5",
            "vendor_b/deepseek/deepseek-v4-pro",
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


class TestBackendGetLabelUriScheme:
    """Python _get_label_for_model must not strip URI scheme."""

    def test_yandex_gpt_uri_label(self):
        label = _backend_label("gpt://b1g12345abcdef/deepseek-v4-flash/latest")
        assert not label.startswith("/"), (
            f"Backend label for URI-scheme ID starts with /: {label!r}"
        )
        assert "b1g12345abcdef" in label.lower()
