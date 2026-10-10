"""#8044 — the "Move to project" pickers: keyboard access, translated labels,
finger-sized rows.

Both pickers (`_showProjectPicker` for one conversation, `_showBatchProjectPicker`
for a selection) built their rows as click-only ``<div>``s, wrote "No project" and
"+ New project" in English whatever the interface language, and kept a 24px row
on a touch screen.

The row helpers are run here in node against a small stand-in DOM, so the key
handling and the focus-return target are exercised, not only read. The real
pickers in a real browser are tests/browser_project_picker_keyboard.py.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = REPO / "static" / "i18n.js"

LOCALE_CODES = [
    "en", "it", "ja", "ru", "es", "de", "zh", "zh-Hant", "pt", "ko", "fr", "cs", "tr", "pl", "vi",
]


def _between(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


BATCH_PICKER = _between(SESSIONS_JS, "function _showBatchProjectPicker(", "function _focusSessionActionMenuRestoreTarget(")
SINGLE_PICKER = _between(SESSIONS_JS, "function _showProjectPicker(", "// ── Project picker rows and keyboard")
HELPERS = _between(SESSIONS_JS, "// ── Project picker rows and keyboard", "// Resize a .project-create-input")
FOCUS_HELPER = _between(
    SESSIONS_JS, "function _focusSessionActionMenuRestoreTarget(", "function closeSessionActionMenu("
)

# A DOM small enough to read: elements with attributes, children, a parent,
# focus, and the two lookups the helpers use.
FAKE_DOM = r"""
let active = null;
const body = {tagName: 'BODY'};
class El {
  constructor(tag){ this.tagName = tag.toUpperCase(); this.attrs = {}; this.children = []; this.className = '';
    this.isConnected = true; this.listeners = {}; this.disabled = false; this.scrollTop = 0;
    this.rect = {top: 0, bottom: 0}; this.clientTop = 0;
    this.classList = {contains: name => this.hasClass(name)}; }
  getBoundingClientRect(){ return this.rect; }
  get clientHeight(){ return this.rect.bottom - this.rect.top - 2 * this.clientTop; }
  setAttribute(name, value){ this.attrs[name] = String(value); }
  getAttribute(name){ return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
  appendChild(child){ this.children.push(child); child.parent = this; return child; }
  addEventListener(type, fn){ (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(type, event){ (this.listeners[type] || []).forEach(fn => fn(event)); }
  focus(){ if (this.isConnected && !this.unfocusable) active = this; }
  scrollIntoView(options){ this.revealed = options; }
  hasClass(name){ return this.className.split(/\s+/).includes(name); }
  querySelectorAll(selector){
    if (selector === '.project-picker-item:not([disabled])')
      return this.children.filter(c => c.hasClass('project-picker-item') && !c.disabled);
    if (selector === '.session-actions-trigger') {
      // Document order, like the real thing.
      const all = el => el.children.flatMap(c => (c.hasClass('session-actions-trigger') ? [c] : []).concat(all(c)));
      return all(this);
    }
    throw new Error('unexpected selector ' + selector);
  }
  querySelector(selector){
    if (selector === '.project-picker-item.active')
      return this.children.find(c => c.hasClass('project-picker-item') && c.hasClass('active')) || null;
    if (selector === '.project-picker-item')
      return this.children.find(c => c.hasClass('project-picker-item')) || null;
    if (selector === '.session-actions-trigger') return this.querySelectorAll(selector)[0] || null;
    if (selector === ':scope > .session-actions-trigger, :scope > .session-actions > .session-actions-trigger') {
      const direct = el => el.children.find(c => c.hasClass('session-actions-trigger')) || null;
      return direct(this)
        || this.children.filter(c => c.hasClass('session-actions')).map(direct).find(Boolean) || null;
    }
    throw new Error('unexpected selector ' + selector);
  }
}
const rowsBySid = {};
const document = {
  createElement: tag => new El(tag),
  get activeElement(){ return active || body; },
  body,
};
function _findSessionRenameRow(sid){ return rowsBySid[String(sid || '')] || null; }
function key(name){
  const event = {key: name, prevented: false, stopped: false,
    preventDefault(){ this.prevented = true; }, stopPropagation(){ this.stopped = true; }};
  return event;
}
"""


def _run(body: str):
    script = FAKE_DOM + FOCUS_HELPER + HELPERS + body
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


PICKER_WITH_ROWS = r"""
const picker = new El('div');
const labels = ['No project', 'Research', 'Client work', '+ New project'];
const rows = labels.map((label, index) => {
  const row = _projectPickerItem(index === 3 ? 'project-picker-create' : '', index === 3 ? undefined : index === 1);
  row.label = label;
  return picker.appendChild(row);
});
let escaped = 0;
_wireProjectPickerKeys(picker, () => { escaped += 1; });
const press = name => { const event = key(name); picker.dispatch('keydown', event); return event; };
const focused = () => (active && active.label) || null;
"""


def test_a_row_is_a_button_with_a_menu_role():
    out = _run(PICKER_WITH_ROWS + r"""
console.log(JSON.stringify({
  picker: picker.getAttribute('role'),
  rows: rows.map(row => ({tag: row.tagName, type: row.type, role: row.getAttribute('role'),
                          checked: row.getAttribute('aria-checked'), cls: row.className})),
}));
""")
    assert out["picker"] == "menu"
    assert [row["tag"] for row in out["rows"]] == ["BUTTON"] * 4
    assert [row["type"] for row in out["rows"]] == ["button"] * 4
    # The single picker's choices are radio items; the create row is an action.
    assert [row["role"] for row in out["rows"]] == ["menuitemradio"] * 3 + ["menuitem"]
    assert [row["checked"] for row in out["rows"]] == ["false", "true", "false", None]
    assert out["rows"][1]["cls"] == "project-picker-item active"
    assert out["rows"][3]["cls"] == "project-picker-item project-picker-create"


def test_a_batch_row_has_no_checked_state():
    """Several conversations have no one current project."""
    out = _run(r"""
const row = _projectPickerItem();
console.log(JSON.stringify({role: row.getAttribute('role'), checked: row.getAttribute('aria-checked'),
                            cls: row.className}));
""")
    assert out == {"role": "menuitem", "checked": None, "cls": "project-picker-item"}


def test_focus_opens_on_the_current_project_else_on_the_first_row():
    out = _run(PICKER_WITH_ROWS + r"""
_focusProjectPickerItem(picker);
const withCurrent = focused();
rows[1].className = 'project-picker-item';
active = null;
_focusProjectPickerItem(picker);
console.log(JSON.stringify({withCurrent, without: focused()}));
""")
    assert out == {"withCurrent": "Research", "without": "No project"}


def test_arrows_wrap_and_home_end_jump():
    out = _run(PICKER_WITH_ROWS + r"""
rows[1].focus();
const seen = [];
for (const name of ['ArrowDown', 'ArrowDown', 'ArrowDown', 'ArrowUp', 'ArrowUp', 'Home', 'End', 'Home']) {
  const event = press(name);
  seen.push([name, focused(), event.prevented]);
}
console.log(JSON.stringify(seen));
""")
    assert out == [
        ["ArrowDown", "Client work", True],
        ["ArrowDown", "+ New project", True],
        ["ArrowDown", "No project", True],
        ["ArrowUp", "+ New project", True],
        ["ArrowUp", "Client work", True],
        ["Home", "No project", True],
        ["End", "+ New project", True],
        ["Home", "No project", True],
    ]


def test_other_keys_are_left_to_the_button():
    """Enter and Space activate a button natively."""
    out = _run(PICKER_WITH_ROWS + r"""
rows[0].focus();
const seen = ['Enter', ' ', 'a'].map(name => { const event = press(name); return [event.prevented, focused()]; });
console.log(JSON.stringify({seen, escaped}));
""")
    assert out == {"seen": [[False, "No project"]] * 3, "escaped": 0}


def test_tab_closes_the_picker_and_is_left_to_the_browser():
    """Tab used to move focus out and leave the picker open, where no key
    could reach it again: the key listener is on the picker. It closes like
    Escape, but the Tab itself is not swallowed, so focus moves on."""
    out = _run(PICKER_WITH_ROWS + r"""
rows[3].focus();
const event = press('Tab');
console.log(JSON.stringify({escaped, prevented: event.prevented, stopped: event.stopped}));
""")
    assert out == {"escaped": 1, "prevented": False, "stopped": False}


def test_escape_runs_the_close_handler_and_goes_no_further():
    out = _run(PICKER_WITH_ROWS + r"""
rows[2].focus();
const event = press('Escape');
console.log(JSON.stringify({escaped, prevented: event.prevented, stopped: event.stopped}));
""")
    assert out == {"escaped": 1, "prevented": True, "stopped": True}


SIDEBAR_ROW = r"""
function sidebarRow(label){
  const row = new El('div'); row.className = 'session-item';
  const actions = row.appendChild(new El('div')); actions.className = 'session-actions';
  const trigger = actions.appendChild(new El('button')); trigger.className = 'session-actions-trigger';
  trigger.label = label;
  return {row, actions, trigger};
}
const label = el => el && el.label || null;
"""


def test_focus_returns_to_the_trigger_the_picker_opened_from():
    out = _run(SIDEBAR_ROW + r"""
const {trigger} = sidebarRow('original');
console.log(JSON.stringify(label(_projectPickerFocusReturnTarget({session_id: 'sa'}, trigger))));
""")
    assert out == "original"


def test_a_row_or_its_actions_box_as_the_anchor_resolves_to_the_trigger():
    """A right click or a long press opens the ⋮ menu on the row or its actions
    box. Neither can take focus; the trigger inside the row can."""
    out = _run(SIDEBAR_ROW + r"""
const {row, actions} = sidebarRow('of this row');
console.log(JSON.stringify({
  fromRow: label(_projectPickerFocusReturnTarget({session_id: 'sa'}, row)),
  fromActions: label(_projectPickerFocusReturnTarget({session_id: 'sa'}, actions)),
}));
""")
    assert out == {"fromRow": "of this row", "fromActions": "of this row"}


def test_a_parent_row_returns_its_own_trigger_not_a_fork_childs():
    """An expanded parent row holds its fork children's rows, and they sit
    before the parent's own actions box in the DOM. The first trigger inside
    the parent row is therefore a child's."""
    out = _run(SIDEBAR_ROW + r"""
const parent = new El('div'); parent.className = 'session-item';
const text = parent.appendChild(new El('div'));
const childList = text.appendChild(new El('div'));
const children = ['first fork', 'second fork'].map(name => {
  const child = childList.appendChild(new El('div')); child.className = 'session-child-session session-child-session-fork';
  const actions = child.appendChild(new El('div')); actions.className = 'session-actions';
  const trigger = actions.appendChild(new El('button')); trigger.className = 'session-actions-trigger';
  trigger.label = name;
  return {child, actions, trigger};
});
const parentActions = parent.appendChild(new El('div')); parentActions.className = 'session-actions';
const parentTrigger = parentActions.appendChild(new El('button'));
parentTrigger.className = 'session-actions-trigger'; parentTrigger.label = 'parent';
rowsBySid['parent'] = parent;
rowsBySid['fork'] = children[1].child;
const detached = new El('button'); detached.isConnected = false;
console.log(JSON.stringify({
  first: label(parent.querySelector('.session-actions-trigger')),
  fromParentRow: label(_projectPickerFocusReturnTarget({session_id: 'parent'}, parent)),
  fromParentActions: label(_projectPickerFocusReturnTarget({session_id: 'parent'}, parentActions)),
  fromChildRow: label(_projectPickerFocusReturnTarget({session_id: 'fork'}, children[1].child)),
  parentAfterRepaint: label(_projectPickerFocusReturnTarget({session_id: 'parent'}, detached)),
  childAfterRepaint: label(_projectPickerFocusReturnTarget({session_id: 'fork'}, detached)),
}));
""")
    assert out == {
        "first": "first fork",  # what a plain querySelector would hand back
        "fromParentRow": "parent",
        "fromParentActions": "parent",
        "fromChildRow": "second fork",
        "parentAfterRepaint": "parent",
        "childAfterRepaint": "second fork",
    }


def test_after_a_repaint_focus_returns_to_the_rows_new_trigger():
    """The sidebar is rebuilt on every refresh, which detaches the trigger the
    picker opened from. The conversation's row is looked up again by its id."""
    out = _run(SIDEBAR_ROW + r"""
const original = sidebarRow('original');
original.trigger.isConnected = false; original.row.isConnected = false; original.actions.isConnected = false;
rowsBySid['sa'] = sidebarRow('repainted').row;
console.log(JSON.stringify({
  fromTrigger: label(_projectPickerFocusReturnTarget({session_id: 'sa'}, original.trigger)),
  fromRow: label(_projectPickerFocusReturnTarget({session_id: 'sa'}, original.row)),
  gone: _projectPickerFocusReturnTarget({session_id: 'filtered-away'}, original.trigger),
}));
""")
    assert out == {"fromTrigger": "repainted", "fromRow": "repainted", "gone": None}


def test_a_focused_row_is_scrolled_into_the_pickers_box():
    """The focus call itself must not scroll, so a row beyond the edge of a
    long picker is brought in by moving the picker's own scroll position."""
    out = _run(PICKER_WITH_ROWS + r"""
picker.rect = {top: 99, bottom: 301};   // a 1px border around a 100..300 box
picker.clientTop = 1;
picker.scrollTop = 50;
const seen = [];
for (const [index, top, bottom] of [[3, 320, 364], [0, 60, 104], [1, 120, 164], [2, 256, 300]]) {
  rows[index].rect = {top, bottom};
  _focusProjectPickerRow(picker, rows[index]);
  seen.push([focused(), picker.scrollTop]);
  picker.scrollTop = 50;
}
rows[1].unfocusable = true; active = null;
const refused = _focusProjectPickerRow(picker, rows[1]);
console.log(JSON.stringify({seen, refused, after: picker.scrollTop}));
""")
    assert out["seen"] == [
        ["+ New project", 114],  # 64px below the box
        ["No project", 10],      # 40px above it
        ["Research", 50],        # inside: untouched
        ["Client work", 50],     # flush with the inner bottom edge: untouched
    ]
    assert out["refused"] is False and out["after"] == 50


def test_a_focused_row_is_brought_onto_the_screen_by_whatever_scrolls_it():
    """The batch picker does not scroll with a mouse; the conversation list around
    it does. The nearest edge, so a row already in view does not move."""
    out = _run(PICKER_WITH_ROWS + r"""
const refused = _focusProjectPickerRow(picker, {isConnected: false});
_focusProjectPickerRow(picker, rows[2]);
console.log(JSON.stringify({refused, revealed: rows.map(row => row.revealed || null)}));
""")
    assert out["refused"] is False
    assert out["revealed"] == [None, None, {"block": "nearest"}, None]


def test_the_reveal_stops_inside_the_pickers_border():
    """Measured against the border box, the last row would overhang by the
    border's width and its focus ring would be clipped."""
    out = _run(PICKER_WITH_ROWS + r"""
picker.rect = {top: 99, bottom: 301};
picker.clientTop = 1;
rows[3].rect = {top: 257, bottom: 301};   // inside the border box, 1px past the inner edge
_focusProjectPickerRow(picker, rows[3]);
const below = picker.scrollTop;
picker.scrollTop = 20;
rows[0].rect = {top: 99, bottom: 143};    // the same at the top
_focusProjectPickerRow(picker, rows[0]);
console.log(JSON.stringify({below, above: picker.scrollTop}));
""")
    assert out == {"below": 1, "above": 19}


def test_opening_reveals_the_current_project_too():
    """With enough projects the conversation's own one starts below the fold."""
    out = _run(PICKER_WITH_ROWS + r"""
picker.rect = {top: 0, bottom: 100};
rows.forEach((row, index) => { row.rect = {top: index * 60, bottom: index * 60 + 44}; });
rows[1].className = 'project-picker-item';
rows[2].className = 'project-picker-item active';
_focusProjectPickerItem(picker);
console.log(JSON.stringify({focused: focused(), scrollTop: picker.scrollTop}));
""")
    assert out == {"focused": "Client work", "scrollTop": 64}


def test_arrow_keys_reveal_the_row_they_move_to():
    out = _run(PICKER_WITH_ROWS + r"""
picker.rect = {top: 0, bottom: 100};
rows.forEach((row, index) => { row.rect = {top: index * 44, bottom: index * 44 + 44}; });
rows[0].focus();
press('End');
console.log(JSON.stringify({focused: focused(), scrollTop: picker.scrollTop}));
""")
    assert out == {"focused": "+ New project", "scrollTop": 76}


def test_both_pickers_build_every_row_with_the_helper():
    assert "createElement('div');none" not in BATCH_PICKER
    assert BATCH_PICKER.count("_projectPickerItem(") == 2
    assert SINGLE_PICKER.count("_projectPickerItem(") == 3
    # The class is assigned in one place only: the helper.
    assert SESSIONS_JS.count("className='project-picker-item'") == 1
    assert "className='project-picker-item'" in HELPERS


def test_both_pickers_wire_the_keys_and_focus_a_row_on_open():
    for name, body in (("batch", BATCH_PICKER), ("single", SINGLE_PICKER)):
        assert "_wireProjectPickerKeys(picker," in body, name
        assert "_focusProjectPickerItem(picker)" in body, name
    assert "_focusSessionActionMenuRestoreTarget(openerEl)" in BATCH_PICKER
    # Each menu has a name: the label of the control that opens it.
    assert "picker.setAttribute('aria-label',t('session_batch_move'))" in BATCH_PICKER
    assert "picker.setAttribute('aria-label',t('session_move_project'))" in SINGLE_PICKER
    assert "_focusSessionActionMenuRestoreTarget(_projectPickerFocusReturnTarget(session,anchorEl))" in SINGLE_PICKER
    # The selection bar hands its Move button over as the place to return to.
    assert "_showBatchProjectPicker(moveBtn)" in SESSIONS_JS


def test_the_two_labels_go_through_t():
    assert "'No project'" not in SESSIONS_JS
    assert "'+ New project'" not in SESSIONS_JS
    assert "t('project_picker_none')" in BATCH_PICKER
    assert "t('project_picker_none')" in SINGLE_PICKER
    assert "t('project_picker_new')" in SINGLE_PICKER


@pytest.fixture(scope="module")
def picker_labels():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const context = {
  localStorage: {getItem(){ return null; }, setItem(){}},
  document: {documentElement: {}, addEventListener(){}, querySelectorAll(){ return []; }},
  navigator: {language: 'en', languages: ['en']},
  console,
};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8') + '\n;this.__locales = LOCALES;', context);
const out = {};
for (const [code, table] of Object.entries(context.__locales))
  out[code] = [table.project_picker_none, table.project_picker_new];
console.log(JSON.stringify(out));
"""
    result = subprocess.run(
        ["node", "-e", script, str(I18N_JS)], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def test_every_locale_has_both_labels(picker_labels):
    assert sorted(picker_labels) == sorted(LOCALE_CODES)
    for code, (none, new) in picker_labels.items():
        assert isinstance(none, str) and none.strip(), code
        assert isinstance(new, str) and new.startswith("+ ") and new[2:].strip(), code
    assert picker_labels["en"] == ["No project", "+ New project"]


def test_no_other_locale_repeats_the_english(picker_labels):
    english = picker_labels["en"]
    for code, labels in picker_labels.items():
        if code == "en":
            continue
        assert labels[0] != english[0], code
        assert labels[1] != english[1], code


def test_a_row_is_a_finger_tall_on_a_touch_screen():
    assert "@media (pointer:coarse){.project-picker-item{min-height:44px;" in STYLE_CSS


def test_taller_rows_scroll_inside_the_picker():
    """44px rows make a long project list taller than a phone's screen."""
    rule = _between(STYLE_CSS, ".project-picker:not(.batch-project-picker){max-height:", "}")
    assert "calc(100dvh - 16px)" in rule
    assert "overflow-y:auto" in rule


def test_the_inline_batch_picker_does_not_take_the_scroll_box():
    """The batch picker sits in the conversation list. As a scroll box with
    `overscroll-behavior:contain` it swallowed the wheel and the list stood still."""
    assert ".project-picker{max-height:" not in STYLE_CSS
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", STYLE_CSS)
    batch = [
        body
        for selector, body in rules
        if ".batch-project-picker" in selector and ":not(.batch-project-picker)" not in selector
    ]
    assert len(batch) >= 2
    assert not any("overscroll-behavior" in body for body in batch)


def test_a_focused_row_can_be_seen():
    assert ".project-picker-item:focus-visible{" in STYLE_CSS
    rule = _between(STYLE_CSS, ".project-picker-item:focus-visible{", "}")
    assert "outline:2px solid var(--focus-ring)" in rule
    # The current project keeps its colour under focus, as it does under hover.
    assert ".project-picker-item.active:focus-visible{color:var(--blue);}" in STYLE_CSS


def test_on_a_light_theme_the_ring_is_the_accent_and_the_pointer_has_a_wash():
    """The white wash and the translucent --focus-ring are both near invisible
    on a light picker. Scoped to :root:not(.dark), so a dark theme keeps both."""
    assert (
        ":root:not(.dark) .project-picker-item:focus-visible{outline-color:var(--accent);}"
        in STYLE_CSS
    )
    assert (
        ":root:not(.dark) .project-picker-item:hover{background:rgba(0,0,0,.05);}" in STYLE_CSS
    )


def test_the_menu_that_opens_the_picker_has_finger_tall_rows_on_touch():
    assert (
        "@media (pointer:coarse){.session-action-opt .ws-opt-action{min-height:44px;}}" in STYLE_CSS
    )
    # The row's own box is the .ws-opt-action inside it: that is what is padded.
    assert ".session-action-opt .ws-opt-action{display:flex;flex-direction:row;align-items:center;" in STYLE_CSS


def test_a_key_that_moves_focus_in_the_menu_reveals_the_row():
    """With 44px rows the menu scrolls inside itself on a phone on its side,
    and its rows are focused with preventScroll. What this does on a real
    screen is in the browser gate."""
    mount = _between(SESSIONS_JS, "function _mountSessionActionMenu(", "function _findSessionRenameRow(")
    focus = "try{items[nextIndex].focus({preventScroll:true});}catch(_){items[nextIndex].focus();}"

    assert mount.count("items[nextIndex].scrollIntoView({block:'nearest'});") == 1
    assert mount.index(focus) < mount.index("items[nextIndex].scrollIntoView({block:'nearest'});")


def test_the_button_keeps_the_rows_look():
    """A bare <button> brings its own border, background, font and centring. The
    reset has no specificity and sits before the row rules, so the create row's
    top border and every hover background still win over it."""
    reset = ":where(button.project-picker-item){"
    assert reset in STYLE_CSS
    rule = _between(STYLE_CSS, reset, "}")
    for declaration in ("width:100%", "background:none", "border:none", "font:inherit", "text-align:left"):
        assert declaration in rule, declaration
    assert STYLE_CSS.index(reset) < STYLE_CSS.index(".project-picker-item{padding:")
    assert STYLE_CSS.index(reset) < STYLE_CSS.index(".project-picker-create{")


def test_touch_batch_picker_is_capped_so_selected_rows_stay_visible():
    """Fable UX gate (2026-10-06): the inline batch picker's 44px touch rows would push the checked
    conversations off screen; on coarse pointers it scrolls inside a ~5.5-row cap."""
    css = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text(encoding="utf-8")
    assert "@media (pointer:coarse){.batch-action-bar .batch-project-picker{max-height:250px;overflow-y:auto;}}" in css


def test_single_picker_is_placed_as_the_session_action_menu_is():
    """Gate 2026-10-07: the picker had a room rule of its own, which left a short list
    scrolling in a strip beside a tall anchor while the ⋮ menu beside it slid into view.
    It now measures its natural height, flips above when that fits whole, slides over the
    anchor otherwise, and pins to the top and scrolls only when taller than the screen."""
    js = (Path(__file__).resolve().parent.parent / "static" / "sessions.js").read_text(encoding="utf-8")
    block = _between(js, "picker.style.maxHeight='';", "picker.style.bottom='auto';")
    for line in (
        "const pickerH=picker.offsetHeight||0;",
        "const maxAvail=window.innerHeight-margin*2;",
        "let top=rect.bottom+4;",
        "if(top+pickerH>window.innerHeight-margin && rect.top>pickerH+12){",
        "top=rect.top-pickerH-4;",
        "if(pickerH>maxAvail){",
        "picker.style.maxHeight=maxAvail+'px';",
        "if(top+pickerH>window.innerHeight-margin) top=window.innerHeight-margin-pickerH;",
        "if(top<margin) top=margin;",
        "picker.style.top=top+'px';",
    ):
        assert line in block, line
    assert "_pickerRoom" not in js and "spaceBelow<160" not in js


def test_an_open_single_picker_is_placed_again_on_resize():
    """Gate 2026-10-07, second pass: the placement wrote a fixed `top` once, so a shorter
    window or a turned phone left an open picker off the screen. The ⋮ menu it mirrors is
    placed again on resize; so is the picker now, by the row its conversation has by then."""
    js = (Path(__file__).resolve().parent.parent / "static" / "sessions.js").read_text(encoding="utf-8")
    show = _between(js, "function _showProjectPicker(session, anchorEl){", "\nfunction ")
    assert "_positionProjectPicker(picker,anchorEl);" in show
    assert "_wireProjectPickerKeys(picker,dismiss);" in show
    assert "_openProjectPicker={picker,anchorEl,sessionId:session.session_id,dismiss};" in show
    assert "picker.style.top=" not in show
    listener = _between(js, "let _openProjectPicker=null;", "// ── Project picker rows and keyboard")
    hook = listener[listener.index("window.addEventListener('resize',()=>{"):]
    assert "if(!open.picker.isConnected){" in hook
    assert "anchor=_findSessionRenameRow(open.sessionId);" in hook
    assert "_projectPickerFocusReturnTarget({session_id:open.sessionId},null)||anchor" in hook
    assert "_positionProjectPicker(open.picker,anchor);" in hook
    # Maintainer 2026-10-07: a turned phone hides the sidebar, and the picker floated over
    # the composer. Decided a frame later, after boot.js's own resize listener collapsed it.
    assert "requestAnimationFrame(()=>{" in hook
    hidden = "if(!anchor||!anchor.offsetParent||_projectPickerSidebarHidden(anchor)){"
    assert hidden in hook
    # Gate 2026-10-08: focus is not handed back to a row whose sidebar is going out of sight.
    closed = "open.dismiss({restoreFocus:false});"
    assert hook.index(hidden) < hook.index(closed) < hook.index("_positionProjectPicker(open.picker,anchor);")
    assert "if(opts&&opts.restoreFocus===false) return;" in show
    assert show.index("document.removeEventListener('click',close);") < show.index(
        "if(opts&&opts.restoreFocus===false) return;"
    ) < show.index("_focusSessionActionMenuRestoreTarget(_projectPickerFocusReturnTarget(session,anchorEl));")
    # Greptile 2026-10-07: the cap a shorter window puts on the picker can cover the focused row.
    assert "if(focused&&open.picker.contains(focused)) _focusProjectPickerRow(open.picker,focused);" in hook


def test_a_hidden_sidebar_is_a_collapsed_one_or_a_closed_drawer():
    """Gate 2026-10-08: the resize hook closed the picker for a collapsed desktop sidebar only.
    A large phone turned upright, or a window narrowed below 641px, leaves the sidebar a
    drawer that is not open, with no `sidebar-collapsed` class to read; the picker floated."""
    js = (Path(__file__).resolve().parent.parent / "static" / "sessions.js").read_text(encoding="utf-8")
    hidden = _between(js, "function _projectPickerSidebarHidden(el){", "\n}\n")
    lines = [line.strip() for line in hidden.splitlines() if line.strip()]
    assert lines == [
        "function _projectPickerSidebarHidden(el){",
        "const sidebar=el.closest('.sidebar');",
        # An open drawer is on screen at any width.
        "if(!sidebar||sidebar.classList.contains('mobile-open')) return false;",
        "if(sidebar.closest('.layout.sidebar-collapsed')) return true;",
        # boot.js owns the 641px breakpoint; sessions.js loads before it.
        "return typeof _isDesktopWidth==='function'&&!_isDesktopWidth();",
    ]
    boot = (Path(__file__).resolve().parent.parent / "static" / "boot.js").read_text(encoding="utf-8")
    assert "function _isDesktopWidth(){" in boot
    assert "window.matchMedia('(min-width:641px)').matches" in boot

