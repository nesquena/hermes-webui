"""Regression coverage for issue #1700 parallel profile switching.

A WebUI profile switch uses cookie/thread-local profile state, so it should be
allowed while another session is streaming. Only process-wide profile switches
must remain blocked because they mutate global Hermes runtime state.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _extract_switch_to_profile() -> str:
    marker = "async function switchToProfile(name) {"
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
    monkeypatch.setattr(
        profiles, "list_profiles_api", lambda: [{"name": "default"}, {"name": "writer"}]
    )
    profiles._tls.profile = None
    return profiles


def test_process_wide_switch_still_blocks_when_stream_is_active(tmp_path, monkeypatch):
    profiles = _prepare_profile_tree(tmp_path, monkeypatch)
    from api.config import STREAMS

    STREAMS.clear()
    STREAMS["stream-default"] = object()
    try:
        with pytest.raises(
            RuntimeError, match="Cannot switch profiles while an agent is running"
        ):
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
    session_block = fn[
        fn.find("const sessionInProgress") : fn.find(
            "try {", fn.find("const sessionInProgress")
        )
    ]

    assert "S.session.active_stream_id" in session_block
    assert "S.session.pending_user_message" in session_block
    assert "S.messages.length > 0" in session_block
