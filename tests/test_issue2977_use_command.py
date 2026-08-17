from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def _function_source(source, marker):
    start = source.find(marker)
    assert start >= 0, f"{marker!r} not found"
    body_start = source.find("{", source.find(")", start))
    assert body_start >= 0, f"{marker!r} has no body"
    depth = 0
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    i = body_start
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
        elif block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 1
        elif quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch == "/" and nxt == "/":
            line_comment = True
            i += 1
        elif ch == "/" and nxt == "*":
            block_comment = True
            i += 1
        elif ch in "'\"`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError(f"{marker!r} body is not balanced")


def _balanced_block(source, marker):
    start = source.find(marker)
    assert start >= 0, f"{marker!r} not found"
    brace = source.find("{", start)
    assert brace >= 0, f"{marker!r} has no opening brace"
    depth = 0
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    i = brace
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
        elif block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 1
        elif quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch == "/" and nxt == "/":
            line_comment = True
            i += 1
        elif ch == "/" and nxt == "*":
            block_comment = True
            i += 1
        elif ch in "'\"`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError(f"{marker!r} block is not balanced")


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
    assert "resolve({name:match.name,directive,content:skillContent});" in src, \
        "cmdUse must resolve the pending payload with skill name, directive, and fetched content"


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
    send = _function_source(read("static/messages.js"), "async function send(")
    forced = _balanced_block(send, "if(_submittedForcedSkill){")
    session_match = _balanced_block(forced, "if(!_pending.sessionId||_pending.sessionId===activeSid){")
    finally_block = _balanced_block(send, "finally{")
    assert "_forcedSkillDirectivePending" not in finally_block, \
        "_forcedSkillDirectivePending must NOT be cleared in the finally block"
    submitted_pos = send.index("const _submittedForcedSkill=_forcedSkillDirectivePending;")
    upload_pos = send.index("await uploadPendingFiles")
    directive_await_pos = send.index("const _directivePayload = await _pending.promise;")
    chat_start_pos = send.index("api('/api/chat/start'")
    assert submitted_pos < upload_pos < directive_await_pos < chat_start_pos
    assert "const _submittedForcedSkill=_forcedSkillDirectivePending;" in send, \
        "send() must snapshot the forced-skill owner before awaiting"
    assert "const _pending=_submittedForcedSkill;" in forced, \
        "the injection site must consume the snapshotted owner"
    assert "const _pending=_forcedSkillDirectivePending;" not in send, \
        "the consume site must not re-read the mutable global"
    assert "const _directivePayload = await _pending.promise;" in session_match, \
        "consume site must await the pending promise"
    owner_check = session_match.index("if(_forcedSkillDirectivePending===_pending)")
    clear_pos = session_match.index("_forcedSkillDirectivePending=null;")
    context_pos = session_match.index("const _forcedSkillBlock")
    assert owner_check < clear_pos < context_pos
    assert "if(_forcedSkillDirectivePending===_pending)_forcedSkillDirectivePending=null;" in session_match, \
        "_forcedSkillDirectivePending must be cleared somewhere in messages.js"
    assert session_match.count("_forcedSkillDirectivePending") == 2, \
        "the consume block may only inspect the global for the equality-guarded clear"


def test_directive_injection_before_empty_guard():
    send = _function_source(read("static/messages.js"), "async function send(")
    inject_pos = send.index("const _forcedSkillBlock")
    guard_pos = send.index("if(!msgText){if(_ownsSendPane()) setComposerStatus('Nothing to send');return;}")
    assert inject_pos < guard_pos, "directive injection must appear before the if(!msgText) guard"


def test_directive_text_uses_match_name():
    src = read("static/commands.js")
    assert "match.name" in src, "directive must use match.name (canonical casing), not raw user input"
    assert "[USER OVERRIDE] You MUST follow the skill '" in src, "directive text must match the specified format"
    assert "content provided below" in src, "directive must reference the injected skill content"


def test_use_fetches_canonical_skill_content():
    src = read("static/commands.js")
    assert "api(`/api/skills/content?name=${encodeURIComponent(match.name)}`)" in src, \
        "cmdUse must fetch the canonical skill content after resolving the canonical skill name"
    assert "typeof detail.content==='string' ? detail.content.trim() : ''" in src, \
        "cmdUse must reject missing or non-string skill content"


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
    src = _function_source(read("static/messages.js"), "async function send(")
    forced = _balanced_block(src, "if(_submittedForcedSkill){")
    session_match = _balanced_block(forced, "if(!_pending.sessionId||_pending.sessionId===activeSid){")
    assert "const _submittedForcedSkill=_forcedSkillDirectivePending;" in src, \
        "send() must snapshot the pending directive before awaiting it"
    assert "const _pending=_submittedForcedSkill;" in forced, \
        "the consume site must use the frozen directive owner"
    assert "if(!_pending.sessionId||_pending.sessionId===activeSid){" in forced, \
        "send() must only consume /use directives issued for the active session"
    assert "if(_forcedSkillDirectivePending===_pending)_forcedSkillDirectivePending=null;" in session_match, \
        "send() must not clear a newer pending directive created while awaiting"
    assert "[FORCED SKILL CONTEXT: ${_forcedSkillName}]" in session_match, \
        "send() must prepend deterministic forced-skill content before the user message"


def test_older_directive_cannot_clear_newer_pending_owner():
    """The old owner can clear the global only while it is still current."""
    send = _function_source(read("static/messages.js"), "async function send(")
    session_match = _balanced_block(
        _balanced_block(send, "if(_submittedForcedSkill){"),
        "if(!_pending.sessionId||_pending.sessionId===activeSid){",
    )
    clear = "if(_forcedSkillDirectivePending===_pending)_forcedSkillDirectivePending=null;"
    assert session_match.count(clear) == 1
    assert session_match.count("_forcedSkillDirectivePending=null;") == 1
    assert "_forcedSkillDirectivePending=null;" not in session_match.replace(clear, "")
