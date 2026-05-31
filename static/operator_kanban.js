let _operatorKanbanLastFetchKey = '';
let _operatorKanbanLastPayload = null;
let _operatorKanbanInFlight = null;
let _operatorKanbanInFlightKey = '';
let _operatorKanbanRequestSeq = 0;

function _operatorKanbanEl(id){
  if (typeof $ === 'function') return $(id);
  return document.getElementById(id);
}

function operatorKanbanBoardParam(){
  return 'hermes-operator';
}

function operatorKanbanUiBoardHint(){
  try {
    if (typeof _kanbanCurrentBoard !== 'undefined' && _kanbanCurrentBoard) return _kanbanCurrentBoard;
  } catch(_) {}
  return '';
}

function operatorKanbanContextKey(){
  const session = (typeof S !== 'undefined' && S && S.session) || {};
  const truthKey = (typeof operatorTruthContextKey === 'function') ? operatorTruthContextKey() : '';
  return JSON.stringify({
    truth: truthKey,
    board: operatorKanbanBoardParam(),
    ui_board: operatorKanbanUiBoardHint(),
    session_id: (typeof operatorTruthSessionId === 'function') ? operatorTruthSessionId() : (session.session_id || session.id || ''),
    profile: (typeof S !== 'undefined' && S && S.activeProfile) || '',
    workspace: session.workspace || '',
  });
}

function _operatorKanbanStateClass(state){
  if (state === 'live') return 'state-live';
  if (state === 'stale') return 'state-stale';
  return 'state-unknown';
}

function _operatorKanbanText(value, fallback){
  const text = String(value == null ? '' : value).trim();
  return text || fallback || '';
}

function _operatorKanbanTime(value){
  if (!value) return '';
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  try {
    return new Date(numeric * 1000).toLocaleString([], {month:'short', day:'2-digit', hour:'2-digit', minute:'2-digit'});
  } catch(_) {
    return String(value);
  }
}

function _operatorKanbanClear(el){
  if (!el) return;
  while (el.firstChild) el.removeChild(el.firstChild);
}

function _operatorKanbanChip(text, state){
  const chip = document.createElement('span');
  chip.className = 'operator-kanban-chip ' + _operatorKanbanStateClass(state || 'unknown');
  chip.textContent = text;
  return chip;
}

function _operatorKanbanRow(label, value, extraClass){
  const row = document.createElement('div');
  row.className = 'operator-kanban-row' + (extraClass ? ' ' + extraClass : '');
  const key = document.createElement('span');
  key.className = 'operator-kanban-row-key';
  key.textContent = label;
  const val = document.createElement('span');
  val.className = 'operator-kanban-row-value';
  val.textContent = _operatorKanbanText(value, 'unknown');
  row.append(key, val);
  return row;
}

function _operatorKanbanSection(title){
  const section = document.createElement('div');
  section.className = 'operator-kanban-section';
  const heading = document.createElement('div');
  heading.className = 'operator-kanban-section-title';
  heading.textContent = title;
  section.append(heading);
  return section;
}

function _operatorKanbanListItem(text, className){
  const item = document.createElement('div');
  item.className = className || 'operator-kanban-muted';
  item.textContent = _operatorKanbanText(text, 'unknown');
  return item;
}

function _operatorKanbanCompletionText(task){
  const completion = (task && task.completion) || {};
  const parts = [];
  if (completion.metadata_state) parts.push('metadata ' + completion.metadata_state);
  if (completion.completed_at) parts.push('completed ' + _operatorKanbanTime(completion.completed_at));
  return parts.join(' · ') || 'completion unknown';
}

function _operatorKanbanTaskCard(task){
  const card = document.createElement('div');
  card.className = 'operator-kanban-task-card';
  card.dataset.taskId = task.id || '';

  const top = document.createElement('div');
  top.className = 'operator-kanban-task-top';
  const title = document.createElement('div');
  title.className = 'operator-kanban-task-title';
  title.textContent = _operatorKanbanText(task.title, task.id || 'Untitled task');
  top.append(title, _operatorKanbanChip(_operatorKanbanText(task.status, 'unknown'), task.status === 'done' ? 'live' : (task.status === 'blocked' ? 'stale' : 'unknown')));
  card.append(top);

  card.append(_operatorKanbanRow('Assignee / profile', _operatorKanbanText(task.assignee, 'unassigned') + ' / ' + _operatorKanbanText(task.profile, 'unknown')));
  card.append(_operatorKanbanRow('Workspace', _operatorKanbanText(task.workspace_kind, 'unknown') + ' · ' + _operatorKanbanText(task.workspace_path, 'missing'), 'operator-kanban-path-row'));

  const scratch = (task && task.scratch_safety) || {};
  card.append(_operatorKanbanRow('Scratch safety', _operatorKanbanText(scratch.state, 'unknown') + ' · ' + _operatorKanbanText(scratch.reason, 'no reason')));
  card.append(_operatorKanbanRow('Blocked reason', _operatorKanbanText(task.blocked_reason, 'none')));
  const review = (task && task.review_state) || {};
  card.append(_operatorKanbanRow('Review state', _operatorKanbanText(review.state, 'unknown') + ' · ' + _operatorKanbanText(review.reason, 'no structured review metadata')));

  const completion = (task && task.completion) || {};
  const completionSection = _operatorKanbanSection('Completion');
  completionSection.append(_operatorKanbanListItem(_operatorKanbanCompletionText(task), 'operator-kanban-muted'));
  if (completion.result_summary) completionSection.append(_operatorKanbanListItem(completion.result_summary, 'operator-kanban-summary'));
  const changed = Array.isArray(completion.changed_files) ? completion.changed_files : [];
  completionSection.append(_operatorKanbanListItem(changed.length ? 'Changed: ' + changed.join(', ') : 'Changed files unknown/missing', 'operator-kanban-muted'));
  const validation = Array.isArray(completion.validation) ? completion.validation : [];
  completionSection.append(_operatorKanbanListItem(validation.length ? 'Validation: ' + validation.join(', ') : 'Validation unknown/missing', 'operator-kanban-muted'));
  const sideEffects = Array.isArray(completion.side_effects) ? completion.side_effects : [];
  completionSection.append(_operatorKanbanListItem(sideEffects.length ? 'Side effects: ' + sideEffects.join(', ') : 'Side effects unknown/missing', 'operator-kanban-muted'));
  card.append(completionSection);

  const receipts = Array.isArray(task.receipt_links) ? task.receipt_links : [];
  const receiptSection = _operatorKanbanSection('Receipts');
  if (!receipts.length) {
    receiptSection.append(_operatorKanbanListItem('Receipts unknown/missing', 'operator-kanban-issue'));
  } else {
    receipts.slice(0, 5).forEach(link => {
      const text = _operatorKanbanText(link.label, 'receipt') + ': ' + _operatorKanbanText(link.path, 'missing');
      receiptSection.append(_operatorKanbanListItem(text, 'operator-kanban-receipt'));
    });
  }
  card.append(receiptSection);
  return card;
}

function _operatorKanbanRenderCounts(body, payload){
  const counts = (payload && payload.counts) || {};
  const wrap = document.createElement('div');
  wrap.className = 'operator-kanban-counts';
  ['triage','todo','ready','running','blocked','done'].forEach(status => {
    const chip = document.createElement('span');
    chip.className = 'operator-kanban-count';
    chip.textContent = status + ' ' + String(counts[status] || 0);
    wrap.append(chip);
  });
  body.append(wrap);
}

function _operatorKanbanRenderSources(body, payload){
  const sources = Array.isArray(payload && payload.sources) ? payload.sources : [];
  const wrap = document.createElement('div');
  wrap.className = 'operator-kanban-sources';
  sources.slice(0, 5).forEach(source => {
    const label = _operatorKanbanText(source.id, 'source') + ' ' + _operatorKanbanText(source.state, 'unknown');
    wrap.append(_operatorKanbanChip(label, source.state));
  });
  if (wrap.childNodes.length) body.append(wrap);
}

function _operatorKanbanRenderIssues(body, payload){
  const issues = Array.isArray(payload && payload.issues) ? payload.issues : [];
  if (!issues.length) return;
  const section = _operatorKanbanSection('Issues');
  issues.slice(0, 6).forEach(issue => section.append(_operatorKanbanListItem(issue, 'operator-kanban-issue')));
  body.append(section);
}

function renderOperatorKanban(payload){
  _operatorKanbanLastPayload = payload || null;
  const panel = _operatorKanbanEl('operatorKanbanPanel');
  const subtitle = _operatorKanbanEl('operatorKanbanSubtitle');
  const body = _operatorKanbanEl('operatorKanbanBody');
  if (!panel || !body) return;
  _operatorKanbanClear(body);

  const status = (payload && payload.status) || 'unknown';
  panel.classList.remove('state-live','state-stale','state-unknown');
  panel.classList.add(_operatorKanbanStateClass(status));
  if (subtitle) subtitle.textContent = (payload && payload.summary) || 'Operator Kanban unknown';

  const safety = (payload && payload.board_safety) || {};
  const top = document.createElement('div');
  top.className = 'operator-kanban-topline';
  top.append(
    _operatorKanbanChip('board ' + _operatorKanbanText((payload && payload.board), 'hermes-operator'), status),
    _operatorKanbanChip('scratch ' + _operatorKanbanText(safety.state, 'unknown'), safety.state),
    _operatorKanbanChip('would_execute:false', 'live'),
  );
  body.append(top);

  _operatorKanbanRenderCounts(body, payload);
  _operatorKanbanRenderSources(body, payload || {});

  const tasks = Array.isArray(payload && payload.tasks) ? payload.tasks : [];
  if (!tasks.length) {
    const empty = document.createElement('div');
    empty.className = 'operator-kanban-empty';
    empty.textContent = status === 'unknown' ? 'No task cards rendered because source evidence is unavailable.' : 'No tasks found on hermes-operator.';
    body.append(empty);
    _operatorKanbanRenderIssues(body, payload || {});
    return;
  }

  const list = document.createElement('div');
  list.className = 'operator-kanban-task-list';
  tasks.forEach(task => list.append(_operatorKanbanTaskCard(task)));
  body.append(list);
  _operatorKanbanRenderIssues(body, payload || {});
}

async function refreshOperatorKanban(opts = {}){
  const force = Boolean(opts.force);
  const contextKey = operatorKanbanContextKey();
  if (!force && _operatorKanbanInFlight && contextKey === _operatorKanbanInFlightKey) return _operatorKanbanInFlight;
  if (!force && _operatorKanbanLastPayload && contextKey === _operatorKanbanLastFetchKey) {
    renderOperatorKanban(_operatorKanbanLastPayload);
    return _operatorKanbanLastPayload;
  }
  if (typeof api !== 'function') {
    const payload = {status:'unknown', summary:'Operator Kanban unavailable — API helper missing', tasks:[], counts:{}, issues:['api helper unavailable']};
    renderOperatorKanban(payload);
    return payload;
  }

  const params = new URLSearchParams();
  params.set('board', operatorKanbanBoardParam());
  const sid = (typeof operatorTruthSessionId === 'function') ? operatorTruthSessionId() : ((typeof S !== 'undefined' && S && S.session && (S.session.session_id || S.session.id)) || '');
  const uiBoard = operatorKanbanUiBoardHint();
  if (sid) params.set('session_id', sid);
  if (uiBoard) params.set('ui_board', uiBoard);
  const path = '/api/operator/kanban?' + params.toString();
  const requestKey = contextKey;
  const requestSeq = ++_operatorKanbanRequestSeq;
  _operatorKanbanInFlightKey = requestKey;

  _operatorKanbanInFlight = api(path)
    .then(payload => {
      if (requestKey !== operatorKanbanContextKey() || requestSeq !== _operatorKanbanRequestSeq) return payload;
      _operatorKanbanLastFetchKey = requestKey;
      renderOperatorKanban(payload);
      return payload;
    })
    .catch(err => {
      if (requestKey === operatorKanbanContextKey() && requestSeq === _operatorKanbanRequestSeq) {
        const payload = {status:'unknown', summary:'Operator Kanban unavailable — source read failed', tasks:[], counts:{}, issues:[(err && err.message) || 'request failed']};
        _operatorKanbanLastFetchKey = requestKey;
        renderOperatorKanban(payload);
      }
      return null;
    })
    .finally(() => {
      if (requestKey === _operatorKanbanInFlightKey && requestSeq === _operatorKanbanRequestSeq) {
        _operatorKanbanInFlight = null;
        _operatorKanbanInFlightKey = '';
      }
    });
  return _operatorKanbanInFlight;
}

function initOperatorKanbanPanel(){
  renderOperatorKanban({status:'unknown', summary:'Not checked yet', board:'hermes-operator', tasks:[], counts:{}, sources:[], issues:['not checked yet']});
}

window.refreshOperatorKanban = refreshOperatorKanban;
window.renderOperatorKanban = renderOperatorKanban;
window.operatorKanbanContextKey = operatorKanbanContextKey;
window.operatorKanbanBoardParam = operatorKanbanBoardParam;

initOperatorKanbanPanel();
