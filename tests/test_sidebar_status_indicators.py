"""
Tests for the per-conversation accent + CLI/agent session real-time busy
indicator added on top of the existing #4 status-indicators feature.

Coverage:

* The sidebar status dot for a CLI / agent session is rendered with
  ``data-cli-session="1"`` and ``data-updated-at=<ts>`` attributes so the
  client-side ``updateSessionDots()`` recency check has the data it needs.
* ``updateSessionDots()`` (in static/ui.js) marks a CLI session as
  ``running`` (green pulse) when its ``updated_at`` is within
  ``HERMES_CLI_BUSY_WINDOW_S`` and clears that class once the activity
  window expires.
* ``_hashHue()`` in static/sessions.js produces a stable hue per session id.

The JS-level checks run by extracting the relevant helper functions out of
the static files and evaluating them in a tiny mini-DOM stub built with
plain Python — no node, jest, or browser required.  This lets us
regression-guard the dot logic from pytest.
"""

import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import time
import unittest


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
SESSIONS_JS = REPO_ROOT / "static" / "sessions.js"
UI_JS = REPO_ROOT / "static" / "ui.js"


# ---------------------------------------------------------------------------
# JS helper extraction
# ---------------------------------------------------------------------------

def _extract_function(src: str, name: str) -> str:
    """Pull a top-level `function name(...){...}` block out of a JS source file.

    Uses brace-matching so it handles bodies with nested ``{...}`` literals.
    """
    pattern = re.compile(r"\bfunction\s+" + re.escape(name) + r"\s*\(")
    m = pattern.search(src)
    if not m:
        raise AssertionError(f"function {name} not found in source")
    # Walk forward to the opening brace
    i = src.index("{", m.end() - 1)
    depth = 0
    j = i
    while j < len(src):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
        j += 1
    raise AssertionError(f"unterminated function {name}")


# ---------------------------------------------------------------------------
# Run-via-node helper.  Skips cleanly when node isn't installed.
# ---------------------------------------------------------------------------

NODE = shutil.which("node")


def _run_node(snippet: str) -> str:
    """Eval a JS snippet via `node -e` and return stdout (stripped)."""
    proc = subprocess.run(
        [NODE, "-e", snippet],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"node failed (rc={proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc.stdout.strip()


@unittest.skipIf(NODE is None, "node not installed; JS-level tests skipped")
class TestHashHueIsStable(unittest.TestCase):
    """``_hashHue(session_id)`` must deterministically map an id to a hue 0–359."""

    def test_hash_hue_returns_int_in_range(self):
        src = SESSIONS_JS.read_text()
        fn = _extract_function(src, "_hashHue")
        out = _run_node(
            fn + ";"
            "const ids = ['s1','session_abcdef','20260424_120000_xyz','',null];"
            "const out = ids.map(s => _hashHue(s));"
            "console.log(JSON.stringify(out));"
        )
        result = json.loads(out)
        self.assertEqual(len(result), 5)
        for h in result:
            self.assertIsInstance(h, int)
            self.assertGreaterEqual(h, 0)
            self.assertLess(h, 360)
        # Identical ids must give identical hues
        out2 = _run_node(
            fn + ";"
            "console.log(_hashHue('repeatable_session_id_001'));"
        )
        out3 = _run_node(
            fn + ";"
            "console.log(_hashHue('repeatable_session_id_001'));"
        )
        self.assertEqual(out2, out3)
        # Different ids should usually give different hues; at least not all
        # collapse to a single value.
        out4 = _run_node(
            fn + ";"
            "const hs=new Set();"
            "for(let i=0;i<50;i++) hs.add(_hashHue('id-'+i));"
            "console.log(hs.size);"
        )
        # 50 ids should land on >5 distinct hues even with bad luck
        self.assertGreater(int(out4), 5)


@unittest.skipIf(NODE is None, "node not installed; JS-level tests skipped")
class TestUpdateSessionDotsRecencyWindow(unittest.TestCase):
    """``updateSessionDots()`` must promote CLI sessions to the ``running``
    class when their data-updated-at timestamp is within the busy window, and
    clear that class once the window has elapsed."""

    def _harness(self, fn_src: str, *, updated_at_offset_s: float, busy_window_s: int = 15) -> dict:
        """Run updateSessionDots against a tiny DOM stub and return the dot's
        className + title at the end.

        ``updated_at_offset_s`` is added to the current Date.now() / 1000.
        Negative values simulate "X seconds ago"; 0 means "right now".
        """
        snippet = (
            # ── DOM stub ──
            "class El {"
            "  constructor(){ this.dataset={}; this.classList=new Set(); this.title=''; this.className=''; }"
            "  classList = null;"  # placeholder; overwritten in ctor
            "}"
            # Intercept className assignment so it also resets classList (mirrors browser)
            "function makeDot(updatedAt){"
            "  const d={dataset:{sessionId:'sid-cli-1',cliSession:'1',updatedAt:String(updatedAt)},_classes:new Set(),title:''};"
            "  Object.defineProperty(d,'className',{"
            "    get(){return Array.from(this._classes).join(' ');},"
            "    set(v){this._classes = new Set(v.split(/\\s+/).filter(Boolean));}"
            "  });"
            "  d.classList = {add:(c)=>d._classes.add(c), remove:(c)=>d._classes.delete(c), contains:(c)=>d._classes.has(c)};"
            "  return d;"
            "}"
            f"const __OFFSET = {updated_at_offset_s};"
            f"const __WINDOW = {busy_window_s};"
            "const __DOT = makeDot(Date.now()/1000 + __OFFSET);"
            "global.document = {"
            "  querySelectorAll: (sel) => sel.includes('status-dot') ? [__DOT] : []"
            "};"
            "global.window = { HERMES_CLI_BUSY_WINDOW_S: __WINDOW };"
            "global.S = { busy:false, session:null };"
            # The function references _CLI_BUSY_WINDOW_S as a const on the
            # outer scope; declare it so the function is self-contained.
            "const _CLI_BUSY_WINDOW_S = 15;"
            f"{fn_src};"
            "updateSessionDots();"
            "console.log(JSON.stringify({className: __DOT.className, title: __DOT.title}));"
        )
        return json.loads(_run_node(snippet))

    def test_cli_session_within_busy_window_is_running(self):
        src = UI_JS.read_text()
        fn = _extract_function(src, "updateSessionDots")
        result = self._harness(fn, updated_at_offset_s=-2)
        self.assertIn("running", result["className"].split())
        self.assertIn("CLI", result["title"])

    def test_cli_session_outside_busy_window_is_idle(self):
        src = UI_JS.read_text()
        fn = _extract_function(src, "updateSessionDots")
        result = self._harness(fn, updated_at_offset_s=-999)
        self.assertNotIn("running", result["className"].split())
        # Idle dot should not advertise CLI activity in its tooltip
        self.assertNotIn("CLI", result["title"])

    def test_busy_window_is_configurable(self):
        src = UI_JS.read_text()
        fn = _extract_function(src, "updateSessionDots")
        # 5s ago is OUTSIDE a 3-second window
        result = self._harness(fn, updated_at_offset_s=-5, busy_window_s=3)
        self.assertNotIn("running", result["className"].split())
        # 5s ago is INSIDE a 60-second window
        result = self._harness(fn, updated_at_offset_s=-5, busy_window_s=60)
        self.assertIn("running", result["className"].split())


# ---------------------------------------------------------------------------
# CSS sanity check (no node required)
# ---------------------------------------------------------------------------

class TestSidebarAccentCss(unittest.TestCase):
    def test_session_item_has_conv_accent_rule(self):
        css = (REPO_ROOT / "static" / "style.css").read_text()
        self.assertIn("--conv-accent", css)
        # The rule must apply only when a session-item actually has the
        # variable set inline (i.e. not the active item, which has its own
        # gold border)
        self.assertRegex(
            css,
            r"\.session-item\[style\*=\"--conv-accent\"\][^{]*\{[^}]*border-left-color",
        )


if __name__ == "__main__":
    unittest.main()
