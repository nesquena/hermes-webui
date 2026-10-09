"""Regression test for #7240 — model dropdown truncates custom model names
containing colons.

getModelLabel() derived the label of a ``@custom:`` id from the substring
following the LAST ``:``, so a tag/variant suffix like ``:free``/``:31b``/
``:397b`` inside the model's own name became the whole label
(``@custom:omni:kg/stepfun/step-3.7-flash:free`` rendered as ``free``).

The fix makes the catalog the authority — a catalogued routing id renders its
exact `m.label` (the backend knows the real provider/model split). The string
peel is only a legacy fallback for ids the catalog has never seen, where a
provider slug (a config key or host:port authority) is distinguished from a
plain-lane model by its lack of a `/`.

Runs the live getModelLabel() via Node so drift between the test and the real
code is caught immediately.
"""
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
const ui = fs.readFileSync(process.argv[1], 'utf8');
// Slice getModelLabel() by function boundaries (regex literals inside it defeat
// a naive brace counter, so bound it by the next top-level function instead).
const start = ui.indexOf('function getModelLabel(');
if (start < 0) throw new Error('getModelLabel not found');
const after = ui.indexOf('\nfunction _gatewayProviderName(', start);
if (after < 0) throw new Error('getModelLabel end boundary not found');
const fnSrc = ui.slice(start, after);
// Optional pre-hydrated catalog: {routingId: label}. Absent -> empty, which
// exercises the string-only legacy fallback.
const _dynamicModelLabels = process.argv[3] ? JSON.parse(process.argv[3]) : {};
function _fmtOllamaLabel(s){ return s; }
// getModelLabel() calls the dotted Bedrock/Vertex prefix normalizer, which lives
// just above it with two Sets it closes over. Pull all three in, or the eval'd
// function ReferenceErrors on the first dotted id.
const _stripStart = ui.indexOf('const _BEDROCK_REGION_PREFIXES');
const _stripEnd = ui.indexOf('function getModelLabel(');
if (_stripStart < 0 || _stripStart > _stripEnd) throw new Error('_stripDottedModelPrefix block not found');
eval(ui.slice(_stripStart, _stripEnd));
eval(fnSrc);
const out = {};
for (const m of JSON.parse(process.argv[2])) out[m] = getModelLabel(m);
process.stdout.write(JSON.stringify(out));
"""


def _labels(model_ids, catalog=None):
    argv = [NODE, "-e", _DRIVER, str(UI_JS_PATH), json.dumps(model_ids)]
    if catalog is not None:
        argv.append(json.dumps(catalog))
    proc = subprocess.run(
        argv,
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    return json.loads(proc.stdout)


def test_custom_model_label_keeps_colon_tag_suffixes():
    """#7240: the full configured model id is the label — a trailing ``:tag``
    inside the model name must never become the whole label."""
    out = _labels([
        # Repro ids from the issue: tag/variant suffix after the model name.
        "@custom:omni:kg/stepfun/step-3.7-flash:free",
        "@custom:omni:kg/tencent/hy3:free",
        "@custom:omni:ollamacloud/gemma4:31b",
        "@custom:omni:ollamacloud/qwen3.5:397b",
        # Non-colon model names must keep working.
        "@custom:omni:gemini/gemini-3.1-flash-lite",
        "@custom:ai_gateway:Qwen3.6-35B-A3B",
    ])
    assert out["@custom:omni:kg/stepfun/step-3.7-flash:free"] == "kg/stepfun/step-3.7-flash:free"
    assert out["@custom:omni:kg/tencent/hy3:free"] == "kg/tencent/hy3:free"
    assert out["@custom:omni:ollamacloud/gemma4:31b"] == "ollamacloud/gemma4:31b"
    assert out["@custom:omni:ollamacloud/qwen3.5:397b"] == "ollamacloud/qwen3.5:397b"
    assert out["@custom:omni:gemini/gemini-3.1-flash-lite"] == "gemini/gemini-3.1-flash-lite"
    assert out["@custom:ai_gateway:Qwen3.6-35B-A3B"] == "Qwen3.6-35B-A3B"


def test_custom_model_label_plain_lane_and_host_port_slugs():
    """#7240 regression guards: the plain custom lane (no provider slug) keeps
    the whole remainder, and an endpoint-authority slug (``host:port``) is
    consumed as provider plumbing rather than leaking into the label."""
    out = _labels([
        # Plain custom lane: '@custom:<model>' with no slug separator.
        "@custom:qwen397b-64k",
        # Endpoint-style slug: ':port' belongs to the provider segment.
        "@custom:10.8.71.41:8080:Qwen3-235B",
        "@custom:localhost:11434:llama3",
        # A digit-leading model after a NAMED slug is not a port — must not be
        # peeled as if the slug were host:port.
        "@custom:omni:11434:Qwen3",
    ])
    assert out["@custom:qwen397b-64k"] == "qwen397b-64k"
    assert out["@custom:10.8.71.41:8080:Qwen3-235B"] == "Qwen3-235B"
    assert out["@custom:localhost:11434:llama3"] == "llama3"
    assert out["@custom:omni:11434:Qwen3"] == "11434:Qwen3"


def test_plain_custom_lane_colon_bearing_model_slash_segment():
    """#7240: a plain-lane ``@custom:<model-with-colon>`` whose first segment
    is slash-bearing (`ollamacloud/qwen3.5:397b`) is the model itself — a
    provider slug never contains a `/`, so it must not be peeled as one."""
    out = _labels([
        "@custom:ollamacloud/qwen3.5:397b",
        "@custom:ollamacloud/gemma4:31b",
        "@custom:some-vendor/step-3.7-flash:free",
    ])
    assert out["@custom:ollamacloud/qwen3.5:397b"] == "ollamacloud/qwen3.5:397b"
    assert out["@custom:ollamacloud/gemma4:31b"] == "ollamacloud/gemma4:31b"
    assert out["@custom:some-vendor/step-3.7-flash:free"] == "some-vendor/step-3.7-flash:free"


def test_catalog_label_wins_over_legacy_peel():
    """Post-hydration: the catalog ships the exact backend label per routing
    id, so a catalogued id renders verbatim even where the string peel alone
    would be ambiguous or lossy."""
    catalog = {
        # Plain lane, colon-bearing model: the backend knows this is ONE model.
        "@custom:ollamacloud/qwen3.5:397b": "ollamacloud/qwen3.5:397b",
        # Named lane, same native model: distinct routing id, distinct entry.
        "@custom:omni:ollamacloud/qwen3.5:397b": "ollamacloud/qwen3.5:397b",
        # A custom display label is preserved exactly.
        "@custom:ai_gateway:Qwen3.6-35B-A3B": "My Qwen Gateway",
    }
    out = _labels(list(catalog), catalog=catalog)
    assert out["@custom:ollamacloud/qwen3.5:397b"] == "ollamacloud/qwen3.5:397b"
    assert out["@custom:omni:ollamacloud/qwen3.5:397b"] == "ollamacloud/qwen3.5:397b"
    assert out["@custom:ai_gateway:Qwen3.6-35B-A3B"] == "My Qwen Gateway"


def test_collision_labels_stay_distinct_by_full_routing_id():
    """Two providers exposing the same native model stay distinguishable: the
    catalog keys labels by the FULL routing id, never by the bare model."""
    catalog = {
        "@custom:omni:ollamacloud/qwen3.5:397b": "ollamacloud/qwen3.5:397b (omni)",
        "@custom:ai_gateway:ollamacloud/qwen3.5:397b": "ollamacloud/qwen3.5:397b (gateway)",
    }
    out = _labels(list(catalog), catalog=catalog)
    assert out["@custom:omni:ollamacloud/qwen3.5:397b"] == "ollamacloud/qwen3.5:397b (omni)"
    assert out["@custom:ai_gateway:ollamacloud/qwen3.5:397b"] == "ollamacloud/qwen3.5:397b (gateway)"
    assert out["@custom:omni:ollamacloud/qwen3.5:397b"] != out["@custom:ai_gateway:ollamacloud/qwen3.5:397b"]
