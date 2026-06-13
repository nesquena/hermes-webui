"""Agents probe for Hermes Agent dashboard.

Aggregates local agents from the filesystem and Hermes running agents,
providing a unified view of all available agents.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Well-known Hermes agent home locations
_HERMES_AGENT_HOME_PATHS = [
    Path.home() / ".hermes",
    Path.home() / ".config" / "hermes",
]

# Fallback: try to detect from HERMES_HOME env var
import os

_hermes_home = os.environ.get("HERMES_HOME")
if _hermes_home:
    _HERMES_AGENT_HOME_PATHS.insert(0, Path(_hermes_home))


def _get_project_agent_paths() -> list[Path]:
    """Return list of project-based agent directories to scan.

    Scans for .agents/skills directories in common project locations
    (repos/onlyfans, current directory's parent, etc.)
    """
    candidates = [
        Path(__file__).parent.parent.parent / "repos" / "onlyfans" / ".agents" / "skills",
        Path.cwd() / ".agents" / "skills",
    ]
    return [p for p in candidates if p.exists() and p.is_dir()]


def _scan_local_agents() -> list[dict[str, Any]]:
    """Scan local Hermes agent directory for installed agents.

    Returns list of agent metadata from .agents/skills/ or .hermes/skills/
    """
    agents = []

    # Check Hermes home for agents
    for home_path in _HERMES_AGENT_HOME_PATHS:
        skills_dir = home_path / "agents" / "skills"
        if skills_dir.exists() and skills_dir.is_dir():
            for skill_path in sorted(skills_dir.iterdir()):
                if skill_path.is_dir():
                    agents.append(
                        {
                            "name": skill_path.name,
                            "source": "hermes-home",
                            "path": str(skill_path),
                            "type": "skill",
                        }
                    )

    # Check project-based agent directories
    for project_skills_dir in _get_project_agent_paths():
        for skill_path in sorted(project_skills_dir.iterdir()):
            if skill_path.is_dir():
                agents.append(
                    {
                        "name": skill_path.name,
                        "source": "project",
                        "path": str(skill_path),
                        "type": "skill",
                    }
                )

    return agents


def _fetch_hermes_agents(
    host: str = "127.0.0.1",
    port: int = 9119,
    timeout: float = 0.5,
    scheme: str = "http",
) -> list[dict[str, Any]]:
    """Fetch running agents from Hermes dashboard /api/agents endpoint.

    Returns list of agent metadata from live Hermes instance.
    """
    agents = []
    try:
        base_url = f"{scheme}://{host}:{port}"
        request = urllib.request.Request(
            f"{base_url}/api/agents",
            headers={"Accept": "application/json", "User-Agent": "hermes-webui-agents-probe"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if getattr(response, "status", None) != 200:
                return []
            payload = json.loads(response.read().decode("utf-8"))

        # Handle both list and dict responses
        if isinstance(payload, list):
            agents = payload
        elif isinstance(payload, dict) and "agents" in payload:
            agents = payload.get("agents", [])
        else:
            agents = []

    except Exception:
        logger.debug("Hermes agents endpoint fetch failed", exc_info=True)

    return agents


def get_all_agents(
    host: str = "127.0.0.1",
    port: int = 9119,
    timeout: float = 0.5,
    scheme: str = "http",
) -> dict[str, Any]:
    """Return aggregated agents from all sources.

    Returns dict with:
    - local_agents: agents from filesystem
    - hermes_agents: agents from running dashboard
    - total_count: total agent count
    - sources: what sources were queried
    """
    local_agents = _scan_local_agents()
    hermes_agents = _fetch_hermes_agents(host, port, timeout, scheme)

    return {
        "local_agents": local_agents,
        "hermes_agents": hermes_agents,
        "total_count": len(local_agents) + len(hermes_agents),
        "sources": ["filesystem", "hermes-dashboard"],
    }
