/* A second presentation of the sidebar's authorized, filtered session rows.
 * Session loading, lineage, unread state and profile switching stay in sessions.js.
 */
(() => {
  let rows = [], loading = false, query = '', filter = 'all', limit = 36;
  const tx = key => t('conversation_board_' + key);
  const active = () => document.querySelector('main.main')?.classList.contains('showing-conversations');
  function button(label, action) {
    const node = document.createElement('button'); node.type = 'button'; node.className = 'sm-btn';
    node.textContent = label; node.onclick = action; return node;
  }
  function state(s) {
    const selected = _sessionLineageContainsSession(s, _activeSessionIdForSidebar());
    return {
      selected,
      running: _isSessionEffectivelyStreaming(s) || !!s._child_session_streaming,
      unread: !selected && (_hasUnreadForSession(s) || !!s._child_session_has_unread),
      attention: _sessionAttentionState(s) || _sessionAttentionState({_child:true, attention:s._child_session_attention}),
    };
  }
  function title(s) {
    const value = _sessionDisplayTitle(s);
    return value.startsWith('[SYSTEM:') ? t('untitled') : value || t('untitled');
  }
  function bot(s) {
    const name = _sidebarSessionProfileName(s) || S.activeProfile || 'default';
    const profile = _profilesCache?.profiles?.find(p => p.name === name);
    return profile ? botDisplayName(profile) : name;
  }
  function project(s) { return _allProjects.find(p => p.project_id === s.project_id)?.name || ''; }
  function mount() {
    const root = $('mainConversations');
    if (root.childElementCount) return root;
    const header = document.createElement('header'); header.className = 'conversation-board-head';
    header.innerHTML = '<div><h2>' + esc(tx('title')) + '</h2><p>' + esc(tx('intro')) + '</p></div>';
    const actions = document.createElement('div'); actions.className = 'conversation-board-actions';
    const back = button(tx('back'), async () => { await switchPanel('chat'); $('msg')?.focus(); });
    back.id = 'conversationBoardBack';
    const create = button(t('new_conversation'), () => $('btnNewChat').click());
    create.id = 'conversationBoardNew'; create.classList.add('workspace-primary'); actions.append(back, create); header.appendChild(actions);
    const toolbar = document.createElement('div'); toolbar.className = 'conversation-board-toolbar';
    const search = document.createElement('input'); search.id = 'conversationBoardSearch'; search.type = 'search';
    search.placeholder = tx('search'); search.setAttribute('aria-label', tx('search'));
    search.oninput = () => { query = search.value; limit = 36; render(); };
    const filters = document.createElement('div'); filters.className = 'conversation-board-filters';
    filters.setAttribute('aria-label', tx('filter'));
    for (const key of ['all', 'attention', 'running', 'unread', 'pinned']) {
      const node = button(tx(key), () => { filter = key; limit = 36; render(); });
      node.dataset.conversationFilter = key; filters.appendChild(node);
    }
    toolbar.append(search, filters);
    const notice = document.createElement('div'); notice.id = 'conversationBoardNotice'; notice.className = 'conversation-board-notice'; notice.setAttribute('role', 'status');
    const count = document.createElement('p'); count.id = 'conversationBoardCount'; count.className = 'conversation-board-count'; count.setAttribute('role', 'status');
    const grid = document.createElement('div'); grid.id = 'conversationBoardGrid'; grid.className = 'conversation-board-grid';
    const more = button(tx('more'), () => { limit += 36; render(); }); more.id = 'conversationBoardMore';
    const reset = button(tx('reset'), () => { search.value = query = ''; filter = 'all'; limit = 36; render(); search.focus(); }); reset.id = 'conversationBoardReset';
    root.append(header, toolbar, notice, count, grid, reset, more); return root;
  }
  function render() {
    if (!active()) return;
    const root = mount();
    root.setAttribute('aria-busy', String(loading));
    root.querySelectorAll('[data-conversation-filter]').forEach(node => node.setAttribute('aria-pressed', String(filter === node.dataset.conversationFilter)));
    const notice = $('conversationBoardNotice'); notice.replaceChildren();
    if (loading) notice.textContent = t('loading');
    else if (_sessionListLoadError) {
      const message = document.createElement('span'); message.textContent = tx('stale');
      const retry = button(tx('retry'), async () => {
        retry.disabled = true;
        try { await renderSessionList({deferWhileInteracting:false}); }
        finally { retry.disabled = false; }
      }); notice.append(message, retry);
    }
    notice.hidden = !loading && !_sessionListLoadError;
    const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    const searched = (loading ? [] : rows).map(s => ({s, flags:state(s)})).filter(({s}) => {
      const text = [title(s), bot(s), project(s)].join(' ').toLocaleLowerCase();
      return words.every(word => text.includes(word));
    });
    const accepts = ({s, flags}, key) => key === 'all' || (key === 'pinned' ? s.pinned : flags[key]);
    root.querySelectorAll('[data-conversation-filter]').forEach(node => {
      const key = node.dataset.conversationFilter;
      node.innerHTML = esc(tx(key)) + '<span class="conversation-filter-count">' + searched.filter(row => accepts(row, key)).length + '</span>';
    });
    const group = flags => flags.attention ? 'attention' : flags.running ? 'running' : flags.unread ? 'unread' : 'recent';
    const order = ['attention', 'running', 'unread', 'recent'];
    const matches = searched.filter(row => accepts(row, filter)).sort((a, b) => order.indexOf(group(a.flags)) - order.indexOf(group(b.flags)));
    const shown = matches.slice(0, limit);
    $('conversationBoardCount').textContent = loading ? '' : tx('count').replace('{shown}', String(shown.length)).replace('{total}', String(matches.length));
    $('conversationBoardMore').hidden = loading || shown.length === matches.length;
    $('conversationBoardReset').hidden = loading || !!shown.length || (!query && filter === 'all');
    const grid = $('conversationBoardGrid');
    const focused = grid.contains(document.activeElement) ? document.activeElement.dataset.conversationOpen : null;
    grid.replaceChildren();
    const groups = new Map();
    for (const key of order) {
      const count = shown.filter(row => group(row.flags) === key).length;
      if (!count) continue;
      const section = document.createElement('section'); section.className = 'conversation-group'; section.dataset.conversationGroup = key;
      section.innerHTML = '<h3>' + esc(tx(key)) + '<span>' + count + '</span></h3><div class="conversation-group-grid"></div>';
      groups.set(key, section.lastElementChild); grid.appendChild(section);
    }
    for (const {s, flags} of shown) {
      const card = button('', async () => {
        card.disabled = true;
        try { await _openSidebarSession(s); }
        catch (error) { showToast(error.message); }
        finally { card.disabled = false; }
      });
      card.className = 'conversation-board-card'; card.dataset.conversationOpen = s.session_id;
      if (flags.selected) card.setAttribute('aria-current', 'true');
      const tags = [];
      for (const key of ['attention', 'running', 'unread']) {
        if (flags[key]) tags.push('<span class="conversation-state" data-state="' + key + '">' + esc(key === 'attention' ? flags.attention.title || tx(key) : tx(key)) + '</span>');
      }
      if (s.pinned) tags.push('<span class="conversation-state">' + esc(tx('pinned')) + '</span>');
      if (_isReadOnlySession(s)) tags.push('<span class="conversation-state">' + esc(tx('readonly')) + '</span>');
      if (flags.selected) tags.push('<span class="conversation-state">' + esc(tx('selected')) + '</span>');
      const source = _getChannelLabel(s);
      const stamp = _sessionTimestampMs(s);
      const profileName = _sidebarSessionProfileName(s) || S.activeProfile || 'default';
      const profile = _profilesCache?.profiles?.find(p => p.name === profileName) || {name:profileName};
      card.innerHTML = '<span class="conversation-card-bot">' + botAvatarHtml(profile) + '<span>' + esc(bot(s)) + '</span></span><strong>' + esc(title(s)) + '</strong><span class="conversation-card-context">' + esc([project(s), source].filter(Boolean).join(' · ') || tx('no_project')) + '</span><span class="conversation-card-signals">' + tags.join('') + '</span><span class="conversation-card-time">' + esc(stamp ? _formatRelativeSessionTime(stamp) : '') + '</span>';
      groups.get(group(flags)).appendChild(card);
    }
    if (!shown.length && !loading) {
      const empty = document.createElement('p'); empty.className = 'conversation-board-empty';
      empty.textContent = tx(query || filter !== 'all' ? 'no_results' : 'empty'); grid.appendChild(empty);
    }
    if (focused) (grid.querySelector('[data-conversation-open="' + CSS.escape(focused) + '"]') || $('conversationBoardSearch')).focus({preventScroll:true});
  }
  async function show() {
    if (!(await switchPanel('conversations'))) return;
    closeMobileSidebar();
    if (_sessionListSkeletonActive) setLoading();
    else { renderSessionListFromCache(); render(); }
    $('conversationBoardSearch')?.focus({preventScroll:true});
  }
  function recent() {
    return rows.slice().sort((a,b) => _sessionTimestampMs(b) - _sessionTimestampMs(a)).slice(0,4).map(s =>
      ({id:s.session_id, title:title(s), bot:bot(s), at:_sessionTimestampMs(s)}));
  }
  function openRecent(id) {
    const row = rows.find(s => s.session_id === id);
    if (row) return _openSidebarSession(row);
  }
  function sync(sessions) { rows = sessions; loading = false; render(); window.SPWorkplace?.refreshRecent(); }
  function setLoading() { rows = []; loading = true; render(); window.SPWorkplace?.refreshRecent(); }
  window.ConversationWorkbench = {show, sync, setLoading, recent, openRecent};
})();
