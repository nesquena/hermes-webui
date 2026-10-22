from pathlib import Path
import re
import pytest


def test_build_agent_thread_env_sets_session_profile():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    match = re.search(
        r"(def _build_agent_thread_env\(.*?\n)(?=\ndef |\nclass )",
        src,
        re.DOTALL,
    )
    assert match, "_build_agent_thread_env not found in api/streaming.py"
    ns: dict = {}
    exec(compile(match.group(1), "<streaming_extract>", "exec"), ns)

    fn = ns["_build_agent_thread_env"]

    # When profile_name is passed
    env_with_profile = fn({}, "/workspace", "sess-123", "/path/home", profile_name="coding")
    assert env_with_profile.get("HERMES_SESSION_PROFILE") == "coding"
    assert env_with_profile.get("HERMES_SESSION_KEY") == "sess-123"
    assert env_with_profile.get("HERMES_HOME") == "/path/home"

    # When profile_name is empty or omitted
    env_no_profile = fn({}, "/workspace", "sess-123", "/path/home")
    assert "HERMES_SESSION_PROFILE" not in env_no_profile

    env_empty_profile = fn({}, "/workspace", "sess-123", "/path/home", profile_name="")
    assert "HERMES_SESSION_PROFILE" not in env_empty_profile


def test_streaming_wires_session_profile_in_thread_and_process_env():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    assert "profile_name=_resolved_profile_name or \"\"" in src
    assert "os.environ['HERMES_SESSION_PROFILE'] = str(_resolved_profile_name)" in src
    assert "if old_session_profile is None: os.environ.pop('HERMES_SESSION_PROFILE', None)" in src
