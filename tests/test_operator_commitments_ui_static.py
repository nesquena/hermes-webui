import re
import subprocess
from pathlib import Path


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def _js_function_body(js, function_name):
    match = re.search(rf"function\s+{re.escape(function_name)}\s*\(", js)
    assert match, f"missing function {function_name}"
    brace = js.find("{", match.end())
    assert brace >= 0, f"missing function body for {function_name}"
    depth = 0
    for index in range(brace, len(js)):
        char = js[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return js[brace + 1 : index]
    raise AssertionError(f"unterminated function body for {function_name}")


def test_operator_commitment_markup_exists_near_proposal_chip_and_popover():
    html = _read("static/index.html")

    assert 'id="operatorCommitmentChip"' in html
    assert 'id="operatorCommitmentPopover"' in html
    assert 'id="operatorCommitmentList"' in html
    assert 'id="operatorCommitmentForm"' in html
    assert html.index('id="operatorProposalChip"') < html.index('id="operatorCommitmentChip"') < html.index('id="composerMobileConfigBtn"')
    assert html.index('id="operatorProposalPopover"') < html.index('id="operatorCommitmentPopover"')


def test_operator_commitments_script_loaded_after_kanban_before_boot():
    html = _read("static/index.html")

    assert html.index("static/operator_kanban.js") < html.index("static/operator_commitments.js") < html.index("static/boot.js")


def test_operator_commitments_js_uses_operator_commitments_endpoints_only():
    js = _read("static/operator_commitments.js")
    compact = js.replace(" ", "")

    assert "'/api/operator/commitments'" in js or '"/api/operator/commitments"' in js
    assert "'/api/operator/commitments/promote'" in js or '"/api/operator/commitments/promote"' in js
    assert "http://" not in js
    assert "https://" not in js
    assert "fetch(" not in js
    assert "api(" in js
    assert "method:'POST'" in compact or 'method:"POST"' in compact
    assert "/api/operator/commitments/promote" in js


def test_operator_commitments_js_no_kanban_chat_cron_goal_dispatch_or_background_tokens():
    js = _read("static/operator_commitments.js")
    forbidden = [
        "/api/kanban/",
        "/api/kanban/dispatch",
        "/api/kanban/tasks",
        "/api/chat/start",
        "/api/chat",
        "/api/cron",
        "/api/crons",
        "/api/goal",
        "runKanbanDispatcher",
        "nudgeKanbanDispatcher",
        "createKanbanTask",
        "updateKanbanTask",
        "addKanbanComment",
        "setInterval",
        "setTimeout",
        "EventSource",
        "WebSocket",
        "send(",
        "sendMessage",
    ]
    for token in forbidden:
        assert token not in js


def test_operator_commitments_values_use_text_content_not_inner_html():
    js = _read("static/operator_commitments.js")

    assert ".textContent" in js
    assert "document.createElement" in js
    assert ".innerHTML" not in js
    assert "payload.innerHTML" not in js
    assert "commitment.innerHTML" not in js


def test_operator_commitments_empty_state_points_to_proposal_promotion():
    js = _read("static/operator_commitments.js")

    assert "No commitments yet. Promote a proposal to create one." in js
    assert "No commitment cards yet." not in js


def test_operator_commitments_raw_secret_helper_rejects_github_pats_before_punctuation():
    js = _read("static/operator_commitments.js")
    body = _js_function_body(js, "_operatorCommitmentQuoteContainsRawSecret")

    assert "ghp_" in body
    assert "github_pat_" in body
    assert "(?![A-Za-z0-9_])" in body

    root = Path(__file__).resolve().parents[1]
    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');

const source = fs.readFileSync('static/operator_commitments.js', 'utf8');
const context = {console, URLSearchParams, Promise};
context.window = context;
context.$ = () => null;
vm.createContext(context);
vm.runInContext(source, context, {filename: 'operator_commitments.js'});

const classic = 'ghp_' + 'A'.repeat(36);
const fineGrained = 'github_pat_' + 'B'.repeat(22) + '_' + 'C'.repeat(59);
for (const [label, token] of [['classic', classic], ['fine-grained', fineGrained]]) {
  for (const [delimiterLabel, suffix] of [
    ['period', '.'],
    ['close paren', ')'],
    ['close bracket', ']'],
    ['double quote', '"'],
    ['newline', '\nnext line'],
  ]) {
    assert.equal(
      context._operatorCommitmentQuoteContainsRawSecret(token + suffix),
      true,
      label + ' token followed by ' + delimiterLabel + ' must be treated as raw secret',
    );
  }
}
assert.equal(
  context._operatorCommitmentQuoteContainsRawSecret('github_pat_[redacted].'),
  false,
  'redacted GitHub PAT placeholders must remain allowed next to punctuation',
);
"""
    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_operator_commitments_manual_refresh_no_polling_timers_or_eventsource():
    js = _read("static/operator_commitments.js")

    assert "function toggleOperatorCommitments" in js
    assert "function refreshOperatorCommitments" in js
    assert "function renderOperatorCommitments" in js
    assert "window.toggleOperatorCommitments" in js
    assert "setInterval" not in js
    assert "setTimeout" not in js
    assert "EventSource" not in js
    assert "DOMContentLoaded" not in js or "refreshOperatorCommitments" not in js.split("DOMContentLoaded", 1)[1]


def test_operator_commitments_form_blocks_submit_until_required_fields_present():
    js = _read("static/operator_commitments.js")
    compact = js.replace(" ", "")

    for field in [
        "owner",
        "deadline_at",
        "review_at",
        "dispatch_mechanism",
        "source",
        "acceptance_criteria",
        "halt_policy",
        "evidence",
        "status",
    ]:
        assert field in js
    assert "_operatorCommitmentValidateForm" in js
    assert "missing.length" in js
    assert "return null" in js
    assert "api('/api/operator/commitments/promote'" in compact or 'api("/api/operator/commitments/promote"' in compact


def test_commitment_recall_helper_exports_session_message_source_without_auto_submit():
    js = _read("static/operator_commitments.js")
    body = _js_function_body(js, "openOperatorCommitmentPromoteFromRecall")
    compact_body = body.replace(" ", "")

    assert "window.openOperatorCommitmentPromoteFromRecall" in js
    assert "kind:'session_message'" in compact_body or 'kind:"session_message"' in compact_body
    for source_field in [
        "session_id",
        "message_index",
        "content_hash",
        "quote",
    ]:
        assert source_field in body
    for forbidden in [
        "/api/operator/commitments/promote",
        "api(",
        "submitOperatorCommitmentForm",
    ]:
        assert forbidden not in body
    assert "operatorCommitmentDispatch" in body
    assert "'manual'" in body or '"manual"' in body
    assert "Stop if source evidence is stale or missing." in body


def test_commitment_recall_helper_refuses_missing_session_message_proof_and_validation_blocks_it():
    root = Path(__file__).resolve().parents[1]
    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');

const source = fs.readFileSync('static/operator_commitments.js', 'utf8');

class ClassList {
  constructor(element) {
    this.element = element;
    this.values = new Set();
  }
  add(...names) {
    names.forEach(name => this.values.add(name));
    this._sync();
  }
  remove(...names) {
    names.forEach(name => this.values.delete(name));
    this._sync();
  }
  contains(name) {
    return this.values.has(name);
  }
  _sync() {
    this.element.className = Array.from(this.values).join(' ');
  }
}

class Element {
  constructor(id = '', tagName = 'div') {
    this.id = id;
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.hidden = false;
    this.title = '';
    this.value = '';
    this.listeners = {};
    this._textContent = '';
    this.className = '';
    this.classList = new ClassList(this);
  }
  get firstChild() {
    return this.children[0] || null;
  }
  append(...nodes) {
    nodes.forEach(node => this.appendChild(node));
  }
  appendChild(node) {
    if (typeof node === 'string') {
      const textNode = new Element('', '#text');
      textNode.textContent = node;
      node = textNode;
    }
    this.children.push(node);
    node.parentNode = this;
    return node;
  }
  removeChild(node) {
    const index = this.children.indexOf(node);
    if (index >= 0) {
      this.children.splice(index, 1);
      node.parentNode = null;
    }
    return node;
  }
  addEventListener(type, handler) {
    this.listeners[type] = handler;
  }
  focus() {}
  set textContent(value) {
    this._textContent = String(value == null ? '' : value);
    this.children = [];
  }
  get textContent() {
    return this._textContent + this.children.map(child => child.textContent).join('');
  }
}

const elements = new Map();
function register(id) {
  const element = new Element(id);
  elements.set(id, element);
  return element;
}
[
  'operatorCommitmentChip',
  'operatorCommitmentLabel',
  'operatorCommitmentPopover',
  'operatorCommitmentList',
  'operatorCommitmentForm',
  'operatorCommitmentMissing',
  'operatorCommitmentSource',
  'operatorCommitmentEvidence',
  'operatorCommitmentTitle',
  'operatorCommitmentOwner',
  'operatorCommitmentDeadline',
  'operatorCommitmentReview',
  'operatorCommitmentDispatch',
  'operatorCommitmentAcceptance',
  'operatorCommitmentHaltPolicy',
  'operatorCommitmentStatus',
].forEach(register);

elements.get('operatorCommitmentForm').hidden = true;
elements.get('operatorCommitmentPopover').hidden = true;

const document = {
  getElementById(id) {
    return elements.get(id) || null;
  },
  createElement(tagName) {
    return new Element('', tagName);
  },
};

const context = {console, document, URLSearchParams, Promise};
context.window = context;
context.$ = id => document.getElementById(id);
context.api = () => Promise.resolve({status:'live', commitments:[], summary:'unused'});

vm.createContext(context);
vm.runInContext(source, context, {filename: 'operator_commitments.js'});

const form = elements.get('operatorCommitmentForm');
const popover = elements.get('operatorCommitmentPopover');
const missing = elements.get('operatorCommitmentMissing');

context.openOperatorCommitmentPromoteFromRecall({});
assert.equal(form.hidden, true, 'malformed recall promotion must keep the draft form closed');
assert.equal(popover.hidden, true, 'malformed recall promotion must not open the popover');
assert.match(missing.textContent, /source proof/i, 'malformed recall promotion should explain the missing source proof');

missing.textContent = '';
form.hidden = true;
popover.hidden = true;
context.openOperatorCommitmentPromoteFromRecall({
  session: {title:'Synthesized fallback proof is not proof', session_id:'session-synthesized-fallback'},
  match: {
    message_index: 0,
    content_hash: 'sha256:' + 'b'.repeat(64),
    snippet: 'valid-looking fallback quote must not become source proof',
    timestamp: 1710000000,
  },
  recency: {label:'live'},
  promotion: {
    task: {
      enabled:true,
      mode:'local_commitment_draft',
      would_execute:false,
      source: {kind:'session_message'},
    },
  },
});
assert.equal(form.hidden, true, 'synthesized fallback proof must keep the draft form closed');
assert.equal(popover.hidden, true, 'synthesized fallback proof must not open the popover');
assert.match(missing.textContent, /source proof/i, 'synthesized fallback proof should explain the missing source proof');

elements.get('operatorCommitmentTitle').value = 'Local commitment title';
elements.get('operatorCommitmentOwner').value = 'operator';
elements.get('operatorCommitmentDeadline').value = '2026-06-01';
elements.get('operatorCommitmentReview').value = '';
elements.get('operatorCommitmentDispatch').value = 'manual';
elements.get('operatorCommitmentAcceptance').value = 'Verify client validation blocks missing source proof';
elements.get('operatorCommitmentHaltPolicy').value = 'Stop if source evidence is stale or missing.';
elements.get('operatorCommitmentEvidence').value = JSON.stringify([{kind:'source', label:'Session recall result', state:'present'}]);
elements.get('operatorCommitmentStatus').value = 'active';
elements.get('operatorCommitmentSource').value = JSON.stringify({kind:'session_message'});
missing.textContent = '';

const payload = context._operatorCommitmentValidateForm();
assert.equal(payload, null, 'client validation must reject session_message sources without full proof');
assert.match(missing.textContent, /source proof/i, 'validation error should name the missing source proof');
assert.match(missing.textContent, /session_id/i, 'validation error should list session_id');
assert.match(missing.textContent, /message_index/i, 'validation error should list message_index');
assert.match(missing.textContent, /content_hash/i, 'validation error should list content_hash');
assert.match(missing.textContent, /quote/i, 'validation error should list quote');

elements.get('operatorCommitmentSource').value = JSON.stringify({
  kind:'session_message',
  session_id:'session-bad-proof',
  message_index:'not-an-index',
  content_hash:'not-a-hash',
  quote:'quoted source proof',
});
missing.textContent = '';

const malformedPayload = context._operatorCommitmentValidateForm();
assert.equal(malformedPayload, null, 'client validation must reject malformed non-empty session_message proof');
assert.match(missing.textContent, /source proof/i, 'malformed validation error should name the source proof');
assert.match(missing.textContent, /message_index/i, 'malformed validation error should list message_index');
assert.match(missing.textContent, /content_hash/i, 'malformed validation error should list content_hash');

missing.textContent = '';
context.openOperatorCommitmentPromoteFromRecall({
  session: {title:'Bad proof recall', session_id:'session-bad-recall'},
  match: {snippet:'quoted malformed proof', timestamp:1710000000},
  recency: {label:'live'},
  promotion: {
    task: {
      enabled:true,
      mode:'local_commitment_draft',
      would_execute:false,
      source: {
        kind:'session_message',
        session_id:'session-bad-recall',
        message_index:'not-an-index',
        content_hash:'not-a-hash',
        quote:'quoted malformed proof',
      },
    },
  },
});
assert.equal(form.hidden, true, 'malformed recall proof must keep the draft form closed');
assert.equal(popover.hidden, true, 'malformed recall proof must not open the popover');
assert.match(missing.textContent, /source proof/i, 'malformed recall proof should explain the invalid source proof');
assert.match(missing.textContent, /message_index/i, 'malformed recall proof should list message_index');
assert.match(missing.textContent, /content_hash/i, 'malformed recall proof should list content_hash');

missing.textContent = '';
form.hidden = true;
popover.hidden = true;
context.openOperatorCommitmentPromoteFromRecall({
  session: {title:'Raw secret recall', session_id:'session-raw-secret'},
  match: {snippet:'raw secret proof quote', timestamp:1710000000},
  recency: {label:'live'},
  promotion: {
    task: {
      enabled:true,
      mode:'local_commitment_draft',
      would_execute:false,
      source: {
        kind:'session_message',
        session_id:'session-raw-secret',
        message_index:0,
        content_hash:'sha256:' + 'c'.repeat(64),
        quote:'password=supersecret',
      },
    },
  },
});
assert.equal(form.hidden, true, 'raw secret recall proof must keep the draft form closed');
assert.equal(popover.hidden, true, 'raw secret recall proof must not open the popover');
assert.match(missing.textContent, /source proof/i, 'raw secret recall proof should explain the invalid source proof');
assert.match(missing.textContent, /secret/i, 'raw secret recall proof should explain that the quote contains a secret');

elements.get('operatorCommitmentSource').value = JSON.stringify({
  kind:'session_message',
  session_id:'session-raw-secret-validation',
  message_index:0,
  content_hash:'sha256:' + 'd'.repeat(64),
  quote:'password=supersecret',
});
missing.textContent = '';

const rawSecretPayload = context._operatorCommitmentValidateForm();
assert.equal(rawSecretPayload, null, 'client validation must reject raw-secret session_message proof quotes');
assert.match(missing.textContent, /source proof/i, 'raw-secret validation error should name the source proof');
assert.match(missing.textContent, /secret/i, 'raw-secret validation error should mention secret');
"""

    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_operator_commitments_css_has_card_form_state_and_mobile_rules():
    css = _read("static/style.css")

    for selector in [
        ".operator-commitment-popover",
        ".operator-commitment-list",
        ".operator-commitment-card",
        ".operator-commitment-form",
        ".operator-commitment-required",
        ".operator-commitment-no-exec",
        ".operator-commitment-missing",
        ".operator-commitment-action",
    ]:
        assert selector in css
    for state in ["state-live", "state-stale", "state-unknown"]:
        assert state in css
    assert "max-width:640px" in css
