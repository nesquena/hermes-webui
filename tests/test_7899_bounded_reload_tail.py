"""Regression tests for #7899 — bounded same-session reload tail stitching.

#7899: with a large session (multi-MB transcript), every focus/SSE
reconciliation on a >500-row session used to fall back to a bare
full-transcript GET (no msg_limit), forcing the backend to re-run the full
merge on the whole transcript each time. The fix keeps the request on the
bounded tail path (clamped to the server ceiling) and stitches the returned
tail onto the already-rendered prefix client-side, so no loaded rows are lost
(Codex gate #6154) and no full re-download happens on refresh.

The stitched offsets are GLOBAL indices into the server's full message array
(api/routes.py _message_window_for_display returns the window's absolute
start_idx as _messages_offset). The original helper treated the new global
offset as a client-local slice length, which duplicated every already-visible
turn whenever the client prefix itself began at a nonzero global origin — i.e.
exactly the >500-row truncated reload this helper exists for. The review on
PR #7925 pinned that failure, so these tests derive the overlap from BOTH
origins (previousOffset, newOffset) and assert no visible row repeats.

These tests pin:
1. the pure `_stitchBoundedReloadTail` helper behavior (node sandbox),
2. the request construction in `_ensureMessagesLoaded` (source assertions):
   msg_limit is ALWAYS present, even when the reload window exceeds the
   server ceiling, and the previous origin is captured BEFORE _oldestIdx is
   overwritten with the response's new offset.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _extract_function(name):
    start = SESSIONS_JS.find(f"function {name}(")
    if start < 0:
        raise AssertionError(f"{name} not found in sessions.js")
    brace = SESSIONS_JS.index("{", start)
    depth = 0
    end = brace
    for i in range(brace, len(SESSIONS_JS)):
        c = SESSIONS_JS[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return SESSIONS_JS[start:end]


def _run_stitch(prev, previous_offset, new_offset, tail):
    """Run _stitchBoundedReloadTail in a node sandbox and return the JSON result."""
    fn_def = _extract_function("_stitchBoundedReloadTail")
    js_code = (
        fn_def
        + "\n"
        + "const input = JSON.parse(process.argv[2]);\n"
        + "process.stdout.write(JSON.stringify("
        + "_stitchBoundedReloadTail(input.prev, input.previousOffset, input.newOffset, input.tail)));\n"
    )
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
    tf.write(js_code)
    tf.close()
    try:
        result = subprocess.run(
            ["node", tf.name, json.dumps(
                {"prev": prev, "previousOffset": previous_offset, "newOffset": new_offset, "tail": tail}
            )],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"node error: {result.stderr}")
        return json.loads(result.stdout)
    finally:
        os.unlink(tf.name)


def _msgs(prefix, lo, hi, role="user"):
    return [{"role": role, "content": f"{prefix}{i}"} for i in range(lo, hi)]


def _contents(out):
    return [m.get("content") for m in out]


class TestStitchBoundedReloadTail:
    """_stitchBoundedReloadTail pure-function behavior.

    Global-index model used throughout: the server's full message array is
    indexed 0..N-1; prevMessages are the rows [previousOffset, previousOffset+len)
    currently rendered on the client, and tailMessages are the fresh rows
    [newOffset, newOffset+len) from the bounded reload response. The overlap
    the client must drop is exactly newOffset - previousOffset rows of the
    prefix — never the raw newOffset value.
    """

    def test_identical_window_returns_fresh_tail_once(self):
        # prev = global 970..999 (30 rows), same 30 rows come back with
        # _messages_offset=970 (server appended nothing windowable). The
        # overlap is the whole prefix → the fresh tail replaces it, 30 rows,
        # each turn exactly once.
        prev = _msgs("m", 970, 1000)
        tail = _msgs("t", 970, 1000, "assistant")
        out = _run_stitch(prev, 970, 970, tail)
        assert len(out) == 30
        assert _contents(out) == _contents(tail)

    def test_truncated_reload_never_duplicates_visible_rows(self):
        # The #7925 review reproduction: a 1000-row server transcript, client
        # rendered global 970..999, the same 30 rows come back truncated with
        # _messages_offset=970. The old helper sliced prevMessages[:970] and
        # produced 60 rows with 30 duplicated turns.
        prev = _msgs("m", 970, 1000)
        tail = _msgs("t", 970, 1000, "assistant")
        out = _run_stitch(prev, 970, 970, tail)
        seen = {}
        for m in out:
            seen[m["content"]] = seen.get(m["content"], 0) + 1
        assert all(c == 1 for c in seen.values()), f"duplicated rows: {seen}"

    def test_overlapping_window_drops_only_the_overlap(self):
        # prev = global 970..999 (30 rows), fresh tail starts at 985 (server
        # returned the last 60 rows 985..1044). Overlap = 15 prefix rows, so
        # the result keeps 15 prefix rows + the 60-row tail, with no repeats.
        prev = _msgs("m", 970, 1000)
        tail = _msgs("t", 985, 1045, "assistant")
        out = _run_stitch(prev, 970, 985, tail)
        assert len(out) == 15 + 60
        assert _contents(out)[:15] == [f"m{i}" for i in range(970, 985)]
        assert _contents(out)[15:] == _contents(tail)
        assert len(set(_contents(out))) == len(out)

    def test_appended_rows_are_kept_exactly_once(self):
        # prev = global 970..999; the user sent one turn, so the server now
        # returns rows 970..1000 with offset 970. All 30 old rows stay, the
        # new row appears once.
        prev = _msgs("m", 970, 1000)
        tail = _msgs("m", 970, 1001)
        out = _run_stitch(prev, 970, 970, tail)
        assert len(out) == 31
        assert _contents(out) == [f"m{i}" for i in range(970, 1001)]
        assert _contents(out).count("m1000") == 1

    def test_prefix_gap_beats_duplication_when_client_fell_far_behind(self):
        # prev = global 970..999 (30 rows), the fresh tail starts at 1500 —
        # the client prefix is entirely older than the new window, so the
        # overlap (530) exceeds the prefix length and the whole prefix is
        # retained. Keeping the old rows plus the new tail leaves a GAP in
        # global order [1000, 1500), but never duplicates visible turns
        # (Codex gate #6154 row-retention).
        prev = _msgs("m", 970, 1000)
        tail = _msgs("t", 1500, 1530, "assistant")
        out = _run_stitch(prev, 970, 1500, tail)
        assert len(out) == 30 + 30
        assert _contents(out[:30]) == [f"m{i}" for i in range(970, 1000)]
        assert _contents(out)[30:] == _contents(tail)
        assert len(set(_contents(out))) == len(out)

    def test_no_overlap_policy_when_prefix_longer_than_overlap(self):
        # prev = global 500..974 (475 rows), fresh tail covers global 900..959.
        # The overlap is 75 prefix rows [900, 975); the retained prefix is the
        # 400 non-overlapped rows [500, 900) — a seamless stitch, no gap.
        prev = _msgs("m", 500, 975)
        tail = _msgs("t", 900, 960, "assistant")
        out = _run_stitch(prev, 500, 900, tail)
        assert len(out) == 400 + 60
        assert _contents(out[:400]) == [f"m{i}" for i in range(500, 900)]
        assert _contents(out)[400:] == _contents(tail)
        assert len(set(_contents(out))) == len(out)

    def test_zero_offset_returns_tail_unchanged(self):
        prev = [{"role": "user", "content": "old"}]
        tail = [{"role": "assistant", "content": "new"}]
        out = _run_stitch(prev, 0, 0, tail)
        assert out == tail

    def test_new_offset_at_or_below_previous_origin_returns_tail(self):
        # A smaller/equal new offset means the server re-wound its window
        # (msg_before paging, or an older cached response); the fresh tail is
        # authoritative.
        prev = _msgs("m", 500, 600)
        tail = _msgs("t", 400, 430, "assistant")
        assert _run_stitch(prev, 500, 400, tail) == tail
        assert _run_stitch(prev, 500, 500, tail) == tail

    def test_empty_prefix_returns_tail(self):
        out = _run_stitch([], 10, 20, [{"role": "user", "content": "x"}])
        assert out == [{"role": "user", "content": "x"}]

    def test_negative_or_nan_offsets_treated_as_zero_origin(self):
        tail = [{"role": "user", "content": "x"}]
        # previousOffset=-3 -> prevOrigin 0, newOffset=5 -> 5 > 0 → keep the
        # whole 1-row prefix (gap policy) + tail.
        assert _run_stitch([{"role": "user", "content": "p"}], -3, 5, tail) == [
            {"role": "user", "content": "p"},
            {"role": "user", "content": "x"},
        ]
        # previousOffset="abc" (NaN) -> prevOrigin 0, newOffset=0 -> tail
        assert _run_stitch([{"role": "user", "content": "p"}], "abc", 0, tail) == tail


class TestEnsureMessagesLoadedBoundedRequest:
    """Source assertions: _ensureMessagesLoaded never drops msg_limit."""

    def _ensure_messages_loaded_body(self):
        start = SESSIONS_JS.index("async function _ensureMessagesLoaded")
        brace = SESSIONS_JS.index("{", start)
        depth = 0
        end = brace
        for i in range(brace, len(SESSIONS_JS)):
            c = SESSIONS_JS[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        return SESSIONS_JS[start:end]

    def test_reload_limit_clamped_to_ceiling_never_null(self):
        body = self._ensure_messages_loaded_body()
        assert "_msgLimitMax" in body
        # The old #6154 fallback (boundedReloadLimit = null → bare
        # full-transcript GET) must be gone.
        assert "boundedReloadLimit ? `&msg_limit=${boundedReloadLimit}` : ''" not in body
        assert "`&msg_limit=${boundedReloadLimit}`" in body, (
            "msg_limit must ALWAYS be present on same-session reload — dropping it "
            "turns every focus/SSE reconciliation into a bare full-transcript GET (#7899)"
        )

    def test_stitch_called_with_both_origins_before_oldest_idx_overwrite(self):
        body = self._ensure_messages_loaded_body()
        assert "_stitchBoundedReloadTail(S.messages, _previousReloadOffset, _reloadOffset, msgs," in body, (
            "the bounded reload must stitch the returned tail onto the "
            "already-rendered prefix using BOTH the previous and new global "
            "offsets — passing only the new offset mis-derives the overlap and "
            "duplicates every visible turn on a >500-row session (#7925)"
        )
        # The previous origin must be captured from _oldestIdx BEFORE the
        # response's _messages_offset overwrites it below.
        cap_idx = body.index("_previousReloadOffset = Math.max(0, Number(_oldestIdx) || 0)")
        overwrite_idx = body.index("_oldestIdx = data.session._messages_offset || 0")
        assert cap_idx < overwrite_idx, (
            "capture the previous global origin BEFORE _oldestIdx is overwritten "
            "with the new response offset — otherwise the overlap math collapses "
            "to the buggy single-offset form (#7925)"
        )


# ---------------------------------------------------------------------------
# #7925 (b)/(c): prefix trustworthiness and the full-fetch fallback.
#
# The stitch is an OPTIMISATION. It is only sound while the retained prefix is
# provably rows [0, newOffset) of the server's CURRENT transcript. Where it is
# not, the reviewer's requirement (c) applies: fall back to the authoritative
# full fetch instead of inventing a transcript the server never produced.
# ---------------------------------------------------------------------------


def _run_trust(prev, previous_offset, new_offset, tail, proof):
    """Evaluate the trust gate with a server-issued prefix proof.

    The fifth argument mirrors what _ensureMessagesLoaded passes: the
    `_prefix_proof` this response minted for rows [0, _messages_offset), or
    '' when the backend omits it (the fail-closed case).

    The digest helpers are inlined ahead of the gate so the sandbox has the
    same module-scope definitions the real file provides.
    """
    fn_def = (
        _extract_function("_reloadPrefixRowFingerprint")
        + "\n"
        + _extract_function("_prefixFreshnessDigest")
        + "\n"
        + _extract_function("_boundedReloadPrefixIsTrustworthy")
    )
    js_code = (
        fn_def
        + "\n"
        + "const input = JSON.parse(process.argv[2]);\n"
        + "process.stdout.write(JSON.stringify(\n"
        + "_boundedReloadPrefixIsTrustworthy(\n"
        + "input.prev, input.previousOffset, input.newOffset, input.tail, input.proof)));\n"
    )
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
    tf.write(js_code)
    tf.close()
    try:
        result = subprocess.run(
            ["node", tf.name, json.dumps(
                {"prev": prev, "previousOffset": previous_offset, "newOffset": new_offset,
                 "tail": tail, "proof": proof}
            )],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"node error: {result.stderr}")
        return json.loads(result.stdout)
    finally:
        os.unlink(tf.name)


def _run_digest(rows, prefix_length):
    """Re-derive the prefix digest the way the client does."""
    fn_def = (
        _extract_function("_reloadPrefixRowFingerprint")
        + "\n"
        + _extract_function("_prefixFreshnessDigest")
    )
    js_code = (
        fn_def
        + "\n"
        + "const input = JSON.parse(process.argv[2]);\n"
        + "process.stdout.write(JSON.stringify(\n"
        + "_prefixFreshnessDigest(input.rows, input.prefixLength)));\n"
    )
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
    tf.write(js_code)
    tf.close()
    try:
        result = subprocess.run(
            ["node", tf.name, json.dumps({"rows": rows, "prefixLength": prefix_length})],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"node error: {result.stderr}")
        return json.loads(result.stdout)
    finally:
        os.unlink(tf.name)


def _proof_for(rows, prefix_length):
    """The server-minted proof string for rows [0, prefix_length).

    #7925 SHOULD-FIX: this used to call the CLIENT digest, so every test that
    "verified the server proof agrees" was really verifying the client against
    itself — there was no cross-language coverage at all, and a server-side
    change to the digest would have passed every one of them.

    It now calls the real ``api/routes.py:_transcript_prefix_proof``. The two
    implementations agree on realistic shapes but NOT on float ``1e-7``, a BOM,
    dict/numeric content, integers above ``2**53``, ``1e21`` or ``\\x1f`` — all of
    which fail closed, so the tests below use plain string rows.
    """
    from api.routes import _transcript_prefix_proof

    return _transcript_prefix_proof(list(rows), prefix_length)


def _row(i):
    return {"role": "user", "content": f"m{i}"}


def test_trustworthy_when_the_proof_matches_the_spliced_prefix():
    """The only trustworthy stitch: our rows hash to the server's fresh proof.

    prevOrigin=0 (the rendered transcript starts at the server's row 0), the
    window advanced to 150, and the server's proof for rows [0, 150) equals the
    digest we re-derive over our own first 150 rows. Both the geometry and the
    content check out, so the bounded tail is a legitimate splice.
    """
    prev = [_row(i) for i in range(0, 600)]
    tail = [_row(i) for i in range(150, 160)]
    assert _run_trust(prev, 0, 150, tail, _proof_for(prev, 150)) is True


def test_untrustworthy_when_the_transcript_is_a_tail_window():
    """#7925 finding 4: a client that paged in mid-transcript cannot be proven.

    prevOrigin=100 means rows [0, 100) live on the server, not on this client.
    The proof covers rows [0, 150) of the SERVER's array; the rows we hold start
    at the server's row 100, so our digest can never match. Must fail closed.
    """
    prev = [_row(i) for i in range(100, 700)]
    tail = [_row(i) for i in range(150, 160)]
    assert _run_trust(prev, 100, 150, tail, _proof_for(prev, 150)) is False


def test_untrustworthy_when_no_proof_was_issued():
    """#7925 finding 4: an older backend that omits the proof fails closed."""
    prev = [_row(i) for i in range(0, 600)]
    tail = [_row(i) for i in range(150, 160)]
    assert _run_trust(prev, 0, 150, tail, "") is False
    assert _run_trust(prev, 0, 150, tail, None) is False


def test_untrustworthy_when_the_server_rewrote_the_prefix():
    """#7925 finding 4 regression: compaction below the window origin.

    The server compacts 100 rows below our prefix and returns a window that
    starts 100 earlier. Every GEOMETRIC check still passes (offsets agree, the
    prefix reaches the gap), but the proof the server mints for rows [0, 150) of
    ITS transcript no longer hashes to the rows we hold, so the splice is
    rejected and the authoritative fetch wins — the 100 compacted-away turns
    cannot silently vanish.
    """
    held = [_row(i) for i in range(0, 600)]
    proof_over_held = _proof_for(held, 150)
    # A server that rewrote the prefix mints its proof over DIFFERENT rows for
    # the same reported offset.
    rewritten = [_row(i) for i in range(0, 600)]
    for row in rewritten[:100]:
        row["content"] = "compacted-away"
    proof_after_compaction = _proof_for(rewritten, 150)
    assert proof_over_held != proof_after_compaction
    # Our rows hash to our own proof, never the post-compaction one.
    assert _run_trust(held, 0, 150, [_row(600)], proof_over_held) is True
    assert _run_trust(held, 0, 150, [_row(600)], proof_after_compaction) is False


def test_untrustworthy_when_the_prefix_cannot_reach_the_gap():
    """The client fell further behind than its own prefix reaches.

    prevOrigin=1000, newOffset=1500, but we only hold 400 rows. Splicing
    prefix+tail would leave rows 1400-1499 invisible (silent row loss).
    """
    prev = [_row(i) for i in range(1000, 1400)]
    tail = [_row(i) for i in range(1500, 1600)]
    proof = _proof_for(prev, 500)
    assert _run_trust(prev, 1000, 1500, tail, proof) is False


def test_untrustworthy_when_the_window_moved_backwards():
    """The response offset is behind the retained prefix's origin.

    prevOrigin=500, newOffset=400: nothing newer to stitch, and the retained
    prefix is not the window the server describes.
    """
    prev = [_row(i) for i in range(500, 900)]
    tail = [_row(i) for i in range(400, 450)]
    proof = _proof_for(prev, 400)
    assert _run_trust(prev, 500, 400, tail, proof) is False


def test_untrustworthy_when_the_offset_did_not_advance():
    """Identical offsets mean the tail is not newer; a splice would duplicate."""
    prev = [_row(i) for i in range(0, 600)]
    tail = [_row(i) for i in range(0, 100)]
    proof = _proof_for(prev, 0)
    assert _run_trust(prev, 0, 0, tail, proof) is False


def test_untrustworthy_when_either_side_is_empty():
    prev = [_row(i) for i in range(100, 700)]
    proof = _proof_for(prev, 150)
    assert _run_trust(prev, 100, 150, [], proof) is False
    assert _run_trust([], 100, 150, [_row(0)], proof) is False


def test_stitch_returns_null_when_the_prefix_is_not_trustworthy():
    """The helper must signal 'cannot stitch' rather than splice blindly."""
    prev = [_row(i) for i in range(1000, 1400)]
    tail = [_row(i) for i in range(1500, 1600)]
    fn_def = _extract_function("_stitchBoundedReloadTail")
    js_code = (
        fn_def
        + "\n"
        + "const input = JSON.parse(process.argv[2]);\n"
        + "process.stdout.write(JSON.stringify("
        + "_stitchBoundedReloadTail("
        + "input.prev, input.previousOffset, input.newOffset, input.tail, false)));\n"
    )
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
    tf.write(js_code)
    tf.close()
    try:
        result = subprocess.run(
            ["node", tf.name, json.dumps(
                {"prev": prev, "previousOffset": 1000, "newOffset": 1500, "tail": tail}
            )],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) is None
    finally:
        os.unlink(tf.name)


def _ensure_messages_loaded_body():
    """The real body of _ensureMessagesLoaded, brace-matched past its strings.

    `_extract_function` stops at the first brace-balanced match, which
    truncates this function on a regex/string containing braces, so mirror the
    class helper that handles it.
    """
    src = SESSIONS_JS
    start = src.find("async function _ensureMessagesLoaded(")
    if start < 0:
        start = src.find("function _ensureMessagesLoaded(")
    assert start >= 0, "_ensureMessagesLoaded not found in sessions.js"
    brace = src.index("{", start)
    depth = 0
    end = brace
    in_str = None
    i = brace
    while i < len(src):
        c = src[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            # ${...} inside a template literal is an interpolation, not code
            # braces; the expression inside may itself contain strings.
            if in_str == "`" and c == "$" and i + 1 < len(src) and src[i + 1] == "{":
                j = i + 2
                d = 1
                while j < len(src) and d:
                    if src[j] == "{":
                        d += 1
                    elif src[j] == "}":
                        d -= 1
                    j += 1
                i = j
                continue
            if c == in_str:
                in_str = None
        elif c in "\"'`":
            in_str = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
        i += 1
    return src[start:end]


def test_reload_falls_back_to_the_full_fetch_when_the_prefix_is_untrustworthy():
    """Requirement (c): a non-provable prefix must trigger a full fetch.

    The bounded tail request is kept (it is what discovers the offset), but
    when the prefix cannot be proven the code re-issues the authoritative
    full-transcript GET rather than splicing.
    """
    body = _ensure_messages_loaded_body()
    assert "_boundedReloadPrefixIsTrustworthy(" in body, (
        "the bounded reload must gate the stitch on a provenance check (#7925)"
    )
    assert "msgs === null" in body, (
        "an unprovable prefix must make the stitch signal 'cannot stitch' (#7925)"
    )
    # Requirement (c) is asserted against the whole file because the brace-matched
    # body truncates inside this function's template literals. The authoritative
    # full-transcript GET is the one WITHOUT a msg_limit param — the bounded tail
    # request above it always appends one, so this exact form can only be the
    # fallback.
    assert SESSIONS_JS.count("&messages=1&resolve_model=0`") == 1, (
        "there must be exactly one unparameterised full-transcript GET: the "
        "untrustworthy-prefix fallback (#7925)"
    )
    full_idx = SESSIONS_JS.index("&messages=1&resolve_model=0`")
    fb_idx = SESSIONS_JS.index("#7925 (c)")
    gate_idx = SESSIONS_JS.index("if (msgs === null) {", fb_idx)
    assert fb_idx < gate_idx < full_idx, (
        "the full-transcript fetch must sit inside the `msgs === null` guard "
        "opened by the untrustworthy-prefix comment (#7925)"
    )
    read_idx = SESSIONS_JS.index(
        "msgs = (data.session.messages || []).filter(m => m && m.role)", full_idx)
    assert read_idx > full_idx, (
        "the full-fetch fallback must re-read messages from the new response (#7925)"
    )
