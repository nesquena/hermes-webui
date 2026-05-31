"""Regression coverage for quiet collapsed tool-card previews.

Collapsed tool rows are transcript metadata.  They should summarize the action
(arguments/status) and keep verbose result JSON inside the expandable detail
body; otherwise long tool-heavy turns visually turn into raw debug logs.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
eval(extractFunc('_toolArgPreviewValue'));
eval(extractFunc('_formatToolArgPreview'));
eval(extractFunc('_toolCardPreviewText'));
let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => {
  const payload = JSON.parse(buf || '{}');
  process.stdout.write(_toolCardPreviewText(payload.tc || {}, payload.displaySnippet || ''));
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("tool_preview_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _preview(driver_path: str, tc: dict, display_snippet: str = "") -> str:
    assert NODE is not None
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        input=json.dumps({"tc": tc, "displaySnippet": display_snippet}),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout


def test_collapsed_tool_header_prefers_argument_summary_over_result_json(driver_path):
    preview = _preview(
        driver_path,
        {
            "name": "search_files",
            "args": {
                "target": "content",
                "pattern": "Hattest du https://github.com/huggingface/speech-to-speech",
                "path": "/tmp/hermes-webui",
                "limit": "20",
            },
            "snippet": '{"total_count": 26, "matches": [{"path": "..."}]}',
            "done": True,
        },
        '{"total_count": 26, "matches": [{"path": "..."}]}',
    )

    assert "pattern=" in preview
    assert "/tmp/hermes-webui" in preview
    assert "total_count" not in preview
    assert "matches" not in preview


def test_collapsed_tool_header_uses_status_when_no_preview_or_args(driver_path):
    preview = _preview(driver_path, {"name": "terminal", "done": True}, "long stdout that belongs in detail")
    assert preview == "Completed"


def test_explicit_progress_preview_still_wins(driver_path):
    preview = _preview(
        driver_path,
        {"name": "terminal", "preview": "Running command", "args": {"command": "pytest"}, "done": False},
        "stdout",
    )
    assert preview == "Running command"
