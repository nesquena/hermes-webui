"""Regression coverage for atomic, serialized projects.json mutations."""

import json
import multiprocessing
import threading
import time

import pytest


def _isolate_projects(tmp_path, monkeypatch):
    import api.models as models

    projects_file = tmp_path / "projects.json"
    monkeypatch.setattr(models, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(models, "_projects_migrated", True)
    monkeypatch.setattr(models, "_PROJECTS_THREAD_LOCK", threading.RLock())
    return models, projects_file


def test_mutate_projects_serializes_concurrent_threads(tmp_path, monkeypatch):
    models, projects_file = _isolate_projects(tmp_path, monkeypatch)
    first_inside = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()

    def first_mutation():
        def _mutate(projects):
            projects.append({"project_id": "first"})
            first_inside.set()
            assert release_first.wait(timeout=5)
            return "first", True

        models.mutate_projects(_mutate)

    def second_mutation():
        second_started.set()

        def _mutate(projects):
            projects.append({"project_id": "second"})
            return "second", True

        models.mutate_projects(_mutate)

    first = threading.Thread(target=first_mutation)
    second = threading.Thread(target=second_mutation)
    first.start()
    assert first_inside.wait(timeout=5)
    second.start()
    assert second_started.wait(timeout=5)
    assert second.is_alive(), "second mutation must wait for the first transaction"
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    saved = json.loads(projects_file.read_text(encoding="utf-8"))
    assert [project["project_id"] for project in saved] == ["first", "second"]


def test_mutate_projects_serializes_separate_processes(tmp_path, monkeypatch):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("requires fork to preserve isolated module paths")

    models, projects_file = _isolate_projects(tmp_path, monkeypatch)
    ctx = multiprocessing.get_context("fork")
    first_inside = ctx.Event()
    release_first = ctx.Event()

    def first_mutation():
        def _mutate(projects):
            projects.append({"project_id": "mcp"})
            first_inside.set()
            if not release_first.wait(timeout=5):
                raise TimeoutError("parent did not release first transaction")
            return "mcp", True

        models.mutate_projects(_mutate)

    def second_mutation():
        def _mutate(projects):
            projects.append({"project_id": "webui"})
            return "webui", True

        models.mutate_projects(_mutate)

    first = ctx.Process(target=first_mutation)
    second = ctx.Process(target=second_mutation)
    first.start()
    assert first_inside.wait(timeout=5)
    second.start()
    time.sleep(0.1)
    assert second.is_alive(), "second process must wait on the projects file lock"
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert first.exitcode == 0
    assert second.exitcode == 0
    saved = json.loads(projects_file.read_text(encoding="utf-8"))
    assert [project["project_id"] for project in saved] == ["mcp", "webui"]


def test_atomic_write_failure_preserves_existing_projects(tmp_path, monkeypatch):
    models, projects_file = _isolate_projects(tmp_path, monkeypatch)
    original = [{"project_id": "existing", "name": "Existing"}]
    projects_file.write_text(json.dumps(original), encoding="utf-8")

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(models.os, "replace", fail_replace)

    def _mutate(projects):
        projects.append({"project_id": "new", "name": "New"})
        return "new", True

    with pytest.raises(OSError, match="simulated replace failure"):
        models.mutate_projects(_mutate)

    assert json.loads(projects_file.read_text(encoding="utf-8")) == original
    assert list(tmp_path.glob(".projects.json.*.tmp")) == []
