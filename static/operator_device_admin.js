'use strict';

const OPERATOR_DEVICE_ADMIN_LIST_ENDPOINT = '/api/operator/device-admin';
const OPERATOR_DEVICE_ADMIN_PREVIEW_ENDPOINT = '/api/operator/device-admin/preview';

let _operatorDeviceAdminLastPayload = null;
let _operatorDeviceAdminHasLoadedPayload = false;
let _operatorDeviceAdminRequestSeq = 0;
let _operatorDeviceAdminPreviewSeq = 0;

function _operatorDeviceAdminEl(id){
  if (typeof $ === 'function') return $(id);
  if (typeof document === 'undefined') return null;
  return document.getElementById(id);
}

function _operatorDeviceAdminText(value, fallback){
  const text = String(value == null ? '' : value).trim();
  return text || fallback || '';
}

function _operatorDeviceAdminArray(value){
  return Array.isArray(value) ? value : [];
}

function _operatorDeviceAdminObject(value){
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function _operatorDeviceAdminClear(el){
  if (!el) return;
  while (el.firstChild) el.removeChild(el.firstChild);
}

function _operatorDeviceAdminCreate(tagName, className, text){
  if (typeof document === 'undefined') return null;
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function _operatorDeviceAdminAppend(parent, node){
  if (parent && node) parent.appendChild(node);
  return node;
}

function _operatorDeviceAdminAppendText(parent, className, text){
  return _operatorDeviceAdminAppend(parent, _operatorDeviceAdminCreate('div', className, text));
}

function _operatorDeviceAdminAppendMeta(parent, label, value, className){
  const rowClass = className ? 'operator-device-admin-source ' + className : 'operator-device-admin-source';
  const display = _operatorDeviceAdminText(value, 'unknown');
  return _operatorDeviceAdminAppendText(parent, rowClass, label + ': ' + display);
}

function _operatorDeviceAdminDefaultPayload(){
  return {
    status: 'unknown',
    execution_state: 'blocked',
    summary: 'Manual device admin foundations only. No device action was executed.',
    query: {text: '', host: 'all', action: 'all', limit: 50},
    sources: [],
    approval_model: {required: true, per_action: true, execution_enabled: false, required_fields: []},
    hosts: [],
    paths: [],
    dry_runs: [],
    receipts: [],
    issues: [],
    would_execute: false,
  };
}

function _operatorDeviceAdminLoadingPayload(){
  return {
    status: 'unknown',
    execution_state: 'blocked',
    summary: 'Loading source-backed device admin foundations. No device action was executed.',
    query: _operatorDeviceAdminCurrentQuery(),
    sources: [],
    approval_model: {required: true, per_action: true, execution_enabled: false, required_fields: []},
    hosts: [],
    paths: [],
    dry_runs: [],
    receipts: [],
    issues: [],
    would_execute: false,
  };
}

function _operatorDeviceAdminErrorPayload(error){
  return {
    status: 'unknown',
    execution_state: 'blocked',
    summary: 'Device admin foundations request failed. No device action was executed.',
    query: _operatorDeviceAdminCurrentQuery(),
    sources: [],
    approval_model: {required: true, per_action: true, execution_enabled: false, required_fields: []},
    hosts: [],
    paths: [],
    dry_runs: [],
    receipts: [],
    issues: [_operatorDeviceAdminText(error && error.message, 'request failed')],
    would_execute: false,
  };
}

function _operatorDeviceAdminKnownStatus(value){
  const status = _operatorDeviceAdminText(value, 'unknown').toLowerCase();
  if (status === 'live') return 'live';
  if (status === 'stale') return 'stale';
  if (status === 'blocked') return 'blocked';
  return 'unknown';
}

function _operatorDeviceAdminStateClass(value){
  const state = _operatorDeviceAdminText(value, 'unknown').toLowerCase();
  if (state === 'blocked' || state === 'disabled' || state === 'stale' || state === 'draft') return 'operator-device-admin-blocked';
  return 'operator-device-admin-unknown';
}

function _operatorDeviceAdminBoolText(value){
  if (value === true) return 'true';
  if (value === false) return 'false';
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (normalized === 'true') return 'true';
    if (normalized === 'false') return 'false';
  }
  return 'unknown';
}

function _operatorDeviceAdminCurrentQuery(){
  const input = _operatorDeviceAdminEl('operatorDeviceAdminInput');
  const host = _operatorDeviceAdminEl('operatorDeviceAdminHost');
  const action = _operatorDeviceAdminEl('operatorDeviceAdminAction');
  return {
    text: _operatorDeviceAdminText(input && input.value, ''),
    host: _operatorDeviceAdminText(host && host.value, 'all'),
    action: _operatorDeviceAdminText(action && action.value, 'all'),
    limit: 50,
  };
}

function _operatorDeviceAdminQueryString(){
  const query = _operatorDeviceAdminCurrentQuery();
  const params = new URLSearchParams();
  params.set('query', query.text);
  params.set('host', query.host || 'all');
  params.set('action', query.action || 'all');
  params.set('limit', String(query.limit || 50));
  const encoded = params.toString();
  return encoded ? '?' + encoded : '';
}

function _operatorDeviceAdminSetChipState(state, count){
  const chip = _operatorDeviceAdminEl('operatorDeviceAdminChip');
  const label = _operatorDeviceAdminEl('operatorDeviceAdminLabel');
  const normalized = _operatorDeviceAdminKnownStatus(state);
  if (label) {
    const suffix = Number.isFinite(Number(count)) && Number(count) > 0 ? ' ' + Number(count) : '';
    label.textContent = normalized === 'live' ? 'Device Admin' + suffix : 'Device Admin';
  }
  if (!chip) return;
  chip.hidden = false;
  chip.title = 'Device admin foundations · ' + normalized + ' · execution blocked';
  if (chip.classList) {
    chip.classList.remove('state-live', 'state-stale', 'state-unknown');
    chip.classList.add(normalized === 'live' ? 'state-live' : normalized === 'stale' ? 'state-stale' : 'state-unknown');
  }
}

function _operatorDeviceAdminPopulateHostFilter(hosts){
  const select = _operatorDeviceAdminEl('operatorDeviceAdminHost');
  if (!select) return;
  const current = _operatorDeviceAdminText(select.value, 'all');
  _operatorDeviceAdminClear(select);
  const allOption = _operatorDeviceAdminCreate('option', '', 'All hosts');
  if (allOption) allOption.value = 'all';
  _operatorDeviceAdminAppend(select, allOption);
  _operatorDeviceAdminArray(hosts).forEach(hostValue => {
    const host = _operatorDeviceAdminObject(hostValue);
    const id = _operatorDeviceAdminText(host.id, '');
    if (!id || id === 'unknown') return;
    const option = _operatorDeviceAdminCreate('option', '', _operatorDeviceAdminText(host.label, id));
    if (!option) return;
    option.value = id;
    _operatorDeviceAdminAppend(select, option);
  });
  const hasCurrent = Array.from(select.options || []).some(option => option.value === current);
  select.value = hasCurrent ? current : 'all';
}

function _operatorDeviceAdminRenderIssues(parent, title, issues){
  const values = _operatorDeviceAdminArray(issues).map(issue => _operatorDeviceAdminText(issue, '')).filter(Boolean);
  const card = _operatorDeviceAdminCreate('div', 'operator-device-admin-card', null);
  if (!card) return null;
  _operatorDeviceAdminAppendText(card, 'operator-device-admin-source', title + ': ' + (values.length ? String(values.length) : 'none'));
  values.forEach(issue => _operatorDeviceAdminAppendText(card, 'operator-device-admin-source operator-device-admin-blocked', '- ' + issue));
  return _operatorDeviceAdminAppend(parent, card);
}

function _operatorDeviceAdminRenderSources(parent, sources){
  const values = _operatorDeviceAdminArray(sources);
  const section = _operatorDeviceAdminCreate('div', 'operator-device-admin-card', null);
  if (!section) return null;
  _operatorDeviceAdminAppendText(section, 'operator-device-admin-source', 'sources: ' + (values.length ? String(values.length) : 'none'));
  if (!values.length) {
    _operatorDeviceAdminAppendText(section, 'operator-device-admin-empty', 'No allowlist or receipt sources were returned.');
    return _operatorDeviceAdminAppend(parent, section);
  }
  values.forEach(sourceValue => {
    const source = _operatorDeviceAdminObject(sourceValue);
    const sourceCard = _operatorDeviceAdminCreate('div', 'operator-device-admin-card ' + _operatorDeviceAdminStateClass(source.state), null);
    if (!sourceCard) return;
    _operatorDeviceAdminAppendMeta(sourceCard, 'source id', source.id);
    _operatorDeviceAdminAppendMeta(sourceCard, 'display_path', source.display_path);
    _operatorDeviceAdminAppendMeta(sourceCard, 'state', source.state);
    _operatorDeviceAdminAppendMeta(sourceCard, 'issue', source.issue);
    _operatorDeviceAdminAppend(section, sourceCard);
  });
  return _operatorDeviceAdminAppend(parent, section);
}

function _operatorDeviceAdminRenderApprovalModel(parent, approvalModel){
  const model = _operatorDeviceAdminObject(approvalModel);
  const card = _operatorDeviceAdminCreate('div', 'operator-device-admin-card operator-device-admin-blocked', null);
  if (!card) return null;
  _operatorDeviceAdminAppendText(card, 'operator-device-admin-source', 'approval_model');
  _operatorDeviceAdminAppendMeta(card, 'required', _operatorDeviceAdminBoolText(model.required));
  _operatorDeviceAdminAppendMeta(card, 'per_action', _operatorDeviceAdminBoolText(model.per_action));
  _operatorDeviceAdminAppendMeta(card, 'execution_enabled', _operatorDeviceAdminBoolText(model.execution_enabled));
  _operatorDeviceAdminAppendMeta(card, 'required_fields', _operatorDeviceAdminArray(model.required_fields).join(', ') || 'unknown');
  return _operatorDeviceAdminAppend(parent, card);
}

function _operatorDeviceAdminRenderHosts(parent, hosts){
  const values = _operatorDeviceAdminArray(hosts);
  const section = _operatorDeviceAdminCreate('div', 'operator-device-admin-card', null);
  if (!section) return null;
  _operatorDeviceAdminAppendText(section, 'operator-device-admin-source', 'hosts: ' + (values.length ? String(values.length) : 'none'));
  if (!values.length) {
    _operatorDeviceAdminAppendText(section, 'operator-device-admin-empty', 'No source-backed hosts are available.');
    return _operatorDeviceAdminAppend(parent, section);
  }
  values.forEach(hostValue => {
    const host = _operatorDeviceAdminObject(hostValue);
    const card = _operatorDeviceAdminCreate('div', 'operator-device-admin-card ' + _operatorDeviceAdminStateClass(host.state), null);
    if (!card) return;
    _operatorDeviceAdminAppendMeta(card, 'host_id', host.id);
    _operatorDeviceAdminAppendMeta(card, 'label', host.label);
    _operatorDeviceAdminAppendMeta(card, 'kind', host.kind);
    _operatorDeviceAdminAppendMeta(card, 'state', host.state);
    _operatorDeviceAdminAppend(section, card);
  });
  return _operatorDeviceAdminAppend(parent, section);
}

function _operatorDeviceAdminRenderPaths(parent, paths){
  const values = _operatorDeviceAdminArray(paths);
  const section = _operatorDeviceAdminCreate('div', 'operator-device-admin-card', null);
  if (!section) return null;
  _operatorDeviceAdminAppendText(section, 'operator-device-admin-source', 'paths: ' + (values.length ? String(values.length) : 'none'));
  if (!values.length) {
    _operatorDeviceAdminAppendText(section, 'operator-device-admin-empty', 'No source-backed paths are available.');
    return _operatorDeviceAdminAppend(parent, section);
  }
  values.forEach(pathValue => {
    const item = _operatorDeviceAdminObject(pathValue);
    const card = _operatorDeviceAdminCreate('div', 'operator-device-admin-card ' + _operatorDeviceAdminStateClass(item.state), null);
    if (!card) return;
    _operatorDeviceAdminAppendMeta(card, 'path_id', item.id);
    _operatorDeviceAdminAppendMeta(card, 'host_id', item.host_id);
    _operatorDeviceAdminAppendMeta(card, 'label', item.label);
    _operatorDeviceAdminAppendMeta(card, 'display_path', item.display_path);
    _operatorDeviceAdminAppendMeta(card, 'capabilities', _operatorDeviceAdminArray(item.capabilities).join(', ') || 'unknown');
    _operatorDeviceAdminAppendMeta(card, 'state', item.state);
    _operatorDeviceAdminAppend(section, card);
  });
  return _operatorDeviceAdminAppend(parent, section);
}

function _operatorDeviceAdminRenderDryRuns(parent, dryRuns){
  const values = _operatorDeviceAdminArray(dryRuns);
  const section = _operatorDeviceAdminCreate('div', 'operator-device-admin-card', null);
  if (!section) return null;
  _operatorDeviceAdminAppendText(section, 'operator-device-admin-source', 'dry_runs: ' + (values.length ? String(values.length) : 'none'));
  if (!values.length) {
    _operatorDeviceAdminAppendText(section, 'operator-device-admin-empty', 'No source-backed dry-run actions are available.');
    return _operatorDeviceAdminAppend(parent, section);
  }
  values.forEach(actionValue => {
    const action = _operatorDeviceAdminObject(actionValue);
    const card = _operatorDeviceAdminCreate('div', 'operator-device-admin-card ' + _operatorDeviceAdminStateClass(action.state), null);
    if (!card) return;
    _operatorDeviceAdminAppendMeta(card, 'action_id', action.id);
    _operatorDeviceAdminAppendMeta(card, 'source_action_id', action.source_action_id);
    _operatorDeviceAdminAppendMeta(card, 'action', action.action);
    _operatorDeviceAdminAppendMeta(card, 'host_id', action.host_id);
    _operatorDeviceAdminAppendMeta(card, 'source_path_id', action.source_path_id);
    _operatorDeviceAdminAppendMeta(card, 'destination_path_id', action.destination_path_id);
    _operatorDeviceAdminAppendMeta(card, 'summary', action.summary);
    _operatorDeviceAdminAppendMeta(card, 'risk', action.risk);
    _operatorDeviceAdminAppendMeta(card, 'state', action.state);
    _operatorDeviceAdminAppendMeta(card, 'approval_required', _operatorDeviceAdminBoolText(action.approval_required));
    _operatorDeviceAdminAppendMeta(card, 'would_execute', _operatorDeviceAdminBoolText(action.would_execute));
    const button = _operatorDeviceAdminCreate('button', 'operator-device-admin-action', 'Dry-run preview');
    if (button) {
      button.type = 'button';
      button.dataset.actionId = _operatorDeviceAdminText(action.id, '');
      button.addEventListener('click', () => previewOperatorDeviceAdminAction(button.dataset.actionId));
      _operatorDeviceAdminAppend(card, button);
    }
    _operatorDeviceAdminRenderIssues(card, 'action issues', action.issues || []);
    _operatorDeviceAdminAppend(section, card);
  });
  return _operatorDeviceAdminAppend(parent, section);
}

function _operatorDeviceAdminRenderReceipts(parent, receipts){
  const values = _operatorDeviceAdminArray(receipts);
  const section = _operatorDeviceAdminCreate('div', 'operator-device-admin-card', null);
  if (!section) return null;
  _operatorDeviceAdminAppendText(section, 'operator-device-admin-source', 'receipts: ' + (values.length ? String(values.length) : 'none'));
  if (!values.length) {
    _operatorDeviceAdminAppendText(section, 'operator-device-admin-empty', 'No receipt-log entries were returned.');
    return _operatorDeviceAdminAppend(parent, section);
  }
  values.forEach(receiptValue => {
    const receipt = _operatorDeviceAdminObject(receiptValue);
    const card = _operatorDeviceAdminCreate('div', 'operator-device-admin-card ' + _operatorDeviceAdminStateClass(receipt.status), null);
    if (!card) return;
    _operatorDeviceAdminAppendMeta(card, 'receipt_id', receipt.id);
    _operatorDeviceAdminAppendMeta(card, 'action_id', receipt.action_id);
    _operatorDeviceAdminAppendMeta(card, 'status', receipt.status);
    _operatorDeviceAdminAppendMeta(card, 'created_at', receipt.created_at);
    _operatorDeviceAdminAppendMeta(card, 'summary', receipt.summary);
    _operatorDeviceAdminAppendMeta(card, 'would_execute', _operatorDeviceAdminBoolText(receipt.would_execute));
    _operatorDeviceAdminAppend(section, card);
  });
  return _operatorDeviceAdminAppend(parent, section);
}

function hideOperatorDeviceAdmin(){
  const popover = _operatorDeviceAdminEl('operatorDeviceAdminPopover');
  if (popover) popover.hidden = true;
}

function toggleOperatorDeviceAdmin(opts = {}){
  const popover = _operatorDeviceAdminEl('operatorDeviceAdminPopover');
  if (!popover) return;
  const shouldOpen = opts && opts.open === true ? true : opts && opts.open === false ? false : popover.hidden;
  popover.hidden = !shouldOpen;
  if (!shouldOpen) return;
  renderOperatorDeviceAdmin(_operatorDeviceAdminLastPayload || _operatorDeviceAdminDefaultPayload());
  if (!_operatorDeviceAdminHasLoadedPayload || (opts && opts.force === true)) {
    refreshOperatorDeviceAdmin(opts);
  }
}

async function refreshOperatorDeviceAdmin(opts = {}){
  const requestSeq = ++_operatorDeviceAdminRequestSeq;
  const queryString = _operatorDeviceAdminQueryString();
  renderOperatorDeviceAdmin(_operatorDeviceAdminLoadingPayload(), {updateFilters: false});
  try {
    if (typeof api !== 'function') throw new Error('api helper unavailable');
    const payload = await api('/api/operator/device-admin' + queryString);
    if (requestSeq !== _operatorDeviceAdminRequestSeq) return payload;
    _operatorDeviceAdminHasLoadedPayload = true;
    _operatorDeviceAdminLastPayload = payload || _operatorDeviceAdminDefaultPayload();
    renderOperatorDeviceAdmin(_operatorDeviceAdminLastPayload);
    return _operatorDeviceAdminLastPayload;
  } catch (error) {
    if (requestSeq !== _operatorDeviceAdminRequestSeq) return null;
    const payload = _operatorDeviceAdminErrorPayload(error);
    _operatorDeviceAdminHasLoadedPayload = true;
    _operatorDeviceAdminLastPayload = payload;
    renderOperatorDeviceAdmin(payload);
    return payload;
  }
}

function renderOperatorDeviceAdmin(payload, opts = {}){
  const safePayload = _operatorDeviceAdminObject(payload);
  const finalPayload = Object.keys(safePayload).length ? safePayload : _operatorDeviceAdminDefaultPayload();
  _operatorDeviceAdminLastPayload = finalPayload;
  const dryRuns = _operatorDeviceAdminArray(finalPayload.dry_runs);
  _operatorDeviceAdminSetChipState(finalPayload.status || 'unknown', dryRuns.length);
  if (!opts || opts.updateFilters !== false) _operatorDeviceAdminPopulateHostFilter(finalPayload.hosts || []);
  const status = _operatorDeviceAdminEl('operatorDeviceAdminStatus');
  if (status) status.textContent = _operatorDeviceAdminText(finalPayload.summary, 'Manual foundations only — no execution');
  const list = _operatorDeviceAdminEl('operatorDeviceAdminList');
  _operatorDeviceAdminClear(list);
  _operatorDeviceAdminRenderSources(list, finalPayload.sources || []);
  _operatorDeviceAdminRenderApprovalModel(list, finalPayload.approval_model || {});
  _operatorDeviceAdminRenderHosts(list, finalPayload.hosts || []);
  _operatorDeviceAdminRenderPaths(list, finalPayload.paths || []);
  _operatorDeviceAdminRenderDryRuns(list, dryRuns);
  _operatorDeviceAdminRenderReceipts(list, finalPayload.receipts || []);
  _operatorDeviceAdminRenderIssues(list, 'issues', finalPayload.issues || []);
  ++_operatorDeviceAdminPreviewSeq;
  renderOperatorDeviceAdminPreview({
    status: 'unknown',
    action: {id: '', action: 'unknown', summary: ''},
    preview: {text: 'No device action was executed. Select a source-backed dry-run action for preview.'},
    issues: [],
    would_execute: false,
  });
}

function renderOperatorDeviceAdminPreview(payload){
  const safePayload = _operatorDeviceAdminObject(payload);
  const preview = _operatorDeviceAdminObject(safePayload.preview);
  const action = _operatorDeviceAdminObject(safePayload.action);
  const panel = _operatorDeviceAdminEl('operatorDeviceAdminPreview');
  _operatorDeviceAdminClear(panel);
  const card = _operatorDeviceAdminCreate('div', 'operator-device-admin-card ' + _operatorDeviceAdminStateClass(safePayload.status || 'blocked'), null);
  if (!card) return;
  _operatorDeviceAdminAppendText(card, 'operator-device-admin-source operator-device-admin-blocked', 'No device action was executed.');
  _operatorDeviceAdminAppendMeta(card, 'status', safePayload.status || 'unknown');
  _operatorDeviceAdminAppendMeta(card, 'action_id', action.id);
  _operatorDeviceAdminAppendMeta(card, 'action', action.action);
  _operatorDeviceAdminAppendMeta(card, 'summary', action.summary);
  _operatorDeviceAdminAppendMeta(card, 'would_execute', _operatorDeviceAdminBoolText(safePayload.would_execute));
  _operatorDeviceAdminAppendText(card, 'operator-device-admin-preview', _operatorDeviceAdminText(preview.text, 'No preview text returned.'));
  _operatorDeviceAdminRenderIssues(card, 'preview issues', safePayload.issues || []);
  _operatorDeviceAdminAppend(panel, card);
}

async function previewOperatorDeviceAdminAction(actionId){
  const normalizedActionId = _operatorDeviceAdminText(actionId, '');
  const previewSeq = ++_operatorDeviceAdminPreviewSeq;
  renderOperatorDeviceAdminPreview({
    status: 'unknown',
    action: {id: normalizedActionId, action: 'unknown', summary: 'Loading dry-run preview'},
    preview: {text: 'No device action was executed. Loading dry-run preview metadata.'},
    issues: [],
    would_execute: false,
  });
  try {
    if (typeof api !== 'function') throw new Error('api helper unavailable');
    const params = new URLSearchParams();
    params.set('id', normalizedActionId);
    const payload = await api('/api/operator/device-admin/preview?' + params.toString());
    if (previewSeq !== _operatorDeviceAdminPreviewSeq) return payload;
    renderOperatorDeviceAdminPreview(payload || {});
    return payload;
  } catch (error) {
    if (previewSeq !== _operatorDeviceAdminPreviewSeq) return null;
    const payload = {
      status: 'unknown',
      action: {id: normalizedActionId, action: 'unknown', summary: ''},
      preview: {text: 'No device action was executed. Preview request failed.'},
      issues: [_operatorDeviceAdminText(error && error.message, 'preview request failed')],
      would_execute: false,
    };
    renderOperatorDeviceAdminPreview(payload);
    return payload;
  }
}

function initOperatorDeviceAdmin(){
  _operatorDeviceAdminSetChipState('unknown', 0);
  renderOperatorDeviceAdmin(_operatorDeviceAdminLastPayload || _operatorDeviceAdminDefaultPayload());
}

window.toggleOperatorDeviceAdmin = toggleOperatorDeviceAdmin;
window.hideOperatorDeviceAdmin = hideOperatorDeviceAdmin;
window.refreshOperatorDeviceAdmin = refreshOperatorDeviceAdmin;
window.renderOperatorDeviceAdmin = renderOperatorDeviceAdmin;
window.previewOperatorDeviceAdminAction = previewOperatorDeviceAdminAction;
window.renderOperatorDeviceAdminPreview = renderOperatorDeviceAdminPreview;
window.initOperatorDeviceAdmin = initOperatorDeviceAdmin;

if (typeof document !== 'undefined') initOperatorDeviceAdmin();
