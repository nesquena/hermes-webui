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
    parent's, and `pinned: true`;
  - the server state afterwards has the fork pinned and the parent not.

  Playwright is optional in this checkout: without it the test skips rather
  than failing the suite.
"""
import json
import urllib.request

import pytest

from tests.conftest import _post

pytestmark = pytest.mark.usefixtures("test_server")

PIN_LABEL = "Pin conversation"


def _sessions_by_id(base_url):
    with urllib.request.urlopen(base_url + "/api/sessions", timeout=15) as response:
        payload = json.loads(response.read())
    return {row["session_id"]: row for row in payload.get("sessions") or []}


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

            page.on("request", _record_pin)

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

            # A real pointer click at the fork kebab's centre: only the fork's
            # own trigger may receive it.
            page.mouse.click(kebab["x"], kebab["y"])
            page.wait_for_selector(".session-action-opt", timeout=10000)

            pin_option = page.locator(
                '.session-action-opt:has(.ws-opt-name:text-is("%s"))' % PIN_LABEL
            ).first
            pin_box = pin_option.bounding_box()
            assert pin_box, "the Pin option is not laid out"
            page.mouse.click(
                pin_box["x"] + pin_box["width"] / 2,
                pin_box["y"] + pin_box["height"] / 2,
            )
            page.wait_for_timeout(800)
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

    rows = _sessions_by_id(base_url)
    assert rows[fork1_sid].get("pinned") is True, (
        "the fork is not pinned after the request: "
        f"{rows[fork1_sid].get('pinned')!r}"
    )
    assert not rows[parent_sid].get("pinned"), (
        "the PARENT ended up pinned — the wrong-target regression is back"
    )
    assert not rows[fork2_sid].get("pinned"), "the second fork was pinned by mistake"
