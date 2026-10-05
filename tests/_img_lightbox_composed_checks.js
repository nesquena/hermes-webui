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
 *   5. selected non-English locale renders the Fit button text/title/aria
 *   6. measured 44x44 touch target and Fit/close non-overlap at desktop and
 *      mobile widths (real getBoundingClientRect, not source strings)
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

  function pointer(vp, type, x, y) {
    vp.dispatchEvent(new PointerEvent(type, {
      bubbles: true,
      cancelable: true,
      composed: true,
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
      button: 0,
      buttons: type === "pointerup" ? 0 : 1,
      clientX: x,
      clientY: y,
    }));
  }

  function wheel(vp, dy, x, y) {
    vp.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      composed: true,
      deltaMode: 0,
      deltaY: dy,
      clientX: x,
      clientY: y,
    }));
  }

  function touch(el, type, points) {
    var list = points.map(function (p, i) {
      return new Touch({
        identifier: i + 1,
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

    // 5. Locale rendering of the Fit control.
    await run("locale_zh_renders", bucket, async function () {
      window.setLocale("zh");
      var box = await openBox(IMG_W, IMG_H);
      var zh = "\u9002\u5e94";
      assert_(box.fitBtn.textContent === zh, "zh Fit text expected " + zh + " got " + box.fitBtn.textContent);
      var title = box.fitBtn.getAttribute("title");
      var aria = box.fitBtn.getAttribute("aria-label");
      assert_(title && title !== "Reset zoom to fit (F)" && title !== "Fit", "zh title must be localized: " + title);
      assert_(aria === title, "aria-label must mirror the localized title");
    });

    await run("locale_ja_renders", bucket, async function () {
      window.setLocale("ja");
      var box = await openBox(IMG_W, IMG_H);
      var ja = "\u30d5\u30a3\u30c3\u30c8";
      assert_(box.fitBtn.textContent === ja, "ja Fit text expected " + ja + " got " + box.fitBtn.textContent);
      window.setLocale("en");
    });

    // 6. Measured geometry at the desktop width.
    await run("geometry_min_touch_target", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var fit = box.fitBtn.getBoundingClientRect();
      assert_(fit.width >= 44 - 0.5, "Fit button measured width " + fit.width + " < 44");
      assert_(fit.height >= 44 - 0.5, "Fit button measured height " + fit.height + "<44");
      var style = getComputedStyle(box.fitBtn);
      assert_(parseFloat(style.minHeight) >= 44, "min-height contract lost: " + style.minHeight);
    });

    await run("geometry_no_overlap", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var fit = box.fitBtn.getBoundingClientRect();
      var close = box.closeBtn.getBoundingClientRect();
      assert_(fit.right <= close.left + 0.5, "Fit and close overlap: fit.right=" + fit.right + " close.left=" + close.left);
      assert_(close.right <= window.innerWidth + 0.5, "close button escapes the viewport");
      assert_(fit.left >= 0 && fit.top >= 0, "Fit button escapes the viewport");
    });
  }

  async function runMobile(bucket) {
    await run("geometry_min_touch_target", bucket, async function () {
      var box = await openBox(IMG_W, IMG_H);
      var fit = box.fitBtn.getBoundingClientRect();
      assert_(fit.width >= 44 - 0.5, "mobile Fit button measured width " + fit.width + " < 44");
      assert_(fit.height >= 44 - 0.5, "mobile Fit button measured height " + fit.height + " < 44");
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
