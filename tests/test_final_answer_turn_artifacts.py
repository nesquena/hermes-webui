import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _function_source(path, start, end):
    source = (ROOT / path).read_text(encoding="utf-8")
    return source[source.index(start) : source.index(end)]


def _run_node(script):
    result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_turn_artifact_references_require_server_landed_descriptors():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    start = workspace.index("const ARTIFACT_IGNORE_RE")
    end = workspace.index("const _turnMutatedPreviewPaths")
    output = _run_node(
        workspace[start:end]
        + "\nconsole.log(JSON.stringify(["
        + "turnArtifactReferencesFromToolCall({name:'write_file',tool_call_id:'call-1',artifacts:[{path:'output/report.md',workspace_root:'/workspace',tool_call_id:'call-1',tool_name:'write_file'},{path:'output/mismatch.md',workspace_root:'/workspace',tool_call_id:'call-1',tool_name:'read_file'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'read_file',arguments:{path:'output/report.md'}}),"
        + "turnArtifactReferencesFromToolCall({name:'write_file',artifacts:[{path:'output/missing-id.md',workspace_root:'/workspace',tool_call_id:'call-missing',tool_name:'write_file'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'write_file',is_error:true,artifacts:[{path:'output/report.md',workspace_root:'/workspace'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'write_file',output:'```diff\\n+++ output/inferred.md\\n```'}),"
        + "turnArtifactReferencesFromToolCall({name:'patch',artifacts:[{path:'output/report.md',workspace_root:'/workspace',tool_call_id:'call-2',tool_name:'patch'},{path:'output/notes.md',workspace_root:'/workspace',tool_call_id:'call-2',tool_name:'patch'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'patch',tid:'tid-1',artifacts:[{path:'output/tid.md',workspace_root:'/workspace',tool_call_id:'tid-1',tool_name:'patch'}]}),"
        + "turnArtifactReferencesFromToolCall({name:'patch',preview:JSON.stringify({success:true,files_modified:['output/rejected.md']})}),"
        + "turnArtifactReferencesFromToolCall({name:'write_file',artifacts:[{path:['invalid'],workspace_root:123,tool_call_id:'call-3',tool_name:'write_file'}]})"
        + "]));"
    )
    assert output == [
        [
            {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-1",
                "tool_name": "write_file",
            }
        ],
        [],
        [],
        [],
        [],
        [
            {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-2",
                "tool_name": "patch",
            },
            {
                "path": "output/notes.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-2",
                "tool_name": "patch",
            },
        ],
        [
            {
                "path": "output/tid.md",
                "workspace_root": "/workspace",
                "tool_call_id": "tid-1",
                "tool_name": "patch",
            }
        ],
        [],
        [],
        [],
    ]


def test_final_answer_artifact_entries_are_turn_owned_and_workspace_scoped():
    ui = (ROOT / "static/ui.js").read_text(encoding="utf-8")
    messages = (ROOT / "static/messages.js").read_text(encoding="utf-8")
    helpers = _function_source(
        "static/ui.js", "function _turnArtifactWorkspacePath", "function _renderTurnArtifactListForMessage"
    )
    scene = {
        "artifacts": [
            {"payload": {"path": "output/report.md", "workspace_root": "/workspace", "session_id":"sid-owner","tool_name":"write_file","tool_call_id":"call-1"}},
            {"payload": {"path": "./output/report.md", "workspace_root": "/workspace", "session_id":"sid-owner","tool_name":"write_file","tool_call_id":"call-2"}},
            {"payload": {"path": "output/old-workspace.md", "workspace_root": "/workspace-a"}},
            {"payload": {"path": "/workspace/output/absolute.md", "workspace_root": "/workspace", "session_id":"sid-owner","tool_name":"write_file","tool_call_id":"call-3"}},
            {"payload": {"path": "../escape.md", "workspace_root": "/workspace"}},
            {"payload": {"path": "output\\windows.md", "workspace_root": "/workspace"}},
            {"payload": {"path": "C:/outside/windows.md", "workspace_root": "/workspace"}},
            {"payload": {"path": "output/unbound.md"}},
        ]
    }
    output = _run_node(
        "const S={session:{workspace:'/workspace',session_id:'sid-owner'}};\n"
        + helpers
        + "\nconsole.log(JSON.stringify(_turnArtifactEntriesFromScene("
        + json.dumps(scene)
        + ")));"
    )
    assert output == [{
        "path": "output/report.md",
        "workspace_root": "/workspace",
        "session_id": "sid-owner",
        "tool_name": "write_file",
        "tool_call_id": "call-1",
        "owner": {
            "session_id": "sid-owner",
            "workspace_root": "/workspace",
        },
    }]
    assert "_attachTurnArtifactsFromToolCall(tc);" in messages
    assert "_applyToAnchor('artifact_reference'" in messages
    assert "_anchorHasArtifactReference(localId,workspaceRoot,path)" in messages
    assert "workspace_root:workspaceRoot" in messages
    assert "if(typeof _renderTurnArtifactListForMessage==='function')" in ui
    assert "_renderTurnArtifactListForMessage(msg, seg, rawIdx);" in ui
    assert "openArtifactPath(entry)" in ui
    assert "return _turnArtifactEntriesFromScene(message&&message._anchor_activity_scene);" in ui
    assert "_turn_artifacts" not in ui


def test_final_answer_uses_anchor_scene_artifact_refs_without_message_history_fallback():
    helpers = _function_source(
        "static/ui.js", "function _turnArtifactWorkspacePath", "function _renderTurnArtifactListForMessage"
    )
    output = _run_node(
        "const S={session:{workspace:'/workspace',session_id:'sid-owner'},messages:[{role:'assistant',content:'final'}]};\n"
        + helpers
        + "\nconsole.log(JSON.stringify(_turnArtifactEntriesForMessage({"
        + "_anchor_activity_scene:{artifacts:[{payload:{path:'output/large-worklog.md',workspace_root:'/workspace',session_id:'sid-owner',tool_name:'patch',tool_call_id:'call-2'}}]}},0)));"
    )
    assert output == [{
        "path": "output/large-worklog.md",
        "workspace_root": "/workspace",
        "session_id": "sid-owner",
        "tool_name": "patch",
        "tool_call_id": "call-2",
        "owner": {
            "session_id": "sid-owner",
            "workspace_root": "/workspace",
        },
    }]


def test_replay_merges_missing_artifact_into_existing_anchor_scene():
    from api import routes

    messages = [
        {
            "role": "assistant",
            "content": "final answer",
            "_anchor_activity_scene": {
                "version": "activity_scene_v1",
                "activity_rows": [{"type": "tool"}],
                "artifacts": [
                    {
                        "type": "artifact_reference",
                        "payload": {"path": "output/report.md", "workspace_root": "/workspace"},
                    }
                ],
            },
        }
    ]

    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
        messages,
        {
            0: [
                {"path": "output/report.md", "workspace_root": "/workspace"},
                {
                    "path": "output/large-worklog.md",
                    "workspace_root": "/workspace",
                    "tool_call_id": "call-2",
                    "tool_name": "patch",
                },
            ]
        },
    )

    scene = hydrated[0]["_anchor_activity_scene"]
    assert scene["activity_rows"] == [{"type": "tool"}]
    assert scene["artifacts"] == [
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "source": "transcript_replay",
            },
        },
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/large-worklog.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-2",
                "tool_name": "patch",
                "source": "transcript_replay",
            },
        },
    ]


def test_replay_replaces_under_typed_existing_anchor_artifacts_with_transcript_descriptors():
    from api import routes

    messages = [
        {
            "role": "assistant",
            "content": "final answer",
            "_anchor_activity_scene": {
                "version": "activity_scene_v1",
                "activity_rows": [{"type": "tool"}],
                "artifacts": [
                    {"type": "artifact_reference", "payload": {"path": "output/report.md", "workspace_root": "/workspace"}},
                    {
                        "type": "artifact_reference",
                        "payload": {
                            "path": "output/typed.md",
                            "workspace_root": "/workspace",
                            "tool_name": "patch",
                            "tool_call_id": "call-existing",
                        },
                    },
                ],
            },
        }
    ]

    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
        messages,
        {
            0: [
                {"path": "output/report.md", "workspace_root": "/workspace", "tool_call_id": "call-1", "tool_name": "write_file"},
                {"path": "output/typed.md", "workspace_root": "/workspace", "tool_call_id": "call-2", "tool_name": "patch"},
            ],
        },
    )

    scene = hydrated[0]["_anchor_activity_scene"]
    assert scene["artifacts"] == [
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-1",
                "tool_name": "write_file",
                "source": "transcript_replay",
            },
        },
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/typed.md",
                "workspace_root": "/workspace",
                "tool_name": "patch",
                "tool_call_id": "call-existing",
            },
        },
    ]


def test_paginated_session_response_keeps_paired_landed_turn_artifacts():
    from api import routes

    messages = [
        {"role": "user", "content": "write the report"},
        {"role": "assistant", "tool_calls": [{"id": "call-1", "function": {"name": "patch"}}]},
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "patch",
            "content": json.dumps({"success": True, "files_modified": ["/workspace/output/large-worklog.md"]}),
        },
        {"role": "assistant", "content": "working"},
        {"role": "tool", "name": "read_file", "content": "ignored"},
        {"role": "assistant", "content": "final answer"},
    ]

    paths_by_final_index = routes._final_turn_artifact_paths(messages, workspace_root="/workspace")
    window, offset = routes._message_window_for_display(messages, msg_limit=1)
    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(window, paths_by_final_index, message_offset=offset)

    assert offset == 5
    assert hydrated[0]["_anchor_activity_scene"]["version"] == "activity_scene_v1"
    assert hydrated[0]["_anchor_activity_scene"]["artifacts"] == [
        {
            "type": "artifact_reference",
            "payload": {
                "path": "output/large-worklog.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-1",
                "tool_name": "patch",
                "source": "transcript_replay",
            },
        }
    ]


def test_replay_rejects_failed_unpaired_duplicate_and_mismatched_writes():
    from api import routes

    messages = [
        {"role": "user", "content": "write"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call-failed", "function": {"name": "write_file"}},
                {"id": "call-mismatch", "function": {"name": "write_file"}},
                {"id": "call-dupe", "function": {"name": "write_file"}},
                {"id": "call-conflict", "function": {"name": "write_file"}},
                {"id": "call-patch", "function": {"name": "patch"}},
                {"id": "call-result", "function": {"name": "write_file"}},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-failed",
            "name": "write_file",
            "content": json.dumps({"error": "denied", "resolved_path": "/workspace/output/failed.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-mismatch",
            "name": "patch",
            "content": json.dumps({"success": True, "files_modified": ["/workspace/output/mismatch.md"]}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-dupe",
            "name": "write_file",
            "content": json.dumps({"error": "first failed", "resolved_path": "/workspace/output/dupe.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-dupe",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 4, "resolved_path": "/workspace/output/dupe.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-orphan",
            "name": "write_file",
            "content": json.dumps({"bytes_written": 2, "resolved_path": "/workspace/output/orphan.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-conflict",
            "name": "write_file",
            "content": json.dumps({"error": "displayed error", "resolved_path": "/workspace/output/conflict.md"}),
            "result": json.dumps({"bytes_written": 8, "resolved_path": "/workspace/output/conflict.md"}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-patch",
            "name": "patch",
            "content": json.dumps({"success": True, "files_modified": ["/workspace/output/report.md"]}),
        },
        {
            "role": "tool",
            "tool_call_id": "call-result",
            "name": "write_file",
            "content": "Wrote /workspace/output/result-field.md",
            "result": json.dumps({"bytes_written": 11, "resolved_path": "/workspace/output/result-field.md"}),
        },
        {"role": "assistant", "content": "final"},
    ]

    assert routes._final_turn_artifact_paths(messages, workspace_root="/workspace") == {
        10: [
            {
                "path": "output/report.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-patch",
                "tool_name": "patch",
            },
            {
                "path": "output/result-field.md",
                "workspace_root": "/workspace",
                "tool_call_id": "call-result",
                "tool_name": "write_file",
            }
        ]
    }


def test_landed_artifact_descriptors_use_actual_hermes_success_shapes():
    from api.turn_artifacts import landed_artifact_descriptors

    assert landed_artifact_descriptors(
        "write_file",
        {"bytes_written": 3, "resolved_path": "/workspace/output/report.md"},
        workspace_root="/workspace",
        tool_call_id="call-write",
    ) == [
        {
            "path": "output/report.md",
            "workspace_root": "/workspace",
            "tool_call_id": "call-write",
            "tool_name": "write_file",
        }
    ]
    assert (
        landed_artifact_descriptors(
            "write_file",
            {"error": "permission denied", "resolved_path": "/workspace/output/report.md"},
            workspace_root="/workspace",
            tool_call_id="call-write",
        )
        == []
    )
    assert (
        landed_artifact_descriptors(
            "write_file",
            {"bytes_written": 3, "resolved_path": "/workspace-a/output/report.md"},
            workspace_root="/workspace-b",
            tool_call_id="call-write",
        )
        == []
    )
    assert (
        landed_artifact_descriptors(
            "mcp_filesystem_write_file",
            {"bytes_written": 3, "resolved_path": "/workspace/output/report.md"},
            workspace_root="/workspace",
            tool_call_id="call-plugin",
        )
        == []
    )


def test_live_stream_completion_uses_landed_artifact_descriptors():
    streaming = (ROOT / "api/streaming.py").read_text(encoding="utf-8")
    start = streaming.index("def on_tool_complete")
    end = streaming.index("# Mirror the todo tool", start)
    body = streaming[start:end]
    assert "landed_artifact_descriptors(" in body
    assert "'artifacts': landed_artifacts" in body
    assert "'is_error': tool_result_is_error(function_result)" in body
    assert "'is_error': False" not in body


def test_artifact_open_expands_a_closed_workspace_preview_before_loading_file():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    start = workspace.index("async function openArtifactPath(path)")
    end = workspace.index("// ── Workspace file-tree", start)
    body = workspace[start:end]
    assert "ensureWorkspacePreviewVisible()" in body
    assert body.index("ensureWorkspacePreviewVisible()") < body.index("openFile(rel,{owner});")
