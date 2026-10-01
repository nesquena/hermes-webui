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


# ── Gate review e43234ad (final round): cancellation settlement ──────────────
# The terminal cancel handler's _applyCancelSessionPayload must go through the
# same generation-fenced canonical install as every destructive-rewrite path.
# The cancel listener lives inside attachLiveStream, so the tests drive the
# listener by attaching a synthetic SSE source and firing 'cancel'.

_MESSAGES_SOURCE = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _extract_cancel_listener() -> str:
    """Extract the terminal 'cancel' listener CALLBACK body from attachLiveStream.

    Returns the body inside ``source.addEventListener('cancel',e=>{ ... })`` so
    the harness can re-home it on a fake source under a callable name.
    """
    marker = "source.addEventListener('cancel',e=>{"
    start = _MESSAGES_SOURCE.index(marker)
    body_start = start + len(marker)
    depth = 1
    for end in range(body_start, len(_MESSAGES_SOURCE)):
        ch = _MESSAGES_SOURCE[end]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return _MESSAGES_SOURCE[body_start:end]
    pytest.fail("Could not extract cancel listener body")


def _attach_stream_harness(extra=""):
    """Node harness exposing the terminal cancel listener via a fake EventSource."""
    cancel_listener = _extract_cancel_listener()
    return "\n".join(
        [
            _HARNESS,
            "let _loadSessionGeneration = 1;",
            "let _messageUserUnpinned = false;",
            "const _isMessagePaneNearBottom = () => true;",
            "const _isMessageReaderUnpinned = () => false;",
            "const scrollToBottom = () => {};",
            "const removeThinking = () => {};",
            "const assistantDisplayName = () => 'Hermes';",
            "const _markSessionViewed = () => {};",
            "const _attachProjectedAnchorSceneToLastAssistant = () => {};",
            "const _carryForwardEphemeralTurnFields = (p, n) => n;",
            "const _hydrateTodosFromSession = () => {};",
            "const _dispatchExtensionTurnLifecycle = () => {};",
            "const renderSessionList = () => {};",
            "const _setActivePaneIdleIfOwner = () => {};",
            "const _scheduleAnchorRegistryCleanup = () => {};",
            "const _clearStreamEndRecovery = () => {};",
            "const _cancelThrottledSnapshotTimer = () => {};",
            "const _clearAnchorProseIncrementalNode = () => {};",
            "const _cancelAnimationFramePendingStreamRender = () => {};",
            "const _streamFadeCleanupReduceMotionListener = () => {};",
            "const _smdEndParser = () => {};",
            "const finalizeThinkingCard = () => {};",
            "const _clearStreamHidden = () => {};",
            "const _clearStreamNotificationBackground = () => {};",
            "const _deferStreamErrorIfPageHidden = () => false;",
            "const _flushReasoningToAnchor = () => {};",
            "const _applyToAnchor = () => {};",
            "const _closeSource = () => {};",
            "const _bailOutOfTerminalEventsFromStaleStream = () => false;",
            "const _clearOwnerInflightState = () => {};",
            "const _clearApprovalForOwner = () => {};",
            # Artifact repaint instrumentation (gate review a43ee902 round 2):
            # record every owner-gated paint so tests can assert fail-closed
            # ordering. projectSessionArtifactsForOwner mirrors the production
            # owner gate (session identity + current pane).
            "const artifactPaints = [];",
            "const renderSessionArtifacts = () => { artifactPaints.push('render'); };",
            "const projectSessionArtifactsForOwner = (sid) => {"
            " artifactPaints.push(sid);"
            " return !!(sid && S.session && S.session.session_id === sid); };",
            "let _loadingSessionId = null;",
            "const _isSessionCurrentPane = " +
            _extract_from([_MESSAGES_SOURCE], "_isSessionCurrentPane") + ";",
            "const _clearClarifyForOwner = () => {};",
            "let assistantText = '';",
            "let _persistTimer = null;",
            "let _streamFinalized = false;",
            "let _terminalStateReached = false;",
            # install + projection helpers (same production sources)
            _NORMALIZE,
            *_projection_helpers(),
            _MUTATION_TOOLS,
            "const _adoptRegenerationRevision = " +
            _extract_from([_SESSIONS_SOURCE], "_adoptRegenerationRevision") + ";",
            "const _installCanonicalSession = " +
            _extract_from([_SESSIONS_SOURCE], "_installCanonicalSession") + ";",
            "const _captureLoadedMessageWindow = " +
            _extract_from([_SESSIONS_SOURCE], "_captureLoadedMessageWindow") + ";",
            "const _loadedMessageBoundarySignature = " +
            _extract_from([_SESSIONS_SOURCE], "_loadedMessageBoundarySignature") + ";",
            "const _preserveLoadedMessageWindow = " +
            _extract_from([_SESSIONS_SOURCE], "_preserveLoadedMessageWindow") + ";",
            "const attachLiveStream = (activeSid, streamId, uploaded, options) => { source.addEventListener('cancel', e => { "
            + cancel_listener
            + " }); };",
            extra,
            # Fake EventSource: record listeners; fire() dispatches synchronously.
            """
            const listeners = {};
            class FakeSource {
              constructor() { this.readyState = 1; }
              addEventListener(name, fn) { (listeners[name] = listeners[name] || []).push(fn); }
              fire(name, data) { for (const fn of (listeners[name] || [])) fn({ data }); }
              close() { this.readyState = 2; }
            }
            const source = new FakeSource();
            attachLiveStream('sess-a', 'run-1', []);
            """,
        ]
    )


def test_cancel_fallback_get_resolving_during_session_switch_is_ignored():
    """A cancel fallback GET resolving while an A→B switch is IN FLIGHT must
    not commit stale A over the switch (gate review e43234ad final round).

    Race shape: session A's stream fires 'cancel'; no embedded snapshot, so
    the bounded fallback GET starts. While it is pending the user switches to
    B: loadSession(B) bumps _loadSessionGeneration synchronously and clears
    _loadingSessionId=null→'sess-b', but B's own fetch has not resolved — the
    pane still shows A. The old session_id check passes in exactly that
    window (S.session is still A) and would render stale A; the generation
    fence must reject the settlement instead.
    """

    out = _run_node(
        "\n".join(
            [
                _attach_stream_harness(),
                """
(async()=>{
  let releaseA;
  const gateA = new Promise(r => { releaseA = r; });
  routes['/api/session?session_id=sess-a&messages=1&resolve_model=0&msg_limit=30&expand_renderable=1'] =
    () => gateA.then(() => ({ session: { session_id: 'sess-a', profile: 'default',
      regeneration_revision: 9, _messages_truncated: false, _messages_offset: 0,
      messages: [{ role: 'user', content: 'stale A tail' }] } }));
  routes['/api/session?session_id=sess-b&messages=1&resolve_model=0'] =
    () => ({ session: { session_id: 'sess-b', profile: 'default',
      regeneration_revision: 4, _messages_truncated: false, _messages_offset: 0,
      messages: [{ role: 'user', content: 'B transcript' }] } });

  S.session = { session_id: 'sess-a', profile: 'default', messages: [{ role: 'user', content: 'A live' }] };
  S.messages = S.session.messages;
  S.activeStreamId = 'run-1';

  // Cancel A: no embedded snapshot → fallback GET starts and parks on gateA.
  source.fire('cancel', JSON.stringify({ status: 'cancelled' }));
  // While it is pending: the user switches to B. loadSession(B) has started
  // (generation bumped, pane target moved) but B has NOT installed yet — the
  // pane still shows A. This is the exact window the session_id check misses.
  const generationAtCancel = _loadSessionGeneration;
  _loadSessionGeneration += 1;

  // Stale A settlement resolves while B is still loading.
  releaseA();
  await new Promise(r => setTimeout(r, 0));
  const staleCommitted = S.messages.length === 1 && S.messages[0].content === 'stale A tail';

  // B's load resolves and installs.
  const b = { session_id: 'sess-b', profile: 'default', regeneration_revision: 4,
    _messages_truncated: false, _messages_offset: 0,
    messages: [{ role: 'user', content: 'B transcript' }] };
  S.session = b; S.messages = b.messages;

  console.log(JSON.stringify({
    generationAdvanced: _loadSessionGeneration === generationAtCancel + 1,
    staleCommitted,
    sessionStillB: S.session === b,
    transcriptStillB: S.messages.length === 1 && S.messages[0].content === 'B transcript',
  }));
})().catch(e=>{console.error(e);process.exit(1)});
""",
            ]
        )
    )
    assert out["generationAdvanced"] is True
    assert out["staleCommitted"] is False
    assert out["sessionStillB"] is True
    assert out["transcriptStillB"] is True


def test_cancel_fallback_get_rejected_after_switch_does_not_touch_b():
    """Symmetric check on the embedded-snapshot path: a stale embedded cancel
    snapshot for A arriving after the A→B switch must be rejected too."""

    out = _run_node(
        "\n".join(
            [
                _attach_stream_harness(),
                """
(async()=>{
  routes['/api/session?session_id=sess-a&messages=1&resolve_model=0&msg_limit=30&expand_renderable=1'] =
    () => { throw new Error('fallback GET must not run for an embedded rejection'); };
  S.session = { session_id: 'sess-a', profile: 'default', messages: [{ role: 'user', content: 'A live' }] };
  S.messages = S.session.messages;
  S.activeStreamId = 'run-1';

  // Switch to B first.
  _loadSessionGeneration += 1;
  const b = { session_id: 'sess-b', profile: 'default',
    messages: [{ role: 'user', content: 'B transcript' }] };
  S.session = b; S.messages = b.messages;

  // NOW the stale stream's cancel event fires with an embedded A snapshot.
  source.fire('cancel', JSON.stringify({ status: 'cancelled', session: {
    session_id: 'sess-a', profile: 'default',
    messages: [{ role: 'user', content: 'stale embedded A' }] } }));
  await new Promise(r => setTimeout(r, 0));

  console.log(JSON.stringify({
    sessionStillB: S.session === b,
    transcriptStillB: S.messages.length === 1 && S.messages[0].content === 'B transcript',
  }));
})().catch(e=>{console.error(e);process.exit(1)});
""",
            ]
        )
    )
    assert out["sessionStillB"] is True
    assert out["transcriptStillB"] is True


def test_bounded_cancel_settlement_hydrates_preboundary_artifacts():
    """A bounded (msg_limit=30) cancel fallback GET must not leave Artifacts
    claiming authority over pre-boundary mutations (gate review e43234ad final
    round): the settlement installs the bounded tail without a projection and
    hydrates the complete one asynchronously — OLD_* mutation before the
    boundary plus NEW_* inside it must BOTH be listed exactly once, while the
    visible transcript stays at the reader's bounded boundary."""

    out = _run_node(
        "\n".join(
            [
                _attach_stream_harness(
                    # Async hydration: production _hydrateSessionArtifactProjection
                    # does a full-history GET for bounded sources. Drive it with a
                    # recorded fetch so the test can assert the fence holds.
                    """
                    const _hydrateSessionArtifactProjection = async (session, ownsLoad) => {
                      const data = await api('/api/session?session_id=' + session.session_id + '&messages=1&resolve_model=0');
                      // The generation fence arrives via ownsLoad (same contract
                      // as the production call in loadSession).
                      if (!ownsLoad()) return null;
                      const full = data.session;
                      return _artifactProjectionForSnapshot(full);
                    };
                    """
                ),
                """
(async()=>{
  routes['/api/session?session_id=sess-a&messages=1&resolve_model=0&msg_limit=30&expand_renderable=1'] =
    () => ({ session: { session_id: 'sess-a', profile: 'default', regeneration_revision: 5,
      _messages_truncated: true, _messages_offset: 40, message_count: 60,
      messages: [{ role: 'user', content: 'bounded tail', tool_calls: [
        { name: 'write_file', arguments: JSON.stringify({ path: '/workspace/NEW_in_window.md' }) }] }] } });
  // Complete-history source the hydration step uses.
  routes['/api/session?session_id=sess-a&messages=1&resolve_model=0'] =
    () => ({ session: { session_id: 'sess-a', profile: 'default', regeneration_revision: 5,
      _messages_truncated: false, _messages_offset: 0,
      messages: [
        { role: 'user', content: 'old turn', tool_calls: [
          { name: 'write_file', arguments: JSON.stringify({ path: '/workspace/OLD_preboundary.md' }) }],
          tool_call_id: 'tc-old' },
        { role: 'assistant', content: 'did the old thing' },
        { role: 'user', content: 'bounded tail', tool_calls: [
          { name: 'write_file', arguments: JSON.stringify({ path: '/workspace/NEW_in_window.md' }) }],
          tool_call_id: 'tc-new' },
        { role: 'assistant', content: 'cancelled mid-run' },
      ] } });

  S.session = { session_id: 'sess-a', profile: 'default', messages: [{ role: 'user', content: 'A live' }] };
  S.messages = S.session.messages;
  S.activeStreamId = 'run-1';

  // No embedded snapshot → bounded fallback GET (msg_limit=30).
  source.fire('cancel', JSON.stringify({ status: 'cancelled' }));
  // Give the hydration promise a tick.
  await new Promise(r => setTimeout(r, 0));

  const proj = S.session._artifactProjection;
  const paths = proj ? proj.items.map(i => i.path).sort() : [];
  console.log(JSON.stringify({
    transcriptBounded: S.messages.length === 1 && S.messages[0].content === 'bounded tail',
    truncatedFlag: _messagesTruncated === true,
    offset: _oldestIdx,
    hydrateFetchedFull: calls.some(c => c.url === '/api/session?session_id=sess-a&messages=1&resolve_model=0'),
    hasProjection: !!proj,
    projGeneration: proj ? proj.generation : null,
    paths,
    oldCount: paths.filter(p => p.includes('OLD_preboundary')).length,
    newCount: paths.filter(p => p.includes('NEW_in_window')).length,
  }));
})().catch(e=>{console.error(e);process.exit(1)});
""",
            ]
        )
    )
    assert out["transcriptBounded"] is True
    assert out["truncatedFlag"] is True
    assert out["offset"] == 40
    assert out["hydrateFetchedFull"] is True
    assert out["hasProjection"] is True
    assert out["projGeneration"] == 1
    assert out["oldCount"] == 1  # pre-boundary mutation recovered exactly once
    assert out["newCount"] == 1  # in-window mutation listed exactly once


# ── Gate review a43ee902 (round 2): rejected fallback GET + fail-closed
# artifact lifecycle. The fallback catch and the hydration callback must both
# satisfy one captured owner predicate, and Artifacts must visibly fail closed
# for bounded/failed hydration and repaint for complete embedded snapshots.


def _rejecting_get_harness():
    """Harness whose bounded fallback GET REJECTS (network failure path)."""
    cancel_listener = _extract_cancel_listener()
    return "\n".join(
        [
            _attach_stream_harness(),
            """
(async()=>{
  routes['/api/session?session_id=sess-a&messages=1&resolve_model=0&msg_limit=30&expand_renderable=1'] =
    () => { throw new Error('network failure'); };

  S.session = { session_id: 'sess-a', profile: 'default', messages: [{ role: 'user', content: 'A live' }] };
  S.messages = S.session.messages;
  S.activeStreamId = 'run-1';

  // Cancel A: no embedded snapshot → fallback GET starts and then rejects.
  source.fire('cancel', JSON.stringify({ status: 'cancelled' }));
  // loadSession(B) begins while the GET is in flight: generation bumps and
  // the pane target moves BEFORE B installs — S.session is still A here,
  // which is exactly the window the old session_id-only catch check missed.
  const generationAtCancel = _loadSessionGeneration;
  _loadSessionGeneration += 1;
  _loadingSessionId = 'sess-b';

  // The rejected GET's catch runs while B owns the in-flight transition.
  await new Promise(r => setTimeout(r, 0));

  console.log(JSON.stringify({
    generationAdvanced: _loadSessionGeneration === generationAtCancel + 1,
    sessionStillA: S.session.session_id === 'sess-a',
    transcriptUnchanged: S.messages.length === 1 && S.messages[0].content === 'A live',
    localCancelRowAppended: S.messages.some(m => m && m._error && String(m.content||'').includes('Task cancelled')),
    renderCalls,
  }));
})().catch(e=>{console.error(e);process.exit(1)});
""",
        ]
    )


def test_rejected_cancel_fallback_get_during_inflight_switch_never_touches_stale_a():
    """The fallback catch must satisfy the same captured owner predicate as the
    install path. A rejected A GET resolving during the loadSession(B) window
    (generation bumped, S.session still A) must not append the local
    cancellation row to A, render it, or mark it viewed."""
    out = _run_node(_rejecting_get_harness())
    assert out["generationAdvanced"] is True
    assert out["transcriptUnchanged"] is True
    assert out["localCancelRowAppended"] is False


def test_rejected_cancel_fallback_get_still_settles_when_owner_holds():
    """Symmetric guard: with NO session switch, the same rejected-GET path must
    still settle normally (append the local cancellation row) — the new fence
    must not break the ordinary failure recovery."""
    cancel_listener = _extract_cancel_listener()
    out = _run_node(
        "\n".join(
            [
                _attach_stream_harness(),
                """
(async()=>{
  routes['/api/session?session_id=sess-a&messages=1&resolve_model=0&msg_limit=30&expand_renderable=1'] =
    () => { throw new Error('network failure'); };

  S.session = { session_id: 'sess-a', profile: 'default', messages: [{ role: 'user', content: 'A live' }] };
  S.messages = S.session.messages;
  S.activeStreamId = 'run-1';

  source.fire('cancel', JSON.stringify({ status: 'cancelled' }));
  await new Promise(r => setTimeout(r, 0));

  console.log(JSON.stringify({
    cancelRowAppended: S.messages.some(m => m && m._error && String(m.content||'').includes('Task cancelled')),
    stillA: S.session.session_id === 'sess-a',
  }));
})().catch(e=>{console.error(e);process.exit(1)});
""",
            ]
        )
    )
    assert out["cancelRowAppended"] is True
    assert out["stillA"] is True


def test_bounded_cancel_snapshot_paints_unavailable_then_fails_closed_on_hydration_error():
    """A bounded embedded cancel snapshot must repaint Artifacts IMMEDIATELY
    (fail-closed 'unavailable' state instead of stale DOM), and a REJECTED
    hydration must repaint the unavailable state through the owner-gated
    helper rather than being silently swallowed."""
    cancel_listener = _extract_cancel_listener()
    out = _run_node(
        "\n".join(
            [
                _attach_stream_harness(
                    # Production-shaped hydration: bounded source → full-history
                    # GET, which REJECTS here (the failure branch under test).
                    """
                    const _hydrateSessionArtifactProjection = async (session, ownsLoad) => {
                      if (session._messages_truncated || session._messages_offset > 0) {
                        const data = await api('/api/session?session_id=' + session.session_id + '&messages=1&resolve_model=0');
                        if (!ownsLoad()) return null;
                        return _artifactProjectionForSnapshot(data.session);
                      }
                      return _artifactProjectionForSnapshot(session);
                    };
                    """
                ),
                """
(async()=>{
  // Stale artifact DOM from a PREVIOUS session view is what must not survive.
  artifactPaints.push('pre-existing-stale-paint');

  routes['/api/session?session_id=sess-a&messages=1&resolve_model=0'] =
    () => { throw new Error('full-history GET failed'); };

  S.session = { session_id: 'sess-a', profile: 'default', messages: [{ role: 'user', content: 'A live' }] };
  S.messages = S.session.messages;
  S.activeStreamId = 'run-1';

  // Embedded BOUNDED snapshot (no embedded projection): install must repaint
  // Artifacts immediately, then hydrate; hydration rejects → repaint again.
  source.fire('cancel', JSON.stringify({ status: 'cancelled', session: {
    session_id: 'sess-a', profile: 'default', regeneration_revision: 3,
    _messages_truncated: true, _messages_offset: 30, message_count: 50,
    messages: [{ role: 'user', content: 'bounded tail' }] } }));
  await new Promise(r => setTimeout(r, 0));
  const paintsAfterInstall = artifactPaints.filter(p => p === 'sess-a').length;

  await new Promise(r => setTimeout(r, 10));

  console.log(JSON.stringify({
    paintsAfterInstall,
    // 'render' pushes can only originate from the hydration .catch repaint —
    // the install paint goes through projectSessionArtifactsForOwner instead.
    repaintAfterHydrationFailure: artifactPaints.includes('render'),
    noProjection: !S.session._artifactProjection,
    snapshotStillInstalled: S.session.session_id === 'sess-a',
  }));
})().catch(e=>{console.error(e);process.exit(1)});
""",
            ]
        )
    )
    assert out["paintsAfterInstall"] == 1  # immediate fail-closed paint on install
    assert out["repaintAfterHydrationFailure"] is True  # failure is not swallowed
    assert out["noProjection"] is True
    assert out["snapshotStillInstalled"] is True


def test_complete_embedded_cancel_snapshot_repaints_fresh_projection():
    """A COMPLETE embedded cancel snapshot installs with its projection and the
    Artifacts pane repaints through the owner-gated helper right away — no
    window where the previous session's artifact DOM lingers."""
    cancel_listener = _extract_cancel_listener()
    out = _run_node(
        "\n".join(
            [
                _attach_stream_harness(),
                """
(async()=>{
  artifactPaints.push('pre-existing-stale-paint');

  S.session = { session_id: 'sess-a', profile: 'default', messages: [{ role: 'user', content: 'A live' }] };
  S.messages = S.session.messages;
  S.activeStreamId = 'run-1';

  // Embedded COMPLETE snapshot with a tool mutation: _installCanonicalSession
  // derives the projection synchronously; the settlement must repaint at once.
  source.fire('cancel', JSON.stringify({ status: 'cancelled', session: {
    session_id: 'sess-a', profile: 'default', regeneration_revision: 2,
    _messages_truncated: false, _messages_offset: 0,
    messages: [{ role: 'user', content: 'did a thing', tool_calls: [
      { name: 'write_file', arguments: JSON.stringify({ path: '/workspace/cancel_artifact.md' }) }] }] } }));
  await new Promise(r => setTimeout(r, 0));

  const proj = S.session._artifactProjection;
  console.log(JSON.stringify({
    immediateRepaint: artifactPaints.filter(p => p === 'sess-a').length === 1,
    hasProjection: !!proj,
    artifactListed: proj ? proj.items.some(i => i.path.includes('cancel_artifact')) : false,
  }));
})().catch(e=>{console.error(e);process.exit(1)});
""",
            ]
        )
    )
    assert out["immediateRepaint"] is True
    assert out["hasProjection"] is True
    assert out["artifactListed"] is True


def test_late_hydration_after_snapshot_or_session_replacement_is_dropped():
    """Hydration completing after ANY owner transition — same-session canonical
    replacement or an A→B switch — must attach to neither the replaced snapshot
    nor the new session, and must not repaint."""
    cancel_listener = _extract_cancel_listener()
    out = _run_node(
        "\n".join(
            [
                _attach_stream_harness(
                    """
                    let releaseHydration;
                    const hydrationGate = new Promise(r => { releaseHydration = r; });
                    const _hydrateSessionArtifactProjection = async (session, ownsLoad) => {
                      await hydrationGate;
                      if (!ownsLoad()) return null;
                      return _artifactProjectionForSnapshot(session);
                    };
                    """
                ),
                """
(async()=>{
  routes['/api/session?session_id=sess-a&messages=1&resolve_model=0&msg_limit=30&expand_renderable=1'] =
    () => ({ session: { session_id: 'sess-a', profile: 'default', regeneration_revision: 6,
      _messages_truncated: true, _messages_offset: 20, message_count: 30,
      messages: [{ role: 'user', content: 'bounded tail' }] } });

  S.session = { session_id: 'sess-a', profile: 'default', messages: [{ role: 'user', content: 'A live' }] };
  S.messages = S.session.messages;
  S.activeStreamId = 'run-1';

  // Cancel → bounded fallback GET installs → hydration starts, parks on gate.
  source.fire('cancel', JSON.stringify({ status: 'cancelled' }));
  await new Promise(r => setTimeout(r, 0));
  const installedSnapshot = S.session;

  // Owner transition WHILE hydration is pending: canonical replacement of the
  // same session (as undo/retry/edit do).
  const replacement = { session_id: 'sess-a', profile: 'default',
    regeneration_revision: 7, _messages_truncated: false, _messages_offset: 0,
    messages: [{ role: 'user', content: 'canonical replacement' }] };
  S.session = replacement;

  releaseHydration({ session: { session_id: 'sess-a', profile: 'default',
    regeneration_revision: 6, _messages_truncated: false, _messages_offset: 0,
    messages: [{ role: 'user', content: 'stale full history' }] } });
  await new Promise(r => setTimeout(r, 0));
  const afterReplacement = {
    projectionOnReplaced: !!installedSnapshot._artifactProjection,
    projectionOnCurrent: !!S.session._artifactProjection,
    repaints: artifactPaints.filter(p => p === 'sess-a').length,
  };

  // Second round: switch to B while hydration for A is pending.
  S.session = { session_id: 'sess-a', profile: 'default', messages: [{ role: 'user', content: 'A live' }] };
  S.messages = S.session.messages;
  S.activeStreamId = 'run-2';
  source.fire('cancel', JSON.stringify({ status: 'cancelled' }));
  await new Promise(r => setTimeout(r, 0));
  const snapshotA = S.session;
  const genBefore = _loadSessionGeneration;
  _loadSessionGeneration += 1;
  _loadingSessionId = 'sess-b';
  releaseHydration({ session: { session_id: 'sess-a', profile: 'default',
    regeneration_revision: 6, _messages_truncated: false, _messages_offset: 0,
    messages: [{ role: 'user', content: 'late A full history' }] } });
  await new Promise(r => setTimeout(r, 0));

  console.log(JSON.stringify({
    projectionOnReplaced: afterReplacement.projectionOnReplaced,
    projectionOnCurrent: afterReplacement.projectionOnCurrent,
    repaintsAfterReplacement: afterReplacement.repaints,
    noLateAttachAfterSwitch: !snapshotA._artifactProjection,
  }));
})().catch(e=>{console.error(e);process.exit(1)});
""",
            ]
        )
    )
    assert out["projectionOnReplaced"] is False
    assert out["projectionOnCurrent"] is False
    assert out["noLateAttachAfterSwitch"] is False
