let _operatorCommitmentLastFetchKey = '';
let _operatorCommitmentLastPayload = null;
let _operatorCommitmentInFlight = null;
let _operatorCommitmentInFlightKey = '';
let _operatorCommitmentRequestSeq = 0;
let _operatorCommitmentDraftSource = null;
let _operatorCommitmentDraftEvidence = [];

function _operatorCommitmentEl(id){
  if (typeof $ === 'function') return $(id);
  return document.getElementById(id);
}

function operatorCommitmentContextKey(){
  const session = (typeof S !== 'undefined' && S && S.session) || {};
  const truthKey = (typeof operatorTruthContextKey === 'function') ? operatorTruthContextKey() : '';
  return JSON.stringify({
    truth: truthKey,
    session_id: (typeof operatorTruthSessionId === 'function') ? operatorTruthSessionId() : (session.session_id || session.id || ''),
    ui_board: (typeof operatorTruthBoardHint === 'function') ? operatorTruthBoardHint() : '',
    profile: (typeof S !== 'undefined' && S && S.activeProfile) || '',
    workspace: session.workspace || '',
  });
}

function _operatorCommitmentStateClass(state){
  if (state === 'live') return 'state-live';
  if (state === 'stale') return 'state-stale';
  return 'state-unknown';
}

function _operatorCommitmentText(value, fallback){
  const text = String(value == null ? '' : value).trim();
  return text || fallback || '';
}

function _operatorCommitmentClear(el){
  if (!el) return;
  while (el.firstChild) el.removeChild(el.firstChild);
}

function _operatorCommitmentApplyChip(payload){
  const chip = _operatorCommitmentEl('operatorCommitmentChip');
  const label = _operatorCommitmentEl('operatorCommitmentLabel');
  if (!chip) return;
  const status = (payload && payload.status) || 'unknown';
  chip.hidden = false;
  chip.classList.remove('state-live','state-stale','state-unknown');
  chip.classList.add(_operatorCommitmentStateClass(status));
  const count = payload && Array.isArray(payload.commitments) ? payload.commitments.length : 0;
  if (label) label.textContent = count ? 'Commitments ' + count : 'Commitments';
  chip.title = (payload && payload.summary) || 'Local commitment cards';
}

function _operatorCommitmentItem(text, className){
  const item = document.createElement('div');
  item.className = className || 'operator-commitment-muted';
  item.textContent = _operatorCommitmentText(text, 'unknown');
  return item;
}

function _operatorCommitmentChip(text, state){
  const chip = document.createElement('span');
  chip.className = 'operator-commitment-required ' + _operatorCommitmentStateClass(state || 'unknown');
  chip.textContent = text;
  return chip;
}

function _operatorCommitmentRow(label, value){
  const row = document.createElement('div');
  row.className = 'operator-commitment-row';
  const key = document.createElement('span');
  key.className = 'operator-commitment-row-key';
  key.textContent = label;
  const val = document.createElement('span');
  val.className = 'operator-commitment-row-value';
  val.textContent = _operatorCommitmentText(value, 'unknown');
  row.append(key, val);
  return row;
}

function _operatorCommitmentSection(title){
  const section = document.createElement('div');
  section.className = 'operator-commitment-section';
  const heading = document.createElement('div');
  heading.className = 'operator-commitment-section-title';
  heading.textContent = title;
  section.append(heading);
  return section;
}

function _operatorCommitmentCard(commitment){
  const card = document.createElement('div');
  card.className = 'operator-commitment-card';
  card.dataset.commitmentId = commitment.id || '';

  const top = document.createElement('div');
  top.className = 'operator-commitment-card-top';
  const title = document.createElement('div');
  title.className = 'operator-commitment-card-title';
  title.textContent = _operatorCommitmentText(commitment.title, commitment.id || 'Untitled commitment');
  const noExec = document.createElement('span');
  noExec.className = 'operator-commitment-no-exec';
  noExec.textContent = 'would_execute:false';
  top.append(title, noExec);
  card.append(top);

  const summary = document.createElement('div');
  summary.className = 'operator-commitment-summary';
  summary.textContent = _operatorCommitmentText(commitment.summary, 'No summary available.');
  card.append(summary);

  const dispatch_mechanism = commitment.dispatch_mechanism || {};
  const source = commitment.source || {};
  card.append(_operatorCommitmentRow('Owner', commitment.owner));
  card.append(_operatorCommitmentRow('Deadline', commitment.deadline_at || commitment.review_at));
  card.append(_operatorCommitmentRow('Dispatch', _operatorCommitmentText(dispatch_mechanism.kind, 'manual') + ' · local record only'));
  card.append(_operatorCommitmentRow('Status', commitment.status));
  card.append(_operatorCommitmentRow('Source', _operatorCommitmentText(source.kind, 'source') + ' · ' + _operatorCommitmentText(source.proposal_id || source.session_id, 'unknown')));

  const acceptance_criteria = Array.isArray(commitment.acceptance_criteria) ? commitment.acceptance_criteria : [];
  const acceptance = _operatorCommitmentSection('Acceptance criteria');
  if (!acceptance_criteria.length) acceptance.append(_operatorCommitmentItem('Acceptance criteria missing', 'operator-commitment-missing'));
  acceptance_criteria.slice(0, 6).forEach(item => acceptance.append(_operatorCommitmentItem(item, 'operator-commitment-muted')));
  card.append(acceptance);

  const halt = _operatorCommitmentSection('Halt policy');
  halt.append(_operatorCommitmentItem(commitment.halt_policy, 'operator-commitment-muted'));
  card.append(halt);

  const evidence = Array.isArray(commitment.evidence) ? commitment.evidence : [];
  const receipts = _operatorCommitmentSection('Evidence');
  if (!evidence.length) receipts.append(_operatorCommitmentItem('Evidence missing', 'operator-commitment-missing'));
  evidence.slice(0, 5).forEach(item => {
    const label = _operatorCommitmentText(item.label || item.source_id || item.kind, 'evidence');
    const state = _operatorCommitmentText(item.state || item.status, 'unknown');
    receipts.append(_operatorCommitmentItem(label + ' · ' + state, 'operator-commitment-muted'));
  });
  card.append(receipts);
  return card;
}

function _operatorCommitmentRenderIssues(list, payload){
  const issues = Array.isArray(payload && payload.issues) ? payload.issues : [];
  issues.slice(0, 6).forEach(issue => list.append(_operatorCommitmentItem(issue, 'operator-commitment-missing')));
  const notes = Array.isArray(payload && payload.notes) ? payload.notes : [];
  notes.slice(0, 4).forEach(note => {
    const missing = Array.isArray(note.missing) ? note.missing.join(', ') : 'required fields';
    list.append(_operatorCommitmentItem('Note: ' + _operatorCommitmentText(note.reason, 'incomplete') + ' · ' + missing, 'operator-commitment-missing'));
  });
}

function renderOperatorCommitments(payload){
  _operatorCommitmentLastPayload = payload || null;
  _operatorCommitmentApplyChip(payload);
  const popover = _operatorCommitmentEl('operatorCommitmentPopover');
  const subtitle = _operatorCommitmentEl('operatorCommitmentSubtitle');
  const list = _operatorCommitmentEl('operatorCommitmentList');
  if (!popover || !list) return;
  _operatorCommitmentClear(list);

  const status = (payload && payload.status) || 'unknown';
  popover.classList.remove('state-live','state-stale','state-unknown');
  popover.classList.add(_operatorCommitmentStateClass(status));
  if (subtitle) subtitle.textContent = (payload && payload.summary) || 'Not checked yet';

  const top = document.createElement('div');
  top.className = 'operator-commitment-source-row';
  top.append(
    _operatorCommitmentChip('local state', status),
    _operatorCommitmentChip('would_execute:false', 'live')
  );
  list.append(top);

  const commitments = Array.isArray(payload && payload.commitments) ? payload.commitments : [];
  if (!commitments.length) {
    const empty = document.createElement('div');
    empty.className = 'operator-commitment-empty';
    empty.textContent = status === 'unknown' ? 'No commitment cards — local source unavailable.' : 'No commitments yet. Promote a proposal to create one.';
    list.append(empty);
    _operatorCommitmentRenderIssues(list, payload || {});
    return;
  }

  commitments.forEach(commitment => list.append(_operatorCommitmentCard(commitment)));
  _operatorCommitmentRenderIssues(list, payload || {});
}

function hideOperatorCommitments(){
  const popover = _operatorCommitmentEl('operatorCommitmentPopover');
  if (popover) popover.hidden = true;
}

function toggleOperatorCommitments(opts = {}){
  const popover = _operatorCommitmentEl('operatorCommitmentPopover');
  if (!popover) return;
  const opening = popover.hidden;
  if (!opening && !opts.force) {
    hideOperatorCommitments();
    return;
  }
  popover.hidden = false;
  refreshOperatorCommitments({force: Boolean(opts.force)});
}

async function refreshOperatorCommitments(opts = {}){
  const force = Boolean(opts.force);
  const contextKey = operatorCommitmentContextKey();
  if (!force && _operatorCommitmentInFlight && contextKey === _operatorCommitmentInFlightKey) return _operatorCommitmentInFlight;
  if (!force && _operatorCommitmentLastPayload && contextKey === _operatorCommitmentLastFetchKey) {
    renderOperatorCommitments(_operatorCommitmentLastPayload);
    return _operatorCommitmentLastPayload;
  }
  if (typeof api !== 'function') {
    const payload = {status:'unknown', summary:'Commitments unavailable — API helper missing', commitments:[], notes:[], issues:['api helper unavailable']};
    renderOperatorCommitments(payload);
    return payload;
  }

  const params = new URLSearchParams();
  const session = (typeof S !== 'undefined' && S && S.session) || {};
  const sid = (typeof operatorTruthSessionId === 'function') ? operatorTruthSessionId() : (session.session_id || session.id || '');
  const board = (typeof operatorTruthBoardHint === 'function') ? operatorTruthBoardHint() : '';
  if (sid) params.set('session_id', sid);
  if (board) params.set('ui_board', board);
  const base = '/api/operator/commitments';
  const path = base + (params.toString() ? '?' + params.toString() : '');
  const requestKey = contextKey;
  const requestSeq = ++_operatorCommitmentRequestSeq;
  _operatorCommitmentInFlightKey = requestKey;

  _operatorCommitmentInFlight = api(path)
    .then(payload => {
      if (requestKey !== operatorCommitmentContextKey() || requestSeq !== _operatorCommitmentRequestSeq) return payload;
      _operatorCommitmentLastFetchKey = requestKey;
      renderOperatorCommitments(payload);
      return payload;
    })
    .catch(err => {
      if (requestKey === operatorCommitmentContextKey() && requestSeq === _operatorCommitmentRequestSeq) {
        const payload = {status:'unknown', summary:'Commitments unavailable — source read failed', commitments:[], notes:[], issues:[(err && err.message) || 'request failed']};
        _operatorCommitmentLastFetchKey = requestKey;
        renderOperatorCommitments(payload);
      }
      return null;
    })
    .finally(() => {
      if (requestKey === _operatorCommitmentInFlightKey && requestSeq === _operatorCommitmentRequestSeq) {
        _operatorCommitmentInFlight = null;
        _operatorCommitmentInFlightKey = '';
      }
    });
  return _operatorCommitmentInFlight;
}

function _operatorCommitmentSetValue(id, value){
  const el = _operatorCommitmentEl(id);
  if (el) el.value = value == null ? '' : String(value);
}

function _operatorCommitmentGetValue(id){
  const el = _operatorCommitmentEl(id);
  return el ? String(el.value || '').trim() : '';
}

function _operatorCommitmentSafeJson(value, fallback){
  try {
    return JSON.parse(value || '');
  } catch(_) {
    return fallback;
  }
}

function _operatorCommitmentLines(value){
  return String(value || '').split('\n').map(line => line.trim().replace(/^[-*]\s*/, '')).filter(Boolean);
}

function _operatorCommitmentValidMessageIndex(value){
  if (typeof value === 'number') return Number.isInteger(value) && value >= 0;
  if (typeof value === 'string') {
    const text = value.trim();
    if (!/^\d+$/.test(text)) return false;
    const numeric = Number(text);
    return Number.isSafeInteger(numeric) && numeric >= 0;
  }
  return false;
}

function _operatorCommitmentValidContentHash(value){
  return /^sha256:[0-9a-f]{64}$/i.test(_operatorCommitmentText(value, ''));
}

function _operatorCommitmentSecretValueRedacted(value){
  const token = _operatorCommitmentText(value, '').replace(/^["']|["']$/g, '').trim();
  const normalized = token.replace(/^[\[\](){}<>]+|[\[\](){}<>]+$/g, '').toLowerCase();
  if (['redacted','masked','hidden','removed','withheld','scrubbed'].includes(normalized)) return true;
  const compact = token.replace(/\s/g, '');
  return Boolean(compact) && /^[*xX•…]+$/.test(compact);
}

function _operatorCommitmentQuoteContainsRawSecret(quote){
  const text = _operatorCommitmentText(quote, '');
  const keyedSecret = /\b(?:password|passwd|pwd|token|access[_\-\s]?token|refresh[_\-\s]?token|auth[_\-\s]?token|api[_\-\s]?key|apikey|secret|client[_\-\s]?secret|private[_\-\s]?key|authorization)\b\s*[:=]\s*(["']?[^"'\s,;]+["']?)/ig;
  let match;
  while ((match = keyedSecret.exec(text)) !== null) {
    if (!_operatorCommitmentSecretValueRedacted(match[1])) return true;
  }
  return (
    /\bBearer\s+[A-Za-z0-9._~+/=-]{16,}(?=$|[\s,;])/i.test(text) ||
    /\bsk-[A-Za-z0-9][A-Za-z0-9_-]{16,}(?=$|[\s,;])/i.test(text) ||
    /\bxox[abp]-[0-9A-Za-z-]{10,}(?=$|[\s,;])/i.test(text) ||
    /\bghp_[A-Za-z0-9_]{16,}(?![A-Za-z0-9_])/i.test(text) ||
    /\bgithub_pat_[A-Za-z0-9_]{16,}(?![A-Za-z0-9_])/i.test(text)
  );
}

function _operatorCommitmentSessionMessageProofMissing(source){
  const missing = [];
  if (!source || source.kind !== 'session_message') return missing;
  if (!_operatorCommitmentText(source.session_id, '')) missing.push('session_id');
  if (source.message_index == null || String(source.message_index).trim() === '' || !_operatorCommitmentValidMessageIndex(source.message_index)) missing.push('message_index');
  if (!_operatorCommitmentText(source.content_hash, '') || !_operatorCommitmentValidContentHash(source.content_hash)) missing.push('content_hash');
  const quote = _operatorCommitmentText(source.quote, '');
  if (!quote) missing.push('quote');
  else if (_operatorCommitmentQuoteContainsRawSecret(quote)) missing.push('quote raw secret');
  return missing;
}

function _operatorCommitmentSourceProofMessage(missingFields){
  const fields = Array.isArray(missingFields) && missingFields.length ? missingFields.join(', ') : 'session_id, message_index, content_hash, quote';
  return 'Missing or invalid source proof fields for session_message: ' + fields;
}

function _operatorCommitmentRejectMissingSourceProof(form, missingFields){
  const missingEl = _operatorCommitmentEl('operatorCommitmentMissing');
  if (missingEl) missingEl.textContent = _operatorCommitmentSourceProofMessage(missingFields);
  if (form) form.hidden = true;
  return false;
}

function _operatorCommitmentValidateForm(){
  const missing = [];
  const owner = _operatorCommitmentGetValue('operatorCommitmentOwner');
  const deadline_at = _operatorCommitmentGetValue('operatorCommitmentDeadline');
  const review_at = _operatorCommitmentGetValue('operatorCommitmentReview');
  const dispatchKind = _operatorCommitmentGetValue('operatorCommitmentDispatch') || 'manual';
  const source = _operatorCommitmentSafeJson(_operatorCommitmentGetValue('operatorCommitmentSource'), null);
  const acceptance_criteria = _operatorCommitmentLines(_operatorCommitmentGetValue('operatorCommitmentAcceptance'));
  const halt_policy = _operatorCommitmentGetValue('operatorCommitmentHaltPolicy');
  const evidence = _operatorCommitmentSafeJson(_operatorCommitmentGetValue('operatorCommitmentEvidence'), []);
  const status = _operatorCommitmentGetValue('operatorCommitmentStatus') || 'active';

  if (!owner) missing.push('owner');
  if (!deadline_at && !review_at) missing.push('deadline_at');
  if (!dispatchKind) missing.push('dispatch_mechanism');
  if (!source || !source.kind) missing.push('source');
  if (source && source.kind === 'session_message') {
    const proofMissing = _operatorCommitmentSessionMessageProofMissing(source);
    if (proofMissing.length) missing.push('source proof fields: ' + proofMissing.join(', '));
  }
  if (!acceptance_criteria.length) missing.push('acceptance_criteria');
  if (!halt_policy) missing.push('halt_policy');
  if (!Array.isArray(evidence) || !evidence.length) missing.push('evidence');
  if (!status) missing.push('status');

  const missingEl = _operatorCommitmentEl('operatorCommitmentMissing');
  if (missing.length) {
    if (missingEl) missingEl.textContent = 'Missing required fields: ' + missing.join(', ');
    return null;
  }
  if (missingEl) missingEl.textContent = '';

  return {
    title: _operatorCommitmentGetValue('operatorCommitmentTitle'),
    owner,
    deadline_at,
    review_at,
    dispatch_mechanism: {kind: dispatchKind, label: dispatchKind + ' local follow-up', would_execute: false},
    source,
    acceptance_criteria,
    halt_policy,
    evidence,
    status,
  };
}

function _operatorCommitmentEvidenceFromProposal(proposal){
  const evidence = [];
  if (proposal && Array.isArray(proposal.evidence)) {
    proposal.evidence.slice(0, 8).forEach(item => {
      if (!item) return;
      evidence.push({
        kind: _operatorCommitmentText(item.kind, 'proposal_evidence'),
        label: _operatorCommitmentText(item.label || item.source_id || item.api || item.path, 'Operator proposal evidence'),
        state: _operatorCommitmentText(item.state || item.status, 'unknown'),
        source_id: item.source_id || undefined,
        path: item.path || undefined,
        api: item.api || undefined,
      });
    });
  }
  evidence.unshift({kind:'source', label:'Operator proposal', state:'present'});
  return evidence;
}

function openOperatorCommitmentPromote(proposal){
  const form = _operatorCommitmentEl('operatorCommitmentForm');
  const popover = _operatorCommitmentEl('operatorCommitmentPopover');
  if (!form || !popover || !proposal) return;
  const session = (typeof S !== 'undefined' && S && S.session) || {};
  _operatorCommitmentDraftSource = {
    kind: 'operator_proposal',
    proposal_id: proposal.id || '',
    session_id: (typeof operatorTruthSessionId === 'function') ? operatorTruthSessionId() : (session.session_id || session.id || ''),
    ui_board: (typeof operatorTruthBoardHint === 'function') ? operatorTruthBoardHint() : '',
  };
  _operatorCommitmentDraftEvidence = _operatorCommitmentEvidenceFromProposal(proposal);

  _operatorCommitmentSetValue('operatorCommitmentSource', JSON.stringify(_operatorCommitmentDraftSource));
  _operatorCommitmentSetValue('operatorCommitmentEvidence', JSON.stringify(_operatorCommitmentDraftEvidence));
  _operatorCommitmentSetValue('operatorCommitmentTitle', _operatorCommitmentText(proposal.title, proposal.id || 'Operator proposal'));
  _operatorCommitmentSetValue('operatorCommitmentOwner', _operatorCommitmentText(proposal.owner, ''));
  _operatorCommitmentSetValue('operatorCommitmentDeadline', '');
  _operatorCommitmentSetValue('operatorCommitmentReview', '');
  _operatorCommitmentSetValue('operatorCommitmentDispatch', 'manual');
  _operatorCommitmentSetValue('operatorCommitmentAcceptance', Array.isArray(proposal.acceptance_criteria) ? proposal.acceptance_criteria.join('\n') : '');
  _operatorCommitmentSetValue('operatorCommitmentHaltPolicy', 'Stop if source evidence is stale or missing.');
  _operatorCommitmentSetValue('operatorCommitmentStatus', 'active');
  const missingEl = _operatorCommitmentEl('operatorCommitmentMissing');
  if (missingEl) missingEl.textContent = 'Fill owner/deadline/criteria, then save locally. No execution is attached.';
  form.hidden = false;
  popover.hidden = false;
  refreshOperatorCommitments({force:false});
}

function openOperatorCommitmentPromoteFromRecall(result){
  const form = _operatorCommitmentEl('operatorCommitmentForm');
  const popover = _operatorCommitmentEl('operatorCommitmentPopover');
  if (!form || !popover || !result) return;
  const session = result.session || {};
  const promotion = result.promotion || {};
  const task = promotion.task || {};
  const sourceHint = (task.source && typeof task.source === 'object') ? task.source : {};
  const sourceKind = _operatorCommitmentText(sourceHint.kind, '');
  if (sourceKind !== 'session_message') {
    _operatorCommitmentRejectMissingSourceProof(form, ['kind', 'session_id', 'message_index', 'content_hash', 'quote']);
    return false;
  }
  const source = {
    kind:'session_message',
    session_id: _operatorCommitmentText(sourceHint.session_id, ''),
    message_index: sourceHint.message_index == null ? '' : sourceHint.message_index,
    content_hash: _operatorCommitmentText(sourceHint.content_hash, ''),
    quote: _operatorCommitmentText(sourceHint.quote, ''),
  };
  const proofMissing = _operatorCommitmentSessionMessageProofMissing(source);
  if (proofMissing.length) {
    _operatorCommitmentRejectMissingSourceProof(form, proofMissing);
    return false;
  }
  const recency = result.recency || {};
  const recencyState = _operatorCommitmentText(recency.label || result.recency_label, 'unknown').toLowerCase();
  _operatorCommitmentDraftSource = source;
  _operatorCommitmentDraftEvidence = [{
    kind: 'source',
    label: 'Session recall result',
    state: recencyState === 'live' ? 'present' : recencyState,
    session_id: source.session_id,
    message_index: source.message_index,
    source_id: result.id || source.content_hash || source.session_id,
  }];

  const sessionTitle = _operatorCommitmentText(session.title || result.title, 'Session recall');
  const quoteTitle = _operatorCommitmentText(source.quote, '').slice(0, 90);
  const title = (sessionTitle + (quoteTitle ? ': ' + quoteTitle : '')).slice(0, 200);
  _operatorCommitmentSetValue('operatorCommitmentSource', JSON.stringify(_operatorCommitmentDraftSource));
  _operatorCommitmentSetValue('operatorCommitmentEvidence', JSON.stringify(_operatorCommitmentDraftEvidence));
  _operatorCommitmentSetValue('operatorCommitmentTitle', title);
  _operatorCommitmentSetValue('operatorCommitmentOwner', '');
  _operatorCommitmentSetValue('operatorCommitmentDeadline', '');
  _operatorCommitmentSetValue('operatorCommitmentReview', '');
  _operatorCommitmentSetValue('operatorCommitmentDispatch', 'manual');
  _operatorCommitmentSetValue('operatorCommitmentAcceptance', '');
  _operatorCommitmentSetValue('operatorCommitmentHaltPolicy', 'Stop if source evidence is stale or missing.');
  _operatorCommitmentSetValue('operatorCommitmentStatus', 'active');
  const missingEl = _operatorCommitmentEl('operatorCommitmentMissing');
  if (missingEl) missingEl.textContent = 'Fill owner, deadline or review date, and acceptance criteria, then save locally. No execution is attached.';
  form.hidden = false;
  popover.hidden = false;
}

function cancelOperatorCommitmentDraft(){
  const form = _operatorCommitmentEl('operatorCommitmentForm');
  if (form) form.hidden = true;
  _operatorCommitmentDraftSource = null;
  _operatorCommitmentDraftEvidence = [];
  const missingEl = _operatorCommitmentEl('operatorCommitmentMissing');
  if (missingEl) missingEl.textContent = '';
}

async function submitOperatorCommitmentForm(event){
  if (event && typeof event.preventDefault === 'function') event.preventDefault();
  const payload = _operatorCommitmentValidateForm();
  if (!payload) return null;
  if (typeof api !== 'function') {
    const missingEl = _operatorCommitmentEl('operatorCommitmentMissing');
    if (missingEl) missingEl.textContent = 'Cannot save: API helper unavailable.';
    return null;
  }
  try {
    const result = await api('/api/operator/commitments/promote',{method:'POST',body:JSON.stringify(payload)});
    if (!result || result.ok !== true) {
      const missingEl = _operatorCommitmentEl('operatorCommitmentMissing');
      const missing = result && Array.isArray(result.missing) ? result.missing.join(', ') : 'validation failed';
      if (missingEl) missingEl.textContent = 'Not saved: ' + missing;
      return result || null;
    }
    cancelOperatorCommitmentDraft();
    _operatorCommitmentLastFetchKey = '';
    await refreshOperatorCommitments({force:true});
    if (typeof showToast === 'function') showToast('Saved local commitment card.', 2400);
    return result;
  } catch(err) {
    const missingEl = _operatorCommitmentEl('operatorCommitmentMissing');
    if (missingEl) missingEl.textContent = 'Not saved: ' + ((err && err.message) || 'request failed');
    return null;
  }
}

function initOperatorCommitments(){
  const chip = _operatorCommitmentEl('operatorCommitmentChip');
  const label = _operatorCommitmentEl('operatorCommitmentLabel');
  if (chip) {
    chip.hidden = false;
    chip.title = 'Local commitment cards · not checked yet';
  }
  if (label) label.textContent = 'Commitments';
}

window.toggleOperatorCommitments = toggleOperatorCommitments;
window.refreshOperatorCommitments = refreshOperatorCommitments;
window.renderOperatorCommitments = renderOperatorCommitments;
window.hideOperatorCommitments = hideOperatorCommitments;
window.openOperatorCommitmentPromote = openOperatorCommitmentPromote;
window.openOperatorCommitmentPromoteFromRecall = openOperatorCommitmentPromoteFromRecall;
window.cancelOperatorCommitmentDraft = cancelOperatorCommitmentDraft;
window.submitOperatorCommitmentForm = submitOperatorCommitmentForm;
window.operatorCommitmentContextKey = operatorCommitmentContextKey;

initOperatorCommitments();
