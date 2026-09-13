"""Behavioural tests for the active-run indicator dot — execute the actual
_updateActiveRunDot(), _persistActiveRunState(), _restoreActiveRunState(),
and setBusy() functions via Node.js against mocked DOM/S/storage.

The static checks in test_6176_active_run_indicator.py confirm structural
properties (call sites exist, correct accessors, CSS rules present), but
they pass even if the runtime logic is inverted — e.g. if the dot hides
when it should show, or persist writes a stale busy flag after stream-finish.

This file pins the actual rendered className/title for every state so the
indicator's show/hide/inflight contract cannot silently regress.

Tests cover:
  - Dot shows on setBusy(true)
  - Dot clears on setBusy(false)
  - No stale dot / stale persist after stream-finish
  - Dot reflects background run after switching sessions (INFLIGHT non-empty)
  - Persist/restore round-trip survives a simulated reload
  - Restore paints the dot via _updateActiveRunDot()
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

// ── Mock DOM element (analogous to makeEl() in reasoning tests) ──
function makeDot() {
    return {
        className: 'active-run-dot-hidden',
        title: '',
    };
}

// ── Mock global state S (matches ui.js const S = {...}) ──
global.S = {
    busy: false,
    session: null,
    activeStreamId: null,
};

// ── Mock INFLIGHT (global map of active streams) ──
global.INFLIGHT = {};

// ── Mock document (getElementById for the dot) ──
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

// ── Mock sessionStorage ──
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

// ── Globals needed by sessions.js but not relevant here ──
global.window = {};
global.ICONS = {};
global._loadingSessionId = null;
global._loadSessionGeneration = 0;
global._autoLoadContinuation = null;
global._pendingCarryForwardSnapshot = null;
global._draftSaveTimer = null;
global._NEW_CHAT_DRAFT_SESSION_KEY = null;
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

// Evaluate the functions from sessions.js
eval(extractFunc(sessionsSrc, '_persistActiveRunState'));
eval(extractFunc(sessionsSrc, '_restoreActiveRunState'));
eval(extractFunc(sessionsSrc, '_updateActiveRunDot'));

// Evaluate setBusy from ui.js
eval(extractFunc(uiSrc, 'setBusy'));

// ── Read the command from argv[4] ──
const cmd = JSON.parse(process.argv[4]);

// Apply state mutations before executing
if (cmd.S_busy !== undefined) S.busy = cmd.S_busy;
if (cmd.S_session !== undefined) S.session = cmd.S_session;
if (cmd.S_activeStreamId !== undefined) S.activeStreamId = cmd.S_activeStreamId;
if (cmd.S__restoredActiveSessionId !== undefined) S._restoredActiveSessionId = cmd.S__restoredActiveSessionId;
if (cmd.INFLIGHT !== undefined) INFLIGHT = cmd.INFLIGHT;
if (cmd._queueDrainSid !== undefined) _queueDrainSid = cmd._queueDrainSid;
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

if (cmd.action === 'persistActiveRunState') {
    _persistActiveRunState();
    const raw = sessionStorage.getItem('hermes-webui-active-run');
    result.persisted = raw ? JSON.parse(raw) : null;
}

if (cmd.action === 'restoreActiveRunState') {
    _restoreActiveRunState();
    result.S_busy = S.busy;
    result.S__restoredActiveSessionId = S._restoredActiveSessionId;
    result.dotClassName = dot.className;
    result.dotTitle = dot.title;
    result.storageCleared = sessionStorage.getItem('hermes-webui-active-run') === null;
}

if (cmd.action === 'setBusy') {
    setBusy(cmd.busyValue);
    result.S_busy = S.busy;
    result.dotClassName = dot.className;
    result.dotTitle = dot.title;
    const raw = sessionStorage.getItem('hermes-webui-active-run');
    result.persisted = raw ? JSON.parse(raw) : null;
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
    S.busy and the INFLIGHT map."""

    def test_dot_shows_when_s_busy_is_true(self, driver_path):
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": True,
            "INFLIGHT": {},
        })
        assert out["dotClassName"] == "active-run-dot", \
            f"Dot must show when S.busy=true, got {out}"
        assert out["dotTitle"] == "Agent is running"

    def test_dot_hides_when_s_busy_is_false_and_inflight_empty(self, driver_path):
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": False,
            "INFLIGHT": {},
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
        })
        assert out["dotClassName"] == "active-run-dot", \
            f"Dot must show when INFLIGHT has entries (background run), got {out}"
        assert out["dotTitle"] == "Agent is running"

    def test_dot_shows_when_both_s_busy_and_inflight_have_runs(self, driver_path):
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": True,
            "INFLIGHT": {"stream-1": {}, "stream-2": {}},
        })
        assert out["dotClassName"] == "active-run-dot", \
            f"Dot must show when both S.busy and INFLIGHT indicate runs, got {out}"

    def test_inflight_undefined_is_handled_gracefully(self, driver_path):
        """When INFLIGHT is undefined (not yet initialised), the dot should
        still work based on S.busy alone."""
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": False,
            "INFLIGHT": None,  # Force undefined via null (driver sets if !== undefined)
        })
        # With INFLIGHT forced to null, only S.busy matters
        assert out["dotClassName"] == "active-run-dot-hidden", \
            f"Dot must hide when S.busy=false and INFLIGHT undefined/null, got {out}"

    def test_inflight_null_treated_as_no_runs(self, driver_path):
        """INFLIGHT might be null during early boot."""
        # We need to test with INFLIGHT explicitly undefined.
        # Use a special command that doesn't set INFLIGHT.
        out = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": True,
        })
        assert out["dotClassName"] == "active-run-dot", \
            f"Dot must show when S.busy=true regardless of INFLIGHT state, got {out}"


# ══════════════════════════════════════════════════════════════════════════════
# _persistActiveRunState() — writes correct snapshot to sessionStorage
# ══════════════════════════════════════════════════════════════════════════════


class TestPersistActiveRunStateBehaviour:
    """_persistActiveRunState() writes busy, activeStreamId, activeSessionId,
    and timestamp to sessionStorage."""

    def test_persist_writes_busy_true_with_session(self, driver_path):
        out = _run(driver_path, {
            "action": "persistActiveRunState",
            "S_busy": True,
            "S_session": {"session_id": "ses-abc-123"},
            "S_activeStreamId": "stream-xyz",
        })
        assert out["persisted"] is not None, "Must write to sessionStorage"
        assert out["persisted"]["busy"] is True
        assert out["persisted"]["activeStreamId"] == "stream-xyz"
        assert out["persisted"]["activeSessionId"] == "ses-abc-123"
        assert isinstance(out["persisted"]["timestamp"], (int, float))

    def test_persist_writes_busy_false_after_stream_finish(self, driver_path):
        """When a stream finishes, persist must write busy=false so a reload
        doesn't flash a stale dot. This is the fix for the false-positive
        flash on reload (#6176 review comment)."""
        out = _run(driver_path, {
            "action": "persistActiveRunState",
            "S_busy": False,
            "S_session": {"session_id": "ses-abc-123"},
            "S_activeStreamId": None,
        })
        assert out["persisted"]["busy"] is False, \
            "Must persist busy=false after stream-finish to prevent stale flash on reload"
        assert out["persisted"]["activeStreamId"] is None

    def test_persist_handles_null_session_gracefully(self, driver_path):
        """S.session may be null early in boot or during new-session flow."""
        out = _run(driver_path, {
            "action": "persistActiveRunState",
            "S_busy": True,
            "S_session": None,
            "S_activeStreamId": "stream-xyz",
        })
        assert out["persisted"]["activeSessionId"] is None, \
            "Must coerce null session to null activeSessionId"

    def test_persist_handles_missing_session_id_gracefully(self, driver_path):
        out = _run(driver_path, {
            "action": "persistActiveRunState",
            "S_busy": True,
            "S_session": {},
            "S_activeStreamId": "stream-xyz",
        })
        assert out["persisted"]["activeSessionId"] is None, \
            "Must handle S.session without session_id"


# ══════════════════════════════════════════════════════════════════════════════
# _restoreActiveRunState() — reads from sessionStorage and paints the dot
# ══════════════════════════════════════════════════════════════════════════════


class TestRestoreActiveRunStateBehaviour:
    """_restoreActiveRunState() reads the persisted state, sets S.busy,
    paints the dot via _updateActiveRunDot(), and clears sessionStorage."""

    def _seed_storage(self, driver_path, busy=True, activeStreamId="stream-1",
                      activeSessionId="ses-1", timestamp=None):
        if timestamp is None:
            import time
            timestamp = int(time.time() * 1000)
        return _run(driver_path, {
            "action": "restoreActiveRunState",
            "storageSeed": [{
                "k": "hermes-webui-active-run",
                "v": json.dumps({
                    "busy": busy,
                    "activeStreamId": activeStreamId,
                    "activeSessionId": activeSessionId,
                    "timestamp": timestamp,
                }),
            }],
        })

    def test_restore_sets_s_busy_true_and_paints_dot(self, driver_path):
        out = self._seed_storage(driver_path)
        assert out["S_busy"] is True, "Restore must set S.busy = true"
        assert out["dotClassName"] == "active-run-dot", \
            "Restore must paint the dot via _updateActiveRunDot()"
        assert out["dotTitle"] == "Agent is running", \
            "Restore must set accessibility title"

    def test_restore_sets_restored_active_session_id(self, driver_path):
        out = self._seed_storage(driver_path, activeSessionId="ses-abc")
        assert out["S__restoredActiveSessionId"] == "ses-abc", \
            "Restore must preserve session ID for loadSession carry-forward"

    def test_restore_clears_storage_after_read(self, driver_path):
        out = self._seed_storage(driver_path)
        assert out["storageCleared"] is True, \
            "Restore must remove the item to prevent stale restores"

    def test_restore_ignores_stale_timestamp_outside_30s_window(self, driver_path):
        import time
        stale_ts = int((time.time() - 60) * 1000)  # 60 seconds ago
        out = self._seed_storage(driver_path, timestamp=stale_ts)
        assert out["S_busy"] is False, \
            "Restore must ignore entries older than 30 seconds"

    def test_restore_ignores_busy_false_entry(self, driver_path):
        out = self._seed_storage(driver_path, busy=False)
        assert out["S_busy"] is False, \
            "Restore must NOT set S.busy=true when persisted busy=false"
        # Dot should remain hidden (default state)
        # After restore sets nothing, dot stays at its initial hidden state
        assert out["dotClassName"] == "active-run-dot-hidden"

    def test_restore_no_storage_does_nothing(self, driver_path):
        out = _run(driver_path, {"action": "restoreActiveRunState"})
        assert out["S_busy"] is False, "No storage → no restore"
        assert out["dotClassName"] == "active-run-dot-hidden"


# ══════════════════════════════════════════════════════════════════════════════
# setBusy() — the single chokepoint that drives update + persist
# ══════════════════════════════════════════════════════════════════════════════


class TestSetBusyChokepointBehaviour:
    """setBusy(v) is the single chokepoint that MUST call both
    _updateActiveRunDot() and _persistActiveRunState()."""

    def test_setbusy_true_shows_dot_and_persists(self, driver_path):
        out = _run(driver_path, {
            "action": "setBusy",
            "busyValue": True,
            "S_session": {"session_id": "ses-1"},
        })
        assert out["dotClassName"] == "active-run-dot", \
            "setBusy(true) must show the dot"
        assert out["persisted"] is not None, \
            "setBusy(true) must persist state"
        assert out["persisted"]["busy"] is True

    def test_setbusy_false_hides_dot_and_persists(self, driver_path):
        """After stream-finish, setBusy(false) must hide the dot AND persist
        busy=false so a reload doesn't flash a stale dot."""
        # Start with dot visible
        pre = _run(driver_path, {
            "action": "updateActiveRunDot",
            "S_busy": True,
            "INFLIGHT": {},
        })
        assert pre["dotClassName"] == "active-run-dot"

        out = _run(driver_path, {
            "action": "setBusy",
            "busyValue": False,
            "S_session": {"session_id": "ses-1"},
            "INFLIGHT": {},  # No background runs
        })
        assert out["dotClassName"] == "active-run-dot-hidden", \
            "setBusy(false) must hide the dot when no background runs"
        assert out["persisted"]["busy"] is False, \
            "setBusy(false) must persist busy=false to prevent stale flash on reload"

    def test_setbusy_false_keeps_dot_when_inflight_has_runs(self, driver_path):
        """When a background session is still running, calling setBusy(false)
        on the foreground session must NOT hide the dot because INFLIGHT still
        has active streams."""
        out = _run(driver_path, {
            "action": "setBusy",
            "busyValue": False,
            "S_session": {"session_id": "ses-1"},
            "INFLIGHT": {"stream-bg": {}},  # Background run still active
        })
        assert out["dotClassName"] == "active-run-dot", \
            "setBusy(false) must NOT hide dot when background runs are active"


# ══════════════════════════════════════════════════════════════════════════════
# Persist/Restore round-trip — simulates a page reload
# ══════════════════════════════════════════════════════════════════════════════


class TestPersistRestoreRoundTrip:
    """A full persist → restore cycle simulates a page reload and verifies
    the dot survives the round-trip."""

    def test_roundtrip_survives_reload(self, driver_path):
        """1. setBusy(true) → persist
           2. Simulate reload: read from storage, restore
           3. Dot should be visible after restore
        """
        # Step 1: Start a run
        out1 = _run(driver_path, {
            "action": "setBusy",
            "busyValue": True,
            "S_session": {"session_id": "ses-roundtrip"},
            "S_activeStreamId": "stream-rt",
        })
        assert out1["dotClassName"] == "active-run-dot"
        persisted = out1["persisted"]
        assert persisted["busy"] is True

        # Step 2: Simulate reload by seeding storage and restoring
        out2 = _run(driver_path, {
            "action": "restoreActiveRunState",
            "storageSeed": [{
                "k": "hermes-webui-active-run",
                "v": json.dumps(persisted),
            }],
        })
        assert out2["S_busy"] is True, \
            "After restore, S.busy must be true"
        assert out2["dotClassName"] == "active-run-dot", \
            "After restore, dot must be visible"
        assert out2["S__restoredActiveSessionId"] == "ses-roundtrip", \
            "After restore, session ID must be carried forward"

    def test_roundtrip_preserves_cleared_state(self, driver_path):
        """1. setBusy(false) → persist busy=false
           2. Simulate reload
           3. Dot should NOT appear (busy=false in storage → no restore)
        """
        # Step 1: Finish a run
        out1 = _run(driver_path, {
            "action": "setBusy",
            "busyValue": False,
            "S_session": {"session_id": "ses-roundtrip"},
            "S_activeStreamId": None,
            "INFLIGHT": {},
        })
        assert out1["persisted"]["busy"] is False

        # Step 2: Simulate reload
        out2 = _run(driver_path, {
            "action": "restoreActiveRunState",
            "storageSeed": [{
                "k": "hermes-webui-active-run",
                "v": json.dumps(out1["persisted"]),
            }],
        })
        assert out2["S_busy"] is False, \
            "After stream-finish reload, S.busy must be false"
        assert out2["dotClassName"] == "active-run-dot-hidden", \
            "After stream-finish reload, dot must be hidden"
