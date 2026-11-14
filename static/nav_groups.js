// Grouped navigation rail (10 Nov 2026).
//
// The desktop rail grew to sixteen destinations in one flat column. This file
// orders them in three groups (work, building blocks, organisation) and puts a
// quiet separator between the groups: a hairline in the icon rail, a small
// label in the rail with labels. It only arranges what is already there:
// who may see which destination stays with governance and the tab settings.
//
// A personal order applies after Studio and Chats. Separators only appear while the groups
// are still contiguous, so a custom order never gets misleading headings.
(function(){
  'use strict';
  const GROUPS = [
    { key: 'work', nl: 'Werk', en: 'Work', panels: ['studio', 'chat', 'profiles', 'tasks', 'kanban', 'projects'] },
    { key: 'build', nl: 'Bouwstenen', en: 'Building blocks', panels: ['modules', 'skills', 'memory', 'integrations', 'workspaces', 'files', 'todos'] },
    { key: 'org', nl: 'Organisatie', en: 'Organisation', panels: ['organisation', 'management', 'insights', 'approvals', 'governance', 'logs'] }
  ];
  const DEFAULT_ORDER = GROUPS.flatMap(g => g.panels);
  const groupOf = panel => (GROUPS.find(g => g.panels.includes(panel)) || {}).key || '';
  const lang = () => ((document.documentElement.lang || 'nl').toLowerCase().startsWith('en') ? 'en' : 'nl');
  const isMember = () => window._navAudience === 'member' && !window._capabilityNavigation;

  // Default order: grouped, unless the person saved an order of their own.
  const original = window._applyTabOrder;
  if (typeof original === 'function') {
    window._applyTabOrder = function(order){
      let custom = Array.isArray(order) ? _sanitizeTabPanelList(order) : [];
      if (!Array.isArray(order)) { try { custom = typeof _getTabOrder === 'function' ? _getTabOrder() : []; } catch (_e) { custom = []; } }
      const result = original.call(this, !isMember() && !custom.length ? DEFAULT_ORDER : order);
      schedule();
      return result;
    };
  }

  function shown(el){
    return !el.classList.contains('nav-tab-hidden') && !el.hidden && getComputedStyle(el).display !== 'none';
  }
  function marker(group, first){
    const g = GROUPS.find(x => x.key === group);
    const el = document.createElement('div');
    el.className = 'rail-group';
    el.dataset.group = group;
    if (first) el.dataset.first = '1';
    el.setAttribute('role', 'separator');
    el.setAttribute('aria-label', g[lang()]);
    const label = document.createElement('span');
    label.textContent = g[lang()];
    el.appendChild(label);
    return el;
  }

  let observer = null, queued = false;
  function watch(){
    const rail = document.querySelector('.rail');
    if (!observer || !rail) return;
    observer.observe(rail, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style', 'hidden'] });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-nav-audience', 'lang'] });
  }
  function sync(){
    queued = false;
    const rail = document.querySelector('.rail');
    if (!rail) return;
    if (observer) observer.disconnect();
    rail.querySelectorAll('.rail-group').forEach(n => n.remove());
    const tabs = [];
    for (const el of rail.children) {
      if (el.classList.contains('rail-spacer')) break;
      if (el.classList.contains('nav-tab') && shown(el)) tabs.push([el, groupOf(el.dataset.panel)]);
    }
    const order = [];
    let last = '', contiguous = true;
    for (const [, g] of tabs) {
      if (!g || g === last) continue;
      if (order.includes(g)) { contiguous = false; break; }
      order.push(g); last = g;
    }
    if (contiguous && order.length > 1 && tabs.length >= 6) {
      last = '';
      for (const [el, g] of tabs) {
        if (!g || g === last) continue;
        rail.insertBefore(marker(g, !last), el);
        last = g;
      }
    }
    rail.dataset.grouped = rail.querySelector('.rail-group') ? '1' : '0';
    watch();
  }
  function schedule(){
    if (queued) return;
    queued = true;
    requestAnimationFrame(sync);
  }
  function start(){
    observer = new MutationObserver(schedule);
    if (typeof window._applyTabVisibility === 'function' && typeof _getHiddenTabs === 'function') {
      try { window._applyTabVisibility(_getHiddenTabs()); } catch (_e) { /* the rail keeps its markup order */ }
    }
    schedule();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
  window.SPNavGroups = { sync: schedule, groups: GROUPS.map(g => ({ key: g.key, panels: g.panels.slice() })) };
})();
