"""#7925 re-gate round 3 — the advancing nonzero-origin window.

The re-gate reproduced one remaining defect: a bounded refresh silently becomes
repeated full-transcript downloading.

``static/sessions.js`` entered the stitch block whenever the fresh window's
origin moved forward. With ``_previousReloadOffset > 0`` the rendered transcript
already starts at a nonzero origin, and the client-side prefix can no longer be
proven to be the server's current prefix *for that earlier range* — the prefix
proof covers the window the last response described, not the one before it. The
trust gate therefore rejects the splice, ``_stitchBoundedReloadTail`` returns
null, and the refresh degrades into a bare full fetch followed by
``msg_limit=all`` reloads. Chromium measured 30 rows then 2,003; master stays at
30.

The fix gates the stitch on a PRISTINE origin (``_previousReloadOffset === 0``).
The fresh bounded response is authoritative for the window it describes, so an
advancing nonzero-origin window takes the plain replace and needs no splice at
all — nothing older than the new origin is being claimed.

These tests drive the REAL entry condition and the REAL trust gate out of
``static/sessions.js`` in node, so they pin the shipped expression rather than a
restatement of it.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile

import pytest

_SESSIONS_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static",
    "sessions.js",
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0
    or not os.path.exists(_SESSIONS_JS),
    reason="node or static/sessions.js unavailable",
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


# The stitch entry condition, sliced verbatim out of _ensureMessagesLoaded so the
# test pins the shipped expression. Everything from `if (` through the closing
# `) {` of the stitch guard.
def _stitch_guard_source() -> str:
    src = _sessions_source()
    start = src.find("_reloadOffset > _previousReloadOffset &&")
    assert start >= 0, "the stitch guard was not found in static/sessions.js"
    # Walk back to the `if (` that opens the guard.
    if_idx = src.rfind("if (", 0, start)
    assert if_idx >= 0, "the stitch guard's `if (` was not found"
    # Walk forward to the `) {` that closes it.
    depth = 0
    i = src.index("(", if_idx)
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[if_idx : i + 1]
        i += 1
    raise AssertionError("unbalanced parentheses in the stitch guard")


_DRIVER = r"""
// Evaluates the REAL stitch guard and the REAL trust gate from sessions.js.
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const payload = JSON.parse(process.argv[3]);

function extract(name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start), depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

// The guard, verbatim, as a predicate over the three values it reads.
const guardSrc = payload.guardSrc;
const guard = new Function(
  '_previousReloadOffset', '_reloadOffset', 'S',
  'return ' + guardSrc.replace(/^if\s*\(/, '(').replace(/\)\s*\{?\s*$/, ')') + ';'
);

// Declared (not merely evaluated as an expression) so the helpers can call each
// other by name in this scope.
for (const name of ['_reloadPrefixRowFingerprint', '_prefixFreshnessDigest',
                    '_boundedReloadPrefixIsTrustworthy', '_stitchBoundedReloadTail']) {
  if (src.indexOf('function ' + name + '(') < 0) throw new Error(name + ' not found');
  eval(extract(name));
}
const trustGate = _boundedReloadPrefixIsTrustworthy;
const stitch = _stitchBoundedReloadTail;
const digest = _prefixFreshnessDigest;

const out = {};
out.guardEnters = guard(payload.previousOffset, payload.reloadOffset, {messages: payload.prevMessages});
// A proof the server would mint for the spliced prefix: the same digest the
// client re-derives, computed by the client's own (algorithm-identical) helper.
out.proof = payload.serverPrefixProof !== null
  ? digest(payload.prevMessages, payload.reloadOffset)
  : null;

// What the reload path would actually do: enter the stitch, and if it returns
// null, take the full-transcript fallback.
if (out.guardEnters) {
  const trustworthy = trustGate(
    payload.prevMessages, payload.previousOffset, payload.reloadOffset,
    payload.tailMessages, out.proof
  );
  out.trustworthy = trustworthy;
  const stitched = stitch(
    payload.prevMessages, payload.previousOffset, payload.reloadOffset,
    payload.tailMessages, trustworthy
  );
  out.stitchedIsNull = stitched === null;
  out.rowCount = stitched === null ? null : stitched.length;
} else {
  out.trustworthy = null;
  out.stitchedIsNull = null;
  // Plain replace: the fresh bounded window stands on its own.
  out.rowCount = payload.tailMessages.length;
}
out.tookFullFallback = out.guardEnters && out.stitchedIsNull === true;

console.log(JSON.stringify(out));
"""


def _run(payload: dict) -> dict:
    driver = _DRIVER
    payload = dict(payload)
    payload["guardSrc"] = _stitch_guard_source()
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
    tf.write(driver)
    tf.close()
    try:
        result = subprocess.run(
            ["node", tf.name, _SESSIONS_JS, json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"node error: {result.stderr}"
        return json.loads(result.stdout)
    finally:
        os.unlink(tf.name)


def _rows(n, start=0):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"row-{i}"} for i in range(start, start + n)]


def test_the_stitch_guard_requires_a_pristine_origin():
    """The guard must carry ``_previousReloadOffset === 0``."""
    guard = _stitch_guard_source()
    assert "_previousReloadOffset === 0" in guard, (
        "the stitch guard no longer requires a pristine origin, so an advancing "
        "nonzero-origin window re-enters the splice the trust gate cannot prove"
    )
    # And it must still require the window to advance (the round-2 MUST-FIX).
    assert "_reloadOffset > _previousReloadOffset" in guard, (
        "the round-2 fix (enter only when the window moved forward) was lost"
    )


def test_an_advancing_nonzero_origin_window_does_not_take_the_full_fallback():
    """The re-gate's reproduction: 30 rows, then 2,003.

    A rendered transcript that already starts at origin 30, refreshed with a
    window at origin 60, must NOT enter the stitch (which the trust gate would
    reject and turn into a bare full fetch).
    """
    out = _run({
        "previousOffset": 30,
        "reloadOffset": 60,
        "prevMessages": _rows(30),
        "tailMessages": _rows(30),
        "serverPrefixProof": None,
    })
    assert out["guardEnters"] is False, (
        "an advancing nonzero-origin window still enters the stitch; the trust "
        "gate will reject it and the refresh becomes a full-transcript fetch"
    )
    assert out["tookFullFallback"] is False
    # The plain replace keeps exactly the fresh bounded window.
    assert out["rowCount"] == 30, (
        f"the plain replace should keep the 30-row bounded window, got {out['rowCount']}"
    )


def test_a_pristine_origin_still_stitches():
    """Control: the common fast path must keep working.

    A transcript rendered from origin 0, refreshed with a window at origin 30,
    still enters the stitch and assembles prefix + tail.
    """
    out = _run({
        "previousOffset": 0,
        "reloadOffset": 30,
        "prevMessages": _rows(60),
        "tailMessages": _rows(30),
        "serverPrefixProof": "minted",
    })
    assert out["guardEnters"] is True, (
        "a pristine origin no longer enters the stitch — the bounded-refresh fast "
        "path is broken (#7899)"
    )
    assert out["tookFullFallback"] is False, (
        "a trusted pristine-origin stitch must not fall back to a full fetch"
    )
    # 30 prefix rows retained + the 30-row tail.
    assert out["rowCount"] == 60, (
        f"expected the 30-row prefix plus the 30-row tail, got {out['rowCount']}"
    )


def test_a_non_advancing_window_still_does_not_stitch():
    """Control: the round-2 MUST-FIX stays fixed.

    A window whose origin is at or before the rendered origin covers every
    rendered row, so master's plain replace is correct and cheap.
    """
    out = _run({
        "previousOffset": 0,
        "reloadOffset": 0,
        "prevMessages": _rows(50),
        "tailMessages": _rows(50),
        "serverPrefixProof": None,
    })
    assert out["guardEnters"] is False
    assert out["tookFullFallback"] is False
    assert out["rowCount"] == 50
