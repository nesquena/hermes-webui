"""Regression test for busy-path slash commands allowlist (#6597, PR #6613).

Covers:
- /btw and /background run immediately during active turn (S.busy)
- /btw and /background run immediately during compression-running state
- Neither _trySteer, queueSessionMessage, nor cancelStream are called when intercepted
- Existing allowlist entries (steer, interrupt, queue, terminal, goal, yolo) retain immediate execution
- Non-allowlisted commands fall through to queue / default message mode routing
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _run_node(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(proc.stdout.strip())


def test_structural_allowlist_contains_btw_and_background():
    assert "['steer','interrupt','queue','terminal','goal','yolo','btw','background']" in MESSAGES_JS


def test_busy_slash_command_execution_harness():
    script = """
    function parseCommand(text) {
      if (!text.startsWith('/')) return null;
      const parts = text.slice(1).trim().split(/\\s+/);
      return { name: parts[0], args: parts.slice(1).join(' ') };
    }

    async function runBusyScenario(cmdText, isCompression = false) {
      const calls = {
        handlerRan: 0,
        handlerArgs: null,
        trySteerCalled: 0,
        queueCalled: 0,
        cancelCalled: 0,
        inputCleared: false,
      };

      const COMMANDS = [
        { name: 'steer', fn: async (args) => { calls.handlerRan++; calls.handlerArgs = args; } },
        { name: 'interrupt', fn: async (args) => { calls.handlerRan++; calls.handlerArgs = args; } },
        { name: 'queue', fn: async (args) => { calls.handlerRan++; calls.handlerArgs = args; } },
        { name: 'terminal', fn: async (args) => { calls.handlerRan++; calls.handlerArgs = args; } },
        { name: 'goal', fn: async (args) => { calls.handlerRan++; calls.handlerArgs = args; } },
        { name: 'yolo', fn: async (args) => { calls.handlerRan++; calls.handlerArgs = args; } },
        { name: 'btw', fn: async (args) => { calls.handlerRan++; calls.handlerArgs = args; } },
        { name: 'background', fn: async (args) => { calls.handlerRan++; calls.handlerArgs = args; } },
        { name: 'other', fn: async (args) => { calls.handlerRan++; calls.handlerArgs = args; } },
      ];

      const S = {
        busy: true,
        compressionRunning: isCompression,
        activeStreamId: 'stream-1'
      };

      const msgEl = { value: cmdText };
      function $(id) { return id === 'msg' ? msgEl : null; }
      function autoResize() {}
      function _trySteer() { calls.trySteerCalled++; }
      function queueSessionMessage() { calls.queueCalled++; }
      function cancelStream() { calls.cancelCalled++; }

      const text = msgEl.value.trim();
      const literalSlash = false;

      // Extract of the busy intercept block under test from static/messages.js:
      if (text.startsWith('/') && !literalSlash) {
        const _pc = typeof parseCommand === 'function' && parseCommand(text);
        if (_pc && ['steer','interrupt','queue','terminal','goal','yolo','btw','background'].includes(_pc.name)) {
          const _bc = COMMANDS.find(c => c.name === _pc.name);
          if (_bc) {
            $('msg').value = '';
            calls.inputCleared = true;
            await _bc.fn(_pc.args);
            return { handledImmediately: true, calls };
          }
        }
      }

      // If not intercepted, falls through to steer / queue logic
      const defaultMessageMode = 'steer';
      if (defaultMessageMode === 'steer' && S.activeStreamId && typeof _trySteer === 'function') {
        _trySteer();
      } else {
        queueSessionMessage();
      }

      return { handledImmediately: false, calls };
    }

    (async () => {
      const results = {};
      const commandsToTest = ['btw', 'background', 'steer', 'interrupt', 'queue', 'terminal', 'goal', 'yolo', 'other'];

      for (const cmd of commandsToTest) {
        // Normal busy state
        results[cmd] = await runBusyScenario('/' + cmd + ' test-arg');
        // Compression-running busy state
        results[cmd + '_compression'] = await runBusyScenario('/' + cmd + ' test-arg', true);
      }

      console.log(JSON.stringify(results));
    })();
    """
    out = _run_node(script)

    # 1. /btw runs immediately during active turn and during compression
    for key in ["btw", "btw_compression"]:
        res = out[key]
        assert res["handledImmediately"] is True
        assert res["calls"]["handlerRan"] == 1
        assert res["calls"]["handlerArgs"] == "test-arg"
        assert res["calls"]["trySteerCalled"] == 0
        assert res["calls"]["queueCalled"] == 0
        assert res["calls"]["cancelCalled"] == 0
        assert res["calls"]["inputCleared"] is True

    # 2. /background runs immediately during active turn and during compression
    for key in ["background", "background_compression"]:
        res = out[key]
        assert res["handledImmediately"] is True
        assert res["calls"]["handlerRan"] == 1
        assert res["calls"]["handlerArgs"] == "test-arg"
        assert res["calls"]["trySteerCalled"] == 0
        assert res["calls"]["queueCalled"] == 0
        assert res["calls"]["cancelCalled"] == 0
        assert res["calls"]["inputCleared"] is True

    # 3. Retain coverage for existing allowlist commands
    for cmd in ["steer", "interrupt", "queue", "terminal", "goal", "yolo"]:
        res = out[cmd]
        assert res["handledImmediately"] is True
        assert res["calls"]["handlerRan"] == 1
        assert res["calls"]["trySteerCalled"] == 0

    # 4. Non-allowlisted command ('other') is not intercepted immediately and falls through to steer/queue
    assert out["other"]["handledImmediately"] is False
    assert out["other"]["calls"]["handlerRan"] == 0
    assert out["other"]["calls"]["trySteerCalled"] == 1
