"""Regression tests for issue #7299 — hidden-tab active-stream poll
keeps retrying deleted sessions forever, generating an invisible stream
of permanent 404 requests from stale background tabs.

When a tab is hidden, Hermes WebUI replaces the persistent per-session
SSE connection with a lightweight ``GET /api/session/status`` poll
every six seconds. If the tracked session has been deleted or no
longer exists in the active state directory, the endpoint returns
``404``, but the client previously converted every non-2xx response to
``null`` and kept the interval alive forever — one tab generates up to
ten requests per minute whenever browser timer throttling allows it.

The fix: in ``_startHiddenActiveStreamPoll``'s fetch chain, treat
``404 Not Found`` and ``410 Gone`` as **terminal** for the
session-owned poll — call ``_stopHiddenActiveStreamPoll()`` so the
interval and the bound ``_sessionStreamHiddenPollSid`` are cleared
together. Transient failures (5xx, rate-limit, network error) keep
polling because they do not necessarily mean the session is gone; a
later tick may catch a server-initiated turn starting while the tab
remains hidden.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Source-shape: the fetch chain in ``_startHiddenActiveStreamPoll`` must
# gate the terminal 404/410 path through ``_stopHiddenActiveStreamPoll``
# and the bound sid check. A silent reversion to the pre-fix
# "convert all non-2xx to null and keep polling" form fails the suite.
# ---------------------------------------------------------------------------


def test_hidden_poll_stops_on_404_response() -> None:
    """The fetch chain must call ``_stopHiddenActiveStreamPoll`` on a 404.

    A 404 from ``/api/session/status`` means the session has been
    deleted or no longer exists in the active state directory. The
    pre-fix code converted every non-2xx to ``null`` and never
    stopped the interval — so the same 404 fired every six seconds
    for the lifetime of the hidden tab. Pin the contract that a 404
    now tears down the poll.
    """
    # The check is anchored on the hidden-poll function so a future
    # refactor cannot satisfy it with an unrelated status-404
    # handler in a different code path.
    start = MESSAGES_JS.find("function _startHiddenActiveStreamPoll(sid)")
    assert start != -1
    # 3.6 KB is enough to cover the fetch → first .then → second .then
    # block, even after the #7299 fix added the 404/410 terminal
    # branch. The hidden poll is one of the larger functions in the
    # file, so we take a wide window to be resilient to a small
    # reformat.
    body = MESSAGES_JS[start:start + 8000]
    assert "r.status === 404" in body, (
        "the hidden poll must check r.status === 404 in the fetch "
        "response so a deleted session no longer triggers an "
        "infinite 404 loop on a stale background tab"
    )
    assert "_stopHiddenActiveStreamPoll" in body, (
        "the hidden poll must call _stopHiddenActiveStreamPoll when "
        "the response is terminal (404/410) so the interval is "
        "cleared instead of left running"
    )


def test_hidden_poll_stops_on_410_response() -> None:
    """The fetch chain must also call ``_stopHiddenActiveStreamPoll`` on 410.

    Some session-state endpoints use ``410 Gone`` (rather than
    ``404 Not Found``) for "this session id is permanently gone".
    Both are terminal for the hidden poll — the pre-fix code treated
    them identically, so the fix must handle both.
    """
    start = MESSAGES_JS.find("function _startHiddenActiveStreamPoll(sid)")
    body = MESSAGES_JS[start:start + 8000]
    assert "r.status === 410" in body, (
        "the hidden poll must check r.status === 410 in the fetch "
        "response so a 'gone' session also tears down the poll, "
        "matching the 404 contract"
    )


def test_hidden_poll_404_stop_guards_on_bound_sid() -> None:
    """The 404/410 stop path must check the bound ``_sessionStreamHiddenPollSid``.

    The same fetch chain is invoked on every tick of a 6-second
    interval. A race can let a stale fetch's response land after
    the user switched sessions — in that case ``_sessionStreamHiddenPollSid``
    no longer matches the fetched ``sid``, and the stop must NOT
    tear down the *new* session's poll. Pin the contract.
    """
    start = MESSAGES_JS.find("function _startHiddenActiveStreamPoll(sid)")
    body = MESSAGES_JS[start:start + 8000]
    # The 404/410 branch must check the bound sid before stopping
    # — pinned as a string match because the JS module is large
    # and we want a regression to show up at a glance in CI.
    assert "_sessionStreamHiddenPollSid === sid" in body, (
        "the 404/410 stop path must guard on "
        "_sessionStreamHiddenPollSid === sid so a stale fetch's "
        "response cannot tear down a different session's poll"
    )


def test_hidden_poll_preserves_transient_failure_retry() -> None:
    """5xx / rate-limit / network errors MUST keep polling (not stop).

    The fix only narrows the stop path to terminal 404/410. Transient
    failures (5xx, 429, network error) still produce ``null`` and
    the next tick re-fires. A regression that broadens the stop
    path to all non-2xx would re-introduce the original bug in
    the opposite direction — the poll would die on a single
    503 and never recover.
    """
    start = MESSAGES_JS.find("function _startHiddenActiveStreamPoll(sid)")
    body = MESSAGES_JS[start:start + 8000]
    # The pre-fix ``r.ok ? r.json() : null`` fallback must still be
    # present for any non-ok / non-terminal response.
    assert "r.ok ? r.json() : null" in body, (
        "the 5xx / rate-limit / network-error path must still "
        "produce null and let the next tick re-fire; only 404/410 "
        "should tear down the poll"
    )


def test_hidden_poll_starts_interval_unchanged() -> None:
    """The interval is unchanged (still 6000 ms).

    The fix is purely about the response handling, not the
    cadence. A regression that doubles the interval (or removes
    the immediate-fire) breaks the original contract — a turn
    already running when the tab goes hidden must be caught
    without waiting a full interval.
    """
    start = MESSAGES_JS.find("function _startHiddenActiveStreamPoll(sid)")
    body = MESSAGES_JS[start:start + 8000]
    assert "setInterval(tick, 6000)" in body, (
        "the hidden poll must keep the 6000ms interval; the fix is "
        "in the response handling, not the cadence"
    )
    # The immediate-fire call (no full interval wait) must also
    # still be present so a turn already running when the tab goes
    # hidden is caught without waiting a full interval.
    assert "tick();" in body, (
        "the immediate-fire tick() call must be preserved so a "
        "turn already running when the tab goes hidden is caught "
        "without waiting a full interval"
    )
