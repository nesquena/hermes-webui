/* Chat recipients use the existing actor-scoped catalog and group dispatch. */
(function () {
  'use strict';
  let key = '', generation = 0, profiles = [], people = [], loading = true, peopleLoading = true, catalogRetry = false;
  let menuRows = [], menuIndex = 0, menuRange = null, preparing = false, retry = null;
  function tx(key, fallback, ...args) {
    const value = typeof t === 'function' ? t(key, ...args) : key;
    if (value && value !== key) return value;
    return String(fallback).replace(/\{(\d+)\}/g, (match, index) => (index in args ? String(args[index]) : match));
  }
  function actor() { return String((window.__GOV_ME__ || {}).email || '').toLowerCase(); }
  function contextKey() {
    const s = S.session || {};
    return JSON.stringify([actor(), s.session_id || '', S.activeProfile || '', s.bot_participants || [], s.participants || []]);
  }
  function preferenceKey() { return actor() ? 'synpulse:bot-roster-collapsed:' + actor() : null; }
  function collapsed() {
    try {
      const stored = preferenceKey() ? localStorage.getItem(preferenceKey()) : null;
      if (stored === '1') return true;
      if (stored === '0') return false;
      // On a phone the row starts folded: the titlebar already switches bots.
      return !!(window.matchMedia && window.matchMedia('(max-width:640px)').matches);
    } catch (_) { return false; }
  }
  function available(rows, session) {
    const bots = session && Array.isArray(session.bot_participants) ? session.bot_participants : [];
    return (rows || []).filter(p => p && typeof p.name === 'string' && p.visible !== false &&
      (!bots.length || bots.includes(p.name)));
  }
  function address(draft, name) {
    return '@' + name + ' ' + String(draft || '').replace(/^\s*@[A-Za-z0-9][A-Za-z0-9_-]{0,63}(?=\s|$)\s*/, '');
  }
  function fold(value) { return String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase(); }

  // ── Group routing (plan 5.2): handles, route reasons, Working and default ──
  // The internal-name denylist of plan E.4.3, equal to api/modules.py
  // DISPLAY_DENYLIST (tests/test_group_routing.py keeps them in step). A
  // handle is a bot's title slug with these names cut out, exactly as
  // api/group_chat.handle_from_text computes it on the server.
  const HANDLE_DENYLIST = [
    'n8n', 'OmniRoute', 'LangGraph', 'Hermes', 'LiteLLM', 'RAGFlow', 'Langfuse',
    'OpenWebUI', 'OpenDesign', 'OpenNotebook', 'DocuSeal', 'OpenWhispr', 'OpenWispr',
    'Fireflies', 'Meetily', 'Vexa', 'OpenBot', 'Nango', 'SurrealDB', 'Elasticsearch',
    'MinIO', 'Valkey', 'Mnemosyne', 'mnemo-hub', 'GenOffice', 'Genspark',
  ];
  const ROUTE_REASONS = ['mention', 'only_bot', 'sticky', 'default'];
  const BOT_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
  const LEAD_MENTION = /^\s*@([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?=\s|$)/;
  function slug(text) {
    return String(text || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase()
      .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  }
  const DENIED_PARTS = HANDLE_DENYLIST.map(slug).filter(Boolean);
  function handleFromText(text) {
    let handle = slug(text), changed = true;
    while (changed) {
      changed = false;
      for (const part of DENIED_PARTS) {
        if (handle.includes(part)) { handle = handle.split(part).join('-'); changed = true; }
      }
      handle = handle.replace(/-+/g, '-').replace(/^-+|-+$/g, '');
    }
    return handle.slice(0, 64).replace(/^-+|-+$/g, '');
  }
  function botHandle(id, title) { return handleFromText(title) || handleFromText(id) || 'bot'; }
  function handleFor(p) { return botHandle(p && p.name, p && p.bot && p.bot.title); }
  function profileFor(id) { return profiles.find(p => p && p.name === id) || null; }
  // Display identity for teamwork lines and cards (static/tool-friendly.js):
  // the viewer's own catalog only, never another person's bot list.
  function botInfo(id) {
    const p = profileFor(id);
    if (!p) return null;
    const title = p.bot && typeof p.bot.title === 'string' ? p.bot.title.trim() : '';
    return {known:true, title, avatar_url:typeof p.bot_avatar_url === 'string' ? p.bot_avatar_url : ''};
  }
  function handleForBot(id, title) { const p = profileFor(id); return p ? handleFor(p) : botHandle(id, title); }
  // The text that addresses a bot: its handle when no other bot here shares it.
  function addressName(p, rows) {
    const handle = handleFor(p);
    return (rows || []).filter(row => row && handleFor(row) === handle).length > 1 ? p.name : handle;
  }
  function reasonText(reason, handle) {
    if (reason === 'mention') return tx('group_route_reason_mention', 'Asked by name');
    if (reason === 'only_bot') return tx('group_route_reason_only_bot', 'Only bot in this chat');
    if (reason === 'sticky') return tx('group_route_reason_sticky', 'Continued with {0} (last to answer)', '@' + handle);
    if (reason === 'default') return tx('group_route_reason_default', 'Default bot of this chat');
    return '';
  }
  function lastBot(messages) {
    const list = Array.isArray(messages) ? messages : [];
    for (let i = list.length - 1; i >= 0; i--) {
      const m = list[i];
      if (m && m.role === 'assistant' && typeof m.bot_profile === 'string' && m.bot_profile.trim()) return m.bot_profile.trim();
    }
    return null;
  }
  // The bot ``@token`` names among ``bots``: its id, or a unique handle.
  function resolveMention(token, bots, handleOf) {
    token = String(token || '');
    if (bots.includes(token)) return token;
    const low = token.toLowerCase();
    const byId = bots.filter(bot => bot.toLowerCase() === low);
    if (byId.length === 1) return byId[0];
    const byHandle = bots.filter(bot => (handleOf || botHandle)(bot) === low);
    return byHandle.length === 1 ? byHandle[0] : null;
  }
  // Which bot answers a draft, mirroring api/group_chat.select_bot_with_reason.
  // ``usable(bot)`` says whether the viewer may use a bot. Returns
  // {bot, reason}, {bot: null, reason: 'last_unavailable'} or null.
  function predictRoute(session, messages, draft, usable, handleOf) {
    const bots = session && Array.isArray(session.bot_participants) ? session.bot_participants : [];
    if (!bots.length) return null;
    const can = typeof usable === 'function' ? usable : () => true;
    const mention = LEAD_MENTION.exec(String(draft || ''));
    if (mention) { const bot = resolveMention(mention[1], bots, handleOf); return bot ? {bot, reason:'mention'} : null; }
    if (bots.length === 1) return can(bots[0]) ? {bot:bots[0], reason:'only_bot'} : null;
    const last = lastBot(messages);
    let skipped = false;
    if (last && bots.includes(last)) { if (can(last)) return {bot:last, reason:'sticky'}; skipped = true; }
    const fallback = session.default_bot;
    if (fallback && fallback !== last && bots.includes(fallback) && can(fallback)) return {bot:fallback, reason:'default'};
    return skipped ? {bot:null, reason:'last_unavailable'} : null;
  }
  function catalogReady() { return !loading && !catalogRetry && profiles.length > 0; }
  function usableByViewer(bot) { return !catalogReady() || profiles.some(p => p && p.name === bot); }
  function currentRoute() {
    const input = document.getElementById('msg');
    return predictRoute(S.session, S.messages, input ? input.value : '', usableByViewer, id => handleForBot(id));
  }
  function canManageDefault(session) {
    if (!session || !session.session_id) return false;
    if (typeof _groupPeopleCanManage === 'function') { try { return !!_groupPeopleCanManage(); } catch (_) { return false; } }
    const owner = String(session.owner_email || '').toLowerCase();
    return !!owner && owner === actor();
  }

  // The live route of the newest turn and the Working state, per session.
  const liveRoutes = {}, working = {};
  let workingTimer = null, decoratePending = false, observed = null, defaultSaving = false;
  function emitWorking(entry, isWorking) {
    try {
      window.dispatchEvent(new CustomEvent('synpulse:group-working', {detail:{
        session_id:entry.sessionId, stream_id:entry.streamId, bot:entry.bot, reason:entry.reason, working:isWorking}}));
    } catch (_) {}
  }
  function stillWorking(entry) {
    const s = S.session || {};
    return s.session_id === entry.sessionId && !!entry.streamId && S.activeStreamId === entry.streamId;
  }
  function checkWorking() {
    let changed = false;
    for (const sid of Object.keys(working)) {
      if (!stillWorking(working[sid])) { const entry = working[sid]; delete working[sid]; emitWorking(entry, false); changed = true; }
    }
    if (!Object.keys(working).length && workingTimer) { clearInterval(workingTimer); workingTimer = null; }
    if (changed) paint();
  }
  function onTurnBot(sessionId, streamId, data) {
    if (!data || typeof data.bot !== 'string' || !BOT_ID.test(data.bot)) return;
    const sid = String(sessionId || data.session_id || '');
    if (!sid) return;
    const handle = typeof data.handle === 'string' && BOT_ID.test(data.handle) ? data.handle.toLowerCase() : handleForBot(data.bot, data.name);
    const entry = {sessionId:sid, streamId:String(streamId || data.stream_id || ''), bot:data.bot,
      reason:ROUTE_REASONS.includes(data.reason) ? data.reason : null, handle, since:Date.now()};
    liveRoutes[sid] = entry;
    const before = working[sid];
    if (stillWorking(entry)) {
      working[sid] = entry;
      if (!before || before.streamId !== entry.streamId || before.bot !== entry.bot) emitWorking(entry, true);
      if (!workingTimer) workingTimer = setInterval(checkWorking, 600);
    }
    paint(); scheduleDecorate();
  }
  function workingState(sessionId) {
    const entry = working[String(sessionId || '')];
    return entry && stillWorking(entry) ? {bot:entry.bot, stream_id:entry.streamId, reason:entry.reason, since:entry.since} : null;
  }

  // The reason chip on a reply: persisted ``route_reason`` on the message,
  // or the live route of the newest turn.
  function messageForTurn(turn) {
    if (!turn || turn.id === 'liveAssistantTurn') return null;
    const index = Number(turn.dataset && turn.dataset.recycleKey);
    return Number.isInteger(index) && Array.isArray(S.messages) ? S.messages[index] || null : null;
  }
  function followedByUser(turn) {
    for (let node = turn.nextElementSibling; node; node = node.nextElementSibling) {
      if (node.matches && node.matches('.msg-row[data-role="user"]')) return true;
    }
    return false;
  }
  function setReplyChip(turn, info) {
    const role = turn.querySelector('.msg-role.assistant');
    if (!role) return;
    let chip = role.querySelector('.group-route-chip');
    const text = info ? reasonText(info.reason, info.handle) : '';
    if (!text) { if (chip) chip.remove(); return; }
    if (!chip) { chip = document.createElement('span'); chip.className = 'group-route-chip'; role.appendChild(chip); }
    if (chip.textContent !== text) chip.textContent = text;
    chip.dataset.routeReason = info.reason;
  }
  function decorateReplies() {
    const inner = document.getElementById('msgInner');
    if (!inner) return;
    const session = S.session || {}, sid = session.session_id || '';
    // Private chats carry no route reasons; skip the walk unless a chip is left over.
    if (!(session.bot_participants || []).length && !liveRoutes[sid] && !inner.querySelector('.group-route-chip')) return;
    const turns = Array.from(inner.querySelectorAll('.assistant-turn'));
    const live = liveRoutes[sid];
    let target = null;
    if (live && live.reason) {
      const last = turns[turns.length - 1];
      if (last && !followedByUser(last)) {
        const m = messageForTurn(last);
        if (last.id === 'liveAssistantTurn' ? S.activeStreamId === live.streamId : !!(m && m.bot_profile === live.bot)) target = last;
      }
    }
    for (const turn of turns) {
      const m = messageForTurn(turn);
      let info = null;
      if (turn === target) info = {reason:live.reason, handle:live.handle};
      else if (m && typeof m.bot_profile === 'string' && ROUTE_REASONS.includes(m.route_reason)) {
        info = {reason:m.route_reason, handle:handleForBot(m.bot_profile, m.bot_name)};
      }
      setReplyChip(turn, info);
    }
  }
  function scheduleDecorate() {
    if (decoratePending) return;
    decoratePending = true;
    const run = () => { decoratePending = false; observe(); decorateReplies(); };
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(run);
    else if (typeof setTimeout === 'function') setTimeout(run, 0);
    else decoratePending = false;
  }
  function observe() {
    const inner = document.getElementById('msgInner');
    if (!inner || inner === observed || typeof MutationObserver !== 'function') return;
    observed = inner;
    // Turns are added, replaced and recycled as direct children of #msgInner.
    new MutationObserver(scheduleDecorate).observe(inner, {childList:true});
  }

  async function setDefaultBot(name) {
    const session = S.session || {};
    if (!session.session_id || defaultSaving) return false;
    const next = name && name !== session.default_bot ? name : null;
    defaultSaving = true; paint();
    try {
      const result = await api('/api/session/participants', {method:'POST', body:JSON.stringify({
        session_id:session.session_id, participants:session.participants || [],
        bot_participants:session.bot_participants || [], default_bot:next})});
      if (!result || !result.ok) throw new Error((result && result.error) || '');
      if (S.session && S.session.session_id === session.session_id) {
        S.session.default_bot = Object.prototype.hasOwnProperty.call(result, 'default_bot') ? result.default_bot : next;
        if (Array.isArray(result.participants)) S.session.participants = result.participants;
        if (Array.isArray(result.bot_participants)) S.session.bot_participants = result.bot_participants;
      }
      return true;
    } catch (error) {
      if (typeof showToast === 'function') showToast((error && error.message) || tx('group_route_default_failed', 'Could not change the default bot.'));
      return false;
    } finally { defaultSaving = false; paint(); }
  }

  function catalog(includeHidden) {
    const bots = profiles.filter(p => p && typeof p.name === 'string');
    return bots.filter(p => includeHidden || p.visible !== false).map(p => {
      const handle = handleFor(p), shared = bots.filter(other => handleFor(other) === handle).length > 1;
      return {kind:'bot', id:p.name, label:botDisplayName(p), handle:shared ? null : handle,
        description:p.bot && typeof p.bot.description === 'string' ? p.bot.description.trim() : ''};
    }).concat(people.filter(p => p && p.email && p.email.toLowerCase() !== actor())
        .map(p => ({kind:'user', id:p.email, label:p.display_name || p.name || p.email})));
  }
  function mentionMatches(text, rows) {
    const result = [];
    // Whole tokens only: an email address in prose is not an invitation.
    for (const match of String(text || '').matchAll(/(?:^|\s)@([^\s,;!?()[\]{}<>]+)/g)) {
      const token = match[1].replace(/[.:]$/, ''), low = token.toLowerCase();
      const item = rows.find(row => row.id.toLowerCase() === low) || rows.find(row => row.handle && row.handle === low);
      if (item) { const start = match.index + match[0].indexOf('@'); result.push({item, start, end:start + token.length + 1}); }
    }
    return result;
  }
  function mentions(text, rows) {
    const result = [];
    for (const match of mentionMatches(text, rows)) {
      if (!result.some(row => row.kind === match.item.kind && row.id === match.item.id)) result.push(match.item);
    }
    return result;
  }
  function normalizeBotAddress(text, rows) {
    const match = mentionMatches(text, rows).find(row => row.item.kind === 'bot');
    if (!match) return text;
    const name = match.item.handle || match.item.id;
    if (match.start === 0) {
      const remainder = text.slice(match.end);
      return '@' + name + (remainder && !/^\s/.test(remainder) ? ' ' : '') + remainder;
    }
    return '@' + name + ' ' + text.slice(0, match.start) + text.slice(match.end);
  }
  function closeMenu() {
    menuRange = null; menuRows = [];
    const popup = document.getElementById('chatMentionDropdown');
    if (popup) popup.hidden = true;
    const input = document.getElementById('msg');
    if (input && input.getAttribute('aria-controls') === 'chatMentionDropdown') {
      input.removeAttribute('aria-controls'); input.removeAttribute('aria-activedescendant'); input.removeAttribute('aria-expanded');
    }
  }
  function choose(item) {
    const input = document.getElementById('msg');
    if (!input || !menuRange || contextKey() !== key) return closeMenu();
    const {start, end} = menuRange;
    input.setRangeText('@' + (item.kind === 'bot' && item.handle ? item.handle : item.id) + ' ', start, end, 'end');
    closeMenu(); input.dispatchEvent(new Event('input', {bubbles:true})); input.focus();
  }
  function paintMenu() {
    const input = document.getElementById('msg'), host = document.getElementById('composerWrap');
    if (!input || !host || document.activeElement !== input || preparing || loading) return closeMenu();
    const before = input.value.slice(0, input.selectionStart);
    const match = /(?:^|\s)@([^\s@]*)$/.exec(before);
    if (!match) return closeMenu();
    const query = fold(match[1]);
    // Hidden bots stay out of the picker; their @mentions still resolve.
    menuRows = catalog(false).filter(row => fold(row.label + ' ' + row.id + ' ' + (row.handle || '')).includes(query)).slice(0, 12);
    menuRange = {start:before.length - match[1].length - 1, end:input.selectionStart};
    menuIndex = Math.min(menuIndex, Math.max(0, menuRows.length - 1));
    let popup = document.getElementById('chatMentionDropdown');
    if (!popup) { popup = document.createElement('div'); popup.id = 'chatMentionDropdown'; popup.className = 'chat-mention-dropdown'; popup.setAttribute('role','listbox'); popup.setAttribute('aria-label',tx('chat_mentions_picker', 'Mention a person or bot')); host.appendChild(popup); }
    popup.hidden = false; popup.replaceChildren();
    input.setAttribute('aria-controls', popup.id); input.setAttribute('aria-expanded','true');
    menuRows.forEach((row, index) => {
      const button = document.createElement('button'); button.type = 'button'; button.id = 'chatMentionOption-' + index;
      button.dataset.mentionKind = row.kind; button.dataset.mentionId = row.id;
      button.setAttribute('role','option'); button.setAttribute('aria-selected',String(index === menuIndex)); button.tabIndex = -1;
      const label = document.createElement('span'); label.textContent = row.label;
      const detail = document.createElement('small'); detail.textContent = (row.kind === 'bot' ? tx('chat_mentions_bot', 'Bot') : tx('chat_mentions_person', 'Person')) + ' · @' + (row.kind === 'bot' && row.handle ? row.handle : row.id);
      button.append(label, detail);
      if (row.kind === 'bot' && row.description) {
        const description = document.createElement('small'); description.className = 'group-route-bot-description'; description.textContent = row.description;
        button.appendChild(description);
      }
      button.onpointerdown = event => event.preventDefault(); button.onclick = () => choose(row); popup.appendChild(button);
    });
    if (menuRows.length) input.setAttribute('aria-activedescendant','chatMentionOption-' + menuIndex);
    else { input.removeAttribute('aria-activedescendant'); const empty = document.createElement('span'); empty.textContent = tx('chat_mentions_empty', 'No matching people or bots'); popup.appendChild(empty); }
  }
  let sidebarSwitchPending = false, scoutPending = '';
  // Bots whose screen waits for a person (bot -> {reason, help_since}), from
  // /api/bot-screen/status with the viewer's own Bot Screens grants; read with
  // the bot catalog, never on a faster timer.
  let screensWaiting = new Map();
  function screenWaitDot(p) {
    if (!screensWaiting.has(p.name)) return null;
    const dot = document.createElement('span'); dot.className = 'sidebar-bot-screen-wait'; dot.setAttribute('role', 'img');
    const wait = screensWaiting.get(p.name) || {};
    const since = typeof window._botScreenSinceText === 'function' ? window._botScreenSinceText(wait.help_since) : '';
    dot.title = tx('waiting_screen', 'Bot screen waits for you') + (wait.reason ? ': ' + wait.reason : '') + (since ? ' · ' + since : '');
    dot.setAttribute('aria-label', dot.title + ' (' + botDisplayName(p) + ')');
    return dot;
  }
  // Scout (api/bot_scout.py): a proactive look-out per person per bot.
  // Everyone who may chat with a bot switches their own Scout for it here;
  // people's own profiles get none.
  function scoutIcon() {
    const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 16 16'); svg.setAttribute('width', '14'); svg.setAttribute('height', '14'); svg.setAttribute('aria-hidden', 'true');
    for (const d of ['M8 8L12.6 3.4', 'M13.5 8A5.5 5.5 0 1 1 8 2.5', 'M10.5 8A2.5 2.5 0 1 1 8 5.5']) {
      const path = document.createElementNS(ns, 'path'); path.setAttribute('d', d);
      path.setAttribute('fill', 'none'); path.setAttribute('stroke', 'currentColor'); path.setAttribute('stroke-width', '1.4'); path.setAttribute('stroke-linecap', 'round');
      svg.appendChild(path);
    }
    return svg;
  }
  function scoutControl(p) {
    if (p.bot_kind !== 'bot' || !p.scout_me) return null;
    const me = p.scout_me, on = !!me.enabled;
    const toggle = document.createElement('button'); toggle.type = 'button'; toggle.className = 'sidebar-bot-scout' + (on ? ' is-on' : '');
    toggle.dataset.scoutBot = p.name; toggle.setAttribute('aria-pressed', String(on));
    toggle.title = tx('bot_scout_toggle', 'Scout for you on/off'); toggle.setAttribute('aria-label', toggle.title + ': ' + botDisplayName(p));
    toggle.disabled = !!scoutPending; toggle.appendChild(scoutIcon());
    toggle.onclick = async () => {
      if (scoutPending) return;
      // The first time someone turns Scout on for a bot, say what that means;
      // turning it off needs no confirmation.
      if (!on && !me.known && typeof window.showConfirmDialog === 'function') {
        const frequency = tx('bot_scout_frequency_'+(me.frequency||'daily'), me.frequency||'daily');
        const ok = await window.showConfirmDialog({
          title: tx('bot_scout_confirm_title', 'Turn Scout on?'),
          message: tx('bot_scout_confirm_on', 'Scout checks {frequency} and shares useful preparations in your conversations. Change the schedule in this bot’s Scout settings.').replace('{frequency}', frequency),
          confirmLabel: tx('bot_scout_confirm_ok', 'Turn Scout on'),
        });
        if (!ok) return;
      }
      scoutPending = p.name; paintSidebarBots();
      try {
        await api('/api/bots/scout', {method: 'POST', body: JSON.stringify({name: p.name, enabled: !on, revision: p.bot_revision || 0})});
        showToast(on ? tx('bot_scout_saved_off', 'Scout off') : tx('bot_scout_saved_on', 'Scout on'));
        window.dispatchEvent(new CustomEvent('synpulse:bot-updated', {detail: {name: p.name}}));
      } catch (error) { showToast(error.message); }
      finally { scoutPending = ''; window.refreshChatBots(); paintSidebarBots(); }
    };
    return toggle;
  }
  window.mountSidebarBots = function (list) {
    const nav = document.querySelector('.rail .nav-tab[data-panel="profiles"]');
    if (!nav) return;
    const section = document.createElement('details');
    section.id = 'sidebarBots'; section.className = 'sidebar-bots-section'; section.dataset.panel = 'profiles';
    section.classList.toggle('nav-tab-hidden', nav.classList.contains('nav-tab-hidden'));
    const pref = 'synpulse:sidebar-bots-collapsed:' + (actor() || 'local');
    try { section.open = localStorage.getItem(pref) === '0'; } catch (_) {}
    const heading = document.createElement('summary'); heading.className = 'sidebar-projects-heading';
    heading.textContent = tx('tab_profiles', 'Bots');
    const items = document.createElement('div'); items.id = 'sidebarBotItems'; items.className = 'sidebar-bot-items';
    section.append(heading, items);
    section.addEventListener('toggle', () => {
      try { localStorage.setItem(pref, section.open ? '0' : '1'); } catch (_) {}
    });
    list.appendChild(section); paintSidebarBots();
  };
  function paintSidebarBots() {
    const items = document.getElementById('sidebarBotItems'); if (!items) return;
    // The navigation shows the entire authorized catalog, not just the bots
    // participating in the currently open group conversation.
    const rows = profiles.filter(p => p && typeof p.name === 'string' && p.visible !== false);
    const signature = JSON.stringify([actor(), rows, loading, S._bootReady, S.activeProfile, sidebarSwitchPending, scoutPending, [...screensWaiting]]);
    if (items.dataset.state === signature) return;
    items.dataset.state = signature; items.replaceChildren();
    // SynthPulse stays on top; the other bots are grouped by category in
    // collapsible groups whose open state each person keeps (spRenderGrouped).
    const entries = [];
    for (const p of rows) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'sidebar-bot-item';
      button.dataset.sidebarBot = p.name; button.setAttribute('aria-pressed', String(S.activeProfile === p.name));
      button.disabled = loading || !S._bootReady || sidebarSwitchPending;
      button.innerHTML = botAvatarHtml(p);
      const label = document.createElement('span'); label.textContent = botDisplayName(p); button.appendChild(label);
      button.onclick = async () => {
        if (loading || !S._bootReady || sidebarSwitchPending) return;
        sidebarSwitchPending = true; paintSidebarBots();
        try { await switchToProfile(p.name); }
        catch (error) { showToast(error.message); }
        finally { sidebarSwitchPending = false; window.refreshChatBots(); paintSidebarBots(); }
      };
      const row = document.createElement('div'); row.className = 'sidebar-bot-row'; row.appendChild(button);
      const scout = scoutControl(p); if (scout) row.appendChild(scout);
      const wait = screenWaitDot(p); if (wait) row.appendChild(wait);
      if (p.name === 'default' || p.is_default) items.appendChild(row);
      else entries.push({category: (p.bot && p.bot.category) || '', node: row});
    }
    if (typeof window.spRenderGrouped === 'function') window.spRenderGrouped(items, entries, {storagePrefix: 'synpulse:sidebar-bots-group:' + (actor() || 'local')});
    else for (const entry of entries) items.appendChild(entry.node);
    if (!rows.length) {
      const state = document.createElement('span'); state.className = 'sidebar-bots-state'; state.setAttribute('role', 'status');
      state.textContent = loading ? tx('chat_bots_loading', 'Loading bots…') : tx('chat_bots_unavailable', 'No available bots'); items.appendChild(state);
    }
    if (catalogRetry && !loading) {
      const retryButton = document.createElement('button'); retryButton.type = 'button'; retryButton.className = 'sidebar-bot-item';
      retryButton.textContent = tx('retry', 'Retry'); retryButton.onclick = () => window.refreshChatBots(); items.appendChild(retryButton);
    }
  }
  function paintDraftState() {
    const input = document.getElementById('msg');
    const draft = input ? input.value : '';
    const mention = /^\s*@([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?=\s|$)/.exec(draft);
    const group = ((S.session || {}).bot_participants || []).length > 0;
    const buttons = document.querySelectorAll('#chatBotRosterItems [data-bot]');
    for (const button of buttons) {
      const route = group ? currentRoute() : null;
      const selected = String(group ? !!route && route.bot === button.dataset.bot : S.activeProfile === button.dataset.bot);
      if (button.getAttribute('aria-pressed') !== selected) button.setAttribute('aria-pressed', selected);
      const disabled = loading || !S._bootReady || preparing || (!group && !!S.busy);
      if (button.disabled !== disabled) button.disabled = disabled;
    }
    const hint = document.getElementById('chatMentionHint');
    const hidden = !preparing && !(hint && hint.dataset.routeWarning) && !/(?:^|\s)@/.test(draft);
    if (hint && hint.hidden !== hidden) hint.hidden = hidden;
  }
  function starIcon(filled) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 16 16'); svg.setAttribute('width', '14'); svg.setAttribute('height', '14'); svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M8 1.8l1.9 3.9 4.3.6-3.1 3 .7 4.3L8 11.6l-3.8 2 .7-4.3-3.1-3 4.3-.6z');
    path.setAttribute('fill', filled ? 'currentColor' : 'none'); path.setAttribute('stroke', 'currentColor'); path.setAttribute('stroke-width', '1.3'); path.setAttribute('stroke-linejoin', 'round');
    svg.appendChild(path);
    return svg;
  }
  // What the roster shows beyond the catalog; a render repaints only when it changed.
  let paintedSignature = '';
  function rosterSignature() {
    const session = S.session || {}, route = (session.bot_participants || []).length ? currentRoute() : null;
    return JSON.stringify([route && route.bot, route && route.reason, session.default_bot || null,
      (workingState(session.session_id) || {}).bot || null, !!S._bootReady, !!S.busy, S.activeProfile || '']);
  }
  function badge(className, text) { const node = document.createElement('small'); node.className = className; node.textContent = text; return node; }
  function paint() {
    paintSidebarBots();
    const host = document.getElementById('composerWrap');
    if (!host) return;
    let bar = document.getElementById('chatBotRoster');
    if (!bar) { bar = document.createElement('div'); bar.id = 'chatBotRoster'; bar.className = 'chat-bot-roster'; host.prepend(bar); }
    bar.replaceChildren();
    const toggle = document.createElement('button'); toggle.type = 'button'; toggle.id = 'chatBotRosterToggle'; toggle.className = 'chat-bot-recipient chat-bot-toggle';
    toggle.dataset.i18n = collapsed() ? 'chat_roster_show' : 'chat_roster_hide';
    toggle.textContent = collapsed() ? tx('chat_roster_show', 'Show bots') : tx('chat_roster_hide', 'Hide bots'); toggle.setAttribute('aria-expanded',String(!collapsed())); toggle.setAttribute('aria-controls','chatBotRosterItems');
    toggle.onclick = () => { try { if (preferenceKey()) localStorage.setItem(preferenceKey(), collapsed() ? '0' : '1'); } catch (_) {} paint(); document.getElementById('chatBotRosterToggle').focus(); };
    bar.appendChild(toggle);
    const items = document.createElement('div'); items.id = 'chatBotRosterItems'; items.className = 'chat-bot-roster-items'; items.hidden = !!collapsed(); bar.appendChild(items);
    const session = S.session || {}, group = (session.bot_participants || []).length > 0;
    const rows = available(profiles, session);
    // On a phone a one-to-one chat has no row at all: the titlebar switches bots.
    const phone = !!(window.matchMedia && window.matchMedia('(max-width:640px)').matches);
    bar.hidden = phone && (session.bot_participants || []).length < 2;
    const route = group ? currentRoute() : null;
    const busyBot = group ? (workingState(session.session_id) || {}).bot : null;
    const manage = group && (session.bot_participants || []).length > 1 && canManageDefault(session);
    for (const p of rows) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'chat-bot-recipient'; button.dataset.bot = p.name;
      const input = document.getElementById('msg');
      button.setAttribute('aria-pressed',String(group ? !!(route && route.bot === p.name) : S.activeProfile === p.name));
      button.title = '@' + handleFor(p); button.disabled = loading || !S._bootReady || preparing || (!group && !!S.busy);
      button.innerHTML = botAvatarHtml(p); const name = document.createElement('span'); name.textContent = botDisplayName(p); button.appendChild(name);
      if (group && session.default_bot === p.name) button.appendChild(badge('group-route-default-badge', tx('group_route_default_badge', 'Default')));
      if (busyBot === p.name) {
        button.dataset.working = 'true';
        const state = badge('group-route-working', tx('group_route_working', 'Working'));
        const dot = document.createElement('span'); dot.className = 'group-route-working-dot'; dot.setAttribute('aria-hidden', 'true');
        state.prepend(dot); button.appendChild(state);
      }
      button.onclick = async () => {
        if (!S._bootReady || preparing) return;
        if (contextKey() !== key) { window.refreshChatBots(); return; }
        if (group) { if (!input) return; input.value = address(input.value, addressName(p, rows)); input.dispatchEvent(new Event('input', {bubbles:true})); input.focus(); paint(); }
        else if (p.name !== S.activeProfile || (typeof _showAllProfiles !== 'undefined' && _showAllProfiles)) { button.disabled = true; try { await switchToProfile(p.name); } catch (err) { showToast(err.message); } finally { button.disabled = false; window.refreshChatBots(); } }
      };
      items.appendChild(button);
      if (manage) {
        const isDefault = session.default_bot === p.name;
        const star = document.createElement('button'); star.type = 'button'; star.className = 'group-route-default-toggle'; star.dataset.defaultBot = p.name;
        const label = (isDefault ? tx('group_route_clear_default', 'Clear default') : tx('group_route_set_default', 'Set as default')) + ': ' + botDisplayName(p);
        star.setAttribute('aria-label', label); star.title = label; star.setAttribute('aria-pressed', String(isDefault));
        star.disabled = loading || !S._bootReady || preparing || defaultSaving;
        star.appendChild(starIcon(isDefault));
        star.onclick = () => { if (contextKey() !== key) { window.refreshChatBots(); return; } setDefaultBot(p.name); };
        items.appendChild(star);
      }
    }
    // A repaint resets the strip's scroll; keep the working bot in view on narrow screens.
    const busy = items.querySelector('[data-working="true"]');
    if (busy && !items.hidden && items.scrollWidth > items.clientWidth) {
      const box = items.getBoundingClientRect(), chip = busy.getBoundingClientRect();
      if (chip.right > box.right) items.scrollLeft += chip.right - box.right + 8;
      else if (chip.left < box.left) items.scrollLeft -= box.left - chip.left + 8;
    }
    if (!rows.length) { const state = document.createElement('span'); state.className = 'chat-bot-roster-state'; state.setAttribute('role','status'); state.textContent = loading ? tx('chat_bots_loading', 'Loading bots…') : tx('chat_bots_unavailable', 'No available bots'); items.appendChild(state); }
    let hint = document.getElementById('chatMentionHint');
    if (!hint) { hint = document.createElement('div'); hint.id = 'chatMentionHint'; hint.className = 'chat-mention-hint'; host.appendChild(hint); }
    paintDraftState();
    const draft = (document.getElementById('msg') || {}).value || '';
    const unavailable = !preparing && !!route && route.reason === 'last_unavailable';
    hint.hidden = !preparing && !unavailable && !/(?:^|\s)@/.test(draft);
    hint.textContent = preparing ? tx('chat_mentions_preparing', 'Preparing recipients…')
      : unavailable ? tx('group_route_last_bot_unavailable', 'The last bot is not available to you. Choose a bot to continue.')
      : tx('chat_mentions_hint', 'Type @ to bring in a colleague or another bot.');
    if (unavailable) hint.dataset.routeWarning = 'true'; else delete hint.dataset.routeWarning;
    paintedSignature = rosterSignature();
  }
  window.refreshChatBots = function () {
    scheduleDecorate();
    const next = contextKey(); if (next === key && !catalogRetry) { if (rosterSignature() !== paintedSignature) paint(); return; }
    const changed = next !== key;
    key = next; catalogRetry = false;
    if (changed) { profiles = []; people = []; }
    loading = true; peopleLoading = true; closeMenu(); const current = ++generation; paint();
    const fresh = () => generation === current && contextKey() === next;
    Promise.resolve(api('/api/profiles?fast=1&counts=0', {timeoutToast:false})).then(result => {
      if (!fresh()) return;
      if (!Array.isArray(result.profiles)) throw new Error('Invalid bot catalog');
      profiles = result.profiles;
    }).catch(() => { if (fresh()) catalogRetry = true; }).finally(() => {
      if (!fresh()) return;
      loading = false; paint(); paintMenu(); scheduleDecorate();
      try { window.dispatchEvent(new CustomEvent('synpulse:bot-catalog')); } catch (_) {}
    });
    if (!actor()) { people = []; peopleLoading = false; return; }
    Promise.resolve(api('/api/bot-screen/status', {timeoutToast:false, redirect401:false})).then(result => {
      if (!fresh()) return;
      const rows = Array.isArray(result && result.screens) ? result.screens : [];
      screensWaiting = new Map(rows.filter(r => r && r.state === 'waiting').map(r => [r.bot, {reason: String(r.reason || ''), help_since: r.help_since || null}]));
    }).catch(() => { if (fresh()) screensWaiting = new Map(); }).finally(() => { if (fresh()) paintSidebarBots(); });
    Promise.resolve(api('/api/people', {timeoutToast:false})).then(result => {
      if (!fresh()) return;
      if (!Array.isArray(result.people)) throw new Error('Invalid people catalog');
      people = result.people;
    }).catch(() => { if (fresh()) catalogRetry = true; }).finally(() => {
      if (!fresh()) return;
      peopleLoading = false; paintMenu();
    });
  };
  window.chatMentionsBlockBusySend = function () {
    const input = document.getElementById('msg');
    if (preparing || ((S.busy || (typeof _sendInProgress !== 'undefined' && _sendInProgress)) && /(?:^|\s)@/.test(input ? input.value : ''))) {
      showToast(tx('chat_mentions_busy', 'Wait for this turn to finish before sending mentions. Your draft is kept.')); return true;
    }
    return false;
  };
  window.prepareChatMentions = async function () {
    const input = document.getElementById('msg'); if (!input || !/(?:^|\s)@/.test(input.value)) return true;
    const origin = contextKey(), original = input.value;
    if (loading || peopleLoading) { showToast(tx('chat_mentions_loading', 'People and bots are still loading. Please try again.')); return false; }
    // Hidden bots are not offered in the picker, but a typed mention still resolves.
    const directory = catalog(true), selected = mentions(original, directory); if (!selected.length) return true;
    const users = selected.filter(row => row.kind === 'user').map(row => row.id), bots = selected.filter(row => row.kind === 'bot').map(row => row.id);
    if (users.length && ((S.pendingFiles || []).length || (typeof _pendingSelections !== 'undefined' && _pendingSelections.length))) {
      showToast(tx('chat_mentions_attachments', 'Remove private attachments or selections first. You can attach files explicitly in the group.')); return false;
    }
    const body = {session_id:(S.session || {}).session_id || null, participants:users, bot_participants:bots};
    if (!body.session_id) {
      const model = typeof _chatPayloadModelState === 'function' ? _chatPayloadModelState() : {};
      body.model = model.model || null; body.model_provider = model.model_provider || null;
      body.chat_mode = S._pendingChatMode || 'super';
    }
    const signature = JSON.stringify([actor(), body, original]);
    if (!retry || retry.signature !== signature) retry = {signature, id:crypto.randomUUID()};
    const oldReadOnly = input.readOnly, pendingFiles = [...(S.pendingFiles || [])];
    preparing = true; input.readOnly = true; closeMenu(); paint();
    try {
      const result = await api('/api/chat/mentions/prepare', {method:'POST', body:JSON.stringify({...body, request_id:retry.id})});
      if (contextKey() !== origin || input.value !== original) throw new Error(tx('chat_mentions_changed', 'The chat or draft changed. Nothing was sent.'));
      const target = result.session && result.session.session_id;
      if (!target) throw new Error(tx('chat_mentions_failed', 'Recipients could not be prepared. Your draft is kept.'));
      if (target !== (S.session || {}).session_id) {
        await loadSession(target);
        if ((S.session || {}).session_id !== target) throw new Error(tx('chat_mentions_navigation', 'The chat changed. Nothing was sent.'));
      } else { S.session = {...S.session, ...result.session}; }
      // Bot dispatch remains one explicitly addressed bot, never automatic fanout.
      const draft = normalizeBotAddress(original, selected);
      if (!users.length) S.pendingFiles = pendingFiles;
      input.value = draft; input.dispatchEvent(new Event('input', {bubbles:true}));
      retry = null; key = ''; window.refreshChatBots(); return true;
    } catch (error) { showToast(error.message || tx('chat_mentions_error', 'Could not prepare recipients. Your draft is kept.')); return false; }
    finally { preparing = false; input.readOnly = oldReadOnly; paint(); }
  };
  document.addEventListener('input', event => { if (event.target && event.target.id === 'msg') { menuIndex = 0; paintDraftState(); paintMenu(); } });
  document.addEventListener('focusin', event => { if (event.target && event.target.id === 'msg') paintMenu(); });
  document.addEventListener('keydown', event => {
    if (!event.target || event.target.id !== 'msg' || !menuRange || event.isComposing || event.keyCode === 229) return;
    if (!['ArrowUp','ArrowDown','Enter','Tab','Escape'].includes(event.key)) return;
    event.preventDefault(); event.stopImmediatePropagation();
    if (event.key === 'Escape') return closeMenu();
    if (!menuRows.length) return;
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') { menuIndex = (menuIndex + (event.key === 'ArrowUp' ? -1 : 1) + menuRows.length) % menuRows.length; paintMenu(); document.getElementById('chatMentionOption-' + menuIndex).scrollIntoView({block:'nearest'}); }
    else choose(menuRows[menuIndex]);
  }, true);
  document.addEventListener('pointerdown', event => { const popup = document.getElementById('chatMentionDropdown'); if (popup && !popup.contains(event.target) && event.target.id !== 'msg') closeMenu(); });
  window.addEventListener('synpulse:bot-updated', () => { key = ''; window.refreshChatBots(); });
  window.addEventListener('synpulse:boot-ready', () => { if (contextKey() !== key) window.refreshChatBots(); else paint(); });
  paint();
  // The recipient catalog (profiles + people) is chrome for the @-menu, not
  // the conversation. Load it after synpulse:boot-ready and browser idle so it
  // stops competing with the session restore; the boot-ready listener above
  // and the shared api() read layer collapse any overlap into one request.
  if (typeof runAfterBootReady === 'function') runAfterBootReady(() => window.refreshChatBots());
  else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => window.refreshChatBots(), {once:true});
  else window.refreshChatBots();
  // Live bot identity of group turns (plan 5.2). static/messages.js hands each
  // `turn_bot` stream event to onTurnBot; workingState(sessionId) is the
  // Working state per session, also announced as `synpulse:group-working`.
  window.groupRouting = {onTurnBot, workingState, reasonText, handleFor, setDefaultBot, predictRoute:currentRoute, botInfo, catalogReady};
  window.chatBotRecipientRules = {available, address, mentions, fold, normalizeBotAddress,
    HANDLE_DENYLIST, handleFromText, botHandle, resolveMention, predictRoute, reasonText, lastBot};
})();
