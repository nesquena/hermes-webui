"""Regression coverage for historical tool-call reasoning fragments.

PR #5634 follow-up: Codex/OpenAI state can persist intermediate assistant
messages whose visible content is empty, but which carry both tool_calls and a
small reasoning fragment ("need to get a readback", "on the", "PR", ...).
Those rows are model/tool-call anchors, not standalone historical Thinking
cards. Rendering the raw reasoning on every anchor turns one thought into a
stack of fragmented Thinking cards.
"""
from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS = REPO_ROOT / "static" / "ui.js"


def _node_harness() -> str:
    return textwrap.dedent(
        r"""
        const fs = require('fs');
        const vm = require('vm');
        const src = fs.readFileSync('static/ui.js', 'utf8');

        function extractFunction(name) {
          const marker = `function ${name}`;
          const start = src.indexOf(marker);
          if (start < 0) throw new Error(`missing ${name}`);
          const brace = src.indexOf('{', start);
          if (brace < 0) throw new Error(`missing brace for ${name}`);
          let depth = 0;
          let inString = null;
          let escaped = false;
          let inLineComment = false;
          let inBlockComment = false;
          for (let i = brace; i < src.length; i++) {
            const ch = src[i];
            const next = src[i + 1];
            if (inLineComment) {
              if (ch === '\n') inLineComment = false;
              continue;
            }
            if (inBlockComment) {
              if (ch === '*' && next === '/') { inBlockComment = false; i++; }
              continue;
            }
            if (inString) {
              if (escaped) { escaped = false; continue; }
              if (ch === '\\') { escaped = true; continue; }
              if (ch === inString) inString = null;
              continue;
            }
            if (ch === '/' && next === '/') { inLineComment = true; i++; continue; }
            if (ch === '/' && next === '*') { inBlockComment = true; i++; continue; }
            if (ch === '"' || ch === "'" || ch === '`') { inString = ch; continue; }
            if (ch === '{') depth++;
            if (ch === '}') {
              depth--;
              if (depth === 0) return src.slice(start, i + 1);
            }
          }
          throw new Error(`unterminated ${name}`);
        }

        global.window = {};
        const helperSource = [
          'function _assistantMessageHasVisibleContent(m){ return !!(m && m.visible); }',
          extractFunction('_assistantMessageHasToolMetadata'),
          extractFunction('_assistantMessageHasVisibleTextBody'),
          extractFunction('_assistantReasoningPayloadIsHistoricalToolCallOnly'),
          extractFunction('_messageHasReasoningPayload'),
          extractFunction('_assistantReasoningPayloadText'),
          extractFunction('_assistantToolAnchorIdxForMessage'),
          'module.exports = { _assistantMessageHasToolMetadata, _assistantReasoningPayloadIsHistoricalToolCallOnly, _messageHasReasoningPayload, _assistantReasoningPayloadText, _assistantToolAnchorIdxForMessage };',
        ].join('\n');
        const sandbox = { module: { exports: {} }, window: global.window };
        vm.runInNewContext(helperSource, sandbox, { filename: 'ui-helper-harness.js' });
        const {
          _assistantMessageHasToolMetadata,
          _assistantReasoningPayloadIsHistoricalToolCallOnly,
          _messageHasReasoningPayload,
          _assistantReasoningPayloadText,
          _assistantToolAnchorIdxForMessage,
        } = sandbox.module.exports;

        function assert(condition, message) {
          if (!condition) throw new Error(message);
        }

        const historicalToolCallReasoning = {
          role: 'assistant',
          content: '',
          reasoning: 'need to get a readback',
          tool_calls: [{ id: 'call_1', function: { name: 'terminal', arguments: '{}' } }],
        };
        assert(_assistantMessageHasToolMetadata(historicalToolCallReasoning) === true,
          'tool-call anchor must still be recognized');
        assert(_assistantReasoningPayloadIsHistoricalToolCallOnly(historicalToolCallReasoning) === true,
          'historical empty tool-call row should be classified as tool-call-only');
        assert(_messageHasReasoningPayload(historicalToolCallReasoning) === true,
          'reasoning without explicit fragment provenance must remain visible');
        assert(_assistantReasoningPayloadText(historicalToolCallReasoning) === 'need to get a readback',
          'tool ownership alone must not suppress distinct historical reasoning');
        assert(_assistantToolAnchorIdxForMessage([historicalToolCallReasoning], 0) === 0,
          'suppressing Thinking must not move tool-card anchoring off the tool-call row');

        const finalReasoning = {
          role: 'assistant',
          content: 'Final answer',
          reasoning: 'compact thought',
        };
        assert(_messageHasReasoningPayload(finalReasoning) === true,
          'normal assistant reasoning remains visible when Thinking is enabled');
        assert(_assistantReasoningPayloadText(finalReasoning) === 'compact thought',
          'normal assistant reasoning text is preserved');

        const liveToolCallReasoning = {
          role: 'assistant',
          content: '',
          reasoning: 'live thought',
          tool_calls: [{ id: 'call_live', function: { name: 'terminal', arguments: '{}' } }],
          _live: true,
        };
        assert(_assistantReasoningPayloadText(liveToolCallReasoning) === 'live thought',
          'live tool-call reasoning remains visible');

        const anchorSceneReasoning = {
          role: 'assistant',
          content: '',
          reasoning: 'recovered activity thought',
          tool_calls: [{ id: 'call_scene', function: { name: 'terminal', arguments: '{}' } }],
          _anchor_activity_scene: { rows: [] },
        };
        assert(_assistantReasoningPayloadText(anchorSceneReasoning) === 'recovered activity thought',
          'anchor-scene recovered reasoning remains visible');

        const substantiveHistoricalToolReasoning = {
          role: 'assistant',
          content: '',
          reasoning: 'The persisted session owns the final answer, so the stale worker must not overwrite it.',
          tool_calls: [{ id: 'call_substantive', function: { name: 'read_file', arguments: '{}' } }],
        };
        assert(_messageHasReasoningPayload(substantiveHistoricalToolReasoning) === true,
          'substantive historical tool reasoning must remain in the Worklog');
        assert(_assistantReasoningPayloadText(substantiveHistoricalToolReasoning) === substantiveHistoricalToolReasoning.reasoning,
          'substantive historical tool reasoning text must be preserved exactly');

        console.log(JSON.stringify({ok: true}));
        """
    )


def test_historical_tool_call_reasoning_without_provenance_remains_visible():
    result = subprocess.run(
        ["node", "-e", _node_harness()],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout.strip())["ok"] is True
