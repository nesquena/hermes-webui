"""Regression coverage for issue #1700 parallel profile switching.

A WebUI profile switch uses cookie/thread-local profile state, so it should be
allowed while another session is streaming. Only process-wide profile switches
must remain blocked because they mutate global Hermes runtime state.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _extract_switch_to_profile() -> str:
    marker = "async function switchToProfile(name, options) {"
    idx = PANELS_JS.find(marker)
    assert idx != -1, "switchToProfile() not found in static/panels.js"
    depth = 0
    for i, ch in enumerate(PANELS_JS[idx:], idx):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return PANELS_JS[idx : i + 1]
    raise AssertionError("Could not extract switchToProfile() body")


def _prepare_profile_tree(tmp_path, monkeypatch):
    import api.profiles as profiles

    default_home = tmp_path / ".hermes"
    target_home = default_home / "profiles" / "writer"
    target_workspace = tmp_path / "writer-workspace"
    target_workspace.mkdir(parents=True)
    target_home.mkdir(parents=True)
    (target_home / "config.yaml").write_text(
        f"model:\n  provider: openai-codex\n  default: gpt-5.5\n"
        f"terminal:\n  cwd: {target_workspace}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", default_home)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [{"name": "default"}, {"name": "writer"}])
    profiles._tls.profile = None
    return profiles


def test_process_wide_switch_still_blocks_when_stream_is_active(tmp_path, monkeypatch):
    profiles = _prepare_profile_tree(tmp_path, monkeypatch)
    from api.config import STREAMS

    STREAMS.clear()
    STREAMS["stream-default"] = object()
    try:
        with pytest.raises(RuntimeError, match="Cannot switch profiles while an agent is running"):
            profiles.switch_profile("writer", process_wide=True)
    finally:
        STREAMS.clear()
        profiles._tls.profile = None


def test_per_client_switch_allowed_when_stream_is_active(tmp_path, monkeypatch):
    profiles = _prepare_profile_tree(tmp_path, monkeypatch)
    from api.config import STREAMS

    STREAMS.clear()
    STREAMS["stream-default"] = object()
    try:
        result = profiles.switch_profile("writer", process_wide=False)
    finally:
        STREAMS.clear()
        profiles._tls.profile = None

    assert result["active"] == "writer"
    assert result["default_model"] == "gpt-5.5"


def test_frontend_profile_switch_no_longer_blocks_on_busy_state():
    fn = _extract_switch_to_profile()

    assert "profiles_busy_switch" not in fn
    assert "if (S.busy)" not in fn
    assert "Profile switches are per-client cookie/TLS scoped" in fn


def test_frontend_treats_active_or_pending_session_as_in_progress():
    fn = _extract_switch_to_profile()
    session_decl = re.search(r"\b(?:let|const)\s+sessionInProgress\b", fn)
    assert session_decl, "sessionInProgress declaration not found"
    try_idx = fn.find("try {", session_decl.start())
    assert try_idx != -1, "switchToProfile() try block not found after sessionInProgress declaration"
    session_block = fn[session_decl.start() : try_idx]

    assert "S.session.active_stream_id" in session_block
    assert "S.session.pending_user_message" in session_block
    assert "S.messages.length > 0" in session_block


def test_frontend_skips_nested_new_session_when_caller_owns_replacement():
    fn = _extract_switch_to_profile()

    # Ownership is invocation-scoped: read from THIS call's options, not a page
    # global (Codex #5510 re-gate — a global leaked across the awaited switch).
    assert "const _callerOwnsNewSession = !!(options && options.callerOwnsNewSession);" in fn, \
        "switchToProfile() must derive caller-owned from its own options, not a page global"
    assert "_profileSwitchCallerOwnsNewSession" not in fn, \
        "the page-global caller-owned flag must not be referenced inside switchToProfile()"
    branch_idx = fn.find("sessionInProgress && _callerOwnsNewSession")
    nested_new_session_idx = fn.find("await newSession(false")
    assert branch_idx != -1, "caller-owned replacement branch not found in switchToProfile()"
    assert nested_new_session_idx != -1, "nested newSession() call not found in switchToProfile()"
    assert branch_idx < nested_new_session_idx, (
        "switchToProfile() must check the caller-owned replacement path before it awaits nested newSession()"
    )


def test_frontend_caller_owned_ownership_is_invocation_scoped_not_global():
    # An overlapping A→B project-new then A→C manual switch must not let C inherit
    # B's caller-owned flag. Structural proof: the page-global is gone and the
    # only owner-setting caller passes {callerOwnsNewSession:true} to its own
    # switchToProfile() call (Codex #5510 re-gate race).
    sessions_js = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    assert "let _profileSwitchCallerOwnsNewSession = false;" not in sessions_js, \
        "the page-global caller-owned flag must be removed"
    ensure_idx = sessions_js.find("async function _ensureProjectProfileForNewSession(project)")
    assert ensure_idx >= 0
    ensure_src = sessions_js[ensure_idx: ensure_idx + 900]
    assert "await switchToProfile(targetProfile,{callerOwnsNewSession:true});" in ensure_src, \
        "project-new must pass caller-owned as an invocation-scoped option"


def test_frontend_caller_owned_profile_switch_keeps_existing_one_shot_workspace():
    fn = _extract_switch_to_profile()

    guard_idx = fn.find("if (!_callerOwnsNewSession) {")
    assign_idx = fn.find("S._profileSwitchWorkspace = data.default_workspace;")

    assert guard_idx != -1, "switchToProfile() must guard profile default one-shot assignment"
    assert assign_idx != -1, "profile-switch workspace assignment not found"
    assert guard_idx < assign_idx, (
        "caller-owned profile switches must not overwrite an existing one-shot workspace with the destination profile default"
    )
