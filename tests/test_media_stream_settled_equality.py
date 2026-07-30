"""Stream-vs-settled MEDIA equality over every chunk cut (PR #6607 re-review).

Reviewer item 2: ``_smdMediaRefHasReliableBoundary()`` finalized a MEDIA token
merely because the current chunk happened to end in a known extension. Splitting
``MEDIA:/tmp/archive.png.bak`` immediately after ``.png`` emitted
``/tmp/archive.png`` during streaming and left ``.bak`` rendered as prose, while
settled parsing consumed the complete ``.bak`` ref.

An extension at the end of the ARRIVED text proves nothing about completeness.
Only a real lexical delimiter (or stream end) does. This module drives the real
extracted production functions under node and asserts the streamed capture list
equals the settled capture list for EVERY 1-cut and 2-cut split of each input —
the property the reviewer asked for, rather than a few hand-picked splits.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")

CASES = [
    # The reviewer's case: an intermediate extension mid-token.
    "MEDIA:/tmp/archive.png.bak",
    "MEDIA:/tmp/img.jpeg.tmp",
    "MEDIA:/tmp/plain.png",
    # Dotted directory before a space.
    "MEDIA:/tmp/v1.2 Reports/chart.png",
    "MEDIA:/tmp/deep/v2.5 Data/final.report.png",
    # Quoted forms, including one holding a closing delimiter.
    'MEDIA:"/tmp/My Files/report (final).png"',
    "MEDIA:'/tmp/My Files/single.png'",
    # Prose either side, and two adjacent tags.
    "prose before MEDIA:/tmp/a.png and after",
    "MEDIA:/tmp/one.png MEDIA:/tmp/two.png",
    # Extension-less fallback.
    "MEDIA:/tmp/no-ext-file",
]


def _extract_js_function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth = 0
    started = False
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
            started = True
        elif src[i] == "}":
            depth -= 1
            if started and depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


_HARNESS = r"""
const _MEDIA_TAIL_MAX = 4096;

function settledCaptures(text){
  const re = _mediaTokenRe();
  const out = [];
  let m;
  while ((m = re.exec(text))) out.push(_unquoteMediaRef(m[1]));
  return out;
}

// Mirrors the buffer/flush decisions in _smdMediaAwareAddText.
function streamedCaptures(chunks){
  const emitted = [];
  let tail = '';
  for (const chunk of chunks){
    const combined = tail + chunk;
    tail = '';
    if (!/MEDIA:/.test(combined)){
      const pm = /(M|ME|MED|MEDI|MEDIA|MEDIA:)$/.exec(combined);
      if (pm) tail = pm[1];
      continue;
    }
    const re = _mediaTokenRe();
    let last = 0, m, unmatchedTail = null;
    while ((m = re.exec(combined))){
      const matchEnd = m.index + m[0].length;
      const trailing = combined.slice(matchEnd);
      const openQuote = _smdMediaHasOpenQuote(combined.slice(m.index));
      const mayGrow = openQuote
        || (matchEnd === combined.length)
        || _smdMediaTailCouldExtend(trailing);
      if (mayGrow && !_smdMediaTokenIsSettled(m[1], false)){
        const candidate = combined.slice(m.index);
        if (candidate.length < _MEDIA_TAIL_MAX) unmatchedTail = candidate;
        last = combined.length;
        break;
      }
      emitted.push(_unquoteMediaRef(m[1]));
      last = matchEnd;
    }
    if (unmatchedTail != null){ tail = unmatchedTail; continue; }
    const rest = combined.slice(last);
    if (rest){
      const pm = /(?:MEDIA:[^\n]*|M|ME|MED|MEDI|MEDIA)$/.exec(rest);
      if (pm && pm[0].length < _MEDIA_TAIL_MAX) tail = pm[0];
    }
  }
  // Stream end: the buffered tail settles, minus any trailing whitespace.
  if (tail){
    const ws = /[^\S\n]+$/.exec(tail);
    const core = ws ? tail.slice(0, tail.length - ws[0].length) : tail;
    const re = _mediaTokenRe();
    let m;
    while ((m = re.exec(core))){
      if (_smdMediaTokenIsSettled(m[1], true)) emitted.push(_unquoteMediaRef(m[1]));
    }
  }
  return emitted;
}

const cases = JSON.parse(process.argv[1]);
const failures = [];
let checks = 0;
for (const input of cases){
  const want = JSON.stringify(settledCaptures(input));
  for (let i = 1; i < input.length; i++){
    checks++;
    const got = JSON.stringify(streamedCaptures([input.slice(0,i), input.slice(i)]));
    if (got !== want) failures.push({input, cut:[i], settled:want, streamed:got});
  }
  for (let i = 1; i < input.length; i++){
    for (let j = i+1; j < input.length; j++){
      checks++;
      const got = JSON.stringify(streamedCaptures([input.slice(0,i), input.slice(i,j), input.slice(j)]));
      if (got !== want) failures.push({input, cut:[i,j], settled:want, streamed:got});
    }
  }
}
console.log(JSON.stringify({checks, failures: failures.slice(0, 20), total: failures.length}));
"""


@pytest.fixture(scope="module")
def equality_result():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = "\n".join([
        _extract_js_function(UI_JS, "_mediaPathSrc"),
        _extract_js_function(UI_JS, "_mediaTokenRe"),
        _extract_js_function(UI_JS, "_unquoteMediaRef"),
        _extract_js_function(MESSAGES_JS, "_smdMediaTokenIsSettled"),
        _extract_js_function(MESSAGES_JS, "_smdMediaTailCouldExtend"),
        _extract_js_function(MESSAGES_JS, "_smdMediaHasOpenQuote"),
        _HARNESS,
    ])
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script, json.dumps(CASES)],
        capture_output=True, text=True, timeout=120, check=True,
    )
    return json.loads(proc.stdout)


def test_stream_and_settled_agree_over_every_chunk_cut(equality_result):
    assert equality_result["checks"] > 4000, (
        "the sweep should cover thousands of splits; "
        f"only {equality_result['checks']} ran"
    )
    assert equality_result["total"] == 0, (
        "streamed rendering must equal settled rendering for every chunk cut; "
        f"{equality_result['total']} mismatches, first few: "
        f"{equality_result['failures']}"
    )


def test_completeness_is_not_decided_by_a_trailing_extension():
    """Guard the specific regression: the buffer decision must not be 'the
    current chunk ends in a known extension'."""
    idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
    block = MESSAGES_JS[idx:idx + 6500]
    assert "_smdMediaRefHasReliableBoundary(m[1])" not in block, (
        "the streaming finalize decision must not be an extension guess"
    )
    assert "_smdMediaTokenIsSettled(m[1], false)" in block
