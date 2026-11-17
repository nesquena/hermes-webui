"""Regression test: pinned snapshot restore must target the POST-rebuild tail.

Live activity-scene rebuilds (`renderLiveAnchorActivityScene`, both compact and
transparent paths) run this sequence on every streamed scene update while a
turn is active:

  1. `scrollSnapshot=_captureMessageScrollSnapshot()` — measures `bottom`
     (scrollHeight - scrollTop - clientHeight) BEFORE the rebuild.
  2. Rebuild the worklog/anchor rows (content grows).
  3. `_restoreMessageScrollSnapshotSameFrame(scrollSnapshot)` →
     `_restorePinnedMessageScrollSnapshot` restores the PINNED reader to
     `maxTop - snapshot.bottom` — the PRE-rebuild bottom gap.
  4. `scrollIfPinned()` → `_setMessageScrollToBottom()` snaps to the true
     post-rebuild bottom.

Step 3's target is stale: content grew during the rebuild, so it lands the
viewport short of the tail by exactly the growth delta. When the browser
paints between steps 3 and 4 (they are separate scrollTop writes ~1-3ms apart,
so paint interleaving is common under streaming load), the reader SEES the
viewport jump up then snap back down — the mid-stream bump/bounce reported
Sep 6 2026 (CDP probe: paired `_restorePinnedMessageScrollSnapshot` +
`_setMessageScrollToBottom` writes on every ~300ms scene rebuild, with the
restore landing up to ~40px above the final tail).

Fix contract: a pinned reader's restore target is the post-rebuild tail
(`maxTop`) — clamped so a stale `bottom` can never push the target negative —
which makes the restore idempotent with the follow writer instead of racing it.
The tail-relative contract is preserved: never an absolute `snapshot.top`
restore, never above the old tail for an unpinned reader (unpinned snapshots
are still refused here).

Executed node-VM tests (behavioral) + source guards on all three pinned
restore sites (`_restorePinnedMessageScrollSnapshot`,
`_restoreMessageScrollSnapshot`, `_restoreMessageScrollSnapshotSameFrame`).
"""
import json
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")

NODE_BIN = shutil.which("node") or str(pathlib.Path.home() / ".local/bin/node")
_node_available = pathlib.Path(NODE_BIN).exists()
_node_tests = pytest.mark.skipif(not _node_available, reason="node not available")


def _extract_fn(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    brace = src.find("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start: i + 1]
    raise AssertionError(f"{name} body did not close")


_PINNED_RESTORE_HARNESS = """
const assert = require('assert');
const writes = [];
let stTop = {initial_top};
const el = {{
  get scrollTop(){{ return stTop; }},
  set scrollTop(v){{ writes.push(Math.round(v)); stTop = v; }},
  scrollHeight: {live_scroll_height},
  clientHeight: {client_height},
}};
function $(id){{ return id === 'messages' ? el : null; }}
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _messageUserUnpinned = false;
let _scrollPinned = true;
let _nearBottomCount = 0;
let _lastScrollTop = 0;
let _lastMessageClientHeight = 0;
const performance = {{ now(){{ return 1234; }} }};
function _deferClearProgrammaticScroll(){{ _programmaticScroll = false; }}
{fn_source}
const snapshot = {{
  anchor: null,
  top: {snapshot_top},
  bottom: {snapshot_bottom},
  scrollHeight: {snapshot_scroll_height},
  inputGeneration: 0,
  pinned: true,
  userUnpinned: false,
}};
const restored = _restorePinnedMessageScrollSnapshot(snapshot);
console.log(JSON.stringify({{ restored, writes, finalTop: stTop,
  maxTop: Math.max(0, el.scrollHeight - el.clientHeight) }}));
"""


def _run_pinned_restore(snapshot_bottom, snapshot_scroll_height, live_scroll_height,
                        client_height=1040, snapshot_top=0, initial_top=9000):
    source = _PINNED_RESTORE_HARNESS.format(
        fn_source=_extract_fn(UI_JS, "_restorePinnedMessageScrollSnapshot"),
        snapshot_bottom=snapshot_bottom,
        snapshot_scroll_height=snapshot_scroll_height,
        live_scroll_height=live_scroll_height,
        client_height=client_height,
        snapshot_top=snapshot_top,
        initial_top=initial_top,
    )
    result = subprocess.run([NODE_BIN, "-e", source], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout.strip())


# ── Executed behavioral tests ────────────────────────────────────────────────

@_node_tests
def test_pinned_restore_targets_post_rebuild_tail_not_stale_gap():
    """Content grew during rebuild (10450->10290+1040=11330 maxTop... concretely:
    snapshot.bottom=40 captured pre-rebuild; post-rebuild tail moved 40px lower.
    Restore must land AT the new tail (bottomDistance 0), not 40px short."""
    m = _run_pinned_restore(snapshot_bottom=40, snapshot_scroll_height=10450,
                            live_scroll_height=10290, client_height=1040)
    # maxTop = 10290 - 1040 = 9250. Old code: 9250-40 = 9210 (stale gap).
    assert m["writes"] == [9250], f"expected single write to new tail 9250, got {m['writes']}"
    assert m["restored"] is True


@_node_tests
def test_pinned_restore_zero_bottom_unchanged():
    """bottom=0 snapshots (nothing grew) keep restoring to the tail exactly."""
    m = _run_pinned_restore(snapshot_bottom=0, snapshot_scroll_height=10290,
                            live_scroll_height=10290, client_height=1040)
    assert m["writes"] == [9250]
    assert m["restored"] is True


@_node_tests
def test_pinned_restore_targets_tail_even_when_stale_gap_exceeds_maxtop():
    """Pathological shrink (stale bottom 5000 > new maxTop 1000): the OLD code
    computed maxTop-bottom = -4000, clamped by the outer Math.max(0,...) into a
    JUMP TO THE TOP (scrollTop 0). The fix targets the tail exactly — no jump."""
    m = _run_pinned_restore(snapshot_bottom=5000, snapshot_scroll_height=10000,
                            live_scroll_height=2000, client_height=1000)
    assert m["writes"] == [1000], f"expected tail 1000, got {m['writes']}"
    assert m["restored"] is True


@_node_tests
def test_unpinned_snapshot_still_refused():
    """userUnpinned snapshots must not be restored by the pinned path."""
    source = _PINNED_RESTORE_HARNESS.format(
        fn_source=_extract_fn(UI_JS, "_restorePinnedMessageScrollSnapshot"),
        snapshot_bottom=40, snapshot_scroll_height=10450,
        live_scroll_height=10290, client_height=1040,
        snapshot_top=0, initial_top=9000,
    ).replace("pinned: true,", "pinned: true,").replace("userUnpinned: false,", "userUnpinned: true,")
    result = subprocess.run([NODE_BIN, "-e", source], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    m = json.loads(result.stdout.strip())
    assert m["restored"] is False
    assert m["writes"] == []


# ── Source guards: every pinned restore site targets the post-rebuild tail ──

def test_all_pinned_restore_sites_target_post_rebuild_tail():
    """Every pinned tail-relative restore must target maxTop (the post-rebuild
    tail), and the regular restore path must keep delegating pinned snapshots
    to the pinned helper (single formula owner) — otherwise one rebuild path
    reintroduces the stale-gap bounce."""
    for fn in ("_restorePinnedMessageScrollSnapshot",
               "_restoreMessageScrollSnapshotSameFrame"):
        body = _extract_fn(UI_JS, fn)
        assert "maxTop-Math.max(0,bottom)" not in body, (
            f"{fn} still restores to the stale pre-rebuild bottom gap"
        )
    pinned = _extract_fn(UI_JS, "_restorePinnedMessageScrollSnapshot")
    assert "const target=maxTop;" in pinned, (
        "_restorePinnedMessageScrollSnapshot must target the post-rebuild tail exactly"
    )
    same_frame = _extract_fn(UI_JS, "_restoreMessageScrollSnapshotSameFrame")
    assert "?maxTop" in same_frame.replace(" ", ""), (
        "_restoreMessageScrollSnapshotSameFrame pinned branch must target the tail"
    )
    delegate = _extract_fn(UI_JS, "_restoreMessageScrollSnapshot")
    assert "_restorePinnedMessageScrollSnapshot(snapshot))return;" in delegate.replace(" ", ""), (
        "_restoreMessageScrollSnapshot must delegate pinned snapshots to the pinned helper"
    )


def test_pinned_restore_comment_documents_bounce_contract():
    pinned = _extract_fn(UI_JS, "_restorePinnedMessageScrollSnapshot")
    assert "POST-rebuild tail (maxTop)" in pinned, "restore must document why it targets maxTop"
    same_frame = _extract_fn(UI_JS, "_restoreMessageScrollSnapshotSameFrame")
    assert "target the tail exactly" in same_frame, "same-frame site must document the same contract"


# ── Mutation bite: the old formula must fail the executed test ──────────────

@_node_tests
def test_bite_old_formula_fails_executed_contract():
    """Bite check: reinject the OLD stale-gap formula into the extracted source
    and confirm the executed harness detects the regression. If this 'bite'
    ever passes, the oracle is not actually sensitive to the bug."""
    fn = _extract_fn(UI_JS, "_restorePinnedMessageScrollSnapshot")
    assert "const target=maxTop;" in fn, (
        "production source must have the fix before the bite can be meaningful"
    )
    mutated = fn.replace(
        "const target=maxTop;",
        "const bottom=Number(snapshot.bottom);"
        "const target=Number.isFinite(bottom)?maxTop-Math.max(0,bottom):maxTop;",
    )
    assert mutated != fn, "bite mutation did not change the source"
    harness = _PINNED_RESTORE_HARNESS.format(
        fn_source=mutated,
        snapshot_bottom=40, snapshot_scroll_height=10450,
        live_scroll_height=10290, client_height=1040,
        snapshot_top=0, initial_top=9000,
    )
    result = subprocess.run([NODE_BIN, "-e", harness], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    m = json.loads(result.stdout.strip())
    assert m["writes"] != [9250], (
        "bite failed: old formula reproduced the regression but the harness did not detect it"
    )
