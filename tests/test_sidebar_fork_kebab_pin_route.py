"""Route-level gate: "Pin conversation" from an expanded fork pins the FORK.

WHY THIS EXISTS
  An expanded parent's action button used to be centred on the whole row
  (`.session-actions`, `top:50%`), which put it on top of the first fork's own
  kebab; the hit test resolved to the parent, so the menu opened for the parent
  and "Pin conversation" sent the PARENT's `session_id` to
  `POST /api/session/pin`. Component-level geometry tests cannot prove that: the
  request body is built from the menu's captured session, one level up.

WHAT IT CHECKS (real server, real stylesheet, Chromium with a hover-capable
pointer)
  - a parent with two forks expands from its "N children" badge;
  - a real mouse click on the FIRST fork's kebab opens the FORK's menu;
  - the production Pin request carries the fork's `session_id`, not the
    parent's, and `pinned: true`, and the server accepted it (HTTP 200);
  - the server state afterwards has the fork pinned and the parent not. The
    list is polled to a deadline rather than read after a fixed sleep: the pin
    is written under the session's lock and the list route can serve one more
    stale snapshot, which made a sleep-based read report a false negative.

  Playwright is optional in this checkout: without it the test skips rather
  than failing the suite.
"""
import json
import time
import urllib.request

import pytest

from tests.conftest import _post

pytestmark = pytest.mark.usefixtures("test_server")

PIN_LABEL = "Pin conversation"
PIN_SETTLE_TIMEOUT = 10.0


def _sessions_by_id(base_url):
    with urllib.request.urlopen(base_url + "/api/sessions", timeout=15) as response:
        payload = json.loads(response.read())
    return {row["session_id"]: row for row in payload.get("sessions") or []}


def _sessions_until(base_url, predicate):
    """Poll the session list until ``predicate`` holds, then return the rows.

    A pin is written under the session's own lock and published afterwards, so
    the list route can still serve its just-built snapshot to the very next
    read: a fixed sleep here was a race that reported "not pinned" while the
    pin was in fact persisted (it showed up ~2s later). Polling to a deadline
    is the assertion; the returned rows are the last observation either way, so
    a real failure still fails.
    """
    deadline = time.monotonic() + PIN_SETTLE_TIMEOUT
    rows = _sessions_by_id(base_url)
    while not predicate(rows) and time.monotonic() < deadline:
        time.sleep(0.1)
        rows = _sessions_by_id(base_url)
    return rows


def _import_session(base_url, title, messages):
    data = _post(base_url, "/api/session/import", {"title": title, "messages": messages})
    assert data.get("session"), f"session import failed: {data}"
    return data["session"]["session_id"]


def _branch(base_url, parent_sid, title):
    data = _post(
        base_url,
        "/api/session/branch",
        {"session_id": parent_sid, "keep_count": 1, "title": title},
    )
    assert data.get("session_id"), f"branch failed: {data}"
    return data["session_id"]


def test_pin_from_an_expanded_fork_targets_the_fork(base_url):
    playwright = pytest.importorskip("playwright.sync_api")

    parent_sid = _import_session(
        base_url,
        "Route-level parent conversation",
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hello back"},
        ],
    )
    fork1_sid = _branch(base_url, parent_sid, "Route-level fork one")
    fork2_sid = _branch(base_url, parent_sid, "Route-level fork two")

    pin_requests = []
    pin_statuses = []
    page_errors = []

    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))

            def _record_pin(request):
                if request.method == "POST" and "/api/session/pin" in request.url:
                    pin_requests.append(json.loads(request.post_data or "{}"))

            def _record_pin_status(response):
                if response.request.method == "POST" and "/api/session/pin" in response.url:
                    pin_statuses.append(response.status)

            page.on("request", _record_pin)
            page.on("response", _record_pin_status)

            page.goto(base_url, wait_until="domcontentloaded")
            page.wait_for_selector("#sessionList", timeout=30000)
            page.wait_for_function(
                "() => typeof renderSessionList === 'function'", timeout=30000
            )
            page.evaluate(
                "() => renderSessionList({deferWhileInteracting: false})"
            )
            page.wait_for_selector(
                f'#sessionList .session-item[data-sid="{parent_sid}"]', timeout=30000
            )

            # Narrow the sidebar to the resizer's minimum (SIDEBAR_MIN in
            # boot.js = 180px) through the app's own resize initialiser and
            # saved-width key. 180px is the width the review reported and the
            # one where the parent's centred button lands on the first fork's
            # kebab, so it is where the hit test has to be proven.
            page.evaluate(
                """() => {
                  try { localStorage.setItem('hermes-sidebar-w', '180'); } catch (e) {}
                  if (window._initResizePanels) window._initResizePanels();
                }"""
            )
            page.wait_for_function(
                "() => document.querySelector('.sidebar').getBoundingClientRect().width < 182",
                timeout=10000,
            )

            # Expand the children from the parent's "N children" badge, exactly
            # as a user does.
            expanded = page.evaluate(
                """(pid) => {
                  const row = document.querySelector(
                    `#sessionList .session-item[data-sid="${pid}"]`);
                  const badge = row && row.querySelector('.session-child-count');
                  if (!badge) return false;
                  badge.click();
                  return true;
                }""",
                parent_sid,
            )
            assert expanded, "the parent row has no children badge to expand"

            fork_selector = (
                f'#sessionList .session-child-session-fork[data-sid="{fork1_sid}"]'
            )
            page.wait_for_selector(fork_selector, timeout=30000)
            page.hover(fork_selector)
            page.wait_for_timeout(120)

            kebab = page.evaluate(
                """(fid) => {
                  const row = document.querySelector(
                    `#sessionList .session-child-session-fork[data-sid="${fid}"]`);
                  const button = row && row.querySelector('.session-actions-trigger');
                  if (!button) return null;
                  const box = button.getBoundingClientRect();
                  return {x: box.left + box.width / 2, y: box.top + box.height / 2};
                }""",
                fork1_sid,
            )
            assert kebab, "the first fork row has no actions trigger"

            sidebar_width = page.evaluate(
                "() => document.querySelector('.sidebar').getBoundingClientRect().width"
            )
            assert sidebar_width == pytest.approx(180, abs=1), (
                f"the sidebar did not take the resizer's 180px minimum ({sidebar_width}px): "
                "this test targets the width where the parent's button covers the "
                "first fork's kebab"
            )

            # A real pointer press on the fork's kebab. It must open the FORK's
            # menu; on the unfixed layout the parent's block-centred button can
            # be over this point and opens the parent's menu instead — the
            # reported symptom, and the reason "Pin conversation" pinned the
            # parent.
            page.mouse.click(kebab["x"], kebab["y"])
            page.wait_for_selector(".session-action-opt", timeout=10000)
            opened_for = page.evaluate(
                """() => {
                  const open = document.querySelector(
                    '.session-child-session-fork.menu-open, .session-item.menu-open');
                  return open ? {row: open.classList.contains('session-item') ? 'parent' : 'fork',
                                 sid: open.dataset.sid} : null;
                }"""
            )
            assert opened_for == {"row": "fork", "sid": fork1_sid}, (
                "the press on the first fork's kebab opened the menu for "
                f"{opened_for!r} instead of for that fork"
            )

            pin_option = page.locator(
                '.session-action-opt:has(.ws-opt-name:text-is("%s"))' % PIN_LABEL
            ).first
            pin_box = pin_option.bounding_box()
            assert pin_box, "the Pin option is not laid out"
            page.mouse.click(
                pin_box["x"] + pin_box["width"] / 2,
                pin_box["y"] + pin_box["height"] / 2,
            )
            # The pin route writes under the session's lock before it answers,
            # so the response is the signal that the write happened; waiting for
            # it (rather than for a fixed delay) is what keeps this test from
            # racing the server.
            status_deadline = time.monotonic() + PIN_SETTLE_TIMEOUT
            while not pin_statuses and time.monotonic() < status_deadline:
                page.wait_for_timeout(100)

            # The layout invariant behind the symptom: on an expanded parent the
            # action button belongs to the parent's own title row. Block-centred
            # it drifts down into the nested rows; the drift is geometry, so it
            # can miss one kebab centre while still sitting over that kebab.
            lane = page.evaluate(
                """(pid) => {
                  const row = document.querySelector(
                    `#sessionList .session-item[data-sid="${pid}"]`);
                  const button = row.querySelector(':scope > .session-actions');
                  const title = row.querySelector('.session-title-row');
                  const b = button.getBoundingClientRect();
                  const t = title.getBoundingClientRect();
                  return {buttonCentre: (b.top + b.bottom) / 2,
                          titleCentre: (t.top + t.bottom) / 2};
                }""",
                parent_sid,
            )
            assert lane["buttonCentre"] == pytest.approx(lane["titleCentre"], abs=0.6), (
                "the parent's action button is not on its title row "
                f"({lane['buttonCentre']} vs {lane['titleCentre']})"
            )
            context.close()
        finally:
            browser.close()

    assert page_errors == [], f"the page raised: {page_errors}"

    assert pin_requests, (
        "no POST /api/session/pin reached the server: the fork's kebab did not "
        "open a menu, or the Pin action did not run"
    )
    assert len(pin_requests) == 1, f"expected one Pin request, got {pin_requests}"
    body = pin_requests[0]
    assert body.get("session_id") == fork1_sid, (
        "the Pin chosen from the first fork's kebab targeted "
        f"{body.get('session_id')!r} instead of the fork {fork1_sid!r} "
        f"(parent is {parent_sid!r}, second fork {fork2_sid!r})"
    )
    assert body.get("pinned") is True, f"the Pin request did not ask to pin: {body}"
    assert pin_statuses == [200], (
        "the Pin request was not accepted: HTTP statuses "
        f"{pin_statuses} for {pin_requests}"
    )

    rows = _sessions_until(base_url, lambda r: r[fork1_sid].get("pinned") is True)
    assert rows[fork1_sid].get("pinned") is True, (
        "the fork is not pinned after the request: "
        f"{rows[fork1_sid].get('pinned')!r}"
    )
    assert not rows[parent_sid].get("pinned"), (
        "the PARENT ended up pinned — the wrong-target regression is back"
    )
    assert not rows[fork2_sid].get("pinned"), "the second fork was pinned by mistake"
