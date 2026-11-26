"""#8571 greptile P1 (2026-11-23): unique trailing interim text must not be folded.

``_anchorSceneProseDuplicatesFinalAnswer`` / ``_anchorSceneProseMatchesFinalAnswer``
accepted EITHER string being a prefix of the other when the shorter one is
>=90% as long. A long checklist followed by a short warning therefore matched a
final answer that repeats only the checklist, and the warning disappeared with
the segment (the rail filter drops the hidden row's only copy). Folding is only
safe when the final answer displays the interim's WHOLE text.

These drivers slice the REAL functions out of static/ui.js and run them in
node — never a reimplementation.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

EXTRACT_FN_JS = """
function extractFn(src, name){
  const start = src.indexOf('function ' + name);
  if(start === -1) throw new Error(name + ' not found');
  const paramsStart = src.indexOf('(', start);
  let depth = 0;
  let paramsEnd = -1;
  for(let i = paramsStart; i < src.length; i++){
    if(src[i] === '(') depth += 1;
    else if(src[i] === ')'){
      depth -= 1;
      if(depth === 0){ paramsEnd = i; break; }
    }
  }
  const braceStart = src.indexOf('{', paramsEnd);
  if(braceStart === -1) throw new Error(name + ' body not found');
  depth = 0;
  for(let i = braceStart; i < src.length; i++){
    if(src[i] === '{') depth += 1;
    else if(src[i] === '}'){
      depth -= 1;
      if(depth === 0){ return src.slice(start, i + 1); }
    }
  }
  throw new Error(name + ' body did not close');
}
"""


def _fold_predicate(prose: str, final_answer: str) -> dict:
    assert NODE is not None, "node is required for the interim-prose predicates"
    ui_src = json.dumps(str(ROOT / "static" / "ui.js"))
    script = EXTRACT_FN_JS + f"""
const fs = require('fs');
const src = fs.readFileSync({ui_src}, 'utf8');
eval(extractFn(src, '_anchorSceneProseMatchesFinalAnswer'));
eval(extractFn(src, '_anchorSceneProseDuplicatesFinalAnswer'));
const prose = {json.dumps(prose)};
const finalAnswer = {json.dumps(final_answer)};
console.log(JSON.stringify({{
  matches: _anchorSceneProseMatchesFinalAnswer(prose, finalAnswer),
  duplicates: _anchorSceneProseDuplicatesFinalAnswer(prose, finalAnswer),
}}));
"""
    env = dict(os.environ)
    env.pop("NODE_CHANNEL_FD", None)  # node SIGABRTs on exit when this leaks into the child
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, (out.stdout, out.stderr)
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node is required for the interim-prose predicates")
def test_unique_trailing_text_is_not_folded_into_a_shorter_final_answer():
    """The final answer repeats only the checklist; the warning exists nowhere
    else. Both predicates must keep the segment visible."""
    checklist = "\n".join(
        f"Step {i}: inspect the ledger segment and confirm the recorded counts before moving on."
        for i in range(1, 9)
    )
    warning = "Never run this on production."
    prose = checklist + "\n\n" + warning
    out = _fold_predicate(prose, checklist)
    assert out["duplicates"] is False, (
        "the trailing warning is not displayed by the final answer and must stay visible"
    )
    assert out["matches"] is False, (
        "the render-time matcher must apply the same whole-text rule"
    )


@pytest.mark.skipif(NODE is None, reason="node is required for the interim-prose predicates")
def test_near_equal_prefix_controls_keep_their_verdicts():
    """#2051 and #4568 controls: a near-equal prefix of the final answer still
    folds, a distinct short intermediate is still kept, and identical text
    still folds."""
    assert _fold_predicate(
        "Hey! What can I help you with today", "Hey! What can I help you with today?"
    )["duplicates"] is True
    assert _fold_predicate(
        "Hey! What can", "Hey! What can I help you with today?"
    )["duplicates"] is False
    checklist = "Step 1: inspect the ledger. Step 2: re-run the checks."
    assert _fold_predicate(checklist, checklist)["duplicates"] is True
