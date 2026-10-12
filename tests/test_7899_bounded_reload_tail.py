"""Regression tests for #7899 — the bounded same-session reload request shape.

#7899: with a large session (multi-MB transcript), every focus/SSE
reconciliation on a >500-row session used to fall back to a bare
full-transcript GET (no msg_limit), forcing the backend to re-run the full
merge on the whole transcript each time. The fix keeps the request on the
bounded tail path, clamped to the server ceiling, and takes the plain
bounded replace — so no loaded rows are lost (Codex gate #6154) and no full
re-download happens on refresh.

#7925 re-gate SHOULD-FIX: the prefix-STITCHING half of that fix is gone. The
server sets `_messages_truncated` only when the window starts after row 0,
and the stitch was gated on a pristine rendered origin
(`_previousReloadOffset === 0`), so no real path could reach it: an
untruncated transcript takes the `msg_limit=all` reload, and a truncated one
always has a nonzero origin, which the gate refuses. What actually solves
#7899 is the reload clamp, which this file still pins. The stitch helpers,
the server-side prefix proof, and the tests that only pinned them were
removed with it; what remains drives the REAL reload request shape — the
one thing the reviewer asked to keep.

These tests pin:
1. the request construction in `_ensureMessagesLoaded` (source assertions):
   msg_limit is ALWAYS present, even when the reload window exceeds the
   server ceiling.
"""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


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
