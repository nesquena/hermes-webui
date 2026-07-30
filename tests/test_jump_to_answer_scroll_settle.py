"""Regression coverage for the first jump-to-answer click after session load."""

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


def _run_node(source: str) -> str:
    assert NODE is not None
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


def _extract_func_script(js: str) -> str:
    return f"""
const src = {js!r};
function extractFunc(name) {{
  const re = new RegExp('(?:async\\\\s+)?function\\\\s+' + name + '\\\\s*\\\\(');
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
"""


def test_first_jump_to_answer_cancels_pending_load_time_bottom_settle():
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + r"""
let _scrollPinned = true;
let _messageUserUnpinned = false;
let _nearBottomCount = 2;
let _bottomSettleToken = 0;
let _settleRAF = 0;
let _settleRO = null;
let _settleTimer = 0;
let _settleFinalTimer = 0;
let targetScrolls = 0;
let snappedBackToBottom = false;

const assistantSegment = {
  getClientRects(){ return [{}]; },
  scrollIntoView(){ targetScrolls += 1; },
};
const container = {
  querySelectorAll(){ return [assistantSegment]; },
};
function $(id){ return id === 'messages' ? container : null; }
function _userMessageDomId(rawIdx){ return 'msg-user-' + rawIdx; }
function _highlightQuestionRow(){}
function _getVisibleMessagesWithIdx(){ throw new Error('visible target should use fast path'); }
function cancelAnimationFrame(){}

eval(extractFunc('_cancelBottomSettle'));
eval(extractFunc('jumpToTurnQuestion'));

(async () => {
  _settleFinalTimer = setTimeout(() => {
    if (_scrollPinned && !_messageUserUnpinned) snappedBackToBottom = true;
  }, 15);

  await jumpToTurnQuestion(4, 5);
  await new Promise(resolve => setTimeout(resolve, 35));

  console.log(JSON.stringify({
    targetScrolls,
    snappedBackToBottom,
    scrollPinned: _scrollPinned,
    messageUserUnpinned: _messageUserUnpinned,
    bottomSettleToken: _bottomSettleToken,
  }));
})().catch(error => {
  console.error(error);
  process.exit(1);
});
"""
    result = json.loads(_run_node(source))

    assert result == {
        "targetScrolls": 1,
        "snappedBackToBottom": False,
        "scrollPinned": False,
        "messageUserUnpinned": True,
        "bottomSettleToken": 1,
    }
