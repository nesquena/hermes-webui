// ── Governance panel ──────────────────────────────────────────────────────
// Admin UI for the dashboard governance policy (roles, groups, users, access
// preview, audit tail). All data comes from /api/governance/* (see
// api/governance_api.py). Visibility of the nav buttons is cosmetic only:
// the server enforces every permission independently of what this file shows.
//
// Mutations are optimistic-concurrency guarded: every POST carries the last
// fetched policy etag in an If-Match header; a 412 response means the policy
// changed elsewhere and triggers a reload toast + refetch.
// CSRF: the global fetch wrapper in index.html injects X-Hermes-CSRF-Token on
// every mutation automatically; this file never sets that header manually.

let _govEtag = null;
let _govTab = 'overview';
let _govEditingUser = null;   // email currently being edited, null = create mode
let _govEditingGroup = null;  // group name currently being edited, null = create mode
let _govSavingUser = false;

function _govEsc(s) {
  if (typeof _escHtml === 'function') return _escHtml(s);
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function _govT(key, fallback) {
  const val = (typeof t === 'function') ? t(key) : key;
  return (val && val !== key) ? val : fallback;
}

function _govIsAdmin(me) {
  if (!me) return false;
  if (me.is_bootstrap_admin) return true;
  const perms = Array.isArray(me.permissions) ? me.permissions : [];
  return perms.includes('*') || perms.includes('governance:read') || perms.includes('governance:write');
}

/** Show/hide every governance nav button. Cosmetic only; server enforces. */
function govApplyVisibility(me) {
  const show = _govIsAdmin(me);
  document.querySelectorAll('[data-panel="governance"]').forEach(btn => {
    btn.style.display = show ? '' : 'none';
  });
}

async function _govFetchMe() {
  try {
    window.__GOV_ME__ = await api('/api/governance/me', { redirect401: false, timeoutToast: false, timeoutMs: 15000 });
  } catch (e) {
    window.__GOV_ME__ = null;
  }
  if (typeof refreshChatBots === 'function') refreshChatBots();
  return window.__GOV_ME__;
}

/**
 * Panel entry point, called by switchPanel. Returns false when the caller is
 * not a governance admin so panels.js can fall back to the chat panel.
 */
async function loadGovernance() {
  const me = await _govFetchMe();
  govApplyVisibility(me);
  if (!_govIsAdmin(me)) return false;
  _govEnsureWorkspacesTab();
  _govRefreshApprovalsBadge();
  await _govSwitchTab(_govTab || 'overview');
  return true;
}

async function _govSwitchTab(name) {
  _govEnsureWorkspacesTab();
  const tab = ['overview', 'users', 'groups', 'workspaces', 'approvals', 'integrations', 'preview', 'audit'].includes(name) ? name : 'overview';
  _govTab = tab;
  document.querySelectorAll('#mainGovernance .gov-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.govTab === tab);
  });
  document.querySelectorAll('#mainGovernance .gov-tab-pane').forEach(pane => {
    pane.classList.toggle('active', pane.id === 'govPane' + tab.charAt(0).toUpperCase() + tab.slice(1));
  });
  if (tab === 'overview') await _govLoadOverview();
  if (tab === 'users') await _govLoadUsers();
  if (tab === 'groups') await _govLoadGroups();
  if (tab === 'workspaces') await _govLoadWorkspaces();
  if (tab === 'approvals') await _govLoadApprovals();
  if (tab === 'integrations') await _govLoadIntegrations();
  if (tab === 'audit') await _govLoadAudit();
  // preview tab is form-driven, but its email/groups pickers need the catalogs
  if (tab === 'preview') _govEnsureCatalogs().then(_govFillCatalogDatalists).catch(() => {});
}

async function _govRefreshTab() {
  try { await _govSwitchTab(_govTab); } catch (e) { /* rendered by the tab loader */ }
}

// ── HTTP helpers ──────────────────────────────────────────────────────────

async function _govPost(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'If-Match': _govEtag || '' },
    body: JSON.stringify(body),
    redirect401: false,
  });
}

/** Shared 412 handler: policy changed elsewhere, reload and let the user retry. */
function _govHandleConflict(e) {
  if (e && Number(e.status) === 412) {
    if (typeof showToast === 'function') showToast(_govT('governance_conflict_reload', 'Policy changed elsewhere, reloading'), 4000, 'error');
    _govRefreshTab();
    return true;
  }
  return false;
}

function _govError(e, containerId) {
  const el = $(containerId);
  let msg = (e && e.message) ? e.message : 'request failed';
  // Governance 403s carry structured reason/resource (attached by api());
  // surface them instead of the bare "forbidden".
  if (e && Number(e.status) === 403 && (e.reason || e.resource)) {
    msg = 'Access restricted' + (e.resource ? ': ' + e.resource : '') + (e.reason ? ' (' + e.reason + ')' : '');
  }
  if (el) el.innerHTML = '<div class="gov-error">' + _govEsc(msg) + '</div>';
}

function _govCsv(value) {
  return String(value || '').split(',').map(s => s.trim()).filter(Boolean);
}

// ── Overview tab ──────────────────────────────────────────────────────────

async function _govLoadOverview() {
  const el = $('govPaneOverview');
  if (!el) return;
  el.innerHTML = '<div class="gov-muted" data-i18n="loading">' + _govT('loading', 'Loading...') + '</div>';
  let data;
  try {
    data = await api('/api/governance/policy', { redirect401: false });
  } catch (e) {
    _govError(e, 'govPaneOverview');
    return;
  }
  _govEtag = data.etag || null;
  const policy = data.policy || {};
  const mode = String(policy.mode || 'off');
  const counts = {
    roles: Object.keys(policy.roles || {}).length,
    groups: Object.keys(policy.groups || {}).length,
    users: Object.keys(policy.users || {}).length,
    admins: Array.isArray(policy.bootstrap_admins) ? policy.bootstrap_admins.length : 0,
  };
  let denials24h = null;
  try {
    const audit = await api('/api/governance/audit?limit=200', { redirect401: false, timeoutToast: false });
    const cutoff = Date.now() - 24 * 3600 * 1000;
    denials24h = (audit.events || []).filter(ev => {
      if (ev.event !== 'deny' && ev.event !== 'would_deny') return false;
      const ts = Date.parse(ev.ts || '');
      return Number.isFinite(ts) && ts >= cutoff;
    }).length;
  } catch (e) { /* audit permission may be missing; leave blank */ }
  const stat = (label, value, iconSvg) => (
    '<div class="gov-stat">' +
      '<div class="gov-stat-value">' + _govEsc(String(value)) + '</div>' +
      '<div class="gov-stat-label">' + iconSvg + ' ' + _govEsc(label) + '</div></div>'
  );
  const svg = (path) => '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px" aria-hidden="true">' + path + '</svg>';
  const iconRole = svg('<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>');
  const iconGroup = svg('<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>');
  const iconUser = svg('<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>');
  const iconAdmin = svg('<path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/>');
  const iconDeny = svg('<circle cx="12" cy="12" r="10"/><path d="M4.93 4.93l14.14 14.14"/>');
  const modeLabels = {
    enforce: _govT('governance_mode_enforce_desc', 'Policies are actively enforced: access outside the policy is denied.'),
    report_only: _govT('governance_mode_report_desc', 'Report-only: violations are logged, but access is not blocked yet.'),
    off: _govT('governance_mode_off_desc', 'Governance is switched off — every signed-in user has full access.'),
  };
  el.innerHTML =
    '<div class="gov-overview-head">' +
      '<span class="gov-mode-badge gov-mode-' + _govEsc(mode) + '">' + _govT('governance_mode', 'Mode') + ': ' + _govEsc(mode) + '</span>' +
      '<span class="gov-muted">' + _govEsc(modeLabels[mode] || modeLabels.off) + '</span>' +
    '</div>' +
    '<div class="gov-stat-grid">' +
      stat(_govT('governance_stat_roles', 'Roles'), counts.roles, iconRole) +
      stat(_govT('governance_stat_groups', 'Groups'), counts.groups, iconGroup) +
      stat(_govT('governance_stat_users', 'Users'), counts.users, iconUser) +
      stat(_govT('governance_stat_admins', 'Bootstrap admins'), counts.admins, iconAdmin) +
      stat(_govT('governance_stat_denials', 'Denials (24h)'), denials24h === null ? '?' : denials24h, iconDeny) +
    '</div>' +
    '<div class="gov-muted gov-overview-note">' + _govT('governance_overview_note',
      'Policy file: dashboard-governance.yaml. Denials include report-only would-deny events.') + '</div>';
}

// ── Users tab ─────────────────────────────────────────────────────────────

async function _govLoadUsers(expectedDraft) {
  const el = $('govPaneUsers');
  if (!el) return;
  if (!expectedDraft) el.innerHTML = '<div class="gov-muted">' + _govT('loading', 'Loading...') + '</div>';
  let data;
  try {
    data = await api('/api/governance/users', { redirect401: false });
  } catch (e) {
    if (expectedDraft) return;
    _govError(e, 'govPaneUsers');
    return;
  }
  // A save may finish after the administrator has selected or edited another
  // draft. Never replace that draft (or advance its concurrency token).
  if (expectedDraft && expectedDraft !== _govUserDraftSignature()) return;
  _govEtag = data.etag || _govEtag;
  const users = data.users || {};
  const rows = Object.keys(users).sort().map(email => {
    const entry = users[email] || {};
    return '<tr>' +
      '<td>' + _govEsc(email) + '</td>' +
      '<td class="gov-policy-cell">' + _govPolicyChips(_govUserPolicySummary(entry)) + '</td>' +
      '<td>' + _govEsc((entry.roles || []).join(', ')) + '</td>' +
      '<td>' + _govEsc((entry.groups || []).join(', ')) + '</td>' +
      '<td class="gov-row-actions">' +
        '<button type="button" class="gov-btn" onclick="_govEditUser(' + _govEsc(JSON.stringify(email)) + ')">' + _govT('governance_edit', 'Edit') + '</button>' +
        '<button type="button" class="gov-btn danger" onclick="_govDeleteUser(' + _govEsc(JSON.stringify(email)) + ')">' + _govT('governance_delete', 'Delete') + '</button>' +
      '</td></tr>';
  }).join('');
  window.__GOV_USERS__ = users;
  window.__GOV_BOOTSTRAP_ADMINS__ = data.bootstrap_admins || [];
  el.innerHTML =
    '<div class="gov-user-table-wrap"><table class="gov-table"><thead><tr>' +
      '<th>' + _govT('governance_col_email', 'Email') + '</th>' +
      '<th>' + _govT('governance_user_policy', 'User policy') + '</th>' +
      '<th>' + _govT('governance_col_roles', 'Roles') + '</th>' +
      '<th>' + _govT('governance_col_groups', 'Groups') + '</th><th></th>' +
    '</tr></thead><tbody>' + (rows || '<tr><td colspan="5" class="gov-muted">' + _govT('governance_no_users', 'No user entries in the policy.') + '</td></tr>') + '</tbody></table></div>' +
    '<div class="gov-form" id="govUserForm">' +
      '<div class="gov-form-section">' +
        '<div class="gov-form-title" id="govUserFormTitle">' + _govT('governance_user_add', 'Add user') + '</div>' +
        '<div class="gov-form-row"><label for="govUserEmail">' + _govT('governance_col_email', 'Email') + '</label>' +
          '<input id="govUserEmail" type="hidden" data-sp-picker data-sp-source="people" data-sp-custom="1" placeholder="' + _govT('picker_pick_person', 'Pick a person') + '" oninput="_govUserPolicyChanged()"></div>' +
        _govChipFieldHtml('govUserRolesSel', _govT('governance_roles_csv', 'Roles'), 'govDlRolesUsr', _govT('governance_pick_role', 'pick a role')) +
        _govChipFieldHtml('govUserGroupsSel', _govT('governance_groups_csv', 'Groups'), 'govDlGroupsUsr', _govT('governance_pick_group', 'pick a group')) +
        '<datalist id="govDlRolesUsr"></datalist><datalist id="govDlGroupsUsr"></datalist>' +
      '</div>' +
      _govUserPolicyHtml() +
      '<div class="gov-form-section">' +
        '<div class="gov-form-title">' + _govT('governance_grants_title', 'Capabilities & grants') + '</div>' +
        '<div class="gov-muted" style="margin-bottom:10px">' + _govT('governance_user_grants_note',
          'Whitelist users receive only explicitly allowed capabilities within their assigned access. Blacklist users receive assigned access except the denied items below.') + '</div>' +
        _govUserExtraFieldsHtml('grant') +
        _govChipFieldHtml('govUserSkillsView', _govT('governance_grants_skills_view', 'Skills view'), 'govDlSkills', 'my-skill, *') +
        _govChipFieldHtml('govUserSkillsLoad', _govT('governance_grants_skills_load', 'Skills load'), 'govDlSkills', 'my-skill') +
        _govChipFieldHtml('govUserSkillsManage', _govT('governance_grants_skills_manage', 'Skills manage'), 'govDlSkills', 'my-skill') +
        _govChipFieldHtml('govUserMcpServers', _govT('governance_grants_mcp_servers', 'MCP servers'), 'govDlMcp', 'notion, playwright') +
        _govMcpToolMapHtml('govUserMcpTools', 'MCP tools to allow', false, 'govDlMcp') +
        _govChipFieldHtml('govUserCliCommands', _govT('governance_grants_cli_commands', 'CLI commands'), 'govDlCli', 'git, gh') +
        _govChipFieldHtml('govUserCliApproval', _govT('governance_grants_cli_approval', 'CLI commands requiring approval'), 'govDlCli', 'rm, sudo') +
      '</div>' +
      '<div class="gov-form-section">' +
        '<div class="gov-form-title">' + _govT('governance_deny_title', 'Off-toggles (deny)') + '</div>' +
        '<div class="gov-muted">' + _govT('governance_deny_note',
          'Denied items override allowed capabilities, including wildcard (*) grants. Automatic approval cannot override a denial.') + '</div>' +
        _govUserExtraFieldsHtml('deny') +
        _govChipFieldHtml('govUserDenySkills', _govT('governance_deny_skills_view_load', 'Skills view and load off'), 'govDlSkills', 'my-skill') +
        _govChipFieldHtml('govUserDenySkillsManage', _govT('governance_deny_skills_manage', 'Skills management off'), 'govDlSkills', 'my-skill') +
        _govChipFieldHtml('govUserDenyCli', _govT('governance_deny_cli', 'CLI commands off'), 'govDlCli', 'rm') +
        _govChipFieldHtml('govUserDenyMcp', _govT('governance_deny_mcp', 'MCP servers off'), 'govDlMcp', 'playwright') +
        _govMcpToolMapHtml('govUserDenyMcpTools', 'MCP tools to deny', true, 'govDlMcp') +
      '</div>' +
      '<div id="govUserEffective"></div>' +
      '<datalist id="govDlSkills"></datalist>' +
      '<datalist id="govDlCli"></datalist>' +
      '<datalist id="govDlMcp"></datalist>' +
      '<div class="gov-form-actions">' +
        '<button id="govUserSave" type="button" class="gov-btn primary" onclick="_govSaveUser()">' + _govT('governance_save', 'Save user') + '</button>' +
        '<button type="button" class="gov-btn" onclick="_govResetUserForm()">' + _govT('governance_cancel', 'Cancel') + '</button>' +
      '</div>' +
    '</div>';
  if (window.SpPicker) SpPicker.mountAll(el);
  _govResetUserForm();
  _govEnsureCatalogs().then(_govFillCatalogDatalists).catch(() => {});
}

// Per-server MCP tool rules. Server grants and tool grants are separate;
// adding a row never inserts a wildcard or changes the server allowlist.
const _govMcpToolMaps = {};
let _govMcpToolRowSequence = 0;

function _govMcpToolMapHtml(id, label, deny, datalistId) {
  const note = deny
    ? 'Block named tools for a server even when other grants allow them. Enter * only to block every tool. An empty list blocks no tools.'
    : 'Allow named tools for each server. Also allow the server above. Enter * only to allow every tool. An empty list grants no tools; access from other roles or groups still applies.';
  return '<div class="gov-form-section" id="' + id + '" data-mcp-tool-map="' + id + '" data-server-list="' + datalistId + '">' +
    '<div class="gov-form-title">' + _govEsc(label) + '</div>' +
    '<p class="gov-muted">' + _govEsc(note) + '</p>' +
    '<div data-mcp-tool-rows></div>' +
    '<button type="button" class="gov-btn" id="' + id + 'Add" onclick="_govMcpToolMapAdd(\'' + id + '\')">Add server tool rule</button>' +
    '</div>';
}

function _govMcpToolNames(value) {
  const values = Array.isArray(value) ? value : typeof value === 'string' ? [value]
    : value && typeof value === 'object' ? Object.keys(value) : value == null ? [] : [value];
  return [...new Set(values.map(item => String(item).trim()).filter(Boolean))];
}

function _govMcpToolMapSet(id, mapping) {
  const copy = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  const previous = _govMcpToolMaps[id];
  if (previous) previous.rows.forEach(row => { delete _govChipState[row.id]; });
  const state = {original: copy(mapping), rows: [], initial: ''};
  _govMcpToolMaps[id] = state;
  const host = $(id);
  if (host) host.querySelector('[data-mcp-tool-rows]').replaceChildren();
  if (mapping && typeof mapping === 'object' && !Array.isArray(mapping)) {
    Object.entries(mapping).forEach(([server, tools]) => _govMcpToolMapAdd(id, server, tools));
  }
  state.initial = _govMcpToolMapSignature(id);
}

function _govMcpToolMapAdd(id, server = '', tools) {
  const host = $(id), state = _govMcpToolMaps[id];
  if (!host || !state) return;
  const rowId = id + 'Rule' + (++_govMcpToolRowSequence);
  const names = _govMcpToolNames(tools);
  const row = {id: rowId, originalServer: server, originalNames: names,
    original: tools === undefined ? undefined : JSON.parse(JSON.stringify(tools))};
  state.rows.push(row);
  const node = document.createElement('div');
  node.className = 'gov-form-section gov-mcp-tool-row'; node.dataset.mcpToolRule = '';
  node.innerHTML = '<div class="gov-form-row"><label for="' + rowId + 'Server">Server</label>' +
    '<input id="' + rowId + 'Server" data-mcp-tool-server type="hidden" data-sp-picker data-sp-source="gov:mcp" data-sp-custom="1" placeholder="' + _govT('picker_pick_server', 'Pick a server') + '"></div>' +
    _govChipFieldHtml(rowId, 'Tools', rowId + 'Options', 'Choose or enter a tool name') +
    '<datalist id="' + rowId + 'Options"></datalist>' +
    '<button type="button" class="gov-btn" data-mcp-tool-remove>Remove server rule</button>';
  host.querySelector('[data-mcp-tool-rows]').appendChild(node);
  row.node = node;
  if (window.SpPicker) {
    SpPicker.sources['gov:mcptools:' + rowId] = () => _govEnsureCatalogs().then(c => {
      const server = String(($(rowId + 'Server') || {}).value || '').trim();
      const names = (c.mcpTools || {})[server] || [];
      return [{ value: '*', label: '*', hint: _govT('picker_all_tools', 'every tool') }].concat(names);
    });
    SpPicker.mountAll(node);
  }
  const input = $(rowId + 'Server'); input.value = server;
  input.addEventListener('input', () => _govMcpToolMapOptions(id));
  node.querySelector('[data-mcp-tool-remove]').addEventListener('click', () => {
    state.rows = state.rows.filter(item => item !== row);
    delete _govChipState[row.id]; node.remove();
  });
  _govChipsSet(rowId, names);
  _govMcpToolMapOptions(id);
  if (!server && input.type !== 'hidden') input.focus();
}

function _govMcpToolMapOptions() { /* tool lists resolve lazily via the picker source */ }

function _govMcpToolMapSignature(id) {
  const state = _govMcpToolMaps[id];
  return JSON.stringify(state ? state.rows.map(row => [($(row.id + 'Server') || {}).value || '', _govChipsGet(row.id)]) : []);
}

function _govMcpToolMapsValid(ids) {
  for (const id of ids) {
    const state = _govMcpToolMaps[id];
    if (!state) continue;
    const servers = new Set();
    for (const row of state.rows) {
      _govChipCommit(row.id);
      const input = $(row.id + 'Server'), server = (input.value || '').trim();
      const names = _govChipsGet(row.id);
      let message = '';
      if (!server && names.length) message = 'Choose a server for these tool names.';
      else if (server && servers.has(server)) message = 'Use one tool rule per server. Combine the tool names in the existing rule.';
      if (message) {
        if (typeof showToast === 'function') showToast(message, 4000, 'error');
        input.focus(); return false;
      }
      if (server) servers.add(server);
    }
  }
  return true;
}

function _govCollectMcpToolMap(id, target) {
  const state = _govMcpToolMaps[id];
  if (!state) return;
  // Keep untouched legacy values (string, array or keyed object) exactly as
  // they arrived. Only an edited rule is normalized to the current list form.
  if (_govMcpToolMapSignature(id) === state.initial) return;
  const entries = [];
  state.rows.forEach(row => {
    const server = String(($(row.id + 'Server') || {}).value || '').trim();
    if (!server) return;
    const names = _govChipsGet(row.id);
    const unchanged = server === row.originalServer && JSON.stringify(names) === JSON.stringify(row.originalNames);
    entries.push([server, unchanged && row.original !== undefined ? row.original : names.slice()]);
  });
  if (entries.length) target.tools = Object.fromEntries(entries);
  else delete target.tools;
}


const _GOV_USER_GRANT_FIELDS = ['govUserSkillsView', 'govUserSkillsLoad', 'govUserSkillsManage', 'govUserMcpServers', 'govUserCliCommands', 'govUserCliApproval'];
const _GOV_USER_DENY_FIELDS = ['govUserDenySkills', 'govUserDenySkillsManage', 'govUserDenyCli', 'govUserDenyMcp'];

// One schema drives the extended allow/deny fields and their round-trip.
const _GOV_USER_ACCESS_FIELDS = [
  ['Permissions', ['permissions'], 'Permissions', 'chat:use, skills:read'],
  ['Profiles', ['profiles'], 'Profiles and bots', 'default'],
  ['Workspaces', ['workspaces'], 'Workspaces', 'team-workspace'],
  ['Routes', ['routes'], 'Application routes', '/api/chat'],
  ['Tools', ['tools', 'builtins'], 'Agent tools', 'read_file, terminal'],
  ['Toolsets', ['tools', 'toolsets'], 'Tool groups', 'file, terminal'],
  ['Models', ['models', 'models'], 'Models', 'provider/model'],
  ['Providers', ['models', 'providers'], 'Model providers', 'openai'],
  ['SettingsRead', ['settings', 'read'], 'Settings to view', 'appearance'],
  ['SettingsWrite', ['settings', 'write'], 'Settings to change', 'appearance'],
  ['FilesRead', ['files', 'read_roots'], 'Folders to read', '/workspace/team'],
  ['FilesWrite', ['files', 'write_roots'], 'Folders to write', '/workspace/team'],
  ['Workdirs', ['cli', 'workdir_roots'], 'Command working folders', '/workspace/team'],
  ['Environment', ['env', 'vars'], 'Environment variable names', 'SERVICE_API_KEY'],
];

function _govUserExtraId(kind, name) { return 'govUser' + (kind === 'deny' ? 'Deny' : 'Grant') + name; }

function _govUserExtraFieldsHtml(kind) {
  return '<details class="gov-access-details"><summary>' +
    _govT(kind === 'deny' ? 'governance_additional_denials' : 'governance_additional_grants',
      kind === 'deny' ? 'Application, tools and data to deny' : 'Application, tools and data to allow') + '</summary>' +
    _GOV_USER_ACCESS_FIELDS.map(([name, path, label, hint]) =>
      _govChipFieldHtml(_govUserExtraId(kind, name), _govT('governance_access_' + name.toLowerCase(), label), '', hint)
    ).join('') + '</details>';
}

function _govPolicyChips(summary) {
  return String(summary || '').split(' · ').filter(Boolean).map(function (part) {
    var inherit = /^keep existing/i.test(part);
    return '<span class="gov-chip ' + (inherit ? 'gov-chip-inherit' : 'gov-chip-policy') + '">' + _govEsc(part) + '</span>';
  }).join('');
}

function _govUserPolicySummary(entry) {
  const level = entry.access_level || _govT('governance_inherited_level', 'Existing roles');
  const mode = entry.access_mode || _govT('governance_legacy_policy', 'Existing policy');
  const approval = (entry.approval || {}).mode || _govT('governance_legacy_approval', 'Existing approval behavior');
  return level + ' · ' + mode + ' · ' + approval;
}

function _govUserPolicyHtml() {
  const option = (value, key, label) => '<option value="' + value + '">' + _govEsc(_govT(key, label)) + '</option>';
  return '<div class="gov-form-section" data-testid="user-governance-policy">' +
    '<div class="gov-form-title">' + _govT('governance_user_policy', 'User policy') + '</div>' +
    '<div class="gov-muted" id="govUserBootstrapHint" hidden></div>' +
    '<div class="gov-form-row"><label for="govUserAccessLevel">' + _govT('governance_access_level', 'Access level') + '</label>' +
    '<select id="govUserAccessLevel" onchange="_govUserPolicyChanged()">' +
      option('', 'governance_inherited_level', 'Keep existing roles') +
      option('user', 'governance_level_user', 'User') + option('elevated', 'governance_level_elevated', 'Elevated access') +
      option('admin', 'governance_level_admin', 'Admin') + '</select></div>' +
    '<div class="gov-form-row"><label for="govUserAccessMode">' + _govT('governance_access_mode', 'Access management') + '</label>' +
    '<select id="govUserAccessMode" aria-describedby="govUserPolicyHint" onchange="_govUserPolicyChanged()">' +
      option('', 'governance_legacy_policy', 'Keep existing policy') +
      option('whitelist', 'governance_mode_whitelist', 'Whitelist — deny until allowed') +
      option('blacklist', 'governance_mode_blacklist', 'Blacklist — allow except denied') + '</select>' +
      '<div id="govUserPolicyHint" class="gov-muted"></div></div>' +
    '<div class="gov-form-row"><label for="govUserApprovalMode">' + _govT('governance_approval_flow', 'Access request review') + '</label>' +
    '<select id="govUserApprovalMode" onchange="_govUserPolicyChanged()">' +
      option('', 'governance_legacy_approval', 'Keep existing approval behavior') +
      option('manual', 'governance_approval_manual', 'Manual approval') +
      option('automatic', 'governance_approval_automatic', 'Automatic AI approval and denial') + '</select></div>' +
    '<div class="gov-form-row" id="govUserApprovalPromptRow"><label for="govUserApprovalPrompt">' +
      _govT('governance_approval_prompt', 'Rules for this user’s access requests') + '</label>' +
      '<textarea id="govUserApprovalPrompt" rows="6" maxlength="8000" aria-describedby="govUserApprovalHint"' +
      ' placeholder="' + _govEsc(_govT('governance_approval_prompt_example', 'Allow reports in the assigned workspace. Deny sending customer data outside the company. Ask for manual review when the destination is unclear.')) + '"></textarea></div>' +
    '<div class="gov-muted" id="govUserApprovalHint"></div>' +
    '</div>';
}

function _govUserPolicyChanged() {
  const email = String(($('govUserEmail') || {}).value || _govEditingUser || '').trim().toLowerCase();
  const bootstrap = (window.__GOV_BOOTSTRAP_ADMINS__ || []).includes(email);
  ['govUserAccessLevel', 'govUserAccessMode', 'govUserApprovalMode', 'govUserApprovalPrompt'].forEach(id => {
    if ($(id)) $(id).disabled = bootstrap;
  });
  const bootstrapHint = $('govUserBootstrapHint');
  if (bootstrapHint) {
    bootstrapHint.hidden = !bootstrap;
    bootstrapHint.textContent = _govT('governance_bootstrap_hint', 'This is a recovery administrator. Per-user access and approval controls do not restrict this account.');
  }
  const mode = ($('govUserAccessMode') || {}).value;
  const hint = $('govUserPolicyHint');
  if (hint) hint.textContent = mode === 'whitelist'
    ? _govT('governance_whitelist_hint', 'Starts with no capabilities. Allow each needed capability below, within the assigned role and resource access.')
    : mode === 'blacklist'
      ? _govT('governance_blacklist_hint', 'Allows capabilities within the assigned role and resource access, except items denied below.')
      : _govT('governance_legacy_hint', 'Preserves the existing role, group and user grant behavior until you choose a mode.');
  const approvalMode = ($('govUserApprovalMode') || {}).value;
  const automatic = approvalMode === 'automatic';
  const row = $('govUserApprovalPromptRow');
  if (row) row.hidden = !automatic;
  const prompt = $('govUserApprovalPrompt');
  if (prompt) prompt.required = automatic;
  const approvalHint = $('govUserApprovalHint');
  if (approvalHint) approvalHint.textContent = automatic
    ? _govT('governance_automatic_hint', 'AI reviews requests for additional access using these rules. Explicit denials and role limits still apply. Uncertain or unavailable decisions stay pending for an administrator.')
    : approvalMode === 'manual'
      ? _govT('governance_manual_hint', 'Requests for additional access wait for an authorized reviewer. Already permitted actions continue normally; configured command approvals still apply.')
      : _govT('governance_legacy_approval_hint', 'Keeps the existing approval behavior until you choose a flow.');
}

function _govSetExtraUserFields(kind, mapping) {
  _GOV_USER_ACCESS_FIELDS.forEach(([name, path]) => {
    const value = path.reduce((node, key) => node && node[key], mapping || {});
    _govChipsSet(_govUserExtraId(kind, name), Array.isArray(value) ? value : []);
  });
}

function _govCollectExtraUserFields(kind, mapping) {
  const result = JSON.parse(JSON.stringify(mapping || {}));
  _GOV_USER_ACCESS_FIELDS.forEach(([name, path]) => {
    const id = _govUserExtraId(kind, name);
    _govChipCommit(id);
    const values = _govChipsGet(id);
    let node = result;
    for (const key of path.slice(0, -1)) {
      if (!node[key] || typeof node[key] !== 'object' || Array.isArray(node[key])) node[key] = {};
      node = node[key];
    }
    const leaf = path[path.length - 1];
    if (values.length) node[leaf] = values;
    else delete node[leaf];
    if (path.length === 2 && !Object.keys(result[path[0]]).length) delete result[path[0]];
  });
  return Object.keys(result).length ? result : null;
}

// ── Chip multi-select backed by the shared picker (static/sp-picker.js) ───
// Values live in the picker; input.value is the comma list, _govChipState
// mirrors it for code that compares snapshots. Nobody types: every field
// offers a searchable list from the live catalogs, custom entries only where
// the catalog cannot be complete (CLI commands, SSO groups, new users).

let _govChipState = {};

function _govChipsGet(id) {
  const el = $(id + 'Input');
  return el ? _govCsv(el.value) : (_govChipState[id] || []);
}

function _govChipsSet(id, values) {
  const seen = new Set();
  const vals = (values || []).map(v => String(v).trim()).filter(v => v && !seen.has(v) && seen.add(v));
  _govChipState[id] = vals;
  const el = $(id + 'Input');
  if (el) el.value = vals.join(', ');
  _govRenderChipField(id);
}

// datalist ids are the historical field keys; map them to picker sources
function _govSourceForDatalist(datalistId) {
  const key = String(datalistId || '');
  if (/^govDlSkills/.test(key)) return 'gov:skills';
  if (/^govDlMcp/.test(key)) return 'gov:mcp';
  if (/^govDlCli/.test(key)) return 'gov:cli';
  if (/^govDlRoles/.test(key)) return 'gov:roles';
  if (/^govDlGroups/.test(key)) return 'gov:groups';
  if (/^govDlSso/.test(key)) return 'gov:sso';
  if (/^govDlEmails/.test(key)) return 'people';
  if (/Options$/.test(key)) return 'gov:mcptools:' + key.replace(/Options$/, '');
  return key;
}

function _govChipFieldHtml(id, label, datalistId, placeholder) {
  const source = _govSourceForDatalist(datalistId);
  // Extended policy fields have no complete catalog (paths, models, env names).
  // Preserve their custom entry flow; catalog-backed fields stay restricted.
  const custom = (!source || source === 'gov:cli' || source === 'gov:sso' || source.indexOf('gov:mcptools:') === 0) ? '1' : '0';
  const hints = { 'gov:skills': _govT('picker_pick_skills', 'Pick skills'), 'gov:mcp': _govT('picker_pick_servers', 'Pick servers'), 'gov:cli': _govT('picker_pick_commands', 'Pick commands'),
    'gov:roles': _govT('picker_pick_roles', 'Pick roles'), 'gov:groups': _govT('picker_pick_groups', 'Pick groups'), 'gov:sso': _govT('picker_pick_sso', 'Pick SSO groups or people'), 'people': _govT('picker_pick_people', 'Pick people') };
  const hint = hints[source] || (source.indexOf('gov:mcptools:') === 0 ? _govT('picker_pick_tools', 'Pick tools') : _govT('picker_placeholder', 'Pick from the list'));
  return '<div class="gov-form-row"><label for="' + id + 'Input">' + _govEsc(label) + '</label>' +
    '<input id="' + id + 'Input" type="hidden" data-sp-picker data-sp-multi="1" data-sp-custom="' + custom + '" data-sp-source="' + _govEsc(source) + '" data-gov-chip="' + _govEsc(id) + '"' +
    ' placeholder="' + _govEsc(hint) + '"></div>';
}

function _govRenderChipField(id) {
  // deny fields drive the effective on/off view
  if (typeof _GOV_USER_DENY_FIELDS !== 'undefined' && _GOV_USER_DENY_FIELDS.includes(id)) _govRenderUserEffective();
}

function _govChipAdd(id, value) {
  const v = String(value || '').trim().replace(/,+$/, '').trim();
  if (!v) return;
  const values = _govChipsGet(id);
  if (!values.includes(v)) _govChipsSet(id, values.concat([v]));
}

function _govChipRemove(id, value) {
  _govChipsSet(id, _govChipsGet(id).filter(v => v !== value));
}

// Picker changes flow back into the mirror and the effective view.
document.addEventListener('change', ev => {
  const el = ev.target;
  if (!el || !el.dataset || !el.dataset.govChip) return;
  const id = el.dataset.govChip;
  _govChipState[id] = _govCsv(el.value);
  _govRenderChipField(id);
});

function _govChipCommit() { /* pickers commit on selection; kept for callers */ }

// Picker sources: resolved lazily from the catalogs so a menu opened before the
// catalog request finished shows "Loading" and then the list.
(function registerGovPickerSources() {
  if (!window.SpPicker) { document.addEventListener('DOMContentLoaded', registerGovPickerSources, { once: true }); return; }
  const cat = () => _govEnsureCatalogs();
  const wild = label => ({ value: '*', label: '*', hint: label });
  SpPicker.sources['gov:skills'] = () => cat().then(c => [wild(_govT('picker_all_skills', 'every skill'))].concat(c.skills));
  SpPicker.sources['gov:mcp'] = () => cat().then(c => [wild(_govT('picker_all_servers', 'every server'))].concat(c.mcp));
  SpPicker.sources['gov:cli'] = () => cat().then(c => [wild(_govT('picker_all_commands', 'every command'))].concat(c.cli));
  SpPicker.sources['gov:roles'] = () => cat().then(c => c.roles);
  SpPicker.sources['gov:groups'] = () => cat().then(c => c.groups);
  SpPicker.sources['gov:sso'] = () => Promise.all([cat(), SpPicker.sources.people()]).then(([c, people]) => {
    const seen = new Set();
    const out = [];
    (c.sso || []).forEach(g => { if (!seen.has(g)) { seen.add(g); out.push({ value: g, label: g, hint: _govT('governance_sso_group', 'SSO group') }); } });
    people.forEach(p => { if (!seen.has(p.value)) { seen.add(p.value); out.push(p); } });
    return out;
  });
})();

// ── Autocomplete catalogs (skills, MCP servers, CLI commands) ─────────────
// Skills and MCP servers come from their live catalogs; CLI command ids have
// no catalog endpoint, so the union of every cli.commands already in the
// policy is offered (free text still works for new commands).

async function _govEnsureCatalogs(force) {
  if (window.__GOV_CAT__ && !force) return window.__GOV_CAT__;
  const cat = { skills: [], mcp: [], mcpTools: Object.create(null), cli: [], roles: [], groups: [], emails: [], sso: [] };
  const opts = { redirect401: false, timeoutToast: false, timeoutMs: 15000 };
  await Promise.all([
    api('/api/skills', opts).then(d => {
      cat.skills = (d.skills || []).map(s => s && s.name).filter(Boolean).sort();
    }).catch(() => {}),
    api('/api/mcp/servers', opts).then(d => {
      cat.mcp = (d.servers || []).map(s => s && s.name).filter(Boolean).sort();
    }).catch(() => {}),
    api('/api/mcp/tools', opts).then(d => {
      for (const tool of d.tools || []) {
        if (!tool || !tool.server || !tool.name) continue;
        const names = cat.mcpTools[tool.server] || (cat.mcpTools[tool.server] = []);
        if (!names.includes(tool.name)) names.push(tool.name);
      }
      Object.values(cat.mcpTools).forEach(names => names.sort());
    }).catch(() => {}),
    api('/api/governance/policy', opts).then(d => {
      const seen = new Set();
      const walk = grants => {
        const cli = (grants && grants.cli && typeof grants.cli === 'object') ? grants.cli : {};
        (Array.isArray(cli.commands) ? cli.commands : []).forEach(c => {
          const v = (typeof c === 'string') ? c : ((c && (c.id || c.argv0)) || '');
          if (v && v !== '*') seen.add(v);
        });
      };
      const policy = d.policy || {};
      Object.values(policy.roles || {}).forEach(r => walk((r || {}).grants));
      Object.values(policy.groups || {}).forEach(g => walk((g || {}).grants));
      Object.values(policy.users || {}).forEach(u => { walk((u || {}).grants); walk((u || {}).deny); });
      cat.cli = Array.from(seen).sort();
      // Selection sources so admins pick instead of type: every role, group
      // and user email already present in the policy.
      cat.roles = Object.keys(policy.roles || {}).sort();
      cat.groups = Object.keys(policy.groups || {}).sort();
      cat.emails = Object.keys(policy.users || {}).sort();
      const sso = new Set();
      Object.values(policy.groups || {}).forEach(g => ((g || {}).sso_groups || []).forEach(v => { if (v) sso.add(String(v)); }));
      cat.sso = Array.from(sso).sort();
      // Autocomplete may finish after an editor loaded an older policy. Its
      // independent request must not replace the editor's concurrency token.
    }).catch(() => {}),
  ]);
  window.__GOV_CAT__ = cat;
  return cat;
}

function _govFillCatalogDatalists(cat) {
  const fill = (id, values) => {
    const dl = $(id);
    if (dl) dl.innerHTML = values.map(v => '<option value="' + _govEsc(v) + '"></option>').join('');
  };
  fill('govDlSkills', cat.skills);
  fill('govDlMcp', cat.mcp);
  fill('govDlCli', cat.cli);
  // Group-form datalists use distinct ids so they never collide with the
  // users-form ids when both panes are in the DOM.
  fill('govDlSkillsGrp', cat.skills);
  fill('govDlMcpGrp', cat.mcp);
  fill('govDlCliGrp', cat.cli);
  fill('govDlRoles', cat.roles);
  fill('govDlRolesUsr', cat.roles);
  fill('govDlGroupsUsr', cat.groups);
  fill('govDlEmails', cat.emails);
  fill('govDlGroups', cat.groups);
  Object.keys(_govMcpToolMaps).forEach(_govMcpToolMapOptions);
}

function _govResetUserForm() {
  _govEditingUser = null;
  _govUserEffective = null;
  const title = $('govUserFormTitle');
  if (title) title.textContent = _govT('governance_user_add', 'Add user');
  const email = $('govUserEmail');
  if (email) { email.value = ''; email.disabled = false; }
  _govChipsSet('govUserRolesSel', []);
  _govChipsSet('govUserGroupsSel', []);
  _GOV_USER_GRANT_FIELDS.concat(_GOV_USER_DENY_FIELDS).forEach(id => _govChipsSet(id, []));
  _govSetExtraUserFields('grant', {});
  _govSetExtraUserFields('deny', {});
  _govMcpToolMapSet('govUserMcpTools');
  _govMcpToolMapSet('govUserDenyMcpTools');
  if ($('govUserAccessLevel')) $('govUserAccessLevel').value = 'user';
  if ($('govUserAccessMode')) $('govUserAccessMode').value = 'whitelist';
  if ($('govUserApprovalMode')) $('govUserApprovalMode').value = 'manual';
  if ($('govUserApprovalPrompt')) $('govUserApprovalPrompt').value = '';
  _govUserPolicyChanged();
  _govRenderUserEffective();
}

function _govEditUser(email) {
  const entry = (window.__GOV_USERS__ || {})[email] || {};
  _govEditingUser = email;
  const title = $('govUserFormTitle');
  if (title) title.textContent = _govT('governance_user_edit', 'Edit user');
  const emailEl = $('govUserEmail');
  if (emailEl) { emailEl.value = email; emailEl.disabled = true; }
  _govChipsSet('govUserRolesSel', entry.roles || []);
  _govChipsSet('govUserGroupsSel', entry.groups || []);
  if ($('govUserAccessLevel')) $('govUserAccessLevel').value = entry.access_level || '';
  if ($('govUserAccessMode')) $('govUserAccessMode').value = entry.access_mode || '';
  if ($('govUserApprovalMode')) $('govUserApprovalMode').value = (entry.approval || {}).mode || '';
  if ($('govUserApprovalPrompt')) $('govUserApprovalPrompt').value = (entry.approval || {}).prompt || '';
  _govUserPolicyChanged();
  const grants = (entry.grants && typeof entry.grants === 'object') ? entry.grants : {};
  _govSetExtraUserFields('grant', grants);
  const skills = (grants.skills && typeof grants.skills === 'object') ? grants.skills : {};
  const mcp = (grants.mcp && typeof grants.mcp === 'object') ? grants.mcp : {};
  const cli = (grants.cli && typeof grants.cli === 'object') ? grants.cli : {};
  _govChipsSet('govUserSkillsView', skills.view || []);
  _govChipsSet('govUserSkillsLoad', skills.load || []);
  _govChipsSet('govUserSkillsManage', skills.manage || []);
  _govChipsSet('govUserMcpServers', mcp.servers || []);
  _govMcpToolMapSet('govUserMcpTools', mcp.tools);
  // cli.commands entries may be strings or {id/argv0} objects per the policy schema
  const commands = (cli.commands || []).map(c => (typeof c === 'string') ? c : ((c && (c.id || c.argv0)) || '')).filter(Boolean);
  _govChipsSet('govUserCliCommands', commands);
  _govChipsSet('govUserCliApproval', cli.approval_commands || []);
  const deny = (entry.deny && typeof entry.deny === 'object') ? entry.deny : {};
  _govSetExtraUserFields('deny', deny);
  const denySkills = (deny.skills && typeof deny.skills === 'object') ? deny.skills : {};
  const denyMcp = (deny.mcp && typeof deny.mcp === 'object') ? deny.mcp : {};
  const denyCli = (deny.cli && typeof deny.cli === 'object') ? deny.cli : {};
  // The combined control displays view+load. An untouched union preserves
  // asymmetric policy entries; changing it applies the combined restriction.
  _govChipsSet('govUserDenySkills', Array.from(new Set([].concat(denySkills.view || [], denySkills.load || []))));
  _govChipsSet('govUserDenySkillsManage', denySkills.manage || []);
  const denyCommands = (denyCli.commands || []).map(c => (typeof c === 'string') ? c : ((c && (c.id || c.argv0)) || '')).filter(Boolean);
  _govChipsSet('govUserDenyCli', denyCommands);
  _govChipsSet('govUserDenyMcp', denyMcp.servers || []);
  _govMcpToolMapSet('govUserDenyMcpTools', denyMcp.tools);
  _govLoadUserEffective(email);
}

// ── Effective access on/off view ──────────────────────────────────────────
// Edit mode only: the union of the user's effective grants (post-deny, from
// the preview endpoint) and the current deny chips, rendered as clickable
// on/off toggles that write into the deny chip fields.

let _govUserEffective = null;
let _govUserEffectiveRequest = 0;

async function _govLoadUserEffective(email) {
  const request = ++_govUserEffectiveRequest;
  const el = $('govUserEffective');
  if (el) el.innerHTML = '<div class="gov-muted">' + _govT('loading', 'Loading...') + '</div>';
  try {
    const data = await api('/api/governance/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email }),
      redirect401: false,
      timeoutToast: false,
    });
    if (_govEditingUser !== email || request !== _govUserEffectiveRequest) return;
    _govUserEffective = ((data.effective_access || {}).grants) || null;
  } catch (e) {
    if (_govEditingUser !== email || request !== _govUserEffectiveRequest) return;
    _govUserEffective = null;
  }
  _govRenderUserEffective();
}

function _govRenderUserEffective() {
  const el = $('govUserEffective');
  if (!el) return;
  if (!_govEditingUser || !_govUserEffective) { el.innerHTML = ''; return; }
  const g = _govUserEffective;
  const sections = [
    { label: _govT('governance_eff_skills', 'Skills'), denyField: 'govUserDenySkills', granted: ((g.skills || {}).load) || [] },
    { label: _govT('governance_eff_cli', 'CLI commands'), denyField: 'govUserDenyCli', granted: ((g.cli || {}).commands) || [] },
    { label: _govT('governance_eff_mcp', 'MCP servers'), denyField: 'govUserDenyMcp', granted: ((g.mcp || {}).servers) || [] },
  ];
  const body = sections.map(sec => {
    const deny = _govChipsGet(sec.denyField);
    const wildcard = sec.granted.includes('*');
    const items = Array.from(new Set(sec.granted.filter(v => v !== '*').concat(deny))).sort();
    const chips = items.map(v => {
      const off = deny.includes(v);
      return '<button type="button" class="gov-chip gov-toggle ' + (off ? 'off' : 'on') +
        '" onclick="_govEffToggle(\'' + sec.denyField + '\', ' + _govEsc(JSON.stringify(v)) + ')" title="' +
        _govT('governance_toggle_hint', 'Click to toggle on/off') + '">' + _govEsc(v) + '</button>';
    }).join(' ');
    const wildcardNote = (wildcard && deny.length)
      ? '<div class="gov-muted">' + _govT('governance_deny_wildcard_active',
          'Wildcard access is limited by the denied items shown here.') + '</div>'
      : (wildcard ? '<div class="gov-muted">' + _govT('governance_eff_wildcard', 'Wildcard (*) grant: everything is allowed.') + '</div>' : '');
    return '<div class="gov-preview-section"><div class="gov-form-title">' + _govEsc(sec.label) + '</div>' +
      (chips || '<span class="gov-muted">' + _govT('governance_none', 'none') + '</span>') + wildcardNote + '</div>';
  }).join('');
  el.innerHTML =
    '<div class="gov-form-title gov-grants-title">' + _govT('governance_eff_title', 'Effective access (click to toggle)') + '</div>' +
    '<div class="gov-muted">' + _govT('governance_eff_note',
      'Saved policy preview. Denied items remain blocked even when a wildcard is shown. Save changes to refresh this preview.') + '</div>' +
    body;
}

function _govEffToggle(denyField, value) {
  if (_govChipsGet(denyField).includes(value)) _govChipRemove(denyField, value);
  else _govChipAdd(denyField, value);
}

/**
 * Build the grants object from the form fields, or null when every field is
 * empty. Grant keys the form does not edit (mcp.tools, cli.workdir_roots,
 * usage_caps, ...) are carried over from the entry being edited so a save
 * never silently drops them.
 */
function _govCopyUserSection(section) {
  const existing = (_govEditingUser && (window.__GOV_USERS__ || {})[_govEditingUser]) || {};
  const prior = existing[section];
  return prior && typeof prior === 'object' ? JSON.parse(JSON.stringify(prior)) : {};
}

function _govSetEditedList(section, key, values) {
  if (values.length) section[key] = values;
  else delete section[key];
}

function _govKeepCommandMetadata(commands, prior) {
  return commands.map(id => (prior || []).find(command =>
    command && typeof command === 'object' && (command.id || command.argv0) === id) || id);
}

function _govCollectUserGrants() {
  const grants = _govCopyUserSection('grants');
  grants.skills = grants.skills || {};
  for (const [key, id] of [['view', 'govUserSkillsView'], ['load', 'govUserSkillsLoad'], ['manage', 'govUserSkillsManage']]) {
    _govSetEditedList(grants.skills, key, _govChipsGet(id));
  }
  grants.mcp = grants.mcp || {};
  _govSetEditedList(grants.mcp, 'servers', _govChipsGet('govUserMcpServers'));
  _govCollectMcpToolMap('govUserMcpTools', grants.mcp);
  grants.cli = grants.cli || {};
  _govSetEditedList(grants.cli, 'commands', _govKeepCommandMetadata(_govChipsGet('govUserCliCommands'), grants.cli.commands));
  _govSetEditedList(grants.cli, 'approval_commands', _govChipsGet('govUserCliApproval'));
  for (const key of ['skills', 'mcp', 'cli']) if (!Object.keys(grants[key]).length) delete grants[key];
  return _govCollectExtraUserFields('grant', grants);
}

/** Preserve unedited policy dimensions, including asymmetric skill denials. */
function _govCollectUserDeny() {
  const deny = _govCopyUserSection('deny');
  deny.skills = deny.skills || {};
  const priorOff = [...new Set([...(deny.skills.view || []), ...(deny.skills.load || [])])].sort();
  const skillsOff = _govChipsGet('govUserDenySkills');
  if (JSON.stringify(priorOff) !== JSON.stringify([...skillsOff].sort())) {
    _govSetEditedList(deny.skills, 'view', skillsOff.slice());
    _govSetEditedList(deny.skills, 'load', skillsOff.slice());
  }
  _govSetEditedList(deny.skills, 'manage', _govChipsGet('govUserDenySkillsManage'));
  deny.mcp = deny.mcp || {};
  _govSetEditedList(deny.mcp, 'servers', _govChipsGet('govUserDenyMcp'));
  _govCollectMcpToolMap('govUserDenyMcpTools', deny.mcp);
  deny.cli = deny.cli || {};
  _govSetEditedList(deny.cli, 'commands', _govKeepCommandMetadata(_govChipsGet('govUserDenyCli'), deny.cli.commands));
  for (const key of ['skills', 'mcp', 'cli']) if (!Object.keys(deny[key]).length) delete deny[key];
  return _govCollectExtraUserFields('deny', deny);
}

function _govUserDraftSignature() {
  const form = $('govUserForm');
  if (!form) return '';
  return JSON.stringify([_govEditingUser,
    Array.from(form.querySelectorAll('input, select, textarea')).map(el => [el.id, el.value]),
    Array.from(form.querySelectorAll('.gov-chip-item')).map(el => el.textContent)]);
}

async function _govSaveUser() {
  if (_govSavingUser) return;
  const email = String(($('govUserEmail') || {}).value || '').trim().toLowerCase();
  if (!email || !email.includes('@')) {
    if (typeof showToast === 'function') showToast(_govT('governance_invalid_email', 'Enter a valid email address'), 3000, 'error');
    return;
  }
  _govChipCommit('govUserRolesSel');
  _govChipCommit('govUserGroupsSel');
  _GOV_USER_GRANT_FIELDS.concat(_GOV_USER_DENY_FIELDS).forEach(_govChipCommit);
  if (!_govMcpToolMapsValid(['govUserMcpTools', 'govUserDenyMcpTools'])) return;
  const approvalMode = String(($('govUserApprovalMode') || {}).value || '');
  const approvalPrompt = String(($('govUserApprovalPrompt') || {}).value || '').trim();
  if (approvalMode === 'automatic' && !approvalPrompt) {
    if (typeof showToast === 'function') showToast(_govT('governance_prompt_required', 'Describe what this user may and may not do before enabling automatic approval.'), 4000, 'error');
    if ($('govUserApprovalPrompt')) $('govUserApprovalPrompt').focus();
    return;
  }
  const previous = (_govEditingUser && (window.__GOV_USERS__ || {})[_govEditingUser]) || {};
  const entry = {
    ...previous,
    roles: _govChipsGet('govUserRolesSel'),
    groups: _govChipsGet('govUserGroupsSel'),
  };
  if (approvalMode) entry.approval = { mode: approvalMode, prompt: approvalPrompt };
  else delete entry.approval;
  const level = String(($('govUserAccessLevel') || {}).value || '');
  const mode = String(($('govUserAccessMode') || {}).value || '');
  if (level) entry.access_level = level;
  else delete entry.access_level;
  if (mode) entry.access_mode = mode;
  else delete entry.access_mode;
  const grants = _govCollectUserGrants();
  if (grants) entry.grants = grants;
  else delete entry.grants;
  const deny = _govCollectUserDeny();
  if (deny) entry.deny = deny;
  else delete entry.deny;
  const path = _govEditingUser ? '/api/governance/users/update' : '/api/governance/users';
  const savedDraft = _govUserDraftSignature();
  _govSavingUser = true;
  if ($('govUserSave')) $('govUserSave').disabled = true;
  try {
    const res = await _govPost(path, { email: email, entry: entry });
    _govEtag = res.etag || _govEtag;
    if (typeof showToast === 'function') showToast(_govT('governance_saved', 'Saved'), 2500);
    if (window.__GOV_USERS__) window.__GOV_USERS__[email] = entry;
    if (savedDraft === _govUserDraftSignature()) await _govLoadUsers(savedDraft);
  } catch (e) {
    if (!_govHandleConflict(e) && typeof showToast === 'function') showToast(e.message || 'save failed', 4000, 'error');
  } finally {
    _govSavingUser = false;
    if ($('govUserSave')) $('govUserSave').disabled = false;
  }
}

async function _govDeleteUser(email) {
  try {
    const res = await _govPost('/api/governance/users/delete', { email: email });
    _govEtag = res.etag || _govEtag;
    if (typeof showToast === 'function') showToast(_govT('governance_deleted', 'Deleted'), 2500);
    await _govLoadUsers();
  } catch (e) {
    if (!_govHandleConflict(e) && typeof showToast === 'function') showToast(e.message || 'delete failed', 4000, 'error');
  }
}

// ── Groups tab ────────────────────────────────────────────────────────────
// Groups carry description, sso_groups, roles and grants (same grant schema
// as users). The backend has supported group grants all along; this editor
// exposes them with the same chip/datalist UX as the users tab.

const _GOV_GROUP_SECTION_FIELDS = ['govGroupSkillsView', 'govGroupSkillsLoad', 'govGroupSkillsManage', 'govGroupMcpServers', 'govGroupCliCommands', 'govGroupCliApproval'];

function _govGroupGrantsHtml() {
  return '<div class="gov-form-section">' +
    '<div class="gov-form-title">' + _govT('governance_grants_title', 'Capabilities & grants') + '</div>' +
    '<div class="gov-muted" style="margin-bottom:10px">' + _govT('governance_group_grants_note',
      'Skills, MCP servers and CLI commands granted to every user in this group (merged with role and user grants).') + '</div>' +
    _govChipFieldHtml('govGroupSkillsView', _govT('governance_grants_skills_view', 'Skills view'), 'govDlSkillsGrp', 'my-skill, *') +
    _govChipFieldHtml('govGroupSkillsLoad', _govT('governance_grants_skills_load', 'Skills load'), 'govDlSkillsGrp', 'my-skill') +
    _govChipFieldHtml('govGroupSkillsManage', _govT('governance_grants_skills_manage', 'Skills manage'), 'govDlSkillsGrp', 'my-skill') +
    _govChipFieldHtml('govGroupMcpServers', _govT('governance_grants_mcp_servers', 'MCP servers'), 'govDlMcpGrp', 'notion, playwright') +
    _govMcpToolMapHtml('govGroupMcpTools', 'MCP tools to allow', false, 'govDlMcpGrp') +
    _govChipFieldHtml('govGroupCliCommands', _govT('governance_grants_cli_commands', 'CLI commands'), 'govDlCliGrp', 'git, gh') +
    _govChipFieldHtml('govGroupCliApproval', _govT('governance_grants_cli_approval', 'CLI commands requiring approval'), 'govDlCliGrp', 'rm, sudo') +
    '<datalist id="govDlSkillsGrp"></datalist><datalist id="govDlCliGrp"></datalist><datalist id="govDlMcpGrp"></datalist>' +
  '</div>';
}

/** Group templates for quick start ("automatic" group creation). */
const _GOV_GROUP_TEMPLATES = [
  {
    key: 'viewers',
    name: 'viewers',
    description: 'Read-only: view sessions, insights and skills',
    roles: ['viewer'],
    grants: { skills: { view: ['*'] } },
  },
  {
    key: 'operators',
    name: 'operators',
    description: 'Daily operators: load skills, run common CLI tools',
    roles: ['operator'],
    grants: { skills: { load: ['*'] }, cli: { commands: ['git', 'gh', 'ls'] } },
  },
  {
    key: 'engineers',
    name: 'engineers',
    description: 'Full capability set: manage skills, MCP servers and CLI',
    roles: ['operator', 'engineer'],
    grants: { skills: { load: ['*'], manage: ['*'] }, cli: { commands: ['*'] } },
  },
];

function _govGroupTemplateChipsHtml() {
  const chips = _GOV_GROUP_TEMPLATES.map(tmp =>
    '<button type="button" class="gov-chip gov-chip-role" onclick="_govApplyGroupTemplate(\'' +
    _govEsc(tmp.key) + '\')" title="' + _govT('governance_group_template_use', 'Start from this template') + '">' +
    _govEsc(tmp.name) + '</button>'
  ).join('');
  return '<div class="gov-form-section">' +
    '<div class="gov-form-title">' + _govT('governance_group_templates', 'Quick start') + '</div>' +
    '<div class="gov-muted" style="margin-bottom:8px">' + _govT('governance_group_templates_note', 'Pick a starting point and adjust before saving.') + '</div>' +
    '<div class="gov-group-card-meta">' + chips + '</div>' +
  '</div>';
}

function _govCapabilityPills(group) {
  const grants = (group && group.grants && typeof group.grants === 'object') ? group.grants : {};
  const skills = (grants.skills && typeof grants.skills === 'object') ? grants.skills : {};
  const mcp = (grants.mcp && typeof grants.mcp === 'object') ? grants.mcp : {};
  const cli = (grants.cli && typeof grants.cli === 'object') ? grants.cli : {};
  const countSkills = new Set([].concat(skills.view || [], skills.load || [], skills.manage || [])).size;
  const countMcp = (mcp.servers || []).length;
  const countCli = (cli.commands || []).length + (cli.approval_commands || []).length;
  if (!countSkills && !countMcp && !countCli) {
    return '<span class="gov-muted">' + _govT('governance_no_grants', 'No capabilities granted') + '</span>';
  }
  const svg = (path) => '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px" aria-hidden="true">' + path + '</svg>';
  const pill = (cls, iconSvg, count, label) => (
    '<span class="gov-cap-pill ' + cls + '" title="' + _govEsc(label) + '">' + iconSvg + '<span class="gov-cap-count">' + count + '</span>' +
    '<span>' + _govEsc(label) + '</span></span>'
  );
  const iconSkill = svg('<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>');
  const iconMcp = svg('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>');
  const iconCli = svg('<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>');
  return '<div class="gov-group-capabilities">' +
    (countSkills ? pill('gov-cap-skill', iconSkill, countSkills, 'skills') : '') +
    (countMcp ? pill('gov-cap-mcp', iconMcp, countMcp, 'MCP') : '') +
    (countCli ? pill('gov-cap-cli', iconCli, countCli, 'CLI') : '') +
  '</div>';
}

/** Count policy users whose groups array contains this group name. */
function _govGroupMemberCount(name, users) {
  let n = 0;
  Object.keys(users || {}).forEach(email => {
    const entry = users[email] || {};
    if ((entry.groups || []).includes(name)) n += 1;
  });
  return n;
}

async function _govLoadGroups() {
  const el = $('govPaneGroups');
  if (!el) return;
  el.innerHTML = '<div class="gov-muted">' + _govT('loading', 'Loading...') + '</div>';
  let data, users;
  try {
    const opts = { redirect401: false, timeoutToast: false };
    [data, users] = await Promise.all([
      api('/api/governance/groups', opts),
      api('/api/governance/users', opts).catch(() => ({ users: {} })),
    ]);
  } catch (e) {
    _govError(e, 'govPaneGroups');
    return;
  }
  _govEtag = data.etag || _govEtag;
  window.__GOV_USERS__ = (users && users.users) || window.__GOV_USERS__ || {};
  const groups = data.groups || {};
  window.__GOV_GROUPS__ = groups;
  const cards = Object.keys(groups).sort().map(name => {
    const entry = groups[name] || {};
    const roles = Array.isArray(entry.roles) ? entry.roles : [];
    const sso = Array.isArray(entry.sso_groups) ? entry.sso_groups : [];
    const members = _govGroupMemberCount(name, window.__GOV_USERS__);
    const roleChips = roles.map(r => '<span class="gov-chip gov-chip-role">' + _govEsc(r) + '</span>').join('');
    const ssoChips = sso.map(s => '<span class="gov-chip">SSO: ' + _govEsc(s) + '</span>').join('');
    const memberBadge = members
      ? '<span class="gov-chip" title="' + _govEsc(String(members)) + ' direct user(s)">' + members + ' member' + (members === 1 ? '' : 's') + '</span>'
      : '';
    return '<div class="gov-group-card">' +
      '<div class="gov-group-card-head">' +
        '<div><div class="gov-group-card-name">' + _govEsc(name) + '</div>' +
        '<div class="gov-group-card-desc">' + (_govEsc(entry.description || '') || '<span class="gov-muted">' + _govT('governance_no_description', 'No description') + '</span>') + '</div></div>' +
      '</div>' +
      '<div class="gov-group-card-meta">' + roleChips + ssoChips + memberBadge + '</div>' +
      _govCapabilityPills(entry) +
      '<div class="gov-group-card-actions">' +
        '<button type="button" class="gov-btn" onclick="_govEditGroup(' + _govEsc(JSON.stringify(name)) + ')">' + _govT('governance_edit', 'Edit') + '</button>' +
        '<button type="button" class="gov-btn danger" onclick="_govDeleteGroup(' + _govEsc(JSON.stringify(name)) + ')">' + _govT('governance_delete', 'Delete') + '</button>' +
      '</div>' +
    '</div>';
  }).join('');
  el.innerHTML =
    '<div class="gov-toolbar" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">' +
      '<div style="font-size:13px;color:var(--muted)">' + Object.keys(groups).length + ' ' + _govT('governance_groups', 'groups') + '</div>' +
      '<button type="button" class="gov-btn primary" onclick="_govToggleGroupForm()">+ ' + _govT('governance_group_add', 'New group') + '</button>' +
    '</div>' +
    (cards
      ? '<div class="gov-group-grid">' + cards + '</div>'
      : '<div class="gov-form" style="max-width:none"><div class="gov-muted">' + _govT('governance_no_groups', 'No group entries in the policy.') + ' ' + _govT('governance_group_empty_hint', 'Create your first group below.') + '</div></div>') +
    '<div class="gov-form" id="govGroupForm" style="' + (window.__GOV_GROUP_FORM_OPEN__ ? '' : 'display:none') + '">' +
      _govGroupTemplateChipsHtml() +
      '<div class="gov-form-section" style="border-top:none;padding-top:0">' +
        '<div class="gov-form-title" id="govGroupFormTitle">' + _govT('governance_group_add', 'New group') + '</div>' +
        '<div class="gov-form-row"><label for="govGroupName">' + _govT('governance_col_name', 'Name') + '</label>' +
          '<input id="govGroupName" type="text" placeholder="sw-engineering" autocomplete="off"></div>' +
        '<div class="gov-form-row"><label for="govGroupDesc">' + _govT('governance_col_description', 'Description') + '</label>' +
          '<input id="govGroupDesc" type="text" placeholder="What is this group for?" autocomplete="off"></div>' +
        '<div class="gov-form-row"><label for="govGroupSso">' + _govT('governance_sso_csv', 'SSO groups') + '</label>' +
          '<input id="govGroupSso" type="hidden" data-sp-picker data-sp-multi="1" data-sp-custom="1" data-sp-source="gov:sso" placeholder="' + _govT('picker_pick_sso', 'Pick SSO groups or people') + '"></div>' +
        '<div class="gov-form-row"><label for="govGroupRoles">' + _govT('governance_roles_csv', 'Roles') + '</label>' +
          '<input id="govGroupRoles" type="hidden" data-sp-picker data-sp-multi="1" data-sp-source="gov:roles" placeholder="' + _govT('picker_pick_roles', 'Pick roles') + '"></div>' +
        '<datalist id="govDlSso"></datalist><datalist id="govDlRoles"></datalist>' +
      '</div>' +
      _govGroupGrantsHtml() +
      '<div class="gov-form-actions">' +
        '<button type="button" class="gov-btn primary" onclick="_govSaveGroup()">' + _govT('governance_save', 'Save group') + '</button>' +
        '<button type="button" class="gov-btn" onclick="_govResetGroupForm()">' + _govT('governance_cancel', 'Cancel') + '</button>' +
      '</div>' +
    '</div>';
  if (window.SpPicker) SpPicker.mountAll(el);
  _govResetGroupForm();
  _govEnsureCatalogs().then(_govFillCatalogDatalists).catch(() => {});
}

function _govToggleGroupForm() {
  window.__GOV_GROUP_FORM_OPEN__ = !window.__GOV_GROUP_FORM_OPEN__;
  const form = $('govGroupForm');
  if (form) form.style.display = window.__GOV_GROUP_FORM_OPEN__ ? '' : 'none';
}

function _govApplyGroupTemplate(key) {
  const tmp = _GOV_GROUP_TEMPLATES.find(t => t.key === key);
  if (!tmp) return;
  _govResetGroupForm();
  const name = $('govGroupName');
  if (name) name.value = tmp.name;
  const desc = $('govGroupDesc');
  if (desc) desc.value = tmp.description;
  const roles = $('govGroupRoles');
  if (roles) roles.value = (tmp.roles || []).join(', ');
  const grants = (tmp.grants && typeof tmp.grants === 'object') ? tmp.grants : {};
  const skills = (grants.skills && typeof grants.skills === 'object') ? grants.skills : {};
  _govChipsSet('govGroupSkillsView', skills.view || []);
  _govChipsSet('govGroupSkillsLoad', skills.load || []);
  _govChipsSet('govGroupSkillsManage', skills.manage || []);
  const mcp = (grants.mcp && typeof grants.mcp === 'object') ? grants.mcp : {};
  _govChipsSet('govGroupMcpServers', mcp.servers || []);
  _govMcpToolMapSet('govGroupMcpTools', mcp.tools);
  const cli = (grants.cli && typeof grants.cli === 'object') ? grants.cli : {};
  _govChipsSet('govGroupCliCommands', (cli.commands || []).map(_govChipIdOf));
  _govChipsSet('govGroupCliApproval', (cli.approval_commands || []).map(_govChipIdOf));
  window.__GOV_GROUP_FORM_OPEN__ = true;
  const form = $('govGroupForm');
  if (form) { form.style.display = ''; const first = $('govGroupName'); if (first) first.focus(); }
  if (typeof showToast === 'function') showToast(_govT('governance_template_applied', 'Template applied — adjust and save'), 2500);
}

function _govResetGroupForm() {
  _govEditingGroup = null;
  const title = $('govGroupFormTitle');
  if (title) title.textContent = _govT('governance_group_add', 'New group');
  const name = $('govGroupName');
  if (name) { name.value = ''; name.disabled = false; }
  const desc = $('govGroupDesc');
  if (desc) desc.value = '';
  const sso = $('govGroupSso');
  if (sso) sso.value = '';
  const roles = $('govGroupRoles');
  if (roles) roles.value = '';
  _GOV_GROUP_SECTION_FIELDS.forEach(id => _govChipsSet(id, []));
  _govMcpToolMapSet('govGroupMcpTools');
}

function _govEditGroup(name) {
  const entry = (window.__GOV_GROUPS__ || {})[name] || {};
  _govEditingGroup = name;
  const title = $('govGroupFormTitle');
  if (title) title.textContent = _govT('governance_group_edit', 'Edit group');
  const nameEl = $('govGroupName');
  if (nameEl) { nameEl.value = name; nameEl.disabled = true; }
  const desc = $('govGroupDesc');
  if (desc) desc.value = entry.description || '';
  const sso = $('govGroupSso');
  if (sso) sso.value = (entry.sso_groups || []).join(', ');
  const roles = $('govGroupRoles');
  if (roles) roles.value = (entry.roles || []).join(', ');
  const grants = (entry.grants && typeof entry.grants === 'object') ? entry.grants : {};
  const skills = (grants.skills && typeof grants.skills === 'object') ? grants.skills : {};
  _govChipsSet('govGroupSkillsView', skills.view || []);
  _govChipsSet('govGroupSkillsLoad', skills.load || []);
  _govChipsSet('govGroupSkillsManage', skills.manage || []);
  const mcp = (grants.mcp && typeof grants.mcp === 'object') ? grants.mcp : {};
  _govChipsSet('govGroupMcpServers', mcp.servers || []);
  _govMcpToolMapSet('govGroupMcpTools', mcp.tools);
  const cli = (grants.cli && typeof grants.cli === 'object') ? grants.cli : {};
  _govChipsSet('govGroupCliCommands', (cli.commands || []).map(_govChipIdOf));
  _govChipsSet('govGroupCliApproval', (cli.approval_commands || []).map(_govChipIdOf));
  window.__GOV_GROUP_FORM_OPEN__ = true;
  const form = $('govGroupForm');
  if (form) form.style.display = '';
  const first = $('govGroupDesc');
  if (first) first.focus();
}

function _govChipIdOf(entry) {
  return (typeof entry === 'string') ? entry : ((entry && (entry.id || entry.argv0)) || '');
}

/** Build the grants object from the group form, or null when every field is
 *  empty. Non-edited grant keys (cli.workdir_roots, usage_caps, ...)
 *  are carried over from the entry being edited. */
function _govCollectGroupGrants() {
  const existing = (_govEditingGroup && (window.__GOV_GROUPS__ || {})[_govEditingGroup]) || {};
  const prior = (existing.grants && typeof existing.grants === 'object') ? existing.grants : {};
  const grants = JSON.parse(JSON.stringify(prior));
  grants.skills = grants.skills || {};
  for (const [key, id] of [['view', 'govGroupSkillsView'], ['load', 'govGroupSkillsLoad'], ['manage', 'govGroupSkillsManage']]) {
    _govSetEditedList(grants.skills, key, _govChipsGet(id));
  }
  grants.mcp = grants.mcp || {};
  _govSetEditedList(grants.mcp, 'servers', _govChipsGet('govGroupMcpServers'));
  _govCollectMcpToolMap('govGroupMcpTools', grants.mcp);
  grants.cli = grants.cli || {};
  _govSetEditedList(grants.cli, 'commands', _govKeepCommandMetadata(_govChipsGet('govGroupCliCommands'), grants.cli.commands));
  _govSetEditedList(grants.cli, 'approval_commands', _govKeepCommandMetadata(_govChipsGet('govGroupCliApproval'), grants.cli.approval_commands));
  for (const key of ['skills', 'mcp', 'cli']) if (!Object.keys(grants[key]).length) delete grants[key];
  return Object.keys(grants).length ? grants : null;
}

async function _govSaveGroup() {
  if (!_govMcpToolMapsValid(['govGroupMcpTools'])) return;
  const name = String(($('govGroupName') || {}).value || '').trim();
  if (!name) {
    if (typeof showToast === 'function') showToast(_govT('governance_invalid_name', 'Enter a group name'), 3000, 'error');
    return;
  }
  const entry = {
    description: String(($('govGroupDesc') || {}).value || '').trim(),
    sso_groups: _govCsv(($('govGroupSso') || {}).value),
    roles: _govCsv(($('govGroupRoles') || {}).value),
  };
  const grants = _govCollectGroupGrants();
  if (grants) entry.grants = grants;
  const path = _govEditingGroup ? '/api/governance/groups/update' : '/api/governance/groups';
  try {
    const res = await _govPost(path, { name: name, entry: entry });
    _govEtag = res.etag || _govEtag;
    if (typeof showToast === 'function') showToast(_govT('governance_saved', 'Group saved'), 2500);
    window.__GOV_GROUP_FORM_OPEN__ = false;
    await _govLoadGroups();
  } catch (e) {
    if (!_govHandleConflict(e) && typeof showToast === 'function') showToast(e.message || 'save failed', 4000, 'error');
  }
}

async function _govDeleteGroup(name) {
  const parents = Object.keys(window.__GOV_USERS__ || {}).filter(email => {
    const groups = ((window.__GOV_USERS__ || {})[email] || {}).groups;
    return Array.isArray(groups) && groups.includes(name);
  });
  if (parents.length) {
    if (typeof showToast === 'function') showToast('Group is assigned to ' + parents.length + ' user(s): reassign them first', 4500, 'error');
    return;
  }
  try {
    const res = await _govPost('/api/governance/groups/delete', { name: name });
    _govEtag = res.etag || _govEtag;
    if (typeof showToast === 'function') showToast(_govT('governance_deleted', 'Deleted'), 2500);
    await _govLoadGroups();
  } catch (e) {
    if (!_govHandleConflict(e) && typeof showToast === 'function') showToast(e.message || 'delete failed', 4000, 'error');
  }
}

// ── Workspaces tab ────────────────────────────────────────────────────────
// Admin management of workspace ownership: owner_email plus members on the
// entries in the per-profile workspaces.json. Data comes from GET
// /api/workspaces (an admin caller receives EVERY entry including
// owner_email, members and the legacy_unowned annotation) and every write
// goes through POST /api/workspaces/assign (admin-only, enforced server
// side). NOTE: unlike /api/governance/* these routes are NOT etag-guarded,
// so mutations are plain api() POSTs without If-Match (last write wins on
// the workspaces store). The tab button and pane are injected here because
// the static markup for the governance panel lives in index.html.

let _govWsUserFilter = '';   // lowercased email of the per-user lens, '' = off

function _govEnsureWorkspacesTab() {
  const bar = document.querySelector('#mainGovernance .gov-tab-bar');
  const content = document.querySelector('#mainGovernance .gov-content');
  if (!bar || !content) return;
  if (!bar.querySelector('[data-gov-tab="workspaces"]')) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'gov-tab';
    btn.dataset.govTab = 'workspaces';
    btn.textContent = 'Workspaces';
    btn.addEventListener('click', () => { _govSwitchTab('workspaces'); });
    bar.insertBefore(btn, bar.querySelector('[data-gov-tab="approvals"]') || null);
  }
  if (!document.getElementById('govPaneWorkspaces')) {
    const pane = document.createElement('div');
    pane.className = 'gov-tab-pane';
    pane.id = 'govPaneWorkspaces';
    content.appendChild(pane);
  }
}

async function _govLoadWorkspaces() {
  const el = $('govPaneWorkspaces');
  if (!el) return;
  el.innerHTML = '<div class="gov-muted">' + _govT('loading', 'Loading...') + '</div>';
  let data;
  try {
    data = await api('/api/workspaces', { redirect401: false });
  } catch (e) {
    _govError(e, 'govPaneWorkspaces');
    return;
  }
  window.__GOV_WS__ = Array.isArray(data.workspaces) ? data.workspaces : [];
  window.__GOV_WS_ADMIN__ = !!data.viewer_is_admin;
  _govRenderWorkspaces();
}

/** All known emails for the per-user datalist: workspace owners plus members
 *  plus policy users (when the users tab already cached them). */
function _govWsKnownEmails() {
  const out = new Set();
  (window.__GOV_WS__ || []).forEach(w => {
    const owner = String(w.owner_email || '').trim().toLowerCase();
    if (owner) out.add(owner);
    (Array.isArray(w.members) ? w.members : []).forEach(m => {
      const email = String(m || '').trim().toLowerCase();
      if (email) out.add(email);
    });
  });
  Object.keys(window.__GOV_USERS__ || {}).forEach(email => {
    const norm = String(email || '').trim().toLowerCase();
    if (norm) out.add(norm);
  });
  return Array.from(out).sort();
}

function _govWsMembersLower(w) {
  return (Array.isArray(w.members) ? w.members : []).map(m => String(m || '').trim().toLowerCase()).filter(Boolean);
}

function _govRenderWorkspaces() {
  const el = $('govPaneWorkspaces');
  if (!el) return;
  const list = window.__GOV_WS__ || [];
  const isAdmin = !!window.__GOV_WS_ADMIN__;
  const filter = _govWsUserFilter;
  const options = _govWsKnownEmails().map(email => '<option value="' + _govEsc(email) + '"></option>').join('');
  const lensHeader = filter ? '<th>' + _govEsc(filter) + '</th>' : '';
  const rows = list.map((w, idx) => {
    const owner = String(w.owner_email || '').trim().toLowerCase();
    const members = _govWsMembersLower(w);
    const legacyBadge = w.legacy_unowned ? ' <span class="gov-chip">Legacy shared</span>' : '';
    let lensCell = '';
    if (filter) {
      let state, action = '';
      if (owner === filter) {
        state = '<span class="gov-chip">Owner</span>';
      } else if (members.includes(filter)) {
        state = '<span class="gov-chip">Member</span>';
        if (isAdmin) action = ' <button type="button" class="gov-btn danger" onclick="_govWsToggleMember(' + idx + ', false)">Remove ' + _govEsc(filter) + '</button>';
      } else {
        state = w.legacy_unowned
          ? '<span class="gov-muted">Shared (legacy)</span>'
          : '<span class="gov-muted">No access</span>';
        if (isAdmin) action = ' <button type="button" class="gov-btn primary" onclick="_govWsToggleMember(' + idx + ', true)">Add ' + _govEsc(filter) + '</button>';
      }
      lensCell = '<td class="gov-nowrap">' + state + action + '</td>';
    }
    const ownerCell = isAdmin
      ? '<input id="govWsOwner_' + idx + '" type="hidden" data-sp-picker data-sp-source="people" placeholder="' + _govT('picker_pick_owner', 'Pick an owner') + '" value="' + _govEsc(owner) + '">'
      : (_govEsc(owner) || '<span class="gov-muted">Shared (unowned)</span>');
    const membersCell = isAdmin
      ? '<input id="govWsMembers_' + idx + '" type="hidden" data-sp-picker data-sp-multi="1" data-sp-source="people" placeholder="' + _govT('picker_pick_members', 'Pick members') + '" value="' + _govEsc(members.join(', ')) + '">'
      : (_govEsc(members.join(', ')) || '<span class="gov-muted">none</span>');
    const actionsCell = isAdmin
      ? '<td class="gov-row-actions"><button type="button" class="gov-btn primary" onclick="_govSaveWorkspaceAssign(' + idx + ')">Save</button></td>'
      : '<td></td>';
    return '<tr>' +
      '<td class="gov-ws-name"><div>' + _govEsc(w.name || '') + legacyBadge + '</div><div class="gov-path" title="' + _govEsc(w.path || '') + '">' + _govEsc(w.path || '') + '</div></td>' +
      '<td>' + ownerCell + '</td>' +
      '<td>' + membersCell + '</td>' +
      lensCell +
      actionsCell +
    '</tr>';
  }).join('');
  const colCount = filter ? 5 : 4;
  el.innerHTML =
    '<div class="gov-form">' +
      '<div class="gov-form-title">Per-user view</div>' +
      '<div class="gov-form-row"><label for="govWsUserFilter">User email</label>' +
        '<input id="govWsUserFilter" type="hidden" data-sp-picker data-sp-source="people" placeholder="' + _govT('picker_pick_person', 'Pick a person') + '" value="' + _govEsc(filter) + '" onchange="_govApplyWsUserFilter()"></div>' +
      '<datalist id="govWsUserEmails">' + options + '</datalist>' +
      '<div class="gov-form-actions">' +
        '<button type="button" class="gov-btn primary" onclick="_govApplyWsUserFilter()">Apply</button>' +
        '<button type="button" class="gov-btn" onclick="_govClearWsUserFilter()">Clear</button>' +
      '</div>' +
    '</div>' +
    (isAdmin ? '' : '<div class="gov-muted">You are not a workspace admin: the list below only shows your own entries and editing is disabled.</div>') +
    '<div class="gov-table-wrap"><table class="gov-table"><thead><tr>' +
      '<th>Name</th><th>Owner</th><th>Members</th>' + lensHeader + '<th></th>' +
    '</tr></thead><tbody>' +
    (rows || '<tr><td colspan="' + colCount + '" class="gov-muted">No workspaces configured.</td></tr>') +
    '</tbody></table></div>' +
    '<div class="gov-muted">Owner and members control who sees a workspace. An entry without either is legacy shared: visible to every signed-in user. Clearing the owner field returns an entry to legacy shared.</div>';
  if (window.SpPicker) SpPicker.mountAll(el);
}

function _govApplyWsUserFilter() {
  _govWsUserFilter = String(($('govWsUserFilter') || {}).value || '').trim().toLowerCase();
  _govRenderWorkspaces();
}

function _govClearWsUserFilter() {
  _govWsUserFilter = '';
  _govRenderWorkspaces();
}

/** POST /api/workspaces/assign and refresh the cached list from the reply. */
async function _govWsAssign(body, okMsg) {
  try {
    const res = await api('/api/workspaces/assign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      redirect401: false,
    });
    if (Array.isArray(res.workspaces)) window.__GOV_WS__ = res.workspaces;
    if (typeof showToast === 'function') showToast(okMsg, 2500);
    _govRenderWorkspaces();
  } catch (e) {
    // 403 forbidden (not a workspace admin) or 400 validation from the server
    let msg = (e && e.message) ? e.message : 'request failed';
    if (e && Number(e.status) === 403 && (e.reason || e.resource)) {
      msg = 'Access restricted' + (e.resource ? ': ' + e.resource : '') + (e.reason ? ' (' + e.reason + ')' : '');
    }
    if (typeof showToast === 'function') showToast('Ownership save failed: ' + msg, 4000, 'error');
  }
}

async function _govSaveWorkspaceAssign(idx) {
  const w = (window.__GOV_WS__ || [])[idx];
  if (!w) return;
  const owner = String(($('govWsOwner_' + idx) || {}).value || '').trim().toLowerCase();
  const seen = new Set();
  const members = _govCsv(($('govWsMembers_' + idx) || {}).value)
    .map(m => m.toLowerCase())
    .filter(m => (seen.has(m) ? false : (seen.add(m), true)));
  // owner_email '' clears the owner (back to legacy shared); [] clears members
  await _govWsAssign({ path: w.path, owner_email: owner, members: members }, 'Ownership saved');
}

/** Quick add/remove of the per-user lens email as a member. Owner untouched
 *  (the request only carries the members key, so owner_email is unchanged). */
async function _govWsToggleMember(idx, add) {
  const w = (window.__GOV_WS__ || [])[idx];
  const email = _govWsUserFilter;
  if (!w || !email) return;
  const members = _govWsMembersLower(w).filter(m => m !== email);
  if (add) members.push(email);
  await _govWsAssign({ path: w.path, members: members }, add ? 'Member added' : 'Member removed');
}

// ── Approvals tab ─────────────────────────────────────────────────────────
// Pending user-requested capabilities (api/approvals registry): skills, third
// party integrations, MCP servers and CLI commands. Approve grants the item;
// rejecting a skill also deletes it from disk. Server-gated by governance:write.
//
// Rows carry their kind, so the decide POST sends {kind, key, decision} with
// the row's own kind instead of the hardcoded 'skill' this tab started with.

// Display order of the kind groups; unknown kinds sort last, alphabetically.
const _GOV_APPROVAL_KINDS = ['grant', 'skill', 'integration', 'mcp', 'cli'];

function _govApprovalKind(item) {
  return String((item && item.kind) || 'skill').trim().toLowerCase() || 'skill';
}

function _govKindLabel(kind) {
  switch (kind) {
    case 'skill': return _govT('governance_kind_skill', 'Skill');
    case 'integration': return _govT('governance_kind_integration', 'Integration');
    case 'mcp': return _govT('governance_kind_mcp', 'MCP');
    case 'cli': return _govT('governance_kind_cli', 'CLI');
    case 'grant': return _govT('governance_kind_grant', 'Access');
    default: return kind;
  }
}

// Reuse the capability chip colours already used by the users/groups tabs.
function _govKindChipClass(kind) {
  if (kind === 'skill' || kind === 'mcp' || kind === 'cli' || kind === 'integration') {
    return ' gov-chip-' + kind;
  }
  return '';
}

// Denial reasons the engine spools (hermes_cli.dashboard_governance
// .grant_requests._map_denial). The list exists so an unrecognised reason
// renders as nothing at all rather than putting an internal slug on screen.
const _GOV_DENIAL_REASONS = [
  'cli_command_not_allowed', 'skill_not_allowed', 'cli_workdir_not_allowed',
  'file_read_root_not_allowed', 'file_write_root_not_allowed', 'mcp_server_not_allowed',
  'profile_not_allowed', 'workspace_not_allowed', 'tool_not_allowed',
  'file_denied_glob', 'route_not_allowed',
];

function _govDenialReasonLabel(reason) {
  return _GOV_DENIAL_REASONS.indexOf(String(reason || '')) === -1
    ? '' : _govT('governance_reason_blocked', 'Stopped by the access rules');
}

/** Compact one-line rendering of an approval payload (address, provider, ...).
 *
 * Allowlisted per kind (28 Aug 2026 ticket). The previous version walked
 * Object.keys(payload), so every field any producer ever added rendered itself
 * on the approver's screen unreviewed: the MCP row showed the header name it
 * needs and the profile it was requested under, neither of which helps a
 * decision. Only fields written for a person to read are shown now. The MCP
 * address is kept in full on purpose: two endpoints on one host are different
 * services, and that is exactly what the approver is deciding about.
 */
function _govPayloadSummary(payload, kind) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return '';
  const trim = (v) => {
    const text = String(v === null || v === undefined ? '' : v).trim();
    return text.length > 120 ? text.slice(0, 117) + '...' : text;
  };
  const parts = [];
  if (kind === 'grant') {
    parts.push(_govDenialReasonLabel(payload.reason));
    const count = Number(payload.count);
    if (Number.isFinite(count) && count > 1) {
      const label = (typeof t === 'function') ? t('governance_blocked_times', count) : '';
      parts.push(label && label !== 'governance_blocked_times' ? label : ('Blocked ' + count + ' times'));
    }
  } else if (kind === 'mcp') {
    parts.push(trim(payload.url));
    parts.push(trim(payload.description));
  } else {
    parts.push(trim(payload.description));
  }
  return parts.filter(Boolean).slice(0, 4).join(' | ');
}

function _govRiskLabel(id) {
  switch (id) {
    case 'external_comms': return _govT('governance_risk_external_comms', 'Sends messages outside this workstation');
    case 'data_access': return _govT('governance_risk_data_access', 'Reads data belonging to people');
    case 'file_write': return _govT('governance_risk_file_write', 'Creates or changes files');
    case 'scheduling': return _govT('governance_risk_scheduling', 'Runs work on a schedule');
    case 'financial': return _govT('governance_risk_financial', 'Can cause spend');
    default: return '';
  }
}

/** The capability and risk detail behind one approval (28 Aug 2026 ticket).
 *
 * Everything here comes from the server's permission metadata; nothing is
 * derived in the browser. Every value is escaped, including inside the title
 * attributes: a skill's description is written by whoever wrote the skill, and
 * a skill author must never be able to put markup on an approver's screen.
 */
/** The advisory block: why they asked, the worst case, and a recommendation.
 *
 * Rendered above the catalogue detail because it is what an approver reads
 * first. Every value is escaped: the requester's own words and a model's
 * output are both untrusted text, never markup. The source line is never
 * dropped, so advice written from the fixed catalogue can never be mistaken
 * for a considered opinion about this particular person.
 */
function _govRecommendationLabel(value) {
  switch (String(value || '')) {
    case 'grant': return _govT('advice_rec_grant', 'Grant it');
    case 'grant_narrower': return _govT('advice_rec_grant_narrower', 'Grant something narrower');
    case 'decline': return _govT('advice_rec_decline', 'Do not grant');
    case 'needs_more_information': return _govT('advice_rec_needs_more_information', 'Ask them first');
    default: return '';
  }
}

function _govRecommendationClass(value) {
  switch (String(value || '')) {
    case 'grant': return 'on';
    case 'decline': return 'off';
    default: return 'warn';
  }
}

function _govAdviceHtml(advice) {
  if (!advice || typeof advice !== 'object') return '';
  const recommendation = String(advice.recommendation || '');
  const label = _govRecommendationLabel(recommendation);
  const row = (key, value) => {
    const text = String(value || '').trim();
    if (!text) return '';
    return '<div class="gov-explain-row"><span class="gov-explain-key">' + _govEsc(key)
      + '</span><span class="gov-explain-val">' + _govEsc(text) + '</span></div>';
  };
  let body = '';
  body += row(_govT('advice_their_words', 'In their own words'), advice.requester_ask);
  body += row(_govT('advice_why', 'Why they asked'), advice.why);
  body += row(_govT('advice_risk', 'Worst case'), advice.risk);
  if (label) {
    body += '<div class="gov-explain-row"><span class="gov-explain-key">'
      + _govEsc(_govT('advice_recommendation', 'Advice')) + '</span><span class="gov-explain-val">'
      + '<span class="gov-pill ' + _govRecommendationClass(recommendation) + '">' + _govEsc(label) + '</span> '
      + _govEsc(String(advice.recommendation_reason || '')) + '</span></div>';
  }
  body += row(_govT('governance_explain_alternatives', 'Narrower alternative'), advice.narrower_alternative);
  if (!body) return '';
  const source = String(advice.source || '') === 'model'
    ? _govT('advice_from_model', 'Written by the assistant')
    : _govT('advice_from_rules', 'From the risk catalogue');
  const note = String(advice.note || '').trim();
  body += '<div class="gov-explain-row gov-muted"><span class="gov-explain-key"></span>'
    + '<span class="gov-explain-val">' + _govEsc(source + (note ? '. ' + note : '')) + '</span></div>';
  const pill = label
    ? '<span class="gov-pill ' + _govRecommendationClass(recommendation) + '">' + _govEsc(label) + '</span>'
    : '';
  return '<details class="gov-explain" open><summary class="gov-explain-summary">'
    + '<span class="gov-explain-toggle">' + _govEsc(_govT('advice_title', 'What this is and what we advise'))
    + '</span>' + pill + '</summary><div class="gov-explain-body">' + body + '</div></details>';
}

function _govExplainHtml(ex) {
  const risks = Array.isArray(ex.risks) ? ex.risks : [];
  const chips = risks.map(id => {
    const label = _govRiskLabel(id);
    if (!label) return '';
    return '<span class="gov-risk gov-risk-' + _govEsc(id) + '" title="' + _govEsc(label) + '">'
      + _govEsc(label) + '</span>';
  }).join('');
  const row = (label, value, sep) => {
    const text = Array.isArray(value) ? value.filter(Boolean).join(sep || ' | ') : String(value || '');
    if (!text) return '';
    return '<div class="gov-explain-row"><span class="gov-explain-key">' + _govEsc(label)
      + '</span><span class="gov-explain-val">' + _govEsc(text) + '</span></div>';
  };
  const body =
    row(_govT('governance_explain_capability', 'Capability'), ex.capability)
    + row(_govT('governance_explain_data', 'Data it can reach'), ex.data)
    + row(_govT('governance_explain_tools', 'Tools'), ex.tools, ', ')
    + row(_govT('governance_explain_systems', 'External systems'), ex.external_systems, ', ')
    + row(_govT('governance_explain_permission', 'Permission still required'), ex.permissions, ', ')
    + row(_govT('governance_explain_permission_notes', 'What that permission covers'), ex.permission_notes, ' ')
    + row(_govT('governance_explain_scope', 'Applies to'), ex.scope_text)
    + row(_govT('governance_explain_duration', 'Duration'), ex.duration)
    + row(_govT('governance_explain_mitigations', 'Recommended mitigations'), ex.mitigations, ' ')
    + row(_govT('governance_explain_alternatives', 'Narrower alternative'), ex.alternatives, ' ')
    + row(_govT('governance_explain_dependencies', 'Also needed'), ex.dependencies, ' ')
    + row(_govT('governance_explain_target', 'Policy entry changed'), ex.policy_target, ', ');
  if (!body) return '';
  return '<details class="gov-explain"><summary class="gov-explain-summary">'
    + '<span class="gov-explain-toggle">' + _govEsc(_govT('governance_explain_toggle', 'What this grants')) + '</span>'
    + chips + '</summary><div class="gov-explain-body">' + body + '</div></details>';
}

function _govUpdateApprovalsBadge(count) {
  const badge = $('govApprovalsBadge');
  if (!badge) return;
  const n = Number(count) || 0;
  badge.textContent = n > 0 ? String(n) : '';
  badge.style.display = n > 0 ? '' : 'none';
}

/** Refresh the pending-count badge without switching to the tab. */
async function _govRefreshApprovalsBadge() {
  try {
    // The queue returns every kind, so the badge counts every kind too.
    const data = await api('/api/governance/approvals', { redirect401: false, timeoutToast: false, timeoutMs: 15000 });
    _govUpdateApprovalsBadge((data.pending || []).length);
  } catch (e) { /* not an admin or endpoint unavailable; leave badge hidden */ }
}

/** One pending row. Skill rows keep rendering their full key, as before. */
function _govApprovalRow(item) {
  const kind = _govApprovalKind(item);
  const key = String(item.key || '');
  const label = String(item.label || item.name || '');
  let when = '';
  // added_at / requested_at are epoch seconds (float) on every kind.
  const ts = Number(item.added_at !== undefined && item.added_at !== null ? item.added_at : item.requested_at);
  if (Number.isFinite(ts) && ts > 0) when = new Date(ts * 1000).toLocaleString();
  const primary = (kind === 'skill' || !label) ? key : label;
  const secondary = (primary !== key && key)
    ? '<div class="gov-path gov-muted">' + _govEsc(key) + '</div>'
    : '';
  const summary = _govPayloadSummary(item.payload, kind);
  const detail = summary ? '<div class="gov-muted">' + _govEsc(summary) + '</div>' : '';
  // The ask behind the request (27 Aug 2026 ticket): an approver needs to see
  // what the person actually wanted, not only the derived capability. Already
  // redacted and truncated server-side; rendered as escaped text, never HTML.
  const trigger = (item.payload && typeof item.payload === 'object')
    ? String(item.payload.trigger || '') : '';
  const triggerHtml = trigger
    ? '<div class="gov-trigger" title="' + _govEsc(trigger) + '">'
      + '<span class="gov-trigger-label">' + _govEsc(_govT('governance_trigger', 'Asked for')) + ':</span> '
      + _govEsc(trigger) + '</div>'
    : '';
  const ex = (item.explanation && typeof item.explanation === 'object') ? item.explanation : null;
  const plainCapability = ex && ex.capability ? String(ex.capability) : _govT('governance_explain_unavailable', 'The effect of this access has not been verified. Ask for the exact action and scope before deciding.');
  const plainScope = ex && ex.scope_text ? String(ex.scope_text) : '';
  const plainExplanation = '<div class="gov-approval-explanation">' + _govEsc(plainCapability)
    + (plainScope ? '<br>' + _govEsc(plainScope) : '') + '</div>';
  const explainHtml = ex ? _govExplainHtml(ex) : '';
  const adviceHtml = _govAdviceHtml(item.advice);
  // Related access (ticket 10) hangs off access requests only: the chain it
  // walks is the route/permission one, which the other kinds do not have.
  const sugHtml = kind === 'grant'
    ? '<button type="button" class="gov-btn gov-sug-toggle" data-gov-suggest-toggle="1"'
      + ' data-key="' + _govEsc(key) + '">'
      + _govEsc(_govT('governance_sug_toggle', 'Related access')) + '</button>'
    : '';
  const sugRow = kind === 'grant'
    ? '<tr class="gov-sug-row" style="display:none"><td colspan="5" class="gov-sug-cell"></td></tr>'
    : '';
  const btn = (decision, cls, label2) => '<button type="button" class="gov-btn ' + cls + '"' +
    ' data-gov-approval="' + decision + '"' +
    ' data-kind="' + _govEsc(kind) + '"' +
    ' data-key="' + _govEsc(key) + '">' + label2 + '</button>';
  return '<tr>' +
    '<td class="gov-nowrap"><span class="gov-chip' + _govKindChipClass(kind) + '">' + _govEsc(_govKindLabel(kind)) + '</span></td>' +
    '<td>' + _govEsc(primary) + secondary + detail + plainExplanation + triggerHtml + adviceHtml + explainHtml + sugHtml + '</td>' +
    '<td>' + _govEsc(item.owner_email || '') + '</td>' +
    '<td class="gov-nowrap">' + _govEsc(when) + '</td>' +
    '<td class="gov-row-actions">' +
      btn('approve', 'primary', _govT('governance_approve', 'Approve')) +
      btn('reject', 'danger', _govT('governance_reject', 'Reject')) +
    '</td></tr>' + sugRow;
}

function _govSugRiskPill(risk) {
  const r = String(risk || 'medium');
  const cls = r === 'high' ? 'off' : (r === 'low' ? 'on' : 'warn');
  const label = r === 'high'
    ? _govT('governance_sug_risk_high', 'High risk')
    : (r === 'low' ? _govT('governance_sug_risk_low', 'Low risk')
                   : _govT('governance_sug_risk_medium', 'Medium risk'));
  return '<span class="gov-pill ' + cls + '">' + _govEsc(label) + '</span>';
}

function _govSugStatusLabel(status) {
  switch (String(status || '')) {
    case 'approved': return _govT('governance_sug_approved', 'Approved');
    case 'denied': return _govT('governance_sug_denied', 'Denied');
    case 'ignored': return _govT('governance_sug_ignored', 'Set aside');
    default: return '';
  }
}

/** One related suggestion. Decided on its own, never bundled with a sibling.
 *
 * A suggestion the server marked non-actionable renders WITHOUT an approve
 * button and says so: the point of this screen is to tell an administrator
 * about the rest of the chain, never to turn a shell or settings-write
 * capability into one click. The server refuses those as well.
 */
function _govSuggestionHtml(item, originKey) {
  const gkind = String(item.gkind || '');
  const value = String(item.value || '');
  const status = String(item.status || 'open');
  const decided = status !== 'open';
  const evidence = (Array.isArray(item.evidence) ? item.evidence : [])
    .map(line => '<div class="gov-sug-evidence">' + _govEsc(String(line)) + '</div>').join('');
  const abtn = (decision, cls, label) => '<button type="button" class="gov-btn ' + cls + '"' +
    ' data-gov-suggestion="' + decision + '"' +
    ' data-origin="' + _govEsc(originKey) + '"' +
    ' data-gkind="' + _govEsc(gkind) + '"' +
    ' data-value="' + _govEsc(value) + '">' + _govEsc(label) + '</button>';
  let actions;
  if (decided) {
    actions = '<div class="gov-sug-info">' + _govEsc(_govSugStatusLabel(status))
      + (item.decided_by ? ' \u00b7 ' + _govEsc(_govT('governance_sug_decided_by', 'Decided by'))
        + ': ' + _govEsc(String(item.decided_by)) : '') + '</div>';
  } else {
    actions = '<div class="gov-sug-actions">'
      + (item.actionable ? abtn('approve', 'primary', _govT('governance_sug_approve', 'Approve')) : '')
      + abtn('deny', 'danger', _govT('governance_sug_deny', 'Deny'))
      + abtn('ignore', '', _govT('governance_sug_ignore', 'Ignore'))
      + (item.actionable ? '' : '<span class="gov-sug-info">'
        + _govEsc(_govT('governance_sug_manual',
          'Not grantable from here. Change it in the access rules.')) + '</span>')
      + '</div>';
  }
  return '<div class="gov-sug-item' + (decided ? ' is-decided' : '') + '">'
    + '<div class="gov-sug-label">' + _govSugRiskPill(item.risk) + _govEsc(String(item.label || value)) + '</div>'
    + '<div class="gov-sug-why">' + _govEsc(String(item.why || '')) + '</div>'
    + (item.risk_note ? '<div class="gov-sug-evidence">' + _govEsc(String(item.risk_note)) + '</div>' : '')
    + evidence + actions + '</div>';
}

/** Load and render the related access for one request into its detail row.
 *
 * The two confidence levels are rendered as two labelled blocks and are never
 * merged: a dependency read out of the rules that do the blocking and a guess
 * from a pattern must not look the same to whoever is deciding.
 */
async function _govLoadSuggestions(key, host) {
  if (!host) return;
  host.innerHTML = '<div class="gov-muted">' + _govT('loading', 'Loading...') + '</div>';
  let data;
  try {
    data = await api('/api/governance/approvals/suggestions?key=' + encodeURIComponent(key),
      { redirect401: false });
  } catch (e) {
    host.innerHTML = '<div class="gov-muted">' + _govEsc(e.message || 'failed') + '</div>';
    return;
  }
  const rows = Array.isArray(data && data.suggestions) ? data.suggestions : [];
  if (!rows.length) {
    host.innerHTML = '<div class="gov-muted">'
      + _govEsc(_govT('governance_sug_none', 'Nothing else is standing in this person\u2019s way.'))
      + '</div>';
    return;
  }
  const block = (confidence, title, pill) => {
    const items = rows.filter(r => String(r.confidence || '') === confidence);
    if (!items.length) return '';
    return '<div class="gov-sug-block"><div class="gov-sug-head">'
      + '<span class="gov-pill ' + pill + '">' + _govEsc(title) + '</span></div>'
      + items.map(item => _govSuggestionHtml(item, key)).join('') + '</div>';
  };
  host.innerHTML =
    '<div class="gov-sug-note">' + _govEsc(_govT('governance_sug_note',
      'Each item is decided on its own. Nothing here is granted unless you approve it.')) + '</div>'
    + block('confirmed', _govT('governance_sug_confirmed', 'Needed for this to work'), 'on')
    + block('heuristic', _govT('governance_sug_heuristic', 'Possibly related'), 'warn');
}

/** Related-access clicks inside the approvals pane. True when handled.
 *
 * Lives beside the decide-button delegation rather than in a listener of its
 * own, and the two probe disjoint attributes, so a click on one can never
 * decide the other. The detail row is always the sibling right after its
 * request row, which is why no generated id is needed for a key that carries
 * '|', '@' and '/'.
 */
function _govSuggestionClick(closest) {
  const toggle = closest('[data-gov-suggest-toggle]');
  if (toggle) {
    if (toggle.disabled) return true;
    const row = toggle.closest('tr');
    const host = row && row.nextElementSibling;
    if (!host || !host.classList.contains('gov-sug-row')) return true;
    const open = host.style.display !== 'none';
    host.style.display = open ? 'none' : '';
    if (!open) _govLoadSuggestions(toggle.getAttribute('data-key') || '', host.querySelector('td'));
    return true;
  }
  const sug = closest('[data-gov-suggestion]');
  if (!sug) return false;
  if (!sug.disabled) {
    _govDecideSuggestion(
      sug.getAttribute('data-origin') || '',
      sug.getAttribute('data-gkind') || '',
      sug.getAttribute('data-value') || '',
      sug.getAttribute('data-gov-suggestion') || '',
      sug.closest('.gov-sug-cell'),
    );
  }
  return true;
}

async function _govDecideSuggestion(origin, gkind, value, decision, host) {
  try {
    await _govPost('/api/governance/approvals/suggestions/decide', {
      origin_key: origin, gkind: gkind, value: value, decision: decision,
    });
    if (typeof showToast === 'function') {
      showToast(_govSugStatusLabel(
        decision === 'approve' ? 'approved' : (decision === 'deny' ? 'denied' : 'ignored')
      ), 2500);
    }
    // Only this block is re-rendered: reloading the whole queue would collapse
    // the detail the administrator is working in and lose their place.
    await _govLoadSuggestions(origin, host);
    _govRefreshApprovalsBadge();
  } catch (e) {
    if (!_govHandleConflict(e) && typeof showToast === 'function') showToast(e.message || 'decision failed', 4000, 'error');
  }
}

async function _govLoadApprovals() {
  const el = $('govPaneApprovals');
  if (!el) return;
  el.innerHTML = '<div class="gov-muted">' + _govT('loading', 'Loading...') + '</div>';
  let data;
  try {
    data = await api('/api/governance/approvals', { redirect401: false });
  } catch (e) {
    _govError(e, 'govPaneApprovals');
    return;
  }
  const pending = data.pending || [];
  _govUpdateApprovalsBadge(pending.length);
  // Group by kind, keeping the server's oldest-first order inside each group.
  const groups = new Map();
  pending.forEach(item => {
    const kind = _govApprovalKind(item);
    if (!groups.has(kind)) groups.set(kind, []);
    groups.get(kind).push(item);
  });
  const kinds = Array.from(groups.keys()).sort((a, b) => {
    const ia = _GOV_APPROVAL_KINDS.indexOf(a);
    const ib = _GOV_APPROVAL_KINDS.indexOf(b);
    if (ia !== ib) return (ia === -1 ? _GOV_APPROVAL_KINDS.length : ia) - (ib === -1 ? _GOV_APPROVAL_KINDS.length : ib);
    return a.localeCompare(b);
  });
  const showHeaders = kinds.length > 1;
  const rows = kinds.map(kind => {
    const items = groups.get(kind) || [];
    const header = showHeaders
      ? '<tr class="gov-approval-group"><td colspan="5">' + _govEsc(_govKindLabel(kind)) + ' (' + items.length + ')</td></tr>'
      : '';
    return header + items.map(_govApprovalRow).join('');
  }).join('');
  el.innerHTML =
    '<table class="gov-table"><thead><tr>' +
      '<th>' + _govT('governance_col_kind', 'Kind') + '</th>' +
      '<th>' + _govT('governance_col_item', 'Item') + '</th>' +
      '<th>' + _govT('governance_col_requested_by', 'Requested by') + '</th>' +
      '<th>' + _govT('governance_col_requested_at', 'Requested') + '</th><th></th>' +
    '</tr></thead><tbody>' +
    (rows || '<tr><td colspan="5" class="gov-muted">' + _govT('governance_no_pending', 'No pending approvals.') + '</td></tr>') +
    '</tbody></table>' +
    '<div class="gov-muted">' + _govT('governance_approvals_note',
      'Approve grants the request. Rejecting a skill also deletes it from disk.') + '</div>';
}

async function _govDecideApproval(kind, key, decision) {
  try {
    await _govPost('/api/governance/approvals/decide', {
      kind: String(kind || 'skill'),
      key: key,
      decision: decision,
    });
    if (typeof showToast === 'function') {
      showToast(decision === 'approve' ? _govT('governance_approved', 'Approved') : _govT('governance_rejected', 'Rejected'), 2500);
    }
    await _govLoadApprovals();
  } catch (e) {
    if (!_govHandleConflict(e) && typeof showToast === 'function') showToast(e.message || 'decision failed', 4000, 'error');
  }
}

// ── Integrations tab ──────────────────────────────────────────────────────
// Admin control plane for third-party access: every provider that is enabled
// in Nango, has an approval entry, or has live connections. All actions are
// buttons; nothing is typed. Enabling new providers happens from the
// Integrations panel (Enable button on any catalog card).

async function _govLoadIntegrations() {
  const el = $('govPaneIntegrations');
  if (!el) return;
  el.innerHTML = '<div class="gov-muted">' + _govT('loading', 'Loading...') + '</div>';
  let catalog, connections;
  try {
    const opts = { redirect401: false, timeoutToast: false, timeoutMs: 20000 };
    [catalog, connections] = await Promise.all([
      api('/api/integrations/catalog', opts),
      api('/api/integrations/connections', opts).catch(() => ({ connections: [] })),
    ]);
  } catch (e) {
    _govError(e, 'govPaneIntegrations');
    return;
  }
  const conns = (connections && connections.connections) || [];
  const connsByKey = new Map();
  conns.forEach(c => {
    const k = String((c && c.provider_config_key) || '');
    if (!k) return;
    if (!connsByKey.has(k)) connsByKey.set(k, []);
    connsByKey.get(k).push(c);
  });
  const governed = ((catalog && catalog.providers) || []).filter(p => {
    const approval = String(p.approval_status || '');
    return p.configured
      || (approval !== '' && approval !== 'none')
      || connsByKey.has(String(p.unique_key || ''))
      || connsByKey.has(String(p.key || ''));
  });
  const nangoDown = ((catalog && catalog.nango) || {}).available === false;
  const banner = nangoDown
    ? '<div class="gov-muted" style="margin-bottom:10px">' + _govT('governance_intg_nango_down',
        'The connection service is unavailable; showing the last known state, actions may fail.') + '</div>'
    : '';
  const rows = governed.map(p => {
    const key = String(p.unique_key || p.key || '');
    const rowConns = connsByKey.get(key) || [];
    const owners = Array.from(new Set(rowConns.map(c => {
      const eu = (c && c.end_user) || {};
      return String(eu.email || eu.id || '').replace(/^u-/, '');
    }).filter(Boolean)));
    const approval = String(p.approval_status || '');
    const badges =
      (p.configured ? '<span class="gov-pill on">' + _govT('governance_intg_enabled', 'Enabled') + '</span>' : '') +
      (approval === 'approved' ? '<span class="gov-pill on">' + _govT('governance_approved', 'Approved') + '</span>' : '') +
      (approval === 'pending' ? '<span class="gov-pill warn">' + _govT('governance_pending', 'Pending') + '</span>' : '') +
      (approval === 'rejected' ? '<span class="gov-pill off">' + _govT('governance_rejected', 'Rejected') + '</span>' : '');
    const connCell = rowConns.length
      ? String(rowConns.length) + (owners.length ? ' <span class="gov-muted">(' + _govEsc(owners.join(', ')) + ')</span>' : '')
      : '<span class="gov-muted">0</span>';
    const btn = (label, action, cls, disabled, title) =>
      '<button type="button" class="gov-btn ' + (cls || '') + '"' + (disabled ? ' disabled' : '') +
      (title ? ' title="' + _govEsc(title) + '"' : '') +
      ' onclick="_govIntgAction(\'' + action + '\', \'' + _govEsc(key) + '\')">' + _govEsc(label) + '</button>';
    let actions = '';
    if (approval === 'pending') {
      actions += btn(_govT('governance_approve', 'Approve'), 'approve', 'primary');
      actions += btn(_govT('governance_reject', 'Reject'), 'reject', 'danger');
    } else if (p.configured) {
      actions += btn(_govT('governance_intg_disable', 'Disable'), 'disable', 'danger',
        rowConns.length > 0, rowConns.length > 0 ? _govT('governance_intg_disable_blocked', 'Disconnect its connections first') : '');
    } else {
      if (approval === 'approved' || approval === 'rejected') {
        actions += btn(_govT('governance_intg_enable', 'Enable'), 'enable', 'primary');
        actions += btn(_govT('governance_intg_revoke', 'Revoke'), 'revoke', '');
      }
    }
    const logo = p.logo ? '<img class="gov-intg-logo" src="' + _govEsc(p.logo) + '" alt="" loading="lazy" onerror="this.remove()">' : '';
    return '<tr>' +
      '<td class="gov-intg-name">' + logo + '<div><div>' + _govEsc(p.display_name || key) + '</div>' +
        '<div class="gov-muted gov-mono">' + _govEsc(key) + '</div></div></td>' +
      '<td>' + _govEsc(p.auth_mode || '') + '</td>' +
      '<td>' + badges + '</td>' +
      '<td>' + connCell + '</td>' +
      '<td class="gov-row-actions">' + actions + '</td>' +
    '</tr>';
  }).join('');
  el.innerHTML = banner +
    '<table class="gov-table"><thead><tr>' +
      '<th>' + _govT('governance_col_provider', 'Provider') + '</th>' +
      '<th>' + _govT('governance_col_auth', 'Auth') + '</th>' +
      '<th>' + _govT('governance_col_status', 'Status') + '</th>' +
      '<th>' + _govT('governance_col_connections', 'Connections') + '</th><th></th>' +
    '</tr></thead><tbody>' +
    (rows || '<tr><td colspan="5" class="gov-muted">' + _govT('governance_intg_none',
      'No governed integrations yet. Enable providers from the Integrations panel.') + '</td></tr>') +
    '</tbody></table>' +
    '<div class="gov-muted">' + _govT('governance_intg_note',
      'Approve/Enable makes a provider connectable for everyone; Disable removes its connection configuration (blocked while connections exist); Revoke returns it to the request flow.') +
    ' <a href="#" onclick="switchPanel(\'integrations\');return false">' + _govT('governance_intg_open_panel', 'Open the Integrations panel') + '</a></div>';
}

async function _govIntgAction(action, key) {
  try {
    if (action === 'approve' || action === 'reject') {
      await _govPost('/api/governance/approvals/decide', { kind: 'integration', key: key, decision: action });
    } else if (action === 'revoke') {
      await _govPost('/api/governance/approvals/revoke', { kind: 'integration', key: key });
    } else if (action === 'enable') {
      const payload = await _intgEnablePayload(key);
      if (payload === null) return;
      await _govPost('/api/integrations/enable', payload);
    } else if (action === 'disable') {
      await _govPost('/api/integrations/disable', { provider_config_key: key });
    } else {
      return;
    }
    if (typeof showToast === 'function') showToast(_govT('governance_saved', 'Saved'), 2500);
  } catch (e) {
    if (!_govHandleConflict(e) && typeof showToast === 'function') showToast(e.message || 'action failed', 5000, 'error');
  }
  await _govLoadIntegrations();
  _govRefreshApprovalsBadge();
}

// ── Preview access tab ────────────────────────────────────────────────────

let _govPreviewRequest = 0;

async function _govRunPreview() {
  const request = ++_govPreviewRequest;
  const out = $('govPreviewResult');
  if (!out) return;
  const email = String(($('govPreviewEmail') || {}).value || '').trim();
  if (!email || !email.includes('@')) {
    out.innerHTML = '<div class="gov-error">' + _govT('governance_invalid_email', 'Enter a valid email address') + '</div>';
    return;
  }
  const groups = _govCsv(($('govPreviewGroups') || {}).value);
  out.innerHTML = '<div class="gov-muted">' + _govT('loading', 'Loading...') + '</div>';
  let data;
  try {
    data = await api('/api/governance/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(groups.length ? { email: email, groups: groups } : { email: email }),
      redirect401: false,
    });
  } catch (e) {
    if (request !== _govPreviewRequest) return;
    _govError(e, 'govPreviewResult');
    return;
  }
  if (request !== _govPreviewRequest) return;
  const access = data.effective_access || {};
  const svg = (path) => '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px" aria-hidden="true">' + path + '</svg>';
  const chips = list => (list || []).map(v => '<span class="gov-chip gov-chip-role">' + _govEsc(v) + '</span>').join(' ') ||
    '<span class="gov-muted">' + _govT('governance_none', 'none') + '</span>';
  const section = (label, body, iconSvg) => '<div class="gov-stat" style="margin-bottom:10px;padding:12px 14px">' +
    '<div class="gov-stat-label" style="margin-bottom:6px">' + iconSvg + ' ' + _govEsc(label) + '</div>' + body + '</div>';
  const iconRole = svg('<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>');
  const iconGroup = svg('<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>');
  const iconPerm = svg('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>');
  const iconProfile = svg('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.09-3.09a2 2 0 0 0-2.82 0L6 21"/>');
  const iconRoutes = svg('<circle cx="6" cy="19" r="3"/><circle cx="18" cy="5" r="3"/><path d="M8.6 13.5l6.8 4"/><path d="M15.4 6.5 8.6 10.5"/>');
  const iconSources = svg('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>');
  const denials = [];
  const collectDenials = (value, path) => {
    if (Array.isArray(value)) value.forEach(item => denials.push(path + ': ' + String(item)));
    else if (value && typeof value === 'object') Object.entries(value).forEach(([key, child]) => collectDenials(child, path ? path + ' / ' + key : key));
  };
  collectDenials(access.deny || {}, '');
  out.innerHTML =
    section(_govT('governance_user_policy', 'User policy'), chips([
      access.access_level || _govT('governance_inherited_level', 'Existing roles'),
      access.access_mode || _govT('governance_legacy_policy', 'Existing policy'),
      access.approval_mode || _govT('governance_legacy_approval', 'Existing approval behavior'),
    ]), iconPerm) +
    section(_govT('governance_col_roles', 'Roles'), chips(access.roles), iconRole) +
    section(_govT('governance_col_groups', 'Groups'), chips(access.groups), iconGroup) +
    section(_govT('governance_permissions', 'Permissions'), chips(access.permissions), iconPerm) +
    section(_govT('governance_profiles', 'Profiles'), chips(access.profiles), iconProfile) +
    section(_govT('governance_routes', 'Routes'), chips(access.routes), iconRoutes) +
    section(_govT('governance_explicit_denials', 'Explicit denials'), chips(denials), iconPerm) +
    section(_govT('governance_grant_sources', 'Grant sources'), chips(data.grant_sources), iconSources);
}

// ── Audit tab ─────────────────────────────────────────────────────────────

async function _govLoadAudit() {
  const el = $('govPaneAudit');
  if (!el) return;
  el.innerHTML = '<div class="gov-muted">' + _govT('loading', 'Loading...') + '</div>';
  let data;
  try {
    data = await api('/api/governance/audit?limit=100', { redirect401: false });
  } catch (e) {
    _govError(e, 'govPaneAudit');
    return;
  }
  const rows = (data.events || []).map(ev => {
    let when = String(ev.ts || '');
    const parsed = Date.parse(when);
    if (Number.isFinite(parsed)) when = new Date(parsed).toLocaleString();
    const subject = String(ev.subject_email_hash || '').slice(0, 8);
    return '<tr>' +
      '<td class="gov-nowrap">' + _govEsc(when) + '</td>' +
      '<td>' + _govEsc(ev.event || '') + '</td>' +
      '<td>' + _govEsc(ev.reason || '') + '</td>' +
      '<td>' + _govEsc(ev.method || '') + '</td>' +
      '<td class="gov-path">' + _govEsc(ev.path || '') + '</td>' +
      '<td class="gov-nowrap">' + _govEsc(subject) + '</td>' +
    '</tr>';
  }).join('');
  el.innerHTML =
    '<div class="gov-form-actions gov-audit-actions">' +
      '<button type="button" class="gov-btn" onclick="_govLoadAudit()">' + _govT('governance_refresh', 'Refresh') + '</button>' +
    '</div>' +
    '<table class="gov-table"><thead><tr>' +
      '<th>' + _govT('governance_col_time', 'Time') + '</th>' +
      '<th>' + _govT('governance_col_event', 'Event') + '</th>' +
      '<th>' + _govT('governance_col_reason', 'Reason') + '</th>' +
      '<th>' + _govT('governance_col_method', 'Method') + '</th>' +
      '<th>' + _govT('governance_col_path', 'Path') + '</th>' +
      '<th>' + _govT('governance_col_subject', 'Subject') + '</th>' +
    '</tr></thead><tbody>' +
    (rows || '<tr><td colspan="6" class="gov-muted">' + _govT('governance_no_events', 'No audit events yet.') + '</td></tr>') +
    '</tbody></table>';
}

// ── Boot: hide/show the nav buttons based on the caller's access ──────────

document.addEventListener('DOMContentLoaded', () => {
  _govFetchMe().then(govApplyVisibility).catch(() => {});
  // Event delegation for the JS-rendered approval rows: kind and key live in
  // data attributes, never inline JS (an MCP server name or a CLI command can
  // carry quotes, dots or colons that would break an inline onclick).
  const approvals = document.getElementById('govPaneApprovals');
  if (approvals) {
    approvals.addEventListener('click', (ev) => {
      const closest = (sel) => (ev.target && ev.target.closest ? ev.target.closest(sel) : null);
      if (_govSuggestionClick(closest)) return;
      const btn = closest('[data-gov-approval]');
      if (!btn || btn.disabled) return;
      _govDecideApproval(
        btn.getAttribute('data-kind') || 'skill',
        btn.getAttribute('data-key') || '',
        btn.getAttribute('data-gov-approval') || '',
      );
    });
  }
});
