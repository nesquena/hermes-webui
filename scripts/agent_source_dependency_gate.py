#!/usr/bin/env python3
"""agent_source_dependency_gate.py — stop NEW hermes-agent source coupling from landing.

Issue #2491 (slice 2 of #2453). ``audit_agent_source_dependencies.py`` enumerates
every place the WebUI couples to the hermes-agent source tree. On its own it is a
report, not a guard: the existing tests only assert that findings *exist*, so a
later PR can add coupling — a new ``from tools.registry import registry`` — and CI
stays green while the residual surface silently grows.

This gate turns the audit into a ratchet:

  * a committed baseline (``docs/architecture/agent-source-dependency-baseline.json``)
    records today's residual coupling, deliberately split by dependency class;
  * findings are keyed by ``path::anchor`` and NEVER by line number, so ordinary
    edits that shift imports down a file are not mistaken for new coupling;
  * any coupling site NOT in the baseline is NEW → exit 1;
  * a baseline entry with no matching finding has been RESOLVED → progress, reported
    but tolerated (exit 0). Shrinking the residual surface is the entire goal of
    this issue, so it must never block a PR;
  * to record progress, re-baseline deliberately with ``--update``.

Usage:
  python3 scripts/agent_source_dependency_gate.py [/path/to/webui/checkout] [--update]

Exit 0 = no new coupling. Exit 1 = new coupling landed (see the report). DO NOT
TAG/RELEASE on non-zero.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


BASELINE_RELPATH = Path("docs/architecture/agent-source-dependency-baseline.json")
AUDIT_RELPATH = Path("scripts/audit_agent_source_dependencies.py")


def _repo_root(argv: list[str]) -> Path:
    positional = [value for value in argv[1:] if not value.startswith("--")]
    if positional:
        return Path(positional[0]).resolve()
    return Path(__file__).resolve().parent.parent


def _load_audit_module(root: Path):
    script = root / AUDIT_RELPATH
    spec = importlib.util.spec_from_file_location("audit_agent_source_dependencies", script)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load audit module at {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def current_anchors(root: Path) -> dict[str, set[str]]:
    """Map each dependency class to its ``path::anchor`` coupling sites."""
    module = _load_audit_module(root)
    report = module.build_report(root)
    classes: dict[str, set[str]] = {}
    for dep_class in report["dependency_classes"]:
        anchors = {
            f"{finding['path']}::{finding['anchor']}"
            for finding in dep_class["findings"]
        }
        classes[str(dep_class["id"])] = anchors
    return classes


def _load_baseline(path: Path) -> dict[str, set[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(class_id): {str(anchor) for anchor in anchors}
        for class_id, anchors in payload["dependency_classes"].items()
    }


def _write_baseline(path: Path, anchors: dict[str, set[str]]) -> None:
    payload = {
        "schema_version": 1,
        "description": (
            "Frozen inventory of the WebUI's residual hermes-agent source-tree coupling "
            "(issue #2491). Findings are keyed path::anchor — never line numbers — so edits "
            "that shift imports are not misread as new coupling. Regenerate deliberately "
            "with: python3 scripts/agent_source_dependency_gate.py --update"
        ),
        "dependency_classes": {
            class_id: sorted(anchors[class_id]) for class_id in sorted(anchors)
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _class_title(class_id: str) -> str:
    return class_id.replace("_", " ")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_root", nargs="?", type=Path, default=None)
    parser.add_argument(
        "--update",
        action="store_true",
        help="Rewrite the baseline from the current tree (records progress).",
    )
    args = parser.parse_args(raw_argv)

    root = args.repo_root.resolve() if args.repo_root else _repo_root(raw_argv)
    baseline_path = root / BASELINE_RELPATH

    try:
        current = current_anchors(root)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"agent-source dependency gate: audit failed: {exc}", file=sys.stderr)
        return 2

    if args.update:
        _write_baseline(baseline_path, current)
        total = sum(len(value) for value in current.values())
        print(f"agent-source dependency gate: baseline updated ({total} coupling sites)")
        return 0

    if not baseline_path.is_file():
        print(
            f"agent-source dependency gate: baseline missing at {baseline_path}.\n"
            "Create it with: python3 scripts/agent_source_dependency_gate.py --update",
            file=sys.stderr,
        )
        return 2

    baseline = _load_baseline(baseline_path)

    new_sites: dict[str, list[str]] = {}
    resolved_sites: dict[str, list[str]] = {}
    for class_id, anchors in current.items():
        recorded = baseline.get(class_id, set())
        added = sorted(anchors - recorded)
        if added:
            new_sites[class_id] = added
        gone = sorted(recorded - anchors)
        if gone:
            resolved_sites[class_id] = gone

    resolved_total = sum(len(value) for value in resolved_sites.values())
    if resolved_total:
        print(
            f"agent-source dependency gate: {resolved_total} coupling site(s) resolved "
            "since the baseline was recorded (progress — re-run with --update to record):"
        )
        for class_id, gone in sorted(resolved_sites.items()):
            for anchor in gone:
                print(f"  resolved  [{_class_title(class_id)}]  {anchor}")

    if not new_sites:
        print(
            "agent-source dependency gate: OK — no new hermes-agent source coupling "
            f"({sum(len(value) for value in current.values())} baseline sites, "
            f"{resolved_total} resolved)"
        )
        return 0

    total_new = sum(len(value) for value in new_sites.values())
    print(
        f"agent-source dependency gate: FAIL — {total_new} NEW hermes-agent source "
        "coupling site(s) not present in the #2491 baseline.",
        file=sys.stderr,
    )
    for class_id, added in sorted(new_sites.items()):
        print(f"  new coupling  [{_class_title(class_id)}]", file=sys.stderr)
        for anchor in added:
            print(f"    {anchor}", file=sys.stderr)
    print(
        "\nEach new site re-couples the WebUI to the agent source tree and blocks the "
        "#2491 goal of removing the hermes-agent-src mount. Either route the call "
        "through a documented agent endpoint, or — if the coupling is deliberate and "
        "inherent (single-container in-process operation, for example) — record it "
        "deliberately with:\n"
        "    python3 scripts/agent_source_dependency_gate.py --update",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())