"""Regression coverage for issue #500 session-sidebar virtualization."""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


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
            timeout=10,
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
"""


def test_session_virtual_window_reduces_large_lists_and_tracks_scroll():
    """A 1000-row sidebar should render a bounded slice near scroll position."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
eval(extractFunc('_sessionVirtualWindow'));
const metrics = _sessionVirtualWindow({
  total: 1000,
  scrollTop: 52 * 420,
  viewportHeight: 520,
  itemHeight: 52,
  buffer: 12,
  threshold: 80,
});
console.log(JSON.stringify(metrics));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["virtualized"] is True
    assert 390 <= metrics["start"] <= 420
    assert metrics["start"] < metrics["end"] <= 1000
    assert metrics["end"] - metrics["start"] <= 40
    assert metrics["topPad"] > 0
    assert metrics["bottomPad"] > 0


def test_session_virtual_window_keeps_active_session_rendered():
    """The active sidebar row must remain in the DOM when we anchor a new active session."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
eval(extractFunc('_sessionVirtualWindow'));
const metrics = _sessionVirtualWindow({
  total: 1000,
  scrollTop: 0,
  viewportHeight: 520,
  itemHeight: 52,
  buffer: 12,
  threshold: 80,
  activeIndex: 995,
});
console.log(JSON.stringify(metrics));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["virtualized"] is True
    assert metrics["start"] <= 995 < metrics["end"]
    assert metrics["end"] - metrics["start"] <= 40


def test_session_list_render_path_uses_virtual_spacers_and_scroll_rerender():
    """renderSessionListFromCache should window rows without stale cached slices."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    render_start = js.index("function renderSessionListFromCache()")
    render_end = js.index("async function _handleActiveSessionStorageEvent", render_start)
    render_body = js[render_start:render_end]

    assert "_sessionVirtualWindow" in render_body
    assert "_sessionVirtualSpacer" in render_body
    assert "spacer.dataset.virtualSpacer=where||'gap'" in js
    assert "list.addEventListener('scroll', _scheduleSessionVirtualizedRender" in js
    assert "requestAnimationFrame(()=>{" in js
    assert "_sessionVirtualScrollRaf=0;" in js
    assert "renderSessionListFromCache();" in js
    assert "const listScrollTopBeforeRender=list.scrollTop||0" in render_body
    assert "scrollTop:listScrollTopBeforeRender" in render_body
    assert "list.scrollTop=listScrollTopBeforeRender" in render_body
    assert "list.dataset.sessionVirtualFilter!==q" in render_body
    assert "list.dataset.sessionVirtualFilter=q" in render_body
    assert "const flatSessionRows=[]" in render_body
    assert "flatSessionRows.push({group:g,session:s})" in render_body

def test_session_list_only_moves_to_active_when_active_row_is_not_visible():
    """Changing filters should not jump the sidebar when active row is already visible."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    render_start = js.index("function renderSessionListFromCache()")
    render_end = js.index("async function _handleActiveSessionStorageEvent", render_start)
    render_body = js[render_start:render_end]

    before_idx = render_body.index("const virtualWindowBeforeActiveAnchor=_sessionVirtualWindow({")
    visible_idx = render_body.index("const activeWasAlreadyVisible=activeIndex>=virtualWindowBeforeActiveAnchor.start&&activeIndex<virtualWindowBeforeActiveAnchor.end")
    move_idx = render_body.index("const shouldMoveSidebarToActive=shouldAnchorActive&&!activeWasAlreadyVisible")
    final_idx = render_body.index("activeIndex:shouldMoveSidebarToActive?activeIndex:-1")
    anchor_idx = render_body.index("if(shouldMoveSidebarToActive&&virtualWindow.virtualized){")

    assert before_idx < visible_idx < move_idx < final_idx < anchor_idx
    assert "activeIndex:-1" in render_body[before_idx:visible_idx]
    assert "activeIndex:shouldAnchorActive?activeIndex:-1" not in render_body
