"""Regression coverage for #4928 — tool-call content args must not be capped to
120 chars (which corrupts long commands/paths and breaks recovery-rebuilt diffs).

Tool-call args were truncated to 120 chars at two backend points
(`_truncate_tool_args`, the live `_tool_args_snapshot`) and one frontend point
(`_toolArgsSnapshot`). For incidental args that's fine, but content/diff-bearing
keys (command/cmd/script/code/patch/diff/old_string/new_string/content/path/
file_path) feed the rendered tool card AND the diff reconstruction in
recovery-rebuilt sessions, so a 120-char cap silently corrupted them. The fix
exempts those keys with a much larger cap (aligned with the result-snippet cap)
at every site, keyed by key name.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from api.streaming import (
    _TOOL_ARG_CONTENT_CAP,
    _TOOL_ARG_CONTENT_KEYS,
    _truncate_tool_args,
)

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

_LONG_CMD = "echo start\n" + ("x" * 300) + "\necho end"  # 320 chars, multi-line
_LONG_PATCH = "@@ -1 +1 @@\n-" + ("a" * 200) + "\n+" + ("b" * 200)  # ~420 chars


# ---------- backend: _truncate_tool_args ----------

def test_backend_keeps_full_command_arg():
    """A long `command` arg must survive well past 120 chars."""
    out = _truncate_tool_args({"command": _LONG_CMD})
    # Not truncated at 120; retained up to the content cap.
    assert len(out["command"].rstrip(".")) > 120
    assert "echo end" in out["command"], out["command"]


def test_backend_keeps_patch_diff_args():
    """old_string/new_string/patch (diff reconstruction inputs) survive."""
    out = _truncate_tool_args({
        "old_string": "o" * 300,
        "new_string": "n" * 300,
        "patch": _LONG_PATCH,
    })
    assert len(out["old_string"].rstrip(".")) > 120
    assert len(out["new_string"].rstrip(".")) > 120
    assert "@@ -1 +1 @@" in out["patch"]


def test_backend_incidental_arg_still_capped():
    """A non-content arg (e.g. a random label) keeps the short 120 cap."""
    out = _truncate_tool_args({"label": "z" * 300})
    assert out["label"] == "z" * 120 + "..."


def test_backend_content_cap_is_large():
    """The content cap is the larger snippet-aligned bound, not 120."""
    assert _TOOL_ARG_CONTENT_CAP >= 4000
    assert "command" in _TOOL_ARG_CONTENT_KEYS
    assert "old_string" in _TOOL_ARG_CONTENT_KEYS


def test_backend_very_large_content_still_bounded():
    """Storage safety: even content keys are bounded at the (large) content cap."""
    huge = "q" * (_TOOL_ARG_CONTENT_CAP + 5000)
    out = _truncate_tool_args({"command": huge})
    assert out["command"].endswith("...")
    assert len(out["command"]) == _TOOL_ARG_CONTENT_CAP + 3  # cap + '...'


# ---------- frontend: _toolArgsSnapshot ----------

_FE_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const m = src.match(/function _toolArgsSnapshot\([^]*?\n}/m);
if (!m) throw new Error('_toolArgsSnapshot not found');
eval(m[0]);
let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => {
  const payload = JSON.parse(buf || '{}');
  process.stdout.write(JSON.stringify(_toolArgsSnapshot(payload.args || {}, payload.limit)));
});
"""


@pytest.fixture(scope="module")
def fe_driver(tmp_path_factory):
    if NODE is None:
        pytest.skip("node not on PATH")
    p = tmp_path_factory.mktemp("args_snapshot_driver") / "driver.js"
    p.write_text(_FE_DRIVER, encoding="utf-8")
    return str(p)


def _fe_snapshot(fe_driver: str, args: dict) -> dict:
    result = subprocess.run(
        [NODE, fe_driver, str(UI_JS_PATH)],
        input=json.dumps({"args": args}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_frontend_keeps_full_command_arg(fe_driver):
    out = _fe_snapshot(fe_driver, {"command": _LONG_CMD})
    assert len(out["command"].rstrip(".")) > 120
    assert "echo end" in out["command"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_frontend_keeps_patch_args(fe_driver):
    out = _fe_snapshot(fe_driver, {"old_string": "o" * 300, "new_string": "n" * 300})
    assert len(out["old_string"].rstrip(".")) > 120
    assert len(out["new_string"].rstrip(".")) > 120


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_frontend_incidental_arg_still_capped(fe_driver):
    out = _fe_snapshot(fe_driver, {"label": "z" * 300})
    assert out["label"] == "z" * 120 + "..."


# ---------- frontend: transparent Full-tab args render must redact ----------

_DETAIL_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function grab(name){
  const re = new RegExp('function ' + name + '\\([^]*?\\n}', 'm');
  const m = src.match(re);
  if (!m) throw new Error('not found: ' + name);
  return m[0];
}
global.esc = (s) => String(s);   // identity so we can scan the raw value
global._decodeToolLabelEntities = (s) => s;
eval(grab('_redactToolTargetLabel'));
eval(grab('_transparentToolDetailHtml'));
let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => {
  const payload = JSON.parse(buf || '{}');
  process.stdout.write(_transparentToolDetailHtml(payload.tc || {}, 'done'));
});
"""


@pytest.fixture(scope="module")
def detail_driver(tmp_path_factory):
    if NODE is None:
        pytest.skip("node not on PATH")
    p = tmp_path_factory.mktemp("detail_driver") / "driver.js"
    p.write_text(_DETAIL_DRIVER, encoding="utf-8")
    return str(p)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_transparent_full_tab_redacts_secret_in_long_command(detail_driver):
    """#4928 gate: now that content args are retained to 4000 chars, the Full-tab
    args render must redact secrets past char 120 (it renders tc.args directly)."""
    cmd = "echo start\n" + ("x" * 130) + "\nexport OPENAI_API_KEY=sk_LEAKsecret123\necho end"
    result = subprocess.run(
        [NODE, detail_driver, str(UI_JS_PATH)],
        input=json.dumps({"tc": {"name": "shell", "args": {"command": cmd}}}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    html = result.stdout
    assert "sk_LEAKsecret123" not in html, f"secret leaked into Full-tab args: {html[:600]}"
    assert "[redacted]" in html
    # The non-secret structure should still be present (full command retained).
    assert "echo end" in html
