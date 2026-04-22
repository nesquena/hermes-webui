"""Tests for slash command echo (#840) — user message shown in chat after /skills, /help, etc."""
import os

_SRC = os.path.join(os.path.dirname(__file__), "..")


def _read(name):
    return open(os.path.join(_SRC, name), encoding="utf-8").read()


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
        src = _read("static/messages.js")
        assert "cmdResult.noEcho" in src, (
            "send() must check cmdResult.noEcho before pushing user message (#840)"
        )

    def test_send_pushes_user_message_for_echo_commands(self):
        src = _read("static/messages.js")
        # User bubble push must happen inside the slash intercept path
        idx = src.find("Slash command intercept")
        block = src[idx:idx + 600]
        assert "role:'user'" in block and "content:text" in block, (
            "send() must push {role:'user', content:text} for echo-worthy slash commands (#840)"
        )

    def test_send_uses_null_check_not_truthy(self):
        """executeCommand now returns null (not false) on no match — send() must handle this."""
        src = _read("static/messages.js")
        idx = src.find("Slash command intercept")
        block = src[idx:idx + 200]
        # The check should be `if(cmdResult){` not `if(executeCommand(text)){`
        assert "cmdResult" in block, (
            "send() must store executeCommand result in a variable and check it"
        )
