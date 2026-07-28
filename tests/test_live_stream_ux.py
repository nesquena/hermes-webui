import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
RUN_JOURNAL_PY = (ROOT / "api" / "run_journal.py").read_text(encoding="utf-8")

def test_stale_interrupted_event_marks_recovery_control():
    assert "\"recovery_control\": True" in RUN_JOURNAL_PY


def test_done_and_restore_filters_recovery_messages_from_frontend_state():
    assert "_filterRecoveryControlMessages(S.messages || [])" in MESSAGES_JS
    assert "if(!m||m.role==='tool') return false;" in MESSAGES_JS
    assert "if(m.recovery_control===true) return true;" in MESSAGES_JS
    assert "continue exactly where you left off" in MESSAGES_JS
    assert "do not retry the same tool call" in MESSAGES_JS


def test_apererror_recovers_on_recovery_control_event():
    assert "isRecoveryControlMessage=isInterrupted && (d.recovery_control===true || _streamRecoveryControlMessageText(d.message));" in MESSAGES_JS
    assert "Stream recovery signal received. Restoring transcript..." in MESSAGES_JS
    node = shutil.which("node")
    if node is None:
        import pytest
        pytest.skip("node not on PATH")

    def event_body(source):
        marker = "source.addEventListener('apperror'"
        start = source.index(marker)
        brace = source.index("{", start)
        depth = 0
        for i in range(brace, len(source)):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    return "let transportGeneration=7;\n" + source[brace + 1:i]
        raise AssertionError("unclosed apperror listener")

    def run(source, mutated=False):
        body = event_body(source)
        if mutated:
            body = body.replace("transportGeneration})) return;", "retainedTransportGeneration})) return;", 1)
        script = textwrap.dedent(
            f"""
            let restoreCalls = [];
            let unhandled = [];
            process.on('unhandledRejection', error => {{ unhandled.push(String(error)); }});
            const source = {{ readyState: 1, listeners: {{}}, closeCalls: 0,
              addEventListener(name, callback) {{ this.listeners[name] = callback; }},
              close() {{ this.closeCalls += 1; this.readyState = 2; }}
            }};
            let activeSid = 'sid-1';
            let streamId = 'stream-1';
            let assistantText = '';
            let _persistTimer = null;
            globalThis.S = {{ session: {{ session_id: activeSid }}, activeStreamId: streamId, messages: [] }};
            globalThis.window = {{}};
            globalThis.localStorage = {{ setItem() {{}} }};
            globalThis.INFLIGHT = {{}};
            globalThis._bailOutOfTerminalEventsFromStaleStream = () => false;
            globalThis._clearStreamEndRecovery = () => {{}};
            globalThis._cancelThrottledSnapshotTimer = () => {{}};
            globalThis._cancelAnimationFramePendingStreamRender = () => {{}};
            globalThis._streamFadeCleanupReduceMotionListener = () => {{}};
            globalThis._smdEndParser = () => {{}};
            globalThis._clearOwnerInflightState = () => {{}};
            globalThis._clearStreamHidden = () => {{}};
            globalThis._clearStreamNotificationBackground = () => {{}};
            globalThis._clearAnchorProseIncrementalNode = () => {{}};
            globalThis._clearApprovalForOwner = () => {{}};
            globalThis._clearClarifyForOwner = () => {{}};
            globalThis._flushReasoningToAnchor = () => {{}};
            globalThis._applyToAnchor = () => {{}};
            globalThis._scheduleAnchorRegistryCleanup = () => {{}};
            globalThis.clearLiveToolCards = () => {{}};
            globalThis.removeThinking = () => {{}};
            globalThis.showToast = () => {{}};
            globalThis._streamRecoveryControlMessageText = () => false;
            globalThis._restoreSettledSession = async (calledSource, options) => {{
              restoreCalls.push({{ calledSource, options }});
              return false;
            }};
            globalThis._filterRecoveryControlMessages = messages => messages.filter(message => !message.recovery_control);
            globalThis._markSessionViewed = () => {{}};
            globalThis.renderMessages = () => {{}};
            globalThis._setActivePaneIdleIfOwner = () => {{}};
            globalThis.renderSessionList = () => {{}};
            globalThis._currentLiveEventSourceOwnsStream = () => true;
            globalThis._liveStreamEndScenePresent = () => false;
            globalThis.$ = () => null;
            const dispatch = async e => {{
            {body}
            }};
            await dispatch({{ currentTarget: source, target: source, data: JSON.stringify({{
              type: 'interrupted', recovery_control: true, session_id: activeSid,
              message: 'continue exactly where you left off'
            }}) }});
            await new Promise(resolve => setImmediate(resolve));
            await new Promise(resolve => setImmediate(resolve));
            console.log(JSON.stringify({{ restoreCalls: restoreCalls.map(call => ({{
              sameSource: call.calledSource === source,
              generation: call.options && call.options.transportGeneration,
            }})), unhandled, closeCalls: source.closeCalls,
              activeStreamId: S.activeStreamId }}));
            """
        )
        with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False, encoding="utf-8") as handle:
            handle.write("(async()=>{\n" + script + "\n})().catch(error=>{ console.error(error); process.exit(1); });\n")
            path = handle.name
        try:
            return subprocess.run([node, path], capture_output=True, text=True, timeout=15)
        finally:
            Path(path).unlink(missing_ok=True)

    result = run(MESSAGES_JS)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["restoreCalls"] == [{"sameSource": True, "generation": 7}]
    assert observed["unhandled"] == []
    assert observed["closeCalls"] == 1
    assert observed["activeStreamId"] is None

    mutated_result = run(MESSAGES_JS, mutated=True)
    assert mutated_result.returncode != 0 or json.loads(mutated_result.stdout)["unhandled"]


def test_ui_rejects_recovery_control_as_visible_assistant_content():
    assert "function _isRecoveryControlMessageText" in UI_JS
    assert "function _assistantMessageHasVisibleContent" in UI_JS
    assert "if(_isRecoveryControlMessage(m)) return false;" in UI_JS
    assert "function _messageIsRenderable(m)" in UI_JS
    assert "if(_isRecoveryControlMessage(m)) return false;" in UI_JS[UI_JS.index("function _messageIsRenderable(m)"):]
    assert "_assistantMessageHasVisibleContent(m)" in UI_JS


def test_recovery_control_detection_is_not_broad_phrase_matching():
    assert "|| /continue exactly where you left off/i.test(normalized)" not in UI_JS
    assert "|| /continue exactly where you left off/i.test(normalized)" not in MESSAGES_JS
    assert "const systemRecovery=/^\\[System:/i.test(normalized)" in UI_JS
    assert "const backendRecovery=/^the live worker stopped before this run finished\\.?$/i.test(normalized)" in UI_JS
def test_recovery_control_does_not_filter_genuine_interruption_card():
    """A real 'Response interrupted' card carries provider_details_label
    'Interruption details' but is NOT a recovery-control row — it must stay
    visible. Earlier drafts filtered on that label, which would drop a genuine
    interruption the user should see (the inverse-#3300 data-loss class). Drive
    the ACTUAL _isRecoveryControlMessage() from ui.js via node.
    """
    import shutil, subprocess, json
    node = shutil.which("node")
    if node is None:
        import pytest
        pytest.skip("node not on PATH")

    def _fn(src, name):
        start = src.index(f"function {name}(")
        brace = src.index("{", start)
        depth = 0
        for i in range(brace, len(src)):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    return src[start:i + 1]
        raise AssertionError(name)

    fns = "\n".join(_fn(UI_JS, n) for n in ("_isRecoveryControlMessageText", "_isRecoveryControlMessage"))
    driver = (
        "function msgContent(m){return (m&&m.content)||'';}\n"
        + fns + "\n"
        + "const cases = JSON.parse(process.argv[1]);\n"
        + "process.stdout.write(JSON.stringify(cases.map(_isRecoveryControlMessage)));\n"
    )
    cases = [
        # genuine interruption card the user MUST see — label set, no marker, normal text
        {"role": "assistant", "content": "**Response interrupted:** the model stopped early",
         "provider_details_label": "Interruption details"},
        # a real user turn that merely mentions interruption — must stay
        {"role": "user", "content": "my previous response was cut off, can you continue?"},
        # explicit server marker — IS recovery control
        {"role": "assistant", "content": "anything", "recovery_control": True},
        # strict synthetic backend text — IS recovery control (backward-compat)
        {"role": "assistant", "content": "The live worker stopped before this run finished."},
    ]
    r = subprocess.run([node, "-e", driver, json.dumps(cases)], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out == [False, False, True, True], (
        "genuine interruption card (label only) and a real user turn must NOT be "
        f"filtered; only the explicit marker + strict synthetic text are. Got {out}"
    )
