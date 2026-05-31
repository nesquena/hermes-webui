let _operatorMemorySkillReviewLastPayload = null;
let _operatorMemorySkillReviewInFlight = null;
let _operatorMemorySkillReviewRequestSeq = 0;

function _operatorMemorySkillReviewEl(id){
  if (typeof $ === 'function') return $(id);
  return document.getElementById(id);
}

function _operatorMemorySkillReviewText(value, fallback){
  const text = String(value == null ? '' : value).trim();
  return text || fallback || '';
}

function _operatorMemorySkillReviewStateClass(state){
  const value = _operatorMemorySkillReviewText(state, 'unknown').toLowerCase();
  if (value === 'live') return 'state-live';
  if (value === 'stale') return 'state-stale';
  return 'state-unknown';
}

function _operatorMemorySkillReviewClear(el){
  if (!el) return;
  while (el.firstChild) el.removeChild(el.firstChild);
}

function _operatorMemorySkillReviewJoin(parts, fallback){
  const values = Array.isArray(parts) ? parts.map(part => _operatorMemorySkillReviewText(part, '')).filter(Boolean) : [];
  return values.join(' · ') || fallback || 'unknown';
}

function _operatorMemorySkillReviewApplyChip(payload){
  const chip = _operatorMemorySkillReviewEl('operatorMemorySkillReviewChip');
  const label = _operatorMemorySkillReviewEl('operatorMemorySkillReviewLabel');
  if (!chip) return;
  const status = (payload && payload.status) || 'unknown';
  const items = payload && Array.isArray(payload.items) ? payload.items : [];
  chip.hidden = false;
  chip.classList.remove('state-live','state-stale','state-unknown');
  chip.classList.add(_operatorMemorySkillReviewStateClass(status));
  if (label) label.textContent = items.length ? 'Memory Review ' + items.length : 'Memory Review';
  chip.title = (payload && payload.summary) || 'Memory and skill review queue';
}

function _operatorMemorySkillReviewRow(label, value, extraClass){
  const row = document.createElement('div');
  row.className = 'operator-memory-skill-review-row' + (extraClass ? ' ' + extraClass : '');
  const key = document.createElement('span');
  key.className = 'operator-memory-skill-review-row-key';
  key.textContent = label;
  const val = document.createElement('span');
  val.className = 'operator-memory-skill-review-row-value';
  val.textContent = _operatorMemorySkillReviewText(value, 'unknown');
  row.append(key, val);
  return row;
}

function _operatorMemorySkillReviewSection(title, className){
  const section = document.createElement('div');
  section.className = 'operator-memory-skill-review-section ' + className;
  const heading = document.createElement('div');
  heading.className = 'operator-memory-skill-review-section-title';
  heading.textContent = title;
  section.append(heading);
  return section;
}

function _operatorMemorySkillReviewButton(text, className, onClick){
  const button = document.createElement('button');
  button.type = 'button';
  button.className = className;
  button.textContent = text;
  button.onclick = onClick;
  return button;
}

function _operatorMemorySkillReviewTargetText(item){
  const target = (item && item.target) || {};
  return _operatorMemorySkillReviewJoin([
    target.kind,
    target.section || target.name,
    target.file_path || target.path,
  ], 'unknown target');
}

function _operatorMemorySkillReviewEvidenceText(evidence){
  return _operatorMemorySkillReviewJoin([
    evidence && evidence.kind,
    evidence && evidence.session_id,
    evidence && evidence.message_index,
    evidence && evidence.content_hash,
    evidence && evidence.quote,
  ], 'evidence unavailable');
}

function _operatorMemorySkillReviewNoteText(note){
  if (!note) return 'unknown issue';
  const missing = Array.isArray(note.missing) ? note.missing.join(', ') : '';
  const issues = Array.isArray(note.issues) ? note.issues.join(', ') : '';
  return _operatorMemorySkillReviewJoin([note.classification, note.reason, missing, issues], 'unknown issue');
}

function _operatorMemorySkillReviewIssue(text){
  const item = document.createElement('div');
  item.className = 'operator-memory-skill-review-invalid';
  item.textContent = _operatorMemorySkillReviewText(text, 'unknown issue');
  return item;
}

function _operatorMemorySkillReviewCard(item){
  item = item || {};
  const proposed_change = (item && item.proposed_change) || {};
  const source_evidence = item && Array.isArray(item.source_evidence) ? item.source_evidence : [];
  const classification = (item && item.classification) || {};
  const stale_risk = (item && item.stale_risk) || {};
  const decision = (item && item.decision) || {};
  const rollback = (item && item.rollback) || {};
  const issues = item && Array.isArray(item.issues) ? item.issues : [];
  const decisionState = _operatorMemorySkillReviewText(decision.state, 'unknown').toLowerCase();
  const staleState = _operatorMemorySkillReviewText(stale_risk.state, 'unknown').toLowerCase();

  const card = document.createElement('div');
  card.className = 'operator-memory-skill-review-card';
  if (decisionState === 'invalid' || staleState === 'expired' || item.would_execute !== false) {
    card.classList.add('operator-memory-skill-review-invalid');
  }
  if (staleState === 'review_required' || staleState === 'expired') card.classList.add('operator-memory-skill-review-stale');
  card.dataset.reviewId = item.id || '';

  const top = document.createElement('div');
  top.className = 'operator-memory-skill-review-card-top';
  const title = document.createElement('div');
  title.className = 'operator-memory-skill-review-card-title';
  title.textContent = _operatorMemorySkillReviewTargetText(item);
  const noExec = document.createElement('span');
  noExec.className = 'operator-memory-skill-review-no-exec';
  noExec.textContent = item.would_execute === false ? 'would_execute:false' : 'would_execute not false';
  top.append(title, noExec);
  card.append(top);

  const summary = document.createElement('div');
  summary.className = 'operator-memory-skill-review-summary';
  summary.textContent = _operatorMemorySkillReviewText(proposed_change.summary, 'No proposed_change summary.');
  card.append(summary);

  card.append(_operatorMemorySkillReviewRow('Operation', proposed_change.operation));
  card.append(_operatorMemorySkillReviewRow('Durability', classification.durability));
  card.append(_operatorMemorySkillReviewRow('Stale risk', staleState));
  card.append(_operatorMemorySkillReviewRow('Expires', stale_risk.expires_at));
  card.append(_operatorMemorySkillReviewRow('Decision', decisionState, 'operator-memory-skill-review-decision-row'));
  card.append(_operatorMemorySkillReviewRow('Previous content', item.previous_content));

  const diff = document.createElement('pre');
  diff.className = 'operator-memory-skill-review-diff';
  diff.textContent = _operatorMemorySkillReviewText(proposed_change.diff || proposed_change.proposed_content, 'No diff available.');
  card.append(diff);

  const evidenceSection = _operatorMemorySkillReviewSection('Source evidence', 'operator-memory-skill-review-evidence');
  if (!source_evidence.length) {
    evidenceSection.append(_operatorMemorySkillReviewIssue('source_evidence missing'));
  } else {
    source_evidence.slice(0, 5).forEach(evidence => {
      const row = document.createElement('div');
      row.className = 'operator-memory-skill-review-evidence-item';
      row.textContent = _operatorMemorySkillReviewEvidenceText(evidence);
      evidenceSection.append(row);
    });
  }
  card.append(evidenceSection);

  const detailSection = _operatorMemorySkillReviewSection('Classification and rollback', 'operator-memory-skill-review-detail');
  detailSection.append(_operatorMemorySkillReviewRow('Reason', classification.reason));
  detailSection.append(_operatorMemorySkillReviewRow('Risk', classification.transient_risk));
  detailSection.append(_operatorMemorySkillReviewRow('Rollback', rollback.previous_hash));
  detailSection.append(_operatorMemorySkillReviewRow('Rollback excerpt', rollback.previous_excerpt));
  card.append(detailSection);

  issues.slice(0, 5).forEach(issue => card.append(_operatorMemorySkillReviewIssue(issue)));

  const actions = document.createElement('div');
  actions.className = 'operator-memory-skill-review-decision';
  actions.append(
    _operatorMemorySkillReviewButton('Approve', 'operator-memory-skill-review-action primary', () => submitOperatorMemorySkillReviewDecision(item.id, 'approved')),
    _operatorMemorySkillReviewButton('Deny', 'operator-memory-skill-review-action', () => submitOperatorMemorySkillReviewDecision(item.id, 'denied')),
  );
  card.append(actions);
  return card;
}

function renderOperatorMemorySkillReview(payload){
  _operatorMemorySkillReviewLastPayload = payload || null;
  _operatorMemorySkillReviewApplyChip(payload);
  const popover = _operatorMemorySkillReviewEl('operatorMemorySkillReviewPopover');
  const subtitle = _operatorMemorySkillReviewEl('operatorMemorySkillReviewSubtitle');
  const list = _operatorMemorySkillReviewEl('operatorMemorySkillReviewList');
  if (!popover || !list) return;
  _operatorMemorySkillReviewClear(list);

  const status = (payload && payload.status) || 'unknown';
  const items = payload && Array.isArray(payload.items) ? payload.items : [];
  popover.classList.remove('state-live','state-stale','state-unknown');
  popover.classList.add(_operatorMemorySkillReviewStateClass(status));
  if (subtitle) {
    const summary = (payload && payload.summary) || 'Not checked yet';
    subtitle.textContent = summary + ' · local review only · no apply';
  }

  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'operator-memory-skill-review-empty';
    empty.textContent = status === 'unknown' ? 'No memory or skill review items — source unavailable.' : 'No memory or skill review items.';
    list.append(empty);
  } else {
    items.forEach(item => list.append(_operatorMemorySkillReviewCard(item)));
  }

  const notes = payload && Array.isArray(payload.notes) ? payload.notes : [];
  notes.slice(0, 5).forEach(note => list.append(_operatorMemorySkillReviewIssue(_operatorMemorySkillReviewNoteText(note))));
  const issues = payload && Array.isArray(payload.issues) ? payload.issues : [];
  issues.slice(0, 5).forEach(issue => list.append(_operatorMemorySkillReviewIssue(issue)));
}

function hideOperatorMemorySkillReview(){
  const popover = _operatorMemorySkillReviewEl('operatorMemorySkillReviewPopover');
  if (popover) popover.hidden = true;
}

function toggleOperatorMemorySkillReview(opts = {}){
  const popover = _operatorMemorySkillReviewEl('operatorMemorySkillReviewPopover');
  if (!popover) return;
  const opening = popover.hidden;
  if (!opening && !opts.force) {
    hideOperatorMemorySkillReview();
    return;
  }
  popover.hidden = false;
  refreshOperatorMemorySkillReview({force: Boolean(opts.force)});
}

async function refreshOperatorMemorySkillReview(opts = {}){
  const force = Boolean(opts.force);
  if (!force && _operatorMemorySkillReviewInFlight) return _operatorMemorySkillReviewInFlight;
  if (!force && _operatorMemorySkillReviewLastPayload) {
    renderOperatorMemorySkillReview(_operatorMemorySkillReviewLastPayload);
    return _operatorMemorySkillReviewLastPayload;
  }
  if (typeof api !== 'function') {
    const payload = {status:'unknown', summary:'Memory and skill review unavailable — API helper missing', items:[], notes:[], issues:['api helper unavailable'], would_execute:false};
    renderOperatorMemorySkillReview(payload);
    return payload;
  }

  const requestSeq = ++_operatorMemorySkillReviewRequestSeq;
  _operatorMemorySkillReviewInFlight = api('/api/operator/memory-skill-review')
    .then(payload => {
      if (requestSeq !== _operatorMemorySkillReviewRequestSeq) return payload;
      renderOperatorMemorySkillReview(payload);
      return payload;
    })
    .catch(err => {
      if (requestSeq === _operatorMemorySkillReviewRequestSeq) {
        const payload = {status:'unknown', summary:'Memory and skill review unavailable — source read failed', items:[], notes:[], issues:[(err && err.message) || 'request failed'], would_execute:false};
        renderOperatorMemorySkillReview(payload);
      }
      return null;
    })
    .finally(() => {
      if (requestSeq === _operatorMemorySkillReviewRequestSeq) _operatorMemorySkillReviewInFlight = null;
    });
  return _operatorMemorySkillReviewInFlight;
}

async function submitOperatorMemorySkillReviewDecision(itemId, decision){
  if (!itemId || typeof api !== 'function') return null;
  const request = {
    id: itemId,
    decision: decision,
    reason: decision === 'approved' ? 'Approved from operator memory/skill review.' : 'Denied from operator memory/skill review.',
  };
  try {
    const result = await api('/api/operator/memory-skill-review/decision',{method:'POST',body:JSON.stringify(request)});
    if (!result || result.ok !== true) {
      const message = (result && (result.error || result.message)) || 'decision failed';
      const list = _operatorMemorySkillReviewEl('operatorMemorySkillReviewList');
      if (list) list.append(_operatorMemorySkillReviewIssue(message));
      return result || null;
    }
    _operatorMemorySkillReviewLastPayload = null;
    await refreshOperatorMemorySkillReview({force:true});
    if (typeof showToast === 'function') showToast('Memory/skill review decision recorded.', 2200);
    return result;
  } catch(err) {
    const list = _operatorMemorySkillReviewEl('operatorMemorySkillReviewList');
    if (list) list.append(_operatorMemorySkillReviewIssue((err && err.message) || 'decision failed'));
    return null;
  }
}

function initOperatorMemorySkillReview(){
  const chip = _operatorMemorySkillReviewEl('operatorMemorySkillReviewChip');
  const label = _operatorMemorySkillReviewEl('operatorMemorySkillReviewLabel');
  if (chip) {
    chip.hidden = false;
    chip.title = 'Memory and skill review queue · not checked yet';
  }
  if (label) label.textContent = 'Memory Review';
}

window.toggleOperatorMemorySkillReview = toggleOperatorMemorySkillReview;
window.refreshOperatorMemorySkillReview = refreshOperatorMemorySkillReview;
window.renderOperatorMemorySkillReview = renderOperatorMemorySkillReview;
window.hideOperatorMemorySkillReview = hideOperatorMemorySkillReview;
window.submitOperatorMemorySkillReviewDecision = submitOperatorMemorySkillReviewDecision;

initOperatorMemorySkillReview();
