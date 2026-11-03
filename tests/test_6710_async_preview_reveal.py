"""Regression tests for #6710 — an async preview must not report success before
its source has actually loaded.

The panel-dismissal guard (``test_6710_panel_dismiss_and_artifact_open.py``)
trusts ``openFile()``'s return value: only a literal ``true`` clears the
dismissal and promotes the panel. Image / media / PDF / HTML previews hand a URL
to a browser element and let the BROWSER fetch it, so the old code assigned
``src``/``innerHTML`` and fell straight through to ``return true``. When that
fetch failed (expired escape grant → 403, file removed between the ``/api/list``
check and the raw request → 404, oversized/binary → attachment), the panel was
promoted onto a broken preview and the user's dismissal was already erased.

These tests cover both halves:
  - Node-VM tests run the REAL helper bodies (``_awaitElementLoad``,
    ``_awaitMediaReady``, ``_workspaceRawReachable``) against element doubles
    whose load outcome the test controls.
  - A Playwright test drives the real page against the isolated test server and
    asserts a broken image fails closed.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_JS_PATH = REPO_ROOT / "static" / "workspace.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _shipped_const_line(name: str) -> str:
    """Return the shipped `const NAME = ...;` line verbatim.

    The preview helpers read shared constants; the harness must run the REAL
    value rather than a copy, so the declaration is lifted from the source.
    """
    src = _read(WORKSPACE_JS_PATH)
    match = re.search(r"^const %s\s*=.*?;$" % re.escape(name), src, re.M)
    assert match, f"const {name} not found in static/workspace.js"
    return match.group(0)


def _helper(name: str) -> str:
    body = _read(WORKSPACE_JS_PATH)
    # `_workspaceRawReachable` is async; extract_function() defaults to the
    # `function` prefix and would drop the `async` keyword, producing a body
    # whose `await` is a syntax error.
    prefix = "async function" if f"async function {name}(" in body else "function"
    return extract_function(body, name, prefix=prefix)


_HARNESS = r"""
const helpers = __HELPERS__;
const params = __PARAMS__;

__CONSTS__

let statuses = [];
function setStatus(s){ statuses.push(s); }
function t(k){ return k; }
let apiCalls = [];
async function api(url, opts){
  apiCalls.push({url: url, opts: opts || null});
  if(params.apiFails){ const e = new Error('nope'); e.status = params.apiStatus || 404; throw e; }
  if(params.probeTimeout){ const e = new Error('Request timed out. Please try again.'); e.name = 'TimeoutError'; e.timeout = true; throw e; }
  return '';
}
// The 401 path in the real api() returns undefined instead of throwing.
async function apiUndefined(url, opts){ apiCalls.push({url: url, opts: opts || null}); return undefined; }

// ── element doubles ─────────────────────────────────────────────────────────
function makeElement(kind){
  const listeners = {};
  const el = {
    kind: kind,
    complete: false,
    naturalWidth: 0,
    readyState: 0,
    assigned: null,
    addEventListener(ev, fn){ (listeners[ev] = listeners[ev] || []).push(fn); },
    removeEventListener(ev, fn){
      if(listeners[ev]) listeners[ev] = listeners[ev].filter(f => f !== fn);
    },
    emit(ev){ (listeners[ev] || []).slice().forEach(f => f()); },
    listenerCount(){ return Object.keys(listeners).reduce((n, k) => n + listeners[k].length, 0); },
  };
  return el;
}

function makeWrap(el){
  return {
    innerHTML: '',
    querySelector(sel){ return el && sel === el.kind ? el : null; },
  };
}

(async () => {
  const out = {};
  eval(helpers._awaitElementLoad);
  eval(helpers._awaitMediaReady);
  eval(helpers._mountMediaPlayer);
  eval(helpers._workspaceRawReachable);

  // ── image element: the outcome the browser reports ────────────────────────
  // A cache hit makes the browser flip `complete` synchronously as the src is
  // assigned, so the assignment callback simulates that state — that is the
  // case the synchronous `el.complete` check in the shipped helper exists for.
  const img = makeElement('img');
  const imgAssign = () => {
    img.assigned = '/api/file/raw?path=x.png';
    if(params.imageOutcome === 'cached-ok'){ img.complete = true; img.naturalWidth = 10; }
    if(params.imageOutcome === 'cached-broken'){ img.complete = true; img.naturalWidth = 0; }
  };
  const pending = _awaitElementLoad(img, imgAssign, 'image_load_failed');
  out.imageAssignedBeforeSettle = img.assigned;
  await new Promise(r => setImmediate(r));
  if(params.imageOutcome === 'load'){
    img.naturalWidth = 10;
    img.emit('load');
  } else if(params.imageOutcome === 'error'){
    img.emit('error');
  } else if(params.imageOutcome === 'never'){
    // settle nothing — the caller must not have resolved yet
  }
  out.imageResult = params.imageOutcome === 'never' ? null : await Promise.resolve(pending)
      .then(v => v, e => 'rejected:' + e.message);
  out.imageListenersLeft = img.listenerCount();
  out.imageStatuses = statuses.slice();

  // ── element with no event plumbing (older test double / harness) ──────────
  statuses = [];
  const bare = { assigned: null };
  out.bareResult = await _awaitElementLoad(bare, () => { bare.assigned = 'x'; }, 'image_load_failed');
  out.bareAssigned = bare.assigned;
  out.bareStatuses = statuses.slice();

  // ── media element ────────────────────────────────────────────────────────
  statuses = [];
  const media = makeElement('video');
  const wrap = makeWrap(media);
  const mounted = _mountMediaPlayer(wrap, '<video src="x"></video>', 'video');
  out.mediaMounted = !!mounted;
  out.mediaInnerHTML = wrap.innerHTML;
  // Metadata already present before the listener attaches (the "already
  // buffered" case) is state, not an event.
  if(params.mediaOutcome === 'already-ready') media.readyState = 2;
  const mediaPending = _awaitMediaReady(media);
  await new Promise(r => setImmediate(r));
  if(params.mediaOutcome === 'loadedmetadata'){
    media.readyState = 1;
    media.emit('loadedmetadata');
  } else if(params.mediaOutcome === 'error'){
    media.emit('error');
  }
  out.mediaResult = params.mediaOutcome === 'never'
      ? 'pending' : await mediaPending;
  out.mediaListenersLeft = media.listenerCount();
  out.mediaStatuses = statuses.slice();

  // ── wrap that cannot be inspected ────────────────────────────────────────
  out.mountUninspectable = _mountMediaPlayer({ innerHTML: '' }, '<video></video>', 'video');

  // ── raw reachability probe ───────────────────────────────────────────────
  statuses = [];
  apiCalls = [];
  out.probeOk = await _workspaceRawReachable('/api/file/raw?session_id=s1&path=a.pdf');
  out.probeCalls = apiCalls.slice();
  out.probeStatuses = statuses.slice();

  console.log(JSON.stringify(out));
})();
"""

# A stalled response fires neither `load` nor `error`. The bound is what keeps
# `openFile()` (and therefore `openArtifactPath()`) from staying pending
# forever, so the probe drives it directly: `setTimeout` is shimmed to capture
# the callback instead of waiting the real bound out. The test asserts on the
# PATH (does a bound exist and does firing it fail closed), never on the number.
_STALL_HARNESS = r"""
const helpers = __HELPERS__;
const params = __PARAMS__;

__CONSTS__

const captured = [];
const realSetTimeout = setTimeout;
setTimeout = (fn, _ms) => { captured.push(fn); return captured.length; };
clearTimeout = () => {};

let statuses = [];
function setStatus(s){ statuses.push(s); }
function t(k){ return k; }

const listeners = {};
const el = {
  complete: false, naturalWidth: 0, assigned: null,
  addEventListener(ev, fn){ (listeners[ev] = listeners[ev] || []).push(fn); },
  removeEventListener(ev, fn){ if(listeners[ev]) listeners[ev] = listeners[ev].filter(f => f !== fn); },
  emit(ev){ (listeners[ev] || []).slice().forEach(f => f()); },
  listenerCount(){ return Object.keys(listeners).reduce((n, k) => n + listeners[k].length, 0); },
};

(async () => {
  const out = {};
  eval(helpers._awaitElementLoad);

  const pending = _awaitElementLoad(el, () => { el.assigned = 'x'; }, 'image_load_failed');
  await new Promise(r => setImmediate(r));
  out.assigned = el.assigned;
  out.timersArmed = captured.length;

  if(params.fireBound){ captured.slice().forEach(fn => fn()); }

  let settled = false;
  out.result = await Promise.race([
    Promise.resolve(pending).then(v => { settled = true; return v; }),
    new Promise(r => realSetTimeout(() => r('UNSETTLED'), 50)),
  ]);
  out.settled = settled;
  out.statuses = statuses.slice();

  // After the bound released the wait, emit whatever arrives late and record
  // what — if anything — was reported, plus whether the pair of late handlers
  // was retired (Greptile P2: only the firing handler used to be removed).
  statuses = [];
  if(params.fireBound && params.late === 'error') el.emit('error');
  if(params.fireBound && params.late === 'load') el.emit('load');
  await new Promise(r => setImmediate(r));
  out.lateStatuses = statuses.slice();
  out.listenersLeft = el.listenerCount();

  // Repeated stalled previews on the SAME shared element must not accumulate
  // handlers (Greptile P2: the paired late handler was never removed).
  if(params.repeat){
    for(let i = 0; i < params.repeat; i++){
      const p = _awaitElementLoad(el, () => {}, 'image_load_failed');
      await new Promise(r => setImmediate(r));
      captured.slice().forEach(fn => fn());   // fire the anti-hang bound
      await p;
      // the stalled request finally fails: with the old code this removed only
      // the error handler and left the paired load handler behind for good.
      el.emit('error');
      await new Promise(r => setImmediate(r));
    }
    out.repeatListenersLeft = el.listenerCount();
  }

  // A response that later succeeds must still be honoured while waiting.
  statuses = [];
  const el2 = Object.assign({}, el, {
    complete: false, naturalWidth: 0,
    addEventListener(ev, fn){ (listeners['two_' + ev] = listeners['two_' + ev] || []).push(fn); },
    removeEventListener(ev, fn){ if(listeners['two_' + ev]) listeners['two_' + ev] = listeners['two_' + ev].filter(f => f !== fn); },
  });
  const pending2 = _awaitElementLoad(el2, () => {}, 'image_load_failed');
  await new Promise(r => setImmediate(r));
  el2.naturalWidth = 10;
  (listeners['two_load'] || []).slice().forEach(f => f());
  out.lateLoadResult = await pending2;

  console.log(JSON.stringify(out));
})();
"""


def _run_harness(*, image_outcome="load", media_outcome="loadedmetadata",
                 api_fails=False, api_status=404, probe_timeout=False) -> dict:
    helpers = {
        "_awaitElementLoad": _helper("_awaitElementLoad"),
        "_awaitMediaReady": _helper("_awaitMediaReady"),
        "_mountMediaPlayer": _helper("_mountMediaPlayer"),
        "_workspaceRawReachable": _helper("_workspaceRawReachable"),
    }
    payload = {
        "imageOutcome": image_outcome,
        "mediaOutcome": media_outcome,
        "apiFails": api_fails,
        "apiStatus": api_status,
        "probeTimeout": probe_timeout,
    }
    js = _HARNESS.replace("__HELPERS__", json.dumps(helpers)).replace(
        "__PARAMS__", json.dumps(payload)
    ).replace("__CONSTS__", _shipped_const_line("_PREVIEW_LOAD_TIMEOUT_MS"))
    proc = subprocess.run(
        [NODE, "-e", js], capture_output=True, text=True, cwd=REPO_ROOT, timeout=30
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_stall_harness(*, fire_bound: bool, late: str = "") -> dict:
    js = _STALL_HARNESS.replace(
        "__HELPERS__", json.dumps({"_awaitElementLoad": _helper("_awaitElementLoad")})
    ).replace("__PARAMS__", json.dumps({"fireBound": fire_bound, "late": late})).replace(
        "__CONSTS__", _shipped_const_line("_PREVIEW_LOAD_TIMEOUT_MS")
    )
    proc = subprocess.run(
        [NODE, "-e", js], capture_output=True, text=True, cwd=REPO_ROOT, timeout=30
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── image: the load outcome must decide the result ──────────────────────────


def test_image_load_success_reports_success():
    out = _run_harness(image_outcome="load")
    assert out["imageResult"] is True, out


def test_image_load_error_reports_failure():
    """The Greptile finding: a broken image must not read as a reveal."""
    out = _run_harness(image_outcome="error")
    assert out["imageResult"] is False, out
    assert out["imageStatuses"] == ["image_load_failed"], out


def test_image_assignment_happens_before_awaiting_the_outcome():
    """The src must still be assigned (that is what starts the load) — the fix
    waits for the outcome, it does not skip the load."""
    out = _run_harness(image_outcome="error")
    assert out["imageAssignedBeforeSettle"], out


def test_cached_image_settles_without_waiting_for_an_event():
    """A cache hit can already be `complete` when the handler attaches — the
    load/error events may never fire, so the current state must decide."""
    out = _run_harness(image_outcome="cached-ok")
    assert out["imageResult"] is True, out
    broken = _run_harness(image_outcome="cached-broken")
    assert broken["imageResult"] is False, broken


def test_listeners_are_removed_once_settled():
    """A leak here would stack handlers on every open."""
    ok = _run_harness(image_outcome="load")
    assert ok["imageListenersLeft"] == 0, ok
    fail = _run_harness(image_outcome="error")
    assert fail["imageListenersLeft"] == 0, fail
    media = _run_harness(media_outcome="error")
    assert media["mediaListenersLeft"] == 0, media


def test_element_without_event_plumbing_keeps_the_old_behaviour():
    """Node harnesses and older doubles have no addEventListener; the fix must
    not hang or throw there — it falls back to the previous fire-and-forget."""
    out = _run_harness()
    assert out["bareResult"] is True, out
    assert out["bareAssigned"] == "x", out


# ── media: metadata is the first readable point ─────────────────────────────


def test_media_metadata_reports_success():
    out = _run_harness(media_outcome="loadedmetadata")
    assert out["mediaResult"] is True, out
    assert out["mediaInnerHTML"] == '<video src="x"></video>', out


def test_media_error_reports_failure():
    out = _run_harness(media_outcome="error")
    assert out["mediaResult"] is False, out
    assert out["mediaStatuses"] == ["file_open_failed"], out


def test_media_already_buffered_does_not_wait():
    """readyState >= 1 means metadata already arrived before the listener."""
    out = _run_harness(media_outcome="already-ready")
    assert out["mediaResult"] is True, out


def test_mount_returns_null_for_uninspectable_wrap():
    out = _run_harness()
    assert out["mountUninspectable"] is None, out


# ── raw probe: iframes cannot report load failure themselves ────────────────


def test_raw_probe_uses_a_one_byte_range():
    """A large image/PDF must not be downloaded twice just to check it."""
    out = _run_harness()
    assert out["probeOk"] is True, out
    assert out["probeCalls"], out
    opts = out["probeCalls"][0]["opts"] or {}
    assert opts.get("headers") == {"Range": "bytes=0-0"}, opts
    assert opts.get("retries") == 0, opts


def test_raw_probe_reports_failure_on_error():
    out = _run_harness(api_fails=True, api_status=403)
    assert out["probeOk"] is False, out
    assert out["probeStatuses"] == ["file_open_failed"], out


def test_raw_probe_treats_a_401_redirect_as_reachable():
    """`api()` resolves (not rejects) for the 401 login redirect; that is an
    auth flow, not a broken file, so the probe must not report it as a failure."""
    js = _HARNESS.replace("__HELPERS__", json.dumps({
        "_awaitElementLoad": _helper("_awaitElementLoad"),
        "_awaitMediaReady": _helper("_awaitMediaReady"),
        "_mountMediaPlayer": _helper("_mountMediaPlayer"),
        "_workspaceRawReachable": _helper("_workspaceRawReachable"),
    })).replace("__PARAMS__", json.dumps({"imageOutcome": "load", "mediaOutcome": "loadedmetadata", "redirect401": True})).replace(
        "__CONSTS__", _shipped_const_line("_PREVIEW_LOAD_TIMEOUT_MS")
    )
    js = js.replace("async function api(url, opts){", "async function api(url, opts){ if(params.redirect401){ return undefined; }")
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["probeOk"] is True, out


def test_raw_probe_treats_a_probe_timeout_as_reachable():
    """Gate re-review [CORE]: the 8 s probe abort must not read as a broken file.

    The probe is a 1-byte ranged GET, but the HTML route ignores `Range` and
    streams the whole document, so a large or slow file can outlast the probe
    while the iframe would have loaded it — master loaded it. A timeout is
    therefore "unknown", not "unreachable"; only a confirmed HTTP answer may
    report failure.
    """
    out = _run_harness(probe_timeout=True)
    assert out["probeCalls"], out
    assert out["probeOk"] is True, (
        "a probe timeout was reported as an unreachable file; a healthy slow "
        f"HTML preview then never opens: {out}"
    )
    assert out["probeStatuses"] == [], (
        f"a timeout must not surface the open-failed status: {out}"
    )


def test_shipped_preview_branches_await_their_outcome():
    """Guards against the branches silently reverting to fire-and-forget."""
    body = _read(WORKSPACE_JS_PATH)
    for needle, message in (
        ("_awaitElementLoad(img", "the image branch no longer awaits its load outcome"),
        ("_awaitMediaReady(mediaEl, ()=>_previewOpenOwned(_openGen,_openSid,_openWsGen))", "the media branch no longer awaits its owned outcome"),
    ):
        assert needle in body, message
    assert body.count("_workspaceRawReachable(url)") >= 2, (
        "both iframe-backed branches (pdf + html) must probe the route"
    )


# ── stalled response: the bound that prevents an indefinite hang ─────────────
#
# The Greptile follow-up: a raw response that stalls fires neither `load` nor
# `error`, so without a bound `_awaitElementLoad()` never settles,
# `openArtifactPath()` stays pending forever, and the explicitly selected image
# is never promoted. These probes drive the bound directly (the timer is
# captured rather than waited out) and assert on the PATH, not on the number.


def test_a_stalled_image_response_arms_a_bound():
    """Without an armed timeout there is nothing that can settle the promise."""
    out = _run_stall_harness(fire_bound=False)
    assert out["assigned"], "the src assignment must still start the load"
    assert out["timersArmed"] >= 1, (
        "no timeout was armed — a response that never fires load/error leaves "
        "openArtifactPath() pending forever (Greptile: image reveal can hang)"
    )
    assert out["settled"] is False, (
        f"the promise settled with no load, no error and no bound fired: {out}"
    )


def test_firing_the_bound_releases_the_wait_without_reporting_failure():
    """The bound is an anti-hang guard, not a failure signal. It cannot tell
    "slow" from "dead", so failing closed here hid legitimately slow images
    behind a closed panel while they were still loading (Greptile: slow
    previews remain hidden). It must release the wait and let the panel open."""
    out = _run_stall_harness(fire_bound=True)
    assert out["settled"] is True, out
    assert out["result"] is True, (
        f"the bound reported a failure for a response that may still be "
        f"loading, hiding a slow-but-valid image: {out}"
    )
    assert out["statuses"] == [], (
        f"the bound reported an error status for an unconfirmed load: {out}"
    )


def test_a_late_error_after_the_bound_is_still_surfaced():
    """The panel is already open by then, so the error is reported through the
    status line — the fire-and-forget code did this too, and dropping it would
    leave a genuinely broken source silent."""
    out = _run_stall_harness(fire_bound=True, late="error")
    assert out["lateStatuses"] == ["image_load_failed"], out


def test_a_late_load_harmlessly_clears_the_late_error_watch():
    """A slow response that does arrive must not be reported as broken."""
    out = _run_stall_harness(fire_bound=True, late="load")
    assert out["lateStatuses"] == [], out


def test_a_late_load_is_still_honoured():
    """The bound must not pre-empt a response that does arrive — only a genuine
    stall is released early."""
    out = _run_stall_harness(fire_bound=False)
    assert out["lateLoadResult"] is True, out


def test_late_handlers_retire_as_a_pair():
    """Greptile P2: `{once:true}` only removes the handler that fired, and this

    element is shared (#previewImg) — so an attempt ending in `error` left its
    paired `load` handler attached permanently. Repeated timed-out failures
    stacked stale closures on the element and let old handlers consume events
    from later previews. Whichever terminal event arrives must clear both."""
    for late in ("error", "load"):
        out = _run_stall_harness(fire_bound=True, late=late)
        assert out["listenersLeft"] == 0, (
            f"a late {late} left {out['listenersLeft']} listener(s) attached to the "
            f"shared preview element: {out}"
        )


def test_repeated_timed_out_failures_do_not_accumulate_listeners():
    """The accumulation the finding describes: several consecutive stalled

    previews must not leave a growing pile of handlers on the shared element."""
    js = _STALL_HARNESS.replace(
        "__HELPERS__", json.dumps({"_awaitElementLoad": _helper("_awaitElementLoad")})
    ).replace(
        "__PARAMS__",
        json.dumps({"fireBound": False, "late": "", "repeat": 4}),
    ).replace("__CONSTS__", _shipped_const_line("_PREVIEW_LOAD_TIMEOUT_MS"))
    proc = subprocess.run(
        [NODE, "-e", js], capture_output=True, text=True, cwd=REPO_ROOT, timeout=30
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["repeatListenersLeft"] == 0, (
        f"stalled previews accumulated listeners on the shared element: {out}"
    )


# ── real browser: a broken image fails closed ───────────────────────────────


_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

_BROWSER_DRIVE = r"""
async () => {
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  S.session = {session_id: 'b6710', workspace: '/tmp/b6710-ws'};
  S.currentDir = '.';
  S.entries = [{name: 'broken.png', path: 'broken.png', type: 'file', mtime_ns: 1000}];
  renderFileTree();
  openWorkspacePanel('browse');
  window._workspacePathExists = async () => true;   // entry "exists"; raw fetch fails
  closeWorkspacePanel();
  const before = {mode: _workspacePanelMode, dismissed: _workspacePanelUserDismissed};
  const returned = await openArtifactPath('/tmp/b6710-ws/broken.png');
  const img = document.getElementById('previewImg');
  await new Promise(r => setTimeout(r, 400));
  return {
    before: before,
    returned: returned,
    after: {
      mode: _workspacePanelMode,
      dismissed: _workspacePanelUserDismissed,
      imgNaturalWidth: img.naturalWidth,
    },
  };
}
"""


def test_browser_broken_image_fails_closed():
    """End to end: a dismissed panel + a broken image artifact must NOT be
    promoted — the user's dismissal survives and openFile reports failure."""
    pw = pytest.importorskip("playwright.sync_api")
    from tests._pytest_port import BASE

    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        context = browser.new_context(viewport={"width": 480, "height": 800})
        page = context.new_page()
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => typeof S !== 'undefined' && S._bootReady === true", timeout=15000
        )
        page.wait_for_function(
            "() => typeof openArtifactPath === 'function'", timeout=15000
        )
        out = page.evaluate(_BROWSER_DRIVE)
        context.close()
        browser.close()

    assert out["before"]["dismissed"] is True, out
    assert out["returned"] is False, (
        "a broken image artifact still reported a successful reveal; the panel "
        "gets promoted onto an empty preview"
    )
    assert out["after"]["dismissed"] is True, (
        "the user's dismissal was cleared by a preview that never loaded"
    )
    assert out["after"]["mode"] == "closed", (
        f"the panel was force-opened onto a failed preview: {out}"
    )


# ── stale completions: a slower open must not commit over a newer one ────────
#
# Gate re-review [SILENT]: both iframe-backed branches await the raw probe
# before committing the frame, so nothing stopped a slower open from landing
# after a newer one. The gate's Chromium repro: delay `ok.html` by 400 ms, then
# open `new.html` → this head showed `ok.html` under the `new.html` filename
# while master showed `new.html`. These drives run the REAL openFile() against a
# probe whose timing the test controls, so the ordering is deterministic.

_STALE_OPEN_DRIVE = r"""
async (paths) => {
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  S.session = {session_id: 'b6710-stale', workspace: '/tmp/b6710-ws'};
  S.currentDir = '.';
  const probes = [];
  window._workspaceRawReachable = (url) => new Promise(resolve => {
    probes.push(String(url));
    const slow = String(url).indexOf(paths.old) !== -1;
    setTimeout(() => resolve(true), slow ? 400 : 0);
  });
  const first = openFile(paths.old);
  await new Promise(r => setTimeout(r, 60));
  const second = openFile(paths.new);
  const firstReturned = await first;
  const secondReturned = await second;
  const frame = paths.new.slice(-4) === '.pdf'
    ? document.getElementById('previewPdfFrame')
    : document.getElementById('previewHtmlIframe');
  return {
    probes: probes.length,
    firstReturned: firstReturned,
    secondReturned: secondReturned,
    frameSrc: frame ? String(frame.getAttribute('src') || '') : null,
    previewPath: _previewCurrentPath,
  };
}
"""

_CLOSE_WHILE_PENDING_DRIVE = r"""
async () => {
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  S.session = {session_id: 'b6710-close', workspace: '/tmp/b6710-ws'};
  S.currentDir = '.';
  const probes = [];
  window._workspaceRawReachable = () => new Promise(resolve => {
    probes.push(1);
    setTimeout(() => resolve(true), 300);
  });
  const pending = openFile('slow.html');
  await new Promise(r => setTimeout(r, 60));
  const inFlightPath = _previewCurrentPath;
  clearPreview({keepPanelOpen: true});
  const returned = await pending;
  const frame = document.getElementById('previewHtmlIframe');
  return {
    probes: probes.length,
    inFlightPath: inFlightPath,
    returned: returned,
    frameSrc: frame ? String(frame.getAttribute('src') || '') : null,
    previewPath: _previewCurrentPath,
  };
}
"""


def _browser_out(drive, arg=None, viewport=None):
    """Run a drive against the real page served by the test server."""
    pw = pytest.importorskip("playwright.sync_api")
    from tests._pytest_port import BASE

    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        context = browser.new_context(
            viewport=viewport or {"width": 1280, "height": 800}
        )
        page = context.new_page()
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => typeof S !== 'undefined' && S._bootReady === true", timeout=15000
        )
        page.wait_for_function("() => typeof openFile === 'function'", timeout=15000)
        out = page.evaluate(drive) if arg is None else page.evaluate(drive, arg)
        context.close()
        browser.close()
    return out


@pytest.mark.parametrize("ext", ["html", "pdf"])
def test_a_slower_open_cannot_commit_over_a_newer_one(ext):
    """The older open must return false and leave the newer frame in place."""
    old, new = f"ok.{ext}", f"new.{ext}"
    out = _browser_out(_STALE_OPEN_DRIVE, {"old": old, "new": new})

    assert out["probes"] >= 2, f"precondition: both opens must really probe: {out}"
    assert out["secondReturned"] is True, out
    assert out["firstReturned"] is False, (
        f"the superseded open claimed success, so its caller promotes the panel: {out}"
    )
    assert old not in (out["frameSrc"] or ""), (
        f"the older open committed its frame over the newer one: {out}"
    )
    assert new in (out["frameSrc"] or ""), out
    assert out["previewPath"] == new, out


def test_a_close_retires_an_in_flight_preview_open():
    """A close advances the preview generation, so the pending open cannot
    repaint the panel the user just closed."""
    out = _browser_out(_CLOSE_WHILE_PENDING_DRIVE)

    assert out["probes"] == 1, f"precondition: the probe really was in flight: {out}"
    assert out["inFlightPath"] == "slow.html", out
    assert out["returned"] is False, (
        f"an open that outlived the user's close still reported success: {out}"
    )
    assert out["frameSrc"] in (None, ""), (
        f"the closed preview was repainted by the pending open: {out}"
    )


def test_shipped_preview_ownership_guards_are_wired():
    """Code-shape guard: the ownership checks must not disappear silently."""
    body = _read(WORKSPACE_JS_PATH)
    assert "const _openGen = ++_previewOpenGen;" in body, (
        "openFile() must take a preview generation"
    )
    assert body.count("_previewOpenOwned(_openGen,_openSid,_openWsGen)") >= 4, (
        "every branch that reports success after an await must reject a stale "
        "completion: image, media (video shares it), pdf and html"
    )
    assert "statusOnly:true" in body and "opts.statusOnly===true" in body, (
        "the reachability probe must ask api() for the status line only, "
        "otherwise it buffers the whole document"
    )
    boot = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    start = boot.index("function clearPreview(opts={}){")
    end = boot.index("\n}\n", start)
    assert "_previewOpenGen++" in boot[start:end], (
        "clearPreview() must retire in-flight preview opens"
    )


# ── the readiness waits need the same ownership check (re-review item 1) ─────
#
# This PR introduced the readiness waits in the image and media branches; both
# report success AFTER an await, so a stale success reached openArtifactPath()
# and cleared the user's dismissal. Gate repro: delay a media readiness by
# 400 ms, switch sessions, run loadDir('.') → this head reopened an empty panel
# while master stayed closed.

_STALE_READINESS_DRIVE = r"""
async (spec) => {
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  S.session = {session_id: 'b6710-before-switch', workspace: '/tmp/b6710-ws'};
  S.currentDir = '.';
  S.entries = [{name: spec.name, path: spec.name, type: 'file', mtime_ns: 1000}];
  renderFileTree();
  openWorkspacePanel('browse');
  window._workspacePathExists = async () => true;   // entry exists; readiness is slow
  closeWorkspacePanel();
  const before = {mode: _workspacePanelMode, dismissed: _workspacePanelUserDismissed};

  // Hold the readiness wait open so the switch happens mid-flight.
  const gate = {release: null, waits: 0};
  if (spec.kind === 'image') {
    window._awaitElementLoad = () => { gate.waits++; return new Promise(r => { gate.release = r; }); };
  } else {
    window._mountMediaPlayer = () => ({
      tagName: spec.tag, readyState: 1,
      addEventListener(){}, removeEventListener(){},
    });
    window._awaitMediaReady = () => { gate.waits++; return new Promise(r => { gate.release = r; }); };
  }

  const pending = openArtifactPath('/tmp/b6710-ws/' + spec.name);
  await new Promise(r => setTimeout(r, 80));
  const waitsIssued = gate.waits;
  const inFlightPath = _previewCurrentPath;

  // Mid-flight the user switches profile/session; loadDir('.') bumps the
  // workspace generation the same way switchToProfile() does.
  S.session = {session_id: 'b6710-after-switch', workspace: '/tmp/b6710-ws'};
  bumpWorkspaceTreeGen();
  if (gate.release) gate.release(true);   // the OLD open now resolves "ready"

  const returned = await pending;
  return {
    before: before,
    waitsIssued: waitsIssued,
    inFlightPath: inFlightPath,
    returned: returned,
    after: {mode: _workspacePanelMode, dismissed: _workspacePanelUserDismissed},
  };
}
"""


@pytest.mark.parametrize("spec", [
    {"kind": "image", "name": "slow.png", "tag": "img"},
    {"kind": "media", "name": "slow.wav", "tag": "audio"},
    {"kind": "media", "name": "slow.mp4", "tag": "video"},
])
def test_a_stale_readiness_success_cannot_reopen_a_closed_panel(spec):
    """Image/audio/video readiness must not report a reveal it no longer owns."""
    out = _browser_out(_STALE_READINESS_DRIVE, spec, viewport={"width": 480, "height": 800})

    assert out["waitsIssued"] == 1, f"precondition: readiness was in flight: {out}"
    assert out["inFlightPath"] == spec["name"], out
    assert out["before"]["dismissed"] is True, out
    assert out["returned"] is False, (
        "a readiness wait that resolved after the session switched reported "
        f"success, so the caller clears the dismissal and promotes the panel: {out}"
    )
    assert out["after"]["dismissed"] is True, (
        f"the user's dismissal was cleared by a superseded preview: {out}"
    )
    assert out["after"]["mode"] == "closed", (
        f"a superseded preview reopened the panel: {out}"
    )


# ── the probe must not download the document (re-review item 2) ──────────────
#
# The inline HTML route answers with the whole document and `Accept-Ranges:
# none` (api/routes.py `_serve_inline_html_preview`), so buffering the probe
# response streamed the file once for the probe and again for the iframe.
# Measured on the real server at a 2 MB/s throttle with a 3.3 MB page: probe
# 1.80–1.99 s vs 0.06–0.33 s, iframe load 5.42–5.78 s vs 3.43–3.69 s.

_PROBE_NO_BUFFER_DRIVE = r"""
async () => {
  const calls = {fetch: 0, text: 0, json: 0, cancel: 0, aborted: false};
  const realFetch = window.fetch;
  window.fetch = async (url, opts) => {
    calls.fetch++;
    const signal = opts && opts.signal;
    if (signal) signal.addEventListener('abort', () => { calls.aborted = true; });
    return {
      ok: true, status: 200, statusText: 'OK',
      headers: {get: () => 'text/html'},
      text: async () => { calls.text++; return 'x'.repeat(4096); },
      json: async () => { calls.json++; return {}; },
      body: {cancel: () => { calls.cancel++; }},
    };
  };
  let reachable = null, thrown = null;
  try {
    reachable = await _workspaceRawReachable('/api/file/raw?session_id=s1&path=big.html&inline=1');
  } catch (error) {
    thrown = String((error && error.message) || error);
  }
  window.fetch = realFetch;
  return {reachable: reachable, thrown: thrown, calls: calls};
}
"""


def test_the_reachability_probe_does_not_buffer_the_document():
    """The probe needs the status line only — never the body."""
    out = _browser_out(_PROBE_NO_BUFFER_DRIVE)

    assert out["thrown"] is None, out
    assert out["reachable"] is True, out
    assert out["calls"]["fetch"] == 1, f"precondition: the probe fetched once: {out}"
    assert out["calls"]["text"] == 0 and out["calls"]["json"] == 0, (
        f"the probe buffered a response body it never uses: {out}"
    )
    assert out["calls"]["aborted"] or out["calls"]["cancel"] > 0, (
        f"the probe left the body stream running instead of dropping it: {out}"
    )

def test_every_async_read_branch_checks_preview_ownership_before_committing():
    """Release gate (Codex): delayed Markdown, CSV and text/Office reads reopened an
    empty workspace panel after a session switch + loadDir('.'), because only the
    HTML/PDF/image/media branches re-checked ownership after their await. Every
    `await api(..., 'read')` branch must check `_previewOpenOwned` right after the
    read resolves, before touching preview state or reporting success."""
    body = _read(WORKSPACE_JS_PATH)
    guard = "if(!_previewOpenOwned(_openGen,_openSid,_openWsGen)) return false;"
    read = "await api(_workspaceRouteForPath(path, 'read'));"
    idx = 0
    reads = 0
    while True:
        i = body.find(read, idx)
        if i < 0:
            break
        reads += 1
        window = body[i + len(read): i + len(read) + 420]
        first_stmt = window.lstrip()
        # Allow explanatory comments between the read and the guard.
        while first_stmt.startswith("//"):
            first_stmt = first_stmt.split("\n", 1)[1].lstrip()
        assert first_stmt.startswith(guard), (
            "an async read branch commits preview state before re-checking ownership:\n"
            + window[:200]
        )
        idx = i + len(read)
    assert reads >= 3, "expected the Markdown, CSV and text read branches"


def test_media_wait_stays_silent_once_its_preview_is_no_longer_owned():
    """Greptile P2 on the release PR: clearPreview()/showPreview() only hide the
    <video>/<audio> element, so an error that fires after the preview was cleared
    or replaced must not report "file open failed" for a preview the user left."""
    body = _read(WORKSPACE_JS_PATH)
    helper = extract_function(body, "_awaitMediaReady")
    js = r"""
    const statuses = [];
    function setStatus(s){ statuses.push(s); }
    function t(k){ return k; }
    const _PREVIEW_LOAD_TIMEOUT_MS = 50;
    eval(__HELPER__);
    function fakeEl(){
      const ls = {};
      return { readyState: 0,
        addEventListener(ev, f){ (ls[ev] = ls[ev] || []).push(f); },
        removeEventListener(ev, f){ ls[ev] = (ls[ev] || []).filter(g => g !== f); },
        fire(ev){ (ls[ev] || []).slice().forEach(f => f()); } };
    }
    (async () => {
      let owned = true;
      const a = fakeEl();
      const pa = _awaitMediaReady(a, () => owned);
      owned = false; a.fire('error');
      const early = await pa;
      const earlyStatuses = statuses.length;
      owned = true;
      const b = fakeEl();
      const pb = _awaitMediaReady(b, () => owned);
      await pb;                       // the bound released it (resolve true)
      owned = false; b.fire('error'); // late error after the preview was left
      const lateStatuses = statuses.length - earlyStatuses;
      owned = true;
      const c = fakeEl();
      const pc = _awaitMediaReady(c, () => owned);
      c.fire('error');
      const ownedFail = await pc;
      console.log(JSON.stringify({ early, earlyStatuses, lateStatuses, ownedFail, total: statuses.length }));
    })();
    """.replace("__HELPER__", json.dumps(helper))
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True, cwd=REPO_ROOT, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["early"] is False and out["earlyStatuses"] == 0, out
    assert out["lateStatuses"] == 0, out
    assert out["ownedFail"] is False and out["total"] == 1, "an owned failure must still report once"

