/* Accessibility glue for Material's runtime markup. Runs on every page load, including Material's
   instant navigation (document$ emits each time).
   - Tables that scroll sideways become focusable, labelled regions, so keyboard users can scroll them.
   - Each code block's action bar (a nav landmark) gets a unique name, so pages don't carry many
     identically named landmarks. */
(function () {
  function labelFor(el) {
    for (var n = el.previousElementSibling; n; n = n.previousElementSibling) {
      if (/^H[1-6]$/.test(n.tagName)) return n.textContent.replace(/\s*¶\s*$/, "").trim();
    }
    return "";
  }
  function syncScrollRegions() {
    document.querySelectorAll(".md-typeset__scrollwrap").forEach(function (wrap) {
      var scrolls = wrap.scrollWidth > wrap.clientWidth + 1;
      if (scrolls) {
        if (!wrap.hasAttribute("tabindex")) wrap.setAttribute("tabindex", "0");
        wrap.setAttribute("role", "region");
        var heading = labelFor(wrap);
        wrap.setAttribute("aria-label", (heading ? heading + " table" : "Table") + ", scrolls sideways");
      } else if (wrap.getAttribute("role") === "region") {
        wrap.removeAttribute("tabindex");
        wrap.removeAttribute("role");
        wrap.removeAttribute("aria-label");
      }
    });
  }
  function relabelCodeActions() {
    document.querySelectorAll(".md-code__nav").forEach(function (nav, i) {
      nav.setAttribute("aria-label", "Code block " + (i + 1) + " actions");
    });
  }
  function run() {
    // After Material mounts this page's components (table wrapping, code actions).
    requestAnimationFrame(function () { syncScrollRegions(); relabelCodeActions(); });
  }
  if (typeof document$ !== "undefined" && document$.subscribe) {
    document$.subscribe(run);
  } else {
    document.addEventListener("DOMContentLoaded", run);
  }
  var pending = null;
  window.addEventListener("resize", function () {
    if (pending) cancelAnimationFrame(pending);
    pending = requestAnimationFrame(syncScrollRegions);
  });
})();
