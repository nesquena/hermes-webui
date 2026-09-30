"""Regression tests for #7899 — bounded same-session reload tail stitching.

#7899: with a large session (multi-MB transcript), every focus/SSE
reconciliation on a >500-row session used to fall back to a bare
full-transcript GET (no msg_limit), forcing the backend to re-run the full
merge on the whole transcript each time. The fix keeps the request on the
bounded tail path (clamped to the server ceiling) and stitches the returned
tail onto the already-rendered prefix client-side, so no loaded rows are lost
(Codex gate #6154) and no full re-download happens on refresh.

These tests pin:
1. the pure `_stitchBoundedReloadTail` helper behavior (node sandbox),
2. the request construction in `_ensureMessagesLoaded` (source assertions):
   msg_limit is ALWAYS present, even when the reload window exceeds the
   server ceiling.
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


def _run_stitch(prev, offset, tail):
    """Run _stitchBoundedReloadTail in a node sandbox and return the JSON result."""
    fn_def = _extract_function("_stitchBoundedReloadTail")
    js_code = (
        fn_def
        + "\n"
        + "const input = JSON.parse(process.argv[2]);\n"
        + "process.stdout.write(JSON.stringify("
        + "_stitchBoundedReloadTail(input.prev, input.offset, input.tail)));\n"
    )
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
    tf.write(js_code)
    tf.close()
    try:
        result = subprocess.run(
            ["node", tf.name, json.dumps({"prev": prev, "offset": offset, "tail": tail})],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"node error: {result.stderr}")
        return json.loads(result.stdout)
    finally:
        os.unlink(tf.name)


class TestStitchBoundedReloadTail:
    """_stitchBoundedReloadTail pure-function behavior."""

    def test_seamless_stitch_when_prefix_covers_clipped_region(self):
        prev = [{"role": "user", "content": f"m{i}"} for i in range(600)]
        tail = [{"role": "assistant", "content": f"t{i}"} for i in range(30)]
        out = _run_stitch(prev, 570, tail)
        assert len(out) == 600
        assert out[:570] == prev[:570]
        assert out[570:] == tail

    def test_keeps_entire_prefix_when_client_fell_behind(self):
        # offset beyond the client prefix: keep every rendered row, append tail.
        prev = [{"role": "user", "content": f"m{i}"} for i in range(600)]
        tail = [{"role": "assistant", "content": f"t{i}"} for i in range(30)]
        out = _run_stitch(prev, 900, tail)
        assert len(out) == 630
        assert out[:600] == prev
        assert out[600:] == tail

    def test_zero_offset_returns_tail_unchanged(self):
        prev = [{"role": "user", "content": "old"}]
        tail = [{"role": "assistant", "content": "new"}]
        out = _run_stitch(prev, 0, tail)
        assert out == tail

    def test_empty_prefix_returns_tail(self):
        out = _run_stitch([], 10, [{"role": "user", "content": "x"}])
        assert out == [{"role": "user", "content": "x"}]

    def test_negative_or_nan_offset_treated_as_zero(self):
        tail = [{"role": "user", "content": "x"}]
        assert _run_stitch([{"role": "user", "content": "p"}], -3, tail) == tail
        assert _run_stitch([{"role": "user", "content": "p"}], "abc", tail) == tail


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

    def test_stitch_called_with_reload_offset(self):
        body = self._ensure_messages_loaded_body()
        assert "_stitchBoundedReloadTail(S.messages, _reloadOffset, msgs)" in body, (
            "the bounded reload must stitch the returned tail onto the "
            "already-rendered prefix (_stitchBoundedReloadTail) — without it, "
            "clamping to the ceiling would silently shrink the transcript (#6154)"
        )
