import os
from pathlib import Path

import yaml


def test_profile_runtime_env_includes_terminal_config_and_dotenv(tmp_path):
    from api.profiles import get_profile_runtime_env

    home = tmp_path / "profiles" / "server-ops"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "terminal": {
                    "backend": "ssh",
                    "cwd": "/home/dso2ng/repos",
                    "timeout": 180,
                    "ssh_host": "pollux",
                    "ssh_user": "dso2ng",
                    "persistent_shell": True,
                    "lifetime_seconds": 300,
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (home / ".env").write_text(
        "TERMINAL_TIMEOUT=60\n"
        "TERMINAL_SSH_HOST=pollux-from-env\n"
        "HERMES_MAX_ITERATIONS=90\n",
        encoding="utf-8",
    )

    env = get_profile_runtime_env(home)

    assert env["TERMINAL_ENV"] == "ssh"
    assert env["TERMINAL_CWD"] == "/home/dso2ng/repos"
    assert env["TERMINAL_SSH_USER"] == "dso2ng"
    assert env["TERMINAL_PERSISTENT_SHELL"] == "true"
    assert env["TERMINAL_LIFETIME_SECONDS"] == "300"
    # .env remains the final override source, matching CLI/profile behaviour.
    assert env["TERMINAL_TIMEOUT"] == "60"
    assert env["TERMINAL_SSH_HOST"] == "pollux-from-env"
    assert env["HERMES_MAX_ITERATIONS"] == "90"


def test_streaming_applies_profile_runtime_env_to_agent_run():
    src = Path("api/streaming.py").read_text(encoding="utf-8")

    assert "get_profile_runtime_env" in src
    assert "_profile_runtime_env" in src
    assert "_set_thread_env(**_agent_run_env)" in src
    assert "old_profile_env" in src
    assert "os.environ.update(_profile_runtime_env)" in src


def test_agent_run_env_overrides_profile_terminal_cwd_without_kwarg_collision():
    from api.config import _clear_thread_env, _set_thread_env
    from api.streaming import _build_agent_run_env

    env = _build_agent_run_env(
        {
            "TERMINAL_CWD": "/profile/default/cwd",
            "HERMES_HOME": "/profile/env/home",
            "HERMES_MAX_ITERATIONS": "90",
        },
        "/session/workspace",
        "session-123",
        "/profile/home",
    )

    assert env["TERMINAL_CWD"] == "/session/workspace"
    assert env["HERMES_HOME"] == "/profile/home"
    assert env["HERMES_SESSION_KEY"] == "session-123"
    assert env["HERMES_MAX_ITERATIONS"] == "90"

    _set_thread_env(**env)
    _clear_thread_env()
