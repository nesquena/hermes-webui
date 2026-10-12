// Node harness for the #7681 finding-6 optimistic turn bump.
//
// Extracts the bump block out of static/sessions.js and drives it with the exact
// sequence send() produces: three update passes over the same pending turn, where
// each pass re-reads S.session.user_message_count — which the previous pass has
// already overwritten. A correct guard keeps the count at server+1; the pre-fix
// guard let it compound.
//
// Usage: node harness_f6_bump.js <repo> '<json cases>'
const fs = require('fs');
const path = require('path');

const REPO = process.argv[2];
const src = fs.readFileSync(path.join(REPO, 'static', 'sessions.js'), 'utf8');

// Include the `let userTurns = Math.max(...)` initialisation above the guard.
const initIdx = src.indexOf('let userTurns=Math.max(serverUserTurns,optimisticUserTurns);');
if (initIdx < 0) throw new Error('userTurns initialisation not found in static/sessions.js');
const start = src.indexOf('if(userTurns===serverUserTurns&&serverUserTurns>0');
if (start < 0) throw new Error('bump block not found in static/sessions.js');
const assignIdx = src.indexOf('S.session.user_message_count=userTurns;', start);
if (assignIdx < 0) throw new Error('assignment after bump block not found');
const block = src.slice(initIdx, assignIdx);

function runPasses(serverTurns, windowTurns, passes) {
  const S = { session: { user_message_count: serverTurns, session_id: "sess-1" } };
  // Module-scope markers in the real file; carried across passes here.
  const markers = { sid: undefined, base: undefined };
  const results = [];
  for (let i = 0; i < passes; i++) {
    const body =
      'const sid = S.session.session_id;\n' +
      'let _optimisticTurnBumpSid = MARKERS.sid;\n' +
      'let _optimisticTurnBumpBase = MARKERS.base;\n' +
      'const _sendInProgress = true;\n' +
      'const _sendInProgressSid = "sess-1";\n' +
      'const serverUserTurns = Number(S.session.user_message_count || 0);\n' +
      'const optimisticUserTurns = WINDOW;\n' +
      block + '\n' +
      'MARKERS.sid = _optimisticTurnBumpSid;\n' +
      'MARKERS.base = _optimisticTurnBumpBase;\n' +
      'S.session.user_message_count = userTurns;\n' +
      'return userTurns;';
    const fn = new Function('S', 'MARKERS', 'WINDOW', body);
    results.push(fn(S, markers, windowTurns));
  }
  return results;
}

const cases = JSON.parse(process.argv[3]);
const out = {};
for (const [name, spec] of Object.entries(cases)) {
  out[name] = runPasses(spec.server, spec.window, spec.passes);
}
process.stdout.write(JSON.stringify(out));
