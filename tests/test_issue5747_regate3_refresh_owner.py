"""#5747 third round (review 2026-11-22T14:33:45Z): the drain must keep its owner,
keep the mutation when it cannot reload, and parse ordinary Python writes.

The maintainer reproduced three blockers on 56864c479:

* B1 - an existing refresh loop can reload a different file while it has unsaved
  edits. The dirty check lives only at the entry of
  ``refreshOpenPreviewIfMutated()``, so a pass that follows the pane after the
  awaited read calls ``openFile('b.txt')`` with ``_previewDirty`` true,
  consuming B's pending change while ``showPreview()`` hides the editor and
  drops its saved baseline.
* B2 - a failed refresh consumes the only retryable mutation. The entry is
  deleted before the await, ``openFile()``'s ``false`` result is ignored, and
  the consumed tool marker stops settlement from recording the event again.
* B3 - ordinary Python writes with additional arguments are missed:
  ``open('report.md', 'w', encoding='utf-8')`` records no path because
  ``_textWriteOpTargets()`` requires ``)`` directly after the mode.

Every case drives the REAL symbols extracted from ``static/workspace.js``. The
only seam is ``openFile`` itself, which models the production publish step
(``showPreview()`` -> ``_resetPreviewDirty()``) plus the publish gate the
refresh drain needs: ``_previewRefreshPublishBlocked(path)``.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
NODE = shutil.which("node") or ""

pytestmark = pytest.mark.skipif(not NODE, reason="node not on PATH")


def _fn(src: str, name: str) -> str:
    start = src.find(f"function {name}(")
    assert start != -1, f"{name}() not found in static/workspace.js"
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


def _fn_opt(src: str, name: str) -> str:
    """Optional symbol: absence must fail behaviour, never crash the harness."""
    try:
        return _fn(src, name)
    except AssertionError:
        return ""


def _decl(src: str, pattern: str) -> str:
    m = re.search(pattern, src)
    assert m, f"declaration not found: {pattern}"
    return m.group(0)


def _run(driver: str, payload=None) -> str:
    script = driver.replace("__PAYLOAD__", json.dumps(payload if payload is not None else {}))
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return r.stdout


PARSER_PRELUDE = "\n".join(
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
    ]
)

DRAIN_PRELUDE = "\n".join(
    [
        _fn(WORKSPACE_JS, "_isOpenPreviewPathMutated"),
        _fn(WORKSPACE_JS, "_runPreviewRefreshLoop"),
        _fn(WORKSPACE_JS, "refreshOpenPreviewIfMutated"),
        _fn(WORKSPACE_JS, "_previewOpenOwned"),
        _fn(WORKSPACE_JS, "_previewHasUnsavedEdits"),
        _fn_opt(WORKSPACE_JS, "_previewRefreshPublishBlocked"),
    ]
)

# `openFile` is the single seam: it models the real publish step
# (showPreview() -> _resetPreviewDirty() nulls _previewSavedText) and consults
# the production publish gate, exactly where the real openFile must consult it.
STATE = """
let S = {session:{session_id:'s1', workspace:'/w'}, activeProfile:'default', activeStreamId:'stream-1'};
let _turnMutatedPreviewPaths = new Map();
let _consumedMutationToolIds = new Set();
const _CONSUMED_MUTATION_TOOL_IDS_MAX = 1000;
let _previewCurrentPath = '';
let _previewDirty = false;
let _previewSavedText = null;
let _previewOpenGen = 0;
let _wsTreeGen = 0;
let _previewRefreshPromise = null;
const __calls = [];
const __pending = [];
let __painted = 0;
let __failNext = 0;
const __editor = {value:'', style:{display:'none'}};
function $(id){
  if(id==='previewEditArea') return __editor;
  return {value:'', style:{display:'none'}, textContent:'', innerHTML:'',
          classList:{add(){},remove(){},contains(){return false;}},
          appendChild(){}, setAttribute(){}, remove(){}, dataset:{}, querySelector(){return null;}};
}
function _deferred(){ let resolve; const promise=new Promise(r=>{resolve=r;}); return {promise, resolve}; }
function _edit(text){ _previewSavedText='on-disk'; __editor.value=text; _previewDirty=true; }
async function openFile(path, opts){
  const gen=_previewOpenGen;
  const sid=S.session && S.session.session_id;
  const wsGen=_wsTreeGen;
  __calls.push(String(path));
  const d=_deferred();
  __pending.push(d);
  await d.promise;
  if(!_previewOpenOwned(gen, sid, wsGen)) return false;
  if(__failNext>0){ __failNext--; return false; }
  if(typeof _previewRefreshPublishBlocked==='function' && _previewRefreshPublishBlocked(path)) return false;
  __painted++;
  _previewSavedText=null;   // showPreview() -> _resetPreviewDirty()
  _previewDirty=false;
  return true;
}
// Release every read the drain issues, including ones started after a previous
// release. A read nobody releases would leave the drain promise pending and the
// node process would exit with rc=0 and empty stdout, hiding the measurement.
async function __pump(manualFirst){
  let idx=0;
  if(manualFirst && __pending.length){ __pending[0].resolve(); idx=1; }
  for(let i=0;i<40;i++){
    await new Promise(r=>setTimeout(r,0));
    if(__pending.length>idx){ __pending[idx].resolve(); idx++; continue; }
    await new Promise(r=>setTimeout(r,0));
    if(__pending.length>idx){ __pending[idx].resolve(); idx++; continue; }
    break;
  }
  return idx;
}
"""


def _mutation(tid, path, tool="terminal"):
    if tool == "execute_code":
        args = {"code": f"open({path!r}, 'w', encoding='utf-8')"}
    else:
        args = {"command": f"sed -i 's/a/b/' {path}"}
    return {"tid": tid, "name": tool, "arguments": args, "result": ""}


def _note(tid, path, tool="terminal"):
    return f"noteWorkspaceMutationsFromToolCall({json.dumps(_mutation(tid, path, tool))});\n"


def _paths_expr():
    return "[..._turnMutatedPreviewPaths.values()].map(e=>e.path).sort()"


def test_pane_switch_during_the_read_does_not_reload_the_edited_file():
    """B1: the drain must keep the owner/path it started with, and never
    reload a pane that went dirty while the previous read was in flight."""
    driver = (
        STATE
        + "\n"
        + PARSER_PRELUDE
        + "\n"
        + DRAIN_PRELUDE
        + "\n"
        + "_previewCurrentPath='a.txt';\n"
        + _note("t1", "a.txt")
        + "const started=refreshOpenPreviewIfMutated();\n"
        # The read for a.txt is in flight. The user opens b.txt — a new open
        # retires the previous read through the generation token — and types in
        # it; a second tool then mutates b.txt.
        + "_previewOpenGen++;\n"
        + "_previewCurrentPath='b.txt';\n"
        + "_edit('my unsaved b');\n"
        + _note("t2", "b.txt")
        + "(async()=>{ const reads=await __pump(true); await started;\n"
        + "  process.stdout.write(JSON.stringify({reads:reads, calls:__calls, painted:__painted,\n"
        + "    paths:" + _paths_expr() + ",\n"
        + "    unsaved:_previewHasUnsavedEdits(), savedStill:_previewSavedText!==null}));\n"
        + "})();\n"
    )
    out = json.loads(_run(driver))
    assert out["calls"] == ["a.txt"], (
        "the drain followed the pane to another file and reloaded it under the "
        f"user's unsaved edits; got {out}"
    )
    assert out["painted"] == 0, (
        f"a retired read must not publish; got {out}"
    )
    assert "b.txt" in out["paths"], (
        "the mutation of the file that is now open must be preserved for a later "
        f"pass, not consumed; got {out}"
    )
    assert out["unsaved"] is True and out["savedStill"] is True, (
        f"the unsaved edit of the new pane must survive untouched; got {out}"
    )


def test_clean_pane_still_reloads_once_per_mutation():
    """Control: the drain still reloads a clean pane and never drops a mutation
    recorded while the previous read was in flight."""
    driver = (
        STATE
        + "\n"
        + PARSER_PRELUDE
        + "\n"
        + DRAIN_PRELUDE
        + "\n"
        + "_previewCurrentPath='a.txt';\n"
        + _note("t1", "a.txt")
        + "const started=refreshOpenPreviewIfMutated();\n"
        + _note("t2", "a.txt")   # second mutation lands during the first read
        + "(async()=>{ await __pump(true); await started;\n"
        + "  process.stdout.write(JSON.stringify({calls:__calls, painted:__painted,\n"
        + "    paths:" + _paths_expr() + "}));\n"
        + "})();\n"
    )
    out = json.loads(_run(driver))
    assert out["calls"] == ["a.txt", "a.txt"], (
        f"one sequential reload per mutation on a clean pane; got {out}"
    )
    assert out["painted"] == 2, f"both reloads must publish; got {out}"
    assert out["paths"] == [], f"no mutation may be left behind; got {out}"


def test_failed_refresh_keeps_the_mutation_for_the_next_trigger():
    """B2: a read failure must restore the same owner's mutation and stop, so
    the next normal trigger retries it exactly once."""
    driver = (
        STATE
        + "\n"
        + PARSER_PRELUDE
        + "\n"
        + DRAIN_PRELUDE
        + "\n"
        + "_previewCurrentPath='a.txt';\n"
        + _note("t1", "a.txt")
        + "__failNext=1;\n"
        + "const started=refreshOpenPreviewIfMutated();\n"
        + "(async()=>{\n"
        + "  await __pump(true);\n"
        + "  await started;\n"
        + "  const afterFail={calls:__calls.slice(), painted:__painted, paths:" + _paths_expr() + "};\n"
        + "  const retry=refreshOpenPreviewIfMutated();\n"
        + "  await __pump(false);\n"
        + "  await retry;\n"
        + "  process.stdout.write(JSON.stringify({afterFail:afterFail, calls:__calls,\n"
        + "    painted:__painted, paths:" + _paths_expr() + "}));\n"
        + "})();\n"
    )
    out = json.loads(_run(driver))
    fail = out["afterFail"]
    assert fail["calls"] == ["a.txt"], (
        f"a failed read must not retry inside the same drain; got {out}"
    )
    assert fail["painted"] == 0
    assert fail["paths"] == ["a.txt"], (
        "the only retryable mutation was consumed by a read that failed, so the "
        f"later trigger has nothing to replay; got {out}"
    )
    assert out["calls"] == ["a.txt", "a.txt"], (
        f"the later normal trigger must retry exactly once; got {out}"
    )
    assert out["painted"] == 1 and out["paths"] == [], (
        f"the retry must publish and consume the mutation; got {out}"
    )


def test_edits_begun_during_the_read_are_not_published_over():
    """B1b: publication is guarded, not just entry. An edit that starts while
    the read is in flight must keep its baseline and its pending mutation."""
    driver = (
        STATE
        + "\n"
        + PARSER_PRELUDE
        + "\n"
        + DRAIN_PRELUDE
        + "\n"
        + "_previewCurrentPath='a.txt';\n"
        + _note("t1", "a.txt")
        + "const started=refreshOpenPreviewIfMutated();\n"
        + "_edit('typed while the read was in flight');\n"
        + "(async()=>{ await __pump(true); await started;\n"
        + "  process.stdout.write(JSON.stringify({calls:__calls, painted:__painted,\n"
        + "    paths:" + _paths_expr() + ", unsaved:_previewHasUnsavedEdits(),\n"
        + "    saved:_previewSavedText}));\n"
        + "})();\n"
    )
    out = json.loads(_run(driver))
    assert out["painted"] == 0, (
        "the read must not publish over an editor that went dirty while it was "
        f"in flight (showPreview() would drop its baseline); got {out}"
    )
    assert out["unsaved"] is True and out["saved"] == "on-disk", (
        f"the typed text and its baseline must survive the read; got {out}"
    )
    assert out["paths"] == ["a.txt"], (
        f"the blocked reload must stay pending for a later pass; got {out}"
    )


def test_python_open_with_extra_arguments_records_the_write_target():
    """B3: ``open('report.md', 'w', encoding='utf-8')`` is an ordinary write."""
    payload = {
        "encoding": "open('report.md', 'w', encoding='utf-8')",
        "append_kw": "open(\"log.txt\", 'a', buffering=1)",
        "plus_kw": "open('z.txt', 'r+', encoding='utf-8')",
        "plain": "open('plain.txt', 'w')",
        "read_only": "open('x.txt', 'r')",
        "binary_read": "open('y.bin', 'rb')",
        "escapes": "open('../secrets.txt', 'w', encoding='utf-8')",
    }
    driver = (
        STATE
        + "\n"
        + PARSER_PRELUDE
        + "\n"
        + "const cases=__PAYLOAD__;\n"
        + "const out={};\n"
        + "for(const k of Object.keys(cases)) out[k]=_textWriteOpTargets(cases[k]);\n"
        + "out.escaped=_textPathTokens(cases.escapes, '/w');\n"
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    out = json.loads(_run(driver, payload))
    assert out["encoding"] == ["report.md"], (
        "an encoding keyword after the mode must not hide the write target; "
        f"got {out}"
    )
    assert out["append_kw"] == ["log.txt"], f"buffering= must not hide the target; got {out}"
    assert out["plus_kw"] == ["z.txt"], f"a read/write mode is still a write; got {out}"
    assert out["plain"] == ["plain.txt"], f"the plain form must keep working; got {out}"
    assert out["read_only"] == [], f"a read-only open is not a mutation; got {out}"
    assert out["binary_read"] == [], f"a binary read is not a mutation; got {out}"
    assert out["escaped"] == [], (
        f"a target outside the workspace must still be dropped; got {out}"
    )


def test_encoded_write_reaches_the_pending_refresh_end_to_end():
    """B3 (composed): tool object -> mutation -> drain, for the encoding form."""
    driver = (
        STATE
        + "\n"
        + PARSER_PRELUDE
        + "\n"
        + DRAIN_PRELUDE
        + "\n"
        + _note("t1", "report.md", tool="execute_code")
        + "const recorded=" + _paths_expr() + ";\n"
        + "_previewCurrentPath='report.md';\n"
        + "const started=refreshOpenPreviewIfMutated();\n"
        + "(async()=>{ await __pump(true); await started;\n"
        + "  process.stdout.write(JSON.stringify({recorded:recorded, calls:__calls,\n"
        + "    painted:__painted, paths:" + _paths_expr() + "}));\n"
        + "})();\n"
    )
    out = json.loads(_run(driver))
    assert out["recorded"] == ["report.md"], (
        f"the tool object must record the encoded write target; got {out}"
    )
    assert out["calls"] == ["report.md"] and out["painted"] == 1, (
        f"the recorded mutation must drive exactly one refresh; got {out}"
    )
    assert out["paths"] == [], f"the drained entry must be consumed; got {out}"
