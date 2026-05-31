let _operatorSessionRecallLastPayload = null;
let _operatorSessionRecallRequestSeq = 0;
let _operatorSessionRecallMemoryReviewDraftEvidence = null;

function _operatorSessionRecallEl(id){
  if (typeof $ === 'function') return $(id);
  return document.getElementById(id);
}

function _operatorSessionRecallText(value, fallback){
  const text = String(value == null ? '' : value).trim();
  return text || fallback || '';
}

function _operatorSessionRecallStateClass(state){
  const value = _operatorSessionRecallText(state, 'unknown').toLowerCase();
  if (value === 'live') return 'state-live';
  if (value === 'stale') return 'state-stale';
  return 'state-unknown';
}

function _operatorSessionRecallClear(el){
  if (!el) return;
  while (el.firstChild) el.removeChild(el.firstChild);
}

function _operatorSessionRecallDefaultPayload(){
  return {
    status: 'unknown',
    summary: 'Enter a query and press Search to recall saved sessions.',
    query: {text: ''},
    sources: [],
    results: [],
    count: 0,
    issues: [],
    would_execute: false,
  };
}

function _operatorSessionRecallBlankPayload(){
  return {
    status: 'unknown',
    summary: 'Enter a query and press Search to recall saved sessions — no source queried yet.',
    query: {text: ''},
    sources: [],
    results: [],
    count: 0,
    issues: ['query is required for session recall'],
    would_execute: false,
  };
}

function _operatorSessionRecallUnavailablePayload(query, issue){
  return {
    status: 'unknown',
    summary: 'Session recall source unavailable for query: ' + _operatorSessionRecallText(query, 'unknown'),
    query: {text: _operatorSessionRecallText(query, '')},
    sources: [],
    results: [],
    count: 0,
    issues: [_operatorSessionRecallText(issue, 'session recall request failed')],
    would_execute: false,
  };
}

function _operatorSessionRecallQueryText(payload){
  if (!payload || !payload.query) return '';
  if (typeof payload.query === 'string') return _operatorSessionRecallText(payload.query, '');
  return _operatorSessionRecallText(payload.query.text, '');
}

function _operatorSessionRecallTimestamp(value){
  const raw = _operatorSessionRecallText(value, '');
  if (!raw) return 'timestamp unknown';
  const numeric = Number(raw);
  if (Number.isFinite(numeric)) {
    const millis = numeric > 100000000000 ? numeric : numeric * 1000;
    const date = new Date(millis);
    if (!Number.isNaN(date.getTime())) return date.toISOString();
  }
  return raw;
}

function _operatorSessionRecallRecency(result){
  const recency = result && result.recency ? result.recency : {};
  return _operatorSessionRecallText(recency.label || result.recency_label, 'unknown').toLowerCase();
}

function _operatorSessionRecallHasOwn(obj, key){
  return Boolean(obj && typeof obj === 'object' && Object.prototype.hasOwnProperty.call(obj, key));
}

function _operatorSessionRecallWouldExecuteFieldState(value){
  if (value === true) return 'true';
  if (value === false) return 'false';
  if (typeof value === 'string' && value.trim().toLowerCase() === 'true') return 'true';
  return 'unknown';
}

function _operatorSessionRecallWouldExecuteFields(result){
  const promotion = result && result.promotion ? result.promotion : {};
  const fields = [];
  if (_operatorSessionRecallHasOwn(result, 'would_execute')) fields.push(result.would_execute);
  if (_operatorSessionRecallHasOwn(promotion.task, 'would_execute')) fields.push(promotion.task.would_execute);
  if (_operatorSessionRecallHasOwn(promotion.memory_review, 'would_execute')) fields.push(promotion.memory_review.would_execute);
  return fields;
}

function _operatorSessionRecallWouldExecuteState(result){
  const states = _operatorSessionRecallWouldExecuteFields(result).map(_operatorSessionRecallWouldExecuteFieldState);
  if (states.includes('true')) return 'true';
  if (states.includes('unknown')) return 'unknown';
  if (states.includes('false')) return 'false';
  return 'unknown';
}

function _operatorSessionRecallWouldExecuteFieldsSafe(result){
  return _operatorSessionRecallWouldExecuteFields(result).every(value => value === false);
}

function _operatorSessionRecallWouldExecute(result){
  return _operatorSessionRecallWouldExecuteState(result) === 'true';
}

function _operatorSessionRecallHasValue(value){
  return value != null && String(value).trim() !== '';
}

function _operatorSessionRecallValidMessageIndex(value){
  if (typeof value === 'number') return Number.isInteger(value) && value >= 0;
  if (typeof value === 'string') {
    const text = value.trim();
    if (!/^\d+$/.test(text)) return false;
    const numeric = Number(text);
    return Number.isSafeInteger(numeric) && numeric >= 0;
  }
  return false;
}

function _operatorSessionRecallValidContentHash(value){
  return /^sha256:[0-9a-f]{64}$/i.test(_operatorSessionRecallText(value, ''));
}

function _operatorSessionRecallSecretValueRedacted(value){
  const token = _operatorSessionRecallText(value, '').replace(/^["']|["']$/g, '').trim();
  const normalized = token.replace(/^[\[\](){}<>]+|[\[\](){}<>]+$/g, '').toLowerCase();
  if (['redacted','masked','hidden','removed','withheld','scrubbed'].includes(normalized)) return true;
  const compact = token.replace(/\s/g, '');
  return Boolean(compact) && /^[*xX•…]+$/.test(compact);
}

function _operatorSessionRecallQuoteContainsRawSecret(quote){
  const text = _operatorSessionRecallText(quote, '');
  const keyedSecret = /\b(?:password|passwd|pwd|token|access[_\-\s]?token|refresh[_\-\s]?token|auth[_\-\s]?token|api[_\-\s]?key|apikey|secret|client[_\-\s]?secret|private[_\-\s]?key|authorization)\b\s*[:=]\s*(["']?[^"'\s,;]+["']?)/ig;
  let match;
  while ((match = keyedSecret.exec(text)) !== null) {
    if (!_operatorSessionRecallSecretValueRedacted(match[1])) return true;
  }
  return (
    /\bBearer\s+[A-Za-z0-9._~+/=-]{16,}(?=$|[^A-Za-z0-9._~+/=-])/i.test(text) ||
    /\bsk-[A-Za-z0-9][A-Za-z0-9_-]{16,}(?=$|[^A-Za-z0-9_-])/i.test(text) ||
    /\bxox[abp]-[0-9A-Za-z-]{10,}(?=$|[^0-9A-Za-z-])/i.test(text) ||
    /\bghp_[A-Za-z0-9_]{16,}(?![A-Za-z0-9_])/i.test(text) ||
    /\bgithub_pat_[A-Za-z0-9_]{16,}(?![A-Za-z0-9_])/i.test(text)
  );
}

function _operatorSessionRecallCommitmentSource(result){
  result = result || {};
  const promotion = result.promotion || {};
  const task = promotion.task || {};
  const source = (task.source && typeof task.source === 'object') ? task.source : {};
  return {
    kind: _operatorSessionRecallText(source.kind, ''),
    session_id: _operatorSessionRecallText(source.session_id, ''),
    message_index: source.message_index == null ? '' : source.message_index,
    content_hash: _operatorSessionRecallText(source.content_hash, ''),
    quote: _operatorSessionRecallText(source.quote, ''),
  };
}

function _operatorSessionRecallHasCompleteSessionMessageProof(source){
  return Boolean(
    source &&
    source.kind === 'session_message' &&
    _operatorSessionRecallHasValue(source.session_id) &&
    _operatorSessionRecallValidMessageIndex(source.message_index) &&
    _operatorSessionRecallValidContentHash(source.content_hash) &&
    _operatorSessionRecallHasValue(source.quote) &&
    !_operatorSessionRecallQuoteContainsRawSecret(source.quote)
  );
}

function _operatorSessionRecallCanPromoteTask(result){
  if (!result || !_operatorSessionRecallWouldExecuteFieldsSafe(result)) return false;
  const promotion = result.promotion || {};
  const task = promotion.task || {};
  if (task.enabled !== true) return false;
  if (_operatorSessionRecallText(task.mode, '') !== 'local_commitment_draft') return false;
  if (task.would_execute !== false) return false;
  const source = _operatorSessionRecallCommitmentSource(result);
  return _operatorSessionRecallHasCompleteSessionMessageProof(source);
}

function _operatorSessionRecallMemoryReviewSourceEvidence(result){
  result = result || {};
  const promotion = result.promotion || {};
  const memoryReview = promotion.memory_review || {};
  if (!Array.isArray(memoryReview.source_evidence) || !memoryReview.source_evidence.length) return null;
  const source = memoryReview.source_evidence[0];
  if (!source || typeof source !== 'object') return null;
  if (typeof source.kind !== 'string') return null;
  if (typeof source.session_id !== 'string') return null;
  if (typeof source.content_hash !== 'string') return null;
  if (typeof source.quote !== 'string') return null;
  if (!(typeof source.message_index === 'number' || typeof source.message_index === 'string')) return null;
  return {
    kind: _operatorSessionRecallText(source.kind, ''),
    session_id: _operatorSessionRecallText(source.session_id, ''),
    message_index: source.message_index,
    content_hash: _operatorSessionRecallText(source.content_hash, ''),
    quote: _operatorSessionRecallText(source.quote, ''),
  };
}

function _operatorSessionRecallCanPromoteMemoryReview(result){
  if (!result || !_operatorSessionRecallWouldExecuteFieldsSafe(result)) return false;
  const promotion = result.promotion || {};
  const memoryReview = promotion.memory_review || {};
  if (memoryReview.enabled === false) return false;
  if (memoryReview.would_execute !== false) return false;
  const mode = _operatorSessionRecallText(memoryReview.mode, 'local_memory_skill_review_proposal');
  if (!['local_memory_skill_review_proposal','memory_skill_review_proposal','local_review_queue_proposal'].includes(mode)) return false;
  const source = _operatorSessionRecallMemoryReviewSourceEvidence(result);
  return _operatorSessionRecallHasCompleteSessionMessageProof(source);
}

function _operatorSessionRecallOpenCommitmentDraft(result){
  if (typeof window !== 'undefined' && typeof window.openOperatorCommitmentPromoteFromRecall === 'function') {
    window.openOperatorCommitmentPromoteFromRecall(result);
    return true;
  }
  if (typeof openOperatorCommitmentPromoteFromRecall === 'function') {
    openOperatorCommitmentPromoteFromRecall(result);
    return true;
  }
  return false;
}

function _operatorSessionRecallSetText(id, value){
  const el = _operatorSessionRecallEl(id);
  if (el) el.textContent = _operatorSessionRecallText(value, '');
}

function _operatorSessionRecallFieldValue(id){
  const el = _operatorSessionRecallEl(id);
  return el ? _operatorSessionRecallText(el.value, '') : '';
}

function _operatorSessionRecallSetFieldValue(id, value){
  const el = _operatorSessionRecallEl(id);
  if (el) el.value = value == null ? '' : String(value);
}

function _operatorSessionRecallMemoryProposalStatus(message){
  _operatorSessionRecallSetText('operatorSessionRecallMemoryProposalStatus', message);
}

function _operatorSessionRecallMemoryProposalDiff(content){
  const lines = String(content == null ? '' : content).split('\n');
  const added = lines.map(line => '+' + line).join('\n');
  return '--- previous\n+++ proposed\n@@\n' + added + '\n';
}

function _operatorSessionRecallResetMemoryProposalFields(source){
  _operatorSessionRecallSetFieldValue('operatorSessionRecallMemoryProposalTargetKind', '');
  _operatorSessionRecallSetFieldValue('operatorSessionRecallMemoryProposalTargetSection', '');
  _operatorSessionRecallSetFieldValue('operatorSessionRecallMemoryProposalTargetName', '');
  _operatorSessionRecallSetFieldValue('operatorSessionRecallMemoryProposalTargetCategory', '');
  _operatorSessionRecallSetFieldValue('operatorSessionRecallMemoryProposalSummary', '');
  _operatorSessionRecallSetFieldValue('operatorSessionRecallMemoryProposalContent', '');
  _operatorSessionRecallSetText('operatorSessionRecallMemoryProposalEvidenceSessionId', source.session_id);
  _operatorSessionRecallSetText('operatorSessionRecallMemoryProposalEvidenceMessageIndex', source.message_index);
  _operatorSessionRecallSetText('operatorSessionRecallMemoryProposalEvidenceContentHash', source.content_hash);
  _operatorSessionRecallSetText('operatorSessionRecallMemoryProposalEvidenceQuote', source.quote);
  _operatorSessionRecallMemoryProposalStatus('Source evidence loaded. Enter target and proposed change fields to save a local proposal.');
}

function _operatorSessionRecallMemoryProposalTarget(){
  const kind = _operatorSessionRecallFieldValue('operatorSessionRecallMemoryProposalTargetKind').toLowerCase();
  if (kind === 'memory') {
    const section = _operatorSessionRecallFieldValue('operatorSessionRecallMemoryProposalTargetSection').toLowerCase();
    if (!['memory','user','soul'].includes(section)) return {error: 'target memory section is required'};
    return {value: {kind: 'memory', section: section}};
  }
  if (kind === 'skill') {
    const name = _operatorSessionRecallFieldValue('operatorSessionRecallMemoryProposalTargetName');
    if (!name) return {error: 'target skill name is required'};
    const target = {kind: 'skill', name: name};
    const category = _operatorSessionRecallFieldValue('operatorSessionRecallMemoryProposalTargetCategory');
    if (category) target.category = category;
    return {value: target};
  }
  return {error: 'target kind is required'};
}

function _operatorSessionRecallBuildMemoryProposalPayload(){
  const source = _operatorSessionRecallMemoryReviewDraftEvidence;
  if (!_operatorSessionRecallHasCompleteSessionMessageProof(source)) return {error: 'source_evidence session_message proof is required'};
  const targetResult = _operatorSessionRecallMemoryProposalTarget();
  if (targetResult.error) return {error: targetResult.error};

  const operation = _operatorSessionRecallFieldValue('operatorSessionRecallMemoryProposalOperation').toLowerCase();
  if (!['append','edit','delete'].includes(operation)) return {error: 'proposed_change operation is required'};
  const summary = _operatorSessionRecallFieldValue('operatorSessionRecallMemoryProposalSummary');
  if (!summary) return {error: 'proposed_change summary is required'};
  const content = _operatorSessionRecallFieldValue('operatorSessionRecallMemoryProposalContent');
  if (!content) return {error: 'proposed_change content is required'};

  const durability = _operatorSessionRecallFieldValue('operatorSessionRecallMemoryProposalClassificationDurability').toLowerCase();
  if (!['durable','transient','unknown'].includes(durability)) return {error: 'classification durability is required'};
  const classificationReason = _operatorSessionRecallFieldValue('operatorSessionRecallMemoryProposalClassificationReason');
  if (!classificationReason) return {error: 'classification reason is required'};
  const transientRisk = _operatorSessionRecallFieldValue('operatorSessionRecallMemoryProposalTransientRisk').toLowerCase();
  if (!['low','medium','high'].includes(transientRisk)) return {error: 'classification transient risk is required'};

  const staleState = _operatorSessionRecallFieldValue('operatorSessionRecallMemoryProposalStaleState').toLowerCase();
  if (!['current','review_required','expired'].includes(staleState)) return {error: 'stale_risk state is required'};
  const expiresAt = _operatorSessionRecallFieldValue('operatorSessionRecallMemoryProposalExpiresAt');
  if (!expiresAt) return {error: 'stale_risk expires_at is required'};
  const staleReason = _operatorSessionRecallFieldValue('operatorSessionRecallMemoryProposalStaleReason');
  if (!staleReason) return {error: 'stale_risk reason is required'};

  return {
    value: {
      target: targetResult.value,
      proposed_change: {
        operation: operation,
        summary: summary,
        diff: _operatorSessionRecallMemoryProposalDiff(content),
        proposed_content: content,
      },
      source_evidence: [source],
      classification: {
        durability: durability,
        reason: classificationReason,
        transient_risk: transientRisk,
      },
      stale_risk: {
        state: staleState,
        expires_at: expiresAt,
        reason: staleReason,
      },
      would_execute: false,
    },
  };
}

function openOperatorSessionRecallMemoryProposal(result){
  const source = _operatorSessionRecallMemoryReviewSourceEvidence(result);
  if (!_operatorSessionRecallHasCompleteSessionMessageProof(source)) {
    _operatorSessionRecallMemoryReviewDraftEvidence = null;
    _operatorSessionRecallMemoryProposalStatus('Cannot draft memory/skill review: explicit safe session_message source_evidence is required.');
    return false;
  }
  _operatorSessionRecallMemoryReviewDraftEvidence = source;
  _operatorSessionRecallResetMemoryProposalFields(source);
  const panel = _operatorSessionRecallEl('operatorSessionRecallMemoryProposalPanel');
  if (panel) panel.hidden = false;
  return true;
}

function cancelOperatorSessionRecallMemoryProposal(){
  _operatorSessionRecallMemoryReviewDraftEvidence = null;
  const panel = _operatorSessionRecallEl('operatorSessionRecallMemoryProposalPanel');
  if (panel) panel.hidden = true;
  _operatorSessionRecallMemoryProposalStatus('');
}

async function submitOperatorSessionRecallMemoryProposal(event){
  if (event && typeof event.preventDefault === 'function') event.preventDefault();
  const built = _operatorSessionRecallBuildMemoryProposalPayload();
  if (!built.value) {
    _operatorSessionRecallMemoryProposalStatus(built.error || 'memory/skill review proposal is incomplete');
    return null;
  }
  if (typeof api !== 'function') {
    _operatorSessionRecallMemoryProposalStatus('api helper unavailable; proposal not saved');
    return null;
  }
  _operatorSessionRecallMemoryProposalStatus('Saving local memory/skill review proposal…');
  try {
    const result = await api('/api/operator/memory-skill-review/propose', {method: 'POST', body: JSON.stringify(built.value)});
    if (!result || result.ok !== true) {
      const message = (result && (result.error || result.message)) || 'memory/skill review proposal failed';
      _operatorSessionRecallMemoryProposalStatus(message);
      return result || null;
    }
    _operatorSessionRecallMemoryProposalStatus('Local memory/skill review proposal saved. Nothing was applied.');
    if (typeof refreshOperatorMemorySkillReview === 'function') {
      await refreshOperatorMemorySkillReview({force: true});
    }
    return result;
  } catch (err) {
    _operatorSessionRecallMemoryProposalStatus((err && err.message) || 'memory/skill review proposal failed');
    return null;
  }
}

function _operatorSessionRecallRow(label, value){
  const row = document.createElement('div');
  row.className = 'operator-session-recall-source';
  const key = document.createElement('span');
  key.className = 'operator-session-recall-source-label';
  key.textContent = label;
  const val = document.createElement('span');
  val.className = 'operator-session-recall-source-value';
  val.textContent = value;
  row.append(key, val);
  return row;
}

function _operatorSessionRecallEmpty(message, sourceMessage){
  const empty = document.createElement('div');
  empty.className = 'operator-session-recall-card operator-session-recall-empty operator-session-recall-stale';
  const snippet = document.createElement('div');
  snippet.className = 'operator-session-recall-snippet';
  snippet.textContent = message;
  const source = document.createElement('div');
  source.className = 'operator-session-recall-source operator-session-recall-stale';
  source.textContent = sourceMessage || 'No session source queried yet.';
  const action = document.createElement('div');
  action.className = 'operator-session-recall-action';
  action.textContent = 'would_execute=false';
  empty.append(snippet, source, action);
  return empty;
}

function _operatorSessionRecallIssue(message){
  const issue = document.createElement('div');
  issue.className = 'operator-session-recall-issue operator-session-recall-stale';
  issue.textContent = _operatorSessionRecallText(message, 'unknown issue');
  return issue;
}

function _operatorSessionRecallCard(result){
  result = result || {};
  const session = result.session || {};
  const match = result.match || {};
  const card = document.createElement('div');
  card.className = 'operator-session-recall-card';

  const recencyLabel = _operatorSessionRecallRecency(result);
  if (recencyLabel === 'historical') card.classList.add('operator-session-recall-historical');
  if (recencyLabel === 'stale' || recencyLabel === 'unknown' || result.stale) card.classList.add('operator-session-recall-stale');

  const top = document.createElement('div');
  top.className = 'operator-session-recall-source';
  const title = document.createElement('span');
  title.className = 'operator-session-recall-title';
  title.textContent = _operatorSessionRecallText(session.title || result.title, 'Untitled session');
  const badge = document.createElement('span');
  badge.className = 'operator-session-recall-action';
  badge.textContent = 'would_execute=' + _operatorSessionRecallWouldExecuteState(result);
  top.append(title, badge);
  card.append(top);

  const snippet = document.createElement('div');
  snippet.className = 'operator-session-recall-snippet';
  snippet.textContent = _operatorSessionRecallText(match.snippet || result.snippet, 'No snippet available.');
  card.append(snippet);

  const sessionId = _operatorSessionRecallText(session.session_id || result.session_id, 'session_id unknown');
  const sourceLabel = _operatorSessionRecallText(session.source_label || result.source_label || result.source, 'source_label unknown');
  const timestamp = _operatorSessionRecallTimestamp(match.timestamp || result.timestamp);
  card.append(
    _operatorSessionRecallRow('session_id', sessionId),
    _operatorSessionRecallRow('source_label', sourceLabel),
    _operatorSessionRecallRow('timestamp', timestamp),
    _operatorSessionRecallRow('recency', recencyLabel),
  );

  if (result.recency && result.recency.reason) {
    card.append(_operatorSessionRecallRow('recency_reason', _operatorSessionRecallText(result.recency.reason, 'unknown')));
  }

  const actionButtons = [];
  if (_operatorSessionRecallCanPromoteTask(result)) {
    const promote = document.createElement('button');
    promote.type = 'button';
    promote.className = 'operator-session-recall-action primary';
    promote.textContent = 'Draft local task';
    promote.title = 'Open a local commitment draft only; no Kanban or chat action.';
    promote.addEventListener('click', () => {
      _operatorSessionRecallOpenCommitmentDraft(result);
    });
    actionButtons.push(promote);
  }
  if (_operatorSessionRecallCanPromoteMemoryReview(result)) {
    const proposeMemory = document.createElement('button');
    proposeMemory.type = 'button';
    proposeMemory.className = 'operator-session-recall-action primary';
    proposeMemory.textContent = 'Draft memory/skill review';
    proposeMemory.title = 'Seed a local memory/skill review proposal only; no apply or file write.';
    proposeMemory.addEventListener('click', () => {
      openOperatorSessionRecallMemoryProposal(result);
    });
    actionButtons.push(proposeMemory);
  }
  if (actionButtons.length) {
    const actions = document.createElement('div');
    actions.className = 'operator-session-recall-actions';
    actionButtons.forEach(button => actions.append(button));
    card.append(actions);
  }

  return card;
}

function renderOperatorSessionRecall(payload){
  payload = payload || _operatorSessionRecallDefaultPayload();
  _operatorSessionRecallLastPayload = payload;

  const chip = _operatorSessionRecallEl('operatorSessionRecallChip');
  const label = _operatorSessionRecallEl('operatorSessionRecallLabel');
  const results = Array.isArray(payload.results) ? payload.results : [];
  if (chip) {
    chip.hidden = false;
    chip.classList.remove('state-live','state-stale','state-unknown');
    chip.classList.add(_operatorSessionRecallStateClass(payload.status));
    chip.title = _operatorSessionRecallText(payload.summary, 'Manual session recall');
  }
  if (label) label.textContent = results.length ? 'Recall ' + results.length : 'Recall';

  const popover = _operatorSessionRecallEl('operatorSessionRecallPopover');
  const status = _operatorSessionRecallEl('operatorSessionRecallStatus');
  const list = _operatorSessionRecallEl('operatorSessionRecallList');
  if (!popover || !list) return payload;

  popover.classList.remove('state-live','state-stale','state-unknown');
  popover.classList.add(_operatorSessionRecallStateClass(payload.status));
  if (status) status.textContent = _operatorSessionRecallText(payload.summary, 'Manual session recall — search on demand only');

  _operatorSessionRecallClear(list);
  const query = _operatorSessionRecallQueryText(payload);
  if (query) {
    list.append(_operatorSessionRecallRow('query', query));
  }

  if (!results.length) {
    const message = query ? 'No source-backed session recall results for this query.' : 'Enter a query and press Search to recall saved sessions.';
    const sourceMessage = query ? 'Search completed or source unavailable; no fake results shown.' : 'No session source queried yet.';
    list.append(_operatorSessionRecallEmpty(message, sourceMessage));
  } else {
    results.forEach(result => list.append(_operatorSessionRecallCard(result)));
  }

  const issues = Array.isArray(payload.issues) ? payload.issues : [];
  issues.slice(0, 5).forEach(issue => list.append(_operatorSessionRecallIssue(issue)));
  return payload;
}

async function refreshOperatorSessionRecall(opts = {}){
  const input = _operatorSessionRecallEl('operatorSessionRecallInput');
  const query = input ? _operatorSessionRecallText(input.value, '') : '';
  const force = Boolean(opts.force);
  if (!query) {
    _operatorSessionRecallRequestSeq += 1;
    return renderOperatorSessionRecall(_operatorSessionRecallBlankPayload());
  }
  if (typeof api !== 'function') {
    return renderOperatorSessionRecall(_operatorSessionRecallUnavailablePayload(query, 'api helper unavailable'));
  }

  const params = new URLSearchParams();
  params.set('q', query);
  params.set('limit', '20');
  params.set('per_session', '2');

  const requestSeq = ++_operatorSessionRecallRequestSeq;
  const pendingSummary = (force ? 'Refreshing' : 'Searching') + ' saved sessions for query: ' + query;
  renderOperatorSessionRecall({
    status: 'stale',
    summary: pendingSummary,
    query: {text: query},
    sources: [],
    results: [],
    count: 0,
    issues: [],
    would_execute: false,
  });

  try {
    const payload = await api('/api/operator/session-recall?' + params.toString());
    if (requestSeq !== _operatorSessionRecallRequestSeq) return payload;
    return renderOperatorSessionRecall(payload);
  } catch (err) {
    if (requestSeq !== _operatorSessionRecallRequestSeq) return null;
    const message = err && err.message ? err.message : 'session recall request failed';
    return renderOperatorSessionRecall(_operatorSessionRecallUnavailablePayload(query, message));
  }
}

function hideOperatorSessionRecall(){
  const popover = _operatorSessionRecallEl('operatorSessionRecallPopover');
  if (popover) popover.hidden = true;
}

function toggleOperatorSessionRecall(opts = {}){
  const popover = _operatorSessionRecallEl('operatorSessionRecallPopover');
  if (!popover) return;
  const opening = popover.hidden;
  if (!opening && !opts.force) {
    hideOperatorSessionRecall();
    return;
  }
  popover.hidden = false;
  renderOperatorSessionRecall(_operatorSessionRecallLastPayload);
  const input = _operatorSessionRecallEl('operatorSessionRecallInput');
  if (input && typeof input.focus === 'function') input.focus();
}

function initOperatorSessionRecall(){
  const chip = _operatorSessionRecallEl('operatorSessionRecallChip');
  const label = _operatorSessionRecallEl('operatorSessionRecallLabel');
  const input = _operatorSessionRecallEl('operatorSessionRecallInput');
  if (chip) {
    chip.hidden = false;
    chip.title = 'Manual session recall · search on demand only';
  }
  if (label) label.textContent = 'Recall';
  if (input && !input.dataset.sessionRecallBound) {
    input.dataset.sessionRecallBound = '1';
    input.addEventListener('keydown', event => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      refreshOperatorSessionRecall();
    });
  }
  renderOperatorSessionRecall(_operatorSessionRecallLastPayload);
}

window.toggleOperatorSessionRecall = toggleOperatorSessionRecall;
window.refreshOperatorSessionRecall = refreshOperatorSessionRecall;
window.renderOperatorSessionRecall = renderOperatorSessionRecall;
window.initOperatorSessionRecall = initOperatorSessionRecall;
window.hideOperatorSessionRecall = hideOperatorSessionRecall;

initOperatorSessionRecall();
