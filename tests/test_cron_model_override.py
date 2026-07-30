"""Tests for cron model override features."""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")


class _JSONHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.response_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass


def _payload(handler):
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _function_body(name: str) -> str:
    marker = f"function {name}("
    start = PANELS_JS.find(marker)
    assert start != -1, f"{name} not found"
    paren = PANELS_JS.find("(", start)
    assert paren != -1, f"{name} params not found"
    depth = 0
    for idx in range(paren, len(PANELS_JS)):
        ch = PANELS_JS[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                brace = PANELS_JS.find("{", idx)
                break
    else:
        raise AssertionError(f"{name} params did not terminate")
    assert brace != -1, f"{name} body not found"
    depth = 0
    for idx in range(brace, len(PANELS_JS)):
        ch = PANELS_JS[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return PANELS_JS[brace + 1 : idx]
    raise AssertionError(f"{name} body did not terminate")


def test_cron_create_forwards_model_and_provider(monkeypatch):
    import api.routes as routes

    created = {"id": "job-model-override", "prompt": "override test", "schedule": "every 1h"}
    calls = []
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.create_job = lambda **kwargs: calls.append(("create", kwargs)) or {**created, **kwargs}
    cron_jobs.update_job = lambda job_id, updates: calls.append(("update", job_id, updates)) or {**created, **updates}
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)

    handler = _JSONHandler()
    routes._handle_cron_create(
        handler,
        {
            "prompt": "override test",
            "schedule": "every 1h",
            "model": "my-custom-model",
            "provider": "my-provider",
        },
    )

    assert handler.status == 200
    assert calls[0][0] == "create"
    assert calls[0][1]["model"] == "my-custom-model"
    assert calls[0][1]["provider"] == "my-provider"


def test_cron_update_allows_overwriting_and_clearing_model_provider(monkeypatch):
    import api.routes as routes

    calls = []
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.update_job = lambda job_id, updates: calls.append(("update", job_id, updates)) or {"id": job_id, **updates}
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)

    # 1. Update model & provider
    handler = _JSONHandler()
    routes._handle_cron_update(
        handler,
        {
            "job_id": "test-job",
            "model": "new-model",
            "provider": "new-provider",
        },
    )
    assert handler.status == 200
    assert calls[0] == ("update", "test-job", {"model": "new-model", "provider": "new-provider"})

    # 2. Clear model & provider overrides to default
    handler = _JSONHandler()
    routes._handle_cron_update(
        handler,
        {
            "job_id": "test-job",
            "model": None,
            "provider": None,
        },
    )
    assert handler.status == 200
    assert calls[1] == ("update", "test-job", {"model": None, "provider": None})


def test_cron_panels_form_structure_and_population():
    render_body = _function_body("_renderCronForm")
    save_body = _function_body("saveCronForm")
    edit_body = _function_body("openCronEdit")
    duplicate_body = _function_body("duplicateCurrentCron")

    # Check that model element is added to the HTML template in panels.js
    assert "cronFormModel" in render_body
    assert "cron_model_label" in render_body

    # Check that _populateCronFormModelSelect is called in _renderCronForm
    assert "_populateCronFormModelSelect" in render_body

    # Check that openCronEdit and duplicateCurrentCron pass model and provider overrides to _renderCronForm
    assert "model" in edit_body
    assert "provider" in edit_body
    assert "model" in duplicate_body
    assert "provider" in duplicate_body

    # Check saveCronForm parses and submits model/provider
    assert "cronFormModel" in save_body
    assert "updates.model" in save_body or "body.model" in save_body
    assert "const modelLoaded = !!(modelEl && modelEl.dataset.loaded === '1')" in save_body
    assert "selectedModel && modelLoaded" in save_body
    assert "else if (modelLoaded)" in save_body
    assert "_cronPreFormDetail.provider || null" in save_body


def test_cron_model_picker_marks_loaded_only_after_successful_population():
    body = _function_body("_populateCronFormModelSelect")

    assert "delete sel.dataset.loaded" in body
    assert "sel.dataset.loaded = '1'" in body
    assert "} finally {" not in body
    try_block = body.split("} catch (e)", 1)[0]
    catch_block = body.split("} catch (e)", 1)[1]
    assert "sel.dataset.loaded = '1'" in try_block
    assert "sel.dataset.loaded = '1'" not in catch_block
