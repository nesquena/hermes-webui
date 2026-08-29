"""Regression coverage for same-session notification toasts (#6673).

Desktop notifications via the Notification API only pop a toast banner the
first time for a given tag: on Windows the notification persists in the
Action Center, and subsequent same-session turn completions produce no
visible toast. The fix keeps the session-scoped `tag` (so stale
notifications are still replaced in the notification center) but sets
`renotify: true` so every turn completion re-pops a toast banner.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_SRC = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extract(source, name) {
  const start = source.indexOf('function ' + name + '(');
  if (start === -1) throw new Error('function not found: ' + name);
  const bodyStart = source.indexOf('){', start) + 1;
  let depth = 0;
  for (let i = bodyStart; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') {
      depth--;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error('function body did not close: ' + name);
}

// Stub the globals `_notificationOptions` touches so the extracted function
// runs standalone: S (session state), location, and _sessionUrlForSid.
let S = { session: { session_id: 'sess-6673' } };
global.S = S;
global.location = { origin: 'http://localhost:8080', href: 'http://localhost:8080/' };
global._sessionUrlForSid = (sid) => '/#/s/' + sid;

eval(extract(src, '_notificationOptions'));

const withSid = _notificationOptions('Turn done', { sid: 'sess-6673' });
// Fallback path: no options.sid AND no session in scope -> generic tag.
S = null;
const withoutSid = _notificationOptions('Turn done', {});
console.log(JSON.stringify({
  withSid: { renotify: withSid.renotify, tag: withSid.tag, body: withSid.body },
  withoutSid: { renotify: withoutSid.renotify, tag: withoutSid.tag },
}));
"""


def _run_driver(tmp_path) -> dict:
    driver = tmp_path / "driver_6673.js"
    driver.write_text(_DRIVER_SRC, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(ROOT / "static" / "messages.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"node failed: {result.stderr}"
    return json.loads(result.stdout)


def test_same_session_notification_renotifies(tmp_path):
    """#6673: a notification for a session-scoped tag must re-pop a toast on
    every turn completion instead of being swallowed after the first pop."""
    out = _run_driver(tmp_path)
    assert out["withSid"]["renotify"] is True
    assert out["withSid"]["body"] == "Turn done"


def test_session_scoped_tag_is_preserved(tmp_path):
    """The session-scoped tag stays so stale notifications are still replaced
    in the notification center (no duplicate clutter)."""
    out = _run_driver(tmp_path)
    assert out["withSid"]["tag"] == "hermes-sess-6673"


def test_fallback_tag_without_session(tmp_path):
    out = _run_driver(tmp_path)
    assert out["withoutSid"]["tag"] == "hermes-webui"
    assert out["withoutSid"]["renotify"] is True
