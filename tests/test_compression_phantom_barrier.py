"""Behavioral regression tests for the phantom compression barrier.

The harness loads the shipping classic scripts in a real browser page, replaces
only the SSE transport, and dispatches events through attachLiveStream's real
listeners. All HTTP requests are fulfilled in-process; no WebUI server or
network access is used.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
UI_JS = (STATIC / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (STATIC / "messages.js").read_text(encoding="utf-8")
SUPPORT_SCRIPTS = [
    (STATIC / name).read_text(encoding="utf-8")
    for name in ("i18n.js", "icons.js", "assistant_turn_anchors.js")
]

def _matching_brace(source: str, opening: int) -> int:
    """Return the closing brace paired with ``opening``."""
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise AssertionError("unclosed JavaScript block in mutation target")


def _revert_running_cleanup(source: str) -> str:
    """Revert the reviewed phase branch inside the production done listener."""
    done_start = source.index("source.addEventListener('done',e=>{")
    done_end = source.index("source.addEventListener('stream_end'", done_start)
    done_handler = source[done_start:done_end]
    phase_match = re.search(
        r"if\s*\(\s*window\._compressionUi\.phase\s*===\s*['\"]running['\"]\s*\)\s*\{",
        done_handler,
    )
    assert phase_match, "running cleanup branch missing from production done listener"

    branch_start = done_start + phase_match.start()
    if_open = source.index("{", done_start + phase_match.start(), done_start + phase_match.end())
    if_close = _matching_brace(source, if_open)
    else_match = re.match(r"\s*else\s*\{", source[if_close + 1 :])
    assert else_match, "running cleanup branch no longer has its rebind control"
    else_open = if_close + 1 + else_match.end() - 1
    else_close = _matching_brace(source, else_open)

    old_branch = source[branch_start : else_close + 1]
    assert "clearCompressionUi()" in old_branch
    assert "sessionId:d.session.session_id" in old_branch.replace(" ", "")
    reverted = "window._compressionUi={...window._compressionUi, sessionId:d.session.session_id};"
    return source[:branch_start] + reverted + source[else_close + 1 :]


# Revert only the fix under review to its pre-fix unconditional rebind. The
# ownership/automatic guard remains intact, so controls must behave identically.
MUTATED_MESSAGES_JS = _revert_running_cleanup(MESSAGES_JS)

# The real page DOM is used, but its deferred script/resource tags are removed:
# scripts are injected explicitly below and every request is fulfilled locally.
HARNESS_HTML = re.sub(r"<script\b[^>]*>.*?</script>", "", INDEX_HTML, flags=re.I | re.S)
HARNESS_HTML = re.sub(r"<link\b[^>]*>", "", HARNESS_HTML, flags=re.I)

_MOCK_EVENT_SOURCE = r"""
class MockEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  static instances = [];
  constructor(url) {
    this.url = String(url);
    this.readyState = MockEventSource.CONNECTING;
    this.listeners = new Map();
    MockEventSource.instances.push(this);
  }
  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }
  removeEventListener(name, listener) {
    this.listeners.set(name, (this.listeners.get(name) || []).filter(fn => fn !== listener));
  }
  emit(name, payload) {
    const event = {type: name, data: JSON.stringify(payload)};
    for (const listener of this.listeners.get(name) || []) listener(event);
  }
  close() { this.readyState = MockEventSource.CLOSED; }
}
window.EventSource = MockEventSource;
window.MockEventSource = MockEventSource;
"""

# These are unrelated terminal-event surfaces. Compression state, timer, lock,
# placeholder, rendering, attachLiveStream, and both SSE listeners remain the
# production implementations.
_STABILIZE_UNRELATED_UI = r"""
appendLiveCompressionCard = () => false;
resetTurnWorkspaceMutations = () => {};
_resetStreamScrollFollow = () => {};
ensureLiveWorklogShell = () => {};
_shouldFollowMessagesOnDomReplace = () => false;
_isMessagePaneNearBottom = () => false;
_markSessionViewed = () => {};
setBusy = () => {};
setComposerStatus = () => {};
setStatus = () => {};
syncTopbar = () => {};
renderMessages = () => {};
renderSessionList = () => {};
loadDir = () => {};
playNotificationSound = () => {};
sendBrowserNotification = () => {};
api = async () => ({});
"""

_SCENARIO = r"""
async ({kind, doneSid}) => {
  const activeSid = 'session-a';
  const streamId = 'stream-under-test';
  S.session = {session_id: activeSid, messages: []};
  S.messages = [];
  S.toolCalls = [];
  S.activeStreamId = streamId;
  S.busy = true;
  window._compressionUi = null;
  window._compressionLockSid = null;

  const input = document.getElementById('msg');
  const cards = document.getElementById('liveCompressionCards');
  input.placeholder = 'Original placeholder';
  cards.innerHTML = '';
  cards.style.display = '';

  attachLiveStream(activeSid, streamId, []);
  const source = MockEventSource.instances.at(-1);
  if (!source) throw new Error('attachLiveStream did not construct EventSource');
  if (!source.listeners.has('compressing') || !source.listeners.has('done')) {
    throw new Error('production compressing/done listeners were not registered');
  }

  if (kind === 'running') {
    source.emit('compressing', {session_id: activeSid});
  } else if (kind === 'non-running') {
    setCompressionUi({sessionId: activeSid, phase: 'done', automatic: true});
  } else if (kind === 'manual') {
    setCompressionUi({sessionId: activeSid, phase: 'running', automatic: false});
  } else if (kind === 'unowned') {
    setCompressionUi({phase: 'running', automatic: true});
  } else if (kind === 'stale-owner') {
    setCompressionUi({sessionId: 'session-stale', phase: 'running', automatic: true});
  } else {
    throw new Error(`unknown scenario: ${kind}`);
  }

  const before = {
    state: window._compressionUi ? {...window._compressionUi} : null,
    timerRunning: _compressionElapsedTimer !== null,
    lock: _compressionSessionLock(),
    placeholder: input.placeholder,
  };

  // A real clearCompressionUi call must remove this stale rendered surface.
  cards.innerHTML = '<span data-stale-compression>stale</span>';
  cards.style.display = '';

  source.emit('done', {
    session: {
      session_id: doneSid,
      messages: [{role: 'assistant', content: 'complete'}],
      tool_calls: [],
    },
    stream_id: streamId,
  });

  const after = {
    state: window._compressionUi ? {...window._compressionUi} : null,
    timerRunning: _compressionElapsedTimer !== null,
    lock: _compressionSessionLock(),
    placeholder: input.placeholder,
    cardsHtml: cards.innerHTML,
    cardsDisplay: cards.style.display,
    currentSid: S.session && S.session.session_id,
  };
  return {before, after};
}
"""


@pytest.fixture(scope="module")
def browser():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        instance = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        yield instance
        instance.close()


def _run(browser, kind: str, done_sid: str, *, mutated: bool = False) -> dict:
    page = browser.new_page()
    page.route(
        "**/*",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html" if route.request.url == "http://harness.test/" else "text/plain",
            body=HARNESS_HTML if route.request.url == "http://harness.test/" else "",
        ),
    )
    page.add_init_script(_MOCK_EVENT_SOURCE)
    try:
        page.goto("http://harness.test/", wait_until="domcontentloaded")
        for script in SUPPORT_SCRIPTS:
            page.add_script_tag(content=script)
        page.add_script_tag(content=UI_JS)
        page.add_script_tag(content=MUTATED_MESSAGES_JS if mutated else MESSAGES_JS)
        page.evaluate(_STABILIZE_UNRELATED_UI)
        return page.evaluate(_SCENARIO, {"kind": kind, "doneSid": done_sid})
    finally:
        page.close()


def _assert_running_prerequisite(result: dict) -> None:
    before = result["before"]
    assert before["state"]["sessionId"] == "session-a"
    assert before["state"]["automatic"] is True
    assert before["state"]["phase"] == "running"
    assert before["timerRunning"] is True
    assert before["lock"] == "session-a"
    assert before["placeholder"] == "Type a message — it will queue and send after compression"


def _assert_canonical_clear(result: dict, done_sid: str) -> None:
    after = result["after"]
    assert after == {
        "state": None,
        "timerRunning": False,
        "lock": None,
        "placeholder": "Original placeholder",
        "cardsHtml": "",
        "cardsDisplay": "none",
        "currentSid": done_sid,
    }


@pytest.mark.parametrize("done_sid", ["session-a", "session-b"], ids=["A-to-A", "A-to-B"])
def test_owned_running_state_is_cleared_through_real_sse_path(browser, done_sid):
    result = _run(browser, "running", done_sid)
    _assert_running_prerequisite(result)
    _assert_canonical_clear(result, done_sid)


@pytest.mark.parametrize("done_sid", ["session-a", "session-b"], ids=["A-to-A", "A-to-B"])
def test_non_running_state_is_rebound_through_real_done_listener(browser, done_sid):
    result = _run(browser, "non-running", done_sid)
    assert result["before"]["state"]["phase"] == "done"
    assert result["after"]["state"]["phase"] == "done"
    assert result["after"]["state"]["sessionId"] == done_sid


@pytest.mark.parametrize("kind", ["manual", "unowned", "stale-owner"])
def test_unowned_compression_controls_are_untouched(browser, kind):
    result = _run(browser, kind, "session-b")
    assert result["after"]["state"] == result["before"]["state"]


def test_reverting_cleanup_branch_breaks_owned_running_only(browser):
    for done_sid in ("session-a", "session-b"):
        result = _run(browser, "running", done_sid, mutated=True)
        _assert_running_prerequisite(result)
        with pytest.raises(AssertionError):
            _assert_canonical_clear(result, done_sid)
        assert result["after"]["state"]["phase"] == "running"
        assert result["after"]["state"]["sessionId"] == done_sid
        assert result["after"]["timerRunning"] is True
        assert result["after"]["lock"] == "session-a"
        assert result["after"]["cardsHtml"] != ""

    for done_sid in ("session-a", "session-b"):
        result = _run(browser, "non-running", done_sid, mutated=True)
        assert result["after"]["state"]["phase"] == "done"
        assert result["after"]["state"]["sessionId"] == done_sid

    for kind in ("manual", "unowned", "stale-owner"):
        result = _run(browser, kind, "session-b", mutated=True)
        assert result["after"]["state"] == result["before"]["state"]
