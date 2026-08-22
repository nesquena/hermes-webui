"""Stable run identity regressions for live Anchor scene projection/hydration."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def test_runtime_journal_snapshot_preserves_run_id_separately_from_stream_id(
    monkeypatch,
):
    from api import routes

    session_id = "session-stable-run"
    run_id = "run-stable-1"
    stream_id = "stream-transport-1"
    events = [
        {
            "event": "token",
            "seq": 1,
            "event_id": f"{run_id}:1",
            "run_id": run_id,
            "created_at": 1.0,
            "payload": {"text": "working"},
        },
        {
            "event": "reasoning",
            "seq": 2,
            "event_id": f"{run_id}:2",
            "run_id": run_id,
            "created_at": 2.0,
            "payload": {"text": "checking"},
        },
        {
            "event": "tool",
            "seq": 3,
            "event_id": f"{run_id}:3",
            "run_id": run_id,
            "created_at": 3.0,
            "payload": {"name": "terminal", "tid": "call-1"},
        },
        {
            "event": "tool_complete",
            "seq": 4,
            "event_id": f"{run_id}:4",
            "run_id": run_id,
            "created_at": 4.0,
            "payload": {"name": "terminal", "tid": "call-1", "preview": "ok"},
        },
    ]

    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda lookup_id: {
            "session_id": session_id,
            # The current summary is keyed by the legacy lookup id, while the
            # durable event envelope already carries the stable run identity.
            "run_id": stream_id,
            "stream_id": stream_id,
            "last_seq": 4,
            "last_event_id": f"{run_id}:4",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda loaded_session_id, lookup_id: {"events": events},
    )

    snapshot = routes._run_journal_live_snapshot(stream_id)
    scene = snapshot["anchor_activity_scene"]

    assert snapshot["stream_id"] == stream_id
    assert scene["identity"]["run_id"] == run_id
    assert scene["identity"]["stream_id"] == stream_id
    assert scene["activity_rows"]
    assert [row["role"] for row in scene["activity_rows"]] == [
        "prose",
        "thinking",
        "tool",
    ]
    assert {row["run_id"] for row in scene["activity_rows"]} == {run_id}
    assert {row["stream_id"] for row in scene["activity_rows"]} == {stream_id}
    assert {row["identity"]["run_id"] for row in scene["activity_rows"]} == {run_id}
    assert {row["identity"]["stream_id"] for row in scene["activity_rows"]} == {
        stream_id
    }


def test_runtime_journal_lifecycle_shell_preserves_stable_run_id(monkeypatch):
    from api import routes

    run_id = "run-stable-lifecycle"
    stream_id = "stream-transport-lifecycle"
    event = {
        "event": "metering",
        "seq": 1,
        "event_id": f"{run_id}:1",
        "run_id": run_id,
        "payload": {},
    }
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda lookup_id: {
            "session_id": "session-stable-lifecycle",
            "run_id": stream_id,
            "stream_id": stream_id,
            "last_seq": 1,
            "last_event_id": f"{run_id}:1",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda loaded_session_id, lookup_id: {"events": [event]},
    )

    snapshot = routes._run_journal_live_snapshot(stream_id)
    scene = snapshot["anchor_activity_scene"]
    row = scene["activity_rows"][0]

    assert row["role"] == "lifecycle"
    assert row["run_id"] == run_id
    assert row["stream_id"] == stream_id
    assert row["identity"]["run_id"] == run_id
    assert row["identity"]["stream_id"] == stream_id


@pytest.mark.parametrize(
    ("event_run_id", "event_id"),
    [
        ({"bad": "id"}, None),
        ("run-conflicting-envelope", "run-conflicting-event-id:1"),
    ],
)
def test_runtime_journal_malformed_envelope_run_id_falls_back_to_transport_cursor(
    monkeypatch,
    event_run_id,
    event_id,
):
    from api import routes

    stream_id = "stream-current-1"
    event = {
        "event": "token",
        "seq": 1,
        "run_id": event_run_id,
        "payload": {"text": "hello"},
    }
    if event_id is not None:
        event["event_id"] = event_id

    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda lookup_id: {
            "session_id": "session-malformed-envelope",
            "run_id": stream_id,
            "stream_id": stream_id,
            "last_seq": 1,
            "last_event_id": f"{stream_id}:1",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda loaded_session_id, lookup_id: {"events": [event]},
    )

    snapshot = routes._run_journal_live_snapshot(stream_id)
    scene = snapshot["anchor_activity_scene"]
    row = scene["activity_rows"][0]

    assert scene["identity"]["run_id"] == stream_id
    assert row["run_id"] == stream_id
    assert row["identity"]["run_id"] == stream_id
    assert snapshot["last_event_id"] == f"{stream_id}:1"
    assert (
        routes._parse_run_journal_after_seq(
            {"after_event_id": [snapshot["last_event_id"]]},
            stream_id,
        )
        == 1
    )


@pytest.mark.skipif(
    NODE is None, reason="node is required for browser hydration regression coverage"
)
def test_browser_scene_hydration_prefers_stable_run_id_with_legacy_stream_fallback():
    messages_path = ROOT / "static" / "messages.js"
    script = f"""
const fs=require('fs');
const src=fs.readFileSync({json.dumps(str(messages_path))},'utf8');
function extractFunc(name){{
  const start=src.indexOf('function '+name);
  if(start===-1) throw new Error(name+' not found');
  const params=src.indexOf('(',start);
  let depth=0,close=-1;
  for(let i=params;i<src.length;i+=1){{
    if(src[i]==='(') depth+=1;
    else if(src[i]===')'){{
      depth-=1;
      if(depth===0){{ close=i; break; }}
    }}
  }}
  const brace=src.indexOf('{{',close);
  depth=0;
  for(let i=brace;i<src.length;i+=1){{
    if(src[i]==='{{') depth+=1;
    else if(src[i]==='}}'){{
      depth-=1;
      if(depth===0) return src.slice(start,i+1);
    }}
  }}
  throw new Error(name+' body did not close');
}}

const activeSid='session-stable-run';
const streamId='stream-transport-1';
let _anchorShadowWarned=false;
let _anchorRegistry={{anchor:{{identity:{{run_id:null,stream_id:streamId}}}}}};
const applied=[];
const _anchorApi={{
  applyAssistantTurnAnchorSourceEvent(registry,event,context){{
    applied.push({{event,context}});
    registry.anchor.identity.run_id=event.run_id||context.run_id||null;
    registry.anchor.identity.stream_id=event.stream_id||context.stream_id||null;
    return event;
  }},
}};
eval(extractFunc('_sourceEventTypeForSnapshotAnchorRow'));
eval(extractFunc('_hydrateAnchorRegistryFromActivityScene'));

function scene(identity,rowId){{
  return {{
    version:'activity_scene_v1',
    identity,
    activity_rows:[{{
      row_id:rowId,
      role:'prose',
      kind:'process_prose',
      source_event_type:'token',
      payload:{{text:'working'}},
      text:'working',
    }}],
  }};
}}

const stableHydrated=_hydrateAnchorRegistryFromActivityScene(scene({{
  run_id:'run-stable-1',
  stream_id:streamId,
}},'stable-row'));
const stable=applied[0];

_anchorRegistry={{anchor:{{identity:{{run_id:null,stream_id:streamId}}}}}};
const legacyHydrated=_hydrateAnchorRegistryFromActivityScene(scene({{
  stream_id:streamId,
}},'legacy-row'));
const legacy=applied[1];

const sceneScopedStreamId='stream-scene-scoped';
_anchorRegistry={{anchor:{{identity:{{run_id:null,stream_id:sceneScopedStreamId}}}}}};
const fallbackHydrated=_hydrateAnchorRegistryFromActivityScene(scene({{
  stream_id:sceneScopedStreamId,
}},null));
const fallback=applied[2];

console.log(JSON.stringify({{
  stableHydrated,
  legacyHydrated,
  fallbackHydrated,
  stable,
  legacy,
  fallback,
}}));
"""
    result = subprocess.run(
        [NODE, "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["stableHydrated"] is True
    assert data["stable"]["event"]["run_id"] == "run-stable-1"
    assert data["stable"]["event"]["stream_id"] == "stream-transport-1"
    assert data["stable"]["context"]["run_id"] == "run-stable-1"
    assert data["stable"]["context"]["stream_id"] == "stream-transport-1"
    assert data["legacyHydrated"] is True
    assert data["legacy"]["event"]["run_id"] == "stream-transport-1"
    assert data["legacy"]["context"]["run_id"] == "stream-transport-1"
    assert data["fallbackHydrated"] is True
    assert data["fallback"]["event"]["local_id"] == "snapshot:stream-scene-scoped:0"
