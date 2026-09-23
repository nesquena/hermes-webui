"""Focused frontend contract tests for the explicit "Resume in WebUI" action.

While ``HERMES_WEBUI_EXTERNAL_STATE_READ_ONLY`` is set, foreign CLI/TUI/ACP/
Desktop sessions are projected as read-only sidebar rows and can never become
writable implicitly. ``POST /api/session/resume_in_webui`` (see
``tests/test_session_resume_in_webui.py`` for the backend contract) is the single
sanctioned takeover path.

These tests pin the FRONTEND half of that contract:

* the action is offered only for read-only rows from resumable sources
  (cli/tui/acp/desktop) — never messaging/subagent/cron/kanban/webhook/etc.;
* the click flow is close-menu → explicit confirm naming session + profile →
  profile switch → fresh lineage report → POST → cache update → loadSession →
  rerender;
* a failure leaves the session read-only and only surfaces a concise error;
* the read-only menu and the normal row-open path stay intact.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


# ── JS source extraction helpers ──────────────────────────────────────────────


def _function_body(src: str, name: str) -> str:
    """Return the full ``function <name>(...) {...}`` block via brace matching."""
    marker = f"function {name}("
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    # Skip the parameter list first: default values may contain braces
    # (e.g. ``async function f(session, opts={})``).
    paren = src.find("(", start)
    depth = 1
    j = paren + 1
    while depth and j < len(src):
        if src[j] == "(":
            depth += 1
        elif src[j] == ")":
            depth -= 1
        j += 1
    assert depth == 0, f"{name} parameters did not close"
    brace = src.find("{", j - 1)
    assert brace > start, f"{name} body not found"
    depth = 1
    i = brace + 1
    while depth and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} body did not close"
    return src[start:i]


def _const_line(src: str, name: str) -> str:
    """Return the single-line ``const <name> = ...;`` declaration."""
    match = re.search(rf"^const {re.escape(name)} = .*;$", src, re.MULTILINE)
    assert match, f"const {name} declaration not found"
    return match.group(0)


def _open_session_action_menu(src: str) -> str:
    start = src.index("function _openSessionActionMenu(session, anchorEl){")
    end = src.index("document.addEventListener('click'", start)
    return src[start:end]


def _english_locale_block(src: str) -> str:
    start = src.index("_lang: 'en'")
    start = src.rindex("  en: {", 0, start)
    end = src.index("\n  it: {", start)
    return src[start:end]


# ── 1. Source gate: only read-only, resumable external rows ───────────────────


def _run_can_resume_cases(cases):
    if NODE is None:
        pytest.skip("node not on PATH")
    driver = "\n".join(
        [
            _const_line(SESSIONS_JS, "_MESSAGING_RAW_SOURCES"),
            _const_line(SESSIONS_JS, "_RESUME_IN_WEBUI_SOURCES"),
            _function_body(SESSIONS_JS, "_isMessagingSession"),
            _function_body(SESSIONS_JS, "_isReadOnlySession"),
            _function_body(SESSIONS_JS, "_sessionResumeSourceKey"),
            _function_body(SESSIONS_JS, "_sessionResumeInWebUiProfile"),
            _function_body(SESSIONS_JS, "_canResumeSessionInWebUi"),
            "const cases = JSON.parse(process.argv[1]);",
            "process.stdout.write(JSON.stringify(cases.map(c => _canResumeSessionInWebUi(c))));",
        ]
    )
    result = subprocess.run(
        [NODE, "-e", driver, json.dumps(cases)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_resume_action_only_for_readonly_resumable_external_sources():
    """cli/tui/acp/desktop + read-only ⇒ offered; everything else stays read-only."""
    cases = [
        # ── resumable read-only external projections ──
        {"session_id": "s1", "profile": "alpha", "read_only": True, "source_tag": "cli"},
        {"session_id": "s2", "profile": "alpha", "read_only": True, "raw_source": "tui"},
        {"session_id": "s3", "profile": "alpha", "read_only": True, "source": "acp"},
        {"session_id": "s4", "profile": "alpha", "read_only": True, "source_tag": "desktop"},
        {"session_id": "s5", "profile": "alpha", "is_read_only": True, "session_source": "CLI"},
        # ── still-live / writable rows ──
        {"session_id": "w1", "read_only": False, "source_tag": "cli"},
        {"session_id": "w2", "read_only": True, "source_tag": "claude_code"},
        # ── other read-only surfaces keep their transcript read-only ──
        {
            "session_id": "m1",
            "read_only": True,
            "session_source": "messaging",
            "raw_source": "telegram",
        },
        {"session_id": "m2", "read_only": True, "source_tag": "slack"},
        {"session_id": "sa1", "read_only": True, "source_tag": "subagent"},
        {"session_id": "cr1", "read_only": True, "source_tag": "cron"},
        {"session_id": "kb1", "read_only": True, "source_tag": "kanban"},
        {"session_id": "wh1", "read_only": True, "source_tag": "webhook"},
        {"session_id": "ap1", "read_only": True, "source_tag": "api_server"},
        {"session_id": "wb1", "read_only": True, "source_tag": "webui"},
        {"session_id": "missing-profile", "read_only": True, "source_tag": "cli"},
        {"profile": "alpha", "read_only": True, "source_tag": "cli"},
    ]
    assert _run_can_resume_cases(cases) == [
        True,
        True,
        True,
        True,
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    ]


def test_source_gate_mirrors_backend_allowlist():
    """The frontend allowlist must match _RESUME_IN_WEBUI_SOURCE_ALLOWLIST."""
    backend = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    match = re.search(
        r"_RESUME_IN_WEBUI_SOURCE_ALLOWLIST = frozenset\(\{([^}]*)\}\)", backend
    )
    assert match, "backend source allowlist not found"
    backend_sources = {
        part.strip().strip('"').strip("'")
        for part in match.group(1).split(",")
        if part.strip()
    }
    frontend_sources = {
        part.strip().strip('"').strip("'")
        for part in _const_line(SESSIONS_JS, "_RESUME_IN_WEBUI_SOURCES")
        .split("[", 1)[1]
        .split("]")[0]
        .split(",")
        if part.strip()
    }
    assert frontend_sources == backend_sources == {"cli", "tui", "acp", "desktop"}


# ── 2. Menu wiring ────────────────────────────────────────────────────────────


def test_readonly_menu_offers_resume_action_behind_source_gate():
    menu = _open_session_action_menu(SESSIONS_JS)
    copy_idx = menu.index("_appendSessionCopyLinkAction(menu, session);")
    gate_idx = menu.index("if(_canResumeSessionInWebUi(session)){")
    append_idx = menu.index("_appendSessionResumeInWebUiAction(menu, session);")
    readonly_idx = menu.index("const isReadOnly = _isReadOnlySession(session);")
    assert copy_idx < gate_idx < append_idx, (
        "Resume in WebUI must be appended to the menu after Copy link, behind the "
        "_canResumeSessionInWebUi gate"
    )
    assert readonly_idx < append_idx

    helper = _function_body(SESSIONS_JS, "_appendSessionResumeInWebUiAction")
    assert "t('session_resume_in_webui')" in helper
    assert "t('session_resume_in_webui_desc'" in helper
    assert "ICONS.play" in helper
    # The click must close the menu first and delegate to the shared flow.
    assert helper.index("closeSessionActionMenu();") < helper.index(
        "await resumeSessionInWebUi(session);"
    )


def test_readonly_menu_branch_and_row_open_path_unchanged():
    """Read-only rows remain non-mutating until the explicit resume action."""
    menu = _open_session_action_menu(SESSIONS_JS)
    assert (
        "if(isReadOnly){\n"
        "    _appendSessionExportHtmlAction(menu, session);\n"
        "    _mountSessionActionMenu(menu, session, anchorEl);\n"
        "    return;\n"
        "  }"
    ) in menu
    open_row = _function_body(SESSIONS_JS, "_openSidebarSession")
    assert "if(_isExternalSession(session)){" in open_row
    assert "if(!_isReadOnlySession(session)){" in open_row
    assert "/api/session/import_cli" in open_row
    assert "await _ensureSidebarSessionProfile(session);" in open_row
    assert "await loadSession(session.session_id," in open_row
    # The resume flow never bypasses the read-only composer: the flag is only
    # ever cleared by the server projection returned by the endpoint.
    body = _function_body(SESSIONS_JS, "resumeSessionInWebUi")
    assert "session.read_only" not in body
    assert "read_only = false" not in body


def test_resumable_readonly_rows_expose_the_action_menu_trigger():
    assert "const canOpenActions=!readOnly||_canResumeSessionInWebUi(s);" in SESSIONS_JS
    assert "if(canOpenActions){" in SESSIONS_JS
    assert "const canOpenChildActions=!readOnlyChild||_canResumeSessionInWebUi(child);" in SESSIONS_JS
    assert "if(canOpenChildActions){" in SESSIONS_JS


# ── 3. Click flow ordering ────────────────────────────────────────────────────


def test_resume_flow_order_confirms_switches_fetches_posts_then_reloads():
    body = _function_body(SESSIONS_JS, "resumeSessionInWebUi")
    steps = [
        "_canResumeSessionInWebUi(session)",
        "showConfirmDialog({",
        "await _ensureSidebarSessionProfile(session);",
        "_fetchResumeLineageReport(session)",
        "api('/api/session/resume_in_webui'",
        "_applyResumedSessionToSidebarCache(sid, response && response.session);",
        "await loadSession(sid);",
        "renderSessionListFromCache();",
        "void renderSessionList();",
    ]
    positions = [body.index(step) for step in steps]
    assert positions == sorted(positions), (
        "resume flow must run confirm → profile switch → lineage → POST → cache "
        "update → loadSession → rerender"
    )
    assert "if(!confirmed) return false;" in body


def test_confirm_names_session_and_owning_profile():
    body = _function_body(SESSIONS_JS, "resumeSessionInWebUi")
    assert "const label = _sessionResumeInWebUiLabel(session);" in body
    assert "const profile = _sessionResumeInWebUiProfile(session);" in body
    assert "message: t('session_resume_in_webui_confirm_message', label, profile)," in body
    label_fn = _function_body(SESSIONS_JS, "_sessionResumeInWebUiLabel")
    # D6: the confirmation label is the human title only — no internal session
    # identifier is shown to the user.
    assert "_truncatedSessionId" not in label_fn
    assert "session.session_id" not in label_fn
    assert "session.title || session.name" in label_fn
    profile_fn = _function_body(SESSIONS_JS, "_sessionResumeInWebUiProfile")
    assert "session.profile" in profile_fn
    assert "return raw;" in profile_fn
    assert "S.activeProfile" not in profile_fn


def test_resume_posts_exact_endpoint_payload_with_confirm_true():
    body = _function_body(SESSIONS_JS, "resumeSessionInWebUi")
    assert "method: 'POST'" in body
    report = body[body.index("const report = await _fetchResumeLineageReport(session);"):]
    assert "report.lineage_key" in report
    assert "report.tip_session_id" in report
    payload = body[body.index("body: JSON.stringify({"):body.index("}),\n    });")]
    for field in (
        "session_id: sid",
        "profile,",
        "lineage_root_id: lineageRootId",
        "lineage_tip_id: lineageTipId",
        "confirm: true",
    ):
        assert field in payload, f"{field} missing from resume payload"

    # A missing/empty lineage report must abort before the POST.
    assert "report.found === false" in body
    lineage_guard_idx = body.index("session_resume_in_webui_lineage_unavailable")
    assert lineage_guard_idx < body.index("api('/api/session/resume_in_webui'")

    fetch_helper = _function_body(SESSIONS_JS, "_fetchResumeLineageReport")
    # Fresh evidence: drop the cached report for this row before refetching.
    assert "_lineageReportCache.delete(cacheKey);" in fetch_helper
    assert "_fetchLineageReportForRow(session, lineageKey)" in fetch_helper


def test_profile_mismatch_aborts_before_lineage_and_post():
    body = _function_body(SESSIONS_JS, "resumeSessionInWebUi")
    mismatch_idx = body.index("session_resume_in_webui_profile_mismatch")
    assert mismatch_idx < body.index("_fetchResumeLineageReport(session)")
    assert (
        "if(!_profileMatchesActiveProfile(profile, S.activeProfile || 'default')){"
        in body
    )


def test_cache_row_replaced_or_merged_and_active_session_adopted():
    cache_fn = _function_body(SESSIONS_JS, "_applyResumedSessionToSidebarCache")
    assert "_allSessions.findIndex(s => s && s.session_id === sid)" in cache_fn
    # Existing row ⇒ replaced with the resumed projection (read_only cleared).
    assert "_allSessions[idx] = Object.assign({}, _allSessions[idx], next, {session_id: sid});" in cache_fn
    # Uncached row ⇒ merged in from the endpoint response.
    assert "_allSessions.push(Object.assign({}, next, {session_id: sid}));" in cache_fn
    assert "S.session && S.session.session_id === sid" in cache_fn


# ── 4. Failure handling ───────────────────────────────────────────────────────


def test_failure_keeps_readonly_and_shows_concise_error():
    body = _function_body(SESSIONS_JS, "resumeSessionInWebUi")
    catch_idx = body.index("}catch(err){")
    catch_body = body[catch_idx:]
    # D6: only localized, identifier-free copy reaches the toast; the raw server
    # text is routed to the console as technical detail.
    assert "showToast(_resumeInWebUiErrorMessage(err), 6000, 'error');" in catch_body
    assert "return false;" in catch_body
    assert "loadSession" not in catch_body
    assert "_applyResumedSessionToSidebarCache" not in catch_body
    assert "showToast(t('session_resume_in_webui_failed') + detail" not in catch_body
    # Technical detail is logged, never appended to the user-visible toast.
    mapper_idx = catch_body.index("console.warn('resume_in_webui failed', err);")
    assert mapper_idx < catch_body.index("showToast(")
    mapper_fn = _function_body(SESSIONS_JS, "_resumeInWebUiErrorMessage")
    assert "err.message" in mapper_fn
    for key in (
        "session_resume_in_webui_confirm_required",
        "session_resume_in_webui_not_allowed",
        "session_resume_in_webui_source_changed",
        "session_resume_in_webui_conflict",
        "session_resume_in_webui_failed",
    ):
        assert key in mapper_fn


def test_success_toast_key_present_and_no_english_hardcoding():
    body = _function_body(SESSIONS_JS, "resumeSessionInWebUi")
    assert "showToast(t('session_resume_in_webui_resumed'));" in body
    for literal in ("'Resume in WebUI'", '"Resume in WebUI"'):
        assert literal not in body


# ── 5. i18n (English source of truth; other locales fall back) ────────────────


def test_english_i18n_keys_defined():
    block = _english_locale_block(I18N_JS)
    expected = {
        "session_resume_in_webui": "Resume in WebUI",
        "session_resume_in_webui_confirm_title": "Resume in WebUI?",
        "session_resume_in_webui_confirm_btn": "Resume",
        "session_resume_in_webui_resumed": "Session resumed in WebUI",
        "session_resume_in_webui_failed": "Could not resume this session. Please try again.",
        "session_resume_in_webui_confirm_required": "Resume was not confirmed.",
    }
    for key, value in expected.items():
        assert f"{key}: '{value}'," in block, f"{key} missing from the English locale"
    assert (
        "session_resume_in_webui_desc: 'Take over this {0} session as a writable "
        "WebUI conversation'," in block
    )
    assert "profile \"{1}\"" in block
    assert "session_resume_in_webui_lineage_unavailable:" in block
    assert "session_resume_in_webui_profile_mismatch:" in block


def test_resume_keys_absent_from_other_locales_fall_back_to_english():
    """Non-English blocks omit the keys; t() falls back to the English value."""
    non_english = I18N_JS.replace(_english_locale_block(I18N_JS), "")
    assert "session_resume_in_webui:" not in non_english
    assert "const val = _locale[key] ?? LOCALES.en[key];" in I18N_JS


def test_confirm_message_placeholders_match_t_argument_order():
    block = _english_locale_block(I18N_JS)
    message = re.search(r"session_resume_in_webui_confirm_message: '([^']*)'", block)
    assert message, "confirm message key missing"
    text = message.group(1)
    assert "{0}" in text and "{1}" in text
    assert text.index("{0}") < text.index("{1}"), "session label must precede profile"
