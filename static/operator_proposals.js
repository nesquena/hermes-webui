let _operatorProposalLastFetchKey = '';
let _operatorProposalLastPayload = null;
let _operatorProposalInFlight = null;
let _operatorProposalInFlightKey = '';
let _operatorProposalRequestSeq = 0;
const _operatorProposalHiddenIds = new Set();

function _operatorProposalEl(id){
  if (typeof $ === 'function') return $(id);
  return document.getElementById(id);
}

function operatorProposalContextKey(){
  if (typeof operatorTruthContextKey === 'function') return operatorTruthContextKey();
  const session = (S && S.session) || {};
  return JSON.stringify({
    session_id: (session.session_id || session.id || ''),
    ui_board: (typeof operatorTruthBoardHint === 'function') ? operatorTruthBoardHint() : '',
    profile: (S && S.activeProfile) || '',
    workspace: session.workspace || '',
  });
}

function _operatorProposalStateClass(state){
  if (state === 'live') return 'state-live';
  if (state === 'stale') return 'state-stale';
  return 'state-unknown';
}

function _operatorProposalApplyChip(payload){
  const chip = _operatorProposalEl('operatorProposalChip');
  const label = _operatorProposalEl('operatorProposalLabel');
  if (!chip) return;
  const status = (payload && payload.status) || 'unknown';
  chip.hidden = false;
  chip.classList.remove('state-live','state-stale','state-unknown');
  chip.classList.add(_operatorProposalStateClass(status));
  const count = payload && Array.isArray(payload.proposals) ? payload.proposals.length : 0;
  if (label) label.textContent = count ? 'Proposals ' + count : 'Proposals';
  chip.title = (payload && payload.summary) || 'Manual next-action proposals';
}

function _operatorProposalText(value, fallback){
  const text = String(value || '').trim();
  return text || fallback || '';
}

function _operatorProposalButton(text, className, onClick){
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = className;
  btn.textContent = text;
  btn.onclick = onClick;
  return btn;
}

function _operatorProposalRow(label, value){
  const row = document.createElement('div');
  row.className = 'operator-proposal-row';
  const key = document.createElement('span');
  key.className = 'operator-proposal-row-key';
  key.textContent = label;
  const val = document.createElement('span');
  val.className = 'operator-proposal-row-value';
  val.textContent = value;
  row.append(key, val);
  return row;
}

function _operatorProposalEvidenceText(ev){
  const parts = [];
  if (ev && ev.source_id) parts.push(String(ev.source_id));
  if (ev && ev.status) parts.push(String(ev.status));
  if (ev && ev.field) parts.push(String(ev.field));
  if (ev && ev.path) parts.push(String(ev.path));
  if (ev && ev.api) parts.push(String(ev.api));
  return parts.join(' · ');
}

function _operatorProposalCard(proposal){
  const card = document.createElement('div');
  card.className = 'operator-proposal-card';
  card.dataset.proposalId = proposal.id || '';

  const top = document.createElement('div');
  top.className = 'operator-proposal-card-top';
  const title = document.createElement('div');
  title.className = 'operator-proposal-card-title';
  title.textContent = '#' + _operatorProposalText(proposal.rank, '?') + ' · ' + _operatorProposalText(proposal.title, 'Untitled proposal');
  const badge = document.createElement('span');
  badge.className = 'operator-proposal-no-exec';
  badge.textContent = 'would_execute:false';
  top.append(title, badge);
  card.append(top);

  const summary = document.createElement('div');
  summary.className = 'operator-proposal-summary';
  summary.textContent = _operatorProposalText(proposal.summary, 'No summary available.');
  card.append(summary);

  card.append(_operatorProposalRow('Side effect', _operatorProposalText(proposal.side_effect_level, 'manual draft only')));
  card.append(_operatorProposalRow('Owner', _operatorProposalText(proposal.owner, 'future Hermes session')));

  const evidence = document.createElement('div');
  evidence.className = 'operator-proposal-evidence';
  const evidenceTitle = document.createElement('div');
  evidenceTitle.className = 'operator-proposal-section-title';
  evidenceTitle.textContent = 'Evidence';
  evidence.append(evidenceTitle);
  const evs = Array.isArray(proposal.evidence) ? proposal.evidence.slice(0, 5) : [];
  if (!evs.length) {
    const empty = document.createElement('div');
    empty.className = 'operator-proposal-muted';
    empty.textContent = 'No evidence listed.';
    evidence.append(empty);
  } else {
    evs.forEach(ev => {
      const item = document.createElement('div');
      item.className = 'operator-proposal-evidence-item';
      item.textContent = _operatorProposalEvidenceText(ev);
      evidence.append(item);
    });
  }
  card.append(evidence);

  const notes = Array.isArray(proposal.safety_notes) ? proposal.safety_notes.slice(0, 4) : [];
  if (notes.length) {
    const safety = document.createElement('div');
    safety.className = 'operator-proposal-safety';
    const safetyTitle = document.createElement('div');
    safetyTitle.className = 'operator-proposal-section-title';
    safetyTitle.textContent = 'Safety';
    safety.append(safetyTitle);
    notes.forEach(note => {
      const item = document.createElement('div');
      item.className = 'operator-proposal-muted';
      item.textContent = String(note || '');
      safety.append(item);
    });
    card.append(safety);
  }

  const actions = document.createElement('div');
  actions.className = 'operator-proposal-actions';
  actions.append(
    _operatorProposalButton('Draft handoff', 'operator-proposal-action primary', () => draftOperatorProposal(proposal.id)),
    _operatorProposalButton('Promote to commitment', 'operator-proposal-action', () => {
      if (typeof openOperatorCommitmentPromote === 'function') openOperatorCommitmentPromote(proposal);
    }),
    _operatorProposalButton('Dismiss', 'operator-proposal-action', () => dismissOperatorProposal(proposal.id)),
  );
  card.append(actions);
  return card;
}

function renderOperatorProposals(payload){
  _operatorProposalLastPayload = payload || null;
  _operatorProposalApplyChip(payload);
  const popover = _operatorProposalEl('operatorProposalPopover');
  const subtitle = _operatorProposalEl('operatorProposalSubtitle');
  const list = _operatorProposalEl('operatorProposalList');
  if (!popover || !list) return;
  while (list.firstChild) list.removeChild(list.firstChild);

  const status = (payload && payload.status) || 'unknown';
  const proposals = ((payload && Array.isArray(payload.proposals)) ? payload.proposals : [])
    .filter(item => item && !_operatorProposalHiddenIds.has(item.id));
  if (subtitle) subtitle.textContent = (payload && payload.summary) || 'Not checked yet';
  popover.classList.remove('state-live','state-stale','state-unknown');
  popover.classList.add(_operatorProposalStateClass(status));

  if (!proposals.length) {
    const empty = document.createElement('div');
    empty.className = 'operator-proposal-empty';
    empty.textContent = status === 'unknown' ? 'No safe proposals — source unavailable.' : 'No proposals to show.';
    list.append(empty);
    const issues = payload && Array.isArray(payload.issues) ? payload.issues.slice(0, 4) : [];
    issues.forEach(issue => {
      const item = document.createElement('div');
      item.className = 'operator-proposal-issue';
      item.textContent = String(issue || '');
      list.append(item);
    });
    return;
  }

  proposals.forEach(proposal => list.append(_operatorProposalCard(proposal)));
}

function hideOperatorProposals(){
  const popover = _operatorProposalEl('operatorProposalPopover');
  if (popover) popover.hidden = true;
}

function toggleOperatorProposals(opts = {}){
  const popover = _operatorProposalEl('operatorProposalPopover');
  if (!popover) return;
  const opening = popover.hidden;
  if (!opening && !opts.force) {
    hideOperatorProposals();
    return;
  }
  popover.hidden = false;
  refreshOperatorProposals({force: Boolean(opts.force)});
}

async function refreshOperatorProposals(opts = {}){
  const force = Boolean(opts.force);
  const contextKey = operatorProposalContextKey();
  if (!force && _operatorProposalInFlight && contextKey === _operatorProposalInFlightKey) return _operatorProposalInFlight;
  if (!force && _operatorProposalLastPayload && contextKey === _operatorProposalLastFetchKey) {
    renderOperatorProposals(_operatorProposalLastPayload);
    return _operatorProposalLastPayload;
  }
  if (typeof api !== 'function') {
    const payload = {status:'unknown', summary:'No safe proposals — API helper unavailable', proposals:[], issues:['api helper unavailable']};
    renderOperatorProposals(payload);
    return payload;
  }

  const params = new URLSearchParams();
  const sid = (typeof operatorTruthSessionId === 'function') ? operatorTruthSessionId() : ((S && S.session && (S.session.session_id || S.session.id)) || '');
  const board = (typeof operatorTruthBoardHint === 'function') ? operatorTruthBoardHint() : '';
  if (sid) params.set('session_id', sid);
  if (board) params.set('ui_board', board);
  const base = '/api/operator/proposals';
  const path = base + (params.toString() ? '?' + params.toString() : '');
  const requestKey = contextKey;
  const requestSeq = ++_operatorProposalRequestSeq;
  _operatorProposalInFlightKey = requestKey;

  _operatorProposalInFlight = api(path)
    .then(payload => {
      if (requestKey !== operatorProposalContextKey() || requestSeq !== _operatorProposalRequestSeq) return payload;
      _operatorProposalLastFetchKey = requestKey;
      _operatorProposalHiddenIds.clear();
      renderOperatorProposals(payload);
      return payload;
    })
    .catch(err => {
      if (requestKey === operatorProposalContextKey() && requestSeq === _operatorProposalRequestSeq) {
        const payload = {status:'unknown', summary:'No safe proposals — source unavailable', proposals:[], issues:[(err && err.message) || 'request failed']};
        _operatorProposalLastFetchKey = requestKey;
        renderOperatorProposals(payload);
      }
      return null;
    })
    .finally(() => {
      if (requestKey === _operatorProposalInFlightKey && requestSeq === _operatorProposalRequestSeq) {
        _operatorProposalInFlight = null;
        _operatorProposalInFlightKey = '';
      }
    });
  return _operatorProposalInFlight;
}

function _operatorProposalFind(proposalId){
  const proposals = (_operatorProposalLastPayload && Array.isArray(_operatorProposalLastPayload.proposals)) ? _operatorProposalLastPayload.proposals : [];
  return proposals.find(item => item && item.id === proposalId) || null;
}

function draftOperatorProposal(proposalId){
  const proposal = _operatorProposalFind(proposalId);
  if (!proposal) return;
  const msg = _operatorProposalEl('msg');
  if (!msg) return;
  msg.value = proposal.handoff_prompt || ('Review proposal: ' + _operatorProposalText(proposal.title, proposal.id || 'operator proposal'));
  if (typeof autoResize === 'function') autoResize();
  if (typeof updateSendBtn === 'function') updateSendBtn();
  msg.focus();
  if (typeof showToast === 'function') showToast('Drafted handoff in composer. Review before running.', 2400);
}

function dismissOperatorProposal(proposalId){
  if (proposalId) _operatorProposalHiddenIds.add(proposalId);
  renderOperatorProposals(_operatorProposalLastPayload || {status:'unknown', proposals:[], issues:[]});
}

function initOperatorProposals(){
  const chip = _operatorProposalEl('operatorProposalChip');
  const label = _operatorProposalEl('operatorProposalLabel');
  if (chip) {
    chip.hidden = false;
    chip.title = 'Manual next-action proposals · not checked yet';
  }
  if (label) label.textContent = 'Proposals';
}

window.toggleOperatorProposals = toggleOperatorProposals;
window.refreshOperatorProposals = refreshOperatorProposals;
window.renderOperatorProposals = renderOperatorProposals;
window.hideOperatorProposals = hideOperatorProposals;
window.draftOperatorProposal = draftOperatorProposal;
window.dismissOperatorProposal = dismissOperatorProposal;
window.operatorProposalContextKey = operatorProposalContextKey;

initOperatorProposals();
