"""Regression tests for cancelStream() owner-aware cancel behavior.

Covers the runtime/control-plane bug where ``cancelStream()`` in
``static/boot.js`` cleared frontend busy state unconditionally after
issuing ``/api/chat/cancel``:

  1. Treated active-session Stop like session-switch teardown and could close
     the SSE before the backend ``cancel`` event settled the visible transcript.
  2. Did not read the cancel response, so a ``cancelled:false`` from the
     backend (stream already finalized, stream rotated, or session lock
     held by a newer turn) could not surface to the user.
  3. Did not guard the local-state clear with the original streamId, so a
     new turn's busy state could be wiped by a cancel of the previous turn.

Two test layers, matching the repo's mixed static/runtime style:

  * ``TestCancelStreamOwnerGuardStructural`` — regex/AST-level checks on
    the function body to lock in the fix structure. Cheap, fast feedback.
  * ``TestCancelStreamOwnerGuardRuntime`` — actually exercises the
    function with mocked globals via ``node --input-type=module -e`` to
    assert the *behavior*, not just the source shape. Covers positive
    and negative paths and the owner-guard race.

Issue reference: #3344; PR body cross-references
``docs/rfcs/webui-run-state-consistency-contract.md`` (Invariants #2, #4).
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")


# ── Source extraction ──────────────────────────────────────────────────────

def _extract_cancel_stream(src: str) -> str:
    """Return the full source of ``async function cancelStream() {...}``.

    Brace-counts so nested blocks (try/catch, if/else) are handled
    correctly. Mirrors ``extract_fn`` in ``test_streaming_markdown.py``.
    """
    m = re.search(r"async function cancelStream\s*\(", src)
    assert m, "cancelStream() not found in static/boot.js"
    brace_pos = src.index("{", m.end())
    depth = 1
    pos = brace_pos + 1
    while pos < len(src) and depth > 0:
        ch = src[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        pos += 1
    return src[m.start():pos]


CANCEL_STREAM_SRC = _extract_cancel_stream(BOOT_JS)


# ── Static tests ───────────────────────────────────────────────────────────

class TestCancelStreamOwnerGuardStructural:
    """Lock in the fix structure inside the cancelStream() function body."""

    def test_function_present(self):
        assert "async function cancelStream" in BOOT_JS, (
            "cancelStream() should still be a top-level function in boot.js"
        )

    def test_captures_sid_and_stream_id_at_entry(self):
        """The fix must snapshot ``sid`` and ``streamId`` at function entry
        so the owner check and SSE close both reference the original
        values, not whatever ``S.activeStreamId`` is when the fetch
        resolves."""
        assert "const sid" in CANCEL_STREAM_SRC or "let sid" in CANCEL_STREAM_SRC, (
            "cancelStream() must capture sid (S.session.session_id) at entry"
        )
        assert "S.activeStreamId" in CANCEL_STREAM_SRC, (
            "cancelStream() must still read S.activeStreamId to get streamId"
        )

    def test_reads_cancel_response_json(self):
        """The fix must read the ``/api/chat/cancel`` response body so
        ``cancelled:false`` can be observed."""
        assert "r.json" in CANCEL_STREAM_SRC, (
            "cancelStream() must read the response JSON to surface cancelled:false"
        )

    def test_calls_close_live_stream(self):
        """The fix must close stale owned-path SSE streams, but not the
        active owned stream before backend terminal settle."""
        assert "closeLiveStream" in CANCEL_STREAM_SRC, (
            "cancelStream() must conditionally call closeLiveStream(sid, streamId) "
            "only when the stream is no longer owned"
        )
        # Must pass the captured streamId, not S.activeStreamId (which may
        # have rotated by the time the fetch resolves).
        m = re.search(r"closeLiveStream\s*\(([^)]*)\)", CANCEL_STREAM_SRC)
        assert m, "closeLiveStream() call must use a positional argument list"
        call_args = m.group(1)
        assert "S.activeStreamId" not in call_args, (
            "closeLiveStream() must use the captured streamId, not the current "
            "S.activeStreamId (which may have rotated to a new turn)"
        )
        assert "S.activeStreamId !== streamId" in CANCEL_STREAM_SRC, (
            "cancelStream() should keep owned active SSE open but close stale streams "
            "where ownership changed"
        )

    def test_owner_guard_before_clearing_active_state(self):
        """The fix must NOT clear S.activeStreamId when the active stream
        has rotated to a new turn between entry and the fetch resolving."""
        # The owner check should reference the captured streamId and the
        # current S.activeStreamId.
        assert "S.activeStreamId" in CANCEL_STREAM_SRC, (
            "owner guard must reference S.activeStreamId"
        )
        # The captured streamId must be compared against the live value
        # in a conditional.
        assert re.search(
            r"S\.activeStreamId\s*===?\s*streamId|S\.activeStreamId\s*!==?\s*streamId",
            CANCEL_STREAM_SRC,
        ), (
            "cancelStream() must compare the captured streamId against the "
            "current S.activeStreamId to detect owner rotation"
        )

    def test_surfaces_cancelled_false_to_user(self):
        """When the backend reports ``cancelled:false``, the fix should
        show a lightweight toast (the backend may have already finalized
        the turn, or a newer turn may hold the session lock)."""
        assert "cancelled" in CANCEL_STREAM_SRC and "false" in CANCEL_STREAM_SRC, (
            "cancelStream() must check respBody.cancelled === false and surface "
            "a toast (e.g. 'Stream is no longer active')"
        )
        assert "showToast" in CANCEL_STREAM_SRC, (
            "cancelStream() must use showToast() for the cancelled:false signal"
        )

    def test_does_not_throw_on_network_error(self):
        """The fetch call must still be inside a try/catch so a network
        failure does not propagate out of cancelStream()."""
        # The fix keeps the existing try/catch around fetch.
        assert "catch" in CANCEL_STREAM_SRC, (
            "cancelStream() must keep the try/catch around fetch to swallow "
            "network errors without tearing down the active owner path"
        )


# ── Runtime tests (node subprocess) ───────────────────────────────────────

# Node script template. Uses `__CANCEL_STREAM_SRC__` as the substitution
# token (replaced with the verbatim cancelStream() body via str.replace).
# Single braces are kept literal — no .format() escaping needed.
_NODE_SCRIPT_TEMPLATE = r'''
// Mocks + capture
const M = {
  closeCalls: [],
  busyCalls: [],
  composerCalls: [],
  statusCalls: [],
  toastCalls: [],
  fetchCalls: [],
};
function reset() {
  M.closeCalls.length = 0;
  M.busyCalls.length = 0;
  M.composerCalls.length = 0;
  M.statusCalls.length = 0;
  M.toastCalls.length = 0;
  M.fetchCalls.length = 0;
}

// Globals the real cancelStream() reaches for.
globalThis.S = { activeStreamId: 'stream-1', session: { session_id: 'sid-1' } };
globalThis.setBusy = (v) => M.busyCalls.push(v);
globalThis.setComposerStatus = (v) => M.composerCalls.push(v);
globalThis.setStatus = (v) => M.statusCalls.push(v);
globalThis.closeLiveStream = (...a) => M.closeCalls.push(a);
globalThis.showToast = (msg, t) => M.toastCalls.push({ msg: String(msg), t: Number(t) || 0 });
let _fetchResponse = null;
let _fetchThrows = false;
globalThis.fetch = (url, opts) => {
  M.fetchCalls.push({ url: String(url), opts });
  if (_fetchThrows) return Promise.reject(new Error('simulated network error'));
  return Promise.resolve(_fetchResponse);
};

// Stub browser globals the unfixed function may touch in its fetch URL.
globalThis.document = { baseURI: 'http://localhost:8787/' };
globalThis.location = { href: 'http://localhost:8787/' };

// The function under test, extracted from boot.js verbatim.
__CANCEL_STREAM_SRC__

async function runAll() {
  const out = {};

  // T1 — no active stream: no-op.
  reset();
  globalThis.S = { activeStreamId: null, session: { session_id: 'sid-1' } };
  _fetchResponse = null;
  _fetchThrows = false;
  await cancelStream();
  out.t1_no_active_stream = {
    finalActiveStreamId: globalThis.S.activeStreamId,
    fetchCalls: M.fetchCalls.length,
    closeCalls: [...M.closeCalls],
    busyCalls: [...M.busyCalls],
    composerCalls: [...M.composerCalls],
    toastCalls: [...M.toastCalls],
  };

  // T2 — happy path: active stream, cancelled:true.
  reset();
  globalThis.S = { activeStreamId: 'stream-1', session: { session_id: 'sid-1' } };
  _fetchResponse = {
    ok: true,
    json: () => Promise.resolve({ ok: true, cancelled: true, stream_id: 'stream-1' }),
  };
  _fetchThrows = false;
  await cancelStream();
  out.t2_happy_path = {
    finalActiveStreamId: globalThis.S.activeStreamId,
    fetchCalls: M.fetchCalls.length,
    closeCalls: [...M.closeCalls],
    busyCalls: [...M.busyCalls],
    composerCalls: [...M.composerCalls],
    toastCalls: [...M.toastCalls],
  };

  // T3 — cancelled:false: toast surfaced, local state cleared because the
  // backend says there is no active stream left to settle.
  reset();
  globalThis.S = { activeStreamId: 'stream-1', session: { session_id: 'sid-1' } };
  _fetchResponse = {
    ok: true,
    json: () => Promise.resolve({ ok: true, cancelled: false, stream_id: 'stream-1' }),
  };
  _fetchThrows = false;
  await cancelStream();
  out.t3_cancelled_false = {
    finalActiveStreamId: globalThis.S.activeStreamId,
    fetchCalls: M.fetchCalls.length,
    closeCalls: [...M.closeCalls],
    busyCalls: [...M.busyCalls],
    composerCalls: [...M.composerCalls],
    toastCalls: [...M.toastCalls],
  };

  // T4 — network error: no throw, active owner kept because we do not know
  // whether the cancel landed.
  reset();
  globalThis.S = { activeStreamId: 'stream-1', session: { session_id: 'sid-1' } };
  _fetchResponse = null;
  _fetchThrows = true;
  let threw = null;
  try { await cancelStream(); } catch (e) { threw = String(e); }
  out.t4_network_error = {
    threw,
    finalActiveStreamId: globalThis.S.activeStreamId,
    fetchCalls: M.fetchCalls.length,
    closeCalls: [...M.closeCalls],
    busyCalls: [...M.busyCalls],
    composerCalls: [...M.composerCalls],
    toastCalls: [...M.toastCalls],
  };

  // T5 — owner guard: S.activeStreamId rotates to a new turn during fetch.
  // The fix must close the OLD SSE for sid-1/stream-1, must NOT clear
  // the NEW S.activeStreamId, and must NOT call setBusy(false) on top of
  // the new turn.
  reset();
  globalThis.S = { activeStreamId: 'stream-1', session: { session_id: 'sid-1' } };
  _fetchResponse = {
    ok: true,
    json: () => {
      // Simulate a new turn starting while the cancel request was in flight.
      globalThis.S.activeStreamId = 'stream-2';
      return Promise.resolve({ ok: true, cancelled: true, stream_id: 'stream-1' });
    },
  };
  _fetchThrows = false;
  await cancelStream();
  out.t5_owner_guard = {
    finalActiveStreamId: globalThis.S.activeStreamId,
    fetchCalls: M.fetchCalls.length,
    closeCalls: [...M.closeCalls],
    busyCalls: [...M.busyCalls],
    composerCalls: [...M.composerCalls],
    toastCalls: [...M.toastCalls],
  };

  return out;
}

runAll()
  .then((r) => console.log(JSON.stringify(r)))
  .catch((e) => {
    console.error('NODE_ERROR:', e && e.stack || e);
    process.exit(1);
  });
'''


def _run_cancel_stream_scenarios() -> dict:
    """Run all cancelStream() scenarios in a single node subprocess and
    return the JSON result dict."""
    script = _NODE_SCRIPT_TEMPLATE.replace(
        "__CANCEL_STREAM_SRC__", CANCEL_STREAM_SRC
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=str(REPO),
        text=True,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"node subprocess failed (exit {completed.returncode}):\n"
            f"--- stdout ---\n{completed.stdout}\n"
            f"--- stderr ---\n{completed.stderr}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"node subprocess returned non-JSON output:\n"
            f"--- stdout ---\n{completed.stdout}\n"
            f"--- stderr ---\n{completed.stderr}"
        ) from e


@pytest.fixture(scope="module")
def runtime_results() -> dict:
    """Run the node script once per test module so all five scenarios
    share one subprocess invocation."""
    return _run_cancel_stream_scenarios()


class TestCancelStreamOwnerGuardRuntime:
    """End-to-end runtime assertions. ``runtime_results`` is a module-
    scoped fixture, so the node script runs once and these tests assert
    on its results. This keeps the test fast while still exercising the
    real function with mocked globals."""

    def test_t1_no_active_stream_is_noop(self, runtime_results):
        r = runtime_results["t1_no_active_stream"]
        assert r["fetchCalls"] == 0, (
            f"cancelStream() with no active stream should not call fetch, "
            f"got {r['fetchCalls']} calls"
        )
        assert r["closeCalls"] == [], (
            f"cancelStream() with no active stream should not call closeLiveStream, "
            f"got {r['closeCalls']}"
        )
        assert r["busyCalls"] == [], (
            f"cancelStream() with no active stream should not call setBusy, "
            f"got {r['busyCalls']}"
        )
        assert r["composerCalls"] == [], (
            f"cancelStream() with no active stream should not call setComposerStatus, "
            f"got {r['composerCalls']}"
        )

    def test_t2_happy_path_keeps_owner_for_sse_cancel_settle(self, runtime_results):
        r = runtime_results["t2_happy_path"]
        assert r["fetchCalls"] == 1, (
            f"cancelStream() with active stream should call fetch exactly once, "
            f"got {r['fetchCalls']}"
        )
        # Active owned stop keeps SSE open so backend cancellation terminal event
        # can drive settle/render directly.
        assert r["closeCalls"] == [], (
            f"cancelStream() must keep owned active SSE open for settle on happy path, "
            f"got {r['closeCalls']}"
        )
        # Keep local owner state until the backend cancel SSE event settles.
        assert r["finalActiveStreamId"] == "stream-1", (
            f"cancelStream() must keep S.activeStreamId for SSE cancel settle, "
            f"got {r['finalActiveStreamId']!r}"
        )
        assert r["busyCalls"] == [], (
            f"cancelStream() must not call setBusy(false) before SSE cancel settle, "
            f"got {r['busyCalls']}"
        )
        assert r["composerCalls"] == [], (
            f"cancelStream() must not reset composer status before SSE cancel settle, "
            f"got {r['composerCalls']}"
        )
        # No toast on the happy path.
        assert r["toastCalls"] == [], (
            f"cancelStream() should not show a toast on the happy path, "
            f"got {r['toastCalls']}"
        )

    def test_t3_cancelled_false_surfaces_toast(self, runtime_results):
        r = runtime_results["t3_cancelled_false"]
        assert r["fetchCalls"] == 1, (
            f"cancelStream() must call fetch even when the backend will "
            f"return cancelled:false, got {r['fetchCalls']}"
        )
        # Local state still cleared (this is the turn we wanted to cancel).
        assert r["finalActiveStreamId"] is None, (
            f"cancelStream() must clear S.activeStreamId even on cancelled:false, "
            f"got {r['finalActiveStreamId']!r}"
        )
        assert r["closeCalls"] == [], (
            f"cancelStream() must keep owned SSE open on cancelled:false, "
            f"got {r['closeCalls']}"
        )
        assert r["busyCalls"] == [False], (
            f"cancelStream() must still call setBusy(false) on cancelled:false, "
            f"got {r['busyCalls']}"
        )
        # Toast shown.
        assert len(r["toastCalls"]) == 1, (
            f"cancelStream() must show a toast on cancelled:false, "
            f"got {r['toastCalls']}"
        )
        assert "no longer active" in r["toastCalls"][0]["msg"].lower() or \
               "already" in r["toastCalls"][0]["msg"].lower(), (
            f"toast message should signal the stream is no longer active, "
            f"got {r['toastCalls'][0]['msg']!r}"
        )

    def test_t4_network_error_does_not_throw(self, runtime_results):
        r = runtime_results["t4_network_error"]
        assert r["threw"] is None, (
            f"cancelStream() must not propagate fetch errors, got: {r['threw']!r}"
        )
        assert r["fetchCalls"] == 1, (
            f"cancelStream() must attempt the fetch, got {r['fetchCalls']}"
        )
        assert r["finalActiveStreamId"] == "stream-1", (
            f"cancelStream() must keep S.activeStreamId on network error, "
            f"got {r['finalActiveStreamId']!r}"
        )
        assert r["closeCalls"] == [], (
            f"cancelStream() must keep owned SSE open on network error, "
            f"got {r['closeCalls']}"
        )
        assert r["busyCalls"] == [], (
            f"cancelStream() must not call setBusy(false) on network error, "
            f"got {r['busyCalls']}"
        )
        # No toast for network error (we don't know if the cancel landed).
        assert r["toastCalls"] == [], (
            f"cancelStream() should not show a toast on network error, "
            f"got {r['toastCalls']}"
        )

    def test_t5_owner_guard_preserves_new_turn(self, runtime_results):
        r = runtime_results["t5_owner_guard"]
        # The fetch still happened for the OLD stream.
        assert r["fetchCalls"] == 1, (
            f"cancelStream() must still issue the cancel request even if a "
            f"new turn has started, got {r['fetchCalls']}"
        )
        # closeLiveStream was called for the OLD (sid, streamId).
        assert r["closeCalls"] == [["sid-1", "stream-1"]], (
            f"cancelStream() must call closeLiveStream(sid-1, 'stream-1') for "
            f"the OLD stream even when a new turn has started, got {r['closeCalls']}"
        )
        # The new turn's S.activeStreamId was NOT cleared.
        assert r["finalActiveStreamId"] == "stream-2", (
            f"cancelStream() owner guard must NOT clear the new turn's "
            f"S.activeStreamId, got {r['finalActiveStreamId']!r}"
        )
        # setBusy(false) was NOT called on top of the new turn.
        assert r["busyCalls"] == [], (
            f"cancelStream() owner guard must NOT call setBusy(false) on the "
            f"new turn, got {r['busyCalls']}"
        )
        # Composer status was NOT reset on top of the new turn.
        assert r["composerCalls"] == [], (
            f"cancelStream() owner guard must NOT reset the new turn's "
            f"composer status, got {r['composerCalls']}"
        )
        # No toast for owner-guard path (we don't want to alarm the user
        # about a turn they didn't ask to cancel).
        assert r["toastCalls"] == [], (
            f"cancelStream() owner guard should not show a toast for the new "
            f"turn, got {r['toastCalls']}"
        )

    def test_owner_guard_surfaces_issue_reference(self, runtime_results):
        """Sanity check: active owner paths preserve the SSE settle contract,
        while no-active-stream and stale-owner paths still clean up locally."""
        for key in ("t2_happy_path", "t4_network_error"):
            assert runtime_results[key]["finalActiveStreamId"] == "stream-1", (
                f"{key} should preserve S.activeStreamId for active owner path"
            )
            assert runtime_results[key]["busyCalls"] == [], (
                f"{key} should not call setBusy(false) before terminal settle"
            )
        assert runtime_results["t3_cancelled_false"]["finalActiveStreamId"] is None
        assert runtime_results["t3_cancelled_false"]["busyCalls"] == [False]
        # T5 is the owner-rotation case and must preserve the new turn.
        assert runtime_results["t5_owner_guard"]["finalActiveStreamId"] == "stream-2"
        assert runtime_results["t5_owner_guard"]["busyCalls"] == []
        # Only the stale-owner path closes the old SSE; active owned paths
        # keep it open so the backend terminal event can settle/render.
        for key in ("t2_happy_path", "t3_cancelled_false", "t4_network_error", "t5_owner_guard"):
            if key == "t5_owner_guard":
                assert runtime_results[key]["closeCalls"] == [["sid-1", "stream-1"]], (
                    f"{key} must close stale old SSE for ('sid-1', 'stream-1'), "
                    f"got {runtime_results[key]['closeCalls']}"
                )
                continue
            assert runtime_results[key]["closeCalls"] == [], (
                f"{key} must keep owned SSE open for terminal settlement path, "
                f"got {runtime_results[key]['closeCalls']}"
            )
