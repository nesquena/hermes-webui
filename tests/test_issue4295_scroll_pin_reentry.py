"""Behavioral regression locks for #4295 scroll re-pin handling."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
NODE = shutil.which("node")


def _ui_js() -> str:
    return (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _balanced_block(src: str, brace_start: int) -> str:
    depth = 0
    for i in range(brace_start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace_start + 1 : i]
    raise AssertionError("balanced block not found")


def _scroll_listener_raf_body() -> str:
    src = _ui_js()
    listener_start = src.index("el.addEventListener('scroll'")
    raf_start = src.index("requestAnimationFrame(()=>", listener_start)
    brace_start = src.index("{", raf_start)
    return _balanced_block(src, brace_start)


def _record_non_message_scroll_intent() -> str:
    src = _ui_js()
    start = src.index("function _recordNonMessageScrollIntent")
    end = src.index("function _recentNonMessageScrollIntent", start)
    return src[start:end]


pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_scroll_listener(samples: list[dict[str, int]]) -> dict[str, int | bool | None]:
    payload = {
        "body": _scroll_listener_raf_body(),
        "samples": samples,
    }
    script = (
        "const payload = " + json.dumps(payload) + ";\n"
        + r"""
const step = new Function(
  'el',
  '_lastScrollTop',
  '_nearBottomCount',
  '_scrollPinned',
  '_messageUserUnpinned',
  '_newMessageCueVisible',
  '_programmaticScroll',
  '_cancelBottomSettle',
  '_clearNewMessageScrollCue',
  '_syncScrollToBottomCue',
  '_updateSessionStartJumpButton',
  '_isSessionEndlessScrollEnabled',
  '_messagesTruncated',
  '_loadOlderMessages',
  payload.body + `
return {
  _lastScrollTop,
  _nearBottomCount,
  _scrollPinned,
  _messageUserUnpinned,
};
`
);

let state = {
  _lastScrollTop: 800,
  _nearBottomCount: 0,
  _scrollPinned: true,
  _messageUserUnpinned: false,
};

const noop = () => {};
for (const sample of payload.samples) {
  state = step(
    sample,
    state._lastScrollTop,
    state._nearBottomCount,
    state._scrollPinned,
    state._messageUserUnpinned,
    false,
    false,
    noop,
    noop,
    noop,
    noop,
    () => false,
    false,
    noop
  );
}

console.log(JSON.stringify(state));
"""
    )
    result = subprocess.run(
        [NODE, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout.strip())


class TestScrollPinReentry:
    def test_manual_scroll_back_to_true_bottom_rearms_follow(self):
        state = _run_scroll_listener(
            [
                {"scrollTop": 760, "scrollHeight": 1000, "clientHeight": 200},
                {"scrollTop": 770, "scrollHeight": 1000, "clientHeight": 200},
                {"scrollTop": 780, "scrollHeight": 1000, "clientHeight": 200},
            ]
        )
        assert state["_scrollPinned"] is True, (
            "Reaching the true bottom tail after a manual scroll-up must re-arm auto-follow."
        )
        assert state["_messageUserUnpinned"] is False, (
            "The sticky unpin flag must clear once the reader manually scrolls back to the real bottom."
        )

    def test_near_bottom_proximity_alone_does_not_repin(self):
        state = _run_scroll_listener(
            [
                {"scrollTop": 760, "scrollHeight": 1200, "clientHeight": 200},
                {"scrollTop": 775, "scrollHeight": 1200, "clientHeight": 200},
                {"scrollTop": 790, "scrollHeight": 1200, "clientHeight": 200},
            ]
        )
        assert state["_scrollPinned"] is False, (
            "The reader must stay unpinned inside the 250px near-bottom band until they reach the true bottom tail."
        )
        assert state["_messageUserUnpinned"] is True, (
            "Near-bottom proximity alone must not clear the sticky unpin flag."
        )

    def test_scroll_up_threshold_prevents_jitter_unpin(self):
        record = _record_non_message_scroll_intent()
        assert "e.deltaY< -30" in record or "e.deltaY < -30" in record, (
            "The wheel-intent path must keep the 30px upward threshold to avoid touch jitter false unpins."
        )
