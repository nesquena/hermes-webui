#!/usr/bin/env python3
"""Audit WebUI-managed Git worktrees without mutating repository state."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, TextIO


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.worktree_gc_inventory import (  # noqa: E402
    audit_managed_worktrees,
    write_report_atomic,
)


def default_state_dir(environ: dict[str, str] | os._Environ[str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    explicit = env.get("HERMES_WEBUI_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    hermes_home = env.get("HERMES_HOME")
    base = Path(hermes_home).expanduser() if hermes_home else Path.home() / ".hermes"
    return base / "webui"


def default_health_url(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> str:
    env = environ if environ is not None else os.environ
    port = str(env.get("HERMES_WEBUI_PORT") or "8787").strip()
    return f"http://127.0.0.1:{port}/health"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError:
        return False
    return True


def default_report_path(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> Path:
    """Prefer durable user state, with temporary storage as the last fallback."""
    env = environ if environ is not None else os.environ
    candidates: list[Path] = []
    xdg_state_home = str(env.get("XDG_STATE_HOME") or "").strip()
    if xdg_state_home:
        xdg_path = Path(xdg_state_home).expanduser()
        if xdg_path.is_absolute():
            candidates.append(
                xdg_path / "hermes-webui" / "worktree-gc" / "report.json"
            )
    candidates.extend(
        (
            Path.home()
            / ".local"
            / "state"
            / "hermes-webui"
            / "worktree-gc"
            / "report.json",
            Path(tempfile.gettempdir())
            / "hermes-webui-worktree-gc"
            / "report.json",
        )
    )
    for candidate in candidates:
        if not _is_within(candidate, REPO_ROOT):
            return candidate
    raise RuntimeError("could not choose a report path outside the repository")


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit WebUI-managed worktrees in observation-only mode.",
    )
    parser.add_argument("--profile", default="default")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--min-age-days", type=_positive_int, default=7)
    parser.add_argument("--target-ref", default="origin/master")
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--health-url", default=default_health_url())
    parser.add_argument("--report-path", type=Path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="audit only (the only supported mode)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="write the complete report as JSON to stdout",
    )
    return parser


def load_git_backend() -> Any:
    """Import the read-only Git classifier only when the CLI actually runs."""
    return importlib.import_module("api.worktree_gc_git")


def _emit_summary(
    report: dict[str, Any],
    report_path: Path,
    *,
    as_json: bool,
    stdout: TextIO,
) -> None:
    if as_json:
        json.dump(
            report,
            stdout,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        stdout.write("\n")
        return
    counts = report.get("counts") or {}
    stdout.write(
        f"mode={report.get('mode')} "
        f"candidates={int(counts.get('candidates') or 0)} "
        f"eligible={int(counts.get('eligible') or 0)} "
        f"blocking={int(counts.get('blocking') or 0)} "
        f"report={report_path}\n"
    )


def _audit_exit_code(report: dict[str, Any]) -> int:
    return 2 if report.get("has_blocking_anomalies", False) else 0


def main(
    argv: list[str] | None = None,
    *,
    git_backend: Any = None,
    audit_fn: Callable[..., dict[str, Any]] = audit_managed_worktrees,
    stdout: TextIO | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output = stdout if stdout is not None else sys.stdout
    backend = git_backend if git_backend is not None else load_git_backend()
    report_path = (
        args.report_path.expanduser()
        if args.report_path is not None
        else default_report_path()
    )
    report = audit_fn(
        state_dir=args.state_dir,
        profile=args.profile,
        repo_filter=args.repo,
        min_age_days=args.min_age_days,
        target_ref=args.target_ref,
        git_backend=backend,
        health_url=args.health_url,
    )
    report["mode"] = "dry-run"
    report["collection_requested"] = False
    write_report_atomic(report, report_path)
    _emit_summary(
        report,
        report_path,
        as_json=args.json,
        stdout=output,
    )
    return _audit_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
