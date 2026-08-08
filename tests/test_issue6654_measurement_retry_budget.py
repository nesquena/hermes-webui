"""Regression coverage for issue #6654 — virtual measurement retry budget.

The convergence guard in ``_scheduleMessageVirtualMeasurementRefresh()``
must cap re-renders for one measurement burst globally, even when measured
row heights move the virtual window between adjacent cycle keys
(``A -> B -> A -> B``). Before the fix, each cycle-key transition reset
``_messageVirtualMeasurementRetryCount``, so alternating keys bypassed
``MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS`` indefinitely and WebKit kept
running layout from animation-frame callbacks.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_function(js: str, name: str) -> str:
    marker = f"function {name}("
    start = js.find(marker)
    assert start >= 0, f"{name} function not found in static/ui.js"
    brace = js.find("{", start)
    assert brace >= 0, f"{name} opening brace not found"
    depth = 1
    i = brace + 1
    while i < len(js) and depth > 0:
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} function braces unbalanced"
    return js[start:i]


def _run_node(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=REPO_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _burst_result(keys, settle_then_keys=None):
    """Feed alternating virtual-window keys to the scheduler and report
    how many RAFs were scheduled and the final retry counter.

    When ``settle_then_keys`` is given, ``_markMessageVirtualMeasurementsSettled``
    runs between the first key burst and the second one (lifecycle reset check).
    """
    js = UI_JS_PATH.read_text(encoding="utf-8")
    schedule_fn = _extract_function(js, "_scheduleMessageVirtualMeasurementRefresh")
    settle_fn = _extract_function(js, "_markMessageVirtualMeasurementsSettled")
    keys_json = json.dumps(keys)
    second_burst = ""
    if settle_then_keys is not None:
        second_burst = (
            "_markMessageVirtualMeasurementsSettled({key:'settled'});\n"
            f"for (const key of {json.dumps(settle_then_keys)}) {{\n"
            "  _scheduleMessageVirtualMeasurementRefresh({key});\n"
            "}\n"
        )
    source = f"""
const MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS=2;
let _messageVirtualMeasurementCycleKey='';
let _messageVirtualMeasurementRetryCount=0;
let _messageVirtualScrollActive=false;
let _messageVirtualDeferredMeasurement=null;
let rafCount=0;
function _messageVirtualMeasurementCycleKeyFor(windowMetrics){{ return windowMetrics.key; }}
function _scheduleMessageVirtualizedRender(force){{ }}
function requestAnimationFrame(cb){{ rafCount++; }}
{schedule_fn}
{settle_fn}
for (const key of {keys_json}) {{
  _scheduleMessageVirtualMeasurementRefresh({{key}});
}}
{second_burst}
console.log(JSON.stringify({{scheduled: rafCount, retries: _messageVirtualMeasurementRetryCount}}));
"""
    return json.loads(_run_node(source))


def test_retry_budget_caps_alternating_window_keys():
    """Window-key oscillation must not reset the retry budget mid-burst.

    Given MESSAGE_VIRTUAL_MEASUREMENT_MAX_RERENDERS=2 and keys A,B,A,B,
    exactly 2 RAFs must be scheduled and the retry counter must end at 2.
    (Before the fix: 4 scheduled RAFs, retry counter stuck at 1.)
    """
    result = _burst_result(["A", "B", "A", "B"])
    assert result == {"scheduled": 2, "retries": 2}


def test_retry_budget_caps_stable_key_burst():
    """Even with a stable window key, the same 2-render cap applies."""
    result = _burst_result(["A", "A", "A", "A"])
    assert result == {"scheduled": 2, "retries": 2}


def test_retry_budget_resets_when_measurements_settle():
    """The budget must reset at the real lifecycle boundary
    (_markMessageVirtualMeasurementsSettled), not on every key change."""
    result = _burst_result(["A", "B", "A", "B"], settle_then_keys=["C", "D", "C", "D"])
    # First burst: 2 RAFs. After settle, a fresh burst of alternating keys
    # again gets exactly 2 RAFs — total 4, counter back to 2.
    assert result == {"scheduled": 4, "retries": 2}
