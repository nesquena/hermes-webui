"""Behavioural regression guard for issue #4622: in the Transparent Stream /
Worklog view, restored (settled / reloaded) tool-call cards must keep showing
the FULL terminal output with a working expand control, and patch/edit cards
must still render their diff body.

This drives the ACTUAL ``buildToolCard`` and ``_anchorSceneToolCallFromRow``
from ``static/ui.js`` via node (not a regex over the source). The reload path
rebuilds a tool card from a persisted ``activity_scene_v1`` row through
``_anchorSceneToolCallFromRow`` and then ``buildToolCard``; this pins that the
reconstructed card:

  * produces a "Show more" expand button for output over the 800-char cap, with
    the FULL output carried in ``data-full=`` (so the inline ``_toggleToolDiff``
    expand handler can reveal it), and
  * renders a ``diff-block`` for patch/edit snippets.

#4622 was reported as a regression after the v0.51.547-.560 wave (which actually
*restored* this activity via #4539/#4587). Independent reproduction on master was
negative; this test is the permanent guard so the restore path cannot silently
lose full output / expand / diff again.
"""
import json
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

// Minimal DOM stub. buildToolCard does createElement('div') then assigns
// row.innerHTML = `...` as a string; we capture that string and assert on it.
function makeEl() {
  return {
    _class: '', innerHTML: '', _attrs: {},
    get className(){return this._class;}, set className(v){this._class=v;},
    dataset: {},
    setAttribute(k,v){this._attrs[k]=String(v);},
    getAttribute(k){return this._attrs[k];},
    removeAttribute(k){delete this._attrs[k];},
    appendChild(){}, querySelector(){return null;}, closest(){return null;},
  };
}
global.document = { createElement: () => makeEl() };
global.window = {};

// Escaping + trivial label/icon helpers that buildToolCard references but which
// are orthogonal to the output/diff behaviour under test.
global.esc = (s)=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
global.li = (n)=>'<i data-lucide="'+n+'"></i>';
global.toolIcon = ()=>'<i></i>';
global._toolActionLabelText = (tc)=>String(tc&&tc.name||'tool');
global._toolDisplayName = (tc)=>String(tc&&tc.name||'tool');
global._toolDisclosureIdentity = ()=>'';
global._toolCardPreviewText = ()=>'';
global._formatToolArgPreview = ()=>'';
global._toolDetailLeadText = ()=>'';
global._toolDetailLeadLabel = ()=>'Shell';
global._isMemorySave = ()=>false;
global._isSkillUpdate = ()=>false;

// Real functions under test (extracted from the live source):
for (const fn of ['_snippetLooksLikeDiff','_colorDiffLines','_toolActionKind',
                  '_toolCardAllowsDetail','buildToolCard','_anchorSceneToolCallFromRow']) {
  eval(extractFunc(fn));
}

const mode = process.argv[3];
const payload = JSON.parse(process.argv[4]);
let html;
if (mode === 'direct') {
  // Build a card directly from a live tool-call object.
  html = buildToolCard(payload).innerHTML;
} else if (mode === 'restored') {
  // Simulate the reload/settled path: reconstruct the tool call from a persisted
  // activity_scene_v1 row, then build the card from THAT.
  const tc = _anchorSceneToolCallFromRow(payload, {settled:true});
  const card = buildToolCard(tc);
  html = card.innerHTML;
  process.stdout.write(JSON.stringify({
    html,
    snippet_len: (tc.snippet||'').length,
  }));
  return;
} else {
  throw new Error('unknown mode ' + mode);
}
process.stdout.write(JSON.stringify({ html, snippet_len: (payload.snippet||'').length }));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("tool_card_restore_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _run(driver_path, mode, payload):
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH), mode, json.dumps(payload)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


# A terminal output well over buildToolCard's 800-char display cap.
LONG_SHELL_OUTPUT = "result line {}\n".format("x" * 20) * 120  # ~> 800 chars
DIFF_SNIPPET = "@@ -1,3 +1,4 @@\n context\n-old line\n+new line\n+added line\n"


class TestRestoredToolCardKeepsFullOutputAndExpand:
    """#4622 symptom 1: restored terminal cards must keep full output + expand."""

    def test_long_output_direct_card_has_expand_with_full_snippet(self, driver_path):
        tc = {"name": "terminal", "args": {"command": "ls -R /"},
              "snippet": LONG_SHELL_OUTPUT, "done": True}
        out = _run(driver_path, "direct", tc)
        assert "tool-card-more" in out["html"], (
            "a terminal output over the display cap must render a Show-more expand button"
        )
        # The inline _toggleToolDiff handler reveals data-full; it must be
        # present (it carries the full output, not the truncated preview).
        assert "data-full=" in out["html"]

    def test_restored_scene_terminal_card_stays_expandable(self, driver_path):
        """The reload/settled path reconstructs the card from a persisted scene
        row; it must still be expandable with the full output recovered."""
        scene_row = {
            "role": "tool", "status": "completed",
            "tool": {"name": "terminal", "args": {"command": "ls -R /"}},
            "payload": {"output": LONG_SHELL_OUTPUT},
        }
        out = _run(driver_path, "restored", scene_row)
        assert out["snippet_len"] >= len(LONG_SHELL_OUTPUT) - 4, (
            "restored tool call must recover the FULL output snippet from the "
            f"persisted scene row (got {out['snippet_len']} vs {len(LONG_SHELL_OUTPUT)})"
        )
        assert "tool-card-more" in out["html"], (
            "restored terminal card must still render the Show-more expand control"
        )


class TestRestoredPatchCardRendersDiff:
    """#4622 symptom 2: restored patch/edit cards must render the diff body."""

    def test_direct_patch_card_renders_diff_block(self, driver_path):
        tc = {"name": "patch", "args": {"path": "foo.py"},
              "snippet": DIFF_SNIPPET, "is_diff": True, "done": True}
        out = _run(driver_path, "direct", tc)
        assert "diff-block" in out["html"]

    def test_restored_scene_patch_card_renders_diff_block(self, driver_path):
        scene_row = {
            "role": "tool", "status": "completed",
            "tool": {"name": "patch", "args": {"path": "foo.py"}},
            "payload": {"output": DIFF_SNIPPET},
        }
        out = _run(driver_path, "restored", scene_row)
        assert out["snippet_len"] >= len(DIFF_SNIPPET) - 4, (
            "restored patch tool call must recover the full diff snippet"
        )
        assert "diff-block" in out["html"], (
            "restored patch card must render the diff body (diff-block) from the "
            "persisted scene snippet"
        )
