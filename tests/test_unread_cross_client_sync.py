"""Regression: unread state must not be clobbered by a second WebUI client.

Two WebUI windows/tabs on the same origin and browser profile share one
``localStorage``, and each caches the unread stores in module state. A stale
client that re-serialises its cache wholesale -- or that drops its cache and
re-reads a value another client's write already destroyed -- breaks two stores:

- ``hermes-session-viewed-counts``: a viewed count means "seen at least up to N
  messages", so it is monotonic per session and additive across clients. It must
  never roll back, and after a concurrent write the client that holds the higher
  count must re-assert it into the store (repair) instead of dropping its cache
  and re-reading whatever the losing write left behind.

- ``hermes-session-completion-unread``: markers are add/remove, so they cannot be
  max-merged. Every clear records a tombstone in
  ``hermes-session-completion-unread-cleared`` (sid -> clearedAt), and the later
  event wins: a marker whose ``completed_at`` does not postdate the clear is
  dropped, while a genuinely later re-mark survives. This is the store behind the
  visible dot, and the reported field symptom was a cleared dot coming back.

These tests drive the real functions extracted from ``static/sessions.js``
against a two-client harness that shares one fake ``localStorage``. The harness
injects the merge/tombstone helpers when the module defines them, so the same
assertions run against the pre-fix code and report the observable failure.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

VIEWED_KEY = "hermes-session-viewed-counts"
UNREAD_KEY = "hermes-session-completion-unread"
CLEARED_KEY = "hermes-session-completion-unread-cleared"


def _extract(name: str) -> str:
    """Extract a top-level `function name(...) { ... }` definition by brace match."""
    marker = f"function {name}("
    start = SESSIONS_JS.index(marker)
    brace = SESSIONS_JS.index("{", start)
    depth = 0
    for i in range(brace, len(SESSIONS_JS)):
        ch = SESSIONS_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return SESSIONS_JS[start:i + 1]
    raise AssertionError(f"could not brace-match {name}")


def _extract_optional(name: str) -> str:
    """Extraction for functions a change may add (empty string when absent).

    The same assertions therefore run before and after the fix: on the pre-fix
    code the harness simply has no merge/tombstone helper to inject.
    """
    return _extract(name) if f"function {name}(" in SESSIONS_JS else ""


def _extract_storage_listener() -> str:
    """The module-level ``window.addEventListener('storage', ...)`` statement."""
    marker = "window.addEventListener('storage'"
    start = SESSIONS_JS.index(marker)
    brace = SESSIONS_JS.index("{", start)
    depth = 0
    for i in range(brace, len(SESSIONS_JS)):
        ch = SESSIONS_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return SESSIONS_JS[start:i + 3]  # include the closing "});"
    raise AssertionError("could not brace-match the storage listener")


def _run_node(script: str) -> dict:
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, f"node harness failed:\n{result.stderr}"
    return json.loads(result.stdout)


# Functions the unread stores are made of. All of these exist before and after
# the fix, so a missing one is a real extraction failure.
_CLIENT_FNS = [
    "_getSessionViewedCounts",
    "_saveSessionViewedCounts",
    "_setSessionViewedCount",
    "_clearSessionViewedCount",
    "_getSessionCompletionUnread",
    "_saveSessionCompletionUnread",
    "_markSessionCompletionUnread",
    "_clearSessionCompletionUnread",
    "_hasSessionCompletionUnread",
]

# Helpers that carry the merge/tombstone rules; injected when present.
_HELPER_FNS = [
    "_readStoredJsonMap",
    "_sessionExistsForUnreadState",
    "_sessionListLoaded",
    "_mergeSessionViewedCounts",
    "_sessionViewedCountDeletedKey",
    "_readSessionViewedCountDeletions",
    "_recordSessionViewedCountDeleted",
    "_markerLosesToUnreadClear",
    "_sessionCompletionUnreadOrder",
    "_nextSessionCompletionUnreadOrder",
    "_sessionCompletionUnreadClearedKey",
    "_parseSessionCompletionUnreadClearedKey",
    "_readSessionCompletionUnreadCleared",
    "_mergeSessionCompletionUnread",
    "_writeSessionCompletionUnreadCleared",
]

_HEADER = f"""
// One fake localStorage shared by every client, counting writes so a test can
// assert a repair converges instead of ping-ponging writes between clients.
const _store = {{}};
let storageWrites = 0;
let _onRead = null;
const localStorage = {{
  getItem: (k) => {{
    const value = Object.prototype.hasOwnProperty.call(_store, k) ? _store[k] : null;
    // Test hook: runs after the value is captured, i.e. inside a read-modify-write.
    if (_onRead) {{ const hook = _onRead; _onRead = null; hook(); }}
    return value;
  }},
  setItem: (k, v) => {{ storageWrites += 1; _store[k] = String(v); }},
  removeItem: (k) => {{ delete _store[k]; }},
  key: (i) => Object.keys(_store)[i] ?? null,
  get length() {{ return Object.keys(_store).length; }},
}};
const SESSION_VIEWED_COUNTS_KEY = {VIEWED_KEY!r};
const SESSION_VIEWED_COUNTS_DELETED_PREFIX = `${{SESSION_VIEWED_COUNTS_KEY}}:deleted:v1:`;
const SESSION_VIEWED_COUNTS_DELETED_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const readViewedDeletions = () =>
  Object.keys(_store).filter((k) => k.startsWith(SESSION_VIEWED_COUNTS_DELETED_PREFIX));
const SESSION_COMPLETION_UNREAD_KEY = {UNREAD_KEY!r};
const SESSION_COMPLETION_UNREAD_CLEARED_KEY = {CLEARED_KEY!r};
const SESSION_COMPLETION_UNREAD_CLEARED_PREFIX = `${{SESSION_COMPLETION_UNREAD_CLEARED_KEY}}:v1:`;
// Mirrors the module constant. The age assertions use hours vs 8 days so they
// do not depend on the exact cap.
const SESSION_COMPLETION_UNREAD_CLEARED_TTL_MS = 7 * 24 * 60 * 60 * 1000;
let _now = 1000;
Date.now = () => _now;
// The session list this client can see: the authority on "still exists".
let _allSessions = [];
const _sessionListSnapshotById = new Map();
const listed = (...ids) => {{ _allSessions = ids.map((sid) => ({{session_id: sid}})); }};
const readDisk = (key) => JSON.parse(_store[key] || '{{}}');
const setDisk = (key, value) => {{ _store[key] = JSON.stringify(value); }};
// Aggregate either the legacy whole map or independent per-session records.
const readAllClears = (client) => client && typeof client.clears === 'function'
  ? client.clears()
  : readDisk(SESSION_COMPLETION_UNREAD_CLEARED_KEY);
const _storageHandlers = [];
const window = {{
  addEventListener: (type, fn) => {{
    if (type === 'storage') _storageHandlers.push(fn);
  }},
}};
// The neighbours the storage registration also routes to; only the unread
// handler is under test here.
async function _handleActiveSessionStorageEvent() {{}}
async function _handleShowAllProfilesStorageEvent() {{}}
"""


def _client_factory(with_listener: bool = False) -> str:
    """One client's module state plus the whole unread-store API."""
    fns = "\n".join(_extract(name) for name in _CLIENT_FNS)
    helpers = "\n".join(_extract_optional(name) for name in _HELPER_FNS)
    handler = _extract_optional("_handleUnreadStorageEvent")
    listener = _extract_storage_listener() if with_listener else ""
    return f"""
function makeClient() {{
  let _sessionViewedCounts = null;
  let _sessionCompletionUnread = null;
  {fns}
  {helpers}
  {handler}
  {listener}
  return {{
    viewed: (sid) => {{
      const counts = _getSessionViewedCounts();
      return Object.prototype.hasOwnProperty.call(counts, sid) ? counts[sid] : null;
    }},
    setViewed: _setSessionViewedCount,
    clearViewed: _clearSessionViewedCount,
    markUnread: _markSessionCompletionUnread,
    clearUnread: _clearSessionCompletionUnread,
    writeClear: _writeSessionCompletionUnreadCleared,
    hasUnread: _hasSessionCompletionUnread,
    clears: (typeof _readSessionCompletionUnreadCleared === 'function')
      ? _readSessionCompletionUnreadCleared
      : () => readDisk(SESSION_COMPLETION_UNREAD_CLEARED_KEY),
    handleStorage: (typeof _handleUnreadStorageEvent === 'function')
      ? _handleUnreadStorageEvent
      : () => {{}},
    deletions: (typeof _readSessionViewedCountDeletions === 'function')
      ? _readSessionViewedCountDeletions
      : () => ({{}}),
  }};
}}
"""


def _script(body: str, with_listener: bool = False) -> str:
    return _HEADER + _client_factory(with_listener) + body


# ── Viewed counts: monotonic, repaired, never resurrected ────────────────────

def test_stale_client_cannot_roll_a_viewed_count_back():
    """Sequential rollback: a client that saw an older list snapshot must not
    lower a count another client already acknowledged."""
    out = _run_node(_script("""
listed('X');
const A = makeClient();
const B = makeClient();
A.setViewed('X', 417);
B.setViewed('X', 131);
console.log(JSON.stringify({
  disk: readDisk(SESSION_VIEWED_COUNTS_KEY),
  bView: B.viewed('X'),
}));
"""))
    assert out["disk"] == {"X": 417}, (
        "a stale client must not lower a session's viewed count"
    )
    assert out["bView"] == 417, "the stale client must converge on the merged count"


def test_clear_viewed_count_still_removes_from_disk():
    """Guard: session deletion must still prune the viewed count."""
    out = _run_node(_script("""
listed('D');
const A = makeClient();
A.setViewed('D', 5);
A.clearViewed('D');
console.log(JSON.stringify({
  diskHasD: Object.prototype.hasOwnProperty.call(readDisk(SESSION_VIEWED_COUNTS_KEY), 'D'),
}));
"""))
    assert out["diskHasD"] is False, "clearing a viewed count must remove it from disk"


def test_delete_prunes_a_viewed_count_only_the_store_holds():
    """Deletion must consult the store, not just our cache. An entry another
    client added after this client's cache was loaded is exactly what the prune
    path exists to remove, and an early return on the cache miss leaves it
    behind for a session the list no longer shows."""
    out = _run_node(_script("""
listed('E');
const A = makeClient();
const B = makeClient();
// B's cache loads while the store is still empty, so B never sees E.
B.viewed('E');
// A acknowledges E, which B cannot know about from its own cache.
A.setViewed('E', 17);
const bCached = B.viewed('E');
B.clearViewed('E');
console.log(JSON.stringify({
  bCached: bCached,
  disk: readDisk(SESSION_VIEWED_COUNTS_KEY),
}));
"""))
    assert out["bCached"] is None, "precondition: B's cache must not hold E"
    assert out["disk"] == {}, (
        "deleting a session must prune its viewed count from the store even "
        "when this client's cache never held the key"
    )


def test_delete_keeps_the_higher_count_this_client_holds_for_others():
    """Pruning one session must not cost this client the acknowledgement it
    holds for another. Adopting the store's view wholesale while pruning would
    drop a count another client already lowered, and this client is the only
    one that can still repair it."""
    out = _run_node(_script("""
listed('X', 'E');
const A = makeClient();
A.setViewed('X', 417);
// Another client lowered X while recording its own acknowledgement for E.
setDisk(SESSION_VIEWED_COUNTS_KEY, { X: 131, E: 17 });
A.clearViewed('E');
const held = A.viewed('X');
// A now processes the storage event that clobber arrived with.
A.handleStorage({ key: SESSION_VIEWED_COUNTS_KEY });
console.log(JSON.stringify({
  held: held,
  disk: readDisk(SESSION_VIEWED_COUNTS_KEY),
}));
"""))
    assert out["held"] == 417, (
        "pruning one session must not drop the higher count this client holds "
        "for another"
    )
    assert out["disk"] == {"X": 417}, (
        "the client must still hold the acknowledgement that repairs the "
        "lowered count in the store"
    )


def test_interleaved_acknowledgements_both_survive():
    """Both clients read one baseline, then each write lands inside the other's
    read-modify-write window. Neither acknowledgement may be lost."""
    out = _run_node(_script("""
listed('X', 'Y');
const A = makeClient();
const B = makeClient();
// Both clients read the same baseline before either write commits.
A.viewed('X');
B.viewed('Y');
// A's save reads the store; B's write for its own session lands in that window,
// so A's write (built from the store it read) drops B's entry.
_onRead = () => { B.setViewed('Y', 999); };
A.setViewed('X', 417);
const clobbered = readDisk(SESSION_VIEWED_COUNTS_KEY);
// B still holds its own acknowledgement, and now processes A's storage event.
B.handleStorage({ key: SESSION_VIEWED_COUNTS_KEY });
const disk = readDisk(SESSION_VIEWED_COUNTS_KEY);
const writesAfterRepair = storageWrites;
B.handleStorage({ key: SESSION_VIEWED_COUNTS_KEY });
console.log(JSON.stringify({
  clobbered: clobbered,
  disk: disk,
  stable: storageWrites === writesAfterRepair,
  aView: A.viewed('X'),
  bView: B.viewed('Y'),
}));
"""))
    assert out["clobbered"] == {"X": 417}, (
        "precondition: the interleaved write dropped B's entry"
    )
    assert out["disk"] == {"X": 417, "Y": 999}, (
        "the client that still holds an acknowledgement must re-assert it after "
        "the other client's storage event, not re-read the store it just lost"
    )
    assert out["aView"] == 417 and out["bView"] == 999, (
        "both clients must converge on a view that contains both acknowledgements"
    )
    assert out["stable"] is True, (
        "a repair must converge: a second event must not write again"
    )


def test_repair_does_not_resurrect_a_deleted_sessions_viewed_count():
    """A session deleted in one client is pruned from the store; a stale client
    that still caches its count must not write it back."""
    out = _run_node(_script("""
listed('S', 'T');
const A = makeClient();
const B = makeClient();
A.setViewed('S', 5);
B.viewed('S');
A.clearViewed('S');
listed('T');
B.setViewed('T', 1);
B.handleStorage({ key: SESSION_VIEWED_COUNTS_KEY });
console.log(JSON.stringify({
  disk: readDisk(SESSION_VIEWED_COUNTS_KEY),
  tView: B.viewed('T'),
}));
"""))
    assert out["disk"] == {"T": 1}, (
        "a deleted session's pruned count must not be resurrected by a stale "
        f"client's save or repair (disk was {out['disk']})"
    )
    assert out["tView"] == 1, "the live acknowledgement for the listed session must survive"


# ── Completion unread: the later event wins ──────────────────────────────────

def test_stale_client_cannot_resurrect_a_deleted_sessions_count():
    """Deleting a session records its own fact, so a client that still caches the
    acknowledgement prunes it instead of writing it back — list membership cannot
    decide this, because the sidebar filters by profile, project, and source."""
    out = _run_node(_script("""
listed('S', 'T');
const A = makeClient();
const B = makeClient();
A.setViewed('S', 5);
B.viewed('S');
A.clearViewed('S');
B.handleStorage({ key: SESSION_VIEWED_COUNTS_KEY });
B.setViewed('T', 1);
console.log(JSON.stringify({
  disk: readDisk(SESSION_VIEWED_COUNTS_KEY),
  deletions: readViewedDeletions().length,
  bView: B.viewed('S'),
}));
"""))
    assert out["disk"] == {"T": 1}, (
        "a deleted session's count must not come back from a stale client"
    )
    assert out["deletions"] == 1
    assert out["bView"] is None


def test_viewed_count_deletion_records_are_pruned_by_age():
    """The deletion records are bounded like the other stores: a later merge
    drops one that has aged out."""
    out = _run_node(_script("""
listed('S', 'X');
const A = makeClient();
const young = (() => { A.clearViewed('S'); return readViewedDeletions().length; })();
const keptYoung = (() => { A.setViewed('X', 2); return readViewedDeletions().length; })();
_now = 1000 + 8 * 24 * 60 * 60 * 1000;
A.setViewed('X', 3);
console.log(JSON.stringify({
  young: young,
  keptYoung: keptYoung,
  afterAge: readViewedDeletions().length,
}));
"""))
    assert out["young"] == 1
    assert out["keptYoung"] == 1, "a fresh deletion record must survive an unrelated merge"
    assert out["afterAge"] == 0, "an expired deletion record must be pruned"


def test_marker_merge_keeps_the_later_operation_within_one_tick():
    """Two clients can complete a session in the same millisecond. The merge must
    pick the later logical operation, not compare wall-clock completion times,
    or the retained marker keeps the older message count and metadata."""
    out = _run_node(_script("""
listed('Y');
const A = makeClient();
const B = makeClient();
_now = 1000;
A.markUnread('Y', 3);
B.markUnread('Y', 7);
const disk = readDisk(SESSION_COMPLETION_UNREAD_KEY);
console.log(JSON.stringify({
  count: (disk.Y || {}).message_count || null,
  completedAt: (disk.Y || {}).completed_at || null,
}));
"""))
    assert out["count"] == 7, (
        "the later completion must win even though both markers share a millisecond"
    )
    assert out["completedAt"] == 1000, (
        "the wall-clock completion time is still recorded for display and pruning"
    )


def test_stale_client_cannot_resurrect_a_cleared_completion_marker():
    """The field symptom: a marker cleared by opening the chat must not come
    back when another client that still holds it saves later."""
    out = _run_node(_script("""
listed('Y', 'Z');
const A = makeClient();
const B = makeClient();
_now = 1000;
A.markUnread('Y', 3);
B.hasUnread('Y');
_now = 2000;
A.clearUnread('Y');
// B has not processed A's storage event yet and saves for another session.
B.markUnread('Z', 7);
const disk = readDisk(SESSION_COMPLETION_UNREAD_KEY);
console.log(JSON.stringify({
  y: Object.prototype.hasOwnProperty.call(disk, 'Y'),
  z: Object.prototype.hasOwnProperty.call(disk, 'Z'),
  yViewA: A.hasUnread('Y'),
}));
"""))
    assert out["z"] is True, "precondition: the stale client's own marker is persisted"
    assert out["y"] is False, (
        "a marker cleared in one client must not be resurrected by another "
        "client that still holds it"
    )
    assert out["yViewA"] is False, "the cleared marker must stay cleared for the clear's own client"


def test_a_genuine_later_re_mark_survives_the_earlier_clear():
    """Control: ordering must not over-clear. A completion that happened after
    the clear is new information and must win."""
    out = _run_node(_script("""
listed('Y');
const A = makeClient();
const B = makeClient();
_now = 1000;
A.markUnread('Y', 3);
B.hasUnread('Y');
_now = 2000;
A.clearUnread('Y');
_now = 3000;
A.markUnread('Y', 9);
B.handleStorage({ key: SESSION_COMPLETION_UNREAD_KEY });
const disk = readDisk(SESSION_COMPLETION_UNREAD_KEY);
console.log(JSON.stringify({
  marker: disk.Y || null,
  aView: A.hasUnread('Y'),
  bView: B.hasUnread('Y'),
}));
"""))
    assert out["marker"] == {
        "message_count": 9,
        "completed_at": 3000,
        "unread_order": 3001,
    }, (
        "a completion that postdates the clear must be kept"
    )
    assert out["aView"] is True and out["bView"] is True, (
        "both clients must show the genuine re-mark"
    )


def test_later_clear_wins_after_same_millisecond_re_mark():
    """A clear must advance from the marker's logical order, not reuse the wall
    clock tick and lose to the marker's synthetic ``clearedAt + 1`` timestamp."""
    out = _run_node(_script("""
listed('Y');
const A = makeClient();
_now = 1000;
A.clearUnread('Y');
A.markUnread('Y', 3);
const marked = A.hasUnread('Y');
// The user visits again before the millisecond clock advances.
A.clearUnread('Y');
console.log(JSON.stringify({
  marked,
  final: A.hasUnread('Y'),
  disk: readDisk(SESSION_COMPLETION_UNREAD_KEY),
}));
"""))
    assert out["marked"] is True, "precondition: the same-ms re-mark must be visible"
    assert out["final"] is False
    assert out["disk"] == {}, "the later clear must remove the same-ms marker from disk"


def test_same_millisecond_clear_beats_marker_prepared_before_it():
    """A marker whose intent was prepared before a concurrent clear must lose,
    even when both operations observe the same wall-clock millisecond."""
    out = _run_node(_script("""
listed('Y');
const A = makeClient();
const B = makeClient();
A.hasUnread('Y');
B.hasUnread('Y');
_now = 1000;
_onRead = () => { A.clearUnread('Y'); };
B.markUnread('Y', 3);
console.log(JSON.stringify({
  marker: readDisk(SESSION_COMPLETION_UNREAD_KEY).Y || null,
  visible: B.hasUnread('Y'),
}));
"""))
    assert out["marker"] is None
    assert out["visible"] is False


def test_repair_removes_a_marker_that_lost_to_a_clear():
    """A stale client's write can land after the clear. The repair must drop the
    losing marker from the store, or the dot returns on every client."""
    out = _run_node(_script("""
listed('Y', 'Z');
const A = makeClient();
const B = makeClient();
_now = 1000;
A.markUnread('Y', 3);
B.hasUnread('Y');
_now = 2000;
A.clearUnread('Y');
// The stale write lands after the clear (saved before the event was processed).
setDisk(SESSION_COMPLETION_UNREAD_KEY, {
  Y: { message_count: 3, completed_at: 1000 },
  Z: { message_count: 2, completed_at: 1500 },
});
A.handleStorage({ key: SESSION_COMPLETION_UNREAD_KEY });
const disk = readDisk(SESSION_COMPLETION_UNREAD_KEY);
console.log(JSON.stringify({
  diskY: Object.prototype.hasOwnProperty.call(disk, 'Y'),
  diskZ: Object.prototype.hasOwnProperty.call(disk, 'Z'),
  yView: A.hasUnread('Y'),
  zView: A.hasUnread('Z'),
}));
"""))
    assert out["diskY"] is False, (
        "a repair must remove a marker that lost to a clear, not leave it for "
        "the next reader"
    )
    assert out["diskZ"] is True, "an unrelated marker must be left alone"
    assert out["yView"] is False and out["zView"] is True, (
        "the repaired view must match the store"
    )


def test_concurrent_clears_keep_both_ordering_facts():
    """Clearing different sessions in the same read/write window must not let
    either whole-map write erase the other session's ordering fact."""
    out = _run_node(_script("""
listed('X', 'Y');
const A = makeClient();
const B = makeClient();
_now = 1000;
A.markUnread('X', 1);
B.markUnread('Y', 1);
A.hasUnread('Y');
B.hasUnread('X');
_now = 2000;
// A captures the old tombstone state; B's clear lands before A writes.
_onRead = () => { B.clearUnread('Y'); };
A.clearUnread('X');
const tombs = readAllClears(A);
console.log(JSON.stringify({ tombs }));
"""))
    assert out["tombs"] == {"X": 2001, "Y": 2001}, (
        "concurrent clears for different sessions must retain both tombstones"
    )


def test_new_clear_does_not_dual_write_the_legacy_whole_map():
    """The independently addressable representation is authoritative. Keeping
    dual-write compatibility would retain the old lost-update race forever."""
    out = _run_node(_script("""
const A = makeClient();
A.writeClear('new', 2000);
console.log(JSON.stringify({
  legacyPresent: Object.prototype.hasOwnProperty.call(_store, SESSION_COMPLETION_UNREAD_CLEARED_KEY),
  all: readAllClears(A),
}));
"""))
    assert out["legacyPresent"] is False
    assert out["all"] == {"new": 2000}


def test_legacy_tombstone_map_is_migrated_then_removed():
    """A reloaded client imports existing clear facts once, then removes the
    unbounded whole map instead of pretending concurrent old/new writes are safe."""
    out = _run_node(_script("""
setDisk(SESSION_COMPLETION_UNREAD_CLEARED_KEY, { legacy: 1000 });
const A = makeClient();
A.writeClear('new', 2000);
console.log(JSON.stringify({
  legacyPresent: Object.prototype.hasOwnProperty.call(_store, SESSION_COMPLETION_UNREAD_CLEARED_KEY),
  all: readAllClears(A),
}));
"""))
    assert out["legacyPresent"] is False
    assert out["all"] == {"legacy": 1000, "new": 2000}


def test_same_session_clear_versions_fold_to_the_newest_operation():
    """An older clear operation that lands after a newer one must not lower the
    ordering fact for that session."""
    out = _run_node(_script("""
listed('Y');
const A = makeClient();
A.writeClear('Y', 2000);
A.writeClear('Y', 1000);
console.log(JSON.stringify({ tombs: readAllClears(A) }));
"""))
    assert out["tombs"] == {"Y": 2000}


def test_clear_orders_out_a_marker_prepared_but_not_yet_persisted():
    """A user visit after completion intent but before its delayed marker write
    must record a clear even when neither its cache nor storage has the marker."""
    out = _run_node(_script("""
listed('Y');
const A = makeClient();
const B = makeClient();
A.hasUnread('Y');
B.hasUnread('Y');
_now = 1000;
// B's marker carries t=1000. Its save captures the empty marker map, then A
// visits at t=2000 before B's delayed write commits.
_onRead = () => { _now = 2000; A.clearUnread('Y'); };
B.markUnread('Y', 3);
const marker = readDisk(SESSION_COMPLETION_UNREAD_KEY).Y || null;
const tombstone = readAllClears(A).Y || null;
console.log(JSON.stringify({ marker, tombstone }));
"""))
    assert out["tombstone"] == 2001, (
        "a clear must persist its ordering fact even when the marker write is delayed"
    )
    assert out["marker"] is None, "the completion that predates the visit must remain cleared"


def test_clear_records_survive_a_session_missing_from_the_list():
    """A session absent from the visible list is not proof of deletion: the list
    is filtered by profile, project, and source. Dropping its clear record would
    re-light a dot the user already cleared, so age is the only pruning signal."""
    out = _run_node(_script("""
listed('Y', 'GONE');
const A = makeClient();
_now = 1000;
A.markUnread('GONE', 1);
A.clearUnread('GONE');
listed('Y');
_now = 2000;
A.markUnread('Y', 1);
A.clearUnread('Y');
console.log(JSON.stringify({ tombs: readAllClears(A) }));
"""))
    assert out["tombs"] == {"GONE": 1002, "Y": 2002}


def test_tombstones_are_capped_by_age():
    """Quota guard: an old tombstone is dropped when a newer clear is recorded."""
    out = _run_node(_script("""
listed('Y', 'Z');
const A = makeClient();
_now = 1000;
A.markUnread('Y', 1);
A.clearUnread('Y');
const young = _now + 60 * 60 * 1000;
_now = young;
A.markUnread('Z', 1);
A.clearUnread('Z');
const keptYoung = readAllClears(A);
_now = young + 8 * 24 * 60 * 60 * 1000;
A.markUnread('Y', 1);
A.clearUnread('Y');
console.log(JSON.stringify({
  keptYoung: keptYoung,
  afterAge: readAllClears(A),
  young: young,
}));
"""))
    assert out["keptYoung"] == {"Y": 1002, "Z": out["young"] + 2}, (
        "a tombstone younger than the cap must survive a newer clear"
    )
    assert out["afterAge"] == {
        "Y": out["young"] + 8 * 24 * 60 * 60 * 1000 + 2,
    }, "a tombstone older than the cap must be removed from storage"


# ── The registration itself ──────────────────────────────────────────────────

def test_registered_storage_listener_syncs_both_unread_keys_from_storage():
    """Behavioural wiring test: capture the handler the module registers on
    ``storage``, dispatch synthetic events for both keys, and assert the client's
    view comes from the updated store."""
    out = _run_node(_script("""
listed('X', 'Q', 'R');
const A = makeClient();
const dispatch = (key) => {
  const value = Object.prototype.hasOwnProperty.call(_store, key) ? _store[key] : null;
  for (const handler of _storageHandlers) handler({ key: key, newValue: value });
};
A.setViewed('X', 511);
A.hasUnread('Q');
// A stale client clobbers the store with its lower count.
setDisk(SESSION_VIEWED_COUNTS_KEY, { X: 131 });
dispatch(SESSION_VIEWED_COUNTS_KEY);
const writesAfterRepair = storageWrites;
dispatch(SESSION_VIEWED_COUNTS_KEY);
const repairStable = storageWrites === writesAfterRepair;
// A marker another client just recorded, and a clear it recorded.
setDisk(SESSION_COMPLETION_UNREAD_KEY, { Q: { message_count: 2, completed_at: 5000 } });
const qStale = A.hasUnread('Q');
dispatch(SESSION_COMPLETION_UNREAD_KEY);
const rClearKey = `${SESSION_COMPLETION_UNREAD_CLEARED_PREFIX}${encodeURIComponent('R')}:9000`;
_store[rClearKey] = '9000';
setDisk(SESSION_COMPLETION_UNREAD_KEY, {
  Q: { message_count: 2, completed_at: 5000 },
  R: { message_count: 3, completed_at: 8000 },
});
dispatch(rClearKey);
const disk = readDisk(SESSION_COMPLETION_UNREAD_KEY);
console.log(JSON.stringify({
  handlers: _storageHandlers.length,
  xView: A.viewed('X'),
  diskX: readDisk(SESSION_VIEWED_COUNTS_KEY).X,
  stable: repairStable,
  qStale: qStale,
  qView: A.hasUnread('Q'),
  rView: A.hasUnread('R'),
  diskR: Object.prototype.hasOwnProperty.call(disk, 'R'),
}));
""", with_listener=True))
    assert out["handlers"] == 1, "the module must register exactly one storage listener"
    assert out["xView"] == 511 and out["diskX"] == 511, (
        "a storage event must repair the store with the count this client still "
        "holds instead of adopting the clobbering value"
    )
    assert out["stable"] is True, "the repair must not write again for the same state"
    assert out["qStale"] is False and out["qView"] is True, (
        "a storage event for the unread key must refresh the client's marker view"
    )
    assert out["rView"] is False and out["diskR"] is False, (
        "the cleared-marker key must be routed to the handler and its tombstone "
        "must win over the stale marker in the store"
    )
