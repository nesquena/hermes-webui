from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_use_entry_in_commands_array():
    src = read("static/commands.js")
    assert "{name:'use'," in src, "COMMANDS must contain a {name:'use', ...} entry"


def test_use_entry_precedes_stop_entry():
    src = read("static/commands.js")
    use_pos = src.index("{name:'use',")
    stop_pos = src.index("{name:'stop',")
    assert use_pos < stop_pos, "/use must be registered before /stop in COMMANDS"


def test_cmdUse_function_defined():
    src = read("static/commands.js")
    assert "async function cmdUse(args)" in src, "cmdUse function must be defined"


def test_forced_skill_directive_declared():
    src = read("static/commands.js")
    assert "let _forcedSkillDirectivePending=null;" in src, "_forcedSkillDirectivePending must be declared at module scope"


def test_forced_skill_directive_set_in_cmdUse():
    src = read("static/commands.js")
    assert "pending.promise = new Promise" in src, "cmdUse must create a pending Promise"
    assert "_forcedSkillDirectivePending = pending;" in src, "cmdUse must publish the pending directive before awaiting"


def test_use_entry_has_noEcho():
    src = read("static/commands.js")
    # Extract the /use entry line and check noEcho:true is present
    idx = src.index("{name:'use',")
    line_end = src.index("}", idx)
    entry = src[idx:line_end + 1]
    assert "noEcho:true" in entry, "/use entry must have noEcho:true"


def test_use_entry_has_subArgs_skills():
    src = read("static/commands.js")
    idx = src.index("{name:'use',")
    line_end = src.index("}", idx)
    entry = src[idx:line_end + 1]
    assert "subArgs:'skills'" in entry, "/use entry must have subArgs:'skills' for autocomplete"


def test_directive_consumed_at_injection_site():
    """_forcedSkillDirectivePending is cleared at the consume site, not in finally."""
    src = read("static/messages.js")
    finally_part = src.split("finally")[1] if "finally" in src else ""
    assert "_forcedSkillDirectivePending = null;" not in finally_part, \
        "_forcedSkillDirectivePending must NOT be cleared in the finally block"
    assert "const _directive = await _pending.promise;" in src, \
        "consume site must await the pending promise"
    assert "_forcedSkillDirectivePending = null;" in src, \
        "_forcedSkillDirectivePending must be cleared somewhere in messages.js"


def test_directive_injection_before_empty_guard():
    src = read("static/messages.js")
    inject_pos = src.index("_forcedSkillDirectivePending")
    guard_pos = src.index("if(!msgText){setComposerStatus('Nothing to send');return;}")
    assert inject_pos < guard_pos, "directive injection must appear before the if(!msgText) guard"


def test_directive_text_uses_match_name():
    src = read("static/commands.js")
    assert "match.name" in src, "directive must use match.name (canonical casing), not raw user input"
    assert "[USER OVERRIDE] You MUST consult skill '" in src, "directive text must match the specified format"


def test_pending_promise_set_synchronously():
    """_forcedSkillDirectivePending must be set before the first await in cmdUse."""
    src = read("static/commands.js")
    fn_start = src.index("async function cmdUse(args)")
    fn_body = src[fn_start:]
    pending_pos = fn_body.index("_forcedSkillDirectivePending = pending;")
    first_await = fn_body.index("await ")
    assert pending_pos < first_await, \
        "_forcedSkillDirectivePending must be set before the first await to close the race window"


def test_directive_survives_local_slash_commands():
    """The consume block must appear after the slash-command early-return, not before."""
    src = read("static/messages.js")
    early_return = src.index("autoResize();hideCmdDropdown();return;")
    consume = src.index("_forcedSkillDirectivePending")
    assert early_return < consume, \
        "slash-command early-return must precede the directive consume block"


def test_directive_pending_captures_session_id():
    src = read("static/commands.js")
    assert "const pending = {sessionId:S.session&&S.session.session_id||null,promise:null};" in src, \
        "cmdUse must capture the session where /use was issued"
    assert "const isCurrentSession = () => !pending.sessionId || (S.session&&S.session.session_id)===pending.sessionId;" in src, \
        "async /use completion must avoid writing status messages into a different session"


def test_directive_only_consumed_by_matching_session():
    src = read("static/messages.js")
    assert "const _pending=_forcedSkillDirectivePending;" in src, \
        "send() must snapshot the pending directive before awaiting it"
    assert "if(!_pending.sessionId||_pending.sessionId===activeSid){" in src, \
        "send() must only consume /use directives issued for the active session"
    assert "if(_forcedSkillDirectivePending===_pending)_forcedSkillDirectivePending = null;" in src, \
        "send() must not clear a newer pending directive created while awaiting"
