/* Accessibility glue for Material's runtime markup. Runs on every page load, including Material's
   instant navigation (document$ emits each time).
   - Tables that scroll sideways become focusable, labelled regions, so keyboard users can scroll them.
   - Each code block's action bar (a nav landmark) gets a unique name, so pages don't carry many
     identically named landmarks.
   - The header's drawer and search buttons (overrides/partials/header.html) toggle Material's hidden
     checkboxes, keep aria-expanded in sync, move focus into what they open, and Escape closes the drawer and
     returns focus to its button. The drawer's nested-panel titles (Material's back control) join the tab order. */
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
    // After Material mounts this page's components (table wrapping, code actions, the swapped-in sidebar).
    requestAnimationFrame(function () { syncScrollRegions(); relabelCodeActions(); wirePanels(); });
  }
  function drawerOpen() {
    var d = document.getElementById("__drawer");
    return !!d && d.checked;
  }
  // Focus the drawer's first control that is actually showing. Its panels slide over each other (the page's
  // nested panel covers the root panel's links), so retry for a few frames until one passes a hit test.
  function focusDrawer(stillWanted) {
    var tries = 0;
    (function attempt() {
      if (!stillWanted()) return;
      var target = Array.prototype.find.call(
        document.querySelectorAll(".md-sidebar--primary a, .md-sidebar--primary label[tabindex]"),
        function (el) {
          if (el.offsetParent === null) return false;
          var r = el.getBoundingClientRect();
          var hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          return !!hit && (hit === el || el.contains(hit));
        });
      if (target) target.focus();
      else if (++tries < 20) setTimeout(attempt, 30);
    })();
  }

  // The drawer opens on the page's nested panel; its title is the back control to the root panel (where the
  // Site links are). Material leaves it out of the tab order, though it handles Enter on a focused label.
  // Going back from it moves focus to the panel now showing. Material replaces the sidebar on every instant
  // navigation, so this runs on each document$ emission; data-site-back keeps it to one binding per label.
  // Drawer mode only: the section openers (labels) become tab stops, and Enter on one moves focus to the new
  // panel's back title. On wide screens the same labels are section headings, so they stay out of the tab order.
  var drawerMode = window.matchMedia("(max-width: 76.234375em)");
  function syncOpeners() {
    document.querySelectorAll(".md-sidebar--primary label.md-nav__link[for]").forEach(function (l) {
      if (drawerMode.matches) l.setAttribute("tabindex", "0");
      else l.setAttribute("tabindex", "");
    });
  }
  if (drawerMode.addEventListener) drawerMode.addEventListener("change", syncOpeners);
  function wirePanels() {
    syncOpeners();
    document.querySelectorAll(".md-sidebar--primary label.md-nav__link[for]:not([data-site-open])").forEach(function (l) {
      l.setAttribute("data-site-open", "");
      l.addEventListener("keydown", function (e) {
        if (e.key !== "Enter" && e.key !== " ") return;
        var toggle = document.getElementById(l.getAttribute("for"));
        setTimeout(function () {
          if (!toggle || !toggle.checked || !drawerOpen()) return;
          var nav = l.parentElement.querySelector(":scope > .md-nav");
          var title = nav && nav.querySelector(":scope > .md-nav__title");
          if (title) title.focus();
        }, 0);
      });
    });
    document.querySelectorAll('.md-sidebar--primary label.md-nav__title:not([for="__drawer"]):not([data-site-back])').forEach(function (l) {
      l.setAttribute("data-site-back", "");
      if (!l.hasAttribute("tabindex") || l.tabIndex < 0) l.setAttribute("tabindex", "0");
      // Material flips the panel's checkbox without a change event, so check it just after the key
      l.addEventListener("keydown", function (e) {
        if (e.key !== "Enter" && e.key !== " ") return;
        var panel = document.getElementById(l.getAttribute("for"));
        // Back always lands on the panel one level up: focus its first control (the root panel's is the logo link,
        // then the Site links). Direct, since a hit test would race the slide.
        setTimeout(function () {
          if (!panel || panel.checked || !drawerOpen()) return;
          var up = l.closest(".md-nav").parentElement.closest(".md-nav");
          var first = up && Array.prototype.find.call(up.querySelectorAll("a, label[tabindex]"), function (el) {
            return el.closest(".md-nav") === up;
          });
          if (first) first.focus();
        }, 0);
      });
    });
  }

  // The header is outside Material's instant-navigation swap, so wire it once.
  function wireToggles() {
    var opener = document.querySelector('[data-site-toggle="__drawer"]');
    document.querySelectorAll("[data-site-toggle]").forEach(function (btn) {
      var box = document.getElementById(btn.getAttribute("data-site-toggle"));
      if (!box) return;
      var sync = function () { btn.setAttribute("aria-expanded", box.checked ? "true" : "false"); };
      btn.addEventListener("click", function () {
        box.checked = !box.checked;
        box.dispatchEvent(new Event("change", { bubbles: true }));
        sync();
        if (!box.checked) return;
        // move focus into what opened
        if (box.id === "__drawer") { focusDrawer(drawerOpen); return; }
        var q = document.querySelector("[data-md-component=search-query]");
        if (q) setTimeout(function () { q.focus(); }, 0);
      });
      box.addEventListener("change", function () {
        sync();
        // search closed (Escape, overlay): focus would drop to <body>; return it to the button
        if (box.id === "__search" && !box.checked && btn.offsetParent !== null) {
          var a = document.activeElement;
          if (!a || a === document.body || a.closest(".md-search")) btn.focus();
        }
      });
      sync();
    });
    // Tab past the drawer's last link would move focus into the page under the overlay: close the drawer instead
    document.addEventListener("focusin", function (e) {
      if (!drawerOpen() || e.target === opener || e.target.closest(".md-sidebar--primary")) return;
      var drawer = document.getElementById("__drawer");
      drawer.checked = false;
      drawer.dispatchEvent(new Event("change", { bubbles: true }));
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || !drawerOpen()) return;
      var drawer = document.getElementById("__drawer");
      drawer.checked = false;
      drawer.dispatchEvent(new Event("change", { bubbles: true }));
      if (opener) opener.focus();
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wireToggles);
  else wireToggles();
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
