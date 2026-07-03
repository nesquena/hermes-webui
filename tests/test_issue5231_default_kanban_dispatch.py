import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function


ROOT = Path(__file__).resolve().parents[1]
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(js: str) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False) as script:
        script.write(js)
        script_path = Path(script.name)
    try:
        proc = subprocess.run(
            [NODE, str(script_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return json.loads(proc.stdout.strip())


def _run_dispatcher_case(function_name: str, current_board, confirm: bool) -> dict:
    fn_source = extract_function(PANELS, function_name, prefix="async function")
    source = f"""
const fnSource = {json.dumps(fn_source)};
eval(fnSource);

let _kanbanCurrentBoard = {json.dumps(current_board)};
let _kanbanIsDispatching = false;
const apiCalls = [];
const confirmCalls = [];
const toastCalls = [];
const showConfirmDialog = async (opts) => {{
  confirmCalls.push(opts);
  return {str(confirm).lower()};
}};
const api = async (path, opts = {{}}) => {{
  apiCalls.push({{ path, method: opts.method || "GET", body: ("body" in opts ? opts.body : null) }});
  return {{
    spawned: 1,
    skipped_unassigned: 0,
    skipped_nonspawnable: 0,
    promoted: 0,
    auto_blocked: 0,
  }};
}};
const showToast = (...args) => {{
  toastCalls.push(args);
}};
const t = (key) => key;
const _setKanbanDispatcherButtonsDisabled = () => {{}};
const loadKanban = async () => {{}};
const _kanbanFormatDispatchResult = () => "ok";
const document = {{
  querySelectorAll: () => [],
}};

(async () => {{
  try {{
    await {function_name}();
    console.log(JSON.stringify({{
      apiCalls,
      confirmCalls,
      toastCalls,
      ok: true,
    }}));
  }} catch (err) {{
    console.error(err && err.stack ? err.stack : String(err));
    process.exit(1);
  }}
}})();
"""
    return _run_node(source)


def test_default_board_real_dispatch_sends_boardless_post_when_confirmed():
    result = _run_dispatcher_case("runKanbanDispatcher", None, True)
    assert result["confirmCalls"], "expected confirmation dialog"
    assert result["apiCalls"] == [
        {"path": "/api/kanban/dispatch?max=8", "method": "POST", "body": None},
    ]


def test_default_board_preview_dispatch_stays_dry_run_and_boardless():
    result = _run_dispatcher_case("nudgeKanbanDispatcher", None, True)
    assert result["confirmCalls"] == []
    assert result["apiCalls"] == [
        {"path": "/api/kanban/dispatch?dry_run=1&max=8", "method": "POST", "body": None},
    ]


def test_named_board_real_dispatch_preserves_board_parameter():
    result = _run_dispatcher_case("runKanbanDispatcher", "qa", True)
    assert result["apiCalls"] == [
        {"path": "/api/kanban/dispatch?max=8&board=qa", "method": "POST", "body": None},
    ]


def test_confirmation_decline_prevents_real_dispatch_post():
    result = _run_dispatcher_case("runKanbanDispatcher", None, False)
    assert result["confirmCalls"], "expected confirmation attempt before dispatch"
    assert result["apiCalls"] == []
