import re
import subprocess
from pathlib import Path


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def test_session_recall_markup_exists_after_memory_skill_review_chip_and_popover():
    html = _read("static/index.html")

    for element_id in [
        "operatorSessionRecallChip",
        "operatorSessionRecallPopover",
        "operatorSessionRecallInput",
        "operatorSessionRecallList",
        "operatorSessionRecallRefresh",
    ]:
        assert f'id="{element_id}"' in html

    assert html.index('id="operatorMemorySkillReviewChip"') < html.index('id="operatorSessionRecallChip"') < html.index('id="composerMobileConfigBtn"')
    assert html.index('id="operatorMemorySkillReviewPopover"') < html.index('id="operatorSessionRecallPopover"')


def test_session_recall_script_loaded_after_memory_skill_review_before_boot():
    html = _read("static/index.html")

    assert html.index("static/operator_memory_skill_review.js") < html.index("static/operator_session_recall.js") < html.index("static/boot.js")


def test_session_recall_css_has_popover_card_snippet_source_action_and_mobile_rules():
    css = _read("static/style.css")

    for selector in [
        ".operator-session-recall-popover",
        ".operator-session-recall-list",
        ".operator-session-recall-card",
        ".operator-session-recall-snippet",
        ".operator-session-recall-source",
        ".operator-session-recall-historical",
        ".operator-session-recall-stale",
        ".operator-session-recall-action",
    ]:
        assert selector in css

    mobile_starts = [
        match.start()
        for match in re.finditer(r"@media\s*\(\s*max-width\s*:\s*640px\s*\)", css)
    ]
    assert mobile_starts
    mobile_sections = [
        css[start : mobile_starts[index + 1] if index + 1 < len(mobile_starts) else len(css)]
        for index, start in enumerate(mobile_starts)
    ]
    assert any("operator-session-recall" in section for section in mobile_sections)


def _session_recall_js():
    return _read("static/operator_session_recall.js")


def test_session_recall_changelog_mentions_manual_read_only_recall():
    changelog = _read("CHANGELOG.md")
    unreleased = changelog.split("## [v0.51.137]", 1)[0]

    assert "Session Recall" in unreleased
    assert "manual" in unreleased.lower()
    assert "read-only" in unreleased.lower()
    assert "local" in unreleased.lower()
    assert "proposal" in unreleased.lower()
    assert "no apply" in unreleased.lower() or "without applying" in unreleased.lower()


def _strip_js_strings_and_comments(js):
    js = re.sub(r"//.*", "", js)
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    return re.sub(r"(['\"`])(?:\\.|(?!\1).)*\1", "''", js, flags=re.DOTALL)


def _top_level_calls(js, function_name):
    cleaned = _strip_js_strings_and_comments(js)
    calls = []
    depth = 0
    for line_number, line in enumerate(cleaned.splitlines(), start=1):
        line_depth = depth
        if re.search(rf"\bfunction\s+{re.escape(function_name)}\s*\(", line):
            pass
        elif line_depth == 0 and re.search(rf"\b{re.escape(function_name)}\s*\(", line):
            calls.append((line_number, line.strip()))
        depth += line.count("{") - line.count("}")
        depth = max(depth, 0)
    return calls


def test_session_recall_js_uses_operator_session_recall_endpoint_only_for_search():
    js = _session_recall_js()
    allowed_endpoints = {"/api/operator/session-recall", "/api/operator/memory-skill-review/propose"}
    quoted_api_paths = set(re.findall(r"[\"'`](/api/[A-Za-z0-9_./-]+)", js))

    assert quoted_api_paths == allowed_endpoints
    assert "/api/operator/session-recall" in quoted_api_paths
    assert "/api/operator/memory-skill-review/propose" in quoted_api_paths
    assert re.search(r"\bapi\s*\(\s*[\"'`]/api/operator/session-recall\b", js)
    assert re.search(r"\bapi\s*\(\s*[\"'`]/api/operator/memory-skill-review/propose\b", js)
    assert not re.search(r"\bfetch\s*\(", js)
    assert "XMLHttpRequest" not in js
    assert "http://" not in js
    assert "https://" not in js
    for forbidden in [
        "/api/memory/write",
        "/api/skills/save",
        "/api/skills/delete",
        "/api/skills/toggle",
        "/apply",
        "/api/kanban/",
        "/api/chat",
        "/api/operator/commitments/",
        "submitChat",
        "startChat",
        "sendMessage",
        "commitmentForm.submit",
        "operatorCommitmentForm.submit",
    ]:
        assert forbidden not in js


def test_session_recall_memory_skill_review_proposal_form_exists_with_user_entered_fields():
    html = _read("static/index.html")
    popover_start = html.index('id="operatorSessionRecallPopover"')
    popover_end = html.index('id="operatorSessionRecallList"', popover_start)
    popover_form = html[popover_start:popover_end]

    for element_id in [
        "operatorSessionRecallMemoryProposalPanel",
        "operatorSessionRecallMemoryProposalForm",
        "operatorSessionRecallMemoryProposalTargetKind",
        "operatorSessionRecallMemoryProposalTargetSection",
        "operatorSessionRecallMemoryProposalTargetName",
        "operatorSessionRecallMemoryProposalTargetCategory",
        "operatorSessionRecallMemoryProposalSummary",
        "operatorSessionRecallMemoryProposalContent",
        "operatorSessionRecallMemoryProposalStatus",
    ]:
        assert f'id="{element_id}"' in popover_form

    for field_name in [
        "target_kind",
        "target_section",
        "target_name",
        "target_category",
        "proposed_summary",
        "proposed_content",
    ]:
        assert f'name="{field_name}"' in popover_form

    for fake_value in [
        "Proposed durable operator memory",
        "Updated durable preference",
        "sample",
        "demo",
        "fake",
    ]:
        assert fake_value not in popover_form


def test_session_recall_values_use_text_content_not_inner_html_or_markdown():
    js = _session_recall_js()

    assert ".textContent" in js
    assert "document.createElement" in js
    for forbidden in [
        ".innerHTML",
        ".outerHTML",
        "insertAdjacentHTML",
        "renderMd(",
        "renderMarkdown(",
        "marked(",
        "eval(",
        "Function(",
    ]:
        assert forbidden not in js


def test_session_recall_manual_search_no_polling_timers_or_eventsource():
    js = _session_recall_js()

    for forbidden in [
        "setInterval",
        "setTimeout",
        "EventSource",
        "WebSocket",
        "Worker(",
        "navigator.sendBeacon",
    ]:
        assert forbidden not in js

    assert "DOMContentLoaded" not in js or not re.search(
        r"DOMContentLoaded[\s\S]*refreshOperatorSessionRecall\s*\(",
        js,
    )
    assert _top_level_calls(js, "refreshOperatorSessionRecall") == []


def test_session_recall_renders_required_fields_and_honest_states():
    js = _session_recall_js()

    for required in [
        "query",
        "results",
        "snippet",
        "timestamp",
        "session_id",
        "source_label",
        "historical",
        "stale",
        "unknown",
        "would_execute",
        "issues",
    ]:
        assert required in js


def test_session_recall_memory_skill_review_proposal_payload_is_draft_only_and_source_backed():
    js = _session_recall_js()
    assert "_operatorSessionRecallMemoryReviewSourceEvidence" in js
    assert "promotion.memory_review" in js
    assert "source_evidence[0]" in js
    assert "Draft memory/skill review" in js
    assert "submitOperatorSessionRecallMemoryProposal" in js
    assert "would_execute: false" in js
    for required in [
        "target",
        "proposed_change",
        "source_evidence",
        "classification",
        "stale_risk",
        "would_execute",
    ]:
        assert required in js

    start = js.index("function _operatorSessionRecallMemoryReviewSourceEvidence")
    end = js.index("function _operatorSessionRecallCanPromoteMemoryReview", start)
    extractor_body = js[start:end]
    assert "session.title" not in extractor_body
    assert "match.snippet" not in extractor_body


def test_session_recall_task_promotion_uses_local_commitment_draft_not_kanban_or_chat():
    js = _session_recall_js()

    assert (
        "openOperatorCommitmentPromoteFromRecall" in js
        or "openOperatorCommitmentPromote" in js
    )

    for forbidden in [
        "/api/kanban/",
        "createKanbanTask",
        "updateKanbanTask",
        "addKanbanComment",
        "runKanbanDispatcher",
        "nudgeKanbanDispatcher",
        "/api/chat",
        "submitChat",
        "startChat",
        "sendMessage",
        "window.send",
        "/api/operator/commitments/promote",
    ]:
        assert forbidden not in js


def test_session_recall_raw_secret_detection_rejects_github_pats_before_punctuation():
    js = _session_recall_js()
    start = js.index("function _operatorSessionRecallQuoteContainsRawSecret")
    end = js.index("function _operatorSessionRecallCommitmentSource", start)
    body = js[start:end]

    assert "ghp_" in body
    assert "github_pat_" in body
    assert "(?![A-Za-z0-9_])" in body

    root = Path(__file__).resolve().parents[1]
    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');

const source = fs.readFileSync('static/operator_session_recall.js', 'utf8');
const context = {console, URLSearchParams, Date, Promise};
context.window = context;
context.$ = () => null;
vm.createContext(context);
vm.runInContext(source, context, {filename: 'operator_session_recall.js'});

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
      context._operatorSessionRecallQuoteContainsRawSecret(token + suffix),
      true,
      label + ' token followed by ' + delimiterLabel + ' must be treated as raw secret',
    );
  }
}
assert.equal(
  context._operatorSessionRecallQuoteContainsRawSecret('github_pat_[redacted].'),
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


def test_session_recall_task_promotion_requires_explicit_safe_session_message_proof():
    root = Path(__file__).resolve().parents[1]
    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');

const source = fs.readFileSync('static/operator_session_recall.js', 'utf8');

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
  'operatorSessionRecallInput',
  'operatorSessionRecallChip',
  'operatorSessionRecallLabel',
  'operatorSessionRecallPopover',
  'operatorSessionRecallStatus',
  'operatorSessionRecallList',
].forEach(register);

const document = {
  getElementById(id) {
    return elements.get(id) || null;
  },
  createElement(tagName) {
    return new Element('', tagName);
  },
};

const context = {console, document, URLSearchParams, Date, Promise};
context.window = context;
context.$ = id => document.getElementById(id);

vm.createContext(context);
vm.runInContext(source, context, {filename: 'operator_session_recall.js'});

const completeSource = suffix => ({
  kind: 'session_message',
  session_id: 'session-' + suffix,
  message_index: 0,
  content_hash: 'sha256:' + 'a'.repeat(64),
  quote: 'source quote ' + suffix,
});

context.renderOperatorSessionRecall({
  status: 'live',
  summary: 'session recall regression payload',
  query: {text: 'promote safety'},
  sources: [],
  results: [
    {
      session: {title: 'Missing explicit safe false', session_id: 'session-missing', source_label: 'saved sessions'},
      match: {snippet: 'malformed missing safe mode snippet', timestamp: 1710000000},
      recency: {label: 'live'},
      promotion: {
        task: {
          enabled: true,
          mode: 'local_commitment_draft',
          source: completeSource('missing'),
        },
      },
    },
    {
      session: {title: 'Task string unsafe true', session_id: 'session-task-string', source_label: 'saved sessions'},
      match: {snippet: 'malformed task string true snippet', timestamp: 1710000001},
      recency: {label: 'live'},
      promotion: {
        task: {
          enabled: true,
          mode: 'local_commitment_draft',
          would_execute: 'true',
          source: completeSource('task-string'),
        },
      },
    },
    {
      session: {title: 'Top level string unsafe true', session_id: 'session-top-string', source_label: 'saved sessions'},
      match: {snippet: 'malformed top string true snippet', timestamp: 1710000002},
      recency: {label: 'live'},
      would_execute: 'true',
      promotion: {
        task: {
          enabled: true,
          mode: 'local_commitment_draft',
          would_execute: false,
          source: completeSource('top-string'),
        },
      },
    },
    {
      session: {title: 'Top level numeric unsafe truthy', session_id: 'session-top-numeric', source_label: 'saved sessions'},
      match: {snippet: 'malformed top numeric truthy snippet', timestamp: 1710000003},
      recency: {label: 'live'},
      would_execute: 1,
      promotion: {
        task: {
          enabled: true,
          mode: 'local_commitment_draft',
          would_execute: false,
          source: completeSource('top-numeric'),
        },
      },
    },
    {
      session: {title: 'Malformed message index proof', session_id: 'session-bad-index', source_label: 'saved sessions'},
      match: {snippet: 'bad message index proof snippet', timestamp: 1710000004},
      recency: {label: 'live'},
      promotion: {
        task: {
          enabled: true,
          mode: 'local_commitment_draft',
          would_execute: false,
          source: {...completeSource('bad-index'), message_index: 'not-an-index'},
        },
      },
    },
    {
      session: {title: 'Malformed content hash proof', session_id: 'session-bad-hash', source_label: 'saved sessions'},
      match: {snippet: 'bad content hash proof snippet', timestamp: 1710000005},
      recency: {label: 'live'},
      promotion: {
        task: {
          enabled: true,
          mode: 'local_commitment_draft',
          would_execute: false,
          source: {...completeSource('bad-hash'), content_hash: 'not-a-hash'},
        },
      },
    },
    {
      session: {title: 'Raw Bearer quote proof', session_id: 'session-raw-bearer', source_label: 'saved sessions'},
      match: {snippet: 'raw bearer quote proof snippet', timestamp: 1710000006},
      recency: {label: 'live'},
      promotion: {
        task: {
          enabled: true,
          mode: 'local_commitment_draft',
          would_execute: false,
          source: {...completeSource('raw-bearer'), quote: 'Bearer abcdefghijklmnop12345'},
        },
      },
    },
    {
      session: {title: 'Synthesized fallback proof is not proof', session_id: 'session-synthesized-fallback', source_label: 'saved sessions'},
      match: {
        message_index: 0,
        content_hash: 'sha256:' + 'b'.repeat(64),
        snippet: 'valid-looking fallback quote must not become source proof',
        timestamp: 1710000007,
      },
      recency: {label: 'live'},
      promotion: {
        task: {
          enabled: true,
          mode: 'local_commitment_draft',
          would_execute: false,
          source: {kind: 'session_message'},
        },
      },
    },
    {
      session: {title: 'Valid safe local draft', session_id: 'session-valid', source_label: 'saved sessions'},
      match: {snippet: 'valid safe snippet', timestamp: 1710000008},
      recency: {label: 'live'},
      promotion: {
        task: {
          enabled: true,
          mode: 'local_commitment_draft',
          would_execute: false,
          source: completeSource('valid'),
        },
      },
    },
  ],
  count: 9,
  issues: [],
  would_execute: false,
});

const list = elements.get('operatorSessionRecallList');
function cardText(title) {
  const card = list.children.find(child => child.textContent.includes(title));
  assert.ok(card, 'missing rendered card for ' + title + '\n' + list.textContent);
  return card.textContent;
}

const missingSafeText = cardText('Missing explicit safe false');
assert.doesNotMatch(missingSafeText, /Draft local task/, 'missing promotion.task.would_execute must not render a local task draft');
assert.doesNotMatch(missingSafeText, /would_execute=false/, 'missing explicit safe false must not be displayed as would_execute=false');

const taskStringText = cardText('Task string unsafe true');
assert.doesNotMatch(taskStringText, /Draft local task/, 'string true task would_execute must not render a local task draft');
assert.doesNotMatch(taskStringText, /would_execute=false/, 'string true task would_execute must not be displayed as false');
assert.match(taskStringText, /would_execute=true/, 'string true task would_execute must be treated as unsafe');

const topStringText = cardText('Top level string unsafe true');
assert.doesNotMatch(topStringText, /Draft local task/, 'string true top-level would_execute must not render a local task draft');
assert.doesNotMatch(topStringText, /would_execute=false/, 'string true top-level would_execute must not be displayed as false');
assert.match(topStringText, /would_execute=true/, 'string true top-level would_execute must be treated as unsafe');

const topNumericText = cardText('Top level numeric unsafe truthy');
assert.doesNotMatch(topNumericText, /Draft local task/, 'numeric truthy top-level would_execute must not render a local task draft');
assert.doesNotMatch(topNumericText, /would_execute=false/, 'numeric truthy top-level would_execute must not be displayed as false');

const badIndexText = cardText('Malformed message index proof');
assert.doesNotMatch(badIndexText, /Draft local task/, 'malformed message_index proof must not render a local task draft');

const badHashText = cardText('Malformed content hash proof');
assert.doesNotMatch(badHashText, /Draft local task/, 'malformed content_hash proof must not render a local task draft');

const rawBearerText = cardText('Raw Bearer quote proof');
assert.doesNotMatch(rawBearerText, /Draft local task/, 'raw bearer session_message quote must not render a local task draft');

const synthesizedFallbackText = cardText('Synthesized fallback proof is not proof');
assert.doesNotMatch(synthesizedFallbackText, /Draft local task/, 'valid-looking session/match fallback fields must not synthesize source proof');

const validText = cardText('Valid safe local draft');
assert.match(validText, /Draft local task/, 'valid explicit false session-message proof may render a local task draft');
assert.match(validText, /would_execute=false/, 'valid explicit false session-message proof may display would_execute=false');
"""

    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_session_recall_memory_skill_review_proposal_requires_user_fields_and_posts_draft_only():
    root = Path(__file__).resolve().parents[1]
    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');

const source = fs.readFileSync('static/operator_session_recall.js', 'utf8');

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
    this.name = '';
    this.type = '';
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
function register(id, value = '') {
  const element = new Element(id);
  element.value = value;
  elements.set(id, element);
  return element;
}
[
  'operatorSessionRecallInput',
  'operatorSessionRecallChip',
  'operatorSessionRecallLabel',
  'operatorSessionRecallPopover',
  'operatorSessionRecallStatus',
  'operatorSessionRecallList',
  'operatorSessionRecallMemoryProposalPanel',
  'operatorSessionRecallMemoryProposalForm',
  'operatorSessionRecallMemoryProposalStatus',
  'operatorSessionRecallMemoryProposalTargetKind',
  'operatorSessionRecallMemoryProposalTargetSection',
  'operatorSessionRecallMemoryProposalTargetName',
  'operatorSessionRecallMemoryProposalTargetCategory',
  'operatorSessionRecallMemoryProposalOperation',
  'operatorSessionRecallMemoryProposalSummary',
  'operatorSessionRecallMemoryProposalContent',
  'operatorSessionRecallMemoryProposalClassificationDurability',
  'operatorSessionRecallMemoryProposalClassificationReason',
  'operatorSessionRecallMemoryProposalTransientRisk',
  'operatorSessionRecallMemoryProposalStaleState',
  'operatorSessionRecallMemoryProposalExpiresAt',
  'operatorSessionRecallMemoryProposalStaleReason',
  'operatorSessionRecallMemoryProposalEvidenceSessionId',
  'operatorSessionRecallMemoryProposalEvidenceMessageIndex',
  'operatorSessionRecallMemoryProposalEvidenceContentHash',
  'operatorSessionRecallMemoryProposalEvidenceQuote',
].forEach(id => register(id));
elements.get('operatorSessionRecallMemoryProposalPanel').hidden = true;
elements.get('operatorSessionRecallMemoryProposalOperation').value = 'append';
elements.get('operatorSessionRecallMemoryProposalClassificationDurability').value = 'durable';
elements.get('operatorSessionRecallMemoryProposalTransientRisk').value = 'low';
elements.get('operatorSessionRecallMemoryProposalStaleState').value = 'current';

const document = {
  getElementById(id) {
    return elements.get(id) || null;
  },
  createElement(tagName) {
    return new Element('', tagName);
  },
};

const apiCalls = [];
const refreshCalls = [];
const context = {
  console,
  document,
  URLSearchParams,
  Date,
  Promise,
  api(url, options) {
    apiCalls.push({url, options: options || {}});
    return Promise.resolve({ok: true, id: 'msr_from_recall', would_execute: false});
  },
  refreshOperatorMemorySkillReview(options) {
    refreshCalls.push(options);
    return Promise.resolve({ok: true});
  },
};
context.window = context;
context.$ = id => document.getElementById(id);

vm.createContext(context);
vm.runInContext(source, context, {filename: 'operator_session_recall.js'});

const sourceEvidence = {
  kind: 'session_message',
  session_id: 'recall-session-1',
  message_index: 7,
  content_hash: 'sha256:' + 'c'.repeat(64),
  quote: 'Please remember that I prefer concise release summaries.',
};

context.renderOperatorSessionRecall({
  status: 'live',
  summary: 'one source-backed memory proposal',
  query: {text: 'release summaries'},
  sources: [],
  results: [{
    session: {title: 'Release summary preference', session_id: 'recall-session-1', source_label: 'saved sessions'},
    match: {snippet: 'This snippet must not become proposal content.', timestamp: 1710000000},
    recency: {label: 'live'},
    would_execute: false,
    promotion: {
      memory_review: {
        enabled: true,
        mode: 'local_memory_skill_review_proposal',
        would_execute: false,
        source_evidence: [sourceEvidence],
      },
    },
  }],
  count: 1,
  issues: [],
  would_execute: false,
});

const list = elements.get('operatorSessionRecallList');
function walk(node, out = []) {
  out.push(node);
  node.children.forEach(child => walk(child, out));
  return out;
}
const memoryButton = walk(list).find(node => node.tagName === 'BUTTON' && /Draft memory\/skill review/.test(node.textContent));
assert.ok(memoryButton, 'eligible recall result must render Draft memory/skill review action');
assert.equal(apiCalls.length, 0, 'rendering/opening must not POST');
memoryButton.listeners.click({preventDefault(){}});

assert.equal(elements.get('operatorSessionRecallMemoryProposalPanel').hidden, false, 'click opens inline proposal form');
assert.equal(elements.get('operatorSessionRecallMemoryProposalEvidenceSessionId').textContent, sourceEvidence.session_id);
assert.equal(elements.get('operatorSessionRecallMemoryProposalEvidenceMessageIndex').textContent, String(sourceEvidence.message_index));
assert.equal(elements.get('operatorSessionRecallMemoryProposalEvidenceContentHash').textContent, sourceEvidence.content_hash);
assert.equal(elements.get('operatorSessionRecallMemoryProposalEvidenceQuote').textContent, sourceEvidence.quote);
assert.equal(elements.get('operatorSessionRecallMemoryProposalTargetKind').value, '', 'target kind is user-entered only');
assert.equal(elements.get('operatorSessionRecallMemoryProposalTargetSection').value, '', 'target section is user-entered only');
assert.equal(elements.get('operatorSessionRecallMemoryProposalTargetName').value, '', 'target name is user-entered only');
assert.equal(elements.get('operatorSessionRecallMemoryProposalTargetCategory').value, '', 'target category is user-entered only');
assert.equal(elements.get('operatorSessionRecallMemoryProposalSummary').value, '', 'summary is user-entered only');
assert.equal(elements.get('operatorSessionRecallMemoryProposalContent').value, '', 'content is user-entered only');

(async () => {
  await context.submitOperatorSessionRecallMemoryProposal({preventDefault(){}});
  assert.equal(apiCalls.length, 0, 'submit without user target/proposed fields must not call API');
  assert.match(elements.get('operatorSessionRecallMemoryProposalStatus').textContent, /target/i);

  elements.get('operatorSessionRecallMemoryProposalTargetKind').value = 'memory';
  elements.get('operatorSessionRecallMemoryProposalTargetSection').value = 'memory';
  elements.get('operatorSessionRecallMemoryProposalSummary').value = 'Remember concise release summary preference.';
  elements.get('operatorSessionRecallMemoryProposalContent').value = 'The user prefers concise release summaries.';
  elements.get('operatorSessionRecallMemoryProposalClassificationReason').value = 'The user stated a durable writing preference.';
  elements.get('operatorSessionRecallMemoryProposalExpiresAt').value = '2026-06-10T00:00:00Z';
  elements.get('operatorSessionRecallMemoryProposalStaleReason').value = 'Preference remains current unless superseded.';

  const result = await context.submitOperatorSessionRecallMemoryProposal({preventDefault(){}});
  assert.equal(result.ok, true);
  assert.equal(apiCalls.length, 1, 'filled form must call one local proposal API');
  assert.equal(apiCalls[0].url, '/api/operator/memory-skill-review/propose');
  const body = JSON.parse(apiCalls[0].options.body);
  assert.equal(apiCalls[0].options.method, 'POST');
  assert.equal(body.would_execute, false);
  assert.deepEqual(body.source_evidence, [sourceEvidence], 'proposal must preserve exact session-message proof');
  assert.deepEqual(body.target, {kind: 'memory', section: 'memory'});
  assert.equal(body.proposed_change.operation, 'append');
  assert.equal(body.proposed_change.summary, 'Remember concise release summary preference.');
  assert.equal(body.proposed_change.proposed_content, 'The user prefers concise release summaries.');
  assert.equal(body.classification.durability, 'durable');
  assert.equal(body.classification.reason, 'The user stated a durable writing preference.');
  assert.equal(body.classification.transient_risk, 'low');
  assert.equal(body.stale_risk.state, 'current');
  assert.equal(body.stale_risk.expires_at, '2026-06-10T00:00:00Z');
  assert.equal(body.stale_risk.reason, 'Preference remains current unless superseded.');
  assert.doesNotMatch(JSON.stringify(body), /This snippet must not become proposal content/);
  assert.equal(
    JSON.stringify(refreshCalls),
    JSON.stringify([{force: true}]),
    'successful proposal refreshes local review queue when available',
  );
})().catch(error => {
  console.error(error && error.stack || error);
  process.exit(1);
});
"""

    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_session_recall_memory_skill_review_proposal_refuses_missing_or_raw_secret_proof():
    root = Path(__file__).resolve().parents[1]
    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');

const source = fs.readFileSync('static/operator_session_recall.js', 'utf8');
class ClassList {
  constructor(element) { this.element = element; this.values = new Set(); }
  add(...names) { names.forEach(name => this.values.add(name)); this._sync(); }
  remove(...names) { names.forEach(name => this.values.delete(name)); this._sync(); }
  contains(name) { return this.values.has(name); }
  _sync() { this.element.className = Array.from(this.values).join(' '); }
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
  get firstChild() { return this.children[0] || null; }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
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
    if (index >= 0) { this.children.splice(index, 1); node.parentNode = null; }
    return node;
  }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  focus() {}
  set textContent(value) { this._textContent = String(value == null ? '' : value); this.children = []; }
  get textContent() { return this._textContent + this.children.map(child => child.textContent).join(''); }
}
const elements = new Map();
function register(id) { const element = new Element(id); elements.set(id, element); return element; }
[
  'operatorSessionRecallInput',
  'operatorSessionRecallChip',
  'operatorSessionRecallLabel',
  'operatorSessionRecallPopover',
  'operatorSessionRecallStatus',
  'operatorSessionRecallList',
  'operatorSessionRecallMemoryProposalPanel',
  'operatorSessionRecallMemoryProposalForm',
  'operatorSessionRecallMemoryProposalStatus',
  'operatorSessionRecallMemoryProposalEvidenceSessionId',
  'operatorSessionRecallMemoryProposalEvidenceMessageIndex',
  'operatorSessionRecallMemoryProposalEvidenceContentHash',
  'operatorSessionRecallMemoryProposalEvidenceQuote',
].forEach(register);
const document = {
  getElementById(id) { return elements.get(id) || null; },
  createElement(tagName) { return new Element('', tagName); },
};
const apiCalls = [];
const context = {console, document, URLSearchParams, Date, Promise, api(url, options){ apiCalls.push({url, options}); return Promise.resolve({ok:true}); }};
context.window = context;
context.$ = id => document.getElementById(id);
vm.createContext(context);
vm.runInContext(source, context, {filename: 'operator_session_recall.js'});

const proof = {
  kind: 'session_message',
  session_id: 'session-safe',
  message_index: 1,
  content_hash: 'sha256:' + 'd'.repeat(64),
  quote: 'safe quote',
};
const missingProof = {
  session: {title: 'Missing proof', session_id: 'session-missing', source_label: 'saved sessions'},
  match: {message_index: 0, content_hash: 'sha256:' + 'e'.repeat(64), snippet: 'fallback snippet is not source proof'},
  recency: {label: 'live'},
  would_execute: false,
  promotion: {memory_review: {enabled: true, mode: 'local_memory_skill_review_proposal', would_execute: false}},
};
const rawSecretProof = {
  session: {title: 'Raw secret proof', session_id: 'session-secret', source_label: 'saved sessions'},
  match: {snippet: 'secret snippet', timestamp: 1710000001},
  recency: {label: 'live'},
  would_execute: false,
  promotion: {memory_review: {enabled: true, mode: 'local_memory_skill_review_proposal', would_execute: false, source_evidence: [{...proof, quote: 'password=hunter2.'}]}},
};
const objectFieldProof = {
  session: {title: 'Object field proof', session_id: 'session-object', source_label: 'saved sessions'},
  match: {snippet: 'object snippet', timestamp: 1710000002},
  recency: {label: 'live'},
  would_execute: false,
  promotion: {memory_review: {enabled: true, mode: 'local_memory_skill_review_proposal', would_execute: false, source_evidence: [{...proof, session_id: {value: 'session-object'}, quote: {text: 'safe quote'}}]}},
};
context.renderOperatorSessionRecall({
  status: 'live',
  summary: 'malformed proposals',
  query: {text: 'bad proofs'},
  sources: [],
  results: [missingProof, rawSecretProof, objectFieldProof],
  count: 3,
  issues: [],
  would_execute: false,
});
const list = elements.get('operatorSessionRecallList');
assert.match(list.textContent, /Missing proof/);
assert.match(list.textContent, /Raw secret proof/);
assert.doesNotMatch(list.textContent, /Draft memory\/skill review/, 'malformed or raw-secret proofs must not render memory proposal buttons');
assert.equal(context.openOperatorSessionRecallMemoryProposal(missingProof), false, 'missing source_evidence must be refused even with fallback fields');
assert.equal(context.openOperatorSessionRecallMemoryProposal(rawSecretProof), false, 'raw secret source quote must be refused');
assert.equal(context.openOperatorSessionRecallMemoryProposal(objectFieldProof), false, 'object session_id/quote proof fields must be refused before coercion');
assert.equal(apiCalls.length, 0, 'refused proposal must not call API');
"""

    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_session_recall_blank_search_invalidates_pending_results():
    root = Path(__file__).resolve().parents[1]
    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');

const source = fs.readFileSync('static/operator_session_recall.js', 'utf8');

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
  'operatorSessionRecallInput',
  'operatorSessionRecallChip',
  'operatorSessionRecallLabel',
  'operatorSessionRecallPopover',
  'operatorSessionRecallStatus',
  'operatorSessionRecallList',
].forEach(register);

elements.get('operatorSessionRecallPopover').hidden = false;

const document = {
  getElementById(id) {
    return elements.get(id) || null;
  },
  createElement(tagName) {
    return new Element('', tagName);
  },
};

let resolveApi;
const apiCalls = [];
const apiPromise = new Promise(resolve => {
  resolveApi = resolve;
});

const context = {
  console,
  document,
  URLSearchParams,
  Date,
  Promise,
  api(url) {
    apiCalls.push(url);
    return apiPromise;
  },
};
context.window = context;
context.$ = id => document.getElementById(id);

vm.createContext(context);
vm.runInContext(source, context, {filename: 'operator_session_recall.js'});

(async () => {
  const input = elements.get('operatorSessionRecallInput');
  const popover = elements.get('operatorSessionRecallPopover');
  const status = elements.get('operatorSessionRecallStatus');
  const list = elements.get('operatorSessionRecallList');

  input.value = 'needle';
  const original = context.refreshOperatorSessionRecall();

  input.value = '';
  await context.refreshOperatorSessionRecall();

  assert.equal(apiCalls.length, 1, 'blank search should not call the API');
  assert.ok(popover.classList.contains('state-unknown'), 'blank search should render unknown state');
  assert.match(status.textContent, /no source queried yet/i);
  assert.match(list.textContent, /query is required for session recall/i);
  assert.doesNotMatch(list.textContent, /old snippet/i);

  resolveApi({
    status: 'live',
    summary: 'old response',
    query: {text: 'needle'},
    sources: [],
    results: [{
      session: {
        title: 'Old Session',
        session_id: 'old-session',
        source_label: 'saved sessions',
      },
      match: {
        snippet: 'old snippet',
        timestamp: 1710000000,
      },
      recency: {label: 'historical'},
      would_execute: false,
    }],
    count: 1,
    issues: [],
    would_execute: false,
  });
  await original;
  await Promise.resolve();

  assert.ok(
    popover.classList.contains('state-unknown'),
    'stale response must not replace blank unknown state',
  );
  assert.match(status.textContent, /no source queried yet/i);
  assert.match(list.textContent, /query is required for session recall/i);
  assert.doesNotMatch(
    list.textContent,
    /old snippet/i,
    'stale in-flight response rendered old snippet after blank search',
  );
})().catch(error => {
  console.error(error && error.stack || error);
  process.exit(1);
});
"""

    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
