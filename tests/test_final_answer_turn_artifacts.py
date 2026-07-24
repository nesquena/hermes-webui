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


def test_turn_artifact_references_require_successful_structured_write_evidence():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    start = workspace.index("const ARTIFACT_IGNORE_RE")
    end = workspace.index("const _turnMutatedPreviewPaths")
    output = _run_node(
        workspace[start:end]
        + "\nconsole.log(JSON.stringify(["
        + "turnArtifactReferencesFromToolCall({name:'write_file',arguments:{path:'output/report.md'}}),"
        + "turnArtifactReferencesFromToolCall({name:'read_file',arguments:{path:'output/report.md'}}),"
        + "turnArtifactReferencesFromToolCall({name:'write_file',is_error:true,arguments:{path:'output/report.md'}}),"
        + "turnArtifactReferencesFromToolCall({name:'write_file',output:'```diff\\n+++ output/inferred.md\\n```'}),"
        + "turnArtifactReferencesFromToolCall({name:'patch',preview:JSON.stringify({success:true,files_modified:['output/report.md','output/notes.md']})}),"
        + "turnArtifactReferencesFromToolCall({name:'patch',preview:JSON.stringify({success:false,files_modified:['output/rejected.md']})})"
        + "]));"
    )
    assert output == [
        [{"path": "output/report.md", "source": "write_file"}], [], [], [],
        [
            {"path": "output/report.md", "source": "patch"},
            {"path": "output/notes.md", "source": "patch"},
        ], [],
    ]


def test_final_answer_artifact_entries_are_turn_owned_and_workspace_scoped():
    ui = (ROOT / "static/ui.js").read_text(encoding="utf-8")
    messages = (ROOT / "static/messages.js").read_text(encoding="utf-8")
    helpers = _function_source(
        "static/ui.js", "function _turnArtifactWorkspacePath", "function _renderTurnArtifactListForMessage"
    )
    scene = {
        "artifacts": [
            {"payload": {"path": "output/report.md"}},
            {"payload": {"path": "./output/report.md"}},
            {"payload": {"path": "/workspace/output/absolute.md"}},
            {"payload": {"path": "/outside/private.md"}},
            {"payload": {"path": "../escape.md"}},
            {"payload": {"path": "output\\windows.md"}},
            {"payload": {"path": "C:/outside/windows.md"}},
        ]
    }
    output = _run_node(
        "const S={session:{workspace:'/workspace'}};\n"
        + helpers
        + "\nconsole.log(JSON.stringify(_turnArtifactEntriesFromScene("
        + json.dumps(scene)
        + ")));"
    )
    assert output == [{"path": "output/report.md"}, {"path": "output/absolute.md"}]
    assert "_attachTurnArtifactsFromToolCall(tc);" in messages
    assert "_applyToAnchor('artifact_reference'" in messages
    assert "if(typeof _renderTurnArtifactListForMessage==='function')" in ui
    assert "_renderTurnArtifactListForMessage(msg, seg, rawIdx);" in ui
    assert "openArtifactPath(entry.path)" in ui
    assert "return _turnArtifactEntriesFromScene(message&&message._anchor_activity_scene);" in ui
    assert "_turn_artifacts" not in ui


def test_final_answer_uses_anchor_scene_artifact_refs_without_message_history_fallback():
    helpers = _function_source(
        "static/ui.js", "function _turnArtifactWorkspacePath", "function _renderTurnArtifactListForMessage"
    )
    output = _run_node(
        "const S={session:{workspace:'/workspace'},messages:[{role:'assistant',content:'final'}]};\n"
        + helpers
        + "\nconsole.log(JSON.stringify(_turnArtifactEntriesForMessage({"
        + "_anchor_activity_scene:{artifacts:[{payload:{path:'/workspace/output/large-worklog.md'}}]}},0)));"
    )
    assert output == [{"path": "output/large-worklog.md"}]


def test_replay_merges_missing_artifact_into_existing_anchor_scene():
    from api import routes

    messages = [
        {
            "role": "assistant",
            "content": "final answer",
            "_anchor_activity_scene": {
                "version": "activity_scene_v1",
                "activity_rows": [{"type": "tool"}],
                "artifacts": [{"type": "artifact_reference", "payload": {"path": "/workspace/output/report.md"}}],
            },
        }
    ]

    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
        messages,
        {0: ["/workspace/output/report.md", "/workspace/output/large-worklog.md"]},
    )

    scene = hydrated[0]["_anchor_activity_scene"]
    assert scene["activity_rows"] == [{"type": "tool"}]
    assert scene["artifacts"] == [
        {"type": "artifact_reference", "payload": {"path": "/workspace/output/report.md"}},
        {
            "type": "artifact_reference",
            "payload": {
                "path": "/workspace/output/large-worklog.md",
                "source": "transcript_replay",
            },
        },
    ]


def test_paginated_replay_keeps_write_arg_artifact_when_result_is_plain_text():
    from api import routes

    messages = [
        {"role": "user", "content": "write the report"},
        {
            "role": "tool",
            "name": "write_file",
            "arguments": json.dumps({"path": "/workspace/output/plain-text-confirmation.md"}),
            "content": "Wrote /workspace/output/plain-text-confirmation.md",
        },
        {"role": "assistant", "content": "final answer"},
    ]

    assert routes._turn_artifact_paths_from_tool_result(messages[1]) == [
        "/workspace/output/plain-text-confirmation.md"
    ]

    paths_by_final_index = routes._final_turn_artifact_paths(messages)
    window, offset = routes._message_window_for_display(messages, msg_limit=1)
    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
        window, paths_by_final_index, message_offset=offset
    )

    assert offset == 2
    assert hydrated[0]["_anchor_activity_scene"]["artifacts"] == [
        {
            "type": "artifact_reference",
            "payload": {
                "path": "/workspace/output/plain-text-confirmation.md",
                "source": "transcript_replay",
            },
        }
    ]


def test_replay_rejects_write_arg_artifact_when_structured_result_failed():
    from api import routes

    message = {
        "role": "tool",
        "name": "write_file",
        "arguments": {"path": "/workspace/output/rejected.md"},
        "content": json.dumps({"success": False, "path": "/workspace/output/rejected.md"}),
    }

    assert routes._turn_artifact_paths_from_tool_result(message) == []


def test_paginated_session_response_keeps_turn_artifacts_from_earlier_tool_rows():
    from api import routes

    messages = [
        {"role": "user", "content": "write the report"},
        {
            "role": "tool",
            "name": "patch",
            "content": json.dumps(
                {
                    "success": True,
                    "files_modified": ["/workspace/output/large-worklog.md"],
                }
            ),
        },
        {"role": "assistant", "content": "working"},
        {"role": "tool", "name": "read_file", "content": "ignored"},
        {"role": "assistant", "content": "final answer"},
    ]

    paths_by_final_index = routes._final_turn_artifact_paths(messages)
    window, offset = routes._message_window_for_display(messages, msg_limit=1)
    hydrated = routes._attach_replayed_turn_artifacts_to_anchor_scenes(
        window, paths_by_final_index, message_offset=offset
    )

    assert offset == 4
    assert hydrated[0]["_anchor_activity_scene"]["version"] == "activity_scene_v1"
    assert hydrated[0]["_anchor_activity_scene"]["artifacts"] == [
        {
            "type": "artifact_reference",
            "payload": {
                "path": "/workspace/output/large-worklog.md",
                "source": "transcript_replay",
            },
        }
    ]


def test_artifact_open_expands_a_closed_workspace_preview_before_loading_file():
    workspace = (ROOT / "static/workspace.js").read_text(encoding="utf-8")
    start = workspace.index("async function openArtifactPath(path)")
    end = workspace.index("// ── Workspace file-tree", start)
    body = workspace[start:end]
    assert "ensureWorkspacePreviewVisible()" in body
    assert body.index("ensureWorkspacePreviewVisible()") < body.index("openFile(rel);")
