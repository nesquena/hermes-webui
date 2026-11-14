// Theme: same localStorage key the existing pages use. Default dark. Runs in <head> before paint.
(() => {
  const root = document.documentElement;
  let stored; try { stored = localStorage.getItem('theme'); } catch (e) {}
  root.dataset.theme = stored === 'light' ? 'light' : 'dark';
  document.addEventListener('DOMContentLoaded', () => {
    const btns = document.querySelectorAll('.theme-toggle');
    const label = () => {
      const dark = root.dataset.theme === 'dark';
      btns.forEach(b => { b.textContent = dark ? 'Light' : 'Dark'; b.setAttribute('aria-label', 'Switch to ' + (dark ? 'light' : 'dark') + ' theme'); });
    };
    label();
    btns.forEach(b => b.addEventListener('click', () => {
      root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem('theme', root.dataset.theme); } catch (e) {}
      label();
    }));
    // Phone menu: close on Escape or an outside click
    const menu = document.querySelector('details.menu');
    if (menu) {
      document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && menu.open) { menu.open = false; menu.querySelector('summary').focus(); }
      });
      document.addEventListener('click', e => { if (menu.open && !menu.contains(e.target)) menu.open = false; });
    }
  });
})();
