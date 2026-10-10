"""#7925 review round 2 — the two MUST-FIX items.

The re-gate found that the two fixes from the previous round cancel each other
out, and that leaves new costs:

1. **[MUST-FIX] A focus refresh on a long session downloads the whole
   transcript, then keeps doing it.** ``static/sessions.js:3989`` entered the
   stitch block whenever the window offset was above 0. The trust gate at
   ``:3817`` returns false for ``newOffset <= prevOrigin``, so
   ``_stitchBoundedReloadTail`` returned null and the full-transcript fallback
   fired. A window whose origin is at or before the rendered origin already
   covers every rendered row, so master simply replaces. A 2,000-row session
   opened at the 50-row window and refreshed with one row appended each time
   downloaded 2,001 rows on the first focus and the whole transcript on every
   later poll and focus.

2. **[MUST-FIX] The server hashes the whole transcript prefix on every windowed
   response, and nothing uses the result.** ``api/routes.py:14067`` minted
   ``_transcript_prefix_proof`` for every ``messages=1`` response with a
   non-zero offset. The digest walks the prefix per UTF-16 code unit in pure
   Python: 307 ms at 1 MB, 1.5 s at 5 MB, 3.0 s at 10 MB per request. 2,048 tool
   rows totalling 32 MiB plus 31 user rows returned the 30-row window in 141 ms
   on master and hit ``TimeoutError`` at 8 s here. A JSON-escaped lone surrogate
   (``\\ud800``) in an omitted historical row also threw ``UnicodeEncodeError``
   where master returned HTTP 200.

The reviewer offered two directions: make the stitch reachable with a bounded,
cached proof, or drop the proof and the stitch. **The first direction is taken
here**, because fixing MUST-FIX 1 is what makes the stitch reachable at all —
once the block is gated on the window actually moving forward, the stitch is the
common path for "load older", and deleting it would give back exactly the
per-refresh full download #7899 set out to remove.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0,
    reason="node not on PATH",
)

_SESSIONS_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static",
    "sessions.js",
)


def _sessions_source() -> str:
    with open(_SESSIONS_JS, encoding="utf-8") as handle:
        return handle.read()


def _extract_function(name: str) -> str:
    src = _sessions_source()
    start = src.find(f"function {name}(")
    assert start >= 0, f"function {name} not found"
    i = src.find("{", start)
    depth = 0
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces after {name}")


def _run_js(js_code: str, payload: dict) -> object:
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
    tf.write(js_code)
    tf.close()
    try:
        result = subprocess.run(
            ["node", tf.name, json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"node error: {result.stderr}"
        return json.loads(result.stdout)
    finally:
        os.unlink(tf.name)


# ── MUST-FIX 1: the stitch block is gated on the window moving forward ──────


def test_the_stitch_block_requires_the_window_to_advance():
    """The entry condition must be ``> _previousReloadOffset``, not ``> 0``.

    A window whose origin is at or before the rendered origin already covers
    every rendered row, so master's plain replace is correct and cheap. Entering
    the stitch on ``> 0`` sent every such refresh into the full-transcript
    fallback.
    """
    src = _sessions_source()
    # Find the reload-offset assignment and the condition that guards the stitch.
    idx = src.find("const _reloadOffset = Number(data.session._messages_offset)")
    assert idx > 0, "the reload-offset read is gone"
    window = src[idx : idx + 1600]
    assert "_reloadOffset > _previousReloadOffset" in window, (
        "the stitch block is not gated on the window actually moving forward; "
        "a non-advancing refresh still falls through to the full-transcript "
        "download (#7925 MUST-FIX 1)"
    )
    assert "_reloadOffset > 0 &&" not in window, (
        "the old ``_reloadOffset > 0`` condition is still present"
    )


def _run_trust(prev, previous_offset, new_offset, tail, proof):
    js_code = (
        _extract_function("_reloadPrefixRowFingerprint")
        + "\n"
        + _extract_function("_prefixFreshnessDigest")
        + "\n"
        + _extract_function("_boundedReloadPrefixIsTrustworthy")
        + "\n"
        + (
            "const input = JSON.parse(process.argv[2]);\n"
            "process.stdout.write(JSON.stringify(_boundedReloadPrefixIsTrustworthy(\n"
            "  input.prev, input.prevOffset, input.newOffset, input.tail, input.proof)));\n"
        )
    )
    return _run_js(
        js_code,
        {
            "prev": prev,
            "prevOffset": previous_offset,
            "newOffset": new_offset,
            "tail": tail,
            "proof": proof,
        },
    )


def test_a_non_advancing_window_fails_the_trust_gate():
    """prevOrigin == newOffset must not be trusted.

    The gate refuses ``clipped <= prevOrigin``: a window whose origin is at or
    before the rendered origin already covers every rendered row, so there is
    nothing to stitch and the tail is not newer. The caller must replace
    outright rather than fall through to the full-transcript download.
    """
    from api.routes import _transcript_prefix_proof

    prev = [{"role": "user", "content": f"m{i}"} for i in range(50)]
    proof = _transcript_prefix_proof(prev, 0)
    assert _run_trust(prev, 0, 0, prev, proof) is False


def test_an_advancing_window_still_passes_the_gate():
    """The gate must not have disabled the optimisation it protects."""
    from api.routes import _transcript_prefix_proof

    prev = [{"role": "user", "content": f"m{i}"} for i in range(600)]
    tail = [{"role": "user", "content": f"m{i}"} for i in range(150, 160)]
    proof = _transcript_prefix_proof(prev, 150)
    assert _run_trust(prev, 0, 150, tail, proof) is True, (
        "an advancing window with a matching proof no longer passes the trust "
        "gate, so every 'load older' on a long session is a full download"
    )


def test_the_stitch_still_assembles_when_trusted():
    """``_stitchBoundedReloadTail`` itself is unchanged and still splices."""
    js_code = _extract_function("_stitchBoundedReloadTail") + "\n" + (
        "const input = JSON.parse(process.argv[2]);\n"
        "const out = _stitchBoundedReloadTail(\n"
        "  input.prev, input.prevOffset, input.newOffset, input.tail, () => true);\n"
        "process.stdout.write(JSON.stringify(out === null ? null : out.length));\n"
    )
    out = _run_js(
        js_code,
        {
            "prev": [{"role": "user", "content": f"m{i}"} for i in range(600)],
            "prevOffset": 0,
            "newOffset": 150,
            "tail": [{"role": "user", "content": f"m{i}"} for i in range(150, 160)],
        },
    )
    # prevOrigin=0, newOffset=150 -> prefixLength = 150, so the result is the
    # first 150 retained rows plus the 10-row fresh tail.
    assert out == 160, (
        f"the bounded stitch no longer assembles (got {out!r})"
    )


# ── MUST-FIX 2: the proof is bounded, cached, and surrogate-safe ────────────


def test_the_proof_accepts_a_lone_surrogate():
    """A JSON-escaped lone surrogate must not 500 a windowed response.

    The row is omitted from the response — only its digest is used — so the
    digest must be able to describe it.
    """
    from api.routes import _transcript_prefix_proof

    rows = [
        {"role": "user", "content": "before"},
        {"role": "assistant", "content": "lone \ud800 surrogate"},
        {"role": "user", "content": "after"},
    ]
    # Must not raise.
    proof = _transcript_prefix_proof(rows, 3)
    assert proof.startswith("3:"), f"unexpected proof shape: {proof!r}"


def test_the_proof_is_bounded_on_a_long_prefix():
    """Hashing 32 MiB of omitted history per request is the reported 8 s timeout.

    #7925 round-3 MUST-FIX: the row cap is not a time bound — the reported
    failing input (2,048 x 16 KiB tool rows plus 31 user rows) sits under the
    row cap yet hashes 32 MiB. The bound is now a cumulative 1 MiB UTF-16
    budget enforced BEFORE the per-unit loop, and a prefix that would exceed
    it gets NO proof at all (``""``) rather than a proof that silently covers
    less than it claims. The client fails closed on an absent proof.
    """
    from api.routes import (
        _PROOF_MAX_HASHED_BYTES,
        _PROOF_MAX_HASHED_ROWS,
        _transcript_prefix_proof,
    )

    rows = [{"role": "user", "content": "x" * 100} for _ in range(20_000)]
    proof = _transcript_prefix_proof(rows, 20_000)
    # 20,000 x ~100 chars is far past the byte budget, so no proof is minted.
    assert proof == "", (
        f"an over-budget prefix must decline to prove, got {proof!r}"
    )
    assert _PROOF_MAX_HASHED_ROWS < 20_000, (
        "the hash cap is above the row count used here, so this test is not "
        "exercising the bound"
    )
    assert _PROOF_MAX_HASHED_BYTES <= 1024 * 1024, (
        "the byte budget is above 1 MiB, so the reported 32 MiB input is not "
        "exercising the bound"
    )
    # A prefix comfortably inside the budget is still proven, and the reported
    # row count stays the real one.
    small = [{"role": "user", "content": "x" * 100} for _ in range(50)]
    small_proof = _transcript_prefix_proof(small, 50)
    assert small_proof.startswith("50:"), small_proof


def test_the_proof_cache_is_keyed_on_the_revision_not_the_rows():
    """A shape-derived key serves a stale digest after a compaction.

    A compaction that rewrites rows INSIDE the prefix keeps the row count and
    the first/last row identities, so any key derived from those serves the
    pre-compaction digest for a transcript that no longer matches it — turning
    the safety check into a lie. The cache must therefore only be consulted when
    the caller can name a revision.
    """
    from api.routes import _transcript_prefix_proof

    held = [{"role": "user", "content": f"m{i}"} for i in range(600)]
    before = _transcript_prefix_proof(held, 150)
    # Same shape, same head, same tail; only the middle 100 rows changed.
    rewritten = [dict(row) for row in held]
    for row in rewritten[:100]:
        row["content"] = "compacted-away"
    after = _transcript_prefix_proof(rewritten, 150)
    assert before != after, (
        "two different transcripts produced the same proof; the cache is "
        "keyed on something that survives a compaction"
    )


def test_the_proof_cache_hits_within_one_revision():
    """The cache must actually remove the repeated cost it was added for."""
    from api.routes import _transcript_proof_cache, _transcript_prefix_proof

    rows = [{"role": "user", "content": f"m{i}"} for i in range(300)]
    _transcript_proof_cache.clear()
    first = _transcript_prefix_proof(rows, 150, _revision="rev-1")
    assert len(_transcript_proof_cache) == 1
    # A different prefix length under the same revision must not be served the
    # first digest.
    second = _transcript_prefix_proof(rows, 200, _revision="rev-1")
    assert second != first
    # The same (revision, prefix length) must hit.
    third = _transcript_prefix_proof(rows, 150, _revision="rev-1")
    assert third == first


def test_the_proof_cache_is_bounded():
    """An unbounded memo on a long-lived process is a leak."""
    from api.routes import (
        _PROOF_CACHE_MAX_ENTRIES,
        _transcript_prefix_proof,
        _transcript_proof_cache,
    )

    rows = [{"role": "user", "content": "x"}]
    _transcript_proof_cache.clear()
    for i in range(_PROOF_CACHE_MAX_ENTRIES + 20):
        _transcript_prefix_proof(rows, 1, _revision=f"rev-{i}")
    assert len(_transcript_proof_cache) <= _PROOF_CACHE_MAX_ENTRIES


def test_the_proof_route_passes_a_revision():
    """Without a revision at the call site the cache never fires.

    #7925 round-3 MUST-FIX: the key must cover EVERY input to the displayed
    transcript, not only state.db. ``_all_msgs`` merges the WebUI sidecar with
    state.db rows, so the call site now derives a rows fingerprint and pairs it
    with the bracketed state.db signature and the session's profile — a
    sidecar-only edit, a legacy store that returns one revision tuple for every
    session, and a revision read after the rows loaded all used to serve a
    proof that did not describe the rows being returned.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "api" / "routes.py"
    ).read_text(encoding="utf-8")
    idx = src.find("_prefix_proof = _transcript_prefix_proof(")
    assert idx > 0, "the proof call site is gone"
    window = src[max(0, idx - 2200) : idx + 400]
    assert "_state_db_rows_fingerprint" in window, (
        "the proof is keyed without the merged rows' own fingerprint, so a "
        "sidecar edit or a legacy store can serve a stale proof (#7925 "
        "MUST-FIX 3)"
    )
    assert "_display_state_db_signature" in window, (
        "the proof no longer carries the bracketed state.db signature"
    )
    assert "_session_profile" in window, (
        "the proof key omits the session's profile, so two profiles of the "
        "same session id can share a memo entry"
    )


# ── SHOULD-FIX: the cross-language test was verifying the client against itself


def test_the_proof_helper_uses_the_server_digest():
    """``_proof_for`` must call the server, not re-derive with the client digest.

    It used to call ``_run_digest`` (the JS implementation), so every test that
    "verified the server proof agrees" was really verifying the client against
    itself: there was no cross-language coverage at all, and a server-side
    change to the digest would have passed every one of them.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "test_7899_bounded_reload_tail.py"
    ).read_text(encoding="utf-8")
    start = src.find("def _proof_for(")
    assert start > 0
    body = src[start : start + 1400]
    assert "_transcript_prefix_proof" in body, (
        "the proof helper still derives the digest on the client side, so the "
        "server implementation is untested (#7925 SHOULD-FIX)"
    )
    assert "_run_digest" not in body, (
        "the proof helper still calls the client digest"
    )


# ── Round-3 findings: the three remaining must-fix items ────────────────────


def test_the_proof_declines_an_over_budget_prefix_without_partial_coverage():
    """#7925 round-3 MUST-FIX 1: the reported failing input (2,048 x 16 KiB
    tool rows plus 31 user rows) sits UNDER the row cap yet hashes ~32 MiB,
    which took 10.14 s against master's 0.79 s and blew the client's 8 s
    timeout. The bound is a cumulative byte budget, and an over-budget prefix
    gets NO proof rather than one that covers less than it claims."""
    import time

    from api.routes import _transcript_prefix_proof

    rows = [{"role": "user", "content": f"u{i}", "timestamp": float(i)} for i in range(31)]
    rows += [
        {"role": "assistant", "content": "x" * 16384, "timestamp": float(1000 + i)}
        for i in range(2048)
    ]
    started = time.time()
    proof = _transcript_prefix_proof(rows, len(rows), _revision="rev-budget")
    elapsed = time.time() - started
    assert proof == "", f"an over-budget prefix must decline to prove, got {proof!r}"
    assert elapsed < 8.0, (
        f"the budget pass must reject before the per-unit loop; took {elapsed:.2f}s"
    )
    # An in-budget prefix of the same shape is still proven.
    small = [{"role": "user", "content": f"u{i}", "timestamp": float(i)} for i in range(31)]
    assert _transcript_prefix_proof(small, 31, _revision="rev-ok").startswith("31:")


def test_the_proof_cache_is_safe_under_concurrent_access():
    """#7925 round-3 MUST-FIX 2: the HTTP server is threaded, and the
    get/move_to_end pair plus the insert/evict pair were unsynchronised — an
    eviction between them raised KeyError, which do_GET turned into an HTTP
    500 on a valid windowed GET."""
    import threading

    from api.routes import (
        _PROOF_CACHE_MAX_ENTRIES,
        _transcript_prefix_proof,
        _transcript_proof_cache,
    )

    rows = [{"role": "user", "content": f"m{i}"} for i in range(40)]
    _transcript_proof_cache.clear()
    errors: list = []

    def worker(worker_id: int) -> None:
        try:
            for i in range(_PROOF_CACHE_MAX_ENTRIES + 20):
                _transcript_prefix_proof(rows, 40, _revision=f"rev-{worker_id}-{i}")
        except Exception as exc:  # pragma: no cover - only on regression
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"concurrent proof access raised: {errors[:3]}"
    assert len(_transcript_proof_cache) <= _PROOF_CACHE_MAX_ENTRIES


def test_the_proof_cache_read_and_write_take_the_same_lock():
    """The lock must guard BOTH the read-modify and the insert/evict, or one
    half still races against the other."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text(
        encoding="utf-8"
    )
    idx = src.find("def _transcript_prefix_proof(")
    assert idx > 0
    body = src[idx : idx + 12000]
    assert body.count("with _PROOF_CACHE_LOCK:") >= 2, (
        "both the cache read-modify and the insert/evict must hold the lock"
    )
