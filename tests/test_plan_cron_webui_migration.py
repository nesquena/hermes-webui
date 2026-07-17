"""Tests for scripts/plan_cron_webui_migration.py."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

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
