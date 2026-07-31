// Offline behavioral test for the wipe-guard minHeight jump-back fix.
// Models the render sequence: capture scrollTop -> pin minHeight -> wipe (content
// height would collapse) -> rebuild (real rows) -> restore scrollTop -> release.
// The invariant under test: at RESTORE time, the scroll container's maxTop must
// still reflect the guarded (pre-wipe) height, so the restore write is NOT clamped.

function makeContainer(clientHeight){
  return {
    clientHeight,
    _contentHeight: 0,       // sum of child rows (what innerHTML drives)
    _minHeight: 0,           // inner.style.minHeight in px
    _scrollTop: 0,
    get scrollHeight(){ return Math.max(this._contentHeight, this._minHeight); },
    get maxTop(){ return Math.max(0, this.scrollHeight - this.clientHeight); },
    set scrollTop(v){ this._scrollTop = Math.max(0, Math.min(v, this.maxTop)); }, // browser clamp
    get scrollTop(){ return this._scrollTop; },
  };
}

// Simulate one mid-stream render for a reader parked mid-history.
// guardEnabled + releaseTiming model the two code variants.
function runRender({guardEnabled, releaseBeforeRestore}){
  const c = makeContainer(800);
  c._contentHeight = 5000;          // long transcript pre-wipe
  c._scrollTop = 3000;              // reader parked at 3000 (mid-history)
  const capturedTop = c._scrollTop; // snapshot.top captured BEFORE wipe

  // pin guard
  if(guardEnabled) c._minHeight = c.scrollHeight; // 5000

  // WIPE: content collapses to ~one viewport until rebuild appends rows
  c._contentHeight = 0;             // innerHTML='' — browser would clamp scrollTop now
  // browser clamp fires against current maxTop:
  c.scrollTop = c._scrollTop;       // re-run clamp with new maxTop

  // REBUILD: real rows appended. New settled height (say slightly shorter: 4900)
  const settledHeight = 4900;
  c._contentHeight = settledHeight;

  let restored;
  if(releaseBeforeRestore){
    // BUGGY variant: release guard, THEN restore
    c._minHeight = 0;
    c.scrollTop = capturedTop;      // restore (clamped against settledHeight)
    restored = c.scrollTop;
  }else{
    // FIXED variant: restore WHILE guarded, then release next frame
    c.scrollTop = capturedTop;      // restore (guard still holds height=max(0,minHeight)=5000)
    restored = c.scrollTop;
    c._minHeight = 0;               // release (next frame); reader already anchored
    c.scrollTop = c._scrollTop;     // re-clamp against settledHeight — only affects if beyond max
  }
  return {capturedTop, restored, finalTop: c.scrollTop, settledHeight};
}

function assert(name, cond){ console.log((cond?'PASS':'FAIL')+' '+name); if(!cond) process.exitCode=1; }

// 1) FIXED: reader stays at 3000 (settledHeight 4900 > 3000+800, so no legit clamp)
const fixed = runRender({guardEnabled:true, releaseBeforeRestore:false});
assert('fixed: restore lands at captured 3000', fixed.restored===3000);
assert('fixed: final position stays 3000 (no jump-back)', fixed.finalTop===3000);

// 2) NO-GUARD (original pre-fix): wipe clamps to 0, restore fights a collapsed frame
const noguard = runRender({guardEnabled:false, releaseBeforeRestore:true});
// Without guard, during wipe maxTop=0 so scrollTop clamped to 0; restore writes 3000
// against settled 4900 (maxTop 4100) so it recovers here in this simple model —
// but the REAL bug is the INTERMEDIATE clamp to 0 being painted. Assert the wipe
// clamp actually happened (the visible jump), which the guard prevents.
assert('no-guard: wipe DID clamp scrollTop to 0 (the visible jump)', (function(){
  const c=makeContainer(800); c._contentHeight=5000; c._scrollTop=3000;
  c._contentHeight=0; c.scrollTop=c._scrollTop; return c.scrollTop===0;
})());

// 3) MUTATION: guard released BEFORE restore (my first buggy fix), reader parked
//    at a valid deep position (cap=4000, valid because pre-wipe maxTop=4200).
//    Settled height comes in SHORTER (4600 -> maxTop 3800), so a release-first
//    restore clamps the reader from 4000 down to 3800 = a jump.
const shortSettle = (function(){
  const c=makeContainer(800); c._contentHeight=5000; c._scrollTop=4000;
  const cap=c._scrollTop; c._minHeight=5000; c._contentHeight=0; c.scrollTop=c._scrollTop;
  c._contentHeight=4600;            // settled maxTop=3800 < cap 4000
  c._minHeight=0;                   // release BEFORE restore (buggy)
  c.scrollTop=cap; return {cap, got:c.scrollTop, max:c.maxTop};
})();
assert('mutation(release-before-restore) CLAMPS short: got '+shortSettle.got+' < cap '+shortSettle.cap,
  shortSettle.got < shortSettle.cap);

// 4) Same short-settle but FIXED order: guard (5000 -> maxTop 4200) holds through
//    restore, so cap=4000 lands EXACTLY at 4000 (no mid-jump). Post-release the
//    reclamp against the genuine settled 4600 (maxTop 3800) is the correct final
//    settle — but crucially the reader was never yanked ABOVE their content during
//    the wipe window; the only movement is the legitimate end-of-content clamp.
const shortFixed = (function(){
  const c=makeContainer(800); c._contentHeight=5000; c._scrollTop=4000;
  const cap=c._scrollTop; c._minHeight=5000; c._contentHeight=0; c.scrollTop=c._scrollTop;
  c._contentHeight=4600;
  c.scrollTop=cap;                  // restore WHILE guarded (maxTop from minHeight 5000 = 4200)
  const restored=c.scrollTop;
  c._minHeight=0; c.scrollTop=c._scrollTop; // release+reclamp against 4600 (maxTop 3800)
  return {cap, restored, finalAfterRelease:c.scrollTop};
})();
assert('fixed(short-settle): restore lands at captured '+shortFixed.cap+' while guarded',
  shortFixed.restored===4000);

// 5) CLASS-4 (virtual bottomPad recompute): a near-bottom reader at scrollTop 2137
//    while streaming appends a tail message. The measure pass replaces an oversized
//    bottomPad ESTIMATE with the smaller MEASURED height, shrinking total content
//    from 2554 to 954 in one frame. Without the guard the browser clamps the reader
//    from 2137 down to ~154 (maxTop of 954-800). The wipe-guard minHeight (pinned to
//    the pre-wipe 2554) holds maxTop across the measure frame, so the reader is not
//    clamped; the next-frame compensation + release settle the genuine height.
function class4({guarded}){
  const c=makeContainer(800);
  c._contentHeight=2554; c._scrollTop=2137;   // near bottom (maxTop 1754... clamp to 1754)
  c.scrollTop=c._scrollTop;                    // normalize to real maxTop
  const parked=c.scrollTop;
  if(guarded) c._minHeight=c.scrollHeight;     // guard pins 2554
  c._contentHeight=954;                        // bottomPad measured smaller -> shrink
  // browser clamp fires against current maxTop (guarded: max(954,2554)=2554)
  c.scrollTop=c._scrollTop;
  return {parked, afterShrink:c.scrollTop, maxTop:c.maxTop};
}
const c4g=class4({guarded:true});
const c4n=class4({guarded:false});
assert('class4 no-guard: near-bottom reader CLAMPED by bottomPad shrink ('+c4n.parked+'->'+c4n.afterShrink+')',
  c4n.afterShrink < c4n.parked - 500);
assert('class4 guarded: reader HELD across bottomPad shrink ('+c4g.parked+'=='+c4g.afterShrink+')',
  c4g.afterShrink === c4g.parked);

// 6) CLASS-5 (virtualized measurement re-window recycle): a huge session's
//    measurement-driven re-render recycles many rendered rows to height 0 and
//    rebuilds a smaller window, collapsing total content 131767 -> 92811 in one
//    frame while the reader is parked high in history. Without a guard the browser
//    clamps the reader from scrollTop 90000 down to maxTop(92811-800)=92011... but
//    the collapse can drop maxTop below the reader → clamp. The compensation guard
//    pins minHeight to the pre-render height so maxTop can't collapse mid-frame.
function class5({guarded}){
  const c=makeContainer(800);
  c._contentHeight=131767; c._scrollTop=120000;   // parked deep in a huge session
  c.scrollTop=c._scrollTop;                        // normalize (maxTop 130967)
  const parked=c.scrollTop;
  if(guarded) c._minHeight=c.scrollHeight;         // guard pins 131767
  c._contentHeight=92811;                          // re-window recycles rows -> shrink
  c.scrollTop=c._scrollTop;                         // browser clamp fires
  return {parked, afterShrink:c.scrollTop};
}
const c5g=class5({guarded:true});
const c5n=class5({guarded:false});
assert('class5 no-guard: deep reader CLAMPED by re-window shrink ('+c5n.parked+'->'+c5n.afterShrink+')',
  c5n.afterShrink < c5n.parked - 20000);
assert('class5 guarded: reader HELD across re-window shrink ('+c5g.parked+'=='+c5g.afterShrink+')',
  c5g.afterShrink === c5g.parked);

// Maintainer blocker gates (15 assertions; total file = 24 assertions).
import fs from 'node:fs';
const uiSrc=fs.readFileSync(new URL('../static/ui.js',import.meta.url),'utf8');
const cssSrc=fs.readFileSync(new URL('../static/style.css',import.meta.url),'utf8');

assert('gate1 source: tokenized pin helper exists', uiSrc.includes('function _pinWipeMinHeight'));
assert('gate1 source: tokenized release helper exists', uiSrc.includes('function _releaseWipeMinHeight'));
assert('gate1 source: baseline recorded only for first owner', uiSrc.includes("if(!inner.dataset.wipeGuardToken) inner.dataset.wipeGuardPrevMinHeight=prev"));
assert('gate1 source: stale owner release cannot clear newer owner', uiSrc.includes("if(inner.dataset.wipeGuardToken!==String(token)) return"));
assert('gate1 source: main pin is unpinned-only', uiSrc.includes('_mainWipeGuardToken=_readerUnpinnedForGuard?_pinWipeMinHeight'));
assert('gate1 source: cache pin is unpinned-only', uiSrc.includes('_cacheGuardToken=_cacheReaderUnpinned?_pinWipeMinHeight'));
assert('gate1 source: measurement pin is unpinned-only', uiSrc.includes('_settleToken=_compUnpinned?_pinWipeMinHeight'));
assert('gate1 source: no unconditional orphan clear', !uiSrc.includes("if(inner&&inner.style&&inner.style.minHeight){ inner.style.minHeight=''; }"));

// Execute the real helper source: releasing stale owner A must not stomp owner B.
const helperStart=uiSrc.indexOf('let _wipeGuardSeq=0;');
const helperEnd=uiSrc.indexOf('function _browserOverflowAnchorActive',helperStart);
const helpers=new Function(uiSrc.slice(helperStart,helperEnd)+'; return {_pinWipeMinHeight,_releaseWipeMinHeight};')();
const owned={style:{minHeight:'17px'},dataset:{}};
const ownerA=helpers._pinWipeMinHeight(owned,100);
const ownerB=helpers._pinWipeMinHeight(owned,200);
helpers._releaseWipeMinHeight(owned,ownerA);
assert('gate1 behavior: stale owner release leaves newer pin intact', owned.style.minHeight==='200px'&&owned.dataset.wipeGuardToken===ownerB);

assert('gate2 CSS: any-pointer fine disables native anchoring', /@media\s*\(any-pointer:fine\).*?\.messages\{overflow-anchor:none;\}/s.test(cssSrc));
assert('gate2 CSS: any-hover hover disables native anchoring', /@media\s*\(any-hover:hover\).*?\.messages\{overflow-anchor:none;\}/s.test(cssSrc));
assert('gate2 JS: any-pointer fine mirrors CSS', uiSrc.includes("matchMedia('(any-pointer:fine)').matches"));
assert('gate2 JS: any-hover hover mirrors CSS', uiSrc.includes("matchMedia('(any-hover:hover)').matches"));

const restoreSrc=uiSrc.slice(uiSrc.indexOf('function _restoreMessageViewportAnchor'),uiSrc.indexOf('let _messageViewportAnchorRemounting'));
const remountSrc=uiSrc.slice(uiSrc.indexOf('function _remountMessageViewportAnchor'),uiSrc.indexOf('function _compensateScrollForMeasurementDelta'));
assert('gate3 sentinel: disappeared button uses prepend-height fallback and returns true', restoreSrc.includes('scrollHeightAtCapture')&&restoreSrc.includes('container.scrollTop=Math.max(0,container.scrollTop+_grew)'));
assert('gate3 remount: load-older sentinel refuses generic message remount', remountSrc.includes("if(anchor.special==='load-older'||anchor.key==='__load_older_indicator__') return false;"));
