"""Stream-vs-settled MEDIA equality, driven through the REAL production flush.

Re-review finding: the previous version of this module reimplemented stream-end
flush with the unanchored global matcher, so it reported equality while
production diverged. `_smdMediaTailFlushEntry()` applied the ANCHORED `^...$`
matcher to the whole buffered candidate, which fails when the candidate is a
complete token plus same-line prose (`MEDIA:/tmp/a.png and after`) — production
then wrote the entire string as literal prose, losing the media card, while
settled `renderMd()` rendered the card and preserved ` and after`.

This module now `eval`s the actual production functions and asserts two
properties over every 1-cut and 2-cut chunk split:

1. the emitted media captures equal the settled captures, and
2. the emitted TEXT spans concatenate to exactly the settled text — no dropped,
   duplicated, or invented prose.

A mirrored oracle cannot catch a flush bug. Drive the real thing.
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
    # Intermediate extension mid-token (the original reported case).
    "MEDIA:/tmp/archive.png.bak",
    "MEDIA:/tmp/img.jpeg.tmp",
    "MEDIA:/tmp/plain.png",
    # Dotted directory before a space.
    "MEDIA:/tmp/v1.2 Reports/chart.png",
    "MEDIA:/tmp/deep/v2.5 Data/final.report.png",
    # Quoted forms, including one holding a closing delimiter.
    'MEDIA:"/tmp/My Files/report (final).png"',
    "MEDIA:'/tmp/My Files/single.png'",
    # FINAL SAME-LINE PROSE — the flush-partition case.
    "prose before MEDIA:/tmp/a.png and after",
    "MEDIA:/tmp/a.png and see notes later",
    # Adjacent tags, punctuation, malformed quote, extension-less fallback.
    "MEDIA:/tmp/one.png MEDIA:/tmp/two.png",
    "see MEDIA:/tmp/a.png.",
    'MEDIA:"/tmp/unterminated.png and prose',
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


# The harness stubs only the DOM/text SINKS so emissions can be recorded. Every
# decision function below is the real production source.
_HARNESS = r"""
const _MEDIA_TAIL_MAX = 4096;

let events = [];
function _smdMediaTailEntryChunk(entry){ return entry && entry.chunk ? entry.chunk : ''; }
function _smdAppendMediaNode(parent, rawRef){ events.push({kind:'MEDIA', v:rawRef}); return true; }
function _smdMediaWriteText(parent, data, baseAddText, writeText, chunk){
  if (chunk !== '' && chunk != null) events.push({kind:'TEXT', v:String(chunk)});
}

function settledEvents(text){
  const re = _mediaTokenRe();
  const out = [];
  let m, last = 0;
  while ((m = re.exec(text))){
    if (m.index > last) out.push({kind:'TEXT', v:text.slice(last, m.index)});
    out.push({kind:'MEDIA', v:_unquoteMediaRef(m[1])});
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({kind:'TEXT', v:text.slice(last)});
  return out;
}

function streamedEvents(chunks){
  events = [];
  let tail = '';
  for (const chunk of chunks){
    const combined = tail + chunk;
    tail = '';
    if (!/MEDIA:/.test(combined)){
      const pm = /(M|ME|MED|MEDI|MEDIA|MEDIA:)$/.exec(combined);
      if (pm){
        const stable = combined.slice(0, combined.length - pm[1].length);
        if (stable) _smdMediaWriteText(null,null,null,null,stable);
        tail = pm[1];
      } else if (combined) {
        _smdMediaWriteText(null,null,null,null,combined);
      }
      continue;
    }
    const re = _mediaTokenRe();
    let last = 0, m, unmatchedTail = null;
    while ((m = re.exec(combined))){
      const matchEnd = m.index + m[0].length;
      if (m.index > last) _smdMediaWriteText(null,null,null,null,combined.slice(last, m.index));
      const trailing = combined.slice(matchEnd);
      const openQuote = _smdMediaHasOpenQuote(combined.slice(m.index));
      const mayGrow = openQuote || (matchEnd === combined.length) || _smdMediaTailCouldExtend(trailing);
      if (mayGrow && !_smdMediaTokenIsSettled(m[1], false)){
        const candidate = combined.slice(m.index);
        if (candidate.length < _MEDIA_TAIL_MAX) unmatchedTail = candidate;
        else _smdMediaWriteText(null,null,null,null,candidate);
        last = combined.length;
        break;
      }
      _smdAppendMediaNode({}, _unquoteMediaRef(m[1]));
      last = matchEnd;
    }
    if (unmatchedTail != null){ tail = unmatchedTail; continue; }
    const rest = combined.slice(last);
    if (rest){
      const pm = /(?:MEDIA:[^\n]*|M|ME|MED|MEDI|MEDIA)$/.exec(rest);
      if (pm && pm[0].length < _MEDIA_TAIL_MAX){
        const stable = rest.slice(0, pm.index);
        if (stable) _smdMediaWriteText(null,null,null,null,stable);
        tail = pm[0];
      } else {
        _smdMediaWriteText(null,null,null,null,rest);
      }
    }
  }
  // Stream end goes through the REAL production flush.
  if (tail) _smdMediaTailFlushEntry({chunk: tail, parent:{}, data:{}, baseAddText:null, writeText:null});
  return events.slice();
}

const mediaOf = (evts) => JSON.stringify(evts.filter(e=>e.kind==='MEDIA').map(e=>e.v));
const textOf  = (evts) => evts.filter(e=>e.kind==='TEXT').map(e=>e.v).join('');

const cases = JSON.parse(process.argv[1]);
const failures = [];
let checks = 0;
for (const input of cases){
  const want = settledEvents(input);
  const wantMedia = mediaOf(want), wantText = textOf(want);
  const splits = [];
  for (let i=1;i<input.length;i++) splits.push([input.slice(0,i), input.slice(i)]);
  for (let i=1;i<input.length;i++) for (let j=i+1;j<input.length;j++)
    splits.push([input.slice(0,i), input.slice(i,j), input.slice(j)]);
  for (const chunks of splits){
    checks++;
    const got = streamedEvents(chunks);
    if (mediaOf(got) !== wantMedia)
      failures.push({input, chunks, kind:'MEDIA', want:wantMedia, got:mediaOf(got)});
    else if (textOf(got) !== wantText)
      failures.push({input, chunks, kind:'TEXT', want:wantText, got:textOf(got)});
  }
}
console.log(JSON.stringify({checks, total: failures.length, failures: failures.slice(0,10)}));
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
        # THE function under test — real production source, not a mirror.
        _extract_js_function(MESSAGES_JS, "_smdMediaTailFlushEntry"),
        _HARNESS,
    ])
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script, json.dumps(CASES)],
        capture_output=True, text=True, timeout=180, check=True,
    )
    return json.loads(proc.stdout)


def test_stream_and_settled_agree_over_every_chunk_cut(equality_result):
    assert equality_result["checks"] > 5000, (
        f"sweep too small: {equality_result['checks']} splits"
    )
    assert equality_result["total"] == 0, (
        "streamed rendering must equal settled rendering (captures AND exact "
        f"remainder) for every chunk cut; {equality_result['total']} mismatches, "
        f"first few: {equality_result['failures']}"
    )


def test_flush_partitions_instead_of_matching_the_whole_candidate():
    """Guard the specific regression.

    The buffered candidate can be a token PLUS same-line prose, so the flush must
    partition at offset 0 with the shared matcher and return the exact remainder.
    Matching the whole candidate with the anchored matcher dropped the media card
    and printed the raw `MEDIA:` keyword.
    """
    src = _extract_js_function(MESSAGES_JS, "_smdMediaTailFlushEntry")
    assert "_mediaTokenRe()" in src, (
        "flush must use the shared (unanchored) token grammar so it can partition"
    )
    assert "m.index===0" in src, "flush must require the token at offset 0"
    assert "raw.slice(m[0].length)" in src, (
        "flush must return the exact unmatched remainder"
    )


def test_completeness_is_not_decided_by_a_trailing_extension():
    idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
    block = MESSAGES_JS[idx:idx + 6500]
    assert "_smdMediaRefHasReliableBoundary(m[1])" not in block, (
        "the streaming finalize decision must not be an extension guess"
    )
    assert "_smdMediaTokenIsSettled(m[1], false)" in block
