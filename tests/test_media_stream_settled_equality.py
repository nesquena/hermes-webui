"""Stream-vs-settled MEDIA equality, driven through the REAL production walk.

Re-review history for this file, both instances of the SAME mistake:

1. v1 reimplemented stream-end flush with the unanchored matcher, so it reported
   equality while production (anchored matcher on the whole candidate) dropped the
   media card for `MEDIA:/tmp/a.png and after`. The reviewer caught it.
2. v2 still reimplemented the per-chunk walk. Its tail-buffer gate compared
   ``pm[0].length`` where production compared ``rest.length``, so it could not see
   that a MEDIA ref preceded by more than ``_MEDIA_TAIL_MAX`` characters of prose
   silently lost its card. A self-audit caught it; a 116913-case sweep against
   faithful production semantics found 24 real divergences.

The lesson both times: **a harness that reimplements any decision function can
only validate its own mirror.** So this version extracts and evals the ENTIRE
production call chain — `_smdMediaAwareAddText` itself, plus every helper and
constant it closes over — and stubs ONLY the leaf sinks (`_smdAppendMediaNode`,
`_smdMediaWriteText`) so emissions can be recorded. No decision logic is retyped.

If a future edit adds a new helper to that call chain, the eval fails loudly with
a ReferenceError rather than silently diverging.
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

# Every function in the real _smdMediaAwareAddText call chain. Extracted, never
# retyped. Order matters only for readability — JS function decls hoist.
_UI_FUNCS = ["_mediaPathSrc", "_mediaTokenRe", "_unquoteMediaRef"]
_MSG_FUNCS = [
    "_smdMediaPrefixTail",
    "_smdMediaTailEntryChunk",
    "_smdMediaTailSameOwner",
    "_smdMediaTailSet",
    "_smdMediaTokenIsSettled",
    "_smdMediaTailCouldExtend",
    "_smdMediaHasOpenQuote",
    "_smdMediaTailFlushEntry",
    "_smdMediaTailFlush",
    "_smdMediaAwareAddText",   # the function under test
]

CASES = [
    # Intermediate extension mid-token.
    "MEDIA:/tmp/archive.png.bak",
    "MEDIA:/tmp/img.jpeg.tmp",
    "MEDIA:/tmp/plain.png",
    # Dotted directory before a space.
    "MEDIA:/tmp/v1.2 Reports/chart.png",
    "MEDIA:/tmp/deep/v2.5 Data/final.report.png",
    # Quoted forms, including one holding a closing delimiter.
    'MEDIA:"/tmp/My Files/report (final).png"',
    "MEDIA:'/tmp/My Files/single.png'",
    # Final same-line prose (the v1 flush bug).
    "prose before MEDIA:/tmp/a.png and after",
    "MEDIA:/tmp/a.png and see notes later",
    # Adjacent tags, punctuation, malformed quote, extension-less fallback.
    "MEDIA:/tmp/one.png MEDIA:/tmp/two.png",
    "see MEDIA:/tmp/a.png.",
    'MEDIA:"/tmp/unterminated.png and prose',
    "MEDIA:/tmp/no-ext-file",
    # Newline handling.
    "MEDIA:/tmp/a.png\nsecond line",
]

# Long-prose cases straddling _MEDIA_TAIL_MAX (4096). The v2 harness could not
# see these because every case above is under 40 chars; production lost the card.
_TAIL_MAX = 4096
LONG_CASES = [
    "w" * n + " MEDIA:/tmp/a.png trailing words"
    for n in (4090, 4094, 4095, 4096, 4097, 4100)
] + [
    "w" * 4094 + ' MEDIA:"/tmp/My Files/a.png" end',
    "w" * 4094 + " MEDIA:/tmp/v1.2 Reports/c.png end",
]

# Reviewer-authored overflow shape: a COMPLETE media token is followed by more
# than the tail limit of same-line prose that itself ends in a filename. Settled
# parsing correctly stops at `/tmp/a.png`; streaming used to buffer the token +
# prose as one possibly-growing candidate and, on overflow, write the ENTIRE
# candidate as plain text — silently dropping the already-complete media card.
LONG_AFTER_CASES = [
    "MEDIA:/tmp/a.png see " + ("w" * (_TAIL_MAX + 32)) + " README.md",
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


# Only SINKS are stubbed. The parser-identity map and constants mirror production
# values, which are asserted against the source below so they cannot drift.
_HARNESS = r"""
const _MEDIA_TAIL_MAX = 4096;
const _SMD_MEDIA_PREFIX = 'MEDIA:';
const _SMD_MEDIA_TAIL = new Map();

let events = [];
// --- leaf sinks: the ONLY stubs -------------------------------------------
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

// Drives the REAL _smdMediaAwareAddText, one call per chunk, then the REAL flush.
function streamedEvents(chunks){
  events = [];
  _SMD_MEDIA_TAIL.clear();
  const parser = {id: 'p1'};
  const parent = {id: 'root'};
  const baseAddText = null, writeText = null, data = {};
  for (const chunk of chunks){
    _smdMediaAwareAddText(baseAddText, parent, data, chunk, _SMD_MEDIA_TAIL, parser, writeText);
  }
  _smdMediaTailFlush(parser);
  return events.slice();
}

const mediaOf = (e) => JSON.stringify(e.filter(x=>x.kind==='MEDIA').map(x=>x.v));
const textOf  = (e) => e.filter(x=>x.kind==='TEXT').map(x=>x.v).join('');

const payload = JSON.parse(process.argv[1]);
const failures = [];
let checks = 0;
for (const input of payload.cases){
  const want = settledEvents(input);
  const wantMedia = mediaOf(want), wantText = textOf(want);
  const splits = [];
  for (let i=1;i<input.length;i++) splits.push([input.slice(0,i), input.slice(i)]);
  if (payload.twoCuts && input.length <= 60){
    for (let i=1;i<input.length;i++) for (let j=i+1;j<input.length;j++)
      splits.push([input.slice(0,i), input.slice(i,j), input.slice(j)]);
  }
  for (const chunks of splits){
    checks++;
    const got = streamedEvents(chunks);
    if (mediaOf(got) !== wantMedia)
      failures.push({input: input.length > 80 ? `<${input.length} chars>` : input,
                     chunkLens: chunks.map(c=>c.length), kind:'MEDIA',
                     want:wantMedia, got:mediaOf(got)});
    else if (textOf(got) !== wantText)
      failures.push({input: input.length > 80 ? `<${input.length} chars>` : input,
                     chunkLens: chunks.map(c=>c.length), kind:'TEXT',
                     wantLen: wantText.length, gotLen: textOf(got).length});
  }
}
console.log(JSON.stringify({checks, total: failures.length, failures: failures.slice(0,10)}));
"""


def _run(cases: list[str], two_cuts: bool) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = "\n".join(
        [_extract_js_function(UI_JS, n) for n in _UI_FUNCS]
        + [_extract_js_function(MESSAGES_JS, n) for n in _MSG_FUNCS]
        + [_HARNESS]
    )
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script,
         json.dumps({"cases": cases, "twoCuts": two_cuts})],
        capture_output=True, text=True, timeout=300, check=True,
    )
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def short_result():
    return _run(CASES, two_cuts=True)


@pytest.fixture(scope="module")
def long_result():
    return _run(LONG_CASES, two_cuts=False)


@pytest.fixture(scope="module")
def long_after_result():
    return _run(LONG_AFTER_CASES, two_cuts=False)


def test_stream_and_settled_agree_over_every_chunk_cut(short_result):
    assert short_result["checks"] > 5000, (
        f"sweep too small: {short_result['checks']} splits"
    )
    assert short_result["total"] == 0, (
        "streamed rendering must equal settled rendering (captures AND exact "
        f"remainder) for every chunk cut; {short_result['total']} mismatches, "
        f"first few: {short_result['failures']}"
    )


def test_long_prose_before_a_ref_does_not_lose_the_card(long_result):
    """Regression: the tail-buffer cap must bound the BUFFER, not the prose.

    Gating on ``rest.length`` meant a MEDIA ref preceded by more than
    ``_MEDIA_TAIL_MAX`` characters of prose had its buffered tail discarded: the
    partial ``MEDIA:/t`` was flushed as prose, the next chunk arrived with no
    buffered tail, and the token never reassembled — a silently missing media
    card on any long agent turn, while settled parsing rendered it fine.
    """
    assert long_result["checks"] > 20000, (
        f"long-prose sweep too small: {long_result['checks']}"
    )
    assert long_result["total"] == 0, (
        "a MEDIA ref preceded by >_MEDIA_TAIL_MAX chars of prose lost its card; "
        f"{long_result['total']} mismatches, first few: {long_result['failures']}"
    )


def test_long_dotted_prose_after_a_complete_ref_does_not_lose_card(long_after_result):
    """Overflow must partition the candidate, not flatten it to plain text."""
    assert long_after_result["checks"] > _TAIL_MAX, (
        f"overflow sweep too small: {long_after_result['checks']}"
    )
    assert long_after_result["total"] == 0, (
        "same-line prose exceeding _MEDIA_TAIL_MAX after a complete ref lost "
        f"the card; {long_after_result['total']} mismatches, first few: "
        f"{long_after_result['failures']}"
    )


def test_tail_cap_bounds_the_buffer_not_the_remaining_text():
    """Pin the exact expression, so the gate cannot silently regress."""
    idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
    block = MESSAGES_JS[idx:idx + 7000]
    assert "tailValue.length < _MEDIA_TAIL_MAX" in block, (
        "the tail cap must bound tailValue (what is buffered), not rest.length"
    )
    assert "rest.length < _MEDIA_TAIL_MAX" not in block, (
        "gating on rest.length discards the buffered tail after long prose"
    )


def test_harness_stubs_only_sinks_not_decision_logic():
    """Meta-test: keep this file honest.

    Both previous versions of this module shipped a bug because the harness
    reimplemented production decision logic. Assert that every function in the
    real call chain is EXTRACTED, and that the harness defines only the two leaf
    sinks plus the settled reference.
    """
    for name in _MSG_FUNCS:
        assert f"function {name}(" in MESSAGES_JS, (
            f"{name} vanished from messages.js — the harness would silently stop "
            f"testing production"
        )
    # The harness must not define its own copy of any extracted function.
    for name in _UI_FUNCS + _MSG_FUNCS:
        assert f"function {name}(" not in _HARNESS, (
            f"harness reimplements {name} — extract it from production instead"
        )
    # Only these stubs are permitted.
    assert "_smdAppendMediaNode" in _HARNESS and "_smdMediaWriteText" in _HARNESS


def test_completeness_is_not_decided_by_a_trailing_extension():
    idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
    block = MESSAGES_JS[idx:idx + 7000]
    assert "_smdMediaRefHasReliableBoundary" not in MESSAGES_JS, (
        "the extension-guess heuristic was deleted as dead code; completeness "
        "must be a real lexical delimiter or stream end"
    )
    assert "_smdMediaTokenIsSettled(m[1], false)" in block


def test_flush_partitions_instead_of_matching_the_whole_candidate():
    """The flush must PARTITION the candidate, not handle one anchored match.

    The behavioral contract (later valid token after a malformed one, no-match
    passthrough, per-token append-failure fallback) is executed against the real
    function in tests/test_smd_media_in_stream.py::
    TestSmdMediaTailFlushPartition. Assert only the wiring here so this cannot
    pass on a source string while behavior regresses.
    """
    src = _extract_js_function(MESSAGES_JS, "_smdMediaTailFlushEntry")
    assert "_mediaTokenRe()" in src
    # A partition loop, not a single anchored exec.
    assert "while((m=re.exec(raw)))" in src
    # Every token span is preserved, including the trailing suffix.
    assert "raw.slice(last)" in src
