"""Regression tests for sidebar lineage collapse helpers."""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    result = subprocess.run(
        [NODE, "-e", source],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def test_sidebar_lineage_collapse_keeps_latest_tip_and_counts_segments():
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = f"""
const src = {js!r};
function extractFunc(name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_sessionLineageKey'));
eval(extractFunc('_collapseSessionLineageForSidebar'));
const sessions = [
  {{session_id:'root', title:'Hermes WebUI', message_count:10, updated_at:10, last_message_at:10, _lineage_root_id:'root', _lineage_tip_id:'root'}},
  {{session_id:'tip', title:'Hermes WebUI', message_count:20, updated_at:20, last_message_at:20, _lineage_root_id:'root', _lineage_tip_id:'tip'}},
  {{session_id:'solo', title:'Other', message_count:5, updated_at:15, last_message_at:15}},
];
const collapsed = _collapseSessionLineageForSidebar(sessions);
console.log(JSON.stringify(collapsed));
"""
    collapsed = _run_node(source)
    assert '"session_id":"tip"' in collapsed
    assert '"session_id":"root"' not in collapsed
    assert '"_lineage_collapsed_count":2' in collapsed
    assert '"session_id":"solo"' in collapsed
