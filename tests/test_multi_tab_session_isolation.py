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
    "TAB_ID_CLAIM_KEY",
    "_TAB_CLAIM_TTL_MS",
    "_TAB_CLAIM_HEARTBEAT_MS",
    "_TAB_SEEN_TTL_MS",
    "TAB_ID_SEEN_KEY",
    "INFLIGHT_KEY_BASE",
    "INFLIGHT_STATE_KEY_BASE",
    "ACTIVE_SESSION_KEY_LEGACY",
)

_HELPER_FUNCS = (
    "function _newTabId",
    "function _updateTabRegistry",
    "function _readTabClaims",
    "function _claimTabId",
    "function _readTabSeen",
    "function _touchTabSeen",
    "function _releaseTabId",
    "function _gcOrphanTabKeys",
    "function _hermesTabId",
    "function _inflightKey",
    "function _inflightStateKey",
    "function _activeSessionKey",
    "function _rememberActiveSession",
    "function _rememberedActiveSession",
    "function _forgetActiveSession",
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
function makeTab(){ return { store: makeStorage(), win: { addEventListener(){}, crypto: undefined } }; }

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


def test_plain_reload_keeps_the_same_tab_id():
    """A reload must NOT re-mint, otherwise the tab loses its own in-flight
    stream recovery — the release-on-pagehide path exists for this."""
    script = f"""
{_HARNESS}
{_helpers()}
const tab = makeTab();
useTab(tab); const before = _hermesTabId();
_releaseTabId();                     // pagehide fires before unload
// Reload: sessionStorage survives, the window (and its cached id) does not.
const reloaded = {{ store: tab.store, win: {{ addEventListener(){{}}, crypto: undefined }} }};
useTab(reloaded); const after = _hermesTabId();
console.log(JSON.stringify({{before, after}}));
"""
    out = _run(script)
    assert out["before"] == out["after"], "a reload must keep the tab's identity"


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
const seen = JSON.parse(localStorage.getItem(TAB_ID_SEEN_KEY) || '{{}}');
seen['ghost'] = __now - (_TAB_SEEN_TTL_MS * 2);
localStorage.setItem(TAB_ID_SEEN_KEY, JSON.stringify(seen));
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
useTab(tab); _hermesTabId();   // this tab is live and stamps itself as seen
// Another tab, last seen a second ago (e.g. a background window).
localStorage.setItem(ACTIVE_SESSION_KEY_LEGACY + '::recent', 'still-used');
const seen = JSON.parse(localStorage.getItem(TAB_ID_SEEN_KEY) || '{{}}');
seen['recent'] = __now - 1000;
localStorage.setItem(TAB_ID_SEEN_KEY, JSON.stringify(seen));
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


def test_registry_writes_do_not_clobber_a_concurrent_tab():
    """Claim/seen updates are read-modify-write on one shared key. A tab that
    read the registry before a second tab registered must not erase it."""
    script = f"""
{_HARNESS}
{_helpers()}
const tabA = makeTab();
const tabB = makeTab();
// Tab A boots and registers.
useTab(tabA); const idA = _hermesTabId();
// Tab B boots and registers (interleaved with A's next heartbeat below).
useTab(tabB); const idB = _hermesTabId();
// A heartbeats using a STALE view of the registry (it re-reads internally).
useTab(tabA); _touchTabSeen(idA);
const claims = JSON.parse(localStorage.getItem(TAB_ID_CLAIM_KEY) || '{{}}');
const seen = JSON.parse(localStorage.getItem(TAB_ID_SEEN_KEY) || '{{}}');
// Releasing A must not take B's claim with it.
useTab(tabA); _releaseTabId();
const afterRelease = JSON.parse(localStorage.getItem(TAB_ID_CLAIM_KEY) || '{{}}');
console.log(JSON.stringify({{
  bothClaimed: !!claims[idA] && !!claims[idB],
  bothSeen: !!seen[idA] && !!seen[idB],
  bSurvivedRelease: !!afterRelease[idB],
  aReleased: !afterRelease[idA],
}}));
"""
    out = _run(script)
    assert out["bothClaimed"], "a concurrent tab's claim must not be clobbered"
    assert out["bothSeen"], "a concurrent tab's seen entry must not be clobbered"
    assert out["bSurvivedRelease"], "releasing one tab must not drop another's claim"
    assert out["aReleased"], "the releasing tab's own claim must be removed"


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
