"""Slice 3 registry tests for Stable Assistant Turn Anchors (#3926)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ANCHORS_JS = REPO / "static" / "assistant_turn_anchors.js"
MESSAGES_JS = REPO / "static" / "messages.js"
UI_JS = REPO / "static" / "ui.js"
SESSIONS_JS = REPO / "static" / "sessions.js"
NODE = shutil.which("node")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _event_listener_body(src: str, event_name: str) -> str:
    start = src.index(f"source.addEventListener('{event_name}'")
    end = src.find("\n    source.addEventListener(", start + 1)
    if end < 0:
        end = src.find("\n    source.onerror", start + 1)
    if end < 0:
        end = src.find("\n  }catch", start + 1)
    if end < 0:
        end = len(src)
    assert end > start
    return src[start:end]


def _function_body(src: str, name: str) -> str:
    start = src.find(f"function {name}")
    assert start != -1, f"{name} not found"
    brace = src.find("{", start)
    assert brace != -1, f"{name} body not found"
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1 : idx]
    raise AssertionError(f"{name} body did not close")


def _function_source(src: str, name: str) -> str:
    start = src.find(f"function {name}")
    assert start != -1, f"{name} not found"
    params = src.find("(", start)
    assert params != -1, f"{name} parameters not found"
    depth = 0
    close = -1
    for idx in range(params, len(src)):
        if src[idx] == "(":
            depth += 1
        elif src[idx] == ")":
            depth -= 1
            if depth == 0:
                close = idx
                break
    assert close != -1, f"{name} parameters did not close"
    brace = src.find("{", close)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name} body did not close")


def _registry_snapshot() -> dict:
    assert NODE, "node is required for assistant_turn_anchors.js registry tests"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(ANCHORS_JS))}, 'utf8');
const sandbox = {{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {{filename:'assistant_turn_anchors.js'}});
const api = sandbox.window.HermesAssistantTurnAnchors;
const registry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-1',
  turn_id:'turn-1',
}});
const results = api.applyAssistantTurnAnchorSourceEvents(registry, [
  {{type:'token', data:'{{"text":"live token"}}', lastEventId:'run-1:1', created_at:'2026-06-11T00:00:01Z'}},
  {{event:'token', payload:{{text:'replay token'}}, event_id:'run-1:1', seq:1}},
  {{event:'reasoning', payload:{{text:'thinking'}}, event_id:'run-1:2', seq:2}},
  {{event:'artifact_reference', payload:{{path:'answer.txt', kind:'workspace_file'}}, event_id:'run-1:3', seq:3}},
  {{event:'state_saved', payload:{{kind:'memory', name:'session-state'}}, event_id:'run-1:4', seq:4}},
  {{event:'stream_end', payload:{{}}, event_id:'run-1:5', seq:5}},
  {{event:'done', payload:{{}}, event_id:'run-1:6', seq:6, created_at:'2026-06-11T00:00:06Z'}},
  {{source_type:'settled_message', payload:{{role:'assistant', id:'message-final', content:'final answer', _turnUsage:{{input_tokens:8, output_tokens:13}}}}}},
], {{run_id:'run-1', stream_id:'stream-1'}});

const isolated = api.createAssistantTurnAnchorRegistry({{session_id:'sid-1', turn_id:'turn-2'}});
api.applyAssistantTurnAnchorSourceEvent(registry, {{
  event:'token',
  payload:{{text:'wrong session', session_id:'sid-2'}},
  event_id:'run-1:7',
  seq:7,
}}, {{run_id:'run-1'}});

console.log(JSON.stringify({{
  version:api.version,
  registry,
  scene:api.projectAssistantTurnAnchorActivityScene(registry,{{mode:'compact_worklog'}}),
  isolated,
  results:results.map((item)=>({{applied:item.applied, reason:item.reason}})),
}}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _shadow_snapshot() -> dict:
    assert NODE, "node is required for assistant_turn_anchors.js registry tests"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(ANCHORS_JS))}, 'utf8');
const sandbox = {{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {{filename:'assistant_turn_anchors.js'}});
const api = sandbox.window.HermesAssistantTurnAnchors;
const shadow = api.createAssistantTurnAnchorShadowSnapshot({{
  anchor:{{
    session_id:'sid-shadow',
    turn_id:'turn-shadow',
  }},
  context:{{
    run_id:'run-shadow',
    stream_id:'stream-shadow',
  }},
  sources:{{
    live_events:[
      {{type:'token', data:'{{"text":"live token"}}', lastEventId:'run-shadow:1', created_at:'2026-06-11T00:00:01Z'}},
    ],
    replay_events:[
      {{event:'token', payload:{{text:'replay duplicate'}}, event_id:'run-shadow:1', seq:1}},
      {{event:'tool_complete', payload:{{tool_call_id:'tool-1', result:'ok'}}, event_id:'run-shadow:2', seq:2}},
    ],
    settled_events:[
      {{source_type:'settled_message', payload:{{role:'assistant', id:'message-shadow', content:'shadow final'}}}},
    ],
    inflight_events:[
      {{source_type:'inflight_snapshot', payload:{{status:'restoring'}}}},
    ],
  }},
}});
console.log(JSON.stringify({{
  version:api.version,
  registry:shadow.registry,
  results:Object.fromEntries(Object.entries(shadow.results).map(([key, value]) => [
    key,
    value.map((item)=>({{applied:item.applied, reason:item.reason}})),
  ])),
}}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)

def _activity_scene_snapshot() -> dict:
    assert NODE, "node is required for assistant_turn_anchors.js registry tests"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(ANCHORS_JS))}, 'utf8');
const sandbox = {{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {{filename:'assistant_turn_anchors.js'}});
const api = sandbox.window.HermesAssistantTurnAnchors;
const registry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-scene',
  turn_id:'turn-scene',
}});
api.applyAssistantTurnAnchorSourceEvents(registry, [
  {{event:'token', payload:{{text:'progress'}}, event_id:'run-scene:1', seq:1}},
  {{event:'reasoning', payload:{{text:'private thinking'}}, event_id:'run-scene:2', seq:2}},
  {{event:'tool', payload:{{
    tool_call_id:'tool-1',
    name:'terminal',
    args:{{command:'rg anchor static'}},
    preview:'rg anchor static',
    activityBurstId:7,
    activitySegmentSeq:3,
    assistant_msg_idx:12,
    started_at:1781200000
  }}, event_id:'run-scene:3', seq:3}},
  {{event:'tool_update', payload:{{
    tool_call_id:'tool-1',
    name:'terminal',
    text:'running',
    preview:'searching workspace',
    activityBurstId:7,
    activitySegmentSeq:3,
    assistant_msg_idx:12
  }}, event_id:'run-scene:4', seq:4}},
  {{event:'tool_complete', payload:{{
    tool_call_id:'tool-1',
    name:'terminal',
    result:'done',
    output:'done',
    snippet:'done',
    is_error:false,
    duration:1.25,
    activityBurstId:7,
    activitySegmentSeq:3,
    assistant_msg_idx:12
  }}, event_id:'run-scene:5', seq:5}},
  {{event:'done', payload:{{}}, event_id:'run-scene:6', seq:6}},
  {{source_type:'settled_message', payload:{{role:'assistant', id:'message-scene', content:'final answer'}}}},
], {{run_id:'run-scene', stream_id:'stream-scene'}});
const compact = api.projectAssistantTurnAnchorActivityScene(registry, {{mode:'compact_worklog'}});
const transparent = api.projectAssistantTurnAnchorActivityScene(registry.anchor, {{mode:'transparent_stream'}});
const empty = api.projectAssistantTurnAnchorActivityScene(null, {{mode:'transparent_stream'}});
const seqlessRegistry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-seqless',
  turn_id:'turn-seqless',
}});
api.applyAssistantTurnAnchorSourceEvents(seqlessRegistry, [
  {{event:'tool', payload:{{tool_call_id:'tool-same', name:'terminal'}}}},
  {{event:'tool_update', payload:{{tool_call_id:'tool-same', text:'running'}}}},
  {{event:'tool_complete', payload:{{tool_call_id:'tool-same', result:'done'}}}},
]);
const seqless = api.projectAssistantTurnAnchorActivityScene(seqlessRegistry, {{mode:'compact_worklog'}});
const zeroRegistry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-zero',
  turn_id:'turn-zero',
}});
api.applyAssistantTurnAnchorSourceEvents(zeroRegistry, [
  {{event:'tool', payload:{{
    tool_call_id:'tool-zero',
    name:'terminal',
    activityBurstId:0,
    activitySegmentSeq:0,
    assistant_msg_idx:0
  }}, event_id:'run-zero:0', seq:0}},
]);
const zero = api.projectAssistantTurnAnchorActivityScene(zeroRegistry, {{mode:'compact_worklog'}});
console.log(JSON.stringify({{
  version:api.version,
  compact,
  transparent,
  empty,
  seqless,
  zero,
}}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _activity_scene_reconciliation_snapshot() -> dict:
    assert NODE, "node is required for assistant_turn_anchors.js registry tests"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(ANCHORS_JS))}, 'utf8');
const sandbox = {{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {{filename:'assistant_turn_anchors.js'}});
const api = sandbox.window.HermesAssistantTurnAnchors;
const registry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-reconcile',
  turn_id:'turn-reconcile',
}});
api.applyAssistantTurnAnchorSourceEvents(registry, [
  {{event:'reasoning', payload:{{text:'thinking'}}, event_id:'run-reconcile:1', seq:1}},
  {{event:'tool', payload:{{tool_call_id:'tool-1', name:'terminal', args:{{command:'rg anchor'}}}}, event_id:'run-reconcile:2', seq:2}},
  {{event:'tool_complete', payload:{{tool_call_id:'tool-1', name:'terminal', result:'ok', is_error:false}}, event_id:'run-reconcile:3', seq:3}},
  {{event:'done', payload:{{status:'done'}}, event_id:'run-reconcile:4', seq:4}},
], {{run_id:'run-reconcile', stream_id:'stream-reconcile'}});
const scene = api.projectAssistantTurnAnchorActivityScene(registry, {{mode:'transparent_stream'}});
const rendererRows = scene.activity_rows.map((row) => ({{
  row_id:row.row_id,
  kind:row.kind,
  role:row.role,
  source_event_type:row.source_event_type,
  status:row.status,
  tool_call_id:row.tool_call_id,
  tool_name:row.tool && row.tool.name,
  tool_done:row.tool && row.tool.done,
  tool_is_error:row.tool && row.tool.is_error,
}}));
const matched = api.reconcileAssistantTurnAnchorActivityScene({{
  registry,
  mode:'transparent_stream',
  renderer_rows:rendererRows,
}});
const mutatedRows = rendererRows
  .filter((row) => row.row_id !== 'run-reconcile:1')
  .map((row) => row.row_id === 'run-reconcile:3'
    ? {{...row, status:'running', tool_done:false}}
    : row);
mutatedRows.push({{
  row_id:'renderer-extra',
  kind:'tool_started',
  role:'tool',
  source_event_type:'tool',
  status:'running',
  tool_call_id:'tool-extra',
  tool_name:'terminal',
  tool_done:false,
  tool_is_error:false,
}});
const mismatched = api.reconcileAssistantTurnAnchorActivityScene({{
  scene,
  renderer_rows:mutatedRows,
}});
const duplicatedRows = rendererRows.map((row) => row.row_id === 'run-reconcile:3'
  ? {{...rendererRows[1]}}
  : row);
const duplicated = api.reconcileAssistantTurnAnchorActivityScene({{
  scene,
  renderer_rows:duplicatedRows,
}});
const mixedIdRows = rendererRows.map((row) => ({{...row}}));
delete mixedIdRows[1].row_id;
const mixedIds = api.reconcileAssistantTurnAnchorActivityScene({{
  scene,
  renderer_rows:mixedIdRows,
}});
console.log(JSON.stringify({{
  version:api.version,
  scene,
  matched,
  mismatched,
  duplicated,
  mixedIds,
}}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _renderer_snapshot_adapter_snapshot() -> dict:
    assert NODE, "node is required for assistant_turn_anchors.js registry tests"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(ANCHORS_JS))}, 'utf8');
const sandbox = {{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {{filename:'assistant_turn_anchors.js'}});
const api = sandbox.window.HermesAssistantTurnAnchors;

function node(attrs, text, children, classes) {{
  const attrMap = attrs || {{}};
  const classSet = new Set(classes || []);
  return {{
    textContent: text || '',
    dataset: {{}},
    classList: {{
      contains(name) {{ return classSet.has(name); }},
    }},
    getAttribute(name) {{
      return Object.prototype.hasOwnProperty.call(attrMap, name) ? attrMap[name] : null;
    }},
    hasAttribute(name) {{
      return Object.prototype.hasOwnProperty.call(attrMap, name);
    }},
    querySelector(selector) {{
      return children && children[selector] || null;
    }},
  }};
}}

const registry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-renderer',
  turn_id:'turn-renderer',
}});
api.applyAssistantTurnAnchorSourceEvents(registry, [
  {{event:'reasoning', payload:{{text:'thinking'}}, event_id:'run-renderer:1', seq:1}},
  {{event:'tool', payload:{{tool_call_id:'tool-1', name:'terminal', args:{{command:'rg anchor'}}}}, event_id:'run-renderer:2', seq:2}},
  {{event:'tool_complete', payload:{{tool_call_id:'tool-1', name:'terminal', result:'ok', is_error:false}}, event_id:'run-renderer:3', seq:3}},
  {{event:'done', payload:{{status:'done'}}, event_id:'run-renderer:4', seq:4}},
], {{run_id:'run-renderer', stream_id:'stream-renderer'}});
const scene = api.projectAssistantTurnAnchorActivityScene(registry, {{mode:'transparent_stream'}});
const transparentRows = [
  node({{
    'data-transparent-event-row':'1',
    'data-event-type':'thinking',
    'data-event-id':'run-renderer:1',
  }}, '', {{
    '.thinking-card-body pre': {{textContent:'thinking'}},
    '.transparent-event-preview': {{textContent:'thinking'}},
  }}, ['transparent-event-row']),
  node({{
    'data-transparent-event-row':'1',
    'data-event-type':'tool',
    'data-event-status':'Completed',
    'data-event-id':'run-renderer:3',
    'data-tool-name':'terminal',
    'data-live-tid':'tool-1',
  }}, '', {{
    '.tool-card-name': {{textContent:'terminal'}},
    '.tool-card-preview': {{textContent:'Completed'}},
  }}, ['transparent-event-row','tool-card-row']),
];
const transparentRoot = {{
  querySelectorAll(selector) {{
    if (selector.includes('transparent-event-row')) return transparentRows;
    return [];
  }},
}};
const transparentSnapshot = api.createAssistantTurnAnchorRendererSnapshot({{
  root:transparentRoot,
  mode:'transparent_stream',
  renderer:'transparent_stream',
}});
const transparentReconciliation = api.reconcileAssistantTurnAnchorRendererSnapshot({{
  scene,
  renderer_snapshot:transparentSnapshot,
}});

const matchingSnapshot = api.createAssistantTurnAnchorRendererSnapshot({{
  mode:'transparent_stream',
  rows:scene.activity_rows.map((row) => ({{
    row_id:row.row_id,
    kind:row.kind,
    role:row.role,
    source_event_type:row.source_event_type,
    status:row.status,
    tool_call_id:row.tool_call_id,
    tool_name:row.tool && row.tool.name,
    tool_done:row.tool && row.tool.done,
    tool_is_error:row.tool && row.tool.is_error,
  }})),
}});
const matchingReconciliation = api.reconcileAssistantTurnAnchorRendererSnapshot({{
  scene,
  renderer_snapshot:matchingSnapshot,
}});

const compactRows = [
  node({{'data-worklog-reason-source':'reasoning'}}, '', {{
    '.thinking-card-body pre': {{textContent:'compact thinking'}},
  }}, ['wl-reason']),
  node({{'data-tool-name':'terminal','data-tool-done':'false','data-tool-error':'false','data-live-tid':'tool-live'}}, '', {{
    '.tool-card-name': {{textContent:'terminal'}},
    '.tool-card-preview': {{textContent:'Running'}},
  }}, ['tool-card-row']),
];
const compactSnapshot = api.createAssistantTurnAnchorRendererSnapshot({{
  mode:'compact_worklog',
  rows:compactRows,
}});
const compressionSnapshot = api.createAssistantTurnAnchorRendererSnapshot({{
  mode:'compact_worklog',
  rows:[
    node({{'data-compression-card':''}}, 'Compression updated', {{}}, ['tool-card-row']),
    node({{'data-compression-card':'false','data-tool-name':'terminal'}}, '', {{
      '.tool-card-name': {{textContent:'terminal'}},
    }}, ['tool-card-row']),
  ],
}});

console.log(JSON.stringify({{
  version:api.version,
  transparentSnapshot,
  transparentReconciliation,
  matchingSnapshot,
  matchingReconciliation,
  compactSnapshot,
  compressionSnapshot,
}}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _final_projection_snapshot() -> dict:
    assert NODE, "node is required for assistant_turn_anchors.js registry tests"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(ANCHORS_JS))}, 'utf8');
const sandbox = {{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {{filename:'assistant_turn_anchors.js'}});
const api = sandbox.window.HermesAssistantTurnAnchors;
const projected = api.projectAssistantTurnAnchorSettledMessageFinalAnswer({{
  role:'assistant',
  id:'message-final',
  content:'raw content should be replaced by render-preserved content',
  _turnUsage:{{input_tokens:8, output_tokens:13}},
}}, {{
  session_id:'sid-project',
  raw_idx:7,
  content:'line one\\nline two',
}});
const projectedByRawIdx = api.projectAssistantTurnAnchorSettledMessageFinalAnswer({{
  role:'assistant',
  content:'message without id',
}}, {{
  session_id:'sid-project',
  raw_idx:11,
  content:'raw index final',
}});
const missingSession = api.projectAssistantTurnAnchorSettledMessageFinalAnswer({{
  role:'assistant',
  id:'message-missing-session',
  content:'final',
}}, {{}});
const nonAssistant = api.projectAssistantTurnAnchorSettledMessageFinalAnswer({{
  role:'user',
  id:'message-user',
  content:'user text',
}}, {{
  session_id:'sid-project',
}});
console.log(JSON.stringify({{
  version:api.version,
  projected,
  projectedByRawIdx,
  missingSession,
  nonAssistant,
}}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _hardening_snapshot() -> dict:
    assert NODE, "node is required for assistant_turn_anchors.js registry tests"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(ANCHORS_JS))}, 'utf8');
const sandbox = {{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {{filename:'assistant_turn_anchors.js'}});
const api = sandbox.window.HermesAssistantTurnAnchors;

const toolRegistry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-tool',
  turn_id:'turn-tool',
}});
const toolResults = api.applyAssistantTurnAnchorSourceEvents(toolRegistry, [
  {{event:'tool', payload:{{tool_call_id:'call-1', name:'shell'}}}},
  {{event:'tool_update', payload:{{tool_call_id:'call-1', text:'running'}}}},
  {{event:'tool_complete', payload:{{tool_call_id:'call-1', result:'ok'}}}},
  {{event:'token', payload:{{text:'first token'}}}},
  {{event:'token', payload:{{text:'second token'}}}},
]);

const runRegistry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-run',
  turn_id:'turn-run',
  run_id:'run-a',
  stream_id:'stream-a',
}});
const runMismatch = api.applyAssistantTurnAnchorSourceEvent(runRegistry, {{
  event:'token',
  payload:{{text:'wrong run'}},
  event_id:'run-b:1',
  run_id:'run-b',
  seq:1,
}});
const streamAccepted = api.applyAssistantTurnAnchorSourceEvent(runRegistry, {{
  event:'reasoning',
  payload:{{text:'same run new stream'}},
  event_id:'run-a:2',
  run_id:'run-a',
  stream_id:'stream-b',
  seq:2,
}});

const identityRegistry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-freeze',
  turn_id:'turn-freeze',
}});
identityRegistry.identity.session_id = 'mutated';

const metadataRegistry = api.createAssistantTurnAnchorRegistry({{
  session_id:'sid-meta',
  turn_id:'turn-meta',
}});
const inheritedPayload = Object.create({{
  role:'assistant',
  content:'inherited final should be ignored',
  id:'inherited-message',
  _turnUsage:{{input_tokens:99, output_tokens:99}},
}});
api.applyAssistantTurnAnchorNormalizedEvent(metadataRegistry, {{
  classification:'metadata',
  anchor_event:{{
    session_id:'sid-meta',
    turn_id:'turn-meta',
    source_event_type:'settled_message',
    local_id:'meta-1',
    payload:inheritedPayload,
  }},
}});
api.applyAssistantTurnAnchorNormalizedEvent(metadataRegistry, {{
  classification:'metadata',
  anchor_event:{{
    session_id:'sid-meta',
    turn_id:'turn-meta',
    source_event_type:'settled_message',
    local_id:'meta-2',
    payload:{{
      role:'assistant',
      id:'message-structured',
      content:[
        {{type:'text', text:'structured '}},
        {{type:'text', text:'answer'}},
      ],
      usage:{{input_tokens:1, output_tokens:1}},
      _turnUsage:{{input_tokens:8, output_tokens:13}},
    }},
  }},
}});

console.log(JSON.stringify({{
  version:api.version,
  toolRegistry,
  toolResults:toolResults.map((item)=>({{applied:item.applied, reason:item.reason}})),
  runRegistry,
  runMismatch:{{applied:runMismatch.applied, reason:runMismatch.reason}},
  streamAccepted:{{applied:streamAccepted.applied, reason:streamAccepted.reason}},
  identityRegistry,
  metadataRegistry,
}}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _race_snapshot() -> dict:
    assert NODE, "node is required for assistant_turn_anchors.js registry tests"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(ANCHORS_JS))}, 'utf8');
const sandbox = {{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {{filename:'assistant_turn_anchors.js'}});
const api = sandbox.window.HermesAssistantTurnAnchors;

function build(order) {{
  const registry = api.createAssistantTurnAnchorRegistry({{
    session_id:'sid-race',
    turn_id:'turn-race',
    run_id:'run-race',
    stream_id:'stream-race',
  }});
  const live = [
    {{event:'token', payload:{{text:'live token'}}, event_id:'run-race:1', run_id:'run-race', seq:1}},
  ];
  const replay = [
    {{event:'token', payload:{{text:'replayed duplicate'}}, event_id:'run-race:1', run_id:'run-race', seq:1}},
    {{event:'tool_complete', payload:{{tool_call_id:'tool-1', result:'ok'}}, event_id:'run-race:2', run_id:'run-race', seq:2}},
    {{event:'done', payload:{{status:'done'}}, event_id:'run-race:3', run_id:'run-race', seq:3, created_at:'2026-06-11T00:00:03Z'}},
  ];
  const settled = [
    {{source_type:'settled_message', payload:{{role:'assistant', id:'message-race', content:'race final', _turnUsage:{{input_tokens:5, output_tokens:8}}}}}},
  ];
  api.applyAssistantTurnAnchorSourceEvents(registry, live);
  if (order === 'replay-first') {{
    api.applyAssistantTurnAnchorSourceEvents(registry, replay);
    api.applyAssistantTurnAnchorSourceEvents(registry, settled);
  }} else {{
    api.applyAssistantTurnAnchorSourceEvents(registry, settled);
    api.applyAssistantTurnAnchorSourceEvents(registry, replay);
  }}
  const anchor = registry.anchor;
  return {{
    stats: registry.stats,
    dedupe_keys: registry.event_index.dedupe_keys,
    activity: anchor.activity_events.map((event) => ({{
      event_id: event.event_id,
      kind: event.kind,
      status: event.status,
      text: event.payload && event.payload.text || null,
      tool_call_id: event.payload && event.payload.tool_call_id || null,
    }})),
    terminal_state: anchor.lifecycle.terminal_state,
    final_answer: anchor.content.final_answer,
    final_message_ref: anchor.content.final_message_ref,
    usage: anchor.usage,
  }};
}}

console.log(JSON.stringify({{
  replayFirst: build('replay-first'),
  settledFirst: build('settled-first'),
}}));
"""
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_registry_owns_one_anchor_and_dedupes_live_plus_replay_events():
    data = _registry_snapshot()
    registry = data["registry"]
    anchor = registry["anchor"]

    assert data["version"] == "slice8-renderer-snapshot-adapter"
    assert [item["reason"] for item in data["results"][:2]] == [None, "duplicate"]
    assert registry["event_index"]["dedupe_keys"][:2] == [
        'event_id:"run-1:1"',
        'event_id:"run-1:2"',
    ]
    assert registry["stats"]["applied"] == 7
    assert registry["stats"]["skipped_duplicate"] == 1
    assert registry["stats"]["skipped_mismatched"] == 1

    assert anchor["identity"]["session_id"] == "sid-1"
    assert anchor["identity"]["turn_id"] == "turn-1"
    assert anchor["identity"]["run_id"] == "run-1"
    assert anchor["identity"]["stream_id"] == "stream-1"
    assert [event["kind"] for event in anchor["activity_events"]] == [
        "process_prose",
        "reasoning",
        "terminal_status",
    ]
    assert anchor["activity_events"][0]["payload"] == {"text": "live token"}


def test_registry_routes_activity_artifacts_side_effects_metadata_and_transport():
    data = _registry_snapshot()
    anchor = data["registry"]["anchor"]

    assert len(anchor["artifacts"]) == 1
    assert anchor["artifacts"][0]["source_event_type"] == "artifact_reference"
    assert anchor["artifacts"][0]["payload"] == {
        "kind": "workspace_file",
        "path": "answer.txt",
    }
    assert len(anchor["side_effects"]) == 1
    assert anchor["side_effects"][0]["source_event_type"] == "state_saved"
    assert len(anchor["metadata_events"]) == 1
    assert anchor["metadata_events"][0]["source_event_type"] == "settled_message"
    assert len(anchor["transport_events"]) == 1
    assert anchor["transport_events"][0]["source_event_type"] == "stream_end"


def test_live_state_saved_listener_attaches_side_effect_to_anchor():
    body = _event_listener_body(_read(MESSAGES_JS), "state_saved")

    assert "_applyToAnchor('state_saved',d,e,null,{render:false});" in body


def test_invisible_anchor_outcome_applies_without_repainting_live_scene():
    assert NODE, "node is required for assistant-turn ownership tests"
    apply_source = _function_source(_read(MESSAGES_JS), "_applyToAnchor")
    script = f"""
const activeSid='sid-owned-outcome';
const streamId='stream-owned-outcome';
const _anchorRegistry={{anchor:{{}}}};
const applied=[];
const _anchorApi={{
  applyAssistantTurnAnchorSourceEvent(registry,event,context){{
    applied.push({{registry,event,context}});
    return {{applied:true}};
  }},
}};
let _anchorShadowWarned=false;
let _assistantSegmentSeq=3;
let _currentActivityBurstId=4;
let renderCount=0;
function _renderAnchorLiveScene(){{ renderCount+=1; return true; }}
{apply_source}
const renderOutcome={{}};
const result=_applyToAnchor(
  'state_saved',
  {{kind:'memory',name:'session-state'}},
  {{lastEventId:'run-owned-outcome:7'}},
  renderOutcome,
  {{render:false}}
);
console.log(JSON.stringify({{result,applied,renderCount,renderOutcome}}));
"""
    result = subprocess.run(
        [NODE, "-e", script], text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["result"] == {"applied": True}
    assert data["renderCount"] == 0
    assert data["renderOutcome"] == {"rendered": False}
    assert data["applied"][0]["event"]["source_event_type"] == "state_saved"
    assert data["applied"][0]["event"]["event_id"] == "run-owned-outcome:7"
    assert data["applied"][0]["context"] == {
        "session_id": "sid-owned-outcome",
        "stream_id": "stream-owned-outcome",
    }


def test_side_effect_owner_survives_scene_settlement_and_reload():
    from api import routes

    data = _registry_snapshot()
    scene = routes._sanitize_anchor_activity_scene(data["scene"])
    messages = [
        {"role": "user", "content": "save this"},
        {"role": "assistant", "content": "final answer"},
    ]
    message_ref = routes._assistant_anchor_scene_message_ref(messages[1])
    records = {
        message_ref: {
            "version": "anchor_activity_scene_record_v1",
            "message_index": 1,
            "message_ref": message_ref,
            "stream_id": "stream-1",
            "scene": scene,
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(messages, records)
    settled_scene = hydrated[1]["_anchor_activity_scene"]

    assert settled_scene["artifacts"] == scene["artifacts"]
    assert settled_scene["artifacts"][0]["payload"] == {
        "kind": "workspace_file",
        "path": "answer.txt",
    }
    assert settled_scene["side_effects"] == scene["side_effects"]
    assert settled_scene["side_effects"][0]["source_event_type"] == "state_saved"
    assert settled_scene["side_effects"][0]["payload"] == {
        "kind": "memory",
        "name": "session-state",
    }


def test_side_effect_only_scene_is_persisted_without_creating_a_worklog():
    assert NODE, "node is required for assistant-turn ownership tests"
    messages_js = _read(MESSAGES_JS)
    has_owned_outcomes = _function_body(messages_js, "_anchorSceneHasOwnedOutcomes")
    attach_scene = _function_body(
        messages_js, "_attachProjectedAnchorSceneToLastAssistant"
    )
    script = f"""
const activeSid='sid-owned-outcome';
const streamId='stream-owned-outcome';
const _anchorRegistry={{}};
let persisted=0;
function _anchorSceneHasOwnedOutcomes(scene){{{has_owned_outcomes}}}
function _anchorSceneHasWorklogWorthyRows(){{ return false; }}
function _projectLiveAnchorActivityScene(){{
  return {{
    version:'activity_scene_v1',
    mode:'compact_worklog',
    activity_rows:[],
    artifacts:[],
    side_effects:[{{source_event_type:'state_saved',payload:{{kind:'memory'}}}}],
  }};
}}
function _completeSettledAnchorSceneForTurn(messages,index,scene){{ return scene; }}
function _persistSettledAnchorScene(){{ persisted+=1; }}
function _attachProjectedAnchorSceneToLastAssistant(messages){{{attach_scene}}}
const messages=[{{role:'assistant',content:'final answer'}}];
const renderedWorklog=_attachProjectedAnchorSceneToLastAssistant(messages);
console.log(JSON.stringify({{
  renderedWorklog,
  persisted,
  attached:messages[0]._anchor_activity_scene,
}}));
"""
    result = subprocess.run(
        [NODE, "-e", script], text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["renderedWorklog"] is False
    assert data["persisted"] == 1
    assert data["attached"]["side_effects"][0]["source_event_type"] == "state_saved"


def test_registry_updates_lifecycle_and_settled_final_projection():
    data = _registry_snapshot()
    anchor = data["registry"]["anchor"]

    assert anchor["lifecycle"]["status"] == "completed"
    assert anchor["lifecycle"]["terminal_state"] == "completed"
    assert anchor["lifecycle"]["started_at"] == "2026-06-11T00:00:01Z"
    assert anchor["lifecycle"]["completed_at"] == "2026-06-11T00:00:06Z"
    assert anchor["content"]["final_answer"] == "final answer"
    assert anchor["content"]["final_message_ref"] == "message-final"
    assert anchor["usage"] == {"input_tokens": 8, "output_tokens": 13}


def test_registry_replay_and_settlement_order_converge_on_same_anchor_state():
    data = _race_snapshot()
    replay_first = data["replayFirst"]
    settled_first = data["settledFirst"]

    assert replay_first == settled_first
    assert replay_first["stats"]["applied"] == 4
    assert replay_first["stats"]["skipped_duplicate"] == 1
    assert replay_first["dedupe_keys"] == [
        'event_id:"run-race:1"',
        'event_id:"run-race:2"',
        'event_id:"run-race:3"',
    ]
    assert replay_first["activity"] == [
        {
            "event_id": "run-race:1",
            "kind": "process_prose",
            "status": None,
            "text": "live token",
            "tool_call_id": None,
        },
        {
            "event_id": "run-race:2",
            "kind": "tool_completed",
            "status": "completed",
            "text": None,
            "tool_call_id": "tool-1",
        },
        {
            "event_id": "run-race:3",
            "kind": "terminal_status",
            "status": "completed",
            "text": None,
            "tool_call_id": None,
        },
    ]
    assert replay_first["terminal_state"] == "completed"
    assert replay_first["final_answer"] == "race final"
    assert replay_first["final_message_ref"] == "message-race"
    assert replay_first["usage"] == {"input_tokens": 5, "output_tokens": 8}


def test_registry_does_not_destructively_dedupe_seqless_local_tool_lifecycle():
    data = _hardening_snapshot()
    registry = data["toolRegistry"]
    anchor = registry["anchor"]

    assert data["version"] == "slice8-renderer-snapshot-adapter"
    assert data["toolResults"] == [
        {"applied": True, "reason": None},
        {"applied": True, "reason": None},
        {"applied": True, "reason": None},
        {"applied": True, "reason": None},
        {"applied": True, "reason": None},
    ]
    assert registry["event_index"]["dedupe_keys"] == []
    assert registry["stats"]["applied"] == 5
    assert [event["kind"] for event in anchor["activity_events"]] == [
        "tool_started",
        "tool_updated",
        "tool_completed",
        "process_prose",
        "process_prose",
    ]


def test_registry_rejects_cross_run_events_but_allows_stream_reconnects():
    data = _hardening_snapshot()
    registry = data["runRegistry"]

    assert data["runMismatch"] == {"applied": False, "reason": "mismatched_anchor"}
    assert data["streamAccepted"] == {"applied": True, "reason": None}
    assert registry["stats"]["skipped_mismatched"] == 1
    assert registry["stats"]["applied"] == 1
    assert registry["anchor"]["identity"]["run_id"] == "run-a"
    assert registry["anchor"]["identity"]["stream_id"] == "stream-a"
    assert registry["anchor"]["activity_events"][0]["stream_id"] == "stream-b"


def test_registry_identity_copy_and_metadata_reads_are_hardened():
    data = _hardening_snapshot()
    identity_registry = data["identityRegistry"]
    metadata_anchor = data["metadataRegistry"]["anchor"]

    assert identity_registry["identity"]["session_id"] == "sid-freeze"
    assert identity_registry["anchor"]["identity"]["session_id"] == "sid-freeze"
    assert metadata_anchor["content"]["final_answer"] == "structured answer"
    assert metadata_anchor["content"]["final_message_ref"] == "message-structured"
    assert metadata_anchor["usage"] == {"input_tokens": 8, "output_tokens": 13}
    assert len(metadata_anchor["metadata_events"]) == 2


def test_shadow_snapshot_feeds_current_source_families_into_one_registry_owner():
    data = _shadow_snapshot()
    registry = data["registry"]
    anchor = registry["anchor"]

    assert data["version"] == "slice8-renderer-snapshot-adapter"
    assert data["results"]["live"] == [{"applied": True, "reason": None}]
    assert data["results"]["replay"] == [
        {"applied": False, "reason": "duplicate"},
        {"applied": True, "reason": None},
    ]
    assert data["results"]["settled"] == [{"applied": True, "reason": None}]
    assert data["results"]["inflight"] == [{"applied": True, "reason": None}]

    assert registry["stats"]["applied"] == 4
    assert registry["stats"]["skipped_duplicate"] == 1
    assert anchor["identity"]["run_id"] == "run-shadow"
    assert anchor["identity"]["stream_id"] == "stream-shadow"
    assert [event["kind"] for event in anchor["activity_events"]] == [
        "process_prose",
        "tool_completed",
    ]
    assert [event["source_event_type"] for event in anchor["metadata_events"]] == [
        "settled_message",
        "inflight_snapshot",
    ]
    assert anchor["content"]["final_answer"] == "shadow final"

def test_activity_scene_projects_current_activity_events_for_both_render_modes():
    data = _activity_scene_snapshot()
    compact = data["compact"]
    transparent = data["transparent"]

    assert data["version"] == "slice8-renderer-snapshot-adapter"
    assert compact["version"] == "activity_scene_v1"
    assert transparent["version"] == "activity_scene_v1"
    assert compact["mode"] == "compact_worklog"
    assert transparent["mode"] == "transparent_stream"
    assert compact["final_answer"] == "final answer"
    assert transparent["final_answer"] == "final answer"
    assert compact["terminal_state"] == "completed"
    assert transparent["terminal_state"] == "completed"

    compact_rows = compact["activity_rows"]
    transparent_rows = transparent["activity_rows"]
    assert [row["row_id"] for row in compact_rows] == [
        "run-scene:1",
        "run-scene:2",
        "run-scene:3",
        "run-scene:4",
        "run-scene:5",
        "run-scene:6",
    ]
    assert [row["row_id"] for row in transparent_rows] == [
        row["row_id"] for row in compact_rows
    ]
    assert [row["kind"] for row in compact_rows] == [
        "process_prose",
        "reasoning",
        "tool_started",
        "tool_updated",
        "tool_completed",
        "terminal_status",
    ]
    assert [row["role"] for row in compact_rows] == [
        "prose",
        "thinking",
        "tool",
        "tool",
        "tool",
        "terminal",
    ]
    assert [row["display_hint"] for row in compact_rows] == [
        "main_prose",
        "collapsed_thinking",
        "tool_row",
        "tool_row",
        "tool_row",
        "terminal_status_row",
    ]
    assert all(row["display_hint"] == "chronological_activity" for row in transparent_rows)
    assert compact_rows[0]["text"] == "progress"
    assert compact_rows[0]["tool_call_id"] is None
    assert compact_rows[2]["tool_call_id"] == "tool-1"
    assert compact_rows[4]["text"] == "done"
    assert compact_rows[1]["thinking"] == {
        "text": "private thinking",
        "preview": "private thinking",
        "dedupe_key": "thinking:private thinking",
    }
    assert compact_rows[2]["group"] == {
        "group_key": "segment:3",
        "activity_burst_id": 7,
        "activity_segment_seq": 3,
        "assistant_msg_idx": 12,
    }
    assert compact_rows[2]["tool"] == {
        "id": "tool-1",
        "name": "terminal",
        "args": {"command": "rg anchor static"},
        "preview": "rg anchor static",
        "snippet": "",
        "result": None,
        "output": None,
        "done": False,
        "is_error": False,
        "duration": None,
        "started_at": 1781200000,
        "signature": 'terminal|tool-1|{"command":"rg anchor static"}',
    }
    assert compact_rows[4]["tool"]["done"] is True
    assert compact_rows[4]["tool"]["is_error"] is False
    assert compact_rows[4]["tool"]["duration"] == 1.25
    assert compact_rows[4]["tool"]["snippet"] == "done"
    assert compact_rows[4]["display_hints"] == {
        "compact_worklog": "tool_row",
        "transparent_stream": "chronological_activity",
    }
    seqless_ids = [row["row_id"] for row in data["seqless"]["activity_rows"]]
    assert len(seqless_ids) == len(set(seqless_ids))
    assert seqless_ids == [
        "tool-same:tool:0",
        "tool-same:tool_update:1",
        "tool-same:tool_complete:2",
    ]
    assert data["zero"]["activity_rows"][0]["group"] == {
        "group_key": "segment:0",
        "activity_burst_id": 0,
        "activity_segment_seq": 0,
        "assistant_msg_idx": 0,
    }


def test_activity_scene_is_renderer_neutral_and_empty_safe():
    data = _activity_scene_snapshot()
    compact = data["compact"]
    empty = data["empty"]

    assert "final answer" not in [row["text"] for row in compact["activity_rows"]]
    assert compact["identity"]["session_id"] == "sid-scene"
    assert compact["identity"]["run_id"] == "run-scene"
    assert compact["identity"]["stream_id"] == "stream-scene"
    assert empty == {
        "version": "activity_scene_v1",
        "mode": "transparent_stream",
        "identity": {"source_message_refs": []},
        "lifecycle": {},
        "final_answer": "",
        "final_message_ref": None,
        "terminal_state": None,
        "activity_rows": [],
        "artifacts": [],
        "side_effects": [],
    }


def test_activity_scene_reconciler_matches_renderer_snapshot_rows():
    data = _activity_scene_reconciliation_snapshot()
    matched = data["matched"]

    assert data["version"] == "slice8-renderer-snapshot-adapter"
    assert matched["version"] == "activity_scene_reconciliation_v1"
    assert matched["scene_version"] == "activity_scene_v1"
    assert matched["mode"] == "transparent_stream"
    assert matched["matched"] is True
    assert matched["summary"] == {
        "expected_count": 4,
        "actual_count": 4,
        "mismatch_count": 0,
    }
    assert matched["terminal_state"] == "completed"
    assert matched["fields"] == [
        "kind",
        "role",
        "source_event_type",
        "status",
        "tool_call_id",
        "tool_name",
        "tool_done",
        "tool_is_error",
    ]
    assert [row["row_id"] for row in matched["expected_rows"]] == [
        "run-reconcile:1",
        "run-reconcile:2",
        "run-reconcile:3",
        "run-reconcile:4",
    ]
    assert matched["actual_rows"][2]["tool_name"] == "terminal"
    assert matched["actual_rows"][2]["tool_done"] is True


def test_activity_scene_reconciler_reports_missing_changed_and_extra_rows():
    data = _activity_scene_reconciliation_snapshot()
    mismatched = data["mismatched"]
    kinds = [item["kind"] for item in mismatched["mismatches"]]

    assert mismatched["matched"] is False
    assert mismatched["summary"]["expected_count"] == 4
    assert mismatched["summary"]["actual_count"] == 4
    assert mismatched["summary"]["mismatch_count"] == len(mismatched["mismatches"])
    assert "missing_actual_row" in kinds
    assert "unexpected_actual_row" in kinds
    assert {
        "kind": "field_mismatch",
        "row_id": "run-reconcile:3",
        "field": "status",
        "expected": "completed",
        "actual": "running",
    } in mismatched["mismatches"]
    assert {
        "kind": "field_mismatch",
        "row_id": "run-reconcile:3",
        "field": "tool_done",
        "expected": True,
        "actual": False,
    } in mismatched["mismatches"]
    missing = [
        item for item in mismatched["mismatches"]
        if item["kind"] == "missing_actual_row"
    ]
    assert missing[0]["row_id"] == "run-reconcile:1"
    extra = [
        item for item in mismatched["mismatches"]
        if item["kind"] == "unexpected_actual_row"
    ]
    assert extra[0]["row_id"] == "renderer-extra"


def test_activity_scene_reconciler_reports_duplicate_renderer_row_ids():
    data = _activity_scene_reconciliation_snapshot()
    duplicated = data["duplicated"]
    kinds = [item["kind"] for item in duplicated["mismatches"]]

    assert duplicated["matched"] is False
    assert "duplicate_actual_row" in kinds
    assert "missing_actual_row" not in kinds
    assert "field_mismatch" in kinds
    duplicates = [
        item for item in duplicated["mismatches"]
        if item["kind"] == "duplicate_actual_row"
    ]
    assert duplicates == [
        {
            "kind": "duplicate_actual_row",
            "row_id": "run-reconcile:2",
            "index": 2,
            "row": duplicated["actual_rows"][2],
        }
    ]
    mismatched_fields = [
        item["field"] for item in duplicated["mismatches"]
        if item["kind"] == "field_mismatch"
    ]
    assert "kind" in mismatched_fields
    assert "tool_done" in mismatched_fields


def test_activity_scene_reconciler_uses_index_matching_for_partial_actual_row_ids():
    data = _activity_scene_reconciliation_snapshot()
    mixed = data["mixedIds"]

    assert mixed["matched"] is True
    assert mixed["summary"] == {
        "expected_count": 4,
        "actual_count": 4,
        "mismatch_count": 0,
    }
    assert mixed["actual_rows"][1]["row_id"] is None


def test_renderer_snapshot_adapter_extracts_current_renderer_rows():
    data = _renderer_snapshot_adapter_snapshot()
    transparent = data["transparentSnapshot"]
    compact = data["compactSnapshot"]
    compression = data["compressionSnapshot"]

    assert data["version"] == "slice8-renderer-snapshot-adapter"
    assert transparent["version"] == "renderer_snapshot_v1"
    assert transparent["mode"] == "transparent_stream"
    assert transparent["renderer"] == "transparent_stream"
    assert transparent["row_count"] == 2
    assert transparent["rows"][0] == {
        "row_id": "run-renderer:1",
        "order_index": 0,
        "kind": "reasoning",
        "role": "thinking",
        "source_event_type": "reasoning",
        "status": None,
        "text": "thinking",
        "tool_call_id": None,
        "tool_name": None,
        "tool_done": None,
        "tool_is_error": None,
    }
    assert transparent["rows"][1] == {
        "row_id": "run-renderer:3",
        "order_index": 1,
        "kind": "tool_completed",
        "role": "tool",
        "source_event_type": "tool_complete",
        "status": "completed",
        "text": "Completed",
        "tool_call_id": "tool-1",
        "tool_name": "terminal",
        "tool_done": True,
        "tool_is_error": False,
    }

    assert compact["mode"] == "compact_worklog"
    assert compact["rows"][0]["kind"] == "reasoning"
    assert compact["rows"][0]["text"] == "compact thinking"
    assert compact["rows"][1]["kind"] == "tool_started"
    assert compact["rows"][1]["status"] is None
    assert compact["rows"][1]["tool_done"] is False
    assert compact["rows"][1]["tool_call_id"] == "tool-live"

    assert compression["rows"][0]["kind"] == "lifecycle_status"
    assert compression["rows"][0]["source_event_type"] == "compressed"
    assert compression["rows"][1]["kind"] == "tool_started"
    assert compression["rows"][1]["tool_name"] == "terminal"


def test_renderer_snapshot_reconciliation_produces_yes_or_no_answer():
    data = _renderer_snapshot_adapter_snapshot()
    matching = data["matchingReconciliation"]
    transparent = data["transparentReconciliation"]

    assert matching["version"] == "renderer_snapshot_reconciliation_v1"
    assert matching["matched"] is True
    assert matching["reconciliation"]["summary"] == {
        "expected_count": 4,
        "actual_count": 4,
        "mismatch_count": 0,
    }

    assert transparent["version"] == "renderer_snapshot_reconciliation_v1"
    assert transparent["matched"] is False
    assert transparent["snapshot"]["row_count"] == 2
    assert transparent["reconciliation"]["summary"]["expected_count"] == 4
    assert transparent["reconciliation"]["summary"]["actual_count"] == 2
    kinds = [item["kind"] for item in transparent["reconciliation"]["mismatches"]]
    assert "row_count" in kinds
    assert "missing_actual_row" in kinds
    missing = [
        item for item in transparent["reconciliation"]["mismatches"]
        if item["kind"] == "missing_actual_row"
    ]
    assert [item["row_id"] for item in missing] == [
        "run-renderer:2",
        "run-renderer:4",
    ]


def test_final_projection_routes_settled_assistant_message_through_anchor_owner():
    data = _final_projection_snapshot()
    projected = data["projected"]
    registry = projected["registry"]
    anchor = registry["anchor"]

    assert data["version"] == "slice8-renderer-snapshot-adapter"
    assert projected["applied"] is True
    assert projected["reason"] is None
    assert projected["final_message_ref"] == "message-final"
    assert projected["final_answer"] == "line one\nline two"
    assert registry["stats"]["applied"] == 1
    assert anchor["identity"]["session_id"] == "sid-project"
    assert anchor["content"]["final_answer"] == "line one\nline two"
    assert anchor["content"]["final_message_ref"] == "message-final"
    assert anchor["usage"] == {"input_tokens": 8, "output_tokens": 13}
    assert [event["source_event_type"] for event in anchor["metadata_events"]] == [
        "settled_message",
    ]
    assert data["projectedByRawIdx"]["final_message_ref"] == "raw_idx:11"
    assert data["projectedByRawIdx"]["registry"]["anchor"]["identity"][
        "source_message_refs"
    ] == ["raw_idx:11"]
    anchor_src = _read(ANCHORS_JS)
    assert "if(!result.applied)" in anchor_src


def test_final_projection_is_scoped_to_settled_assistant_messages():
    data = _final_projection_snapshot()

    assert data["missingSession"] == {
        "applied": False,
        "reason": "missing_session",
        "final_answer": "",
        "final_message_ref": None,
        "registry": None,
    }
    assert data["nonAssistant"] == {
        "applied": False,
        "reason": "non_assistant",
        "final_answer": "",
        "final_message_ref": None,
        "registry": None,
    }


def test_render_messages_uses_anchor_projection_only_for_settled_final_prose():
    src = _read(UI_JS)
    start = src.index("function renderMessages")
    end = src.index("function _toolDisplayName", start)
    render_body = src[start:end]

    flatten_idx = render_body.index("content=content.filter(p=>p&&p.type==='text')")
    projection_idx = render_body.index("_assistantTurnAnchorSettledFinalAnswer(m, content")
    thinking_idx = render_body.index("_extractInlineThinkingFromContentForRender(content")

    assert flatten_idx < projection_idx < thinking_idx
    assert "if(m.role==='assistant'&&!m._live&&typeof content==='string'){" in render_body
    assert "createAssistantTurnAnchorRegistry" not in render_body
    assert "applyAssistantTurnAnchorSourceEvent" not in render_body
    assert "_assistantTurnAnchorSettledFinalAnswerWarned" in src
    assert "console.warn('assistant turn anchor settled-final projection failed',err)" in src


def test_registry_instances_do_not_share_owner_state():
    data = _registry_snapshot()
    isolated = data["isolated"]

    assert isolated["identity"]["turn_id"] == "turn-2"
    assert isolated["event_index"]["dedupe_keys"] == []
    assert isolated["stats"]["applied"] == 0
    assert isolated["anchor"]["activity_events"] == []

def test_live_visible_order_handoff_wires_scene_projection_without_ui_registry_ownership():
    scene_helper = "projectAssistantTurnAnchorActivityScene"
    for helper in [
        "applyAssistantTurnAnchorNormalizedEvent",
        "applyAssistantTurnAnchorSourceEvents",
        "createAssistantTurnAnchorShadowSnapshot",
        "reconcileAssistantTurnAnchorActivityScene",
    ]:
        assert helper not in _read(UI_JS)
        assert helper not in _read(SESSIONS_JS)
    assert "projectAssistantTurnAnchorSettledMessageFinalAnswer" in _read(UI_JS)
    assert scene_helper in _read(UI_JS)
    assert scene_helper not in _read(SESSIONS_JS)
    assert scene_helper in _read(MESSAGES_JS)
    assert "function _renderLiveAnchorActivitySceneForStream" in _read(UI_JS)
    assert "window._renderLiveAnchorActivitySceneForStream" in _read(SESSIONS_JS)
    assert "window._renderLiveAnchorActivitySceneForStream" in _read(MESSAGES_JS)


def test_slice6_live_shadow_feed_wires_anchor_scene_for_visible_order_handoff():
    src = _read(MESSAGES_JS)
    helper_body = src.split("function _applyToAnchor", 1)[1].split(
        "function _mergeSettledToolCallsWithLiveMetadata", 1
    )[0]

    assert "window._liveAnchorRegistries=window._liveAnchorRegistries||new Map()" in src
    assert "_anchorRegistryMap.get(streamId)" in src
    assert "_anchorRegistryMap.set(streamId,_anchorRegistry)" in src
    assert "createAssistantTurnAnchorRegistry" in src
    assert "applyAssistantTurnAnchorSourceEvent" in src
    assert "const eventId=(sseEvent&&sseEvent.lastEventId)||raw.event_id||raw.lastEventId||raw.last_event_id||'';" in helper_body
    assert helper_body.index("...raw,") < helper_body.index("source_event_type:sourceEventType")

    for event_name in [
        "interim_assistant",
        "tool",
        "tool_complete",
        "approval",
        "clarify",
        "goal_continue",
        "pending_steer_leftover",
        "compressing",
        "compressed",
        "apperror",
        "cancel",
    ]:
        assert f"_applyToAnchor('{event_name}'" in _event_listener_body(src, event_name)

    token_body = _event_listener_body(src, "token")
    assert "_scheduleRender(" in token_body
    assert "function _upsertAnchorProcessProse" in src
    assert "_upsertAnchorProcessProse(displayText" in src
    reasoning_body = _event_listener_body(src, "reasoning")
    assert "_applyToAnchor" not in reasoning_body
    assert "const liveThinkingText=_liveThinkingText();" in reasoning_body
    assert "const anchorReasoningFallback={};" in reasoning_body
    assert "if(!_upsertAnchorReasoning(liveThinkingText, anchorReasoningFallback))" in reasoning_body
    assert "_updateLiveThinkingCard(liveThinkingText,{" in reasoning_body
    assert "...anchorReasoningFallback" in reasoning_body
    assert "anchorRenderFallback:true" in reasoning_body
    assert "sessionId:activeSid" in reasoning_body
    assert "streamId" in reasoning_body
    assert "function _flushReasoningToAnchor()" in src
    assert "_upsertAnchorReasoning(reasoningText" in src
    assert "`live-reasoning:${streamId}:final`" in src
    error_body = _event_listener_body(src, "error")
    assert "_applyToAnchor('error'" not in error_body
    assert "_flushReasoningToAnchor();" in error_body
    assert "_scheduleAnchorRegistryCleanup(120000);" in error_body
    assert "_handleStreamError(source)" in error_body
    assert "projectAssistantTurnAnchorActivityScene" in src

    tool_body = _event_listener_body(src, "tool")
    assert tool_body.index("upsertLiveToolCall(d,'start')") < tool_body.index(
        "_applyToAnchor('tool'"
    )
    assert tool_body.index("_upsertAnchorProcessProse(pendingDisplayTextBeforeTool") < tool_body.index(
        "_applyToAnchor('tool'"
    )
    done_body = _event_listener_body(src, "done")
    assert "_applyToAnchor('done',{" in done_body
    assert "usage:d.usage||null" in done_body
    assert "created_at:d.created_at||null" in done_body
    assert "_applyToAnchor('done',{...d" not in done_body
    assert "_flushReasoningToAnchor();" in done_body
    assert "_scheduleAnchorRegistryCleanup();" in done_body
    assert "_attachProjectedAnchorSceneToLastAssistant(S.messages);" in done_body
    attach_body = _function_body(src, "_attachProjectedAnchorSceneToLastAssistant")
    assert "lastAsst._anchor_stream_id=streamId" in attach_body
    assert "lastAsst._anchor_activity_scene=scene" in attach_body
    assert "'_anchor_stream_id'" in src
    assert "'_anchor_activity_scene'" in src
    assert src.index("'_anchor_stream_id'") < src.index("function _carryForwardEphemeralTurnFields")
