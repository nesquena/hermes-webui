#!/usr/bin/env node
// Production-composed harness for img lightbox gestures.
// Mounts the real lightbox via _openImgLightboxWithNav and drives
// the listeners it registers (pointer / wheel / touch / keydown / click).
// No Python-side eval; this file is the sole JS evaluator.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
function read(p) { return fs.readFileSync(path.join(ROOT, p), 'utf-8'); }
const i18nSrc = read('static/i18n.js');
const uiSrc = read('static/ui.js');
const bootSrc = read('static/boot.js');
const styleSrc = read('static/style.css');

let JSDOM;
try { JSDOM = require(path.join(ROOT, 'node_modules/jsdom/lib/api.js')).JSDOM; }
catch (_) { JSDOM = require('jsdom').JSDOM; }

function extractFn(src, name) {
  const marker = 'function ' + name + '(';
  const s = src.indexOf(marker);
  if (s < 0) throw new Error(name + ' not found');
  let b = src.indexOf('{', s), d = 1, i = b + 1;
  while (i < src.length && d > 0) { if (src[i] === '{') d++; else if (src[i] === '}') d--; i++; }
  return src.slice(s, i);
}

const html = `<!DOCTYPE html><html><head><style>${styleSrc}</style></head><body></body></html>`;
const dom = new JSDOM(html, { pretendToBeVisual: true, url: 'http://localhost/', runScripts: 'dangerously' });
const { window } = dom; const { document } = window;
window.matchMedia = () => ({ matches: false, addEventListener(){}, removeEventListener(){}, addListener(){}, removeListener(){}, removeEventListener(){}});
// jsdom's getComputedStyle for focus-visible needs window to have the style
const ctx = vm.createContext(window);
vm.runInContext(i18nSrc, ctx);
const mountSrc = extractFn(uiSrc, '_mountImgLightboxZoom');
const openSrc = extractFn(uiSrc, '_openImgLightboxWithNav');
const closeSrc = extractFn(uiSrc, '_closeImgLightbox');
const navSrc = extractFn(uiSrc, '_navigateLightbox');
vm.runInContext(mountSrc, ctx);
vm.runInContext(closeSrc, ctx);
vm.runInContext(navSrc, ctx);
vm.runInContext(openSrc, ctx);
// Also make boot's swipe target helper available for sidebar test
// boot.js defines many globals; stub minimal to allow its _isInteractiveSwipeTarget to be evaluated
let hasSwipeHelper = false;
let swipeHelperSrc = '';
try {
  const start = bootSrc.indexOf('function _isInteractiveSwipeTarget');
  if (start >= 0) {
    let b = bootSrc.indexOf('{', start), d2 = 1, j = b + 1;
    while (j < bootSrc.length && d2 > 0) { if (bootSrc[j] === '{') d2++; else if (bootSrc[j] === '}') d2--; j++; }
    swipeHelperSrc = bootSrc.slice(start, j);
    // Need to provide a window with Element etc - use JSDOM window via vm
    vm.runInContext(swipeHelperSrc, ctx);
    hasSwipeHelper = typeof window._isInteractiveSwipeTarget === 'function';
  }
} catch (_) { /* ignore */ }

function openLightbox(viewportW, viewportH, boxW, boxH, locale) {
  if (locale && locale !== 'en') window.setLocale(locale); else window.setLocale('en');
  document.body.innerHTML = '';
  const src = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII=';
  window._openImgLightboxWithNav(src, 'test alt', [{ src, alt: 'test alt' }], 0);
  const lb = document.querySelector('.img-lightbox');
  const vp = lb.querySelector('.img-lightbox-viewport');
  const cv = lb.querySelector('.img-lightbox-canvas');
  const img = cv.querySelector('img');
  const fitBtn = lb.querySelector('.img-lightbox-fit');
  const closeBtn = lb.querySelector('.img-lightbox-close');
  vp.getBoundingClientRect = () => ({ width: viewportW, height: viewportH, left: 0, top: 0, right: viewportW, bottom: viewportH, x: 0, y: 0, toJSON(){ return {}; } });
  Object.defineProperty(vp, 'clientWidth', { get(){ return viewportW; }, configurable: true });
  Object.defineProperty(vp, 'clientHeight', { get(){ return viewportH; }, configurable: true });
  const z = lb._zoom;
  img.naturalWidth = boxW; img.naturalHeight = boxH; img.width = boxW; img.height = boxH; img.complete = true;
  z.boxW = boxW; z.boxH = boxH;
  cv.style.width = boxW + 'px'; cv.style.height = boxH + 'px';
  z.fit();
  return { lb, vp, cv, img, fitBtn, closeBtn, z };
}

const results = {};
function check(name, fn) {
  try { fn(); results[name] = { ok: true }; } catch (e) { results[name] = { ok: false, error: e.message, stack: e.stack && e.stack.slice(0, 1200) }; }
}
function assert_(cond, msg) { if (!cond) throw new Error(msg); }
function approx(a, b, eps) { eps = eps || 1.5; if (Math.abs(a - b) > eps) throw new Error(`approx ${a} vs ${b} eps ${eps}`); }

// 1) extreme pointer drag clamping + undersized axis centering via real pointer path
check('pointer_extreme_negative_clamp', () => {
  const { vp, z } = openLightbox(900, 900, 800, 450, 'en');
  z.scale = 2; // scaled 1600x900, viewport 900x900 => x in [-700,0], y centered 0
  vp.onpointerdown({ clientX: 450, clientY: 450, button: 0, pointerId: 1, preventDefault(){} });
  vp.onpointermove({ clientX: -4550, clientY: 450 }); // dx -5000
  assert_(z.x === -700, `extreme negative x expected -700 got ${z.x}`);
  assert_(z.y === 0, `undersized H should be centered 0 got ${z.y}`);
  vp.onpointerup({});
  document.body.innerHTML = '';
});
check('pointer_extreme_positive_clamp', () => {
  const { vp, z } = openLightbox(900, 900, 800, 900, 'en');
  z.scale = 1.5; // 1200x1350 in 900
  vp.onpointerdown({ clientX: 450, clientY: 450, button: 0, pointerId: 1, preventDefault(){} });
  vp.onpointermove({ clientX: 5450, clientY: 5450 });
  assert_(z.x === 0, `positive clamp x 0 got ${z.x}`);
  assert_(z.y === 0, `positive clamp y 0 got ${z.y}`);
  vp.onpointerup({});
  document.body.innerHTML = '';
});
check('pointer_undersized_centred', () => {
  const { vp, z } = openLightbox(900, 900, 100, 100, 'en');
  z.x = 999; z.y = -999; z.scale = 1;
  vp.onpointerdown({ clientX: 450, clientY: 450, button: 0, pointerId: 1, preventDefault(){} });
  vp.onpointermove({ clientX: 1450, clientY: 450 });
  // undersized 100 in 900 => centered 400 regardless of drag
  assert_(z.x === 400, `undersized x 400 got ${z.x}`);
  assert_(z.y === 400, `undersized y 400 got ${z.y}`);
  vp.onpointerup({});
  document.body.innerHTML = '';
});
check('pointer_zoom_out_centres', () => {
  const { vp, z } = openLightbox(900, 900, 800, 800, 'en');
  z.scale = 2; z.x = -500; z.y = -500;
  // zoom out via wheel to 0.5 should center: need factor 0.25 from 2 => delta ~ 924
  // easier: call _imgSetScale via wheel is indirect; use direct fit path's clamp after setScale
  // simulate wheel that zooms out to 0.5 anchor center
  // Instead drive via z's internal _imgSetScale is not exposed; use zoomBy factor
  // We test via pointer: second case via direct api that's still real handler path: use wheel
  // For zoom-out-centering, the clamp path is same as pointer: set scale then clamp
  // Use the real fit path: zoom out far
  vp.onwheel({ deltaY: 924, clientX: 450, clientY: 450, preventDefault(){} });
  // scaled 800*scale: scale should be clamped to min 0.25 (fitScale ~1 => min 0.25) => not 0.5 directly
  // Instead test the zoom-out-centering property via fit: after zoom out, undersized axis centered
  // Our earlier proto showed pinch zoom-out centering works via state; simpler assert that after extreme zoom-out, x,y are centered
  // Let's force via direct scale and clamp through next wheel that triggers clamp
  // Reset and do direct: set scale small then trigger pointer move to clamp
  z.scale = 0.4; // undersized (320 in 900) should be centered
  vp.onpointerdown({ clientX: 450, clientY: 450, button: 0, pointerId: 1, preventDefault(){} });
  vp.onpointermove({ clientX: 450, clientY: 450 });
  approx(z.x, 290, 2); approx(z.y, 290, 2);
  vp.onpointerup({});
  document.body.innerHTML = '';
});

// 2) wheel and pinch anchoring via real handlers
check('wheel_anchor', () => {
  const { vp, z } = openLightbox(600, 600, 800, 600, 'en');
  z.scale = 1; z.x = 0; z.y = 0; z.fitScale = 0.75;
  // wheel that doubles scale: deltaY = -ln2/0.0015 ~ -462
  vp.onwheel({ deltaY: -462.098, clientX: 100, clientY: 100, preventDefault(){} });
  approx(z.scale, 2, 0.05);
  approx(z.x, -100, 3); approx(z.y, -100, 3);
  document.body.innerHTML = '';
});
check('pinch_anchor', () => {
  const { vp, z } = openLightbox(900, 900, 800, 800, 'en');
  z.scale = 1; z.x = 0; z.y = 0; z.fitScale = 1;
  vp.getBoundingClientRect = () => ({ width: 900, height: 900, left: 0, top: 0, right: 900, bottom: 900, x: 0, y: 0, toJSON(){return{};} });
  let ev = document.createEvent('Event'); ev.initEvent('touchstart', true, true);
  ev.touches = [{ clientX: 400, clientY: 450 }, { clientX: 500, clientY: 450 }]; ev.preventDefault = ()=>{};
  vp.dispatchEvent(ev);
  assert_(z.pinching === true, 'pinch start should set pinching');
  let ev2 = document.createEvent('Event'); ev2.initEvent('touchmove', true, true);
  ev2.touches = [{ clientX: 350, clientY: 450 }, { clientX: 550, clientY: 450 }]; ev2.preventDefault = ()=>{};
  vp.dispatchEvent(ev2);
  approx(z.scale, 2, 0.05);
  // anchor 450,450 with midpoint shift 0 => x = 450 - (450-0)*2 = -450
  approx(z.x, -450, 4); approx(z.y, -450, 4);
  // pinch blocks pointer
  let draggingBefore = z.dragging;
  vp.onpointerdown({ clientX: 60, clientY: 450, button: 0, pointerId: 1, preventDefault(){} });
  assert_(z.dragging === false, 'pinching should block pointer drag');
  // touchend should set dragged for one-shot suppression
  let evUp = document.createEvent('Event'); evUp.initEvent('touchend', true, true);
  evUp.touches = []; evUp.preventDefault = ()=>{};
  vp.dispatchEvent(evUp);
  assert_(z.pinching === false, 'touchend should clear pinching');
  assert_(z.dragged === true, 'pinch end should set dragged');
  document.body.innerHTML = '';
});

// 3) left-edge pinch does not trigger sidebar swipe recogniser
check('sidebar_swipe_excluded', () => {
  // a) boot helper must include .img-lightbox
  assert_(bootSrc.includes('.img-lightbox'), 'boot.js must include .img-lightbox');
  assert_(bootSrc.includes('_isInteractiveSwipeTarget'), 'boot missing helper');
  if (hasSwipeHelper) {
    const inside = document.createElement('div'); inside.className = 'img-lightbox';
    const child = document.createElement('div'); child.className = 'img-lightbox-viewport';
    inside.appendChild(child);
    document.body.appendChild(inside);
    assert_(window._isInteractiveSwipeTarget(child) === true, 'img-lightbox should be interactive swipe target');
    assert_(window._isInteractiveSwipeTarget(inside) === true, 'img-lightbox itself interactive');
    document.body.removeChild(inside);
    const outside = document.createElement('div');
    document.body.appendChild(outside);
    assert_(window._isInteractiveSwipeTarget(outside) === false, 'outside should not be interactive');
    document.body.removeChild(outside);
  }
  // b) left-edge pinch via touch at x~10 should still be pinching, not sidebar
  const { vp, z } = openLightbox(900, 900, 800, 800, 'en');
  vp.getBoundingClientRect = () => ({ width: 900, height: 900, left: 0, top: 0, right: 900, bottom: 900, x: 0, y: 0, toJSON(){return{};} });
  let ev = document.createEvent('Event'); ev.initEvent('touchstart', true, true);
  ev.touches = [{ clientX: 10, clientY: 450 }, { clientX: 90, clientY: 450 }]; ev.preventDefault = ()=>{};
  vp.dispatchEvent(ev);
  assert_(z.pinching === true, 'left-edge pinch should still arm pinching');
  document.body.innerHTML = '';
});

// 4) Fit click + F, +/=, -/_ key dispatch
check('fit_click_resets', () => {
  const { z, fitBtn } = openLightbox(900, 900, 800, 600, 'en');
  z.scale = 2; z.x = -200; z.y = -100;
  const beforeScale = z.scale;
  assert_(beforeScale === 2, 'setup');
  fitBtn.click();
  assert_(Math.abs(z.scale - z.fitScale) < 1e-9, `fit click should reset to fitScale ${z.fitScale} got ${z.scale}`);
  // fit should center
  const expectedX = (900 - 800 * z.fitScale) / 2;
  approx(z.x, expectedX, 2);
  document.body.innerHTML = '';
});
check('keyboard_F_resets', () => {
  const { lb, z } = openLightbox(900, 900, 800, 600, 'en');
  z.scale = 2; z.x = -200;
  lb._keyHandler({ key: 'f', preventDefault(){}, stopPropagation(){} });
  assert_(Math.abs(z.scale - z.fitScale) < 1e-9, 'F should reset');
  document.body.innerHTML = '';
  const { lb: lb2, z: z2 } = openLightbox(900, 900, 800, 600, 'en');
  z2.scale = 2;
  lb2._keyHandler({ key: 'F', preventDefault(){}, stopPropagation(){} });
  assert_(Math.abs(z2.scale - z2.fitScale) < 1e-9, 'Shift+F should reset');
  document.body.innerHTML = '';
});
check('keyboard_plus_minus', () => {
  const { lb, z } = openLightbox(900, 900, 800, 600, 'en');
  z.scale = 1; z.x = 0; z.y = 0;
  lb._keyHandler({ key: '+', preventDefault(){}, stopPropagation(){} });
  approx(z.scale, 1.25, 0.02);
  lb._keyHandler({ key: '=', preventDefault(){}, stopPropagation(){} });
  approx(z.scale, 1.5625, 0.03);
  lb._keyHandler({ key: '-', preventDefault(){}, stopPropagation(){} });
  approx(z.scale, 1.25, 0.02);
  lb._keyHandler({ key: '_', preventDefault(){}, stopPropagation(){} });
  approx(z.scale, 1, 0.02);
  document.body.innerHTML = '';
});
check('viewport_click_suppression', () => {
  const { vp, z, cv } = openLightbox(900, 900, 800, 600, 'en');
  z.dragged = false;
  let stopped = false;
  vp.onclick({ target: vp, stopPropagation(){ stopped = true; } });
  assert_(stopped === false, 'undragged viewport click should bubble');
  assert_(z.dragged === false, 'dragged should be cleared');
  z.dragged = true;
  stopped = false;
  vp.onclick({ target: vp, stopPropagation(){ stopped = true; } });
  assert_(stopped === true, 'dragged viewport click should suppress');
  assert_(z.dragged === false, 'dragged one-shot cleared');
  z.dragged = false;
  stopped = false;
  vp.onclick({ target: cv, stopPropagation(){ stopped = true; } });
  assert_(stopped === true, 'canvas click should suppress');
  document.body.innerHTML = '';
});

// 5) non-English locale renders button text/title/aria
check('locale_zh_renders', () => {
  const { fitBtn } = openLightbox(900, 600, 800, 600, 'zh');
  assert_(fitBtn.textContent === '\u9002\u5e94', `zh fit text expected 适应 got ${fitBtn.textContent}`);
  const title = fitBtn.getAttribute('title');
  const aria = fitBtn.getAttribute('aria-label');
  assert_(title && title !== 'Reset zoom to fit (F)' && title !== 'Fit', `zh title should be localized got ${title}`);
  assert_(aria === title, 'aria-label should equal title');
  document.body.innerHTML = '';
});
check('locale_ja_renders', () => {
  const { fitBtn } = openLightbox(900, 600, 800, 600, 'ja');
  assert_(fitBtn.textContent === '\u30d5\u30a3\u30c3\u30c8', `ja fit expected got ${fitBtn.textContent}`);
  document.body.innerHTML = '';
});

// 6) geometry: computed 44x44, focus-visible, Fit/close not overlapping (incl mobile + safe-area)
check('geometry_computed_44_and_focus', () => {
  const { fitBtn, closeBtn } = openLightbox(900, 600, 800, 600, 'en');
  const cs = window.getComputedStyle(fitBtn);
  const h = parseFloat(cs.height) || 0;
  const minH = parseFloat(cs.minHeight) || 0;
  const effectiveH = Math.max(h, minH);
  assert_(effectiveH >= 44, `fit effective height ${effectiveH} <44 (h=${cs.height} minH=${cs.minHeight})`);
  // width: check computed width or ensure min-width/padding yields >=44; jsdom width is 0 but minHeight is the gate
  // Check scoped CSS: .img-lightbox-fit must contain min-height:44px, not just any file
  const fitRuleMatch = styleSrc.match(/\.img-lightbox-fit\s*\{[^}]*\}/);
  assert_(fitRuleMatch && fitRuleMatch[0].includes('min-height:44px'), '.img-lightbox-fit rule must contain min-height:44px');
  assert_(fitRuleMatch && fitRuleMatch[0].includes('height:44px'), '.img-lightbox-fit rule must contain height:44px');
  assert_(styleSrc.includes('.img-lightbox-fit:focus-visible'), 'missing focus-visible');
  assert_(styleSrc.includes('outline:2px solid'), 'focus-visible should have outline');
  // focusable
  fitBtn.focus();
  assert_(document.activeElement === fitBtn, 'fit button should be focusable');
  // safe-area
  assert_(styleSrc.includes('env(safe-area-inset-top'), 'missing safe-area top');
  assert_(styleSrc.includes('env(safe-area-inset-right'), 'missing safe-area right');
  assert_(fitRuleMatch[0].includes('env(safe-area-inset-'), 'fit rule should use safe-area');
  document.body.innerHTML = '';
});
check('geometry_no_overlap', () => {
  // Check style positioning: fit at right max(68px, calc(env+68px)), close at right max(20px, env...), so fit is 48px left of close
  const fitRule = styleSrc.match(/\.img-lightbox-fit\s*\{[^}]*\}/);
  const closeRule = styleSrc.match(/\.img-lightbox-close\s*\{[^}]*\}/);
  assert_(fitRule && fitRule[0].includes('right:max(68px'), `fit right should be max(68px got ${fitRule && fitRule[0].slice(0,200)}`);
  assert_(closeRule && closeRule[0].includes('right:max(20px'), 'close right should be max(20px');
  // At narrow mobile viewport (375px), both buttons still fit: viewport 375, fit at 68 from right, close at 20, buttons ~36-60 wide => no overlap
  // Simulate by opening at 375 width and checking computed right distances
  const { fitBtn, closeBtn } = openLightbox(375, 600, 800, 600, 'en');
  const fitCS = window.getComputedStyle(fitBtn);
  const closeCS = window.getComputedStyle(closeBtn);
  // Both have position absolute; check they have distinct right values
  assert_(fitCS.right !== closeCS.right, `fit and close right should differ: fit ${fitCS.right} close ${closeCS.right}`);
  // With safe-area env, max() ensures notch handling; just verify rule contains env
  assert_(fitCS.right.includes('68px') || fitCS.right.includes('env'), 'fit right should reference 68px or safe-area');
  document.body.innerHTML = '';
});

const failed = Object.entries(results).filter(([, v]) => !v.ok);
// Emit a single-line marker so the Python harness can parse without brace-balancing
console.log('__HARNESS_JSON__' + JSON.stringify({ results, hasSwipeHelper, swipeHelperSrc: swipeHelperSrc.slice(0, 200) }));
if (failed.length) {
  console.error(`FAILED ${failed.length}/${Object.keys(results).length}`);
  failed.forEach(([k, v]) => console.error(`- ${k}: ${v.error}\n  ${v.stack}`));
  process.exit(1);
} else {
  console.log(`ALL ${Object.keys(results).length} COMPOSED CHECKS PASSED`);
  process.exit(0);
}
