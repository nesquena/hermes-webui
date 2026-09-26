"""Execute the settings operation owner with deferred network responses."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_vision_operations_fail_closed_and_serialize():
    if not shutil.which("node"):
        pytest.skip("node required")
    source = (ROOT / "static/panels.js").read_text()
    source = source[source.index("function _invalidateVisionCapabilityFirst(){"):source.index("async function _loadAuxiliaryModels(){")]
    script = r"""
const assert=require('node:assert/strict');
const S={activeProfile:'A'};
const cb={checked:false,disabled:true,addEventListener(_,fn){this.change=fn;}};
const $=()=>cb, t=k=>k, toasts=[], showToast=x=>toasts.push(x);
const requests=[];
function api(url,opts){return new Promise((resolve,reject)=>requests.push({opts,resolve,reject}));}
""" + source + r"""
(async()=>{
 let load=_loadVisionCapabilityFirst();
 assert.equal(cb.disabled,true);
 requests[0].reject(Error('offline')); await load;
 assert.equal(cb.disabled,true); assert.equal(cb.checked,false);
 assert.equal(toasts.at(-1),'settings_vcf_load_failed');
 load=_loadVisionCapabilityFirst();
 requests[1].resolve({vision_capability_first:true}); await load;
 assert.equal(cb.checked,true); assert.equal(cb.disabled,false);
 cb.checked=false; cb.change();
 assert.equal(cb.disabled,true);
 const save=cb._vcfPending;
 cb.checked=true; cb.change(); // synthetic change cannot bypass disabled control
 const reopen=_loadVisionCapabilityFirst();
 assert.equal(requests.length,3); // no overlapping GET or POST
 assert.deepEqual(JSON.parse(requests[2].opts.body),{enabled:false});
 requests[2].reject(Error('offline')); await save; await reopen;
 assert.equal(cb.checked,true); assert.equal(cb.disabled,false);
 cb.checked=false; cb.change();
 requests[3].resolve({vision_capability_first:false}); await cb._vcfPending;
 assert.equal(cb.checked,false); assert.equal(cb.disabled,false);
 load=_loadVisionCapabilityFirst();
 requests[4].resolve({vision_capability_first:'yes'}); await load;
 assert.equal(cb.disabled,true);
 // A pending read must not be reused when Settings reopens under B.
 load=_loadVisionCapabilityFirst(); // A retry remains pending
 const staleA=load;
 S.activeProfile='B'; _invalidateVisionCapabilityFirst();
 const loadB=_loadVisionCapabilityFirst();
 assert.equal(requests.length,7);
 requests[5].resolve({vision_capability_first:false}); await staleA;
 assert.equal(cb.disabled,true); assert.equal(cb._vcfProfile,'B');
 requests[6].resolve({vision_capability_first:true}); await loadB;
 assert.equal(cb.checked,true); assert.equal(cb.disabled,false);
 // A late B save, including failure, cannot write the newly active A UI.
 cb.checked=false; cb.change();
 const saveB=cb._vcfPending;
 S.activeProfile='A'; _invalidateVisionCapabilityFirst();
 const loadA=_loadVisionCapabilityFirst();
 requests[7].reject(Error('offline')); await saveB;
 assert.equal(cb._vcfProfile,'A'); assert.equal(cb.disabled,true);
 requests[8].resolve({vision_capability_first:false}); await loadA;
 assert.equal(cb.checked,false); assert.equal(cb.disabled,false);
 // A -> B -> A with the original A read unresolved must still reject it.
 _invalidateVisionCapabilityFirst();
 const oldA=_loadVisionCapabilityFirst();
 S.activeProfile='B'; _invalidateVisionCapabilityFirst();
 S.activeProfile='A'; _invalidateVisionCapabilityFirst();
 const freshA=_loadVisionCapabilityFirst();
 requests[9].resolve({vision_capability_first:true}); await oldA;
 assert.equal(cb.disabled,true);
 requests[10].resolve({vision_capability_first:false}); await freshA;
 assert.equal(cb.checked,false); assert.equal(cb.disabled,false);
 console.log('PASS: failed GET/retry, serialized saves/reopen, rollback, malformed load, profile races');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
