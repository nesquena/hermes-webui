"""Regression coverage for approval-command retry identity (#7629).

The WebUI dispatches a canonical command string, while failure recovery must
also retain the raw composer text so it can restore what the user typed. These
scenarios execute the real parser, /skills handler, command transport, and
retry/stash helpers against a fake idempotent server.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = ROOT.joinpath("static", "commands.js").read_text(encoding="utf-8")
SESSIONS_JS = ROOT.joinpath("static", "sessions.js").read_text(encoding="utf-8")


def _block(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    finish = source.index(end, begin)
    return source[begin:finish]


def _run_scenario(typed_text: str, *, restore_via_session: bool = False) -> dict:
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")

    parse_command = _block(COMMANDS_JS, "function parseCommand(text){", "\nconst DESKTOP_COMPANION_EXTENSION_ID")
    transport = _block(
        COMMANDS_JS,
        "async function _runAgentCommandTransport(text,_meta){",
        "\nasync function resolveBundleCommand",
    )
    cmd_skills = _block(COMMANDS_JS, "function cmdSkills(args){", "\nasync function cmdUse")
    profile_matcher = _block(
        SESSIONS_JS,
        "function _profileMatchesActiveProfile(profile, activeProfile){",
        "function _sessionEventProfilesMatch",
    )
    retry_helpers = _block(
        SESSIONS_JS,
        "const _APPROVAL_TRANSPORT_FAILURE_KEY=",
        "function _restoreApprovalCommandDraft",
    )

    harness = textwrap.dedent(
        """
        %(parse_command)s
        %(profile_matcher)s
        %(retry_helpers)s

        const storage=new Map();
        const sessionStorage={
          getItem:(key)=>storage.has(key)?storage.get(key):null,
          setItem:(key,value)=>storage.set(key,String(value)),
          removeItem:(key)=>storage.delete(key),
        };
        const S={
          session:{session_id:'sid-A'},
          activeProfile:'default',
          activeProfileIsDefault:true,
          pendingFiles:[],
          messages:[],
        };
        const composer={value:''};
        const $=()=>composer;
        const calls=[];
        const executions=[];
        const seenCommandIds=new Set();
        let firstResponseLost=true;

        // The server performs the side effect before the response is lost. A
        // repeated command_id is deduplicated exactly as the real server does.
        const api=async(_path,opts)=>{
          const body=JSON.parse(opts.body);
          calls.push(body);
          if(!seenCommandIds.has(body.command_id)){
            seenCommandIds.add(body.command_id);
            executions.push(body.command_id);
          }
          if(firstResponseLost){
            firstResponseLost=false;
            throw new Error('response lost after server execution');
          }
          return {output:'approved',command_id:body.command_id};
        };
        const _clearComposerDraft=()=>Promise.resolve(true);
        const _saveComposerDraftNow=async()=>true;
        const _restoreApprovalCommandDraft=(profile,sid,text,files)=>{
          if(S.session.session_id===sid&&!composer.value) composer.value=String(text||'');
        };
        const _skillsResponseOwnerStillValid=()=>true;
        const _steerOwnerIsCurrent=()=>true;
        const showToast=()=>{};
        const renderMessages=()=>{};
        const autoResize=()=>{};
        const renderTray=()=>{};
        const hideCmdDropdown=()=>{};
        const SKILLS_AGENT_SUBCOMMANDS=['pending','approve','apply','reject','deny','drop','diff','approval','mode'];
        %(transport)s
        %(cmd_skills)s

        async function runTyped(text){
          composer.value=text;
          const parsed=parseCommand(text);
          if(!parsed || parsed.name!=='skills') throw new Error('parse failed');
          if(cmdSkills(parsed.args)!==true) throw new Error('cmdSkills did not claim command');
          await new Promise((resolve)=>setTimeout(resolve,10));
        }

        (async()=>{
          await runTyped(%(typed_text)s);
          const firstFailureState={
            calls:calls.slice(),
            executions:executions.slice(),
            failures:_readApprovalTransportFailures(),
            composer:composer.value,
          };
          %(restore_step)s
          await runTyped(composer.value);
          console.log(JSON.stringify({
            firstFailureState,
            calls,
            executions,
            failures:_readApprovalTransportFailures(),
            retries:_readApprovalCommandRetries(),
            restoreResult:typeof restoreResult==='undefined'?null:restoreResult,
            restoredComposer:composer.value,
            commandIds:calls.map((call)=>call.command_id),
          }));
        })().catch((error)=>{
          console.error(error&&error.stack||error);
          process.exit(1);
        });
        """
    ) % {
        "parse_command": parse_command,
        "profile_matcher": profile_matcher,
        "retry_helpers": retry_helpers,
        "transport": transport,
        "cmd_skills": cmd_skills,
        "typed_text": json.dumps(typed_text),
        "restore_step": (
            "composer.value='';\n"
            "const restoreResult=await _restoreApprovalTransportFailureForSession({session_id:'sid-A'});\n"
        )
        if restore_via_session
        else "",
    }

    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"node harness failed: {result.stderr}\n{result.stdout}"
    return json.loads(result.stdout.strip())


def _assert_single_execution(out: dict) -> None:
    assert len(out["commandIds"]) == 2, out
    assert len(set(out["commandIds"])) == 1, out
    assert len(out["executions"]) == 1, out
    assert out["failures"] == [], out


def test_double_space_approval_retry_reuses_one_command_id():
    out = _run_scenario("/skills  approve abc")
    _assert_single_execution(out)


def test_uppercase_approval_retry_reuses_one_command_id():
    out = _run_scenario("/SKILLS approve abc")
    _assert_single_execution(out)


def test_trailing_space_approval_retry_reuses_one_command_id():
    out = _run_scenario("/skills approve abc ")
    _assert_single_execution(out)


def test_restored_response_loss_reuses_one_command_id_after_session_restore():
    out = _run_scenario("/skills  approve restored", restore_via_session=True)
    _assert_single_execution(out)
    assert out["retries"] == [], out
