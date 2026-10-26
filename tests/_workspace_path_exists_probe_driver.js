// Drive the REAL _workspacePathExists / _workspaceProbeFile out of the served
// static/workspace.js and prove the decision branches.
//
// This is a logic test, not a text assertion: the functions are extracted from
// the file the browser actually downloads, so a change in the shipped source
// changes what runs here. The route the probe requests is built by the shipped
// _workspaceRouteForPath/_workspaceRouteForPathRel pair too, so the URL
// asserted here is the URL a real browser would request.
//
// Usage: node _workspace_path_exists_probe_driver.js <path-to-static/workspace.js>
const fs = require('fs');

const SRC = process.argv[2];
if (!SRC) {
  console.error('usage: node driver.js <path-to-static/workspace.js>');
  process.exit(2);
}
const src = fs.readFileSync(SRC, 'utf8');

function extract(name) {
  let start = src.indexOf(`async function ${name}(`);
  if (start < 0) start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`${name} not found in ${SRC}`);
  // Walk the parameter list first: a default parameter (e.g. `opts={}`) closes
  // with a brace that is NOT the function body.
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

// Minimal stand-ins for the globals the extracted code touches.
let S = { session: { session_id: 'SID', workspace: '/ws' } };
let apiCalls = [];
let apiImpl = async () => ({ entries: [] });
let grantForPath = () => null;

// No document/location in Node: _workspaceRouteForPath falls back to the
// app-relative route, which is exactly the string the probe hands to api().
const document = undefined;
const location = undefined;

const factory = new Function(
  'S', 'api', 'document', 'location', 'URL', 'URLSearchParams', 'encodeURIComponent',
  '_workspaceEscapeGrantForPath', 'console',
  [
    extract('_normalizeWorkspaceRelPath'),
    extract('_isSameOrChildPath'),
    extract('_workspaceRouteForPathRel'),
    extract('_workspaceRouteForPath'),
    extract('_workspacePathExists'),
    extract('_workspaceProbeFile'),
  ].join('\n') + '\nreturn { _workspacePathExists, _workspaceProbeFile };'
);
const { _workspacePathExists } = factory(
  S,
  async (...args) => { apiCalls.push(args); return apiImpl(...args); },
  document,
  location,
  URL,
  URLSearchParams,
  encodeURIComponent,
  (path) => grantForPath(path),
  console
);

let failures = 0;
function check(label, cond, extra) {
  if (cond) {
    console.log(`  ✅ ${label}`);
  } else {
    failures++;
    console.log(`  ❌ ${label}${extra ? ' — ' + extra : ''}`);
  }
}

(async () => {
  console.log(`driving real functions from ${SRC}\n`);

  // ── Branch 1: the listing contains the name → true, no probe ──────────
  console.log('Branch 1: name present in the parent listing');
  apiCalls = [];
  apiImpl = async () => ({ entries: [{ name: 'note.md', path: 'note.md' }] });
  let r = await _workspacePathExists('note.md');
  check('returns true', r === true, `got ${r}`);
  check('did not need the direct probe', apiCalls.length === 1, `api calls: ${apiCalls.length}`);

  // ── Branch 2: the listing is CAPPED and the name is missing ──────────
  // The bug: the name sits past the response cap, so the listing cannot
  // prove absence. The probe must find the file.
  console.log('\nBranch 2: listing capped, name missing from it (the bug)');
  apiCalls = [];
  apiImpl = async (url) => {
    if (url.includes('/api/list')) return { entries: [{ name: 'other.md' }] };
    return { status: 200 }; // raw probe resolves → 200 (api() statusOnly contract)
  };
  r = await _workspacePathExists('a-file-past-the-cap.md');
  check('returns true via the direct probe', r === true, `got ${r}`);
  check('issued the probe request', apiCalls.length === 2, `api calls: ${apiCalls.length}`);
  check('probe hit the raw route', apiCalls[1] && apiCalls[1][0].includes('/api/file/raw'), apiCalls[1] && apiCalls[1][0]);
  check('probe used statusOnly (no body buffered)', apiCalls[1] && apiCalls[1][1] && apiCalls[1][1].statusOnly === true);
  // Provenance: /api/file/raw falls back to the session attachment inbox when
  // the workspace path is absent, so a bare 200 does not prove the WORKSPACE
  // file exists. The probe must ask for workspace-only resolution.
  check('probe scoped the lookup to the workspace', !!apiCalls[1] && apiCalls[1][0].includes('workspace_only=1'), apiCalls[1] && apiCalls[1][0]);

  // ── Branch 3: the file genuinely does not exist → false ──────────────
  console.log('\nBranch 3: file genuinely absent');
  apiCalls = [];
  apiImpl = async (url) => {
    if (url.includes('/api/list')) return { entries: [{ name: 'other.md' }] };
    const err = new Error('Not found');
    err.status = 404;
    throw err;
  };
  r = await _workspacePathExists('missing.md');
  check('returns false', r === false, `got ${r}`);
  check('only 404 counts as absent', apiCalls.length === 2, `api calls: ${apiCalls.length}`);

  // ── Branch 4: the listing itself fails → still probe ─────────────────
  console.log('\nBranch 4: the listing request fails');
  apiCalls = [];
  apiImpl = async (url) => {
    if (url.includes('/api/list')) throw new Error('network down');
    return { status: 200 }; // probe succeeds
  };
  r = await _workspacePathExists('note.md');
  check('falls back to the probe and returns true', r === true, `got ${r}`);
  check('probe was attempted after the listing error', apiCalls.length === 2, `api calls: ${apiCalls.length}`);

  // ── Branch 5: a non-404 probe failure must NOT be reported as absent ─
  console.log('\nBranch 5: probe fails with a non-404 status');
  apiCalls = [];
  apiImpl = async (url) => {
    if (url.includes('/api/list')) return { entries: [] };
    const err = new Error('forbidden');
    err.status = 403;
    throw err;
  };
  r = await _workspacePathExists('note.md');
  // Real contract: _workspaceProbeFile re-raises any non-404, non-timeout
  // failure, and the caller turns that into false. So an inconclusive probe
  // (403/503) ends up as "not openable" — the intended outcome for an expired
  // escape grant, where "file open failed" is the honest message. What must
  // NOT happen is the probe being skipped, or a 404-shaped error being
  // invented.
  //
  // The probe must be attempted exactly once here: a probe that raises is a
  // verdict in itself, not a reason to ask again. Asking twice could even
  // return two different answers for the same path.
  check('probe was attempted (not skipped)', apiCalls.length >= 2, `api calls: ${apiCalls.length}`);
  check('the probe is NOT retried after it raised', apiCalls.length === 2, `api calls: ${apiCalls.length}`);
  check('reports not-openable for an inconclusive probe', r === false, `got ${r}`);

  // ── Branch 6: the probe resolves but carries NO status → not proof of
  // existence. A helper that resolves without a status (or with a non-2xx)
  // must not be reported as "the file is there" — otherwise a missing
  // artifact would be treated as openable.
  console.log('\nBranch 6: probe resolves without a status');
  apiCalls = [];
  apiImpl = async (url) => {
    if (url.includes('/api/list')) return { entries: [] };
    return { entries: [] }; // resolves, but there is no status
  };
  r = await _workspacePathExists('missing.md');
  check('does NOT claim the file exists', r === false, `got ${r}`);
  check('the probe was still attempted', apiCalls.length >= 2, `api calls: ${apiCalls.length}`);

  // ── Branch 7: the probe TIMES OUT → must NOT be reported as missing.
  // #6710: only a confirmed HTTP answer proves absence. A timeout means the
  // server is slow, not that the file was deleted — reporting "file open
  // failed" here would tell the user a file is gone when it is not.
  console.log('\nBranch 7: probe times out');
  apiCalls = [];
  apiImpl = async (url) => {
    if (url.includes('/api/list')) return { entries: [] };
    const err = new Error('Request timed out. Please try again.');
    err.name = 'TimeoutError';
    err.timeout = true;
    throw err;
  };
  r = await _workspacePathExists('slow.md');
  check('is NOT reported as missing', r === true, `got ${r}`);
  check('the probe was attempted', apiCalls.length >= 2, `api calls: ${apiCalls.length}`);

  // ── Branch 8: escape-granted paths keep the escape route ─────────────
  // /api/escape/file/raw resolves only against the authorized external root:
  // it has no attachment-inbox fallback, so it stays an existence authority
  // without the flag. The probe must not downgrade it to /api/file/raw.
  console.log('\nBranch 8: escape-granted path keeps the escape route');
  apiCalls = [];
  grantForPath = () => ({ token: 'GRANT', path: '/outside' });
  apiImpl = async (url) => {
    if (url.includes('/api/list')) return { entries: [] };
    return { status: 200 };
  };
  r = await _workspacePathExists('/outside/note.md');
  check('returns true', r === true, `got ${r}`);
  const escapeUrl = apiCalls[1] && apiCalls[1][0];
  check('probe used the escape raw route', !!escapeUrl && escapeUrl.includes('/api/escape/file/raw'), escapeUrl);
  check('escape route carried the grant token', !!escapeUrl && escapeUrl.includes('token=GRANT'), escapeUrl);
  check('escape route did NOT gain workspace_only', !!escapeUrl && !escapeUrl.includes('workspace_only=1'), escapeUrl);
  grantForPath = () => null;

  // ── Branch 9: an inbox-only upload must not satisfy the workspace probe.
  // The server answers 404 for a workspace-only lookup when the name exists
  // only in the session attachment inbox; the predicate must report false so
  // openArtifactPath refuses the absent workspace artifact instead of opening
  // (and downloading) the uploaded copy.
  console.log('\nBranch 9: name exists only in the attachment inbox');
  apiCalls = [];
  apiImpl = async (url) => {
    if (url.includes('/api/list')) return { entries: [] };
    if (url.includes('workspace_only=1')) {
      const err = new Error('not found');
      err.status = 404;
      throw err;
    }
    // The unscoped route would have answered 200 from the inbox fallback.
    return { status: 200 };
  };
  r = await _workspacePathExists('note.txt');
  check('reports the workspace file as absent', r === false, `got ${r}`);
  check('the probe asked for workspace provenance', apiCalls.length === 2 && apiCalls[1][0].includes('workspace_only=1'), apiCalls[1] && apiCalls[1][0]);

  console.log(`\n${failures === 0 ? '✅ ALL BRANCHES OK' : `❌ ${failures} check(s) failed`}`);
  process.exit(failures === 0 ? 0 : 1);
})();
