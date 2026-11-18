"""Closing a model picker that holds keyboard focus returns focus to the button that opened it.

Release-review fold on #8445. That PR moved keyboard focus into the open picker (the search box,
or the container on a touch-only device). Before this fold both close paths only removed `open`,
so the focused node was hidden and focus fell to <body>: Tab restarted at the page edge and the
next `k` switched the conversation through the global J/K binding, which ignores keys whose
target is a button. Every anchor below is a <button>.

Runs the real `_returnModelPickerFocus`, `closeModelDropdown` and `closeSettingsModelDropdown`
from static/ui.js under node against a minimal DOM that keeps the two browser rules this relies
on: `contains()` is inclusive, and an element with no layout box (display:none) cannot take focus.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

UI_JS = pathlib.Path(__file__).resolve().parent.parent / "static" / "ui.js"
NODE = shutil.which("node")

_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');
// Optional so the pre-fold code still runs and fails on behaviour, not on extraction.
const OPTIONAL = new Set(['_returnModelPickerFocus']);
function extractFunc(name) {
  const start = ui.search(new RegExp('function\\s+' + name + '\\s*\\('));
  if (start < 0) { if (OPTIONAL.has(name)) return ''; throw new Error(name + ' not found'); }
  let i = ui.indexOf('{', ui.indexOf(')', start));
  let depth = 1; i++;
  while (depth > 0 && i < ui.length) { if (ui[i] === '{') depth++; else if (ui[i] === '}') depth--; i++; }
  return ui.slice(start, i);
}
let active = null;
const document = { get activeElement() { return active; } };
function node(id, opts) {
  const n = Object.assign({ id, visible: true, focusable: true, disabled: false, kids: [], parent: null }, opts || {});
  const cls = new Set(n.cls || []);
  n.classList = { add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c) };
  n.setAttribute = (k, v) => { n[k] = v; };
  n.getClientRects = () => (n.visible ? [{}] : []);
  n.focus = () => { if (n.focusable && n.visible && !n.disabled) active = n; };
  n.contains = (o) => { for (let x = o; x; x = x.parent) if (x === n) return true; return false; };
  n.add = (k) => { k.parent = n; n.kids.push(k); return k; };
  return n;
}
let restored = 0;
function _restoreModelDropdownHome() { restored++; }
const results = {};
function scenario(name, build) {
  active = null; restored = 0;
  const env = build();
  const $ = (id) => env.ids[id] || null;
  eval(extractFunc('_returnModelPickerFocus'));
  eval(extractFunc('closeModelDropdown'));
  eval(extractFunc('closeSettingsModelDropdown'));
  env.run($, closeModelDropdown, closeSettingsModelDropdown);
  const chip = env.ids.composerModelChip || env.ids.settingsModelChip;
  results[name] = { active: active ? active.id : null, open: env.dd ? env.dd.classList.contains('open') : null, restored,
                    expanded: chip ? chip['aria-expanded'] : null };
}
function composer(o) {
  const dd = node('composerModelDropdown', { cls: o.open === false ? [] : ['open'] });
  const search = dd.add(node('search'));
  const chip = node('composerModelChip', { visible: o.chipVisible !== false });
  const mobile = node('composerMobileModelAction', { visible: !!o.mobileVisible });
  const msg = node('msg');
  return { dd, search, chip, mobile, msg, ids: { composerModelDropdown: dd, composerModelChip: chip, composerMobileModelAction: mobile } };
}
scenario('search_focused_desktop', () => { const e = composer({}); e.run = ($, c) => { active = e.search; c(); }; return e; });
scenario('container_focused_touch', () => { const e = composer({ chipVisible: false, mobileVisible: true }); e.run = ($, c) => { active = e.dd; c(); }; return e; });
scenario('phone_skips_hidden_chip', () => { const e = composer({ chipVisible: false, mobileVisible: true }); e.run = ($, c) => { active = e.search; c(); }; return e; });
scenario('no_visible_anchor', () => { const e = composer({ chipVisible: false, mobileVisible: false }); e.run = ($, c) => { active = e.search; c(); }; return e; });
scenario('focus_outside_kept', () => { const e = composer({}); e.run = ($, c) => { active = e.msg; c(); }; return e; });
scenario('already_closed', () => { const e = composer({ open: false }); e.run = ($, c) => { active = e.search; c(); }; return e; });
scenario('settings_search_focused', () => {
  const dd = node('settingsModelDropdown', { cls: ['open'] });
  const search = dd.add(node('search'));
  const chip = node('settingsModelChip');
  return { dd, ids: { settingsModelDropdown: dd, settingsModelChip: chip }, run: ($, c, s) => { active = search; s(); } };
});
process.stdout.write(JSON.stringify(results));
"""


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    if not NODE:
        pytest.skip("node is not installed")
    driver = tmp_path_factory.mktemp("focus") / "driver.js"
    driver.write_text(_DRIVER)
    out = subprocess.run([str(NODE), str(driver), str(UI_JS)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_closing_the_composer_picker_from_its_search_box_focuses_the_chip(results):
    assert results["search_focused_desktop"] == {"active": "composerModelChip", "open": False, "restored": 1, "expanded": "false"}


def test_touch_container_focus_returns_to_the_visible_phone_action(results):
    assert results["container_focused_touch"]["active"] == "composerMobileModelAction"
    assert results["phone_skips_hidden_chip"]["active"] == "composerMobileModelAction"


def test_no_visible_anchor_leaves_focus_alone_and_still_closes(results):
    assert results["no_visible_anchor"] == {"active": "search", "open": False, "restored": 1, "expanded": "false"}


def test_a_close_after_focus_moved_elsewhere_does_not_steal_it(results):
    assert results["focus_outside_kept"]["active"] == "msg"


def test_closing_an_already_closed_picker_moves_nothing(results):
    assert results["already_closed"]["active"] == "search"


def test_closing_the_settings_picker_from_its_search_box_focuses_its_chip(results):
    assert results["settings_search_focused"] == {"active": "settingsModelChip", "open": False, "restored": 0, "expanded": "false"}


def test_closing_resets_the_disclosure_state_on_every_path(results):
    # The chips follow the WAI-ARIA disclosure pattern (aria-expanded + aria-controls; the
    # popup is role=group, so no aria-haspopup value applies). Every close reports collapsed.
    for name in ("search_focused_desktop", "container_focused_touch", "focus_outside_kept", "already_closed"):
        assert results[name].get("expanded") == "false", name


def test_triggers_declare_the_disclosure_pattern_not_a_listbox():
    html = (UI_JS.parent / "index.html").read_text()
    for chip in ("composerModelChip", "composerMobileModelAction", "settingsModelChip"):
        start = html.index(f'id="{chip}"')
        tag = html[html.rindex("<button", 0, start):html.index(">", start)]
        assert 'aria-expanded="false"' in tag, chip
        assert 'aria-controls="' in tag, chip
        assert "aria-haspopup" not in tag, chip


def test_escape_in_the_picker_stops_at_the_picker():
    # The document-level Escape handler (boot.js) closes the Settings panel, clears the
    # session search and cancels a message edit. Escape inside an open picker must close
    # only the picker; a second Escape reaches those. Browser proof: RM release review.
    src = UI_JS.read_text()
    start = src.index("const _modelPickerKey=(e)=>{")
    branch = src[start:src.index("return;}", start)]
    assert "e.stopPropagation()" in branch and "closeDropdown()" in branch
    custom = src[src.index("_ci.addEventListener('keydown'"):]
    custom = custom[:custom.index("});") + 3]
    assert "e.stopPropagation();closeDropdown()" in custom
