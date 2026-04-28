"""Behavioural tests for the session-render cache key helper in ui.js.

The session HTML cache (used to make session-switch instant) was originally
keyed by ``(session_id, msgCount)`` only.  That made it serve **stale** HTML
when a user edited / retried a message — the count is unchanged but the
content isn't.  Task 2 of the session-switch perf plan upgrades the key to
``(session_id, msgCount, lastKey)`` where ``lastKey`` is a cheap fingerprint
of the *last visible message*.  This file pins that helper.

We drive the actual ``_sessionCacheKey`` extracted from ``static/ui.js``
through node so a Python mirror can't drift from reality.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
eval(extractFunc('_sessionCacheKey'));

let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => {
  const messages = JSON.parse(buf);
  process.stdout.write(JSON.stringify(_sessionCacheKey(messages)));
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("cache_key_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _key(driver_path, messages):
    r = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        input=json.dumps(messages),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if r.returncode != 0:
        raise RuntimeError(f"node driver failed: {r.stderr}")
    return json.loads(r.stdout)


def test_empty_session_returns_zero_count(driver_path):
    out = _key(driver_path, [])
    assert out["count"] == 0
    assert out["lastKey"] == ""


def test_count_reflects_total_messages(driver_path):
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    out = _key(driver_path, msgs)
    assert out["count"] == 2


def test_lastkey_changes_when_last_message_content_changes(driver_path):
    a = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    b = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "HELLO!"}]
    ka = _key(driver_path, a)
    kb = _key(driver_path, b)
    assert ka["count"] == kb["count"] == 2
    assert ka["lastKey"] != kb["lastKey"], (
        "lastKey must differ when last message content changes — otherwise "
        "edits/retries serve stale cached HTML on session re-switch."
    )


def test_lastkey_stable_for_identical_messages(driver_path):
    msgs = [
        {"role": "user", "content": "what's 2+2?", "_ts": 1700000000},
        {"role": "assistant", "content": "4", "_ts": 1700000001},
    ]
    k1 = _key(driver_path, msgs)
    k2 = _key(driver_path, msgs)
    assert k1 == k2, "Same messages must produce identical cache keys (deterministic)"


def test_lastkey_changes_when_only_earlier_message_changes_NOT_required(driver_path):
    """We deliberately fingerprint *only* the last visible message — adequate
    for catching edits to the most recent turn (the common case) without
    paying O(N) hash cost on every session switch.  This test pins that
    intentional trade-off so it's not 'fixed' by accident."""
    a = [{"role": "user", "content": "v1"}, {"role": "assistant", "content": "same tail"}]
    b = [{"role": "user", "content": "v2"}, {"role": "assistant", "content": "same tail"}]
    assert _key(driver_path, a)["lastKey"] == _key(driver_path, b)["lastKey"]


def test_lastkey_includes_role_so_truncation_invalidates(driver_path):
    """If the last message changes role (e.g. user trims off the assistant
    reply via /clear-after), the key must change so we don't serve a cache
    that still shows the deleted assistant turn."""
    a = [{"role": "user", "content": "ping"}, {"role": "assistant", "content": "pong"}]
    b = [{"role": "user", "content": "pong"}]
    ka = _key(driver_path, a)
    kb = _key(driver_path, b)
    assert ka["count"] != kb["count"]
    # Different counts already invalidate; this case is covered by the count
    # check, but we also assert lastKey differs as a defence in depth.
    assert ka["lastKey"] != kb["lastKey"]


def test_lastkey_handles_array_content_blocks(driver_path):
    """Anthropic-style messages have content as a list of blocks."""
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]
    out = _key(driver_path, msgs)
    assert out["count"] == 2
    assert out["lastKey"] != ""


def test_lastkey_handles_missing_content_field(driver_path):
    """Tool-call-only assistant messages have no textual content — the key
    helper must still produce a stable, non-throwing fingerprint."""
    msgs = [
        {"role": "user", "content": "run X"},
        {"role": "assistant", "tool_calls": [{"id": "t1", "function": {"name": "X"}}]},
    ]
    out = _key(driver_path, msgs)
    assert out["count"] == 2
    # No content → empty-ish key, but must be a string and not crash.
    assert isinstance(out["lastKey"], str)
