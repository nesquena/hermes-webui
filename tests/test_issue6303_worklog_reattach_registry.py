"""Behavioral regression tests for #6303/#6381 — Worklog identity across session reattach.

The fix deletes the stale `window._liveAnchorRegistries` entry ONLY after passing
the existing-OPEN-transport check in `attachLiveStream()`. This ensures a reused
EventSource keeps the registry its handlers actually own, while a fresh connection
gets a clean registry in sync with the restored INFLIGHT counters.

Two ownership paths are tested:

Path 1 (reconnect-OPEN-reuse)
  - Seed an existing OPEN same-stream EventSource + registry.
  - Call attachLiveStream({reconnecting:true}).
  - The early-return branch fires (existing OPEN transport is reused).
  - The registry survives the call: handler-owned == renderer-projected.

Path 2 (fresh-connection-cleanup)
  - No existing OPEN transport for the streamId.
  - Call attachLiveStream({reconnecting:true}).
  - The function reaches the registry-delete line (after the transport check).
  - The stale registry is cleared; a new one will be created by the new closure.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = ROOT / "static" / "messages.js"
NODE = shutil.which("node")


def _read(path: Path) -> str:
    assert path.exists(), f"{path} not found"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Function extractors for attachLiveStream
# ---------------------------------------------------------------------------

def _function_source(src: str, name: str) -> str:
    """Extract a complete 'function name(...){...}' declaration from source,
    correctly handling nested braces by tracking depth.
    """
    start = src.find(f"function {name}(")
    assert start != -1, f"function {name} not found"
    params_idx = src.find("(", start)
    assert params_idx != -1
    depth_p = 0
    close_p = -1
    for idx in range(params_idx, len(src)):
        c = src[idx]
        if c == "(":
            depth_p += 1
        elif c == ")":
            depth_p -= 1
            if depth_p == 0:
                close_p = idx
                break
    assert close_p != -1, f"{name} params did not close"
    brace = src.find("{", close_p)
    assert brace != -1, f"{name} body not found"
    depth_b = 0
    for idx in range(brace, len(src)):
        c = src[idx]
        if c == "{":
            depth_b += 1
        elif c == "}":
            depth_b -= 1
            if depth_b == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name} body did not close")


# ---------------------------------------------------------------------------
# Node.js test runner — writes script to temp file to avoid arg length limits
# ---------------------------------------------------------------------------

_MOCK_GLOBALS = textwrap.dedent("""\
// Mock globals that attachLiveStream expects. These mirror the module-scope
// declarations in messages.js and the callees attachLiveStream calls.
var window = globalThis;
var INFLIGHT = {};
var LIVE_STREAMS = {};
var S = { session: null, activeStreamId: null, messages: [] };
var _STREAM_WAS_HIDDEN = {};
var _STREAM_NOTIFICATION_BACKGROUND = {};
var _desktopBackgroundedForNotifications = false;

// attachLiveStream mutates window._liveAnchorRegistries — seed it.
window._liveAnchorRegistries = new Map();

// EventSource creation needs document.baseURI
var document = { baseURI: 'http://localhost:8080/', hidden: false };
var location = { href: 'http://localhost:8080/' };

// Mock EventSource
function EventSource() {
  // Return an object with addEventListener so _wireSSE doesn't crash
  this.addEventListener = () => {};
  this.close = () => {};
  this.readyState = EventSource.OPEN;
}
EventSource.OPEN = 1;
EventSource.CONNECTING = 0;

// Mock functions called by attachLiveStream
function _bindStreamHiddenTracker() {}
function ensureLiveWorklogShell() {}
function showLiveRunStatus() {}
function closeOtherLiveStreams() {}
function closeLiveStream() {}
function resetTurnWorkspaceMutations() {}
function _resetStreamScrollFollow() {}
function _suspendSessionStreamForLiveChat() {}
function clearInflight() {}
function clearInflightState() {}
function _scheduleAnchorRegistryCleanup() {}
function _closeSource() {}
function _approvalBelongsToOwner() { return true; }
function _clarifyBelongsToOwner() { return true; }
function _clearApprovalPendingForSession() {}
function _clearClarifyPendingForSession() {}
function stopApprovalPolling() {}
function stopClarifyPolling() {}
function hideApprovalCard() {}
function hideClarifyCard() {}
function _closeAdjustableRelated() {}
function _relateLiveStreamPane() {}
function _ensureInflightLiveAssistantMessage() {}
""").lstrip()


def _run_attach_script(setup_code: str) -> dict:
    """Extract attachLiveStream from messages.js, wrap it in mocks + setup,
    run via Node temp file, and return parsed JSON results.

    The ``setup_code`` must define ``const __results = {};``, set up mocks,
    call ``attachLiveStream(...)``, populate ``__results``, and
    ``console.log(JSON.stringify(__results));``.
    """
    assert NODE, "node executable is required for JavaScript behavioral checks"
    attach_fn = _function_source(_read(MESSAGES_JS), "attachLiveStream")
    full_script = (
        _MOCK_GLOBALS
        + "\n"
        + attach_fn
        + "\n\n"
        + setup_code
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", encoding="utf-8", delete=False
    ) as f:
        f.write(full_script)
        script_path = f.name
    try:
        result = subprocess.run(
            [NODE, script_path],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        Path(script_path).unlink(missing_ok=True)
        pytest.fail(
            "node behavior check timed out"
            f"\nstdout:\n{exc.stdout or '<empty>'}"
            f"\nstderr:\n{exc.stderr or '<empty>'}"
        )
    Path(script_path).unlink(missing_ok=True)
    if result.returncode:
        pytest.fail(
            "node behavior check failed"
            f"\nexit code: {result.returncode}"
            f"\nstdout:\n{result.stdout or '<empty>'}"
            f"\nstderr:\n{result.stderr or '<empty>'}"
        )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        pytest.fail("node produced no stdout output")
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# Path 1: Existing OPEN same-stream EventSource is reused — registry preserved
# ---------------------------------------------------------------------------


def test_reconnect_keeps_registry_when_open_transport_reused():
    """Path 1: attachLiveStream(reconnecting=true) with an existing OPEN
    same-stream EventSource must preserve the handler-owned registry.

    After reattach:
      - the registry in window._liveAnchorRegistries is the SAME object that
        the existing handler owns (same reference as set up before the call);
      - the registry was not deleted (early return prevents deletion).
    """
    setup = textwrap.dedent("""\
    const __results = {};

    const SID = 'test-sid';
    const STREAM_ID = 'test-stream';

    // Seed INFLIGHT
    INFLIGHT[SID] = {
      messages: [],
      uploaded: [],
      toolCalls: [],
      streamId: STREAM_ID,
      activityBurstAnchors: [],
      currentActivityBurstId: 0,
      currentLiveSegmentSeq: 0,
    };

    // Create a known registry object and store it before the call.
    const originalRegistry = { anchor: { session_id: SID, turn_id: 'turn-1' }, events: [] };
    window._liveAnchorRegistries.set(STREAM_ID, originalRegistry);

    // Mock an OPEN same-stream EventSource so the transport-reuse branch fires.
    LIVE_STREAMS[SID] = {
      streamId: STREAM_ID,
      source: { readyState: EventSource.OPEN },
    };

    // Capture the handler's registry reference BEFORE attachLiveStream.
    __results.handlerRegistryRef = window._liveAnchorRegistries.get(STREAM_ID);
    __results.sameObjectBefore = (__results.handlerRegistryRef === originalRegistry);

    // Call attachLiveStream — should take the early-return (OPEN-reuse) branch.
    attachLiveStream(SID, STREAM_ID, [], { reconnecting: true });

    // After the call, the registry must still be in the window map.
    __results.registryKept = window._liveAnchorRegistries.has(STREAM_ID);

    // The projected registry ref must be the SAME object as what the handler owns.
    __results.projectedRegistryRef = window._liveAnchorRegistries.get(STREAM_ID);
    __results.sameObjectRef = (__results.handlerRegistryRef === __results.projectedRegistryRef);

    console.log(JSON.stringify(__results));
    """)

    result = _run_attach_script(setup)

    assert result["sameObjectBefore"] is True, (
        "handlerRegistryRef must be the original registry object"
    )
    assert result["registryKept"] is True, (
        "Registry should survive when the same-stream OPEN EventSource is reused"
    )
    assert result["sameObjectRef"] is True, (
        "Handler-owned registry and renderer-projected registry must be the same object"
        " — a stale delete would leave renderers pointing at a different entry"
    )


# ---------------------------------------------------------------------------
# Path 2: Fresh connection (no existing OPEN transport) clears stale registry
# ---------------------------------------------------------------------------


def test_reconnect_clears_stale_registry_when_no_open_transport():
    """Path 2: attachLiveStream(reconnecting=true) with NO existing same-stream
    OPEN EventSource must delete the stale registry from the window map.

    A new EventSource will be created and the fresh closure will build its own
    registry in sync with the restored INFLIGHT counters.
    """
    setup = textwrap.dedent("""\
    const __results = {};

    const SID = 'test-sid';
    const STREAM_ID = 'test-stream';

    // Seed INFLIGHT so the function has known state.
    INFLIGHT[SID] = {
      messages: [],
      uploaded: [],
      toolCalls: [],
      streamId: STREAM_ID,
      activityBurstAnchors: [],
      currentActivityBurstId: 0,
      currentLiveSegmentSeq: 0,
    };

    // Create and store a stale registry.
    const staleRegistry = { anchor: { session_id: SID, turn_id: 'turn-stale' }, events: [] };
    window._liveAnchorRegistries.set(STREAM_ID, staleRegistry);

    // Do NOT set up LIVE_STREAMS — no existing transport.

    // Capture the ref before the call.
    __results.handlerRegistryRef = window._liveAnchorRegistries.get(STREAM_ID);

    // Call attachLiveStream — no OPEN transport to reuse, so the registry-delete
    // path fires after the transport check fails.
    attachLiveStream(SID, STREAM_ID, [], { reconnecting: true });

    // The stale registry must be gone.
    __results.registryCleared = !window._liveAnchorRegistries.has(STREAM_ID);
    __results.projectedRegistryRef = window._liveAnchorRegistries.get(STREAM_ID);

    console.log(JSON.stringify(__results));
    """)

    result = _run_attach_script(setup)

    assert result["registryCleared"] is True, (
        "Stale registry should be deleted when no existing OPEN transport is reused"
    )
    # After deletion, get() returns undefined → JSON.stringify omits the key.
    # We verify the key is absent rather than asserting null.
    assert "projectedRegistryRef" not in result, (
        "After deletion, window._liveAnchorRegistries.get(streamId) must return undefined"
    )
