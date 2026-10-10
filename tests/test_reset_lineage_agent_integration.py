"""Opt-in production composition with an explicitly selected Hermes Agent checkout.

No Agent implementation is mocked. The subprocess isolates Agent imports and
state from the WebUI suite's stubs. Only the clock is fixed for historical rows.
See TESTING.md for the known markerless-branch failure and --runxfail command.
"""
import json
import os
from pathlib import Path
import subprocess

import pytest

from tests.test_session_lineage_collapse import NODE, render_sidebar_rows

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """This module owns its Agent subprocess; it needs no WebUI HTTP server."""


# Keep imports inside the subprocess: conftest and neighboring WebUI tests may
# install lightweight hermes_state stubs, which cannot prove this contract.
PROBE = r'''
import json
from pathlib import Path
import sys
from unittest.mock import patch
from hermes_state import SessionDB
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from api.agent_sessions import (
    read_importable_agent_session_rows, read_session_lineage_metadata,
    read_session_lineage_report,
)

home = Path(sys.argv[1])
db = SessionDB(home / "state.db")

def at(when, fn, *args, **kwargs):
    with patch("hermes_state_sessions.time.time", return_value=when):
        return fn(*args, **kwargs)

def observe(parent, child):
    metadata = read_session_lineage_metadata(home / "state.db", [parent, child])
    rows = read_importable_agent_session_rows(
        home / "state.db", limit=None, exclude_sources=None,
    )
    rows = [dict(row, session_id=row["id"]) for row in rows
            if row["id"] in (parent, child)]
    return {
        "parent": parent, "child": child,
        "config": json.loads(db.get_session(child)["model_config"] or "{}"),
        "metadata": metadata[child], "rows": rows,
        "report": read_session_lineage_report(home / "state.db", child),
    }

results = {}
for case, source, marker in [
    ("legacy_branch", "telegram", None),
    ("explicit_branch", "telegram", "_branched_from"),
    ("delegate", "telegram", "_delegate_from"),
    ("tool", "tool", None),
]:
    parent, child, key = case + "_parent", case + "_child", "telegram:" + case
    at(100, db.create_session, parent, "telegram", session_key=key)
    at(150, db.create_session, child, source, session_key=key,
       parent_session_id=parent, model_config={marker: parent} if marker else None)
    db.append_message(parent, "user", "parent turn", timestamp=101)
    db.append_message(child, "user", "child turn", timestamp=151)
    at(160, db.end_session, parent, "branched")
    at(170, db.reopen_session, parent)
    at(200, db.end_session, parent, "session_switch")
    before = observe(parent, child)
    at(210, db.reopen_session, parent)
    results[case] = {"before": before, "after": observe(parent, child)}

# A markerless tool child created after the parent's reset boundary is still a
# subagent, even though its routing key and timestamps match the legacy heuristic.
parent, child, key = "late_tool_parent", "late_tool_child", "telegram:late-tool"
at(100, db.create_session, parent, "telegram", session_key=key)
at(200, db.end_session, parent, "session_reset")
at(210, db.create_session, child, "tool", session_key=key, parent_session_id=parent)
db.append_message(parent, "user", "parent turn", timestamp=101)
db.append_message(child, "user", "tool task", timestamp=211)
before = observe(parent, child)
at(220, db.reopen_session, parent)
results["tool_after_reset"] = {"before": before, "after": observe(parent, child)}

# Preserve a real legacy reset across a second, later end stamp. Rejecting a
# stamped row just because it predates the latest end would destroy this case.
parent, child, key = "old_reset_parent", "old_reset_child", "telegram:old-reset"
at(100, db.create_session, parent, "telegram", session_key=key)
at(130, db.end_session, parent, "session_reset")
at(150, db.create_session, child, "telegram", session_key=key, parent_session_id=parent)
db.append_message(parent, "user", "parent turn", timestamp=101)
db.append_message(child, "user", "legacy reset turn", timestamp=151)
at(170, db.reopen_session, parent)
before = observe(parent, child)
at(200, db.end_session, parent, "session_switch")
at(210, db.reopen_session, parent)
results["legacy_reset"] = {"before": before, "after": observe(parent, child)}

# Real reset creation uses SessionStore.reset_session, not a hand-written marker.
store = SessionStore(home / "sessions", GatewayConfig())
original = store.get_or_create_session(SessionSource(
    platform=Platform.TELEGRAM, chat_id="reset-integration", user_id="test-user",
))
successor = store.reset_session(original.session_key)
db.append_message(original.session_id, "user", "original turn")
db.append_message(successor.session_id, "user", "new conversation")
before = observe(original.session_id, successor.session_id)
db.reopen_session(original.session_id)
db.end_session(original.session_id, "session_switch")
db.reopen_session(original.session_id)
results["gateway_reset"] = {
    "before": before, "after": observe(original.session_id, successor.session_id),
}
store._db_for_key(original.session_key).close()
db.close()
print(json.dumps(results))
'''


@pytest.fixture(scope="module")
def real_agent_scenarios(tmp_path_factory):
    configured = os.environ.get("HERMES_WEBUI_AGENT_DIR")
    if configured is None:
        pytest.skip("set HERMES_WEBUI_AGENT_DIR to run the real SessionDB integration")
    agent_dir = Path(configured).expanduser().resolve()
    if not configured or not (agent_dir / "hermes_state.py").is_file():
        pytest.fail(f"HERMES_WEBUI_AGENT_DIR must contain hermes_state.py: {agent_dir}")
    # The suite's interpreter need not have Agent dependencies. Require the
    # existing explicit runtime override rather than silently reusing it.
    configured_python = os.environ.get("HERMES_WEBUI_PYTHON")
    if not configured_python:
        pytest.fail("set HERMES_WEBUI_PYTHON to the Agent environment's Python executable")
    # Keep venv symlinks intact; resolving bin/python can escape its environment.
    agent_python = Path(configured_python).expanduser().absolute()
    if not agent_python.is_file():
        pytest.fail(f"HERMES_WEBUI_PYTHON is not a Python executable: {agent_python}")
    home = tmp_path_factory.mktemp("real-agent-reset")
    # Allow no credentials, inherited config or existing user state into Agent.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "HERMES_HOME": str(home),
        "HERMES_BASE_HOME": str(home),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_CONFIG_PATH": str(home / "config.yaml"),
        "HERMES_WEBUI_STATE_DIR": str(home / "webui"),
        "HERMES_WEBUI_AGENT_DIR": str(agent_dir),
        "HERMES_WEBUI_PYTHON": str(agent_python),
        "PYTHONPATH": os.pathsep.join((str(agent_dir), str(ROOT))),
    }
    try:
        result = subprocess.run(
            [str(agent_python), "-c", PROBE, str(home)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.fail(f"real Agent probe could not run with {agent_python}: {exc}")
    if result.returncode != 0:
        pytest.fail(
            f"real Agent probe failed with {agent_python} (exit {result.returncode}):\n"
            + result.stdout + result.stderr
        )
    try:
        scenarios = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"real Agent probe returned invalid JSON: {exc}\n" + result.stdout + result.stderr)
    if NODE is None:
        pytest.fail("node must be on PATH for the opted-in real Agent sidebar integration")
    return scenarios


def _assert_child_projection(observation):
    child, parent = observation["child"], observation["parent"]
    assert observation["metadata"]["relationship_type"] == "child_session", observation
    projected = {row["session_id"]: row for row in observation["rows"]}
    assert projected[child]["relationship_type"] == "child_session"
    visible = render_sidebar_rows(observation["rows"], observation["rows"])
    assert child not in {row["session_id"] for row in visible}
    owner = next(row for row in visible if row["session_id"] == parent)
    assert child in {row["session_id"] for row in owner["_child_sessions"]}


@pytest.mark.parametrize("case", ["explicit_branch", "delegate", "tool", "tool_after_reset"])
def test_real_agent_reopen_preserves_non_reset_children(real_agent_scenarios, case):
    scenario = real_agent_scenarios[case]
    for phase in ("before", "after"):
        assert "_reset_from" not in scenario[phase]["config"]
        _assert_child_projection(scenario[phase])


@pytest.mark.parametrize("case", ["gateway_reset", "legacy_reset"])
def test_real_reset_stays_independent_after_parent_reopen(real_agent_scenarios, case):
    for observation in real_agent_scenarios[case].values():
        parent, child = observation["parent"], observation["child"]
        assert observation["config"]["_reset_from"] == parent
        assert observation["metadata"]["relationship_type"] == "reset_successor"
        assert observation["metadata"]["_lineage_root_id"] == child
        assert observation["report"]["total_segments"] == 1
        visible = render_sidebar_rows(observation["rows"], observation["rows"])
        assert {parent, child} == {row["session_id"] for row in visible}


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Agent reopen_session backfills creation-authority _reset_from onto markerless legacy branches",
)
def test_real_agent_markerless_branch_survives_later_reset_boundary(real_agent_scenarios):
    scenario = real_agent_scenarios["legacy_branch"]
    assert "_reset_from" not in scenario["before"]["config"]
    _assert_child_projection(scenario["before"])
    _assert_child_projection(scenario["after"])
