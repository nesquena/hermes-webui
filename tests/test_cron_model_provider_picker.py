"""Coverage for model-aware cron job creation/editing in the WebUI."""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


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


def test_cron_create_persists_model_provider_pair(monkeypatch):
    import api.routes as routes

    created = {"id": "job-model", "name": "Model aware", "prompt": "ping"}
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
            "prompt": "ping",
            "schedule": "every 1h",
            "model": "gpt-5.4",
            "provider": "openai-codex",
        },
    )

    assert handler.status == 200
    assert calls[0] == (
        "create",
        {
            "prompt": "ping",
            "schedule": "every 1h",
            "name": None,
            "deliver": "local",
            "skills": [],
            "model": "gpt-5.4",
            "provider": "openai-codex",
        },
    )
    assert _payload(handler)["job"]["provider"] == "openai-codex"
    assert _payload(handler)["job"]["model"] == "gpt-5.4"


def test_cron_create_omits_blank_model_provider_to_use_profile_default(monkeypatch):
    import api.routes as routes

    calls = []
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.create_job = lambda **kwargs: calls.append(("create", kwargs)) or {"id": "job-default", **kwargs}
    cron_jobs.update_job = lambda job_id, updates: calls.append(("update", job_id, updates)) or {"id": job_id, **updates}
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)

    handler = _JSONHandler()
    routes._handle_cron_create(
        handler,
        {
            "prompt": "ping",
            "schedule": "every 1h",
            "model": "",
            "provider": "",
        },
    )

    assert handler.status == 200
    assert calls[0][1]["model"] is None
    assert calls[0][1]["provider"] is None


def test_cron_update_can_set_and_clear_model_provider_pair(monkeypatch):
    import api.routes as routes

    calls = []
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")

    def update_job(job_id, updates):
        calls.append((job_id, updates))
        return {"id": job_id, "name": "Updated", **updates}

    cron_jobs.update_job = update_job
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)

    set_handler = _JSONHandler()
    routes._handle_cron_update(
        set_handler,
        {"job_id": "job-model", "model": "gpt-5.4", "provider": "openai-codex"},
    )
    assert set_handler.status == 200

    clear_handler = _JSONHandler()
    routes._handle_cron_update(clear_handler, {"job_id": "job-model", "model": "", "provider": ""})
    assert clear_handler.status == 200

    assert calls == [
        ("job-model", {"model": "gpt-5.4", "provider": "openai-codex"}),
        ("job-model", {"model": None, "provider": None}),
    ]


def test_cron_form_has_model_picker_and_saves_model_provider_state():
    render_body = _function_body("_renderCronForm")
    save_body = _function_body("saveCronForm")
    edit_body = _function_body("openCronEdit")
    detail_body = _function_body("_renderCronDetail")

    picker_body = _function_body("_populateCronFormModelSelect")

    assert "cronFormModel" in render_body
    assert "cron_model_label" in render_body
    assert "cron_model_use_default" in picker_body
    assert "_populateCronFormModelSelect" in render_body
    assert "_modelStateForSelect" in save_body
    assert "updates.model" in save_body
    assert "updates.provider" in save_body
    assert "body.model" in save_body
    assert "body.provider" in save_body
    assert "model: job.model" in edit_body
    assert "provider: job.provider" in edit_body
    assert "cron_model_use_default" in detail_body


def test_cron_model_picker_i18n_keys_exist():
    assert "cron_model_label" in I18N_JS
    assert "cron_model_use_default" in I18N_JS
    assert "cron_model_hint" in I18N_JS
