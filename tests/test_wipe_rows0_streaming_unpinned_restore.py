"""Regression tests for the mobile/desktop scroll jump-back caused by the
renderMessages wipe-and-rebuild collapsing #msgInner to zero rows mid-stream
(the "wipe-rows0" scrollHeight-collapse -> browser scrollTop CLAMP class).

Root cause (reproduced on desktop Chromium AND matching real-device telemetry):
renderMessages() rebuilds the transcript by `inner.innerHTML=''` then re-appending
rows. That wipe collapses #msgInner to the empty-table height, so the browser is
FORCED to clamp #messages.scrollTop down to the new (near-zero) maximum -- a
browser primitive, no JS writes the scrollTop (telemetry tags it JS=none, and it
is unaffected by overflow-anchor). When this rebuild happens while a reader has
scrolled UP into history (`_messageUserUnpinned`) during an active stream, the
post-render helper `_scrollAfterMessageRender` took the `S.activeStreamId` branch
and called `scrollIfPinned()`, which is a NO-OP for an unpinned reader -- so the
clamped scrollTop was never restored and the reader was stranded at the top.

Fix: in the `S.activeStreamId` branch, when the reader is unpinned and a pre-wipe
scroll snapshot exists (renderMessages captures one whenever _messageUserUnpinned),
restore the snapshot instead of the no-op. Pinned / tail-following readers keep
scrollIfPinned() so live-follow is unchanged.

Each behavioral test is written to FAIL on the known-buggy version (bare
`scrollIfPinned()` with no unpinned-restore) and PASS only on the fixed version.
The pure-string structural test locks the branch order so a future refactor can't
silently drop the restore ahead of the fallback.
"""
import json
import pathlib
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).parent.parent
UI_JS_PATH = ROOT / "static" / "ui.js"
UI_JS = UI_JS_PATH.read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function_body(src: str, name: str) -> str:
    start = src.index(f"function {name}")
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function {name} body not found")


def _run_node(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = pathlib.Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _harness(scroll_helper_body: str, scenario_js: str) -> str:
    """Run the REAL extracted _scrollAfterMessageRender body against a fake DOM +
    instrumented stubs, so we observe WHICH restore path fires. The stubs record
    their calls; the scenario sets the state (unpinned / activeStreamId / snapshot)
    and prints the recorded calls + resulting scrollTop.
    """
    return (
        r"""
'use strict';
// ---- recorded side effects ----
const calls = [];
// ---- fake #messages scroller ----
const el = { scrollTop: 0, scrollHeight: 600, clientHeight: 400 };
function $(id){ return id === 'messages' ? el : null; }
// ---- module-level state the helper closes over ----
let _messageUserUnpinned = false;
let _scrollPinned = true;
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _lastScrollTop = 0;
let _lastMessageClientHeight = 0;
let _nearBottomCount = 0;
const S = { activeStreamId: null };
function performanceNow(){ return 0; }
const performance = { now: performanceNow };
// ---- instrumented stubs for the two restore paths ----
function scrollIfPinned(){ calls.push('scrollIfPinned'); }
function scrollToBottom(){ calls.push('scrollToBottom'); }
function _followMessagesAfterDomReplace(){ calls.push('follow'); return true; }
function _maybeShowNewMessageScrollCue(){ calls.push('newMessageCue'); }
// _restoreMessageScrollSnapshot is what the FIX must call for an unpinned reader.
// Model its real effect: it moves scrollTop back toward the captured snapshot.top.
function _restoreMessageScrollSnapshot(snap){
  calls.push('restoreSnapshot');
  if(snap && typeof snap.top === 'number'){ el.scrollTop = snap.top; }
}
"""
        + scroll_helper_body
        + "\n"
        + scenario_js
    )


SCROLL_HELPER = _function_body(UI_JS, "_scrollAfterMessageRender")


def test_streaming_unpinned_reader_is_restored_not_left_to_scrollifpinned_noop():
    """The exact bug event: mid-stream re-render (S.activeStreamId set) while the
    reader is unpinned, with a pre-wipe snapshot captured. The browser has already
    clamped scrollTop to 0 (modelled). The fixed helper must RESTORE the snapshot
    (moving scrollTop back to 3000) and must NOT fall through to the scrollIfPinned
    no-op. On the buggy version only 'scrollIfPinned' is recorded and scrollTop
    stays clamped at 0.
    """
    scenario = r"""
S.activeStreamId = 'stream-1';
_messageUserUnpinned = true;
_scrollPinned = false;
// Browser already clamped the wipe: reader was at 3000, now stranded at 0.
el.scrollTop = 0;
const snapshot = { top: 3000, userUnpinned: true, pinned: false, bottom: 5200 };
_scrollAfterMessageRender(false, snapshot);
console.log(JSON.stringify({ calls, finalTop: el.scrollTop }));
"""
    m = json.loads(_run_node(_harness(SCROLL_HELPER, scenario)))
    assert "restoreSnapshot" in m["calls"], (
        "an unpinned reader during an active-stream re-render must have their "
        f"pre-wipe viewport restored; recorded calls={m['calls']!r}. On the buggy "
        "version this is 'scrollIfPinned' only (a no-op), leaving the reader "
        "stranded at the clamped top."
    )
    assert m["finalTop"] == 3000, (
        "after restore, scrollTop must return to the reader's pre-wipe position "
        f"(3000), not stay at the browser-clamped 0; got {m['finalTop']}"
    )
    assert "newMessageCue" in m["calls"], (
        "after restoring an unpinned reader mid-stream, the new-message cue must be "
        "shown (consistent with the preserveScroll and idle-unpin restore branches); "
        f"recorded calls={m['calls']!r}"
    )


def test_streaming_pinned_reader_still_follows_via_scrollifpinned():
    """A pinned / tail-following reader (NOT unpinned) during an active stream must
    STILL take scrollIfPinned() -- the fix must not divert the live-follow path.
    """
    scenario = r"""
S.activeStreamId = 'stream-1';
_messageUserUnpinned = false;   // pinned / following the tail
_scrollPinned = true;
el.scrollTop = 200;
const snapshot = { top: 200, userUnpinned: false, pinned: true, bottom: 0 };
_scrollAfterMessageRender(false, snapshot);
console.log(JSON.stringify({ calls, finalTop: el.scrollTop }));
"""
    m = json.loads(_run_node(_harness(SCROLL_HELPER, scenario)))
    assert m["calls"] == ["scrollIfPinned"], (
        "a pinned reader during an active stream must follow via scrollIfPinned() "
        f"only; recorded calls={m['calls']!r}"
    )
    assert "restoreSnapshot" not in m["calls"], (
        "the fix must NOT restore a stale snapshot for a pinned/tail reader"
    )


def test_streaming_unpinned_without_snapshot_falls_back_to_scrollifpinned():
    """Defensive: if no snapshot was captured (e.g. a caller that did not preserve
    scroll), the unpinned-restore is skipped and the branch falls back to the
    existing scrollIfPinned() behavior rather than throwing.
    """
    scenario = r"""
S.activeStreamId = 'stream-1';
_messageUserUnpinned = true;
_scrollPinned = false;
el.scrollTop = 0;
_scrollAfterMessageRender(false, null);
console.log(JSON.stringify({ calls, finalTop: el.scrollTop }));
"""
    m = json.loads(_run_node(_harness(SCROLL_HELPER, scenario)))
    assert m["calls"] == ["scrollIfPinned"], (
        "with no snapshot the branch must fall back to scrollIfPinned(); "
        f"recorded calls={m['calls']!r}"
    )


def test_source_locks_unpinned_restore_precedes_scrollifpinned_fallback():
    """Structural lock: inside the S.activeStreamId branch the unpinned-restore
    guard must appear BEFORE the scrollIfPinned() fallback, or the fallback would
    shadow it and re-introduce the no-op strand.
    """
    helper = SCROLL_HELPER
    stream_idx = helper.index("if(S.activeStreamId){")
    branch = helper[stream_idx:]
    restore_idx = branch.index("_restoreMessageScrollSnapshot(scrollSnapshot);")
    fallback_idx = branch.index("scrollIfPinned();")
    guard_idx = branch.index("if(_messageUserUnpinned && scrollSnapshot){")
    assert guard_idx < restore_idx < fallback_idx, (
        "the unpinned-restore guard + restore call must precede the scrollIfPinned() "
        "fallback inside the S.activeStreamId branch"
    )
