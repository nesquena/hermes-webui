"""Tests for cron model override features."""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

import pytest

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


def test_cron_update_preserves_provider_only_pin_when_model_cleared():
    """Clearing the picker to "Default" must not wipe a provider-only pin.

    #4030 intentionally made "clearing the picker = no overrides", which is
    correct for model+provider pins but silently destroyed provider-only
    pins (jobs that pin a provider with no model, e.g. self-hosted LLM
    endpoints). The combined picker cannot represent that state, so
    saveCronForm must preserve prev.provider when the job carried no model
    AND the user never changed the picker; a deliberate return to Default
    must clear it.
    """
    save_body = _function_body("saveCronForm")
    helper_body = _function_body("_cronProviderForClear")

    # The clear branch delegates to the helper and records picker changes.
    assert "_cronProviderForClear(_cronPreFormDetail, _cronModelPickerTouched)" in save_body
    assert "preserveProvider" not in save_body

    # The helper distinguishes an explicit clear (pickerTouched) from an
    # untouched provider-only pin.
    assert "if (pickerTouched) return null" in helper_body
    assert "prev.model == null" in helper_body

    # The form entry points reset the touched flag.
    for fn in ("openCronCreate", "openCronEdit", "duplicateCurrentCron"):
        assert "_cronModelPickerTouched = false" in _function_body(fn)
    # The picker records deliberate changes.
    pop_body = _function_body("_populateCronFormModelSelect")
    assert "_cronModelPickerTouched = true" in pop_body


def test_cron_provider_for_clear_behavior():
    """Exercise _cronProviderForClear through node for both pin shapes."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")

    # Locate the full declaration (signature + body) of the helper.
    src = PANELS_JS
    start = src.find("function _cronProviderForClear(")
    assert start != -1, "helper declaration not found"
    brace = src.find("{", start)
    depth = 0
    end = None
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    decl = src[start:end]

    driver = f"""
{decl}
const cases = [
  ['provider_only_untouched', {{model: null, provider: 'custom:llama-swap'}}, false],
  ['provider_only_touched',   {{model: null, provider: 'custom:llama-swap'}}, true],
  ['model_pin_touched',       {{model: 'gpt-5', provider: 'openai'}}, true],
  ['no_override_untouched',   {{model: null, provider: null}}, false],
  ['no_prev',                 null, false],
];
const out = {{}};
for (const [name, detail, touched] of cases) out[name] = _cronProviderForClear(detail, touched) ?? null;
process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run([node, "-e", driver], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    r = json.loads(proc.stdout)

    # Untouched picker on a provider-only job: the pin survives.
    assert r["provider_only_untouched"] == "custom:llama-swap"
    # Deliberate return to Default: "Default = no overrides" wins.
    assert r["provider_only_touched"] is None
    # Model-pinned job, explicit clear: provider removed too.
    assert r["model_pin_touched"] is None
    # No overrides stored, untouched picker: nothing to preserve.
    assert r["no_override_untouched"] is None
    # No pre-form snapshot (create form): no provider.
    assert r["no_prev"] is None
