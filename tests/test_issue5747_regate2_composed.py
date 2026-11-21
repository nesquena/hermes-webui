"""#5747 re-gate (2026-10-29): composed coverage for the three findings.

The maintainer's review asks for three composed paths, each driven through the
REAL `static/workspace.js` / `static/messages.js` symbols (brace-extracted at
runtime), never a Python mirror:

* F1 - `tool_complete -> delayed refresh -> done`: a run that ends while the
  refresh read is in flight must not drop that refresh. The stale-read contract
  is the base's ownership token (`_previewOpenOwned`), which does not carry the
  stream id; `done` clears `S.activeStreamId`, so a stream-keyed predicate
  discarded the last tool call of every turn.
* F2 - `open -> close -> read resolves / rejects`: closing the preview retires
  in-flight opens (`clearPreview()` bumps `_previewOpenGen`), so a late read must
  neither repaint content nor set status.
* F3 - two identical unkeyed tool calls in one activity segment must not share
  an identity: the second mutation must not be treated as already consumed.
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
    start = src.find(f"function {name}(")
    assert start != -1, f"{name}() not found"
    if src[max(0, start - 6) : start] == "async ":
        start -= 6
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


def _run(driver: str, payload=None) -> str:
    script = driver.replace("__PAYLOAD__", json.dumps(payload if payload is not None else {}))
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return r.stdout


MUTATION_PRELUDE = "\n".join(
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
        _fn(WORKSPACE_JS, "noteWorkspaceMutationsFromToolCall"),
        _fn(WORKSPACE_JS, "_isOpenPreviewPathMutated"),
        _fn(WORKSPACE_JS, "_runPreviewRefreshLoop"),
        _fn(WORKSPACE_JS, "refreshOpenPreviewIfMutated"),
        _fn(WORKSPACE_JS, "_previewOpenOwned"),
    ]
)

MUTATION_STATE = """
let S = {session:{session_id:'s1', workspace:'/w'}, activeProfile:'default', activeStreamId:'stream-1'};
let _turnMutatedPreviewPaths = new Map();
let _consumedMutationToolIds = new Set();
const _CONSUMED_MUTATION_TOOL_IDS_MAX = 1000;
let _previewCurrentPath = '';
let _previewDirty = false;
let _previewOpenGen = 0;
let _wsTreeGen = 0;
let _previewRefreshPromise = null;
const __pending = [];
let __painted = 0;
let __status = [];
function setStatus(m){ __status.push(String(m)); }
function _deferred(){ let resolve; const promise=new Promise(r=>{resolve=r;}); return {promise, resolve}; }
// Mirrors the production first-await-then-guard shape of every openFile() branch:
// the ownership token is captured before the await and consulted after it.
async function openFile(path, opts){
  const gen=_previewOpenGen;
  const sid=S.session && S.session.session_id;
  const wsGen=_wsTreeGen;
  const d=_deferred();
  __pending.push(d);
  await d.promise;
  if(!_previewOpenOwned(gen, sid, wsGen)) return false;
  __painted++;
  return true;
}
"""


def test_run_settle_does_not_discard_the_inflight_refresh():
    """F1: `done` clearing the stream id must not void the refresh read."""
    driver = (
        MUTATION_STATE
        + "\n"
        + MUTATION_PRELUDE
        + "\n"
        + "_previewCurrentPath = 'static/style.css';\n"
        + "noteWorkspaceMutationsFromToolCall({tid:'t1', name:'terminal',"
        " arguments:{command:\"sed -i 's/a/b/' static/style.css\"}, result:''});\n"
        + "const started = refreshOpenPreviewIfMutated();\n"
        + "const queued = __pending.length;\n"
        + "S.activeStreamId = '';   // the run ended while the read was in flight\n"
        + "__pending[0].resolve();\n"
        + "Promise.resolve(started).then(()=>{\n"
        + "  process.stdout.write(JSON.stringify({queued:queued, painted:__painted,"
        " pending:__pending.length, status:__status}));\n"
        + "});\n"
    )
    out = json.loads(_run(driver))
    assert out["queued"] == 1, f"the mutation must start exactly one refresh; got {out}"
    assert out["painted"] == 1, (
        "a run that ends while the refresh read is in flight must still commit the "
        f"refresh — the stream id is not part of preview ownership; got {out}"
    )


def test_closing_the_preview_retires_the_inflight_read():
    """F2: a read that lands after the pane closed must not paint or set status."""
    driver = (
        MUTATION_STATE
        + "\n"
        + MUTATION_PRELUDE
        + "\n"
        + "_previewCurrentPath = 'static/app.css';\n"
        + "const pendingRead = openFile('static/app.css', {bustCache:true});\n"
        + "_previewOpenGen++;          // what clearPreview() does\n"
        + "_previewCurrentPath = '';\n"
        + "__pending[0].resolve();\n"
        + "Promise.resolve(pendingRead).then(()=>{\n"
        + "  process.stdout.write(JSON.stringify({painted:__painted, status:__status}));\n"
        + "});\n"
    )
    out = json.loads(_run(driver))
    assert out == {"painted": 0, "status": []}, (
        f"a closed preview must fence its in-flight read (#5747 F2); got {out}"
    )


def test_identical_unkeyed_calls_get_distinct_identities():
    """F3: two identical calls in one burst/segment must not share a tid."""
    driver = (
        "let activeSid='s1';\n"
        + _fn(MESSAGES_JS, "_stableStringify")
        + "\n"
        + _fn(MESSAGES_JS, "_hashString")
        + "\n"
        + _fn(MESSAGES_JS, "_toolCallSignature")
        + "\n"
        + _fn(MESSAGES_JS, "_liveToolTid")
        + "\n"
        + _fn(WORKSPACE_JS, "_toolMutationIdentity")
        + "\n"
        + "const mk=()=>({name:'terminal', args:{command:\"sed -i 's/a/b/' static/a.css\"}});\n"
        + "const first=mk(), second=mk();\n"
        + "const t1=_liveToolTid(first, 3, 1);\n"
        + "const t2=_liveToolTid(second, 3, 1);\n"
        + "const t1again=_liveToolTid(first, 3, 1);\n"
        + "const explicit=_liveToolTid({name:'terminal', args:{}, tid:'call-abc'}, 3, 1);\n"
        + "process.stdout.write(JSON.stringify({\n"
        + "  t1:t1, t2:t2, t1again:t1again, explicit:explicit,\n"
        + "  id1:_toolMutationIdentity({tid:t1, name:'terminal'}),\n"
        + "  id2:_toolMutationIdentity({tid:t2, name:'terminal'}),\n"
        + "}));\n"
    )
    out = json.loads(_run(driver))
    assert out["explicit"] == "call-abc", "a provider id must win over any generated one"
    assert out["t1again"] == out["t1"], (
        f"the same live call must keep a stable identity across renders; got {out}"
    )
    assert out["t1"] != out["t2"], (
        "two identical unkeyed calls in one activity segment collide on a "
        f"name/args hash, so the second mutation reads as already consumed; got {out}"
    )
    assert out["id1"] != out["id2"], (
        f"mutation identities must differ for the two occurrences; got {out}"
    )
