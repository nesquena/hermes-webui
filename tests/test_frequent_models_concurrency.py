"""Node-driven concurrency/identity regressions for the frequent-models helpers.

These drive the REAL helper block sliced out of ``static/ui.js`` — not a
reimplementation — so they run in CI without a browser:

1. ``@openrouter:openrouter/auto`` and the server's canonical
   ``openrouter/auto`` are the same frequent-model identity. The old key
   function stripped ``<provider>/`` as if it were a routing prefix, which
   turned the canonical form into ``auto`` and made a saved pin invisible.
2. Overlapping pin saves: request-start order is not server-commit order. The
   cache must match the server's final state after both settle.
3. A GET that started during a POST and returns an older snapshot must not
   discard the successful save.
4. Both arrival orders of that overlap keep the pin: the GET is only started
   AFTER the POST is confirmed on the wire (the old test started the GET
   before the POST, so it never exercised the race), and the stale GET is
   settled first or last.  A save is judged by its OWN sequence, never the
   shared GET sequence; a GET drops its result when a save was pending when it
   started or when the save epoch moved while it was in flight.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
UI = ROOT / "static" / "ui.js"

BLOCK_START = "// ── Frequently-used models + server-backed pin state"
BLOCK_END = "function renderModelDropdown(){"

PRELUDE = """
const fs = require('fs');
const ui = fs.readFileSync(__UI__, 'utf8');
const block = ui.split(__BLOCK_START__)[1].split(__BLOCK_END__)[0];
if (!block || block.length < 1000) throw new Error('helper block not found');
// The split leaves the rest of the banner line (" ─────…"), which is not
// executable: drop through the first newline.
const body = block.slice(block.indexOf(String.fromCharCode(10)) + 1);
if (!body || body.length < 1000) throw new Error('helper body not found');
const S = {activeProfile: 'default'};
const t = (k) => k;
const showToast = () => {};
const $ = () => ({});
const window = {};
let apiImpl = null;
function api(path, opts) { return apiImpl(path, opts); }
// The block declares its cache with `let`; an eval scope is not visible from
// here, so export accessors from INSIDE the same eval.
eval(body + `
globalThis.__hooks = {
  get cache() { return _modelFrequentCache; },
  set cache(v) { _modelFrequentCache = v; },
  key: _frequentModelKey,
  load: _loadModelFrequentPrefs,
  save: _setModelPinned,
};
`);
"""

CASE_KEY = """
const out = {
  same: __hooks.key('@openrouter:openrouter/auto', 'openrouter')
        === __hooks.key('openrouter/auto', 'openrouter'),
  plainSame: __hooks.key('@p:m1', 'p') === __hooks.key('m1', 'p'),
  distinctProviders: __hooks.key('m1', 'p') !== __hooks.key('m1', 'q'),
};
console.log(JSON.stringify(out));
"""

CASE_OVERLAP = """
(async () => {
  __hooks.cache = {profile: 'default', pins: [], usage: []};
  const server = [];
  apiImpl = async (path, opts) => {
    const body = JSON.parse(opts.body);
    // First-started request lands last: a real transport can reorder.
    await new Promise((r) => setTimeout(r, body.model === 'a' ? 40 : 5));
    if (body.pinned) server.push(body.model);
    return {profile: 'default',
            pins: server.map((m) => ({model: m, provider: 'p'})),
            usage: []};
  };
  const a = __hooks.save('a', 'p', true);
  const b = __hooks.save('b', 'p', true);
  await Promise.all([a, b]);
  const out = {server,
               pins: (__hooks.cache && __hooks.cache.pins || []).map((x) => x.model)};
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""

CASE_GET_RACE = """
(async () => {
  __hooks.cache = {profile: 'default', pins: [], usage: []};
  const server = [];
  apiImpl = async (path, opts) => {
    const method = (opts && opts.method) || 'GET';
    if (method === 'POST') {
      const body = JSON.parse(opts.body);
      await new Promise((r) => setTimeout(r, 30));
      if (body.pinned) server.push(body.model);
      return {profile: 'default',
              pins: server.map((m) => ({model: m, provider: 'p'})),
              usage: []};
    }
    // GET: snapshot captured before the save commits, returns first.
    const snapshot = server.map((m) => ({model: m, provider: 'p'}));
    await new Promise((r) => setTimeout(r, 5));
    return {profile: 'default', pins: snapshot, usage: []};
  };
  const save = __hooks.save('a', 'p', true);
  const load = __hooks.load();
  await Promise.all([save, load]);
  const out = {server,
               pins: (__hooks.cache && __hooks.cache.pins || []).map((x) => x.model)};
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def _run(case_js: str) -> dict:
    assert NODE is not None, "node is required for the frequent-model helper driver"
    script = (
        PRELUDE.replace("__UI__", json.dumps(str(UI)))
        .replace("__BLOCK_START__", json.dumps(BLOCK_START))
        .replace("__BLOCK_END__", json.dumps(BLOCK_END))
        + case_js
    )
    env = dict(os.environ)
    env.pop("NODE_CHANNEL_FD", None)
    env.pop("NODE_OPTIONS", None)
    env.pop("NODE_CHANNEL_SERIALIZATION_MODE", None)
    proc = subprocess.run(
        [NODE, "-e", script], cwd=str(ROOT), text=True, capture_output=True, check=False, env=env
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node is required for the frequent-model helper driver")
def test_namespace_model_key_is_not_stripped_twice():
    out = _run(CASE_KEY)
    assert out["same"] is True, out
    assert out["plainSame"] is True, out
    assert out["distinctProviders"] is True, out


@pytest.mark.skipif(NODE is None, reason="node is required for the frequent-model helper driver")
def test_overlapping_pin_saves_match_the_server_state():
    out = _run(CASE_OVERLAP)
    assert out["pins"] == out["server"], out


@pytest.mark.skipif(NODE is None, reason="node is required for the frequent-model helper driver")
def test_get_during_pending_save_cannot_discard_it():
    out = _run(CASE_GET_RACE)
    assert out["pins"] == ["a"], out


# Both orders of the REAL overlap: the refresh handler only runs once the POST
# is confirmed on the wire, so the GET starts while the save is pending and
# reads a snapshot taken before the commit.  Whichever response lands first,
# the successful pin must survive in the cache (maintainer finding 2b).
_ORDER_GET_FIRST = (
    "releaseGet(); await new Promise((r) => setTimeout(r, 0)); releasePost();"
)
_ORDER_POST_FIRST = (
    "releasePost(); await new Promise((r) => setTimeout(r, 0)); releaseGet();"
)


def _case_get_during_pending_save(order: str) -> str:
    return """
(async () => {
  __hooks.cache = {profile: 'default', pins: [], usage: []};
  const server = [];
  let onWire; const postOnWire = new Promise((r) => { onWire = r; });
  let releasePost = null;
  let releaseGet = null;
  apiImpl = (path, opts) => {
    const method = (opts && opts.method) || 'GET';
    if (method === 'POST') {
      const body = JSON.parse(opts.body);
      onWire();  // the POST is on the wire before the refresh is kicked
      return new Promise((resolve) => {
        releasePost = () => {
          if (body.pinned) server.push(body.model);
          resolve({profile: 'default',
                  pins: server.map((m) => ({model: m, provider: 'p'})),
                  usage: []});
        };
      });
    }
    // GET: the snapshot is read BEFORE the save commits (stale), and the
    // response is released only when the case settles it.
    const snapshot = server.map((m) => ({model: m, provider: 'p'}));
    return new Promise((resolve) => {
      releaseGet = () => resolve({profile: 'default', pins: snapshot, usage: []});
    });
  };
  const save = __hooks.save('a', 'p', true);
  await postOnWire;            // the save is pending; only now kick the refresh
  const load = __hooks.load(); // the GET starts while the POST is pending
  await new Promise((r) => setTimeout(r, 0));
  __ORDER__;
  await Promise.all([save, load]);
  const out = {server,
               pins: (__hooks.cache && __hooks.cache.pins || []).map((x) => x.model)};
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
""".replace("__ORDER__", order)


@pytest.mark.skipif(NODE is None, reason="node is required for the frequent-model helper driver")
def test_get_started_during_a_pending_save_returns_first_and_keeps_the_pin():
    out = _run(_case_get_during_pending_save(_ORDER_GET_FIRST))
    assert out["server"] == ["a"], out
    assert out["pins"] == ["a"], out


@pytest.mark.skipif(NODE is None, reason="node is required for the frequent-model helper driver")
def test_get_started_during_a_pending_save_returns_last_and_keeps_the_pin():
    out = _run(_case_get_during_pending_save(_ORDER_POST_FIRST))
    assert out["server"] == ["a"], out
    assert out["pins"] == ["a"], out


CASE_NO_BODY_POST = """
(async () => {
  __hooks.cache = {profile: 'default', pins: [], usage: []};
  const server = [];
  let getCount = 0;
  apiImpl = (path, opts) => {
    const method = (opts && opts.method) || 'GET';
    if (method === 'POST') {
      const body = JSON.parse(opts.body);
      if (body.pinned) server.push(body.model);
      return;  // no body: the save must re-read authoritative state
    }
    getCount += 1;
    return {profile: 'default',
            pins: server.map((m) => ({model: m, provider: 'p'})),
            usage: []};
  };
  const ok = await __hooks.save('a', 'p', true);
  const out = {ok, getCount, server,
               pins: (__hooks.cache && __hooks.cache.pins || []).map((x) => x.model)};
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


@pytest.mark.skipif(NODE is None, reason="node is required for the frequent-model helper driver")
def test_body_less_post_still_performs_its_authoritative_read():
    out = _run(CASE_NO_BODY_POST)
    assert out["ok"] is True, out
    assert out["getCount"] == 1, out
    assert out["pins"] == ["a"], out


CASE_NO_BODY_WITH_REFRESH = """
(async () => {
  __hooks.cache = {profile: 'default', pins: [], usage: []};
  const pin = {model: 'a', provider: 'p'};
  let server = __PINNING__ ? [] : [pin];
  __hooks.cache.pins = server.slice();
  let postOnWire;
  const postStarted = new Promise(resolve => { postOnWire = resolve; });
  let finishPost, finishStale;
  let reads = 0;
  const snapshot = () => ({profile: 'default', pins: server.slice(), usage: []});
  apiImpl = (_path, opts) => {
    if(opts && opts.method === 'POST') {
      const request = JSON.parse(opts.body);
      return new Promise(resolve => {
        finishPost = () => { server = request.pinned ? [pin] : []; resolve(); };
        postOnWire();
      });
    }
    const read = snapshot();
    if(++reads === 1) return new Promise(resolve => { finishStale = () => resolve(read); });
    return Promise.resolve(read);
  };
  const save = __hooks.save('a', 'p', __PINNING__);
  await postStarted;
  const stale = __hooks.load();
  finishPost();
  const ok = await save;
  finishStale();
  await stale;
  console.log(JSON.stringify({ok, reads, actual: __hooks.cache.pins, expected: server}));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


@pytest.mark.skipif(NODE is None, reason="node is required for the frequent-model helper driver")
@pytest.mark.parametrize("pinned", [True, False])
def test_body_less_save_refreshes_before_a_stale_get_returns(pinned):
    out = _run(CASE_NO_BODY_WITH_REFRESH.replace("__PINNING__", json.dumps(pinned)))
    assert out["actual"] == out["expected"], out
    assert out["ok"] is True, out
    assert out["reads"] == 2, out


CASE_PROFILE_SWITCH = """
(async () => {
  __hooks.cache = {profile: 'alpha', pins: [], usage: []};
  S.activeProfile = 'alpha';
  const calls = [];
  apiImpl = async (path, opts) => {
    calls.push(JSON.parse(opts.body));
    return {profile: 'alpha', pins: [], usage: []};
  };
  // Queue the save while alpha is active, then switch before it can start.
  const pending = __hooks.save('m1', 'p', true);
  S.activeProfile = 'beta';
  const result = await pending;
  console.log(JSON.stringify({calls, result}));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


@pytest.mark.skipif(NODE is None, reason="node is required for the frequent-model helper driver")
def test_a_queued_pin_save_is_cancelled_when_the_profile_switches():
    """#8622: the click's owner decides, not whatever profile is live at send.

    The POST authenticates with the cookie of whichever profile is active when
    it leaves, so a save queued in profile A and started after a switch to B
    would rewrite B's pins.
    """
    out = _run(CASE_PROFILE_SWITCH)
    assert out["calls"] == [], (
        "the queued save posted under the NEW profile's cookie: " + json.dumps(out)
    )
    assert out["result"] is None, out
