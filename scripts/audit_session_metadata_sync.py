#!/usr/bin/env python3
"""Offline aggregate audit for lifecycle metadata drift (issue 498 shadow slice).

Aggregate-only, read-only, fail-closed. Never outputs IDs, titles, prompts,
transcript, or message content. Requires explicit session-dir and
state-db to avoid touching live user state; help must not mention
apply/yes flags.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.session_metadata_sync import compute_aggregate_diagnostics


def _nonempty_path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise argparse.ArgumentTypeError("must be non-empty and not whitespace-only")
    return Path(value)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="audit_session_metadata_sync.py",
        description=(
            "Aggregate-only, read-only audit for lifecycle drift between "
            "WebUI JSON sidecars and agent state.db (pinned/archived). "
            "Emits only aggregate counts and profile names; never IDs, titles, prompts, or transcript. "
            "Requires explicit --session-dir and --state-db to avoid live state. "
            "Default output is aggregate-only human-readable text; --json emits machine-readable aggregate JSON."
        ),
    )
    p.add_argument("--session-dir", required=True, type=_nonempty_path, help="Fixture/test session directory (explicit, fail-closed)")
    p.add_argument("--state-db", required=True, type=_nonempty_path, help="Fixture/test state.db path (explicit, fail-closed)")
    p.add_argument("--profile", required=True, help="Profile name to scope sidecars (non-empty, fail-closed)")
    p.add_argument("--json", action="store_true", help="Emit JSON to stdout")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    profile = args.profile
    if not isinstance(profile, str) or not profile.strip():
        print("error: --profile is required and must be non-empty", file=sys.stderr)
        return 2
    diag = compute_aggregate_diagnostics(args.session_dir, args.state_db, profile)
    if args.json:
        json.dump(diag, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        for k in ("profile", "total_lineages", "matched", "sidecar_only", "core_only", "blocked"):
            sys.stdout.write(f"{k}: {json.dumps(diag.get(k), sort_keys=True)}\n")
        sys.stdout.write(f"pinned_mismatch: {json.dumps(diag.get('pinned_mismatch'), sort_keys=True)}\n")
        sys.stdout.write(f"archived_mismatch: {json.dumps(diag.get('archived_mismatch'), sort_keys=True)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
