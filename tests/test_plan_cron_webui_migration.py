"""Tests for scripts/plan_cron_webui_migration.py."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "plan_cron_webui_migration.py"
spec = importlib.util.spec_from_file_location("plan_cron_webui_migration", SCRIPT)
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def _write_jobs(path: Path, jobs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": jobs, "updated_at": "2026-01-01T00:00:00Z"}, indent=2) + "\n", encoding="utf-8")


def _sample_home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes"
    _write_jobs(home / "cron" / "jobs.json", [
        {"id": "default-origin", "name": "Default origin", "deliver": "origin", "prompt": "keep"},
        {"id": "default-local", "name": "Default local", "deliver": "local", "prompt": "keep"},
        {"id": "already", "name": "Already migrated", "deliver": "webui,origin", "prompt": "keep"},
    ])
    _write_jobs(home / "profiles" / "newsletteros" / "cron" / "jobs.json", [
        {"id": "telegram", "name": "Telegram", "deliver": "telegram:123", "prompt": "keep"},
        {"id": "list", "name": "List", "deliver": ["origin"], "prompt": "keep"},
    ])
    (home / "profiles" / "emptyprofile").mkdir(parents=True)
    return home


def test_dry_run_adds_webui_without_writing_or_touching_local(tmp_path):
    home = _sample_home(tmp_path)
    before = (home / "cron" / "jobs.json").read_text(encoding="utf-8")

    reports, changes, planned = mod.build_plan(home)

    assert len(changes) == 3
    observed = {
        (c.profile, c.job_id, json.dumps(c.old_deliver, sort_keys=True), json.dumps(c.new_deliver, sort_keys=True))
        for c in changes
    }
    assert observed == {
        ("default", "default-origin", '"origin"', '"webui,origin"'),
        ("newsletteros", "telegram", '"telegram:123"', '"webui,telegram:123"'),
        ("newsletteros", "list", '["origin"]', '["webui", "origin"]'),
    }
    assert any(r.profile == "emptyprofile" and r.status == "missing" for r in reports)
    assert planned["default"]["jobs"][0]["prompt"] == "keep"
    assert (home / "cron" / "jobs.json").read_text(encoding="utf-8") == before


def test_apply_requires_backup_dir(tmp_path):
    home = _sample_home(tmp_path)
    assert mod.main(["--home", str(home), "--apply"]) == 2
    data = json.loads((home / "cron" / "jobs.json").read_text(encoding="utf-8"))
    assert data["jobs"][0]["deliver"] == "origin"


def test_apply_writes_only_delivery_changes_and_backups(tmp_path):
    home = _sample_home(tmp_path)
    backup_dir = tmp_path / "backup"
    default_path = home / "cron" / "jobs.json"
    before = json.loads(default_path.read_text(encoding="utf-8"))
    before_copy = copy.deepcopy(before)

    assert mod.main(["--home", str(home), "--apply", "--backup-dir", str(backup_dir)]) == 0

    after = json.loads(default_path.read_text(encoding="utf-8"))
    assert after["jobs"][0]["deliver"] == "webui,origin"
    assert after["jobs"][1]["deliver"] == "local"
    assert after["jobs"][2]["deliver"] == "webui,origin"
    # Non-delivery fields and top-level metadata remain logically unchanged.
    comparable_before = copy.deepcopy(before_copy)
    comparable_before["jobs"][0]["deliver"] = "webui,origin"
    assert after == comparable_before

    backed_up = json.loads((backup_dir / "default" / "cron" / "jobs.json").read_text(encoding="utf-8"))
    assert backed_up == before_copy
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["changes"]) == 3
    assert {item["profile"] for item in manifest["backups"]} == {"default", "newsletteros"}


def test_profile_symlink_is_rejected_before_plan_or_apply(tmp_path):
    home = tmp_path / "hermes"
    profiles_dir = home / "profiles"
    profiles_dir.mkdir(parents=True)
    outside = tmp_path / "outside-profile"
    outside_jobs = outside / "cron" / "jobs.json"
    _write_jobs(
        outside_jobs,
        [{"id": "outside", "deliver": "origin", "prompt": "preserve"}],
    )
    before = outside_jobs.read_bytes()
    (profiles_dir / "escaped").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked profile directory rejected"):
        mod.build_plan(home)

    assert outside_jobs.read_bytes() == before


def test_home_symlink_is_rejected_before_resolution(tmp_path):
    real_home = _sample_home(tmp_path)
    linked_home = tmp_path / "linked-hermes"
    linked_home.symlink_to(real_home, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked path component rejected"):
        mod.build_plan(linked_home)


def test_cron_symlink_is_rejected_before_plan_or_apply(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    outside_cron = tmp_path / "outside-cron"
    outside_jobs = outside_cron / "jobs.json"
    _write_jobs(
        outside_jobs,
        [{"id": "outside", "deliver": "origin", "prompt": "preserve"}],
    )
    before = outside_jobs.read_bytes()
    (home / "cron").symlink_to(outside_cron, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked path component rejected"):
        mod.build_plan(home)

    assert outside_jobs.read_bytes() == before


def test_apply_rejects_cron_swap_after_descriptor_open(tmp_path, monkeypatch):
    home = _sample_home(tmp_path)
    reports, changes, planned = mod.build_plan(home)
    original_path = home / "cron" / "jobs.json"
    original_before = original_path.read_bytes()
    outside_cron = tmp_path / "outside-cron"
    outside_jobs = outside_cron / "jobs.json"
    _write_jobs(
        outside_jobs,
        [{"id": "outside", "deliver": "origin", "prompt": "preserve"}],
    )
    outside_before = outside_jobs.read_bytes()
    real_backup = mod.backup_file_from_fd
    swapped = False

    def swap_then_backup(source_fd, backup_dir, profile):
        nonlocal swapped
        if profile == "default" and not swapped:
            swapped = True
            (home / "cron").rename(home / "cron-before-swap")
            (home / "cron").symlink_to(outside_cron, target_is_directory=True)
        return real_backup(source_fd, backup_dir, profile)

    monkeypatch.setattr(mod, "backup_file_from_fd", swap_then_backup)

    with pytest.raises((OSError, ValueError)):
        mod.apply_plan(home, tmp_path / "backup", reports, changes, planned)

    assert swapped is True
    assert outside_jobs.read_bytes() == outside_before
    assert (home / "cron-before-swap" / "jobs.json").read_bytes() == original_before


def test_apply_rejects_jobs_changed_after_plan(tmp_path):
    home = _sample_home(tmp_path)
    reports, changes, planned = mod.build_plan(home)
    jobs_path = home / "cron" / "jobs.json"
    current = json.loads(jobs_path.read_text(encoding="utf-8"))
    current["jobs"].append(
        {
            "id": "concurrent-job",
            "name": "Concurrent job",
            "deliver": "local",
            "prompt": "must survive",
        }
    )
    jobs_path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    changed_bytes = jobs_path.read_bytes()

    with pytest.raises(ValueError, match="jobs contents changed after planning"):
        mod.apply_plan(home, tmp_path / "backup", reports, changes, planned)

    assert jobs_path.read_bytes() == changed_bytes


def test_canonical_writer_waits_and_preserves_both_changes(
    tmp_path, monkeypatch
):
    if mod.fcntl is None:
        pytest.skip("POSIX flock required")
    home = _sample_home(tmp_path)
    reports, changes, planned = mod.build_plan(home)
    jobs_path = home / "cron" / "jobs.json"
    migration_has_lock = threading.Event()
    writer_done = threading.Event()
    real_backup = mod.backup_file_from_fd

    def signal_after_lock(source_fd, backup_dir, profile):
        if profile == "default":
            migration_has_lock.set()
        return real_backup(source_fd, backup_dir, profile)

    def canonical_writer():
        assert migration_has_lock.wait(timeout=5)
        lock_path = home / "cron" / mod.JOBS_LOCK_FILE
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            mod.fcntl.flock(lock_file.fileno(), mod.fcntl.LOCK_EX)
            current = json.loads(jobs_path.read_text(encoding="utf-8"))
            current["jobs"].append(
                {
                    "id": "concurrent-job",
                    "name": "Concurrent job",
                    "deliver": "local",
                    "prompt": "must survive",
                }
            )
            temp_path = home / "cron" / ".writer-jobs.tmp"
            temp_path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
            temp_path.replace(jobs_path)
            mod.fcntl.flock(lock_file.fileno(), mod.fcntl.LOCK_UN)
        writer_done.set()

    monkeypatch.setattr(mod, "backup_file_from_fd", signal_after_lock)
    writer = threading.Thread(target=canonical_writer)
    writer.start()
    try:
        mod.apply_plan(home, tmp_path / "backup", reports, changes, planned)
        assert writer_done.wait(timeout=5)
    finally:
        writer.join(timeout=5)

    final = json.loads(jobs_path.read_text(encoding="utf-8"))
    by_id = {job["id"]: job for job in final["jobs"]}
    assert by_id["default-origin"]["deliver"] == "webui,origin"
    assert by_id["concurrent-job"]["prompt"] == "must survive"
