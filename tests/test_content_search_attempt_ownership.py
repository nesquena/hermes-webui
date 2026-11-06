"""Regression: a superseded content-search attempt must never win.

Maintainer review of #8882, P2. `A -> B -> A` reuses the same query string, so a
response from the abandoned first attempt could still pass a text comparison and
contribute rows. `_mergeContentSearchResults` keeps the first row per session id,
so the stale `match_message_idx`/preview would stick permanently even after the
current response arrived. Every attempt now carries a generation and both the
fallback and the stream callbacks check it.
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")

EXTRACT_JS = r"""
function extract(name) {
  const marker = 'function ' + name + '(';
  let start = SRC.indexOf(marker);
  if (start < 0) throw new Error('missing ' + name);
  if (SRC.slice(Math.max(0, start - 6), start) === 'async ') start -= 6;
  const brace = SRC.indexOf('{', start);
  let depth = 0;
  for (let i = brace; i < SRC.length; i++) {
    const c = SRC[i];
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return SRC.slice(start, i + 1); }
  }
  throw new Error('missing ' + name);
}
"""

HARNESS_JS = r"""
global.window = { _sessionSearchJumpGen: 0 };
global._contentSearchSSE = null;
global._contentSearchQuery = '';
global._contentSearchGen = 0;
global._contentSearchResults = [];
global._allSessions = [];
global._lastSessionSearchQuery = '';
global._hideSearchPreviewsAfterSelect = false;
global._searchDebounceTimer = null;
let searchValue = '';
let timerCb = null;
global.$ = (id) => (id === 'sessionSearch' ? { value: searchValue } : null);
global.syncSessionSearchClear = () => {};
global._syncArchivedSearchPagingRefresh = () => {};
global.renderSessionListFromCache = () => {};
global._sessionSearchDirectAndTitleMatches = () => [];
global._repaintSidebarForBackgroundChurn = () => {};
global.clearTimeout = () => { timerCb = null; };
global.setTimeout = (fn) => { timerCb = fn; return 1; };
global.EventSource = undefined;
const pending = [];
global.api = (url) => new Promise((resolve) => pending.push({ url, resolve }));
eval(extract('_bumpContentSearchGen'));
eval(extract('_stopContentSearchSSE'));
eval(extract('_mergeContentSearchResults'));
eval(extract('_contentSearchFallback'));
eval(extract('_startContentSearchSSE'));
eval(extract('filterSessions'));
const tick = () => new Promise((resolve) => setImmediate(resolve));
(async () => {
  // Attempt 1: query A, response left pending.
  searchValue = 'candidate-a';
  filterSessions();
  timerCb();
  // Abandon it, then come back to the same text.
  searchValue = 'candidate-b';
  filterSessions();
  searchValue = 'candidate-a';
  filterSessions();
  timerCb();
  // The abandoned attempt answers first, with the stale match index.
  pending[0].resolve({
    sessions: [{ session_id: 's', match_type: 'content', match_message_idx: 1, match_preview: 'stale' }],
  });
  await tick();
  // The current attempt answers with the fresh index.
  pending[1].resolve({
    sessions: [{ session_id: 's', match_type: 'content', match_message_idx: 9, match_preview: 'fresh' }],
  });
  await tick();
  await tick();
  console.log(JSON.stringify({ attempts: pending.length, rows: _contentSearchResults }));
})();
"""


def _build_script():
    return (
        "const fs = require('fs');\n"
        "const SRC = fs.readFileSync(" + json.dumps(str(SESSIONS_JS)) + ", 'utf8');\n"
        + EXTRACT_JS
        + HARNESS_JS
    )


@pytest.mark.skipif(NODE is None, reason="node is required for the JS harness")
def test_superseded_attempt_cannot_pin_rows_after_a_b_a():
    proc = subprocess.run(
        [NODE, "-e", _build_script()], capture_output=True, text=True, check=True
    )
    result = json.loads(proc.stdout)

    assert result["attempts"] == 2, result
    rows = result["rows"]
    assert len(rows) == 1, rows
    assert rows[0]["session_id"] == "s"
    assert rows[0]["match_message_idx"] == 9, (
        "the current attempt must win; a superseded A->B->A response pinned %r"
        % (rows[0],)
    )
    assert rows[0]["match_preview"] == "fresh"
