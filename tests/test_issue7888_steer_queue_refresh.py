"""#7888: queued steers survive a page refresh.

A steer sent during a Gateway-backed run is queued client-side before the run's later
assistant output. The refresh-restore block in ``loadSession`` used to classify it stale,
discard it and wipe the persisted queue. These tests drive the real block from
``static/sessions.js`` through Node with fake storage, composer and toast.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

START = "// Restore any queued message that survived page refresh or tab restore."
END = "// Reconstruct tool calls from message metadata"

DRIVER = r"""
const block = process.argv[1];
const sc = JSON.parse(process.argv[2]);
const cleared = []; const toasts = [];
const composer = { value: sc.composerValue || '' };
const ctx = {
  S: { messages: sc.messages }, sid: 'sid1',
  queueSessionMessage: () => 0,
  _readPersistedSessionQueue: () => sc.entries.map(e => ({ ...e })),
  _clearPersistedSessionQueue: (s) => cleared.push(s),
  $: (id) => (id === 'msg' ? (sc.noComposer ? null : composer) : null),
  autoResize: () => {},
  showToast: (m) => toasts.push(m),
};
new Function(...Object.keys(ctx), block)(...Object.values(ctx));
process.stdout.write(JSON.stringify({ composer: composer.value, cleared: cleared.length, toasts }));
"""

T0 = 1_800_000_000


def _run(scenario):
    if NODE is None:
        pytest.skip("node not available")
    start = SESSIONS_JS.index(START)
    block = SESSIONS_JS[start:SESSIONS_JS.index(END, start)]
    out = subprocess.run([NODE, "-e", DRIVER, block, json.dumps(scenario)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_steer_queued_mid_run_is_restored_after_refresh():
    res = _run({
        "messages": [{"role": "user", "timestamp": T0}, {"role": "assistant", "timestamp": T0 + 60}],
        "entries": [{"text": "my steer", "_queued_at": (T0 + 30) * 1000}],
    })
    assert res["composer"] == "my steer"
    assert res["cleared"] == 1
    assert res["toasts"] and "moved on" in res["toasts"][0]


def test_fresh_entry_keeps_the_existing_toast():
    res = _run({
        "messages": [{"role": "assistant", "timestamp": T0}],
        "entries": [{"text": "new", "_queued_at": (T0 + 10) * 1000}],
    })
    assert res["composer"] == "new"
    assert res["cleared"] == 1
    assert res["toasts"] == ["Queued message restored — review and send when ready"]


def test_queue_is_kept_when_the_composer_is_occupied():
    res = _run({
        "messages": [{"role": "assistant", "timestamp": T0 + 60}],
        "entries": [{"text": "my steer", "_queued_at": (T0 + 30) * 1000}],
        "composerValue": "draft in progress",
    })
    assert res["composer"] == "draft in progress"
    assert res["cleared"] == 0


def test_text_entry_is_preferred_over_a_files_only_entry():
    res = _run({
        "messages": [{"role": "assistant", "timestamp": T0 + 60}],
        "entries": [{"text": "", "files": ["a.png"], "_queued_at": (T0 + 90) * 1000},
                    {"text": "older steer", "_queued_at": (T0 + 30) * 1000}],
    })
    assert res["composer"] == "older steer"
    assert res["cleared"] == 1


def test_files_only_queue_is_still_cleared():
    res = _run({
        "messages": [{"role": "assistant", "timestamp": T0 + 60}],
        "entries": [{"text": "", "files": ["a.png"], "_queued_at": (T0 + 30) * 1000}],
    })
    assert res["composer"] == ""
    assert res["cleared"] == 1
