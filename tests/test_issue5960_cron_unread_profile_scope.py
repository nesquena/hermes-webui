"""Regression coverage for cross-profile cron unread badges (#5960)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 1
    pos = brace + 1
    while depth and pos < len(source):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
        pos += 1
    assert depth == 0
    return source[start:pos]


def test_recent_handler_reuses_dispatcher_cron_context_without_nesting():
    dispatch_start = ROUTES_PY.index('if parsed.path == "/api/crons/recent":')
    dispatch_end = ROUTES_PY.index('if parsed.path == "/api/crons/status":', dispatch_start)
    dispatch = ROUTES_PY[dispatch_start:dispatch_end]
    handler_start = ROUTES_PY.index("def _handle_cron_recent(")
    handler_end = ROUTES_PY.index("\ndef ", handler_start + 1)
    handler = ROUTES_PY[handler_start:handler_end]

    assert "with cron_profile_context():" in dispatch
    assert "cron_profile_context_for_home" not in handler


def test_successful_profile_switch_resets_unread_cron_state():
    switch_start = PANELS_JS.index("async function switchToProfile(name) {")
    switch_end = PANELS_JS.index("// ── Cron completion alerts", switch_start)
    switch_body = PANELS_JS[switch_start:switch_end]

    state_update = switch_body.index("S.activeProfile = data.active || name;")
    reset_call = switch_body.index("_resetCronUnreadForProfileSwitch();")
    assert reset_call > state_update

    reset_start = PANELS_JS.index("function _resetCronUnreadForProfileSwitch(){")
    reset_end = PANELS_JS.index("\n}", reset_start)
    reset_body = PANELS_JS[reset_start:reset_end]
    assert "_cronPollGeneration++;" in reset_body
    assert "_cronNewJobIds.clear();" in reset_body
    assert "_cronPollSince=Date.now()/1000;" in reset_body
    assert "updateCronBadge();" in reset_body


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_poll_started_before_switch_cannot_recreate_unread_state():
    polling = _extract_function(PANELS_JS, "startCronPolling")
    reset = _extract_function(PANELS_JS, "_resetCronUnreadForProfileSwitch")
    script = f"""
let _cronPollSince=10;
let _cronPollTimer=null;
let _cronUnreadCount=0;
let _cronPollGeneration=0;
const _cronNewJobIds=new Set();
let intervalCallback=null;
let resolveApi;
global.document={{hidden:false}};
global.setInterval=(callback)=>{{ intervalCallback=callback; return 1; }};
global.api=()=>new Promise(resolve=>{{ resolveApi=resolve; }});
global.showToast=()=>{{}};
global.t=(key)=>key;
global.updateCronBadge=()=>{{ _cronUnreadCount=_cronNewJobIds.size; }};
{polling}
{reset}
startCronPolling();
(async()=>{{
  const stalePoll=intervalCallback();
  _resetCronUnreadForProfileSwitch();
  resolveApi({{completions:[{{job_id:'old-profile-job',completed_at:20}}]}});
  await stalePoll;
  process.stdout.write(JSON.stringify({{
    unread:Array.from(_cronNewJobIds),
    count:_cronUnreadCount,
    generation:_cronPollGeneration,
  }}));
}})().catch(error=>{{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    state = json.loads(result.stdout)
    assert state == {"unread": [], "count": 0, "generation": 1}
