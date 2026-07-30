"""Regression coverage for #4930 — a settled turn must not render a fabricated
"Processed 15h 32m" from a stale `pending_started_at`.

`_anchorSceneTurnDurationForSettlement` prefers a recorded live duration
(`lastAsst._turnDuration` / `base.turn_duration`). Its last-resort fallback used
to compute `(Date.now()/1000) - session.pending_started_at` unconditionally — but
`pending_started_at` is the start of an IN-FLIGHT turn, so for a settled turn it
is either stale (a session that sat idle) or about a different pending turn,
producing a wildly wrong duration. The fallback is now gated on a turn actually
being in flight (active_stream_id / pending_user_message); otherwise it returns
no duration rather than a fabricated one.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
MESSAGES_JS = REPO_ROOT / "static" / "messages.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# Grab the nested function body and eval it with a controllable global S.
_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const m = src.match(/function _anchorSceneTurnDurationForSettlement\([^]*?\n  }/);
if (!m) throw new Error('_anchorSceneTurnDurationForSettlement not found');
const payload = JSON.parse(process.argv[3] || '{}');
global.S = payload.S || null;
// Pin Date.now well past any pending_started_at so a stale stamp would yield a
// large bogus elapsed if the gate were absent.
const FIXED_NOW = (payload.nowSeconds || 0) * 1000;
Date.now = () => FIXED_NOW;
eval('global._fn = (' + m[0] + ');');
const out = global._fn(payload.lastAsst || null, payload.base || null);
process.stdout.write(JSON.stringify({ duration: out === undefined ? null : out }));
"""


def _run(payload: dict) -> dict:
    assert NODE is not None
    p = REPO_ROOT / ".tmp_4930_driver.js"
    p.write_text(_DRIVER, encoding="utf-8")
    try:
        result = subprocess.run(
            [NODE, str(p), str(MESSAGES_JS), json.dumps(payload)],
            capture_output=True, text=True, timeout=30,
        )
    finally:
        p.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def test_stale_pending_started_at_not_used_when_no_turn_in_flight():
    """Settled turn, no live duration, stale pending_started_at, NOT in flight →
    no fabricated duration (the #4930 bug)."""
    out = _run({
        "S": {"session": {"pending_started_at": 1000.0}},  # ~15.5h before now
        "nowSeconds": 1000.0 + 55920,  # 15h 32m later
        "lastAsst": {},
        "base": {},
    })
    assert out["duration"] is None, f"stale pending_started_at must not produce a duration: {out}"


def test_pending_started_at_used_when_turn_in_flight():
    """When a turn IS in flight, the elapsed-from-pending_started_at fallback is
    still used (live progress timer behavior preserved)."""
    out = _run({
        "S": {"session": {"pending_started_at": 1000.0, "active_stream_id": "s1"}},
        "nowSeconds": 1000.0 + 5,  # 5s in flight
        "lastAsst": {},
        "base": {},
    })
    assert out["duration"] is not None and abs(out["duration"] - 5) < 0.5, out


def test_recorded_live_duration_always_wins():
    """A recorded _turnDuration is returned regardless of the in-flight gate."""
    out = _run({
        "S": {"session": {"pending_started_at": 1000.0}},
        "nowSeconds": 1000.0 + 55920,
        "lastAsst": {"_turnDuration": 4.2},
        "base": {},
    })
    assert abs(out["duration"] - 4.2) < 0.001, out


def test_base_turn_duration_wins_over_fallback():
    out = _run({
        "S": {"session": {"pending_started_at": 1000.0}},
        "nowSeconds": 1000.0 + 55920,
        "lastAsst": {},
        "base": {"turn_duration": 7.5},
    })
    assert abs(out["duration"] - 7.5) < 0.001, out
