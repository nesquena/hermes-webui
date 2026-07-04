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

Fix: `_repinMessagesAfterComposerResize()` (static/ui.js) re-pins the transcript
to the bottom ONLY when the reader is genuinely still pinned (honors
`_messageUserUnpinned` / `_scrollPinned` so a reader who scrolled away is never
yanked back). It is called (a) from autoResize() and (b) from a ResizeObserver on
the #composerWrap wrapper that catches every height-change path (typed input,
paste, draft restore, attachment tray / selection chip).

This module verifies BOTH the static wiring and the helper's guard logic via a
node `vm` sandbox that reproduces the pinned-vs-unpinned viewport-shrink scenario.
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


# ---------------------------------------------------------------------------
# Static wiring
# ---------------------------------------------------------------------------

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


def test_autoresize_calls_the_repin():
    start = MESSAGES_JS.find("function autoResize(")
    assert start != -1
    end = MESSAGES_JS.find("function scheduleComposerAutoResize(", start)
    assert end > start
    body = MESSAGES_JS[start:end]
    # The height write must still be there...
    assert "el.style.height=Math.min(el.scrollHeight,200)+'px'" in body
    # ...and the re-pin must be called after it, guarded so it only fires when the
    # composer actually grew (skips the DOM read on a no-height-change keystroke).
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


# ---------------------------------------------------------------------------
# Behavioral (node vm) — the pin guard under a viewport shrink
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
