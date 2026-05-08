"""Regression coverage for issue #1937 session navigation race."""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = REPO_ROOT / "static" / "sessions.js"
NODE = shutil.which("node")


pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node_probe(script: str) -> dict:
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"node probe failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_start_jump_waits_for_inflight_endless_scroll_before_full_history_load():
    """A pending older-page prefetch must not prepend duplicates after Start loads all messages."""
    sessions_path = json.dumps(str(SESSIONS_JS))
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const source = fs.readFileSync({sessions_path}, 'utf8');
        const start = source.indexOf('let _messagesTruncated = false;');
        const end = source.indexOf('let _allSessions = [];', start);
        if (start < 0 || end < 0) throw new Error('unable to locate message loading block');
        const block = source.slice(start, end) + `
          globalThis.__raceProbe = {{
            loadOlder: _loadOlderMessages,
            ensureAll: _ensureAllMessagesLoaded,
            setTruncated(v) {{ _messagesTruncated = v; }},
            getTruncated() {{ return _messagesTruncated; }},
            setOldestIdx(v) {{ _oldestIdx = v; }},
            getOldestIdx() {{ return _oldestIdx; }},
            getLoadingOlder() {{ return _loadingOlder; }},
          }};
        `;

        let olderResolve;
        const apiCalls = [];
        const firstPage = Array.from({{length: 30}}, (_, i) => ({{ role: i % 2 ? 'assistant' : 'user', content: 'msg-' + i }}));
        const olderMessages = Array.from({{length: 30}}, (_, i) => ({{ role: i % 2 ? 'assistant' : 'user', content: 'msg-' + (i + 30) }}));
        const tailMessages = Array.from({{length: 30}}, (_, i) => ({{ role: i % 2 ? 'assistant' : 'user', content: 'msg-' + (i + 60) }}));
        const fullMessages = [...firstPage, ...olderMessages, ...tailMessages];
        const context = {{
          S: {{
            session: {{ session_id: 'race-session', message_count: 90 }},
            messages: [...tailMessages],
            toolCalls: [],
          }},
          window: {{}},
          console,
          MESSAGE_RENDER_WINDOW_DEFAULT: 30,
          _loadingSessionId: null,
          _messageRenderWindowSize: 30,
          _programmaticScroll: false,
          _scrollPinned: true,
          api: (url) => {{
            apiCalls.push(url);
            if (url.includes('msg_before=')) {{
              return new Promise(resolve => {{ olderResolve = resolve; }});
            }}
            return Promise.resolve({{
              session: {{ messages: fullMessages, message_count: 90, _messages_truncated: false, _messages_offset: 0 }}
            }});
          }},
          $: () => ({{ scrollHeight: 1000, scrollTop: 10 }}),
          renderMessages: () => {{}},
          setTimeout,
          requestAnimationFrame: (fn) => setTimeout(fn, 0),
          msgContent: (m) => m && m.content,
          _currentMessageRenderWindowSize: () => 30,
          _isContextCompactionMessage: () => false,
          _isPreservedCompressionTaskListMessage: () => false,
          _messageHasReasoningPayload: () => false,
        }};
        vm.createContext(context);
        vm.runInContext(block, context);
        const probe = context.__raceProbe;
        probe.setTruncated(true);
        probe.setOldestIdx(60);

        (async () => {{
          const loadPromise = probe.loadOlder();
          await Promise.resolve();
          if (!olderResolve) throw new Error('older prefetch did not start');
          const ensurePromise = probe.ensureAll();
          await Promise.resolve();
          const fullCalledBeforeOlderSettled = apiCalls.some(url => !url.includes('msg_before='));

          olderResolve({{
            session: {{ messages: olderMessages, message_count: 90, _messages_truncated: true, _messages_offset: 30 }}
          }});
          await loadPromise;
          await ensurePromise;

          const contents = context.S.messages.map(m => m.content);
          const uniqueContents = new Set(contents);
          console.log(JSON.stringify({{
            fullCalledBeforeOlderSettled,
            messageCount: context.S.messages.length,
            uniqueCount: uniqueContents.size,
            first: contents[0],
            last: contents[contents.length - 1],
            apiCalls,
            loadingOlder: probe.getLoadingOlder(),
            truncated: probe.getTruncated(),
            oldestIdx: probe.getOldestIdx(),
          }}));
        }})().catch(err => {{
          console.error(err && err.stack || err);
          process.exit(1);
        }});
        """
    )
    out = _run_node_probe(script)
    assert out["fullCalledBeforeOlderSettled"] is False, (
        "Start-jump full-history load must wait for the in-flight endless-scroll "
        "prefetch to settle before issuing its own full-history request."
    )
    assert out["messageCount"] == 90
    assert out["uniqueCount"] == 90
    assert out["first"] == "msg-0"
    assert out["last"] == "msg-89"
    assert out["loadingOlder"] is False
    assert out["truncated"] is False
    assert out["oldestIdx"] == 0
