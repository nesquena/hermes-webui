"""A row's actions button never sits on its badges, and never on a fork's kebab.

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

An EXPANDED parent adds a second collision: the button is centred on the whole
block, so it lands on the first nested row — exactly where that row's own kebab
is — and wins the hit test ("Pin conversation" pinned the parent). Reserving a
gutter inside the nested lists fixes that but eats the fork's label at the
180px sidebar minimum, so the parent's button is anchored to its own title row.

The source-level tests pin the selectors and the renderer condition they mirror.
The browser tests measure the real stylesheet on rows shaped like the
renderer's output: nothing may move when the pointer enters a row or a children
list expands, the button may not intersect the badge or the chip left of the
timestamp, and a fork's kebab must belong to the fork. The fork label is pinned
from both ends — a long label must keep a readable content box, and a short one
must not be clipped even though it fits — because a squeezed label box only
becomes visible as a bug in the short case.
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


# ── An expanded parent's trigger stays on its own title row ─────────────────
# `.session-actions` is centred on the whole row (top:50%), so on an expanded
# parent it lands on the first nested row, exactly where that row's own kebab
# sits, and wins the hit test: hovering a fork's kebab and picking "Pin
# conversation" pinned the PARENT. Padding the nested lists fixes the target
# but eats the fork's label (at the 180px sidebar minimum the fork's own title
# was left with zero content width), so the parent's trigger is anchored to its
# title row instead. The scenes below are the renderer's real markup: the fork
# rows carry `.session-child-session.session-child-session-fork`, their
# `.session-child-session-state` slot and their own `.session-actions`.


def _fork_row(n, *, running=True, short=False):
    state = " is-streaming" if running else ""
    streaming = " streaming" if running else ""
    # Two label lengths, one bug class: a long label proves the row does not
    # collapse its text to nothing, a short one ("-> F1 - 1m") proves the label
    # box is not clipped even when the text would fit — the same width bug the
    # other way round, and the shape a `max-width:calc(...)` "fix" breaks.
    label = (
        f"-&gt; F{n} - {n}m"
        if short
        else f"-&gt; Fork {n} that also fills the whole row - {n}m"
    )
    return (
        f'<div class="session-child-session session-child-session-fork{streaming}"'
        f' id="fork{n}" data-sid="fork{n}">'
        f'<button type="button" class="session-child-session-main" id="fork{n}-main"'
        f' title="Open forked session">{label}</button>'
        f'<span class="session-state-indicator session-child-session-state{state}"'
        f' aria-hidden="true"></span>'
        f'<div class="session-actions"><button type="button" class="session-actions-trigger"'
        f' id="fork{n}-k" aria-label="More">&middot;&middot;&middot;</button></div>'
        f"</div>"
    )


def _expanded_scene(width, *, detailed=False, running=True, nested="children",
                    short_label=False):
    """The renderer's real row markup, at a given sidebar width."""
    meta = (
        '<div class="session-meta">12 messages · 2 children · deepseek-v4</div>'
        if detailed
        else ""
    )
    if nested == "children":
        kids = (
            '<button type="button" class="session-child-session" id="plain-child">'
            "-&gt; A plain child conversation row - 6m</button>"
            + _fork_row(1, running=running, short=short_label)
            + _fork_row(2, running=running, short=short_label)
        )
        kids_class = "session-child-sessions"
    else:
        kids = (
            '<button type="button" class="session-lineage-segment" id="segment1">'
            "-&gt; segment one - 4m</button>"
            '<button type="button" class="session-lineage-segment" id="segment2">'
            "-&gt; segment two - 3m</button>"
        )
        kids_class = "session-lineage-segments"
    return f"""
<div class="sidebar" style="width:{width}px">
  <div class="session-list">
    <div class="session-item" id="parent" data-sid="parent">
      <div class="session-text">
        <div class="session-title-row">
          <div class="session-title-group">
            <button type="button" class="session-title session-open-control">Long parent title that fills the row</button>
          </div>
          <span class="session-project-dot" style="background:#4a9"></span>
          <span class="session-child-count">2 children</span>
          <span class="session-time">5m</span>
        </div>
        {meta}
        <div class="{kids_class}" id="kids">
          {kids}
        </div>
      </div>
      <div class="session-actions" id="parent-actions"><button type="button" class="session-actions-trigger" id="parent-k" aria-label="More">&middot;&middot;&middot;</button></div>
    </div>
  </div>
</div>
"""


# Who owns the box at each kebab's centre, how wide is the first fork's label,
# and does the parent's trigger still reach into the fork's? The click log
# answers the reviewer's question directly: a click at the fork kebab must
# reach the FORK. The badge/title rects are sampled with the nested list
# visible and hidden, to prove expanding moves nothing.
EXPANDED_MEASURE = """() => {
  window.__clicks = [];
  for (const b of document.querySelectorAll(
      '.session-actions-trigger, .session-child-session-main, .session-lineage-segment')) {
    b.addEventListener('click', () => {
      const fork = b.closest('.session-child-session-fork');
      if (fork && b.classList.contains('session-actions-trigger')) { window.__clicks.push(fork.id); return; }
      if (b.id) { window.__clicks.push(b.id); return; }
      window.__clicks.push(b.className);
    });
  }
  const rect = (el) => { const b = el ? el.getBoundingClientRect() : {left:0,right:0,top:0,bottom:0,width:0,height:0};
    return {left: b.left, right: b.right, top: b.top, bottom: b.bottom, width: b.width, height: b.height}; };
  const overlap = (a, b) => !(a.right <= b.left + 0.01 || a.left >= b.right - 0.01
    || a.bottom <= b.top + 0.01 || a.top >= b.bottom - 0.01);
  const owner = (el) => {
    if (!el) return null;
    const b = el.getBoundingClientRect();
    const hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
    if (!hit) return null;
    const fork = hit.closest('.session-child-session-fork');
    if (fork) return fork.id;
    const child = hit.closest('.session-child-session');
    if (child) return child.id || 'child-row';
    const seg = hit.closest('.session-lineage-segment');
    if (seg) return seg.id;
    if (hit.closest('.session-item')) return 'parent';
    return hit.id || hit.className || hit.tagName;
  };
  const actions = document.getElementById('parent-actions');
  const pa = rect(actions);
  const fork1 = document.getElementById('fork1-k');
  const f1 = rect(fork1);
  const kids = document.getElementById('kids');
  const badge = document.querySelector('.session-child-count');
  const titleGroup = document.querySelector('.session-title-group');
  const titleRow = document.querySelector('#parent .session-title-row');
  const expanded = {badge: rect(badge), titleGroup: rect(titleGroup)};
  kids.style.display = 'none';
  const collapsed = {badge: rect(badge), titleGroup: rect(titleGroup)};
  kids.style.display = '';
  const main = document.getElementById('fork1-main');
  const mainStyle = main && getComputedStyle(main);
  return {
    parentActions: pa,
    parentActionsDisplay: getComputedStyle(actions).display,
    parentActionsCentre: (pa.top + pa.bottom) / 2,
    titleRow: rect(titleRow),
    titleRowCentre: (rect(titleRow).top + rect(titleRow).bottom) / 2,
    fork1Trigger: f1,
    fork1Owner: owner(fork1),
    parentOwner: owner(document.getElementById('parent-k')),
    plainChildOwner: owner(document.getElementById('plain-child')),
    segmentOwner: owner(document.getElementById('segment1')),
    parentTriggerOverFork: overlap(pa, f1),
    fork1MainWidth: main ? rect(main).width : 0,
    fork1MainContent: main
      ? rect(main).width - parseFloat(mainStyle.paddingLeft) - parseFloat(mainStyle.paddingRight)
      : 0,
    fork1LabelClipped: main ? main.scrollWidth > main.clientWidth + 0.5 : true,
    fork1LabelText: main ? main.textContent.trim() : null,
    nestedPaddingRight: parseFloat(getComputedStyle(kids).paddingRight),
    expanded: expanded,
    collapsed: collapsed,
    fork1Centre: {x: (f1.left + f1.right) / 2, y: (f1.top + f1.bottom) / 2},
  };
}"""


TOUCH_MEASURE = """() => {
  const hitCentre = (el) => {
    if (!el) return null;
    const b = el.getBoundingClientRect();
    const hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
    if (!hit) return null;
    const fork = hit.closest('.session-child-session-fork');
    if (fork) return fork.id;
    const child = hit.closest('.session-child-session');
    if (child) return child.id || 'child-row';
    if (hit.closest('.session-item')) return 'parent';
    return hit.id || hit.className || hit.tagName;
  };
  const parentActions = document.getElementById('parent-actions');
  const forkActions = document.querySelector('#fork1 .session-actions');
  const box = parentActions.getBoundingClientRect();
  return {
    parentActionsDisplay: getComputedStyle(parentActions).display,
    parentActionsWidth: box.width,
    forkActionsDisplay: forkActions ? getComputedStyle(forkActions).display : null,
    forkMainOwner: hitCentre(document.getElementById('fork1-main')),
    plainChildOwner: hitCentre(document.getElementById('plain-child')),
    nestedPaddingRight: parseFloat(getComputedStyle(document.getElementById('kids')).paddingRight),
  };
}"""


def _measure_expanded(width=280, *, detailed=False, running=True, nested="children",
                      touch=False, click=False, short_label=False):
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            if touch:
                context = browser.new_context(
                    viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
                )
            else:
                context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()
            page.set_content(
                _expanded_scene(width, detailed=detailed, running=running, nested=nested,
                                short_label=short_label)
            )
            page.add_style_tag(path=str(REPO / "static" / "style.css"))
            page.wait_for_timeout(100)
            if touch:
                measured = page.evaluate(TOUCH_MEASURE)
                context.close()
                return measured
            # The pointer enters the first nested row, which also means it is
            # inside the parent: this is the state in which both triggers used
            # to sit on the same spot.
            page.hover("#fork1" if nested == "children" else "#segment1")
            page.wait_for_timeout(80)
            measured = page.evaluate(EXPANDED_MEASURE)
            if click:
                page.mouse.click(
                    measured["fork1Centre"]["x"], measured["fork1Centre"]["y"]
                )
                page.wait_for_timeout(80)
                measured["clicks"] = page.evaluate("() => window.__clicks")
            context.close()
            return measured
        finally:
            browser.close()


def _is_expanded_anchor(selector):
    flat = " ".join(re.sub(r"/\*.*?\*/", " ", selector, flags=re.S).split())
    return (
        ".session-item:has(> .session-text > :is(" in flat
        and ".session-child-sessions,.session-lineage-segments)) > .session-actions" in flat
    )


def test_nested_lists_take_no_lane_from_the_fork_label():
    offenders = [
        selector
        for selector, body in RULES
        if any(
            marker in selector
            for marker in (".session-child-sessions", ".session-lineage-segments")
        )
        and "padding-right" in body
    ]
    assert not offenders, (
        "a nested list reserves a lane for the parent's button: at the 180px "
        "sidebar minimum that leaves the fork's own label with no content "
        "width. Anchor the parent's button to its title row instead: "
        + "; ".join(offenders)
    )


def test_an_expanded_parents_trigger_is_anchored_to_its_title_row():
    anchor = [body for selector, body in RULES if _is_expanded_anchor(selector)]
    assert anchor, (
        "no rule anchors an expanded parent's button to its own title row: "
        "centred on the block it lands on the first nested row's kebab"
    )
    body = anchor[0]
    assert "top:auto" in body and "bottom:auto" in body, (
        "the anchored button must take its static position (the row's "
        "content-box top, where the title row starts) instead of restating a "
        "row padding: " + body
    )
    assert re.search(r"transform:translateY\(calc\(\.5lh - 50%\)\)", body), (
        "the button must be centred on the title row's own line box (`lh`), so "
        "skins and both sidebar densities follow: " + body
    )
    assert not re.search(r"\d+px", body), (
        "no hard-coded pixel offset in the anchor — it does not survive the "
        "other densities and skins: " + body
    )


def test_a_forks_kebab_opens_the_forks_menu_and_not_its_parents():
    m = _measure_expanded(280, click=True)

    assert m["fork1Owner"] == "fork1", (
        "a click at the first fork's kebab resolves to "
        f"{m['fork1Owner']!r}: the parent's button is over the fork row"
    )
    assert m["clicks"] == ["fork1"], (
        f"the pointer click at the fork kebab reached {m['clicks']!r}, not the fork"
    )
    assert m["parentOwner"] == "parent", (
        f"the parent's own kebab resolves to {m['parentOwner']!r}"
    )
    assert not m["parentTriggerOverFork"], (
        "the parent's trigger still reaches the fork's: parent "
        f"{m['parentActions']}, fork {m['fork1Trigger']}"
    )
    # The separation is vertical now: the parent's button sits on its title
    # row, not on the first nested row.
    assert m["parentActionsCentre"] == pytest.approx(m["titleRowCentre"], abs=0.6), (
        "the parent's button is not centred on its title row: button centre "
        f"{m['parentActionsCentre']}, title row centre {m['titleRowCentre']}"
    )


def test_the_anchored_trigger_holds_at_the_detailed_density():
    compact = _measure_expanded(280)
    detailed = _measure_expanded(280, detailed=True)

    for label, m in (("compact", compact), ("detailed", detailed)):
        assert m["parentActionsCentre"] == pytest.approx(m["titleRowCentre"], abs=0.6), (
            f"{label}: the button is not centred on its title row "
            f"({m['parentActionsCentre']} vs {m['titleRowCentre']})"
        )
        assert not m["parentTriggerOverFork"], (
            f"{label}: the parent's trigger reaches the fork's kebab"
        )
        assert m["fork1Owner"] == "fork1", (
            f"{label}: the fork kebab resolves to {m['fork1Owner']!r}"
        )


def test_minimum_sidebar_width_keeps_the_fork_label_readable():
    # 180px is the sidebar's supported minimum (boot.js resize floor). With a
    # running child the parent's hidden timestamp reserves 40px, and the fork
    # row adds its own indentation, state slot and trigger: the label must
    # still get room.
    for detailed in (False, True):
        m = _measure_expanded(180, detailed=detailed, running=True)
        label = "detailed" if detailed else "compact"
        assert m["nestedPaddingRight"] == pytest.approx(0, abs=0.05), (
            f"{label}: the nested list still reserves {m['nestedPaddingRight']}px"
        )
        assert m["fork1MainContent"] > 16, (
            f"{label}: at the 180px minimum the fork's title is left with "
            f"{m['fork1MainContent']}px of content width (button "
            f"{m['fork1MainWidth']}px): its label is unreadable"
        )
        assert not m["parentTriggerOverFork"], (
            f"{label}: at 180px the parent's trigger reaches the fork's kebab: "
            f"parent {m['parentActions']}, fork {m['fork1Trigger']}"
        )
        assert m["fork1Owner"] == "fork1", (
            f"{label}: at 180px the first fork's kebab resolves to {m['fork1Owner']!r}"
        )
        assert m["plainChildOwner"] == "plain-child", (
            f"{label}: the plain child row's centre resolves to "
            f"{m['plainChildOwner']!r}, not the row"
        )


def test_a_short_fork_label_is_never_clipped_by_the_lane():
    # The twin fixture of the long label above: "-> F1 - 1m" is 59px of text at
    # the fork row's 11px font, so from a 200px sidebar up it fits and must be
    # shown whole. A long label hides this half of the bug — it ellipsizes
    # either way — while a label box that has been squeezed (the removed 40px
    # nested gutter, or a `max-width:calc(...)` on a width:auto flex row, which
    # collapses instead of clamping) cuts a short label that would have fit.
    for width in (200, 240, 320):
        m = _measure_expanded(width, running=True, short_label=True)
        assert m["fork1LabelText"] == "-> F1 - 1m", (
            f"{width}px: the fork label fixture is not the short one: "
            f"{m['fork1LabelText']!r}"
        )
        assert not m["fork1LabelClipped"], (
            f"{width}px: the short fork label is clipped (button content "
            f"{m['fork1MainContent']}px): a label that fits is being cut"
        )
        assert not m["parentTriggerOverFork"], (
            f"{width}px: the parent's trigger reaches the fork's kebab"
        )
        assert m["fork1Owner"] == "fork1", (
            f"{width}px: the first fork's kebab resolves to {m['fork1Owner']!r}"
        )
        assert m["nestedPaddingRight"] == pytest.approx(0, abs=0.05), (
            f"{width}px: the nested list reserves {m['nestedPaddingRight']}px "
            "for the parent's button, which comes out of the fork's label"
        )


def test_expanding_the_children_does_not_move_the_parents_badge():
    m = _measure_expanded(280)

    assert m["nestedPaddingRight"] == pytest.approx(0, abs=0.05), (
        f"the nested list reserves {m['nestedPaddingRight']}px — that is the "
        "gutter that used to be taken out of the fork's label"
    )
    for key in ("badge", "titleGroup"):
        assert m["expanded"][key]["left"] == pytest.approx(
            m["collapsed"][key]["left"], abs=0.05
        ), (
            f"the parent's {key} moved when the children list expanded: "
            f"{m['collapsed'][key]['left']} -> {m['expanded'][key]['left']}"
        )
        assert m["expanded"][key]["width"] == pytest.approx(
            m["collapsed"][key]["width"], abs=0.05
        ), f"the parent's {key} changed width when the children list expanded"


def test_lineage_segments_keep_the_parents_trigger_off_the_first_segment():
    for width in (280, 180):
        m = _measure_expanded(width, nested="lineage", running=False)
        assert m["segmentOwner"] == "segment1", (
            f"{width}px: the first lineage segment's centre resolves to "
            f"{m['segmentOwner']!r}"
        )
        assert m["parentActionsCentre"] == pytest.approx(m["titleRowCentre"], abs=0.6), (
            f"{width}px: the parent's trigger is not on its title row with a "
            "lineage segment list open"
        )


def test_touch_layout_hides_the_parents_trigger_so_it_cannot_intercept():
    m = _measure_expanded(180, running=True, touch=True)

    assert m["parentActionsDisplay"] == "none", (
        "on a touch-primary layout the actions button must stay display:none "
        f"(got {m['parentActionsDisplay']!r}): a tap on a nested row would "
        "otherwise land on the parent's button"
    )
    assert m["forkActionsDisplay"] == "none", (
        "the fork's own actions button is expected to be hidden on a "
        f"touch-primary layout too (got {m['forkActionsDisplay']!r})"
    )
    assert m["forkMainOwner"] == "fork1", (
        f"a tap on the first fork's title resolves to {m['forkMainOwner']!r}"
    )
    assert m["plainChildOwner"] == "plain-child", (
        f"a tap on the plain child row resolves to {m['plainChildOwner']!r}"
    )
    assert m["nestedPaddingRight"] == pytest.approx(0, abs=0.05), (
        f"the nested list reserves {m['nestedPaddingRight']}px on touch"
    )
