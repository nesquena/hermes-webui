"""Frontend regressions for manual title-regeneration timeout sizing."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _title_regenerate_action_block() -> str:
    marker = "t('session_title_regenerate')"
    start = SESSIONS_JS.find(marker)
    assert start >= 0, "manual title regenerate action not found"
    end = SESSIONS_JS.find("if(!isExternalSession){", start)
    assert end > start, "manual title regenerate action end marker not found"
    return SESSIONS_JS[start:end]


def _title_timeout_support_block() -> str:
    start = SESSIONS_JS.find("function _manualTitleAuxConfigFromPayload")
    assert start >= 0, "manual title auxiliary config support not found"
    end = SESSIONS_JS.find("function _formatSessionModelWithGateway", start)
    assert end > start, "manual title auxiliary config support end marker not found"
    return SESSIONS_JS[start:end]


def _run_script(script: str):
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def _run_helper(setup: str = ""):
    helper = _title_timeout_support_block()
    script = f"""
    (async()=>{{
      {setup}
      {helper}
      const result=await _manualTitleRegenerateTimeoutMs();
      process.stdout.write(JSON.stringify(result));
    }})().catch(err=>{{
      console.error(err&&err.stack?err.stack:err);
      process.exit(1);
    }});
    """
    return _run_script(script)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_manual_title_regenerate_timeout_uses_fetched_positive_aux_timeout():
    assert _run_helper("async function api(){return {title_generation:{timeout:180}};}") == 185000
    assert _run_helper("async function api(){return {title_generation:{timeout:'31'}};}") == 36000


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_manual_title_regenerate_timeout_falls_back_when_fetch_missing_or_invalid():
    assert _run_helper("async function api(){throw new Error('missing mock should fall back');}") is None
    for value in ("undefined", "null", "''", "'abc'", "0", "-4", "Infinity", "NaN"):
        setup = f"async function api(){{return {{title_generation:{{timeout:{value}}}}};}}"
        assert _run_helper(setup) is None


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_manual_title_regenerate_timeout_adds_slack_and_enforces_minimum():
    assert _run_helper("async function api(){return {title_generation:{timeout:10}};}") == 30000
    assert _run_helper("async function api(){return {title_generation:{timeout:25.1}};}") == 30100


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_manual_title_regenerate_timeout_fetches_aux_config_fresh_each_time():
    setup = """
    let calls=0;
    async function api(path,opts){
      calls++;
      if(path!=='/api/model/auxiliary') throw new Error('wrong path '+path);
      if(!opts||opts.retries!==0||opts.timeoutToast!==false) throw new Error('wrong fetch options');
      return {tasks:[{task:'title_generation',timeout:180}]};
    }
    """
    helper = _title_timeout_support_block()
    script = f"""
    (async()=>{{
      {setup}
      {helper}
      const first=await _manualTitleRegenerateTimeoutMs();
      const second=await _manualTitleRegenerateTimeoutMs();
      process.stdout.write(JSON.stringify({{first,second,calls}}));
    }})().catch(err=>{{
      console.error(err&&err.stack?err.stack:err);
      process.exit(1);
    }});
    """
    assert _run_script(script) == {"first": 185000, "second": 185000, "calls": 2}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_manual_title_regenerate_timeout_empty_tasks_normalizes_to_null():
    setup = """
    let calls=0;
    async function api(){
      calls++;
      return {tasks:[]};
    }
    """
    helper = _title_timeout_support_block()
    script = f"""
    (async()=>{{
      {setup}
      {helper}
      const normalized=_manualTitleAuxConfigFromPayload({{tasks:[]}});
      const first=await _loadManualTitleAuxConfig();
      const second=await _loadManualTitleAuxConfig();
      process.stdout.write(JSON.stringify({{
        normalized,
        first,
        second,
        calls
      }}));
    }})().catch(err=>{{
      console.error(err&&err.stack?err.stack:err);
      process.exit(1);
    }});
    """
    assert _run_script(script) == {
        "normalized": None,
        "first": None,
        "second": None,
        "calls": 2,
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_manual_title_regenerate_timeout_fetch_failure_falls_back_then_retries():
    setup = """
    let calls=0;
    async function api(){
      calls++;
      if(calls===1) throw new Error('network down');
      return {tasks:[{task:'title_generation',timeout:180}]};
    }
    """
    helper = _title_timeout_support_block()
    script = f"""
    (async()=>{{
      {setup}
      {helper}
      const first=await _manualTitleRegenerateTimeoutMs();
      const second=await _manualTitleRegenerateTimeoutMs();
      process.stdout.write(JSON.stringify({{first,second,calls}}));
    }})().catch(err=>{{
      console.error(err&&err.stack?err.stack:err);
      process.exit(1);
    }});
    """
    assert _run_script(script) == {"first": None, "second": 185000, "calls": 2}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_manual_title_regenerate_timeout_uses_each_fetched_value_after_profile_switch():
    setup = """
    let calls=0;
    async function api(path,opts){
      calls++;
      if(path!=='/api/model/auxiliary') throw new Error('wrong path '+path);
      if(!opts||opts.retries!==0||opts.timeoutToast!==false) throw new Error('wrong fetch options');
      if(calls===1) return {tasks:[{task:'title_generation',timeout:180}]};
      return {tasks:[{task:'title_generation',timeout:31}]};
    }
    """
    helper = _title_timeout_support_block()
    script = f"""
    (async()=>{{
      {setup}
      {helper}
      const first=await _manualTitleRegenerateTimeoutMs();
      const second=await _manualTitleRegenerateTimeoutMs();
      process.stdout.write(JSON.stringify({{first,second,calls}}));
    }})().catch(err=>{{
      console.error(err&&err.stack?err.stack:err);
      process.exit(1);
    }});
    """
    assert _run_script(script) == {"first": 185000, "second": 36000, "calls": 2}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_manual_title_regenerate_timeout_invalid_fetched_timeout_falls_back_without_timeout():
    setup = """
    let calls=0;
    async function api(){
      calls++;
      return {tasks:[{task:'title_generation',timeout:'abc'}]};
    }
    """
    helper = _title_timeout_support_block()
    script = f"""
    (async()=>{{
      {setup}
      {helper}
      const first=await _manualTitleRegenerateTimeoutMs();
      const second=await _manualTitleRegenerateTimeoutMs();
      process.stdout.write(JSON.stringify({{first,second,calls}}));
    }})().catch(err=>{{
      console.error(err&&err.stack?err.stack:err);
      process.exit(1);
    }});
    """
    assert _run_script(script) == {"first": None, "second": None, "calls": 2}


def test_manual_title_regenerate_api_call_uses_computed_timeout_option():
    block = _title_regenerate_action_block()
    request_pos = block.find("const requestOpts={method:'POST',body:JSON.stringify({session_id:session.session_id})};")
    timeout_pos = block.find("const timeoutMs=await _manualTitleRegenerateTimeoutMs();")
    assign_pos = block.find("if(timeoutMs) requestOpts.timeoutMs=timeoutMs;")
    api_pos = block.find("api('/api/session/title/regenerate',requestOpts)")

    assert -1 not in (request_pos, timeout_pos, assign_pos, api_pos)
    assert request_pos < timeout_pos < assign_pos < api_pos
    assert "timeoutMs:0" not in block
