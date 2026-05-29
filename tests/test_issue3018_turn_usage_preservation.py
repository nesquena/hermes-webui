"""Regression test for issue #3018 — _preserveClientTurnState helper.

When stream_end fires during the stream-fade window, _restoreSettledSession()
replaces S.messages with server data that lacks client-side props (_turnUsage,
_turnDuration, _turnTps, _gatewayRouting). The _preserveClientTurnState helper
must migrate these props from old messages to new ones.

This test drives the ACTUAL JS helper from static/messages.js via node.
"""
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
MESSAGES_JS = REPO_ROOT / "static" / "messages.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_preserve_function(src: str) -> str:
    """Extract the _preserveClientTurnState function body from messages.js."""
    marker = "function _preserveClientTurnState("
    start = src.find(marker)
    if start == -1:
        raise ValueError("_preserveClientTurnState not found in messages.js")
    # Find the matching closing brace
    depth = 0
    i = src.index("{", start)
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise ValueError("Could not find end of _preserveClientTurnState")


def _run_js_test(test_body: str) -> str:
    """Run a JS test snippet that has _preserveClientTurnState available."""
    src = MESSAGES_JS.read_text(encoding="utf-8")
    func_src = _extract_preserve_function(src)

    driver = textwrap.dedent(f"""\
        // --- function under test ---
        {func_src}

        // --- test ---
        {test_body}
    """)
    result = subprocess.run(
        [NODE, "-e", driver],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"node exited {result.returncode}:\\n{result.stderr}\\n{result.stdout}"
        )
    return result.stdout.strip()


class TestPreserveClientTurnState:
    """Unit tests for the _preserveClientTurnState helper."""

    def test_transfers_turn_usage_to_last_assistant(self):
        out = _run_js_test("""\
            const oldMsgs = [
                { role: 'user', content: 'hello' },
                { role: 'assistant', content: 'hi',
                  _turnUsage: { input_tokens: 100, output_tokens: 50, estimated_cost: 0.01,
                                 cache_read_tokens: 10, cache_write_tokens: 5, cache_hit_percent: 80 },
                  _turnDuration: 3.2,
                  _turnTps: 42.5,
                  _gatewayRouting: 'opus' },
            ];
            const newMsgs = [
                { role: 'user', content: 'hello' },
                { role: 'assistant', content: 'hi' },
            ];

            _preserveClientTurnState(oldMsgs, newMsgs);

            const last = newMsgs[newMsgs.length - 1];
            console.assert(last._turnUsage.input_tokens === 100, 'input_tokens');
            console.assert(last._turnUsage.output_tokens === 50, 'output_tokens');
            console.assert(last._turnDuration === 3.2, 'duration');
            console.assert(last._turnTps === 42.5, 'tps');
            console.assert(last._gatewayRouting === 'opus', 'routing');
            console.log('PASS');
        """)
        assert out == "PASS"

    def test_no_crash_on_empty_messages(self):
        out = _run_js_test("""\
            _preserveClientTurnState([], []);
            _preserveClientTurnState(null, []);
            _preserveClientTurnState([], null);
            _preserveClientTurnState(null, null);
            console.log('PASS');
        """)
        assert out == "PASS"

    def test_no_crash_when_no_assistant_in_old(self):
        out = _run_js_test("""\
            const oldMsgs = [{ role: 'user', content: 'hello' }];
            const newMsgs = [{ role: 'user', content: 'hello' }, { role: 'assistant', content: 'hi' }];
            _preserveClientTurnState(oldMsgs, newMsgs);
            console.assert(!('_turnUsage' in newMsgs[1]), 'should not add _turnUsage');
            console.log('PASS');
        """)
        assert out == "PASS"

    def test_no_crash_when_no_assistant_in_new(self):
        out = _run_js_test("""\
            const oldMsgs = [
                { role: 'assistant', content: 'hi',
                  _turnUsage: { input_tokens: 100, output_tokens: 50 } },
            ];
            const newMsgs = [{ role: 'user', content: 'hello' }];
            _preserveClientTurnState(oldMsgs, newMsgs);
            console.log('PASS');
        """)
        assert out == "PASS"

    def test_preserves_existing_turn_usage_on_new_messages(self):
        """If newMsgs already has _turnUsage (e.g. from _finishDone), it should be overwritten."""
        out = _run_js_test("""\
            const oldMsgs = [
                { role: 'assistant', content: 'hi',
                  _turnUsage: { input_tokens: 200, output_tokens: 100 },
                  _turnDuration: 5.0 },
            ];
            const newMsgs = [
                { role: 'assistant', content: 'hi',
                  _turnUsage: { input_tokens: 999, output_tokens: 999 } },
            ];
            _preserveClientTurnState(oldMsgs, newMsgs);
            console.assert(newMsgs[0]._turnUsage.input_tokens === 200, 'should overwrite');
            console.assert(newMsgs[0]._turnDuration === 5.0, 'should add missing prop');
            console.log('PASS');
        """)
        assert out == "PASS"

    def test_only_transfers_defined_properties(self):
        """Undefined props on old msg should not be set on new msg."""
        out = _run_js_test("""\
            const oldMsgs = [
                { role: 'assistant', content: 'hi',
                  _turnUsage: { input_tokens: 100, output_tokens: 50 } },
                // no _turnDuration, _turnTps, _gatewayRouting
            ];
            const newMsgs = [
                { role: 'assistant', content: 'hi' },
            ];
            _preserveClientTurnState(oldMsgs, newMsgs);
            const last = newMsgs[0];
            console.assert(last._turnUsage.input_tokens === 100, 'turnUsage transferred');
            console.assert(last._turnDuration === undefined, 'duration not set');
            console.assert(last._turnTps === undefined, 'tps not set');
            console.assert(last._gatewayRouting === undefined, 'routing not set');
            console.log('PASS');
        """)
        assert out == "PASS"

    def test_multiple_assistant_messages_picks_last_with_usage(self):
        """When multiple assistant messages exist, pick the last one with _turnUsage."""
        out = _run_js_test("""\
            const oldMsgs = [
                { role: 'assistant', content: 'first reply',
                  _turnUsage: { input_tokens: 50, output_tokens: 20 } },
                { role: 'assistant', content: 'second reply',
                  _turnUsage: { input_tokens: 200, output_tokens: 100 } },
            ];
            const newMsgs = [
                { role: 'assistant', content: 'first reply' },
                { role: 'assistant', content: 'second reply' },
            ];
            _preserveClientTurnState(oldMsgs, newMsgs);
            // Should attach to the last assistant in newMsgs
            console.assert(newMsgs[1]._turnUsage.input_tokens === 200, 'last assistant gets usage');
            console.assert(newMsgs[0]._turnUsage === undefined, 'first assistant untouched');
            console.log('PASS');
        """)
        assert out == "PASS"
