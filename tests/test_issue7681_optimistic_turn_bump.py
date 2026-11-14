"""Behaviour tests for the #7681 finding-6 optimistic turn bump.

The bump guard lives in static/sessions.js inside
``upsertActiveSessionForLocalTurn``. Pinning it by grepping the source (the
round-3 approach) passed while the compounding bug was live, so these tests
extract the real block and execute it under node with the pass sequence send()
actually produces.

Each pass re-reads ``S.session.user_message_count`` — the value the previous pass
has already overwritten — which is precisely what defeated the old guard.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
HARNESS = pathlib.Path(__file__).resolve().parent / "harness_f6_bump.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to execute the bump harness"
)


def _run(cases: dict) -> dict:
    proc = subprocess.run(
        ["env", "-u", "NODE_CHANNEL_FD", "node", str(HARNESS), str(REPO), json.dumps(cases)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO),
    )
    assert proc.returncode == 0, f"harness failed: {proc.stderr[-800:]}"
    return json.loads(proc.stdout)


def test_three_send_passes_bump_the_count_exactly_once():
    """#7681 finding 6: send() runs three optimistic update passes over the same
    pending turn. The count must advance once (40 -> 41), not compound."""
    out = _run({"three_passes": {"server": 40, "window": 5, "passes": 3}})
    assert out["three_passes"] == [41, 41, 41], (
        f"expected the pending turn to bump the count exactly once (40 -> 41), "
        f"got {out['three_passes']} — the guard is letting the passes compound"
    )


def test_two_passes_also_bump_once():
    """The provisional-title path only runs two passes; same invariant."""
    out = _run({"two_passes": {"server": 40, "window": 5, "passes": 2}})
    assert out["two_passes"] == [41, 41], (
        f"expected [41, 41], got {out['two_passes']}"
    )


def test_short_session_window_is_not_bumped():
    """Counterpart: when the local window already reflects the new turn, the
    max(server, window) path handles it and no +1 is applied."""
    out = _run({"short_session": {"server": 3, "window": 4, "passes": 3}})
    assert out["short_session"] == [4, 4, 4], (
        f"expected the window total (4) with no extra bump, got {out['short_session']}"
    )


def test_marker_is_cleared_when_the_send_finishes():
    """#7681 finding 6 (second half): the marker must not outlive the pending turn.

    send()'s finally clears it so the NEXT send on the same session can bump
    again. Without that, the next send sees ``alreadyBumped`` true for its own
    baseline and the sidebar stops advancing entirely.
    """
    src = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
    finally_idx = src.rindex("}finally{", 0, src.index("async function startRegeneration"))
    finally_block = src[finally_idx:]
    assert "_optimisticTurnBumpSid" in finally_block, (
        "send()'s finally must clear the optimistic turn-bump marker, or the "
        "next send on the same session can never bump the count again"
    )
    assert "_optimisticTurnBumpBase" in finally_block, (
        "send()'s finally must clear both halves of the bump marker"
    )
