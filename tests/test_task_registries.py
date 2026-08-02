from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from api import routes, task_registries
from api.task_registries import (
    RegistryConflict,
    RegistryValidationError,
    TaskRegistryStore,
)


def _task(task_id: str | None = None) -> dict:
    now = "2026-08-02T12:00:00+03:00"
    return {
        "id": task_id or str(uuid.uuid4()),
        "text": "Existing task",
        "status": "pending",
        "priority": "normal",
        "due_date": None,
        "due_at": None,
        "notes": None,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "cancelled_at": None,
        "history": [
            {
                "id": str(uuid.uuid4()),
                "changed_at": now,
                "action": "created",
                "changes": {"text": {"old": None, "new": "Existing task"}},
            }
        ],
    }


def _registry(tasks: list[dict] | None = None) -> dict:
    return {
        "version": 3,
        "timezone": "Europe/Moscow",
        "updated_at": "2026-08-02T12:00:00+03:00",
        "tasks": tasks or [],
    }


def _write_registry(root: Path, name: str, payload: dict) -> Path:
    path = root / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_lists_only_valid_task_registry_files(tmp_path):
    _write_registry(tmp_path, "ivan-daily-tasks.json", _registry([_task()]))
    _write_registry(tmp_path, "urbanfit-tasks.json", {**_registry(), "project": "Urbanfit"})
    _write_registry(tmp_path, "not-a-registry.json", {"items": []})
    _write_registry(tmp_path, "ignored-tasks.json", {"items": []})

    result = TaskRegistryStore(tmp_path).list_registries()

    assert [item["id"] for item in result] == ["ivan-daily", "urbanfit"]
    assert result[0]["task_count"] == 1
    assert "path" not in result[0]


def test_invalid_registry_timezone_is_rejected_before_listing(tmp_path):
    _write_registry(tmp_path, "ivan-daily-tasks.json", {**_registry(), "timezone": "Mars/Olympus"})

    assert TaskRegistryStore(tmp_path).list_registries() == []


def test_registry_root_rejects_group_or_world_writable_directory(tmp_path):
    root = tmp_path / "registries"
    root.mkdir(mode=0o770)
    root.chmod(0o770)
    _write_registry(root, "ivan-daily-tasks.json", _registry())

    with pytest.raises(task_registries.RegistryError, match="unsafe ownership or permissions"):
        TaskRegistryStore(root).list_registries()


def test_default_root_uses_request_scoped_active_profile_home(tmp_path, monkeypatch):
    profile_home = tmp_path / "profiles" / "copywriter"
    private = profile_home / "private"
    private.mkdir(parents=True)
    monkeypatch.delenv("HERMES_TASK_REGISTRY_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "wrong-process-global"))
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: profile_home)

    assert task_registries._registry_root() == private


def test_named_profile_ignores_process_global_override_and_legacy_root(tmp_path, monkeypatch):
    process_home = tmp_path / "default"
    profile_home = tmp_path / "profiles" / "copywriter"
    override = tmp_path / "shared-override"
    monkeypatch.setenv("HERMES_HOME", str(process_home))
    monkeypatch.setenv("HERMES_TASK_REGISTRY_DIR", str(override))
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: profile_home)

    assert task_registries._registry_root() == profile_home / "private"


def test_profile_resolution_failure_is_fail_closed(monkeypatch):
    monkeypatch.setattr(
        "api.profiles.get_active_hermes_home",
        lambda: (_ for _ in ()).throw(RuntimeError("profile lookup failed")),
    )

    with pytest.raises(task_registries.RegistryError, match="cannot be resolved"):
        task_registries._registry_root()


def test_registry_file_rejects_unsafe_permissions_and_hardlinks(tmp_path):
    path = _write_registry(tmp_path, "ivan-daily-tasks.json", _registry())
    path.chmod(0o660)
    assert TaskRegistryStore(tmp_path).list_registries() == []

    path.chmod(0o600)
    hardlink = tmp_path / "copy.json"
    hardlink.hardlink_to(path)
    assert TaskRegistryStore(tmp_path).list_registries() == []


def test_create_adds_uuid_history_backup_and_private_permissions(tmp_path):
    path = _write_registry(tmp_path, "ivan-daily-tasks.json", _registry())
    store = TaskRegistryStore(tmp_path)
    before = store.get_registry("ivan-daily")

    result = store.create_task(
        "ivan-daily",
        {
            "text": "New task",
            "priority": "high",
            "due_date": "2026-08-04",
            "notes": "Context",
        },
        before["revision"],
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    created = saved["tasks"][0]
    uuid.UUID(created["id"], version=4)
    uuid.UUID(created["history"][0]["id"], version=4)
    assert created["text"] == "New task"
    assert created["status"] == "pending"
    assert created["history"][0]["note"] == "Created in Hermes WebUI"
    assert result["revision"] != before["revision"]
    backups = list((tmp_path / ".task-registry-backups").glob("ivan-daily-tasks.*.json"))
    assert len(backups) == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_update_requires_current_revision(tmp_path):
    existing = _task()
    path = _write_registry(tmp_path, "ivan-daily-tasks.json", _registry([existing]))
    store = TaskRegistryStore(tmp_path)
    stale = store.get_registry("ivan-daily")["revision"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["updated_at"] = "2026-08-02T12:00:01+03:00"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RegistryConflict):
        store.update_task("ivan-daily", existing["id"], {"status": "completed"}, stale)


def test_completion_reopen_and_cancel_append_history(tmp_path):
    existing = _task()
    _write_registry(tmp_path, "ivan-daily-tasks.json", _registry([existing]))
    store = TaskRegistryStore(tmp_path)
    first = store.get_registry("ivan-daily")

    completed = store.update_task(
        "ivan-daily", existing["id"], {"status": "completed"}, first["revision"]
    )
    assert completed["task"]["completed_at"]
    assert completed["task"]["completed_at_source"] == "hermes_webui"
    assert completed["task"]["history"][-1]["action"] == "completed"

    reopened = store.update_task(
        "ivan-daily", existing["id"], {"status": "pending"}, completed["revision"]
    )
    assert reopened["task"]["completed_at"] is None
    assert reopened["task"]["history"][-1]["action"] == "reopened"

    cancelled = store.update_task(
        "ivan-daily", existing["id"], {"status": "cancelled"}, reopened["revision"]
    )
    assert cancelled["task"]["cancelled_at"]
    assert cancelled["task"]["history"][-1]["action"] == "cancelled"
    assert len(cancelled["task"]["history"]) == 4


def test_noop_update_does_not_write_or_backup(tmp_path):
    existing = _task()
    path = _write_registry(tmp_path, "ivan-daily-tasks.json", _registry([existing]))
    store = TaskRegistryStore(tmp_path)
    current = store.get_registry("ivan-daily")
    original_bytes = path.read_bytes()

    result = store.update_task(
        "ivan-daily", existing["id"], {"text": existing["text"]}, current["revision"]
    )

    assert result["changed"] is False
    assert path.read_bytes() == original_bytes
    assert not (tmp_path / ".task-registry-backups").exists()


@pytest.mark.parametrize(
    "body",
    [
        {"text": ""},
        {"text": "x", "status": "invented"},
        {"text": "x", "due_date": "04.08.2026"},
        {"text": "x", "due_at": "2026-08-04T12:00:00"},
        {"text": "x", "unknown": True},
    ],
)
def test_rejects_invalid_create_fields(tmp_path, body):
    _write_registry(tmp_path, "ivan-daily-tasks.json", _registry())
    store = TaskRegistryStore(tmp_path)
    revision = store.get_registry("ivan-daily")["revision"]

    with pytest.raises(RegistryValidationError):
        store.create_task("ivan-daily", body, revision)


def test_read_does_not_modify_registry(tmp_path):
    path = _write_registry(tmp_path, "ivan-daily-tasks.json", _registry([_task()]))
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    TaskRegistryStore(tmp_path).get_registry("ivan-daily")

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_registry_symlink_cannot_disclose_or_mutate_target(tmp_path, monkeypatch):
    victim = tmp_path / "outside.json"
    victim.write_text(json.dumps(_registry([_task()])), encoding="utf-8")
    link = tmp_path / "linked-tasks.json"
    link.symlink_to(victim)
    store = TaskRegistryStore(tmp_path)

    assert store.list_registries() == []
    monkeypatch.setattr(store, "_discover", lambda: {"linked": link})
    with pytest.raises(RegistryValidationError):
        store.get_registry("linked")
    before = victim.read_bytes()
    with pytest.raises(RegistryValidationError):
        store.create_task("linked", {"text": "must not land"}, hashlib.sha256(before).hexdigest())
    assert victim.read_bytes() == before


def test_discovery_rejects_symlink_swapped_in_at_open(tmp_path, monkeypatch):
    path = _write_registry(tmp_path, "swapped-tasks.json", _registry())
    victim = tmp_path / "outside.json"
    victim.write_text(json.dumps(_registry([_task()])), encoding="utf-8")
    original_open = task_registries.os.open
    swapped = False

    def swap_at_open(target, flags, *args, **kwargs):
        nonlocal swapped
        if Path(target) == path and not swapped:
            swapped = True
            path.unlink()
            path.symlink_to(victim)
        return original_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(task_registries.os, "open", swap_at_open)
    assert TaskRegistryStore(tmp_path).list_registries() == []
    assert swapped is True


def test_lock_and_backup_directory_reject_preexisting_symlinks(tmp_path):
    path = _write_registry(tmp_path, "ivan-daily-tasks.json", _registry())
    store = TaskRegistryStore(tmp_path)
    revision = store.get_registry("ivan-daily")["revision"]
    lock_target = tmp_path / "lock-target"
    lock_target.write_bytes(b"unchanged")
    (tmp_path / ".task-registry.lock").symlink_to(lock_target)

    with pytest.raises(task_registries.RegistryError):
        store.create_task("ivan-daily", {"text": "blocked"}, revision)
    assert lock_target.read_bytes() == b"unchanged"
    assert json.loads(path.read_text(encoding="utf-8"))["tasks"] == []

    (tmp_path / ".task-registry.lock").unlink()
    backup_target = tmp_path / "backup-target"
    backup_target.mkdir()
    (tmp_path / ".task-registry-backups").symlink_to(backup_target, target_is_directory=True)
    with pytest.raises(task_registries.RegistryError):
        store.create_task("ivan-daily", {"text": "blocked"}, revision)
    assert list(backup_target.iterdir()) == []
    assert json.loads(path.read_text(encoding="utf-8"))["tasks"] == []


def test_lock_replacement_after_open_fails_closed(tmp_path, monkeypatch):
    path = _write_registry(tmp_path, "ivan-daily-tasks.json", _registry())
    store = TaskRegistryStore(tmp_path)
    revision = store.get_registry("ivan-daily")["revision"]
    original_flock = task_registries.fcntl.flock
    replaced = False

    def replace_after_lock(fd, operation):
        nonlocal replaced
        original_flock(fd, operation)
        if operation == task_registries.fcntl.LOCK_EX and not replaced:
            replaced = True
            lock_path = tmp_path / ".task-registry.lock"
            replacement = tmp_path / ".replacement-lock"
            replacement.write_bytes(b"")
            replacement.replace(lock_path)

    monkeypatch.setattr(task_registries.fcntl, "flock", replace_after_lock)
    with pytest.raises(task_registries.RegistryError, match="changed while locking"):
        store.create_task("ivan-daily", {"text": "blocked"}, revision)

    assert replaced is True
    assert json.loads(path.read_text(encoding="utf-8"))["tasks"] == []


def test_external_atomic_write_at_exchange_boundary_is_rolled_back_as_conflict(tmp_path, monkeypatch):
    path = _write_registry(tmp_path, "ivan-daily-tasks.json", _registry())
    store = TaskRegistryStore(tmp_path)
    revision = store.get_registry("ivan-daily")["revision"]
    external = _registry([_task()])
    original_exchange = task_registries._rename_exchange
    exchanges = 0

    def inject_then_exchange(first, second):
        nonlocal exchanges
        exchanges += 1
        if exchanges == 1:
            external_path = tmp_path / ".external.tmp"
            external_path.write_text(json.dumps(external) + "\n", encoding="utf-8")
            external_path.replace(path)
        return original_exchange(first, second)

    monkeypatch.setattr(task_registries, "_rename_exchange", inject_then_exchange)
    with pytest.raises(RegistryConflict):
        store.create_task("ivan-daily", {"text": "WebUI write"}, revision)

    assert exchanges == 2
    assert json.loads(path.read_text(encoding="utf-8")) == external


def test_malformed_external_atomic_write_at_exchange_boundary_is_restored(tmp_path, monkeypatch):
    path = _write_registry(tmp_path, "ivan-daily-tasks.json", _registry())
    store = TaskRegistryStore(tmp_path)
    revision = store.get_registry("ivan-daily")["revision"]
    original_exchange = task_registries._rename_exchange
    exchanges = 0

    def inject_then_exchange(first, second):
        nonlocal exchanges
        exchanges += 1
        if exchanges == 1:
            external_path = tmp_path / ".external.tmp"
            external_path.write_bytes(b"external malformed bytes\n")
            external_path.replace(path)
        return original_exchange(first, second)

    monkeypatch.setattr(task_registries, "_rename_exchange", inject_then_exchange)
    with pytest.raises(RegistryConflict):
        store.create_task("ivan-daily", {"text": "WebUI write"}, revision)

    assert exchanges == 2
    assert path.read_bytes() == b"external malformed bytes\n"


def test_newer_external_write_at_rollback_boundary_remains_canonical(tmp_path, monkeypatch):
    path = _write_registry(tmp_path, "ivan-daily-tasks.json", _registry())
    store = TaskRegistryStore(tmp_path)
    revision = store.get_registry("ivan-daily")["revision"]
    first_external = _registry([_task()])
    newest_external = _registry([_task(), _task()])
    original_exchange = task_registries._rename_exchange
    exchanges = 0

    def inject_at_both_boundaries(first, second):
        nonlocal exchanges
        exchanges += 1
        if exchanges == 1:
            external_path = tmp_path / ".external-one.tmp"
            external_path.write_text(json.dumps(first_external) + "\n", encoding="utf-8")
            external_path.replace(path)
        elif exchanges == 2:
            external_path = tmp_path / ".external-two.tmp"
            external_path.write_text(json.dumps(newest_external) + "\n", encoding="utf-8")
            external_path.replace(path)
        return original_exchange(first, second)

    monkeypatch.setattr(task_registries, "_rename_exchange", inject_at_both_boundaries)
    with pytest.raises(RegistryConflict):
        store.create_task("ivan-daily", {"text": "WebUI write"}, revision)

    assert exchanges == 3
    assert json.loads(path.read_text(encoding="utf-8")) == newest_external


def test_repeated_rollback_replacements_preserve_newest_displaced_version(tmp_path, monkeypatch):
    path = _write_registry(tmp_path, "ivan-daily-tasks.json", _registry())
    store = TaskRegistryStore(tmp_path)
    revision = store.get_registry("ivan-daily")["revision"]
    original_exchange = task_registries._rename_exchange
    injected_payloads = []

    def replace_before_every_exchange(first, second):
        sequence = len(injected_payloads) + 1
        external = {**_registry([_task()]), "updated_at": f"2026-08-02T12:00:{sequence:02d}+03:00"}
        injected_payloads.append(external)
        external_path = tmp_path / f".external-{sequence}.tmp"
        external_path.write_text(json.dumps(external) + "\n", encoding="utf-8")
        external_path.replace(path)
        return original_exchange(first, second)

    monkeypatch.setattr(task_registries, "_rename_exchange", replace_before_every_exchange)
    with pytest.raises(task_registries.RegistryError, match="newest displaced version preserved"):
        store.create_task("ivan-daily", {"text": "WebUI write"}, revision)

    recovery_files = list(tmp_path.glob(".ivan-daily-tasks.json.conflict-recovery.*.json"))
    assert len(recovery_files) == 1
    assert json.loads(recovery_files[0].read_text(encoding="utf-8")) == injected_payloads[-1]
    assert all(task.get("text") != "WebUI write" for task in json.loads(path.read_text())["tasks"])


def test_recovery_rename_failure_keeps_newest_displaced_temp(tmp_path, monkeypatch):
    path = _write_registry(tmp_path, "ivan-daily-tasks.json", _registry())
    store = TaskRegistryStore(tmp_path)
    revision = store.get_registry("ivan-daily")["revision"]
    original_exchange = task_registries._rename_exchange
    injected_payloads = []

    def replace_before_every_exchange(first, second):
        sequence = len(injected_payloads) + 1
        external = {**_registry([_task()]), "updated_at": f"2026-08-02T12:01:{sequence:02d}+03:00"}
        injected_payloads.append(external)
        external_path = tmp_path / f".rename-failure-external-{sequence}.tmp"
        external_path.write_text(json.dumps(external) + "\n", encoding="utf-8")
        external_path.replace(path)
        return original_exchange(first, second)

    monkeypatch.setattr(task_registries, "_rename_exchange", replace_before_every_exchange)
    monkeypatch.setattr(task_registries.os, "rename", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("rename failed")))
    with pytest.raises(OSError, match="rename failed"):
        store.create_task("ivan-daily", {"text": "WebUI write"}, revision)

    preserved = list(tmp_path.glob(".ivan-daily-tasks.json.*.tmp"))
    assert len(preserved) == 1
    assert json.loads(preserved[0].read_text(encoding="utf-8")) == injected_payloads[-1]


def test_mutation_fails_closed_when_atomic_exchange_is_unavailable(tmp_path, monkeypatch):
    path = _write_registry(tmp_path, "ivan-daily-tasks.json", _registry())
    store = TaskRegistryStore(tmp_path)
    revision = store.get_registry("ivan-daily")["revision"]
    monkeypatch.setattr(task_registries, "_rename_exchange", lambda _first, _second: False)

    with pytest.raises(task_registries.RegistryError, match="compare-and-swap is unavailable"):
        store.create_task("ivan-daily", {"text": "must not replace"}, revision)

    assert json.loads(path.read_text(encoding="utf-8"))["tasks"] == []


def test_route_helpers_list_create_update_and_reject_stale_write(tmp_path, monkeypatch):
    existing = _task()
    _write_registry(tmp_path, "ivan-daily-tasks.json", _registry([existing]))
    monkeypatch.setenv("HERMES_TASK_REGISTRY_DIR", str(tmp_path))
    responses: list[tuple[int, dict]] = []

    def fake_j(_handler, payload, status=200, **_kwargs):
        responses.append((status, payload))
        return True

    def fake_bad(_handler, message, status=400, **_kwargs):
        responses.append((status, {"error": message}))
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(routes, "bad", fake_bad)
    handler = SimpleNamespace()

    assert routes._handle_task_registry_get(handler, urlparse("/api/task-registries")) is True
    assert responses[-1][0] == 200
    assert responses[-1][1]["registries"][0]["id"] == "ivan-daily"

    routes._handle_task_registry_get(handler, urlparse("/api/task-registries/ivan-daily"))
    current = responses[-1][1]
    routes._handle_task_registry_post(
        handler,
        urlparse("/api/task-registries/ivan-daily/tasks"),
        {
            "expected_revision": current["revision"],
            "task": {"text": "Created through WebUI", "priority": "normal"},
        },
    )
    assert responses[-1][0] == 201
    created = responses[-1][1]

    routes._handle_task_registry_post(
        handler,
        urlparse(
            "/api/task-registries/ivan-daily/tasks/"
            + created["task"]["id"]
            + "/update"
        ),
        {
            "expected_revision": created["revision"],
            "changes": {"status": "in_progress"},
        },
    )
    assert responses[-1][0] == 200
    assert responses[-1][1]["task"]["status"] == "in_progress"

    routes._handle_task_registry_post(
        handler,
        urlparse(
            "/api/task-registries/ivan-daily/tasks/" + existing["id"] + "/update"
        ),
        {
            "expected_revision": current["revision"],
            "changes": {"status": "completed"},
        },
    )
    assert responses[-1][0] == 409


def test_frontend_contains_registry_mode_and_keeps_cron_mode():
    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "panels.js").read_text(encoding="utf-8")

    assert 'id="taskSourceRegistryTab"' in html
    assert 'id="taskSourceCronTab"' in html
    assert 'id="cronTaskDetailView"' in html
    assert 'id="taskRegistryMain"' in html
    assert "function switchTaskSource(source)" in js
    assert "if (_taskSource === 'cron') return loadCrons();" in js
    assert "/api/task-registries/" in js
    assert "expected_revision" in js


def _panel_function(source: str, declaration: str, next_declaration: str) -> str:
    start = source.index(declaration)
    end = source.index(next_declaration, start)
    return source[start:end]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_frontend_uses_registry_timezone_for_datetime_local_roundtrip():
    source = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(encoding="utf-8")
    functions = _panel_function(
        source,
        "function _taskRegistryDateTimeInput",
        "function _taskRegistryDeadline",
    )
    script = f"""
let _taskRegistryData = {{timezone: 'America/Los_Angeles'}};
{functions}
const utc = _taskRegistryAwareDateTime('2026-01-15T12:00');
if (!utc.ok || utc.value !== '2026-01-15T20:00:00.000Z') throw new Error('wrong UTC conversion: ' + JSON.stringify(utc));
const local = _taskRegistryDateTimeInput(utc.value);
if (local !== '2026-01-15T12:00') throw new Error('wrong local conversion: ' + local);
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_frontend_rejects_dst_gap_and_overlap():
    source = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(encoding="utf-8")
    functions = _panel_function(source, "function _taskRegistryDateTimeInput", "function _taskRegistryDeadline")
    script = f"""
let _taskRegistryData = {{timezone: 'America/New_York'}};
const _taskRegistryText = (_key, fallback) => fallback;
{functions}
const gap = _taskRegistryAwareDateTime('2026-03-08T02:30');
if (gap.ok || !gap.error) throw new Error('spring-forward gap accepted: ' + JSON.stringify(gap));
const overlap = _taskRegistryAwareDateTime('2026-11-01T01:30');
if (overlap.ok || !overlap.error || !overlap.error.includes('twice')) throw new Error('fall-back overlap accepted: ' + JSON.stringify(overlap));
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_frontend_date_only_deadline_is_formatted_in_utc():
    source = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(encoding="utf-8")
    function = _panel_function(source, "function _taskRegistryDeadline", "function _taskRegistryFormHtml")
    script = f"""
let _taskRegistryData = {{timezone:'Pacific/Kiritimati'}};
const _taskRegistryText = (_key, fallback) => fallback;
Date.prototype.toLocaleDateString = function(_locale, options) {{
  if (!options || options.timeZone !== 'UTC') throw new Error('date-only rendering is not pinned to UTC');
  return 'stable-date';
}};
{function}
if (_taskRegistryDeadline({{due_date:'2026-01-15'}}) !== 'stable-date') throw new Error('date-only deadline changed');
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_background_refresh_does_not_destroy_draft_opened_in_flight():
    source = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(encoding="utf-8")
    function = _panel_function(source, "async function loadTaskRegistry", "async function loadTaskRegistries")
    script = f"""
let _taskRegistryData = {{revision:'old'}};
let _activeTaskRegistryId = 'sample';
let draftOpen = false;
const api = async () => {{ draftOpen = true; return {{revision:'fresh', tasks:[]}}; }};
const _taskRegistryHasOpenDraft = () => draftOpen;
const $ = () => null;
const _taskRegistryText = (_key, fallback) => fallback;
const _taskRegistrySetError = () => {{}};
const renderTaskRegistryScopes = () => {{ throw new Error('background refresh rerendered scopes'); }};
const renderTaskRegistryTasks = () => {{ throw new Error('background refresh destroyed draft'); }};
{function}
(async () => {{
  await loadTaskRegistry('sample', true, true);
  if (_taskRegistryData.revision !== 'old') throw new Error('background refresh replaced data under draft');
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_frontend_409_refreshes_revision_without_destroying_draft():
    source = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(encoding="utf-8")
    function = _panel_function(
        source,
        "async function _mutateTaskRegistry",
        "async function createTaskRegistryTask",
    )
    script = f"""
let _taskRegistryData = {{revision: 'old', tasks: [{{id:'task', status:'pending'}}]}};
let _taskRegistrySaving = false;
let _activeTaskRegistryId = 'sample';
const draft = {{value: 'unsaved draft'}};
let calls = 0;
const api = async path => {{
  calls++;
  if (calls === 1) {{ const error = new Error('conflict'); error.status = 409; throw error; }}
  if (path !== '/api/task-registries/sample') throw new Error('wrong refresh path: ' + path);
  return {{revision:'fresh', tasks:[{{id:'task', status:'blocked'}}]}};
}};
const $ = id => id === 'draft' ? draft : null;
const _taskRegistrySetError = () => {{}};
{function}
(async () => {{
  const result = await _mutateTaskRegistry('/test', {{}});
  if (result !== false || _taskRegistryData.revision !== 'fresh') throw new Error('409 did not refresh data');
  if (draft.value !== 'unsaved draft') throw new Error('draft was destroyed');
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_frontend_failed_status_update_reverts_to_persisted_status():
    source = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(encoding="utf-8")
    bind = _panel_function(source, "function _bindTaskRegistryContent", "function renderTaskRegistryScopes")
    update = _panel_function(source, "async function updateTaskRegistryTask", "function _syncTaskSourceUi")
    script = f"""
let _taskRegistryData = {{revision:'r', tasks:[{{id:'task-1', status:'pending'}}]}};
let _activeTaskRegistryId = 'sample';
let changeHandler;
const status = {{value:'completed', addEventListener:(_name, fn) => {{ changeHandler = fn; }}}};
const card = {{dataset:{{taskId:'task-1'}}, querySelector:selector => selector === '[data-task-status]' ? status : null}};
const content = {{querySelectorAll:selector => selector === '[data-task-form]' ? [] : [card]}};
const $ = id => id === 'taskRegistryContent' ? content : null;
const _mutateTaskRegistry = async () => false;
const showToast = () => {{}};
const _taskRegistryText = (_key, fallback) => fallback;
{bind}
{update}
(async () => {{
  _bindTaskRegistryContent();
  await changeHandler();
  if (status.value !== 'pending') throw new Error('status did not revert: ' + status.value);
  const ok = await updateTaskRegistryTask('task-1', {{status:'completed'}});
  if (ok !== false) throw new Error('updateTaskRegistryTask did not return failure');
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
