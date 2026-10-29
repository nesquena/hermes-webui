from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
ANCHORS_JS = ROOT / "static" / "assistant_turn_anchors.js"
NODE = shutil.which("node")


def _function_source(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    params = source.index("(", start)
    depth = 0
    close = -1
    for index in range(params, len(source)):
        if source[index] == "(":
            depth += 1
        elif source[index] == ")":
            depth -= 1
            if depth == 0:
                close = index
                break
    assert close >= 0, f"{name} params did not close"
    brace = source.index("{", close)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"{name} did not close")


@pytest.mark.skipif(not NODE, reason="node is required")
def test_outcome_only_runtime_scene_hydrates_state_saved_into_anchor_registry():
    hydrate = _function_source(MESSAGES_JS, "_hydrateAnchorRegistryFromActivityScene")
    source_type = _function_source(MESSAGES_JS, "_sourceEventTypeForSnapshotAnchorRow")
    script = f"""
const assert=require('assert');
const fs=require('fs');
const vm=require('vm');
const activeSid='sid-state-saved';
const streamId='stream-state-saved';
let _anchorShadowWarned=false;
const sandbox={{window:{{}},console}};
sandbox.globalThis=sandbox.window;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(ROOT / "static" / "assistant_turn_anchors.js"))},'utf8'),sandbox);
const _anchorApi=sandbox.window.HermesAssistantTurnAnchors;
const _anchorRegistry=_anchorApi.createAssistantTurnAnchorRegistry({{
  session_id:activeSid,
  stream_id:streamId,
  run_id:'run-state-saved',
}});
{source_type}
{hydrate}
const scene={{
  version:'activity_scene_v1',
  identity:{{session_id:activeSid,run_id:'run-state-saved',stream_id:streamId}},
  activity_rows:[],
  artifacts:[],
  side_effects:[{{
    source_event_type:'state_saved',
    event_id:'run-state-saved:7',
    session_id:activeSid,
    run_id:'run-state-saved',
    stream_id:streamId,
    seq:7,
    created_at:123.5,
    payload:{{session_id:activeSid,kind:'memory',action:'saved'}},
  }}],
}};
assert.strictEqual(_hydrateAnchorRegistryFromActivityScene(scene),true);
assert.strictEqual(_anchorRegistry.anchor.side_effects.length,1);
const recovered=_anchorRegistry.anchor.side_effects[0];
assert.strictEqual(recovered.source_event_type,'state_saved');
assert.strictEqual(recovered.event_id,'run-state-saved:7');
assert.strictEqual(recovered.session_id,activeSid);
assert.strictEqual(recovered.run_id,'run-state-saved');
assert.strictEqual(recovered.stream_id,streamId);
assert.strictEqual(recovered.payload.session_id,undefined);
assert.strictEqual(recovered.payload.kind,'memory');
assert.strictEqual(recovered.payload.action,'saved');
// Exact scene replay is idempotent at the real registry seam.
assert.strictEqual(_hydrateAnchorRegistryFromActivityScene(scene),true);
assert.strictEqual(_anchorRegistry.anchor.side_effects.length,1);
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not NODE, reason="node is required")
def test_server_snapshot_keeps_outcome_only_scene_and_surfaces_truncation_once():
    snapshot_helper = _function_source(SESSIONS_JS, "_serverLiveSnapshotInflight")
    notify_helper = _function_source(
        SESSIONS_JS, "_notifyRunJournalSideEffectRecoveryTruncation"
    )
    script = f"""
const assert=require('assert');
globalThis.window=globalThis;
const toasts=[];
function showToast(...args){{ toasts.push(args); }}
{snapshot_helper}
{notify_helper}
const snapshot={{
  stream_id:'stream-state-saved',
  last_seq:9,
  last_event_id:'run-state-saved:9',
  messages:[],
  tool_calls:[],
  anchor_activity_scene:{{
    version:'activity_scene_v1',
    identity:{{session_id:'sid-state-saved',run_id:'run-state-saved',stream_id:'stream-state-saved'}},
    activity_rows:[],
    artifacts:[],
    side_effects:[{{
      source_event_type:'state_saved',
      event_id:'run-state-saved:7',
      payload:{{session_id:'sid-state-saved',kind:'memory',action:'saved'}},
    }}],
    side_effects_truncated:true,
  }},
}};
const inflight=_serverLiveSnapshotInflight(snapshot,[]);
assert.ok(inflight);
assert.strictEqual(inflight.anchorActivityScene.side_effects.length,1);
assert.strictEqual(inflight.anchorActivityScene.side_effects_truncated,true);
assert.strictEqual(
  _notifyRunJournalSideEffectRecoveryTruncation(inflight,'sid-state-saved','stream-state-saved'),
  true
);
assert.strictEqual(toasts.length,1);
assert.match(String(toasts[0][0]),/saved-state updates/i);
assert.strictEqual(toasts[0][2],'warning');
// Same recovered snapshot warns only once per page lifetime.
assert.strictEqual(
  _notifyRunJournalSideEffectRecoveryTruncation(inflight,'sid-state-saved','stream-state-saved'),
  false
);
assert.strictEqual(toasts.length,1);
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_load_session_notifies_when_selected_recovery_snapshot_was_truncated():
    assert "_notifyRunJournalSideEffectRecoveryTruncation(" in SESSIONS_JS
    load_start = SESSIONS_JS.index("async function loadSession")
    load_block = SESSIONS_JS[load_start : load_start + 60000]
    compact_load = "".join(load_block.split())
    assert (
        "_notifyRunJournalSideEffectRecoveryTruncation("
        "liveRecoveryInflight,sid,activeStreamId)"
    ) in compact_load
