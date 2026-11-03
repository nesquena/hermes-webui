"""Sidebar swipe and Ctrl/Cmd+B stay quiet while the user is typing."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

BOOT = (Path(__file__).resolve().parents[1] / "static" / "boot.js").read_text(encoding="utf-8")


def test_swipe_aborts_on_composer_or_message_field():
    start = BOOT.index("function _onPwaSidebarSwipeStart")
    body = BOOT[start : BOOT.index("function _onPwaSidebarSwipeMove", start)]
    assert "function _swipeHitsRect" in BOOT
    assert "_swipeHitsRect(point,'composerWrap')" in body
    assert "_swipeHitsRect(point,'msg')" in body
    assert "_swipeInKeyboardBand" not in BOOT


def _drive_edge_swipes() -> dict:
    """Touch-swipe the real handlers. A fold can report a negative visualViewport."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    start = BOOT.index("const _PWA_SIDEBAR_SWIPE_EDGE")
    source = BOOT[start : BOOT.index("function _onPwaSidebarSwipeEnd", start)]
    script = r"""
function _isDesktopWidth(){ return false; }
function _setPanelInert(){}
function classList(){
  const names = new Set();
  return {
    contains(name){ return names.has(name); },
    add(name){ names.add(name); },
    remove(name){ names.delete(name); },
  };
}
const sidebar = { classList: classList() };
const rects = {
  composerWrap: { left: 0, right: 390, top: 800, bottom: 844 },
  msg: { left: 0, right: 0, top: 0, bottom: 0 },
};
function makeEl(id){
  return { id, getBoundingClientRect(){ return rects[id]; } };
}
const byId = { composerWrap: makeEl('composerWrap'), msg: makeEl('msg') };
globalThis.document = {
  getElementById(id){ return byId[id] || null; },
  querySelector(sel){ return sel === '.sidebar' ? sidebar : null; },
  documentElement: { removeAttribute(){}, classList: { contains(){ return false; } } },
};
globalThis.window = {
  innerHeight: 844,
  visualViewport: { height: 844, offsetTop: 0, scale: 1, width: 390, offsetLeft: 0 },
  matchMedia(){ return { matches: false }; },
  navigator: {},
};
""" + source + r"""
const garbage = { height: -33554432, offsetTop: 9, scale: 1, width: 390, offsetLeft: 0 };
const zoom = { height: 422, offsetTop: 0, scale: 2, width: 195, offsetLeft: 0 };
const ordinary = { height: 844, offsetTop: 0, scale: 1, width: 390, offsetLeft: 0 };
const cases = [
  ['garbage-386', 386, garbage, false],
  ['garbage-63', 63, garbage, false],
  ['garbage-709', 709, garbage, false],
  ['zoom-416', 416, zoom, false],
  ['ordinary-386', 386, ordinary, false],
  ['composer-386', 386, ordinary, true],
];
const out = {};
for (const [id, y, vv, onComposer] of cases) {
  sidebar.classList.remove('mobile-open');
  _pwaSidebarSwipe = null;
  window.visualViewport = vv;
  rects.composerWrap = onComposer
    ? { left: 0, right: 390, top: 300, bottom: 500 }
    : { left: 0, right: 390, top: 800, bottom: 844 };
  const target = { closest(){ return null; } };
  _onPwaSidebarSwipeStart({ touches: [{ clientX: 10, clientY: y }], target });
  _onPwaSidebarSwipeMove({
    touches: [{ clientX: 90, clientY: y }],
    cancelable: true,
    preventDefault(){},
  });
  out[id] = sidebar.classList.contains('mobile-open');
}
console.log(JSON.stringify(out));
"""
    result = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            "swipe harness failed\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return json.loads(result.stdout)


def test_left_edge_swipe_opens_under_garbage_and_zoomed_viewport():
    opened = _drive_edge_swipes()
    for case_id in ("garbage-386", "garbage-63", "garbage-709", "zoom-416", "ordinary-386"):
        assert opened[case_id] is True, case_id
    assert opened["composer-386"] is False
