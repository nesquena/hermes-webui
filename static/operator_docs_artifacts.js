'use strict';

const OPERATOR_DOCS_ARTIFACTS_ALLOWED_ENDPOINTS = {
  list: '/api/operator/docs-artifacts',
  open: '/api/operator/docs-artifacts/open',
};
const OPERATOR_DOCS_ARTIFACTS_OPEN_ENDPOINT = OPERATOR_DOCS_ARTIFACTS_ALLOWED_ENDPOINTS.open;

let _operatorDocsArtifactsLastPayload = null;
let _operatorDocsArtifactsRequestSeq = 0;
let _operatorDocsArtifactsPreviewSeq = 0;

function _operatorDocsArtifactsEl(id){
  if (typeof $ === 'function') return $(id);
  if (typeof document === 'undefined') return null;
  return document.getElementById(id);
}

function _operatorDocsArtifactsText(value, fallback){
  const text = String(value == null ? '' : value).trim();
  return text || fallback || '';
}

function _operatorDocsArtifactsArray(value){
  return Array.isArray(value) ? value : [];
}

function _operatorDocsArtifactsObject(value){
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function _operatorDocsArtifactsHasOwn(obj, key){
  return Boolean(obj && typeof obj === 'object' && Object.prototype.hasOwnProperty.call(obj, key));
}

function _operatorDocsArtifactsClear(el){
  if (!el) return;
  while (el.firstChild) el.removeChild(el.firstChild);
}

function _operatorDocsArtifactsCreate(tagName, className, text){
  if (typeof document === 'undefined') return null;
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function _operatorDocsArtifactsAppend(parent, node){
  if (parent && node) parent.appendChild(node);
  return node;
}

function _operatorDocsArtifactsAppendText(parent, className, text){
  return _operatorDocsArtifactsAppend(parent, _operatorDocsArtifactsCreate('div', className, text));
}

function _operatorDocsArtifactsAppendMeta(parent, label, value, className){
  const rowClass = className ? 'operator-docs-artifacts-source ' + className : 'operator-docs-artifacts-source';
  const display = _operatorDocsArtifactsText(value, 'unknown');
  return _operatorDocsArtifactsAppendText(parent, rowClass, label + ': ' + display);
}

function _operatorDocsArtifactsDefaultPayload(){
  return {
    status: 'unknown',
    summary: 'Press Refresh to list allowlisted docs and artifacts.',
    query: {text: '', kind: 'all', root: 'all', limit: 50},
    sources: [],
    items: [],
    count: 0,
    issues: [],
    would_execute: false,
  };
}

function _operatorDocsArtifactsLoadingPayload(){
  return {
    status: 'unknown',
    summary: 'Loading docs and artifacts from allowlisted local sources.',
    query: _operatorDocsArtifactsCurrentQuery(),
    sources: [],
    items: [],
    count: 0,
    issues: [],
    would_execute: false,
  };
}

function _operatorDocsArtifactsErrorPayload(error){
  return {
    status: 'unknown',
    summary: 'Docs and artifacts request failed.',
    query: _operatorDocsArtifactsCurrentQuery(),
    sources: [],
    items: [],
    count: 0,
    issues: [_operatorDocsArtifactsText(error && error.message, 'request failed')],
    would_execute: false,
  };
}

function _operatorDocsArtifactsKnownState(value, fallback){
  const state = _operatorDocsArtifactsText(value, fallback || 'unknown').toLowerCase();
  if (state === 'current') return 'current';
  if (state === 'historical') return 'historical';
  if (state === 'stale') return 'stale';
  if (state === 'live') return 'live';
  return 'unknown';
}

function _operatorDocsArtifactsStateClass(state){
  const value = _operatorDocsArtifactsKnownState(state, 'unknown');
  if (value === 'current' || value === 'live') return 'operator-docs-artifacts-current';
  if (value === 'historical') return 'operator-docs-artifacts-historical';
  if (value === 'stale') return 'operator-docs-artifacts-stale';
  return '';
}

function _operatorDocsArtifactsBoolState(value){
  if (value === true) return 'true';
  if (value === false) return 'false';
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (normalized === 'true') return 'true';
    if (normalized === 'false') return 'false';
  }
  return 'unknown';
}

function _operatorDocsArtifactsMtime(value){
  if (value == null || value === '') return 'unknown';
  const numeric = Number(value);
  if (Number.isFinite(numeric)) {
    const millis = numeric > 100000000000 ? numeric : numeric * 1000;
    const date = new Date(millis);
    if (!Number.isNaN(date.getTime())) return date.toISOString();
  }
  return _operatorDocsArtifactsText(value, 'unknown');
}

function _operatorDocsArtifactsFreshness(item){
  const freshness = _operatorDocsArtifactsObject(item && item.freshness);
  const rawLabel = freshness.label || (item && item.freshness_label);
  const label = _operatorDocsArtifactsKnownState(rawLabel, 'unknown');
  const reason = _operatorDocsArtifactsText(freshness.reason || (item && item.freshness_reason), 'reason unknown');
  return {label, reason};
}

function _operatorDocsArtifactsCurrentQuery(){
  const input = _operatorDocsArtifactsEl('operatorDocsArtifactsInput');
  const kind = _operatorDocsArtifactsEl('operatorDocsArtifactsKind');
  const root = _operatorDocsArtifactsEl('operatorDocsArtifactsRoot');
  return {
    text: _operatorDocsArtifactsText(input && input.value, ''),
    kind: _operatorDocsArtifactsText(kind && kind.value, 'all'),
    root: _operatorDocsArtifactsText(root && root.value, 'all'),
    limit: 50,
  };
}

function _operatorDocsArtifactsQueryString(){
  const query = _operatorDocsArtifactsCurrentQuery();
  const params = new URLSearchParams();
  params.set('query', query.text);
  params.set('kind', query.kind || 'all');
  params.set('root', query.root || 'all');
  params.set('limit', String(query.limit || 50));
  const encoded = params.toString();
  return encoded ? '?' + encoded : '';
}

function _operatorDocsArtifactsSetChipState(state, count){
  const chip = _operatorDocsArtifactsEl('operatorDocsArtifactsChip');
  const label = _operatorDocsArtifactsEl('operatorDocsArtifactsLabel');
  const normalized = _operatorDocsArtifactsKnownState(state, 'unknown');
  if (label) {
    const suffix = Number.isFinite(Number(count)) && Number(count) > 0 ? ' ' + Number(count) : '';
    label.textContent = normalized === 'live' || normalized === 'current' ? 'Docs' + suffix : normalized === 'stale' ? 'Docs stale' : 'Docs';
  }
  if (!chip) return;
  chip.hidden = false;
  chip.title = 'Manual docs and artifacts browser · ' + normalized;
  if (chip.classList) {
    chip.classList.remove('state-live', 'state-stale', 'state-unknown');
    chip.classList.add(normalized === 'live' || normalized === 'current' ? 'state-live' : normalized === 'stale' ? 'state-stale' : 'state-unknown');
  }
}

function _operatorDocsArtifactsRenderIssues(parent, title, issues){
  const values = _operatorDocsArtifactsArray(issues).map(issue => _operatorDocsArtifactsText(issue, '')).filter(Boolean);
  const card = _operatorDocsArtifactsCreate('div', 'operator-docs-artifacts-card', null);
  if (!card) return null;
  _operatorDocsArtifactsAppendText(card, 'operator-docs-artifacts-source', title + ': ' + (values.length ? String(values.length) : 'none'));
  values.forEach(issue => _operatorDocsArtifactsAppendText(card, 'operator-docs-artifacts-source operator-docs-artifacts-stale', '- ' + issue));
  return _operatorDocsArtifactsAppend(parent, card);
}

function _operatorDocsArtifactsRenderSources(parent, sources){
  const values = _operatorDocsArtifactsArray(sources);
  const section = _operatorDocsArtifactsCreate('div', 'operator-docs-artifacts-card', null);
  if (!section) return null;
  _operatorDocsArtifactsAppendText(section, 'operator-docs-artifacts-source', 'sources: ' + (values.length ? String(values.length) : 'none'));
  if (!values.length) {
    _operatorDocsArtifactsAppendText(section, 'operator-docs-artifacts-empty', 'No source registry entries were returned.');
    return _operatorDocsArtifactsAppend(parent, section);
  }
  values.forEach(sourceValue => {
    const source = _operatorDocsArtifactsObject(sourceValue);
    const sourceCard = _operatorDocsArtifactsCreate('div', 'operator-docs-artifacts-card ' + _operatorDocsArtifactsStateClass(source.state), null);
    if (!sourceCard) return;
    _operatorDocsArtifactsAppendMeta(sourceCard, 'source id', source.id, 'operator-docs-artifacts-current');
    _operatorDocsArtifactsAppendMeta(sourceCard, 'label', source.label);
    _operatorDocsArtifactsAppendMeta(sourceCard, 'kind', source.kind);
    _operatorDocsArtifactsAppendMeta(sourceCard, 'display_path', source.display_path);
    _operatorDocsArtifactsAppendMeta(sourceCard, 'state', _operatorDocsArtifactsKnownState(source.state, 'unknown'));
    _operatorDocsArtifactsAppendMeta(sourceCard, 'count', source.count == null ? 'unknown' : source.count);
    _operatorDocsArtifactsRenderIssues(sourceCard, 'source issues', source.issue ? [source.issue] : []);
    _operatorDocsArtifactsAppend(section, sourceCard);
  });
  return _operatorDocsArtifactsAppend(parent, section);
}

function _operatorDocsArtifactsItemWouldExecute(item, payload){
  if (_operatorDocsArtifactsHasOwn(item, 'would_execute')) return _operatorDocsArtifactsBoolState(item.would_execute);
  if (_operatorDocsArtifactsHasOwn(payload, 'would_execute')) return _operatorDocsArtifactsBoolState(payload.would_execute);
  return 'unknown';
}

function _operatorDocsArtifactsRenderItems(parent, items, payload){
  const values = _operatorDocsArtifactsArray(items);
  if (!values.length) {
    _operatorDocsArtifactsAppendText(parent, 'operator-docs-artifacts-empty', 'No docs or artifacts returned for the current filters.');
    return;
  }
  values.forEach(itemValue => {
    const item = _operatorDocsArtifactsObject(itemValue);
    const freshness = _operatorDocsArtifactsFreshness(item);
    const stateClass = _operatorDocsArtifactsStateClass(freshness.label);
    const card = _operatorDocsArtifactsCreate('div', 'operator-docs-artifacts-card' + (stateClass ? ' ' + stateClass : ''), null);
    if (!card) return;
    const title = _operatorDocsArtifactsText(item.title, _operatorDocsArtifactsText(item.display_path, 'Untitled docs/artifacts item'));
    _operatorDocsArtifactsAppendText(card, 'operator-docs-artifacts-source operator-docs-artifacts-current', title);
    _operatorDocsArtifactsAppendMeta(card, 'kind', item.kind);
    _operatorDocsArtifactsAppendMeta(card, 'display_path', item.display_path);
    _operatorDocsArtifactsAppendMeta(card, 'root_id', item.root_id);
    _operatorDocsArtifactsAppendMeta(card, 'relative_path', item.relative_path);
    _operatorDocsArtifactsAppendMeta(card, 'size_bytes', item.size_bytes == null ? 'unknown' : item.size_bytes);
    _operatorDocsArtifactsAppendMeta(card, 'mtime', _operatorDocsArtifactsMtime(item.mtime));
    _operatorDocsArtifactsAppendMeta(card, 'freshness', freshness.label + ' — ' + freshness.reason, stateClass);
    _operatorDocsArtifactsAppendMeta(card, 'preview_available', _operatorDocsArtifactsBoolState(item.preview_available));
    _operatorDocsArtifactsAppendMeta(card, 'would_execute', _operatorDocsArtifactsItemWouldExecute(item, payload));
    _operatorDocsArtifactsRenderIssues(card, 'item issues', item.issues);
    const previewAvailable = item.preview_available === true;
    const action = _operatorDocsArtifactsCreate('button', 'operator-docs-artifacts-action', previewAvailable ? 'Open preview' : 'Preview unavailable');
    if (action) {
      action.type = 'button';
      action.disabled = !previewAvailable;
      if (previewAvailable) {
        action.addEventListener('click', () => openOperatorDocsArtifactPreview(item));
      } else {
        action.title = 'Preview unavailable for this item.';
        action.setAttribute('aria-disabled', 'true');
      }
      _operatorDocsArtifactsAppend(card, action);
    }
    _operatorDocsArtifactsAppend(parent, card);
  });
}

function _operatorDocsArtifactsPreviewId(itemOrId){
  if (typeof itemOrId === 'string' || typeof itemOrId === 'number') {
    return _operatorDocsArtifactsText(itemOrId, '');
  }
  const item = _operatorDocsArtifactsObject(itemOrId);
  return _operatorDocsArtifactsText(item.id, '');
}

function _operatorDocsArtifactsPreviewEmptyPayload(itemOrId, issue){
  const item = _operatorDocsArtifactsObject(itemOrId);
  return {
    status: 'unknown',
    item,
    preview: {
      format: 'metadata-only',
      text: '',
      truncated: false,
      bytes_read: 0,
      max_bytes: 0,
    },
    issues: issue ? [issue] : [],
    would_execute: false,
  };
}

function _operatorDocsArtifactsRenderPreview(payload, requestedItem){
  const container = _operatorDocsArtifactsEl('operatorDocsArtifactsPreview');
  _operatorDocsArtifactsClear(container);
  if (!payload) {
    _operatorDocsArtifactsAppendText(container, 'operator-docs-artifacts-empty', 'Select an available item preview to open it.');
    _operatorDocsArtifactsAppendMeta(container, 'status', 'unknown');
    _operatorDocsArtifactsAppendMeta(container, 'format', 'metadata-only');
    _operatorDocsArtifactsAppendMeta(container, 'preview_available', 'unknown');
    return payload;
  }

  const data = _operatorDocsArtifactsObject(payload);
  const fallbackItem = _operatorDocsArtifactsObject(requestedItem);
  const hasItem = _operatorDocsArtifactsHasOwn(data, 'item');
  const item = hasItem ? _operatorDocsArtifactsObject(data.item) : fallbackItem;
  const hasPreview = _operatorDocsArtifactsHasOwn(data, 'preview');
  const preview = _operatorDocsArtifactsObject(data.preview);
  const previewTextIsString = typeof preview.text === 'string';
  const hasPreviewText = _operatorDocsArtifactsHasOwn(preview, 'text');
  const malformed = !hasItem || !hasPreview || (hasPreviewText && !previewTextIsString);
  const status = _operatorDocsArtifactsText(data.status, 'unknown');
  const format = _operatorDocsArtifactsText(preview.format, 'metadata-only');
  const previewText = previewTextIsString ? preview.text : '';
  const issues = _operatorDocsArtifactsArray(data.issues).slice();
  if (hasPreviewText && !previewTextIsString) issues.unshift('malformed preview text');
  if (malformed) issues.unshift('malformed preview payload');

  _operatorDocsArtifactsAppendText(container, 'operator-docs-artifacts-source operator-docs-artifacts-current', _operatorDocsArtifactsText(item.title, 'Selected docs/artifacts item'));
  _operatorDocsArtifactsAppendMeta(container, 'status', status);
  _operatorDocsArtifactsAppendMeta(container, 'format', format);
  _operatorDocsArtifactsAppendMeta(container, 'truncated', _operatorDocsArtifactsBoolState(preview.truncated));
  _operatorDocsArtifactsAppendMeta(container, 'bytes_read', preview.bytes_read == null ? 'unknown' : preview.bytes_read);
  _operatorDocsArtifactsAppendMeta(container, 'max_bytes', preview.max_bytes == null ? 'unknown' : preview.max_bytes);
  _operatorDocsArtifactsAppendMeta(container, 'id', item.id);
  _operatorDocsArtifactsAppendMeta(container, 'display_path', item.display_path);
  _operatorDocsArtifactsAppendMeta(container, 'root_id', item.root_id);
  _operatorDocsArtifactsAppendMeta(container, 'relative_path', item.relative_path);
  _operatorDocsArtifactsAppendMeta(container, 'preview_available', _operatorDocsArtifactsBoolState(item.preview_available));
  _operatorDocsArtifactsRenderIssues(container, 'preview issues', issues);

  if (malformed) {
    _operatorDocsArtifactsAppendText(container, 'operator-docs-artifacts-empty', 'Malformed preview response; no preview body was rendered.');
    return payload;
  }
  if (!previewText) {
    _operatorDocsArtifactsAppendText(container, 'operator-docs-artifacts-empty', format === 'metadata-only' ? 'Metadata-only response; no preview text returned.' : 'No preview text returned.');
    return payload;
  }
  if (typeof document === 'undefined') return payload;
  const body = document.createElement('pre');
  body.className = 'operator-docs-artifacts-source operator-docs-artifacts-preview-body';
  body.textContent = previewText;
  _operatorDocsArtifactsAppend(container, body);
  return payload;
}

function renderOperatorDocsArtifacts(payload){
  const data = payload || _operatorDocsArtifactsLastPayload || _operatorDocsArtifactsDefaultPayload();
  _operatorDocsArtifactsLastPayload = data;

  const sources = _operatorDocsArtifactsArray(data.sources);
  const items = _operatorDocsArtifactsArray(data.items);
  const issues = _operatorDocsArtifactsArray(data.issues);
  const count = data.count == null ? items.length : data.count;
  const summary = _operatorDocsArtifactsText(data.summary, 'Docs and artifacts state unknown.');
  const statusText = summary + ' · items: ' + String(count) + ' · would_execute: ' + _operatorDocsArtifactsBoolState(data.would_execute);

  const status = _operatorDocsArtifactsEl('operatorDocsArtifactsStatus');
  if (status) status.textContent = statusText;
  _operatorDocsArtifactsSetChipState(data.status, count);

  const list = _operatorDocsArtifactsEl('operatorDocsArtifactsList');
  _operatorDocsArtifactsClear(list);
  _operatorDocsArtifactsRenderIssues(list, 'issues', issues);
  _operatorDocsArtifactsRenderSources(list, sources);
  _operatorDocsArtifactsRenderItems(list, items, data);
  _operatorDocsArtifactsPreviewSeq += 1;
  _operatorDocsArtifactsRenderPreview(null, null);

  return data;
}

async function refreshOperatorDocsArtifacts(){
  const requestSeq = ++_operatorDocsArtifactsRequestSeq;
  const queryString = _operatorDocsArtifactsQueryString();
  renderOperatorDocsArtifacts(_operatorDocsArtifactsLoadingPayload());
  try {
    const payload = await api('/api/operator/docs-artifacts' + queryString);
    if (requestSeq !== _operatorDocsArtifactsRequestSeq) return _operatorDocsArtifactsLastPayload;
    return renderOperatorDocsArtifacts(payload);
  } catch (error) {
    if (requestSeq !== _operatorDocsArtifactsRequestSeq) return _operatorDocsArtifactsLastPayload;
    return renderOperatorDocsArtifacts(_operatorDocsArtifactsErrorPayload(error));
  }
}

function openOperatorDocsArtifactPreview(itemOrId){
  const previewSeq = ++_operatorDocsArtifactsPreviewSeq;
  const id = _operatorDocsArtifactsPreviewId(itemOrId);
  if (!id) {
    return _operatorDocsArtifactsRenderPreview(_operatorDocsArtifactsPreviewEmptyPayload(itemOrId, 'missing or unknown docs/artifacts item id'), itemOrId);
  }
  const params = new URLSearchParams();
  params.set('id', id);
  _operatorDocsArtifactsRenderPreview(_operatorDocsArtifactsPreviewEmptyPayload(itemOrId, 'Loading explicit docs/artifacts preview.'), itemOrId);
  return api(OPERATOR_DOCS_ARTIFACTS_OPEN_ENDPOINT + '?' + params.toString())
    .then(payload => {
      if (previewSeq !== _operatorDocsArtifactsPreviewSeq) return _operatorDocsArtifactsLastPayload;
      return _operatorDocsArtifactsRenderPreview(payload, itemOrId);
    })
    .catch(error => {
      if (previewSeq !== _operatorDocsArtifactsPreviewSeq) return _operatorDocsArtifactsLastPayload;
      return _operatorDocsArtifactsRenderPreview(_operatorDocsArtifactsPreviewEmptyPayload(itemOrId, _operatorDocsArtifactsText(error && error.message, 'preview request failed')), itemOrId);
    });
}

function hideOperatorDocsArtifacts(){
  const popover = _operatorDocsArtifactsEl('operatorDocsArtifactsPopover');
  if (popover) popover.hidden = true;
}

function toggleOperatorDocsArtifacts(opts = {}){
  const popover = _operatorDocsArtifactsEl('operatorDocsArtifactsPopover');
  if (!popover) return;
  const opening = popover.hidden;
  if (!opening && !opts.force) {
    hideOperatorDocsArtifacts();
    return;
  }
  popover.hidden = false;
  renderOperatorDocsArtifacts(_operatorDocsArtifactsLastPayload || _operatorDocsArtifactsDefaultPayload());
  const input = _operatorDocsArtifactsEl('operatorDocsArtifactsInput');
  if (input && typeof input.focus === 'function') input.focus();
}

function initOperatorDocsArtifacts(){
  _operatorDocsArtifactsSetChipState('unknown', 0);
  const input = _operatorDocsArtifactsEl('operatorDocsArtifactsInput');
  if (input && !input.dataset.docsArtifactsBound) {
    input.dataset.docsArtifactsBound = '1';
    input.addEventListener('keydown', event => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      refreshOperatorDocsArtifacts();
    });
  }
  renderOperatorDocsArtifacts(_operatorDocsArtifactsLastPayload || _operatorDocsArtifactsDefaultPayload());
}

window.toggleOperatorDocsArtifacts = toggleOperatorDocsArtifacts;
window.hideOperatorDocsArtifacts = hideOperatorDocsArtifacts;
window.refreshOperatorDocsArtifacts = refreshOperatorDocsArtifacts;
window.renderOperatorDocsArtifacts = renderOperatorDocsArtifacts;
window.openOperatorDocsArtifactPreview = openOperatorDocsArtifactPreview;
window.initOperatorDocsArtifacts = initOperatorDocsArtifacts;

if (typeof document !== 'undefined') {
  initOperatorDocsArtifacts();
}
