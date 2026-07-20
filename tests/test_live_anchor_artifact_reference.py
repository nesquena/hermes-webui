"""Regression coverage for production turn-owned workspace artifacts."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from api.artifact_references import derive_file_artifact_references


REPO = Path(__file__).resolve().parent.parent
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_block(source: str, name: str) -> str:
    start = source.index(f"def {name}(")
    next_def = source.find("\n            def ", start + 1)
    assert next_def != -1, f"end of {name} not found"
    return source[start:next_def]


def test_successful_write_file_emits_only_bounded_reference_metadata(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "reports" / "final.md"
    result = json.dumps({
        "bytes_written": 19,
        "resolved_path": str(target),
        "files_modified": [str(target)],
        "output": "private generated body",
    })

    references = derive_file_artifact_references(
        "write_file",
        {"path": str(target), "content": "private input body"},
        result,
        workspace,
        tool_call_id="call-write",
    )

    assert references == [{
        "kind": "workspace_file",
        "path": "reports/final.md",
        "source_tool": "write_file",
        "tool_call_id": "call-write",
    }]
    encoded = json.dumps(references)
    assert "private generated body" not in encoded
    assert "private input body" not in encoded
    assert str(workspace) not in encoded


def test_failed_incomplete_or_read_only_results_do_not_become_artifacts(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = str(workspace / "failed.md")

    cases = [
        ("write_file", {"path": path}, json.dumps({"error": "permission denied"})),
        ("write_file", {"path": path}, json.dumps({"resolved_path": path})),
        ("patch", {"path": path}, json.dumps({"success": False, "error": "stale"})),
        ("patch", {"path": path}, "truncated result"),
        ("read_file", {"path": path}, json.dumps({"bytes_written": 1})),
        ("terminal", {"command": f"touch {path}"}, json.dumps({"success": True})),
        ("create_file", {"path": path}, json.dumps({"bytes_written": 1})),
    ]

    for name, args, result in cases:
        assert derive_file_artifact_references(
            name, args, result, workspace, tool_call_id="call-failed"
        ) == []
    assert derive_file_artifact_references(
        "write_file", {"path": path}, json.dumps({"bytes_written": 1}), ""
    ) == []


def test_successful_patch_keeps_result_order_dedupes_and_rejects_unsafe_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    cache_path = workspace / "node_modules" / "generated.js"
    root_build_path = workspace / "build" / "bundle.js"
    nested_build_path = workspace / "docs" / "build" / "report.md"
    first = workspace / "src" / "app.py"
    second = workspace / "Makefile"
    result = json.dumps({
        "success": True,
        "files_modified": [
            str(first),
            str(first),
            str(outside),
            "../traversal.md",
            "https://example.com/not-a-file",
            str(cache_path),
            str(root_build_path),
            str(nested_build_path),
            {"path": str(workspace / "not-a-string.md")},
            str(second),
        ],
    })

    references = derive_file_artifact_references(
        "patch", {"mode": "patch"}, result, workspace, tool_call_id="call-patch"
    )

    assert [item["path"] for item in references] == [
        "src/app.py",
        "docs/build/report.md",
        "Makefile",
    ]


def test_surrounding_whitespace_path_does_not_take_decoy_ownership(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    decoy = workspace / "report"
    actual = workspace / "report "
    decoy.write_text("decoy", encoding="utf-8")
    actual.write_text("actual", encoding="utf-8")

    assert derive_file_artifact_references(
        "write_file",
        {"path": str(actual)},
        json.dumps({"bytes_written": 6, "files_modified": [str(actual)]}),
        workspace,
        tool_call_id="call-space",
    ) == []

    assert derive_file_artifact_references(
        "write_file",
        {"path": str(decoy)},
        json.dumps({"bytes_written": 5, "files_modified": [str(decoy)]}),
        workspace,
        tool_call_id="call-decoy",
    ) == [{
        "kind": "workspace_file",
        "path": "report",
        "source_tool": "write_file",
        "tool_call_id": "call-decoy",
    }]


def test_artifact_derivation_limits_fail_closed_on_overflow(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    too_many_paths = [f"generated-{idx}.md" for idx in range(65)]
    assert derive_file_artifact_references(
        "patch",
        {"mode": "patch"},
        json.dumps({"success": True, "files_modified": too_many_paths}),
        workspace,
        tool_call_id="call-many",
    ) == []

    huge_result = json.dumps({
        "bytes_written": 1,
        "resolved_path": str(workspace / "huge.md"),
        "padding": "x" * (65 * 1024),
    })
    assert derive_file_artifact_references(
        "write_file",
        {"path": str(workspace / "huge.md")},
        huge_result,
        workspace,
        tool_call_id="call-huge-result",
    ) == []

    assert derive_file_artifact_references(
        "write_file",
        {"path": str(workspace / "tool-id.md")},
        json.dumps({"bytes_written": 1, "resolved_path": str(workspace / "tool-id.md")}),
        workspace,
        tool_call_id="t" * 257,
    ) == []

    patch = "\n".join(
        ["*** Begin Patch"]
        + [f"*** Add File: generated-{idx}.md" for idx in range(65)]
        + ["*** End Patch"]
    )
    assert derive_file_artifact_references(
        "patch",
        {"mode": "patch", "patch": patch},
        json.dumps({"success": True}),
        workspace,
        tool_call_id="call-patch-overflow",
    ) == []


def test_successful_patch_can_fall_back_to_v4a_targets(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    args = {
        "mode": "patch",
        "patch": "\n".join([
            "*** Begin Patch",
            "*** Update File: src/app.py",
            "*** Add File: Makefile",
            "*** Move File: old.txt -> docs/new.txt",
            "*** End Patch",
        ]),
    }

    references = derive_file_artifact_references(
        "functions.patch",
        args,
        json.dumps({"success": True}),
        workspace,
        tool_call_id="call-v4a",
    )

    assert [item["path"] for item in references] == [
        "src/app.py",
        "Makefile",
        "old.txt",
        "docs/new.txt",
    ]


def test_existing_symlink_escape_is_rejected(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "linked"
    link.symlink_to(outside, target_is_directory=True)

    references = derive_file_artifact_references(
        "write_file",
        {"path": "linked/secret.md"},
        json.dumps({"bytes_written": 1, "resolved_path": str(link / "secret.md")}),
        workspace,
        tool_call_id="call-link",
    )

    assert references == []


def test_foreign_drive_path_is_rejected_on_posix(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    references = derive_file_artifact_references(
        "write_file",
        {"path": r"C:\\foreign\\report.md"},
        json.dumps({"bytes_written": 1, "resolved_path": r"C:\\foreign\\report.md"}),
        workspace,
        tool_call_id="call-foreign-drive",
    )

    assert references == []


def test_backend_emits_artifact_after_tool_completion_and_keeps_cancel_journalable():
    modern = _function_block(STREAMING_PY, "on_tool_complete")
    legacy = _function_block(STREAMING_PY, "on_tool")

    for block in (modern, legacy):
        derive = block.index("derive_file_artifact_references(")
        tool_complete = block.index("put('tool_complete'")
        artifact = block.index("put('artifact_reference', artifact_reference)")
        assert tool_complete < derive < artifact

    assert "event not in ('cancel', 'error', 'artifact_reference')" in STREAMING_PY


def test_runtime_journal_snapshot_projects_artifact_references_for_reconnect(monkeypatch):
    from api import routes

    session_id = "session-artifact-snapshot"
    run_id = "run-artifact-snapshot"
    stream_id = "stream-artifact-snapshot"
    events = [
        {
            "event": "tool",
            "seq": 1,
            "event_id": f"{run_id}:1",
            "run_id": run_id,
            "created_at": 1.0,
            "payload": {"name": "write_file", "tid": "call-write"},
        },
        {
            "event": "tool_complete",
            "seq": 2,
            "event_id": f"{run_id}:2",
            "run_id": run_id,
            "created_at": 2.0,
            "payload": {"name": "write_file", "tid": "call-write", "preview": "ok"},
        },
        {
            "event": "artifact_reference",
            "seq": 3,
            "event_id": f"{run_id}:3",
            "run_id": run_id,
            "created_at": 3.0,
            "payload": {
                "kind": "workspace_file",
                "path": "reports/final.md",
                "source_tool": "write_file",
                "tool_call_id": "call-write",
                "content": "private body must not persist",
            },
        },
    ]

    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda lookup_id: {
            "session_id": session_id,
            "run_id": stream_id,
            "stream_id": stream_id,
            "last_seq": 3,
            "last_event_id": f"{run_id}:3",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda loaded_session_id, lookup_id: {"events": events},
    )

    snapshot = routes._run_journal_live_snapshot(stream_id)
    scene = snapshot["anchor_activity_scene"]
    artifact = scene["artifacts"][0]

    assert snapshot["last_seq"] == 3
    assert scene["identity"]["run_id"] == run_id
    assert scene["identity"]["stream_id"] == stream_id
    assert artifact["event_id"] == f"{run_id}:3"
    assert artifact["run_id"] == run_id
    assert artifact["stream_id"] == stream_id
    assert artifact["seq"] == 3
    assert artifact["payload"] == {
        "kind": "workspace_file",
        "path": "reports/final.md",
        "source_tool": "write_file",
        "tool_call_id": "call-write",
    }
    assert "private body" not in json.dumps(scene)


def test_frontend_routes_artifact_sse_to_anchor_without_repainting_worklog():
    start = MESSAGES_JS.index("source.addEventListener('artifact_reference'")
    end = MESSAGES_JS.index("source.addEventListener('todo_state'", start)
    block = MESSAGES_JS[start:end]

    assert "S.session.session_id!==activeSid" in block
    assert "S.activeStreamId!==streamId" in block
    assert "_applyToAnchor('artifact_reference',d,e,null,{render:false});" in block
    assert "'tool_complete','artifact_reference','todo_state'" in MESSAGES_JS


@pytest.mark.skipif(NODE is None, reason="node is required for browser hydration coverage")
def test_frontend_hydrates_artifact_only_activity_scene():
    script = f"""
const fs=require('fs');
const src=fs.readFileSync({json.dumps(str(REPO / "static" / "messages.js"))},'utf8');
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

const activeSid='session-artifact-snapshot';
const streamId='stream-artifact-snapshot';
let _anchorShadowWarned=false;
let _anchorRegistry={{}};
const applied=[];
const _anchorApi={{
  applyAssistantTurnAnchorSourceEvent(registry,event,context){{
    applied.push({{event,context}});
    return {{applied:true}};
  }},
}};
eval(extractFunc('_hydrateAnchorRegistryFromActivityScene'));

const ok=_hydrateAnchorRegistryFromActivityScene({{
  version:'activity_scene_v1',
  identity:{{run_id:'run-artifact-snapshot', stream_id:streamId}},
  activity_rows:[],
  artifacts:[{{
    event_id:'run-artifact-snapshot:3',
    local_id:'artifact:run-artifact-snapshot:3',
    run_id:'run-artifact-snapshot',
    stream_id:streamId,
    seq:3,
    source_event_type:'artifact_reference',
    payload:{{
      kind:'workspace_file',
      path:'reports/final.md',
      source_tool:'write_file',
      tool_call_id:'call-write',
    }},
  }}],
}});
if(!ok) throw new Error('artifact-only scene was not hydrated');
if(applied.length!==1) throw new Error('expected one applied artifact event, got '+applied.length);
const item=applied[0];
if(item.event.source_event_type!=='artifact_reference') throw new Error('wrong source event');
if(item.event.path!=='reports/final.md') throw new Error('path did not hydrate');
if(item.event.run_id!=='run-artifact-snapshot') throw new Error('run identity lost');
if(item.context.stream_id!==streamId) throw new Error('stream context lost');
"""
    subprocess.run([NODE, "-e", script], check=True)
