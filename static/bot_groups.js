// Presentation only: Hermes owns membership, execution, approvals and replay.
window.BotGroups = (() => {
  const read = (op, params = {}, signal) => api('/api/bot-groups/' + op + '?' + new URLSearchParams(params), { signal });
  const write = (op, params) => api('/api/bot-groups/' + op, { method: 'POST', body: JSON.stringify(params), retries: 0 });
  const node = (tag, className = '', text = '') => {
    const result = document.createElement(tag);
    result.className = className;
    result.textContent = text;
    return result;
  };
  const button = (label, action, disabled = false) => {
    const result = node('button', 'bot-groups-button', label);
    result.type = 'button';
    result.onclick = action;
    result.disabled = disabled;
    return result;
  };
  let capabilities = null, rooms = [], profiles = [], selected = null, snapshot = null;
  let cursor = 0, events = [], authority = '', nextOffset = null;
  let active = false, timer = null, generation = 0, reading = false, busy = false;
  let controller = null, fresh = false, createAttempt = null;
  let readError = '', actionError = '', renderedLog = '', renderedMembers = '', renderedActions = '';
  // Unacknowledged *user drafts*, never an execution queue. Explicit retry reuses
  // the same event ID and payload; leaving/reloading never automatically sends.
  const drafts = new Map();
  const can = op => capabilities?.driver === true && capabilities.methods?.includes('groups.' + op);
  const error = message => { actionError = message || ''; showError(); };
  const showError = () => { $('botGroupsError').textContent = actionError || readError; };

  async function init() {
    try {
      const status = await read('capabilities');
      document.querySelectorAll('[data-panel="botGroups"]').forEach(item => { item.hidden = !status.enabled; });
    } catch (_) { /* Optional, default-off feature; existing chat is unaffected. */ }
  }

  function leave() {
    active = false;
    generation++;
    clearTimeout(timer);
    controller?.abort();
    reading = false;
  }

  async function enter() {
    saveDraft();
    leave();
    active = true;
    const own = generation;
    controller = new AbortController();
    capabilities = null; snapshot = null; fresh = false; events = [];
    readError = ''; error(''); renderRoom();
    try {
      const status = await read('capabilities', {}, controller.signal);
      if (own !== generation) return;
      capabilities = status.available ? status.capabilities : null;
      if (!capabilities || !['groups.list', 'groups.state', 'groups.log'].every(m => capabilities.methods?.includes(m))) {
        throw new Error(status.error || t('bot_groups_unavailable'));
      }
      const [list, roster] = await Promise.all([
        read('list', { limit: 100, offset: 0 }, controller.signal), read('profiles', {}, controller.signal),
      ]);
      if (own !== generation) return;
      rooms = list.rooms || [];
      nextOffset = list.next_offset;
      profiles = roster.profiles || [];
      renderList();
      $('botGroupsCreate').disabled = !can('create');
      if (selected && rooms.some(r => r.room_id === selected)) await select(selected);
      else if (rooms.length) await select(rooms[0].room_id);
      else { selected = null; renderRoom(); }
    } catch (e) {
      if (own === generation && e.name !== 'AbortError') error(e.message);
    }
  }

  function renderList() {
    const list = $('botGroupsList');
    list.replaceChildren();
    if (!rooms.length) list.append(node('p', 'bot-groups-muted', t('bot_groups_empty')));
    rooms.forEach(room => {
      const item = button(room.name || room.room_id, () => select(room.room_id));
      item.classList.toggle('selected', selected === room.room_id);
      item.setAttribute('aria-pressed', String(selected === room.room_id));
      list.append(item);
    });
    if (nextOffset != null) list.append(button(t('bot_groups_more'), async () => {
      const own = generation;
      try {
        const data = await read('list', { limit: 100, offset: nextOffset }, controller.signal);
        if (own !== generation) return;
        const known = new Set(rooms.map(r => r.room_id));
        rooms.push(...data.rooms.filter(r => !known.has(r.room_id)));
        nextOffset = data.next_offset;
        renderList();
      } catch (e) { if (own === generation && e.name !== 'AbortError') error(e.message); }
    }));
  }

  async function select(roomId) {
    saveDraft();
    generation++;
    controller?.abort();
    controller = new AbortController();
    clearTimeout(timer);
    reading = false;
    selected = roomId;
    snapshot = null; cursor = 0; events = []; authority = ''; fresh = false;
    readError = ''; error('');
    $('botGroupsCreateForm').hidden = true;
    $('botGroupsComposer').elements.text.value = drafts.get(roomId)?.text || '';
    renderList(); renderRoom();
    if (typeof closeMobileSidebar === 'function' && window.innerWidth < 768) closeMobileSidebar();
    await refresh();
  }

  function saveDraft() {
    if (!selected) return;
    const text = $('botGroupsComposer').elements.text.value;
    const previous = drafts.get(selected);
    drafts.set(selected, previous?.text === text ? previous : { text });
  }

  async function refresh() {
    if (!active || !selected || reading || document.hidden) return;
    const roomId = selected, own = generation;
    reading = true;
    let more = false;
    try {
      const state = await read('state', { room_id: roomId }, controller.signal);
      if (own !== generation) return;
      const identity = state.room.authority_gateway_id + ':' + state.room.authority_epoch;
      if (authority && authority !== identity) { cursor = 0; events = []; }
      authority = identity;
      const log = await read('log', { room_id: roomId, since_seq: cursor, limit: 100 }, controller.signal);
      if (own !== generation) return;
      if (identity !== log.authority.gateway_id + ':' + log.authority.epoch) {
        cursor = 0; events = []; authority = '';
        throw new Error(t('bot_groups_reconnecting'));
      }
      const seen = new Set(events.map(e => e.event_id));
      events.push(...log.events.filter(e => e.room_id === roomId && !seen.has(e.event_id)));
      events = events.slice(-500);
      cursor = log.cursor;
      snapshot = state;
      fresh = true; readError = ''; showError();
      more = log.has_more;
      renderRoom();
    } catch (e) {
      if (own === generation && e.name !== 'AbortError') {
        fresh = false; readError = e.message; showError(); renderRoom();
      }
    } finally {
      if (own === generation) {
        reading = false;
        clearTimeout(timer);
        if (active) timer = setTimeout(refresh, more ? 100 : 1800);
      }
    }
  }

  function renderRoom() {
    const room = snapshot?.room;
    const status = snapshot?.driver_status;
    $('botGroupsTitle').textContent = room?.name || t('bot_groups_title');
    $('botGroupsStatus').textContent = !selected ? t('bot_groups_intro')
      : !snapshot ? t('loading') : !status?.running ? t('bot_groups_no_driver')
      : status.blocked ? t('bot_groups_blocked') : status.working ? t('bot_groups_working') : t('bot_groups_ready');
    $('botGroupsStop').disabled = busy || !fresh || !can('stop') || !(status?.working || status?.blocked);
    $('botGroupsComposer').hidden = !room;
    $('botGroupsSend').disabled = busy || !fresh || !can('send') || !status?.running;
    const members = $('botGroupsMembers');
    const memberKey = JSON.stringify(room?.members || []);
    if (renderedMembers !== memberKey) {
    renderedMembers = memberKey;
    members.replaceChildren();
    (room?.members || []).forEach(member => members.append(button('@' + member.handle, () => {
      const input = $('botGroupsComposer').elements.text;
      input.value += (input.value && !input.value.endsWith(' ') ? ' ' : '') + '@' + member.handle + ' ';
      saveDraft(); input.focus();
    })));
    }
    const log = $('botGroupsMessages');
    const logKey = selected + ':' + authority + ':' + events.map(e => e.event_id).join(',');
    if (renderedLog !== logKey) {
    renderedLog = logKey;
    const following = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    const rows = events.filter(e => ['message.user', 'message.member', 'turn.failed', 'turn.cancelled', 'turn.deferred'].includes(e.kind));
    log.replaceChildren(...rows.map(event => {
      const message = event.kind.startsWith('message.');
      const row = node('article', message ? 'bot-groups-message' : 'bot-groups-notice');
      const member = room?.members?.find(m => m.member_id === event.payload?.member_id || m.member_id === event.actor?.id);
      row.dataset.eventId = event.event_id;
      row.append(node('strong', '', event.kind === 'message.user' ? t('bot_groups_you') : member?.display_name || member?.handle || t('bot_groups_title')));
      row.append(node('div', 'bot-groups-prose', message ? String(event.payload?.text || '')
        : String(event.payload?.error || event.payload?.reason || t('bot_groups_' + event.kind.replace('.', '_')))));
      return row;
    }));
    if (events.length === 500) log.prepend(node('p', 'bot-groups-muted', t('bot_groups_recent')));
    if (following) log.scrollTop = log.scrollHeight;
    }
    const actions = $('botGroupsActions');
    const actionKey = JSON.stringify([selected, status?.pending_actions, busy, fresh, capabilities?.methods]);
    if (renderedActions === actionKey) return;
    renderedActions = actionKey;
    actions.replaceChildren();
    (status?.pending_actions || []).forEach(action => {
      const row = node('div', 'bot-groups-approval');
      if (action.kind === 'approval') {
        const approval = action.approval || {};
        row.append(node('strong', '', t('bot_groups_approval')));
        row.append(node('pre', 'bot-groups-prose', String(approval.command || approval.description || approval.prompt || action.task_id)));
        ['once', 'deny'].filter(choice => approval.choices?.includes(choice)).forEach(choice => {
          row.append(button(t('bot_groups_' + choice), () => mutate('approve', {
            room_id: selected, member_id: action.member_id, task_id: action.task_id,
            execution_generation: action.execution_generation, request_id: action.request_id, choice,
          }), busy || !fresh || !can('approve')));
        });
      } else if (action.kind === 'retry') {
        row.append(node('p', '', t('bot_groups_retry_warning')));
        row.append(button(t('bot_groups_retry_task'), async () => {
          const roomId = selected;
          const confirmed = await showConfirmDialog({ title: t('bot_groups_retry_task'), message: t('bot_groups_retry_warning'), confirmLabel: t('bot_groups_retry_task'), danger: true, focusCancel: true });
          if (confirmed && selected === roomId) mutate('retry', { room_id: roomId, task_id: action.task_id });
        }, busy || !fresh || !can('retry')));
      }
      if (row.childNodes.length) actions.append(row);
    });
  }

  async function mutate(op, params) {
    if (busy || !fresh || !can(op)) return;
    const own = generation;
    busy = true; renderRoom(); error('');
    try { await write(op, params); }
    catch (e) { if (own === generation) error(e.message); }
    finally { busy = false; if (active) renderRoom(); if (own === generation) await refresh(); }
  }

  async function send(event) {
    event.preventDefault();
    if (busy || !fresh || !selected || !can('send') || !snapshot?.driver_status?.running) return;
    saveDraft();
    const roomId = selected, own = generation, draft = drafts.get(roomId);
    if (!draft?.text.trim()) return;
    draft.event_id ||= crypto.randomUUID();
    busy = true; renderRoom(); error('');
    try {
      const result = await write('send', { room_id: roomId, event_id: draft.event_id, payload: { text: draft.text, thread_id: 'main' } });
      if (result.accepted !== true) throw new Error(t('bot_groups_send_unknown'));
      if (drafts.get(roomId) === draft) drafts.delete(roomId);
      if (own === generation && $('botGroupsComposer').elements.text.value === draft.text) $('botGroupsComposer').elements.text.value = '';
    } catch (e) { if (own === generation) error(e.message + ' ' + t('bot_groups_send_unknown')); }
    finally { busy = false; if (active) renderRoom(); if (own === generation) await refresh(); }
  }

  function openCreate() {
    if (!can('create')) return;
    const form = $('botGroupsCreateForm');
    form.hidden = false;
    const choices = $('botGroupsProfileChoices');
    choices.replaceChildren(...profiles.map((profile, index) => {
      const label = node('label', 'bot-groups-profile');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox'; checkbox.value = String(index); checkbox.name = 'member';
      label.append(checkbox, document.createTextNode(profile.display_name));
      return label;
    }));
    form.elements.name.focus();
    if (typeof closeMobileSidebar === 'function' && window.innerWidth < 768) closeMobileSidebar();
  }

  async function create(event) {
    event.preventDefault();
    if (busy || !can('create')) return;
    const form = event.currentTarget;
    const chosen = Array.from(form.querySelectorAll('input[name="member"]:checked')).map(input => profiles[Number(input.value)]);
    if (chosen.length < 2 || chosen.length > 6) { error(t('bot_groups_select_members')); return; }
    const own = generation;
    busy = true; $('botGroupsSave').disabled = true; error('');
    try {
      const params = { name: form.elements.name.value.trim(), members: chosen.map((p, i) => ({
        member_id: 'member-' + (i + 1), profile: p.name, display_name: p.display_name,
        handle: 'bot-' + (i + 1),
      })) };
      const signature = JSON.stringify(params);
      if (createAttempt?.signature !== signature) createAttempt = { signature, room_id: 'group-' + crypto.randomUUID() };
      const result = await write('create', { ...params, room_id: createAttempt.room_id });
      if (own !== generation) return;
      createAttempt = null;
      rooms = [result.room, ...rooms.filter(r => r.room_id !== result.room.room_id)];
      form.hidden = true; form.reset(); await select(result.room.room_id);
    } catch (e) { if (own === generation) error(e.message); }
    finally { busy = false; $('botGroupsSave').disabled = false; renderRoom(); }
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('botGroupsCreate').onclick = openCreate;
    $('botGroupsRefresh').onclick = enter;
    $('botGroupsCancelCreate').onclick = () => { $('botGroupsCreateForm').hidden = true; };
    $('botGroupsCreateForm').onsubmit = create;
    $('botGroupsComposer').onsubmit = send;
    $('botGroupsComposer').elements.text.oninput = saveDraft;
    $('botGroupsStop').onclick = () => mutate('stop', { room_id: selected, cancel_id: crypto.randomUUID() });
    init();
  });
  document.addEventListener('visibilitychange', () => { if (active && !document.hidden) refresh(); });
  window.addEventListener('pagehide', leave);
  return { enter, leave };
})();
