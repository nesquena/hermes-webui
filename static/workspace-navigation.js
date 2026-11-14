/* Search the existing permitted destinations. This adds no permissions or data reads. */
(() => {
  const tx = key => t('workspace_navigation_' + key);
  let dialog, search, list, status, matches = [], selected = 0, returnFocus;
  function destinations() {
    const me = window.__GOV_ME__;
    if (!Array.isArray(me?.hidden_nav)) return [];
    const hidden = new Set([...me.hidden_nav, ...(window._govHiddenNav || []), ...(window._moduleHiddenNav || [])]);
    const entries = [];
    document.querySelectorAll('.rail .nav-tab[data-panel]').forEach(node => {
      const panel = node.dataset.panel;
      if (node.hidden || node.disabled || node.classList.contains('nav-tab-hidden') || hidden.has(panel)) return;
      if (window.SynthPulseModules && !window.SynthPulseModules.canOpen(panel)) return;
      // workspace-secondary-hidden is a personal disclosure, not a permission denial.
      const label = node.dataset.tooltip || node.getAttribute('aria-label') || panel;
      entries.push({panel:panel === 'chat' ? 'conversations' : panel, label,
        aliases:panel === 'chat' ? 'chat ' + t('conversation_board_title') : panel === 'projects' ? tx('project_aliases') : '',
        icon:node.querySelector('svg')});
    });
    return entries;
  }
  function highlight() {
    [...list.children].forEach((node, index) => node.setAttribute('aria-selected', String(index === selected)));
    if (matches.length) {
      search.setAttribute('aria-activedescendant', 'workspaceDestination-' + selected);
      list.children[selected]?.scrollIntoView({block:'nearest'});
    } else search.removeAttribute('aria-activedescendant');
  }
  function render() {
    const previous = matches[selected]?.panel;
    const words = search.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    matches = destinations().filter(item => words.every(word => (item.label + ' ' + item.panel + ' ' + (item.aliases || '')).toLocaleLowerCase().includes(word)));
    selected = Math.max(0, matches.findIndex(item => item.panel === previous));
    list.replaceChildren();
    for (const [index, item] of matches.entries()) {
      const option = document.createElement('div'); option.id = 'workspaceDestination-' + index;
      option.setAttribute('role', 'option'); option.dataset.destination = item.panel;
      if (item.icon) { const icon = item.icon.cloneNode(true); icon.setAttribute('aria-hidden', 'true'); option.appendChild(icon); }
      const label = document.createElement('span'); label.textContent = item.label; option.appendChild(label);
      const arrow = document.createElement('span'); arrow.className = 'workspace-destination-arrow'; arrow.textContent = '↵'; arrow.setAttribute('aria-hidden','true'); option.appendChild(arrow);
      option.onpointerdown = event => event.preventDefault();
      option.onclick = () => activate(item.panel);
      list.appendChild(option);
    }
    status.textContent = matches.length ? t('workspace_navigation_count', matches.length) : tx('empty');
    highlight();
  }
  async function activate(panel) {
    if (!destinations().some(item => item.panel === panel)) { render(); return; }
    if (dialog?.open) dialog.close();
    try {
      if (panel === 'conversations') { await ConversationWorkbench.show(); return; }
      if (!(await switchPanel(panel))) return;
      if (innerWidth <= 640 && panel !== 'chat' && !MAIN_VIEW_PANELS.includes(panel)) {
        document.querySelector('.sidebar')?.classList.add('mobile-panel-drawer', 'mobile-open');
      } else closeMobileSidebar();
      if (panel === 'profiles') $('botDirectorySearch')?.focus();
      else {
        const heading = [...document.querySelectorAll('main h2, .panel-view.active .panel-head')].find(node => node.getClientRects().length);
        if (heading) { heading.tabIndex = -1; heading.focus({preventScroll:true}); }
      }
      window.SynthPulseWorkspace?.sync();
    } catch (error) { showToast(error.message); }
  }
  function mount() {
    if (dialog) return;
    dialog = document.createElement('dialog'); dialog.id = 'workspaceNavigator'; dialog.setAttribute('aria-labelledby','workspaceNavigatorTitle');
    dialog.innerHTML = '<header><h2 id="workspaceNavigatorTitle">' + esc(tx('title')) + '</h2><button type="button" class="workspace-navigator-close" aria-label="' + esc(tx('close')) + '">Esc</button></header><input id="workspaceNavigatorSearch" type="search" role="combobox" aria-autocomplete="list" aria-expanded="true" aria-controls="workspaceDestinationList" autocomplete="off"><div id="workspaceDestinationList" role="listbox"></div><p id="workspaceNavigatorStatus" role="status"></p><footer>' + esc(tx('hint')) + '</footer>';
    document.body.appendChild(dialog);
    search = $('workspaceNavigatorSearch'); list = $('workspaceDestinationList'); status = $('workspaceNavigatorStatus');
    search.placeholder = tx('search'); search.setAttribute('aria-label', tx('search'));
    list.setAttribute('aria-label', tx('title'));
    search.oninput = () => { selected = 0; matches = []; render(); };
    dialog.querySelector('button').onclick = () => dialog.close();
    dialog.addEventListener('close', () => { if (returnFocus?.isConnected && document.activeElement === document.body) returnFocus.focus({preventScroll:true}); });
    dialog.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); dialog.close(); return; }
      if (event.target !== search || event.isComposing) return;
      if (['ArrowDown', 'ArrowUp', 'Enter'].includes(event.key)) { event.preventDefault(); event.stopPropagation(); }
      if (event.key === 'Enter') { if (matches[selected]) activate(matches[selected].panel); }
      else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        selected = matches.length ? (selected + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length : 0;
        highlight();
      }
    });
    new MutationObserver(() => { if (dialog.open) render(); }).observe(document.querySelector('.rail'), {subtree:true, attributes:true, attributeFilter:['class','hidden','disabled','data-tooltip']});
  }
  function show() {
    if (document.querySelector('dialog[open]')) return;
    mount();
    $('workspaceNavigatorTitle').textContent = tx('title');
    dialog.querySelector('button').setAttribute('aria-label', tx('close'));
    dialog.querySelector('footer').textContent = tx('hint');
    search.placeholder = tx('search'); search.setAttribute('aria-label', tx('search')); list.setAttribute('aria-label', tx('title'));
    returnFocus = document.activeElement; search.value = ''; matches = []; selected = 0; render(); dialog.showModal(); search.focus();
  }
  window.WorkspaceDestinations = {destinations, open:activate};
  function start() {
    $('btnWorkspaceNavigator').onclick = show;
    document.addEventListener('keydown', event => {
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.code === 'KeyP' && !event.altKey && !event.repeat) {
        event.preventDefault(); event.stopPropagation(); show();
      }
    }, true);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
