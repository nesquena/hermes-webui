"""Regression coverage for #3319: pinned chat should recover after DOM rebuilds.

The recovery assertions are behavioral (executed in node against the real
production function bodies): a pinned reader far from the bottom must reach the
true tail through scrollIfPinned()'s settle path without a redundant
pre-write, and the settle path must keep retrying after the next layout frame.
"""
import json
import shutil
import subprocess

import pytest

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")

NODE = shutil.which("node") or str(Path.home() / ".local/bin/node")
_node_available = Path(NODE).exists()
_node_tests = pytest.mark.skipif(not _node_available, reason="node not available")


def _extract_fn(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    brace = src.find("{", start)
    assert brace >= 0, f"{name} body not found"
    depth = 0
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"{name} body did not close")


def test_scroll_to_bottom_retries_after_next_layout_frame():
    fn = _extract_fn(UI_JS, "_setMessageScrollToBottom")
    assert "el.scrollTop=el.scrollHeight;" in fn
    assert "requestAnimationFrame(()=>{" in fn
    assert fn.count("el.scrollTop=el.scrollHeight;") >= 2
    assert fn.count("_lastScrollTop=el.scrollTop;") >= 2


def _run_scroll_if_pinned(*, bottom_distance: int) -> dict:
    """Run the real scrollIfPinned body against mocked collaborators.

    Counts direct tail writes (`_setMessageScrollToBottom`) versus settle-path
    invocations (`_settleMessageScrollToBottom`), so the executed behavior —
    not a source literal — pins the recovery contract.
    """
    fn_src = _extract_fn(UI_JS, "scrollIfPinned")
    script = f"""
let directWrites=0;
let settleCalls=0;
let _scrollPinned=true;
let _messageUserUnpinned=false;
let _nearBottomCount=0;
const window={{_autoScrollFollow:true}};
const _messageBottomDistance=()=>{bottom_distance};
const _recentMessageWheelIntent=()=>false;
const _recentMessageKeyScrollIntent=()=>false;
const _recentMessageTouchScrollIntent=()=>false;
const _recentNonMessageScrollIntent=()=>false;
const _setMessageScrollToBottom=()=>{{directWrites++;}};
const _settleMessageScrollToBottom=()=>{{settleCalls++;}};
{fn_src}
scrollIfPinned();
console.log(JSON.stringify({{directWrites,settleCalls}}));
"""
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@_node_tests
def test_scroll_if_pinned_recovers_when_far_from_bottom():
    """A pinned reader left far above the tail after a DOM rebuild recovers: the
    settle path runs (its sync write + ResizeObserver retry re-anchor the tail)
    exactly once, with no duplicate direct pre-write on the hot path."""
    observed = _run_scroll_if_pinned(bottom_distance=1000)
    assert observed == {"directWrites": 0, "settleCalls": 1}, (
        "scrollIfPinned() must delegate the single initial bottom write to the "
        "settle path — a separate pre-write doubles scrollTop writes in the "
        "streaming hot path (the settle's sync write + observer retry already "
        "anchor and re-anchor the tail)."
    )


@_node_tests
def test_scroll_if_pinned_delegates_near_bottom_recovery_to_settle():
    """Same contract when the rebuild left the reader just past the tail: the
    settle path is the only writer, so recovery never duplicates writes."""
    observed = _run_scroll_if_pinned(bottom_distance=4)
    assert observed == {"directWrites": 0, "settleCalls": 1}
