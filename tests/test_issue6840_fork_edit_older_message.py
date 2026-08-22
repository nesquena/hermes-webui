import json
import pathlib
import subprocess


ROOT = pathlib.Path(__file__).parents[1]
UI = ROOT / "static/ui.js"
I18N = ROOT / "static/i18n.js"
ROUTES = ROOT / "api/routes.py"


def _function_source(source, signature):
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    quote = None
    escaped = False
    in_comment = False
    for pos in range(brace, len(source)):
        char = source[pos]
        if in_comment:
            if char == "\n":
                in_comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if source[pos : pos + 2] == "//":
            in_comment = True
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : pos + 1]
    raise AssertionError(f"unterminated function: {signature}")


def _run_node(scenario):
    source = UI.read_text(encoding="utf-8")
    script = f"""
{_function_source(source, "function _lastUserMessageIndex")}
{_function_source(source, "async function submitEdit")}
const scenario = {json.dumps(scenario)};
const calls = [];
let confirmResult = scenario.confirm !== false;
let activeSid = 'source';
const S = {{session: {{session_id: 'source', profile:'default', model:'m', model_provider:'p', workspace:'/w', read_only:!!scenario.readOnly}}, messages: scenario.messages, busy:false, activeProfile:'default'}};
let _oldestIdx = scenario.oldest || 0;
const _deliberateSessionModelPick = () => null;
const _isBranchableReadOnlySession = (session) => !!session.read_only && !!scenario.branchable;
const _reArmRecoveryPick = (...args) => calls.push(['rearm', ...args]);
const _ensureAllMessagesLoaded = async () => {{ if(scenario.resetOldest) _oldestIdx = 0; }};
const showConfirmDialog = async () => {{ calls.push(['confirm']); return confirmResult; }};
const api = async (url, options) => {{ calls.push([url, JSON.parse(options.body)]); if(url.includes('/branch')) {{ if(scenario.branchFailure) throw new Error('branch unavailable'); if(scenario.switchAfterBranch) S.session={{session_id:'other'}}; return {{session_id:'child'}}; }} return {{}}; }};
const loadSession = async (sid) => {{ calls.push(['load', sid]); S.session = scenario.contractMismatch ? {{session_id:sid, profile:'other', model:'m', model_provider:'p', workspace:'/w'}} : {{session_id:sid, profile:'default', model:'m', model_provider:'p', workspace:'/w'}}; activeSid=sid; }};
const renderSessionList = async () => calls.push(['render-list']);
const renderMessages = () => calls.push(['render']);
const send = async () => calls.push(['send', S.session.session_id]);
const showToast = (...args) => calls.push(['toast', ...args]);
const setStatus = (...args) => calls.push(['status', ...args]);
const autoResize = () => calls.push(['resize']);
const t = (key, value) => key + (value === undefined ? '' : ':' + value);
const $ = () => ({{value:''}});
(async () => {{ await submitEdit(scenario.index, 'edited'); console.log(JSON.stringify({{calls, session:S.session.session_id}})); }})();
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_older_edit_forks_without_truncating_and_sends_once():
    result = _run_node({"index": 0, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]})
    assert [call[0] for call in result["calls"]] == [
        "confirm",
        "/api/session/branch",
        "load",
        "render-list",
        "resize",
        "rearm",
        "toast",
        "send",
    ]
    assert result["calls"][1][1] == {"session_id": "source", "keep_count": 0}
    assert result["session"] == "child"


def test_latest_edit_keeps_existing_truncate_path():
    result = _run_node({"index": 2, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]})
    assert "/api/session/branch" not in [call[0] for call in result["calls"]]
    assert result["calls"][0][0] == "/api/session/truncate"
    assert result["calls"][0][1]["keep_count"] == 2
    assert [call[0] for call in result["calls"]].count("send") == 1


def test_paginated_latest_edit_compares_absolute_keep_count():
    result = _run_node(
        {"index": 2, "oldest": 3, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert "/api/session/branch" not in [call[0] for call in result["calls"]]
    assert result["calls"][0][0] == "/api/session/truncate"
    assert result["calls"][0][1]["keep_count"] == 5
    assert [call[0] for call in result["calls"]].count("send") == 1


def test_branchable_read_only_latest_edit_never_truncates():
    result = _run_node(
        {"index": 2, "readOnly": True, "branchable": True, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert "/api/session/truncate" not in [call[0] for call in result["calls"]]
    assert result["calls"][0][0] == "confirm"
    assert result["calls"][1][0] == "/api/session/branch"
    assert result["calls"][1][1]["keep_count"] == 2
    assert [call[0] for call in result["calls"]].count("send") == 1


def test_cancelled_older_edit_makes_no_request():
    result = _run_node(
        {"index": 0, "confirm": False, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert result["calls"] == [["confirm"]]


def test_branch_failure_reports_error_without_sending():
    result = _run_node(
        {"index": 0, "branchFailure": True, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert result["calls"][0][0] == "confirm"
    assert result["calls"][1][0] == "/api/session/branch"
    assert result["calls"][-1][0] == "status"
    assert "branch_failed" in result["calls"][-1][1]
    assert "send" not in [call[0] for call in result["calls"]]


def test_pane_switch_after_branch_aborts_before_child_load():
    result = _run_node(
        {"index": 0, "switchAfterBranch": True, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert [call[0] for call in result["calls"]] == ["confirm", "/api/session/branch"]


def test_child_contract_mismatch_aborts_before_send():
    result = _run_node(
        {"index": 0, "contractMismatch": True, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert [call[0] for call in result["calls"]][-1] == "status"
    assert "contract changed" in result["calls"][-1][1]
    assert "send" not in [call[0] for call in result["calls"]]


def test_older_edit_uses_absolute_index_when_transcript_is_windowed():
    result = _run_node(
        {"index": 0, "oldest": 3, "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]}
    )
    assert result["calls"][1][1]["keep_count"] == 3


def test_source_contracts_and_locales_are_present():
    source = UI.read_text(encoding="utf-8")
    i18n = I18N.read_text(encoding="utf-8")
    assert "const isEditableUser=isUser&&!(readOnlySession&&!branchableReadOnlySession);" in source
    assert "const lastUserKeepCount=lastUserIdx>=0 ? _oldestIdx+lastUserIdx : -1;" in source
    assert "if(branchableReadOnlySession || (lastUserIdx>=0 && absoluteKeepCount!==lastUserKeepCount)){" in source
    assert source.count("const readOnlySession=typeof _isReadOnlySession==='function'") == 1
    assert "session_id:initialSid" in source
    assert "keep_count:absoluteKeepCount" in source
    assert "/api/session/truncate" in source
    assert "/api/session/delete" not in _function_source(source, "async function submitEdit")
    assert i18n.count("edit_fork_title:") == 15
    assert i18n.count("edit_fork_message:") == 15
    assert i18n.count("edit_fork_confirm:") == 15


def test_empty_branch_is_persisted_for_first_prompt_edit():
    source = ROUTES.read_text(encoding="utf-8")
    branch_block = source[source.index('if parsed.path == "/api/session/branch"') :]
    branch_block = branch_block[: branch_block.index('if parsed.path == "/api/session/compress/start"')]
    assert "branch.save()" in branch_block
    assert "if forked_messages:" not in branch_block
