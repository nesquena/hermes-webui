"""#5747 re-gate: behavioral coverage for the preview auto-refresh findings.

Each test drives the REAL static/*.js helper via node (source is brace-extracted
from the file at runtime), so the assertions describe behavior, never the shape
of the source text:

* F1 - raw command/code text reaches the write-op parser (JSON framing hid
  quoted targets behind escape backslashes).
* F2 - the pending-mutation key survives the transient stream id.
* F4 - the stale-read predicate the new guards rely on.
* F5 - collectSessionArtifacts() keeps extension-less structured candidates.
* F3 - the settled merge carries the live identity/marker onto settled rows.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _fn(src: str, name: str) -> str:
    """Brace-matched extraction of a function definition from real source."""
    start = src.find(f"function {name}(")
    assert start != -1, f"{name}() not found"
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name}() body did not close")


def _decl(src: str, pattern: str) -> str:
    m = re.search(pattern, src)
    assert m, f"declaration not found: {pattern}"
    return m.group(0)


def _run(driver: str, payload) -> str:
    script = driver.replace("__PAYLOAD__", json.dumps(payload))
    r = subprocess.run(
        [NODE, "-e", script],
        capture_output=True, text=True, timeout=20,
    )
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return r.stdout


WORKSPACE_PRELUDE = "\n".join(
    [
        _decl(WORKSPACE_JS, r"const ARTIFACT_IGNORE_RE = /.*?/;"),
        _decl(WORKSPACE_JS, r"const ARTIFACT_MUTATION_TOOLS\s*=\s*new Set\([\s\S]*?\);"),
        _decl(WORKSPACE_JS, r"const ARTIFACT_TEXT_MUTATION_TOOLS\s*=\s*new Set\([\s\S]*?\);"),
        _fn(WORKSPACE_JS, "_collapseDotSegments"),
        _fn(WORKSPACE_JS, "_normalizeArtifactPath"),
        _fn(WORKSPACE_JS, "_textWriteOpTargets"),
        _fn(WORKSPACE_JS, "_textPathTokens"),
        _fn(WORKSPACE_JS, "_artifactCandidatesFromText"),
        _fn(WORKSPACE_JS, "_artifactCandidatesFromToolCall"),
        _fn(WORKSPACE_JS, "_currentMutationOwner"),
        _fn(WORKSPACE_JS, "_mutationKey"),
        _fn(WORKSPACE_JS, "_toolMutationIdentity"),
        _fn(WORKSPACE_JS, "_openFileReadStale"),
    ]
)

# Stub module state: declared here so the extracted helpers close over it.
WORKSPACE_STATE = """
let S = {session:{session_id:'s1', workspace:'/w'}, activeProfile:'default', activeStreamId:'stream-1'};
let _turnMutatedPreviewPaths = new Map();
let _consumedMutationToolIds = new Set();
const _CONSUMED_MUTATION_TOOL_IDS_MAX = 1000;
let _previewInFlightGen = 4;
let __sink = [];
function noteWorkspaceMutationsFromToolCalls(entries){ __sink = entries; }
"""


def _mutations(tc: dict, active_stream_id: str = "stream-1") -> list:
    """Drive noteWorkspaceMutationsFromToolCall() with real state -> paths."""
    driver = (
        WORKSPACE_STATE
        + "\n"
        + WORKSPACE_PRELUDE
        + "\n"
        + _fn(WORKSPACE_JS, "noteWorkspaceMutationsFromToolCall")
        + "\n"
        + "function noteWorkspaceMutationsFromToolCalls(entries){ __sink = entries; }\n"
        + f"S.activeStreamId = {json.dumps(active_stream_id)};\n"
        + "const tc = __PAYLOAD__;\n"
        + "noteWorkspaceMutationsFromToolCall(tc);\n"
        + "process.stdout.write(JSON.stringify([..._turnMutatedPreviewPaths.values()].map(v=>v.path)));\n"
    )
    return json.loads(_run(driver, tc))


def test_shell_write_op_from_raw_command_is_recorded():
    """F1: a quoted write target must survive the parser's raw-text anchors."""
    paths = _mutations(
        {
            "tid": "t-raw-1",
            "name": "terminal",
            "arguments": {"command": 'python -c \'open("notes/out.txt","w")\''},
            "result": "",
        }
    )
    assert any(p.endswith("notes/out.txt") for p in paths), (
        "raw command text must reach _textWriteOpTargets so the shell/python "
        f"write target is recorded (#5747 F1); got {paths}"
    )


def test_relative_redirect_and_sed_targets_are_recorded():
    """F1 (reviewer's own examples): canonical fields, relative paths."""
    rel = _mutations(
        {
            "tid": "t-raw-3",
            "name": "terminal",
            "arguments": {"command": "echo updated > static/style.css"},
            "result": "",
        }
    )
    assert any(p.endswith("static/style.css") for p in rel), (
        f"a workspace-relative shell redirect must register a mutation; got {rel}"
    )
    sed = _mutations(
        {
            "tid": "t-raw-4",
            "name": "terminal",
            "arguments": {"command": "sed -i 's/old/new/' static/theme.css"},
            "result": "",
        }
    )
    assert any(p.endswith("static/theme.css") for p in sed), (
        f"an in-place sed edit must register a mutation; got {sed}"
    )


def test_merely_mentioned_path_is_not_a_mutation():
    """F1/F2 guard: prose mentions are not write ops."""
    paths = _mutations(
        {
            "tid": "t-raw-2",
            "name": "terminal",
            "arguments": {"command": "cat notes/out.txt"},
            "result": "notes/out.txt: 42 lines",
        }
    )
    assert paths == [], f"a read/mention must not register a mutation; got {paths}"


def test_pending_mutation_key_survives_transient_stream_id():
    """F2: the stream id is transient and must not partition the key."""
    driver = (
        WORKSPACE_STATE
        + "\n"
        + WORKSPACE_PRELUDE
        + "\n"
        + "const [a, b, c, d] = __PAYLOAD__;\n"
        + "process.stdout.write(JSON.stringify({\n"
        + "  sameOwner: _mutationKey(a, 'src/a.js') === _mutationKey(b, 'src/a.js'),\n"
        + "  otherPath: _mutationKey(a, 'src/a.js') === _mutationKey(c, 'src/b.js'),\n"
        + "  otherWs: _mutationKey(a, 'src/a.js') === _mutationKey(d, 'src/a.js'),\n"
        + "}));\n"
    )
    out = json.loads(
        _run(
            driver,
            [
                {"sessionId": "s1", "profile": "default", "workspace": "/w", "streamId": "stream-1"},
                {"sessionId": "s1", "profile": "default", "workspace": "/w", "streamId": ""},
                {"sessionId": "s1", "profile": "default", "workspace": "/w", "streamId": "stream-1"},
                {"sessionId": "s1", "profile": "default", "workspace": "/w2", "streamId": "stream-1"},
            ],
        )
    )
    assert out["sameOwner"] is True, (
        "the pending key must not include the transient stream id, otherwise a "
        "mutation recorded mid-stream is orphaned once the stream settles (#5747 F2)"
    )
    assert out["otherPath"] is False, "different paths must not collide (#5747 F2)"
    assert out["otherWs"] is False, "different workspaces must not collide (#5747 F2)"


def test_stale_read_predicate_flags_superseded_reads():
    """F4: the guards added to the rejection paths key off this predicate."""
    driver = (
        WORKSPACE_STATE
        + "\n"
        + WORKSPACE_PRELUDE
        + "\n"
        + "function stale(args){ return _openFileReadStale(args.gen, args.sid, args.pid, args.ws, args.stream); }\n"
        + "const c = __PAYLOAD__;\n"
        + "process.stdout.write(JSON.stringify({\n"
        + "  current: stale({gen:4, sid:'s1', pid:'default', ws:'/w', stream:'stream-1'}),\n"
        + "  newerOpen: stale({gen:3, sid:'s1', pid:'default', ws:'/w', stream:'stream-1'}),\n"
        + "  otherWs: stale({gen:4, sid:'s1', pid:'default', ws:'/w2', stream:'stream-1'}),\n"
        + "  settledStream: stale({gen:4, sid:'s1', pid:'default', ws:'/w', stream:''}),\n"
        + "}));\n"
    )
    out = json.loads(_run(driver, {}))
    assert out == {
        "current": False,
        "newerOpen": True,
        "otherWs": True,
        "settledStream": True,
    }, (
        "a read superseded by a newer openFile(), a workspace switch, or a "
        f"settled stream must all read as stale (#5747 F4); got {out}"
    )


def test_artifacts_keep_extensionless_structured_candidate():
    """F5: provenance decides normalization, not the path shape."""
    driver = (
        WORKSPACE_STATE
        + "\n"
        + WORKSPACE_PRELUDE
        + "\n"
        + _fn(WORKSPACE_JS, "collectSessionArtifacts")
        + "\n"
        + "const sessionToolCalls = __PAYLOAD__;\n"
        + "S.toolCalls = sessionToolCalls;\n"
        + "S.messages = [];\n"
        + "process.stdout.write(JSON.stringify(collectSessionArtifacts().map(a=>a.path)));\n"
    )
    paths = json.loads(
        _run(
            driver,
            [
                {"tid": "a1", "name": "write_file", "arguments": {"path": "Makefile"}, "result": ""},
                {"tid": "a2", "name": "write_file", "arguments": {"path": "Dockerfile"}, "result": ""},
                {"tid": "a3", "name": "write_file", "arguments": {"path": "src/app.js"}, "result": ""},
            ],
        )
    )
    assert paths == ["Makefile", "Dockerfile", "src/app.js"], (
        "structured, already-normalized candidates must not be re-normalized "
        f"without allowBare — that drops Makefile/Dockerfile (#5747 F5); got {paths}"
    )


def test_settled_merge_keeps_live_identity_and_consumed_marker():
    """F3: settled rows must adopt the live row's identity and marker."""
    driver = (
        "const settled = __PAYLOAD__[0];\n"
        "let S = {toolCalls: __PAYLOAD__[1]};\n"
        + _fn(MESSAGES_JS, "_mergeSettledToolCallsWithLiveMetadata")
        + "\n"
        + "process.stdout.write(JSON.stringify(_mergeSettledToolCallsWithLiveMetadata(settled)));\n"
    )
    live = [
        {
            "tid": "tid-1",
            "name": "write_file",
            "activityBurstId": "burst-9",
            "duration": 1200,
            "started_at": 111,
            "_workspaceMutationConsumed": True,
        }
    ]
    settled = [{"tool_call_id": "tid-1", "name": "write_file"}]
    merged = json.loads(_run(driver, [settled, live]))
    assert isinstance(merged, list) and len(merged) == 1, f"unexpected merge output: {merged}"
    row = merged[0]
    assert row.get("activityBurstId") == "burst-9" and row.get("duration") == 1200, (
        f"settled rows must inherit the live burst/identity metadata (#5747 F3); got {row}"
    )
    assert row.get("_workspaceMutationConsumed") is True, (
        "the consumed marker must survive the merge, otherwise the settled "
        f"replay re-adds the same tool event (#5747 F3); got {row}"
    )
