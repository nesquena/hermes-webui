"""Tests for #1707 — single-click on workspace tree filename does nothing.

Background: #1698 fixed a regression where the filename's dblclick rename
handler was unreachable because the row's `el.onclick` (openFile) fired
synchronously on the first click. The fix in #1702 stopped click propagation
on `nameEl` — but that broke single-click activation entirely (#1707):
clicking the filename now does nothing, you have to click the icon or row
whitespace to open the file.

The correct fix preserves both intents:

    let _nameClickTimer = null;
    nameEl.onclick = (e) => {
      e.stopPropagation();
      if (_nameClickTimer) { clearTimeout(_nameClickTimer); _nameClickTimer = null; }
      _nameClickTimer = setTimeout(() => {
        _nameClickTimer = null;
        if (typeof el.onclick === 'function') el.onclick(e);
      }, 300);
    };
    nameEl.ondblclick = (e) => {
      e.stopPropagation();
      if (_nameClickTimer) { clearTimeout(_nameClickTimer); _nameClickTimer = null; }
      // ... existing rename body
    };

Single-click → 300ms debounce → delegates to the row's `el.onclick` (openFile
for files, expand-toggle for directories). Double-click → cancels the pending
timer and triggers rename.

These tests guard the handler shape against regression by static-analyzing
`static/ui.js` and by driving the patched handler through a Node VM.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")


def _read_ui_js() -> str:
    with open(UI_JS_PATH, encoding="utf-8") as f:
        return f.read()


def _name_handler_block() -> str:
    """Return the source between `nameEl.title=t('double_click_rename')` and the
    line that appends nameEl to the row (`el.appendChild(nameEl);`).
    """
    src = _read_ui_js()
    start_marker = "nameEl.title=t('double_click_rename');"
    start = src.find(start_marker)
    assert start >= 0, "nameEl rename tooltip not found in static/ui.js"
    end_marker = "el.appendChild(nameEl);"
    end = src.find(end_marker, start)
    assert end >= 0, "el.appendChild(nameEl) not found after rename tooltip"
    return src[start:end + len(end_marker)]


# ── Source-level regression locks ─────────────────────────────────────────────


class TestNameClickHandlerShape:
    """Static-analysis assertions on the patched handler shape."""

    def test_nameel_onclick_no_longer_pure_stoppropagation(self):
        """The pre-fix shape `nameEl.onclick=(e)=>e.stopPropagation();` swallows
        the click entirely and breaks #1707. The handler must do more than just
        stop propagation — it must defer activation to `el.onclick`.
        """
        block = _name_handler_block()
        assert not re.search(
            r"nameEl\.onclick\s*=\s*\(?\s*e\s*\)?\s*=>\s*e\.stopPropagation\(\)\s*;",
            block,
        ), (
            "nameEl.onclick is pure stopPropagation (the #1707 regression); "
            "it must defer activation to el.onclick after a debounce so single-click "
            "on the filename still opens the file"
        )

    def test_nameel_onclick_uses_settimeout_debounce(self):
        """The fix uses setTimeout to defer activation by ~300ms so dblclick can
        cancel before the row's openFile fires.
        """
        block = _name_handler_block()
        # Find the nameEl.onclick body (balanced braces) and confirm setTimeout appears in it.
        m = re.search(r"nameEl\.onclick\s*=\s*\(?\s*e\s*\)?\s*=>\s*\{", block)
        assert m, "nameEl.onclick assignment not found"
        start = m.end() - 1
        depth = 0
        body = None
        for i in range(start, len(block)):
            c = block[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    body = block[start:i + 1]
                    break
        assert body is not None, "could not find balanced nameEl.onclick body"
        assert "setTimeout" in body, (
            "nameEl.onclick must wrap a setTimeout that defers the row's openFile "
            "by ~300ms so a follow-up dblclick can cancel it. Found body: " + body[:300]
        )
        # The debounce duration must be in the dblclick-detection range (200-500ms).
        delay_m = re.search(r"setTimeout\s*\([^,]+,\s*(\d+)\s*\)", body)
        assert delay_m, "setTimeout call with numeric delay not found in onclick body"
        delay = int(delay_m.group(1))
        assert 200 <= delay <= 500, (
            f"debounce delay should be in dblclick-detection range (200-500ms); got {delay}ms"
        )

    def test_nameel_onclick_delegates_to_row_handler(self):
        """The deferred activation must invoke `el.onclick(...)` (the row's
        single-click handler) rather than calling openFile directly.
        """
        block = _name_handler_block()
        assert re.search(
            r"el\.onclick\s*\(",
            block,
        ), (
            "deferred activation must call el.onclick(...) so files use openFile "
            "and directories use the expand/collapse toggle bound on the row"
        )

    def test_nameel_ondblclick_cancels_pending_timer(self):
        """The dblclick handler must clear the pending click-debounce timer."""
        block = _name_handler_block()
        m = re.search(
            r"nameEl\.ondblclick\s*=\s*\(?\s*e\s*\)?\s*=>\s*\{(.*?)\bif\(item\.type==='dir'",
            block,
            re.DOTALL,
        )
        assert m, "nameEl.ondblclick body not found"
        ondblclick_head = m.group(1)
        assert "clearTimeout" in ondblclick_head, (
            "nameEl.ondblclick must clearTimeout the pending click-debounce timer"
        )

    def test_row_handlers_still_present(self):
        """The row's `el.onclick=async()=>openFile(...)` must still be bound."""
        src = _read_ui_js()
        assert "el.onclick=async()=>openFile(item.path);" in src, (
            "row el.onclick must still bind openFile for files"
        )

    def test_handler_does_not_call_openfile_directly(self):
        """nameEl.onclick should delegate via el.onclick, not call openFile directly."""
        block = _name_handler_block()
        m = re.search(
            r"nameEl\.onclick\s*=\s*\(?\s*e\s*\)?\s*=>\s*\{(.*?)\};",
            block,
            re.DOTALL,
        )
        if m:
            onclick_body = m.group(1)
            assert "openFile(" not in onclick_body, (
                "nameEl.onclick must not call openFile directly — delegate to el.onclick(e)"
            )


# ── Behavioral tests via Node VM ──────────────────────────────────────────────


pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node_with_clicks(click_count: int, dblclick_after_first: bool, item_type: str = "file"):
    """Drive a synthesized click sequence against the patched handler."""
    handler = _name_handler_block()
    payload = {
        "handlerBlock": handler,
        "clickCount": click_count,
        "dblclickAfter": dblclick_after_first,
        "itemType": item_type,
    }
    js = (
        "const params = " + json.dumps(payload) + ";\n"
        + r"""
const handlerBlock = params.handlerBlock;
const clickCount = params.clickCount;
const dblclickAfter = params.dblclickAfter;
const itemType = params.itemType;

let openFileCalled = false;
let dirToggleCalled = false;
let renameInputMounted = false;
let pendingTimerClearedByDblclick = false;

const document = {
  createElement: (tag) => {
    const el = {
      tagName: tag.toUpperCase(),
      className: '', textContent: '', title: '', value: '',
      onclick: null, ondblclick: null, onkeydown: null, onblur: null,
      _appended: [], _parent: null,
      replaceWith(other) { renameInputMounted = true; },
      appendChild(child) { this._appended.push(child); child._parent = this; },
      focus() {}, select() {},
    };
    return el;
  },
};

const nameEl = document.createElement('span');
const el = {
  onclick: itemType === 'file'
    ? (() => { openFileCalled = true; })
    : (() => { dirToggleCalled = true; }),
  appendChild() {},
};
const item = { type: itemType, path: 'foo/bar.md', name: 'bar.md' };
const S = { session: { session_id: 'sess-1' }, _expandedDirs: new Set(), _dirCache: {}, currentDir: '.' };
const t = (key) => key;
const loadDir = () => {};
const showToast = () => {};
const api = async () => ({});
const setTimeout_ = setTimeout;
const clearTimeout_ = clearTimeout;

let scheduledTimerId = null;
const trackedSetTimeout = (cb, ms) => {
  scheduledTimerId = setTimeout_(cb, ms);
  return scheduledTimerId;
};
const trackedClearTimeout = (id) => {
  if (id === scheduledTimerId) pendingTimerClearedByDblclick = true;
  clearTimeout_(id);
};

const runner = new Function(
  'nameEl', 'el', 'item', 'S', 't', 'loadDir', 'document', 'showToast', 'api', 'window',
  'setTimeout', 'clearTimeout',
  '(()=>{' + handlerBlock + '})();'
);
runner(nameEl, el, item, S, t, loadDir, document, showToast, api, {}, trackedSetTimeout, trackedClearTimeout);

const evt = { stopPropagation: () => {} };
for (let i = 0; i < clickCount; i++) {
  if (typeof nameEl.onclick === 'function') nameEl.onclick(evt);
}
if (dblclickAfter && typeof nameEl.ondblclick === 'function') {
  nameEl.ondblclick(evt);
}

setTimeout_(() => {
  console.log(JSON.stringify({
    openFileCalled,
    dirToggleCalled,
    renameInputMounted,
    pendingTimerClearedByDblclick,
  }));
}, 450);
"""
    )
    r = subprocess.run(
        [NODE, "-e", js],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestNameClickBehavior:
    """End-to-end behavioral tests against the patched handler in a Node VM."""

    def test_single_click_opens_file_after_debounce(self):
        """Single click on a FILE name → after 300ms debounce → openFile fires."""
        out = _run_node_with_clicks(click_count=1, dblclick_after_first=False, item_type="file")
        assert out["openFileCalled"] is True, (
            f"single click on filename must trigger openFile after debounce; got {out}"
        )
        assert out["renameInputMounted"] is False
        assert out["dirToggleCalled"] is False

    def test_single_click_toggles_dir_after_debounce(self):
        """Single click on a DIRECTORY name → expand/collapse toggle fires."""
        out = _run_node_with_clicks(click_count=1, dblclick_after_first=False, item_type="dir")
        assert out["dirToggleCalled"] is True, (
            f"single click on directory name must trigger expand/collapse toggle; got {out}"
        )

    def test_dblclick_cancels_pending_open_and_mounts_rename(self):
        """Click → dblclick on a file name → rename input mounts, openFile does NOT fire."""
        out = _run_node_with_clicks(click_count=1, dblclick_after_first=True, item_type="file")
        assert out["renameInputMounted"] is True, (
            f"dblclick on filename must mount rename input; got {out}"
        )
        assert out["openFileCalled"] is False, (
            f"dblclick on filename must cancel the pending openFile debounce; got {out}"
        )
        assert out["pendingTimerClearedByDblclick"] is True, (
            f"dblclick must clearTimeout the pending click debounce; got {out}"
        )
