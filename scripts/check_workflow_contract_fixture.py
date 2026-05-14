#!/usr/bin/env python3
"""Check that WebUI's copied workflow API fixture matches Hermes Core.

Core owns the canonical JSON fixture at docs/contracts/workflow-api-v1.fixture.json.
WebUI keeps a checked-in copy because docs/* is ignored in this repo. This
script is the cheap guardrail that fails when the WebUI copy drifts.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WEBUI_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "workflow-api-v1.fixture.json"
DEFAULT_CORE_FIXTURE_CANDIDATES = (
    Path.home() / ".hermes" / "hermes-agent" / "docs" / "contracts" / "workflow-api-v1.fixture.json",
    REPO_ROOT.parent / "hermes-agent" / "docs" / "contracts" / "workflow-api-v1.fixture.json",
)


@dataclass(frozen=True)
class FixtureSyncResult:
    ok: bool
    message: str
    core_path: Path
    webui_path: Path


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_core_fixture(explicit_path: Path | None) -> Path:
    if explicit_path is not None:
        return explicit_path.expanduser().resolve()
    for candidate in DEFAULT_CORE_FIXTURE_CANDIDATES:
        if candidate.exists():
            return candidate.resolve()
    searched = ", ".join(str(path) for path in DEFAULT_CORE_FIXTURE_CANDIDATES)
    raise FileNotFoundError(
        "Could not find Hermes Core workflow contract fixture. "
        f"Pass --core-fixture explicitly. Searched: {searched}"
    )


def check_fixture_sync(core_path: Path, webui_path: Path = DEFAULT_WEBUI_FIXTURE) -> FixtureSyncResult:
    """Return whether the WebUI fixture copy matches Core's canonical JSON."""

    core_path = core_path.expanduser().resolve()
    webui_path = webui_path.expanduser().resolve()
    core_fixture = _load_json(core_path)
    webui_fixture = _load_json(webui_path)
    if core_fixture != webui_fixture:
        return FixtureSyncResult(
            ok=False,
            message=(
                "WebUI workflow contract fixture is stale. "
                f"Copy {core_path} to {webui_path}."
            ),
            core_path=core_path,
            webui_path=webui_path,
        )
    return FixtureSyncResult(
        ok=True,
        message="Workflow contract fixtures are in sync.",
        core_path=core_path,
        webui_path=webui_path,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--core-fixture",
        type=Path,
        default=None,
        help="Path to Hermes Core docs/contracts/workflow-api-v1.fixture.json.",
    )
    parser.add_argument(
        "--webui-fixture",
        type=Path,
        default=DEFAULT_WEBUI_FIXTURE,
        help="Path to WebUI's copied workflow API fixture.",
    )
    args = parser.parse_args(argv)

    try:
        core_path = _resolve_core_fixture(args.core_fixture)
        result = check_fixture_sync(core_path, args.webui_fixture)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    stream = sys.stdout if result.ok else sys.stderr
    print(result.message, file=stream)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
