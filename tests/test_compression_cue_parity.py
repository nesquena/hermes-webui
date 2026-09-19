"""JS/Python parity for the compression-start classifier.

``api/streaming.py::_is_agent_compression_start_status`` decides whether the LIVE
SSE path paints a "Compressing context" divider. ``static/messages.js`` decides
the same thing during REPLAY, inside ``_sourceEventTypeForSnapshotAnchorRow``.
When the two disagree, a reloaded session shows a divider the live session never
painted, or hides one it did.

The existing test in ``tests/test_auto_compression_card.py`` asserts that a few
individual cue strings are present. That is not parity: a JS cue with no Python
counterpart passes every such assertion while still inventing a divider.
Measured on upstream at the time this test was written, the JS branch accepted
5 inputs that Python rejects, including ``"Skipping preflight compression
(cooldown)"``.

So this module extracts BOTH accept lists from the real sources and asserts they
are the same set, then drives the real Python predicate over a shared corpus.

Extraction notes, learned by getting them wrong first:

- Only the FINAL accept expression counts on each side. Both files also mention
  cue-shaped strings in rejection branches, in the ``compressed`` branch, and in
  prose that explains why a cue is excluded. Counting those produced three false
  mismatches.
- JS writes the em dash as the escape ``\\u2014`` while Python carries the literal
  character. The extractor normalizes escapes before comparing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from api.streaming import _is_agent_compression_start_status

ROOT = Path(__file__).resolve().parent.parent

_REJECTIONS = ("skipping", "defer", "cooldown", "will not start")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _normalize(cue: str) -> str:
    """Decode source-level escapes so both sides compare as real characters."""
    return cue.encode("utf-8").decode("unicode_escape") if "\\u" in cue else cue


def _js_lifecycle_block() -> str:
    src = _read("static/messages.js")
    start = src.find("function _sourceEventTypeForSnapshotAnchorRow")
    assert start != -1, "replay classifier not found"
    end = src.find("function _hydrateAnchorRegistryFromActivityScene", start)
    assert end != -1, "end of replay classifier not found"
    marker = "if(role==='lifecycle'||kind==='lifecycle_status'){"
    li = src.find(marker, start, end)
    assert li != -1, "lifecycle branch not found"
    lifecycle = src[li:end]
    return "\n".join(
        line for line in lifecycle.splitlines()
        if not line.strip().startswith("//")
    )


def _js_start_cues() -> set[str]:
    """Cues in the JS branch that returns ``'compressing'``.

    Slices from the end of the ``compressed`` branch to the ``'compressing'``
    return, so ``auto-compressed`` and ``compression finished`` (which belong to
    the completed branch) and the rejection cues are both excluded.
    """
    block = _js_lifecycle_block()
    after_compressed = block.find("return 'compressed';")
    assert after_compressed != -1, "compressed branch not found"
    end = block.find("return 'compressing';", after_compressed)
    assert end != -1, "compressing branch not found"
    region = block[after_compressed:end]
    cues = {_normalize(c) for c in re.findall(r"text\.includes\('([^']+)'\)", region)}
    return {c for c in cues if c not in _REJECTIONS}


def _python_start_cues() -> set[str]:
    """Cues in Python's final accept expression only.

    The docstring names ``'preflight compression'`` while explaining why the old
    matcher was wrong, and the rejection branch lists skip phrases. Neither is an
    accept cue.
    """
    src = _read("api/streaming.py")
    start = src.find("def _is_agent_compression_start_status")
    assert start != -1, "python predicate not found"
    nxt = src.find("\ndef ", start + 1)
    body = src[start:nxt if nxt != -1 else len(src)]
    ret = body.rfind("return (")
    assert ret != -1, "accept expression not found"
    accept = body[ret:]
    return {_normalize(c) for c in re.findall(r"'([^']+)' in m", accept)}


def test_extraction_is_not_vacuous():
    """Guard the extractor: an empty set makes every parity assertion vacuous."""
    js, py = _js_start_cues(), _python_start_cues()
    assert len(js) >= 5, f"suspiciously few JS cues: {sorted(js)}"
    assert len(py) >= 5, f"suspiciously few Python cues: {sorted(py)}"


def test_js_accepts_no_cue_that_python_rejects():
    """A JS-only cue lets replay invent a divider the live path never painted."""
    js_only = _js_start_cues() - _python_start_cues()
    assert not js_only, (
        "static/messages.js accepts compression cues that "
        f"_is_agent_compression_start_status does not: {sorted(js_only)}. "
        "Replay would paint a 'Compressing context' divider that the live SSE "
        "path never painted."
    )


def test_python_accepts_no_cue_that_js_rejects():
    """A Python-only cue means replay HIDES a divider the live path painted."""
    py_only = _python_start_cues() - _js_start_cues()
    assert not py_only, (
        "_is_agent_compression_start_status accepts cues that "
        f"static/messages.js does not: {sorted(py_only)}. "
        "Replay would omit a divider the live path painted."
    )


def test_js_rejects_skip_and_defer_before_matching_cues():
    """Order matters: the rejection must precede the positive cue checks.

    "Skipping preflight compression" contains a compression phrase, so a
    rejection placed AFTER the cue checks never runs.
    """
    block = _js_lifecycle_block()
    for phrase in _REJECTIONS:
        assert f"text.includes('{phrase}')" in block, f"missing rejection: {phrase}"
    reject_at = block.find("text.includes('skipping')")
    accept_at = block.find("text.includes('compacting context')")
    assert reject_at != -1 and accept_at != -1
    assert reject_at < accept_at, (
        "the skip/defer rejection must come BEFORE the positive cue checks, "
        "otherwise 'Skipping preflight compression' matches a cue first"
    )


# Real emitter strings, plus the shell row that produced the phantom divider.
_CORPUS = [
    ("", False),
    ("Working on request", False),
    ("Skipping preflight compression (cooldown)", False),
    ("Preflight compression will not start", False),
    ("Preflight compression deferred", False),
    ("Preflight compression: evaluating", False),
    ("Pre-API compression: 120k tokens", True),
    ("Compacting context", True),
    ("Context too large", True),
    ("\u2014 compressing (attempt 1)", True),
    ("- compressing (attempt 2)", True),
    ("compression attempt 3", True),
    ("Auto-compressed 40k tokens", False),
    ("Compression finished", False),
]


@pytest.mark.parametrize("message,expected", _CORPUS)
def test_python_predicate_over_the_shared_corpus(message, expected):
    """Pin the authority's behaviour so a later widening is a visible change."""
    assert _is_agent_compression_start_status("lifecycle", message) is expected


@pytest.mark.parametrize("message,expected", _CORPUS)
def test_shared_cues_reproduce_the_python_verdict(message, expected):
    """The extracted shared cue set must reach Python's verdict on every row.

    Applies the extracted cues rather than re-implementing either branch, so the
    test cannot drift from the sources it checks.
    """
    m = message.strip().lower()
    rejected = any(p in m for p in _REJECTIONS)
    post_compress = (
        "compressed" in m and "compressing" not in m and "compression attempt" not in m
    )
    shared = _js_start_cues() & _python_start_cues()
    hit = any(c in m for c in shared)
    verdict = bool(m) and not rejected and not post_compress and hit
    assert verdict is expected, (
        f"shared-cue verdict {verdict} != python verdict {expected} for {message!r}"
    )
