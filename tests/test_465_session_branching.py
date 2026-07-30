"""Tests for issue #465 — session branching (/branch).

Verifies:
  1. Backend endpoint POST /api/session/branch exists in routes.py
  2. Session model supports parent_session_id field
  3. Frontend /branch slash command is registered
  4. forkFromMessage function exists in commands.js
  5. Fork button (git-branch icon) is rendered in ui.js message actions
  6. Parent session indicator uses a subtle git-branch icon in sessions.js sidebar
  7. i18n keys exist for all branch-related strings
  8. git-branch icon exists in icons.js
"""
import json
import io
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import api.routes as routes
import pytest


ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = ROOT / "static" / "commands.js"
SESSIONS_JS = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _extract_async_function(source: str, name: str) -> str:
    start = source.find(f"async function {name}(")
    assert start != -1, f"Could not find async function {name}"
    brace = source.find("{", start)
    assert brace != -1, f"Could not find opening brace for {name}"
    depth = 0
    for idx in range(brace, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:idx + 1]
    pytest.fail(f"Could not extract complete function body for {name}")


def _extract_function(source: str, name: str) -> str:
    start = source.find(f"function {name}(")
    assert start != -1, f"Could not find function {name}"
    brace = source.find("{", start)
    assert brace != -1, f"Could not find opening brace for {name}"
    depth = 0
    for idx in range(brace, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:idx + 1]
    pytest.fail(f"Could not extract complete function body for {name}")


def _run_node(script: str) -> str:
    if NODE is None:
        pytest.skip("node not on PATH")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        script_path = handle.name
    try:
        proc = subprocess.run(
            [NODE, script_path],
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip()
    finally:
        Path(script_path).unlink(missing_ok=True)


def _commands_harness(body: str) -> str:
    source = COMMANDS_JS.read_text(encoding="utf-8")
    session_source = SESSIONS_JS.read_text(encoding="utf-8")
    cmd_branch = _extract_async_function(source, "cmdBranch")
    fork_from = _extract_async_function(source, "forkFromMessage")
    is_read_only = _extract_function(session_source, "_isReadOnlySession")
    is_branchable_read_only = _extract_function(session_source, "_isBranchableReadOnlySession")
    return _run_node(
        "\n".join([
            "const calls = [];",
            "const toasts = [];",
            "let ensureCalls = 0;",
            "let loadedSessions = [];",
            "let renderCalls = 0;",
            "let _oldestIdx = 0;",
            "let S = { session: null, busy: false };",
            "const t = (key) => ({",
            "  no_active_session: 'No active session',",
            "  branch_forked: 'Forked into new session',",
            "  branch_failed: 'Fork failed: ',",
            "}[key] || key);",
            "const showToast = (...args) => { toasts.push(args); };",
            "const api = async (url, opts) => {",
            "  calls.push({ url, body: JSON.parse(opts.body) });",
            "  return { session_id: 'forked-session' };",
            "};",
            "const loadSession = async (sid) => { loadedSessions.push(sid); };",
            "const renderSessionList = async () => { renderCalls += 1; };",
            "const _ensureAllMessagesLoaded = async () => { ensureCalls += 1; };",
            is_read_only,
            is_branchable_read_only,
            cmd_branch,
            fork_from,
            "(async () => {",
            body,
            "})().catch((err) => {",
            "  console.error(err && err.stack ? err.stack : String(err));",
            "  process.exit(1);",
            "});",
        ])
    )


# ── Backend ────────────────────────────────────────────────────────────────────


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {"Content-Type": "application/json", "Content-Length": "1"}
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.command = "POST"
        self.path = "/api/session/branch"
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


def _capture_route(monkeypatch):
    cap = {}

    def _bad(_handler, msg, code=400):
        cap["bad"] = (msg, code)
        return True

    def _j(_handler, obj, *_, **kwargs):
        cap["ok"] = obj
        cap["status"] = kwargs.get("status", 200)
        return True

    monkeypatch.setattr(routes, "bad", _bad)
    monkeypatch.setattr(routes, "j", _j)
    return cap

def test_branch_endpoint_exists():
    """Verify the POST /api/session/branch route handler exists."""
    src = _read('api/routes.py')
    assert '"POST /api/session/branch"' in src or '"/api/session/branch"' in src, \
        "Missing /api/session/branch route"


def test_branch_endpoint_validates_session_id():
    """Verify the branch endpoint requires session_id."""
    src = _read('api/routes.py')
    # Find the branch block
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src, re.DOTALL
    )
    assert branch_match, "Could not find /api/session/branch handler block"
    block = branch_match.group(1)
    assert 'require(body, "session_id")' in block, \
        "Branch handler should validate session_id"


def test_branch_endpoint_consults_foreign_session_guard_on_missing_sidecar():
    """Missing sidecars should classify foreign read-only sessions before 404ing.

    The classification logic was extracted from the inline handler into the
    ``_load_branch_source_or_refuse`` helper (#5449). Assert the handler
    delegates to it, and that the helper carries the real not_claimable→403
    provenance logic — not a stale inline copy.
    """
    src = _read('api/routes.py')
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src, re.DOTALL
    )
    assert branch_match, "Could not find /api/session/branch handler block"
    block = branch_match.group(1)
    assert '_load_branch_source_or_refuse(handler, body["session_id"])' in block, \
        "Branch handler should delegate source-load/refusal to the shared helper"

    # The helper itself must carry the foreign-session classification and pass
    # read-only cron-like sources to the branch builder without saving them.
    helper_match = re.search(
        r'def _load_branch_source_or_refuse\(.*?\)(.*?)(?=\ndef )',
        src, re.DOTALL
    )
    assert helper_match, "Could not find _load_branch_source_or_refuse helper"
    helper = helper_match.group(1)
    assert '_claim_or_synthesize_cli_session(sid)' in helper, \
        "Helper should classify missing-sidecar foreign sessions before returning"
    assert 'if _reason == "not_claimable":' in helper, \
        "Helper should branch on not_claimable foreign ownership"
    assert '_source_kind == "cron"' in helper, \
        "Helper should narrow read-only branch sources to resolved cron source metadata"
    assert 'is_cron_session(' not in helper, \
        "Helper should not use the cron_ session-id prefix as a branch permission gate"
    assert '_foreign_session._branch_source_readonly = True' in helper, \
        "Helper should mark synthesized read-only sources so branch does not save them"
    assert 'return _foreign_session' in helper, \
        "Helper should return synthesized read-only sources to the branch builder"
    assert 'bad(handler, "Read-only sessions cannot be branched from WebUI", 403)' in helper, \
        "Helper should keep non-cron not_claimable sources refused"


def test_branch_helper_gates_persisted_read_only_sources_too():
    """A PERSISTED (stored) read-only session must hit the same branch gate as a
    synthesized foreign one — not slip through `get_session(sid)` (#5555 gate fix).

    Codex found that a stored `read_only=True, source_tag="messaging"` session could
    be branched (200) and its source .save()d because the read-only check only lived
    on the missing-sidecar (except KeyError) path. The loaded-session path must:
    only allow a canonical-cron read-only source, mark it read-only-for-branch, and
    403 every other read-only source.
    """
    src = _read('api/routes.py')
    helper_match = re.search(
        r'def _load_branch_source_or_refuse\(.*?\)(.*?)(?=\ndef )',
        src, re.DOTALL
    )
    assert helper_match, "Could not find _load_branch_source_or_refuse helper"
    helper = helper_match.group(1)
    # The loaded (non-KeyError) path assigns the session to a local, not a bare return.
    assert 'source = get_session(sid)' in helper, \
        "Loaded session must be captured so it can be gated (not returned unconditionally)"
    # The loaded path applies the read-only gate.
    assert 'getattr(source, "read_only", False)' in helper, \
        "Loaded read-only sessions must be gated for branching"
    # Only canonical cron read-only sources pass, marked so the fork won't save them.
    assert 'source._branch_source_readonly = True' in helper, \
        "Loaded read-only cron sources must be marked read-only-for-branch"
    # Verify the read-only gate + 403 come BEFORE the final unconditional return.
    ro_idx = helper.index('getattr(source, "read_only", False)')
    final_return_idx = helper.rindex('return source')
    assert ro_idx < final_return_idx, \
        "The read-only gate must run before the loaded session is returned"
    # The 403 refusal exists on the loaded path (two occurrences now: synth + loaded).
    assert helper.count('bad(handler, "Read-only sessions cannot be branched from WebUI", 403)') >= 2, \
        "Both the synthesized and persisted read-only non-cron paths must 403"


def test_branch_endpoint_returns_new_session_id():
    """Verify the branch endpoint returns session_id and title."""
    src = _read('api/routes.py')
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src, re.DOTALL
    )
    assert branch_match
    block = branch_match.group(1)
    assert '"session_id"' in block, "Branch handler should return session_id"
    assert '"title"' in block, "Branch handler should return title"
    assert '"parent_session_id"' in block, \
        "Branch handler should return parent_session_id"


def test_branch_creates_session_with_parent():
    """Verify the branch creates a Session with parent_session_id set."""
    src = _read('api/routes.py')
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src, re.DOTALL
    )
    assert branch_match
    block = branch_match.group(1)
    assert 'parent_session_id=source.session_id' in block, \
        "Branch handler should set parent_session_id to source session"


def test_branch_marks_explicit_forks_as_fork_sessions():
    """Explicit branches must not be mistaken for compression lineage rows."""
    src = _read('api/routes.py')
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src, re.DOTALL
    )
    assert branch_match
    block = branch_match.group(1)
    assert 'session_source="fork"' in block, \
        "Branch handler should mark explicit forks with session_source='fork'"


def test_branch_fork_sessions_do_not_collapse_into_parent_lineage():
    """Fork sessions are not collapsed into compression-lineage; guard must remain in _sessionLineageKey."""
    src = _read('static/sessions.js')
    fn = re.search(r'function _sessionLineageKey\(.*?\n\}', src, re.DOTALL)
    assert fn, "Could not find _sessionLineageKey"
    block = fn.group(0)
    assert "if(s.session_source==='fork') return null;" in block, \
        "Fork guard must remain in _sessionLineageKey to prevent compression-lineage merging"
    assert block.index("if(s.session_source==='fork') return null;") < block.index('return s.parent_session_id || null')


def test_branch_fork_sessions_nest_under_parent():
    """Forks with a resolvable in-list parent are subgrouped via _isForkWithResolvableParent
    and fed into _attachChildSessionsToSidebarRows, not rendered as flat top-level rows."""
    src = _read('static/sessions.js')
    # Helper must exist
    assert 'function _isForkWithResolvableParent(' in src, \
        "Missing _isForkWithResolvableParent helper"
    # _attachChildSessionsToSidebarRows must check for fork children
    fn = re.search(r'function _attachChildSessionsToSidebarRows\(.*?\n\}', src, re.DOTALL)
    assert fn, "Could not find _attachChildSessionsToSidebarRows"
    block = fn.group(0)
    assert '_isForkWithResolvableParent' in block, \
        "_attachChildSessionsToSidebarRows must route fork children via _isForkWithResolvableParent"
    # _resolveSessionIdFromSidebarLineage must no longer skip fork rows wholesale
    resolve_fn = re.search(
        r'function _resolveSessionIdFromSidebarLineage\(.*?\n\}', src, re.DOTALL)
    assert resolve_fn, "Could not find _resolveSessionIdFromSidebarLineage"
    resolve_block = resolve_fn.group(0)
    assert "row.session_source==='fork'" not in resolve_block, \
        "_resolveSessionIdFromSidebarLineage must not skip fork rows; they may now be active nested children"
    assert "!_isChildSession(s)&&((s&&s.pinned)||!_isForkWithResolvableParent(s, sessionIdsInList))" in block, \
        "Only unpinned resolvable fork rows should be filtered out of the top-level rows array"


def test_branch_nested_fork_rows_keep_session_actions():
    """Nested fork rows should keep the standard session action menu path."""
    src = _read('static/sessions.js')
    assert 'session-child-session-fork' in src, \
        "Missing fork-specific nested child row path"
    assert '_openSessionActionMenu(child, menuBtn)' in src, \
        "Nested fork rows should route the standard session action menu"
    assert 'row._startRename=_buildSessionRenameStarter(child, mainBtn' in src, \
        "Nested fork rows should expose the same rename entry point as top-level rows"


def test_branch_nested_fork_search_results_auto_expand():
    """Nested fork hits should stay visible while sidebar search is active."""
    src = _read('static/sessions.js')
    assert "(_expandedChildSessionKeys.has(lineageKey)||!!searchQueryRaw)" in src, \
        "Search-active fork matches should auto-expand their nested child group"


def test_branch_nested_fork_rows_render_their_own_state_indicator():
    """Expanded fork rows should keep unread/streaming/attention affordances."""
    src = _read('static/sessions.js')
    css = _read('static/style.css')
    assert "session-state-indicator session-child-session-state" in src, \
        "Nested fork rows should render a per-row state indicator"
    assert "session-child-session-fork.streaming" in css, \
        "Nested fork rows should expose row-level streaming styling"


def test_branch_keep_count_support():
    """Verify the branch endpoint supports keep_count parameter."""
    src = _read('api/routes.py')
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src, re.DOTALL
    )
    assert branch_match
    block = branch_match.group(1)
    assert 'keep_count' in block, "Branch handler should support keep_count"
    assert 'forked_messages = source_messages[:keep_count]' in block, \
        "Branch handler should slice messages by keep_count"


def test_branch_auto_title():
    """Verify fork title defaults to '<original> (fork)'."""
    src = _read('api/routes.py')
    branch_match = re.search(
        r'parsed\.path == "/api/session/branch"(.*?)(?=\n    if parsed\.path|$)',
        src, re.DOTALL
    )
    assert branch_match
    block = branch_match.group(1)
    assert '(fork)' in block, "Branch handler should auto-title as '(fork)'"


def test_branch_route_allows_not_claimable_cron_sessions_to_fork(monkeypatch):
    """Direct or stale branch POSTs for read-only cron sessions should create a fork."""
    handler = _FakeHandler()
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": "cron-1"})
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda _sid, metadata_only=False: (_ for _ in ()).throw(KeyError("Session not found")),
    )
    source = routes.Session(
        session_id="cron-1",
        title="Cron Run",
        workspace=".",
        model="claude-sonnet",
        messages=[{"role": "user", "content": "summarize"}],
        source_tag="cron",
        raw_source="cron",
        session_source="other",
    )
    monkeypatch.setattr(routes, "_claim_or_synthesize_cli_session", lambda _sid: (source, "not_claimable"))
    cap = _capture_route(monkeypatch)
    routes.handle_post(handler, urlparse("/api/session/branch"))
    assert "bad" not in cap
    assert cap["status"] == 200
    assert cap["ok"]["title"] == "Cron Run (fork)"
    assert cap["ok"]["parent_session_id"] == "cron-1"
    assert cap["ok"]["session_id"] in routes.SESSIONS


def test_branch_route_keeps_404_for_truly_missing_sessions(monkeypatch):
    """Only real foreign read-only sessions should switch from 404 to 400."""
    handler = _FakeHandler()
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": "ghost-1"})
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda _sid, metadata_only=False: (_ for _ in ()).throw(KeyError("Session not found")),
    )
    monkeypatch.setattr(
        routes,
        "_claim_or_synthesize_cli_session",
        lambda _sid: (None, "no_foreign_state"),
    )
    cap = _capture_route(monkeypatch)
    routes.handle_post(handler, urlparse("/api/session/branch"))
    assert cap["bad"] == ("Session not found", 404)


# ── Session model ──────────────────────────────────────────────────────────────

def test_session_model_parent_session_id():
    """Verify Session model supports parent_session_id."""
    src = _read('api/models.py')
    assert 'parent_session_id' in src, "Session model should have parent_session_id"
    # Check __init__ parameter
    assert 'parent_session_id: str=None' in src, \
        "Session.__init__ should accept parent_session_id parameter"
    # Check it's set on self
    assert 'self.parent_session_id = parent_session_id' in src, \
        "Session.__init__ should assign parent_session_id"


def test_session_compact_includes_parent():
    """Verify compact() includes parent_session_id."""
    src = _read('api/models.py')
    # Find the compact method and scan its full body for parent_session_id.
    # PR #1591 (May 2026) added a has_pending_user_message recompute block at
    # the top of compact() which pushed the parent_session_id field beyond a
    # 1500-char window — widen the scan to 3000 chars to cover the full
    # return-dict body without re-tightening every time compact() grows.
    compact_def_match = re.search(r"def compact\(self", src)
    assert compact_def_match, "Could not find compact() method"
    snippet = src[compact_def_match.start():compact_def_match.start() + 3000]
    assert "'parent_session_id'" in snippet, \
        "compact() should include parent_session_id"


def test_session_metadata_fields_includes_parent():
    """Verify parent_session_id is in METADATA_FIELDS for persistence."""
    src = _read('api/models.py')
    assert "'parent_session_id'" in src, \
        "METADATA_FIELDS should include parent_session_id"


# ── Frontend: slash command ────────────────────────────────────────────────────

def test_branch_slash_command_registered():
    """Verify /branch is registered as a slash command."""
    src = _read('static/commands.js')
    assert "name:'branch'" in src, "/branch should be registered as a command"
    assert 'cmdBranch' in src, "cmdBranch handler should be defined"


def test_cmdBranch_function_exists():
    """Verify cmdBranch function is defined."""
    src = _read('static/commands.js')
    assert 'async function cmdBranch(' in src, \
        "cmdBranch should be an async function"


def test_cmdBranch_calls_branch_endpoint():
    """Verify cmdBranch calls the /api/session/branch endpoint."""
    src = _read('static/commands.js')
    branch_fn = re.search(r'async function cmdBranch\(.*?\n\}', src, re.DOTALL)
    assert branch_fn, "Could not find cmdBranch function"
    block = branch_fn.group(0)
    assert "'/api/session/branch'" in block, \
        "cmdBranch should call /api/session/branch"


def test_cmdBranch_switches_session():
    """Verify cmdBranch calls loadSession after branching."""
    src = _read('static/commands.js')
    branch_fn = re.search(r'async function cmdBranch\(.*?\n\}', src, re.DOTALL)
    assert branch_fn
    block = branch_fn.group(0)
    assert 'loadSession(' in block, \
        "cmdBranch should switch to the new session via loadSession"


# ── Frontend: forkFromMessage ─────────────────────────────────────────────────

def test_forkFromMessage_function_exists():
    """Verify forkFromMessage function exists."""
    src = _read('static/commands.js')
    assert 'async function forkFromMessage(' in src, \
        "forkFromMessage should be defined"


def test_forkFromMessage_passes_keep_count():
    """Verify forkFromMessage passes keep_count to the endpoint."""
    src = _read('static/commands.js')
    fn = re.search(r'async function forkFromMessage\(.*?\n\}', src, re.DOTALL)
    assert fn
    block = fn.group(0)
    assert 'keep_count' in block, \
        "forkFromMessage should pass keep_count to /api/session/branch"


# ── Frontend: fork button in messages ──────────────────────────────────────────

def test_fork_button_rendered_in_ui():
    """Verify fork button is rendered in message actions."""
    src = _read('static/ui.js')
    assert "forkBtn" in src, "forkBtn variable should exist in ui.js"
    assert "fork_from_here" in src, \
        "fork_from_here i18n key should be referenced for tooltip"
    assert "forkFromMessage(" in src, \
        "forkFromMessage should be called from the button"


def test_fork_button_in_message_actions():
    """Verify fork button is included in the msg-actions span."""
    src = _read('static/ui.js')
    # The footHtml template should include forkBtn
    assert '${forkBtn}' in src, \
        "forkBtn should be included in message actions template"


def test_fork_button_is_hidden_for_read_only_sessions():
    """Non-cron read-only sessions should not render the message-level fork affordance."""
    src = _read('static/ui.js')
    assert "const readOnlySession=typeof _isReadOnlySession==='function'" in src, \
        "ui.js should derive a read-only session flag from the shared helper"
    assert "const branchableReadOnlySession=typeof _isBranchableReadOnlySession==='function'" in src, \
        "ui.js should derive a branchable read-only flag from the shared helper"
    assert "const forkBtn  = (readOnlySession&&!branchableReadOnlySession) ? '' :" in src, \
        "fork button should be suppressed when a read-only session is not branchable"


def test_branchable_read_only_helper_accepts_cron_sources():
    """Read-only cron sessions should be forkable follow-up sources."""
    src = _read('static/sessions.js')
    assert "function _isBranchableReadOnlySession(session)" in src
    assert "session && session.source_tag" in src
    assert "session && session.raw_source" in src
    assert "sources.includes('cron')" in src
    assert "sid.startsWith('cron_')" not in src
    assert "sid.startsWith('cron-')" not in src


def test_cmdBranch_rejects_read_only_sessions_without_posting():
    """The /branch command must not POST for non-cron read-only sessions."""
    result = _commands_harness(
        "S.session = { session_id: 'subagent-1', session_source: 'subagent', read_only: true };\n"
        "await cmdBranch('');\n"
        "console.log(JSON.stringify({ calls, toasts, ensureCalls, loadedSessions, renderCalls }));"
    )
    payload = json.loads(result)
    assert payload["calls"] == [], "read-only /branch should not POST /api/session/branch"
    assert payload["ensureCalls"] == 0, "cmdBranch should not trigger message loading"
    assert payload["loadedSessions"] == [], "read-only /branch should not switch sessions"
    assert payload["renderCalls"] == 0, "read-only /branch should not refresh the session list"
    assert payload["toasts"], "read-only /branch should surface a toast"
    assert payload["toasts"][0][0] == "Read-only sessions cannot be forked."


def test_cmdBranch_allows_read_only_cron_sessions_to_post():
    """The /branch command should POST for read-only cron sessions."""
    result = _commands_harness(
        "S.session = { session_id: 'daily-summary', session_source: 'other', raw_source: 'cron', read_only: true };\n"
        "await cmdBranch('Follow-up');\n"
        "console.log(JSON.stringify({ calls, toasts, ensureCalls, loadedSessions, renderCalls }));"
    )
    payload = json.loads(result)
    assert len(payload["calls"]) == 1, "read-only cron /branch should POST once"
    call = payload["calls"][0]
    assert call["url"] == "/api/session/branch"
    assert call["body"]["session_id"] == "daily-summary"
    assert call["body"]["title"] == "Follow-up"
    assert payload["ensureCalls"] == 0, "cmdBranch should not trigger message loading"
    assert payload["loadedSessions"] == ["forked-session"], "cron /branch should load the forked session"
    assert payload["renderCalls"] == 1, "cron /branch should refresh the session list"
    assert payload["toasts"][0][0] == "Forked into new session"


def test_forkFromMessage_rejects_read_only_non_cron_sessions_without_loading_or_posting():
    """Non-cron read-only message forks must stop before the load/post path."""
    result = _commands_harness(
        "S.session = { session_id: 'subagent-1', session_source: 'subagent', read_only: true };\n"
        "await forkFromMessage(1);\n"
        "console.log(JSON.stringify({ calls, toasts, ensureCalls, loadedSessions, renderCalls }));"
    )
    payload = json.loads(result)
    assert payload["calls"] == [], "read-only forkFromMessage should not POST /api/session/branch"
    assert payload["ensureCalls"] == 0, "read-only forkFromMessage should return before loading messages"
    assert payload["loadedSessions"] == [], "read-only forkFromMessage should not switch sessions"
    assert payload["renderCalls"] == 0, "read-only forkFromMessage should not refresh the session list"
    assert payload["toasts"], "read-only forkFromMessage should surface a toast"
    assert payload["toasts"][0][0] == "Read-only sessions cannot be forked."


def test_forkFromMessage_allows_read_only_cron_sessions_to_post():
    """Read-only cron message forks should reach the existing keep_count path."""
    result = _commands_harness(
        "S.session = { session_id: 'cron_1', raw_source: 'cron', read_only: true };\n"
        "_oldestIdx = 2;\n"
        "await forkFromMessage(4);\n"
        "console.log(JSON.stringify({ calls, toasts, ensureCalls, loadedSessions, renderCalls }));"
    )
    payload = json.loads(result)
    assert len(payload["calls"]) == 1, "read-only cron forkFromMessage should POST once"
    call = payload["calls"][0]
    assert call["url"] == "/api/session/branch"
    assert call["body"]["session_id"] == "cron_1"
    assert call["body"]["keep_count"] == 6
    assert payload["ensureCalls"] == 2, "cron forkFromMessage should preserve the full-load flow"
    assert payload["loadedSessions"] == ["forked-session"], "cron forkFromMessage should load the fork"
    assert payload["renderCalls"] == 1, "cron forkFromMessage should refresh the session list"


def test_cmdBranch_rejects_cron_prefixed_id_without_canonical_source():
    """Only canonical cron source fields should unlock read-only branching."""
    result = _commands_harness(
        "S.session = { session_id: 'cron_spoof_messaging', session_source: 'other', read_only: true };\n"
        "await cmdBranch('');\n"
        "console.log(JSON.stringify({ calls, toasts, ensureCalls, loadedSessions, renderCalls }));"
    )
    payload = json.loads(result)
    assert payload["calls"] == [], "cron_ id alone should not unlock read-only /branch"
    assert payload["toasts"][0][0] == "Read-only sessions cannot be forked."


def test_forkFromMessage_preserves_absolute_keep_count_for_writable_sessions():
    """The read-only guard must not break the existing absolute keep_count fix."""
    result = _commands_harness(
        "S.session = { session_id: 'webui-1', read_only: false };\n"
        "_oldestIdx = 5;\n"
        "await forkFromMessage(3);\n"
        "console.log(JSON.stringify({ calls, toasts, ensureCalls, loadedSessions, renderCalls }));"
    )
    payload = json.loads(result)
    assert len(payload["calls"]) == 1, "writable forkFromMessage should still POST once"
    call = payload["calls"][0]
    assert call["url"] == "/api/session/branch"
    assert call["body"]["session_id"] == "webui-1"
    assert call["body"]["keep_count"] == 8, "keep_count should remain absolute across the guard"
    assert payload["ensureCalls"] == 2, "writable forkFromMessage should preserve both message-load calls"
    assert payload["loadedSessions"] == ["forked-session"]
    assert payload["renderCalls"] == 1


# ── Frontend: sidebar parent indicator ────────────────────────────────────────

def test_sidebar_parent_indicator():
    """Verify parent session indicator is rendered in session list."""
    src = _read('static/sessions.js')
    assert 'parent_session_id' in src, \
        "sessions.js should check parent_session_id"
    assert 'session-branch-indicator' in src, \
        "Should have session-branch-indicator class"
    assert "li('git-branch',12)" in src, \
        "Sidebar parent indicator should use the git-branch icon"
    assert '\\u2442' not in src, \
        "Sidebar parent indicator should not use the opaque OCR double-backslash glyph"


def test_parent_indicator_not_clickable():
    """Verify parent indicator is informational, not hidden navigation."""
    src = _read('static/sessions.js')
    # Find the parent indicator block
    parent_block = re.search(
        r'branch-indicator[\s\S]*?parent_session_id[\s\S]*?titleRow\.appendChild',
        src
    )
    assert parent_block, "Could not find parent indicator block"
    block = parent_block.group(0)
    assert 'loadSession(' not in block, \
        "Parent indicator should not navigate to the parent from the sidebar"
    assert 'onclick' not in block, \
        "Parent indicator should not register a hidden click target"


def test_parent_indicator_tooltip_uses_parent_title_fallback():
    """Tooltip should prefer a parent title and only fall back to a short id."""
    src = _read('static/sessions.js')
    assert 'function _sessionTitleForForkParent' in src, \
        "sessions.js should resolve a user-facing parent title"
    assert 'function _truncatedSessionId' in src, \
        "sessions.js should fall back to a truncated id, not raw session_id"
    assert "_sessionTitleForForkParent(s.parent_session_id)||_truncatedSessionId(s.parent_session_id)" in src, \
        "parent indicator tooltip must prefer title and fall back to truncated id"


def test_parent_indicator_hover_only_style():
    """The sidebar lineage indicator should be visually subdued until row hover/focus."""
    src = _read('static/style.css')
    assert '.session-branch-indicator' in src, \
        "Missing session branch indicator CSS"
    assert 'opacity:.35' in src, \
        "Fork lineage indicator should be subdued at rest"
    assert '.session-item:hover .session-branch-indicator' in src, \
        "Fork lineage indicator should become visible on row hover"


# ── Frontend: i18n keys ────────────────────────────────────────────────────────

def test_i18n_branch_keys():
    """Verify all branch-related i18n keys exist in English locale."""
    src = _read('static/i18n.js')
    required_keys = [
        'cmd_branch',
        'cmd_branch_usage',
        'branch_forked',
        'branch_failed',
        'fork_from_here',
        'forked_from',
    ]
    for key in required_keys:
        assert f"{key}:" in src or f"{key} :" in src, \
            f"Missing i18n key: {key}"


# ── Frontend: icon ─────────────────────────────────────────────────────────────

def test_git_branch_icon_exists():
    """Verify git-branch icon is defined in icons.js."""
    src = _read('static/icons.js')
    assert "'git-branch'" in src, \
        "git-branch icon should be defined in LI_PATHS"
