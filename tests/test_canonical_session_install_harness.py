"""Gate review 221beca7 #1 frontend regressions: canonical session install.

Drives the extracted production functions (not reimplementations):

- ``_installCanonicalSession`` retires the stale artifact projection and
  rebuilds it only from a provably complete canonical snapshot.
- ``cmdUndo`` routes its fetched session through the canonical install, so a
  destructive rewrite can never keep artifacts owned by rows that no longer
  exist.
- A late response for a different session cannot re-apply its state.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
COMMANDS_JS = ROOT / "static" / "commands.js"
WORKSPACE_JS = ROOT / "static" / "workspace.js"
NODE = shutil.which("node")

_WORKSPACE_SOURCE = WORKSPACE_JS.read_text(encoding="utf-8")
_SESSIONS_SOURCE = SESSIONS_JS.read_text(encoding="utf-8")
_COMMANDS_SOURCE = COMMANDS_JS.read_text(encoding="utf-8")
_UI_SOURCE = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _extract_function(source: str, name: str) -> str:
    # Match the LONGEST declaration prefix: 'async function name(' contains
    # 'function name(' as a substring, so checking the plain prefix first
    # would start the extraction mid-declaration and drop the async keyword.
    start = -1
    for prefix in (f"async function {name}(", f"function {name}("):
        found = source.find(prefix)
        if found != -1 and (start == -1 or found < start):
            start = found
    if start != -1:
        brace = source.find("{", start)
        depth = 0
        for idx in range(brace, len(source)):
            ch = source[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return source[start:idx + 1]
    pytest.fail(f"Could not extract complete function body for {name}")


def _extract_from(sources, name):
    failures = []
    for source in sources:
        try:
            return _extract_function(source, name)
        except pytest.fail.Exception as exc:
            failures.append(str(exc))
    pytest.fail(f"Could not extract {name} from any source: {failures}")


def _run_node(script: str) -> dict:
    if NODE is None:
        pytest.skip("node not on PATH")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(script)
        script_path = handle.name
    try:
        proc = subprocess.run(
            [NODE, script_path], check=False, capture_output=True, text=True
        )
        if proc.returncode != 0:
            pytest.fail(f"node failed: {proc.stderr[-2000:]}")
        return json.loads(proc.stdout.strip())
    finally:
        Path(script_path).unlink(missing_ok=True)


_HARNESS = """
const t = (key) => key;
const calls = [];
const toasts = [];
let S = { session: null, messages: [], toolCalls: [], activeProfile: 'default' };
let _messagesTruncated = false;
let _oldestIdx = 0;
let renderCalls = 0;
const clearLiveToolCards = () => { calls.push('clearLiveToolCards'); };
const renderMessages = () => { renderCalls += 1; };
const autoResize = () => {};
const $ = () => ({ value: '' });
const _reArmRecoveryPick = () => {};
const _deliberateSessionModelPick = () => null;
let routes = {};
let api = async (url, opts) => {
  calls.push({ url, opts });
  if (routes[url]) return routes[url](url);
  return {};
};
const showToast = (...args) => { toasts.push(args); };
"""

_NORMALIZE = (
    "function _normalizeArtifactPath(p){return p?String(p).replace(/\\\\/g,'/'):'';}"
)

_MUTATION_TOOLS = (
    "const ARTIFACT_MUTATION_TOOLS = new Set(['write_file','edit_file','create_file',"
    "'patch_file','move_file','delete_file','apply_patch','str_replace_editor','bash']);"
)


def _projection_helpers():
    return [
        _extract_from([_WORKSPACE_SOURCE], "_artifactCandidatesFromToolCall"),
        _extract_from([_WORKSPACE_SOURCE], "_artifactCandidatesFromText"),
        _extract_from([_WORKSPACE_SOURCE], "_harvestArtifactCandidatesFromMessages"),
        _extract_from([_WORKSPACE_SOURCE], "_artifactProjectionForSnapshot"),
        _extract_from([_WORKSPACE_SOURCE], "_artifactProjectionMatches"),
    ]


def test_install_canonical_session_retires_stale_projection():
    install = _extract_from([_SESSIONS_SOURCE], "_installCanonicalSession")
    adopt = _extract_from([_SESSIONS_SOURCE], "_adoptRegenerationRevision")
    out = _run_node(
        "\n".join(
            [
                _HARNESS,
                "let _loadSessionGeneration = 3;",
                _NORMALIZE,
                *_projection_helpers(),
                adopt,
                install,
                """
(async()=>{
const staleProjection = {
  session_id: 'sess-1', profile: 'default', revision: undefined,
  generation: 2,
  items: [{ path: '/workspace/GHOST_artifact.md', kind: 'write_file' }],
};
S.session = {
  session_id: 'sess-1', profile: 'default',
  regeneration_revision: undefined,
  _artifactProjection: staleProjection,
  messages: [{ role: 'user', content: 'old' }],
};
const replaced = {
  session_id: 'sess-1', profile: 'default', regeneration_revision: 7,
  _messages_truncated: false, _messages_offset: 0,
  messages: [{ role: 'user', content: 'new' }],
};
const ok = _installCanonicalSession(replaced);
const result = {
  ok,
  sameObject: S.session === replaced,
  ghostGone: !S.session._artifactProjection ||
    !S.session._artifactProjection.items.some(i => i.path.includes('GHOST')),
  revision: S.session.regeneration_revision,
};
// A truncated canonical snapshot must NOT rebuild a projection.
const truncated = {
  session_id: 'sess-1', profile: 'default', regeneration_revision: 8,
  _messages_truncated: true, _messages_offset: 50,
  messages: [{ role: 'user', content: 'tail' }],
};
_installCanonicalSession(truncated);
result.truncatedNoProjection = !truncated._artifactProjection;
// A capped (backstop-clipped) snapshot must NOT rebuild one either.
const capped = {
  session_id: 'sess-1', profile: 'default', regeneration_revision: 9,
  _state_db_rows_capped: true, _messages_truncated: false,
  messages: [{ role: 'user', content: 'partial' }],
};
_installCanonicalSession(capped);
result.cappedNoProjection = !capped._artifactProjection;
// A late response for another session must not touch the active pane.
const foreign = { session_id: 'sess-other', messages: [] };
_installCanonicalSession(foreign);
result.foreignIgnored = S.session === capped;
console.log(JSON.stringify(result));
})().catch(e=>{console.error(e);process.exit(1);});
""",
            ]
        )
    )
    assert out["ok"] is True
    assert out["sameObject"] is True
    assert out["ghostGone"] is True
    assert out["revision"] == 7
    assert out["truncatedNoProjection"] is True
    assert out["cappedNoProjection"] is True
    assert out["foreignIgnored"] is True


def test_undo_installs_canonical_session_and_rebuilds_projection():
    undo = _extract_from([_COMMANDS_SOURCE], "cmdUndo")
    install = _extract_from([_SESSIONS_SOURCE], "_installCanonicalSession")
    adopt = _extract_from([_SESSIONS_SOURCE], "_adoptRegenerationRevision")
    out = _run_node(
        "\n".join(
            [
                _HARNESS,
                "let _loadSessionGeneration = 5;",
                _NORMALIZE,
                *_projection_helpers(),
                _MUTATION_TOOLS,
                adopt,
                install,
                undo,
                """
(async()=>{
routes = {
  '/api/session/undo': () => ({ ok: true, removed_count: 2 }),
  '/api/session?session_id=sess-u&messages=1&resolve_model=0&msg_limit=30&expand_renderable=1': () => ({
    session: {
      session_id: 'sess-u', regeneration_revision: 3,
      _messages_truncated: false, _messages_offset: 0,
      messages: [{ role: 'user', content: 'kept', tool_calls: [
        { function: { name: 'write_file', arguments: JSON.stringify({ path: '/workspace/KEPT.md' }) } },
      ] }],
    },
  }),
};
S.session = {
  session_id: 'sess-u',
  _artifactProjection: { session_id: 'sess-u', profile: 'default', revision: 2,
    generation: 4, items: [{ path: '/workspace/REMOVED_artifact.md' }] },
  messages: [{ role: 'user', content: 'old' }],
};
S.messages = [{ role: 'user', content: 'old' }];
S.toolCalls = [{ name: 'write_file' }];
await cmdUndo();
const undoState = {
  installed: S.session.regeneration_revision === 3,
  projectionPaths: (S.session._artifactProjection || { items: [] }).items.map(i => i.path),
  ghostGone: !(S.session._artifactProjection || { items: [] }).items.some(i => i.path.includes('REMOVED')),
};
console.log(JSON.stringify(undoState));
})().catch(e=>{console.error(e);process.exit(1);});
""",
            ]
        )
    )
    assert out["installed"] is True
    assert out["ghostGone"] is True
    assert out["projectionPaths"] == ["/workspace/KEPT.md"]


def test_artifact_projection_never_falls_back_to_partial_resident_rows():
    collect = _extract_from([_WORKSPACE_SOURCE], "collectSessionArtifacts")
    match = _extract_from([_WORKSPACE_SOURCE], "_artifactProjectionMatches")
    out = _run_node("\n".join([
        _HARNESS, "let _loadSessionGeneration=4;", _NORMALIZE, match, collect,
        """
S.session={session_id:'a',profile:'default',regeneration_revision:2};
S.messages=[{tool_calls:[{function:{name:'write_file',arguments:'{"path":"/partial"}'}}]}];
S.toolCalls=[{name:'write_file',args:{path:'/partial'}}];
const unavailable=collectSessionArtifacts();
S.session._artifactProjection={session_id:'a',profile:'default',revision:2,
  generation:4,items:[{path:'/old'},{path:'/new'},{path:'/old'}]};
const complete=collectSessionArtifacts();
_loadSessionGeneration=5;
const stale=collectSessionArtifacts();
console.log(JSON.stringify({unavailable,complete,stale}));
""",
    ]))
    assert out == {"unavailable": None, "complete": [
        {"path": "/old", "source": "tool"}, {"path": "/new", "source": "tool"},
    ], "stale": None}


def test_late_undo_response_for_other_session_is_ignored():
    undo = _extract_from([_COMMANDS_SOURCE], "cmdUndo")
    install = _extract_from([_SESSIONS_SOURCE], "_installCanonicalSession")
    adopt = _extract_from([_SESSIONS_SOURCE], "_adoptRegenerationRevision")
    out = _run_node(
        "\n".join(
            [
                _HARNESS,
                _NORMALIZE,
                *_projection_helpers(),
                _MUTATION_TOOLS,
                adopt,
                install,
                undo,
                """
(async()=>{
routes = {
  '/api/session/undo': () => ({ ok: true, removed_count: 1 }),
  '/api/session?session_id=sess-a': () => ({
    session: { session_id: 'sess-a', messages: [{ role: 'user', content: 'A' }] },
  }),
};
// The active pane switched to another session while the undo round-trip was
// in flight; the late GET must not re-apply session A's state to session B.
S.session = { session_id: 'sess-b', messages: [{ role: 'user', content: 'B' }] };
S.messages = [{ role: 'user', content: 'B' }];
await cmdUndo();
console.log(JSON.stringify({
  sessionStillB: S.session.session_id === 'sess-b',
  messagesStillB: S.messages.length === 1 && S.messages[0].content === 'B',
  calledUndo: calls.some(c => c.url === '/api/session/undo'),
}));
})().catch(e=>{console.error(e);process.exit(1);});
""",
            ]
        )
    )
    assert out["calledUndo"] is True
    assert out["sessionStillB"] is True
    assert out["messagesStillB"] is True


@pytest.mark.parametrize("operation", ["undo", "retry", "edit"])
def test_deferred_destructive_response_cannot_restore_session_a_into_b(operation):
    """Start on A, switch to B while the canonical GET is unresolved."""
    install = _extract_from([_SESSIONS_SOURCE], "_installCanonicalSession")
    adopt = _extract_from([_SESSIONS_SOURCE], "_adoptRegenerationRevision")
    action = {
        "undo": _extract_from([_COMMANDS_SOURCE], "cmdUndo"),
        "retry": _extract_from([_COMMANDS_SOURCE], "cmdRetry"),
        "edit": _extract_from([_UI_SOURCE], "submitEdit"),
    }[operation]
    invocation = {"undo": "cmdUndo()", "retry": "cmdRetry()", "edit": "submitEdit(0,'replacement')"}[operation]
    out = _run_node("\n".join([
        _HARNESS, "let _loadSessionGeneration=1;", _NORMALIZE,
        *_projection_helpers(), _MUTATION_TOOLS, adopt, install,
        "let _submitEditInFlight=false; let sends=0; const send=async()=>{sends++};",
        "const setStatus=()=>{}; const _ensureAllMessagesLoaded=async()=>{};",
        action,
        """
(async()=>{
  let release, fetched=false;
  const waiting=new Promise(resolve=>{release=resolve});
  routes={
    '/api/session/undo':()=>({ok:true,removed_count:1}),
    '/api/session/retry':()=>({ok:true,last_user_text:'A draft'}),
    '/api/session/truncate':()=>({ok:true}),
  };
  const apiBefore=api;
  api=async(url,opts)=>{
    if(url.startsWith('/api/session?session_id=a')){
      fetched=true;
      return waiting;
    }
    return apiBefore(url,opts);
  };
  S.session={session_id:'a',profile:'default',messages:[{role:'user',content:'A'}]};
  S.messages=S.session.messages;
  const running=INVOKE;
  for(let i=0;i<20&&!fetched;i++) await Promise.resolve();
  if(!fetched) throw new Error('canonical GET was never reached');
  const b={session_id:'b',profile:'default',messages:[{role:'user',content:'B'}]};
  S.session=b; S.messages=b.messages;
  release({session:{session_id:'a',profile:'default',messages:[{role:'user',content:'late A'}]}});
  await running;
  console.log(JSON.stringify({sameSession:S.session===b, sameMessages:S.messages===b.messages,
    content:S.messages[0].content,sends,composer:$('msg').value, inFlight:_submitEditInFlight}));
})().catch(e=>{console.error(e);process.exit(1)});
""".replace("INVOKE", invocation),
    ]))
    assert out["sameSession"] and out["sameMessages"]
    assert out["content"] == "B"
    assert out["sends"] == 0
    assert out["inFlight"] is False
