"""Clicking a sidebar chat should leave the caret in the composer.

The shared chokepoint is `_openSidebarSession` (main row, lineage segment,
and child/fork rows). Deep-link / boot `loadSession` must not steal focus.
Touch-primary viewports skip auto-focus so selecting a chat does not pop
the mobile keyboard.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_open_sidebar_session() -> str:
    start = SESSIONS_JS.index("async function _openSidebarSession(session, loadOpts={})")
    end = SESSIONS_JS.index("function _isReadOnlySession", start)
    return SESSIONS_JS[start:end]


def _run_node(source: str) -> dict:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    return json.loads(result.stdout)


def _harness(extra_js: str) -> str:
    fn = _extract_open_sidebar_session()
    return f"""
const events = [];
const composer = {{
  focusCalls: 0,
  lastFocusOpts: null,
  focus(opts) {{
    this.focusCalls += 1;
    this.lastFocusOpts = opts || null;
    events.push('focus');
  }},
}};
function $(id) {{
  return id === 'msg' ? composer : null;
}}
let cancelOpen = false;
let touchKeyboard = false;
function _hermesNotifySessionOpen() {{
  return cancelOpen ? {{cancel: true}} : null;
}}
function closeMobileSidebar() {{ events.push('closeSidebar'); }}
function _isExternalSession() {{ return false; }}
async function _ensureSidebarSessionProfile() {{ events.push('profile'); }}
async function loadSession() {{ events.push('loadSession'); }}
function renderSessionListFromCache() {{ events.push('renderList'); }}
function _isTouchKeyboardViewport() {{ return touchKeyboard; }}
{fn}
{extra_js}
"""


def test_sidebar_open_focuses_composer_after_session_load():
    payload = _run_node(_harness("""
(async () => {
  await _openSidebarSession({session_id: 's1'});
  console.log(JSON.stringify({
    events,
    focusCalls: composer.focusCalls,
    preventScroll: !!(composer.lastFocusOpts && composer.lastFocusOpts.preventScroll),
  }));
})();
"""))
    assert payload["focusCalls"] == 1
    assert payload["events"] == ["closeSidebar", "profile", "loadSession", "renderList", "focus"]
    assert payload["preventScroll"] is True


def test_sidebar_open_cancel_does_not_focus_composer():
    payload = _run_node(_harness("""
(async () => {
  cancelOpen = true;
  await _openSidebarSession({session_id: 's1'});
  console.log(JSON.stringify({events, focusCalls: composer.focusCalls}));
})();
"""))
    assert payload["focusCalls"] == 0
    assert "focus" not in payload["events"]
    assert "loadSession" not in payload["events"]


def test_sidebar_open_skips_focus_on_touch_keyboard_viewport():
    payload = _run_node(_harness("""
(async () => {
  touchKeyboard = true;
  await _openSidebarSession({session_id: 's1'});
  console.log(JSON.stringify({events, focusCalls: composer.focusCalls}));
})();
"""))
    assert payload["focusCalls"] == 0
    assert payload["events"] == ["closeSidebar", "profile", "loadSession", "renderList"]
