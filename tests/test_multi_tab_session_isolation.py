"""Multi-tab conversation isolation (behavioural, executed in Node).

The active-session id, the in-flight stream marker and the mid-stream
transcript snapshots used to live in GLOBAL localStorage keys shared by every
tab of the origin, so opening two conversations in two tabs let the last
writer win:

  * reloading tab A restored tab B's conversation,
  * tab A's reconnect banner pointed at tab B's stream,
  * tab A merged tab B's in-flight transcript into its own chat.

These probes extract the real helpers from static/ui.js and run them against
two simulated tabs sharing one localStorage, so they fail on the pre-fix
build instead of merely asserting on source text.
"""

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_SRC = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        char = src[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"could not extract function body for {signature!r}")


def _const_line(src: str, name: str) -> str:
    marker = f"const {name} = "
    start = src.index(marker)
    end = src.index("\n", start)
    return src[start:end]


_HELPER_CONSTS = (
    "TAB_ID_KEY",
    "TAB_ID_RELEASED_BASE",
    "_TAB_RELEASED_TTL_MS",
    "INFLIGHT_KEY_BASE",
    "INFLIGHT_STATE_KEY_BASE",
    "ACTIVE_SESSION_KEY_LEGACY",
    "ACTIVE_SESSION_TOMBSTONE_BASE",
    "TAB_ACTIVE_SESSION_MIRROR_KEY",
    "TAB_ACTIVE_SESSION_TOMBSTONE_MIRROR_KEY",
    "TAB_INFLIGHT_MIRROR_KEY",
    "TAB_INFLIGHT_STATE_MIRROR_KEY",
)

_HELPER_FUNCS = (
    "function _newTabId",
    "function _scopedTabKey",
    "function _mirrorTabValue",
    "function _restoreDocumentScopedState",
    "function _gcOrphanTabKeys",
    "function _releaseTabId",
    "function _hermesTabId",
    "function _inflightKey",
    "function _inflightStateKey",
    "function _activeSessionKey",
    "function _activeSessionTombstoneKey",
    "function _rememberActiveSession",
    "function _rememberedActiveSession",
    "function _forgetActiveSession",
    "function _migrateLegacyInflight",
    "function _readInflightStateMap",
    "function loadInflightState",
    "function clearInflightState",
)


def _helpers() -> str:
    parts = [_const_line(UI_SRC, name).replace("const ", "var ", 1) for name in _HELPER_CONSTS]
    parts += [_function_body(UI_SRC, sig) for sig in _HELPER_FUNCS]
    return "\n".join(parts)


_HARNESS = """
// Two tabs of the same origin share ONE localStorage but each has its OWN
// sessionStorage AND its own window object — this is exactly how browsers
// behave. The helpers close over `sessionStorage` / `window`, so useTab()
// rebinds what those names resolve to instead of shadowing them.
let __uuid = 0;
function makeStorage(){
  const map = new Map();
  return {
    getItem: (k) => (map.has(String(k)) ? map.get(String(k)) : null),
    setItem: (k, v) => { map.set(String(k), String(v)); },
    removeItem: (k) => { map.delete(String(k)); },
    clear: () => map.clear(),
    get length(){ return map.size; },
    key: (i) => Array.from(map.keys())[i],
    _keys: () => Array.from(map.keys()),
  };
}
// A browser tab = its own sessionStorage + its own window (module scope).
// crypto.randomUUID() is document-local and never copied with sessionStorage.
function makeTab(){
  return {
    store: makeStorage(),
    win: {
      addEventListener(){},
      crypto: { randomUUID(){ return 'uuid-' + (++__uuid); } },
    },
  };
}

const localStorage = makeStorage();
let __tab = makeTab();
const sessionStorage = {
  getItem: (k) => __tab.store.getItem(k),
  setItem: (k, v) => __tab.store.setItem(k, v),
  removeItem: (k) => __tab.store.removeItem(k),
  get length(){ return __tab.store.length; },
  key: (i) => __tab.store.key(i),
  _keys: () => __tab.store._keys(),
};
const window = new Proxy({}, {
  get: (_t, prop) => __tab.win[prop],
  set: (_t, prop, value) => { __tab.win[prop] = value; return true; },
  has: (_t, prop) => prop in __tab.win,
  deleteProperty: (_t, prop) => { delete __tab.win[prop]; return true; },
});
// The real helper installs a heartbeat interval; keep Node's event loop empty
// so the probe exits.
function setInterval(){ return 0; }
let __now = 1_000_000;
// Route the helpers' Date.now() through a controllable clock.
const Date = { now: () => __now };
// Bring a given tab to the foreground.
function useTab(tab){ __tab = tab; }
"""


def _run(script: str) -> dict:
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_two_tabs_get_distinct_identities_and_scoped_keys():
    script = f"""
{_HARNESS}
{_helpers()}
const tabA = makeTab();
const tabB = makeTab();
useTab(tabA); const idA = _hermesTabId(); const keyA = _activeSessionKey();
useTab(tabB); const idB = _hermesTabId(); const keyB = _activeSessionKey();
console.log(JSON.stringify({{idA, idB, keyA, keyB}}));
"""
    out = _run(script)
    assert out["idA"] and out["idB"], "each tab must mint an id"
    assert out["idA"] != out["idB"], "two concurrent tabs must not share a tab id"
    assert out["keyA"] != out["keyB"], "active-session keys must be tab-scoped"


def test_reload_restores_this_tabs_conversation_not_the_other_tabs():
    """The original bug: tab A reloads and lands in tab B's conversation."""
    script = f"""
{_HARNESS}
{_helpers()}
const tabA = makeTab();
const tabB = makeTab();
useTab(tabA); _rememberActiveSession('session-AAA');
useTab(tabB); _rememberActiveSession('session-BBB');   // last writer used to win
useTab(tabA); const restored = _rememberedActiveSession();
console.log(JSON.stringify({{restored}}));
"""
    assert _run(script)["restored"] == "session-AAA"


def test_inflight_stream_marker_is_per_tab():
    script = f"""
{_HARNESS}
{_helpers()}
const tabA = makeTab();
const tabB = makeTab();
useTab(tabA); localStorage.setItem(_inflightKey(), 'stream-A');
useTab(tabB); localStorage.setItem(_inflightKey(), 'stream-B');
useTab(tabA); const markerA = localStorage.getItem(_inflightKey());
useTab(tabB); const markerB = localStorage.getItem(_inflightKey());
console.log(JSON.stringify({{markerA, markerB}}));
"""
    out = _run(script)
    assert out["markerA"] == "stream-A"
    assert out["markerB"] == "stream-B"


def test_duplicated_tab_remints_its_id():
    """Chrome's "Duplicate tab" COPIES sessionStorage, so the clone would
    otherwise start life holding the original tab's id and both tabs would
    write to the same per-tab keys."""
    script = f"""
{_HARNESS}
{_helpers()}
const original = makeTab();
useTab(original); const idOriginal = _hermesTabId();
// Duplicate: sessionStorage contents are copied verbatim.
const clone = makeTab();
for (const k of original.store._keys()) clone.store.setItem(k, original.store.getItem(k));
useTab(clone); const idClone = _hermesTabId();
console.log(JSON.stringify({{idOriginal, idClone}}));
"""
    out = _run(script)
    assert out["idOriginal"] != out["idClone"], "a duplicated tab must re-mint its id"


def test_plain_reload_remints_and_restores_recovery_under_the_new_id():
    """A reload receives a fresh document id, but its sessionStorage recovery
    mirror must be restored and re-stamped before scoped state is read."""
    script = f"""
{_HARNESS}
{_helpers()}
const tab = makeTab();
useTab(tab); const before = _hermesTabId();
_rememberActiveSession('session-reload');
const snapshot = {{'session-reload':{{streamId:'stream-reload', updated_at:__now, tabId:before}}}};
localStorage.setItem(_inflightStateKey(), JSON.stringify(snapshot));
_mirrorTabValue(TAB_INFLIGHT_STATE_MIRROR_KEY, JSON.stringify(snapshot));
const marker = JSON.stringify({{sid:'session-reload', streamId:'stream-reload', ts:__now}});
localStorage.setItem(_inflightKey(), marker);
_mirrorTabValue(TAB_INFLIGHT_MIRROR_KEY, marker);
_releaseTabId();                     // pagehide fires before unload
// Reload: sessionStorage survives, the window (and its cached id) does not.
const freshDocument = makeTab();
const reloaded = {{ store: tab.store, win: freshDocument.win }};
useTab(reloaded); const after = _hermesTabId();
const restoredSession = _rememberedActiveSession();
const restoredState = loadInflightState('session-reload', 'stream-reload');
const restoredMarker = JSON.parse(localStorage.getItem(_inflightKey()));
console.log(JSON.stringify({{
  before,
  after,
  restoredSession,
  restoredOwner:restoredState&&restoredState.tabId,
  restoredMarkerSid:restoredMarker&&restoredMarker.sid,
}}));
"""
    out = _run(script)
    assert out["before"] != out["after"], "every new document must receive a fresh id"
    assert out["restoredSession"] == "session-reload"
    assert out["restoredOwner"] == out["after"], "recovery must be re-stamped"
    assert out["restoredMarkerSid"] == "session-reload"


def test_orphan_tab_keys_are_reclaimed():
    """Per-tab keys must not accumulate until localStorage hits quota."""
    script = f"""
{_HARNESS}
{_helpers()}
const tab = makeTab();
useTab(tab);
const live = _hermesTabId();
// Leftovers from a tab closed long ago.
localStorage.setItem(ACTIVE_SESSION_KEY_LEGACY + '::ghost', 'old-session');
localStorage.setItem(INFLIGHT_KEY_BASE + '::ghost', 'old-stream');
localStorage.setItem(TAB_ID_RELEASED_BASE + '::ghost', String(__now - (_TAB_RELEASED_TTL_MS * 2)));
_gcOrphanTabKeys();
const remaining = localStorage._keys().filter(k => k.indexOf('::ghost') !== -1);
const liveKept = localStorage._keys().filter(k => k.indexOf('::' + live) !== -1);
console.log(JSON.stringify({{remaining, liveKeptCount: liveKept.length}}));
"""
    out = _run(script)
    assert out["remaining"] == [], "keys of long-gone tabs must be reclaimed"


def test_recent_tab_keys_are_not_reclaimed():
    """GC must not evict a tab that is merely in a background window."""
    script = f"""
{_HARNESS}
{_helpers()}
const tab = makeTab();
useTab(tab); _hermesTabId();
// Another document was only just released; retain its recovery grace period.
localStorage.setItem(ACTIVE_SESSION_KEY_LEGACY + '::recent', 'still-used');
localStorage.setItem(TAB_ID_RELEASED_BASE + '::recent', String(__now - 1000));
_gcOrphanTabKeys();
const kept = localStorage.getItem(ACTIVE_SESSION_KEY_LEGACY + '::recent');
console.log(JSON.stringify({{kept}}));
"""
    assert _run(script)["kept"] == "still-used"


def test_legacy_key_still_written_for_first_paint_fallback():
    """boot.js may read the legacy key before a tab id exists; keep it fresh so
    a brand-new tab still lands on the last conversation."""
    script = f"""
{_HARNESS}
{_helpers()}
const tab = makeTab();
useTab(tab); _rememberActiveSession('session-XYZ');
const legacy = localStorage.getItem(ACTIVE_SESSION_KEY_LEGACY);
console.log(JSON.stringify({{legacy}}));
"""
    assert _run(script)["legacy"] == "session-XYZ"


def test_forget_clears_this_tab_only():
    script = f"""
{_HARNESS}
{_helpers()}
const tabA = makeTab();
const tabB = makeTab();
useTab(tabA); _rememberActiveSession('session-AAA');
useTab(tabB); _rememberActiveSession('session-BBB');
useTab(tabA); _forgetActiveSession();
useTab(tabA); const aScoped = localStorage.getItem(_activeSessionKey());
useTab(tabB); const b = _rememberedActiveSession();
console.log(JSON.stringify({{aScoped, b}}));
"""
    out = _run(script)
    assert out["aScoped"] is None, "this tab's stored session must be cleared"
    assert out["b"] == "session-BBB", "another tab's session must survive"


def test_forget_does_not_strip_the_shared_fallback_of_other_tabs():
    """One tab clearing its own conversation must not blank the legacy key that
    a brand-new (or older-client) tab relies on for first paint."""
    script = f"""
{_HARNESS}
{_helpers()}
const tabA = makeTab();
const tabB = makeTab();
useTab(tabA); _rememberActiveSession('session-AAA');
useTab(tabB); _rememberActiveSession('session-BBB');   // legacy key now = BBB
// Tab A forgets ITS session; the shared slot still points at tab B's.
useTab(tabA); _forgetActiveSession();
const legacyAfter = localStorage.getItem(ACTIVE_SESSION_KEY_LEGACY);
// A brand-new tab has no scoped key and must fall back to the shared slot.
const tabC = makeTab();
useTab(tabC); const fresh = _rememberedActiveSession();
// And when the LAST writer forgets, the shared slot is released.
useTab(tabB); _forgetActiveSession();
const legacyFinal = localStorage.getItem(ACTIVE_SESSION_KEY_LEGACY);
console.log(JSON.stringify({{legacyAfter, fresh, legacyFinal}}));
"""
    out = _run(script)
    assert out["legacyAfter"] == "session-BBB", "another tab's fallback must survive"
    assert out["fresh"] == "session-BBB", "a new tab must still restore the last session"
    assert out["legacyFinal"] is None, "the owning tab must still be able to clear it"


def test_release_markers_are_independent_per_document():
    """Teardown uses one scalar key per document, never a shared RMW registry."""
    script = f"""
{_HARNESS}
{_helpers()}
const tabA = makeTab();
const tabB = makeTab();
useTab(tabA); const idA = _hermesTabId();
useTab(tabB); const idB = _hermesTabId();
useTab(tabA); _releaseTabId();
useTab(tabB); _releaseTabId();
console.log(JSON.stringify({{
  idA, idB,
  releasedA: localStorage.getItem(TAB_ID_RELEASED_BASE + '::' + idA),
  releasedB: localStorage.getItem(TAB_ID_RELEASED_BASE + '::' + idB),
}}));
"""
    out = _run(script)
    assert out["idA"] != out["idB"]
    assert out["releasedA"] == "1000000"
    assert out["releasedB"] == "1000000"


def test_remembered_session_returns_null_when_empty():
    """Callers treat the result as falsy-or-id; keep the original
    localStorage.getItem() contract."""
    script = f"""
{_HARNESS}
{_helpers()}
useTab(makeTab());
console.log(JSON.stringify({{value: _rememberedActiveSession()}}));
"""
    assert _run(script)["value"] is None


def test_empty_legacy_fallback_is_read_only_once_per_document():
    """A later write by another tab must not bleed into a document that already
    established that it had no active session."""
    script = f"""
{_HARNESS}
{_helpers()}
const tabA = makeTab();
const tabB = makeTab();
useTab(tabA); const before = _rememberedActiveSession();
useTab(tabB); _rememberActiveSession('session-B');
useTab(tabA); const after = _rememberedActiveSession();
console.log(JSON.stringify({{before, after}}));
"""
    out = _run(script)
    assert out["before"] is None
    assert out["after"] is None


def test_missing_crypto_fails_closed_without_scoped_access():
    """An inherited id is never authority; without a cryptographic fresh id the
    document must neither read nor write scoped recovery state."""
    script = f"""
{_HARNESS}
{_helpers()}
const unsafe = makeTab();
unsafe.win.crypto = undefined;
unsafe.store.setItem(TAB_ID_KEY, 'copied-id');
const foreignKey = ACTIVE_SESSION_KEY_LEGACY + '::copied-id';
localStorage.setItem(foreignKey, 'foreign-session');
localStorage.setItem(ACTIVE_SESSION_KEY_LEGACY, 'legacy-session');
const rawGetItem = localStorage.getItem;
let foreignReads = 0;
localStorage.getItem = (key) => {{
  if(key === foreignKey) foreignReads++;
  return rawGetItem(key);
}};
useTab(unsafe);
const id = _hermesTabId();
const restored = _rememberedActiveSession();
_rememberActiveSession('attempted-write');
let scopedKeyRejected = false;
try{{ _activeSessionKey(); }}catch(_){{ scopedKeyRejected = true; }}
console.log(JSON.stringify({{
  id,
  restored,
  foreignReads,
  scopedKeyRejected,
  foreignValue: rawGetItem(foreignKey),
  legacyValue: rawGetItem(ACTIVE_SESSION_KEY_LEGACY),
}}));
"""
    out = _run(script)
    assert out["id"] is None
    assert out["restored"] is None
    assert out["foreignReads"] == 0
    assert out["scopedKeyRejected"]
    assert out["foreignValue"] == "foreign-session"
    assert out["legacyValue"] == "legacy-session"


# ── Adversarial re-gate regressions (maintainer CHANGES_REQUESTED 2026-08-17) ──
# Each test below EXECUTES the real helpers under the exact race/upgrade
# condition the maintainer reproduced against head 590debcf. They fail on the
# timer-based design and pass only with authoritative ownership.


def test_duplicated_tab_never_reuses_id_after_heartbeat_gap():
    """Must-fix 1 (ui.js:9298): with timeout-based leases, advancing the clock
    past one 45-second lease made the original tab's claim look expired, so a
    duplicated tab (copied sessionStorage) kept the original's id and writing
    session B in the clone made the ORIGINAL tab restore B. A missed heartbeat
    must never make an id reusable: ownership is a per-context nonce record,
    and only explicit release frees the id."""
    script = f"""
{_HARNESS}
{_helpers()}
const original = makeTab();
useTab(original);
const idOriginal = _hermesTabId();
_rememberActiveSession('session-A');
// Duplicate the tab: sessionStorage is COPIED verbatim; the window is not.
const clone = makeTab();
for (const k of original.store._keys()) clone.store.setItem(k, original.store.getItem(k));
// The original misses several heartbeats (laptop lid closed, tab throttled).
__now += 46_000;
useTab(clone);
const idClone = _hermesTabId();
_rememberActiveSession('session-B');
// Even a WEEK of missed heartbeats must not surrender the id.
__now += 7 * 24 * 60 * 60 * 1000;
const late = makeTab();
for (const k of original.store._keys()) late.store.setItem(k, original.store.getItem(k));
useTab(late);
const idLate = _hermesTabId();
useTab(original);
const restoredByOriginal = _rememberedActiveSession();
const idOriginalAfter = _hermesTabId();
console.log(JSON.stringify({{idOriginal, idClone, idLate, restoredByOriginal, idOriginalAfter}}));
"""
    out = _run(script)
    assert out["idClone"] != out["idOriginal"], (
        "a duplicated tab must NOT reuse the original's id after a heartbeat gap"
    )
    assert out["idLate"] != out["idOriginal"], (
        "no amount of elapsed time may make a claimed (unreleased) id reusable"
    )
    assert out["restoredByOriginal"] == "session-A", (
        "the clone writing session B must not change what the original restores"
    )
    assert out["idOriginalAfter"] == out["idOriginal"], (
        "revalidation must keep the original tab's identity stable"
    )


def test_gc_does_not_collect_live_tab_after_seen_ttl():
    """Must-fix 2a (ui.js:9330): a live tab whose 'seen' stamp aged past the
    24-hour TTL used to have its scoped keys garbage-collected by the next
    booting tab. Liveness is now authoritative: an unreleased ownership record
    protects the keys no matter how much time elapsed."""
    script = f"""
{_HARNESS}
{_helpers()}
const longLived = makeTab();
useTab(longLived);
const id = _hermesTabId();
_rememberActiveSession('session-precious');
localStorage.setItem(_inflightStateKey(), JSON.stringify({{'session-precious':{{streamId:'s1'}}}}));
// 25 hours pass with no heartbeat (suspended machine). The tab is still open.
__now += 25 * 60 * 60 * 1000;
// A brand-new tab boots and runs the GC pass.
useTab(makeTab());
_hermesTabId();
useTab(longLived);
const scopedSession = localStorage.getItem(_activeSessionKey());
const scopedInflight = localStorage.getItem(_inflightStateKey());
console.log(JSON.stringify({{scopedSession, scopedInflightKept: scopedInflight!=null}}));
"""
    out = _run(script)
    assert out["scopedSession"] == "session-precious", (
        "GC must never collect a claimed (unreleased) tab's keys on elapsed time alone"
    )
    assert out["scopedInflightKept"], "the live tab's inflight snapshot must survive GC"


def test_gc_fails_safe_on_missing_or_malformed_release_evidence():
    """GC may collect only after a valid, old, per-document release marker."""
    script = f"""
{_HARNESS}
{_helpers()}
const tab = makeTab();
useTab(tab);
const id = _hermesTabId();
_rememberActiveSession('session-live');
localStorage.setItem(TAB_ID_RELEASED_BASE + '::' + id, 'not-a-number');
_gcOrphanTabKeys();
const keptAfterMalformedRelease = localStorage.getItem(_activeSessionKey());
// A scoped key with no release evidence must also survive indefinitely.
localStorage.setItem(ACTIVE_SESSION_KEY_LEGACY + '::unrecorded', 'legacy-key');
_gcOrphanTabKeys();
const keptLegacy = localStorage.getItem(ACTIVE_SESSION_KEY_LEGACY + '::unrecorded');
console.log(JSON.stringify({{
  keptAfterMalformedRelease,
  keptLegacy,
  scopedStillThere: localStorage.getItem(_activeSessionKey()),
}}));
"""
    out = _run(script)
    assert out["keptAfterMalformedRelease"] == "session-live", (
        "malformed release evidence must disable collection for that document"
    )
    assert out["keptLegacy"] == "legacy-key", (
        "missing release evidence must retain unrecorded scoped keys"
    )
    assert out["scopedStillThere"] == "session-live"


def test_forgotten_tab_does_not_resurrect_another_tabs_legacy_session():
    """Must-fix 3a (ui.js:9441): after tab A forgot its session, the legacy
    fallback (kept fresh by tab B) used to bleed back into tab A's reads. The
    per-tab 'no session' tombstone pins the forgotten state."""
    script = f"""
{_HARNESS}
{_helpers()}
const tabA = makeTab();
const tabB = makeTab();
useTab(tabA); _rememberActiveSession('session-A');
useTab(tabB); _rememberActiveSession('session-B');   // legacy slot now = B
useTab(tabA); _forgetActiveSession();
const afterForget = _rememberedActiveSession();
// Tab B keeps refreshing the shared slot; tab A must STAY forgotten.
useTab(tabB); _rememberActiveSession('session-B');
useTab(tabA); const afterRewrite = _rememberedActiveSession();
// Opening a new conversation lifts the tombstone.
_rememberActiveSession('session-C');
const afterReopen = _rememberedActiveSession();
console.log(JSON.stringify({{afterForget, afterRewrite, afterReopen}}));
"""
    out = _run(script)
    assert out["afterForget"] is None, (
        "a forgotten tab must not restore another tab's legacy session"
    )
    assert out["afterRewrite"] is None, (
        "the tombstone must hold even while another tab rewrites the legacy slot"
    )
    assert out["afterReopen"] == "session-C", (
        "remembering a new session must supersede the tombstone"
    )


def test_forget_clears_expected_legacy_only_sid_on_upgrade():
    """Must-fix 3b (ui.js:9441): a legacy-only dead session (written by the
    pre-upgrade build, so no scoped value exists) used to survive
    _forgetActiveSession(), defeating the 404/profile-switch self-heal. The
    caller now names the dead sid explicitly and a matching legacy value is
    cleared even without scoped state."""
    script = f"""
{_HARNESS}
{_helpers()}
// Pre-upgrade storage: only the unsuffixed key exists.
localStorage.setItem(ACTIVE_SESSION_KEY_LEGACY, 'dead-session');
const tab = makeTab();
useTab(tab);
_forgetActiveSession('dead-session');           // 404 self-heal names the sid
const legacyAfterHeal = localStorage.getItem(ACTIVE_SESSION_KEY_LEGACY);
// A mismatching expected sid must NOT clear someone else's fallback.
localStorage.setItem(ACTIVE_SESSION_KEY_LEGACY, 'other-tabs-session');
useTab(makeTab());
_forgetActiveSession('a-different-dead-sid');
const legacyAfterMismatch = localStorage.getItem(ACTIVE_SESSION_KEY_LEGACY);
console.log(JSON.stringify({{legacyAfterHeal, legacyAfterMismatch}}));
"""
    out = _run(script)
    assert out["legacyAfterHeal"] is None, (
        "forget must clear a matching legacy-only sid so a dead session cannot "
        "survive the 404 self-heal on upgrade"
    )
    assert out["legacyAfterMismatch"] == "other-tabs-session", (
        "a non-matching expected sid must leave the shared fallback alone"
    )


def test_legacy_session_is_adopted_once_into_scoped_storage():
    """Must-fix 3 (adoption): the first legacy read is copied into the tab's
    scoped slot, so later changes to the shared slot by other tabs can no
    longer change what THIS tab restores."""
    script = f"""
{_HARNESS}
{_helpers()}
localStorage.setItem(ACTIVE_SESSION_KEY_LEGACY, 'upgraded-session');
const tab = makeTab();
useTab(tab);
const first = _rememberedActiveSession();       // adopts into scoped storage
const scoped = localStorage.getItem(_activeSessionKey());
// Another tab moves the shared slot; this tab's restore must not follow.
localStorage.setItem(ACTIVE_SESSION_KEY_LEGACY, 'other-session');
const second = _rememberedActiveSession();
console.log(JSON.stringify({{first, scoped, second}}));
"""
    out = _run(script)
    assert out["first"] == "upgraded-session"
    assert out["scoped"] == "upgraded-session", (
        "the legacy value must be adopted into the tab's scoped key on first read"
    )
    assert out["second"] == "upgraded-session", (
        "after adoption, the shared slot must no longer steer this tab"
    )


def test_upgrade_migrates_populated_legacy_inflight_under_new_tab_stamp():
    """Must-fix 4 (ui.js:9498): both inflight readers use suffixed keys
    exclusively, so a populated pre-upgrade `hermes-webui-inflight` /
    `hermes-webui-inflight-state` produced empty scoped reads and mid-stream
    recovery was lost on upgrade. A valid legacy payload must be migrated once
    under the adopting tab's stamp, then the legacy entries removed."""
    script = f"""
{_HARNESS}
{_helpers()}
// Pre-upgrade mid-stream state, written by the old build minutes ago.
localStorage.setItem(INFLIGHT_KEY_BASE, JSON.stringify({{sid:'sess-1', streamId:'stream-1', ts:__now-60_000}}));
localStorage.setItem(INFLIGHT_STATE_KEY_BASE, JSON.stringify({{
  'sess-1': {{streamId:'stream-1', messages:[{{role:'user',content:'hi'}}], updated_at:__now-60_000}},
}}));
const tab = makeTab();
useTab(tab);
const restored = loadInflightState('sess-1', 'stream-1');
const marker = JSON.parse(localStorage.getItem(_inflightKey()) || 'null');
const legacyMarkerGone = localStorage.getItem(INFLIGHT_KEY_BASE) === null;
const legacyStateGone = localStorage.getItem(INFLIGHT_STATE_KEY_BASE) === null;
console.log(JSON.stringify({{
  restoredStream: restored && restored.streamId,
  restoredTab: restored && restored.tabId,
  tabId: _hermesTabId(),
  markerSid: marker && marker.sid,
  legacyMarkerGone, legacyStateGone,
}}));
"""
    out = _run(script)
    assert out["restoredStream"] == "stream-1", (
        "a populated legacy inflight-state map must be readable after upgrade"
    )
    assert out["restoredTab"] == out["tabId"], (
        "migrated entries must be re-stamped under the adopting tab's id"
    )
    assert out["markerSid"] == "sess-1", (
        "the legacy reconnect marker must be migrated under the scoped key"
    )
    assert out["legacyMarkerGone"] and out["legacyStateGone"], (
        "legacy entries must be removed after the one-shot migration"
    )


def test_stale_or_malformed_legacy_inflight_is_invalidated_not_migrated():
    """Must-fix 4 (validation): the legacy payload is validated (shape + age)
    before adoption; stale/corrupt entries are removed without migrating, and
    existing scoped state is never overwritten."""
    script = f"""
{_HARNESS}
{_helpers()}
// Stale marker (2 hours old) + corrupt state map.
localStorage.setItem(INFLIGHT_KEY_BASE, JSON.stringify({{sid:'old-sess', streamId:'old-stream', ts:__now-2*60*60*1000}}));
localStorage.setItem(INFLIGHT_STATE_KEY_BASE, 'not json at all');
const tab = makeTab();
useTab(tab);
_migrateLegacyInflight();
const scopedMarker = localStorage.getItem(_inflightKey());
const scopedState = localStorage.getItem(_inflightStateKey());
const legacyGone = localStorage.getItem(INFLIGHT_KEY_BASE) === null
  && localStorage.getItem(INFLIGHT_STATE_KEY_BASE) === null;
// Second scenario: a tab that already HAS scoped state keeps it.
const tab2 = makeTab();
useTab(tab2);
localStorage.setItem(INFLIGHT_KEY_BASE, JSON.stringify({{sid:'legacy-sess', streamId:'legacy-stream', ts:__now-1000}}));
localStorage.setItem(_inflightKey(), JSON.stringify({{sid:'own-sess', streamId:'own-stream', ts:__now-500}}));
_migrateLegacyInflight();
const kept = JSON.parse(localStorage.getItem(_inflightKey()));
console.log(JSON.stringify({{
  scopedMarker, scopedState, legacyGone,
  keptSid: kept && kept.sid,
  legacyGone2: localStorage.getItem(INFLIGHT_KEY_BASE) === null,
}}));
"""
    out = _run(script)
    assert out["scopedMarker"] is None, "a stale legacy marker must not be migrated"
    assert out["scopedState"] is None, "a corrupt legacy state map must not be migrated"
    assert out["legacyGone"], "invalid legacy entries must still be invalidated (removed)"
    assert out["keptSid"] == "own-sess", (
        "migration must never overwrite a tab's existing scoped marker"
    )
    assert out["legacyGone2"], "the legacy slot is consumed exactly once either way"


def test_inflight_readers_run_the_migration_before_scoped_reads():
    """Wiring: both readers (the boot marker check and the state-map read) must
    invoke the one-shot migration BEFORE their scoped read, or a populated
    legacy payload stays invisible exactly as the re-gate reproduced."""
    check_body = _function_body(UI_SRC, "async function checkInflightOnBoot")
    assert "_migrateLegacyInflight()" in check_body, (
        "checkInflightOnBoot must migrate legacy inflight before reading"
    )
    assert check_body.index("_migrateLegacyInflight()") < check_body.index(
        "localStorage.getItem(_inflightKey())"
    ), "migration must precede the scoped marker read"
    read_body = _function_body(UI_SRC, "function _readInflightStateMap")
    assert "_migrateLegacyInflight()" in read_body, (
        "_readInflightStateMap must migrate legacy inflight before reading"
    )
    assert read_body.index("_migrateLegacyInflight()") < read_body.index(
        "localStorage.getItem(_inflightStateKey())"
    ), "migration must precede the scoped state read"


def test_simultaneous_same_id_documents_remint_before_scoped_access():
    """Two documents inherit one copied id and boot concurrently. The registry
    hook reproduces the stale-read schedule in the rejected CAS implementation;
    the sessionStorage hook applies the equivalent pause to the fresh-id design.
    Both documents must establish distinct authority before scoped access."""
    script = f"""
{_HARNESS}
{_helpers()}
const inherited = 'copied-tab-id';
const tabA = makeTab();
const tabB = makeTab();
tabA.store.setItem(TAB_ID_KEY, inherited);
tabB.store.setItem(TAB_ID_KEY, inherited);

// Deterministic collision schedule: A has already received the registry value
// when this hook runs. B initializes completely before that value is returned
// to A, so A resumes with a stale "free" view.
const rawGetItem = localStorage.getItem;
const rawSessionSetItem = sessionStorage.setItem;
let interleaved = false;
let idB = null;
localStorage.getItem = (key) => {{
  const value = rawGetItem(key);
  // This raw legacy key intentionally keeps the test discriminating against the
  // rejected read/write/verify localStorage arbitration implementation.
  if(!interleaved && key === 'hermes-webui-tab-claims' && __tab === tabA){{
    interleaved = true;
    useTab(tabB);
    idB = _hermesTabId();
    useTab(tabA);
  }}
  return value;
}};
sessionStorage.setItem = (key, value) => {{
  rawSessionSetItem(key, value);
  if(!interleaved && key === TAB_ID_KEY && __tab === tabA){{
    interleaved = true;
    useTab(tabB);
    idB = _hermesTabId();
    useTab(tabA);
  }}
}};

useTab(tabA);
const idA = _hermesTabId();
if(!idB){{ useTab(tabB); idB = _hermesTabId(); }}
useTab(tabA); _rememberActiveSession('session-A');
useTab(tabB); _rememberActiveSession('session-B');
useTab(tabA); const restoredA = _rememberedActiveSession();
console.log(JSON.stringify({{idA, idB, inherited, interleaved, restoredA}}));
"""
    out = _run(script)
    assert out["interleaved"], "the probe must exercise a concurrent boot schedule"
    assert out["idA"] != out["idB"], (
        "simultaneous documents must not both accept one inherited identity"
    )
    assert out["restoredA"] == "session-A", (
        "the losing document must remint before any scoped session access"
    )
