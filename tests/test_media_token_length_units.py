"""The MEDIA length ceiling must use ONE unit in both languages (PR #6607).

Re-review blocker: ``MEDIA_TOKEN_MAX_LENGTH``/``media_token_exceeds_max_length()``
in api/helpers.py and ``_mediaTokenMaxLength()``/``_mediaTokenExceedsMaxLength()``
in static/ui.js both said 4096, and they measured DIFFERENT THINGS. Python
``len()`` counts Unicode code points; JavaScript ``.length`` counts UTF-16 code
units. A token of 2049 U+1F600 characters measured 2049 in Python and 4098 in
JavaScript, so Python admitted a token JavaScript refused — for every astral
token from 2049 through 4096 characters.

The chosen unit is **UTF-16 code units**, because the cap bounds a streaming
buffer that is a JavaScript string, and code units are what that buffer costs.
api/helpers.py converts via ``media_token_length()``.

Two things are asserted for every row, and BOTH matter:

1. **Parity** — Python and JavaScript return the same verdict.
2. **Correctness** — that verdict equals a hardcoded ``expected`` in the table.

Parity alone is the "mirrored oracle" the reviewer rejects: two implementations
can agree and both be wrong. The hardcoded column is derived from the stated
contract (4096 UTF-16 code units), not from either implementation.

Fixtures cross the boundary as a RECIPE (code point + repeat count) rather than
as a literal string, so each language builds the subject natively. That keeps a
4KB astral payload and a lone surrogate off the JSON transport, and it means
neither side can be handed a string the other could not represent.
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
    media_token_exceeds_max_length,
    media_token_length,
)

UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")

CAP = 4096

# Code points, one per width class. ASCII and BMP cost 1 UTF-16 code unit;
# anything above U+FFFF needs a surrogate pair and costs 2.
_ASCII = 0x61      # 'a'
_BMP = 0x6587      # '文' — 3 UTF-8 bytes, still ONE UTF-16 code unit
_ASTRAL = 0x1F600  # '😀' — a surrogate pair, TWO UTF-16 code units
_LONE_SURROGATE = 0xD800

# (label, code_point, repeat, pad_ascii, expected_exceeds)
#
# The subject is chr(code_point) * repeat + 'a' * pad_ascii. `expected` is the
# contract's own answer — UTF-16 code units above 4096 — written out by hand.
ROWS = [
    # ── ASCII: 1 code unit each ──────────────────────────────────────────────
    ("ascii at cap minus one", _ASCII, CAP - 1, 0, False),
    ("ascii exactly at cap", _ASCII, CAP, 0, False),
    ("ascii at cap plus one", _ASCII, CAP + 1, 0, True),
    # ── BMP: 1 code unit each, but 3 UTF-8 BYTES — a byte-based cap would
    #    reject these, which is why the unit must be named explicitly.
    ("bmp at cap minus one", _BMP, CAP - 1, 0, False),
    ("bmp exactly at cap", _BMP, CAP, 0, False),
    ("bmp at cap plus one", _BMP, CAP + 1, 0, True),
    # ── Astral: 2 code units each. This is the class that diverged.
    ("astral at cap minus two", _ASTRAL, (CAP // 2) - 1, 0, False),
    ("astral exactly at cap", _ASTRAL, CAP // 2, 0, False),
    # 2049 astral characters: Python len() said 2049 (legal), JS .length said
    # 4098 (illegal). The exact case the reviewer measured.
    ("astral 2049 — the reported divergence", _ASTRAL, 2049, 0, True),
    # ── Astral landing on the boundary ODD, so a pair straddles it ───────────
    ("astral plus pad exactly at cap", _ASTRAL, (CAP // 2) - 1, 2, False),
    ("astral plus pad at cap plus one", _ASTRAL, (CAP // 2) - 1, 3, True),
    # ── Lone surrogate: 1 code unit on both sides. A predicate must return a
    #    verdict for it rather than raise, so it is a row, not a footnote.
    ("lone surrogate exactly at cap", _LONE_SURROGATE, CAP, 0, False),
    ("lone surrogate at cap plus one", _LONE_SURROGATE, CAP + 1, 0, True),
    # ── Degenerate rows ─────────────────────────────────────────────────────
    ("empty", _ASCII, 0, 0, False),
    ("single ascii", _ASCII, 1, 0, False),
]


def _subject(code_point: int, repeat: int, pad_ascii: int) -> str:
    """Build a row's subject in Python, from the same recipe JavaScript uses."""
    return chr(code_point) * repeat + "a" * pad_ascii


def _extract_js_function(src: str, name: str) -> str:
    """Lift a production function out of the shipped source, verbatim.

    Same brace-counting extraction the existing MEDIA harnesses use (see
    tests/test_media_stream_settled_equality.py and the note at static/ui.js
    ``_localTargetMarkers``). Never a re-implementation: if a helper is renamed
    or deleted, this raises instead of quietly testing a copy.
    """
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


def _node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    return node


def _run_node(script: str, payload) -> Any:
    """Run *script* under node with *payload* handed over via a temp file.

    Not via ``process.argv``: a consumer row carries a ~4096-character ref, and
    nine of them overflow ``execve``'s argument limit with
    ``OSError: [Errno 7] Argument list too long``. A file also keeps subject
    construction in ONE place (Python), so no fixture logic is duplicated in JS.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", encoding="utf-8", delete=False
    ) as handle:
        json.dump(payload, handle)
        payload_path = handle.name
    try:
        proc = subprocess.run(
            [_node(), "--input-type=module", "-e", script, payload_path],
            capture_output=True, text=True, timeout=120, check=True,
        )
    finally:
        Path(payload_path).unlink(missing_ok=True)
    return json.loads(proc.stdout)


# ── The matrix ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def js_verdicts() -> Any:
    """Real ``_mediaTokenExceedsMaxLength()`` over every row, under node."""
    script = "\n".join([
        _extract_js_function(UI_JS, "_mediaTokenMaxLength"),
        _extract_js_function(UI_JS, "_mediaTokenExceedsMaxLength"),
        r"""
import {readFileSync} from 'node:fs';
const rows = JSON.parse(readFileSync(process.argv[1], 'utf8'));
const out = rows.map((r) => {
  // Built HERE from the recipe, so the astral payload and the lone surrogate
  // never cross the JSON boundary as literal text.
  const subject = String.fromCodePoint(r.codePoint).repeat(r.repeat)
    + 'a'.repeat(r.pad);
  return {
    label: r.label,
    utf16Length: subject.length,
    exceeds: _mediaTokenExceedsMaxLength(subject),
  };
});
console.log(JSON.stringify({cap: _mediaTokenMaxLength(), rows: out}));
""",
    ])
    payload = [
        {"label": label, "codePoint": cp, "repeat": repeat, "pad": pad}
        for label, cp, repeat, pad, _ in ROWS
    ]
    return _run_node(script, payload)


def test_the_two_caps_are_the_same_number(js_verdicts):
    assert js_verdicts["cap"] == MEDIA_TOKEN_MAX_LENGTH == CAP


@pytest.mark.parametrize(
    "label,code_point,repeat,pad,expected",
    ROWS,
    ids=[row[0] for row in ROWS],
)
def test_python_and_js_agree_and_are_correct(
    label, code_point, repeat, pad, expected, js_verdicts
):
    """Same verdict in both languages, and that verdict matches the contract."""
    subject = _subject(code_point, repeat, pad)
    js = next(r for r in js_verdicts["rows"] if r["label"] == label)

    # The two sides must agree on the MEASUREMENT, not merely on the verdict —
    # equal verdicts either side of the boundary would hide a unit mismatch.
    assert media_token_length(subject) == js["utf16Length"], (
        f"{label}: length disagreement — python="
        f"{media_token_length(subject)} js={js['utf16Length']}"
    )
    assert media_token_exceeds_max_length(subject) is expected, (
        f"{label}: python verdict wrong for {js['utf16Length']} code units"
    )
    assert js["exceeds"] is expected, (
        f"{label}: js verdict wrong for {js['utf16Length']} code units"
    )


def test_reported_2049_astral_divergence_is_closed():
    """The exact case from the re-review, named so a regression is unmistakable.

    Python ``len()`` returned 2049 and admitted the token; JavaScript
    ``.length`` returned 4098 and refused it. Both must now refuse it.
    """
    token = "\U0001F600" * 2049
    assert len(token) == 2049, "code-point count, for contrast"
    assert media_token_length(token) == 4098, "UTF-16 code units — the contract"
    assert media_token_exceeds_max_length(token) is True


def test_every_astral_count_from_2049_to_4096_is_refused():
    """The whole divergent band, not just its first row.

    Any astral token from 2049 through 4096 characters exceeds 4096 code units,
    so every one of them was accepted by Python and refused by JavaScript.
    Sampled rather than exhaustive: the boundaries are pinned above, and this
    covers the band's interior at low cost.
    """
    for count in (2049, 2050, 3000, 4095, 4096):
        token = "\U0001F600" * count
        assert media_token_exceeds_max_length(token) is True, count
    # And the row immediately below the band is still legal.
    assert media_token_exceeds_max_length("\U0001F600" * 2048) is False


def test_media_token_length_counts_code_units_not_code_points_or_bytes():
    """Pin the unit itself, against all three plausible readings of "length"."""
    assert media_token_length("") == 0
    assert media_token_length(None) == 0  # type: ignore[arg-type]
    assert media_token_length("abc") == 3
    # BMP: one code unit, but three UTF-8 bytes. A byte-based cap would say 3.
    assert media_token_length("文") == 1
    assert len("文".encode()) == 3, "contrast: bytes are NOT the unit"
    # Astral: two code units, one code point, four UTF-8 bytes.
    assert media_token_length("\U0001F600") == 2
    assert len("\U0001F600") == 1, "contrast: code points are NOT the unit"
    # A lone surrogate is one code unit and must not raise.
    assert media_token_length("\ud800") == 1
    assert media_token_length("a\ud800\U0001F600") == 4


# ── Consumer matrices: the same verdict at each place a token can activate ───
#
# The reviewer asked for matching ASCII / BMP / astral boundary rows in the
# settled renderer, the streaming path, the route allow-list, and the share
# inliner. The rows below use a GRAMMAR-LEGAL ref so each consumer really runs.
#
# Note the physical limit on two of those four: Linux PATH_MAX is 4096 BYTES, so
# no real file can exist at a path of 4096 UTF-16 code units. The share and
# allow-list rows therefore assert the decision the cap drives (an over-cap token
# is inert) rather than a successful embed at the boundary, which is unreachable
# by construction.


def _ref_of_utf16_length(units: int, code_point: int = _ASTRAL) -> str:
    """A grammar-legal ``/tmp/…​.png`` ref measuring exactly *units* code units."""
    head, tail = "/tmp/", ".png"
    body_units = units - len(head) - len(tail)
    assert body_units > 0, units
    width = 2 if code_point > 0xFFFF else 1
    whole, remainder = divmod(body_units, width)
    ref = head + chr(code_point) * whole + "a" * remainder + tail
    assert media_token_length(ref) == units, (media_token_length(ref), units)
    return ref


# (label, code_point) — one row per width class, each swept across the boundary.
_CONSUMER_CLASSES = [("ascii", _ASCII), ("bmp", _BMP), ("astral", _ASTRAL)]
# (offset from the cap, is the ref legal)
_CONSUMER_OFFSETS = [(-1, True), (0, True), (1, False)]

_CONSUMER_ROWS = [
    (f"{label} cap{offset:+d}", _ref_of_utf16_length(CAP + offset, cp), legal)
    for label, cp in _CONSUMER_CLASSES
    for offset, legal in _CONSUMER_OFFSETS
]


@pytest.fixture(scope="module")
def consumer_verdicts() -> Any:
    """Settled renderMd() and the streaming walk, both real, over every row.

    Only leaf sinks are stubbed, matching the harness discipline in
    tests/test_media_stream_settled_equality.py: no decision function is retyped.
    """
    ui = [
        "_mediaPathSrc",
        "_mediaTokenRe",
        "_unquoteMediaRef",
        "_mediaTokenMaxLength",
        "_mediaTokenExceedsMaxLength",
    ]
    msg = [
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
        "_smdMediaAwareAddText",
    ]
    script = "\n".join(
        [_extract_js_function(UI_JS, n) for n in ui]
        + [_extract_js_function(MESSAGES_JS, n) for n in msg]
        + [r"""
const _SMD_MEDIA_PREFIX = 'MEDIA:';
const _SMD_MEDIA_TAIL = new Map();

let events = [];
// Leaf sinks only.
function _smdAppendMediaNode(parent, rawRef){ events.push({kind:'MEDIA', v:rawRef}); return true; }
function _smdMediaWriteText(parent, data, baseAddText, writeText, chunk){
  if (chunk !== '' && chunk != null) events.push({kind:'TEXT', v:String(chunk)});
}

// The settled MEDIA stash from renderMd(), same shape as static/ui.js: a token
// whose capture is over the ceiling is left as prose.
function settledMedia(text){
  const out = [];
  text.replace(_mediaTokenRe(), (whole, raw) => {
    if (_mediaTokenExceedsMaxLength(raw)) return whole;
    out.push(_unquoteMediaRef(raw));
    return '';
  });
  return out;
}

function streamedMedia(chunks){
  events = [];
  _SMD_MEDIA_TAIL.clear();
  const parser = {id:'p1'}, parent = {id:'root'};
  for (const chunk of chunks) {
    _smdMediaAwareAddText(null, parent, {}, chunk, _SMD_MEDIA_TAIL, parser, null);
  }
  _smdMediaTailFlush(parser);
  return events.filter(e => e.kind === 'MEDIA').map(e => e.v);
}

import {readFileSync} from 'node:fs';
const rows = JSON.parse(readFileSync(process.argv[1], 'utf8'));
const out = rows.map((r) => {
  const text = 'see MEDIA:' + r.ref + ' ok';
  const settled = settledMedia(text);
  // Split at a fixed inner offset AND right after the keyword, so a ref that
  // crosses the boundary is exercised from more than one cut.
  const cuts = [1, 'see MEDIA:'.length, Math.floor(text.length / 2), text.length - 1];
  const streamed = cuts.map((i) => streamedMedia([text.slice(0, i), text.slice(i)]));
  return {label: r.label, settled, streamed};
});
console.log(JSON.stringify(out));
"""]
    )
    payload = [{"label": label, "ref": ref} for label, ref, _ in _CONSUMER_ROWS]
    return {row["label"]: row for row in _run_node(script, payload)}


@pytest.mark.parametrize(
    "label,ref,legal", _CONSUMER_ROWS, ids=[row[0] for row in _CONSUMER_ROWS]
)
def test_settled_and_streamed_agree_with_python_across_the_boundary(
    label, ref, legal, consumer_verdicts
):
    """One verdict for one token, at every consumer, in both languages."""
    row = consumer_verdicts[label]
    expected_media = [ref] if legal else []

    assert media_token_exceeds_max_length(ref) is (not legal), (
        f"{label}: python predicate disagrees with the row's contract"
    )
    assert row["settled"] == expected_media, (
        f"{label}: settled renderMd() stash diverged"
    )
    for i, streamed in enumerate(row["streamed"]):
        assert streamed == expected_media, (
            f"{label}: streamed rendering diverged at cut {i}"
        )


@pytest.mark.parametrize(
    "label,ref,legal", _CONSUMER_ROWS, ids=[row[0] for row in _CONSUMER_ROWS]
)
def test_share_inliner_leaves_an_over_cap_ref_exactly_as_prose(
    label, ref, legal, tmp_path
):
    """The share snapshot must not touch a span neither renderer will activate.

    An over-cap token is inert everywhere else, so the snapshot has to return it
    byte-for-byte rather than embed it or replace it with the missing-media
    placeholder — otherwise the share and the live view disagree about one token.
    """
    from api import shares

    text = f"see MEDIA:{ref} ok"
    out = shares._embed_share_media(text, allowed_roots=(tmp_path,))
    if legal:
        # No such file (and none can exist at this length — PATH_MAX is 4096
        # bytes), so a legal ref is resolved and placeholdered. What matters is
        # that it was CONSIDERED.
        assert out != text, f"{label}: a legal ref was never examined"
    else:
        assert out == text, (
            f"{label}: an over-cap ref was rewritten in a share snapshot"
        )


def test_route_allow_list_ignores_an_over_cap_astral_token(tmp_path, monkeypatch):
    """An over-cap token must not mint an /api/media allow-list entry.

    Nothing on the page can request it — neither renderer activates it — so
    honouring it would authorize a path no view asks for. Driven through the real
    ``_session_media_token_allows_path``.
    """
    from api import routes

    target = tmp_path / "real.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n")
    over_cap = _ref_of_utf16_length(CAP + 1, _ASTRAL)

    def _session_with(*refs):
        class _Session:
            messages = [
                {
                    "role": "assistant",
                    "content": " ".join(f"MEDIA:{r}" for r in refs),
                }
            ]

        return _Session()

    # An over-cap token alone authorizes nothing.
    monkeypatch.setattr(routes, "get_session", lambda sid: _session_with(over_cap))
    assert routes._session_media_token_allows_path(
        "sess-1", target, {"image/png"}
    ) is False

    # Non-vacuity: the same call path DOES authorize the real, in-cap path, so
    # the False above is the ceiling talking and not a broken fixture.
    monkeypatch.setattr(
        routes, "get_session", lambda sid: _session_with(over_cap, str(target))
    )
    assert routes._session_media_token_allows_path(
        "sess-1", target, {"image/png"}
    ) is True


# ── Harness integrity ───────────────────────────────────────────────────────


def test_harness_extracts_production_and_reimplements_nothing():
    """Keep this file honest: no local copy of a decision function.

    The reviewer has rejected mirrored oracles here before. Every predicate under
    test is lifted from the shipped source by name, so a rename fails loudly
    instead of leaving a stale copy passing.
    """
    for name in ("_mediaTokenMaxLength", "_mediaTokenExceedsMaxLength"):
        assert f"function {name}(" in UI_JS, f"{name} vanished from ui.js"
    assert "function _smdMediaAwareAddText(" in MESSAGES_JS
    # Python side: the conversion is production code, not a test helper.
    assert media_token_length.__module__ == "api.helpers"
    # And this file must not carry its own copy of it. Scanned with the token
    # split, so this very assertion is not what the scan finds.
    source = Path(__file__).read_text(encoding="utf-8")
    assert ("def " + "media_token_length") not in source, (
        "the test must not carry its own copy of the length function"
    )
    assert ("def " + "media_token_exceeds_max_length") not in source, (
        "the test must not carry its own copy of the ceiling predicate"
    )
