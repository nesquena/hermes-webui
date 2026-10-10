"""Executed lifecycle regressions for approval retry mutation generations (#7629).

These harnesses splice the production callers and the production retry/storage
helpers. Only HTTP and presentation adapters are deterministic test fixtures;
the generation capture, retirement, and sessionStorage writers are not replaced.
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
MESSAGES_JS = ROOT.joinpath("static", "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = ROOT.joinpath("static", "sessions.js").read_text(encoding="utf-8")
UI_JS = ROOT.joinpath("static", "ui.js").read_text(encoding="utf-8")


def _block(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    finish = source.index(end, begin)
    return source[begin:finish]


PROFILE_MATCHER = _block(
    SESSIONS_JS,
    "function _profileMatchesActiveProfile(profile, activeProfile){",
    "function _sessionEventProfilesMatch",
)
APPROVAL_HELPERS = _block(
    SESSIONS_JS,
    "const _APPROVAL_TRANSPORT_FAILURE_KEY=",
    "function _restoreApprovalCommandDraft",
)
COMMAND_RUNTIME = _block(
    COMMANDS_JS,
    "async function executeAgentCommand(text,_meta){",
    "\nasync function resolveBundleCommand",
)
SKILLS_SUBCOMMANDS = _block(
    COMMANDS_JS,
    "const SKILLS_AGENT_SUBCOMMANDS=",
    "\n\nfunction cmdSkills",
)
SKILLS_OWNER = _block(
    COMMANDS_JS,
    "function _steerOwnerIsCurrent(ownerSid){",
    "function _steerOwnerStreamIsCurrent",
)
SKILLS_COMMAND = _block(
    COMMANDS_JS,
    "function _skillsResponseOwnerStillValid(ownerSid, ownerProfile){",
    "\nasync function cmdUse",
)
AGENT_ALLOWLIST = _block(
    MESSAGES_JS,
    "const _AGENT_COMMANDS_RUN_ON_WEBUI",
    "\n\n",
)
GENERIC_CALLER = _block(
    MESSAGES_JS,
    "if(_parsedCmd.name==='sessions' || _parsedCmd.name==='resume'){",
    "if(_agentCmd&&_agentCmd.category==='Plugin'){",
)
PLUGIN_CALLER = _block(
    MESSAGES_JS,
    "if(_agentCmd&&_agentCmd.category==='Plugin'){",
    "if(_agentCmdName==='moa'){",
)
SUBMIT_EDIT = _block(
    UI_JS,
    "let _submitEditInFlight = false;",
    "\nasync function regenerateResponse",
)


COMMON = """
const storage = new Map();
const sessionStorage = {
  getItem: (key) => storage.has(key) ? storage.get(key) : null,
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: (key) => storage.delete(key),
};
const S = {
  session: {session_id:'sid-A'},
  activeProfile: 'default',
  activeProfileIsDefault: true,
  pendingFiles: [],
  messages: [],
};
const composer = {value:''};
const $ = () => composer;
let warnings = 0;
const showToast = () => { warnings++; };
const renderMessages = () => {};
const renderTray = () => {};
const autoResize = () => {};
const hideCmdDropdown = () => {};
const _composerDraftRevision = () => 0;
const _clearComposerDraft = () => Promise.resolve(true);
let _saveComposerDraftNow = async () => true;
let api;
%(profile_matcher)s
%(approval_helpers)s
%(command_runtime)s
"""


def _run_node(body: str) -> dict:
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    script = textwrap.dedent(COMMON % {
        "profile_matcher": PROFILE_MATCHER,
        "approval_helpers": APPROVAL_HELPERS,
        "command_runtime": COMMAND_RUNTIME,
    }) + "\n(async()=>{\n" + textwrap.dedent(body) + "\n})().catch((error)=>{\n  console.error(error&&error.stack||error);\n  process.exit(1);\n});\n"
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}\n{proc.stdout}"
    return json.loads(proc.stdout.strip())


def _caller_body(caller: str, retirement: str) -> str:
    if caller == "memory":
        caller_setup = """
        %(allowlist)s
        const getAgentCommandMetadata = async () => ({name:'memory'});
        const renderSessionList = async () => {};
        const newSession = async () => { S.session={session_id:'sid-NEW'}; S.messages=[]; };
        const rawText='/MEMORY  pending   ';
        composer.value=rawText;
        const _rawComposerText=rawText;
        const text=rawText.trim();
        const _parsedCmd={name:'memory',args:'pending'};
        async function runCaller(){
          %(generic)s
          return 'fell-through';
        }
        const done=runCaller();
        """ % {"allowlist": AGENT_ALLOWLIST, "generic": GENERIC_CALLER}
    elif caller == "skills":
        caller_setup = """
        %(subcommands)s
        %(skills_owner)s
        %(skills_command)s
        composer.value='/skills approve abc123';
        const done=(async()=>cmdSkills('approve abc123'))();
        """ % {
            "subcommands": SKILLS_SUBCOMMANDS,
            "skills_owner": SKILLS_OWNER,
            "skills_command": SKILLS_COMMAND,
        }
    elif caller == "plugin":
        caller_setup = """
        const text='/plugin run';
        const _agentCmd={name:'plugin',category:'Plugin'};
        const _agentCmdName='plugin';
        const _cmdOwner={sid:'sid-A',profile:'default'};
        const _cmdOwnerIsCurrent=()=>((S.session&&S.session.session_id)||null)===_cmdOwner.sid
          &&(S.activeProfile||'default')===_cmdOwner.profile;
        const _cmdMutationGeneration=0;
        const _cmdMutationGenerationIsCurrent=()=>_approvalCommandMutationGeneration(
          _cmdOwner.profile,_cmdOwner.sid
        )===_cmdMutationGeneration;
        const _cmdLifecycleIsCurrent=()=>_cmdOwnerIsCurrent()&&_cmdMutationGenerationIsCurrent();
        async function runCaller(){
          %(plugin)s
          return 'fell-through';
        }
        const done=runCaller();
        """ % {"plugin": PLUGIN_CALLER}
    else:  # pragma: no cover
        raise AssertionError(caller)

    return textwrap.dedent(
        """
        let rejectApi;
        api = async () => new Promise((_resolve, reject) => { rejectApi = reject; });
        %(caller_setup)s
        await new Promise((resolve) => setTimeout(resolve, 5));
        %(retirement)s
        rejectApi(new Error('offline'));
        await done;
        await new Promise((resolve) => setTimeout(resolve, 10));
        console.log(JSON.stringify({
          caller:%(caller_json)s,
          retirement:%(retirement_json)s,
          sid:S.session&&S.session.session_id,
          composer:composer.value,
          warnings,
          failures:_readApprovalTransportFailures(),
          retries:_readApprovalCommandRetries(),
        }));
        """
    ) % {
        "caller_setup": caller_setup,
        "retirement": retirement,
        "caller_json": json.dumps(caller),
        "retirement_json": json.dumps("clear" if retirement else "switch"),
    }


@pytest.mark.parametrize("caller", ["memory", "skills", "plugin"])
def test_late_transport_failure_after_retirement_cannot_republish(caller: str):
    """Clear/truncate retirement fences all three real transport callers."""
    out = _run_node(_caller_body(
        caller,
        "composer.value=''; _clearApprovalCommandStateForSession('default','sid-A');",
    ))
    assert out["sid"] == "sid-A"
    assert out["failures"] == []
    assert out["retries"] == []
    assert out["composer"] == ""
    assert out["warnings"] == 1


@pytest.mark.parametrize("caller", ["memory", "skills", "plugin"])
def test_switch_away_keeps_origin_failure_for_real_callers(caller: str):
    """Navigation alone is not retirement: the originating failure remains recoverable."""
    out = _run_node(_caller_body(
        caller,
        "S.session={session_id:'sid-B'}; composer.value='draft typed in B';",
    ))
    assert out["sid"] == "sid-B"
    assert out["composer"] == "draft typed in B"
    assert len(out["failures"]) == 1
    assert out["failures"][0]["sid"] == "sid-A"
    assert out["failures"][0]["command_id"]
    assert out["retries"] == []
    assert out["warnings"] == 1


def test_generic_memory_failure_preserves_raw_draft_and_canonical_command():
    """Generic /memory recovery keeps raw composer text but canonical identity."""
    out = _run_node(_caller_body("memory", ""))
    assert len(out["failures"]) == 1
    assert out["failures"][0]["text"] == "/MEMORY  pending   "
    assert out["failures"][0]["command"] == "/MEMORY  pending"
    assert out["failures"][0]["command_id"]
    assert out["retries"] == []


def test_success_with_failed_draft_clear_cannot_recreate_retry_after_retirement():
    """A successful command's late failed draft-clear must not resurrect its retry ID."""
    out = _run_node(
        """
        api = async () => ({output:'saved',command_id:'old-command'});
        let releaseDraftClear;
        const draftClearPromise = new Promise((resolve) => { releaseDraftClear=resolve; });
        const captured = _approvalCommandMutationGeneration('default','sid-A');
        const done = executeAgentCommand('/memory pending', {
          draftClearPromise,
          ownerMutationGeneration:captured,
        });
        await new Promise((resolve) => setTimeout(resolve, 0));
        _clearApprovalCommandStateForSession('default','sid-A');
        releaseDraftClear(false);
        const result = await done;
        console.log(JSON.stringify({
          result,
          failures:_readApprovalTransportFailures(),
          retries:_readApprovalCommandRetries(),
          retryId:_approvalCommandRetryId('default','sid-A','/memory pending'),
        }));
        """
    )
    assert out["result"]["command_id"] == "old-command"
    assert out["failures"] == []
    assert out["retries"] == []
    assert out["retryId"] is None


def test_recovery_save_after_retirement_keeps_composer_and_allows_fresh_identity():
    """Recovery must fence both visible/save completion and the next deliberate action."""
    out = _run_node(
        """
        let releaseSave;
        _saveComposerDraftNow = () => new Promise((resolve) => { releaseSave=resolve; });
        _stashApprovalTransportFailure(
          'default','sid-A','/memory old',[],'old-command','/memory old',0
        );
        const restoring = _restoreApprovalTransportFailureForSession({session_id:'sid-A'});
        await new Promise((resolve) => setTimeout(resolve, 0));
        _clearApprovalCommandStateForSession('default','sid-A');
        composer.value='/memory new deliberate action';
        releaseSave(true);
        const restoreResult = await restoring;
        const retired = {
          restoreResult,
          composer:composer.value,
          failures:_readApprovalTransportFailures(),
          retries:_readApprovalCommandRetries(),
          retryId:_approvalCommandRetryId('default','sid-A','/memory old'),
        };
        api = async () => ({output:'new',command_id:'new-command'});
        const newGeneration = _approvalCommandMutationGeneration('default','sid-A');
        const newResult = await executeAgentCommand('/memory new deliberate action', {
          draftClearPromise:Promise.resolve(false),
          ownerMutationGeneration:newGeneration,
        });
        console.log(JSON.stringify({
          retired,
          newResult,
          newRetryId:_approvalCommandRetryId('default','sid-A','/memory new deliberate action'),
          finalFailures:_readApprovalTransportFailures(),
          finalRetries:_readApprovalCommandRetries(),
        }));
        """
    )
    assert out["retired"]["restoreResult"] is False
    assert out["retired"]["composer"] == "/memory new deliberate action"
    assert out["retired"]["failures"] == []
    assert out["retired"]["retries"] == []
    assert out["retired"]["retryId"] is None
    assert out["newResult"]["command_id"] == "new-command"
    assert out["newRetryId"] == "new-command"
    assert out["finalFailures"] == []
    assert [item["command_id"] for item in out["finalRetries"]] == ["new-command"]


def test_submit_edit_truncate_retirement_uses_origin_profile_and_fences_late_callback():
    """A deferred truncate must retire the origin profile, not the current one."""
    body = """
    %(submit_edit)s
    let releaseTruncate;
    let truncateCalls=0;
    let sendCalls=0;
    const _oldestIdx=0;
    const _deliberateSessionModelPick=()=>null;
    const _reArmRecoveryPick=()=>{};
    const _ensureAllMessagesLoaded=async()=>{};
    const send=async()=>{ sendCalls+=1; };
    S.activeProfile='profile-A';
    S.activeProfileIsDefault=false;
    S.session={session_id:'sid-A'};
    S.messages=[{role:'user',content:'before'}];
    api=async()=>{
      truncateCalls+=1;
      return new Promise((resolve)=>{ releaseTruncate=resolve; });
    };
    _stashApprovalTransportFailure(
      'profile-A','sid-A','/memory pending   ',[],'failed-command','/memory pending',0
    );
    _rememberApprovalCommandRetry({
      profile:'profile-A',sid:'sid-A',text:'/memory pending',command_id:'retry-command'
    },0);
    const editDone=submitEdit(0,'edited');
    await new Promise((resolve)=>setTimeout(resolve,0));
    S.activeProfile='profile-B';
    S.activeProfileIsDefault=false;
    S.session={session_id:'sid-B'};
    releaseTruncate({ok:true});
    await editDone;
    console.log(JSON.stringify({
      truncateCalls,
      sendCalls,
      session:S.session&&S.session.session_id,
      failures:_readApprovalTransportFailures(),
      retries:_readApprovalCommandRetries(),
      messages:S.messages,
    }));
    """ % {"submit_edit": SUBMIT_EDIT}
    out = _run_node(body)
    assert out["truncateCalls"] == 1
    assert out["sendCalls"] == 0, "late truncate callback must not send into the switched profile"
    assert out["session"] == "sid-B"
    assert out["failures"] == [], "origin failure store must be retired"
    assert out["retries"] == [], "origin retry store must be retired"
    assert out["messages"] == [{"role": "user", "content": "before"}]
