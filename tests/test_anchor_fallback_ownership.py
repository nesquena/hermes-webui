"""Anchor fallback ownership guards for the settled activity scene.

The Stable Assistant Turn Anchor should own settled activity when a message has
`_anchor_activity_scene`. Raw `content[]` ordering and legacy settled tool-card
rebuilds are still required for historical/non-anchor transcripts, but they must
exit before competing with anchor-owned turns.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = ROOT / "static" / "ui.js"
PHASE0_DOC_PATH = (
    ROOT / "docs" / "architecture" / "stable-assistant-turn-anchor-phase0.md"
)


def _read_required_text(path: Path, label: str) -> str:
    assert path.exists(), f"{label} not found at {path}"
    return path.read_text(encoding="utf-8")


def _ui_js() -> str:
    return _read_required_text(UI_JS_PATH, "static/ui.js")


def _phase0_doc() -> str:
    return _read_required_text(
        PHASE0_DOC_PATH,
        "Stable Assistant Turn Anchors Phase 0 inventory",
    )


def _run_node_script(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node executable is required for JavaScript behavior checks")
    try:
        result = subprocess.run(
            [node, "-e", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            "node behavior check timed out"
            f"\nstdout:\n{exc.stdout or '<empty>'}"
            f"\nstderr:\n{exc.stderr or '<empty>'}",
        )
    if result.returncode:
        pytest.fail(
            "node behavior check failed"
            f"\nexit code: {result.returncode}"
            f"\nstdout:\n{result.stdout or '<empty>'}"
            f"\nstderr:\n{result.stderr or '<empty>'}",
        )
    stdout_lines = [line for line in result.stdout.splitlines() if line.strip()]
    return stdout_lines[-1] if stdout_lines else ""


def _is_js_identifier_char(char: str) -> bool:
    return char.isalnum() or char in {"_", "$"}


def _previous_significant_js_token(src: str, idx: int) -> str:
    idx -= 1
    while idx >= 0 and src[idx].isspace():
        idx -= 1
    if idx < 0:
        return ""
    if _is_js_identifier_char(src[idx]):
        end = idx + 1
        while idx >= 0 and _is_js_identifier_char(src[idx]):
            idx -= 1
        return src[idx + 1 : end]
    return src[idx]


def _looks_like_js_regex_literal_start(src: str, idx: int) -> bool:
    if src[idx] != "/" or src.startswith(("//", "/*"), idx):
        return False
    previous = _previous_significant_js_token(src, idx)
    if previous in {"return", "throw", "case", "delete", "typeof", "void", "yield"}:
        return True
    return previous in {
        "",
        "(",
        "[",
        "{",
        "=",
        ":",
        ",",
        ";",
        "!",
        "?",
        "&",
        "|",
        "+",
        "-",
        "*",
        "%",
        "^",
        "~",
        "<",
        ">",
    }


def _skip_js_regex_literal(src: str, idx: int) -> int:
    assert src[idx] == "/", f"expected regex literal at {idx}"
    idx += 1
    in_class = False
    while idx < len(src):
        if src[idx] == "\\":
            idx += 2
            continue
        if src[idx] == "[":
            in_class = True
        elif src[idx] == "]":
            in_class = False
        elif src[idx] == "/" and not in_class:
            idx += 1
            while idx < len(src) and src[idx].isalpha():
                idx += 1
            return idx
        idx += 1
    raise AssertionError("JavaScript regex literal did not close")


def _skip_js_string_or_comment(src: str, idx: int) -> int:
    if src.startswith("//", idx):
        end = src.find("\n", idx + 2)
        return len(src) if end == -1 else end + 1
    if src.startswith("/*", idx):
        end = src.find("*/", idx + 2)
        assert end != -1, "JavaScript block comment did not close"
        return end + 2
    if _looks_like_js_regex_literal_start(src, idx):
        return _skip_js_regex_literal(src, idx)
    quote = src[idx]
    if quote == "`":
        return _skip_js_template_literal(src, idx)
    if quote not in {"'", '"'}:
        return idx
    idx += 1
    while idx < len(src):
        if src[idx] == "\\":
            idx += 2
            continue
        if src[idx] == quote:
            return idx + 1
        idx += 1
    raise AssertionError(f"JavaScript string literal {quote!r} did not close")


def _skip_js_template_literal(src: str, idx: int) -> int:
    assert src[idx] == "`", f"expected template literal at {idx}"
    idx += 1
    while idx < len(src):
        if src[idx] == "\\":
            idx += 2
            continue
        if src[idx] == "`":
            return idx + 1
        if src.startswith("${", idx):
            expression_close = _matching_delimiter(src, idx + 1, "{", "}")
            idx = expression_close + 1
            continue
        idx += 1
    raise AssertionError("JavaScript template literal did not close")


def _matching_delimiter(src: str, open_idx: int, opener: str, closer: str) -> int:
    assert src[open_idx] == opener, f"expected {opener!r} at {open_idx}"
    depth = 0
    idx = open_idx
    while idx < len(src):
        next_idx = _skip_js_string_or_comment(src, idx)
        if next_idx != idx:
            idx = next_idx
            continue
        if src[idx] == opener:
            depth += 1
        elif src[idx] == closer:
            depth -= 1
            if depth == 0:
                return idx
        idx += 1
    raise AssertionError(f"{opener}{closer} delimiter did not close")


def _skip_js_whitespace(src: str, idx: int) -> int:
    while idx < len(src) and src[idx].isspace():
        idx += 1
    return idx


def _find_function_declaration(src: str, name: str) -> tuple[int, int]:
    idx = 0
    while idx < len(src):
        next_idx = _skip_js_string_or_comment(src, idx)
        if next_idx != idx:
            idx = next_idx
            continue
        if not src.startswith("function", idx):
            idx += 1
            continue
        before = src[idx - 1] if idx else ""
        after_keyword = idx + len("function")
        after = src[after_keyword] if after_keyword < len(src) else ""
        if _is_js_identifier_char(before) or _is_js_identifier_char(after):
            idx += 1
            continue
        name_start = _skip_js_whitespace(src, after_keyword)
        name_end = name_start + len(name)
        if src[name_start:name_end] != name:
            idx += 1
            continue
        after_name = src[name_end] if name_end < len(src) else ""
        if _is_js_identifier_char(after_name):
            idx += 1
            continue
        params_open = _skip_js_whitespace(src, name_end)
        if params_open < len(src) and src[params_open] == "(":
            return idx, params_open
        idx += 1
    raise AssertionError(f"{name} not found")


def _function_source(src: str, name: str) -> str:
    start, params_open = _find_function_declaration(src, name)
    params_close = _matching_delimiter(src, params_open, "(", ")")
    brace = src.find("{", params_close)
    assert brace != -1, f"{name} body not found"
    close = _matching_delimiter(src, brace, "{", "}")
    return src[start : close + 1]


def _function_body(src: str, name: str) -> str:
    _, params_open = _find_function_declaration(src, name)
    params_close = _matching_delimiter(src, params_open, "(", ")")
    brace = src.find("{", params_close)
    assert brace != -1, f"{name} body not found"
    close = _matching_delimiter(src, brace, "{", "}")
    return src[brace + 1 : close]


def test_phase0_doc_records_settled_fallback_ownership_matrix():
    doc = _phase0_doc()

    assert "### Settled Fallback Ownership Matrix" in doc
    assert "_anchor_activity_scene` is the semantic" in doc
    assert "| Settled Compact Worklog activity |" in doc
    assert "| Settled Transparent Stream activity |" in doc
    assert "| Historical / non-anchor transcripts |" in doc
    assert "This matrix is the current settled-render contract" in doc
    assert "compatibility-only rebuilds" in doc
    assert "explicit raw transcript indexes" in doc


def test_function_extractor_handles_nested_template_literal_interpolation():
    source = """
    function sample(){
      const text=`outer ${condition ? `inner ${value}` : { fallback: true }}`;
      if(anchorOwnedAssistantRawIdxs.has(rawIdx)) return;
    }
    function afterSample(){ return false; }
    """

    body = _function_body(source, "sample")

    assert "if(anchorOwnedAssistantRawIdxs.has(rawIdx)) return;" in body
    assert "function afterSample" not in body


def test_function_extractor_matches_exact_declarations_outside_comments():
    source = """
    // function sample(){ return 'comment'; }
    function samplePrefix(){ return 'prefix'; }
    function sample(){
      const hasBrace = /\\{[^}]+\\}/.test(text);
      return hasBrace ? 'target' : 'fallback';
    }
    function afterSample(){ return false; }
    """

    body = _function_body(source, "sample")

    assert "return hasBrace ? 'target' : 'fallback';" in body
    assert "return 'comment';" not in body
    assert "return 'prefix';" not in body
    assert "function afterSample" not in body


def test_function_extractor_ignores_destructured_parameter_braces():
    source = """
    function sample({ fallback = true }){
      if(anchorOwnedAssistantRawIdxs.has(rawIdx)) return;
    }
    """

    body = _function_body(source, "sample")

    assert body.lstrip().startswith("if(anchorOwnedAssistantRawIdxs.has(rawIdx))")
    assert "fallback = true" not in body


def test_function_extractor_skips_regex_after_binary_operator():
    source = """
    function sample(){
      const matcher = fallback || /\\{[^}]+\\}/;
      if(anchorOwnedAssistantRawIdxs.has(rawIdx)) return;
    }
    """

    body = _function_body(source, "sample")

    assert "const matcher = fallback || /\\{[^}]+\\}/;" in body
    assert "if(anchorOwnedAssistantRawIdxs.has(rawIdx)) return;" in body


def test_transparent_raw_content_helper_is_fallback_only_when_anchor_scene_absent():
    helper = _function_body(_ui_js(), "_transparentStreamOrderedParts")

    transparent_gate = helper.index("!isTransparentStream()) return null;")
    role_gate = helper.index("!message||message.role!=='assistant'||message._live")
    anchor_exit = helper.index("if(message._anchor_activity_scene) return null;")
    content_loop = helper.index("for(const part of message.content)")
    fallback_return = helper.index("return hasText&&hasTool?ordered:null;")

    assert transparent_gate < role_gate < anchor_exit < content_loop < fallback_return
    assert "part.type==='text'" in helper
    assert "part.type==='tool_use'" in helper


def test_transparent_raw_content_fallback_exits_for_anchor_owned_messages():
    helper_source = _function_source(_ui_js(), "_transparentStreamOrderedParts")
    script = textwrap.dedent(
        f"""
        let transparentStream = true;
        function isTransparentStream() {{
          return transparentStream;
        }}

        eval({json.dumps(helper_source)});

        const anchorOwned = {{
          role: 'assistant',
          content: [
            {{ type: 'text', text: 'Checked the repo state.' }},
            {{ type: 'tool_use', id: 'toolu_anchor', name: 'terminal', input: {{ cmd: 'git status' }} }},
          ],
          _anchor_activity_scene: {{
            schema_version: 'activity_scene_v1',
            activity_rows: [],
          }},
        }};
        const historical = {{
          role: 'assistant',
          content: [
            {{ type: 'text', text: 'Checked the repo state.' }},
            {{ type: 'tool_use', id: 'toolu_history', name: 'terminal', input: {{ cmd: 'git status' }} }},
          ],
        }};

        const anchorResult = _transparentStreamOrderedParts(anchorOwned);
        const historicalResult = _transparentStreamOrderedParts(historical);
        transparentStream = false;
        const disabledResult = _transparentStreamOrderedParts(historical);

        console.log(JSON.stringify({{
          anchorResult,
          historicalResult,
          disabledResult,
        }}));
        """
    )

    result = json.loads(_run_node_script(script))

    assert result["anchorResult"] is None
    assert result["disabledResult"] is None
    assert [part["kind"] for part in result["historicalResult"]] == ["text", "tool"]
    assert result["historicalResult"][0]["text"] == "Checked the repo state."
    assert result["historicalResult"][1] == {
        "kind": "tool",
        "toolUseId": "toolu_history",
        "name": "terminal",
        "input": {"cmd": "git status"},
    }


def test_render_messages_keeps_anchor_owned_turn_out_of_legacy_activity_rebuilds():
    """Drive the real renderMessages() gate, not only source-order assertions."""

    render_source = _function_source(_ui_js(), "renderMessages")
    transparent_source = _function_source(_ui_js(), "_transparentStreamOrderedParts")
    legacy_metadata_source = _function_source(
        _ui_js(), "_legacySettledFallbackHasToolMetadata"
    )
    script = textwrap.dedent(
        f"""
        class FakeClassList {{
          constructor(el) {{ this.el = el; }}
          _set() {{ return new Set(String(this.el.className || '').split(/\\s+/).filter(Boolean)); }}
          contains(name) {{ return this._set().has(name); }}
          add(...names) {{
            const set = this._set();
            names.forEach((name) => set.add(name));
            this.el.className = Array.from(set).join(' ');
          }}
          remove(...names) {{
            const set = this._set();
            names.forEach((name) => set.delete(name));
            this.el.className = Array.from(set).join(' ');
          }}
        }}
        class FakeElement {{
          constructor(tag = 'div') {{
            this.tagName = tag.toUpperCase();
            this.children = [];
            this.parentElement = null;
            this.dataset = {{}};
            this.attributes = {{}};
            this.className = '';
            this.id = '';
            this.hidden = false;
            this.innerHTML = '';
            this.style = {{}};
            this.classList = new FakeClassList(this);
          }}
          appendChild(child) {{
            child.parentElement = this;
            this.children.push(child);
            return child;
          }}
          insertBefore(child, ref) {{
            child.parentElement = this;
            const idx = this.children.indexOf(ref);
            if (idx < 0) this.children.push(child);
            else this.children.splice(idx, 0, child);
            return child;
          }}
          remove() {{
            if (!this.parentElement) return;
            const idx = this.parentElement.children.indexOf(this);
            if (idx >= 0) this.parentElement.children.splice(idx, 1);
            this.parentElement = null;
          }}
          setAttribute(name, value) {{
            this.attributes[name] = String(value);
            if (name === 'id') this.id = String(value);
            if (name === 'class') this.className = String(value);
            if (name.startsWith('data-')) this.dataset[dataKey(name)] = String(value);
          }}
          getAttribute(name) {{
            if (name === 'id') return this.id || null;
            if (name === 'class') return this.className || null;
            if (name.startsWith('data-')) {{
              const value = this.dataset[dataKey(name)];
              return value === undefined ? null : String(value);
            }}
            return this.attributes[name] === undefined ? null : this.attributes[name];
          }}
          removeAttribute(name) {{
            delete this.attributes[name];
            if (name.startsWith('data-')) delete this.dataset[dataKey(name)];
          }}
          matches(selector) {{ return matchesSelector(this, selector); }}
          closest(selector) {{
            let node = this;
            while (node) {{
              if (matchesSelector(node, selector)) return node;
              node = node.parentElement;
            }}
            return null;
          }}
          querySelectorAll(selector) {{
            const found = [];
            const visit = (node) => {{
              for (const child of node.children) {{
                if (matchesSelector(child, selector)) found.push(child);
                visit(child);
              }}
            }};
            visit(this);
            return found;
          }}
          querySelector(selector) {{
            return this.querySelectorAll(selector)[0] || null;
          }}
          insertAdjacentHTML() {{}}
        }}
        function dataKey(name) {{
          return String(name).slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
        }}
        function matchesSelector(el, selector) {{
          return String(selector || '').split(',').some((part) => matchesSimple(el, part.trim()));
        }}
        function matchesSimple(el, selector) {{
          if (!selector) return false;
          const negated = [];
          const baseSelector = selector.replace(/:not\\(([^()]*)\\)/g, (_, inner) => {{
            negated.push(String(inner || '').trim());
            return '';
          }}).trim();
          if (negated.some((inner) => inner && matchesSimple(el, inner))) return false;
          if (!baseSelector) return true;
          const classMatches = [...baseSelector.matchAll(/\\.([A-Za-z0-9_-]+)/g)].map((m) => m[1]);
          if (classMatches.some((name) => !el.classList.contains(name))) return false;
          const attrMatches = [...baseSelector.matchAll(/\\[([^=\\]]+)(?:=["']?([^"'\\]]+)["']?)?\\]/g)];
          for (const match of attrMatches) {{
            const value = el.getAttribute(match[1]);
            if (value === null) return false;
            if (match[2] !== undefined && String(value) !== String(match[2])) return false;
          }}
          const idMatch = baseSelector.match(/#([A-Za-z0-9_-]+)/);
          if (idMatch && el.id !== idMatch[1]) return false;
          const tagMatch = baseSelector.match(/^[A-Za-z][A-Za-z0-9_-]*/);
          if (tagMatch && el.tagName.toLowerCase() !== tagMatch[0].toLowerCase()) return false;
          return true;
        }}

        const elements = {{
          msgInner: new FakeElement('div'),
          emptyState: new FakeElement('div'),
        }};
        global.window = {{}};
        global.document = {{
          createElement: (tag) => new FakeElement(tag),
          getElementById: (id) => elements[id] || null,
        }};
        global.performance = {{ now: () => 1 }};
        global.requestAnimationFrame = (fn) => fn();
        global.setTimeout = (fn) => fn();
        function $(id) {{ return elements[id] || null; }}
        function isTransparentStream() {{ return false; }}
        function isCompactWorklogMode() {{ return true; }}
        function isSimplifiedToolCalling() {{ return true; }}
        function t(key) {{ return key; }}
        function li() {{ return ''; }}
        function esc(value) {{ return String(value == null ? '' : value); }}
        function msgContent(message) {{
          if (Array.isArray(message.content)) {{
            return message.content.filter((p) => p && p.type === 'text').map((p) => p.text || p.content || '').join('\\n');
          }}
          return String(message.content || '');
        }}
        let S;
        const INFLIGHT = {{}};
        let _loadingSessionId = null;
        let _messageRenderWindowSid = null;
        let _messageUserUnpinned = false;
        let _programmaticScroll = false;
        let _programmaticScrollSetAt = 0;
        let _sessionHtmlCacheSid = null;
        let _messagesTruncated = false;
        let _oldestIdx = 0;
        const _sessionHtmlCache = new Map();
        const _recycleStash = new Map();
        const _msgNodeRecycleEnabled = false;
        const _recycleResetAttrs = [];
        const _ERR_MSG_RE = /__never__/;

        function _captureMessageScrollSnapshot() {{ return null; }}
        function _resetMessageRenderWindow(sid) {{ _messageRenderWindowSid = sid; }}
        function _latestPreservedCompressionTaskListMessages() {{ return []; }}
        function _getVisibleMessagesWithIdx() {{ return S.messages.map((m, rawIdx) => (m && m.role !== 'tool') ? {{ m, rawIdx }} : null).filter(Boolean); }}
        function _messageVirtualKeepTailCount() {{ return 100; }}
        function _currentMessageVirtualWindow(vis) {{ return {{ virtualized: false, start: 0, end: vis.length, topPad: 0, bottomPad: 0, total: vis.length, tailStart: vis.length }}; }}
        function _messageVirtualWindowKeyFor() {{ return 'all'; }}
        function _messageRenderCacheSignature() {{ return 'sig'; }}
        function _compressionStateForCurrentSession() {{ return null; }}
        function clearCompressionUi() {{}}
        function _handoffStateForCurrentSession() {{ return null; }}
        function _captureWorklogDetailDisclosureState() {{ return null; }}
        function _latestCompressionReferenceMessage() {{ return {{ message: null, rawIdx: -1 }}; }}
        function _shouldShowSettledCompressionReference() {{ return false; }}
        function _applySessionNavigationPrefs() {{}}
        function _messageVirtualSpacer() {{ return new FakeElement('div'); }}
        function _compressionAnchorIndex() {{ return null; }}
        function _assistantTurnFinalVisibleContentMap() {{ return new Map(); }}
        function _assistantTurnVisibleContentMap() {{ return new Map(); }}
        function _isPreservedCompressionTaskListMessage() {{ return false; }}
        function _preservedCompressionTaskListCardsHtml() {{ return ''; }}
        function _isContextCompactionMessage() {{ return false; }}
        function _createAssistantTurn() {{
          const turn = new FakeElement('div');
          turn.className = 'assistant-turn';
          const blocks = new FakeElement('div');
          blocks.className = 'assistant-turn-blocks';
          turn.appendChild(blocks);
          return turn;
        }}
        function _assistantTurnBlocks(turn) {{ return turn ? turn.querySelector('.assistant-turn-blocks') : null; }}
        function _setLatestAssistantTurnLandmark() {{}}
        function _assistantRoleHtml() {{ return ''; }}
        function _userMessageDomId(rawIdx) {{ return `user-${{rawIdx}}`; }}
        function _messageSessionIndexForRawIdx(rawIdx) {{ return rawIdx; }}
        function _messageViewportAnchorKeyForMessage() {{ return 'k'; }}
        function _stripAttachedFilesMarkerForDisplay(value) {{ return String(value || ''); }}
        function _stripWorkspaceDisplayPrefix(value) {{ return String(value || ''); }}
        function _stripLeadingAssistantThinkingMarkup(value) {{ return String(value || ''); }}
        function _getCachedRender(value) {{ return String(value || ''); }}
        function _formatInServerTz() {{ return ''; }}
        function _formatMessageFooterTimestamp() {{ return ''; }}
        function _questionJumpButtonHtml() {{ return ''; }}
        function _formatTurnTps() {{ return ''; }}
        function isTpsDisplayEnabled() {{ return false; }}
        function _renderAttachmentHtml() {{ return ''; }}
        function _isMarkerOnlyAssistantCompressionMessage() {{ return false; }}
        function _isAssistantEmptyPlaceholderContent() {{ return false; }}
        function _assistantTurnAnchorSettledFinalAnswer() {{ return null; }}
        function _worklogReasoningTextFromMessage() {{ return ''; }}
        function _assistantMessageBelongsInWorklog() {{ return false; }}
        function _assistantThinkingBelongsInWorklog() {{ return false; }}
        function _assistantReasoningPayloadText() {{ return ''; }}
        function _statusCardHtml() {{ return ''; }}
        function _collectHandoffSummaryStates() {{ return []; }}
        function _insertCompressionLikeNode() {{}}
        function _handoffCardsNode() {{ return null; }}
        function renderCompressionUi() {{}}
        function _assistantToolAnchorIdxForMessage(messages, rawIdx) {{ return rawIdx; }}
        function _cliToolResultSnippet(value) {{ return String(value || ''); }}
        function _cliPatchSnippetFromArgs() {{ return ''; }}
        function _cliToolCardSnippet(value) {{ return String(value || ''); }}
        function _cliToolCardHasDiffSnippet() {{ return false; }}
        function _toolArgsSnapshot(args) {{ return args || {{}}; }}
        function _worklogReasonHtmlFromAnchor() {{ return ''; }}
        function _normalizeThinkingEchoCompare(value) {{ return String(value || ''); }}
        function _toolWorklogListEl(group) {{ return group; }}
        let legacyCards = [];
        function ensureActivityGroup(parent, opts) {{
          const group = new FakeElement('div');
          group.className = 'tool-worklog-group tool-call-group agent-activity-group';
          group.setAttribute('data-legacy-fallback-owner', '1');
          const anchor = opts && opts.anchor;
          if (parent && anchor && anchor.parentElement === parent) parent.insertBefore(group, anchor);
          else if (parent) parent.appendChild(group);
          return group;
        }}
        function _appendWorklogStep(group, anchor, cards) {{
          for (const card of cards || []) {{
            legacyCards.push({{
              tid: card.tid || card.id || card.tool_call_id || '',
              name: card.name || '',
              snippet: String(card.snippet || ''),
            }});
            const row = new FakeElement('div');
            row.className = 'tool-card-row';
            row.setAttribute('data-tool-id', card.tid || card.id || card.tool_call_id || '');
            group.appendChild(row);
          }}
        }}
        function _syncToolCallGroupSummary() {{}}
        function _restoreWorklogDetailDisclosureState() {{}}
        function _scrollAfterMessageRender() {{}}
        function _maybeRecoverVirtualizedBlankViewport() {{ return false; }}
        function _updateMessageVirtualMeasurements() {{}}
        function postProcessRenderedMessages() {{}}
        function _formatGatewayModelLabel() {{ return ''; }}
        function _gatewayRoutingFailoverText() {{ return ''; }}
        function _gatewayModelWarningText() {{ return ''; }}
        function _formatTurnDuration() {{ return ''; }}
        function _renderSettledAnchorSceneForMessage(message, segment, rawIdx) {{
          const group = new FakeElement('div');
          group.className = 'tool-worklog-group agent-activity-group';
          group.setAttribute('data-anchor-settled-scene-owner', '1');
          const blocks = _assistantTurnBlocks(segment.closest('.assistant-turn'));
          if (blocks) blocks.insertBefore(group, segment);
          return true;
        }}

        eval({json.dumps(transparent_source)});
        eval({json.dumps(legacy_metadata_source)});
        eval({json.dumps(render_source)});

        const toolResult = {{ role: 'tool', tool_call_id: 'toolu_1', content: 'tool result' }};
        const selectorSanityElement = new FakeElement('div');
        selectorSanityElement.className = 'tool-worklog-group anchor-owned';
        selectorSanityElement.setAttribute('data-owner', 'anchor');
        const selectorSanity = {{
          positive: selectorSanityElement.matches('.tool-worklog-group:not(.legacy-owner)[data-owner="anchor"]'),
          negatedClass: selectorSanityElement.matches('.tool-worklog-group:not(.anchor-owned)'),
          negatedAttr: selectorSanityElement.matches('.tool-worklog-group:not([data-owner="anchor"])'),
        }};
        const legacyToolCall = {{
          id: 'toolu_1',
          function: {{ name: 'terminal', arguments: '{{"cmd":"git status"}}' }},
        }};
        const legacyPartial = {{ id: 'partial_1', name: 'terminal', args: {{ cmd: 'pwd' }}, snippet: 'partial result' }};
        const legacyContentTool = {{ type: 'tool_use', id: 'content_1', name: 'terminal', input: {{ cmd: 'ls' }} }};
        const anchorOwned = {{
          role: 'assistant',
          content: [{{ type: 'text', text: 'Anchor final answer' }}, legacyContentTool],
          tool_calls: [legacyToolCall],
          _partial_tool_calls: [legacyPartial],
          _anchor_activity_scene: {{
            version: 'activity_scene_v1',
            activity_rows: [{{ id: 'row1', kind: 'tool', role: 'tool', tool: {{ name: 'terminal' }} }}],
            final_answer: 'Anchor final answer',
          }},
        }};
        S = {{
          session: {{ session_id: 's1', tool_calls: [{{ tid: 'toolu_1', snippet: 'persisted result' }}] }},
          messages: [{{ role: 'user', content: 'run' }}, anchorOwned, toolResult],
          toolCalls: [{{ tid: 'toolu_1', assistant_msg_idx: 1, name: 'terminal', snippet: 'session fallback' }}],
          busy: false,
        }};
        renderMessages();
        const anchorSummary = {{
          anchorGroups: elements.msgInner.querySelectorAll('[data-anchor-settled-scene-owner]').length,
          legacyGroups: elements.msgInner.querySelectorAll('[data-legacy-fallback-owner]').length,
          legacyRows: elements.msgInner.querySelectorAll('.tool-card-row').length,
          legacyCards,
          sToolCalls: S.toolCalls.length,
        }};

        elements.msgInner = new FakeElement('div');
        legacyCards = [];
        const historical = {{
          role: 'assistant',
          content: 'Historical answer',
        }};
        S = {{
          session: {{ session_id: 's2', tool_calls: [{{ tid: 'toolu_1', snippet: 'persisted result' }}] }},
          messages: [{{ role: 'user', content: 'run' }}, historical, toolResult],
          toolCalls: [{{ tid: 'toolu_1', assistant_msg_idx: 1, name: 'terminal', snippet: 'session fallback' }}],
          busy: false,
        }};
        renderMessages();
        const historicalSummary = {{
          anchorGroups: elements.msgInner.querySelectorAll('[data-anchor-settled-scene-owner]').length,
          legacyGroups: elements.msgInner.querySelectorAll('[data-legacy-fallback-owner]').length,
          legacyRows: elements.msgInner.querySelectorAll('.tool-card-row').length,
          legacyCards,
          sToolCalls: S.toolCalls.length,
        }};

        elements.msgInner = new FakeElement('div');
        legacyCards = [];
        const rawHistorical = {{
          role: 'assistant',
          content: [{{ type: 'text', text: 'Historical raw answer' }}, legacyContentTool],
          tool_calls: [legacyToolCall],
          _partial_tool_calls: [legacyPartial],
        }};
        S = {{
          session: {{
            session_id: 's3',
            tool_calls: [{{ tid: 'content_1', snippet: 'persisted content result' }}],
          }},
          messages: [{{ role: 'user', content: 'run' }}, rawHistorical, toolResult],
          toolCalls: [],
          busy: false,
        }};
        renderMessages();
        const rawHistoricalSummary = {{
          anchorGroups: elements.msgInner.querySelectorAll('[data-anchor-settled-scene-owner]').length,
          legacyGroups: elements.msgInner.querySelectorAll('[data-legacy-fallback-owner]').length,
          legacyRows: elements.msgInner.querySelectorAll('.tool-card-row').length,
          legacyCards,
          sToolCalls: S.toolCalls.length,
        }};

        elements.msgInner = new FakeElement('div');
        legacyCards = [];
        const duplicateAnchorTool = {{ type: 'tool_use', id: 'toolu_anchor_dup', name: 'terminal', input: {{ cmd: 'anchor' }} }};
        const duplicateHistoricalTool = {{ type: 'tool_use', id: 'toolu_hist_dup', name: 'terminal', input: {{ cmd: 'history' }} }};
        const duplicateAnchorCall = {{
          id: 'toolu_anchor_dup',
          function: {{ name: 'terminal', arguments: '{{"cmd":"anchor"}}' }},
        }};
        const duplicateAnchorOwned = {{
          role: 'assistant',
          content: [{{ type: 'text', text: 'Duplicate answer' }}, duplicateAnchorTool],
          tool_calls: [duplicateAnchorCall],
          _anchor_activity_scene: {{
            version: 'activity_scene_v1',
            activity_rows: [{{ id: 'row-dup', kind: 'tool', role: 'tool', tool: {{ name: 'terminal' }} }}],
            final_answer: 'Duplicate answer',
          }},
        }};
        const duplicateHistorical = {{
          role: 'assistant',
          content: [{{ type: 'text', text: 'Duplicate answer' }}, duplicateHistoricalTool],
        }};
        S = {{
          session: {{
            session_id: 's4',
            tool_calls: [{{ tid: 'toolu_hist_dup', snippet: 'historical persisted result' }}],
          }},
          messages: [
            {{ role: 'user', content: 'anchor turn' }},
            duplicateAnchorOwned,
            {{ role: 'user', content: 'historical turn' }},
            duplicateHistorical,
            toolResult,
          ],
          toolCalls: [{{ tid: 'toolu_anchor_dup', assistant_msg_idx: 1, name: 'terminal', snippet: 'anchor session fallback' }}],
          busy: false,
        }};
        renderMessages();
        const duplicateReferenceSummary = {{
          anchorGroups: elements.msgInner.querySelectorAll('[data-anchor-settled-scene-owner]').length,
          legacyGroups: elements.msgInner.querySelectorAll('[data-legacy-fallback-owner]').length,
          legacyRows: elements.msgInner.querySelectorAll('.tool-card-row').length,
          legacyCards,
          sToolCalls: S.toolCalls.length,
        }};

        console.log(JSON.stringify({{
          selectorSanity,
          anchorSummary,
          historicalSummary,
          rawHistoricalSummary,
          duplicateReferenceSummary,
        }}));
        """
    )

    result = json.loads(_run_node_script(script))

    assert result["selectorSanity"] == {
        "positive": True,
        "negatedClass": False,
        "negatedAttr": False,
    }
    assert result["anchorSummary"] == {
        "anchorGroups": 1,
        "legacyGroups": 0,
        "legacyRows": 0,
        "legacyCards": [],
        "sToolCalls": 1,
    }
    assert result["historicalSummary"]["anchorGroups"] == 0
    assert result["historicalSummary"]["legacyGroups"] == 1
    assert result["historicalSummary"]["legacyRows"] >= 1
    assert result["historicalSummary"]["sToolCalls"] >= 1
    assert [card["tid"] for card in result["historicalSummary"]["legacyCards"]] == [
        "toolu_1"
    ]

    raw_cards = result["rawHistoricalSummary"]["legacyCards"]
    raw_tids = {card["tid"] for card in raw_cards}
    raw_snippets = {card["tid"]: card["snippet"] for card in raw_cards}
    assert result["rawHistoricalSummary"]["anchorGroups"] == 0
    assert result["rawHistoricalSummary"]["legacyGroups"] == 1
    assert result["rawHistoricalSummary"]["legacyRows"] >= 3
    assert result["rawHistoricalSummary"]["sToolCalls"] >= 3
    assert {"toolu_1", "partial_1", "content_1"}.issubset(raw_tids)
    assert raw_snippets["toolu_1"] == "tool result"
    assert raw_snippets["partial_1"] == "partial result"
    assert raw_snippets["content_1"] == "persisted content result"

    duplicate_cards = result["duplicateReferenceSummary"]["legacyCards"]
    assert result["duplicateReferenceSummary"]["anchorGroups"] == 1
    assert result["duplicateReferenceSummary"]["legacyGroups"] == 1
    assert result["duplicateReferenceSummary"]["legacyRows"] >= 1
    assert result["duplicateReferenceSummary"]["sToolCalls"] >= 1
    assert {card["tid"] for card in duplicate_cards} == {"toolu_hist_dup"}
    assert "toolu_anchor_dup" not in {card["tid"] for card in duplicate_cards}
    assert duplicate_cards[0]["snippet"] == "historical persisted result"


def test_settled_legacy_tool_rebuild_excludes_anchor_owned_turns():
    render = _function_body(_ui_js(), "renderMessages")

    set_decl = render.index("const anchorOwnedAssistantRawIdxs=new Set();")
    collect_segments = render.index("turn.querySelectorAll('.assistant-segment[data-msg-idx]')")
    metadata_scan = render.index("const hasMessageToolMetadata=")
    fallback_sources = render.index("const fallbackToolSources=[];")
    source_collect = render.index("fallbackToolSources.push({m,rawIdx});")

    assert set_decl < collect_segments < metadata_scan < fallback_sources < source_collect
    assert "S.messages.indexOf(m)" not in render
    assert "S.messages.some((m,rawIdx)=>" in render
    assert "!anchorOwnedAssistantRawIdxs.has(rawIdx)&&_legacySettledFallbackHasToolMetadata(m)" in render
    assert "if(anchorOwnedAssistantRawIdxs.has(rawIdx)) return;" in render


def test_settled_legacy_activity_buckets_skip_anchor_owned_turns_before_rendering():
    render = _function_body(_ui_js(), "renderMessages")

    tool_loop = render.index("for(const tc of (S.toolCalls||[])){")
    tool_skip = render.index("if(anchorOwnedAssistantRawIdxs.has(aIdx)) continue;", tool_loop)
    thinking_loop = render.index("for(const aIdx of assistantThinking.keys()){")
    thinking_skip = render.index("if(anchorOwnedAssistantRawIdxs.has(aIdx)) continue;", thinking_loop)
    worklog_loop = render.index("for(const [aIdx,seg] of assistantSegments){")
    worklog_skip = render.index("if(anchorOwnedAssistantRawIdxs.has(aIdx)) continue;", worklog_loop)
    anchor_render = render.index("_renderSettledAnchorSceneForMessage(msg, seg, rawIdx)")

    assert tool_loop < tool_skip < thinking_loop < thinking_skip < worklog_loop < worklog_skip
    assert worklog_skip < anchor_render


def test_anchor_settled_renderers_remain_the_primary_scene_path():
    settled = _function_body(_ui_js(), "_renderSettledAnchorSceneForMessage")
    transparent = _function_body(
        _ui_js(),
        "_renderSettledAnchorSceneTransparentForMessage",
    )

    assert "if(!message||!message._anchor_activity_scene||!segment) return false;" in settled
    assert "return _renderSettledAnchorSceneTransparentForMessage(message,segment,rawIdx);" in settled
    assert "_anchorSceneRowsForRendering(scene,{settled:true})" in settled
    assert "group.setAttribute('data-anchor-settled-scene-owner','1');" in settled

    assert "if(!message||!message._anchor_activity_scene||!segment) return false;" in transparent
    assert "_anchorSceneRowsForRendering(scene,{settled:true})" in transparent
    assert "_anchorSceneTransparentNodeForRow(row,{settled:true,finalAnswer})" in transparent
