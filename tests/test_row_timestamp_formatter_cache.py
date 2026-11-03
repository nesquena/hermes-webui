"""Cached timestamp formatters retain Date locale-method output and edge cases."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _function(path, name):
    source = (ROOT / path).read_text()
    return re.search(r"function " + re.escape(name) + r"\([^\n]*\)[\s\S]*?\n}", source).group()


def _functions():
    return "\n".join([
        _function("static/ui.js", "_intlFormatter"),
        _function("static/ui.js", "_activityClockLabel"),
        _function("static/ui.js", "_activityFullClockLabel"),
        _function("static/sessions.js", "_formatInServerTz"),
    ])


def _run(script, env=None):
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    result = subprocess.run(["node", "-e", script.replace("FUNCTIONS", _functions())],
                            capture_output=True, text=True, env={**os.environ, **(env or {})})
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("locale,tz", [("en_US.UTF-8", "UTC"), ("en_GB.UTF-8", "America/Los_Angeles"), ("fr_FR.UTF-8", "Asia/Kathmandu")])
def test_timestamp_formatters_match_native_date_methods(locale, tz):
    script = r"""
const assert=require('node:assert/strict');
const _activityNowSeconds=()=>1700000000;
let _serverTz;
FUNCTIONS
for(const ts of [undefined,null,'',0,-1,NaN,Infinity,'bad',1700000000,1700000000.125,8.64e12,8.64e12+1,1e20]){
  const stamp=Number(ts||_activityNowSeconds());
  const clock=Number.isFinite(stamp)&&stamp>0?new Date(stamp*1000).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'}):'';
  const fullStamp=Number(ts);
  const full=Number.isFinite(fullStamp)&&fullStamp>0&&fullStamp<=8.64e12?new Date(fullStamp*1000).toLocaleString([], {year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'';
  assert.equal(_activityClockLabel(ts),clock);
  assert.equal(_activityFullClockLabel(ts),full);
}
const opts=[undefined,{},null,{hour:'2-digit',minute:'2-digit'},{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'},{year:'numeric',month:'long',day:'2-digit'},{timeZone:'Asia/Kathmandu'},{timeStyle:'short'},{dateStyle:'full'},{weekday:'long'},{fractionalSecondDigits:3},{dayPeriod:'short'},{hour12:false},{hour:undefined},{hour:'numeric',minute:'numeric',timeZone:'invalid'},{hour:'invalid'},Object.create({hour:'numeric'}),Object.defineProperty({},'hour',{value:'2-digit'}),Object.defineProperty({},'hour',{value:'invalid'})];
const capture=fn=>{try{return ['value',fn()];}catch(e){return ['error',e.name];}};
for(_serverTz of [undefined,'','+0000','-0000','bad','+0800','+0530','+0545','-0330']){
  for(const ms of [0,1700000000125,Date.parse('2026-03-08T09:59:59Z'),Date.parse('2026-03-08T10:00:00Z'),8.64e15,-8.64e15,NaN]){
    for(const options of opts){
      const date=new Date(ms);
      const expected=()=>{
        if(!_serverTz||_serverTz==='+0000'||_serverTz==='-0000')return date.toLocaleString(undefined,options);
        const m=_serverTz.match(/^([+-])(\d{2})(\d{2})$/);
        if(!m)return date.toLocaleString(undefined,options);
        const offsetMin=(m[1]==='+'?1:-1)*(parseInt(m[2])*60+parseInt(m[3]));
        return new Date(date.getTime()+offsetMin*60*1000).toLocaleString(undefined,{...options,timeZone:'UTC'});
      };
      assert.deepEqual(capture(()=>_formatInServerTz(date,options)),capture(expected),JSON.stringify({_serverTz,ms,options}));
    }
  }
}
"""
    _run(script, {"LANG": locale, "LC_ALL": locale, "TZ": tz})


def test_timestamp_formatters_reuse_intl_instances():
    script = r"""
const assert=require('node:assert/strict');
const _activityNowSeconds=()=>1700000000;
let _serverTz='+0530';
FUNCTIONS
let constructed=0, probes=0;
const Native=Intl.DateTimeFormat;
// Formatter builds (with options) and the argument-less default locale/zone
// probe that guards the cache are counted separately: the probe must run once
// per task, not once per row.
Intl.DateTimeFormat=new Proxy(Native,{construct(target,args){if(args.length)constructed++;else probes++;return Reflect.construct(target,args);}});
(async()=>{
for(let i=0;i<100;i++){
  _activityClockLabel(1700000000+i);
  _activityFullClockLabel(1700000000+i);
  _formatInServerTz(new Date((1700000000+i)*1000),{hour:'2-digit',minute:'2-digit'});
  _formatInServerTz(new Date((1700000000+i)*1000),{});
}
assert.equal(constructed,4);
assert.equal(probes,1);
// Server offset changes shift the Date anew but reuse the identical UTC format.
_serverTz='-0330';
_formatInServerTz(new Date(1700000000000),{hour:'2-digit',minute:'2-digit'});
assert.equal(constructed,4);
// The browser-zone fallback must not reuse the server-zone formatter.
_serverTz='+0000';
_formatInServerTz(new Date(1700000000000),{hour:'2-digit',minute:'2-digit'});
assert.equal(constructed,5);
assert.equal(probes,1);
// The next task checks the default locale/zone again, once.
await Promise.resolve();
for(let i=0;i<100;i++) _activityClockLabel(1700000000+i);
assert.equal(probes,2);
assert.equal(constructed,5);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    _run(script)


def test_timestamp_formatters_follow_runtime_time_zone_change():
    # The old per-call Date locale methods picked up an OS time-zone change
    # without a reload; the cached formatters must too.
    script = r"""
const assert=require('node:assert/strict');
const _activityNowSeconds=()=>1700000000;
let _serverTz='+0000';
FUNCTIONS
const ts=1700000000, d=new Date(ts*1000), hm={hour:'2-digit',minute:'2-digit'};
const native=()=>({
  clock:d.toLocaleTimeString([], {hour:'numeric',minute:'2-digit'}),
  full:d.toLocaleString([], {year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}),
  footer:d.toLocaleString(undefined,hm),
});
const helpers=()=>({clock:_activityClockLabel(ts),full:_activityFullClockLabel(ts),footer:_formatInServerTz(d,hm)});
(async()=>{
  process.env.TZ='UTC';
  await Promise.resolve();
  const utc=native();
  assert.deepEqual(helpers(),utc);
  process.env.TZ='America/Los_Angeles';
  await Promise.resolve();
  const la=native();
  assert.notDeepEqual(la,utc);
  assert.deepEqual(helpers(),la);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    _run(script)
