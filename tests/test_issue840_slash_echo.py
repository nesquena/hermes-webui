"""Tests for slash command echo (#840) — user message shown in chat after /skills, /help, etc."""
import json
import os
import re
import shutil
import subprocess

import pytest

_SRC = os.path.join(os.path.dirname(__file__), "..")


def _read(name):
    return open(os.path.join(_SRC, name), encoding="utf-8").read()


def _js_function(src, name):
    markers = (f"async function {name}(", f"function {name}(")
    start = next((src.find(marker) for marker in markers if src.find(marker) >= 0), -1)
    if start < 0:
        raise AssertionError(f"{name}() not found")
    parens = 0
    brace = -1
    for idx in range(src.index("(", start), len(src)):
        if src[idx] == "(":
            parens += 1
        elif src[idx] == ")":
            parens -= 1
        elif src[idx] == "{" and parens == 0:
            brace = idx
            break
    if brace < 0:
        raise AssertionError(f"{name}() body not found")
    depth = 1
    for idx in range(brace + 1, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name}() body is unbalanced")


def _builtin_probe():
    commands = _read("static/commands.js")
    messages = _read("static/messages.js")
    start = commands.index("const COMMANDS=[")
    end = commands.index("];", start) + 2
    registry = commands[start:end]
    names = sorted(set(re.findall(r"fn:([A-Za-z_$][\w$]*)", registry)))
    handlers = []
    for name in names:
        if name == "cmdHelp":
            handlers.append(
                "function cmdHelp(){snapshots.help=S.messages.map(m=>m.role+':'+m.content);S.messages.push({role:'assistant',content:'help'});return true;}"
            )
        elif name == "cmdClear":
            handlers.append(
                "function cmdClear(){snapshots.clear=S.messages.map(m=>m.role+':'+m.content);return true;}"
            )
        elif name == "cmdPersonality":
            handlers.append(
                "function cmdPersonality(){snapshots.optout=S.messages.map(m=>m.role+':'+m.content);return false;}"
            )
        else:
            handlers.append(f"function {name}(){{return true;}}")
    helpers = "\n".join(
        _js_function(messages, name)
        for name in (
            "_slashCommandMatch",
            "_echoSlashUserMessage",
            "_finishSlashCommand",
            "_runBuiltinSlashCommand",
        )
    )
    script = f"""
const S={{session:{{session_id:'sid'}},messages:[]}};
const input={{value:''}};
const snapshots={{}};
function $(id){{return id==='msg'?input:null;}}
function parseCommand(text){{
  if(!text.startsWith('/'))return null;
  const parts=text.slice(1).split(/\\s+/);
  return {{name:parts[0].toLowerCase(),args:parts.slice(1).join(' ').trim()}};
}}
function renderMessages(){{}}
function autoResize(){{}}
function hideCmdDropdown(){{}}
function t(key){{return key;}}
{chr(10).join(handlers)}
{registry}
{helpers}
async function run(text,key){{
  S.messages=[];
  input.value=text;
  const result=await _runBuiltinSlashCommand(text,{{echo:true}});
  return {{result,messages:S.messages,snapshot:snapshots[key]||[]}};
}}
(async()=>{{
  const output={{
    echo:await run('/help','help'),
    noEcho:await run('/clear','clear'),
    optout:await run('/personality test','optout'),
  }};
  console.log(JSON.stringify(output));
}})().catch(error=>{{console.error(error.stack||error);process.exit(1);}});
"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    result = subprocess.run(
        [node, "-"], input=script, capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


class TestExecuteCommandReturnValue:
    """executeCommand() now returns null or {noEcho:bool} instead of true/false."""

    def test_execute_command_returns_null_on_no_match(self):
        src = _read("static/commands.js")
        idx = src.find("function executeCommand(")
        block = src[idx:idx + 400]
        # Must return null (not false) when no command matched
        assert "return null;" in block, (
            "executeCommand must return null when no command found (not false)"
        )

    def test_execute_command_returns_noecho_object(self):
        src = _read("static/commands.js")
        assert "return {noEcho:" in src, (
            "executeCommand must return {noEcho:...} when a command runs"
        )

    def test_no_echo_flag_on_clear(self):
        src = _read("static/commands.js")
        # Find the clear command entry
        idx = src.find("name:'clear'")
        assert idx >= 0
        entry = src[idx:idx + 100]
        assert "noEcho:true" in entry, "/clear must have noEcho:true"

    def test_no_echo_flag_on_new(self):
        src = _read("static/commands.js")
        idx = src.find("name:'new'")
        assert idx >= 0
        entry = src[idx:idx + 80]
        assert "noEcho:true" in entry, "/new must have noEcho:true"

    def test_no_echo_flag_on_stop(self):
        src = _read("static/commands.js")
        idx = src.find("name:'stop'")
        assert idx >= 0
        entry = src[idx:idx + 80]
        assert "noEcho:true" in entry, "/stop must have noEcho:true"

    def test_no_echo_flag_on_retry(self):
        src = _read("static/commands.js")
        idx = src.find("name:'retry'")
        assert idx >= 0
        entry = src[idx:idx + 80]
        assert "noEcho:true" in entry, "/retry must have noEcho:true"

    def test_no_echo_flag_on_undo(self):
        src = _read("static/commands.js")
        idx = src.find("name:'undo'")
        assert idx >= 0
        entry = src[idx:idx + 80]
        assert "noEcho:true" in entry, "/undo must have noEcho:true"

    def test_no_echo_flag_on_voice(self):
        src = _read("static/commands.js")
        idx = src.find("name:'voice'")
        assert idx >= 0
        entry = src[idx:idx + 80]
        assert "noEcho:true" in entry, "/voice must have noEcho:true"

    def test_no_echo_flag_on_theme(self):
        src = _read("static/commands.js")
        idx = src.find("name:'theme'")
        assert idx >= 0
        entry = src[idx:idx + 80]
        assert "noEcho:true" in entry, "/theme must have noEcho:true"

    def test_no_echo_flag_on_model(self):
        src = _read("static/commands.js")
        idx = src.find("name:'model'")
        assert idx >= 0
        entry = src[idx:idx + 130]
        assert "noEcho:true" in entry, "/model must have noEcho:true"

    def test_skills_has_no_noecho(self):
        """Commands that produce chat responses must NOT have noEcho."""
        src = _read("static/commands.js")
        idx = src.find("name:'skills'")
        assert idx >= 0
        entry = src[idx:idx + 100]
        assert "noEcho" not in entry, "/skills must echo — no noEcho flag"

    def test_help_has_no_noecho(self):
        src = _read("static/commands.js")
        idx = src.find("name:'help'")
        assert idx >= 0
        entry = src[idx:idx + 80]
        assert "noEcho" not in entry, "/help must echo — no noEcho flag"

    def test_status_has_no_noecho(self):
        src = _read("static/commands.js")
        idx = src.find("name:'status'")
        assert idx >= 0
        entry = src[idx:idx + 80]
        assert "noEcho" not in entry, "/status must echo — no noEcho flag"


class TestSendSlashIntercept:
    """send() in messages.js must push user message for echo-worthy commands."""

    def test_send_checks_noecho_flag(self):
        result = _builtin_probe()
        assert result["echo"]["messages"][0]["content"] == "/help"
        assert result["noEcho"]["messages"] == []

    def test_send_pushes_user_message_for_echo_commands(self):
        result = _builtin_probe()
        assert [
            {key: message[key] for key in ("role", "content")}
            for message in result["echo"]["messages"]
        ] == [
            {"role": "user", "content": "/help"},
            {"role": "assistant", "content": "help"},
        ]

    def test_send_pushes_user_message_before_running_handler(self):
        """Ordering fix: cmdHelp-style handlers push their assistant response
        synchronously.  The user message must be pushed BEFORE the handler
        runs so S.messages ends up [user, assistant] — not [assistant, user]
        which would display in reverse chronological order."""
        result = _builtin_probe()
        assert result["echo"]["snapshot"] == ["user:/help"]

    def test_send_rolls_back_user_push_on_handler_optout(self):
        """If an echo-worthy handler opts out,
        the pre-pushed user message must be popped so the normal send path
        can add it cleanly for forwarding to the agent."""
        result = _builtin_probe()
        assert result["optout"]["result"] == {"matched": True, "handled": False}
        assert result["optout"]["snapshot"] == ["user:/personality test"]
        assert result["optout"]["messages"] == []


def test_compress_has_no_echo_flag():
    """compress is action-only — it resets S.messages internally; echo would flicker."""
    src = _read("static/commands.js")
    import re
    m = re.search(r"\{name:'compress'[^}]+\}", src)
    assert m, "compress entry not found in COMMANDS"
    assert 'noEcho:true' in m.group(), f"compress missing noEcho:true: {m.group()}"


def test_compact_has_no_echo_flag():
    """compact is an alias for compress — same noEcho requirement."""
    src = _read("static/commands.js")
    import re
    m = re.search(r"\{name:'compact'[^}]+\}", src)
    assert m, "compact entry not found in COMMANDS"
    assert 'noEcho:true' in m.group(), f"compact missing noEcho:true: {m.group()}"


def test_title_with_args_pushes_confirmation_message():
    """When /title <name> succeeds, cmdTitle pushes an assistant confirmation bubble."""
    src = _read("static/commands.js")
    # After the rename API call succeeds, an assistant message is pushed
    idx = src.find("title_set")
    segment = src[idx: idx + 300]
    assert 'S.messages.push' in segment, "cmdTitle success path must push an assistant message"
    assert "role:'assistant'" in segment, "cmdTitle confirmation must have role:assistant"


def test_personality_with_args_pushes_confirmation_message():
    """When /personality <name> succeeds, cmdPersonality pushes an assistant confirmation bubble."""
    src = _read("static/commands.js")
    # Find the set-personality success path (after the clear/none/default branch)
    # S.messages.push comes BEFORE the personality_set toast
    idx = src.find("personality_set')+`**${name}**`")
    assert idx != -1, "cmdPersonality confirmation push not found in source"
    segment = src[max(0, idx-100): idx + 200]
    assert 'S.messages.push' in segment, "cmdPersonality success path must push an assistant message"
    assert "role:'assistant'" in segment, "cmdPersonality confirmation must have role:assistant"
