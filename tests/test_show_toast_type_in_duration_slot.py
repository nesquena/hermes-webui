"""showToast(msg, 'error') must not dismiss the toast instantly.

The signature is showToast(msg, ms, type). 21 call sites in static/panels.js (Kanban, workspace, MCP, checkpoint
restore) pass the type as the SECOND argument: ms='error' reached setTimeout(fn, 'error'), which fires at once, so
those error toasts flashed and vanished, and only kept error styling when the English text happened to contain an
error keyword. showToast now treats a type name in the ms slot as the type.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def _function(src, name):
    m = re.search(rf"function {name}\(", src)
    assert m, name
    i, depth = src.index("{", m.end()), 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise AssertionError(name)


HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
function extract(re) { const m = src.match(re); if (!m) throw new Error('missing ' + re); return m[0]; }
const timers = [];
global.setTimeout = (fn, ms) => { timers.push(ms); return timers.length; };
global.clearTimeout = () => {};
const el = {className: '', dataset: {}, innerHTML: '', textContent: '', classList: {remove() {}}};
global.$ = (id) => (id === 'toast' ? el : null);
global.esc = (s) => String(s);
global.t = (k) => k;
eval(process.argv[2]);
const out = {};
for (const [label, args] of Object.entries(JSON.parse(process.argv[3]))) {
  timers.length = 0;
  showToast(...args);
  out[label] = {cls: el.className, delay: timers[timers.length - 1]};
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_type_in_the_duration_slot_is_honoured():
    ui = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    consts = "\n".join(re.findall(r"^const TOAST_[A-Z_]+=\d+;$", ui, re.M))
    funcs = "\n".join(_function(ui, n) for n in ("clearToastDismissTimer", "setToastDismissTimer", "showToast"))
    cases = {
        "two_arg_error": ["Kanban backend unavailable: boom", "error"],
        "two_arg_error_no_keyword": ["Kanban-Backend nicht erreichbar", "error"],
        "three_arg_error": ["Something", None, "error"],
        "explicit_ms": ["Saved", 1500],
        "plain": ["Saved"],
        "swapped_info_ms": ["Dispatched 2 tasks", "info", 6000],
        "zero_sentinel_error": ["Worktree failed", 0, "error"],
        "unknown_string_ms": ["Saved", "oops"],
        "numeric_string_ms": ["Failed", "1e3"],
    }
    res = subprocess.run(
        [NODE, "-e", HARNESS, str(REPO / "static" / "ui.js"), consts + "\n" + funcs, json.dumps(cases)],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    error_ms = int(re.search(r"const TOAST_ERROR_DEFAULT_MS=(\d+);", ui).group(1))
    default_ms = int(re.search(r"const TOAST_DEFAULT_MS=(\d+);", ui).group(1))
    for label in ("two_arg_error", "two_arg_error_no_keyword", "three_arg_error"):
        assert out[label]["delay"] == error_ms, (label, out[label])
        assert "error" in out[label]["cls"].split(), (label, out[label])
    assert out["explicit_ms"]["delay"] == 1500
    assert out["plain"]["delay"] == default_ms
    # (msg, 'info', 6000): the Kanban dispatcher result toast passes type and duration swapped.
    assert out["swapped_info_ms"]["delay"] == 6000
    assert "info" in out["swapped_info_ms"]["cls"].split()
    # 0 was the "use the default" sentinel before ms==null replaced ms||2800; it must not hide the toast at once.
    assert out["zero_sentinel_error"]["delay"] == error_ms
    assert "error" in out["zero_sentinel_error"]["cls"].split()
    # Only the four type names are treated as types; other strings keep text-based inference.
    assert "oops" not in out["unknown_string_ms"]["cls"].split()
    assert "success" in out["unknown_string_ms"]["cls"].split()
    assert out["unknown_string_ms"]["delay"] == default_ms
    # A numeric string is still a duration (setTimeout coerces it).
    assert float(out["numeric_string_ms"]["delay"]) == 1000
    assert "error" in out["numeric_string_ms"]["cls"].split()
