/* TEMPORARY diagnostic probe for the iOS 26/27 status-bar "scroll pocket" blur.
 *
 * Why this exists: the installed web app cannot be inspected from the Mac, and
 * on this origin (plain HTTP on a LAN/Tailscale address) it is not a secure
 * context, so there is no service worker and every `?v=` shell asset is frozen
 * in the browser HTTP cache. The app therefore reports what it actually sees as
 * a GET to /ios-probe?… — the request path is what matters, because the WebUI
 * access log records it (the route itself 302s/404s harmlessly).
 *
 * Read it out of the log with:
 *   grep -o '/ios-probe?[^"]*' ~/.hermes/profiles/paimon/webui.log | tail -3
 *
 * Delete this file together with its <script> tag in index.html once the blur
 * investigation is settled. It renders nothing and touches no app state.
 */
(function () {
  'use strict';

  function envInsetTop() {
    try {
      var d = document.createElement('div');
      d.style.cssText =
        'position:fixed;top:0;left:0;width:0;overflow:hidden;height:env(safe-area-inset-top,0px)';
      document.documentElement.appendChild(d);
      var h = d.getBoundingClientRect().height;
      document.documentElement.removeChild(d);
      return Math.round(h);
    } catch (e) {
      return -1;
    }
  }

  function read() {
    var out = [];
    function add(k, v) {
      out.push(k + '=' + encodeURIComponent(String(v)));
    }
    try {
      var root = document.documentElement;
      var tb = document.querySelector('.app-titlebar');
      var cs = tb ? window.getComputedStyle(tb) : null;
      var rect = tb ? tb.getBoundingClientRect() : null;
      var link = document.querySelector('link[rel="stylesheet"]');
      var meta = document.querySelector('meta[name="apple-mobile-web-app-status-bar-style"]');
      add('sa', window.navigator.standalone === true);
      add('dm', !!(window.matchMedia && window.matchMedia('(display-mode: standalone)').matches));
      add('swa', !!window.navigator.serviceWorker);
      add('swc', !!(window.navigator.serviceWorker && window.navigator.serviceWorker.controller));
      add('proto', location.protocol.replace(':', ''));
      add('meta', meta ? meta.getAttribute('content') : 'none');
      add('pos', cs ? cs.position : '-');
      add('bg', cs ? cs.backgroundColor : '-');
      add('h', rect ? Math.round(rect.height) : -1);
      add('top', rect ? Math.round(rect.top) : -999);
      add('safe', window.getComputedStyle(root).getPropertyValue('--app-titlebar-safe-top').trim() || '-');
      add('env', envInsetTop());
      add('ih', window.innerHeight);
      add('sh', window.screen.height);
      add('dh', root.clientHeight);
      add('vv', window.visualViewport ? Math.round(window.visualViewport.height) : -1);
      add('cls', root.className);
      add('href', link ? link.getAttribute('href') : '-');
      add('t', Date.now());
    } catch (e) {
      add('err', e && e.message ? e.message : 'unknown');
    }
    return out.join('&');
  }

  function send() {
    try {
      new Image().src = '/ios-probe?' + read();
    } catch (e) {
      /* diagnostic only — never break the app */
    }
  }

  if (document.readyState === 'complete') send();
  else window.addEventListener('load', send);
  window.setTimeout(send, 2500);
})();
