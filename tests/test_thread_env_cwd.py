from api.config import _clear_thread_env, _set_thread_env
from api.streaming import _build_agent_thread_env


def test_agent_thread_env_workspace_overrides_profile_terminal_cwd():
    """Regression: profile terminal.cwd must not duplicate TERMINAL_CWD.

    get_profile_runtime_env() maps terminal.cwd from config.yaml to TERMINAL_CWD.
    Streaming also has a per-session workspace. Building one merged dict lets the
    workspace override the profile default before calling _set_thread_env(**env).
    """
    env = _build_agent_thread_env(
        {
            "TERMINAL_CWD": ".",
            "TERMINAL_TIMEOUT": "60",
            "OPENROUTER_API_KEY": "test-key",
        },
        workspace="/tmp/webui-workspace",
        session_id="session-123",
        hermes_home="/tmp/hermes-home",
    )

    assert env["TERMINAL_CWD"] == "/tmp/webui-workspace"
    assert env["TERMINAL_TIMEOUT"] == "60"
    assert env["HERMES_EXEC_ASK"] == "1"
    assert env["HERMES_SESSION_KEY"] == "session-123"
    assert env["HERMES_HOME"] == "/tmp/hermes-home"

    # This is the call that used to fail when TERMINAL_CWD was passed twice.
    _set_thread_env(**env)
    _clear_thread_env()
