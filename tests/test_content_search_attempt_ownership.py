"""Regression: a superseded content-search attempt must never win.

Maintainer review of #8882, P2. `A -> B -> A` reuses the same query string, so a
response from the abandoned first attempt could still pass a text comparison and
contribute rows. `_mergeContentSearchResults` keeps the first row per session id,
so the stale `match_message_idx`/preview would stick permanently even after the
current response arrived. Every attempt now carries a generation and both the
fallback and the stream callbacks check it.

The harness interpolates the production functions the way #4766 does; no
`eval()` is used, so the repository's execution gate stays quiet.
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")

QUERY_A = "candidate-a"
QUERY_B = "candidate-b"

HARNESS_PREFIX = """
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

"""

HARNESS_EPILOGUE = """
const tick = () => new Promise((resolve) => setImmediate(resolve));
(async () => {
  // Attempt 1: query A, response left pending.
  searchValue = __QUERY_A__;
  filterSessions();
  timerCb();
  // Abandon it, then come back to the same text.
  searchValue = __QUERY_B__;
  filterSessions();
  searchValue = __QUERY_A__;
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


def _extract_function(source_text, function_name, *, required=True):
    """Return one whole `function name(...) {...}` body (incl. `async` if present)."""
    marker = f"function {function_name}("
    start = source_text.find(marker)
    if start < 0:
        if required:
            raise AssertionError(f"Could not extract {function_name}")
        return ""
    if source_text[max(0, start - 6) : start] == "async ":
        start -= 6
    brace_start = source_text.index("{", start)
    depth = 0
    for index in range(brace_start, len(source_text)):
        char = source_text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source_text[start : index + 1]
    raise AssertionError(f"Could not extract {function_name}")


def _build_script():
    source = SESSIONS_JS.read_text(encoding="utf-8")
    functions = "\n\n".join(
        _extract_function(source, name, required=required)
        for name, required in (
            ("_bumpContentSearchGen", False),
            ("_stopContentSearchSSE", True),
            ("_mergeContentSearchResults", True),
            ("_contentSearchFallback", True),
            ("_startContentSearchSSE", True),
            ("filterSessions", True),
        )
    )
    # The token only exists with this change; on a base/master checkout the test
    # must still run and fail on behaviour rather than on extraction.
    stub = (
        ""
        if "function _bumpContentSearchGen(" in source
        else "function _bumpContentSearchGen(){ return 1; }\n"
    )
    epilogue = HARNESS_EPILOGUE.replace("__QUERY_A__", json.dumps(QUERY_A)).replace(
        "__QUERY_B__", json.dumps(QUERY_B)
    )
    return HARNESS_PREFIX + stub + functions + "\n" + epilogue


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
