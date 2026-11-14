// Theme: same localStorage key the existing pages use. Default dark. Runs in <head> before paint.
(() => {
  const root = document.documentElement;
  let stored; try { stored = localStorage.getItem('theme'); } catch (e) {}
  root.dataset.theme = stored === 'light' ? 'light' : 'dark';
  document.addEventListener('DOMContentLoaded', () => {
    const btns = document.querySelectorAll('.site-header .theme-toggle');
    // Pages that still carry their own theme script keep an inert #theme-toggle inside .legacy-hooks.
    // Delegating to it runs that page's own side effects (e.g. the homepage's theme-paired screenshots).
    // ceiling: remove once every page drops its own theme script.
    const legacy = document.querySelector('.legacy-hooks #theme-toggle');
    const label = () => {
      const dark = root.dataset.theme !== 'light';
      btns.forEach(b => { b.textContent = dark ? 'Light' : 'Dark'; b.setAttribute('aria-label', 'Switch to ' + (dark ? 'light' : 'dark') + ' theme'); });
    };
    label();
    btns.forEach(b => b.addEventListener('click', () => {
      if (legacy) {
        legacy.click();
      } else {
        root.dataset.theme = root.dataset.theme === 'light' ? 'dark' : 'light';
        try { localStorage.setItem('theme', root.dataset.theme); } catch (e) {}
      }
      label();
    }));
    // Menu: close on Escape or an outside click
    const menu = document.querySelector('.site-header details.menu');
    if (menu) {
      document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && menu.open) { menu.open = false; menu.querySelector('summary').focus(); }
      });
      // the theme item delegates to the hidden legacy toggle; that click is not "outside"
      document.addEventListener('click', e => {
        if (menu.open && !menu.contains(e.target) && !e.target.closest('.legacy-hooks')) menu.open = false;
      });
      // a same-page link (e.g. /#compare on home) scrolls without navigating, so close explicitly
      menu.addEventListener('click', e => { if (e.target.closest('a')) menu.open = false; });
    }
    // Scrolling regions with nothing focusable inside (wide tables, long code lines) become focusable, labelled
    // regions, so keyboard users can scroll them; re-checked on resize, since what scrolls depends on the width.
    const scrollRegions = () => {
      document.querySelectorAll('main *').forEach(el => {
        const s = getComputedStyle(el);
        const x = /(auto|scroll)/.test(s.overflowX) && el.scrollWidth > el.clientWidth + 1;
        const y = /(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 1;
        const ours = el.hasAttribute('data-scroll-region');
        if ((x || y) && el.offsetParent && (ours || !el.hasAttribute('tabindex'))) {
          if (!ours && el.querySelector('a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])')) return;
          const what = el.querySelector('table') || el.tagName === 'TABLE' ? 'Table' : (el.closest('pre') || el.querySelector('pre, code') ? 'Code' : 'Content');
          el.setAttribute('data-scroll-region', '');
          el.setAttribute('tabindex', '0');
          el.setAttribute('role', 'region');
          el.setAttribute('aria-label', what + (x ? ', scrolls sideways' : ', scrolls'));
        } else if (ours && !(x || y)) {
          ['data-scroll-region', 'tabindex', 'role', 'aria-label'].forEach(a => el.removeAttribute(a));
        }
      });
    };
    scrollRegions();
    window.addEventListener('load', scrollRegions);  // late styles (home's highlight.js theme) can add scrolling
    let pendingScroll = null;
    window.addEventListener('resize', () => { cancelAnimationFrame(pendingScroll); pendingScroll = requestAnimationFrame(scrollRegions); });
    // Skip link: move focus too (home's smooth-scroll handler cancels the default jump)
    const skip = document.querySelector('.skip');
    const main = document.getElementById('main');
    if (skip && main) skip.addEventListener('click', () => {
      if (!main.hasAttribute('tabindex')) main.setAttribute('tabindex', '-1');
      main.focus();
    });
  });
})();
