"""One inclusive candidate ceiling in the streaming walk (PR #6607 re-review).

Re-review blocker: ``_smdMediaAwareAddText()`` carried TWO ceilings.

* The matched-token branch bounded the buffered candidate at
  ``_mediaTokenMaxLength() + _SMD_MEDIA_PREFIX.length`` — 4096 + 6 = 4102.
* The no-match / open-quote tail branch bounded it at a separate
  ``_MEDIA_TAIL_MAX`` of 4096.

A LEGAL quoted capture whose opening quote crosses a chunk boundary reaches the
second branch, because the complete quoted grammar has not matched yet. At
candidate length 4096 the stream flushed it as literal text and lost quote
ownership, so a later closing quote could not reassemble it — while settled
``renderMd()`` still accepted the identical capture through 4096 characters.
Streamed and settled output disagreed about one reference.

Measured at the unmodified head, driving the real ``_smdMediaAwareAddText``: a
4096-code-unit quoted capture lost its card entirely (``[]`` instead of one
media node) at chunk cuts 4100 through 4104, and the whole 4108-character span
was written as prose.

The fix uses ONE inclusive ceiling in every branch, keeps explicit quote and
owner state across chunks, and fails an actually over-limit reference CLOSED so
its suffix cannot activate on its own.

Harness discipline follows tests/test_media_stream_settled_equality.py: the
entire production call chain is extracted from the shipped source and only the
leaf sinks are stubbed. No decision function is retyped, so a rename fails loudly
instead of leaving a mirror passing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.helpers import (  # noqa: E402
    MEDIA_TOKEN_MAX_LENGTH,
    media_token_length,
)

UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")

CAP = MEDIA_TOKEN_MAX_LENGTH
PREFIX = "MEDIA:"
# The one candidate ceiling: `MEDIA:` plus a legal capture, inclusive.
CANDIDATE_MAX = CAP + len(PREFIX)

_UI_FUNCS = [
    "_mediaPathSrc",
    "_mediaTokenRe",
    "_unquoteMediaRef",
    "_mediaTokenMaxLength",
    "_mediaTokenExceedsMaxLength",
]
_MSG_FUNCS = [
    "_smdMediaPrefixTail",
    "_smdMediaTailEntryChunk",
    "_smdMediaTailSameOwner",
    "_smdMediaTailSet",
    "_smdMediaTokenIsSettled",
    "_smdMediaTailCouldExtend",
    "_smdMediaHasOpenQuote",
    "_smdMediaOpenQuoteChar",
    "_smdMediaRefuseLine",
    "_smdMediaEntryRefused",
    "_smdMediaRunChar",
    "_smdMediaRefusedRunLength",
    "_smdMediaCandidateMax",
    "_smdMediaTailFlushEntry",
    "_smdMediaTailFlush",
    "_smdMediaAwareAddText",   # the function under test
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
import {readFileSync} from 'node:fs';
const _SMD_MEDIA_PREFIX = 'MEDIA:';
const _SMD_MEDIA_TAIL = new Map();

let events = [];
// Leaf sinks: the ONLY stubs.
function _smdAppendMediaNode(parent, rawRef){ events.push({kind:'MEDIA', v:rawRef}); return true; }
function _smdMediaWriteText(parent, data, baseAddText, writeText, chunk){
  if (chunk !== '' && chunk != null) events.push({kind:'TEXT', v:String(chunk)});
}

// The settled contract, mirroring renderMd()'s MEDIA stash in static/ui.js: an
// over-ceiling capture is not a legal token, so its whole span stays prose.
function settledEvents(text){
  const re = _mediaTokenRe();
  const out = [];
  let m, last = 0;
  while ((m = re.exec(text))){
    if (_mediaTokenExceedsMaxLength(m[1])) continue;
    if (m.index > last) out.push({kind:'TEXT', v:text.slice(last, m.index)});
    out.push({kind:'MEDIA', v:_unquoteMediaRef(m[1])});
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({kind:'TEXT', v:text.slice(last)});
  return out;
}

function streamedEvents(chunks){
  events = [];
  _SMD_MEDIA_TAIL.clear();
  const parser = {id:'p1'}, parent = {id:'root'};
  for (const chunk of chunks){
    _smdMediaAwareAddText(null, parent, {}, chunk, _SMD_MEDIA_TAIL, parser, null);
  }
  _smdMediaTailFlush(parser);
  return events.slice();
}

const mediaOf = (e) => e.filter(x => x.kind === 'MEDIA').map(x => x.v);
const textOf  = (e) => e.filter(x => x.kind === 'TEXT').map(x => x.v).join('');

const payload = JSON.parse(readFileSync(process.argv[1], 'utf8'));
const out = [];
for (const row of payload){
  const want = settledEvents(row.text);
  const wantMedia = mediaOf(want), wantText = textOf(want);
  // `cuts` null means EVERY cut position; otherwise the listed ones.
  const cuts = row.cuts
    || Array.from({length: row.text.length - 1}, (_, i) => i + 1);
  const bad = [];
  for (const cut of cuts){
    const got = streamedEvents([row.text.slice(0, cut), row.text.slice(cut)]);
    const gm = mediaOf(got), gt = textOf(got);
    if (JSON.stringify(gm) !== JSON.stringify(wantMedia) || gt !== wantText){
      bad.push({cut, gotMedia: gm, gotTextLen: gt.length, gotTextTail: gt.slice(-40)});
    }
  }
  out.push({
    label: row.label,
    textLen: row.text.length,
    cutsChecked: cuts.length,
    wantMedia, wantTextLen: wantText.length, wantText,
    badCount: bad.length, bad: bad.slice(0, 5),
  });
}
console.log(JSON.stringify(out));
"""


def _run(rows: list[dict]) -> Any:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = "\n".join(
        [_extract_js_function(UI_JS, n) for n in _UI_FUNCS]
        + [_extract_js_function(MESSAGES_JS, n) for n in _MSG_FUNCS]
        + [_HARNESS]
    )
    # Payload via a temp file: a 4KB ref in argv overflows execve (Errno 7).
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", encoding="utf-8", delete=False
    ) as handle:
        json.dump(rows, handle)
        path = handle.name
    try:
        proc = subprocess.run(
            [node, "--input-type=module", "-e", script, path],
            capture_output=True, text=True, timeout=600, check=True,
        )
    finally:
        Path(path).unlink(missing_ok=True)
    return {row["label"]: row for row in json.loads(proc.stdout)}


# ── Fixtures: references whose OWN length crosses the ceiling ────────────────


def _quoted_ref(capture_units: int) -> str:
    """A quoted capture measuring exactly *capture_units* code units.

    The capture INCLUDES its quotes — that is the span the ceiling measures and
    the span the streaming buffer holds.
    """
    head, tail = "/tmp/", ".png"
    body = capture_units - 2 - len(head) - len(tail)
    assert body > 0, capture_units
    ref = '"' + head + ("y" * body) + tail + '"'
    assert media_token_length(ref) == capture_units
    return ref


def _bare_ref(capture_units: int) -> str:
    head, tail = "/tmp/", ".png"
    body = capture_units - len(head) - len(tail)
    assert body > 0, capture_units
    ref = head + ("y" * body) + tail
    assert media_token_length(ref) == capture_units
    return ref


# 4095 / 4096 / 4097 — the exact rows the re-review asked for. Quoted AND bare,
# because only the quoted shape reaches the branch that carried the wrong ceiling.
_BOUNDARY_ROWS = [
    (f"{shape} capture {CAP + off}", f"see MEDIA:{build(CAP + off)} ok", off <= 0)
    for shape, build in (("quoted", _quoted_ref), ("bare", _bare_ref))
    for off in (-1, 0, 1)
]


@pytest.fixture(scope="module")
def boundary_result() -> Any:
    """EVERY chunk cut, for every boundary row.

    Exhaustive rather than sampled: the head's divergence sat at cuts 4100-4104,
    a five-position window inside a 4108-character string, and a sampled sweep
    walks straight past it.
    """
    return _run([
        {"label": label, "text": text, "cuts": None}
        for label, text, _ in _BOUNDARY_ROWS
    ])


@pytest.mark.parametrize(
    "label,text,legal", _BOUNDARY_ROWS, ids=[r[0] for r in _BOUNDARY_ROWS]
)
def test_streamed_equals_settled_at_every_cut_across_the_ceiling(
    label, text, legal, boundary_result
):
    """Captures AND exact remaining prose, at every possible chunk boundary."""
    row = boundary_result[label]
    assert row["cutsChecked"] > CAP, (
        f"{label}: sweep too small ({row['cutsChecked']} cuts)"
    )
    assert row["badCount"] == 0, (
        f"{label}: streamed rendering diverged from settled at "
        f"{row['badCount']} of {row['cutsChecked']} cuts; first few: {row['bad']}"
    )
    # Non-vacuity: the row must actually be on the side of the boundary it claims.
    assert (len(row["wantMedia"]) == 1) is legal, (
        f"{label}: fixture is on the wrong side of the ceiling "
        f"(settled media={row['wantMedia'][:1]})"
    )


def test_quoted_capture_split_at_its_opening_quote_keeps_its_card():
    """The named blocker: opening quote in one chunk, closing quote in another.

    This is the shape that reached the wrongly-bounded branch. Asserted on the
    exact captured token AND the exact remaining prose, at the cuts the head
    actually failed on (4100-4104) plus the structural cut right after the
    opening quote.
    """
    ref = _quoted_ref(CAP)
    text = f"see MEDIA:{ref} ok"
    open_quote = text.index('"')
    rows = [{
        "label": "quoted split at open quote",
        "text": text,
        "cuts": [open_quote + 1, open_quote + 2, 4100, 4101, 4102, 4103, 4104],
    }]
    row = _run(rows)["quoted split at open quote"]

    # The whole point: one card, and the prose either side of it, unchanged.
    assert row["wantMedia"] == [ref.strip('"')], "settled must render one card"
    assert row["wantText"] == "see  ok"
    assert row["badCount"] == 0, (
        "a legal quoted capture whose opening quote crossed a chunk boundary "
        f"diverged from settled output: {row['bad']}"
    )


def test_over_limit_reference_fails_closed_without_activating_its_suffix():
    """An over-limit reference must not let its own suffix become a token.

    An over-cap external URL is tempered-greedy and swallows a nested ``MEDIA:``,
    so settled parsing sees ONE over-limit span and renders no card. At the head
    the stream flushed the truncated prefix and then matched the nested token in
    the next chunk, producing a card that settled output does not have.

    The companion row proves the refusal is not over-broad: a genuinely
    INDEPENDENT reference after a delimiter is still legal and must still render.
    """
    swallowed = "MEDIA:https://h/" + ("y" * 4200) + "MEDIA:/tmp/ok.png"
    independent = "MEDIA:/tmp/" + ("y" * 4200) + " and MEDIA:/tmp/ok.png"
    cuts = [20, 4090, 4096, 4100, 4102, 4103, 4110]
    result = _run([
        {"label": "over-cap url swallows nested token", "text": swallowed, "cuts": cuts},
        {"label": "over-cap ref then independent token", "text": independent, "cuts": cuts},
    ])

    swallow = result["over-cap url swallows nested token"]
    assert swallow["wantMedia"] == [], (
        "settled parsing must render no card for one over-limit span"
    )
    assert swallow["badCount"] == 0, (
        f"the refused reference's suffix activated on its own: {swallow['bad']}"
    )

    later = result["over-cap ref then independent token"]
    assert later["wantMedia"] == ["/tmp/ok.png"], (
        "an independent later reference is legal and settled renders it"
    )
    assert later["badCount"] == 0, (
        f"failing closed swallowed an independent later reference: {later['bad']}"
    )


def test_candidate_ceiling_is_inclusive_and_derived_not_a_literal():
    """The ceiling is ONE number, computed from the shared capture ceiling.

    Driven through the real function rather than asserted on source text, so a
    literal 4102 reappearing anywhere would still be caught by the boundary
    sweeps above.
    """
    script = "\n".join(
        [_extract_js_function(UI_JS, n) for n in ("_mediaTokenMaxLength",)]
        + [
            "const _SMD_MEDIA_PREFIX = 'MEDIA:';",
            _extract_js_function(MESSAGES_JS, "_smdMediaCandidateMax"),
            "console.log(JSON.stringify({max:_smdMediaCandidateMax()}));",
        ]
    )
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, check=True,
    )
    assert json.loads(proc.stdout)["max"] == CANDIDATE_MAX == 4102


def test_refused_run_length_stops_where_the_grammar_stops():
    """The refusal owns the reference, not the rest of the turn.

    ``_smdMediaRefusedRunLength`` decides how much of a later chunk still belongs
    to a refused reference. It must end at a token-closing character (unquoted) or
    after the closing quote (quoted), or a refused reference would swallow an
    independent one. Driven through the real function.
    """
    script = "\n".join(
        [_extract_js_function(UI_JS, n) for n in ("_mediaPathSrc", "_mediaTokenRe")]
        + [
            _extract_js_function(MESSAGES_JS, "_smdMediaRunChar"),
            _extract_js_function(MESSAGES_JS, "_smdMediaRefusedRunLength"),
            r"""
const cases = [
  ['yyy and more', '', 3],          // unquoted: stops at the space
  ['yyy', '', 3],                   // runs to the end of the chunk
  ['yy)tail', '', 2],               // stops at a closing delimiter
  ['yy\nnext', '', 2],              // a newline ends any token
  ['yyy" ok', '"', 4],              // quoted: through the closing quote
  ['yyy ok" x', '"', 7],            // quoted spans a space to reach its quote
  ['yyy no close', '"', 12],        // never closes: owns the chunk
  ['yy\nnext', '"', 2],             // unclosed quote still stops at a newline
];
const out = cases.map(([text, quote, want]) => ({
  text, quote, want, got: _smdMediaRefusedRunLength(text, quote),
}));
console.log(JSON.stringify(out));
""",
        ]
    )
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, check=True,
    )
    for row in json.loads(proc.stdout):
        assert row["got"] == row["want"], row


def test_existing_flush_before_clear_behaviour_is_preserved():
    """The main, anchor, safe, and fade paths still flush before clearing.

    A refused-line marker travels through the same per-parser map as a buffered
    tail, so it must not disturb owner handling. Pinned on the wiring, with the
    behaviour itself exercised by the sweeps above and by
    tests/test_smd_media_in_stream.py.
    """
    block = _extract_js_function(MESSAGES_JS, "_smdMediaAwareAddText")
    # Foreign-owner tails are flushed through their original writer, not merged.
    assert "_smdMediaTailFlushEntry(leadEntry)" in block
    assert "_smdMediaTailSameOwner(leadEntry, parent, baseAddText, writeText)" in block
    # The refused marker is owner-checked exactly like a real tail.
    refuse = _extract_js_function(MESSAGES_JS, "_smdMediaRefuseLine")
    for field in ("parent", "baseAddText", "writeText"):
        assert field in refuse, f"the refusal marker must carry {field}"
    assert "refused:true" in refuse


def test_harness_stubs_only_sinks():
    """Meta-test: every function in the real call chain is extracted, not retyped."""
    for name in _MSG_FUNCS:
        assert f"function {name}(" in MESSAGES_JS, (
            f"{name} vanished from messages.js — the harness would silently stop "
            f"testing production"
        )
    for name in _UI_FUNCS + _MSG_FUNCS:
        assert f"function {name}(" not in _HARNESS, (
            f"harness reimplements {name} — extract it from production instead"
        )
    assert "_smdAppendMediaNode" in _HARNESS and "_smdMediaWriteText" in _HARNESS
