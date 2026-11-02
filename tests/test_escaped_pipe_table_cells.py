"""Regression tests for GFM backslash-escaped pipes in markdown table cells.

GFM defines ``\\|`` inside a table row as an escaped literal pipe, not a column
delimiter (the same rule that lets a cell contain a pipe at all). Before this
fix ``renderMd``'s ``_protectPipes`` only stashed pipes that sat inside a
matching bracket pair (``(a|b)``, ``Union[int|float]``) or an inline
``<code>`` span. A bare escaped pipe has no bracket partner, so it was left to
the naive ``split('|')`` and the cell broke apart:

    | 日志 error\\|panic\\|fatal | **0 行** |

rendered as four cells with the backslashes leaking into the visible text
(``error\\`` | ``panic\\`` | ``fatal`` | ``0 行``). Three cells of the row then
had no matching header column and the table body visibly drifted out of
alignment — the symptom a user reports as "the table doesn't line up".

Tests drive the real ``renderMd()`` in ``static/ui.js`` through a node driver,
following the pattern in ``test_issue2428_table_pipe_protection.py``, so the
regex cannot silently regress.
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
const _IMAGE_EXTS=/\\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _SVG_EXTS=/\\.svg$/i;
const _AUDIO_EXTS=/\\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm)$/i;
const _VIDEO_EXTS=/\\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;

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
    p = tmp_path_factory.mktemp("escaped_pipe_table_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _render(driver_path: str, markdown: str) -> str:
    import os

    env = dict(os.environ)
    env.pop("NODE_CHANNEL_FD", None)  # node SIGABRTs on exit if it survives
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        input=markdown,
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return result.stdout


def _table_cells(html: str) -> list[str]:
    """Extract td/th text content in document order."""
    import re

    out = []
    for tag in ("th", "td"):
        for m in re.finditer(rf"<{tag}\b[^>]*>(.*?)</{tag}>", html, re.S):
            out.append((m.start(), m.group(1).strip()))
    out.sort(key=lambda p: p[0])
    return [c for _, c in out]


def _row_cell_counts(html: str) -> list[int]:
    """Number of cells in each rendered <tr>, in document order."""
    import re

    return [len(re.findall(r"<t[dh]\b", row)) for row in re.findall(r"<tr>.*?</tr>", html, re.S)]


class TestEscapedPipeStaysInOneCell:
    """A ``\\|`` in a row must protect the pipe and consume the backslash."""

    def test_single_escaped_pipe_does_not_split(self, driver_path):
        md = "| a \\| b | c |\n|---|---|\n| ok | y |"
        cells = _table_cells(_render(driver_path, md))
        assert len(cells) == 4, f"escaped pipe must not split. Got: {cells!r}"

    def test_escaped_pipe_renders_as_bare_pipe(self, driver_path):
        """GFM: ``\\|`` is an escaped literal — the backslash must not survive."""
        md = "| a \\| b | c |\n|---|---|\n| ok | y |"
        cells = _table_cells(_render(driver_path, md))
        assert cells[0] == "a | b", f"backslash must be consumed. Got: {cells[0]!r}"

    def test_multiple_escaped_pipes_stay_in_one_cell(self, driver_path):
        """The reported shape: three alternatives in one cell."""
        md = "| 日志 error\\|panic\\|fatal | **0 行** |\n|---|---|\n| ok | y |"
        html = _render(driver_path, md)
        counts = _row_cell_counts(html)
        assert counts == [2, 2], f"each row must have exactly 2 cells. Got: {counts!r}"
        cells = _table_cells(html)
        assert cells[0] == "日志 error|panic|fatal", (
            f"escaped pipes must collapse to literals. Got: {cells[0]!r}"
        )
        assert "\\" not in html, f"no bare backslash may survive. Got: {html!r}"

    def test_escaped_pipe_in_body_row_keeps_table_aligned(self, driver_path):
        """The full reported table: one escaped-pipe row among plain rows.

        Pre-fix the body row emitted 4 cells against a 2-cell header, so every
        row after it inherited the widened column layout — the visible
        misalignment. Assert per-row cell counts instead of a total, because a
        total of 8 would pass for both [2,2,2,2] (correct) and [2,2,4] shapes.
        """
        md = (
            "| 项目 | 结果 |\n"
            "|---|---|\n"
            "| 版本探针 | v0.1.2666 |\n"
            "| 日志 error\\|panic\\|fatal | 0 行 |\n"
            "| 账号零丢失 | abc |\n"
        )
        html = _render(driver_path, md)
        counts = _row_cell_counts(html)
        assert counts == [2, 2, 2, 2], f"every row must have 2 cells. Got: {counts!r}"


class TestEscapedPipeInteractsWithExistingProtection:
    """The new rule must not disturb the bracket / code / comparison cases."""

    def test_escaped_pipe_inside_backticks_is_literal(self, driver_path):
        md = "| `a \\| b` | c |\n|---|---|\n| ok | y |"
        html = _render(driver_path, md)
        assert _row_cell_counts(html) == [2, 2], f"got: {html!r}"

    def test_bracket_pipe_still_protected_alongside_escaped_pipe(self, driver_path):
        """Both protection rules in one row must coexist."""
        md = "| `(a|b)` and c \\| d | e |\n|---|---|\n| ok | y |"
        html = _render(driver_path, md)
        assert _row_cell_counts(html) == [2, 2], f"got: {html!r}"

    def test_comparison_operators_across_columns_still_two_cells(self, driver_path):
        """Control: no escaped pipe here — the #2428 angle-bracket rule holds."""
        md = "| x < 5 | y > 10 |\n|---|---|\n| less | more |"
        html = _render(driver_path, md)
        assert _row_cell_counts(html) == [2, 2], f"got: {html!r}"

    def test_unescaped_pipe_still_separates_columns(self, driver_path):
        """Control: a plain pipe must keep splitting cells (no over-protection)."""
        md = "| a | b |\n|---|---|\n| ok | y |"
        html = _render(driver_path, md)
        assert _row_cell_counts(html) == [2, 2], f"got: {html!r}"
