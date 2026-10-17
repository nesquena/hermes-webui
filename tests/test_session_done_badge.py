"""Sidebar `done` badge: a row with `done_at` renders the badge, a row without it does not.

`done_at` is the structural "this conversation is finished" flag (written by the
close/checkmark scripts through POST /api/session/mark-done). The sidebar must
render it as data — never by inferring from a title prefix or from silence.

The repo intentionally avoids a node/jsdom dependency (see AGENTS.md), so this
file follows the WEBUI-SUB convention: precise structural assertions on
`static/sessions.js` + `static/style.css`, plus one real execution of the
extracted render fragment under node with a minimal fake DOM. That way removing
the `if(s.done_at)` guard, dropping the `.done` row class, or inlining the wrong
i18n key reddens the test instead of silently rendering nothing.

i18n: every locale block must carry both keys, or the badge falls back to the
raw key name in that language.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node required")


DONE_PREDICATE = "s.done_at!==null&&s.done_at!==undefined&&s.done_at!==''"


def _done_badge_block() -> str:
    """The `if(<done predicate>){...}` fragment from the row renderer."""
    pattern = r"^    if\(" + re.escape(DONE_PREDICATE) + r"\)\{.*?^    \}\n"
    m = re.search(pattern, SESSIONS_JS, re.S | re.M)
    assert m, "done badge block not found in static/sessions.js"
    return m.group(0)


def _run_render(rows):
    """Execute the extracted fragment against a minimal fake DOM."""
    block = _done_badge_block()
    harness = f"""
class FakeEl {{
  constructor(tag) {{
    this.tagName = tag.toUpperCase();
    this.className = '';
    this.textContent = '';
    this.title = '';
    this.children = [];
  }}
  appendChild(child) {{ this.children.push(child); return child; }}
}}
const document = {{ createElement: (tag) => new FakeEl(tag) }};
function t(key, ...args) {{
  const table = {{
    session_done_badge: 'Done',
    session_done_tooltip: 'Marked done {{0}}',
    session_time_unknown: 'unknown',
  }};
  const value = table[key];
  if (value === undefined) return key;
  return value.replace('{{0}}', String(args[0] === undefined ? '' : args[0]));
}}
function _formatRelativeSessionTime(ms) {{ return ms ? '2h ago' : 'unknown'; }}

const out = [];
for (const s of {json.dumps(rows)}) {{
  const titleRow = new FakeEl('span');
  // The row-class predicate, verbatim from _renderOneSession (presence, not
  // truthiness — a stored 0 is a valid timestamp).
  const isDone = {DONE_PREDICATE};
  {block}
  out.push({{ isDone, children: titleRow.children.map(c => ({{
    className: c.className,
    textContent: c.textContent,
    title: c.title,
  }})) }});
}}
console.log(JSON.stringify(out));
"""
    proc = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_row_with_done_at_renders_the_badge():
    out = _run_render([{"session_id": "a", "done_at": 1700000000.0}])
    assert len(out[0]["children"]) == 1, out
    badge = out[0]["children"][0]
    assert badge["className"] == "session-done-badge"
    assert badge["textContent"] == "Done"
    assert badge["title"] == "Marked done 2h ago"
    assert out[0]["isDone"] is True


def test_row_without_done_at_renders_nothing():
    out = _run_render([{"session_id": "b"}, {"session_id": "c", "done_at": None}])
    assert out == [
        {"isDone": False, "children": []},
        {"isDone": False, "children": []},
    ], out


def test_done_at_zero_still_reads_as_done():
    """0 is a valid timestamp: presence, not truthiness, decides (JS `if(0)` is false)."""
    out = _run_render([{"session_id": "d", "done_at": 0}])
    assert out[0]["isDone"] is True
    assert len(out[0]["children"]) == 1, out


def test_done_row_gets_the_done_class_and_archived_stays_independent():
    """`done` is workflow state, `archived` is storage: both classes can coexist."""
    # The class is appended next to `archived`, never as a replacement for it.
    m = re.search(r"el\.className='session-item'.*?;(?=\n)", SESSIONS_JS)
    assert m, "session-item className line not found"
    line = m.group(0)
    assert "(s.archived?' archived':'')" in line
    assert f"(({DONE_PREDICATE})?' done':'')" in line
    assert "s.archived?s.done_at" not in line and "s.done_at?s.archived" not in line


def test_badge_style_is_defined_once():
    # Base rule exactly once; the `.active` variant is a separate selector and
    # contains the base string as a substring, so count the full selector line.
    assert "\n.session-done-badge{" in STYLE_CSS
    assert STYLE_CSS.count("\n.session-done-badge{") == 1
    assert ".session-item.active .session-done-badge{" in STYLE_CSS


def test_done_row_class_has_a_style_consumer():
    """A class the renderer emits and a test asserts must actually be styled —
    otherwise it is dead weight that reads as a feature."""
    assert ".session-item.done .session-title{" in STYLE_CSS


def test_every_locale_defines_both_done_keys():
    # Locale keys are bare (`en: {`) or quoted when they carry a hyphen
    # (`'zh-Hant': {`). An unquoted-only pattern silently skips zh-Hant and the
    # "every locale" assertion becomes a 14-of-15 claim that still passes.
    locales = re.findall(r"^  (?:'([A-Za-z-]+)'|([a-z]{2})): \{\s*$", I18N_JS, re.M)
    names = [a or b for a, b in locales]
    assert names, "no locale blocks found"
    assert len(names) >= 15, f"only {len(names)} locales found: {names}"
    missing = [
        loc
        for loc in names
        if "session_done_badge:" not in _locale_block(loc)
        or "session_done_tooltip:" not in _locale_block(loc)
    ]
    assert not missing, f"locales missing done keys: {missing}"


def _locale_block(loc: str) -> str:
    m = re.search(rf"^  (?:'{loc}'|{loc}): \{{\s*$", I18N_JS, re.M)
    assert m, f"locale {loc} not found"
    start = m.start()
    nxt = re.search(r"^  (?:'[A-Za-z-]+'|[a-z]{2}): \{\s*$", I18N_JS[start + 5 :], re.M)
    end = start + 5 + nxt.start() if nxt else len(I18N_JS)
    return I18N_JS[start:end]
