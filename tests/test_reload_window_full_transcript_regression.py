"""Reload-window regression: bound safe refreshes without shrinking the transcript.

`_messageReloadLimitForSession()` decides how wide a window a same-session
force-reload asks the server for. Two inputs made it return `null`, and a null
window means the bare `/api/session?messages=1` request — the FULL transcript:

1. `hint.truncated === false` (the whole conversation is already loaded, which
   is the normal state for any conversation shorter than the initial window);
2. a hint width above the server `msg_limit` ceiling (`_msgLimitMax`), where the
   caller deliberately dropped the window to avoid the backend clamping it and
   silently shrinking an already-loaded transcript (#6154).

Both cases are hit by the returning-from-background path
(`refreshActiveSessionIfExternallyUpdated` -> `loadSession({force:true})`), so
coming back to a conversation after switching apps refetched every message to
display the one or two that actually arrived.

The fix keeps the anti-shrink invariant that motivated the null: never ask for a
window NARROWER than the loaded rows plus newly appended rows. A bounded reload
is safe only when that whole desired window fits under the ceiling; otherwise it
must retain the full-transcript fallback.

These tests execute the real functions under node instead of asserting on source
strings, so they fail on the pre-fix behaviour rather than on a refactor.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
SESSIONS_JS = SESSIONS_JS_PATH.read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=REPO_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _slice_function(name: str) -> str:
    """Return the source text of one top-level `function name(...)` block."""
    start = SESSIONS_JS.index(f"function {name}(")
    if SESSIONS_JS[max(0, start - len("async ")) : start] == "async ":
        start -= len("async ")
    depth = 0
    i = SESSIONS_JS.index("{", start)
    for pos in range(i, len(SESSIONS_JS)):
        ch = SESSIONS_JS[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return SESSIONS_JS[start : pos + 1]
    raise AssertionError(f"unbalanced braces while slicing {name}")


def _harness(cases: list[dict]) -> str:
    """Run _messageReloadLimitForSession + the caller's bounding step per case.

    The bounding expression is the real one from _ensureMessagesLoaded; it is
    reproduced here because that function is a large async body that touches the
    network. `resolved` is the msg_limit actually sent to the server, and null
    means "no msg_limit param" -> full transcript.

    The cold-load constant is read from the file instead of hard-coded:
    `_INITIAL_TAIL_MSG_LIMIT` is a local addition and upstream still falls back
    to `_INITIAL_MSG_LIMIT`, so pinning a literal here would make the test
    depend on an unrelated patch rather than on the reload-window behaviour.
    """
    fn = _slice_function("_messageReloadLimitForSession")
    tail_const = (
        "const _INITIAL_TAIL_MSG_LIMIT = 8;"
        if "_INITIAL_TAIL_MSG_LIMIT" in SESSIONS_JS
        else ""
    )
    return f"""
const _INITIAL_MSG_LIMIT = 30;
{tail_const}
let _msgLimitMax = 500;
let _sameSessionForceReloadHint = null;
let S = {{}};

{fn}

const cases = {json.dumps(cases)};
const out = [];
for (const c of cases) {{
  _msgLimitMax = c.ceiling;
  _sameSessionForceReloadHint = c.hint;
  S = {{ session: {{ session_id: c.sid, message_count: c.server_message_count }} }};
  const requested = _messageReloadLimitForSession(c.sid);
  const resolved = (requested && requested > 0) ? requested : null;
  out.push({{ name: c.name, requested, resolved }});
}}
console.log(JSON.stringify(out));
"""


def _cold_load_width() -> int:
    """Width used when there is no width hint (cold load), per the live source."""
    return 8 if "_INITIAL_TAIL_MSG_LIMIT" in SESSIONS_JS else 30


def _resolve(cases: list[dict]) -> dict:
    rows = json.loads(_run_node(_harness(cases)))
    return {row["name"]: row for row in rows}


def _replacement_harness() -> str:
    """Drive the real fetch-and-replace path for the 480 + 60 regression."""
    reload_limit = _slice_function("_messageReloadLimitForSession")
    ensure_loaded = _slice_function("_ensureMessagesLoaded")
    return f"""
const _INITIAL_MSG_LIMIT = 30;
const _MSG_LIMIT_MAX = 500;
let _msgLimitMax = _MSG_LIMIT_MAX;
let _messagesTruncated = true;
let _oldestIdx = 9061;
let _pendingCarryForwardSnapshot = null;
let _loadingSessionId = 's2';
let _loadSessionGeneration = 0;
const window = {{}};

const previousTotal = 9541;
const appendedCount = 60;
const currentTotal = previousTotal + appendedCount;
const allMessages = Array.from({{length: currentTotal}}, (_, rowId) => ({{
  role: rowId % 2 ? 'assistant' : 'user',
  content: `row-${{rowId}}`,
  row_id: rowId,
}}));
const originalMessages = allMessages.slice(previousTotal - 480, previousTotal);
const originalReference = originalMessages;
const originalIds = originalMessages.map(m => m.row_id);
const appendedIds = allMessages.slice(previousTotal).map(m => m.row_id);
let S = {{
  session: {{session_id: 's2', message_count: currentTotal}},
  messages: originalMessages,
  lastUsage: {{}},
}};
let _sameSessionForceReloadHint = {{
  session_id: 's2',
  loaded_renderable_count: 480,
  loaded_message_count: 480,
  message_count: previousTotal,
  truncated: true,
}};

function _clearSameSessionForceReloadHint(sid) {{
  if (!_sameSessionForceReloadHint) return;
  if (!sid || _sameSessionForceReloadHint.session_id === sid) _sameSessionForceReloadHint = null;
}}
function _syncToolCallsForLoadedMessages() {{}}
function clearLiveToolCards() {{}}
function clearVisibleMessageRowCache() {{}}
function _isSessionActivelyViewedForList() {{ return false; }}

let requestedUrl = '';
async function api(url) {{
  requestedUrl = url;
  const match = url.match(/[?&]msg_limit=(\\d+)/);
  const requestedLimit = match ? Number(match[1]) : null;
  const start = requestedLimit === null ? 0 : Math.max(0, allMessages.length - requestedLimit);
  const returned = allMessages.slice(start);
  return {{
    session: {{
      session_id: 's2',
      message_count: currentTotal,
      messages: returned,
      _messages_truncated: start > 0,
      _messages_offset: start,
      _msg_limit_max: _MSG_LIMIT_MAX,
    }},
  }};
}}

{reload_limit}
{ensure_loaded}

(async () => {{
  await _ensureMessagesLoaded('s2', {{force: true}});
  const finalIds = new Set(S.messages.map(m => m.row_id));
  console.log(JSON.stringify({{
    requestedUrl,
    replacedReference: S.messages !== originalReference,
    finalCount: S.messages.length,
    lostOriginalIds: originalIds.filter(rowId => !finalIds.has(rowId)),
    missingAppendedIds: appendedIds.filter(rowId => !finalIds.has(rowId)),
  }}));
}})().catch(err => {{ console.error(err); process.exit(1); }});
"""


def test_fully_loaded_conversation_does_not_refetch_whole_transcript():
    """A short conversation held entirely in memory must reload only a tail window.

    Pre-fix: hint.truncated === false returned null -> the client refetched all
    285 messages (measured at 1.76 MB / 406 ms on a real session) to pick up the
    two that arrived while the tab was hidden.
    """
    got = _resolve(
        [
            {
                "name": "fully_loaded",
                "sid": "s1",
                "ceiling": 500,
                "server_message_count": 287,
                "hint": {
                    "session_id": "s1",
                    "loaded_renderable_count": 285,
                    "loaded_message_count": 285,
                    "message_count": 285,
                    "truncated": False,
                },
            }
        ]
    )["fully_loaded"]

    assert got["resolved"] is not None, (
        "a fully-loaded conversation still asks for the FULL transcript on refresh"
    )
    # Never narrower than what is already rendered, or the transcript shrinks.
    assert got["resolved"] >= 285
    assert got["resolved"] <= 500


def test_hint_above_ceiling_falls_back_to_full_transcript():
    """An over-ceiling desired width must retain the #6154 full-transcript fallback.

    With 480 loaded rows and 60 appended rows, a 500-row tail would replace the
    transcript with logical rows 40-539 and silently lose rows 0-39. Only the
    bare request can preserve the entire 540-row desired window.
    """
    got = _resolve(
        [
            {
                "name": "above_ceiling",
                "sid": "s2",
                "ceiling": 500,
                "server_message_count": 9601,
                "hint": {
                    "session_id": "s2",
                    "loaded_renderable_count": 480,
                    "loaded_message_count": 480,
                    "message_count": 9541,
                    "truncated": True,
                },
            }
        ]
    )["above_ceiling"]

    assert got["resolved"] is None, (
        "an over-ceiling desired window must use the full-transcript fallback"
    )


def test_over_ceiling_real_replacement_preserves_loaded_and_appended_rows():
    """The real wholesale replacement must lose none of 480 loaded + 60 new rows."""
    got = json.loads(_run_node(_replacement_harness()))

    assert "msg_limit=" not in got["requestedUrl"], got
    assert got["replacedReference"] is True, "precondition: exercise the real replacement"
    assert got["finalCount"] == 9601, "the bare request must return the full transcript"
    assert got["lostOriginalIds"] == [], "the replacement dropped already-loaded rows"
    assert got["missingAppendedIds"] == [], "the replacement omitted newly appended rows"


def test_visible_transcript_wider_than_ceiling_keeps_full_reload():
    """The anti-shrink invariant (#6154) still wins when the ceiling cannot hold the view.

    If more rows are on screen than the server will ever return in one window,
    clamping WOULD drop already-loaded rows. That case must keep the full
    transcript path — fail closed toward correctness, not toward speed.
    """
    got = _resolve(
        [
            {
                "name": "cannot_clamp",
                "sid": "s3",
                "ceiling": 500,
                "server_message_count": 9543,
                "hint": {
                    "session_id": "s3",
                    "loaded_renderable_count": 900,
                    "loaded_message_count": 900,
                    "message_count": 9541,
                    "truncated": True,
                },
            }
        ]
    )["cannot_clamp"]

    assert got["resolved"] is None, (
        "clamping below the visible row count would silently shrink the transcript"
    )


def test_ordinary_truncated_window_is_unchanged():
    """The already-correct common path must keep its exact behaviour."""
    got = _resolve(
        [
            {
                "name": "ordinary",
                "sid": "s4",
                "ceiling": 500,
                "server_message_count": 9543,
                "hint": {
                    "session_id": "s4",
                    "loaded_renderable_count": 60,
                    "loaded_message_count": 60,
                    "message_count": 9541,
                    "truncated": True,
                },
            },
            {
                "name": "cold",
                "sid": "s5",
                "ceiling": 500,
                "server_message_count": 400,
                "hint": None,
            },
        ]
    )
    assert got["ordinary"]["resolved"] == 62
    assert got["cold"]["resolved"] == _cold_load_width()


def test_ceiling_is_read_from_server_metadata():
    """A server advertising a different ceiling must be honoured (no mirrored const)."""
    got = _resolve(
        [
            {
                "name": "low_ceiling",
                "sid": "s6",
                "ceiling": 120,
                "server_message_count": 9543,
                "hint": {
                    "session_id": "s6",
                    "loaded_renderable_count": 118,
                    "loaded_message_count": 118,
                    "message_count": 9541,
                    "truncated": True,
                },
            }
        ]
    )["low_ceiling"]
    assert got["resolved"] == 120
