// Test harness for the two greptile-flagged fixes in _attachImgZoom.
// Extracts the real function body from static/ui.js, runs it against a
// mock DOM, and asserts the two bugs are fixed. Mutation-checked: reverting
// either fix must make the corresponding test FAIL.
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'static', 'ui.js'), 'utf8');

// Extract the _attachImgZoom function body verbatim (balanced-brace scan).
function extractFn(name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('fn not found: ' + name);
  let i = src.indexOf('{', start), depth = 0, end = -1;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
  }
  return src.slice(start, end);
}

const attachSrc = extractFn('_attachImgZoom');

// ── Mock DOM ──
function makeImg(scale0 = 1, natW = 800, natH = 600) {
  const listeners = {};
  const img = {
    style: {},
    _rectW: natW, _rectH: natH, _cx: 500, _cy: 400,
    getBoundingClientRect() {
      // Rendered size scales with the CURRENT applied scale (parsed from transform).
      const m = /scale\(([\d.]+)\)/.exec(img.style.transform || 'scale(1)');
      const s = m ? parseFloat(m[1]) : 1;
      const w = img._rectW * s, h = img._rectH * s;
      return { width: w, height: h, left: img._cx - w / 2, top: img._cy - h / 2 };
    },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    _fire(type, ev) { (listeners[type] || []).forEach(fn => fn(ev)); },
  };
  return img;
}
function makeLb() { return { classList: { toggle() {} }, _imgZoom: null }; }

global.window = {
  innerWidth: 1000, innerHeight: 800,
  addEventListener() {}, removeEventListener() {},
};
global.document = { addEventListener() {}, removeEventListener() {} };
global.Math = Math;

function instantiate() {
  const lb = makeLb(), img = makeImg();
  const fn = new Function('lb', 'img', 'window', 'document', 'Math',
    attachSrc + '\n_attachImgZoom(lb, img);');
  fn(lb, img, global.window, global.document, Math);
  return { lb, img };
}

let pass = 0, fail = 0;
function assert(cond, msg) { if (cond) { pass++; console.log('  PASS ' + msg); } else { fail++; console.log('  FAIL ' + msg); } }

// ── TEST 1: cursor-anchored zoom survives the first zoom step from 1x ──
// Bug: clampPan used old (1x) size → bounds ~0 → offset clamped to ~0 → zoom
// snaps to center. Fixed: clampPan projects to nextScale so the offset survives.
console.log('TEST 1: cursor-anchored double-click zoom from 1x');
{
  const { lb, img } = instantiate();
  const st = lb._imgZoom;
  // double-click near the right edge of the image (cx=500 center; click at 800)
  img._fire('dblclick', { clientX: 800, clientY: 400, preventDefault(){}, stopPropagation(){} });
  // After zooming to 2.5x anchored at x=800 (300px right of center), tx should be
  // pulled strongly negative (image shifts left so the clicked point stays put),
  // NOT clamped to ~0 (which is the bug = center zoom).
  // Expected raw offset before clamp: tx = -300*(2.5-1) = -450. Projected bounds at
  // 2.5x: projW=800*2.5=2000, maxX=(2000-1000)/2+40=540. So -450 is within ±540 → survives.
  assert(st.scale === 2.5, 'scale became 2.5 (got ' + st.scale + ')');
  assert(st.tx < -300, 'tx anchored to cursor, strongly negative (got ' + st.tx.toFixed(1) + ', bug would give ~0)');
}

// ── TEST 2: navigation reset clears in-flight mouse drag ──
// Bug: st.reset() left mDown=true → the next mousemove (button still held while
// arrow-key navigating) applies the previous image's drag to the new image.
// Fixed: reset() calls _clearGestures() which sets mDown=false.
// Note: the drag must be probed while the NEW image is zoomed, otherwise
// clampPan at scale=1 forces tx to 0 and masks the stale-drag regardless.
console.log('TEST 2: nav reset clears active drag gesture');
{
  const captured = {};
  const savedWin = global.window;
  global.window = { innerWidth:1000, innerHeight:800,
    addEventListener(t, fn){ captured[t] = fn; }, removeEventListener(){} };
  const lb = makeLb(), img = makeImg();
  const fn = new Function('lb','img','window','document','Math',
    attachSrc + '\n_attachImgZoom(lb, img);');
  fn(lb, img, global.window, global.document, Math);
  const st = lb._imgZoom;
  // Zoom in, then press the mouse button (arming a drag: mDown=true).
  img._fire('dblclick', { clientX:500, clientY:400, preventDefault(){}, stopPropagation(){} });
  img._fire('mousedown', { clientX:500, clientY:400, preventDefault(){}, stopPropagation(){} });
  // Navigate to the next image → reset(). With the fix this also clears mDown.
  st.reset();
  assert(st.scale === 1 && st.tx === 0, 'reset zeroed transform');
  // The new image is zoomed by the user (its own gesture). If the OLD drag's
  // mDown leaked through reset, the very first mousemove would fire the stale
  // drag branch. Zoom the new image so a leaked drag is observable (clampPan
  // won't force tx to 0 at 2.5x).
  img._fire('dblclick', { clientX:500, clientY:400, preventDefault(){}, stopPropagation(){} });
  // Stray mousemove with NO fresh mousedown. Fixed: mDown was cleared by reset,
  // so this is a no-op and tx stays at the (centered) zoom value 0. Buggy:
  // mDown still true → st.tx = mTx + (900-500) = 400 (stale drag applied).
  if (captured.mousemove) captured.mousemove({ clientX: 900, clientY: 700 });
  assert(st.tx === 0,
    'stray mousemove after reset is ignored (tx=' + st.tx + '; bug would apply stale drag → ~400)');
  global.window = savedWin;
}

console.log('\n' + (fail === 0 ? 'ALL PASS' : fail + ' FAILED') + ' (' + pass + ' passed)');
process.exit(fail === 0 ? 0 : 1);
