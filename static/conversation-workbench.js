/* A second presentation of the sidebar's authorized, filtered session rows.
 * Session loading, lineage, unread state and profile switching stay in sessions.js.
 */
(() => {
  let rows = [], loading = false, query = '', filter = 'all', limit = 36;
  let selectedSection = '', creating = false, optionIndex = null;
  const branchPageSize = 6;
  const branchLimits = new Map();
  const filteredFolds = new Map();
  let foldOwner, savedFolds = new Map();
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
  async function startInSection(sectionId) {
    if (creating || _newSessionInFlight) return;
    creating = true;
    render();
    try {
      const session = await newSession(false, {freshComposer:true});
      const sid = session.session_id;
      const current = () => S.session?.session_id === sid;
      let assignmentFailed = false;
      try {
        await window.SynthPulseSidebarOrg.assignNewChat(sid, sectionId);
      } catch (error) {
        showToast(tx('assign_failed') + ' ' + error.message);
        assignmentFailed = true;
      }
      if (current() && active()) await switchPanel('chat');
      if (current() && _currentPanel === 'chat') {
        _setActiveSessionUrl(sid);
        closeMobileSidebar();
        $('msg')?.focus();
        if (assignmentFailed) await window.SynthPulseSidebarOrg.moveChat(session, sectionId);
      }
    } catch (error) {
      showToast(error.message);
    } finally {
      creating = false;
      render();
    }
  }
  function resetPages() {
    limit = 36;
    branchLimits.clear();
    filteredFolds.clear();
  }
  function foldState(filtered) {
    const owner = window._myIdentityEmail?.() || '';
    if (owner !== foldOwner) {
      foldOwner = owner;
      savedFolds = new Map();
      if (owner) {
        try { savedFolds = new Map(JSON.parse(localStorage.getItem('conversation-board-folds:' + owner) || '[]')); } catch (_) {}
      }
    }
    return filtered ? filteredFolds : savedFolds;
  }
  function saveFolds(folds) {
    if (foldOwner && folds === savedFolds) {
      try { localStorage.setItem('conversation-board-folds:' + foldOwner, JSON.stringify([...folds])); } catch (_) {}
    }
    render();
  }
  function updateSectionPicker(index) {
    const toolbar = $('conversationBoardSections');
    toolbar.hidden = !index?.available || _sessionKindFilter === 'jobs';
    const useSections = !toolbar.hidden && index.sections.length > 0;
    const select = $('conversationBoardSectionPick');
    select.hidden = !useSections;
    if (!useSections || !index.paths.has(selectedSection)) selectedSection = '';
    if (useSections && optionIndex !== index) {
      optionIndex = index;
      select.replaceChildren(new Option(tx('all_sections'), ''));
      for (const {s,path} of index.sections) select.add(new Option(path, s.id));
    }
    select.value = selectedSection;
    $('conversationBoardNew').disabled = creating;
    return useSections;
  }
  function mount() {
    const root = $('mainConversations');
    if ($('conversationBoardSearch')) return root;
    const header = document.createElement('header'); header.className = 'conversation-board-head';
    header.innerHTML = '<div><h2>' + esc(tx('title')) + '</h2><p>' + esc(tx('intro')) + '</p></div>';
    const actions = document.createElement('div'); actions.className = 'conversation-board-actions';
    const back = button(tx('back'), async () => {
      const previous = history.state?.returnSession;
      if (!(await switchPanel('chat'))) return;
      if (S.session?.session_id) _setActiveSessionUrl(S.session.session_id);
      else if (previous) await loadSession(previous);
      else history.pushState(null, '', new URL('?view=chat', document.baseURI));
      $('msg')?.focus();
    });
    back.id = 'conversationBoardBack';
    const create = button(t('new_conversation'), () => selectedSection ? startInSection(selectedSection) : $('btnNewChat').click());
    create.id = 'conversationBoardNew'; create.classList.add('workspace-primary'); actions.append(back, create); header.appendChild(actions);
    const toolbar = document.createElement('div'); toolbar.className = 'conversation-board-toolbar';
    const search = document.createElement('input'); search.id = 'conversationBoardSearch'; search.type = 'search';
    search.placeholder = tx('search'); search.setAttribute('aria-label', tx('search'));
    search.oninput = () => { query = search.value; resetPages(); render(); };
    const filters = document.createElement('div'); filters.className = 'conversation-board-filters';
    filters.setAttribute('aria-label', tx('filter'));
    for (const key of ['all', 'attention', 'running', 'unread', 'pinned']) {
      const node = button(tx(key), () => { filter = key; resetPages(); render(); });
      node.dataset.conversationFilter = key; filters.appendChild(node);
    }
    const sections = document.createElement('div'); sections.id = 'conversationBoardSections';
    const select = document.createElement('select'); select.id = 'conversationBoardSectionPick';
    select.setAttribute('aria-label',tx('sections'));
    select.onchange = () => { selectedSection = select.value; resetPages(); render(); };
    const foldAll = button('', null); foldAll.id = 'conversationBoardFoldAll'; foldAll.hidden = true;
    sections.append(select, foldAll, button(t('sidebar_org_new'), () => window.SynthPulseSidebarOrg.newSection()));
    toolbar.append(search, filters);
    const notice = document.createElement('div'); notice.id = 'conversationBoardNotice'; notice.className = 'conversation-board-notice'; notice.setAttribute('role', 'status');
    const count = document.createElement('p'); count.id = 'conversationBoardCount'; count.className = 'conversation-board-count'; count.setAttribute('role', 'status');
    const grid = document.createElement('div'); grid.id = 'conversationBoardGrid'; grid.className = 'conversation-board-grid';
    const more = button(tx('more'), () => { limit += 36; render(); }); more.id = 'conversationBoardMore';
    const reset = button(tx('reset'), () => { search.value = query = ''; filter = 'all'; selectedSection = ''; resetPages(); render(); search.focus(); }); reset.id = 'conversationBoardReset';
    root.replaceChildren(header, toolbar, sections, notice, count, grid, reset, more); return root;
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
    const org = window.SynthPulseSidebarOrg;
    const index = org?.boardSections();
    const candidates = (loading ? [] : rows).map(s => ({s, flags:state(s), section:org?.sectionFor(s)})).filter(({s,section}) => {
      const text = [title(s), bot(s), project(s), index?.paths.get(section)].join(' ').toLocaleLowerCase();
      return words.every(word => text.includes(word));
    });
    const accepts = ({s, flags}, key) => key === 'all' || (key === 'pinned' ? s.pinned : flags[key]);
    const useSections = updateSectionPicker(index);
    const searched = selectedSection ? candidates.filter(row => row.section === selectedSection) : candidates;
    root.querySelectorAll('[data-conversation-filter]').forEach(node => {
      const key = node.dataset.conversationFilter;
      node.innerHTML = esc(tx(key)) + '<span class="conversation-filter-count">' + searched.filter(row => accepts(row, key)).length + '</span>';
    });
    const group = flags => flags.attention ? 'attention' : flags.running ? 'running' : flags.unread ? 'unread' : 'recent';
    const order = ['attention', 'running', 'unread', 'recent'];
    const matches = searched.filter(row => accepts(row, filter)).sort((a, b) => order.indexOf(group(a.flags)) - order.indexOf(group(b.flags)));
    const grid = $('conversationBoardGrid');
    const focused = grid.contains(document.activeElement) ? document.activeElement : null;
    grid.replaceChildren();
    grid.classList.toggle('conversation-tree', useSections);
    $('conversationBoardFoldAll').hidden = true;
    const shownCount = Math.min(matches.length, limit);
    if (useSections) {
      renderBranches(grid, index, matches, words.length > 0 || filter !== 'all');
      $('conversationBoardCount').textContent = loading ? '' : tx('matches').replace('{total}', matches.length);
    } else {
      const shown = matches.slice(0, limit);
      for (const key of order) {
        const items = shown.filter(row => group(row.flags) === key);
        if (!items.length) continue;
        const section = document.createElement('section'); section.className = 'conversation-group'; section.dataset.conversationGroup = key;
        section.innerHTML = '<h3>' + esc(tx(key)) + '<span>' + items.length + '</span></h3><div class="conversation-group-grid"></div>';
        for (const row of items) section.lastElementChild.appendChild(conversationCard(row));
        grid.appendChild(section);
      }
      $('conversationBoardCount').textContent = loading ? '' : tx('count').replace('{shown}', shownCount).replace('{total}', matches.length);
    }
    $('conversationBoardMore').hidden = useSections || loading || shownCount === matches.length;
    $('conversationBoardReset').hidden = loading || !!matches.length || (!query && filter === 'all' && !selectedSection);
    if (!matches.length && !loading && !grid.childElementCount) {
      const empty = document.createElement('p'); empty.className = 'conversation-board-empty';
      empty.textContent = tx(query || filter !== 'all' ? 'no_results' : 'empty'); grid.appendChild(empty);
    }
    if (focused) {
      const attr = ['data-conversation-open','data-section-toggle','data-create-in-section','data-more-in-section'].find(key => focused.hasAttribute(key));
      const id = attr && CSS.escape(focused.getAttribute(attr));
      const target = attr && grid.querySelector('[' + attr + '="' + id + '"]');
      (target || (attr === 'data-more-in-section' && grid.querySelector('[data-section-toggle="' + id + '"]')) || $('conversationBoardSearch')).focus({preventScroll:true});
    }
  }
  function conversationCard({s, flags}) {
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
    return card;
  }
  function renderBranches(grid, index, matches, filtered) {
    const entries = selectedSection ? index.sections.filter(({s}) => s.id === selectedSection) : index.sections;
    const buckets = new Map();
    for (const row of matches) {
      const key = index.paths.has(row.section) ? row.section : 'unassigned';
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(row);
    }
    // Sections arrive in parent-first sidebar order. Count descendants before
    // rendering so a collapsed parent still reports matches in its children.
    const totals = new Map(Array.from(buckets, ([id,items]) => [id,items.length]));
    const parents = new Set();
    for (let i = entries.length - 1; i >= 0 && !selectedSection; i--) {
      const {id,parent} = entries[i].s;
      if (parent) {
        parents.add(parent);
        totals.set(parent,(totals.get(parent) || 0) + (totals.get(id) || 0));
      }
    }
    const branches = entries.slice();
    if (buckets.has('unassigned')) branches.push({s:{id:'unassigned',name:tx('unassigned')},path:'',depth:0});
    const folds = foldState(filtered || !!selectedSection);
    const bodies = new Map();
    const matchingIds = [];
    let anyOpen = false;
    for (const {s,depth,path} of branches) {
      const {id,parent} = s;
      const total = totals.get(id) || 0;
      if (filtered && !total && !selectedSection) continue;
      matchingIds.push(id);
      const nested = parent && !selectedSection;
      const host = nested ? bodies.get(parent) : grid;
      if (!host) continue;
      const open = folds.get(id) ?? (filtered || !!selectedSection || total > 0 || parents.has(id));
      anyOpen ||= open;
      const branch = document.createElement('section'); branch.className = 'conversation-branch';
      branch.dataset.sectionBranch = id;
      if (nested && depth <= 3) branch.classList.add('conversation-branch-inset');
      const header = document.createElement('div'); header.className = 'conversation-branch-head';
      const heading = document.createElement('h' + Math.min(6,3 + (nested ? depth : 0)));
      const toggle = button('', () => { folds.set(id,!open); saveFolds(folds); });
      toggle.dataset.sectionToggle = id;
      toggle.setAttribute('aria-expanded',String(open));
      toggle.setAttribute('aria-controls','conversation-branch-' + id);
      toggle.innerHTML = '<span class="conversation-chevron" aria-hidden="true"></span><span>' + esc(selectedSection ? path : s.name) + '</span><span class="conversation-branch-count">' + total + '</span>';
      heading.appendChild(toggle); header.appendChild(heading);
      if (id !== 'unassigned') {
        const create = button(t('new_conversation'), () => startInSection(id));
        create.dataset.createInSection = id; create.disabled = creating;
        const label = tx('create_in').replace('{name}', () => path);
        create.title = label; create.setAttribute('aria-label',label);
        header.appendChild(create);
      }
      const body = document.createElement('div'); body.id = 'conversation-branch-' + id; body.hidden = !open;
      branch.append(header,body); host.appendChild(branch);
      if (!open) continue;
      bodies.set(id,body);
      const items = buckets.get(id) || [];
      const pageSize = branchLimits.get(id) || branchPageSize;
      if (items.length) {
        const cards = document.createElement('div'); cards.className = 'conversation-group-grid';
        for (const row of items.slice(0,pageSize)) cards.appendChild(conversationCard(row));
        body.appendChild(cards);
        if (items.length > pageSize) {
          const more = button(tx('more_in_section').replace('{count}',items.length-pageSize), () => {
            branchLimits.set(id,pageSize + branchPageSize); render();
          });
          more.dataset.moreInSection = id; body.appendChild(more);
        }
      } else if (!parents.has(id)) {
        const empty = document.createElement('p'); empty.className = 'conversation-branch-empty';
        empty.textContent = tx('section_empty'); body.appendChild(empty);
      }
    }
    const foldAll = $('conversationBoardFoldAll');
    foldAll.hidden = selectedSection !== '' || matchingIds.length < 2;
    foldAll.textContent = t(anyOpen ? 'collapse_all' : 'expand_all');
    foldAll.onclick = () => {
      for (const id of matchingIds) folds.set(id,!anyOpen);
      saveFolds(folds);
    };
  }
  function show() { return switchPanel('conversations'); }
  function enter() {
    if (window.HermesPWA.workspaceView() !== 'conversations') {
      history.pushState({returnSession:S.session?.session_id}, '', new URL('?view=conversations', document.baseURI));
    }
    closeMobileSidebar();
    if (_sessionListSkeletonActive) setLoading();
    else { renderSessionListFromCache(); render(); }
    if (!matchMedia('(pointer: coarse)').matches) $('conversationBoardSearch')?.focus({preventScroll:true});
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
  window.ConversationWorkbench = {show, enter, sync, setLoading, recent, openRecent};
})();
