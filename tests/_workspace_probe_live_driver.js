// Drive the SHIPPED api() + _workspacePathExists against a LIVE hermes-webui
// server. This is the end-to-end half of the #8421 gate fix: the browser code
// that decides "does this workspace file exist?" talks to the real
// /api/list and /api/file/raw handlers, so the workspace-versus-attachment
// provenance question is answered by the server, not by a mock.
//
// Usage: node _workspace_probe_live_driver.js <workspace.js> <base> <sid> <workspace> [path...]
// Prints one JSON object per path: {"path":..., "exists":..., "requests":[...]}
const fs = require('fs');

const [SRC, BASE, SID, WS, ...PATHS] = process.argv.slice(2);
if (!SRC || !BASE || !SID || !WS) {
  console.error('usage: node driver.js <workspace.js> <base> <sid> <workspace> [path...]');
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

const S = { session: { session_id: SID, workspace: WS } };
const requests = [];
const realFetch = globalThis.fetch;
const recordingFetch = (url, opts) => {
  requests.push(String(url));
  return realFetch(url, opts);
};

const document = { baseURI: BASE.replace(/\/+$/, '') + '/' };
const location = { href: document.baseURI };
const window = { location: { href: document.baseURI, pathname: '/', search: '' } };

const factory = new Function(
  'S', 'api', 'document', 'location', 'window', 'fetch', 'AbortController',
  'setTimeout', 'clearTimeout', 'URL', 'URLSearchParams', 'encodeURIComponent',
  'showToast', '_workspaceEscapeGrantForPath', 'console',
  [
    extract('_normalizeWorkspaceRelPath'),
    extract('_isSameOrChildPath'),
    extract('_workspaceRouteForPathRel'),
    extract('_workspaceRouteForPath'),
    extract('_workspacePathExists'),
    extract('_workspaceProbeFile'),
    // The shipped api() helper is under test too: it owns the status/statusOnly
    // contract the probe relies on, so a mock api() would not prove the
    // browser/server seam.
    extract('api'),
  ].join('\n') + '\nreturn { _workspacePathExists };'
);

const { _workspacePathExists } = factory(
  S,
  null, // replaced below: api is defined by the extracted body
  document,
  location,
  window,
  recordingFetch,
  AbortController,
  setTimeout,
  clearTimeout,
  URL,
  URLSearchParams,
  encodeURIComponent,
  () => {},
  () => null,
  console
);

(async () => {
  for (const path of PATHS) {
    requests.length = 0;
    let exists;
    let error = null;
    try {
      exists = await _workspacePathExists(path);
    } catch (e) {
      error = String((e && e.message) || e);
      exists = 'threw';
    }
    console.log(JSON.stringify({ path, exists, error, requests: [...requests] }));
  }
})();
