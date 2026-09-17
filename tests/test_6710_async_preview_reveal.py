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

let statuses = [];
function setStatus(s){ statuses.push(s); }
function t(k){ return k; }
let apiCalls = [];
async function api(url, opts){
  apiCalls.push({url: url, opts: opts || null});
  if(params.apiFails){ const e = new Error('nope'); e.status = params.apiStatus || 404; throw e; }
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


def _run_harness(*, image_outcome="load", media_outcome="loadedmetadata",
                 api_fails=False, api_status=404) -> dict:
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
    }
    js = _HARNESS.replace("__HELPERS__", json.dumps(helpers)).replace(
        "__PARAMS__", json.dumps(payload)
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
    })).replace("__PARAMS__", json.dumps({"imageOutcome": "load", "mediaOutcome": "loadedmetadata"}))
    js = js.replace("async function api(url, opts){", "async function api(url, opts){ if(params.redirect401){ return undefined; }")
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["probeOk"] is True, out


# ── the shipped branches must actually use the outcome ──────────────────────


def test_shipped_preview_branches_await_their_outcome():
    """Guards against the branches silently reverting to fire-and-forget."""
    body = _read(WORKSPACE_JS_PATH)
    for needle, message in (
        ("_awaitElementLoad(img", "the image branch no longer awaits its load outcome"),
        ("_awaitMediaReady(mediaEl)", "the media branch no longer awaits its outcome"),
    ):
        assert needle in body, message
    assert body.count("_workspaceRawReachable(url)") >= 2, (
        "both iframe-backed branches (pdf + html) must probe the route"
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
