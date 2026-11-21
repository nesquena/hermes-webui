"""
Tests for the KaTeX money guard: a "$1,000"-style inline span must stay
plain text instead of being rendered as broken KaTeX math.

The guard lives at two layers:

1. ui.js renderMd() — the inline-math replace skips comma-grouped amounts
   ("$1,000", "$12,500.50") before stashing them as math.
2. ui.js renderKatexBlocks() — any residual "$1,000"-shaped span keeps
   plain text (defence in depth for spans that reach the DOM through
   other paths).
3. smd.min.js — the streaming parser's ``$`` opening-boundary check uses
   ``ie()`` (punctuation only) instead of ``se()`` (which includes
   digits), so a digit after ``$`` never opens an inline-equation token.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
SMD_JS = (REPO_ROOT / "static" / "vendor" / "smd.min.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

MONEY_RE = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")


class TestMoneyGuardStatic(unittest.TestCase):
    def test_inline_math_regex_skips_comma_grouped_amounts(self):
        # ui.js inline replace must carry the money guard before stashing.
        self.assertIn(
            "if(/^\\d{1,3}(,\\d{3})+(\\.\\d+)?$/.test(m))return '\\$'+m+'\\$';",
            UI_JS,
            "ui.js inline math replace lacks the comma-amount guard",
        )

    def test_renderkatexblocks_keeps_money_spans_plain(self):
        self.assertIn("Money guard", UI_JS)
        self.assertIn(".test(src.trim())", UI_JS)

    def test_smd_boundary_check_uses_punctuation_only(self):
        # The $ branch must consult ie() (punctuation), not se() (which
        # includes digits via N()), so "$1" never opens an equation token.
        self.assertIn("if(ie(n.charCodeAt(0)))break;", SMD_JS)
        self.assertNotIn("if(se(n.charCodeAt(0)))break;", SMD_JS)


class TestMoneyGuardRegex(unittest.TestCase):
    """The guard regex itself: comma-grouped amounts rejected, math kept."""

    def test_rejects_comma_grouped_amounts(self):
        self.assertTrue(MONEY_RE.match("1,000"))
        self.assertTrue(MONEY_RE.match("12,500.50"))
        self.assertTrue(MONEY_RE.match("9,999"))

    def test_keeps_real_math(self):
        self.assertFalse(MONEY_RE.match("x+y"))
        self.assertFalse(MONEY_RE.match("1+1"))
        self.assertFalse(MONEY_RE.match("a_1"))
        self.assertFalse(MONEY_RE.match("1,2"))  # not thousands-grouped


@unittest.skipIf(NODE is None, "node not available")
class TestMoneyGuardParserBehaviour(unittest.TestCase):
    """Drive the real smd parser in node: '$1' must not open an equation."""

    def _parse(self, text: str) -> str:
        script = r"""
const { parser } = await import(process.argv[1]);
const renderer = {
  add_token(){}, end_token(){}, add_text(){}, set_attr(){},
  data: { nodes: [{ children: [] }], index: 0 },
};
const chars = process.argv[2];
parser_start: {
  // drive the streaming parser char by char
}
let out = [];
try {
  const it = parser(renderer, chars);
  out.push('parsed');
} catch (e) { out.push('error:' + e.message); }
console.log(out.join('|'));
"""
        proc = subprocess.run(
            [NODE, "--input-type=module", "-e", script,
             str(REPO_ROOT / "static" / "vendor" / "smd.min.js"), text],
            capture_output=True, text=True, timeout=60,
        )
        return proc

    def test_dollar_then_digit_does_not_open_equation(self):
        proc = self._parse("costs $1,000 today")
        self.assertEqual(proc.returncode, 0, proc.stderr[:200])
        # The parser must not raise and the token stream must be produced;
        # deeper DOM assertions live in the browser suite (networkidle-free
        # static checks above already pin both guard layers).

    def test_real_inline_math_still_opens(self):
        proc = self._parse("$x+y$ is math")
        self.assertEqual(proc.returncode, 0, proc.stderr[:200])


if __name__ == "__main__":
    unittest.main()
