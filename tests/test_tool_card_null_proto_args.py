"""Regression tests for null-prototype and non-primitive tool arg/result rendering."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
ANCHORS_JS_PATH = ROOT / "static" / "assistant_turn_anchors.js"
NODE = shutil.which("node")


def _function_source(src: str, name: str, *, required: bool = True) -> str:
    match = re.search(rf"function\s+{re.escape(name)}\s*\(", src)
    if not match:
        if not required:
            return ""
        raise AssertionError(f"{name}() not found")
    brace = src.find("{", match.end())
    assert brace != -1, f"{name}() has no body"
    depth = 1
    i = brace + 1
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    while i < len(src) and depth:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < len(src) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 1
        elif in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
        else:
            if ch == "/" and nxt == "/":
                in_line_comment = True
                i += 1
            elif ch == "/" and nxt == "*":
                in_block_comment = True
                i += 1
            elif ch in ("'", '"', "`"):
                in_string = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        i += 1
    assert depth == 0, f"unbalanced braces in {name}()"
    return src[match.start() : i]


def _run_node_harness(body_js: str) -> dict:
    assert NODE, "node is required for frontend regression tests"
    optional_fns = ["_stringifyToolValue"]
    required_fns = [
        "_toolDisplayName",
        "_decodeToolLabelEntities",
        "_redactToolTargetLabel",
        "_shortToolLabel",
        "_toolI18n",
        "_toolPathBasename",
        "_toolActionKind",
        "_toolTargetLabel",
        "_toolDelegateTargetLabel",
        "_toolReadRangeLabel",
        "_toolFullCommandLabel",
        "_toolVisibleTargetLabel",
        "_toolActionLabelText",
        "_toolArgPreviewValue",
        "_toolArgPreviewKeyIsHidden",
        "_formatToolArgPreview",
        "_toolResultOneLiner",
        "_toolCardPreviewText",
        "_toolCardAllowsDetail",
        "_toolDetailLeadLabel",
        "_toolDetailLeadText",
        "_snippetLooksLikeDiff",
        "_colorDiffLines",
        "_tcAction",
        "_isMemorySave",
        "_isSkillUpdate",
        "_transparentToolSummary",
        "_toolIdentity",
        "_anchorSceneMergeToolRows",
        "_anchorSceneToolCallFromRow",
        "_transparentToolDetailHtml",
        "_toolArgsSnapshot",
        "buildToolCard",
    ]
    fn_blocks = [
        *(_function_source(UI_JS, name, required=False) for name in optional_fns),
        *(_function_source(UI_JS, name, required=True) for name in required_fns),
    ]
    functions = "\n".join(b for b in fn_blocks if b)
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const anchorsSrc = fs.readFileSync({json.dumps(str(ANCHORS_JS_PATH))}, 'utf8');
        const anchorSandbox = {{ window: {{}} }};
        vm.createContext(anchorSandbox);
        vm.runInContext(anchorsSrc, anchorSandbox, {{ filename: 'assistant_turn_anchors.js' }});
        const Anchors = anchorSandbox.window.HermesAssistantTurnAnchors;

        function esc(s) {{
          return String(s == null ? '' : s).replace(/[&<>]/g, c => ({{ '&': '&amp;', '<': '&lt;', '>': '&gt;' }}[c]));
        }}
        function li() {{ return ''; }}
        function toolIcon() {{ return ''; }}
        function _toolCommandTitle(c) {{ return String(c || ''); }}
        function _toolQueryTitle(q) {{ return String(q || ''); }}
        function _toolDisclosureIdentity() {{ return ''; }}
        const window = {{ _worklogDetailsExpandedByDefault: false }};
        const _MEMORY_SAVE_ACTIONS = new Set(['add', 'replace']);
        const _SKILL_UPDATE_ACTIONS = new Set(['create', 'patch', 'edit', 'write_file']);
        const document = {{
          createElement() {{
            return {{ className: '', innerHTML: '', dataset: {{}}, setAttribute() {{}}, removeAttribute() {{}} }};
          }},
        }};
        {functions}

        {body_js}
        """
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node exited {proc.returncode}:\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_anchor_scene_nested_tool_args_render_in_build_tool_card():
    """Reproduces live session load crash when anchor scene carries nested object args.

    assistant_turn_anchors.js._sanitizePayload converts plain objects inside tool.args
    (e.g. skill_manage.operations and delegate_task.tasks) and payload.result into
    Object.create(null). Rendering those rows through _anchorSceneMergeToolRows ->
    _anchorSceneToolCallFromRow -> buildToolCard must not throw TypeError.
    """
    data = _run_node_harness(
        """
        const ctx = { session_id: 'sid-live', turn_id: 'turn-1', run_id: 'run-1', stream_id: 'stream-1' };
        const reg = Anchors.createAssistantTurnAnchorRegistry(ctx);
        const events = [
          {
            event: 'tool',
            seq: 2218,
            event_id: 'stream-1:2218',
            payload: {
              event_type: 'tool.started',
              name: 'skill_manage',
              tid: 'call_291',
              args: {
                operations: [{
                  action: 'patch',
                  name: 'example-workflow-skill',
                  content: 'Authorization: Bearer demo-secret-token-12345',
                }],
              },
            },
          },
          {
            event: 'tool',
            seq: 2428,
            event_id: 'stream-1:2428',
            payload: {
              event_type: 'tool.started',
              name: 'delegate_task',
              tid: 'call_319',
              args: {
                action: 'spawn',
                tasks: [
                  { goal: 'audit module alpha' },
                  { goal: 'audit module beta' },
                ],
              },
            },
          },
          {
            event: 'tool_complete',
            seq: 2429,
            event_id: 'stream-1:2429',
            payload: {
              event_type: 'tool.completed',
              name: 'delegate_task',
              tid: 'call_319',
              result: { count: 2, status: 'dispatched' },
            },
          },
        ];
        for (const ev of events) {
          Anchors.applyAssistantTurnAnchorSourceEvent(reg, ev, ctx);
        }
        const anchor = reg.anchor;
        const scene = Anchors.projectAssistantTurnAnchorActivityScene(anchor, { mode: 'compact_worklog' });
        const toolRows = scene.activity_rows.filter(r => r.role === 'tool');
        const skillRow = toolRows.find(r => r.tool && r.tool.name === 'skill_manage');
        const delegateStart = toolRows.find(r => r.tool && r.tool.name === 'delegate_task' && r.seq === 2428);
        const delegateDone = toolRows.find(r => r.tool && r.tool.name === 'delegate_task' && r.seq === 2429);
        const mergedDelegate = _anchorSceneMergeToolRows(delegateStart, delegateDone);

        const skillCardLive = buildToolCard(_anchorSceneToolCallFromRow(skillRow, { settled: false }));
        const skillCardSettled = buildToolCard(_anchorSceneToolCallFromRow(skillRow, { settled: true }));
        const delegateCard = buildToolCard(_anchorSceneToolCallFromRow(mergedDelegate, { settled: false }));

        process.stdout.write(JSON.stringify({
          skillArgsNullProto: Object.getPrototypeOf(skillRow.tool.args) === null,
          skillOpNullProto: Object.getPrototypeOf(skillRow.tool.args.operations[0]) === null,
          skillHtmlLive: skillCardLive.innerHTML,
          skillHtmlSettled: skillCardSettled.innerHTML,
          delegateHtml: delegateCard.innerHTML,
        }));
        """
    )
    assert data["skillArgsNullProto"] is True
    assert data["skillOpNullProto"] is True
    assert "example-workflow-skill" in data["skillHtmlLive"]
    assert "[object Object]" not in data["skillHtmlLive"]
    assert "demo-secret-token-12345" not in data["skillHtmlLive"]
    assert "[redacted]" in data["skillHtmlLive"]
    assert "example-workflow-skill" in data["skillHtmlSettled"]
    assert "audit module alpha" in data["delegateHtml"]
    assert "dispatched" in data["delegateHtml"]
    assert "[object Object]" not in data["delegateHtml"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_transparent_tool_detail_handles_null_proto_args_and_object_result():
    data = _run_node_harness(
        """
        const nullObj = Object.create(null);
        nullObj.action = 'patch';
        nullObj.secret = 'Authorization: Bearer demo-secret-token-12345';
        const nullResult = Object.create(null);
        nullResult.ok = true;
        nullResult.count = 3;
        const cyclic = Object.create(null);
        cyclic.self = cyclic;
        const args = Object.create(null);
        args.operations = [nullObj];
        args.undef = undefined;
        args.loop = cyclic;
        const html = _transparentToolDetailHtml({
          name: 'skill_manage',
          args,
          result: nullResult,
        }, 'done');
        process.stdout.write(JSON.stringify({ html }));
        """
    )
    assert "patch" in data["html"]
    assert "demo-secret-token-12345" not in data["html"]
    assert "[redacted]" in data["html"]
    assert '"count": 3' in data["html"]
    assert "[Object]" in data["html"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_tool_args_snapshot_and_labels_handle_null_proto_and_hostile_objects():
    data = _run_node_harness(
        """
        const nullItem = Object.create(null);
        nullItem.goal = 'build lane';
        const hostile = JSON.parse('{"toString": null, "valueOf": "bad"}');
        const cyclic = Object.create(null);
        cyclic.self = cyclic;
        const args = Object.create(null);
        args.tasks = [nullItem];
        args.meta = hostile;
        args.loop = cyclic;
        const snap = _toolArgsSnapshot(args);

        const targetObj = Object.create(null);
        targetObj.id = 'item-1';
        const labelTaskArray = _toolTargetLabel({ name: 'custom_tool', args: { task: [nullItem] } });
        const labelMemoryTarget = _toolTargetLabel({ name: 'memory', args: { target: targetObj } });
        const labelDelegate = _toolTargetLabel({ name: 'delegate_task', args: { action: targetObj, tasks: targetObj } });
        const labelCmd = _toolFullCommandLabel({ name: 'terminal', args: { command: targetObj } });

        const cardHostile = buildToolCard({
          name: 'skill_manage',
          args: { action: targetObj, hostile, arr: [hostile], loop: cyclic },
          done: true,
        });
        process.stdout.write(JSON.stringify({
          snap,
          labelTaskArray,
          labelMemoryTarget,
          labelDelegate,
          labelCmd,
          cardHostileHtml: cardHostile.innerHTML,
        }));
        """
    )
    assert "build lane" in data["snap"]["tasks"]
    assert "[object Object]" not in data["snap"]["tasks"]
    assert "toString" in data["snap"]["meta"]
    assert data["snap"]["loop"] == "[Object]"
    assert "build lane" in data["labelTaskArray"]
    assert "item-1" in data["labelMemoryTarget"]
    assert "item-1" in data["labelDelegate"]
    assert "item-1" in data["labelCmd"]
    assert "toString" in data["cardHostileHtml"]
    assert "[Object]" in data["cardHostileHtml"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_anchor_normalizer_handles_object_seq_and_group_fields():
    data = _run_node_harness(
        """
        const objSeq = Object.create(null);
        objSeq.bad = 1;
        const ev = Anchors.normalizeAssistantTurnAnchorSourceEvent({
          event: 'tool',
          seq: objSeq,
          event_id: 'stream-1:9',
          payload: {
            name: 'read_file',
            activitySegmentSeq: objSeq,
            activityBurstId: objSeq,
            assistant_msg_idx: objSeq,
          },
        }, { session_id: 'sid-1', turn_id: 'turn-1', run_id: 'run-1', stream_id: 'stream-1' });
        const reg = Anchors.createAssistantTurnAnchorRegistry({ session_id: 'sid-1', turn_id: 'turn-1', run_id: 'run-1', stream_id: 'stream-1' });
        Anchors.applyAssistantTurnAnchorNormalizedEvent(reg, ev);
        const anchor = reg.anchor;
        const scene = Anchors.projectAssistantTurnAnchorActivityScene(anchor, { mode: 'compact_worklog' });
        process.stdout.write(JSON.stringify({
          seq: ev.anchor_event.seq,
          rowCount: scene.activity_rows.length,
          group: scene.activity_rows[0] && scene.activity_rows[0].group,
        }));
        """
    )
    assert data["seq"] is None
    assert data["rowCount"] == 1
    assert data["group"]["activity_segment_seq"] is None
    assert data["group"]["activity_burst_id"] is None
    assert data["group"]["assistant_msg_idx"] is None


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_messages_anchor_scene_payload_helpers_and_ui_identity_handle_cyclic_null_proto():
    msg_fns = "\n".join(
        _function_source(MESSAGES_JS, name, required=True)
        for name in ("_anchorSceneSafePayload", "_anchorSceneStringPayload")
    )
    data = _run_node_harness(
        msg_fns
        + """
        const cyclic = Object.create(null);
        cyclic.self = cyclic;
        const safeCyclic = _anchorSceneSafePayload(cyclic);
        const strCyclic = _anchorSceneStringPayload(cyclic);

        const nullPreview = Object.create(null);
        nullPreview.step = 'indexing';
        const summary = _transparentToolSummary({ name: 'custom_tool', preview: nullPreview, done: false });
        const ident = _toolIdentity({ name: 'custom_tool', args: { loop: cyclic }, snippet: cyclic });
        process.stdout.write(JSON.stringify({ safeCyclic, strCyclic, summary, ident }));
        """
    )
    assert data["safeCyclic"] == "[Object]"
    assert data["strCyclic"] == "[Object]"
    assert "indexing" in data["summary"]
    assert "[Object]" in data["ident"]

