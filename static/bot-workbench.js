/* Bot presentation uses the authorized profile catalog; no additional reads or writes.
   Favorites/layout belong to the signed-in viewer. Drafts remain in their original DOM. */
(function () {
  'use strict';
  const memory = new Map();
  let filter = 'all', identity = '';
  const tx = key => t('bot_workspace_' + key);
  function preferences() {
    const key = 'synthpulse-bot-workspace:' + String((window.__GOV_ME__ || {}).email || 'local');
    if (identity !== key) { identity = key; filter = 'all'; }
    let value = memory.get(key);
    if (!value) {
      try { value = JSON.parse(localStorage.getItem(key) || '{}'); } catch (_) {}
      value = {favorites: Array.isArray(value?.favorites) ? value.favorites.filter(x => typeof x === 'string') : [], layout: value?.layout === 'list' ? 'list' : 'grid'};
      memory.set(key, value);
    }
    return value;
  }
  function save() { try { localStorage.setItem(identity, JSON.stringify(preferences())); } catch (_) {} }
  function button(label, className, action) {
    const node = document.createElement('button'); node.type = 'button'; node.className = className;
    node.textContent = label; node.onclick = action; return node;
  }
  function favorite(p) {
    const selected = preferences().favorites.includes(p.name);
    const node = button(selected ? '★' : '☆', 'bot-favorite', () => {
      const value = preferences();
      value.favorites = selected ? value.favorites.filter(name => name !== p.name) : [...value.favorites, p.name];
      save(); filterProfilesPanel();
      (document.querySelector('[data-bot-favorite="' + CSS.escape(p.name) + '"]') || document.querySelector('[data-bot-filter="favorites"]'))?.focus();
    });
    node.dataset.botFavorite = p.name;
    node.setAttribute('aria-label', tx(selected ? 'unfavorite' : 'favorite') + ': ' + botDisplayName(p));
    node.setAttribute('aria-pressed', String(selected)); node.title = node.getAttribute('aria-label');
    return node;
  }
  function matches(p, words) {
    const value = preferences();
    const text = [botDisplayName(p), p.name, p.bot?.description, p.bot?.category, p.bot?.role, p.model].join(' ').toLocaleLowerCase();
    return words.every(word => text.includes(word)) && (filter !== 'favorites' || value.favorites.includes(p.name));
  }
  function reset() {
    filter = 'all'; const search = document.getElementById('profileSearch');
    if (search) search.value = ''; filterProfilesPanel();
    const directorySearch = document.getElementById('botDirectorySearch');
    (directorySearch?.getClientRects().length ? directorySearch : search)?.focus();
  }
  function toolbar() {
    let host = document.getElementById('botCatalogFilters');
    if (!host) {
      host = document.createElement('div'); host.id = 'botCatalogFilters'; host.className = 'bot-catalog-filters';
      document.getElementById('profileSearchStatus')?.before(host);
    }
    host.replaceChildren(); host.setAttribute('aria-label', tx('filter'));
    for (const key of ['all', 'favorites']) {
      const node = button(tx(key), 'bot-filter', () => { filter = key; filterProfilesPanel(); document.querySelector('[data-bot-filter="' + key + '"]')?.focus(); });
      node.dataset.botFilter = key; node.setAttribute('aria-pressed', String(filter === key)); host.appendChild(node);
    }
  }
  async function chat(p, node) {
    node.disabled = true;
    try {
      await switchToProfile(p.name);
      if (S.activeProfile === p.name) { mobileSwitchPanel('chat'); document.getElementById('msg')?.focus(); }
    } catch (error) { showToast(error.message); }
    finally { node.disabled = false; }
  }
  function chatButton(p) {
    const node = button(tx('chat'), 'sm-btn bot-chat-action', () => chat(p, node));
    node.dataset.botChat = p.name; return node;
  }
  function tags(p) {
    const values = [p.bot?.role, p.bot?.category];
    if (p.name === S.activeProfile) values.push(tx('selected'));
    if (p.visible === false) values.push(tx('hidden'));
    return values.filter(Boolean).map(value => '<span class="bot-meta-pill">' + esc(value) + '</span>').join('');
  }
  function render(data, profiles) {
    const value = preferences(); toolbar();
    const empty = document.getElementById('profileDetailEmpty'); if (!empty) return;
    empty.dataset.botDirectory = '1';
    let directory = document.getElementById('botDirectory');
    if (!directory) {
      directory = document.createElement('section'); directory.id = 'botDirectory'; empty.appendChild(directory);
      directory.innerHTML = '<header class="bot-directory-head"><div><h2>' + esc(tx('title')) + '</h2><p>' + esc(tx('intro')) + '</p></div></header><div class="bot-directory-toolbar"><input id="botDirectorySearch" type="search"><div class="bot-directory-controls"><div class="bot-directory-filters"></div><div class="bot-layout-controls"></div></div></div><p id="botDirectoryCount" role="status"></p><div class="bot-directory-grid"></div>';
      const create = button(t('new_profile'), 'sm-btn bot-directory-create workspace-primary', () => openProfileCreate());
      directory.querySelector('.bot-directory-head').appendChild(create);
      const search = directory.querySelector('#botDirectorySearch');
      search.placeholder = tx('search'); search.setAttribute('aria-label', search.placeholder);
      search.oninput = () => { document.getElementById('profileSearch').value = search.value; filterProfilesPanel(); };
      const filters = directory.querySelector('.bot-directory-filters'); filters.setAttribute('aria-label', tx('filter'));
      for (const key of ['all', 'favorites']) {
        const node = button(tx(key), 'bot-filter', () => { filter = key; filterProfilesPanel(); });
        node.dataset.directoryFilter = key; filters.appendChild(node);
      }
      const layouts = directory.querySelector('.bot-layout-controls'); layouts.setAttribute('aria-label', tx('layout'));
      for (const layout of ['grid', 'list']) {
        const node = button(tx(layout), 'bot-filter', () => { preferences().layout = layout; save(); filterProfilesPanel(); });
        node.dataset.botLayout = layout; layouts.appendChild(node);
      }
    }
    directory.dataset.layout = value.layout;
    directory.querySelector('h2').textContent = tx('title');
    directory.querySelector('.bot-directory-head p').textContent = tx('intro');
    directory.querySelector('.bot-directory-create').textContent = t('new_profile');
    directory.querySelector('.bot-directory-create').hidden = data.single_profile_mode || !_canUseFeature('profiles:admin');
    const search = directory.querySelector('#botDirectorySearch'), currentQuery = document.getElementById('profileSearch')?.value || '';
    search.placeholder = tx('search'); search.setAttribute('aria-label', search.placeholder);
    if (search.value !== currentQuery) search.value = currentQuery;
    directory.querySelectorAll('[data-directory-filter]').forEach(node => { node.setAttribute('aria-pressed', String(filter === node.dataset.directoryFilter)); node.textContent = tx(node.dataset.directoryFilter); });
    directory.querySelectorAll('[data-bot-layout]').forEach(node => { node.setAttribute('aria-pressed', String(value.layout === node.dataset.botLayout)); node.textContent = tx(node.dataset.botLayout); });
    directory.querySelector('#botDirectoryCount').textContent = t('profiles_search_count', profiles.length, (data.profiles || []).length);
    const grid = directory.querySelector('.bot-directory-grid');
    const focused = grid.contains(document.activeElement) ? {key:document.activeElement.dataset.botOpen ? 'bot-open' : document.activeElement.dataset.botStart ? 'bot-start' : 'bot-favorite', name:document.activeElement.dataset.botOpen || document.activeElement.dataset.botStart || document.activeElement.dataset.botFavorite} : null;
    grid.replaceChildren();
    for (const p of profiles) {
      const card = document.createElement('article'); card.className = 'bot-directory-card';
      const open = button('', 'bot-directory-open', () => openProfileDetail(p.name)); open.dataset.botOpen = p.name;
      open.innerHTML = '<div class="bot-directory-identity">' + botAvatarHtml(p) + '<div><h3>' + esc(botDisplayName(p)) + '</h3><div class="bot-meta-pills">' + tags(p) + '</div></div><span class="bot-card-chevron" aria-hidden="true">›</span></div><p>' + esc(p.bot?.description || t('bot_description_empty')) + '</p>';
      open.setAttribute('aria-label', botDisplayName(p) + ' · ' + tx('overview'));
      const footer = document.createElement('div'); footer.className = 'bot-directory-footer';
      const model = document.createElement('span'); model.className = 'bot-model-label'; model.textContent = typeof p.model === 'string' ? p.model.split('/').pop() : ''; model.title = p.model || '';
      const start = button(tx('start'), 'sm-btn bot-start-action', () => chat(p, start)); start.dataset.botStart = p.name;
      start.setAttribute('aria-label', tx('chat') + ': ' + botDisplayName(p));
      footer.append(start, model, favorite(p)); card.append(open, footer); grid.appendChild(card);
    }
    if (!profiles.length) {
      const state = document.createElement('div'); state.className = 'sp-search-empty';
      const message = document.createElement('p'); message.textContent = tx(filter === 'favorites' ? 'no_favorites' : 'no_results');
      const clear = button(tx('reset'), 'sm-btn', reset); clear.dataset.resetBots = '';
      state.append(message, clear); grid.appendChild(state);
    }
    if (focused?.name) (grid.querySelector('[data-' + focused.key + '="' + CSS.escape(focused.name) + '"]') || search).focus({preventScroll:true});
  }
  function mountDetail(p) {
    const content = document.querySelector('#profileDetailBody .bot-detail-content'); if (!content) return;
    const hero = content.querySelector('.bot-overview');
    const pills = document.createElement('div'); pills.className = 'bot-meta-pills'; pills.innerHTML = tags(p);
    hero.querySelector('h2')?.after(pills);
    const actions = document.createElement('div'); actions.className = 'bot-overview-actions';
    const back = button(tx('all'), 'sm-btn', () => { _clearProfileDetail(); document.querySelector('#botDirectory [data-bot-open="' + CSS.escape(p.name) + '"]')?.focus(); });
    actions.append(back, chatButton(p)); hero.appendChild(actions);
    const links = content.querySelector('.bot-configuration-links');
    for (const node of links?.querySelectorAll('button') || []) {
      const section = /openBotConfiguration\('([^']+)'\)/.exec(node.getAttribute('onclick') || '')?.[1];
      if (section === 'identity') { actions.appendChild(node); continue; }
      const caption = {soul: 'instructions_hint', memory: 'memory_hint', skills: 'skills_hint', settings: 'tools_hint'}[section];
      if (!caption) continue;
      const title = document.createElement('strong'); title.textContent = node.textContent;
      const hint = document.createElement('small'); hint.textContent = tx(caption);
      node.replaceChildren(title, hint);
    }
    const tabs = document.createElement('div'); tabs.className = 'bot-detail-tabs'; tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', tx('sections'));
    hero.after(tabs);
    const definitions = [
      ['overview', '.bot-configuration-links, .bot-duties-card'],
      ['work', '.bot-jobs, .bot-work'],
      ['identity', '.bot-editor'],
      ['configuration', '.bot-scout, .bot-scout-card, .bot-technical-details']
    ];
    const buttons = [];
    for (const [key, selector] of definitions) {
      const section = document.createElement('section'); section.className = 'bot-detail-section'; section.id = 'botDetailSection-' + key;
      section.setAttribute('role', 'tabpanel'); section.setAttribute('aria-labelledby', 'botDetailTab-' + key);
      content.querySelectorAll(selector).forEach(node => section.appendChild(node));
      if (key === 'work') { const hint = document.createElement('p'); hint.className = 'bot-section-intro'; hint.textContent = tx('work_hint'); section.prepend(hint); }
      const node = button(tx(key), '', () => select(key)); node.id = 'botDetailTab-' + key;
      node.setAttribute('role', 'tab'); node.setAttribute('aria-controls', section.id);
      buttons.push(node); tabs.appendChild(node); content.appendChild(section);
    }
    function select(key) {
      for (const node of buttons) {
        const selected = node.id === 'botDetailTab-' + key;
        node.setAttribute('aria-selected', String(selected)); node.tabIndex = selected ? 0 : -1;
        document.getElementById(node.getAttribute('aria-controls')).hidden = !selected;
      }
      if (key === 'overview') document.getElementById('profileDetailBody').scrollTop = 0;
    }
    tabs.onkeydown = event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const index = buttons.indexOf(document.activeElement), count = buttons.length;
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? count - 1 : (index + (event.key === 'ArrowLeft' ? -1 : 1) + count) % count;
      buttons[next].click(); buttons[next].focus();
    };
    select('overview');
  }
  const isFavorite = name => preferences().favorites.includes(name);
  window.BotWorkbench = {matches, render, mountDetail, reset, isFavorite};
})();
