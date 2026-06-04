"""Regression coverage for the WebUI session-store contract."""

import json
import os
import pathlib
import subprocess
import sys


REPO = pathlib.Path(__file__).resolve().parents[1]


def test_session_dir_is_under_webui_state_dir(tmp_path):
    """WebUI sessions must live under HERMES_WEBUI_STATE_DIR, not ~/.hermes/sessions.

    Project assignments, read-state, attachments, and settings are all scoped to
    the WebUI state directory. Splitting sessions into the agent-wide
    ~/.hermes/sessions store makes existing project-assigned WebUI sessions look
    missing after restart and causes source/project filters to disagree.
    """
    state_dir = tmp_path / "webui-state"
    env = os.environ.copy()
    env["HERMES_WEBUI_STATE_DIR"] = str(state_dir)
    env.pop("PYTHONPATH", None)

    script = """
import json
import api.config as config
print(json.dumps({
    'state_dir': str(config.STATE_DIR),
    'session_dir': str(config.SESSION_DIR),
    'session_index_file': str(config.SESSION_INDEX_FILE),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**env, "PYTHONPATH": str(REPO)},
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(result.stdout)

    assert payload["state_dir"] == str(state_dir.resolve())
    assert payload["session_dir"] == str((state_dir / "sessions").resolve())
    assert payload["session_index_file"] == str((state_dir / "sessions" / "_index.json").resolve())
