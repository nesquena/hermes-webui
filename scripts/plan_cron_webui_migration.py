#!/usr/bin/env python3
"""Plan/apply additive Hermex/WebUI cron delivery migration.

Adds the profile-local WebUI inbox target (`webui`) in front of existing push
cron delivery targets while preserving existing Telegram/origin fallback. Local
jobs stay local by default.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

WEBUI_ALIASES = {"webui", "hermex"}
LOCAL_TOKENS = {"", "local"}


@dataclass
class ProfileCronFile:
    profile: str
    home: Path
    path: Path


@dataclass
class DeliveryChange:
    profile: str
    job_id: str
    name: str
    old_deliver: Any
    new_deliver: Any
    reason: str


@dataclass
class ProfileReport:
    profile: str
    path: str
    status: str
    total_jobs: int = 0
    changed_jobs: int = 0
    unchanged_jobs: int = 0
    local_jobs: int = 0


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def discover_profile_files(home: Path) -> list[ProfileCronFile]:
    home = home.expanduser().resolve()
    rows = [ProfileCronFile("default", home, home / "cron" / "jobs.json")]
    profiles_dir = home / "profiles"
    if profiles_dir.is_dir():
        for child in sorted(profiles_dir.iterdir(), key=lambda p: p.name):
            if child.is_dir() and child.name:
                rows.append(ProfileCronFile(child.name, child, child / "cron" / "jobs.json"))
    return rows


def load_jobs_file(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected top-level object")
    jobs = data.get("jobs")
    if jobs is None:
        jobs = []
        data["jobs"] = jobs
    if not isinstance(jobs, list):
        raise ValueError(f"{path}: expected jobs list")
    for idx, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise ValueError(f"{path}: jobs[{idx}] is not an object")
    return data, jobs


def split_deliver(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value).split(",")
    tokens: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in tokens:
            tokens.append(text)
    return tokens


def deliver_has_webui(tokens: Iterable[str]) -> bool:
    return any(str(token).strip().lower() in WEBUI_ALIASES for token in tokens)


def deliver_is_local_only(tokens: list[str]) -> bool:
    if not tokens:
        return True
    return len(tokens) == 1 and tokens[0].strip().lower() in LOCAL_TOKENS


def add_webui_delivery(value: Any, *, include_local: bool = False) -> tuple[Any, str | None]:
    tokens = split_deliver(value)
    if deliver_has_webui(tokens):
        return value, None
    if deliver_is_local_only(tokens) and not include_local:
        return value, "local"
    if not tokens:
        tokens = ["local"]
    new_tokens = ["webui"] + tokens
    if isinstance(value, list):
        return new_tokens, "additive"
    return ",".join(new_tokens), "additive"


def job_label(job: dict[str, Any]) -> str:
    return str(job.get("name") or job.get("id") or "unnamed")


def plan_profile(profile_file: ProfileCronFile, *, include_local: bool = False) -> tuple[ProfileReport, list[DeliveryChange], dict[str, Any] | None]:
    path = profile_file.path
    if not path.exists():
        return ProfileReport(profile_file.profile, str(path), "missing"), [], None
    data, jobs = load_jobs_file(path)
    planned = copy.deepcopy(data)
    planned_jobs = planned.get("jobs") or []
    changes: list[DeliveryChange] = []
    local_jobs = 0
    unchanged = 0
    for job in planned_jobs:
        old = job.get("deliver", "local")
        new, reason = add_webui_delivery(old, include_local=include_local)
        if reason == "local":
            local_jobs += 1
            unchanged += 1
            continue
        if reason is None:
            unchanged += 1
            continue
        if new != old:
            job["deliver"] = new
            changes.append(
                DeliveryChange(
                    profile=profile_file.profile,
                    job_id=str(job.get("id") or ""),
                    name=job_label(job),
                    old_deliver=old,
                    new_deliver=new,
                    reason=reason,
                )
            )
        else:
            unchanged += 1
    report = ProfileReport(
        profile=profile_file.profile,
        path=str(path),
        status="ok",
        total_jobs=len(jobs),
        changed_jobs=len(changes),
        unchanged_jobs=unchanged,
        local_jobs=local_jobs,
    )
    return report, changes, planned if changes else data


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def backup_file(source: Path, backup_dir: Path, profile: str) -> Path:
    target = backup_dir / profile / "cron" / "jobs.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def build_plan(home: Path, *, include_local: bool = False) -> tuple[list[ProfileReport], list[DeliveryChange], dict[str, dict[str, Any]]]:
    reports: list[ProfileReport] = []
    changes: list[DeliveryChange] = []
    planned_by_profile: dict[str, dict[str, Any]] = {}
    for profile_file in discover_profile_files(home):
        report, profile_changes, planned = plan_profile(profile_file, include_local=include_local)
        reports.append(report)
        changes.extend(profile_changes)
        if profile_changes and planned is not None:
            planned_by_profile[profile_file.profile] = planned
    return reports, changes, planned_by_profile


def print_report(reports: list[ProfileReport], changes: list[DeliveryChange], *, as_json: bool) -> None:
    payload = {
        "summary": {
            "profiles": len(reports),
            "changed_jobs": len(changes),
            "local_jobs_unchanged": sum(r.local_jobs for r in reports),
        },
        "profiles": [asdict(r) for r in reports],
        "changes": [asdict(c) for c in changes],
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"Hermex/WebUI cron migration plan: {len(changes)} job(s) would gain webui")
    for report in reports:
        if report.status == "missing":
            print(f"- {report.profile}: no cron/jobs.json ({report.path})")
        else:
            print(
                f"- {report.profile}: {report.changed_jobs}/{report.total_jobs} change(s), "
                f"{report.local_jobs} local-only unchanged"
            )
    if changes:
        print("\nProposed delivery changes:")
        for change in changes:
            print(
                f"- {change.profile}:{change.job_id} — {change.name}: "
                f"{change.old_deliver!r} -> {change.new_deliver!r}"
            )


def apply_plan(home: Path, backup_dir: Path, reports: list[ProfileReport], changes: list[DeliveryChange], planned_by_profile: dict[str, dict[str, Any]]) -> dict[str, Any]:
    backup_dir = backup_dir.expanduser().resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    profile_files = {row.profile: row for row in discover_profile_files(home)}
    backups: list[dict[str, str]] = []
    for profile in planned_by_profile:
        source = profile_files[profile].path
        backup_path = backup_file(source, backup_dir, profile)
        backups.append({"profile": profile, "source": str(source), "backup": str(backup_path)})
    manifest = {
        "created_at": utc_now(),
        "home": str(home.expanduser().resolve()),
        "changes": [asdict(c) for c in changes],
        "reports": [asdict(r) for r in reports],
        "backups": backups,
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for profile, planned in planned_by_profile.items():
        atomic_write_json(profile_files[profile].path, planned)
    return manifest


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan/apply additive webui delivery migration for Hermes cron jobs.")
    parser.add_argument("--home", default=os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")), help="Base Hermes home containing cron/jobs.json and profiles/. Default: $HERMES_HOME or ~/.hermes")
    parser.add_argument("--apply", action="store_true", help="Write proposed delivery changes. Dry-run is the default.")
    parser.add_argument("--backup-dir", help="Required with --apply. A copy of every affected jobs.json is written here before mutation.")
    parser.add_argument("--include-local", action="store_true", help="Also add webui to local-only jobs. Default leaves local jobs unchanged.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON plan/report.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    home = Path(args.home).expanduser().resolve()
    reports, changes, planned_by_profile = build_plan(home, include_local=args.include_local)
    if args.apply and not args.backup_dir:
        print("ERROR: --apply requires --backup-dir", file=sys.stderr)
        return 2
    print_report(reports, changes, as_json=args.json)
    if args.apply:
        manifest = apply_plan(home, Path(args.backup_dir), reports, changes, planned_by_profile)
        if args.json:
            # Keep stdout JSON parseable for dry-run plans; apply receipt goes to stderr.
            print(json.dumps({"applied": True, "manifest": str(Path(args.backup_dir).expanduser().resolve() / "manifest.json")}), file=sys.stderr)
        else:
            print(f"\nApplied {len(changes)} change(s). Backup manifest: {Path(args.backup_dir).expanduser().resolve() / 'manifest.json'}")
            for item in manifest["backups"]:
                print(f"- backup {item['profile']}: {item['backup']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
