"""Behavioural test for the composer's ~/path autocomplete (static/commands.js).

Typing a ``~/path`` token used to fire one ``GET /api/workspaces/suggest`` per
input event — no debounce, no in-flight dedupe, no cache. This drives the
ACTUAL functions from static/commands.js via node against a mocked ``api`` and
asserts the observable request behaviour, mirroring the approach of
``test_goal_command_js_behaviour.py``:

- a typing burst collapses into ONE request, for the final prefix;
- a prefix that is already in flight is not re-requested;
- the returned match shape and the response filtering are unchanged;
- text without a ``~/`` token still issues nothing.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS_PATH = REPO_ROOT / "static" / "commands.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extract(name) {
  const marker = 'function ' + name + '(';
  let start = src.indexOf(marker);
  if (start < 0) throw new Error('missing function ' + name);
  if (src.slice(Math.max(0, start - 6), start) === 'async ') start -= 6;
  const brace = src.indexOf('{', start);
  let depth = 1, i = brace + 1;
  while (i < src.length && depth > 0) {
    if (src[i] === '{') depth += 1;
    else if (src[i] === '}') depth -= 1;
    i += 1;
  }
  if (depth !== 0) throw new Error('unbalanced braces for ' + name);
  return src.slice(start, i);
}

// Bind to the REAL debounce constant and state declaration (drift guard).
const dm = src.match(/_PATH_SUGGEST_DEBOUNCE_MS\s*=\s*(\d+)/);
if (!dm) throw new Error('_PATH_SUGGEST_DEBOUNCE_MS declaration missing');
const DEBOUNCE = Number(dm[1]);
if (!/const _pathSuggest=\{[^}]*inflight:new Map\(\)[^}]*\};/.test(src)) {
  throw new Error('_pathSuggest declaration missing');
}

const fns = [
  '_findComposerPathToken',
  '_fetchPathSuggestions',
  '_debouncedPathSuggestions',
  'getComposerPathAutocompleteMatches',
].map(extract).join('\n');

const sleep = ms => new Promise(r => setTimeout(r, ms));

function makeEnv() {
  const calls = [];
  const state = { delay: 30, reply: () => ({ suggestions: ['~/Documents', '~/Docs', '/tmp/other'] }) };
  const _pathSuggest = { timer: null, inflight: new Map() };
  const api = url => {
    calls.push(url);
    const prefix = decodeURIComponent(url.split('prefix=')[1] || '');
    return new Promise(res => setTimeout(() => res(state.reply(prefix)), state.delay));
  };
  const factory = new Function(
    '_pathSuggest', '_PATH_SUGGEST_DEBOUNCE_MS', 'api', 'URLSearchParams',
    fns + '\nreturn {getComposerPathAutocompleteMatches};'
  );
  const env = factory(_pathSuggest, DEBOUNCE, api, URLSearchParams);
  return { calls, env, state };
}

(async () => {
  const report = { debounce: DEBOUNCE };

  // A) a typing burst collapses into ONE request for the final prefix
  {
    const t = makeEnv();
    const burst = ['~/', '~/D', '~/Do', '~/Doc', '~/Docu', '~/Documents'];
    const promises = burst.map(text => t.env.getComposerPathAutocompleteMatches(text, text.length));
    await sleep(DEBOUNCE + 150);
    const last = await promises[promises.length - 1];
    report.burst = { calls: t.calls, matches: last.map(m => m.value) };
  }

  // B) a prefix already in flight is not re-requested (single-flight)
  {
    const t = makeEnv();
    t.state.delay = 400;
    const p1 = t.env.getComposerPathAutocompleteMatches('~/Doc', 5);
    await sleep(DEBOUNCE + 60); // past the debounce; first request in flight
    const p2 = t.env.getComposerPathAutocompleteMatches('~/Doc', 5);
    await sleep(DEBOUNCE + 500);
    await Promise.all([p1, p2]);
    report.singleFlight = { calls: t.calls };
  }

  // C) match shape + response filtering unchanged
  {
    const t = makeEnv();
    const p = t.env.getComposerPathAutocompleteMatches('~/Doc', 5);
    await sleep(DEBOUNCE + 150);
    report.shape = { matches: await p };
  }

  // D) text without a ~/ token issues nothing
  {
    const t = makeEnv();
    const out = await t.env.getComposerPathAutocompleteMatches('hello world', 11);
    await sleep(DEBOUNCE + 150);
    report.plain = { calls: t.calls, matches: out };
  }

  console.log(JSON.stringify(report));
})().catch(e => {
  console.error(String((e && e.stack) || e));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    driver = tmp_path_factory.mktemp("pathautocomplete") / "driver.js"
    driver.write_text(_DRIVER_SRC)
    assert COMMANDS_JS_PATH.exists(), "static/commands.js missing"
    assert NODE is not None  # pytestmark skips when node is unavailable
    proc = subprocess.run(
        [NODE, str(driver), str(COMMANDS_JS_PATH)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"driver failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_typing_burst_issues_one_request_for_the_final_prefix(report):
    burst = report["burst"]
    assert len(burst["calls"]) == 1, f"expected 1 request, saw {len(burst['calls'])}: {burst['calls']}"
    assert "prefix=%7E%2FDocuments" in burst["calls"][0]
    assert burst["matches"] == ["~/Documents"]


def test_identical_prefix_already_in_flight_is_not_re_requested(report):
    assert len(report["singleFlight"]["calls"]) == 1, report["singleFlight"]["calls"]


def test_match_shape_and_filtering_unchanged(report):
    assert report["shape"]["matches"] == [
        {
            "name": "~/Documents",
            "value": "~/Documents",
            "desc": "Workspace path",
            "source": "path",
            "tokenStart": 0,
            "tokenEnd": 5,
        },
        {
            "name": "~/Docs",
            "value": "~/Docs",
            "desc": "Workspace path",
            "source": "path",
            "tokenStart": 0,
            "tokenEnd": 5,
        },
    ]


def test_non_tilde_text_issues_no_request(report):
    assert report["plain"]["calls"] == []
    assert report["plain"]["matches"] == []