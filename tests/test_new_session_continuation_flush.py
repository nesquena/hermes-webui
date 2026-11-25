"""New Chat must not clear the departing draft's restored continuation token.

Review round on #6423 (maintainer, replayed against production frontend modules
in Chromium): ``newSession()`` flushed the departing composer with an explicit
empty fourth argument to ``_saveComposerDraftNow``, so an unchanged restored
goal-continuation draft lost its ``goal_continuation_id`` on the New Chat
hand-off. The text survived, but returning to that chat and sending it posted an
empty token to ``/api/chat/start`` and the goal loop stopped. The ordinary
session-switch save and the repaired profile-switch save already preserved a
matching token; this file pins the same rule for the New Chat source flush.

Covered here, driving the real shipped modules in a headless Chromium page:

1. regression: an unchanged restored continuation is flushed WITH its token, and
   the peek does not consume the one-shot binding;
2. control: an edited departing draft carries no token (round-7 rule);
3. control: a token bound to another owner's draft is never flushed with this
   session's draft;
4. durable save/reload: the flushed body is what a reload restores, and the next
   Send still posts that token to ``/api/chat/start``.

API responses are controlled fixtures. This is frontend request-contract and
restore-binding evidence, not a claim about backend persistence or provider
execution.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# The New Chat navigation suite already boots the real production modules in a
# headless page; reuse its harness rather than growing a second copy of it.
from tests.test_new_session_navigation_supersession import (
    COMPOSER_AUTHORITY_JS,
    MESSAGES_JS,
    PANELS_JS,
    SESSIONS_JS,
    WORKSPACE_JS,
    _BOOTSTRAP,
    _OVERRIDES,
)

ROOT = Path(__file__).parents[1]

CONT_TOKEN = "0123abcd"
A_TEXT = "restored continuation text"
DRAFT_SID = "session-a"


@pytest.fixture()
def composer_page():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(
            "http://local.test/",
            lambda route: route.fulfill(
                body="<!doctype html><html><body></body></html>",
                content_type="text/html",
            ),
        )
        page.goto("http://local.test/")
        page.evaluate(_BOOTSTRAP)
        page.add_script_tag(content=COMPOSER_AUTHORITY_JS)
        page.evaluate("() => { window.__fixtureApi = api; }")
        page.add_script_tag(path=str(WORKSPACE_JS))
        page.evaluate(
            "() => { window.__productionLoadDir = loadDir; window.api = window.__fixtureApi; }"
        )
        page.add_script_tag(path=str(PANELS_JS))
        page.add_script_tag(path=str(SESSIONS_JS))
        page.add_script_tag(path=str(MESSAGES_JS))
        page.evaluate(_OVERRIDES)
        page.evaluate(
            r"""() => {
              for (const name of ['_restoreExpandedDirs', 'renderBreadcrumb', 'renderFileTree',
                  '_refreshGitBadge', 'clearPreview', '_syncWorkspaceBirthtimeSupportScope',
                  '_profileSwitchPanelLoad', '_refreshProfileSwitchBackground', '_clearPendingSkill',
                  'closeSessionActionMenu', 'showSessionListSkeleton', '_invalidateSessionListRenders']) {
                window[name] = () => {};
              }
              window._workspaceRouteForPath = () => null;
              window.__toasts = [];
              window.showToast = (...args) => window.__toasts.push(args);
              window._deferWorkspaceRefreshForSession = () => {};
            }"""
        )
        yield page, errors
        page.close()
        browser.close()


def _open_session_a(page, *, text: str = A_TEXT, owner: str = DRAFT_SID) -> None:
    """Render session A with ``text`` in the composer and a restored binding."""
    page.evaluate(
        """({ text, owner, token }) => {
          S.session = {
            session_id: 'session-a', profile: 'default', workspace: '/workspace-a',
            title: 'A', message_count: 1, messages: [],
            composer_draft: { text: '', files: [] }, model: 'test-model',
          };
          S.messages = [];
          S.pendingFiles = [];
          document.getElementById('msg').value = text;
          _setRestoredGoalContinuationDraft(token, text, owner);
        }""",
        {"text": text, "owner": owner, "token": CONT_TOKEN},
    )


def _start_new_chat(page) -> None:
    page.evaluate(
        """() => {
          window.__done = newSession().then(
            result => { window.__result = result; },
            error => { window.__error = String(error && error.stack || error); },
          );
        }"""
    )
    page.wait_for_function(
        """() => !!window.__pendingRequest('new')
          || !!window.__error"""
    )


def _settle_new_chat(page, created_sid: str = "session-created") -> None:
    page.evaluate(
        """sid => window.__resolveRequest('new', null, {session: {
          session_id: sid, profile: 'default', workspace: '/workspace-a', title: 'Untitled',
          message_count: 0, messages: [], composer_draft: {text: '', files: []}, model: 'test-model'
        }})""",
        created_sid,
    )
    page.evaluate("() => window.__done")


def _source_draft_bodies(page, sid: str = DRAFT_SID) -> list[dict]:
    return page.evaluate(
        """sid => window.__apiLog
          .filter(call => call.kind === 'draft' && call.sid === sid && call.body)
          .map(call => call.body)""",
        sid,
    )


def _last_source_draft(page, sid: str = DRAFT_SID) -> dict:
    bodies = _source_draft_bodies(page, sid)
    assert bodies, "the New Chat hand-off never flushed the departing draft"
    return bodies[-1]


def test_unchanged_restored_continuation_keeps_its_token(composer_page):
    page, errors = composer_page
    _open_session_a(page)

    _start_new_chat(page)
    body = _last_source_draft(page)

    assert body["text"] == A_TEXT, body
    assert body["goal_continuation_id"] == CONT_TOKEN, body
    # The peek must not consume the one-shot binding: the user can still return
    # to this chat and resend the draft as a continuation in-page.
    peeked = page.evaluate("() => _peekRestoredGoalContinuationDraft()")
    assert peeked and peeked["id"] == CONT_TOKEN, peeked

    _settle_new_chat(page)
    assert errors == [], errors


def test_edited_departing_draft_carries_no_token(composer_page):
    page, errors = composer_page
    _open_session_a(page)
    page.evaluate(
        "() => { const msg = document.getElementById('msg'); msg.value = msg.value + ' (edited)'; }"
    )

    _start_new_chat(page)
    body = _last_source_draft(page)

    assert body["text"].endswith("(edited)"), body
    assert body["goal_continuation_id"] == "", body

    _settle_new_chat(page)
    assert errors == [], errors


def test_token_bound_to_another_owner_is_not_flushed(composer_page):
    page, errors = composer_page
    # The binding was restored for a different session; session A merely still
    # reuses the same words. Flushing A's draft must not carry that token.
    _open_session_a(page, owner="session-other")

    _start_new_chat(page)
    body = _last_source_draft(page)

    assert body["text"] == A_TEXT, body
    assert body["goal_continuation_id"] == "", body

    _settle_new_chat(page)
    assert errors == [], errors


def test_flushed_token_survives_reload_and_still_reaches_chat_start(composer_page):
    page, errors = composer_page
    _open_session_a(page)

    _start_new_chat(page)
    flushed = _last_source_draft(page)
    assert flushed["goal_continuation_id"] == CONT_TOKEN, flushed
    _settle_new_chat(page)

    # Replay the persisted draft the way the server would return it on reload.
    page.evaluate(
        """draft => {
          window.__reloadedDraft = draft;
          window.__loadDone = loadSession('session-a', {force: true}).catch(
            error => { window.__loadError = String(error && error.stack || error); }
          );
        }""",
        {"text": flushed["text"], "files": [], "goal_continuation_id": flushed["goal_continuation_id"]},
    )
    page.wait_for_function("() => !!window.__pendingRequest('metadata', 'session-a')")
    page.evaluate(
        """() => window.__resolveRequest('metadata', 'session-a', {session: {
          session_id: 'session-a', profile: 'default', workspace: '/workspace-a', title: 'A',
          message_count: 1, messages: [], tool_calls: [], _messages_truncated: false,
          _messages_offset: 0,
          composer_draft: window.__reloadedDraft,
        }})"""
    )
    page.wait_for_function("() => !!window.__pendingRequest('messages', 'session-a')")
    page.evaluate(
        """() => window.__resolveRequest('messages', 'session-a', {session: {
          session_id: 'session-a', profile: 'default', workspace: '/workspace-a', title: 'A',
          message_count: 1, messages: [{role: 'assistant', content: 'A transcript'}],
          tool_calls: [], _messages_truncated: false, _messages_offset: 0,
          composer_draft: window.__reloadedDraft,
        }})"""
    )
    page.evaluate("() => window.__loadDone")

    restorable = page.evaluate(
        """() => ({
          value: document.getElementById('msg').value,
          peeked: _peekRestoredGoalContinuationDraft(),
        })"""
    )
    assert restorable["value"] == A_TEXT, restorable
    assert restorable["peeked"] and restorable["peeked"]["id"] == CONT_TOKEN, restorable
    assert restorable["peeked"]["ownerSid"] == "session-a", restorable

    page.evaluate(
        """() => {
          window.__sendDone = send().catch(error => {
            window.__sendError = String(error && error.stack || error);
          });
        }"""
    )
    page.wait_for_function(
        "() => window.__apiLog.some(call => call.kind === 'chat-start') || !!window.__sendError"
    )
    starts = page.evaluate(
        "() => window.__apiLog.filter(call => call.kind === 'chat-start').map(call => call.body)"
    )
    assert starts, page.evaluate("() => window.__sendError")
    assert starts[0]["goal_continuation_id"] == CONT_TOKEN, starts[0]
    assert starts[0]["message"] == A_TEXT, starts[0]

    page.evaluate("() => window.__sendDone")
    assert errors == [], errors
