"""Regression coverage for #5514 + #5515 — transcript scrolls up when the composer
grows while the reader is pinned to the bottom.

#5514 (deterministic): with the chat pinned to the bottom, growing the composer
(multi-line typing, Shift+Enter, a multi-line / WisprFlow paste) shrinks the
`flex:1` `.messages` viewport by the same delta, stranding the reader Δpx above
the bottom — the transcript "scrolls up one row per composer row." autoResize()
resized the textarea but never re-pinned the transcript.

#5515 (intermittent "random scroll up"): the same class of viewport-shrink from
composer growth via paths that don't route through the input->autoResize seam
(paste, draft restore, programmatic value set, reflow) reads to the user as a
random upward jump.

Fix (v2, #5514 root cause): `autoResize()` now snapshots `#messages.scrollTop`
BEFORE its `height:'auto'` → measure → restore round-trip and restores it AFTER,
undoing the transient viewport-grow clamp within the same synchronous task. This
protects BOTH a bottom-pinned reader and a near-bottom reader who scrolled up to
re-read (their exact position is preserved), and — because the poisoning async
scroll event never fires — it stops the clamp from sticky-unpinning the reader
(which had dead-ended the #5516 grow-path re-pin and stream auto-follow). The
grow-gated `_repinMessagesAfterComposerResize()` (static/ui.js) and the
`#composerWrap` ResizeObserver REMAIN for genuine NET growth: they re-pin the
transcript to the bottom ONLY when the reader is still pinned (honoring
`_messageUserUnpinned` / `_scrollPinned`).

This module verifies the static wiring, the helper's guard logic, AND the actual
autoResize() round-trip clamp-and-restore via a node `vm` sandbox.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")


def _function_source(source: str, name: str) -> str:
    start = source.find(f"function {name}(")
    assert start != -1, f"{name} not found"
    open_brace = source.find("{", start)
    depth = 0
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"{name} is not balanced")


def test_single_line_growth_does_not_write_height_in_node_fixture():
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    body = _autoresize_body()
    harness = textwrap.dedent(
        """
        let _composerAutoResizeRaf = 0;
        let _composerLastResizeValue = '';
        let writes = 0;
        const msg = {
          value: 'a',
          offsetHeight: 44,
          scrollHeight: 44,
          style: {
            set height(_value) { writes += 1; },
            get height() { return '44px'; },
          },
        };
        const messages = { scrollTop: 0 };
        const $ = (id) => id === 'msg' ? msg : id === 'messages' ? messages : null;
        const getComputedStyle = () => ({ minHeight: '44px' });
        let sendUpdates = 0;
        function updateSendBtn() { sendUpdates += 1; }
        function _repinMessagesAfterComposerResize() { throw new Error('one-line append must not repin transcript'); }
        %(autoresize)s
        autoResize();
        console.log(JSON.stringify({ writes, lastValue: _composerLastResizeValue, sendUpdates }));
        """
    ) % {"autoresize": body}
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"writes": 0, "lastValue": "a", "sendUpdates": 1}


def test_session_restore_replaces_tall_composer_with_natural_one_line_height():
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    body = _autoresize_body()
    harness = textwrap.dedent(
        """
        let _composerAutoResizeRaf = 0;
        let _composerLastResizeValue = 'a\\nb';
        let height = 150, writes = 0;
        const msg = {
          // Mirrors sessions.js draft restore: a prior multi-line value is
          // replaced directly, then autoResize() runs without an input event.
          value: 'a longer one-line restored draft',
          get offsetHeight() { return height; },
          get scrollHeight() { return height > 44 ? height : 44; },
          style: {
            set height(value) { writes += 1; height = value === 'auto' ? 44 : parseInt(value, 10); },
            get height() { return height + 'px'; },
          },
        };
        const messages = { scrollTop: 0 };
        const $ = (id) => id === 'msg' ? msg : id === 'messages' ? messages : null;
        function updateSendBtn() {}
        function _repinMessagesAfterComposerResize() {}
        %(autoresize)s
        autoResize();
        console.log(JSON.stringify({ writes, height, lastValue: _composerLastResizeValue }));
        """
    ) % {"autoresize": body}
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "writes": 2,
        "height": 44,
        "lastValue": "a longer one-line restored draft",
    }


def test_append_to_an_oversized_composer_remeasures_natural_height():
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    body = _autoresize_body()
    harness = textwrap.dedent(
        """
        let _composerAutoResizeRaf = 0;
        let _composerLastResizeValue = 'short prefi';
        let height = 176, writes = 0;
        const msg = {
          // The content is an append-only update, but the textarea is still
          // taller than the content needs. The fast path must not preserve it.
          value: 'short prefix',
          get offsetHeight() { return height; },
          get scrollHeight() { return height > 44 ? height : 44; },
          style: {
            set height(value) { writes += 1; height = value === 'auto' ? 44 : parseInt(value, 10); },
            get height() { return height + 'px'; },
          },
        };
        const messages = { scrollTop: 0 };
        const $ = (id) => id === 'msg' ? msg : id === 'messages' ? messages : null;
        const getComputedStyle = () => ({ minHeight: '44px' });
        function updateSendBtn() {}
        function _repinMessagesAfterComposerResize() {}
        %(autoresize)s
        autoResize();
        console.log(JSON.stringify({ writes, height, lastValue: _composerLastResizeValue }));
        """
    ) % {"autoresize": body}
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "writes": 2,
        "height": 44,
        "lastValue": "short prefix",
    }


def test_programmatic_one_line_fill_refreshes_primary_button_without_height_writes():
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    auto_resize = _autoresize_body()
    composer_has_content = _function_source(UI_JS, "_composerHasContent")
    primary_action = _function_source(UI_JS, "getComposerPrimaryAction")
    button_icon = _function_source(UI_JS, "_setComposerPrimaryButtonIcon")
    update_send = _function_source(UI_JS, "updateSendBtn")
    harness = textwrap.dedent(
        """
        let _composerAutoResizeRaf = 0;
        let _composerLastResizeValue = '';
        let heightWrites = 0;
        const classes = new Set();
        const btn = {
          dataset: {}, style: {}, disabled: true, title: 'Type a message to send', innerHTML: '',
          classList: {
            toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
            contains(name) { return classes.has(name); },
            add(name) { classes.add(name); },
            remove(name) { classes.delete(name); },
          },
          setAttribute(name, value) { this[name] = value; },
        };
        const msg = {
          value: 'prefilled', disabled: false, offsetHeight: 44, scrollHeight: 44,
          style: { set height(_value) { heightWrites += 1; }, get height() { return '44px'; } },
        };
        const messages = { scrollTop: 0 };
        const $ = (id) => id === 'msg' ? msg : id === 'messages' ? messages : id === 'btnSend' ? btn : null;
        const S = { busy: false, pendingFiles: [] };
        const getComputedStyle = () => ({ minHeight: '44px' });
        const window = { _hasPendingSelections() { return false; } };
        function t(_key) { return _key; }
        function requestAnimationFrame(callback) { callback(); return 1; }
        function _applyBusyComposerPlaceholder() {}
        function _repinMessagesAfterComposerResize() { throw new Error('one-line programmatic fill must not repin transcript'); }
        %(composer_has_content)s
        %(primary_action)s
        %(button_icon)s
        %(update_send)s
        %(auto_resize)s
        // This models URL prefill and other programmatic fills: no DOM input event
        // is dispatched before autoResize() runs.
        autoResize();
        console.log(JSON.stringify({
          heightWrites,
          disabled: btn.disabled,
          action: btn.dataset.action,
          title: btn.title,
          ariaLabel: btn['aria-label'],
        }));
        """
    ) % {
        "composer_has_content": composer_has_content,
        "primary_action": primary_action,
        "button_icon": button_icon,
        "update_send": update_send,
        "auto_resize": auto_resize,
    }
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "heightWrites": 0,
        "disabled": False,
        "action": "send",
        "title": "Send message",
        "ariaLabel": "Send message",
    }


def test_single_line_delete_still_runs_the_height_round_trip_in_node_fixture():
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    body = _autoresize_body()
    harness = textwrap.dedent(
        """
        let _composerAutoResizeRaf = 0;
        let _composerLastResizeValue = 'hello';
        let writes = 0, height = 100;
        const msg = {
          value: 'a',
          get offsetHeight() { return height; },
          scrollHeight: 44,
          style: {
            set height(value) { writes += 1; height = value === 'auto' ? 44 : parseInt(value, 10); },
            get height() { return height + 'px'; },
          },
        };
        const messages = { scrollTop: 0 };
        const $ = (id) => id === 'msg' ? msg : id === 'messages' ? messages : null;
        let sendUpdates = 0;
        function updateSendBtn() { sendUpdates += 1; }
        function _repinMessagesAfterComposerResize() {}
        %(autoresize)s
        autoResize();
        console.log(JSON.stringify({ writes, height, lastValue: _composerLastResizeValue, sendUpdates }));
        """
    ) % {"autoresize": body}
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"writes": 2, "height": 44, "lastValue": "a", "sendUpdates": 1}


def _run_resize_boundary_fixture(*, value: str, previous_value: str, offset_height: int, scroll_height: int):
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    body = _autoresize_body()
    harness = textwrap.dedent(
        """
        let _composerAutoResizeRaf = 0;
        let _composerLastResizeValue = %(previous_value)r;
        let writes = 0, height = %(offset_height)s, repins = 0;
        const msg = {
          value: %(value)r,
          get offsetHeight() { return height; },
          scrollHeight: %(scroll_height)s,
          style: {
            set height(value) { writes += 1; height = value === 'auto' ? 44 : parseInt(value, 10); },
            get height() { return height + 'px'; },
          },
        };
        const messages = { scrollTop: 0 };
        const $ = (id) => id === 'msg' ? msg : id === 'messages' ? messages : null;
        function updateSendBtn() {}
        function _repinMessagesAfterComposerResize() { repins += 1; }
        %(autoresize)s
        autoResize();
        console.log(JSON.stringify({ writes, height, lastValue: _composerLastResizeValue, repins }));
        """
    ) % {
        "previous_value": previous_value,
        "offset_height": offset_height,
        "scroll_height": scroll_height,
        "value": value,
        "autoresize": body,
    }
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_newline_growth_keeps_full_resize_and_repin_path():
    out = _run_resize_boundary_fixture(
        value="a\nb",
        previous_value="a",
        offset_height=44,
        scroll_height=68,
    )
    assert out == {"writes": 2, "height": 68, "lastValue": "a\nb", "repins": 1}


def test_equal_length_replacement_keeps_full_resize_path():
    out = _run_resize_boundary_fixture(
        value="ab",
        previous_value="cd",
        offset_height=44,
        scroll_height=44,
    )
    assert out == {"writes": 2, "height": 44, "lastValue": "ab", "repins": 0}


def _helper_body() -> str:
    start = UI_JS.find("function _repinMessagesAfterComposerResize(")
    assert start != -1, "the _repinMessagesAfterComposerResize helper must exist in ui.js"
    end = UI_JS.find("\nif(typeof window!=='undefined') window._repinMessagesAfterComposerResize", start)
    assert end != -1, "helper must be immediately followed by its window export"
    return UI_JS[start:end]


def test_helper_exists_and_is_pin_guarded():
    body = _helper_body()
    # Never re-pin a reader who scrolled away (sticky-unpin model).
    assert "if(_messageUserUnpinned || !_scrollPinned) return;" in body
    # Uses the canonical bottom-pin primitive.
    assert "_setMessageScrollToBottom" in body
    # Cheap no-op when already at bottom.
    assert "<=1) return;" in body


def _autoresize_body() -> str:
    start = MESSAGES_JS.find("function autoResize(")
    assert start != -1
    end = MESSAGES_JS.find("function scheduleComposerAutoResize(", start)
    assert end > start
    return MESSAGES_JS[start:end]


def test_autoresize_preserves_scrolltop_across_height_roundtrip():
    # #5514 root-cause fix: autoResize() momentarily collapses the textarea to
    # height:'auto' before restoring the measured height. That transient collapse
    # grows the flex:1 transcript viewport and the browser clamps a bottom-anchored
    # scrollTop DOWN. The fix snapshots #messages.scrollTop BEFORE the round-trip
    # and restores it AFTER, undoing the clamp in the same synchronous task (so the
    # poisoning async scroll event never fires and near-bottom UNPINNED readers are
    # protected too, not just pinned ones).
    body = _autoresize_body()
    # The height round-trip must still be there.
    assert "el.style.height='auto'" in body
    assert "el.style.height=Math.min(el.scrollHeight,200)+'px'" in body
    # scrollTop is snapshotted BEFORE the round-trip...
    assert "const _prevScrollTop=" in body
    prev_at = body.find("const _prevScrollTop=")
    auto_at = body.find("el.style.height='auto'")
    assert prev_at != -1 and auto_at != -1 and prev_at < auto_at, (
        "scrollTop must be captured before the height:'auto' collapse"
    )
    # ...and restored AFTER the final height write (undo the transient clamp).
    restore = "if(_msgs&&_msgs.scrollTop!==_prevScrollTop) _msgs.scrollTop=_prevScrollTop;"
    assert restore in body
    write_at = body.find("el.style.height=Math.min(el.scrollHeight,200)+'px'")
    restore_at = body.find(restore)
    assert write_at < restore_at, "scrollTop must be restored AFTER the settled height write"


def test_autoresize_keeps_grow_gated_repin_for_net_growth():
    # The preserve-scrollTop fix handles the transient clamp; genuine NET growth
    # still shrinks the settled viewport, so the grow-gated re-pin must REMAIN to
    # snap a still-pinned reader to the true bottom on a real new row.
    body = _autoresize_body()
    assert "_repinMessagesAfterComposerResize()" in body
    assert "el.offsetHeight>_prevComposerH" in body


def test_resize_observer_installed_on_composer():
    # A ResizeObserver on the composer wrapper re-pins on grow (catches
    # tray/chip/paste height changes, not just the typed-input seam).
    assert "new ResizeObserver(" in BOOT_JS
    assert "_repinMessagesAfterComposerResize" in BOOT_JS
    # Observes the whole wrapper so attach-tray / selection-chip growth is covered.
    assert "$('composerWrap')" in BOOT_JS
    # Only re-pin on GROW (a shrink enlarges the viewport, can't strand a reader).
    assert "can't strand" in BOOT_JS


def test_single_line_growth_skips_the_height_round_trip():
    body = _autoresize_body()
    assert "let _composerLastResizeValue='';" in MESSAGES_JS
    assert "const _isAppendOnly=_nextValue.length>_composerLastResizeValue.length&&_nextValue.startsWith(_composerLastResizeValue);" in body
    assert "const _fitsCurrentHeight=el.scrollHeight<=el.offsetHeight;" in body
    assert "const _minHeightRaw=_isAppendOnly&&_fitsCurrentHeight?getComputedStyle(el).minHeight:'';" in body
    assert "const _minHeight=/^(?:\\d+(?:\\.\\d+)?|\\.\\d+)px$/.test(_minHeightRaw)?parseFloat(_minHeightRaw):NaN;" in body
    # The strict finite-pixel guard rejects a percentage/auto/calc min-height so a
    # bogus parseFloat("50%")===50 can't wrongly enable the fast path (Codex #6349 re-gate).
    assert "parseFloat(getComputedStyle(el).minHeight)" not in body  # old lax parse is gone
    assert "const _isAtMinimumHeight=Number.isFinite(_minHeight)&&el.offsetHeight<=Math.ceil(_minHeight)+1;" in body
    assert "if(_isAppendOnly&&_fitsCurrentHeight&&_isAtMinimumHeight){" in body
    assert "el.style.height='auto'" in body
# ---------------------------------------------------------------------------

def _run(scenario):
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    body = _helper_body()
    harness = textwrap.dedent(
        """
        // Minimal DOM/scroll-state stub reproducing the composer-grow viewport shrink.
        let _messageUserUnpinned = %(unpinned)s;
        let _scrollPinned = %(pinned)s;
        // messages pane: scrollHeight fixed; clientHeight shrinks when composer grows.
        const el = { scrollHeight: 8768, clientHeight: 745, scrollTop: %(scrolltop)s };
        const $ = (id) => (id === 'messages' ? el : null);
        function _messageBottomDistance(){ return el.scrollHeight - el.scrollTop - el.clientHeight; }
        function _setMessageScrollToBottom(){ el.scrollTop = el.scrollHeight - el.clientHeight; el._pinnedCalled = true; }

        %(helper)s

        // Simulate the composer growing by 132px: the viewport shrinks.
        el.clientHeight -= 132;
        _repinMessagesAfterComposerResize();
        const bottomDist = el.scrollHeight - el.scrollTop - el.clientHeight;
        console.log(JSON.stringify({ scrollTop: el.scrollTop, bottomDist, pinnedCalled: !!el._pinnedCalled }));
        """
    ) % {**scenario, "helper": body}
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


def test_pinned_reader_is_repinned_after_composer_grow():
    # Reader pinned to bottom (scrollTop at old max = 8768-745 = 8023), composer
    # grows -> viewport shrinks -> must re-pin so bottomDist returns to 0.
    out = _run({"unpinned": "false", "pinned": "true", "scrolltop": 8023})
    assert out["pinnedCalled"] is True
    assert out["bottomDist"] == 0, f"pinned reader must be re-pinned to the bottom; got {out}"


def test_unpinned_reader_is_not_yanked_down():
    # Reader scrolled up (unpinned). Composer grow must NOT re-pin — no yank.
    out = _run({"unpinned": "true", "pinned": "false", "scrolltop": 3000})
    assert out["pinnedCalled"] is False
    assert out["scrollTop"] == 3000, f"unpinned reader must not be moved; got {out}"


def test_pinned_but_already_at_bottom_is_a_noop():
    # scrollTop already exactly at the (post-shrink) bottom -> nothing to do.
    # scrollHeight 8768, clientHeight after shrink 613 -> bottom scrollTop = 8155.
    out = _run({"unpinned": "false", "pinned": "true", "scrolltop": 8155})
    # bottomDist is already <=1 at call time (8768-8155-613 = 0), so no re-pin call.
    assert out["pinnedCalled"] is False
    assert out["bottomDist"] == 0


# ---------------------------------------------------------------------------
# Behavioral (node vm) — the autoResize() round-trip clamp-and-restore (#5514)
# ---------------------------------------------------------------------------
# This exercises the ACTUAL autoResize() body extracted from messages.js (not
# just the helper), reproducing the transient height:'auto' collapse that grows
# the transcript viewport and clamps a bottom-anchored scrollTop. It is the
# non-vacuous mechanism proof: on the pre-fix (net-growth-gated) autoResize this
# harness leaves the reader stranded; with the preserve-scrollTop fix it does not.

def _run_autoresize(scenario):
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    body = _autoresize_body()
    harness = textwrap.dedent(
        """
        // Model the browser's scroll clamp faithfully: scrollTop is a STORED
        // value. When clientHeight grows (viewport enlarges) the browser lowers
        // the stored scrollTop to the new max (scrollHeight-clientHeight) — and
        // does NOT restore it when clientHeight shrinks back. That persisted
        // downward clamp is exactly the #5514 strand, so clientHeight must be a
        // setter that re-clamps the stored top (a naive read-time getter would
        // silently "heal" the clamp and hide the bug).
        function makeEl(scrollHeight, clientHeight, scrollTop){
          let _top = scrollTop, _ch = clientHeight;
          return {
            scrollHeight,
            get clientHeight(){ return _ch; },
            set clientHeight(v){ _ch = v; if(_top > this.scrollHeight - _ch) _top = this.scrollHeight - _ch; },
            get scrollTop(){ return _top; },
            set scrollTop(v){ _top = Math.max(0, Math.min(v, this.scrollHeight - _ch)); },
          };
        }
        // The composer textarea: height:'auto' collapses it to its 1-row min,
        // which enlarges the messages viewport by (currentHeight - minHeight);
        // restoring the measured height shrinks the viewport back.
        const MIN_H = 44, FULL_H = %(composerH)s, SCROLLH = 8768, BASE_CLIENT = 745;
        // messages viewport at the SETTLED composer height:
        const msgsEl = makeEl(SCROLLH, BASE_CLIENT, %(scrolltop)s);
        let _composerH = FULL_H;
        const msgEl = {
          // offsetHeight tracks the styled height; scrollHeight (content) is FULL_H.
          get offsetHeight(){ return _composerH; },
          scrollHeight: FULL_H,
          style: {
            set height(v){
              if(v === 'auto'){ _composerH = MIN_H; msgsEl.clientHeight = BASE_CLIENT + (FULL_H - MIN_H); }
              else { _composerH = Math.min(parseInt(v,10) || FULL_H, 200);
                     msgsEl.clientHeight = BASE_CLIENT + (FULL_H - _composerH); }
            },
            get height(){ return _composerH + 'px'; },
          },
        };
        const $ = (id) => (id === 'msg' ? msgEl : id === 'messages' ? msgsEl : null);
        let _messageUserUnpinned = %(unpinned)s, _scrollPinned = %(pinned)s;
        let _composerAutoResizeRaf = 0;
        let _composerLastResizeValue = '';
        let _repinCalls = 0;
        function updateSendBtn(){}
        function _messageBottomDistance(){ return msgsEl.scrollHeight - msgsEl.scrollTop - msgsEl.clientHeight; }
        function _setMessageScrollToBottom(){ msgsEl.scrollTop = msgsEl.scrollHeight; }
        function _repinMessagesAfterComposerResize(){
          _repinCalls++;
          if(_messageUserUnpinned || !_scrollPinned) return;
          if(_messageBottomDistance() <= 1) return;
          _setMessageScrollToBottom();
        }

        %(autoresize)s

        // Steady-state keystroke: composer already FULL_H, no NET height change.
        autoResize();
        const bottomDist = msgsEl.scrollHeight - msgsEl.scrollTop - msgsEl.clientHeight;
        console.log(JSON.stringify({ scrollTop: msgsEl.scrollTop, bottomDist, repinCalls: _repinCalls }));
        """
    ) % {**scenario, "autoresize": body}
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


def test_steady_state_keystroke_does_not_strand_pinned_reader():
    # THE #5514 REGRESSION. Pinned at the true bottom, multi-row (164px) composer,
    # a keystroke with NO net height change. Pre-fix (net-growth gate) this left
    # bottomDist == (FULL_H - MIN_H) = 120 (stranded); the preserve-scrollTop fix
    # restores the reader to the bottom within the same task.
    out = _run_autoresize({"unpinned": "false", "pinned": "true", "scrolltop": 8023, "composerH": 164})
    assert out["bottomDist"] == 0, f"pinned reader must stay glued to the bottom; got {out}"


def test_steady_state_keystroke_preserves_near_bottom_unpinned_reader():
    # Fable's residual: a reader who scrolled up a little (sticky-unpinned) to
    # re-read the tail while composing must keep their EXACT position across the
    # round-trip — the re-pin approach would skip them and the transient clamp
    # would still move them. Preserve-scrollTop keeps them put.
    # scrolltop 7990 sits INSIDE the clamp zone: settled bottom = 8768-745 = 8023,
    # transient max (composer collapsed) = 8768-865 = 7903. So on the pre-fix path
    # the transient clamp pulls 7990 -> 7903 (an 87px yank); the fix restores 7990.
    # (Chosen > 7903 so the case genuinely exercises the clamp — a value below the
    # transient max would never clamp and the assertion would be vacuous.)
    out = _run_autoresize({"unpinned": "true", "pinned": "false", "scrolltop": 7990, "composerH": 164})
    assert out["scrollTop"] == 7990, f"near-bottom unpinned reader must not move; got {out}"
    assert out["repinCalls"] == 0, "must not re-pin an unpinned reader"


# ---------------------------------------------------------------------------
# Strict min-height parse (#6349 Codex re-gate): the single-row fast path must
# only fire when getComputedStyle(el).minHeight is a genuine "<number>px". A
# percentage / auto / calc min-height must fail closed to the FULL resize —
# otherwise a bogus parseFloat("50%")===50 would wrongly enable the fast path
# and leave an oversized composer stuck tall.
# ---------------------------------------------------------------------------

def _run_fastpath_minheight(min_height_css, composer_h=50, content_h=44):
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    body = _autoresize_body()
    harness = textwrap.dedent(
        """
        // A composer at offsetHeight=%(composerH)s whose content fits %(contentH)s.
        // An append-only keystroke arrives. The fast path (early return, NO resize)
        // fires only when minHeight parses to a finite px AND offsetHeight is at
        // that minimum. This scenario is chosen so a BUGGY lax parse of a
        // percentage min-height ("50%%" -> parseFloat -> 50) would compute
        // _isAtMinimumHeight = (50 <= ceil(50)+1) = true and WRONGLY skip, leaving
        // the composer stuck at %(composerH)s. The strict /^<number>px$/ parse
        // yields NaN for "50%%", forcing the full resize that shrinks it to %(contentH)s.
        let _composerH = %(composerH)s;
        let _styleHeightWrites = 0;
        const msgEl = {
          value: '',
          get offsetHeight(){ return _composerH; },
          scrollHeight: %(contentH)s,
          style: {
            set height(v){
              _styleHeightWrites++;
              if(v === 'auto'){ _composerH = %(contentH)s; }
              else { _composerH = Math.min(parseInt(v,10) || _composerH, 200); }
            },
            get height(){ return _composerH + 'px'; },
          },
        };
        const msgsEl = { scrollHeight: 1000, clientHeight: 500, scrollTop: 0 };
        const $ = (id) => (id === 'msg' ? msgEl : id === 'messages' ? msgsEl : null);
        function getComputedStyle(){ return { minHeight: %(minheight)r }; }
        let _messageUserUnpinned = false, _scrollPinned = false;
        let _composerAutoResizeRaf = 0;
        let _composerLastResizeValue = '';
        function updateSendBtn(){}
        function _messageBottomDistance(){ return 0; }
        function _setMessageScrollToBottom(){}
        function _repinMessagesAfterComposerResize(){}

        %(autoresize)s

        msgEl.value = 'abc';   // append-only keystroke
        autoResize();
        console.log(JSON.stringify({ offsetHeight: _composerH, styleWrites: _styleHeightWrites }));
        """
    ) % {"minheight": min_height_css, "autoresize": body,
         "composerH": composer_h, "contentH": content_h}
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


def test_percentage_minheight_never_enables_fast_path():
    # THE #6349 Codex re-gate defect, red-before-green. A composer at 50px whose
    # content fits 44px gets an append. With a "50%" min-height, a LAX
    # parseFloat("50%")===50 would read _isAtMinimumHeight = (50 <= 51) = true and
    # WRONGLY skip the resize, leaving the composer stuck at 50px. The strict
    # /^<number>px$/ parse yields NaN, forcing the full resize down to 44px.
    out = _run_fastpath_minheight("50%", composer_h=50, content_h=44)
    assert out["styleWrites"] >= 1, f"percentage min-height must force the full resize; got {out}"
    assert out["offsetHeight"] == 44, f"composer must shrink to its real content height; got {out}"


def test_auto_and_calc_minheight_fail_closed_to_full_resize():
    # auto / empty / calc min-height are equally unparseable as a fixed pixel min
    # and must fail closed to the full resize path.
    for css in ("auto", "", "calc(44px + 2%)"):
        out = _run_fastpath_minheight(css, composer_h=50, content_h=44)
        assert out["styleWrites"] >= 1, f"min-height={css!r} must force full resize; got {out}"
        assert out["offsetHeight"] == 44, f"min-height={css!r}: composer must remeasure; got {out}"


def test_valid_px_minheight_fast_path_still_skips_at_minimum():
    # The optimization must still work: a composer already AT its 44px min-height
    # with a valid "44px" min-height takes the fast path (no style.height write).
    out = _run_fastpath_minheight("44px", composer_h=44, content_h=44)
    assert out["styleWrites"] == 0, f"at-minimum append with valid px min-height must fast-path (no resize write); got {out}"
    assert out["offsetHeight"] == 44
