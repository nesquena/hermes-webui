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


def test_streaming_runtime_env_merges_without_duplicate_terminal_cwd():
    from api.streaming import _build_agent_runtime_env

    env = _build_agent_runtime_env(
        {
            "TERMINAL_ENV": "ssh",
            "TERMINAL_CWD": "/profile/cwd",
            "TERMINAL_TIMEOUT": "60",
        },
        workspace="/session/workspace",
        session_id="sess-123",
        hermes_home="/tmp/hermes-home",
    )

    assert env["TERMINAL_ENV"] == "ssh"
    assert env["TERMINAL_TIMEOUT"] == "60"
    assert env["TERMINAL_CWD"] == "/session/workspace"
    assert env["HERMES_EXEC_ASK"] == "1"
    assert env["HERMES_SESSION_KEY"] == "sess-123"
    assert env["HERMES_HOME"] == "/tmp/hermes-home"
    assert list(env).count("TERMINAL_CWD") == 1


def test_streaming_applies_profile_runtime_env_to_agent_run():
    src = Path("api/streaming.py").read_text(encoding="utf-8")

    assert "get_profile_runtime_env" in src
    assert "_profile_runtime_env" in src
    assert "old_profile_env" in src
    assert "os.environ.update(_profile_runtime_env)" in src
