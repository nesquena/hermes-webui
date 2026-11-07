"""Opt-in real HTTP/native-Agent reproduction for WebUI issue #5937.

The default suite skips this test because it needs a user-supplied native Agent
checkout and (for the Docker row) Docker. It intentionally does not mock the
WebUI, Agent, filesystem tools, authentication, or SSE stream.
"""

import os
from pathlib import Path

import pytest

from tests.native_workspace_routing_fixture import NativeWorkspaceCase


pytestmark = pytest.mark.integration
_BACKENDS = tuple(
    item.strip()
    for item in os.environ.get(
        "HERMES_WEBUI_NATIVE_WORKSPACE_BACKENDS", "local,docker"
    ).split(",")
    if item.strip()
)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_native_agent_uses_selected_workspace_for_profile_session(backend: str) -> None:
    """Two selected workspaces must survive profile/session routing to real tools."""
    if os.environ.get("HERMES_WEBUI_NATIVE_WORKSPACE_TEST") != "1":
        pytest.skip(
            "set HERMES_WEBUI_NATIVE_WORKSPACE_TEST=1 to run the external integration"
        )
    agent_dir = os.environ.get("HERMES_WEBUI_AGENT_DIR", "").strip()
    if not agent_dir:
        pytest.fail(
            "HERMES_WEBUI_AGENT_DIR is required when the integration is opted in"
        )
    if backend not in {"local", "docker"}:
        pytest.fail(f"unsupported backend {backend!r}; use local or docker")

    repo = (
        Path(
            os.environ.get(
                "HERMES_WEBUI_NATIVE_TEST_SOURCE",
                str(Path(__file__).resolve().parents[1]),
            )
        )
        .expanduser()
        .resolve()
    )
    if not (repo / "server.py").is_file():
        pytest.fail(f"HERMES_WEBUI_NATIVE_TEST_SOURCE has no server.py: {repo}")
    with NativeWorkspaceCase(repo, backend) as case:
        reports = [
            case.run_profile(profile) for profile in ("native-alpha", "native-beta")
        ]

    assert [report["profile"] for report in reports] == ["native-alpha", "native-beta"]
    assert all(
        report["workspace"].endswith(f"{report['profile']}-selected")
        for report in reports
    )
    failures = {
        report["profile"]: [name for name, ok in report["checks"].items() if not ok]
        for report in reports
    }
    assert not any(failures.values()), (
        f"Real native workspace routing failed: {failures}"
    )
