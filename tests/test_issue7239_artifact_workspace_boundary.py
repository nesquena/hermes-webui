"""Regression coverage for fail-closed artifact workspace classification (#7239)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_fn(name):
    marker = f"function {name}("
    start = WORKSPACE_JS.index(marker)
    if start >= 6 and WORKSPACE_JS[start - 6 : start] == "async ":
        start -= 6
    brace = WORKSPACE_JS.index("{", start)
    depth = 0
    for pos in range(brace, len(WORKSPACE_JS)):
        if WORKSPACE_JS[pos] == "{":
            depth += 1
        elif WORKSPACE_JS[pos] == "}":
            depth -= 1
            if depth == 0:
                return WORKSPACE_JS[start : pos + 1]
    raise AssertionError(f"could not extract {name}")


def _common_js(*functions):
    constants = re.findall(r"const (?:ARTIFACT_IGNORE_RE|ARTIFACT_MUTATION_TOOLS) = .*?;", WORKSPACE_JS)
    return "\n".join(constants) + "\n" + "\n".join(_extract_fn(name) for name in functions)


def _node_json(script, *args):
    result = subprocess.run(
        [NODE, "-e", script, *[json.dumps(arg) for arg in args]],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _classify(paths, workspace):
    script = _common_js("_sanitizeArtifactPath", "_classifyArtifactPath") + "\nconst input=JSON.parse(process.argv[1]); const ws=JSON.parse(process.argv[2]); process.stdout.write(JSON.stringify(input.map(p=>_classifyArtifactPath(p, ws))));"
    return _node_json(script, paths, workspace)


def test_reported_absolute_artifact_is_display_only_and_network_silent():
    script = _common_js("_sanitizeArtifactPath", "_classifyArtifactPath", "_classifyArtifactCandidate", "_artifactCandidatesFromText", "_artifactCandidatesFromToolCall", "collectSessionArtifacts", "renderSessionArtifacts", "_workspacePathExists", "openArtifactPath") + r'''
const calls=[]; const statuses=[]; const root={innerHTML:'',};
const S={session:{workspace:'/home/hermesuser/workspace',session_id:'s'},toolCalls:[{name:'write_file',args:{path:'/home/hermesuser/.hermes/shared/hermes-profile-setup-credentials.md'}}],messages:[]};
const $=id=>id==='workspaceArtifacts'?root:null; const esc=s=>String(s); const t=k=>k;
const switchWorkspacePanelTab=()=>calls.push('tab'); const api=async url=>{calls.push(url);return {entries:[]}}; const setStatus=s=>statuses.push(s); const openFile=()=>calls.push('open');
renderSessionArtifacts(); openArtifactPath('/home/hermesuser/.hermes/shared/hermes-profile-setup-credentials.md').then(()=>process.stdout.write(JSON.stringify({html:root.innerHTML,calls,statuses})));'''
    out = _node_json(script)
    assert "<button" not in out["html"]
    assert "workspace_artifact_outside_workspace" in out["html"]
    assert out["calls"] == []
    assert out["statuses"] == ["workspace_artifact_outside_workspace"]


def test_posix_containment_matrix():
    rows = _classify([
        "/workspace/report.md", "/workspace-other/report.md", "/Workspace/report.md",
        "~/shared/file.md", "../secret.md", "./relative.md", "report.md",
    ], "/workspace")
    assert [row["kind"] for row in rows] == [
        "workspace-contained", "outside", "outside", "unsupported", "unsupported",
        "workspace-relative", "workspace-relative",
    ]
    assert rows[0]["openPath"] == "report.md"
    assert rows[1]["openPath"] is None
    assert rows[5]["openPath"] == "relative.md"


def test_collection_dedupes_relative_and_workspace_absolute_aliases():
    script = _common_js(
        "_sanitizeArtifactPath",
        "_classifyArtifactPath",
        "_classifyArtifactCandidate",
        "_artifactCandidatesFromToolCall",
        "_artifactCandidatesFromText",
        "collectSessionArtifacts",
    ) + r'''
const S={session:{workspace:'/workspace'},toolCalls:[
  {name:'write_file',args:{path:'./report.md'}},
  {name:'write_file',args:{path:'/workspace/report.md'}},
  {name:'write_file',args:{path:'report.md'}}
],messages:[]};
process.stdout.write(JSON.stringify(collectSessionArtifacts().map(item=>item.classification.dedupeKey)));'''
    assert _node_json(script) == ["report.md"]


def test_windows_containment_matrix():
    rows = _classify([
        r"d:/proj/src/report.pdf", r"D:\Proj\report.pdf", r"E:\Proj\file.md",
        r"C:\work-old\report.md", r"c:/WORK/report.md", r"C:relative.md",
    ], r"D:\Proj")
    assert [row["kind"] for row in rows] == [
        "workspace-contained", "workspace-contained", "outside", "outside", "outside", "unsupported",
    ]
    assert rows[0]["openPath"] == "src/report.pdf"
    assert rows[1]["openPath"] == "report.pdf"


def test_windows_relative_and_canonical_boundary_matrix():
    rows = _classify([
        "./report.md", "Report.md", "d:/proj/report.md",
        "d:/proj/dir/./report.md", "d:/proj/foo/..", "C:/report.md",
    ], r"D:\Proj")
    assert [row["kind"] for row in rows] == [
        "workspace-relative", "workspace-relative", "workspace-contained",
        "workspace-contained", "unsupported", "outside",
    ]
    assert rows[0]["openPath"] == "report.md"
    assert rows[1]["dedupeKey"] == "report.md"
    assert rows[2]["dedupeKey"] == "report.md"
    assert rows[3]["openPath"] == "dir/report.md"

    root_rows = _classify(["/report.md", "report.md", "C:/report.md"], "/")
    assert [row["kind"] for row in root_rows] == [
        "workspace-contained", "workspace-relative", "outside",
    ]


def test_direct_outside_open_call_has_no_side_effects():
    script = _common_js("_sanitizeArtifactPath", "_classifyArtifactPath", "_workspacePathExists", "openArtifactPath") + r'''
const calls=[]; const S={session:{workspace:'/workspace',session_id:'s'}}; const t=k=>k;
const switchWorkspacePanelTab=()=>calls.push('tab'); const api=async url=>calls.push(url); const setStatus=s=>calls.push(s); const openFile=()=>calls.push('open');
openArtifactPath('/workspace-other/report.md').then(()=>process.stdout.write(JSON.stringify(calls)));'''
    assert _node_json(script) == ["workspace_artifact_outside_workspace"]


def test_inside_missing_artifact_preserves_failure_status():
    script = _common_js("_sanitizeArtifactPath", "_classifyArtifactPath", "_workspacePathExists", "openArtifactPath") + r'''
const calls=[]; const S={session:{workspace:'/workspace',session_id:'s'}}; const t=k=>k;
const switchWorkspacePanelTab=()=>calls.push('tab'); const api=async url=>{calls.push(url);return {entries:[]}}; const setStatus=s=>calls.push(s); const openFile=()=>calls.push('open');
openArtifactPath('/workspace/missing.md').then(()=>process.stdout.write(JSON.stringify(calls)));'''
    calls = _node_json(script)
    assert calls[0] == "tab"
    assert calls[1].startswith("/api/list?")
    assert calls[-1] == "file_open_failed"
    assert "open" not in calls


def test_artifact_boundary_keys_exist_in_all_locale_blocks():
    blocks = list(re.finditer(r"^  (?:'[^']+'|[A-Za-z-]+): \{", I18N_JS, re.MULTILINE))
    assert len(blocks) == 15
    for index, match in enumerate(blocks):
        end = blocks[index + 1].start() if index + 1 < len(blocks) else I18N_JS.index("\n};", match.start())
        block = I18N_JS[match.start() : end]
        for key in ("workspace_artifact_outside_workspace", "workspace_artifact_unsupported"):
            assert len(re.findall(rf"\b{key}:", block)) == 1


def test_collection_render_and_open_route_through_classifier():
    assert WORKSPACE_JS.count("function _classifyArtifactPath(") == 1
    assert "_classifyArtifactCandidate(path, S.session && S.session.workspace)" in WORKSPACE_JS
    assert "_classifyArtifactPath(path, S.session && S.session.workspace)" in WORKSPACE_JS
    render = _extract_fn("renderSessionArtifacts")
    assert "classification.kind" in render
    assert "onclick=\"openArtifactPath" in render
    assert "startsWith(normWs)" not in render
    opener = _extract_fn("openArtifactPath")
    assert opener.index("_classifyArtifactPath") < opener.index("switchWorkspacePanelTab")
    assert "_workspacePathExists(rel)" in opener
