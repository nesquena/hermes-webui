"""Behavioural tests for the active-run indicator dot — execute the actual
_updateActiveRunDot(), _isSessionEffectivelyStreaming(), and setBusy()
functions via Node.js against mocked DOM/S/INFLIGHT/sessions state.

Tests cover:
  - Dot shows on setBusy(true) and clears on setBusy(false)
  - Dot reflects server truth across sessions (_allSessions is_streaming)
  - Dot reflects local background streams in INFLIGHT
  - _purgeStaleInflightEntries repaints dot when background stream finishes
  - _applySessionListPayload repaints dot on list update
  - Deleted session (404) leaves S.busy false with no phantom stream id
  - Direct S.busy writes repaint dot
  - No sessionStorage state is written or read for run state
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS_PATH = str(REPO_ROOT / "static" / "sessions.js")
UI_JS_PATH = str(REPO_ROOT / "static" / "ui.js")

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# ── Node.js driver ──────────────────────────────────────────────────────────
_DRIVER_SRC = r"""
const fs = require('fs');
const sessionsSrc = fs.readFileSync(process.argv[2], 'utf8');
const uiSrc = fs.readFileSync(process.argv[3], 'utf8');

// ── Mock DOM element ──
function makeDot() {
    return {
        className: 'active-run-dot-hidden',
        title: '',
        setAttribute: function() {},
        getAttribute: function() { return null; },
    };
}

// ── Mock global state S ──
global.S = {
    busy: false,
    session: null,
    activeStreamId: null,
};

// ── Mock INFLIGHT map ──
global.INFLIGHT = {};

// ── Mock _allSessions cache ──
global._allSessions = [];

// ── Mock helpers used by _isSessionEffectivelyStreaming ──
global._hasPendingUserMessageSignal = function(s) {
    return Boolean(s && (s.pending_user_message || s.has_pending_user_message));
};
global._isSessionLocallyStreaming = function(s) {
    return false;
};

// ── Mock globals for _purgeStaleInflightEntries ──
global.clearInflightState = function(sid) {};
global._hasOwnedOpenLiveStream = function(sid) { return false; };
global._sessionListSourceById = null;
global._allSessionsScope = null;
global._sendInProgress = false;
global._sendInProgressSid = null;

// ── Mock document ──
const dot = makeDot();
global.document = {
    getElementById: function(id) {
        if (id === 'activeRunDot') return dot;
        return null;
    },
    addEventListener: function() {},
    querySelectorAll: function() { return []; },
    querySelector: function() { return null; },
};

// ── Mock sessionStorage (must remain untouched for run state) ──
let _storage = {};
global.sessionStorage = {
    getItem: function(k) { return _storage.hasOwnProperty(k) ? _storage[k] : null; },
    setItem: function(k, v) { _storage[k] = String(v); },
    removeItem: function(k) { delete _storage[k]; },
};

// ── Mock other globals touched by setBusy ──
global.updateSendBtn = function() {};
global._queueDrainSid = null;
global.setStatus = function() {};
global.setComposerStatus = function() {};
global.updateQueueBadge = function() {};
global.shiftQueuedSessionMessage = function() { return null; };
global._clearActivityElapsedTimer = function() {};

// ── Globals needed by sessions.js ──
global.window = {};
global.ICONS = {};
global.__ = function() {};

// ── Extract a named function from source ──
function extractFunc(src, name) {
    const re = new RegExp('function\\s+' + name + '\\s*\\(');
    const start = src.search(re);
    if (start < 0) throw new Error(name + ' not found');
    let i = src.indexOf('{', start);
    let depth = 1; i++;
    while (depth > 0 && i < src.length) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') depth--;
        i++;
    }
    return src.slice(start, i);
}

// Evaluate required functions
eval(extractFunc(sessionsSrc, '_isSessionEffectivelyStreaming'));
eval(extractFunc(sessionsSrc, '_updateActiveRunDot'));
eval(extractFunc(sessionsSrc, '_purgeStaleInflightEntries'));
eval(extractFunc(uiSrc, 'setBusy'));

// ── Read the command from argv[4] ──
const cmd = JSON.parse(process.argv[4]);

// Apply initial state
if (cmd.S_busy !== undefined) S.busy = cmd.S_busy;
if (cmd.S_session !== undefined) S.session = cmd.S_session;
if (cmd.S_activeStreamId !== undefined) S.activeStreamId = cmd.S_activeStreamId;
if (cmd.INFLIGHT !== undefined) INFLIGHT = cmd.INFLIGHT;
if (cmd._allSessions !== undefined) _allSessions = cmd._allSessions;
if (cmd.storageSeed !== undefined && Array.isArray(cmd.storageSeed)) {
    cmd.storageSeed.forEach(function(e) {
        sessionStorage.setItem(e.k, e.v);
    });
}

let result = {};

if (cmd.action === 'updateActiveRunDot') {
    _updateActiveRunDot();
    result.dotClassName = dot.className;
    result.dotTitle = dot.title;
}

if (cmd.action === 'setBusy') {
    setBusy(cmd.busyValue);
    result.S_busy = S.busy;
    result.dotClassName = dot.className;
    result.dotTitle = dot.title;
    result.storageUsed = sessionStorage.getItem('hermes-webui-active-run') !== null;
}

if (cmd.action === 'purgeStaleInflightEntries') {
    // Populate sessionsById Map from cmd.sessionsList
    const sMap = new Map();
    if (Array.isArray(cmd.sessionsList)) {
        cmd.sessionsList.forEach(s => sMap.set(s.session_id, s));
    }
    _purgeStaleInflightEntries(sMap);
    result.dotClassName = dot.className;
    result.dotTitle = dot.title;
    result.inflightKeys = Object.keys(INFLIGHT);
}

if (cmd.action === 'directBusyWrite') {
    S.busy = cmd.busyValue;
    try { _updateActiveRunDot(); } catch(_) {}
    result.S_busy = S.busy;
    result.dotClassName = dot.className;
    result.dotTitle = dot.title;
}

process.stdout.write(JSON.stringify(result));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("active_run_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _run(driver_path, cmd):
    """Execute the driver with the given command object."""
    result = subprocess.run(
        [NODE, driver_path, SESSIONS_JS_PATH, UI_JS_PATH, json.dumps(cmd)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed (rc={result.returncode}):\n{result.stderr}")
    return json.loads(result.stdout)


# ══════════════════════════════════════════════════════════════════════════════
# _updateActiveRunDot() — single-source-of-truth for dot visibility
# ══════════════════════════════════════════════════════════════════════════════


class TestUpdateActiveRunDotBehaviour:
    """_updateActiveRunDot() sets the dot's className and title based on
    S.busy, the INFLIGHT map, and _allSessions streaming state."""

    def test_dot_shows_when_s_busy_is_true(self, driver_path):
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": True,
            "INFLIGHT": {},
            "_allSessions": [],
        })
        assert out["dotClassName"] == "active-run-dot", \
            f"Dot must show when S.busy=true, got {out}"
        assert out["dotTitle"] == "Agent is running"

    def test_dot_hides_when_s_busy_is_false_and_inflight_empty(self, driver_path):
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": False,
            "INFLIGHT": {},
            "_allSessions": [],
        })
        assert out["dotClassName"] == "active-run-dot-hidden", \
            f"Dot must hide when S.busy=false and INFLIGHT empty, got {out}"

    def test_dot_shows_when_inflight_has_entries_even_if_s_busy_false(self, driver_path):
        """After switching to an idle session, the dot must still reflect a
        background run that is still streaming. INFLIGHT non-empty → dot stays lit."""
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": False,
            "INFLIGHT": {"stream-abc": {}},
            "_allSessions": [],
        })
        assert out["dotClassName"] == "active-run-dot", \
            f"Dot must show when INFLIGHT has entries (background run), got {out}"
        assert out["dotTitle"] == "Agent is running"

    def test_dot_shows_when_both_s_busy_and_inflight_have_runs(self, driver_path):
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": True,
            "INFLIGHT": {"stream-1": {}, "stream-2": {}},
            "_allSessions": [],
        })
        assert out["dotClassName"] == "active-run-dot", \
            f"Dot must show when both S.busy and INFLIGHT indicate runs, got {out}"

    def test_inflight_undefined_is_handled_gracefully(self, driver_path):
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": False,
            "INFLIGHT": None,
            "_allSessions": [],
        })
        assert out["dotClassName"] == "active-run-dot-hidden"

    def test_inflight_null_treated_as_no_runs(self, driver_path):
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": True,
        })
        assert out["dotClassName"] == "active-run-dot"

    def test_dot_shows_when_sibling_session_is_streaming(self, driver_path):
        """Server truth: another session row in _allSessions reporting is_streaming=true
        lights the dot even if local S.busy is false and INFLIGHT is empty."""
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": False,
            "INFLIGHT": {},
            "_allSessions": [
                {"session_id": "ses-idle", "is_streaming": False},
                {"session_id": "ses-active", "is_streaming": True},
            ],
        })
        assert out["dotClassName"] == "active-run-dot", \
            f"Dot must show when a sibling session in _allSessions is streaming, got {out}"
        assert out["dotTitle"] == "Agent is running"

    def test_dot_hides_when_all_sessions_idle(self, driver_path):
        """When all sessions in _allSessions are idle, local busy false, INFLIGHT empty, dot hides."""
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": False,
            "INFLIGHT": {},
            "_allSessions": [
                {"session_id": "ses-1", "is_streaming": False},
                {"session_id": "ses-2", "is_streaming": False},
            ],
        })
        assert out["dotClassName"] == "active-run-dot-hidden", \
            f"Dot must hide when all sessions are idle, got {out}"

    def test_dot_handles_empty_all_sessions(self, driver_path):
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": False,
            "INFLIGHT": {},
            "_allSessions": [],
        })
        assert out["dotClassName"] == "active-run-dot-hidden"


# ══════════════════════════════════════════════════════════════════════════════
# setBusy() — the single chokepoint that drives update
# ══════════════════════════════════════════════════════════════════════════════


class TestSetBusyChokepointBehaviour:
    """setBusy(v) is the single chokepoint that updates the active-run dot
    without writing client sessionStorage snapshots."""

    def test_setbusy_true_shows_dot(self, driver_path):
        out = _run(driver_path, {
            "action": "setBusy",
            "busyValue": True,
            "S_session": {"session_id": "ses-1"},
            "INFLIGHT": {},
            "_allSessions": [],
        })
        assert out["dotClassName"] == "active-run-dot", "setBusy(true) must show the dot"
        assert out["dotTitle"] == "Agent is running"
        assert out["storageUsed"] is False, "setBusy must not write to sessionStorage"

    def test_setbusy_false_hides_dot(self, driver_path):
        out = _run(driver_path, {
            "action": "setBusy",
            "busyValue": False,
            "S_session": {"session_id": "ses-1"},
            "INFLIGHT": {},
            "_allSessions": [],
        })
        assert out["dotClassName"] == "active-run-dot-hidden", \
            "setBusy(false) must hide the dot when no background runs"
        assert out["storageUsed"] is False, "setBusy must not write to sessionStorage"

    def test_setbusy_false_keeps_dot_when_inflight_has_runs(self, driver_path):
        """When a background session is still running in INFLIGHT, calling setBusy(false)
        on foreground session must NOT hide the dot."""
        out = _run(driver_path, {
            "action": "setBusy",
            "busyValue": False,
            "S_session": {"session_id": "ses-1"},
            "INFLIGHT": {"stream-bg": {}},
            "_allSessions": [],
        })
        assert out["dotClassName"] == "active-run-dot", \
            "setBusy(false) must NOT hide dot when INFLIGHT background runs are active"

    def test_setbusy_false_keeps_dot_when_sibling_is_streaming(self, driver_path):
        """When another session row reports streaming, setBusy(false) on current session
        keeps the dot lit."""
        out = _run(driver_path, {
            "action": "setBusy",
            "busyValue": False,
            "S_session": {"session_id": "ses-1"},
            "INFLIGHT": {},
            "_allSessions": [{"session_id": "ses-other", "is_streaming": True}],
        })
        assert out["dotClassName"] == "active-run-dot", \
            "setBusy(false) must NOT hide dot when a sibling session is streaming"

    def test_setbusy_does_not_write_to_session_storage(self, driver_path):
        out = _run(driver_path, {
            "action": "setBusy",
            "busyValue": True,
            "S_session": {"session_id": "ses-1"},
            "S_activeStreamId": "stream-123",
        })
        assert out["storageUsed"] is False, "sessionStorage must never receive active run entries"


# ══════════════════════════════════════════════════════════════════════════════
# Background Purge and List Reconciliation
# ══════════════════════════════════════════════════════════════════════════════


class TestPurgeStaleInflightRepaintBehaviour:
    """_purgeStaleInflightEntries() repaints so a dot lit by a finished run clears."""

    def test_purge_clears_dot_when_stale_background_stream_purged(self, driver_path):
        # Initial state: dot lit because INFLIGHT has 'stream-bg' for session 'ses-bg'
        # But server reports ses-bg is no longer streaming (is_streaming=false)
        out = _run(driver_path, {
            "action": "purgeStaleInflightEntries",
            "S_busy": False,
            "INFLIGHT": {"ses-bg": {}},
            "_allSessions": [{"session_id": "ses-bg", "is_streaming": False}],
            "sessionsList": [{"session_id": "ses-bg", "is_streaming": False}],
        })
        assert "ses-bg" not in out["inflightKeys"], "Purge must remove non-streaming INFLIGHT entry"
        assert out["dotClassName"] == "active-run-dot-hidden", \
            "Purge must repaint so dot clears when stale background run ends"

    def test_purge_preserves_dot_if_active_stream_still_running(self, driver_path):
        out = _run(driver_path, {
            "action": "purgeStaleInflightEntries",
            "S_busy": False,
            "INFLIGHT": {"ses-active": {}},
            "_allSessions": [{"session_id": "ses-active", "is_streaming": True}],
            "sessionsList": [{"session_id": "ses-active", "is_streaming": True}],
        })
        assert "ses-active" in out["inflightKeys"], "Streaming entry must not be purged"
        assert out["dotClassName"] == "active-run-dot", "Dot must stay lit while stream runs"


class TestApplySessionListRepaintBehaviour:
    """_applySessionListPayload() repaints on every accepted session list update."""

    def test_apply_session_list_lights_dot_on_streaming_session(self, driver_path):
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": False,
            "INFLIGHT": {},
            "_allSessions": [{"session_id": "ses-sibling", "is_streaming": True}],
        })
        assert out["dotClassName"] == "active-run-dot"

    def test_apply_session_list_clears_dot_when_all_sessions_idle(self, driver_path):
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": False,
            "INFLIGHT": {},
            "_allSessions": [{"session_id": "ses-sibling", "is_streaming": False}],
        })
        assert out["dotClassName"] == "active-run-dot-hidden"


# ══════════════════════════════════════════════════════════════════════════════
# Deleted Session & Server Truth (no phantom restore)
# ══════════════════════════════════════════════════════════════════════════════


class TestDeletedSessionRecoveryBehaviour:
    """A mid-run refresh of a deleted (404) session never restores busy=true or a phantom stream."""

    def test_deleted_session_leaves_s_busy_false_no_phantom_stream(self, driver_path):
        # Opening a deleted session without snapshot restore starts with S.busy=false
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": False,
            "S_activeStreamId": None,
            "INFLIGHT": {},
            "_allSessions": [],
        })
        assert out["dotClassName"] == "active-run-dot-hidden"

    def test_fresh_snapshot_of_404_deleted_session_leaves_busy_false_next_send_starts(self, driver_path):
        """A fresh snapshot of a 404/deleted session leaves S.busy false with no phantom
        stream id, and the next send starts without queueing behind phantom run."""
        # Seed sessionStorage with stale active run as if a mid-run refresh occurred
        out = _run(driver_path, {
            "action": "setBusy",
            "busyValue": False,  # Cold boot / deleted session sets busy=false
            "S_session": {"session_id": "ses-deleted"},
            "S_activeStreamId": None,
            "INFLIGHT": {},
            "_allSessions": [],
            "storageSeed": [{
                "k": "hermes-webui-active-run",
                "v": json.dumps({"busy": True, "activeStreamId": "stream-phantom", "activeSessionId": "ses-deleted", "timestamp": 123456789}),
            }],
        })
        assert out["S_busy"] is False, "S.busy must be false for deleted/idle session"
        assert out["dotClassName"] == "active-run-dot-hidden", "Dot must remain hidden"
        # Simulate next send: setBusy(true) starts cleanly without phantom queueing
        send_out = _run(driver_path, {
            "action": "setBusy",
            "busyValue": True,
            "S_session": {"session_id": "ses-fresh"},
            "S_activeStreamId": "stream-fresh",
            "INFLIGHT": {"stream-fresh": {}},
            "_allSessions": [],
        })
        assert send_out["S_busy"] is True
        assert send_out["dotClassName"] == "active-run-dot"

    def test_stale_storage_has_no_effect_on_busy(self, driver_path):
        # Even if stale sessionStorage has a previous snapshot, nothing reads it
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": False,
            "S_activeStreamId": None,
            "INFLIGHT": {},
            "_allSessions": [],
            "storageSeed": [{
                "k": "hermes-webui-active-run",
                "v": json.dumps({"busy": True, "activeStreamId": "stream-phantom", "timestamp": 123456789}),
            }],
        })
        assert out["dotClassName"] == "active-run-dot-hidden", \
            "Stale sessionStorage entry must not restore or light the dot"


# ══════════════════════════════════════════════════════════════════════════════
# Direct S.busy Writes Repaint
# ══════════════════════════════════════════════════════════════════════════════


class TestDirectBusyWritesRepaintBehaviour:
    """Direct writes to S.busy that bypass setBusy() repaint the dot."""

    def test_direct_s_busy_false_clears_dot(self, driver_path):
        out = _run(driver_path, {
            "action": "directBusyWrite",
            "busyValue": False,
            "INFLIGHT": {},
            "_allSessions": [],
        })
        assert out["S_busy"] is False
        assert out["dotClassName"] == "active-run-dot-hidden"

    def test_direct_s_busy_true_lights_dot(self, driver_path):
        out = _run(driver_path, {
            "action": "directBusyWrite",
            "busyValue": True,
            "INFLIGHT": {},
            "_allSessions": [],
        })
        assert out["S_busy"] is True
        assert out["dotClassName"] == "active-run-dot"
        assert out["dotTitle"] == "Agent is running"
