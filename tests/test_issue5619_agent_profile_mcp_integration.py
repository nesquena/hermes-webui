"""Cross-repo regression for profile-scoped Agent MCP inventory (#5619).

Set ``HERMES_WEBUI_PROFILE_MCP_AGENT_DIR`` to test a specific Agent checkout.
Otherwise the test uses an installed Agent once it exposes profile-scoped
registry queries, and skips older installations.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


WEBUI_REPO = Path(__file__).resolve().parents[1]


def _profile_scoped_agent_repo() -> Path:
    configured = os.environ.get("HERMES_WEBUI_PROFILE_MCP_AGENT_DIR")
    if configured:
        candidate = Path(configured).expanduser().resolve()
    else:
        try:
            from tools.registry import ToolRegistry
        except Exception as exc:
            pytest.skip(f"Hermes Agent registry unavailable: {exc}")
        parameters = inspect.signature(ToolRegistry.get_all_tool_names).parameters
        if "profile_home" not in parameters:
            pytest.skip("installed Hermes Agent predates profile-scoped MCP registry queries")
        candidate = Path(inspect.getfile(ToolRegistry)).resolve().parents[1]

    if not (candidate / "tools" / "registry.py").is_file() or not (
        candidate / "tools" / "mcp_tool.py"
    ).is_file():
        pytest.fail(f"invalid profile-scoped Hermes Agent checkout: {candidate}")
    return candidate


def _agent_python(agent_repo: Path) -> str:
    configured = os.environ.get("HERMES_WEBUI_PYTHON")
    if configured:
        return configured
    for candidate in (
        agent_repo / ".venv" / "bin" / "python",
        agent_repo / "venv" / "bin" / "python",
    ):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def test_real_agent_same_named_mcp_status_schema_and_handler_are_profile_scoped(
    tmp_path: Path,
):
    """Exercise the real Agent registry/MCP surface through WebUI inventory."""
    agent_repo = _profile_scoped_agent_repo()
    env = dict(os.environ)
    env.update(
        {
            "HERMES_HOME": str(tmp_path / "hermes-home"),
            "HERMES_BASE_HOME": str(tmp_path / "hermes-home"),
            "HERMES_CONFIG_PATH": str(tmp_path / "hermes-home" / "config.yaml"),
            "HERMES_WEBUI_STATE_DIR": str(tmp_path / "webui-state"),
            "HERMES_WEBUI_TEST_STATE_DIR": str(tmp_path / "webui-state"),
            "HERMES_WEBUI_NO_DOTENV": "1",
            "HERMES_WEBUI_AGENT_DIR": str(agent_repo),
            "PROFILE_A": str(tmp_path / "profile-a"),
            "PROFILE_B": str(tmp_path / "profile-b"),
            "PYTHONPATH": os.pathsep.join(
                part
                for part in (
                    str(agent_repo),
                    str(WEBUI_REPO),
                    env.get("PYTHONPATH", ""),
                )
                if part
            ),
        }
    )
    probe = r'''
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from api import routes
import tools.mcp_tool as mcp_tool
import tools.registry as registry_module

agent_root = Path(os.environ["HERMES_WEBUI_AGENT_DIR"]).resolve()
assert Path(registry_module.__file__).resolve().is_relative_to(agent_root)
assert Path(mcp_tool.__file__).resolve().is_relative_to(agent_root)

homes = {
    "a": str(Path(os.environ["PROFILE_A"]).resolve()),
    "b": str(Path(os.environ["PROFILE_B"]).resolve()),
}
state = {"home": homes["a"]}
registry = registry_module.ToolRegistry()
registry_module.registry = registry
mcp_tool._active_profile_home_or_none = lambda: state["home"]
mcp_tool._load_mcp_config = lambda: {"shared": {"command": "mock"}}
mcp_tool._run_on_mcp_loop = lambda factory, timeout=None: asyncio.run(factory())
routes.get_active_hermes_home = lambda: Path(state["home"])
routes._active_profile_mcp_config_data = lambda: {
    "mcp_servers": {"shared": {"command": "mock"}}
}
routes._active_profile_allows_ownerless_mcp_inventory = lambda: False


class Session:
    def __init__(self, label):
        self.label = label

    async def call_tool(self, name, arguments):
        assert name == "echo"
        assert arguments == {"message": self.label}
        return SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(text=self.label)],
            structuredContent=None,
        )


for label, home in homes.items():
    server = mcp_tool.MCPServerTask("shared", profile_home=home)
    server.session = Session(label)
    server._tools = [
        SimpleNamespace(
            name="echo",
            description=f"{label} schema",
            inputSchema={"type": "object", "properties": {"message": {"type": "string"}}},
        )
    ]
    names = mcp_tool._register_server_tools(
        "shared",
        server,
        {"tools": {"resources": False, "prompts": False}},
    )
    assert names == ["mcp__shared__echo"]
    server._registered_tool_names = names
    mcp_tool._servers[mcp_tool._server_key("shared", home)] = server


def response_payload(handler):
    return json.loads(handler.wfile.write.call_args[0][0])


observed = []
for label in ("a", "b", "a"):
    home = homes[label]
    state["home"] = home
    status = mcp_tool.get_mcp_status()
    assert len(status) == 1
    assert status[0]["profile_home"] == home
    assert status[0]["connected"] is True

    handler = MagicMock()
    routes._handle_mcp_tools_list(handler)
    payload = response_payload(handler)
    assert payload["source"] == "tool_registry"
    assert [tool["name"] for tool in payload["tools"]] == ["mcp__shared__echo"]
    assert payload["tools"][0]["description"] == f"{label} schema"

    dispatched = json.loads(
        registry.dispatch(
            "mcp__shared__echo",
            {"message": label},
            profile_home=home,
        )
    )
    assert dispatched == {"result": label}
    observed.append([home, payload["tools"][0]["description"], dispatched["result"]])

print(json.dumps(observed))
'''
    result = subprocess.run(
        [_agent_python(agent_repo), "-c", probe],
        cwd=WEBUI_REPO,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed == [
        [str((tmp_path / "profile-a").resolve()), "a schema", "a"],
        [str((tmp_path / "profile-b").resolve()), "b schema", "b"],
        [str((tmp_path / "profile-a").resolve()), "a schema", "a"],
    ]
