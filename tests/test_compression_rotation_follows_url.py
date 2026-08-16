"""Compression session rotation must carry the browser tab to the continuation.

When context compression rotates the session id (A -> B), the server emits the
``compressed`` SSE event carrying ``new_session_id``. Until this change the
client handler updated the compression card but left ``S.session.session_id``,
the restore state and the address bar pointing at the *archived* parent.

Consequences observed in production (session 20260816_183052_8d321c):

* the archive is a frozen snapshot -- the active-session tail reduction
  deliberately refuses to prune it (``parent_session_id`` is None there), so a
  user sitting on that URL sees the full pre-compression transcript and
  concludes compaction "did nothing";
* reloading mid-turn, backgrounding a mobile tab, or losing the terminal
  ``done`` event strands the tab on the archive.

The backend already resolves archive -> continuation (#2980) and ``loadSession``
already follows that hint on a fresh load. The missing link is the *live*
rotation, which is what this module pins.

Executed through Node against the exact handler body extracted from the shipped
``static/messages.js`` so a real source-order/scope regression fails CI. This
repo runs pytest only -- standalone ``tests/*.js`` files are not collected --
so the harness is a pytest wrapper that shells out to node.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _extract_listener(src: str, event: str) -> str:
    """Return the arrow-function body registered for ``event`` on the SSE source."""
    marker = f"source.addEventListener('{event}',e=>{{"
    start = src.find(marker)
    assert start != -1, f"listener for {event!r} not found in messages.js"
    brace = src.index("{", start + len(marker) - 1)
    depth = 1
    i = brace + 1
    while depth > 0 and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    body = src[brace + 1 : i - 1]
    return body


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"function {name} not found"
    brace = src.index("{", start)
    depth = 1
    i = brace + 1
    while depth > 0 and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[start:i]


COMPRESSED_BODY = _extract_listener(MESSAGES_JS, "compressed")
SET_URL_FN = _extract_function(SESSIONS_JS, "_setActiveSessionUrl")
URL_FOR_SID_FN = _extract_function(SESSIONS_JS, "_sessionUrlForSid")


def _run_compressed_handler(current_sid: str, payload: dict) -> dict:
    """Execute the real 'compressed' handler body with stubbed collaborators."""
    script = f"""
const calls = {{ url: [], remember: [], cards: [] }};
const S = {{ session: {{ session_id: {json.dumps(current_sid)} }}, busy: false, messages: [] }};
const activeSid = {json.dumps(current_sid)};

// Collaborators the handler touches. Deliberately minimal: the point is to
// observe URL / restore-state effects, not to re-test the card renderer.
function _applyToAnchor(){{ return true; }}
function appendLiveCompressionCard(o){{ calls.cards.push(o); }}
function clearCompressionUi(){{ }}
function _setCompressionSessionLock(){{ }}
function renderMessages(){{ }}
function _rememberActiveSession(sid){{ calls.remember.push(sid); }}
function _setActiveSessionUrl(sid, opts){{ calls.url.push({{sid: sid, opts: opts || null}}); }}
const window = {{ _compressionUi: null }};

const e = {{ data: JSON.stringify({json.dumps(payload)}) }};
const handler = (e) => {{{COMPRESSED_BODY}}};
handler(e);

console.log(JSON.stringify({{
  calls: calls,
  sessionIdAfter: S.session ? S.session.session_id : null,
}}));
"""
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def test_rotation_switches_active_session_url_to_continuation():
    """A -> B rotation must move the tab (URL + restore state) to B."""
    out = _run_compressed_handler(
        "20260816_183052_8d321c",
        {
            "old_session_id": "20260816_183052_8d321c",
            "new_session_id": "20260816_202645_88ab0b",
        },
    )
    urls = [c["sid"] for c in out["calls"]["url"]]
    assert "20260816_202645_88ab0b" in urls, (
        "compression rotation left the address bar on the archived parent; "
        f"_setActiveSessionUrl calls={out['calls']['url']}"
    )
    assert out["sessionIdAfter"] == "20260816_202645_88ab0b", (
        "S.session.session_id still points at the archive after rotation"
    )
    assert "20260816_202645_88ab0b" in out["calls"]["remember"], (
        "restore state was not moved to the continuation session"
    )


def test_rotation_uses_replace_not_push():
    """The archive is not a navigation step: don't grow the back stack."""
    out = _run_compressed_handler(
        "sess_parent",
        {"old_session_id": "sess_parent", "new_session_id": "sess_child"},
    )
    entries = [c for c in out["calls"]["url"] if c["sid"] == "sess_child"]
    assert entries, "no URL update for the continuation session"
    opts = entries[0]["opts"] or {}
    assert opts.get("replace") is True, (
        "rotation must use replaceState so Back doesn't return to a stale archive; "
        f"opts={opts}"
    )


def test_no_rotation_leaves_url_untouched():
    """A -> A (compression without rotation) must not touch the URL."""
    out = _run_compressed_handler(
        "sess_same",
        {"old_session_id": "sess_same", "new_session_id": "sess_same"},
    )
    assert out["calls"]["url"] == [], (
        f"URL was rewritten without a real rotation: {out['calls']['url']}"
    )
    assert out["sessionIdAfter"] == "sess_same"


def test_missing_continuation_is_ignored():
    """An event without a continuation id must never blank the session."""
    out = _run_compressed_handler("sess_only", {"session_id": "sess_only"})
    assert out["calls"]["url"] == []
    assert out["sessionIdAfter"] == "sess_only"


def test_continuation_redirect_is_announced():
    """Following an archive -> continuation redirect must tell the user why.

    Opening a bookmarked/shared pre-compression archive silently swaps the
    session under the user's feet. Without a cue, a transcript that suddenly
    looks shorter reads as data loss rather than as compaction.
    """
    src = SESSIONS_JS
    marker = "if(continuationSid&&continuationSid!==sid&&!opts.skipContinuationResolve){"
    start = src.find(marker)
    assert start != -1, "continuation-follow branch not found in sessions.js"
    block = src[start : start + 700]
    assert "showToast" in block, (
        "continuation redirect happens silently; expected a showToast cue in "
        f"the follow branch:\n{block[:400]}"
    )
    assert "continuation_followed_toast" in block, (
        "redirect cue must go through the i18n key continuation_followed_toast"
    )


def test_continuation_toast_key_is_translated():
    """The cue must exist in both shipped locales."""
    i18n = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
    assert i18n.count("continuation_followed_toast") >= 2, (
        "continuation_followed_toast must be defined in both en and fr catalogs"
    )

def test_set_active_session_url_supports_replace_mode():
    """_setActiveSessionUrl must honour an explicit replace request."""
    script = f"""
{URL_FOR_SID_FN}
{SET_URL_FN}
const seen = [];
const window = {{
  history: {{
    pushState: (s,t,u) => seen.push(['push', u]),
    replaceState: (s,t,u) => seen.push(['replace', u]),
  }},
  location: {{
    pathname: '/', search: '', hash: '',
    href: 'http://example.test/', origin: 'http://example.test',
  }},
}};
const document = {{ baseURI: 'http://example.test/' }};
globalThis.window = window;
globalThis.document = document;
_setActiveSessionUrl('sess_new', {{replace: true}});
_setActiveSessionUrl('sess_other');
console.log(JSON.stringify(seen));
"""
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    seen = json.loads(proc.stdout)
    assert seen and seen[0][0] == "replace", (
        f"replace:true was not honoured, got {seen}"
    )
    assert len(seen) > 1 and seen[1][0] == "push", (
        f"default behaviour must stay pushState, got {seen}"
    )
