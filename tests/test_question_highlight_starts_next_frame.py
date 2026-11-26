"""The jump highlight starts on the frame after the jump (#5106).

A far jump renders and lays out the whole transcript in the same task as the scroll. A CSS animation started inside
that task takes its clock from the frame that began before it, so on a 240-message session the first painted frame
was already 0.4-0.6 s into the 1.6 s pulse. ``_highlightQuestionRow`` now adds the class on the next animation frame.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UI_JS = REPO / "static" / "ui.js"
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2];
function extract(name) {
  const start = src.search(new RegExp('function\\s+' + name + '\\s*\\('));
  if (start < 0) throw Error('missing ' + name);
  let i = src.indexOf('{', start), depth = 1; i++;
  while (depth && i < src.length) { if (src[i] === '{') depth++; if (src[i] === '}') depth--; i++; }
  return src.slice(start, i);
}
const timers = new Map(); let nextTimer = 1;
global.window = global;
window.setTimeout = (f, ms) => { const id = nextTimer++; timers.set(id, {f, ms}); return id; };
window.clearTimeout = (id) => { timers.delete(id); };
const frames = [];
if (scenario !== 'no-raf') global.requestAnimationFrame = (f) => { frames.push(f); return frames.length; };
const classes = new Set(); let reflows = 0;
const row = {
  classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
  get offsetWidth() { reflows++; return 100; },
};
eval(extract('_highlightQuestionRow'));
const has = () => classes.has('msg-question-highlight');
const flush = () => { while (frames.length) frames.shift()(); };
const fire = () => { const all = [...timers.entries()]; timers.clear(); for (const [, t] of all) t.f(); };
const out = {};
_highlightQuestionRow(row);
out.sync = has();
out.pendingFrames = frames.length;
flush();
out.afterFrame = has();
out.reflowsBeforeAdd = reflows;
out.timerMs = [...timers.values()].map(t => t.ms);
if (scenario === 'repeat') {
  // A second jump to the same row while the first highlight is still on: the first removal timer must not
  // cut the second highlight short.
  _highlightQuestionRow(row);
  out.timersAfterRepeat = timers.size;
  flush();
  out.timersAfterRepeatFrame = timers.size;
  out.afterRepeatFrame = has();
}
fire();
out.afterTimer = has();
_highlightQuestionRow(null);
out.nullFrames = frames.length;
console.log(JSON.stringify(out));
"""


def _run(scenario="default"):
    result = subprocess.run([NODE, "-e", _DRIVER, str(UI_JS), scenario], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
def test_highlight_class_is_added_on_the_next_frame_not_inside_the_jump_task():
    r = _run()
    assert r["sync"] is False, "the class must not be added inside the task that rendered and scrolled"
    assert r["pendingFrames"] == 1
    assert r["afterFrame"] is True
    assert r["reflowsBeforeAdd"] == 1, "the class is re-added after a reflow so the pulse restarts"
    assert r["timerMs"] == [1800]
    assert r["afterTimer"] is False
    assert r["nullFrames"] == 0


@needs_node
def test_repeat_jump_keeps_the_second_highlight_for_its_full_duration():
    r = _run("repeat")
    assert r["timersAfterRepeat"] == 0, "the first highlight's removal timer is cancelled"
    assert r["timersAfterRepeatFrame"] == 1
    assert r["afterRepeatFrame"] is True
    assert r["afterTimer"] is False


@needs_node
def test_without_request_animation_frame_the_highlight_is_applied_immediately():
    r = _run("no-raf")
    assert r["sync"] is True
    assert r["afterTimer"] is False


def test_reduced_motion_keeps_a_static_ring_instead_of_the_pulse():
    block = re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{[^@]*?\.msg-question-highlight \.msg-body \{([^}]*)\}",
        STYLE_CSS,
    )
    assert block, "reduced-motion override for the jump highlight is missing"
    body = block.group(1)
    assert "animation: none;" in body
    assert "box-shadow: 0 0 0 3px var(--message-landing-ring);" in body


# -- One landing cue for every jump (#5106 Fable r3) ------------------------------------------------------------------
# Assistant targets (a search hit inside a reply, "jump to the start of this response") used to land with no cue, and
# the outline panel used its own tint while search/jump-to-question used a faint ring. Every path now uses the
# transcript highlight: the outline's row tint plus the ring, started on the next frame.

_JUMP_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
function extract(name) {
  const start = src.search(new RegExp('(async\\s+)?function\\s+' + name + '\\s*\\('));
  if (start < 0) throw Error('missing ' + name);
  let i = src.indexOf('{', start), depth = 1; i++;
  while (depth && i < src.length) { if (src[i] === '{') depth++; if (src[i] === '}') depth--; i++; }
  return src.slice(start, i);
}
global.window = global;
const highlighted = [], scrolled = [];
const seg = { id: 'seg', getClientRects: () => [1], scrollIntoView: (o) => scrolled.push(['seg', o.block]) };
const row = { id: 'row', scrollIntoView: (o) => scrolled.push(['row', o.block]) };
const container = { querySelectorAll: () => [seg] };
global.$ = (id) => id === 'messages' ? container : null;
global.document = { getElementById: (id) => id === 'msg-user-3' ? row : null };
global._userMessageDomId = (i) => 'msg-user-' + i;
global._cancelBottomSettle = () => {};
global._beginMessageJumpScroll = () => {};
global._messageJumpScrollBehavior = () => 'auto';
global._highlightQuestionRow = (el) => highlighted.push(el.id);
eval(extract('jumpToTurnQuestion'));
(async () => {
  await jumpToTurnQuestion(3, 4);
  const assistant = { highlighted: highlighted.slice(), scrolled: scrolled.slice() };
  highlighted.length = 0; scrolled.length = 0;
  await jumpToTurnQuestion(3, -1);
  console.log(JSON.stringify({ assistant, question: { highlighted, scrolled } }));
})().catch(e => { console.error(e); process.exitCode = 1; });
"""


@needs_node
def test_assistant_jump_target_gets_the_landing_cue():
    result = subprocess.run([NODE, "-e", _JUMP_DRIVER, str(UI_JS)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    r = json.loads(result.stdout)
    assert r["assistant"] == {"highlighted": ["seg"], "scrolled": [["seg", "start"]]}, r
    assert r["question"] == {"highlighted": ["row"], "scrolled": [["row", "center"]]}, r


def test_outline_jump_uses_the_transcript_landing_cue():
    outline = (REPO / "static" / "outline.js").read_text(encoding="utf-8")
    start = outline.index("function _flashRow(row)")
    body = outline[start:outline.index("\n}\n", start)]
    assert "if (typeof _highlightQuestionRow === 'function') { _highlightQuestionRow(row); return; }" in body


def test_landing_cue_tints_the_row_and_keeps_a_static_tint_under_reduced_motion():
    assert re.search(r"\.msg-question-highlight \{\s*animation: message-landing-tint 1\.6s ease-out;\s*\}", STYLE_CSS)
    keyframes = re.search(r"@keyframes message-landing-tint \{([^@]*?)\n\}", STYLE_CSS)
    assert keyframes and "var(--message-landing-tint)" in keyframes.group(1)
    reduced = re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{\s*\.msg-question-highlight \{([^}]*)\}", STYLE_CSS
    )
    assert reduced, "reduced-motion override for the landing tint is missing"
    assert "animation: none;" in reduced.group(1)
    assert "background-color: var(--message-landing-tint);" in reduced.group(1)


# -- Fable r4 follow-ups ----------------------------------------------------------------------------------------------
# The cue has its own tokens derived from --accent (tint ~1.4:1, ring >=3:1 in both themes; one hue in every skin),
# assistant segments land with room for the ring, and the outline panel's far jumps land instantly like the other paths.

def test_landing_cue_tokens_derive_from_accent_with_fallbacks():
    fallback = re.search(r":root \{ --message-landing-tint: ([^;]+); --message-landing-ring: ([^;]+); \}", STYLE_CSS)
    assert fallback and fallback.groups() == ("var(--accent-bg-strong)", "var(--focus-ring)")
    block = STYLE_CSS[STYLE_CSS.index("@supports (color: color-mix(in srgb, red 50%, transparent)) {"):]
    block = block[: block.index("\n}\n") + 2]
    assert "--message-landing-tint: color-mix(in srgb, var(--accent) 30%, transparent);" in block
    assert "--message-landing-ring: var(--accent);" in block
    dark = block[block.index(":root.dark {"):]
    assert "--message-landing-tint: color-mix(in srgb, var(--accent) 16%, transparent);" in dark
    assert "--message-landing-ring: color-mix(in srgb, var(--accent) 70%, transparent);" in dark
    pulse = re.search(r"@keyframes question-highlight-pulse \{([^@]*?)\n\}", STYLE_CSS).group(1)
    assert "var(--message-landing-ring)" in pulse and "--focus-ring" not in pulse


def test_jump_targets_leave_room_for_the_ring():
    assert "#messages [data-msg-idx] { scroll-margin-top: 12px; }" in STYLE_CSS


def test_outline_jump_uses_the_shared_scroll_behavior():
    outline = (REPO / "static" / "outline.js").read_text(encoding="utf-8")
    start = outline.index("const scrollToRow = function()")
    body = outline[start:outline.index("};", start)]
    assert "_messageJumpScrollBehavior(container, row)" in body
    assert "row.scrollIntoView({ block: 'center', behavior: behavior });" in body
    assert "behavior: 'smooth' }" not in body
