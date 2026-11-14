// Community page: progressive search and category filter over the project cards. Without JS the form stays hidden
// and every card shows. Filtering only toggles `hidden`; nothing typed is ever inserted as markup.
(() => {
  'use strict';
  const form = document.getElementById('catalog-filters');
  if (!form) return;
  const query = document.getElementById('catalog-query');
  const category = document.getElementById('catalog-category');
  const status = document.getElementById('catalog-status');
  const clear = form.querySelector('button[type=reset]');
  const sections = [...document.querySelectorAll('main .section')].filter(s => s.querySelector('a.project-card'));
  const entries = sections.map((section, index) => {
    section.classList.add('catalog-section');
    const option = document.createElement('option');
    option.value = String(index + 1);
    option.textContent = section.querySelector('h2').textContent.trim();
    category.append(option);
    const cards = [...section.querySelectorAll('a.project-card')].map(card => ({card, text: card.textContent.toLocaleLowerCase()}));
    return {section, value: option.value, cards};
  });
  const total = entries.reduce((sum, e) => sum + e.cards.length, 0);
  function filter() {
    const words = query.value.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
    let shown = 0, visible = 0;
    for (const entry of entries) {
      let matches = 0;
      for (const {card, text} of entry.cards) {
        const show = (!category.value || category.value === entry.value) && words.every(w => text.includes(w));
        card.hidden = !show;
        if (show) matches++;
      }
      entry.section.hidden = matches === 0;
      // keep the alternating section backgrounds alternating among the sections still shown
      if (matches) entry.section.classList.toggle('section-alt', visible++ % 2 === 0);
      shown += matches;
    }
    const active = words.length > 0 || category.value !== '';
    clear.hidden = !active;
    status.textContent = !active ? `Showing all ${total} projects`
      : shown ? `Showing ${shown} of ${total} projects` : 'No projects match. Try another search.';
  }
  form.addEventListener('submit', e => e.preventDefault());
  form.addEventListener('input', filter);
  form.addEventListener('change', filter);
  form.addEventListener('reset', () => { query.value = ''; category.value = ''; filter(); query.focus(); });
  form.hidden = false;
})();
