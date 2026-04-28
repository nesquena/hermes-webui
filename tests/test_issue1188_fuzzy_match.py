"""
Regression tests for #1188 — _findModelInDropdown step-3 fuzzy match
returning a sibling-version model when the user's session model is unique.

The pre-fix logic stripped the trailing version segment from the target
(e.g. ``gpt-5.5`` → base ``gpt.5``) and matched against any option that
``startsWith(base)`` or ``includes(base)``. That over-matched: ``gpt.5.5``
returned ``@nous:openai/gpt-5.4-mini`` because ``gpt.5.4.mini`` starts
with ``gpt.5``.

The fix: use the FULL normalized target as the prefix when the stripped
base has meaningful content (length > 4 and base !== target). Only fall
back to the shorter base when it is a bare root word (``gpt``, ``claude``,
length ≤ 4) where stripping was effectively a no-op.

Tests below run the live ``_findModelInDropdown`` function via Node so
the real regex/normalization rules are exercised — drift between this
test and the JS would be caught by behavioural mismatch.
"""
import shutil
import subprocess

import pytest

REPO_ROOT = __import__("pathlib").Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');
function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = ui.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < ui.length) {
    if (ui[i] === '{') depth++;
    else if (ui[i] === '}') depth--;
    i++;
  }
  return ui.slice(start, i);
}
eval(extractFunc('_findModelInDropdown'));
const args = JSON.parse(process.argv[3]);
const sel = { options: args.options.map(v => ({value: v})) };
const got = _findModelInDropdown(args.modelId, sel);
process.stdout.write(JSON.stringify(got));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("findmodel_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _find(driver_path, model_id: str, options: list[str]):
    import json
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH),
         json.dumps({"modelId": model_id, "options": options})],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


# ── Regression: original #1188 false-match cases ───────────────────────────


class TestNoFalseSiblingVersionMatch:
    def test_gpt_5_5_does_not_match_gpt_5_4_mini(self, driver_path):
        """The exact bug from #1188: session model gpt-5.5 should NOT
        resolve to @nous:openai/gpt-5.4-mini just because both share
        the gpt.5 prefix once the trailing version is stripped."""
        got = _find(
            driver_path,
            "gpt-5.5",
            ["@nous:openai/gpt-5.4-mini", "@nous:anthropic/claude-opus-4.6"],
        )
        assert got is None, (
            "gpt-5.5 must not fuzzy-match gpt-5.4-mini (#1188)"
        )

    def test_claude_opus_4_7_does_not_match_claude_opus_4_6(self, driver_path):
        """Same shape: a different minor version must not be a fuzzy hit."""
        got = _find(
            driver_path,
            "claude-opus-4.7",
            ["@nous:anthropic/claude-opus-4.6"],
        )
        assert got is None, (
            "claude-opus-4.7 must not fuzzy-match claude-opus-4.6"
        )


# ── Things that should still match (no regression for legit fuzzy use) ─────


class TestPreservedFuzzyMatches:
    def test_gpt_5_5_finds_exact_provider_prefixed(self, driver_path):
        got = _find(
            driver_path,
            "gpt-5.5",
            ["@nous:openai/gpt-5.5", "@nous:openai/gpt-5.4-mini"],
        )
        assert got == "@nous:openai/gpt-5.5"

    def test_bare_root_gpt_matches_versioned_option(self, driver_path):
        """Short root targets still fall back to the looser prefix match."""
        got = _find(driver_path, "gpt", ["@nous:openai/gpt-5.4-mini"])
        assert got == "@nous:openai/gpt-5.4-mini"

    def test_short_target_gpt_5_falls_back_to_bare_root(self, driver_path):
        """When base after stripping is a bare root (length ≤ 4),
        fall back so user-typed shorthand still resolves."""
        got = _find(driver_path, "gpt-5", ["@nous:openai/gpt-5.4-mini"])
        assert got == "@nous:openai/gpt-5.4-mini"

    def test_bare_root_claude_matches(self, driver_path):
        got = _find(
            driver_path, "claude", ["@nous:anthropic/claude-opus-4.6"]
        )
        assert got == "@nous:anthropic/claude-opus-4.6"

    def test_target_without_version_suffix_still_matches(self, driver_path):
        """claude-opus has no trailing version → base === target → useBase
        path → still finds claude-opus-4.6 via prefix."""
        got = _find(
            driver_path, "claude-opus", ["@nous:anthropic/claude-opus-4.6"]
        )
        assert got == "@nous:anthropic/claude-opus-4.6"

    def test_exact_match_short_circuits(self, driver_path):
        got = _find(
            driver_path,
            "gpt-5.4-mini",
            ["@nous:openai/gpt-5.4-mini", "@nous:openai/gpt-5.5"],
        )
        assert got == "@nous:openai/gpt-5.4-mini"

    def test_unrelated_target_returns_null(self, driver_path):
        got = _find(
            driver_path, "mistral-large", ["@nous:openai/gpt-5.4-mini"]
        )
        assert got is None
