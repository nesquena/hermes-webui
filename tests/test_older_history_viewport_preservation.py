from pathlib import Path
import json
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
_NODE = shutil.which("node")


def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function body not found: {signature}")


def test_loading_older_messages_expands_render_window_before_rendering():
    body = _function_body(SESSIONS_JS, "async function _loadOlderMessages")

    replace_idx = body.index("S.messages = nextMessages")
    expand_idx = body.index("_messageRenderWindowSize=_currentMessageRenderWindowSize()")
    render_idx = body.index("renderMessages({ preserveScroll: true });")

    assert replace_idx < expand_idx < render_idx, (
        "scroll-to-top paging must expand the DOM render window before renderMessages(); "
        "otherwise fetched older messages stay hidden and only the hidden counter changes"
    )
    assert "if(typeof _messageIsRenderable==='function') return _messageIsRenderable(m);" in body
    assert "Math.max(addedRenderable, MESSAGE_RENDER_WINDOW_DEFAULT)" in body


def test_loading_older_messages_preserves_viewport_without_bottom_snap():
    body = _function_body(SESSIONS_JS, "async function _loadOlderMessages")

    assert "renderMessages({ preserveScroll: true });" in body
    assert "const viewportAnchor = (container && typeof _captureMessageViewportAnchor === 'function')" in body
    assert "_captureMessageViewportAnchor()" in body
    assert "_restoreMessageViewportAnchor(viewportAnchor, olderMsgs.length)" in body
    assert "const restoredViaAnchor = (viewportAnchor && typeof _restoreMessageViewportAnchor === 'function')" in body
    assert "if (!restoredViaAnchor) {" in body
    assert "const virtualAddedHeight = (typeof _messageVirtualPrependedHeightDelta === 'function')" in body
    assert "_messageVirtualPrependedHeightDelta(addedRenderable)" in body
    assert "const addedHeight = Number.isFinite(virtualAddedHeight)" in body
    assert "container.scrollTop = oldTop + addedHeight" in body
    assert "container.scrollTop = newScrollH - prevScrollH" not in body

    restore_idx = body.index("_restoreMessageViewportAnchor(viewportAnchor, olderMsgs.length)")
    virtual_idx = body.index("_messageVirtualPrependedHeightDelta(addedRenderable)")
    scroll_delta_idx = body.index("Math.max(0, newScrollH - prevScrollH)")
    unpin_idx = body.rindex("_scrollPinned = false")
    assert restore_idx < virtual_idx < scroll_delta_idx < unpin_idx


def test_loading_older_messages_marks_scroll_programmatic_while_anchoring():
    body = _function_body(SESSIONS_JS, "async function _loadOlderMessages")

    set_idx = body.index("_programmaticScroll = true;")
    restore_idx = body.index("container.scrollTop = oldTop + addedHeight")
    clear_idx = body.index("requestAnimationFrame(()=>{ _programmaticScroll = false; })")
    assert set_idx < restore_idx < clear_idx


def test_loading_older_messages_captures_anchor_before_replacing_messages():
    body = _function_body(SESSIONS_JS, "async function _loadOlderMessages")

    anchor_idx = body.index("const viewportAnchor = (container && typeof _captureMessageViewportAnchor === 'function')")
    replace_idx = body.index("S.messages = nextMessages")
    render_idx = body.index("renderMessages({ preserveScroll: true });")
    restore_idx = body.index("_restoreMessageViewportAnchor(viewportAnchor, olderMsgs.length)")

    assert anchor_idx < replace_idx < render_idx < restore_idx


def _run_loader_case(case: dict) -> dict:
    loader = _function_body(SESSIONS_JS, "async function _loadOlderMessages")
    comparable_helpers = "\n".join(
        _function_body(SESSIONS_JS, signature)
        for signature in (
            "function _messageComparableText",
            "function _stripAttachedFilesMarker",
            "function _stripForcedSkillEnvelope",
            "function _normalizeUserTranscriptText",
            "function _sameTranscriptMessage",
        )
    )
    script = r"""
const assert = require('node:assert/strict');
const scenario = __CASE__;
let _loadingOlder = false;
let _messagesTruncated = true;
let _oldestIdx = 170;
let _messagesGeneration = 7;
let _loadingSessionId = null;
let replacementSnapshot = null;
const _INITIAL_MSG_LIMIT = 30;
const _msgLimitMax = scenario.strategy === 'direct' ? 60 : 1000;
const S = { session: { session_id: 's1', message_count: 200 }, messages: [] };
const calls = [];
const api = (url) => new Promise((resolve) => calls.push({ url, resolve }));
const row = (index) => ({
  _idx: index,
  role: index % 2 ? 'assistant' : 'user',
  content: `row-${index}`,
  _ts: `ts-${index}`,
});
const rows = (start, end) => Array.from({ length: end - start }, (_, i) => row(start + i));
S.messages = rows(170, 200);
const container = { scrollHeight: 800, scrollTop: 120 };
const $ = () => container;
const window = { _carryForwardEphemeralTurnFields: (_old, next) => next };
const _syncToolCallsForLoadedMessages = () => {};
const _advanceVisibleBaselineForServerKnownRows = () => {};
let _messageRenderWindowSize = 30;
const MESSAGE_RENDER_WINDOW_DEFAULT = 30;
const _currentMessageRenderWindowSize = () => _messageRenderWindowSize;
const _messageIsRenderable = (message) => !!message.role;
const msgContent = (message) => message && message.content;
const _isContextCompactionMessage = () => false;
const _isPreservedCompressionTaskListMessage = () => false;
const _isRecoveryControlMessage = () => false;
const _messageHasReasoningPayload = () => false;
const _assistantMessageHasVisibleContent = () => false;
const _captureMessageViewportAnchor = () => null;
const _restoreMessageViewportAnchor = () => false;
const _messageVirtualPrependedHeightDelta = () => null;
const renderMessages = () => {};
let _scrollPinned = true;
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
const performance = { now: () => 0 };
const requestAnimationFrame = (callback) => callback();

__COMPARABLE_HELPERS__
__LOADER__

function primaryPayload() {
  let messages = scenario.primary === 'page' ? rows(140, 170) : rows(140, 200);
  if (scenario.primary === 'mismatch') messages[messages.length - 1] = { ...row(199), content: 'different tail' };
  return { session: {
    session_id: 's1', message_count: 200, messages,
    _messages_offset: 140, _messages_truncated: true, tool_calls: [],
  } };
}
function fallbackPayload() {
  return { session: {
    session_id: 's1', message_count: 200, messages: rows(140, 170),
    _messages_offset: 140, _messages_truncated: true, tool_calls: [],
  } };
}
async function waitForCalls(count) {
  for (let i = 0; i < 20; i++) {
    if (calls.length >= count) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.fail(`expected ${count} API call(s), saw ${calls.length}`);
}
function replaceView() {
  const replacement = rows(scenario.replacementOffset, scenario.replacementOffset + 30);
  if (scenario.sameOffsetReplacement) replacement[29] = { ...replacement[29], content: 'new same-offset row' };
  replacementSnapshot = JSON.stringify(replacement);
  S.messages = replacement;
  S.session.message_count = scenario.replacementOffset + replacement.length;
  _oldestIdx = scenario.replacementOffset;
  return replacement;
}

(async () => {
  const pending = _loadOlderMessages();
  await waitForCalls(1);
  let replacement = null;
  if (scenario.replaceAt === 'primary') replacement = replaceView();
  calls[0].resolve(primaryPayload());
  if (scenario.replaceAt === 'fallback') {
    await waitForCalls(2);
    replacement = replaceView();
    calls[1].resolve(fallbackPayload());
  } else {
    for (let i = 0; i < 20 && calls.length < 2; i++) {
      await new Promise((resolve) => setImmediate(resolve));
    }
    if (calls.length > 1) calls[1].resolve(fallbackPayload());
  }
  await pending;
  console.log(JSON.stringify({
    sameArray: replacement ? S.messages === replacement : false,
    replacementUnchanged: replacement ? JSON.stringify(S.messages) === replacementSnapshot : false,
    messages: S.messages.map((message) => message._idx),
    offset: _oldestIdx,
    loading: _loadingOlder,
    requests: calls.map((call) => call.url),
  }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
""".replace("__CASE__", json.dumps(case)).replace("__COMPARABLE_HELPERS__", comparable_helpers).replace("__LOADER__", loader)
    result = subprocess.run([_NODE, "-e", script], text=True, capture_output=True, check=False, timeout=20)
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("case", "expected_requests"),
    [
        ({"strategy": "direct", "primary": "page", "replaceAt": "primary", "replacementOffset": 172}, 1),
        ({"strategy": "cumulative", "primary": "tail", "replaceAt": "primary", "replacementOffset": 172}, 1),
        ({"strategy": "cumulative", "primary": "mismatch", "replaceAt": "fallback", "replacementOffset": 172}, 2),
        ({"strategy": "direct", "primary": "page", "replaceAt": "primary", "replacementOffset": 170, "sameOffsetReplacement": True}, 1),
    ],
    ids=("direct-page-replaced", "cumulative-tail-replaced", "fallback-await-replaced", "same-offset-replaced"),
)
@pytest.mark.skipif(_NODE is None, reason="node is required to execute the production paging helper")
def test_load_older_messages_rejects_replaced_transcript_view(case, expected_requests):
    result = _run_loader_case(case)

    assert result["sameArray"] is True
    assert result["replacementUnchanged"] is True
    assert result["offset"] == case["replacementOffset"]
    assert result["loading"] is False
    assert len(result["requests"]) == expected_requests


@pytest.mark.parametrize(
    ("case", "expected_requests"),
    [
        ({"strategy": "direct", "primary": "page"}, 1),
        ({"strategy": "cumulative", "primary": "tail"}, 1),
        ({"strategy": "cumulative", "primary": "mismatch"}, 2),
    ],
    ids=("direct-page", "cumulative-tail", "mismatch-fallback"),
)
@pytest.mark.skipif(_NODE is None, reason="node is required to execute the production paging helper")
def test_load_older_messages_keeps_unchanged_view_paging_paths(case, expected_requests):
    result = _run_loader_case(case)

    assert result["messages"] == list(range(140, 200))
    assert result["offset"] == 140
    assert result["loading"] is False
    assert len(result["requests"]) == expected_requests
