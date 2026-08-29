"""Regression for #6195 / #6199: a bare model id that collides with the profile
default across multiple provider groups must NOT silently snap to the default
group's option when no provider hint is available.

`_findModelInDropdown('glm-5.2', sel, '')` previously returned the FIRST
normalized match (`z-ai/glm-5.2`, the default group), so a deliberate non-default
pick reverted to the default provider on re-render when the provider hint was
momentarily unavailable. The Stage-2 guard now returns null in that ambiguous
case so the caller injects a provider-scoped option instead of asserting the
wrong provider.

Invariants this locks in (proven against origin/master vs the fix):
  - ambiguous bare id + EMPTY hint  -> null   (was: default-group snap)  [FIXED]
  - ambiguous bare id + correct hint -> the hinted provider option        [unchanged]
  - uniquely-named bare id           -> its single match                  [unchanged]
  - bare id that IS an exact option value -> returned at Stage 0          [unchanged]
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_DRIVER = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[1], 'utf8');

function extractFunction(source, name){
  const marker='function '+name+'(';
  const start=source.indexOf(marker);
  if(start<0) throw new Error('not found: '+name);
  const brace=source.indexOf('{', source.indexOf(')', start));
  let d=0;
  for(let i=brace;i<source.length;i++){
    if(source[i]==='{') d++;
    else if(source[i]==='}'){ d--; if(d===0) return source.slice(start,i+1); }
  }
  throw new Error('unterminated: '+name);
}

eval([
  '_getOptionProviderId',
  '_providerFromModelValue',
  '_findModelInDropdown',
].map(name => extractFunction(uiSrc, name)).join('\n'));

function makeOpt(value, provider){
  return { value, textContent:value, dataset:{}, parentElement:{tagName:'OPTGROUP', dataset:{provider}} };
}
function makeSelect(options){
  let idx = 0;
  return {
    id:'modelSelect', options,
    querySelectorAll(){ return []; },
    get selectedOptions(){ return idx>=0?[options[idx]]:[]; },
    get value(){ return idx>=0?options[idx].value:''; },
    set value(v){ idx = options.findIndex(o=>o.value===v); },
  };
}

const AMB = () => [ makeOpt('z-ai/glm-5.2','z-ai'), makeOpt('@custom:tokenrouter:z-ai/glm-5.2','custom:tokenrouter') ];

const out = {
  // #6195 repro: ambiguous bare id, no hint -> must be null (not a default-group snap)
  ambiguousEmptyHint: _findModelInDropdown('glm-5.2', makeSelect(AMB()), ''),
  // with the correct hint -> resolves to the hinted provider (unchanged)
  ambiguousWithHint: _findModelInDropdown('glm-5.2', makeSelect(AMB()), 'custom:tokenrouter'),
  // uniquely-named bare id still matches (unchanged)
  uniqueBare: _findModelInDropdown('glm-5.2-free', makeSelect([makeOpt('z-ai/glm-5.2-free','z-ai')]), ''),
  // bare id that IS an exact option value -> returned at Stage 0 even when ambiguous (unchanged)
  bareExactAmbiguous: _findModelInDropdown('glm-5.2', makeSelect([makeOpt('glm-5.2','z-ai'), makeOpt('@custom:tokenrouter:glm-5.2','custom:tokenrouter')]), ''),
};
process.stdout.write(JSON.stringify(out));
"""


def _run():
    assert NODE is not None
    result = subprocess.run([NODE, "-e", _DRIVER, str(UI_JS)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_ambiguous_bare_id_no_hint_does_not_snap_to_default():
    # THE fix: no wrong-provider revert. null => caller injects a scoped option.
    assert _run()["ambiguousEmptyHint"] is None


def test_ambiguous_bare_id_with_hint_resolves_to_hinted_provider():
    assert _run()["ambiguousWithHint"] == "@custom:tokenrouter:z-ai/glm-5.2"


def test_unique_bare_id_still_matches():
    assert _run()["uniqueBare"] == "z-ai/glm-5.2-free"


def test_bare_id_that_is_exact_option_value_returns_at_stage0():
    assert _run()["bareExactAmbiguous"] == "glm-5.2"
