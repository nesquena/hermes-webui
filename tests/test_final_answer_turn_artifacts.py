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
        + "turnArtifactReferencesFromToolCall({name:'write_file',output:'```diff\\n+++ output/inferred.md\\n```'})"
        + "]));"
    )
    assert output == [[{"path": "output/report.md", "source": "write_file"}], [], [], []]


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
    assert "_renderTurnArtifactListForMessage(msg, seg);" in ui
    assert "openArtifactPath(entry.path)" in ui
