#!/usr/bin/env python3
"""Record and check raw lifecycle step outcomes, before soft job policy.

Certificates must belong to this workflow run, commit and attempt. Missing or
stale evidence never inherits success from a softened matrix needs.result.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROW_STEPS = {
    "normal": ("gate", "settle_frame", "missing_terminal"),
    "terminal-error": ("gate",),
    "historical-transcript-hydration": ("gate",),
    "reconnect-scene-redraw": ("gate",),
}
OUTCOME_ENV = {
    "gate": "GATE_OUTCOME",
    "settle_frame": "SETTLE_FRAME_OUTCOME",
    "missing_terminal": "MISSING_TERMINAL_OUTCOME",
}
IDENTITY_ENV = {
    "run_id": "GITHUB_RUN_ID",
    "run_attempt": "GITHUB_RUN_ATTEMPT",
    "sha": "GITHUB_SHA",
}
MAX_CERTIFICATE_BYTES = 8192


def _identity() -> dict[str, str]:
    identity = {key: os.environ.get(env, "") for key, env in IDENTITY_ENV.items()}
    if not all(identity.values()):
        raise ValueError("Current workflow run, attempt and commit are required")
    return identity


def record(directory: Path, row: str) -> None:
    payload = {
        "version": 1,
        "row": row,
        **_identity(),
        "steps": {
            step: os.environ.get(OUTCOME_ENV[step], "") for step in ROW_STEPS[row]
        },
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{row}.json").write_text(json.dumps(payload), encoding="utf-8")


def _row_failures(directory: Path, row: str, identity: dict[str, str]) -> list[str]:
    path = directory / f"{row}.json"
    try:
        with path.open("rb") as source:
            data = source.read(MAX_CERTIFICATE_BYTES + 1)
        if len(data) > MAX_CERTIFICATE_BYTES:
            return ["oversized result"]
        payload = json.loads(data)
    except (OSError, ValueError, UnicodeError):
        return ["missing or unreadable result"]
    if not isinstance(payload, dict):
        return ["invalid result schema"]
    if type(payload.get("version")) is not int or payload["version"] != 1:
        return ["invalid result version"]
    if payload.get("row") != row or any(
        payload.get(key) != val for key, val in identity.items()
    ):
        return ["stale or wrong-owner result"]
    steps = payload.get("steps")
    if not isinstance(steps, dict) or set(steps) != set(ROW_STEPS[row]):
        return ["missing or unexpected proof steps"]
    failures = []
    for step in ROW_STEPS[row]:
        result = steps[step]
        if result != "success":
            label = (
                result
                if isinstance(result, str)
                and result in {"failure", "cancelled", "skipped"}
                else "unproven"
            )
            failures.append(f"{step}: {label}")
    return failures


def check(directory: Path) -> bool:
    identity = _identity()
    rows = [(row, _row_failures(directory, row, identity)) for row in ROW_STEPS]
    mutation_ok = os.environ.get("MUTATION_RESULT") == "success"
    report = [
        "## Conversation lifecycle proof",
        "",
        "| Component | Raw proof |",
        "| --- | --- |",
    ]
    for row, failures in rows:
        report.append(f"| {row} | {', '.join(failures) if failures else 'success'} |")
    report.append(
        f"| Mutation canaries | {'success' if mutation_ok else 'not successful'} |"
    )
    text = "\n".join(report) + "\n"
    print(text, end="")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as dest:
            dest.write(text)
    return mutation_ok and all(not failures for _, failures in rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("record", "check"))
    parser.add_argument("--row", choices=tuple(ROW_STEPS))
    args = parser.parse_args()
    directory = Path(os.environ.get("LIFECYCLE_RESULTS_DIR", "lifecycle-results"))
    if args.mode == "record":
        if args.row is None:
            parser.error("record requires --row")
        record(directory, args.row)
        return 0
    return 0 if check(directory) else 1


if __name__ == "__main__":
    raise SystemExit(main())
