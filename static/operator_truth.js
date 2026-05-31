let _operatorTruthLastFetchAt = 0;
let _operatorTruthLastFetchKey = '';
let _operatorTruthInFlight = null;
let _operatorTruthInFlightKey = '';
let _operatorTruthRequestSeq = 0;
const OPERATOR_TRUTH_TTL_MS = 30000;

function operatorTruthSessionId(){
  return (S && S.session && (S.session.session_id || S.session.id)) || '';
}

function operatorTruthBoardHint(){
  try {
    if (typeof _kanbanCurrentBoard !== 'undefined' && _kanbanCurrentBoard) return _kanbanCurrentBoard;
  } catch(_) {}
  return '';
}

function operatorTruthContextKey(){
  const session = (S && S.session) || {};
  return JSON.stringify({
    session_id: operatorTruthSessionId(),
    ui_board: operatorTruthBoardHint(),
    profile: (S && S.activeProfile) || '',
    workspace: session.workspace || '',
    profile_default_workspace: (S && S._profileDefaultWorkspace) || '',
    profile_switch_workspace: (S && S._profileSwitchWorkspace) || '',
  });
}

function _operatorTruthChip(payload, id){
  const chips = (payload && Array.isArray(payload.chips)) ? payload.chips : [];
  return chips.find(chip => chip && chip.id === id) || null;
}

function _operatorTruthStateClass(state){
  if (state === 'live') return 'state-live';
  if (state === 'stale') return 'state-stale';
  return 'state-unknown';
}

function _operatorTruthApplyState(el, state){
  if (!el) return;
  el.classList.remove('state-live','state-stale','state-unknown');
  el.classList.add(_operatorTruthStateClass(state));
}

function _operatorTruthVerifiedText(ts){
  if (!ts) return 'Verified unknown';
  try {
    return 'Verified ' + new Date(Number(ts) * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
  } catch(_) {
    return 'Verified unknown';
  }
}

function _operatorTruthIssues(chip){
  if (!chip || !Array.isArray(chip.issues) || !chip.issues.length) return '';
  return chip.issues.filter(Boolean).slice(0, 2).join(' · ');
}

function _operatorTruthSourceText(chip){
  const source = (chip && chip.source) || {};
  const bits = [];
  if (source.kind) bits.push('Source: ' + source.kind);
  if (source.path) bits.push(source.path);
  if (source.metadata_path) bits.push(source.metadata_path);
  return bits.join(' · ');
}

function _operatorTruthTitle(payload, chip, fallback){
  const bits = [];
  if (payload && payload.summary) bits.push(payload.summary);
  else bits.push(fallback || 'Truth unknown');
  bits.push(_operatorTruthVerifiedText((payload && payload.verified_at) || (chip && chip.checked_at)));
  const issueText = _operatorTruthIssues(chip);
  if (issueText) bits.push(issueText);
  const sourceText = _operatorTruthSourceText(chip);
  if (sourceText) bits.push(sourceText);
  return bits.join(' · ');
}

function _operatorTruthSetButton(buttonId, labelId, text, state, title, hidden){
  const button = $(buttonId);
  const label = $(labelId);
  if (!button) return;
  button.hidden = Boolean(hidden);
  _operatorTruthApplyState(button, state || 'unknown');
  button.title = title || text || 'Operator truth status unknown';
  if (label) label.textContent = text || 'Unknown';
}

function renderOperatorTruth(payload){
  const status = (payload && payload.status) || 'unknown';
  const summary = (payload && payload.summary) || 'Truth unknown';
  const board = _operatorTruthChip(payload, 'kanban_board');
  const scratch = _operatorTruthChip(payload, 'scratch_safety');
  const summaryTitle = _operatorTruthTitle(payload, board || scratch, summary);

  _operatorTruthSetButton('operatorTruthSummaryChip', 'operatorTruthSummaryLabel', summary, status, summaryTitle, false);

  if (board) {
    const boardState = board.state || 'unknown';
    const boardValue = board.value || 'unknown';
    _operatorTruthSetButton(
      'operatorTruthBoardChip',
      'operatorTruthBoardLabel',
      boardState === 'live' ? 'Board ' + boardValue : 'Board ' + boardState,
      boardState,
      _operatorTruthTitle(payload, board, 'Board unknown'),
      false,
    );
  } else {
    _operatorTruthSetButton('operatorTruthBoardChip', 'operatorTruthBoardLabel', 'Board unknown', 'unknown', 'Board unknown', true);
  }

  if (scratch) {
    const scratchState = scratch.state || 'unknown';
    const scratchText = scratchState === 'live' ? 'Scratch safe' : (scratchState === 'stale' ? 'Scratch risky' : 'Scratch unknown');
    _operatorTruthSetButton(
      'operatorTruthScratchChip',
      'operatorTruthScratchLabel',
      scratchText,
      scratchState,
      _operatorTruthTitle(payload, scratch, scratchText),
      false,
    );
  } else {
    _operatorTruthSetButton('operatorTruthScratchChip', 'operatorTruthScratchLabel', 'Scratch unknown', 'unknown', 'Scratch unknown', true);
  }
}

function setOperatorTruthUnknown(reason){
  const detail = reason ? String(reason) : 'truth source unavailable';
  _operatorTruthSetButton('operatorTruthSummaryChip', 'operatorTruthSummaryLabel', 'Truth unknown', 'unknown', 'Truth unknown · ' + detail, false);
  _operatorTruthSetButton('operatorTruthBoardChip', 'operatorTruthBoardLabel', 'Board unknown', 'unknown', 'Board unknown · ' + detail, true);
  _operatorTruthSetButton('operatorTruthScratchChip', 'operatorTruthScratchLabel', 'Scratch unknown', 'unknown', 'Scratch unknown · ' + detail, true);
}

async function refreshOperatorTruth(opts = {}){
  const force = Boolean(opts.force);
  const now = Date.now();
  const contextKey = operatorTruthContextKey();
  if (!force && _operatorTruthInFlight && contextKey === _operatorTruthInFlightKey) return _operatorTruthInFlight;
  if (!force && _operatorTruthLastFetchAt && contextKey === _operatorTruthLastFetchKey && (now - _operatorTruthLastFetchAt) < OPERATOR_TRUTH_TTL_MS) return null;
  if (typeof api !== 'function') {
    setOperatorTruthUnknown('api helper unavailable');
    return null;
  }

  const params = new URLSearchParams();
  const sid = operatorTruthSessionId();
  const board = operatorTruthBoardHint();
  if (sid) params.set('session_id', sid);
  if (board) params.set('ui_board', board);
  const path = '/api/operator/truth' + (params.toString() ? '?' + params.toString() : '');
  const requestKey = contextKey;
  const requestSeq = ++_operatorTruthRequestSeq;
  _operatorTruthInFlightKey = requestKey;

  _operatorTruthInFlight = api(path)
    .then(payload => {
      if (requestKey !== operatorTruthContextKey() || requestSeq !== _operatorTruthRequestSeq) return payload;
      _operatorTruthLastFetchAt = Date.now();
      _operatorTruthLastFetchKey = requestKey;
      renderOperatorTruth(payload);
      return payload;
    })
    .catch(err => {
      if (requestKey === operatorTruthContextKey() && requestSeq === _operatorTruthRequestSeq) {
        _operatorTruthLastFetchAt = Date.now();
        _operatorTruthLastFetchKey = requestKey;
        setOperatorTruthUnknown((err && err.message) || 'fetch failed');
      }
      return null;
    })
    .finally(() => {
      if (requestKey === _operatorTruthInFlightKey && requestSeq === _operatorTruthRequestSeq) {
        _operatorTruthInFlight = null;
        _operatorTruthInFlightKey = '';
      }
    });
  return _operatorTruthInFlight;
}

function initOperatorTruthStrip(){
  setOperatorTruthUnknown('not checked yet');
  refreshOperatorTruth({force:true, reason:'boot'});
}

window.refreshOperatorTruth = refreshOperatorTruth;
window.renderOperatorTruth = renderOperatorTruth;
window.setOperatorTruthUnknown = setOperatorTruthUnknown;
window.operatorTruthSessionId = operatorTruthSessionId;
window.operatorTruthBoardHint = operatorTruthBoardHint;
window.operatorTruthContextKey = operatorTruthContextKey;

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initOperatorTruthStrip, {once:true});
} else {
  initOperatorTruthStrip();
}
