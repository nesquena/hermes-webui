"""Regression tests for PR #2428 — table renderer pipe protection.

These pin three classes of input that previously mis-split into too many table
cells because `parseRow` / `parseHeader` ran a naive `split('|')` over the row
text:

1. Single-pipe inside parens/brackets (e.g. ``(a|b)``, ``[int|float]``) — was
   the original #2428 bug.
2. Multi-pipe inside one bracket pair (e.g. ``(a|b|c)``, ``Union[int|float|str]``) —
   common Python type hint / regex character class shape; first version of the
   #2428 fix protected only the first pipe per pair and still mis-split here.
3. Cells containing apostrophes alongside protected pipes (e.g. ``('a'|'b')``) —
   the intermediate version of the fix added ``'`` to the negated character
   classes which caused a regression on string-literal-in-union input.

Tests drive the real `renderMd()` in `static/ui.js` via the same node-driver
pattern used by `test_renderer_js_behaviour.py` so the regex can't silently
regress in either direction.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
global.window = {};
global.document = { createElement: () => ({ innerHTML: '', textContent: '' }) };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
eval(extractFunc('_matchBacktickFenceLine'));
eval(extractFunc('_isBacktickFenceClose'));
eval(extractFunc('renderMd'));

let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => { process.stdout.write(renderMd(buf)); });
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("table_pipe_protect_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _render(driver_path: str, markdown: str) -> str:
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        input=markdown,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return result.stdout


def _table_cells(html: str) -> list[str]:
    """Extract td/th text content in document order.

    Returns the inner HTML of each <td> or <th>. Two-pass: match <td> cells
    and <th> cells separately so the same regex can use a single fixed tag
    name (avoiding the open-th-close-td mismatch that a `<t[dh]>...</t[dh]>`
    pattern would allow).
    """
    import re
    out = []
    for tag in ("th", "td"):
        # We need positions to preserve document order across the two tags.
        for m in re.finditer(rf"<{tag}\b[^>]*>(.*?)</{tag}>", html, re.S):
            out.append((m.start(), m.group(1).strip()))
    out.sort(key=lambda p: p[0])
    return [c for _, c in out]


class TestSinglePipeInBracketsStaysInOneCell:
    """Original #2428 bug: cells containing a single pipe inside parens or
    brackets used to split into two cells. _protectPipes was added to handle
    this. These tests pin the single-pipe case."""

    def test_pipe_inside_parens_stays_in_one_cell(self, driver_path):
        md = "| `(a|b)` | x |\n|---|---|\n| ok | y |"
        cells = _table_cells(_render(driver_path, md))
        # 2 columns × 2 rows = 4 cells total (header row + 1 data row).
        assert len(cells) == 4, f"expected 4 cells, got {len(cells)}: {cells!r}"
        # The first header cell must contain the full (a|b) backtick span.
        assert "(a|b)" in cells[0], (
            f"`(a|b)` inside backticks must stay in one cell. Got cells: {cells!r}"
        )

    def test_pipe_inside_square_brackets_stays_in_one_cell(self, driver_path):
        md = "| `Union[int|float]` | bracket |\n|---|---|\n| ok | y |"
        cells = _table_cells(_render(driver_path, md))
        assert len(cells) == 4, f"expected 4 cells, got {len(cells)}: {cells!r}"
        assert "Union[int|float]" in cells[0], (
            f"`Union[int|float]` must stay in one cell. Got: {cells!r}"
        )


class TestMultiPipeInBracketsStaysInOneCell:
    """First version of #2428 fix only protected the first pipe per bracket
    pair. The shipped version uses a `do { ... } while (r !== prev)` loop so
    iterative passes catch all pipes inside one pair. These tests pin that
    iteration."""

    def test_three_pipes_inside_parens(self, driver_path):
        md = "| `(a|b|c)` | x |\n|---|---|\n| ok | y |"
        cells = _table_cells(_render(driver_path, md))
        assert len(cells) == 4, (
            f"three-pipe paren cell must not split. Got {len(cells)} cells: {cells!r}"
        )
        assert "(a|b|c)" in cells[0], f"got cells: {cells!r}"

    def test_three_pipes_inside_square_brackets(self, driver_path):
        md = "| `Union[int|float|str]` | bracket |\n|---|---|\n| ok | y |"
        cells = _table_cells(_render(driver_path, md))
        assert len(cells) == 4, (
            f"three-pipe square-bracket cell must not split. "
            f"Got {len(cells)} cells: {cells!r}"
        )
        assert "Union[int|float|str]" in cells[0], f"got cells: {cells!r}"

    def test_four_pipes_inside_parens(self, driver_path):
        md = "| `(a|b|c|d)` | x |\n|---|---|\n| ok | y |"
        cells = _table_cells(_render(driver_path, md))
        assert len(cells) == 4, (
            f"four-pipe paren cell must not split. Got {len(cells)} cells: {cells!r}"
        )
        assert "(a|b|c|d)" in cells[0], f"got cells: {cells!r}"


class TestApostrophesInsideProtectedBrackets:
    """The first iterative version of the #2428 fix added `'` to the negated
    character classes (`[^)\\]}'>]`), which broke cells containing string
    literals separated by pipes (e.g. `('a'|'b')` in Python type-union
    examples). The shipped form drops the `'`-stop so apostrophes can live
    inside protected content."""

    def test_apostrophes_inside_parens_with_pipe(self, driver_path):
        md = "| `('a'|'b')` | x |\n|---|---|\n| ok | y |"
        cells = _table_cells(_render(driver_path, md))
        assert len(cells) == 4, (
            f"apostrophe-and-pipe-in-parens must not split. "
            f"Got {len(cells)} cells: {cells!r}"
        )
        # The apostrophes survive inlineMd's esc() pass as `&#39;`, so look
        # for either form depending on whether the cell content was escaped.
        first = cells[0]
        assert "'a'" in first or "&#39;a&#39;" in first, (
            f"cell content must contain the escaped or literal `'a'`. Got: {first!r}"
        )

    def test_apostrophes_inside_square_brackets_with_pipe(self, driver_path):
        md = "| `Tuple['a'|'b']` | x |\n|---|---|\n| ok | y |"
        cells = _table_cells(_render(driver_path, md))
        assert len(cells) == 4, (
            f"apostrophe-and-pipe-in-brackets must not split. "
            f"Got {len(cells)} cells: {cells!r}"
        )

    def test_apostrophe_outside_bracket_pair(self, driver_path):
        """Smoke test — apostrophes that are not part of a pipe-protect range
        must still render normally."""
        md = "| `it's a test` | x |\n|---|---|\n| ok | y |"
        cells = _table_cells(_render(driver_path, md))
        assert len(cells) == 4, f"plain apostrophe must not split. Got: {cells!r}"


class TestKatexDollarInTableCell:
    """The second half of #2428 — KaTeX `$...$` math spans containing ` | `
    must not stash math when they straddle table column separators."""

    def test_katex_with_pipe_separator_inside_does_not_stash_as_math(self, driver_path):
        # Two cells, second cell starts with `$x` and first ends with `$`.
        # Pre-fix, the inline-math regex would greedily match across `|` and
        # turn the row into one mathy cell. Post-fix, the `m.includes(' | ')`
        # guard skips the math stash for this shape.
        md = "| $5 | $10 |\n|---|---|\n| ok | y |"
        out = _render(driver_path, md)
        cells = _table_cells(out)
        assert len(cells) == 4, (
            f"`$5 | $10` must remain two cells, not collapse to one math span. "
            f"Got {len(cells)} cells: {cells!r}"
        )


class TestComparisonOperatorsAcrossColumns:
    """The first cut of #2428 included `<` and `>` in the protected-bracket
    set. That caused tables containing comparison operators across adjacent
    columns to mis-collapse: ``| x < 5 | y > 10 |`` matched `< … >` as a
    bracket pair and stashed the inner pipe, producing one cell instead of
    two. Stage-fix removed `<` / `>` from the bracket set because real LLM
    output uses angle brackets as comparison operators far more often than
    as a content-grouping pair."""

    def test_less_than_greater_than_across_columns_stays_two_cells(self, driver_path):
        md = "| x < 5 | y > 10 |\n|---|---|\n| less | more |"
        cells = _table_cells(_render(driver_path, md))
        assert len(cells) == 4, (
            f"`| x < 5 | y > 10 |` must produce 4 cells (2 cols × 2 rows). "
            f"Got {len(cells)} cells: {cells!r}"
        )

    def test_less_than_alone_in_cell(self, driver_path):
        md = "| a < b | c |\n|---|---|\n| ok | y |"
        cells = _table_cells(_render(driver_path, md))
        assert len(cells) == 4, f"got cells: {cells!r}"

    def test_greater_than_alone_in_cell(self, driver_path):
        md = "| a > b | c |\n|---|---|\n| ok | y |"
        cells = _table_cells(_render(driver_path, md))
        assert len(cells) == 4, f"got cells: {cells!r}"
