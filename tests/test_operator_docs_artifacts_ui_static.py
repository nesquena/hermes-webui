import re
import subprocess
from pathlib import Path


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def _docs_artifacts_js():
    return _read("static/operator_docs_artifacts.js")


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


def test_docs_artifacts_markup_exists_after_session_recall_chip_and_popover():
    html = _read("static/index.html")

    for element_id in [
        "operatorDocsArtifactsChip",
        "operatorDocsArtifactsLabel",
        "operatorDocsArtifactsPopover",
        "operatorDocsArtifactsRefresh",
        "operatorDocsArtifactsInput",
        "operatorDocsArtifactsKind",
        "operatorDocsArtifactsRoot",
        "operatorDocsArtifactsList",
        "operatorDocsArtifactsPreview",
    ]:
        assert f'id="{element_id}"' in html

    assert html.index('id="operatorSessionRecallChip"') < html.index('id="operatorDocsArtifactsChip"') < html.index('id="composerMobileConfigBtn"')
    assert html.index('id="operatorSessionRecallPopover"') < html.index('id="operatorDocsArtifactsPopover"')


def test_docs_artifacts_script_loaded_after_session_recall_before_boot():
    html = _read("static/index.html")

    assert html.index("static/operator_session_recall.js") < html.index("static/operator_docs_artifacts.js") < html.index("static/boot.js")


def test_docs_artifacts_css_has_popover_card_source_preview_action_and_mobile_rules():
    css = _read("static/style.css")

    for selector in [
        ".operator-docs-artifacts-popover",
        ".operator-docs-artifacts-list",
        ".operator-docs-artifacts-card",
        ".operator-docs-artifacts-source",
        ".operator-docs-artifacts-preview",
        ".operator-docs-artifacts-action",
        ".operator-docs-artifacts-current",
        ".operator-docs-artifacts-historical",
        ".operator-docs-artifacts-stale",
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
    assert any("operator-docs-artifacts" in section for section in mobile_sections)


def test_docs_artifacts_kind_filter_options_match_backend_kinds():
    html = _read("static/index.html")
    start = html.index('id="operatorDocsArtifactsKind"')
    end = html.index("</select>", start)
    select = html[start:end]
    values = set(re.findall(r'<option\s+value="([^"]+)"', select))

    assert {
        "all",
        "meta_plan",
        "plan",
        "handoff",
        "brief",
        "artifact_manifest",
        "action_summary",
        "state_summary",
        "changelog",
    } <= values


def test_docs_artifacts_markup_copy_does_not_claim_wired_list_search_is_coming_next():
    html = _read("static/index.html")
    start = html.index('id="operatorDocsArtifactsPopover"')
    end = html.index('id="operatorDocsArtifactsPreview"', start)
    popover = html[start:end]

    for stale_copy in [
        "list and preview not wired yet",
        "Search docs or artifacts (coming next)",
        "coming next",
    ]:
        assert stale_copy not in popover


def test_docs_artifacts_changelog_mentions_manual_read_only_browser():
    changelog = _read("CHANGELOG.md")
    unreleased = changelog.split("## [v0.51.137]", 1)[0]

    assert "Docs/Artifact Browser" in unreleased or "Docs / Artifacts" in unreleased
    assert "Slice 7" in unreleased
    assert "manual" in unreleased.lower()
    assert "read-only" in unreleased.lower()
    assert "local" in unreleased.lower()
    assert "no fake" in unreleased.lower() or "without fake" in unreleased.lower()
    assert "no apply" in unreleased.lower() or "without applying" in unreleased.lower()


def test_docs_artifacts_js_shell_exports_manual_functions():
    js = _docs_artifacts_js()

    for name in [
        "toggleOperatorDocsArtifacts",
        "hideOperatorDocsArtifacts",
        "refreshOperatorDocsArtifacts",
        "renderOperatorDocsArtifacts",
        "openOperatorDocsArtifactPreview",
        "initOperatorDocsArtifacts",
    ]:
        assert f"window.{name}" in js


def test_docs_artifacts_js_uses_docs_artifacts_endpoints_only():
    js = _docs_artifacts_js()
    allowed_endpoints = {"/api/operator/docs-artifacts", "/api/operator/docs-artifacts/open"}
    quoted_api_paths = set(re.findall(r"[\"'`](/api/[A-Za-z0-9_./-]+)", js))

    assert quoted_api_paths == allowed_endpoints
    assert re.search(r"\bapi\s*\(\s*[\"'`]/api/operator/docs-artifacts\b", js)
    assert not re.search(r"\bfetch\s*\(", js)
    assert "XMLHttpRequest" not in js
    assert "http://" not in js
    assert "https://" not in js
    for forbidden_method in ["POST", "PATCH", "DELETE"]:
        assert forbidden_method not in js


def test_docs_artifacts_values_use_text_content_not_inner_html_or_markdown():
    js = _docs_artifacts_js()

    assert ".textContent" in js
    assert "document.createElement" in js
    for forbidden in [
        ".innerHTML",
        ".outerHTML",
        "insertAdjacentHTML",
        "renderMd(",
        "renderMarkdown(",
        "marked(",
        "markdownToHtml",
        "eval(",
        "Function(",
    ]:
        assert forbidden not in js


def test_docs_artifacts_manual_only_no_polling_timers_or_eventsource():
    js = _docs_artifacts_js()

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
        r"DOMContentLoaded[\s\S]*refreshOperatorDocsArtifacts\s*\(",
        js,
    )
    assert _top_level_calls(js, "refreshOperatorDocsArtifacts") == []


def test_docs_artifacts_renders_required_fields_and_honest_states():
    js = _docs_artifacts_js()

    for required in [
        "sources",
        "items",
        "issues",
        "root_id",
        "relative_path",
        "display_path",
        "size_bytes",
        "mtime",
        "freshness",
        "current",
        "historical",
        "stale",
        "unknown",
        "would_execute",
        "preview_available",
    ]:
        assert required in js


def test_docs_artifacts_forbids_mutation_dispatch_memory_skill_and_chat_tokens():
    js = _docs_artifacts_js()
    forbidden = [
        "/api/chat",
        "/api/chat/start",
        "send(",
        "sendMessage",
        "startChat",
        "submitChat",
        "window.send",
        "/api/kanban/",
        "/api/kanban/tasks",
        "/api/kanban/dispatch",
        "createKanbanTask",
        "updateKanbanTask",
        "addKanbanComment",
        "runKanbanDispatcher",
        "nudgeKanbanDispatcher",
        "/api/cron",
        "/api/crons",
        "/api/goal",
        "/api/background",
        "/background",
        "background",
        "setInterval",
        "setTimeout",
        "EventSource",
        "WebSocket",
        "Worker(",
        "navigator.sendBeacon",
        "/api/memory/write",
        "/api/skills/save",
        "/api/skills/delete",
        "/api/skills/toggle",
        "/apply",
        "would_execute:true",
        "fetch(",
        "XMLHttpRequest",
        "http://",
        "https://",
        "POST",
        "PATCH",
        "DELETE",
        ".innerHTML",
        ".outerHTML",
        "insertAdjacentHTML",
        "renderMd(",
        "renderMarkdown(",
        "marked(",
        "markdownToHtml",
        "eval(",
        "Function(",
    ]
    for token in forbidden:
        assert token not in js


def _function_section(js, function_name):
    start = js.index(f"function {function_name}")
    match = re.search(r"\nfunction\s+", js[start + 1 :])
    if not match:
        return js[start:]
    return js[start : start + 1 + match.start()]


def test_docs_artifacts_preview_is_explicit_open_only():
    js = _docs_artifacts_js()
    open_body = _function_section(js, "openOperatorDocsArtifactPreview")

    assert "OPERATOR_DOCS_ARTIFACTS_OPEN_ENDPOINT" in open_body
    assert re.search(r"\bapi\s*\(", open_body)
    assert "URLSearchParams" in open_body or "encodeURIComponent" in open_body
    assert "disabled" in js
    assert "preview_available" in js

    for function_name in [
        "renderOperatorDocsArtifacts",
        "refreshOperatorDocsArtifacts",
        "initOperatorDocsArtifacts",
    ]:
        body = _function_section(js, function_name)
        assert "OPERATOR_DOCS_ARTIFACTS_OPEN_ENDPOINT" not in body
        assert "/api/operator/docs-artifacts/open" not in body


def test_docs_artifacts_preview_renders_bounded_text_as_plain_text():
    js = _docs_artifacts_js()

    assert "document.createElement('pre')" in js or 'document.createElement("pre")' in js
    assert ".textContent" in js
    for required in ["truncated", "bytes_read", "max_bytes", "format", "preview.text"]:
        assert required in js
    for forbidden in [".innerHTML", "renderMd(", "renderMarkdown(", "marked(", "markdownToHtml"]:
        assert forbidden not in js


def test_docs_artifacts_preview_handles_unknown_or_malformed_without_fake_content():
    js = _docs_artifacts_js()
    preview_body = _function_section(js, "_operatorDocsArtifactsRenderPreview")

    for required in ["status", "unknown", "issues", "metadata-only", "malformed"]:
        assert required in preview_body
    for fake_copy in ["sample preview", "example artifact", "demo preview", "fake preview"]:
        assert fake_copy not in js.lower()


def _run_docs_artifacts_node(test_body):
    root = Path(__file__).resolve().parents[1]
    node_script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');

const source = fs.readFileSync('static/operator_docs_artifacts.js', 'utf8');

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
    this.attributes = {};
    this.hidden = false;
    this.disabled = false;
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
  setAttribute(name, value) {
    this.attributes[name] = String(value);
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
  'operatorDocsArtifactsInput',
  'operatorDocsArtifactsKind',
  'operatorDocsArtifactsRoot',
  'operatorDocsArtifactsChip',
  'operatorDocsArtifactsLabel',
  'operatorDocsArtifactsPopover',
  'operatorDocsArtifactsStatus',
  'operatorDocsArtifactsList',
  'operatorDocsArtifactsPreview',
].forEach(id => register(id));
elements.get('operatorDocsArtifactsKind').value = 'all';
elements.get('operatorDocsArtifactsRoot').value = 'all';
elements.get('operatorDocsArtifactsPopover').hidden = false;

const document = {
  getElementById(id) {
    return elements.get(id) || null;
  },
  createElement(tagName) {
    return new Element('', tagName);
  },
};

const apiCalls = [];
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return {promise, resolve, reject};
}

const context = {
  console,
  document,
  URLSearchParams,
  Date,
  Promise,
  api(url) {
    const d = deferred();
    apiCalls.push({url, resolve: d.resolve, reject: d.reject, promise: d.promise});
    return d.promise;
  },
};
context.window = context;
context.$ = id => document.getElementById(id);

vm.createContext(context);
vm.runInContext(source, context, {filename: 'operator_docs_artifacts.js'});

function item(id, title) {
  return {
    id,
    title,
    kind: 'plan',
    display_path: 'docs/' + id + '.md',
    root_id: 'docs',
    relative_path: id + '.md',
    preview_available: true,
  };
}

function previewPayload(itemValue, text) {
  return {
    status: 'live',
    item: itemValue,
    preview: {
      format: 'text',
      text,
      truncated: false,
      bytes_read: typeof text === 'string' ? text.length : 1,
      max_bytes: 24000,
    },
    issues: [],
    would_execute: false,
  };
}

(async () => {
__TEST_BODY__
})().catch(error => {
  console.error(error && error.stack || error);
  process.exit(1);
});
""".replace("__TEST_BODY__", test_body)
    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_docs_artifacts_preview_ignores_stale_response_when_newer_item_resolves_first():
    _run_docs_artifacts_node(r"""
const itemA = item('a', 'Artifact A');
const itemB = item('b', 'Artifact B');
const promiseA = context.openOperatorDocsArtifactPreview(itemA);
const promiseB = context.openOperatorDocsArtifactPreview(itemB);
assert.equal(apiCalls.length, 2);
assert.match(apiCalls[0].url, /\/api\/operator\/docs-artifacts\/open\?id=a/);
assert.match(apiCalls[1].url, /\/api\/operator\/docs-artifacts\/open\?id=b/);
apiCalls[1].resolve(previewPayload(itemB, 'B body'));
await promiseB;
assert.match(elements.get('operatorDocsArtifactsPreview').textContent, /B body/);
apiCalls[0].resolve(previewPayload(itemA, 'A body'));
await promiseA;
const text = elements.get('operatorDocsArtifactsPreview').textContent;
assert.match(text, /B body/, 'newer preview must remain visible');
assert.doesNotMatch(text, /A body/, 'stale older preview must not overwrite newer preview');
""")


def test_docs_artifacts_preview_response_after_list_render_is_ignored():
    _run_docs_artifacts_node(r"""
const itemC = item('c', 'Artifact C');
const promiseC = context.openOperatorDocsArtifactPreview(itemC);
assert.equal(apiCalls.length, 1);
context.renderOperatorDocsArtifacts({
  status: 'live',
  summary: 'manual refresh reset',
  sources: [],
  items: [],
  issues: [],
  count: 0,
  would_execute: false,
});
apiCalls[0].resolve(previewPayload(itemC, 'C body'));
await promiseC;
const text = elements.get('operatorDocsArtifactsPreview').textContent;
assert.doesNotMatch(text, /C body/, 'preview response from before list render must be ignored');
assert.match(text, /Select an available item preview|status: unknown/);
""")


def test_docs_artifacts_preview_rejects_non_string_body_without_object_coercion():
    _run_docs_artifacts_node(r"""
const itemD = item('d', 'Artifact D');
const promiseD = context.openOperatorDocsArtifactPreview(itemD);
assert.equal(apiCalls.length, 1);
apiCalls[0].resolve(previewPayload(itemD, {not: 'plain text'}));
await promiseD;
const text = elements.get('operatorDocsArtifactsPreview').textContent;
assert.doesNotMatch(text, /\[object Object\]/);
assert.match(text, /malformed|No preview text|Metadata-only/i);
""")
