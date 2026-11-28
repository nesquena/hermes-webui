"""#5747 fourth round (review 2026-11-23T04:52:58Z): quoted shell separators and
the successor refresh behind a retired read.

The maintainer reproduced two blockers on ``5b24508f5c06``:

* Q1 - a quoted shell separator hides the mutation target.
  ``sed -i 's/red/blue & green/' static/style.css`` produces no refresh through
  the actual tool-complete listener, because the sed boundary class stops at the
  ``&`` even inside quotes and never reaches the file argument.
* S1 - a clean successor's refresh is lost behind a retired read. A's refresh is
  held, the user opens clean B, B is mutated and its tool-complete plus
  settlement requests join the already in-flight promise; when A's read retires,
  the drain restores A's mutation and returns, so B's obligation is never
  serviced (three reads where B's refresh should be the fourth).

Every case drives the REAL symbols extracted from ``static/workspace.js``. The
whole #5747 mutation/refresh block is taken verbatim (from
``_artifactCandidatesFromToolCall`` up to ``collectSessionArtifacts``), so a
helper added inside that block by the repair is exercised too. The only seam is
``openFile`` itself, which models the production publish step
(``showPreview()`` -> ``_resetPreviewDirty()``) plus the publish gate the drain
consults.
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


def _decl(src: str, pattern: str) -> str:
    m = re.search(pattern, src)
    assert m, f"declaration not found: {pattern}"
    return m.group(0)


def _block(src: str, start_marker: str, end_marker: str) -> str:
    start = src.find(start_marker)
    assert start != -1, f"block start not found: {start_marker}"
    end = src.find(end_marker, start)
    assert end != -1, f"block end not found: {end_marker}"
    return src[start:end]


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


def _run(driver: str, payload=None) -> str:
    script = driver.replace("__PAYLOAD__", json.dumps(payload if payload is not None else {}))
    # Windows rejects a command line over 32767 chars (WinError 206), which the
    # extracted block plus the driver can exceed, so feed the program on stdin.
    r = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return r.stdout


PRELUDE = "\n".join(
    [
        _decl(WORKSPACE_JS, r"const ARTIFACT_IGNORE_RE = /.*?/;"),
        _decl(WORKSPACE_JS, r"const ARTIFACT_MUTATION_TOOLS\s*=\s*new Set\([\s\S]*?\);"),
        _decl(WORKSPACE_JS, r"const ARTIFACT_TEXT_MUTATION_TOOLS\s*=\s*new Set\([\s\S]*?\);"),
        _block(
            WORKSPACE_JS,
            "function _collapseDotSegments(",
            "function _artifactCandidatesFromToolCall(",
        ),
        _block(
            WORKSPACE_JS,
            "function _artifactCandidatesFromToolCall(",
            "function collectSessionArtifacts(",
        ),
        _fn(WORKSPACE_JS, "_previewHasUnsavedEdits"),
        _fn(WORKSPACE_JS, "_previewOpenOwned"),
    ]
)

# `openFile` is the single seam. It models the production publish step and the
# publish gate; a new open retires an older read through `_previewOpenGen`,
# exactly like the real `openFile()` does.
STATE = """
let S = {session:{session_id:'s1', workspace:'/w'}, activeProfile:'default', activeStreamId:'stream-1'};
let _previewCurrentPath = '';
let _previewDirty = false;
let _previewSavedText = null;
let _previewOpenGen = 0;
let _wsTreeGen = 0;
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


def _driver(body: str) -> str:
    return STATE + "\n" + PRELUDE + "\n" + body


def _paths_expr() -> str:
    return "[..._turnMutatedPreviewPaths.values()].map(e=>e.path).sort()"


def test_quoted_separator_does_not_hide_the_sed_target():
    """Q1: `&`, `|` and `;` inside quotes belong to the argument, not to the
    command boundary, so the explicit target must still be extracted."""
    payload = {
        "amp": "sed -i 's/red/blue & green/' static/style.css",
        "pipe": "sed -i 's/a|b/c|d/' docs/notes.md",
        "semicolon": "sed -i 's/a;b/c/' src/app.js",
        "double_quoted": 'sed -i "s/x&y/z/" styles/theme.css',
        "escaped_amp": "sed -i s/a/b/ src/esc.css && echo done",
        "two_commands": "sed -i 's/a/b/' one.css && sed -i 's/c/d/' two.css",
        "plain": "sed -i 's/a/b/' plain.css",
        "read_only_grep": "grep -n 'a&b' static/style.css",
        "read_only_cat": "cat 'x;y.css'",
        "escapes": "sed -i 's/a/b/' ../outside.css",
    }
    driver = _driver(
        "const cases=__PAYLOAD__;\n"
        "const out={};\n"
        "for(const k of Object.keys(cases)) out[k]=_textWriteOpTargets(cases[k]);\n"
        "out.escaped_paths=_textPathTokens(cases.escapes, '/w');\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    out = json.loads(_run(driver, payload))
    assert out["amp"] == ["static/style.css"], (
        "a quoted '&' must not end the sed invocation before its file argument; "
        f"got {out}"
    )
    assert out["pipe"] == ["docs/notes.md"], (
        f"a quoted '|' must not truncate the sed segment; got {out}"
    )
    assert out["semicolon"] == ["src/app.js"], (
        f"a quoted ';' must not truncate the sed segment; got {out}"
    )
    assert out["double_quoted"] == ["styles/theme.css"], (
        f"double quotes must be respected the same way; got {out}"
    )
    # Controls: the unquoted separator still ends the command, and the read-only
    # / escaping negatives stay negatives.
    assert out["escaped_amp"] == ["src/esc.css"], (
        f"an unquoted '&&' must still terminate the command; got {out}"
    )
    assert out["two_commands"] == ["one.css", "two.css"], (
        f"both chained sed commands must be found; got {out}"
    )
    assert out["plain"] == ["plain.css"], f"the plain form must keep working; got {out}"
    assert out["read_only_grep"] == [], f"a read-only grep is not a mutation; got {out}"
    assert out["read_only_cat"] == [], f"a read only cat is not a mutation; got {out}"
    assert out["escaped_paths"] == [], (
        f"a target outside the workspace must still be dropped; got {out}"
    )


def test_quoted_sed_write_reaches_the_pending_refresh_end_to_end():
    """Q1 (composed): the real tool-complete payload -> mutation -> drain."""
    tc = {
        "tid": "t1",
        "name": "terminal",
        "arguments": {
            "command": "sed -i 's/red/blue & green/' static/style.css",
            "workdir": "/w",
        },
        "result": "",
    }
    driver = _driver(
        "noteWorkspaceMutationsFromToolCall(" + json.dumps(tc) + ");\n"
        "const recorded=" + _paths_expr() + ";\n"
        "_previewCurrentPath='static/style.css';\n"
        "const started=refreshOpenPreviewIfMutated();\n"
        "(async()=>{ await __pump(true); await started;\n"
        "  process.stdout.write(JSON.stringify({recorded:recorded, calls:__calls,\n"
        "    painted:__painted, paths:" + _paths_expr() + "}));\n"
        "})();\n"
    )
    out = json.loads(_run(driver))
    assert out["recorded"] == ["static/style.css"], (
        f"the real terminal payload must record the quoted-sed target; got {out}"
    )
    assert out["calls"] == ["static/style.css"] and out["painted"] == 1, (
        f"the recorded mutation must drive exactly one refresh; got {out}"
    )
    assert out["paths"] == [], f"the drained entry must be consumed; got {out}"


def test_clean_successor_is_serviced_after_the_previous_read_retires():
    """S1: A's retirement must not swallow B's already-requested refresh."""
    driver = _driver(
        "_previewCurrentPath='a.txt';\n"
        "noteWorkspaceMutationsFromToolCall("
        + json.dumps({"tid": "t1", "name": "terminal",
                      "arguments": {"command": "sed -i 's/a/b/' a.txt"}, "result": ""})
        + ");\n"
        "const started=refreshOpenPreviewIfMutated();\n"
        # The user opens clean b.txt: a new open retires A's in-flight read.
        "_previewOpenGen++;\n"
        "_previewCurrentPath='b.txt';\n"
        "noteWorkspaceMutationsFromToolCall("
        + json.dumps({"tid": "t2", "name": "terminal",
                      "arguments": {"command": "sed -i 's/a/b/' b.txt"}, "result": ""})
        + ");\n"
        "const joined=refreshOpenPreviewIfMutated();\n"  # settlement joins the promise
        "(async()=>{ await __pump(true); await started; await joined;\n"
        "  const first={calls:__calls.slice(), painted:__painted, paths:" + _paths_expr() + "};\n"
        "  process.stdout.write(JSON.stringify({first:first, calls:__calls,\n"
        "    painted:__painted, paths:" + _paths_expr() + "}));\n"
        "})();\n"
    )
    out = json.loads(_run(driver))
    assert out["calls"] == ["a.txt", "b.txt"], (
        "the clean successor opened while A's read was in flight must get its "
        f"refresh after A retires; got {out}"
    )
    assert out["painted"] == 1, (
        f"only the successor's reload may publish (A's read retired); got {out}"
    )
    # A's obligation is retained for a later trigger (that pane is no longer open,
    # so it is never reloaded in this drain) and B's obligation is consumed by its
    # own reload. Dropping A's entry would lose the retry the mutated file will need
    # when the user reopens it.
    assert out["paths"] == ["a.txt"], (
        f"A's mutation must survive its retired read while B's is consumed; got {out}"
    )


def test_dirty_successor_is_not_reloaded_and_keeps_its_mutation():
    """Control: the successor path must stay behind the dirty fence and keep the
    pending entry for a later pass instead of reloading over unsaved text."""
    driver = _driver(
        "_previewCurrentPath='a.txt';\n"
        "noteWorkspaceMutationsFromToolCall("
        + json.dumps({"tid": "t1", "name": "terminal",
                      "arguments": {"command": "sed -i 's/a/b/' a.txt"}, "result": ""})
        + ");\n"
        "const started=refreshOpenPreviewIfMutated();\n"
        "_previewOpenGen++;\n"
        "_previewCurrentPath='b.txt';\n"
        "_edit('unsaved b');\n"
        "noteWorkspaceMutationsFromToolCall("
        + json.dumps({"tid": "t2", "name": "terminal",
                      "arguments": {"command": "sed -i 's/a/b/' b.txt"}, "result": ""})
        + ");\n"
        "const joined=refreshOpenPreviewIfMutated();\n"
        "(async()=>{ await __pump(true); await started; await joined;\n"
        "  process.stdout.write(JSON.stringify({calls:__calls, painted:__painted,\n"
        "    paths:" + _paths_expr() + ", unsaved:_previewHasUnsavedEdits(),\n"
        "    savedStill:_previewSavedText!==null}));\n"
        "})();\n"
    )
    out = json.loads(_run(driver))
    assert out["calls"] == ["a.txt"], (
        f"a dirty pane must never be reloaded by the drain; got {out}"
    )
    assert out["painted"] == 0, f"a retired read must not publish; got {out}"
    assert out["paths"] == ["a.txt", "b.txt"], (
        f"both mutations must stay pending for later triggers; got {out}"
    )
    assert out["unsaved"] is True and out["savedStill"] is True, (
        f"the unsaved edit and its baseline must survive; got {out}"
    )


def test_failed_read_still_retains_the_mutation_and_stops():
    """Control: a plain read failure is not a retirement — retain and stop."""
    driver = _driver(
        "_previewCurrentPath='a.txt';\n"
        "noteWorkspaceMutationsFromToolCall("
        + json.dumps({"tid": "t1", "name": "terminal",
                      "arguments": {"command": "sed -i 's/a/b/' a.txt"}, "result": ""})
        + ");\n"
        "__failNext=1;\n"
        "const started=refreshOpenPreviewIfMutated();\n"
        "(async()=>{ await __pump(true); await started;\n"
        "  process.stdout.write(JSON.stringify({calls:__calls, painted:__painted,\n"
        "    paths:" + _paths_expr() + "}));\n"
        "})();\n"
    )
    out = json.loads(_run(driver))
    assert out["calls"] == ["a.txt"], (
        f"a failed read must not retry inside the same drain; got {out}"
    )
    assert out["painted"] == 0 and out["paths"] == ["a.txt"], (
        f"the mutation must survive the failed read; got {out}"
    )
