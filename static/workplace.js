/* Workplace (10 Nov 2026): what is yours, your team's and the organisation's.

   Two parts, both views over data the other screens own:
     SPScope      who you are in the structure (teams, department, role) and
                  the lens "mine | my team | my department | organisation"
                  that any list can reuse.
     SPWorkplace  the Studio landing "Overzicht": use cases, scheduled tasks,
                  bots, playbooks, prompts and backlog in one place, grouped
                  per kind, per team (under its department) or per person.
   Nothing is stored here except the chosen lens and grouping (localStorage).
   Rights stay on the server: every list is what the routes already return. */
(function(){
  'use strict';
  const NL = {
    recent_chats:'Recente gesprekken', recent_runs:'Laatst uitgevoerde taken', no_chats:'Nog geen recente gesprekken.', no_runs:'Nog geen uitgevoerde taken in deze weergave.',
    projects:'Projecten', chart:'Botorganogram', agenda:'Taakagenda', navigate:'Alle onderdelen',
    title: 'Overzicht', morning: 'Goedemorgen', afternoon: 'Goedemiddag', evening: 'Goedenavond',
    intro: 'Alles wat van jou is, van je team en van de organisatie op één plek.',
    l_mine: 'Van mij', l_team: 'Mijn team', l_teams: 'Mijn teams', l_dept: 'Mijn afdeling', l_depts: 'Mijn afdelingen', l_org: 'Organisatie', l_all: 'Alles',
    lens: 'Toon', group: 'Groepeer', g_kind: 'Per soort', g_team: 'Per team', g_person: 'Per persoon',
    k_usecase: 'Use cases', k_task: 'Geplande taken', ks_task: 'Taken', k_bot: 'Bots', k_playbook: 'Playbooks', k_prompt: 'Prompts', k_backlog: 'Backlog',
    r_admin: 'Beheerder', r_dept: 'Afdelingsbeheerder', r_lead: 'Teamlead', r_member: 'Lid',
    no_team: 'Geen team', no_owner: 'Zonder eigenaar', no_dept: 'Overig', not_placed: 'Nog niet ingedeeld in een team',
    all: 'Alles bekijken', more: 'Toon nog {n}', less: 'Minder tonen', untitled: 'Naamloos',
    s_on: 'Actief', s_paused: 'Gepauzeerd', s_error: 'Fout', s_done: 'Afgerond', assistant: 'Persoonlijke assistent',
    lead: 'Teamlead', members: '{n} leden', member_one: '1 lid', you: 'jij', by: 'van {p}',
    empty_h: 'Hier staat nog niets', empty_mine: 'Begin met een use case: vertel over je werk en de Super Agent zoekt uit wat hij kan overnemen.',
    empty_scope: 'In deze weergave staat nog niets. Kies een andere weergave of maak een use case.',
    empty_peek: 'Bekijk {l}', kpi_hint: '{k} openen',
    loading: 'Laden…', failed: 'Laden is niet gelukt. Probeer het opnieuw.', retry: 'Opnieuw', people: 'Mensen', nothing: 'Nog niets',
    p_discover: 'Ontdekken', p_design: 'Inrichten', p_playbook: 'Bouwplan', p_build: 'Bouwen', p_measure: 'Meten'
  };
  const EN = {
    recent_chats:'Recent conversations', recent_runs:'Recently run tasks', no_chats:'No recent conversations yet.', no_runs:'No task runs in this view yet.',
    projects:'Projects', chart:'Bot org chart', agenda:'Task calendar', navigate:'All destinations',
    title: 'Overview', morning: 'Good morning', afternoon: 'Good afternoon', evening: 'Good evening',
    intro: 'Everything that is yours, your team\'s and the organisation\'s in one place.',
    l_mine: 'Mine', l_team: 'My team', l_teams: 'My teams', l_dept: 'My department', l_depts: 'My departments', l_org: 'Organisation', l_all: 'Everything',
    lens: 'Show', group: 'Group', g_kind: 'By kind', g_team: 'By team', g_person: 'By person',
    k_usecase: 'Use cases', k_task: 'Scheduled tasks', ks_task: 'Tasks', k_bot: 'Bots', k_playbook: 'Playbooks', k_prompt: 'Prompts', k_backlog: 'Backlog',
    r_admin: 'Admin', r_dept: 'Department admin', r_lead: 'Team lead', r_member: 'Member',
    no_team: 'No team', no_owner: 'No owner', no_dept: 'Other', not_placed: 'Not placed in a team yet',
    all: 'View all', more: 'Show {n} more', less: 'Show fewer', untitled: 'Untitled',
    s_on: 'Active', s_paused: 'Paused', s_error: 'Failing', s_done: 'Completed', assistant: 'Personal assistant',
    lead: 'Team lead', members: '{n} members', member_one: '1 member', you: 'you', by: 'by {p}',
    empty_h: 'Nothing here yet', empty_mine: 'Start with a use case: tell about your work and the Super Agent works out what it can take over.',
    empty_scope: 'Nothing in this view yet. Choose another view or create a use case.',
    empty_peek: 'View {l}', kpi_hint: 'Open {k}',
    loading: 'Loading…', failed: 'Loading failed. Try again.', retry: 'Retry', people: 'People', nothing: 'Nothing yet',
    p_discover: 'Discover', p_design: 'Design', p_playbook: 'Build plan', p_build: 'Build', p_measure: 'Measure'
  };
  const lang = () => ((document.documentElement.lang || 'nl').toLowerCase().startsWith('en') ? 'en' : 'nl');
  const w = (k, v) => { let s = (lang() === 'en' ? EN : NL)[k] || NL[k] || k; if (v) for (const [a, b] of Object.entries(v)) s = s.replace('{' + a + '}', b); return s; };
  const H = v => (typeof esc === 'function' ? esc(String(v ?? '')) : String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const call = p => api(p, { timeoutToast: false });
  const norm = e => String(e || '').trim().toLowerCase();
  const initials = s => String(s || '?').replace(/@.*/, '').split(/[.\s_-]+/).filter(Boolean).slice(0, 2).map(x => x[0].toUpperCase()).join('') || '?';
  const hue = v => { let h = 0; for (const c of String(v || '')) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
  const store = { get: (k, d) => { try { return localStorage.getItem(k) || d; } catch (_e) { return d; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch (_e) {} } };
  const KINDS = ['usecase', 'task', 'bot', 'playbook', 'prompt', 'backlog'];

  // ── Scope: who am I in the structure ───────────────────────────────────────
  let ctxCache = null, ctxAt = 0, ctxLoading = null;
  async function loadCtx(force){
    if (!force && ctxCache && Date.now() - ctxAt < 60000) return ctxCache;
    if (ctxLoading) return ctxLoading;
    ctxLoading = (async () => {
      const [me, pr] = await Promise.all([call('/api/org/structure/me').catch(() => ({})), call('/api/studio/prompts').catch(() => null)]);
      const chart = me && me.can_view ? await call('/api/org/chart').catch(() => null) : null;
      const c = { email: norm(me.email), scope: me.scope || 'none', canView: !!me.can_view, canManage: !!me.can_manage,
                  leadTeams: new Set(me.lead_teams || []), memberTeams: new Set(me.member_teams || []), adminDepts: new Set(me.departments || []),
                  teams: new Map(), depts: new Map(), people: new Map(), jobTeam: new Map(), promptRows: pr ? (pr.prompts || []) : [], chart };
      c.myTeams = new Set([...c.leadTeams, ...c.memberTeams]);
      for (const d of (pr && pr.departments) || []) c.depts.set(d.key, { key: d.key, name: d.name || d.key });
      for (const t of (pr && pr.teams) || []) c.teams.set(t.key, { key: t.key, name: t.name || t.key, department: t.department || t.key.split('/')[0], lead: '', members: [] });
      for (const d of (chart && chart.departments) || []) {
        if (!c.depts.has(d.key)) c.depts.set(d.key, { key: d.key, name: d.name || d.key });
        for (const t of d.teams || []) {
          const row = c.teams.get(t.key) || { key: t.key, name: t.name || t.key, department: t.department || d.key };
          row.lead = norm(t.lead); row.members = (t.members || []).map(norm).filter(Boolean);
          c.teams.set(t.key, row);
          for (const j of t.job_list || []) { c.jobTeam.set((j.store || 'default') + ':' + j.id, t.key); }
        }
      }
      for (const [email, p] of Object.entries((chart && chart.people) || {})) c.people.set(norm(email), { email: norm(email), title: p.title || '', avatar_url: p.avatar_url || '' });
      c.myDepts = new Set([...c.adminDepts, ...[...c.myTeams].map(k => deptKey(c, k))].filter(Boolean));
      ctxCache = c; ctxAt = Date.now();
      return c;
    })();
    try { return await ctxLoading; } finally { ctxLoading = null; }
  }
  function deptKey(c, teamKey){ const t = c.teams.get(teamKey); return t ? t.department : String(teamKey || '').split('/')[0]; }
  function teamName(c, key){ return (c.teams.get(key) || {}).name || (key ? String(key).split('/').pop() : w('no_team')); }
  function deptName(c, key){ return (c.depts.get(key) || {}).name || key || w('no_dept'); }
  function personName(email){
    const local = String(email || '').replace(/@.*/, '');
    return local.split(/[._-]+/).filter(Boolean).map(p => p[0].toUpperCase() + p.slice(1)).join(' ') || w('no_owner');
  }
  // The team a person is shown under: where they lead first, else their first team.
  function primaryTeam(c, email){
    email = norm(email);
    if (!email) return '';
    let member = '';
    for (const t of c.teams.values()) {
      if (t.lead === email) return t.key;
      if (!member && t.members.includes(email)) member = t.key;
    }
    return member;
  }
  function roles(c){
    const out = [];
    if (c.scope === 'all') out.push({ label: w('r_admin'), cls: 'admin' });
    else if (c.scope === 'departments') out.push({ label: w('r_dept') + ' ' + [...c.adminDepts].map(k => deptName(c, k)).join(', '), cls: 'admin' });
    if (c.leadTeams.size) out.push({ label: w('r_lead') + ' ' + [...c.leadTeams].map(k => teamName(c, k)).join(', '), cls: 'lead' });
    const only = [...c.memberTeams].filter(k => !c.leadTeams.has(k));
    if (only.length) out.push({ label: w('r_member') + ' ' + only.map(k => teamName(c, k)).join(', '), cls: '' });
    return out;
  }
  function lenses(c){
    const out = ['mine'];
    if (c.myTeams.size) out.push('team');
    // The department lens only earns its place when it shows more than the team lens.
    const deptTeams = [...c.teams.values()].filter(t => c.myDepts.has(t.department)).map(t => t.key);
    if (c.myDepts.size && deptTeams.some(k => !c.myTeams.has(k))) out.push('dept');
    out.push('org');
    return out;
  }
  function lensLabel(c, lens){
    if (lens === 'mine') return w('l_mine');
    if (lens === 'team') return w(c.myTeams.size > 1 ? 'l_teams' : 'l_team');
    if (lens === 'dept') return w(c.myDepts.size > 1 ? 'l_depts' : 'l_dept');
    return w(c.scope === 'all' ? 'l_org' : 'l_all');
  }
  function match(c, it, lens){
    if (lens === 'mine') return !!it.owner && norm(it.owner) === c.email;
    if (lens === 'team') return !!it.team && c.myTeams.has(it.team);
    if (lens === 'dept') return !!it.team && c.myDepts.has(deptKey(c, it.team));
    return true;
  }
  function getLens(c, surface, fallback){
    const saved = store.get('sp-lens-' + surface, fallback || 'org');
    return lenses(c).includes(saved) ? saved : (fallback && lenses(c).includes(fallback) ? fallback : 'org');
  }
  function barHtml(c, surface, items, current){
    const ls = lenses(c);
    const counts = Object.fromEntries(ls.map(l => [l, items.filter(it => match(c, it, l)).length]));
    return `<div class="om-seg sp-lens" role="group" aria-label="${H(w('lens'))}">${ls.map(l => `<button type="button" aria-pressed="${l === current}" data-sp-lens="${l}" data-sp-lens-surface="${H(surface)}">${H(lensLabel(c, l))}<span class="sp-lens-n">${counts[l]}</span></button>`).join('')}</div>`;
  }
  // Department > team, the viewer's own teams first, "no team" last.
  function groupByTeam(c, items){
    const by = new Map();
    for (const it of items) { const k = it.team || ''; if (!by.has(k)) by.set(k, []); by.get(k).push(it); }
    const keys = [...by.keys()].sort((a, b) => (!a) - (!b) || (c.myTeams.has(b) - c.myTeams.has(a)) || deptName(c, deptKey(c, a)).localeCompare(deptName(c, deptKey(c, b))) || teamName(c, a).localeCompare(teamName(c, b)));
    return keys.map(k => ({ key: k, name: k ? teamName(c, k) : w('no_team'), department: k ? deptName(c, deptKey(c, k)) : '', items: by.get(k) }));
  }
  document.addEventListener('click', ev => {
    const b = ev.target.closest && ev.target.closest('[data-sp-lens]');
    if (!b) return;
    const surface = b.dataset.spLensSurface || '';
    store.set('sp-lens-' + surface, b.dataset.spLens);
    document.dispatchEvent(new CustomEvent('sp-lens-change', { detail: { surface, lens: b.dataset.spLens } }));
  });
  window.SPScope = { load: loadCtx, lenses, lensLabel, match, getLens, barHtml, groupByTeam, teamName, deptName, deptKey, personName, primaryTeam, roles,
    face: (c, email, cls) => faceHtml(c, email, cls) };

  function faceHtml(c, email, cls){
    const p = (c && c.people.get(norm(email))) || {};
    return `<span class="om-face ${cls || ''}" style="--h:${hue(email)}" title="${H(personName(email))}">${p.avatar_url ? `<img src="${H(p.avatar_url)}" alt="" loading="lazy">` : H(initials(email))}</span>`;
  }

  // ── Workplace: the items ───────────────────────────────────────────────────
  const Wp = { data: null, at: 0, group: store.get('sp-wp-group', 'kind'), open: new Set(), more: new Set(), token: 0, body: null, intake: null, setup: '' };

  async function loadData(c){
    const [uc, bl, pb, cr, pf, me] = await Promise.all([
      call('/api/studio/usecases').catch(() => null), call('/api/studio/backlog').catch(() => null), call('/api/studio/playbooks').catch(() => null),
      call('/api/crons?all_profiles=1').catch(() => null), call('/api/profiles').catch(() => null), call('/api/studio/profile?who=me').catch(() => null)]);
    Wp.intake = me;
    const items = [];
    const withTeam = (team, owner) => team || primaryTeam(c, owner);
    for (const r of (uc && uc.usecases) || []) items.push({ kind: 'usecase', id: r.id, title: r.title || '', owner: norm(r.owner), team: withTeam(r.team, r.owner), sub: w('p_' + (r.phase || 'discover')), at: r.updated_at || 0 });
    for (const r of (bl && bl.items) || []) { if (['live', 'parked'].includes(r.status)) continue; items.push({ kind: 'backlog', id: r.id, title: r.title || '', owner: norm(r.owner), team: withTeam(r.team, r.owner), sub: r.pain || '', at: r.updated_at || 0 }); }
    for (const r of (pb && pb.playbooks) || []) items.push({ kind: 'playbook', id: r.id, title: r.title || '', owner: norm(r.owner), team: withTeam(r.team, r.owner), sub: r.summary || r.category || '', at: r.updated_at || 0 });
    for (const r of c.promptRows) items.push({ kind: 'prompt', id: r.id, title: r.label || r.title || '', owner: norm(r.owner), team: withTeam(r.scope === 'team' ? r.team : '', r.scope === 'org' ? '' : r.owner), sub: r.category || '', at: r.updated_at || 0 });
    const bots = new Map();
    for (const p of (pf && pf.profiles) || []) {
      if (p.visible === false) continue;
      const b = p.bot || {};
      const person = (p.bot_kind || b.kind) === 'person';
      const owner = person ? ([...c.people.keys()].find(e => e.split('@')[0].split('.')[0] === String(p.name).toLowerCase()) || '') : '';
      const team = (Array.isArray(b.serves) && b.serves[0]) || primaryTeam(c, owner);
      const title = b.title || (p.is_default ? 'Super Agent' : p.name);
      bots.set(p.name, { title, team });
      items.push({ kind: 'bot', id: p.name, title, owner, team, sub: person ? w('assistant') : (b.category || b.description || ''), avatar: p.bot_avatar_url || '', color: b.color || '', at: 0 });
    }
    for (const j of (cr && cr.jobs) || []) {
      const st = String(j.owner_profile ?? j.profile ?? '') || 'default';
      const bot = String(j.assigned_bot || '') || st;
      const failing = j.enabled && (j.last_status === 'error' || (j.failure_streak || 0) > 0);
      const status = j.state === 'completed' ? 'done' : !j.enabled ? 'paused' : failing ? 'error' : 'on';
      const sched = typeof _cronScheduleInWords === 'function' ? _cronScheduleInWords(j.schedule_display || (j.schedule && j.schedule.display) || '') : (j.schedule_display || '');
      const botTitle = (bots.get(bot) || {}).title || '';
      items.push({ kind: 'task', id: j.id, store: st, bot, title: j.name || '', owner: norm(j.owner_email),
                   team: c.jobTeam.get(st + ':' + j.id) || (bots.get(bot) || {}).team || primaryTeam(c, j.owner_email),
                   sub: [sched, botTitle].filter(Boolean).join(' · '), status, runStatus:j.last_status === 'error' ? w('s_error') : j.last_status === 'ok' || j.last_status === 'success' ? w('s_done') : '', at: Date.parse(j.last_run_at || '') / 1000 || 0 });
    }
    Wp.runsReadable = Array.isArray(cr?.jobs);
    Wp.data = items; Wp.at = Date.now();
    return items;
  }

  const STATUS_RANK = { error: 0, on: 1, paused: 2, done: 3 };
  function sortItems(rows){
    return rows.slice().sort((a, b) => ((STATUS_RANK[a.status] ?? 1) - (STATUS_RANK[b.status] ?? 1)) || (b.at - a.at) || String(a.title).localeCompare(String(b.title)));
  }
  function statusBadge(it){
    if (it.kind !== 'task') return '';
    const cls = { on: 'ok', error: 'error', paused: 'paused', done: 'paused' }[it.status] || '';
    return `<span class="om-badge ${cls}">${H(w('s_' + it.status))}</span>`;
  }
  function rowHtml(c, it, opts){
    opts = opts || {};
    const bits = [];
    if (opts.kind) bits.push(w('k_' + it.kind));
    if (it.sub) bits.push(it.sub);
    if (opts.team && it.team) bits.push(teamName(c, it.team));
    if (opts.owner && it.owner && it.owner !== c.email) bits.push(personName(it.owner));
    const lead = it.kind === 'bot'
      ? `<span class="om-face bot wp-face-sm" style="${it.color ? '--bot:' + H(it.color) : ''}">${it.avatar ? `<img src="${H(it.avatar)}" alt="" loading="lazy">` : H(initials(it.title))}</span>`
      : `<span class="wp-kind-dot k-${it.kind}" aria-hidden="true"></span>`;
    return `<button type="button" class="wp-row" data-wp-open="${H(it.kind)}" data-wp-id="${H(it.id)}" data-wp-store="${H(it.store || '')}" data-wp-bot="${H(it.bot || '')}">${lead}
      <span class="wp-row-main"><span class="wp-row-title ${it.title ? '' : 'wp-untitled'}">${H(it.title || w('untitled'))}</span>${bits.length ? `<span class="wp-row-sub">${H(bits.join(' · '))}</span>` : ''}</span>${statusBadge(it)}</button>`;
  }
  function listHtml(c, rows, key, opts, limit){
    limit = limit || 6;
    const sorted = sortItems(rows);
    const openAll = Wp.more.has(key);
    const shown = openAll ? sorted : sorted.slice(0, limit);
    return shown.map(it => rowHtml(c, it, opts)).join('') + (sorted.length > limit ? `<button type="button" class="wp-more" data-wp-more="${H(key)}">${H(openAll ? w('less') : w('more', { n: sorted.length - limit }))}</button>` : '');
  }
  function kindCard(c, kind, rows, key, opts){
    return `<section class="wp-card"><header class="wp-card-head"><h3>${H(w('k_' + kind))}</h3><span class="wp-count">${rows.length}</span><button type="button" class="om-link wp-all" data-wp-go="${kind}">${H(w('all'))}</button></header>
      <div class="wp-rows">${listHtml(c, rows, key, opts)}</div></section>`;
  }
  function short(k){ return k === 'task' ? w('ks_task') : w('k_' + k); }
  function chipsHtml(rows, key){
    const n = Object.fromEntries(KINDS.map(k => [k, rows.filter(it => it.kind === k).length]));
    const have = KINDS.filter(k => n[k]);
    if (!have.length) return `<span class="wp-none">${H(w('nothing'))}</span>`;
    return have.map(k => `<button type="button" class="wp-chip" aria-pressed="${Wp.open.has(key + '|' + k)}" data-wp-toggle="${H(key + '|' + k)}"><span class="wp-kind-dot k-${k}" aria-hidden="true"></span>${H(short(k))}<strong>${n[k]}</strong></button>`).join('');
  }
  function openLists(c, rows, key, opts){
    return KINDS.filter(k => Wp.open.has(key + '|' + k)).map(k => {
      const sub = rows.filter(it => it.kind === k);
      return sub.length ? `<div class="wp-sub"><div class="wp-sub-label">${H(w('k_' + k))}</div><div class="wp-rows">${listHtml(c, sub, key + '|' + k, opts, 8)}</div></div>` : '';
    }).join('');
  }
  function teamView(c, rows){
    const groups = groupByTeam(c, rows);
    let lastDept = null, out = '';
    const showDept = groups.some(g => g.department && g.department !== g.name);
    for (const g of groups) {
      const t = c.teams.get(g.key) || {};
      if (showDept && g.department !== lastDept) { out += `<div class="wp-dept">${H(g.department || w('no_dept'))}</div>`; lastDept = g.department; }
      const key = 't:' + g.key;
      const members = (t.members || []).filter(e => e !== t.lead);
      out += `<section class="wp-card wp-team"><header class="wp-team-head">
          <div class="wp-team-name"><h3>${H(g.name)}</h3>${c.myTeams.has(g.key) ? `<span class="om-badge wp-mine">${H(w(c.leadTeams.has(g.key) ? 'r_lead' : 'r_member'))}</span>` : ''}</div>
          ${t.lead || members.length ? `<div class="wp-team-people">${t.lead ? faceHtml(c, t.lead, 'lead') + `<span class="wp-lead-name">${H(personName(t.lead))}</span>` : ''}${members.length ? `<span class="om-faces wp-faces">${members.slice(0, 6).map(e => faceHtml(c, e)).join('')}${members.length > 6 ? `<span class="wp-face-more">+${members.length - 6}</span>` : ''}</span>` : ''}</div>` : ''}
        </header><div class="wp-chips">${chipsHtml(g.items, key)}</div>${openLists(c, g.items, key, { owner: true })}</section>`;
    }
    return out;
  }
  function personView(c, rows, lens){
    const byOwner = new Map();
    for (const it of rows) { const k = it.owner || ''; if (!byOwner.has(k)) byOwner.set(k, []); byOwner.get(k).push(it); }
    // Everyone in the teams in view, also when they have nothing yet.
    const inView = t => lens === 'org' || (lens === 'team' ? c.myTeams.has(t.key) : c.myDepts.has(t.department));
    const placed = new Map();
    for (const t of c.teams.values()) {
      if (!inView(t)) continue;
      for (const e of [t.lead, ...t.members].filter(Boolean)) if (!placed.has(e) && primaryTeam(c, e) === t.key) placed.set(e, t.key);
    }
    for (const e of byOwner.keys()) if (e && !placed.has(e)) placed.set(e, primaryTeam(c, e));
    const teams = new Map();
    for (const [e, k] of placed) { if (!teams.has(k)) teams.set(k, []); teams.get(k).push(e); }
    const keys = [...teams.keys()].sort((a, b) => (!a) - (!b) || (c.myTeams.has(b) - c.myTeams.has(a)) || teamName(c, a).localeCompare(teamName(c, b)));
    const person = (e, teamKey) => {
      const mine = byOwner.get(e) || [];
      const p = c.people.get(e) || {};
      const lead = !!teamKey && (c.teams.get(teamKey) || {}).lead === e;
      const key = 'p:' + e;
      return `<div class="wp-person"><div class="wp-person-head">${faceHtml(c, e, lead ? 'lead' : '')}
          <div class="wp-person-main"><div class="wp-person-name">${H(personName(e))}${e === c.email ? ` <span class="wp-you">${H(w('you'))}</span>` : ''}</div>
          <div class="wp-row-sub">${H([p.title, teamKey ? w(lead ? 'r_lead' : 'r_member') : ''].filter(Boolean).join(' · '))}</div></div>
          <div class="wp-chips">${chipsHtml(mine, key)}</div></div>${openLists(c, mine, key, {})}</div>`;
    };
    let out = keys.map(k => `<section class="wp-card wp-team"><header class="wp-team-head"><div class="wp-team-name"><h3>${H(k ? teamName(c, k) : w('not_placed'))}</h3>${k && deptName(c, deptKey(c, k)) !== teamName(c, k) ? `<span class="wp-team-dept">${H(deptName(c, deptKey(c, k)))}</span>` : ''}</div></header>
        ${teams.get(k).sort((a, b) => ((c.teams.get(k) || {}).lead === b) - ((c.teams.get(k) || {}).lead === a) || a.localeCompare(b)).map(e => person(e, k)).join('')}</section>`).join('');
    const orphans = byOwner.get('') || [];
    if (orphans.length) out += `<section class="wp-card wp-team"><header class="wp-team-head"><div class="wp-team-name"><h3>${H(w('no_owner'))}</h3></div></header><div class="wp-chips">${chipsHtml(orphans, 'p:')}</div>${openLists(c, orphans, 'p:', { team: true })}</section>`;
    return out;
  }
  function greeting(){ const h = new Date().getHours(); return w(h < 12 ? 'morning' : h < 18 ? 'afternoon' : 'evening'); }
  function heroHtml(c){
    const first = personName(c.email).split(' ')[0];
    const job = ((Wp.intake || {}).role || {}).title || (c.people.get(c.email) || {}).title || '';
    const rs = roles(c);
    return `<section class="wp-hero">${faceHtml(c, c.email, 'wp-avatar')}
      <div class="wp-hero-main"><h2>${H(greeting())}, ${H(first)}</h2>
        <p>${H(job || w('intro'))}</p>
        ${rs.length ? `<div class="wp-badges">${rs.map(r => `<span class="om-badge wp-role ${r.cls}">${H(r.label)}</span>`).join('')}</div>` : ''}</div></section>`;
  }
  function allowed(panel) { return !!window.SynthPulseWorkspace?.allowed(panel); }
  function recentHtml(title, target, items, empty) {
    return `<section class="wp-card"><header class="wp-card-head"><h3>${H(w(title))}</h3><button type="button" class="om-link wp-all" data-wp-go="${target}">${H(w('all'))}</button></header>${items || '<p class="wp-no-recent">'+H(w(empty))+'</p>'}</section>`;
  }
  function recentChats() {
    const chats = window.ConversationWorkbench?.recent() || [];
    return recentHtml('recent_chats', 'conversations', chats.map(s => rowHtml(ctxCache, {kind:'conversation', id:s.id,
      title:s.title, sub:[s.bot, s.at ? _formatRelativeSessionTime(s.at) : ''].filter(Boolean).join(' · ')})).join(''), 'no_chats');
  }
  function refreshRecent() {
    const node = Wp.body?.querySelector('[data-wp-recent-chats]');
    if (node && allowed('chat')) {
      const focused = node.contains(document.activeElement) ? document.activeElement.dataset : null;
      node.innerHTML = recentChats();
      if (focused) [...node.querySelectorAll('button')].find(b=>b.dataset.wpGo === focused.wpGo && b.dataset.wpId === focused.wpId)?.focus({preventScroll:true});
    }
  }
  function activity(rows) {
    const jobs = rows.filter(r=>r.kind === 'task' && r.at > 0).sort((a,b)=>b.at-a.at).slice(0,4);
    const runs = jobs.map(r=>rowHtml(ctxCache, {...r, status:undefined, kind:'run',
      sub:[_formatRelativeSessionTime(r.at*1000), r.runStatus].filter(Boolean).join(' · ')})).join('');
    return `<div class="wp-grid wp-activity">${allowed('chat') ? '<div data-wp-recent-chats>'+recentChats()+'</div>' : ''}
      ${allowed('tasks') ? recentHtml('recent_runs','task',runs,Wp.runsReadable ? 'no_runs' : 'failed') : ''}</div>`;
  }
  function shortcuts() {
    const actions = [['projects','projects','projects'],['profiles','chart','chart'],['tasks','agenda','agenda']];
    return '<div class="wp-shortcuts">'+actions.filter(([panel])=>allowed(panel)).map(([,target,label])=>
      `<button type="button" class="sm-btn" data-wp-go="${target}">${H(w(label))}</button>`).join('')+
      `<button type="button" class="sm-btn" data-wp-go="navigate">${H(t('workspace_navigation_search'))}</button></div>`;
  }
  function paint(force){
    const body = Wp.body, c = ctxCache;
    if (!body || !body.isConnected || !c || !Wp.data) return;
    // The Studio body is shared by every hub: never paint over another hub that opened meanwhile.
    if (!force && !(body.firstElementChild && body.firstElementChild.classList.contains('wp'))) return;
    const lens = homeLens(c);
    const rows = Wp.data.filter(it => match(c, it, lens) && (it.kind !== 'task' || allowed('tasks')) && (it.kind !== 'bot' || allowed('profiles')));
    const group = lens === 'mine' ? 'kind' : Wp.group;
    const n = Object.fromEntries(KINDS.map(k => [k, rows.filter(it => it.kind === k).length]));
    const groupSeg = lens === 'mine' ? '' : `<div class="om-seg" role="group" aria-label="${H(w('group'))}">${['kind', 'team', 'person'].map(g => `<button type="button" aria-pressed="${g === group}" data-wp-group="${g}">${H(w('g_' + g))}</button>`).join('')}</div>`;
    let main;
    if (!rows.length) {
      const wider = lenses(c).find(l => l !== lens && Wp.data.some(it => match(c, it, l)));
      main = `<div class="wp-empty"><span class="wp-empty-art" aria-hidden="true"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l2.2 5.3L20 9.5l-4.3 3.9L17 19l-5-2.9L7 19l1.3-5.6L4 9.5l5.8-1.2z"/></svg></span>
        <h3>${H(w('empty_h'))}</h3><p>${H(w(lens === 'mine' ? 'empty_mine' : 'empty_scope'))}</p>
        ${wider ? `<button type="button" class="sm-btn" data-sp-lens="${wider}" data-sp-lens-surface="home">${H(w('empty_peek', { l: lensLabel(c, wider).toLowerCase() }))}</button>` : ''}</div>`;
    }
    else if (group === 'team') main = `<div class="wp-stack">${teamView(c, rows)}</div>`;
    else if (group === 'person') main = `<div class="wp-stack">${personView(c, rows, lens)}</div>`;
    else main = `<div class="wp-grid">${KINDS.filter(k => n[k]).map(k => kindCard(c, k, rows.filter(it => it.kind === k), 'k:' + lens + ':' + k, { team: lens !== 'mine' && k !== 'bot', owner: lens !== 'mine' })).join('')}</div>`;
    body.innerHTML = `<div class="wp" data-tour="studio-home">${heroHtml(c)}
      ${Wp.setup}
      <div class="wp-bar">${barHtml(c, 'home', Wp.data, lens)}${groupSeg}</div>
      <div class="wp-kpis">${KINDS.filter(k => k === 'task' ? allowed('tasks') : k !== 'bot' || allowed('profiles')).map(k => `<button type="button" class="wp-kpi ${n[k] ? '' : 'zero'}" title="${H(w('kpi_hint', { k: w('k_' + k) }))}" data-wp-go="${k}"${k === 'usecase' ? ' data-tour="studio-usecases"' : ''}><span class="wp-kpi-label"><span class="wp-kind-dot k-${k}" aria-hidden="true"></span>${H(short(k))}</span><span class="wp-kpi-value">${n[k]}</span></button>`).join('')}</div>
      ${shortcuts()}
      <details class="wp-card wp-destinations"><summary>${H(w('navigate'))}</summary><div class="wp-shortcuts">${(window.WorkspaceDestinations?.destinations() || []).map(d=>
        `<button type="button" class="sm-btn" data-wp-destination="${H(d.panel)}">${H(d.label)}</button>`).join('')}</div></details>
      ${activity(rows)}${main}</div>`;
  }
  async function render(body, opts){
    Wp.body = body; Wp.setup = (opts && opts.setup) || '';
    const token = ++Wp.token;
    if (!body.dataset.wpBound) {
      body.dataset.wpBound = '1';
      body.addEventListener('click', onClick);
    }
    if (ctxCache && Wp.data) paint(true); else body.innerHTML = `<div class="wp"><div class="wp-skeleton" aria-busy="true" aria-label="${H(w('loading'))}"><span></span><span></span><span></span></div></div>`;
    try { const c = await loadCtx(Date.now() - Wp.at > 30000); await loadData(c); }
    catch (_e) { if (token === Wp.token && body.isConnected && body.firstElementChild && body.firstElementChild.classList.contains('wp')) body.innerHTML = `<div class="om-empty wp-failed"><h3>${H(w('failed'))}</h3><div class="om-actions"><button type="button" class="sm-btn" data-wp-retry>${H(w('retry'))}</button></div></div>`; return; }
    if (token === Wp.token) paint();
  }
  // Opens on "mine". Someone who owns nothing yet and never chose a lens starts on
  // the first lens that has something to show, so the first visit is never an empty page.
  function homeLens(c){
    const lens = getLens(c, 'home', 'mine');
    if (lens !== 'mine' || store.get('sp-lens-home', '') || !Wp.data) return lens;
    if (Wp.data.some(it => match(c, it, 'mine'))) return lens;
    return lenses(c).find(l => l !== 'mine' && Wp.data.some(it => match(c, it, l))) || lens;
  }
  async function go(kind){
    const c = ctxCache;
    const lens = c ? homeLens(c) : 'org';
    if (kind === 'conversations' && allowed('chat')) return window.ConversationWorkbench.show();
    if (kind === 'projects' && allowed('projects')) return switchPanel('projects');
    if (kind === 'chart' && allowed('profiles')) {
      await switchPanel('profiles'); if (_currentPanel === 'profiles') openBotOrgChart(); return;
    }
    if (kind === 'agenda' && allowed('tasks')) {
      await switchPanel('tasks'); if (_currentPanel === 'tasks' && ['empty','read'].includes(_cronMode)) window.CronAgenda.open(); return;
    }
    if (kind === 'navigate') return document.getElementById('btnWorkspaceNavigator')?.click();
    if (kind === 'task' && allowed('tasks')) { if (typeof switchPanel === 'function') await switchPanel('tasks'); return; }
    if (kind === 'bot' && allowed('profiles')) { if (typeof switchPanel === 'function') await switchPanel('profiles'); return; }
    const tab = { usecase: 'usecases', playbook: 'playbooks', prompt: 'prompts', backlog: 'backlog' }[kind];
    if (tab === 'usecases') store.set('sp-lens-usecases', lens);
    if (tab && window.SPStudio) window.SPStudio.setTab(tab);
  }
  async function openItem(b){
    const kind = b.dataset.wpOpen, id = b.dataset.wpId;
    if (kind === 'usecase' && window.SPStudio) return window.SPStudio.openUseCase(id);
    if (kind === 'playbook' && window.SPLibrary) return window.SPLibrary.openPlaybook(id);
    if ((kind === 'backlog' || kind === 'prompt') && window.SPStudio) return window.SPStudio.setTab(kind === 'backlog' ? 'backlog' : 'prompts');
    if (kind === 'conversation' && allowed('chat')) return window.ConversationWorkbench.openRecent(id);
    if ((kind === 'task' || kind === 'run') && allowed('tasks') && typeof openCronFromBot === 'function') return openCronFromBot(id, b.dataset.wpStore === 'default' ? '' : b.dataset.wpStore, b.dataset.wpBot);
    if (kind === 'bot' && allowed('profiles') && typeof switchPanel === 'function') {
      await switchPanel('profiles');
      if (typeof openProfileDetail === 'function') openProfileDetail(id);
    }
  }
  function onClick(ev){
    const b = ev.target.closest('button');
    if (!b || !Wp.body || !Wp.body.contains(b)) return;
    if (b.dataset.wpGroup) { Wp.group = b.dataset.wpGroup; store.set('sp-wp-group', Wp.group); paint(); return; }
    if (b.dataset.wpToggle) { const k = b.dataset.wpToggle; if (Wp.open.has(k)) Wp.open.delete(k); else Wp.open.add(k); paint(); return; }
    if (b.dataset.wpMore) { const k = b.dataset.wpMore; if (Wp.more.has(k)) Wp.more.delete(k); else Wp.more.add(k); paint(); return; }
    if (b.dataset.wpDestination) { window.WorkspaceDestinations.open(b.dataset.wpDestination); return; }
    if (b.dataset.wpGo) { go(b.dataset.wpGo); return; }
    if (b.dataset.wpOpen) { openItem(b); return; }
    if (b.hasAttribute('data-wp-retry')) { render(Wp.body); }
  }
  document.addEventListener('sp-lens-change', ev => { if (ev.detail && ev.detail.surface === 'home') paint(); });

  window.SPWorkplace = { render, refreshRecent, title: () => w('title'), invalidate: () => { Wp.at = 0; ctxAt = 0; } };
})();
