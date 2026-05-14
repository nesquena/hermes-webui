"""Workflow API contract fixture loader for WebUI tests.

The fixture is copied from Hermes Core's canonical workflow contract fixture.
WebUI tests use it to pin the browser-facing field names it consumes.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

WORKFLOW_API_CONTRACT_VERSION = "workflow-api-v1"


def workflow_contract_fixture_path() -> Path:
    return Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "workflow-api-v1.fixture.json"


def load_workflow_api_contract_fixture() -> dict[str, Any]:
    fixture = json.loads(workflow_contract_fixture_path().read_text(encoding="utf-8"))
    if fixture.get("contractVersion") != WORKFLOW_API_CONTRACT_VERSION:
        raise ValueError(
            f"unsupported workflow API contract version: {fixture.get('contractVersion')!r}"
        )
    return deepcopy(fixture)
