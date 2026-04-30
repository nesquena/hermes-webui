import os
import re
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
    assert "old_profile_env" in src
    assert "os.environ.update(_profile_runtime_env)" in src


def test_streaming_thread_env_workspace_overrides_profile_terminal_cwd():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    match = re.search(
        r"(def _build_agent_thread_env\(.*?)\n(?=\ndef |\nclass )",
        src,
        re.DOTALL,
    )
    assert match, "_build_agent_thread_env not found in api/streaming.py"

    ns: dict = {}
    exec(compile(match.group(1), "<streaming_extract>", "exec"), ns)
    build_env = ns["_build_agent_thread_env"]

    env = build_env(
        {
            "TERMINAL_ENV": "local",
            "TERMINAL_CWD": "/profile/cwd",
            "HERMES_HOME": "/profile/home-from-env",
        },
        workspace="/workspace/session",
        session_id="session-123",
        profile_home="/profile/home",
    )

    assert env["TERMINAL_ENV"] == "local"
    assert env["TERMINAL_CWD"] == "/workspace/session"
    assert env["HERMES_EXEC_ASK"] == "1"
    assert env["HERMES_SESSION_KEY"] == "session-123"
    assert env["HERMES_HOME"] == "/profile/home"
