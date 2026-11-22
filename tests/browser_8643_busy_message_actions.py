#!/usr/bin/env python3
"""#8643 (behaviour 1): a running turn's Regenerate / Undo / Edit are dead buttons.

WHY THIS EXISTS
  `static/ui.js` paints the message action bar from `isLastAssistant` /
  `isEditableUser` alone. While a turn runs, the last visible row IS an
  assistant row, so Regenerate sits on it, and the last user row keeps its
  Edit. But every one of those actions refuses on `S.busy`
  (`editMessage`, `regenerateResponse`, `submitEdit`), so the tap did nothing
  and said nothing. On a phone the footer is always at full opacity
  (`.msg-foot .msg-actions{opacity:1}`), so no hover is needed to reach a
  button that cannot work.

  The maintainer's ask: hide (or disable with a tooltip) Regenerate, Undo and
  Edit while `S.busy`, and offer Regenerate only on the turn's final assistant
  message.

WHAT THIS CHECKS
  A persisted turn with an interim answer and a final answer, loaded through
  the normal browser session path. All three states are read inside ONE
  synchronous page evaluation — idle, then `S.busy = true` + `renderMessages()`,
  then back to idle — because a background sidebar refresh
  (`_reconcileActiveSessionIdleStateFromList`) clears `S.busy` on its own a
  few hundred milliseconds later, and the symptom is what the render paints
  while the turn is running.

  - idle: exactly one Regenerate, on the FINAL assistant segment of the turn
    and none on the interim one; one Edit on the last user row; Copy is still
    there, so the transcript itself rendered.
  - busy: no Regenerate and no Edit anywhere, while the transcript still
    renders (Copy survives, the final answer is still on screen) — the buttons
    are gone, not the messages.
  - idle again: both come back, so the gate is the run, not the row.

  Then behaviour 2 — the review reproduction for a regeneration that fails to
  START: the production `startRegeneration()` runs for real, with ONLY the
  `/api/chat/start` API-adapter boundary substituted by a controlled
  rejection. After the rejection the transcript must be back to its idle
  action bar (Edit + Regenerate), the restore must survive a forced
  session-HTML cache read, and a render that ran while busy must never be
  served to the idle view (the cache signature covers the busy flag).

  Then behaviour 3 — the review reproduction for a regeneration that is
  ACCEPTED and then ends in an application-level error: the production
  `startRegeneration()` runs for real, the `/api/chat/start` response and the
  SSE stream are controlled at the HTTP boundary only (a real native
  EventSource carries a real `apperror` frame), and the production sidebar
  reconciler is left in place. The settled transcript must paint the IDLE
  action bar (Edit + Regenerate), and both text markers must survive in their
  own assistant rows.

  Finally, a native EventSource ends without a terminal frame. Both the
  stream-owned snapshot recovery and the sidebar-first idle recovery must
  restore the action bar immediately, even when regeneration keeps the same
  session message count. Run the sidebar-first path at desktop, narrow and
  touch-phone sizes; tap Regenerate and the restored Edit on the phone.

  Undo is pinned at the source instead (see
  tests/test_issue8643_busy_message_actions.py): the `undoBtn` const in
  renderMessages() is never inserted into `footHtml`, so no Undo button exists
  in the DOM to observe in any state.

SCOPE
  Agent-free, like tests/browser_smoke.py: the real server.py on an ephemeral
  port with isolated temp state, and a transcript imported through the public
  API. `HERMES_DISABLE_LAZY_INSTALLS=1` is forced because there is no agent
  here: without it the server relaunches into a managed runtime that owes a
  full dependency sync before it can serve /health (tests/conftest.py sets the
  same variable for its own server).

USAGE
  python tests/browser_8643_busy_message_actions.py [--screenshots DIR]

EXIT CODES
  0 — every check passed
  1 — a check failed (regression)
  2 — Playwright or its Chromium executable is unavailable
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from browser_conversation_lifecycle import (  # noqa: E402
    _capture_page_errors,
    _start_webui_server,
    _terminate_process,
)


INTERIM_TEXT = "Interim answer: checking the workspace first."
FINAL_TEXT = "Final answer for the running-turn action buttons."
USER_TEXT = "Run the two-step job."

MESSAGES = [
    {"role": "user", "content": USER_TEXT},
    {
        "role": "assistant",
        "content": INTERIM_TEXT,
        "tool_calls": [
            {
                "id": "busy-actions-tool-1",
                "type": "function",
                "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "busy-actions-tool-1", "content": "/workspace"},
    {"role": "assistant", "content": FINAL_TEXT},
]

# The tool-result row is not a renderable message, so the visible transcript is
# user(0) / interim assistant(1) / final assistant(3).
FINAL_ASSISTANT_IDX = "3"
INTERIM_ASSISTANT_IDX = "1"
LAST_USER_IDX = "0"


# ── Behaviour 3 fixtures: an ACCEPTED start that ends in an application error ──
# The multi-turn conversation the review reproduction used: a previous answer
# carrying a list, a table and a code block, then the turn being regenerated.
# startRegeneration() truncates the transcript to the user row it regenerates,
# so the PREVIOUS answer stays on screen and the error row lands after it —
# two assistant rows, two Copy controls, exactly what the reviewer observed.
APPERROR_PREVIOUS_ANSWER_TEXT = (
    "Previous answer with a list, a table and a code block.\n\n"
    "- first bullet\n"
    "- second bullet\n\n"
    "| column | value |\n"
    "|---|---|\n"
    "| alpha | 1 |\n\n"
    "```js\nconsole.log('probe');\n```\n"
)
APPERROR_FIRST_USER_TEXT = "First question about the workspace layout."
APPERROR_SECOND_USER_TEXT = "Second question, please regenerate this turn."
APPERROR_SECOND_ANSWER_TEXT = "Second answer that the regeneration replaces."

# The prose line the rendered transcript keeps verbatim: the markdown markers
# above (bullets, table pipes, code fences) are consumed by rendering, so a
# "did the session load?" wait cannot key on the raw source string.
APPERROR_PREVIOUS_ANSWER_PROSE = (
    "Previous answer with a list, a table and a code block."
)

APPERROR_MESSAGES = [
    {"role": "user", "content": APPERROR_FIRST_USER_TEXT},
    {"role": "assistant", "content": APPERROR_PREVIOUS_ANSWER_TEXT},
    {"role": "user", "content": APPERROR_SECOND_USER_TEXT},
    {"role": "assistant", "content": APPERROR_SECOND_ANSWER_TEXT},
]

# The error frame the server sends for a gateway turn that produced no
# assistant message (api/gateway_chat.py). `session_id` is added so the frame
# targets THIS session — the client only rebuilds the transcript it owns.
APPERROR_FRAME = {
    "label": "Gateway returned no response",
    "type": "gateway_empty_response",
    "message": "Gateway returned no assistant message for this turn.",
    "hint": "Check that Hermes Gateway API server is running and reachable.",
}

# Raw message indices in the settled transcript after the error row is
# appended: user(0) / previous assistant(1) / pending user(2) / error(3).
APPERROR_PREVIOUS_ASSISTANT_IDX = "1"
APPERROR_PENDING_USER_IDX = "2"
APPERROR_ERROR_ASSISTANT_IDX = "3"

RECOVERED_TEXT = "Recovered final answer after the stream disconnected."
RECOVERY_MESSAGES = [APPERROR_MESSAGES[0],
                     {"role": "assistant", "content": APPERROR_PREVIOUS_ANSWER_PROSE},
                     *APPERROR_MESSAGES[2:]]
REGENERATE_BUTTON = '#msgInner button[onclick="regenerateResponse(this)"]'
EDIT_BUTTON = '#msgInner button[onclick="editMessage(this)"]'


IMPORT_AND_LOAD_JS = """async ({messages, finalText}) => {
  const response = await fetch('/api/session/import', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title: 'Running-turn action buttons', messages}),
  });
  const data = await response.json();
  if (!response.ok || !data.session || !data.session.session_id) {
    throw new Error(`session import failed: ${JSON.stringify(data)}`);
  }
  // The import response seeds S.messages, so a plain loadSession treats the
  // transcript as already resident and asks the stream for known_count=N
  // (which never replays). Drop the local copy so the load renders it.
  S.session = null;
  S.messages = [];
  await loadSession(data.session.session_id, {force: true});
  await new Promise((resolve, reject) => {
    const deadline = Date.now() + 15000;
    const check = () => {
      const text = (document.querySelector('#msgInner') || {}).innerText || '';
      if (text.includes(finalText)) return resolve();
      if (Date.now() >= deadline) return reject(new Error('loaded session never rendered the final answer'));
      setTimeout(check, 50);
    };
    check();
  });
  return data.session.session_id;
}
"""


# The action buttons, by the handler each one wires, per rendered row, plus the
# flag the render was supposed to be reading. One row is an assistant segment
# (`.assistant-segment[data-msg-idx]`) or a user row (`.msg-row[data-role]`).
ACTION_BUTTONS_JS = """() => {
  const rowsWith = (selector, needle) => Array.from(document.querySelectorAll(selector))
    .filter(row => row.querySelector('.msg-foot'))
    .map(row => ({
      idx: row.dataset.msgIdx,
      has: !!row.querySelector(`button[onclick*="${needle}"]`),
    }));
  const assistant = rowsWith('.assistant-segment[data-msg-idx]', 'regenerateResponse(this)');
  const users = rowsWith('.msg-row[data-role="user"]', 'editMessage(this)');
  const copy = Array.from(document.querySelectorAll('.assistant-segment[data-msg-idx] .msg-foot'))
    .filter(foot => foot.querySelector('button[onclick*="copyMsg(this)"]')).length;
  return {
    busy: !!S.busy,
    regenerate: assistant.filter(row => row.has).map(row => row.idx),
    assistantRows: assistant.map(row => row.idx),
    edit: users.filter(row => row.has).map(row => row.idx),
    copyCount: copy,
    renderedText: ((document.querySelector('#msgInner') || {}).innerText || '').slice(0, 200),
  };
}
"""

# Idle, busy, idle again — in one synchronous evaluation, so no background
# refresh can clear S.busy between the render and the reading of the DOM.
THREE_STATES_JS = """() => {
  const probe = %s;
  const idle = probe();
  S.busy = true;
  renderMessages();
  const busy = probe();
  S.busy = false;
  renderMessages();
  const restored = probe();
  return {idle, busy, restored};
}
""" % ACTION_BUTTONS_JS


# The review reproduction: a regeneration whose /api/chat/start is REFUSED.
# The production startRegeneration() runs for real; only the api() adapter
# boundary for that one path is substituted with a controlled rejection (the
# same substitution the reviewer's probe made). The background sidebar
# reconciler is stubbed for the duration — it clears S.busy on its own when a
# /api/sessions response reports the session idle, and a clear landing between
# the rejection and the restore render would paper over the ordering bug.
# renderMessages() is wrapped to record the busy flag every render actually
# saw, which is the ordering under test read straight off the production calls.
FAILED_REGENERATION_JS = """async ({sid}) => {
  const probe = %s;
  const realReconcile = window._reconcileActiveSessionIdleStateFromList;
  window._reconcileActiveSessionIdleStateFromList = () => false;
  const realRender = window.renderMessages;
  const renderBusy = [];
  window.renderMessages = function (...args) {
    renderBusy.push(!!S.busy);
    return realRender.apply(this, args);
  };
  const realApi = window.api;
  window.api = (path, opts) => (path === '/api/chat/start')
    ? Promise.reject(Object.assign(new Error('controlled rejection: /api/chat/start refused'), {status: 500}))
    : realApi(path, opts);
  let threw = null;
  S.session.regeneration_revision = 'probe-regeneration-revision';
  try {
    await startRegeneration(sid, 'probe-regeneration-revision');
  } catch (error) {
    threw = String((error && error.message) || error);
  }
  window.api = realApi;
  window.renderMessages = realRender;
  window._reconcileActiveSessionIdleStateFromList = realReconcile;
  const afterReject = probe();
  // What switching away and back does: force the session-HTML cache read.
  _sessionHtmlCacheSid = null;
  renderMessages();
  const afterCacheRestore = probe();
  // A render that ran while busy must never be served to the idle view: paint
  // busy (that footer lands in the cache), then restore at idle.
  S.busy = true;
  renderMessages();
  const busyRender = probe();
  S.busy = false;
  _sessionHtmlCacheSid = null;
  renderMessages();
  const afterBusyTaggedRestore = probe();
  return {threw, renderBusy, afterReject, afterCacheRestore, busyRender, afterBusyTaggedRestore};
}
""" % ACTION_BUTTONS_JS


# The review reproduction for the SECOND terminal: a regeneration that is
# ACCEPTED and then ends in an application-level error.
#
# Unlike behaviour 2 (where /api/chat/start refuses), here the start SUCCEEDS
# and the turn dies later, in the `apperror` handler of attachLiveStream().
# Everything on the production path runs for real: startRegeneration(), the
# api() adapter, a native EventSource, the apperror listener, the renderer and
# the sidebar reconciler. Only the HTTP boundary is controlled — the
# /api/chat/start response, the SSE body, and the session-list row — which is
# exactly the surface the reviewer controlled.
#
# Why the HTTP boundary and not the api() adapter (as behaviour 2 does): the
# reviewer's finding is about the ORDER inside the apperror handler
# (renderMessages() vs the idle clear). Substituting the api() adapter would
# leave the real EventSource unwired, so the frame would never reach the
# handler and the section would pass vacuously. Serving a real SSE body to the
# real EventSource keeps the whole transport path live.
#
# The SSE body carries the server's own apperror payload for a gateway turn
# that produced no assistant message (api/gateway_chat.py) plus the session_id
# the server injects on every apperror frame (put_gateway_event). The frame
# deliberately has NO `session` key: that is what makes the handler take the
# "normal error branch" — append the error row to the local transcript and
# repaint — instead of adopting a server snapshot.
#
# The production sidebar reconciler stays in place (the reviewer asserted its
# source is unchanged). It only clears busy when /api/sessions reports the
# session idle, so the session-list row is answered with the running-stream
# metadata that matches the stream the client actually owns — which is what
# keeps the reconciler from racing the terminal. The behaviour-2 section had
# to stub the reconciler for the opposite reason: there the turn really did
# end idle, and a background clear between the rejection and the restore
# render would have papered over the very ordering bug under test.
#
# Inspect the exact message rows before any further render can disguise the
# terminal result. The held-stream check separately verifies the busy side.
APPERROR_SCENARIO_JS = """async ({sid, frame, previousIndex, errorIndex}) => {
  const readActions = %s;
  const bodyText = idx => document.querySelector(
    `#msgInner .assistant-segment[data-msg-idx="${idx}"] .msg-body`
  )?.innerText || '';
  const realRender = window.renderMessages;
  const renderBusy = [];
  window.renderMessages = function (...args) {
    renderBusy.push(!!S.busy);
    return realRender.apply(this, args);
  };
  try {
    const revision = S.session.regeneration_revision;
    if (!revision) throw new Error('loaded session has no regeneration revision');
    await startRegeneration(sid, revision);
    const deadline = Date.now() + 8000;
    while (!bodyText(errorIndex).includes(frame.message) && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    return {
      renderBusy,
      afterError: {
        ...readActions(),
        previousAnswerText: bodyText(previousIndex),
        errorText: bodyText(errorIndex),
      },
    };
  } finally {
    window.renderMessages = realRender;
  }
}
""" % ACTION_BUTTONS_JS


def _failures_apperror_terminal(result):
    """An accepted regeneration that dies in apperror must leave the idle bar.

    The review's P2: the apperror handler called renderMessages() BEFORE
    _setActivePaneIdleIfOwner(), so the final action-bar render still saw
    S.busy===true and the busy predicate omitted Edit and Regenerate. The
    clear that followed only touched the composer, never those action bars,
    so the settled transcript kept the button-less footer.
    """
    problems = []
    if not result["renderBusy"] or result["renderBusy"][0] is not True:
        problems.append("the accepted regeneration never painted its running turn")
    after_error = result["afterError"]
    if APPERROR_FRAME["message"] not in after_error["errorText"]:
        problems.append("the apperror text never appeared in the error row before the wait expired")
    if APPERROR_PREVIOUS_ANSWER_PROSE not in after_error["previousAnswerText"]:
        problems.append("the previous answer is gone from its original assistant row")
    if not after_error.get("copyCount"):
        problems.append(
            "no Copy button rendered after the error — the transcript itself"
            " did not render"
        )
    if after_error.get("busy"):
        problems.append(
            "the settled transcript was read with S.busy="
            f"{after_error.get('busy')!r}, expected an idle render"
        )
    if after_error.get("regenerate") != [APPERROR_ERROR_ASSISTANT_IDX]:
        problems.append(
            "Regenerate sits on rows"
            f" {after_error.get('regenerate')}, expected the turn's final"
            f" assistant row [{APPERROR_ERROR_ASSISTANT_IDX}] — the terminal"
            " action-bar render still saw the running-turn state (the review's"
            " P2)"
        )
    if after_error.get("edit") != [APPERROR_PENDING_USER_IDX]:
        problems.append(
            "Edit sits on rows"
            f" {after_error.get('edit')}, expected the last user row"
            f" [{APPERROR_PENDING_USER_IDX}] — the terminal action-bar render"
            " still saw the running-turn state (the review's P2)"
        )
    return problems


def _failures_idle(state, label):
    problems = []
    if state["busy"]:
        problems.append(f"{label}: read with S.busy={state['busy']!r}, expected an idle render")
    if state["regenerate"] != [FINAL_ASSISTANT_IDX]:
        problems.append(
            f"{label}: Regenerate sits on rows {state['regenerate']}, expected only the turn's"
            f" final assistant row [{FINAL_ASSISTANT_IDX}]"
        )
    if INTERIM_ASSISTANT_IDX in state["regenerate"]:
        problems.append(
            f"{label}: Regenerate is offered on the interim answer (row {INTERIM_ASSISTANT_IDX})"
        )
    if state["edit"] != [LAST_USER_IDX]:
        problems.append(
            f"{label}: Edit sits on rows {state['edit']}, expected only the last user row"
            f" [{LAST_USER_IDX}]"
        )
    if not state["copyCount"]:
        problems.append(f"{label}: no Copy button rendered — the transcript did not render")
    if FINAL_TEXT not in state["renderedText"]:
        problems.append(f"{label}: the final answer is not on screen — the transcript did not render")
    return problems


def _failures_busy(state):
    problems = []
    if not state["busy"]:
        problems.append(f"busy: read with S.busy={state['busy']!r}, expected a running-turn render")
    for name in ("regenerate", "edit"):
        if state[name]:
            problems.append(
                f"busy: {name} button(s) still in the DOM on rows {state[name]} — a running turn"
                f" leaves dead buttons on screen"
            )
    if not state["copyCount"]:
        problems.append("busy: no Copy button rendered — the transcript itself is missing")
    if FINAL_TEXT not in state["renderedText"]:
        problems.append("busy: the final answer is no longer rendered — the transcript went away")
    return problems


def _failures_rejected_regeneration(result):
    """A regeneration refused at /api/chat/start must leave the idle action bar.

    Every check here discriminates the review's P2: the restore render used to
    run while S.busy was still true (the busy predicate then omitted Edit and
    Regenerate, and setBusy(false) never rebuilt them), and the busy-derived
    HTML landed in the session cache without busy in its signature, so a cache
    restore kept the missing buttons on an idle transcript.
    """
    problems = []
    if "controlled rejection" not in (result.get("threw") or ""):
        problems.append(
            "the controlled rejection never reached startRegeneration()"
            f" (threw={result.get('threw')!r}) — this section would pass vacuously"
        )
    render_busy = result.get("renderBusy") or []
    if not render_busy or render_busy[0] is not True:
        problems.append(
            "startRegeneration()'s optimistic render ran with S.busy="
            f"{render_busy[0] if render_busy else None!r}, expected the running-turn state —"
            " the pending user row keeps a dead Edit until the stream attaches"
        )
    if not render_busy or render_busy[-1] is not False:
        problems.append(
            "the restored transcript was last painted with S.busy="
            f"{render_busy[-1] if render_busy else None!r}, expected idle — the busy"
            " predicate omits Edit and Regenerate from an idle transcript"
        )
    problems += _failures_idle(
        result.get("afterReject") or {}, "after the refused regeneration"
    )
    problems += _failures_idle(
        result.get("afterCacheRestore") or {}, "cache restore after the refused regeneration"
    )
    problems += _failures_busy(result.get("busyRender") or {})
    problems += _failures_idle(
        result.get("afterBusyTaggedRestore") or {}, "idle restore of a busy-written cache entry"
    )
    return problems


def _exercise_apperror(page, capture=None):
    sid = page.evaluate(IMPORT_AND_LOAD_JS, {
        "messages": APPERROR_MESSAGES, "finalText": APPERROR_SECOND_ANSWER_TEXT,
    })
    stream_id = "probe-apperror-stream"

    listing = page.request.get("/api/sessions").json()

    def boundary(route):
        url = urlsplit(route.request.url)
        if url.path == "/api/chat/start":
            payload = route.request.post_data_json
            assert payload["session_id"] == sid and payload["regenerate"] is True
            route.fulfill(json={"stream_id": stream_id, "session_id": sid})
        elif url.path == "/api/chat/stream" and parse_qs(url.query).get("stream_id") == [stream_id]:
            route.fulfill(content_type="text/event-stream", body="event: apperror\ndata: "
                          + json.dumps(dict(APPERROR_FRAME, session_id=sid)) + "\n\n")
        else:
            _session_list_for_probe(route, listing, sid, stream_id)

    pattern = "**/api/{chat/start,chat/stream,sessions}*"
    page.route(pattern, boundary)
    try:
        result = page.evaluate(APPERROR_SCENARIO_JS, {
            "sid": sid, "frame": APPERROR_FRAME,
            "previousIndex": APPERROR_PREVIOUS_ASSISTANT_IDX,
            "errorIndex": APPERROR_ERROR_ASSISTANT_IDX,
        })
        problems = _failures_apperror_terminal(result)
        if capture:
            capture(page, "apperror")
        if not problems:
            print("OK  accepted regeneration that ends in apperror — both text markers and idle actions survive")
        return problems
    finally:
        page.unroute(pattern, boundary)


def _session_list_for_probe(route, listing, sid, stream_id):
    if urlsplit(route.request.url).path != "/api/sessions":
        route.fallback()
        return
    rows = [dict(row, active_stream_id=stream_id, is_streaming=bool(stream_id))
            if row["session_id"] == sid else row for row in listing["sessions"]]
    route.fulfill(json={**listing, "sessions": rows})


def _exercise_disconnect_recovery(page, *, touch=False, sidebar_first=False, capture=None):
    """Hold a native EventSource, then end it without a terminal frame."""
    sid = page.evaluate(IMPORT_AND_LOAD_JS, {
        "messages": RECOVERY_MESSAGES, "finalText": APPERROR_SECOND_ANSWER_TEXT,
    })
    stream_id = "probe-disconnect-stream"
    settled = page.evaluate("() => S.session")
    assert settled["message_count"] == len(RECOVERY_MESSAGES), "fixture must keep the durable count unchanged"
    settled.update({
        "active_stream_id": None,
        "pending_user_message": None,
        "pending_started_at": None,
        "messages": [*RECOVERY_MESSAGES[:-1], {"role": "assistant", "content": RECOVERED_TEXT}],
    })
    listing = page.request.get("/api/sessions").json()
    streams = []
    status_requests = []
    settled_reads = []
    report_idle = False

    def boundary(route):
        url = urlsplit(route.request.url)
        query = parse_qs(url.query)
        if url.path == "/api/chat/start":
            assert route.request.post_data_json["session_id"] == sid
            assert route.request.post_data_json["regenerate"] is True
            route.fulfill(json={"session_id": sid, "stream_id": stream_id})
        elif url.path == "/api/chat/stream" and query.get("stream_id") == [stream_id]:
            streams.append(route)
        elif url.path == "/api/chat/stream/status" and query.get("stream_id") == [stream_id]:
            status_requests.append(route)
            if not sidebar_first:
                route.fulfill(json={"active": False, "replay_available": False})
        elif url.path == "/api/session" and query.get("session_id") == [sid] and report_idle:
            if query.get("messages") == ["0"]:
                metadata = {k: v for k, v in settled.items() if k not in {"messages", "regeneration_revision"}}
                route.fulfill(json={"session": metadata})
            else:
                settled_reads.append(route.request.url)
                route.fulfill(json={"session": settled})
        elif (url.path == "/api/session" and query.get("session_id") == [sid]
              and query.get("expand_renderable") == ["1"]):
            settled_reads.append(route.request.url)
            route.fulfill(json={"session": settled})
        else:
            _session_list_for_probe(route, listing, sid, None if report_idle else stream_id)

    pattern = "**/api/{chat/start,chat/stream,chat/stream/status,session,sessions}*"
    page.route(pattern, boundary)
    try:
        page.evaluate("""({sid, marker}) => {
          const render = window.renderMessages;
          const shift = window.shiftQueuedSessionMessage;
          const api = window.api;
          window.__recoveredActions = null;
          window.__recoveryDrains = [];
          window.__reconnectReplyHandled = false;
          window.api = async function(path, ...args) {
            const data = await api.call(this, path, ...args);
            if (path.startsWith('/api/chat/stream/status?')) {
              // After the suspended reconnect handler's promise continuations.
              setTimeout(() => { window.__reconnectReplyHandled = true; }, 0);
            }
            return data;
          };
          window.shiftQueuedSessionMessage = function(owner) {
            window.__recoveryDrains.push(owner);
            return shift.apply(this, arguments);
          };
          window.renderMessages = function() {
            const result = render.apply(this, arguments);
            if (window.__recoveredActions === null && S.session?.session_id === sid
                && document.querySelector('#msgInner').innerText.includes(marker)) {
              window.__recoveredActions = (%s)();
            }
            return result;
          };
          window.__restoreRecoveryProbes = () => {
            window.renderMessages = render;
            window.shiftQueuedSessionMessage = shift;
            window.api = api;
          };
        }""" % ACTION_BUTTONS_JS, {"sid": sid, "marker": RECOVERED_TEXT})
        regenerate = page.locator(REGENERATE_BUTTON)
        if not touch:
            regenerate.locator("xpath=ancestor::div[@class='msg-foot'][1]").hover()
        with page.expect_request(lambda request: urlsplit(request.url).path == "/api/chat/stream", timeout=10000):
            (regenerate.tap if touch else regenerate.click)(timeout=10000)
        page.wait_for_function("stream => S.busy && S.activeStreamId === stream", arg=stream_id)
        assert len(streams) == 1 and not settled_reads, "did not reach the held EventSource"
        busy = page.evaluate(ACTION_BUTTONS_JS)
        pending_user = page.locator('#msgInner .msg-row[data-role="user"]').last
        if capture:
            pending_user.scroll_into_view_if_needed()
            if not touch:
                pending_user.hover()
            capture(page, "busy")
        if sidebar_first:
            with page.expect_request(lambda request: urlsplit(request.url).path == "/api/chat/stream/status", timeout=5000):
                streams[0].fulfill(content_type="text/event-stream", body=": connection closed\n\n")
            report_idle = True
            page.evaluate("() => renderSessionList()")
            page.wait_for_function("() => !S.busy && !S.activeStreamId")
            assert len(status_requests) == 1, "expected exactly one held reconnect probe"
        else:
            streams[0].fulfill(content_type="text/event-stream", body=": connection closed\n\n")

        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        try:
            page.wait_for_function("() => window.__recoveredActions !== null && !S.busy", timeout=8000)
        except PlaywrightTimeoutError:
            if capture:
                pending_user.scroll_into_view_if_needed()
                capture(page, "recovered")
            state = page.evaluate(ACTION_BUTTONS_JS)
            return [f"recovery never installed the settled answer ({len(settled_reads)} transcript reads): {state}"]
        finally:
            if sidebar_first:
                # Let the sidebar path finish (or fail) without the reconnect
                # attempt fetching the same transcript behind its back.
                status_requests[0].fulfill(json={"active": False, "replay_available": False})
        if sidebar_first:
            page.wait_for_function("() => window.__reconnectReplyHandled", timeout=5000)
        restored = page.evaluate("() => window.__recoveredActions")
        assert len(settled_reads) == 1, "did not recover from exactly one settled snapshot"
        problems = []
        if sidebar_first:
            status = page.locator('#composerStatus').text_content()
            if status:
                problems.append(f"stale reconnect reply changed the idle composer status: {status}")
        # Direct settlement must drain once. The sidebar path may also finish
        # the old stream's cleanup, but must never drain twice or for another pane.
        drains = page.evaluate("() => window.__recoveryDrains")
        if not (drains == [sid] or (sidebar_first and not drains)):
            problems.append(f"unexpected queue drains: {drains}")
        if capture:
            final = page.locator(f'#msgInner .assistant-segment[data-msg-idx="{APPERROR_ERROR_ASSISTANT_IDX}"] .msg-foot')
            final.scroll_into_view_if_needed()
            if not touch:
                final.hover()
            capture(page, "recovered")
        if busy["edit"] or busy["regenerate"] or not busy["copyCount"]:
            problems.append(f"held running turn: unexpected action bar {busy}")
        if restored["busy"] or restored["edit"] != [APPERROR_PENDING_USER_IDX] or restored["regenerate"] != [APPERROR_ERROR_ASSISTANT_IDX]:
            problems.append(f"first recovered render: Edit/Regenerate missing or on wrong rows {restored}")
        previous = page.locator(f'.assistant-segment[data-msg-idx="{APPERROR_PREVIOUS_ASSISTANT_IDX}"] .msg-body')
        if APPERROR_PREVIOUS_ANSWER_PROSE not in previous.inner_text(timeout=5000):
            problems.append("previous answer was not preserved during recovery")
        if touch and not problems:
            page.locator(EDIT_BUTTON).tap(timeout=10000)
            editor = page.locator('#msgInner .msg-edit-area')
            if editor.input_value(timeout=5000) != APPERROR_SECOND_USER_TEXT:
                problems.append("touch Edit did not open the recovered prompt")
            else:
                if capture:
                    capture(page, "editing")
                page.locator('#msgInner .msg-edit-cancel').tap(timeout=5000)
                print("OK  phone touch — Regenerate starts and restored Edit opens the correct prompt")
        if not problems:
            mechanism = "sidebar-first, same count" if sidebar_first else "stream-owned, one queue drain"
            print(f"OK  disconnect recovery ({mechanism}) — the first recovered render offers Edit and Regenerate")
        return problems
    finally:
        page.unroute(pattern, boundary)
        page.evaluate("() => window.__restoreRecoveryProbes?.()")


VIEWPORTS = (
    ("desktop", 1440, 900, False),
    ("narrow", 820, 900, False),
    ("phone", 390, 844, True),
)


def _exercise_viewport(context, name, width, touch, screenshots):
    page = context.new_page()
    errors = _capture_page_errors(page)
    page.goto("/", wait_until="domcontentloaded")
    page.wait_for_selector("#msg", state="visible", timeout=15000)
    page.wait_for_function("() => typeof window._autoScrollFollow === 'boolean'", timeout=15000)
    assert page.evaluate("() => window.innerWidth") == width, "wrong layout viewport"
    assert page.evaluate("() => matchMedia('(hover: none) and (pointer: coarse)').matches") is touch

    def capture(page, state):
        if screenshots is not None:
            page.screenshot(path=screenshots / f"{name}-{state}.png", animations="disabled")

    failures = []
    if name == "desktop":
        page.evaluate(IMPORT_AND_LOAD_JS, {"messages": MESSAGES, "finalText": FINAL_TEXT})
        states = page.evaluate(THREE_STATES_JS)

        idle_problems = _failures_idle(states["idle"], "idle")
        failures += idle_problems
        if not idle_problems:
            print("OK  idle — Regenerate only on the turn's final assistant row,"
                  " Edit on the last user row")

        busy_problems = _failures_busy(states["busy"])
        failures += busy_problems
        if not busy_problems:
            print("OK  busy — Regenerate and Edit are gone while the turn runs")

        restored_problems = _failures_idle(states["restored"], "idle again")
        failures += restored_problems
        if not restored_problems:
            print("OK  idle again — both buttons return once the turn is over")

        # The turn-final rule must hold in every state, busy or not.
        for label, state in states.items():
            if INTERIM_ASSISTANT_IDX in state["regenerate"]:
                failures.append(
                    f"{label}: Regenerate is offered on the interim answer"
                    f" (row {INTERIM_ASSISTANT_IDX})"
                )

        # Behaviour 2: the regeneration whose start is refused.
        session_id = page.evaluate(
            "() => (S.session && S.session.session_id) || ''"
        )
        rejected = page.evaluate(FAILED_REGENERATION_JS, {"sid": session_id})
        rejected_problems = _failures_rejected_regeneration(rejected)
        failures += rejected_problems
        if not rejected_problems:
            print("OK  rejected regeneration — Edit and Regenerate return once"
                  " /api/chat/start refuses")
            print("OK  cache restore after a refused regeneration keeps both buttons")
            print("OK  a busy-written cache entry is never served to the idle transcript")
            print("OK  the refused regeneration paints no buttons while the turn runs")
        failures += _exercise_apperror(page, capture)
        failures += _exercise_disconnect_recovery(page)

    failures += _exercise_disconnect_recovery(
        page, touch=touch, sidebar_first=True, capture=capture if screenshots is not None else None,
    )
    failures.extend(f"pageerror: {kind} {text}" for kind, text in errors)
    if not failures:
        print(f"OK  {name} viewport — busy and recovered action bars")
    return failures


def main(*, screenshots=None) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SETUP FAIL: playwright is not installed", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    state_tmp = tempfile.TemporaryDirectory(prefix="hermes-8643-busy-actions-")
    state_dir = Path(state_tmp.name)
    agent_dir = state_dir / "no-agent"
    workspace_dir = state_dir / "workspace"
    agent_dir.mkdir()
    workspace_dir.mkdir()
    (agent_dir / "run_agent.py").write_text('"""Test-only agent stub."""\n', encoding="utf-8")

    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY"):
            env.pop(key, None)
    for key in ("API_SERVER_KEY", "HERMES_WEBUI_PASSWORD"):
        env.pop(key, None)
    # No agent here, so keep the server from relaunching into a managed runtime
    # that owes a full dependency sync before /health answers (conftest.py does
    # the same for its own server).
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    env.update({
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_TEST_NETWORK_BLOCK": "1",
        "HERMES_WEBUI_STATE_DIR": str(state_dir / "webui-state"),
        "HERMES_HOME": str(state_dir / "hermes-home"),
        "HERMES_BASE_HOME": str(state_dir / "hermes-home"),
        "HERMES_CONFIG_PATH": str(state_dir / "hermes-home" / "config.yaml"),
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": str(agent_dir),
        "HERMES_WEBUI_DEFAULT_WORKSPACE": str(workspace_dir),
    })

    failures: list[str] = []
    proc = log = log_path = None
    try:
        proc, log, log_path, base_url = _start_webui_server(repo_root, env, state_dir)
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(
                    headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
            except Exception as launch_error:  # noqa: BLE001
                # A machine can have the playwright PACKAGE installed and no
                # browser binary. That is "the optional gate cannot run", not
                # a gate failure: report the documented skip (exit 2) instead
                # of a driver error (exit 1) the pytest callers turn into red
                # (#8668 review 2026-11-18). Failures AFTER a successful
                # launch still count as failures.
                message = str(launch_error)
                if "Executable doesn't exist at" in message:
                    print(
                        "SKIP: playwright chromium executable is unavailable; "
                        "run `playwright install chromium`",
                        file=sys.stderr,
                    )
                    return 2
                raise
            for name, width, height, touch in VIEWPORTS:
                context = browser.new_context(
                    base_url=base_url, viewport={"width": width, "height": height},
                    is_mobile=touch, has_touch=touch, locale="en-US",
                    reduced_motion="reduce", service_workers="block",
                )
                context.add_init_script("if (window === window.top) sessionStorage.setItem('hermes-update-checked', '1')")
                try:
                    problems = _exercise_viewport(context, name, width, touch, screenshots)
                    failures.extend(f"{name} ({width}×{height}): {problem}" for problem in problems)
                finally:
                    context.close()
            browser.close()
    except Exception as error:  # noqa: BLE001 — reported as a gate failure
        failures.append(f"driver error: {type(error).__name__}: {error}")
        if isinstance(error, AssertionError):
            import traceback

            failures.append(traceback.format_exc())
        if log_path is not None and Path(log_path).exists():
            tail = Path(log_path).read_text(encoding="utf-8", errors="replace")[-2000:]
            failures.append("last server log tail:\n" + tail)
    finally:
        if proc is not None:
            _terminate_process(proc)
        if log is not None:
            log.close()
        state_tmp.cleanup()

    if failures:
        print("\nRUNNING-TURN ACTION BUTTONS GATE FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("\nRUNNING-TURN ACTION BUTTONS GATE PASSED")
    return 0


if __name__ == "__main__":
    from argparse import ArgumentParser

    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", type=Path, help="save optional busy and recovered viewport evidence")
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    sys.exit(main(screenshots=args.screenshots))
