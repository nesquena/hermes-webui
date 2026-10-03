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
        assert "_stitchBoundedReloadTail(S.messages, _previousReloadOffset, _reloadOffset, msgs)" in body, (
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
