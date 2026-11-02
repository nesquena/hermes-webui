// Force the workspace-existence probe to TIME OUT, then prove the artifact
// open cannot reach the session attachment inbox.
//
// The gate reproduced a P1 on PR #8421: `_workspaceProbeFile()` answered
// "exists" when `api()` timed out, so `openArtifactPath()` fell through into
// `openFile()` — and every follow-up route there was UNSCOPED. `/api/file/raw`
// falls back to the requesting session's upload inbox when the workspace path
// is absent, so the server answered the read's download fallback (and the
// image/audio/video/PDF/HTML preview URLs) with the uploaded copy: one click
// downloaded a session upload as if it were a workspace file.
//
// This driver holds the probe request and records what the click actually
// requested:
//   * `--mock`  injects the timeout at the api() seam (the same
//               `{name:'TimeoutError', timeout:true}` shape api() throws), so
//               the decision logic runs fast and deterministically.
//   * `--live`  wraps fetch so the workspace_only request never settles; the
//               SHIPPED api() then hits its own 30 s deadline and throws the
//               real TimeoutError. Every other request reaches the real
//               server. This is the gate's reproduction, minus the browser.
//
// The driver is a recorder: it prints one JSON object per scenario and lets
// the pytest wrapper judge the policy (zero unscoped raw requests, zero
// downloads that would serve the inbox copy).
//
// Usage:
//   node _workspace_probe_timeout_open_driver.js <workspace.js> --mock
//   node _workspace_probe_timeout_open_driver.js <workspace.js> --live <base> <sid> <workspace>
const fs = require('fs');

const args = process.argv.slice(2);
const SRC = args[0];
if (!SRC) {
  console.error('usage: node driver.js <workspace.js> --mock | --live <base> <sid> <workspace>');
  process.exit(2);
}
const MODE = args[1] === '--live' ? 'live' : 'mock';
const BASE = MODE === 'live' ? args[2] : null;
const SID = MODE === 'live' ? args[3] : null;
const WS = MODE === 'live' ? args[4] : null;
if (MODE === 'live' && (!BASE || !SID || !WS)) {
  console.error('usage: node driver.js <workspace.js> --live <base> <sid> <workspace>');
  process.exit(2);
}
const src = fs.readFileSync(SRC, 'utf8');

function extract(name) {
  let start = src.indexOf(`async function ${name}(`);
  if (start < 0) start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`${name} not found in ${SRC}`);
  let i = src.indexOf('(', start);
  let parens = 0;
  for (; i < src.length; i++) {
    if (src[i] === '(') parens++;
    else if (src[i] === ')') {
      parens--;
      if (parens === 0) break;
    }
  }
  const brace = src.indexOf('{', i);
  let depth = 0;
  for (let j = brace; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') {
      depth--;
      if (depth === 0) return src.slice(start, j + 1);
    }
  }
  throw new Error(`could not extract ${name}`);
}

// Slice a whole `const NAME = …;` statement (the extension sets span several
// lines), so the routing under test uses the SHIPPED sets, not a copy.
function extractStatement(name) {
  const start = src.indexOf(`const ${name}`);
  if (start < 0) throw new Error(`${name} not found in ${SRC}`);
  let depth = 0;
  let quote = null;
  for (let i = start; i < src.length; i++) {
    const c = src[i];
    if (quote) {
      if (c === '\\') { i++; continue; }
      if (c === quote) quote = null;
      continue;
    }
    if (c === "'" || c === '"' || c === '`') { quote = c; continue; }
    if (c === '(' || c === '[' || c === '{') depth++;
    else if (c === ')' || c === ']' || c === '}') depth--;
    else if (c === ';' && depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`could not extract const ${name}`);
}

// ── the SHIPPED code under test ────────────────────────────────────────────
const CORE = [
  extract('_normalizeWorkspaceRelPath'),
  extract('_isSameOrChildPath'),
  extractStatement('IMAGE_EXTS'),
  extractStatement('MD_EXTS'),
  extractStatement('HTML_EXTS'),
  extractStatement('PDF_EXTS'),
  extractStatement('AUDIO_EXTS'),
  extractStatement('VIDEO_EXTS'),
  extractStatement('DOWNLOAD_EXTS'),
  extract('fileExt'),
  extractStatement('_PREVIEW_LOAD_TIMEOUT_MS'),
  extract('_workspaceRouteForPathRel'),
  extract('_workspaceRouteForPath'),
  extract('_workspacePathExists'),
  extract('_workspaceProbeFile'),
  extract('_awaitElementLoad'),
  extract('_awaitMediaReady'),
  extract('_mountMediaPlayer'),
  extract('_workspaceRawReachable'),
  extract('openArtifactPath'),
  extract('openFile'),
  extract('downloadFile'),
];
if (MODE === 'live') CORE.push(extract('api'));

// Harness globals, bound BY NAME so the wiring cannot silently shift the way a
// long positional argument list can. The mutable ones are the module state the
// shipped functions assign to; they are rebuilt per scenario. In live mode the
// SHIPPED api() is part of CORE, so it is not bound from the harness object.
const HARNESS_NAMES = [
  'S', 'api', 'document', 'location', 'window', 'fetch', 'AbortController',
  'setTimeout', 'clearTimeout', 'URL', 'URLSearchParams', 'encodeURIComponent',
  'Date', '_workspaceEscapeGrantForPath', '_clearWorkspaceEscapeGrant',
  '$', 'showPreview', 'renderFileBreadcrumb', 'renderCodePreviewContent',
  'renderCsvPreviewContent', 'renderMarkdownPreviewContent',
  'shouldRenderMarkdownPreviewAsPlainText', 'setLargeMarkdownForceRenderVisible',
  'largeMarkdownPlainTextStatus', 'setStatus', 'showToast', 't',
  '_previewOpenOwned', '_mediaPlayerHtml', '_applyMediaPlaybackPreferences',
  '_previewServerEditable', '_previewSaveRoute', '_previewOfficeFormat',
  '_previewPreviewKind', '_previewCurrentPath', '_previewRawContent',
  '_previewRawContentPath', '_previewOpenGen', '_wsTreeGen',
  'switchWorkspacePanelTab', '_artifactClickGen', '_workspacePanelDismissGen',
  '_setWorkspacePanelDismissed', '_workspacePanelUserDismissed',
  'ensureWorkspacePreviewVisible', 'console',
];
const BINDINGS = HARNESS_NAMES
  .filter((k) => !(MODE === 'live' && k === 'api'))
  .map((k) => `let ${k} = H[${JSON.stringify(k)}];`);
const factory = new Function(
  'H',
  BINDINGS.join('\n') + '\n' +
  CORE.join('\n') + '\nreturn { openArtifactPath, openFile, downloadFile };'
);

// ── recorders ──────────────────────────────────────────────────────────────
const requests = [];      // every URL handed to api()/fetch
const downloads = [];     // every download anchor click
const previewUrls = [];   // every URL assigned to a preview element
const rendered = [];      // {path, content} handed to a preview renderer
const statuses = [];      // setStatus() messages
const toasts = [];        // showToast() messages

const S = { session: { session_id: SID || 'SID', workspace: WS || '/ws' } };
let grantForPath = () => null;
let apiImpl = async () => ({});

// Fake DOM: only what openFile()/downloadFile() touch. `src`/`innerHTML` are
// accessors so every URL a preview element receives is recorded.
function makeEl(id) {
  const e = {
    id,
    style: {},
    classList: { add() {}, remove() {}, contains() { return false; } },
    _listeners: {},
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      const l = this._listeners[type] || [];
      const i = l.indexOf(fn);
      if (i >= 0) l.splice(i, 1);
    },
  };
  if (id === 'previewImg') { e.complete = true; e.naturalWidth = 1; }
  if (id === 'previewMediaWrap') {
    e.querySelector = () => ({
      addEventListener() {}, removeEventListener() {}, readyState: 1,
    });
  }
  Object.defineProperty(e, 'src', {
    get() { return this._src; },
    set(v) { this._src = v; if (v) previewUrls.push(String(v)); },
  });
  Object.defineProperty(e, 'innerHTML', {
    get() { return this._html; },
    set(v) { this._html = v; },
  });
  return e;
}
const els = {};
function $(id) { if (!els[id]) els[id] = makeEl(id); return els[id]; }

function fakeDocument() {
  // No baseURI in mock mode: _workspaceRouteForPath keeps the app-relative
  // form, which is exactly the string the browser would request.
  return {
    createElement: () => ({
      href: '', download: '',
      click() { downloads.push({ href: this.href, filename: this.download }); },
    }),
    body: { appendChild() {}, removeChild() {} },
  };
}

function buildHarness() {
  const document = MODE === 'live'
    ? Object.assign(fakeDocument(), { baseURI: BASE.replace(/\/+$/, '') + '/' })
    : fakeDocument();
  const location = MODE === 'live' ? { href: document.baseURI } : undefined;
  const window = MODE === 'live'
    ? { location: { href: document.baseURI, pathname: '/', search: '' } }
    : undefined;

  const H = {
    S,
    document,
    location,
    window,
    fetch: globalThis.fetch,
    AbortController,
    setTimeout,
    clearTimeout,
    URL,
    URLSearchParams,
    encodeURIComponent,
    Date,
    _workspaceEscapeGrantForPath: (path) => grantForPath(path),
    _clearWorkspaceEscapeGrant: () => {},
    $,
    showPreview: () => {},
    renderFileBreadcrumb: () => {},
    renderCodePreviewContent: (path, content) => rendered.push({ path, content }),
    renderCsvPreviewContent: (path, content) => rendered.push({ path, content }),
    renderMarkdownPreviewContent: (data) => rendered.push({ path: data && data.path, content: data && data.content }),
    shouldRenderMarkdownPreviewAsPlainText: () => false,
    setLargeMarkdownForceRenderVisible: () => {},
    largeMarkdownPlainTextStatus: () => '',
    setStatus: (m) => statuses.push(m),
    showToast: (m) => toasts.push(m),
    t: (k) => k,
    _previewOpenOwned: () => true,
    _mediaPlayerHtml: undefined,
    _applyMediaPlaybackPreferences: undefined,
    _previewServerEditable: null,
    _previewSaveRoute: '/api/file/save',
    _previewOfficeFormat: '',
    _previewPreviewKind: '',
    _previewCurrentPath: '',
    _previewRawContent: '',
    _previewRawContentPath: '',
    _previewOpenGen: 0,
    _wsTreeGen: 0,
    switchWorkspacePanelTab: () => {},
    _artifactClickGen: 0,
    _workspacePanelDismissGen: 0,
    _setWorkspacePanelDismissed: () => {},
    _workspacePanelUserDismissed: false,
    ensureWorkspacePreviewVisible: () => {},
    console,
  };

  if (MODE === 'live') {
    const realFetch = globalThis.fetch;
    const recordingFetch = (url, opts) => {
      const u = String(url);
      requests.push(u);
      if (u.includes('workspace_only=1')) {
        // The gate held ONLY the workspace_only request, letting the browser's
        // unmodified 30 s api() deadline fire. Reproduce that exactly.
        return new Promise(() => {});
      }
      return realFetch(url, opts);
    };
    H.fetch = recordingFetch;
    // The SHIPPED api() is part of CORE here: it owns the 30 s deadline and the
    // statusOnly contract, so it runs from the same extracted source against
    // the recording fetch above.
  } else {
    H.api = async (path, opts) => { requests.push(String(path)); return apiImpl(path, opts); };
  }
  return factory(H);
}

// ── mock server ────────────────────────────────────────────────────────────
// Models the real handlers: `/api/list` (capped), `/api/file/raw` with the
// workspace_only provenance flag (404 when the workspace path is absent), the
// unscoped raw route (200 from the upload inbox), and `/api/file` read
// (workspace-only on the server, 404 when absent).
function mockServer(spec) {
  return async (path, opts) => {
    if (path.includes('/api/list')) return { entries: spec.list || [] };
    if (path.includes('/api/escape/file/raw')) return { status: 200 };
    if (path.includes('/api/escape/file/read')) return { content: 'ESCAPED-BYTES' };
    if (path.includes('/api/file/raw')) {
      if (path.includes('workspace_only=1')) {
        const ranged = !!(opts && opts.headers && opts.headers.Range);
        if (ranged) {
          if (spec.reachable === 'missing') { const e = new Error('Not found'); e.status = 404; throw e; }
          return { status: 200 };
        }
        if (spec.probe === 'timeout') {
          const e = new Error('Request timed out. Please try again.');
          e.name = 'TimeoutError';
          e.timeout = true;
          throw e;
        }
        if (spec.probe === 'missing') { const e = new Error('Not found'); e.status = 404; throw e; }
        return { status: 200 };
      }
      // Unscoped: the session attachment inbox answers 200 for the upload.
      return { status: 200 };
    }
    if (path.includes('/api/file?')) {
      if (spec.read === 'missing') { const e = new Error('Not a file'); e.status = 404; throw e; }
      return { content: 'WORKSPACE-BYTES', path: 'x', size: 15, lines: 1 };
    }
    return {};
  };
}

// ── scenarios ──────────────────────────────────────────────────────────────
// `call: 'artifact'` drives the real openArtifactPath() (the artifact button);
// `call: 'openFile'` calls openFile() directly, i.e. any OTHER caller (file
// tree, force-render, cache-bust reload) whose attachment fallback must stay.
// `modes` limits a scenario to the harness that can serve it: the mock models
// every route, while --live talks to a real server whose fixture pytest set up
// (an uploaded note.txt with no workspace copy, and a real workspace file), so
// live runs only the scenarios marked for it.
const SCENARIOS = [
  {
    name: 'timeout-inbox-only-txt',
    path: 'note.txt',
    call: 'artifact',
    modes: ['mock', 'live'],
    server: { probe: 'timeout', read: 'missing' },
    note: 'gate fixture: probe held past the 30 s api() deadline, .txt falls to the read route and its download fallback',
  },
  {
    name: 'timeout-inbox-only-png',
    path: 'note.png',
    call: 'artifact',
    server: { probe: 'timeout' },
    note: 'image preview builds its own raw URL',
  },
  {
    name: 'timeout-inbox-only-zip',
    path: 'bundle.zip',
    call: 'artifact',
    server: { probe: 'timeout' },
    note: 'download-only extension goes straight to downloadFile()',
  },
  {
    name: 'timeout-inbox-only-mp3',
    path: 'note.mp3',
    call: 'artifact',
    server: { probe: 'timeout' },
    note: 'audio preview builds its own raw URL',
  },
  {
    name: 'timeout-inbox-only-pdf',
    path: 'note.pdf',
    call: 'artifact',
    server: { probe: 'timeout', reachable: 'missing' },
    note: 'PDF branch probes reachability before committing the frame',
  },
  {
    name: 'probe-200-workspace-txt',
    path: 'real.txt',
    call: 'artifact',
    modes: ['mock', 'live'],
    server: { probe: 'ok', read: 'ok' },
    note: 'conclusive probe: the workspace file must still open (no over-blocking)',
  },
  {
    name: 'probe-200-workspace-png',
    path: 'real.png',
    call: 'artifact',
    server: { probe: 'ok' },
    note: 'conclusive probe: image preview must still open',
  },
  {
    name: 'probe-200-workspace-zip',
    path: 'real.zip',
    call: 'artifact',
    server: { probe: 'ok' },
    note: 'conclusive probe: download-only artifact must still download',
  },
  {
    name: 'escape-grant-txt',
    path: 'escape-file.txt',
    call: 'artifact',
    grant: () => ({ token: 'GRANT', path: 'escape-file.txt' }),
    server: { probe: 'ok', read: 'ok' },
    note: 'escape-granted path keeps /api/escape/file/raw + token, no workspace_only',
  },
  {
    name: 'escape-grant-png',
    path: 'escape-img.png',
    call: 'artifact',
    grant: () => ({ token: 'GRANT', path: 'escape-img.png' }),
    server: { probe: 'ok' },
    note: 'escape-granted preview URL must not gain workspace_only',
  },
  {
    name: 'other-caller-openFile-txt',
    path: 'note.txt',
    call: 'openFile',
    server: { read: 'missing' },
    note: 'any OTHER caller keeps the default attachment fallback (unscoped raw)',
  },
  {
    name: 'other-caller-openFile-png',
    path: 'note.png',
    call: 'openFile',
    server: {},
    note: 'any OTHER caller keeps the default attachment fallback (unscoped raw)',
  },
];

(async () => {
  for (const sc of SCENARIOS) {
    if (!(sc.modes || ["mock"]).includes(MODE)) continue;
    const startedAt = Date.now();
    requests.length = 0;
    downloads.length = 0;
    previewUrls.length = 0;
    rendered.length = 0;
    statuses.length = 0;
    toasts.length = 0;
    // Fresh fake DOM per scenario: the element registry is module-level state
    // in the browser, and a stale preview element would otherwise carry the
    // previous scenario's URL into this one's record.
    for (const k of Object.keys(els)) delete els[k];
    grantForPath = sc.grant || (() => null);
    apiImpl = mockServer(sc.server || {});
    const { openArtifactPath, openFile } = buildHarness();

    let opened;
    let error = null;
    try {
      opened = sc.call === 'artifact'
        ? await openArtifactPath(sc.path)
        : await openFile(sc.path, {});
    } catch (e) {
      error = String((e && e.message) || e);
      opened = 'threw';
    }

    // The media player markup carries the URL; scrape it like the browser would.
    const mediaHtml = els.previewMediaWrap && els.previewMediaWrap._html;
    if (mediaHtml) {
      const m = /<(?:video|audio)[^>]*\ssrc="([^"]+)"/.exec(mediaHtml);
      if (m) previewUrls.push(m[1]);
    }

    const allUrls = [...requests, ...previewUrls, ...downloads.map((d) => d.href)];
    const unscopedRaw = allUrls.filter(
      (u) => /\/api\/file\/raw/.test(u) && !/workspace_only=1/.test(u)
    );
    const escapeUrls = allUrls.filter((u) => /\/api\/escape\//.test(u));
    const workspaceOnlyUrls = allUrls.filter((u) => /workspace_only=1/.test(u));

    const row = {
      scenario: sc.name,
      mode: MODE,
      note: sc.note,
      path: sc.path,
      elapsedMs: Date.now() - startedAt,
      opened,
      error,
      requests: [...requests],
      previewUrls: [...previewUrls],
      rendered: [...rendered],
      downloads: [...downloads],
      statuses: [...statuses],
      toasts: [...toasts],
      unscopedRaw,
      escapeUrls,
      workspaceOnlyUrls,
    };

    if (MODE === 'live') {
      // Fetch every download URL the click produced and record what the real
      // server answered: a download that serves the uploaded bytes is the leak.
      for (const d of row.downloads) {
        try {
          const res = await fetch(d.href);
          const buf = Buffer.from(await res.arrayBuffer());
          d.status = res.status;
          d.bytes = buf.length;
          d.body = buf.toString('utf8').slice(0, 64);
        } catch (e) {
          d.status = 'error';
          d.error = String((e && e.message) || e);
        }
      }
    }

    console.log(JSON.stringify(row));
  }
})();
