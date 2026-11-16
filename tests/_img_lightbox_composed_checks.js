/* In-page behavioural checks for the image-lightbox gestures.
 *
 * Injected into the real application page by
 * tests/test_img_lightbox_gestures_composed.py, which runs Chromium through
 * Playwright against the server the pytest session already boots.
 *
 * Every check drives the production lightbox and the listeners the
 * application itself registered: real PointerEvent / WheelEvent / TouchEvent
 * / KeyboardEvent objects are dispatched at the production targets (the
 * gesture viewport, the Fit button, and `document` for the keydown handler),
 * and geometry comes from the browser's own layout engine. There is no DOM
 * emulation layer here and no third-party JS package: the page under test is
 * the app the server already serves.
 *
 * Coverage mirrors the six review-required points:
 *   1. extreme drag clamping & undersized-axis centring via the pointer path
 *   2. wheel and two-finger pinch anchoring via the production handlers
 *   3. left-edge touch inside the lightbox never arms the sidebar swipe
 *      recogniser (with a positive control proving the recogniser is live)
 *   4. Fit click + F / + / = / - / _ change and reset production zoom state
 *   5. selected non-English locale localizes the Fit control's title/aria and
 *      it renders the shared fit glyph
 *   6. the Fit control measures as the close button's 36px circle with the
 *      counter's dark recipe behind both, and the two never overlap at
 *      desktop and mobile widths (real getBoundingClientRect, not source
 *      strings)
 *   7. the 2026-10-05 greptile follow-ups: a second pointer cannot take the
 *      pan over, the document-wide shortcuts never hijack a background
 *      editable field, focus moves into the dialog on open, and a failed
 *      image load drops the previous image's geometry while preserving the
 *      user's zoom level (with a recovery control proving the stage rebuilds
 *      — at that zoom — on the next good load)
 */
(function () {
  "use strict";

  var IMG_W = 800;
  var IMG_H = 450;

  function svgDataUrl(w, h) {
    var svg =
      '<svg xmlns="http://www.w3.org/2000/svg" width="' + w + '" height="' + h +
      '"><rect width="100%" height="100%" fill="#3a7"/></svg>';
    return "data:image/svg+xml;base64," + window.btoa(svg);
  }

  function currentLb() {
    return document.querySelector(".img-lightbox");
  }

  function closeLightbox() {
    var lb = currentLb();
    if (lb && lb.parentNode) {
      try { window._closeImgLightbox(lb); } catch (_) { /* keep going */ }
    }
    var stray = currentLb();
    if (stray && stray.parentNode) stray.parentNode.removeChild(stray);
  }

  function frame() {
    return new Promise(function (resolve) {
      requestAnimationFrame(function () { resolve(); });
    });
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  async function openBox(w, h, opts) {
    opts = opts || {};
    closeLightbox();
    window._openImgLightboxWithNav(
      svgDataUrl(w, h),
      opts.alt || "test alt",
      opts.images || null,
      opts.index || 0
    );
    var lb = currentLb();
    if (!lb) throw new Error("lightbox did not open");
    var z = lb._zoom;
    if (!z) throw new Error("lightbox zoom state missing");
    for (var i = 0; i < 180 && !z.boxW; i++) await frame();
    if (!z.boxW) throw new Error("image never decoded (boxW stayed 0)");
    for (var j = 0; j < 4; j++) await frame();
    return {
      lb: lb,
      z: z,
      vp: lb.querySelector(".img-lightbox-viewport"),
      cv: lb.querySelector(".img-lightbox-canvas"),
      img: lb.querySelector(".img-lightbox-canvas img"),
      fitBtn: lb.querySelector(".img-lightbox-fit"),
      closeBtn: lb.querySelector(".img-lightbox-close"),
    };
  }

  function vpSize(vp) {
    var r = vp.getBoundingClientRect();
    return { w: Math.max(1, r.width), h: Math.max(1, r.height) };
  }

  // `pointerType` defaults to "mouse" for the pointer-only checks; a gesture
  // that must thread the real touch stream passes "touch", because the pan
  // re-armed after a pinch looks the contact up by the id carried here.
  function pointer(vp, type, x, y, id, pointerType) {
    var pid = id == null ? 1 : id;
    vp.dispatchEvent(new PointerEvent(type, {
      bubbles: true,
      cancelable: true,
      composed: true,
      pointerId: pid,
      pointerType: pointerType || "mouse",
      isPrimary: pid === 1,
      button: 0,
      buttons: type === "pointerup" ? 0 : 1,
      clientX: x,
      clientY: y,
    }));
  }

  function wheel(vp, dy, x, y, mode) {
    vp.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      composed: true,
      deltaMode: mode == null ? 0 : mode,
      deltaY: dy,
      clientX: x,
      clientY: y,
    }));
  }

  // `points` are {x, y} (identifier defaults to the index + 1) or
  // {x, y, id} when a check needs stable touch identifiers across events —
  // replacing one finger of a pinch means the remaining pair must keep ITS
  // identifiers (see the item-5 checks).
  function touch(el, type, points) {
    var list = points.map(function (p, i) {
      return new Touch({
        identifier: p.id == null ? i + 1 : p.id,
        target: el,
        clientX: p.x,
        clientY: p.y,
        pageX: p.x,
        pageY: p.y,
        screenX: p.x,
        screenY: p.y,
      });
    });
    el.dispatchEvent(new TouchEvent(type, {
      bubbles: true,
      cancelable: true,
      composed: true,
      touches: list,
      targetTouches: list,
      changedTouches: list,
    }));
  }

  function key(k) {
    document.dispatchEvent(new KeyboardEvent("keydown", {
      key: k,
      bubbles: true,
      cancelable: true,
    }));
  }

  function assert_(cond, msg) {
    if (!cond) throw new Error(msg);
  }

  function approx(actual, expected, eps, label) {
    eps = eps == null ? 1.5 : eps;
    if (!(Math.abs(actual - expected) <= eps)) {
      throw new Error((label || "approx") + ": " + actual + " vs " + expected + " (eps " + eps + ")");
    }
  }

  function dragTo(vp, from, to) {
    pointer(vp, "pointerdown", from.x, from.y);
    pointer(vp, "pointermove", (from.x + to.x) / 2, (from.y + to.y) / 2);
    pointer(vp, "pointermove", to.x, to.y);
    pointer(vp, "pointerup", to.x, to.y);
  }

  function results() {
    return {};
  }

  function run(name, bucket, fn) {
    return Promise.resolve()
      .then(fn)
      .then(function () { bucket[name] = { ok: true }; })
      .catch(function (e) {
        bucket[name] = {
          ok: false,
          error: String((e && e.message) || e),
          stack: String((e && e.stack) || "").slice(0, 1200),
        };
      })
      .then(function () { closeLightbox(); });
  }

  // Fit carries over navigation, not the raw scale (reviewer re-gate
  // 2026-10-08T23:18:39Z, static/ui.js:2800). A small image shown at its fit
  // followed by a large one must open the large one at ITS OWN fit — keeping
  // the numeric scale cropped a 4000x3000 image at scale 1, and the reverse
  // navigation shrank the small image to ~25px. A genuine user zoom is still
  // retained across the navigation.
  async function navigationCarriesFit() {
    var small = 100, bigW = 4000, bigH = 3000;
    var box = await openBox(small, small, {
      images: [
        { src: svgDataUrl(small, small), alt: "tiny" },
        { src: svgDataUrl(bigW, bigH), alt: "huge" },
      ],
      index: 0,
    });
    approx(box.z.scale, box.z.fitScale, 1e-9, "fixture: the tiny image must open at its fit");
    var tinyFit = box.z.fitScale;
    key("ArrowRight");
    for (var i = 0; i < 240 && box.z.pendingNav; i++) await frame();
    for (var a = 0; a < 4; a++) await frame();
    assert_(box.z.boxW === bigW, "navigation must adopt the big image's geometry");
    assert_(box.z.fitScale < 0.5, "fixture: the big image needs a real downscale, fit=" + box.z.fitScale);
    approx(
      box.z.scale, box.z.fitScale, 1e-9,
      "an at-fit image must open the new image at its fit, not the raw scale: " +
        box.z.scale + " vs fit " + box.z.fitScale
    );
    // Reverse: the big image is still at fit, so the tiny one must fit again.
    key("ArrowLeft");
    for (var j = 0; j < 240 && box.z.pendingNav; j++) await frame();
    for (var b = 0; b < 4; b++) await frame();
    assert_(box.z.boxW === small, "navigation back must adopt the tiny geometry");
    approx(box.z.scale, tinyFit, 1e-9, "the reverse navigation must restore the tiny image's fit");
    // Positive control: a real user zoom still survives the navigation.
    key("+");
    key("+");
    var zoomed = box.z.scale;
    assert_(zoomed > box.z.fitScale, "fixture: expected zoom-in before navigating");
    key("ArrowRight");
    for (var k = 0; k < 240 && box.z.pendingNav; k++) await frame();
    for (var c = 0; c < 4; c++) await frame();
    approx(box.z.scale, zoomed, 1e-9, "a genuine user zoom must still survive the navigation");
  }

  // A failed load must not break the at-fit carry-over. _imgOnError used to
  // reset fitScale to 1 while preserving scale, so an image that WAS at fit
  // (fitScale != 1) looked user-zoomed and the next working image opened
  // cropped/undersized instead of at its own fit (greptile P1,
  // 2026-10-08T23:43:03Z).
  async function navigationAfterAFailedLoadCarriesFit() {
    var box = await openBox(4000, 3000, {
      images: [
        { src: svgDataUrl(4000, 3000), alt: "huge" },
        { src: "data:image/png;base64,AAAA", alt: "broken" },
        { src: svgDataUrl(1600, 900), alt: "medium" },
      ],
      index: 0,
    });
    approx(box.z.scale, box.z.fitScale, 1e-9, "fixture: the huge image opens at its fit");
    assert_(box.z.fitScale < 1, "fixture: the huge image needs a downscale, fit=" + box.z.fitScale);
    key("ArrowRight");
    for (var i = 0; i < 180 && box.z.boxW; i++) await frame();
    assert_(box.z.boxW === 0, "the broken image must drop the geometry");
    approx(
      box.z.scale, box.z.fitScale, 1e-9,
      "a failed load must keep the at-fit relation, not reset fitScale to 1: " +
        box.z.scale + " vs fit " + box.z.fitScale
    );
    key("ArrowRight");
    for (var j = 0; j < 180 && !box.z.boxW; j++) await frame();
    for (var a = 0; a < 4; a++) await frame();
    assert_(box.z.boxW === 1600, "the recovered load must adopt the medium image's geometry");
    approx(
      box.z.scale, box.z.fitScale, 1e-9,
      "an image that was at fit before a failed load must open the next one at ITS fit: " +
        box.z.scale + " vs fit " + box.z.fitScale
    );
  }

  async function navigationAfterAFailedLoadKeepsADeliberateScale() {
    // Maintainer re-gate 2026-10-09T00:47:03Z (static/ui.js:2861): a deliberate
    // zoom must survive a failed load. _imgOnError used to rewrite fitScale
    // (first to a constant 1, then to state.scale), so a zoom that landed on
    // exactly the baseline was reclassified as "at fit" and the next image
    // threw it away. The 1350x800 start fits at ~0.8 and "=" multiplies by
    // 1.25, which is exactly the reported 0.8 -> 1.0 -> 0.675 loss.
    var box = await openBox(1350, 800, {
      images: [
        { src: svgDataUrl(1350, 800), alt: "wide" },
        { src: "data:image/png;base64,AAAA", alt: "broken" },
        { src: svgDataUrl(1600, 900), alt: "medium" },
      ],
      index: 0,
    });
    approx(box.z.scale, box.z.fitScale, 1e-9, "fixture: the wide image opens at its fit");
    assert_(box.z.fitScale < 1, "fixture: the wide image needs a downscale, fit=" + box.z.fitScale);
    var fitBefore = box.z.fitScale;
    key("=");
    var deliberate = box.z.scale;
    assert_(deliberate > fitBefore, "fixture: '=' must zoom past the fit, got " + deliberate);
    key("ArrowRight");
    for (var i = 0; i < 180 && box.z.boxW; i++) await frame();
    assert_(box.z.boxW === 0, "the broken image must drop the geometry");
    approx(
      box.z.fitScale, fitBefore, 1e-9,
      "a failed load must RETAIN the previous fit baseline instead of rewriting it: " +
        box.z.fitScale + " vs " + fitBefore
    );
    approx(
      box.z.scale, deliberate, 1e-9,
      "a failed load must not touch the deliberate scale: " + box.z.scale + " vs " + deliberate
    );
    key("ArrowRight");
    for (var j = 0; j < 180 && !box.z.boxW; j++) await frame();
    for (var a = 0; a < 4; a++) await frame();
    assert_(box.z.boxW === 1600, "the recovered load must adopt the medium image's geometry");
    approx(
      box.z.scale, deliberate, 1e-9,
      "a deliberate zoom must not be reinterpreted as 'at fit' by the next image: " +
        box.z.scale + " vs " + deliberate
    );
    assert_(
      Math.abs(box.z.scale - box.z.fitScale) > 1e-9,
      "the recovered image must keep the deliberate zoom, not open at its own fit: " +
        box.z.scale + " vs fit " + box.z.fitScale
    );
  }

  async function runDesktop(bucket) {
    // 1. Pointer-path pan clamping / centring.
    await run("pointer_extreme_negative_clamp", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var size = vpSize(box.vp);
      // Zoom until the scaled image overflows the stage horizontally.
      var guard = 0;
      while (box.z.boxW * box.z.scale <= size.w && guard++ < 40) key("+");
      var scaledW = box.z.boxW * box.z.scale;
      assert_(scaledW > size.w, "expected horizontal overflow after zooming in");
      var start = { x: size.w / 2, y: size.h / 2 };
      dragTo(box.vp, start, { x: start.x - 5000, y: start.y });
      var x = box.z.x;
      assert_(x + scaledW >= size.w - 1, "image dragged wholly out of view: x=" + x + " scaledW=" + scaledW);
      assert_(x <= 0, "pan was not clamped to the left bound: x=" + x);
      // A further drag left must saturate (no extra movement).
      var before = box.z.x;
      dragTo(box.vp, start, { x: start.x - 5000, y: start.y });
      approx(box.z.x, before, 0.001, "saturated pan moved");
    });

    await run("pointer_extreme_positive_clamp", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var size = vpSize(box.vp);
      var guard = 0;
      while (box.z.boxW * box.z.scale <= size.w && guard++ < 40) key("+");
      var scaledW = box.z.boxW * box.z.scale;
      var start = { x: size.w / 2, y: size.h / 2 };
      dragTo(box.vp, start, { x: start.x + 5000, y: start.y + 5000 });
      var x = box.z.x;
      var y = box.z.y;
      assert_(x <= 0, "pan must never leave a gap on the left: x=" + x);
      assert_(x >= size.w - scaledW - 1, "pan overshot the right bound: x=" + x);
      var scaledH = box.z.boxH * box.z.scale;
      if (scaledH <= size.h) {
        approx(y, (size.h - scaledH) / 2, 1.5, "undersized axis must stay centred");
      } else {
        assert_(y <= 0 && y >= size.h - scaledH - 1, "vertical pan out of bounds: y=" + y);
      }
    });

    await run("pointer_undersized_centred", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var size = vpSize(box.vp);
      var scaledW = box.z.boxW * box.z.scale;
      var scaledH = box.z.boxH * box.z.scale;
      assert_(scaledW <= size.w && scaledH <= size.h, "image is not undersized at fit scale");
      dragTo(box.vp, { x: size.w / 2, y: size.h / 2 }, { x: size.w / 2 + 1000, y: size.h / 2 - 1000 });
      approx(box.z.x, (size.w - scaledW) / 2, 1.5, "undersized x must be centred");
      approx(box.z.y, (size.h - scaledH) / 2, 1.5, "undersized y must be centred");
    });

    await run("pointer_zoom_out_centres", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var size = vpSize(box.vp);
      key("+");
      key("+");
      assert_(box.z.scale > box.z.fitScale, "expected zoom-in before zooming out");
      for (var i = 0; i < 6; i++) wheel(box.vp, 900, size.w / 2, size.h / 2);
      var scaledW = box.z.boxW * box.z.scale;
      var scaledH = box.z.boxH * box.z.scale;
      if (scaledW <= size.w) approx(box.z.x, (size.w - scaledW) / 2, 1.5, "zoomed-out x must be centred");
      if (scaledH <= size.h) approx(box.z.y, (size.h - scaledH) / 2, 1.5, "zoomed-out y must be centred");
    });

    // 2. Anchoring through the production wheel / touch handlers.
    await run("wheel_anchor", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var size = vpSize(box.vp);
      var rect = box.vp.getBoundingClientRect();
      // Offsets are measured from the stage's own origin: the production
      // handler anchors on (clientX - rect.left).
      var anchorX = size.w * 0.37;
      var anchorY = size.h * 0.43;
      var beforeScale = box.z.scale;
      var beforeX = box.z.x;
      var beforeY = box.z.y;
      wheel(box.vp, -900, rect.left + anchorX, rect.top + anchorY);
      assert_(box.z.scale > beforeScale * 2, "a large wheel-up must zoom in substantially");
      // The image point under the cursor must not move. Guard against a
      // fixture where the pan clamp (not the anchor) decides the outcome.
      var r = box.z.scale / beforeScale;
      var expectX = anchorX - (anchorX - beforeX) * r;
      var expectY = anchorY - (anchorY - beforeY) * r;
      assert_(expectX < -1 && expectX > size.w - box.z.boxW * box.z.scale, "degenerate fixture: x expectation is clamped");
      assert_(expectY < -1 && expectY > size.h - box.z.boxH * box.z.scale, "degenerate fixture: y expectation is clamped");
      approx((anchorX - box.z.x) / box.z.scale, (anchorX - beforeX) / beforeScale, 0.8, "anchored image point drifted (x)");
      approx((anchorY - box.z.y) / box.z.scale, (anchorY - beforeY) / beforeScale, 0.8, "anchored image point drifted (y)");
    });

    await run("pinch_anchor", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var size = vpSize(box.vp);
      var rect = box.vp.getBoundingClientRect();
      var cx = rect.left + size.w / 2;
      var cy = rect.top + size.h / 2;
      var from = [{ x: cx - 50, y: cy }, { x: cx + 50, y: cy }];
      var to = [{ x: cx - 100, y: cy }, { x: cx + 100, y: cy }];
      var beforeScale = box.z.scale;
      var beforeX = box.z.x;
      var beforeY = box.z.y;
      touch(box.vp, "touchstart", from);
      assert_(box.z.pinching === true, "two-finger touchstart must arm pinching");
      touch(box.vp, "touchmove", to);
      approx(box.z.scale / beforeScale, 2, 0.05, "pinch distance doubling must double the scale");
      var mid = size.w / 2;
      var midY = size.h / 2;
      approx((mid - box.z.x) / box.z.scale, (mid - beforeX) / beforeScale, 1.5, "pinch midpoint drifted (x)");
      approx((midY - box.z.y) / box.z.scale, (midY - beforeY) / beforeScale, 1.5, "pinch midpoint drifted (y)");
      pointer(box.vp, "pointerdown", rect.left + 20, cy);
      assert_(box.z.dragging === false, "pinching must block the pointer drag path");
      touch(box.vp, "touchend", []);
      assert_(box.z.pinching === false, "touchend must clear pinching");
      assert_(box.z.dragged === true, "pinch end must arm one-shot click suppression");
    });

    // 4. Fit button + keyboard, both through the production registrations.
    await run("fit_click_resets", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var size = vpSize(box.vp);
      key("+");
      dragTo(box.vp, { x: size.w / 2, y: size.h / 2 }, { x: size.w / 2 + 60, y: size.h / 2 + 40 });
      box.fitBtn.click();
      approx(box.z.scale, box.z.fitScale, 1e-6, "Fit click must restore the fit scale");
      approx(box.z.x, (size.w - box.z.boxW * box.z.fitScale) / 2, 1.5, "Fit click must re-centre x");
      approx(box.z.y, (size.h - box.z.boxH * box.z.fitScale) / 2, 1.5, "Fit click must re-centre y");
    });

    await run("keyboard_F_resets", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      key("+");
      key("+");
      assert_(box.z.scale > box.z.fitScale, "expected zoom-in before pressing F");
      key("f");
      approx(box.z.scale, box.z.fitScale, 1e-6, "'f' must restore the fit scale");
      key("+");
      key("F");
      approx(box.z.scale, box.z.fitScale, 1e-6, "'F' must restore the fit scale");
    });

    await run("keyboard_plus_minus", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var base = box.z.scale;
      key("+");
      approx(box.z.scale / base, 1.25, 0.02, "'+' must zoom in by 1.25");
      var afterPlus = box.z.scale;
      key("=");
      approx(box.z.scale / afterPlus, 1.25, 0.02, "'=' must zoom in by 1.25");
      var afterEquals = box.z.scale;
      key("-");
      approx(box.z.scale / afterEquals, 1 / 1.25, 0.02, "'-' must zoom out by 1.25");
      key("_");
      approx(box.z.scale, base, 0.02, "'_' must return to the starting scale");
    });

    await run("viewport_click_suppression", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var size = vpSize(box.vp);
      // A real drag arms the one-shot suppression, so the following click on
      // the stage must not bubble to the backdrop close handler.
      dragTo(box.vp, { x: size.w / 2, y: size.h / 2 }, { x: size.w / 2 + 70, y: size.h / 2 });
      box.vp.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, composed: true }));
      assert_(currentLb() !== null, "a drag-then-click sequence must not close the lightbox");
      // A click on the zoomed canvas is suppressed for the same reason.
      box.cv.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, composed: true }));
      assert_(currentLb() !== null, "canvas clicks must not close the lightbox");
      // Without a drag, a click on the stage bubbles and closes it (the
      // production close path animates out, so wait for the removal).
      closeLightbox();
      var fresh = await openBox(IMG_W, IMG_H);
      fresh.vp.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, composed: true }));
      for (var k = 0; k < 30 && currentLb(); k++) await sleep(20);
      assert_(currentLb() === null, "an undragged stage click must close the lightbox");
    });

    await run("viewport_click_retargeted_image_press", bucket, async function () {
      // Pointer capture retargets the click produced by a real mouse press on
      // image pixels to the viewport (mouseup is captured, so the click target
      // becomes the closest common ancestor). A press on the image must not
      // therefore be mistaken for a letterbox press (maintainer gate recheck of
      // #6896, 2026-10-06).
      var box = await openBox(IMG_W, IMG_H);
      var size = vpSize(box.vp);
      var rect = box.vp.getBoundingClientRect();
      var cx = rect.left + size.w / 2;
      var cy = rect.top + size.h / 2;
      var cv = box.cv.getBoundingClientRect();
      assert_(cx >= cv.left && cx <= cv.right && cy >= cv.top && cy <= cv.bottom,
        "fixture: the stage centre must lie on the image");
      pointer(box.cv, "pointerdown", cx, cy, 1);
      assert_(box.z.pressOnImage === true, "a press on image pixels must be recorded as such");
      pointer(box.vp, "pointerup", cx, cy, 1);
      // The retargeted click: target is the viewport, coordinates are on the image.
      box.vp.dispatchEvent(new MouseEvent("click", {
        bubbles: true, cancelable: true, composed: true, clientX: cx, clientY: cy,
      }));
      for (var i = 0; i < 30 && currentLb(); i++) await sleep(20);
      assert_(currentLb() !== null, "a retargeted click from an image press must not close the lightbox");

      // Letterbox control: a press above the fitted image still dismisses.
      closeLightbox();
      var fresh = await openBox(IMG_W, IMG_H);
      var fsize = vpSize(fresh.vp);
      var frect = fresh.vp.getBoundingClientRect();
      var fcv = fresh.cv.getBoundingClientRect();
      var lx = frect.left + fsize.w / 2;
      var ly = frect.top + 6;
      assert_(ly < fcv.top - 4, "fixture: expected a letterbox band above the image");
      pointer(fresh.vp, "pointerdown", lx, ly, 1);
      assert_(fresh.z.pressOnImage === false, "a press on the letterbox must not count as image pixels");
      pointer(fresh.vp, "pointerup", lx, ly, 1);
      fresh.vp.dispatchEvent(new MouseEvent("click", {
        bubbles: true, cancelable: true, composed: true, clientX: lx, clientY: ly,
      }));
      for (var j = 0; j < 30 && currentLb(); j++) await sleep(20);
      assert_(currentLb() === null, "a letterbox press must still close the lightbox");
    });

    await run("second_pointer_cannot_block_dismissal", bucket, async function () {
      // A refused second pointer must not re-classify the active gesture: if it
      // overwrote the recorded press origin, the owning pointer's letterbox
      // click would look like an image click and the user could not dismiss the
      // dialog (greptile review of #6896, 2026-10-05).
      var box = await openBox(IMG_W, IMG_H);
      var size = vpSize(box.vp);
      var rect = box.vp.getBoundingClientRect();
      var fcv = box.cv.getBoundingClientRect();
      var lx = rect.left + size.w / 2;
      var ly = rect.top + 6;
      assert_(ly < fcv.top - 4, "fixture: expected a letterbox band above the image");
      pointer(box.vp, "pointerdown", lx, ly, 1);
      assert_(box.z.pressOnImage === false, "fixture: the dismissal press starts on the letterbox");
      pointer(box.cv, "pointerdown", fcv.left + fcv.width / 2, fcv.top + fcv.height / 2, 2);
      assert_(box.z.pressOnImage === false, "a refused second pointer must not overwrite the press origin");
      assert_(box.z.dragging === true, "fixture: the first pointer must still own the gesture");
      pointer(box.vp, "pointerup", lx, ly, 1);
      box.vp.dispatchEvent(new MouseEvent("click", {
        bubbles: true, cancelable: true, composed: true, clientX: lx, clientY: ly,
      }));
      for (var i = 0; i < 30 && currentLb(); i++) await sleep(20);
      assert_(currentLb() === null, "the owning pointer's letterbox click must still dismiss the lightbox");
      pointer(box.vp, "pointerup", fcv.left + fcv.width / 2, fcv.top + fcv.height / 2, 2);
    });

    await run("keyboard_modifier_shortcuts_untouched", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var base = box.z.scale;
      // Ctrl/Meta/Alt combinations belong to the browser: they must not be
      // prevented and must not zoom the image (maintainer gate recheck of
      // #6896, 2026-10-06). An in-page recorder added after the lightbox
      // handler observes the decision it made.
      var seen = [];
      var rec = function (e) {
        seen.push({ key: e.key, ctrl: e.ctrlKey, meta: e.metaKey, alt: e.altKey, prevented: e.defaultPrevented });
      };
      document.addEventListener("keydown", rec);
      try {
        var combos = [
          { key: "+", ctrl: true }, { key: "=", ctrl: true }, { key: "-", ctrl: true }, { key: "_", ctrl: true },
          { key: "+", meta: true }, { key: "-", meta: true },
          { key: "f", ctrl: true },
        ];
        for (var i = 0; i < combos.length; i++) {
          var c = combos[i];
          document.dispatchEvent(new KeyboardEvent("keydown", {
            key: c.key, ctrlKey: !!c.ctrl, metaKey: !!c.meta, altKey: !!c.alt,
            bubbles: true, cancelable: true, composed: true,
          }));
          approx(box.z.scale, base, 1e-9, "modified key '" + c.key + "' must not zoom the lightbox");
        }
        for (var j = 0; j < seen.length; j++) {
          assert_(seen[j].prevented === false,
            "a browser shortcut (ctrl=" + seen[j].ctrl + " meta=" + seen[j].meta + " key=" + seen[j].key + ") must stay unprevented");
        }
        // Control: the unmodified shortcuts still work.
        key("+");
        assert_(box.z.scale > base, "control: an unmodified '+' must still zoom");
        key("f");
        approx(box.z.scale, box.z.fitScale, 1e-6, "control: an unmodified 'f' must still reset to fit");
      } finally {
        document.removeEventListener("keydown", rec);
      }
    });

    await run("navigation_during_pan_drops_the_stale_baseline", bucket, async function () {
      // Real-app repro from the maintainer re-warmup of #6896 (2026-10-06):
      // zoom the wide image, press on it, move 1px, navigate to the portrait
      // image with ArrowRight, wait for the load, then move 5px. The in-flight
      // pan's baseline belongs to the previous image's coordinate space;
      // re-applying it threw the freshly-centred image 83px up. Changing the
      // image must cancel the pan without touching the selected zoom.
      var box = await openBox(1200, 300, {
        images: [
          { src: svgDataUrl(1200, 300), alt: "wide" },
          { src: svgDataUrl(300, 900), alt: "portrait" },
        ],
        index: 0,
      });
      var size = vpSize(box.vp);
      key("+");
      key("+");
      key("+");
      var keptScale = box.z.scale;
      assert_(keptScale > box.z.fitScale, "fixture: expected zoom-in before navigating");
      var rect = box.vp.getBoundingClientRect();
      var cx = rect.left + size.w / 2;
      var cy = rect.top + size.h / 2;
      pointer(box.vp, "pointerdown", cx, cy, 1);
      pointer(box.vp, "pointermove", cx, cy + 1, 1);
      assert_(box.z.dragging === true, "fixture: the press must own the pan");
      key("ArrowRight");
      assert_(box.lb._navIndex === 1, "navigation must move to the next image");
      for (var i = 0; i < 240 && box.z.pendingNav; i++) await frame();
      for (var j = 0; j < 4; j++) await frame();
      assert_(box.z.boxW > 0, "the new image must decode");
      assert_(box.z.pendingNav === false, "the new image's load must consume pendingNav");
      assert_(box.z.dragging === false, "changing the image must cancel the in-flight pan");
      var beforeX = box.z.x;
      var beforeY = box.z.y;
      pointer(box.vp, "pointermove", cx, cy + 6, 1);
      pointer(box.vp, "pointerup", cx, cy + 6, 1);
      approx(box.z.y, beforeY, 1.5, "a move after navigation must not teleport the new image (y)");
      approx(box.z.x, beforeX, 1.5, "a move after navigation must not teleport the new image (x)");
      approx(box.z.scale, keptScale, 1e-9, "the selected zoom must survive the navigation");
    });

    await run("navigation_during_pinch_drops_the_stale_baseline", bucket, async function () {
      // Maintainer review of #6896 (2026-10-06): a two-finger pinch that is
      // still down when the image changes used to keep pinching/pinchStart*
      // alive, so the next touchmove applied the previous image's baselines to
      // the freshly-centred one and it jumped 544px on the reviewer's fixture.
      // Changing the image must end the pinch (making the next move a no-op
      // until a fresh two-finger start) and keep the pinched zoom.
      var box = await openBox(1200, 300, {
        images: [
          { src: svgDataUrl(1200, 300), alt: "wide" },
          { src: svgDataUrl(300, 900), alt: "portrait" },
        ],
        index: 0,
      });
      var size = vpSize(box.vp);
      key("+");
      key("+");
      key("+");
      var rect = box.vp.getBoundingClientRect();
      var cx = rect.left + size.w / 2;
      var cy = rect.top + size.h / 2;
      var start = [{ x: cx - 50, y: cy }, { x: cx + 50, y: cy }];
      var spread = [{ x: cx - 60, y: cy }, { x: cx + 60, y: cy }];
      touch(box.vp, "touchstart", start);
      assert_(box.z.pinching === true, "fixture: two fingers must start a pinch");
      touch(box.vp, "touchmove", spread);
      assert_(box.z.scale > box.z.fitScale, "fixture: the pinch must zoom in before navigating");
      key("ArrowRight");
      assert_(box.lb._navIndex === 1, "navigation must move to the next image");
      for (var i = 0; i < 240 && box.z.pendingNav; i++) await frame();
      for (var j = 0; j < 4; j++) await frame();
      assert_(box.z.boxW > 0, "the new image must decode");
      assert_(box.z.pendingNav === false, "the new image's load must consume pendingNav");
      assert_(box.z.pinching === false, "changing the image must end the in-flight pinch");
      var beforeX = box.z.x;
      var beforeY = box.z.y;
      var beforeScale = box.z.scale;
      // Both fingers stay down and spread further: the next touchmove must be
      // ignored (the pinch is over) rather than re-apply a stale baseline.
      touch(box.vp, "touchmove", [{ x: cx - 61, y: cy + 40 }, { x: cx + 61, y: cy + 40 }]);
      approx(box.z.x, beforeX, 1.5, "a pinch move after navigation must not teleport the new image (x)");
      approx(box.z.y, beforeY, 1.5, "a pinch move after navigation must not teleport the new image (y)");
      approx(box.z.scale, beforeScale, 1e-9, "a pinch move after navigation must not change the scale");
      touch(box.vp, "touchend", []);
    });

    await run("navigation_carries_fit_not_raw_scale", bucket, navigationCarriesFit);
    await run("navigation_after_a_failed_load_carries_fit", bucket, navigationAfterAFailedLoadCarriesFit);
    await run("navigation_after_a_failed_load_keeps_a_deliberate_scale", bucket, navigationAfterAFailedLoadKeepsADeliberateScale);

    // 5. Review follow-ups (greptile 2026-10-05): a second pointer must not
    // take the pan over, the document-wide shortcuts must not hijack typing
    // in a background field, focus must move into the dialog, and a failed
    // image load must not leave the stage on the previous image's geometry.
    await run("pointer_second_pointer_guard", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var size = vpSize(box.vp);
      // Zoom until the image overflows horizontally so a pan has room to move.
      var guard = 0;
      while (box.z.boxW * box.z.scale <= size.w && guard++ < 40) key("+");
      assert_(box.z.boxW * box.z.scale > size.w, "fixture: expected horizontal overflow");
      var start = { x: size.w / 2, y: size.h / 2 };
      pointer(box.vp, "pointerdown", start.x, start.y, 1);
      assert_(box.z.dragging === true, "the first pointer must arm the pan");
      var originX = box.z.dragOriginX;
      var startX = box.z.dragStartX;
      // A second pointer presses the stage mid-pan, far from the first one.
      pointer(box.vp, "pointerdown", start.x + 200, start.y + 60, 2);
      approx(box.z.dragOriginX, originX, 0.001, "a second pointer must not replace the drag origin");
      approx(box.z.dragStartX, startX, 0.001, "a second pointer must not replace the drag start");
      var beforeX = box.z.x;
      pointer(box.vp, "pointermove", start.x + 260, start.y + 60, 2);
      approx(box.z.x, beforeX, 0.001, "a second pointer's move must not pan the image");
      assert_(box.z.dragging === true, "a second pointer's press must not end the pan");
      // The owning pointer keeps panning from ITS origin...
      pointer(box.vp, "pointermove", start.x - 40, start.y, 1);
      approx(box.z.x, beforeX - 40, 1.5, "the owning pointer must keep panning from its own origin");
      // ...and only the owning pointer's release ends the drag.
      pointer(box.vp, "pointerup", start.x + 260, start.y + 60, 2);
      assert_(box.z.dragging === true, "a non-owning pointer's release must not end the pan");
      pointer(box.vp, "pointerup", start.x - 40, start.y, 1);
      assert_(box.z.dragging === false, "the owning pointer's release must end the pan");
      // The stage must accept a fresh gesture afterwards.
      pointer(box.vp, "pointerdown", start.x, start.y, 1);
      pointer(box.vp, "pointermove", start.x - 20, start.y, 1);
      pointer(box.vp, "pointerup", start.x - 20, start.y, 1);
      assert_(box.z.dragging === false, "the stage must resume normal panning after the gesture");
    });

    await run("keyboard_ignores_editable_target", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H, {
        images: [
          { src: svgDataUrl(IMG_W, IMG_H), alt: "first" },
          { src: svgDataUrl(IMG_W, IMG_H), alt: "second" },
        ],
        index: 0,
      });
      var base = box.z.scale;
      var real = document.querySelector(
        "#composer-input, #composer-textarea, textarea.composer-input, .composer textarea, textarea"
      );
      var field = real || document.createElement("textarea");
      if (!real) {
        field.style.cssText = "position:fixed;left:-4000px;top:0;width:120px;height:40px";
        document.body.appendChild(field);
      }
      try {
        field.focus();
        var keys = ["+", "=", "-", "_", "f", "F"];
        for (var i = 0; i < keys.length; i++) {
          field.dispatchEvent(new KeyboardEvent("keydown", {
            key: keys[i], bubbles: true, cancelable: true, composed: true,
          }));
          approx(box.z.scale, base, 1e-9, "key '" + keys[i] + "' typed in an editable field must not zoom the lightbox");
        }
        field.dispatchEvent(new KeyboardEvent("keydown", {
          key: "ArrowRight", bubbles: true, cancelable: true, composed: true,
        }));
        assert_(box.lb._navIndex === 0, "ArrowRight typed in an editable field must not navigate the lightbox");
        field.dispatchEvent(new KeyboardEvent("keydown", {
          key: "ArrowLeft", bubbles: true, cancelable: true, composed: true,
        }));
        assert_(box.lb._navIndex === 0, "ArrowLeft typed in an editable field must not navigate the lightbox");
        // Control: the same keys at document level (focus inside the dialog)
        // still drive zoom and navigation.
        key("+");
        assert_(box.z.scale > base, "control: document-level '+' must still zoom");
        key("ArrowRight");
        assert_(box.lb._navIndex === 1, "control: document-level ArrowRight must still navigate");
      } finally {
        field.blur();
        if (!real && field.parentNode) field.parentNode.removeChild(field);
      }
    });

    await run("focus_moves_into_dialog", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      assert_(
        box.lb.getAttribute("tabindex") === "-1",
        "the dialog must be programmatically focusable (tabindex=-1)"
      );
      assert_(
        document.activeElement === box.lb,
        "opening the lightbox must move focus into the dialog, got " +
          (document.activeElement && (document.activeElement.tagName || document.activeElement.nodeName))
      );
      assert_(
        getComputedStyle(box.lb).outlineStyle === "none",
        "the programmatic container focus must not draw an outline on the backdrop"
      );
      // Focus inside the dialog must still drive the shortcuts.
      var base = box.z.scale;
      key("+");
      assert_(box.z.scale > base, "the shortcuts must work while focus sits inside the dialog");
    });

    await run("img_error_keeps_zoom_drops_stale_geometry", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H, {
        images: [
          { src: svgDataUrl(IMG_W, IMG_H), alt: "good" },
          { src: "data:image/png;base64,AAAA", alt: "broken" },
          { src: svgDataUrl(IMG_W, IMG_H), alt: "good again" },
        ],
        index: 0,
      });
      key("+");
      key("+");
      var keptScale = box.z.scale;
      assert_(keptScale > box.z.fitScale, "fixture: expected zoom-in before navigating away");
      key("ArrowRight");
      assert_(box.lb._navIndex === 1, "navigation must move to the broken image");
      for (var i = 0; i < 180 && box.z.boxW; i++) await frame();
      assert_(
        box.z.boxW === 0 && box.z.boxH === 0,
        "a failed load must drop the stale box size, got " + box.z.boxW + "x" + box.z.boxH
      );
      assert_(
        box.cv.style.transform === "",
        "a failed load must clear the stale transform, got '" + box.cv.style.transform + "'"
      );
      assert_(box.z.dragging === false, "a failed load must not leave a drag armed");
      // The user's zoom level is part of the navigation contract ("keep the
      // current zoom level when switching images"), so a broken image in the
      // middle of a sequence must not silently reset it to fit.
      assert_(box.z.scale === keptScale, "a failed load must not drop the user's zoom level, got " + box.z.scale);
      // Recovery: navigating onward to a working image rebuilds the stage AND
      // keeps that zoom level.
      key("ArrowRight");
      assert_(box.lb._navIndex === 2, "navigation must continue past the broken image");
      for (var j = 0; j < 180 && !box.z.boxW; j++) await frame();
      assert_(box.z.boxW > 0 && box.z.boxH > 0, "a later successful load must restore the box size");
      assert_(box.z.pendingNav === false, "the recovered load must consume the pending-nav flag");
      assert_(
        Math.abs(box.z.scale - keptScale) < 1e-9,
        "the zoom level must survive the broken image: " + box.z.scale + " vs " + keptScale
      );
    });

    // 5b. A duplicate `load` for the frame we already initialized must not
    //     re-fit the stage (re-gate 2026-10-08T19:21:38Z: cached previews lost
    //     the user's selected zoom — 1.7578125 -> 0.9 — because a cached image
    //     initializes synchronously at mount (`img.complete`) and the browser
    //     still dispatches its queued `load` afterwards). The duplicate is
    //     invoked directly here so the check is deterministic; the natural
    //     ordering is covered by the trusted-input probe in the PR notes.
    await run("duplicate_load_keeps_zoom", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H, {
        images: [
          { src: svgDataUrl(IMG_W, IMG_H), alt: "a" },
          { src: svgDataUrl(1600, 900), alt: "b" },
        ],
        index: 0,
      });
      key("+");
      key("+");
      var zoomed = box.z.scale;
      assert_(zoomed > box.z.fitScale, "fixture: expected zoom-in before the duplicate");
      assert_(box.z.pendingNav === false, "fixture: no navigation may be armed yet");
      var boxW = box.z.boxW;
      // The production `load` handler, same source, same natural size, no
      // navigation pending: the duplicate the browser dispatches for an
      // already initialized frame.
      box.img.onload();
      assert_(
        Math.abs(box.z.scale - zoomed) < 1e-9,
        "a duplicate load for an initialized frame must not re-fit: " + box.z.scale +
          " vs " + zoomed
      );
      assert_(
        Math.abs(box.z.boxW - boxW) < 1e-9,
        "a duplicate load must not change the initialized geometry"
      );
      // The other ordering: the duplicate lands while a navigation is armed.
      // It cannot be the frame that navigation asked for (a src swap reports
      // its own source), so it must leave `pendingNav` armed for the real load
      // instead of consuming it — consuming it made the next load re-fit.
      box.z.pendingNav = true;
      box.img.onload();
      assert_(
        box.z.pendingNav === true,
        "a duplicate load for the already initialized frame must not consume pendingNav"
      );
      assert_(
        Math.abs(box.z.scale - zoomed) < 1e-9,
        "the stale event must leave the zoom untouched: " + box.z.scale + " vs " + zoomed
      );
      box.z.pendingNav = false;
      // ...and the navigation it was armed for still works.
      key("ArrowRight");
      for (var i = 0; i < 180 && box.z.pendingNav; i++) await frame();
      assert_(box.z.pendingNav === false, "the real navigation load must consume pendingNav");
      assert_(box.z.boxW === 1600, "the navigation must adopt the new image's geometry");
      assert_(
        Math.abs(box.z.scale - zoomed) < 1e-9,
        "navigation keeps the user's zoom level: " + box.z.scale + " vs " + zoomed
      );
    });

    // 6. Locale rendering of the Fit control. The control is icon-only since
    //    the 2026-10-08 visual review: it renders the shared fit glyph and its
    //    accessible name (title + aria-label) stays translated.
    await run("locale_zh_renders", bucket, async function () {
      window.setLocale("zh");
      var box = await openBox(IMG_W, IMG_H);
      assert_(box.fitBtn.querySelector("svg"), "Fit button must render the shared fit icon");
      var zh = "\u91cd\u7f6e\u7f29\u653e\u4ee5\u9002\u5e94 (F)";
      var title = box.fitBtn.getAttribute("title");
      var aria = box.fitBtn.getAttribute("aria-label");
      assert_(title === zh, "zh Fit title expected " + zh + " got " + title);
      assert_(aria === title, "aria-label must mirror the localized title");
    });

    await run("locale_ja_renders", bucket, async function () {
      window.setLocale("ja");
      var box = await openBox(IMG_W, IMG_H);
      assert_(box.fitBtn.querySelector("svg"), "Fit button must render the shared fit icon");
      var ja = "\u30ba\u30fc\u30e0\u3092\u30d5\u30a3\u30c3\u30c8\u306b\u30ea\u30bb\u30c3\u30c8 (F)";
      assert_(box.fitBtn.getAttribute("title") === ja,
        "ja Fit title expected " + ja + " got " + box.fitBtn.getAttribute("title"));
      window.setLocale("en");
    });

    // 7. Measured geometry + dark recipe at the desktop width. Fit must be the
    //    close button's 36px circle (maintainer visual review of #6896,
    //    2026-10-08: the old 44px text pill overlapped the image band).
    await run("geometry_circle_size", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var fit = box.fitBtn.getBoundingClientRect();
      var close = box.closeBtn.getBoundingClientRect();
      // 36px circle for a fine pointer, 44px tap target on a coarse one (maintainer fold, Fable UX gate:
      // WCAG/HIG minimum). These contexts emulate touch, so matchMedia decides which size applies.
      var want = window.matchMedia("(pointer:coarse)").matches ? 44 : 36;
      assert_(Math.abs(fit.width - want) <= 0.5, "Fit circle measured width " + fit.width + " != " + want);
      assert_(Math.abs(fit.height - want) <= 0.5, "Fit circle measured height " + fit.height + " != " + want);
      assert_(Math.abs(fit.width - close.width) <= 0.5 && Math.abs(fit.height - close.height) <= 0.5,
        "Fit must match the close circle: fit " + fit.width + "x" + fit.height + " close " + close.width + "x" + close.height);
      assert_(Math.abs(fit.top - close.top) <= 0.5, "Fit and close must share a top edge: " + fit.top + " vs " + close.top);
      var icon = box.fitBtn.querySelector("svg");
      assert_(icon, "Fit must render the shared fit icon");
      var glyph = icon.querySelector("path") && icon.querySelector("path").getAttribute("d");
      assert_(glyph && glyph.indexOf("M4 9V4h5") === 0, "Fit must use the shared Mermaid fit glyph, got " + glyph);
      // The counter's dark recipe, so both controls stay legible over light images.
      assert_(getComputedStyle(box.fitBtn).backgroundColor === "rgba(0, 0, 0, 0.5)",
        "Fit must use the counter's dark background, got " + getComputedStyle(box.fitBtn).backgroundColor);
      assert_(getComputedStyle(box.closeBtn).backgroundColor === "rgba(0, 0, 0, 0.5)",
        "Close must use the counter's dark background, got " + getComputedStyle(box.closeBtn).backgroundColor);
    });

    await run("geometry_no_overlap", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var fit = box.fitBtn.getBoundingClientRect();
      var close = box.closeBtn.getBoundingClientRect();
      assert_(fit.right <= close.left + 0.5, "Fit and close overlap: fit.right=" + fit.right + " close.left=" + close.left);
      assert_(close.right <= window.innerWidth + 0.5, "close button escapes the viewport");
      assert_(fit.left >= 0 && fit.top >= 0, "Fit button escapes the viewport");
    });

    await runGate1015Touch(bucket);
    await runGate1015Mouse(bucket);
  }

  async function runMobile(bucket) {
    await run("navigation_carries_fit_not_raw_scale", bucket, navigationCarriesFit);
    await run("navigation_after_a_failed_load_carries_fit", bucket, navigationAfterAFailedLoadCarriesFit);
    await run("navigation_after_a_failed_load_keeps_a_deliberate_scale", bucket, navigationAfterAFailedLoadKeepsADeliberateScale);

    await run("geometry_circle_size", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var fit = box.fitBtn.getBoundingClientRect();
      var close = box.closeBtn.getBoundingClientRect();
      var want = window.matchMedia("(pointer:coarse)").matches ? 44 : 36;
      assert_(Math.abs(fit.width - want) <= 0.5, "mobile Fit circle measured width " + fit.width + " != " + want);
      assert_(Math.abs(fit.height - want) <= 0.5, "mobile Fit circle measured height " + fit.height + " != " + want);
      // A near-miss on Fit must not land on close: keep a real gap between the two targets.
      assert_(close.left - fit.right >= 11.5, "mobile Fit/close gap " + (close.left - fit.right) + "px < 12");
      assert_(Math.abs(fit.width - close.width) <= 0.5 && Math.abs(fit.height - close.height) <= 0.5,
        "mobile Fit must match the close circle: fit " + fit.width + "x" + fit.height + " close " + close.width + "x" + close.height);
      assert_(Math.abs(fit.top - close.top) <= 0.5, "mobile Fit and close must share a top edge: " + fit.top + " vs " + close.top);
    });

    await run("geometry_no_overlap", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var fit = box.fitBtn.getBoundingClientRect();
      var close = box.closeBtn.getBoundingClientRect();
      assert_(fit.right <= close.left + 0.5, "mobile Fit and close overlap: fit.right=" + fit.right + " close.left=" + close.left);
      assert_(close.right <= window.innerWidth + 0.5, "mobile close button escapes the viewport");
    });

    await run("sidebar_swipe_excluded", bucket, async function () {
      var sidebar = document.querySelector(".sidebar");
      if (sidebar) sidebar.classList.remove("mobile-open", "mobile-panel-drawer");

      var originalOpen = window._openMobileSidebarFromGesture;
      var openCalls = 0;
      window._openMobileSidebarFromGesture = function () {
        openCalls++;
        return originalOpen.apply(this, arguments);
      };
      var plain = document.createElement("div");
      plain.style.cssText = "position:fixed;left:0;top:0;width:200px;height:200px;z-index:5";
      try {
        // The real case first: a left-edge touch that starts inside the
        // lightbox must be ignored by the production start handler.
        var box = await openBox(IMG_W, IMG_H);
        assert_(_pwaSidebarSwipe === null, "precondition: no swipe may be armed");
        assert_(
          !(sidebar && sidebar.classList.contains("mobile-open")),
          "precondition: the mobile sidebar must be closed"
        );
        touch(box.vp, "touchstart", [{ x: 12, y: 400 }]);
        assert_(_pwaSidebarSwipe === null, "a touch inside the img lightbox must not arm the sidebar swipe");
        touch(box.vp, "touchmove", [{ x: 120, y: 400 }]);
        assert_(openCalls === 0, "the sidebar must not open from a gesture inside the img lightbox");
        touch(box.vp, "touchend", []);

        // Positive control: the same gesture on an ordinary element must arm
        // the recogniser and open the sidebar, proving the check above is not
        // passing merely because the recogniser is dormant.
        document.body.appendChild(plain);
        touch(plain, "touchstart", [{ x: 12, y: 400 }]);
        assert_(_pwaSidebarSwipe !== null, "control: a left-edge touch outside the lightbox must arm the sidebar swipe");
        touch(plain, "touchmove", [{ x: 120, y: 400 }]);
        assert_(openCalls === 1, "control: a 108px horizontal swipe must open the sidebar (calls=" + openCalls + ")");
        touch(plain, "touchend", []);
        assert_(_pwaSidebarSwipe === null, "touchend must clear the swipe state");
      } finally {
        window._openMobileSidebarFromGesture = originalOpen;
        if (plain.parentNode) plain.parentNode.removeChild(plain);
        if (sidebar) sidebar.classList.remove("mobile-open", "mobile-panel-drawer");
      }
    });

    await runGate1015Touch(bucket);
  }

  /* ---------------------------------------------------------------------
   * Maintainer re-gate 2026-10-15T17:54:15Z (review 5537610966): Codex drove
   * the gesture code on desktop and iPhone (390x844) and found seven silent
   * edge cases, each with a reproduction and a small exact fix. One browser
   * check per item, all through the production handlers; item 5 needs touch
   * identifiers that survive a finger being replaced, which is why `touch()`
   * accepts an explicit `id`.
   * ------------------------------------------------------------------- */

  // 1a. static/ui.js:2795 — gestures used to overwrite freshly fitted geometry.
  // A pinch armed while the image is still decoding kept a baseline measured
  // against the PREVIOUS frame's fit, so the next move rescaled the new one:
  // a 4000x3000 image jumped from its fitted 351px width to 4080px on an
  // iPhone. Touch updates must be ignored until the image has dimensions.
  async function fitSurvivesAGestureStartedWithoutGeometry() {
    var box = await openBox(4000, 3000, {
      images: [
        { src: svgDataUrl(4000, 3000), alt: "huge" },
        { src: "data:image/png;base64,AAAA", alt: "broken" },
        { src: svgDataUrl(4000, 3000), alt: "huge again" },
      ],
      index: 0,
    });
    var size = vpSize(box.vp);
    var rect = box.vp.getBoundingClientRect();
    var cx = rect.left + size.w / 2;
    var cy = rect.top + size.h / 2;
    // The broken entry drops the geometry, which is the reported window.
    key("ArrowRight");
    for (var i = 0; i < 180 && box.z.boxW; i++) await frame();
    assert_(box.z.boxW === 0, "fixture: the broken entry must drop the geometry");
    var fingers = [{ x: cx - 40, y: cy }, { x: cx + 40, y: cy }];
    touch(box.vp, "touchstart", fingers);
    assert_(box.z.pinching === false, "a pinch must not arm before the image has dimensions");
    // The next entry opens at ITS own fit; the fingers are still down and
    // spread, and must not be able to overwrite that fit.
    key("ArrowRight");
    for (var j = 0; j < 240 && !box.z.boxW; j++) await frame();
    for (var a = 0; a < 4; a++) await frame();
    var fitted = box.z.scale;
    var fittedW = box.z.boxW * box.z.scale;
    assert_(Math.abs(fitted - box.z.fitScale) < 1e-9, "fixture: the image must open at its fit");
    touch(box.vp, "touchmove", [{ x: cx - 80, y: cy }, { x: cx + 80, y: cy }]);
    approx(box.z.scale, fitted, 1e-9, "a pinch armed before the fit must not overwrite it");
    approx(box.z.boxW * box.z.scale, fittedW, 1e-6, "the fitted width must survive the gesture");
    assert_(Math.abs(box.z.scale - box.z.fitScale) < 1e-9, "the image must still be at its fit");
    touch(box.vp, "touchend", []);
  }

  // 1b. static/ui.js:2795 — "Fit is undone by the next finger movement": a fit
  // must end the gesture that is in flight instead of being overwritten by it.
  async function fitResetsAnActivePinch() {
    var box = await openBox(IMG_W, IMG_H);
    var size = vpSize(box.vp);
    var rect = box.vp.getBoundingClientRect();
    var cx = rect.left + size.w / 2;
    var cy = rect.top + size.h / 2;
    touch(box.vp, "touchstart", [{ x: cx - 50, y: cy }, { x: cx + 50, y: cy }]);
    assert_(box.z.pinching === true, "fixture: two contacts must start a pinch");
    touch(box.vp, "touchmove", [{ x: cx - 100, y: cy }, { x: cx + 100, y: cy }]);
    assert_(box.z.scale > box.z.fitScale, "fixture: the pinch must zoom in");
    key("f");
    approx(box.z.scale, box.z.fitScale, 1e-6, "Fit must restore the fit scale");
    assert_(box.z.pinching === false, "a fresh fit must end the gesture that was in flight");
    touch(box.vp, "touchmove", [{ x: cx - 140, y: cy }, { x: cx + 140, y: cy }]);
    approx(box.z.scale, box.z.fitScale, 1e-6, "the next finger movement must not undo Fit");
    touch(box.vp, "touchend", []);
  }

  // 2. static/ui.js:3134 — a wheel zoom during a held pan left the drag's
  // baseline on the pre-zoom position, so the next pointermove re-applied the
  // whole accumulated displacement at the new scale (674px for a 1px move).
  async function wheelZoomMidPanDoesNotSnap() {
    var box = await openBox(IMG_W, IMG_H);
    var size = vpSize(box.vp);
    var rect = box.vp.getBoundingClientRect();
    var guard = 0;
    while (box.z.boxW * box.z.scale < size.w + 400 && guard++ < 40) key("+");
    var cx = rect.left + size.w / 2;
    var cy = rect.top + size.h / 2;
    pointer(box.vp, "pointerdown", cx, cy, 1);
    pointer(box.vp, "pointermove", cx - 20, cy, 1);
    var scaleBefore = box.z.scale;
    wheel(box.vp, -240, cx - 20, cy);
    assert_(box.z.scale > scaleBefore, "fixture: the wheel must still zoom during a held pan");
    var afterWheelX = box.z.x;
    var scaledW = box.z.boxW * box.z.scale;
    assert_(afterWheelX < -2 && afterWheelX > size.w - scaledW + 2,
      "fixture: the wheel zoom must leave the pan unclamped, x=" + afterWheelX);
    pointer(box.vp, "pointermove", cx - 21, cy, 1);
    approx(box.z.x, afterWheelX - 1, 1.5, "a 1px move after a mid-pan wheel zoom must not snap");
    pointer(box.vp, "pointerup", cx - 21, cy, 1);
  }

  // 3. static/ui.js:2843 — navigating between two entries that share an image
  // source was classified as the previous frame's duplicate load, so init and
  // recentring were skipped and pendingNav stayed armed.
  async function sameSourceNavigationRecentres() {
    var shared = svgDataUrl(IMG_W, IMG_H);
    var box = await openBox(IMG_W, IMG_H, {
      images: [
        { src: shared, alt: "first copy" },
        { src: shared, alt: "second copy" },
      ],
      index: 0,
    });
    var size = vpSize(box.vp);
    var rect = box.vp.getBoundingClientRect();
    var cx = rect.left + size.w / 2;
    var cy = rect.top + size.h / 2;
    key("+");
    key("+");
    var zoomed = box.z.scale;
    assert_(zoomed > box.z.fitScale, "fixture: expected zoom-in before navigating");
    // Deliberately off-centre it, so a re-centre is measurable.
    pointer(box.vp, "pointerdown", cx, cy, 1);
    pointer(box.vp, "pointermove", cx - 30, cy - 10, 1);
    pointer(box.vp, "pointerup", cx - 30, cy - 10, 1);
    var wantX = (size.w - box.z.boxW * zoomed) / 2;
    assert_(box.z.x < wantX - 20, "fixture: the preview must be off-centre, x=" + box.z.x + " want " + wantX);
    key("ArrowRight");
    assert_(box.lb._navIndex === 1, "navigation must move to the second entry");
    for (var i = 0; i < 240 && box.z.pendingNav; i++) await frame();
    for (var j = 0; j < 4; j++) await frame();
    assert_(box.z.pendingNav === false, "the same-source navigation must consume pendingNav");
    assert_(box.z.boxW === IMG_W, "the same-source navigation must keep the frame's geometry");
    approx(box.z.x, wantX, 1.5, "the same-source navigation must re-centre (x)");
    approx(box.z.y, (size.h - box.z.boxH * zoomed) / 2, 1.5, "the same-source navigation must re-centre (y)");
    approx(box.z.scale, zoomed, 1e-9, "the chosen zoom must survive the navigation");
  }

  // 4. static/ui.js:3145 — resizing a zoomed preview kept the old fit baseline,
  // so zoom-out stayed clamped at 0.25 while the new fit was 0.08775.
  async function resizeRecomputesTheZoomOutLimit() {
    var box = await openBox(4000, 3000);
    var size = vpSize(box.vp);
    assert_(box.z.fitScale < 0.9, "fixture: the huge image needs a real downscale");
    key("+");
    key("+");
    assert_(Math.abs(box.z.scale - box.z.fitScale) > 1e-9,
      "fixture: the preview must be user-zoomed for the resize branch");
    var oldLimit = Math.min(0.25, box.z.fitScale);
    // Shrink the stage the way the phone does, then let the debounced handler
    // run: the fit baseline belongs to the NEW size.
    box.vp.style.width = "300px";
    box.vp.style.height = "300px";
    var shrunk = vpSize(box.vp);
    assert_(shrunk.w < size.w / 2 && shrunk.h < size.h / 2, "fixture: the stage must actually shrink");
    window.dispatchEvent(new Event("resize"));
    for (var i = 0; i < 60; i++) await sleep(20);
    var expectedFit = Math.min(1, shrunk.w / box.z.boxW, shrunk.h / box.z.boxH);
    approx(box.z.fitScale, expectedFit, 1e-6, "the fit baseline must be recomputed for the new size");
    // The user-visible symptom: zooming out must reach the new fit instead of
    // stopping at the obsolete limit the old size produced.
    var r = box.vp.getBoundingClientRect();
    wheel(box.vp, 900, r.left + shrunk.w / 2, r.top + shrunk.h / 2);
    wheel(box.vp, 900, r.left + shrunk.w / 2, r.top + shrunk.h / 2);
    approx(box.z.scale, Math.min(0.25, expectedFit), 1e-6, "zoom-out must reach the new size's fit");
    assert_(box.z.scale < oldLimit - 1e-9,
      "the zoom-out limit must not stay at the old size's " + oldLimit + ", got " + box.z.scale);
  }

  // 5. static/ui.js:3088 — replacing a finger mid-pinch kept the old baseline,
  // which belonged to a contact that was no longer down: an iPhone pinch
  // dropped from 0.1638 to 0.1260 after a 1px move.
  async function replacedFingerReanchorsThePinch() {
    var box = await openBox(IMG_W, IMG_H);
    var size = vpSize(box.vp);
    var rect = box.vp.getBoundingClientRect();
    var cx = rect.left + size.w / 2;
    var cy = rect.top + size.h / 2;
    var f1 = { x: cx - 50, y: cy, id: 1 };
    var f2 = { x: cx + 50, y: cy, id: 2 };
    var f3 = { x: cx, y: cy + 200, id: 3 };
    touch(box.vp, "touchstart", [f1, f2]);
    touch(box.vp, "touchstart", [f1, f2, f3]);
    assert_(box.z.pinching === true, "fixture: two contacts must be pinching");
    // Finger 2 lifts: the pair under the fingers becomes [1, 3], whose distance
    // (206px) is nothing like the 100px baseline the pinch was armed with.
    touch(box.vp, "touchend", [f1, f3]);
    assert_(box.z.pinching === true, "fixture: one contact of three lifting leaves a pinch");
    var scaleBefore = box.z.scale;
    var xBefore = box.z.x;
    var yBefore = box.z.y;
    touch(box.vp, "touchmove", [f1, f3]);
    approx(box.z.scale, scaleBefore, 1e-9, "a replaced contact pair must not change the scale");
    approx(box.z.x, xBefore, 1.5, "a replaced contact pair must not move the image (x)");
    approx(box.z.y, yBefore, 1.5, "a replaced contact pair must not move the image (y)");
    // Positive control: the NEW pair pinches from its own baseline.
    touch(box.vp, "touchmove", [{ x: cx - 100, y: cy, id: 1 }, { x: cx, y: cy + 250, id: 3 }]);
    assert_(box.z.scale > scaleBefore * 1.1,
      "the new pair must pinch from its own baseline: " + box.z.scale + " vs " + scaleBefore);
    touch(box.vp, "touchend", []);
  }

  // 6. static/ui.js:2993 — reversing a drag after overshooting an edge did
  // nothing until the whole overshoot was retraced.
  async function overshootReversalRespondsImmediately() {
    var box = await openBox(IMG_W, IMG_H);
    var size = vpSize(box.vp);
    var rect = box.vp.getBoundingClientRect();
    var guard = 0;
    while (box.z.boxW * box.z.scale < size.w + 400 && guard++ < 40) key("+");
    var scaledW = box.z.boxW * box.z.scale;
    var cx = rect.left + size.w / 2;
    var cy = rect.top + size.h / 2;
    pointer(box.vp, "pointerdown", cx, cy, 1);
    pointer(box.vp, "pointermove", cx - 5000, cy, 1);
    var clampedX = box.z.x;
    assert_(clampedX <= size.w - scaledW + 1, "fixture: panning left must clamp at the edge");
    // Reverse while still 4970px past the edge: the image must follow by the
    // 30px it was reversed, not stay put until the overshoot is retraced.
    pointer(box.vp, "pointermove", cx - 4970, cy, 1);
    approx(box.z.x, clampedX + 30, 1.5, "a reversed drag must move the image immediately");
    pointer(box.vp, "pointerup", cx - 4970, cy, 1);
  }

  // 7. static/ui.js:3133 — line- and page-mode wheel deltas are not pixels:
  // treating them as pixels made a three-line tick zoom by 0.45%.
  async function lineModeWheelZooms() {
    var box = await openBox(IMG_W, IMG_H);
    var rect = box.vp.getBoundingClientRect();
    var ax = rect.left + 200;
    var ay = rect.top + 200;
    var before = box.z.scale;
    wheel(box.vp, -3, ax, ay, 1);
    approx(box.z.scale / before, Math.exp(3 * 30 * 0.0015), 0.02,
      "a three-line tick must zoom like 90px of pixels");
    var afterLine = box.z.scale;
    // Control: pixel-mode deltas keep the same recipe.
    wheel(box.vp, -90, ax, ay);
    approx(box.z.scale / afterLine, Math.exp(90 * 0.0015), 0.02, "pixel mode must keep the same recipe");
    var afterPixel = box.z.scale;
    wheel(box.vp, -1, ax, ay, 2);
    approx(box.z.scale / afterPixel, Math.exp(600 * 0.0015), 0.02,
      "a one-page tick must zoom like 600px of pixels");
  }

  // Maintainer re-gate 2026-10-15T17:54:15Z: one browser check per reported
  // item. The touch-driven items run in both buckets (a phone is where they
  // bite); the mouse/wheel-driven ones only make sense on the desktop.
  // Greptile P2 on head 860c0e85 (2026-10-15T19:09:46Z, static/ui.js:3293,
  // "Zoom keys make gestures jump"): the keyboard zoom moved the image but left
  // the held pan's baseline (dragStartX/Y) on the pre-zoom geometry, so the next
  // pointermove re-applied the accumulated displacement and the image jumped.
  async function keyboardZoomMidPanDoesNotSnap() {
    var box = await openBox(IMG_W, IMG_H);
    var size = vpSize(box.vp);
    var rect = box.vp.getBoundingClientRect();
    var guard = 0;
    while (box.z.boxW * box.z.scale < size.w + 400 && guard++ < 40) key("+");
    var cx = rect.left + size.w / 2;
    var cy = rect.top + size.h / 2;
    pointer(box.vp, "pointerdown", cx, cy, 1);
    pointer(box.vp, "pointermove", cx - 20, cy, 1);
    var scaleBefore = box.z.scale;
    key("+");
    assert_(box.z.scale > scaleBefore, "fixture: the keyboard zoom must zoom during a held pan");
    var afterZoomX = box.z.x;
    var scaledW = box.z.boxW * box.z.scale;
    assert_(afterZoomX > size.w - scaledW + 2 && afterZoomX < -2,
      "fixture: the keyboard zoom must leave the pan strictly inside the clamp range, x=" + afterZoomX);
    pointer(box.vp, "pointermove", cx - 21, cy, 1);
    approx(box.z.x, afterZoomX - 1, 1.5, "a 1px move after a mid-pan keyboard zoom must not snap");
    pointer(box.vp, "pointerup", cx - 21, cy, 1);
  }

  // Same greptile P2, pinch half: a keyboard zoom during an active pinch left
  // pinchStartScale (and the anchor midpoint) on the pre-zoom geometry, so the
  // next touchmove rescaled from the OLD scale and undid the keyboard zoom.
  async function keyboardZoomMidPinchIsNotUndone() {
    var box = await openBox(IMG_W, IMG_H);
    var size = vpSize(box.vp);
    var rect = box.vp.getBoundingClientRect();
    var cx = rect.left + size.w / 2;
    var cy = rect.top + size.h / 2;
    var f1 = { x: cx - 50, y: cy, id: 1 };
    var f2 = { x: cx + 50, y: cy, id: 2 };
    touch(box.vp, "touchstart", [f1, f2]);
    touch(box.vp, "touchmove", [{ x: cx - 100, y: cy, id: 1 }, { x: cx + 100, y: cy, id: 2 }]);
    assert_(box.z.pinching === true, "fixture: two contacts must be pinching");
    assert_(box.z.scale > box.z.fitScale, "fixture: the pinch must zoom in");
    // Measure the key alone: the pinch above already scaled the image, so
    // comparing against fitScale would pass even if `+` did nothing.
    var scaleBeforeKey = box.z.scale;
    key("+");
    var zoomed = box.z.scale;
    var xAfterZoom = box.z.x;
    assert_(zoomed > scaleBeforeKey, "fixture: the keyboard zoom must zoom in during the pinch");
    // The fingers do not move: the pinch baseline was re-anchored onto these
    // contacts, so neither the scale nor the position may change.
    touch(box.vp, "touchmove", [{ x: cx - 100, y: cy, id: 1 }, { x: cx + 100, y: cy, id: 2 }]);
    approx(box.z.scale, zoomed, 1e-9, "a keyboard zoom mid-pinch must survive the next touchmove");
    approx(box.z.x, xAfterZoom, 1.5, "the pinch must not snap the image back (x)");
    // Positive control: the pinch keeps pinching from the re-anchored baseline.
    touch(box.vp, "touchmove", [{ x: cx - 150, y: cy, id: 1 }, { x: cx + 150, y: cy, id: 2 }]);
    assert_(box.z.scale > zoomed * 1.1,
      "the pinch must keep zooming from its re-anchored baseline: " + box.z.scale + " vs " + zoomed);
    touch(box.vp, "touchend", []);
  }

  // Greptile P2 on the merge-only head 25684d89 (2026-10-15T22:36:41Z,
  // static/ui.js:3275, "Wheel zoom gets undone"): the wheel path re-anchored
  // the pan (`_imgRebaseDrag`) but not the pinch, so a wheel tick under two
  // held contacts left pinchStartScale (and the anchor midpoint) on the
  // pre-tick geometry and the next touchmove restored the OLD zoom. Runs in
  // both buckets: the wheel is the desktop's whole zoom gesture but a phone
  // with a mouse still dispatches it.
  async function wheelZoomMidPinchIsNotUndone() {
    var box = await openBox(IMG_W, IMG_H);
    var size = vpSize(box.vp);
    var rect = box.vp.getBoundingClientRect();
    var cx = rect.left + size.w / 2;
    var cy = rect.top + size.h / 2;
    var f1 = { x: cx - 50, y: cy, id: 1 };
    var f2 = { x: cx + 50, y: cy, id: 2 };
    touch(box.vp, "touchstart", [f1, f2]);
    touch(box.vp, "touchmove", [{ x: cx - 100, y: cy, id: 1 }, { x: cx + 100, y: cy, id: 2 }]);
    assert_(box.z.pinching === true, "fixture: two contacts must be pinching");
    assert_(box.z.scale > box.z.fitScale, "fixture: the pinch must zoom in");
    // Measure the wheel alone: the pinch above already scaled the image, so
    // comparing against fitScale would pass even if the wheel did nothing.
    var scaleBeforeWheel = box.z.scale;
    wheel(box.vp, -240, cx, cy);
    var zoomed = box.z.scale;
    var xAfterWheel = box.z.x;
    assert_(zoomed > scaleBeforeWheel, "fixture: the wheel must zoom in during the pinch");
    // The fingers do not move: the pinch baseline was re-anchored onto these
    // contacts, so neither the scale nor the position may change.
    touch(box.vp, "touchmove", [{ x: cx - 100, y: cy, id: 1 }, { x: cx + 100, y: cy, id: 2 }]);
    approx(box.z.scale, zoomed, 1e-9, "a wheel zoom mid-pinch must survive the next touchmove");
    approx(box.z.x, xAfterWheel, 1.5, "the pinch must not snap the image back after a wheel zoom (x)");
    // Positive control: the pinch keeps pinching from the re-anchored baseline.
    touch(box.vp, "touchmove", [{ x: cx - 150, y: cy, id: 1 }, { x: cx + 150, y: cy, id: 2 }]);
    assert_(box.z.scale > zoomed * 1.1,
      "the pinch must keep zooming from its re-anchored baseline: " + box.z.scale + " vs " + zoomed);
    touch(box.vp, "touchend", []);
  }

  // Greptile P2 on the merge-only head c69de101 (2026-10-15T23:15:55Z,
  // static/ui.js:3213, "Reversing fingers leaves images stuck"): the pinch
  // recomputed its position from a baseline that still held the clamped-away
  // overshoot, so a gesture reversed after pushing past an edge did nothing
  // until the whole overshoot was retraced - the pan half of this family was
  // fixed long ago, the pinch half was not.
  async function pinchReversalRespondsImmediately() {
    var box = await openBox(IMG_W, IMG_H);
    var size = vpSize(box.vp);
    var rect = box.vp.getBoundingClientRect();
    var cx = rect.left + size.w / 2;
    var cy = rect.top + size.h / 2;
    // Zoom in well past the viewport width so the x axis really clamps.
    var guard = 0;
    while (box.z.boxW * box.z.scale < size.w + 400 && guard++ < 40) key("+");
    // Two contacts at a constant spread: the ratio stays 1, so x follows the
    // midpoint one-for-one and the assertion below is exact.
    var d = 50;
    touch(box.vp, "touchstart", [{ x: cx - d, y: cy, id: 1 }, { x: cx + d, y: cy, id: 2 }]);
    var overshoot = size.w;
    touch(box.vp, "touchmove", [
      { x: cx - d + overshoot, y: cy, id: 1 },
      { x: cx + d + overshoot, y: cy, id: 2 },
    ]);
    approx(box.z.x, 0, 1.5, "fixture: the overshoot must clamp x at its maximum");
    // The reversal: 10px back must move the image 10px, not wait for the whole
    // overshoot to be retraced.
    touch(box.vp, "touchmove", [
      { x: cx - d + overshoot - 10, y: cy, id: 1 },
      { x: cx + d + overshoot - 10, y: cy, id: 2 },
    ]);
    approx(box.z.x, -10, 2.5, "a reversed pinch must move the image immediately");
    touch(box.vp, "touchend", []);
  }

  // Greptile P1 on head 5e206e06 (2026-10-15T23:28:11Z, static/ui.js:3222,
  // "Pinch zoom moves the image"): asked for a check with SEVERAL spread
  // updates, because the constant-spread reversal check cannot see drift. This
  // drives a spread/grow/spread-back sequence with a moving midpoint and
  // asserts the invariant a user can see: the SAME two fingers always produce
  // the SAME frame. The positional rebase above is the identity whenever the
  // clamp did not bite, so this holds; re-anchoring the whole pinch baseline
  // instead (pinchStartScale/pinchStartDist) breaks exactly this, which is why
  // the reversal fix moves only the positional half.
  async function pinchSpreadIsPathIndependent() {
    var box = await openBox(IMG_W, IMG_H);
    var size = vpSize(box.vp);
    var rect = box.vp.getBoundingClientRect();
    var cx = rect.left + size.w / 2;
    var cy = rect.top + size.h / 2;
    // Zoom in until the x axis overflows by 1600px, so the whole sequence stays
    // far inside the clamp range: a fitting axis is force-centred by
    // _imgClampPan, and a clamped frame legitimately re-anchors, so either would
    // hide a genuine drift instead of exposing it.
    var guard = 0;
    while (box.z.boxW * box.z.scale < size.w + 1600 && guard++ < 60) key("+");
    var d = 80;
    function fingers(off, spread) {
      return [
        { x: cx + off - spread, y: cy, id: 1 },
        { x: cx + off + spread, y: cy, id: 2 },
      ];
    }
    touch(box.vp, "touchstart", fingers(0, d));
    // A: spread 20% with the midpoint 10px right.
    touch(box.vp, "touchmove", fingers(10, d * 1.2));
    var scaleA = box.z.scale;
    var xA = box.z.x;
    // B: spread further and shift the midpoint again, so the ratio changes
    // after a re-anchor would have happened.
    touch(box.vp, "touchmove", fingers(30, d * 1.4));
    assert_(box.z.scale > scaleA * 1.05, "fixture: the second spread must zoom further");
    assert_(box.z.x !== xA, "fixture: the midpoint shift must move the image");
    var scaleB = box.z.scale;
    var xB = box.z.x;
    // Back to EXACTLY A's fingers.
    touch(box.vp, "touchmove", fingers(10, d * 1.2));
    var xC = box.z.x;
    var minX = size.w - box.z.boxW * box.z.scale;
    var diag = " [xA=" + xA + " xB=" + xB + " xC=" + xC + " scaleA=" + scaleA +
      " scaleB=" + scaleB + " scaleC=" + box.z.scale + " minX=" + minX +
      " vw=" + size.w + " psX=" + box.z.pinchStartX + " psCX=" + box.z.pinchStartCX +
      " psScale=" + box.z.pinchStartScale + " psDist=" + box.z.pinchStartDist + "]";
    assert_(xC > minX + 5 && xC < -5, "fixture: the sequence must stay off the clamp edges" + diag);
    approx(box.z.scale, scaleA, 1e-9, "the same fingers must restore the same scale" + diag);
    approx(box.z.x, xA, 1.5, "the same fingers must restore the same position" + diag);
    touch(box.vp, "touchend", []);
  }

  // Greptile P2 on the merge-only head 6f462d82 (2026-10-16T01:39:15Z,
  // static/ui.js:3235, "Dragging stops after pinching"): a pinch ends the
  // pointer drag (_imgOnTouchStart calls _imgEndPointerDrag) and the contact
  // that is still down never receives a new pointerdown, so its moves were
  // dropped until the user lifted and pressed again. The pan must be re-armed
  // from that contact, anchored on the geometry it has NOW, so the first move
  // after the pinch continues the frame instead of jumping to a stale origin.
  // The gesture drives BOTH streams, as a real touch does (the ids only exist
  // on the pointer events, the contacts only on the touch events).
  async function pinchEndKeepsTheRemainingFingerPanning() {
    var box = await openBox(IMG_W, IMG_H);
    var size = vpSize(box.vp);
    var rect = box.vp.getBoundingClientRect();
    var cx = rect.left + size.w / 2;
    var cy = rect.top + size.h / 2;
    // Zoom past the viewport on BOTH axes: a fitting axis is force-centred by
    // _imgClampPan, and a centred axis moves with any pan (it is re-centred),
    // so only an overflowing pair makes the displacement below observable.
    var guard = 0;
    while ((box.z.boxW * box.z.scale < size.w + 400 ||
            box.z.boxH * box.z.scale < size.h + 400) && guard++ < 80) key("+");
    pointer(box.vp, "pointerdown", cx - 40, cy, 11, "touch");
    pointer(box.vp, "pointerdown", cx + 40, cy, 12, "touch");
    touch(box.vp, "touchstart", [{ x: cx - 40, y: cy, id: 1 }, { x: cx + 40, y: cy, id: 2 }]);
    touch(box.vp, "touchmove", [{ x: cx - 60, y: cy, id: 1 }, { x: cx + 60, y: cy, id: 2 }]);
    assert_(box.z.pinching, "fixture: two contacts must arm the pinch");
    var scaleAfterPinch = box.z.scale;
    assert_(box.z.scale !== 0, "fixture: the pinch must produce a frame");
    // Lift the second contact. Its pointerup arrives before the touchend that
    // reports the single remaining contact, exactly as a browser delivers it.
    pointer(box.vp, "pointerup", cx + 60, cy, 12, "touch");
    touch(box.vp, "touchend", [{ x: cx - 60, y: cy, id: 1 }]);
    assert_(!box.z.pinching, "fixture: the pinch must have ended");
    var xBefore = box.z.x;
    var yBefore = box.z.y;
    var minX = size.w - box.z.boxW * box.z.scale;
    var minY = size.h - box.z.boxH * box.z.scale;
    var diag = " [x=" + xBefore + " y=" + yBefore + " scale=" + box.z.scale +
      " minX=" + minX + " minY=" + minY + " vw=" + size.w + " vh=" + size.h + "]";
    assert_(xBefore < -5 && xBefore > minX + 5,
      "fixture: the pan must not start on a horizontal clamp edge" + diag);
    assert_(yBefore < -5 && yBefore > minY + 5,
      "fixture: the pan must not start on a vertical clamp edge" + diag);
    // No new press: this single move of the contact still down is the whole
    // regression. 40px right and 24px down must be a 1:1 continuation.
    pointer(box.vp, "pointermove", cx - 20, cy + 24, 11, "touch");
    approx(box.z.x, xBefore + 40, 2.5, "the remaining contact must pan the image" + diag);
    approx(box.z.y, yBefore + 24, 2.5, "the remaining contact must pan on both axes" + diag);
    approx(box.z.scale, scaleAfterPinch, 1e-9, "a pan must not change the zoom" + diag);
    pointer(box.vp, "pointerup", cx - 20, cy + 24, 11, "touch");
    touch(box.vp, "touchend", []);
  }

  async function runGate1015Touch(bucket) {
    await run("gate1015_fit_survives_a_gesture_started_without_geometry", bucket, fitSurvivesAGestureStartedWithoutGeometry);
    await run("gate1015_fit_resets_an_active_pinch", bucket, fitResetsAnActivePinch);
    await run("gate1015_replaced_finger_reanchors_the_pinch", bucket, replacedFingerReanchorsThePinch);
    await run("gate1015_pinch_end_keeps_the_remaining_finger_panning", bucket, pinchEndKeepsTheRemainingFingerPanning);
    await run("gate1015_keyboard_zoom_mid_pinch_is_not_undone", bucket, keyboardZoomMidPinchIsNotUndone);
    await run("gate1015_wheel_zoom_mid_pinch_is_not_undone", bucket, wheelZoomMidPinchIsNotUndone);
    await run("gate1015_pinch_reversal_responds_immediately", bucket, pinchReversalRespondsImmediately);
    await run("gate1015_pinch_spread_is_path_independent", bucket, pinchSpreadIsPathIndependent);
    await run("gate1015_same_source_navigation_recentres", bucket, sameSourceNavigationRecentres);
  }

  async function runGate1015Mouse(bucket) {
    await run("gate1015_wheel_zoom_mid_pan_does_not_snap", bucket, wheelZoomMidPanDoesNotSnap);
    await run("gate1015_keyboard_zoom_mid_pan_does_not_snap", bucket, keyboardZoomMidPanDoesNotSnap);
    await run("gate1015_resize_recomputes_the_zoom_out_limit", bucket, resizeRecomputesTheZoomOutLimit);
    await run("gate1015_overshoot_reversal_responds_immediately", bucket, overshootReversalRespondsImmediately);
    await run("gate1015_line_mode_wheel_zooms", bucket, lineModeWheelZooms);
  }

  window.__composedChecks = async function (mode) {
    var bucket = results();
    if (mode === "mobile") await runMobile(bucket);
    else await runDesktop(bucket);
    return bucket;
  };

  // Used by the Python-side keyboard/focus test: opens a lightbox and leaves
  // it in the DOM so the test can drive it with real key presses.
  window.__composedOpenFit = async function () {
    var box = await openBox(IMG_W, IMG_H);
    box.fitBtn.focus();
    return true;
  };
})();
