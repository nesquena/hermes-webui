"""#6649 — the recovered prompt keeps its CLIENT clock, so identity must use the turn start.

A recovered prompt row is displayed with the client `_ts` (that is what #8701
asserts), while the turn it belongs to is identified by the server's start
(`_server_start_ts` / `_recoveredTurnStart`). `_recoveredPromptIdentityMatch`
used to compare only `_ts` / `timestamp`, so once the turn ended and the
persisted row lost its active-turn marker, the display copy and the loaded
saved row carried different clocks and both survived — loading older messages
brought the prompt back a second time.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required to run the real helper")


def _extract(name: str) -> str:
    start = SESSIONS_JS.find("function " + name + "(")
    assert start >= 0, f"{name} not found in static/sessions.js"
    brace = SESSIONS_JS.find("{", start)
    depth = 0
    for i in range(brace, len(SESSIONS_JS)):
        if SESSIONS_JS[i] == "{":
            depth += 1
        elif SESSIONS_JS[i] == "}":
            depth -= 1
            if depth == 0:
                return SESSIONS_JS[start : i + 1]
    raise AssertionError(f"{name} braces unbalanced")


_PRELUDE = "\n".join(
    [
        "const _PENDING_ACTIVE_TURN_TS_EPSILON=1e-6;",
        "const _RECOVERED_PROMPT_TS_EPSILON=1e-6;",
        "const S={session:null};",
        _extract("_messageTimestampValue"),
        _extract("_recoveredPromptIdentityMatch"),
    ]
)


def _run(expression: str) -> dict:
    script = _PRELUDE + "\n" + expression
    done = subprocess.run(
        [NODE, "-"], input=script, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_identity_uses_the_recovered_turn_start_not_the_client_clock():
    """The display copy keeps `_ts: 100`; the persisted row says 200. Same turn."""
    out = _run(
        """
const canonical={role:'user',content:'go',timestamp:200};
const recovered={role:'user',content:'go',_ts:100,_recoveredTurnStart:200};
console.log(JSON.stringify({match:_recoveredPromptIdentityMatch(canonical,recovered)}));
"""
    )
    assert out["match"] is True, out


def test_identity_uses_the_server_start_stamp_when_that_is_all_there_is():
    """The adopted server start is the other legitimate identity carrier."""
    out = _run(
        """
const canonical={role:'user',content:'go',timestamp:200};
const recovered={role:'user',content:'go',_ts:100,_server_start_ts:200};
console.log(JSON.stringify({match:_recoveredPromptIdentityMatch(canonical,recovered)}));
"""
    )
    assert out["match"] is True, out


def test_identity_still_rejects_a_different_turn():
    """Control: a genuinely different turn must not be paired away."""
    out = _run(
        """
const canonical={role:'user',content:'go',timestamp:500};
const recovered={role:'user',content:'go',_ts:100,_recoveredTurnStart:200};
console.log(JSON.stringify({match:_recoveredPromptIdentityMatch(canonical,recovered)}));
"""
    )
    assert out["match"] is False, out
