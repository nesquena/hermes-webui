// Node-level tests for the async view fence shipped in static/ui.js
// (PR #7075 review item #5 — from Genie's review: a restore started on view A
// must never apply its projection to view B; a stale modal must not read
// another session's S.messages slot; S.restoreInFlight needs an owner).
//
// The fence block is delimited by FENCE-BLOCK-START/END sentinels and kept
// dependency-free (S only), so this test evals the EXACT shipped source range —
// no duplicated logic that could drift from the browser file.
//
// Run: node --test tests/test_view_fence_node.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';
import { test } from 'node:test';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const src = readFileSync(join(root, 'static', 'ui.js'), 'utf8');
const sessionsSrc = readFileSync(join(root, 'static', 'sessions.js'), 'utf8');

function loadFence(S) {
  const start = src.indexOf('// FENCE-BLOCK-START');
  const end = src.indexOf('// FENCE-BLOCK-END');
  assert.ok(start !== -1 && end !== -1, 'fence block markers present in ui.js');
  const block = src.slice(start, end);
  const factory = new Function(
    'S',
    `${block}\nreturn { _bumpViewGeneration, _viewToken, _viewTokenMatches, _restoreClaim, _restoreRelease };`,
  );
  return factory(S);
}

test('view generation bumps monotonically and tolerates a missing counter', () => {
  const S = {};
  const f = loadFence(S);
  assert.equal(f._bumpViewGeneration(), 1);
  assert.equal(f._bumpViewGeneration(), 2);
  // A legacy S without viewGen reads as generation 0 — never NaN.
  const S2 = {};
  const f2 = loadFence(S2);
  assert.equal(f2._viewToken().gen, 0);
});

test('tokens require a live session and the same generation (ABA safe)', () => {
  const S = { session: { session_id: 'A' } };
  const f = loadFence(S);
  const t1 = f._viewToken();
  assert.deepEqual(t1, { sid: 'A', gen: 0 });
  assert.equal(f._viewTokenMatches(t1), true);
  // Switch away -> no match.
  S.session = { session_id: 'B' };
  assert.equal(f._viewTokenMatches(t1), false);
  // Null session -> NEVER a match (no stale write into a blank view).
  S.session = null;
  assert.equal(f._viewTokenMatches(t1), false);
  // A->B->A: same id comes back, but the view was replaced -> generation differs.
  S.session = { session_id: 'A' };
  f._bumpViewGeneration();
  assert.equal(f._viewTokenMatches(t1), false);
  const t2 = f._viewToken();
  assert.equal(f._viewTokenMatches(t2), true);
});

test('restore claim release is ownership-checked', () => {
  const S = { session: { session_id: 'A' } };
  const f = loadFence(S);
  const claim = f._restoreClaim(f._viewToken());
  assert.equal(S.restoreInFlight, claim);
  assert.deepEqual(
    { sid: claim.sid, gen: claim.gen, ticket: claim.ticket },
    { sid: 'A', gen: 0, ticket: 1 },
  );
  // A newer flow replaces the claim (only possible if the older one finished);
  // the older claim's release must NOT free the newer slot.
  f._restoreClaim(f._viewToken());
  const current = S.restoreInFlight;
  f._restoreRelease(claim);
  assert.equal(S.restoreInFlight, current, 'stale release must not clear a newer claim');
  f._restoreRelease(current);
  assert.equal(S.restoreInFlight, false);
});

test('shipped restore flow consults the fence at every choke point', () => {
  assert.ok(src.includes('_viewTokenMatches(token)'), 'post-await fence in _doRestoreCheckpoint');
  assert.ok(src.includes('_viewTokenMatches(openedToken)'), 'stale-modal fence in the picker');
  assert.ok(src.includes('_doRestoreCheckpoint(target, msg, viewToken)'), 'token passed from the inline button');
  assert.ok(src.includes('_doRestoreCheckpoint(target, msg, openedToken)'), 'token passed from the picker');
  assert.ok(src.includes('_restoreClaim(token)'), 'claim taken before the destructive call');
  assert.ok(src.includes('_restoreRelease(claim)'), 'claim-owner release in finally');
});

test('every session settle point bumps the generation', () => {
  const uiBumps = (src.match(/_bumpViewGeneration\(\)/g) || []).length;
  const sessionBumps = (sessionsSrc.match(/_bumpViewGeneration\(\)/g) || []).length;
  // ui.js: 1 declaration + 4 call sites (compression-recovery fallback,
  // refresh, new-file session, new-folder session). sessions.js: 4 call sites
  // (new session, loadSession settle, and the two delete-clears).
  assert.ok(uiBumps >= 5, `ui.js fence references, got ${uiBumps}`);
  assert.ok(sessionBumps >= 4, `sessions.js fence references, got ${sessionBumps}`);
});
