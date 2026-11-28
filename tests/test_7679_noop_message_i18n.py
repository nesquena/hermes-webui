"""#7679 — the Force no-op message must ride the locale runtime, not raw English.

Greptile P2 (thread 4607736698, 2026-11-18): the backend no-op branch
(``{ok:true,up_to_date:true}``) showed its message straight to ``showToast()``
as an English literal. The repository requires new user-facing copy to live in
the ``en`` locale and to be read through ``t()``, so every other locale would
render the English string while the rest of the panel is translated.

The test drives the REAL ``static/i18n.js`` (evaluated verbatim, so a missing
key behaves exactly as it does in production — it returns the key itself) and
the REAL ``forceUpdate()`` from ``static/ui.js``, and asserts two things:

1. the no-op branch reads its message through ``t()`` — no English literal is
   passed to ``showToast()``;
2. the key it reads exists in the ``en`` block, and ``t()`` resolves it to
   real copy rather than echoing the key name back to the user.

Both halves are discriminating: reverting the production change (a) turns the
source scan red and (b) makes ``t()`` return the raw key name.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest


REPO = pathlib.Path(__file__).parent.parent
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

NODE_BIN = str(NODE)

NOOP_KEY = "update_force_no_op"


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _extract_js_function(src: str, name: str) -> str:
    match = re.search(rf"(async\s+)?function\s+{re.escape(name)}\b", src)
    assert match, f"{name}() not found"
    open_paren = src.index("(", match.start())
    depth = 1
    idx = open_paren + 1
    while depth > 0 and idx < len(src):
        ch = src[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        idx += 1
    brace = src.index("{", idx)
    depth = 0
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[match.start() : i + 1]
    raise AssertionError(f"{name}() body was not balanced")


UI = _read("static/ui.js")
I18N = _read("static/i18n.js")

FN_FORCE = _extract_js_function(UI, "forceUpdate")

# The REAL locale runtime: static/i18n.js is evaluated verbatim in its own
# context, so the real t() and the real missing-key behaviour are what the
# assertions below measure.
_REAL_T = r"""
const __vm = require('vm');
const __i18nCtx = {
  console,
  localStorage: { getItem() { return null; }, setItem() {} },
  navigator: { languages: ['en-US'], language: 'en-US' },
  document: { documentElement: {}, querySelectorAll() { return []; } },
};
__vm.createContext(__i18nCtx);
__vm.runInContext(global.__I18N_SRC__, __i18nCtx);
const t = __i18nCtx.t;
"""


def _build(extra_js: str) -> str:
    return "\n".join(
        [
            f"global.__I18N_SRC__ = {json.dumps(I18N)};",
            _REAL_T,
            extra_js,
        ]
    )


def _run(program: str) -> dict:
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(program)
        path = handle.name
    try:
        proc = subprocess.run(
            [NODE_BIN, path], capture_output=True, text=True, timeout=60
        )
    finally:
        pathlib.Path(path).unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr[:4000]
    return json.loads(proc.stdout)


def test_the_no_op_branch_reads_its_message_through_t():
    """The no-op toast must not carry an English literal.

    Greptile P2 (4607736698): the branch sent English straight to
    ``showToast()``. Every other locale rendered that English string in a
    translated panel.
    """
    body = FN_FORCE
    # Locate the no-op branch: the backend answers {ok:true,up_to_date:true}.
    match = re.search(r"if\s*\(\s*res\.up_to_date\s*\)\s*\{", body)
    assert match, "forceUpdate() has no up_to_date no-op branch"

    # Take the branch body by brace matching.
    brace = body.index("{", match.end() - 1)
    depth = 0
    end = None
    for i in range(brace, len(body)):
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    assert end is not None, "the no-op branch body was not balanced"
    branch = body[brace : end + 1]

    # A showToast() call whose message is a bare string literal is the defect:
    # it bypasses t() entirely.
    toasts = re.findall(r"showToast\(\s*'([^']*)'", branch)
    assert toasts == [], (
        "the no-op branch sends an English literal to showToast() instead of "
        f"reading it through t(): {toasts!r}"
    )

    # And it must actually read the copy through the locale runtime.
    assert re.search(r"\bt\(\s*['\"]" + re.escape(NOOP_KEY) + r"['\"]", branch), (
        "the no-op branch does not read its message through t('"
        + NOOP_KEY
        + "')"
    )


def test_the_real_locale_defines_the_no_op_message():
    """The key must exist in the real locale runtime, not only as a fallback."""
    assert f"{NOOP_KEY}:" in I18N, (
        f"static/i18n.js has no {NOOP_KEY} entry, so the real locale runtime "
        "renders the raw key name to the user"
    )
    program = _build(
        "\nprocess.stdout.write(JSON.stringify({ v: t('" + NOOP_KEY + "') }));\n"
    )
    result = _run(program)
    assert result["v"] != NOOP_KEY, (
        f"t() returned the key itself: {result['v']!r}"
    )


def test_the_no_op_message_is_only_in_the_english_locale():
    """New English copy lives only in ``en`` and rides the existing fallback.

    The locale suites assert the inverse for every non-English block: a key
    that exists there is a duplicated English string. This test pins the same
    rule for the new key — it is present exactly once, in ``en``.
    """
    occurrences = [
        match.start()
        for match in re.finditer(r"^\s*" + re.escape(NOOP_KEY) + r"\s*:", I18N, re.M)
    ]
    assert len(occurrences) == 1, (
        f"{NOOP_KEY} is defined {len(occurrences)} times; new English copy must "
        "live only in the en locale"
    )
