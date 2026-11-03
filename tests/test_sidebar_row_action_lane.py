"""A parent row whose child is running keeps the actions button off its badges.

A row's timestamp is the last flex item of ``.session-title-row`` and the
``margin-left:auto`` anchor that pins the badges (child count, lineage count,
project dot) to the row's right edge. ``sessions.js`` hides that timestamp
(``is-hidden``, ``display:none``) not only when the ROW streams but also when a
child does — ``isStreaming = ownStreaming || !!s._child_session_streaming`` —
while the row itself gets the ``.streaming`` class only for ``ownStreaming``.
The 40px lane that clears the absolutely-positioned action button
(``.session-actions``, 26x26 at ``right:6px``) therefore has to come from the
timestamp's own hidden state, not from the class.

Short relative stamps ("1m", "2h") are narrower than that 26px button, so the
timestamp's box needs the button's width too, or the button pokes past it into
whatever chip sits to its left.

The source-level tests pin the selectors and the renderer condition they mirror.
The browser tests measure the real stylesheet on a row shaped like the
renderer's output: nothing may move when the pointer enters the row, and the
button may not intersect the badge or the chip left of the timestamp.
"""
import re
from pathlib import Path

import pytest

from tests._css_blocks import css_block

REPO = Path(__file__).resolve().parents[1]
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")

RULES = [
    (selector.strip(), body)
    for selector, body in re.findall(r"([^{}]*)\{([^{}]*)\}", CSS)
]

# The renderer's own path to the hidden timestamp: `isStreaming` folds in the
# child's run, `hasAttentionState` follows it, and only `ownStreaming` adds the
# `.streaming` class. If any of these three moves, the CSS below has to move.
JS_ROW_CLASS = re.compile(r"el\.className='session-item'.*?\(ownStreaming\?' streaming':''\)", re.S)
JS_TIME_CLASS = re.compile(r"const ts=document\.createElement\('span'\);.*?ts\.className='session-time'\+\(hasAttentionState\?' is-hidden':''\);", re.S)


def _hover_media_block():
    """The `@media (hover:hover)` block that hides the timestamp on hover."""
    idx = CSS.find(".session-item:hover .session-time{visibility:hidden;}")
    assert idx != -1, "the timestamp is no longer hidden with visibility on hover"
    at_pos = CSS.rindex("@media (hover:hover)", 0, idx)
    return css_block(CSS, CSS.index("{", at_pos))


def test_renderer_hides_the_timestamp_for_a_running_child_without_the_streaming_class():
    assert "const isStreaming=ownStreaming||!!s._child_session_streaming;" in SESSIONS_JS, (
        "the row's timestamp is hidden for a running child, which is exactly why "
        "the CSS lane cannot key off .streaming"
    )
    assert JS_ROW_CLASS.search(SESSIONS_JS), (
        "the row gets .streaming from ownStreaming only — a parent with a running "
        "child does not"
    )
    assert JS_TIME_CLASS.search(SESSIONS_JS), (
        "the timestamp is marked is-hidden from hasAttentionState, which includes "
        "the child's run"
    )
    assert "sessionText.className='session-text';" in SESSIONS_JS
    assert "titleRow.className='session-title-row';" in SESSIONS_JS


def test_action_lane_is_reserved_for_a_row_whose_timestamp_is_hidden():
    candidates = [
        selector
        for selector, body in RULES
        if ".session-item" in selector
        and ".session-time.is-hidden" in selector
        and "padding-right:40px" in body
    ]
    assert candidates, (
        "no rule reserves the 40px action-button lane for a row whose timestamp "
        "is hidden (a parent with a running child): the button covers the child "
        "badge on hover"
    )
    assert any(
        ":has(> .session-text > .session-title-row > .session-time.is-hidden)" in selector
        for selector in candidates
    ), (
        "the lane rule must follow the renderer's own path "
        "(.session-text > .session-title-row > .session-time.is-hidden) so it "
        "also outranks the skins' .active padding shorthand: " + "; ".join(candidates)
    )


def test_short_timestamp_reserves_the_action_button_width():
    block = _hover_media_block()
    assert re.search(r"\.session-time\{[^{}]*min-width:26px", block), (
        "a '1m' timestamp is ~16px wide while the action button is 26px: the "
        "timestamp's box must reserve the button's width or the button overlaps "
        "the chip to its left on hover"
    )


SCENE = """
<div class="sidebar" style="width:280px">
  <div class="session-list">
    <div class="session-item" id="child-run">
      <div class="session-text">
        <div class="session-title-row">
          <div class="session-title-group">
            <button type="button" class="session-title session-open-control">Long session title that fills the row</button>
          </div>
          <span class="session-project-dot" style="background:#4a9"></span>
          <span class="session-child-count">1 child</span>
          <span class="session-time is-hidden"></span>
        </div>
      </div>
      <div class="session-actions"><button type="button" class="session-actions-trigger">k</button></div>
    </div>
    <div class="session-item" id="short-stamp">
      <div class="session-text">
        <div class="session-title-row">
          <div class="session-title-group">
            <button type="button" class="session-title session-open-control">Long session title that fills the row</button>
          </div>
          <span class="session-project-dot" style="background:#4a9"></span>
          <span class="session-time">1m</span>
        </div>
      </div>
      <div class="session-actions"><button type="button" class="session-actions-trigger">k</button></div>
    </div>
  </div>
</div>
"""

# Content positions that must not move, plus the boxes whose overlap the lane
# exists to prevent. `x` is the left edge; the button is measured too.
MEASURE = """() => {
  const rows = {};
  for (const id of ['child-run', 'short-stamp']) {
    const row = document.getElementById(id);
    const r = row.getBoundingClientRect();
    const box = (sel) => {
      const el = row.querySelector(sel);
      if (!el) return null;
      const b = el.getBoundingClientRect();
      return {left: b.left, right: b.right, width: b.width};
    };
    const leftmost = row.querySelector('.session-child-count') || row.querySelector('.session-project-dot');
    const l = leftmost.getBoundingClientRect();
    rows[id] = {
      row: {left: r.left, right: r.right},
      paddingRight: parseFloat(getComputedStyle(row).paddingRight),
      titleGroup: box('.session-title-group'),
      timestamp: box('.session-time'),
      leftChip: {left: l.left, right: l.right},
      actions: box('.session-actions'),
    };
  }
  return rows;
}"""


def _measure_rows():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.set_content(SCENE)
            page.add_style_tag(path=str(REPO / "static" / "style.css"))
            page.wait_for_timeout(100)
            rest = page.evaluate(MEASURE)
            page.hover("#child-run")
            page.wait_for_timeout(80)
            child_hover = page.evaluate(MEASURE)["child-run"]
            page.hover("#short-stamp")
            page.wait_for_timeout(80)
            short_hover = page.evaluate(MEASURE)["short-stamp"]
            return rest, child_hover, short_hover
        finally:
            browser.close()


def test_parent_with_a_running_child_does_not_move_and_keeps_the_button_off_the_badge():
    rest, hover, _short = _measure_rows()

    before = rest["child-run"]
    # The lane is reserved from the hidden timestamp itself, not from .streaming.
    assert before["paddingRight"] == 40, (
        "a row whose timestamp is hidden (running child) must keep the 40px action "
        f"lane at rest; got {before['paddingRight']}px"
    )
    # Nothing reflows when the pointer enters: same content x, same lane.
    assert hover["titleGroup"]["left"] == pytest.approx(before["titleGroup"]["left"], abs=0.05), (
        f"title group moved on hover: {before['titleGroup']['left']} -> {hover['titleGroup']['left']}"
    )
    assert hover["leftChip"]["left"] == pytest.approx(before["leftChip"]["left"], abs=0.05), (
        f"child badge moved on hover: {before['leftChip']['left']} -> {hover['leftChip']['left']}"
    )
    assert hover["paddingRight"] == pytest.approx(before["paddingRight"], abs=0.05)
    # The button must not sit on the badge it would otherwise expand into.
    assert hover["actions"]["left"] >= hover["leftChip"]["right"], (
        "the actions button covers the child badge on hover: button starts at "
        f"{hover['actions']['left']}, badge ends at {hover['leftChip']['right']}"
    )
    assert hover["actions"]["right"] <= hover["row"]["right"], "the button leaves the row"


def test_short_timestamp_keeps_the_button_off_the_chip_left_of_it():
    rest, _child, hover = _measure_rows()

    before = rest["short-stamp"]
    # "1m" is ~16px wide; the box must still be at least the button's width.
    assert before["timestamp"]["width"] >= 26 - 0.05, (
        f"a '1m' timestamp reserves only {before['timestamp']['width']}px, less than "
        "the 26px action button"
    )
    assert hover["timestamp"]["width"] == pytest.approx(before["timestamp"]["width"], abs=0.05), (
        "the timestamp's box shrank on hover, so the button lost its clearance"
    )
    assert hover["titleGroup"]["left"] == pytest.approx(before["titleGroup"]["left"], abs=0.05), (
        f"title group moved on hover: {before['titleGroup']['left']} -> {hover['titleGroup']['left']}"
    )
    assert hover["leftChip"]["left"] == pytest.approx(before["leftChip"]["left"], abs=0.05), (
        f"project dot moved on hover: {before['leftChip']['left']} -> {hover['leftChip']['left']}"
    )
    assert hover["actions"]["left"] >= hover["leftChip"]["right"], (
        "the actions button overlaps the project dot on hover: button starts at "
        f"{hover['actions']['left']}, dot ends at {hover['leftChip']['right']}"
    )


# ── An expanded parent must not steal its fork's kebab ──────────────────────
# `.session-actions` is centred on the whole row (top:50%), so on an expanded
# parent it lands on the first nested row, right on top of that row's own
# button. The hit test then resolves to the parent, and "Pin conversation"
# pins the parent instead of the fork. The lane has to be reserved on the
# nested lists themselves: padding the parent would move its badge the moment
# the list expands.

EXPANDED_SCENE = """
<div class="sidebar" style="width:280px">
  <div class="session-list">
    <div class="session-item" id="parent">
      <div class="session-text">
        <div class="session-title-row">
          <div class="session-title-group">
            <button type="button" class="session-title session-open-control">Long parent title that fills the row</button>
          </div>
          <span class="session-project-dot" style="background:#4a9"></span>
          <span class="session-child-count">2 children</span>
          <span class="session-time">5m</span>
        </div>
        <div class="session-child-sessions" id="kids">
          <div class="session-child-session-fork" id="fork1">
            <button type="button" class="session-child-session-main">-&gt; Fork one that also fills the row - 4m</button>
            <div class="session-actions"><button type="button" class="session-actions-trigger" id="fork1-k">k</button></div>
          </div>
          <div class="session-child-session-fork" id="fork2">
            <button type="button" class="session-child-session-main">-&gt; Fork two - 3m</button>
            <div class="session-actions"><button type="button" class="session-actions-trigger" id="fork2-k">k</button></div>
          </div>
        </div>
      </div>
      <div class="session-actions" id="parent-actions"><button type="button" class="session-actions-trigger" id="parent-k">k</button></div>
    </div>
  </div>
</div>
"""

# Who owns the box at each kebab's centre, and does the parent's title row stay
# put when the children list is expanded? The click counter answers the
# reviewer's question directly: a click at the fork kebab must reach the FORK.
EXPANDED_MEASURE = """() => {
  window.__clicks = [];
  for (const b of document.querySelectorAll('.session-actions-trigger')) {
    b.addEventListener('click', () => {
      const fork = b.closest('.session-child-session-fork');
      window.__clicks.push(fork ? fork.id : 'parent');
    });
  }
  const rect = (el) => { const b = el.getBoundingClientRect();
    return {left: b.left, right: b.right, top: b.top, bottom: b.bottom}; };
  const owner = (el) => {
    const b = el.getBoundingClientRect();
    const hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
    if (!hit || !hit.closest('.session-actions')) return null;
    const fork = hit.closest('.session-child-session-fork');
    return fork ? fork.id : 'parent';
  };
  const fork1 = document.getElementById('fork1-k');
  const fork2 = document.getElementById('fork2-k');
  const parentBtn = document.getElementById('parent-k');
  const badge = document.querySelector('.session-child-count');
  const titleGroup = document.querySelector('.session-title-group');
  const kids = document.getElementById('kids');
  const expanded = {badge: rect(badge), titleGroup: rect(titleGroup)};
  kids.style.display = 'none';
  const collapsed = {badge: rect(badge), titleGroup: rect(titleGroup)};
  kids.style.display = '';
  const f1 = rect(fork1);
  return {
    fork1Owner: owner(fork1),
    fork2Owner: owner(fork2),
    parentOwner: owner(parentBtn),
    fork1Trigger: f1,
    parentActions: rect(document.getElementById('parent-actions')),
    kidsPaddingRight: parseFloat(getComputedStyle(kids).paddingRight),
    expanded: expanded,
    collapsed: collapsed,
    fork1Centre: {x: (f1.left + f1.right) / 2, y: (f1.top + f1.bottom) / 2},
  };
}"""


def _measure_expanded_parent():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.set_content(EXPANDED_SCENE)
            page.add_style_tag(path=str(REPO / "static" / "style.css"))
            page.wait_for_timeout(100)
            page.hover("#fork1")
            page.wait_for_timeout(80)
            measured = page.evaluate(EXPANDED_MEASURE)
            # A real pointer click at the fork kebab's centre: only the fork's
            # own trigger may receive it (this is the "pins the fork" check).
            page.mouse.click(measured["fork1Centre"]["x"], measured["fork1Centre"]["y"])
            page.wait_for_timeout(80)
            measured["clicks"] = page.evaluate("() => window.__clicks")
            return measured
        finally:
            browser.close()


def test_nested_lists_reserve_the_parents_action_lane():
    block = _hover_media_block()
    for selector in (".session-child-sessions", ".session-lineage-segments"):
        assert re.search(
            r"[^{}]*" + re.escape(selector) + r"[^{}]*\{[^{}]*padding-right:40px", block
        ), (
            f"{selector} must reserve the 40px lane: an expanded parent's button is "
            "centred on the block and lands on the first nested row's own button"
        )


def test_a_forks_kebab_opens_the_forks_menu_and_not_its_parents():
    m = _measure_expanded_parent()

    assert m["fork1Owner"] == "fork1", (
        "a click at the first fork's kebab resolves to "
        f"{m['fork1Owner']!r}: the parent's button is still over the fork row"
    )
    assert m["fork2Owner"] == "fork2", f"the second fork's kebab resolves to {m['fork2Owner']!r}"
    assert m["parentOwner"] == "parent", (
        f"the parent's own kebab resolves to {m['parentOwner']!r}"
    )
    assert m["clicks"] == ["fork1"], (
        f"the pointer click at the fork kebab reached {m['clicks']!r}, not the fork"
    )
    # The lane is what makes the hit test land on the fork: the nested button
    # has to end left of the parent's button.
    assert m["fork1Trigger"]["right"] <= m["parentActions"]["left"], (
        "the fork's button still overlaps the parent's: fork ends at "
        f"{m['fork1Trigger']['right']}, parent starts at {m['parentActions']['left']}"
    )


def test_expanding_the_children_does_not_move_the_parents_badge():
    m = _measure_expanded_parent()

    assert m["kidsPaddingRight"] == pytest.approx(40, abs=0.05), (
        "the nested list must hold the 40px lane on a hover-capable pointer; got "
        f"{m['kidsPaddingRight']}px"
    )
    for key in ("badge", "titleGroup"):
        assert m["expanded"][key]["left"] == pytest.approx(m["collapsed"][key]["left"], abs=0.05), (
            f"the parent's {key} moved when the children list expanded: "
            f"{m['collapsed'][key]['left']} -> {m['expanded'][key]['left']}"
        )
